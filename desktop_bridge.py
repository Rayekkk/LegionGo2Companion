# SPDX-License-Identifier: BSD-3-Clause
"""Loopback-only second frontend. No second hardware owner or settings store.

The deliberately small HTTP subset accepts one request per connection. Every
RPC runs on Decky's loop through the already guarded public Plugin methods.
Disconnects/timeouts never cancel a hardware transaction already admitted.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import secrets
import diagnostics
import module_runtime

MAX_BODY = 64 * 1024
MAX_CLIENTS = 24
READ_SECONDS = 5
RPC_SECONDS = 150


class DesktopBridge:
    def __init__(self, plugin, methods, context=None, overrides=None, read_logs=None):
        self.plugin = plugin
        self.methods = frozenset(methods)
        self.context = context
        self.overrides = overrides or {}
        self.read_logs = read_logs
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.port = 0
        self.clients = set()
        self.operations = set()
        self.closing = False

    @property
    def origin(self):
        return f'http://127.0.0.1:{self.port}'

    async def start(self):
        if self.server:
            return
        self.server = await asyncio.start_server(self._client, '127.0.0.1', 0, limit=8192)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self):
        self.closing = True
        if self.server:
            self.server.close()
            # On current Python, wait_closed includes active connections.
            # Closing transport does not cancel an admitted hardware operation.
            for writer in tuple(self.clients):
                writer.close()
            await self.server.wait_closed()
            self.server = None
        for writer in tuple(self.clients):
            writer.close()
        # Mutation safety belongs to module_runtime; don't cancel on UI exit.
        if self.operations:
            await asyncio.gather(*tuple(self.operations), return_exceptions=True)

    async def dispatch(self, method, args):
        if self.closing:
            raise RuntimeError('Desktop connection closed. Reopen the app after Decky starts.')
        if not isinstance(method, str) or not isinstance(args, list) or len(args) > 12:
            raise ValueError('Invalid RPC request.')
        if method == 'desktop_context' and self.context and not args:
            return await self.context()
        if method == 'desktop_get_logs' and self.read_logs and not args:
            return await self.read_logs()
        if method not in self.methods or method.startswith('_'):
            raise ValueError('RPC is not available in Desktop Mode.')
        function = self.overrides.get(method) or getattr(self.plugin, method, None)
        if not callable(function) or not inspect.iscoroutinefunction(function):
            raise ValueError('RPC unavailable.')
        inspect.signature(function).bind(*args)
        with diagnostics.source_context('desktop'):
            return await function(*args)

    async def _client(self, reader, writer):
        if self.closing or len(self.clients) >= MAX_CLIENTS:
            diagnostics.event('desktop', 'client_capacity_rejected', level='warning', repeat=60)
            writer.close()
            await writer.wait_closed()
            return
        self.clients.add(writer)
        try:
            await self._request(reader, writer)
        except (ValueError, UnicodeError, RecursionError, asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            diagnostics.event('desktop', 'malformed_request', level='warning', repeat=60)
            await self._reply(writer, 400, {'error': 'Malformed request.'})
        except asyncio.TimeoutError:
            diagnostics.event('desktop', 'request_deadline', level='warning', repeat=60)
            await self._reply(writer, 408, {'error': 'Request timed out. Refresh before retrying a change.'})
        except (ConnectionError, OSError):
            pass
        finally:
            self.clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _request(self, reader, writer):
        headers_bytes = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), READ_SECONDS)
        if len(headers_bytes) > 8192:
            raise ValueError('Headers too long')
        lines = headers_bytes.decode('ascii').split('\r\n')
        verb, path, version = lines[0].split(' ')
        if version != 'HTTP/1.1':
            raise ValueError('HTTP version')
        headers = {}
        for line in lines[1:-2]:
            key, value = line.split(':', 1)
            key = key.lower()
            if key in headers or not key or any(c.isspace() for c in key):
                raise ValueError('Duplicate header')
            headers[key] = value.strip()
        if headers.get('host') != f'127.0.0.1:{self.port}':
            diagnostics.event('desktop', 'request_rejected', level='warning', reason='host', repeat=60)
            await self._reply(writer, 403, {'error': 'Unexpected host.'})
            return
        if ('transfer-encoding' in headers or '?' in path or '#' in path
                or headers.get('sec-fetch-site') not in (None, 'same-origin', 'none')):
            await self._reply(writer, 403, {'error': 'Unexpected request source.'})
            return
        length = headers.get('content-length', '0')
        if not length.isascii() or not length.isdecimal() or len(length) > 6:
            raise ValueError('Length')
        length = int(length)
        if length > MAX_BODY:
            await self._reply(writer, 413, {'error': 'Request too large.'})
            return
        if verb != 'POST' or path != '/rpc':
            await self._reply(writer, 404, {'error': 'Not found.'})
            return
        authorization = headers.get('authorization', '')
        if (headers.get('origin') != self.origin or
                not secrets.compare_digest(authorization, 'Bearer ' + self.token)):
            diagnostics.event('desktop', 'request_rejected', level='warning', reason='session_or_origin', repeat=60)
            await self._reply(writer, 403, {'error': 'Session expired. Reopen Legion Go Companion.'})
            return
        if headers.get('content-type') != 'application/json' or not length:
            raise ValueError('Content type')
        data = json.loads(await asyncio.wait_for(reader.readexactly(length), READ_SECONDS))
        if not isinstance(data, dict) or set(data) != {'method', 'args'}:
            raise ValueError('RPC shape')
        if len(self.operations) >= MAX_CLIENTS:
            diagnostics.event('desktop', 'operation_capacity_rejected', level='warning', repeat=60)
            await self._reply(writer, 429, {'error': 'Too many pending requests. Wait and refresh.'})
            return
        method = data['method'] if isinstance(data['method'], str) and data['method'] in self.methods | {'desktop_context', 'desktop_get_logs'} else 'unavailable'
        with diagnostics.operation('desktop', method):
            await self._run_operation(data, writer)

    async def _run_operation(self, data, writer):
        operation = asyncio.create_task(self.dispatch(data['method'], data['args']))
        self.operations.add(operation)
        def finished(task):
            self.operations.discard(task)
            if not task.cancelled():
                task.exception()  # A timed-out client may have gone away.
        operation.add_done_callback(finished)
        try:
            result = await asyncio.wait_for(asyncio.shield(operation), RPC_SECONDS)
            await self._reply(writer, 200, {'result': result})
        except (ValueError, TypeError, RuntimeError) as exc:
            if not isinstance(exc, module_runtime.AdmissionClosed):
                diagnostics.event('desktop', 'rpc_rejected', level='warning', error=exc, repeat=60)
            await self._reply(writer, 409, {'error': str(exc)[:1000]})
        except asyncio.TimeoutError:
            diagnostics.event('desktop', 'rpc_deadline', level='warning', outcome='refresh_required', repeat=60)
            raise
        except Exception as exc:
            diagnostics.event('desktop', 'dispatch_failed', level='error', error=exc, repeat=60)
            await self._reply(writer, 500, {'error': 'The backend could not complete this request. Refresh its state.'})

    async def _reply(self, writer, status, data):
        data = json.dumps(data, allow_nan=False).encode('utf-8')
        response = (f'HTTP/1.1 {status} Response\r\nContent-Type: application/json; charset=utf-8\r\n'
                    f'Content-Length: {len(data)}\r\nConnection: close\r\n'
                    'Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n'
                    'Cross-Origin-Resource-Policy: same-origin\r\n\r\n').encode('ascii')
        try:
            writer.write(response + data)
            await asyncio.wait_for(writer.drain(), READ_SECONDS)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            pass

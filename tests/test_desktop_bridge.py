# SPDX-License-Identifier: BSD-3-Clause
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from desktop_bridge import DesktopBridge, MAX_BODY


class Plugin:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def get_version(self):
        return {'version': 'test', 'blocked': False}

    async def change(self, value):
        self.calls.append(value)
        self.entered.set()
        await self.release.wait()
        return {'saved': value}


class DesktopBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin = Plugin()
        async def context():
            return {'app_id': '123', 'gamescope': False}
        self.bridge = DesktopBridge(self.plugin, ['get_version', 'change'], context)
        await self.bridge.start()

    async def asyncTearDown(self):
        self.plugin.release.set()
        await self.bridge.close()

    async def eventually(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), 3)

    async def request(self, method='get_version', args=None, origin=None, token=None, path='/rpc', verb='POST', extras='', body=None, host=None):
        body = json.dumps({'method': method, 'args': args or []}).encode() if body is None else body
        reader, writer = await asyncio.open_connection('127.0.0.1', self.bridge.port)
        headers = (f'{verb} {path} HTTP/1.1\r\nHost: {host or "127.0.0.1:" + str(self.bridge.port)}\r\n'
                   f'Origin: {origin or self.bridge.origin}\r\nAuthorization: Bearer {token if token is not None else self.bridge.token}\r\n'
                   f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n{extras}\r\n').encode()
        writer.write(headers + body)
        await writer.drain()
        result = await reader.read()
        writer.close()
        await writer.wait_closed()
        return result

    async def test_loopback_only_random_port_and_capability(self):
        self.assertEqual(self.bridge.server.sockets[0].getsockname()[0], '127.0.0.1')
        self.assertEqual(len(self.bridge.token), 43)
        result = await self.request()
        self.assertIn(b'HTTP/1.1 200', result)
        self.assertIn(b'"version": "test"', result)
        self.assertIn(b'Cross-Origin-Resource-Policy: same-origin', result)
        self.assertIn(b'Cache-Control: no-store', result)
        self.assertNotIn(self.bridge.token.encode(), result)

    async def test_unauthorized_and_cross_origin_cannot_mutate(self):
        for options in ({'token': ''}, {'token': 'wrong'}, {'origin': 'http://evil.invalid'},
                        {'host': 'evil.invalid'}, {'extras': 'Sec-Fetch-Site: cross-site\r\n'}):
            with self.subTest(options=options):
                self.assertIn(b'HTTP/1.1 403', await self.request('change', [1], **options))
        self.assertEqual(self.plugin.calls, [])

    async def test_default_deny_and_argument_validation(self):
        for name, args in [('_unload', []), ('__getattribute__', ['change']), ('unknown', []), ('change', [1,2])]:
            self.assertIn(b'HTTP/1.1 409', await self.request(name, args))
        self.assertEqual(self.plugin.calls, [])

    async def test_no_get_mutations_or_preflight(self):
        self.assertIn(b'HTTP/1.1 404', await self.request('change', [1], verb='GET'))
        self.assertIn(b'HTTP/1.1 404', await self.request(verb='OPTIONS'))
        self.assertEqual(self.plugin.calls, [])

    async def test_no_web_frontend_or_static_file_server(self):
        for path in ('/', '/app.js', '/../main.py', '/%2e%2e/main.py', '/main.py', '/app.js.map'):
            self.assertIn(b'HTTP/1.1 404', await self.request(path=path, verb='GET', body=b''))
        self.assertIn(b'HTTP/1.1 403', await self.request(path='/?token=leak', verb='GET', body=b''))

    async def test_malformed_duplicate_and_oversized_requests(self):
        self.assertIn(b'HTTP/1.1 400', await self.request(body=b'[]'))
        self.assertIn(b'HTTP/1.1 400', await self.request(body=b'{'))
        self.assertIn(b'HTTP/1.1 400', await self.request(extras='Content-Length: 1\r\n'))
        self.assertIn(b'HTTP/1.1 403', await self.request(extras='Transfer-Encoding: chunked\r\n'))
        self.assertIn(b'HTTP/1.1 413', await self.request(body=b'x' * (MAX_BODY + 1)))

    async def test_non_finite_numbers_never_reach_the_backend(self):
        for constant in (b'NaN', b'Infinity', b'-Infinity'):
            with self.subTest(constant=constant):
                body = b'{"method": "change", "args": [' + constant + b']}'
                self.assertIn(b'HTTP/1.1 400', await self.request(body=body))
        self.assertEqual(self.plugin.calls, [])

    async def test_context_is_read_only(self):
        result = await self.request('desktop_context')
        self.assertIn(b'"app_id": "123"', result)
        self.assertEqual(self.plugin.calls, [])

    async def test_logs_use_explicit_read_only_callback_and_same_authentication(self):
        calls=[]
        async def logs():
            calls.append(True)
            return {'events':[], 'warnings':['journal_unavailable'], 'truncated':False}
        self.bridge.read_logs=logs
        self.assertIn(b'HTTP/1.1 403',await self.request('desktop_get_logs',token='invalid'))
        self.assertFalse(calls)
        self.assertIn(b'HTTP/1.1 409',await self.request('desktop_get_logs',['/etc/shadow']))
        self.assertFalse(calls)
        result=await self.request('desktop_get_logs')
        self.assertIn(b'HTTP/1.1 200',result)
        self.assertIn(b'journal_unavailable',result)
        self.assertEqual(calls,[True]);self.assertEqual(self.plugin.calls,[])

    async def test_disconnected_client_and_close_drain_admitted_transaction(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.bridge.port)
        body = b'{"method":"change","args":[42]}'
        writer.write((f'POST /rpc HTTP/1.1\r\nHost: 127.0.0.1:{self.bridge.port}\r\nOrigin: {self.bridge.origin}\r\n'
                      f'Authorization: Bearer {self.bridge.token}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body)
        await writer.drain()
        await asyncio.wait_for(self.plugin.entered.wait(), 3)
        writer.close()
        await writer.wait_closed()
        closing = asyncio.create_task(self.bridge.close())
        await self.eventually(lambda: self.bridge.closing)
        self.assertFalse(closing.done(), 'unload must wait for admitted hardware work')
        self.plugin.release.set()
        await closing
        self.assertEqual(self.plugin.calls, [42])
        with self.assertRaises(RuntimeError):
            await self.bridge.dispatch('change', [43])

    async def test_idle_connected_client_does_not_delay_server_shutdown(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.bridge.port)
        try:
            await self.eventually(lambda: bool(self.bridge.clients))
            self.assertTrue(self.bridge.clients)
            await asyncio.wait_for(self.bridge.close(), 1)
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_close_disconnects_connected_ui_but_finishes_its_admitted_write(self):
        request = asyncio.create_task(self.request('change', [99]))
        await asyncio.wait_for(self.plugin.entered.wait(), 3)
        closing = asyncio.create_task(self.bridge.close())
        try:
            self.assertEqual(await asyncio.wait_for(asyncio.shield(request), 1), b'')
            self.assertFalse(closing.done())
            self.assertEqual(self.plugin.calls, [99])
        finally:
            self.plugin.release.set()
            await closing
            await request

    def test_rpc_contract_matches_shared_frontend(self):
        import re
        import inspect
        import support
        import main
        root = Path(__file__).resolve().parents[1]
        expected = set()
        for file in (root / 'src').glob('*.tsx'):
            expected.update(re.findall(r'\b(?:callable|rpc)\s*<[\s\S]*?>\s*\(\s*"([a-z_]+)"',
                                       file.read_text(encoding='utf-8')))
        for method in expected:
            self.assertTrue(inspect.iscoroutinefunction(getattr(main.Plugin, method, None)), method)
        # Same exclusions as scripts/desktop-contract.mjs (Steam app hints, RPCs the app never calls).
        expected -= {'set_active_app', 'vibe_set_active_app', 'is_ready'}
        expected |= {'list_game_profiles'}  # read-only, shown only by the native app
        self.assertTrue(inspect.iscoroutinefunction(main.Plugin.list_game_profiles))
        contract = root / 'desktop/native/rpc-methods.json'
        self.assertEqual(set(json.loads(contract.read_text())), expected)
        # The method set may grow with a legitimate shared feature. Compare
        # the actual generated contract, not a frozen count of today's RPCs.
        self.assertTrue({'firmware_get_status', 'firmware_check', 'firmware_download', 'firmware_install'} <= expected)
        self.assertNotIn('firmware_check_updates', expected)


if __name__ == '__main__':
    unittest.main()

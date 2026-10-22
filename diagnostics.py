# SPDX-License-Identifier: BSD-3-Clause
"""Best-effort diagnostic events, never an admission or hardware safety gate.

The backend queues bounded JSON records to a private rotating file. Detached
workers send finite lifecycle/fault events directly to journald without waiting
for Decky, a pipe reader, a filesystem or a network service. Import has no I/O.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import datetime
import json
import logging
import logging.handlers
import math
import os
from pathlib import Path
import queue
import re
import socket
import stat
import sys
import threading
import time
import uuid

# Root-owned and outside ~/homebrew: Decky chowns its logs tree to the desktop
# user on every plugin load, which the owner checks below rightly refuse.
LOG_DIR = '/var/log/legiongo2companion'
MAX_BYTES = 1024 * 1024
BACKUPS = 2
QUEUE_SIZE = 256
MAX_RECORD = 4096
_FIELDS = frozenset(('operation', 'stage', 'outcome', 'reason', 'scope', 'attribute',
    'store', 'phase', 'component', 'job_id', 'worker_id', 'errno', 'returncode',
    'duration_ms', 'pending', 'enabled', 'restored', 'source', 'count', 'retryable',
    'supported', 'version', 'code', 'status', 'bytes', 'repeated', 'kernel', 'python', 'architecture',
    'model', 'bios', 'ec', 'steamos', 'steamos_build', 'decky', 'ready', 'failed',
    'spl', 'sppt', 'fppt', 'mode', 'previous'))
_IDENTITY = ('store', 'attribute', 'operation', 'stage', 'scope', 'component', 'source')
_LEVELS = {'debug': 10, 'info': 20, 'warning': 30, 'error': 40, 'critical': 50}
_operation = contextvars.ContextVar('companion_diagnostic_operation', default=None)
_source = contextvars.ContextVar('companion_diagnostic_source', default='decky')
_method = contextvars.ContextVar('companion_diagnostic_method', default=None)
_session = uuid.uuid4().hex[:12]
_sink = None
_writer = None
_legacy = None
_filter = None
_asyncio_filter = None
_seen = collections.OrderedDict()
_early = collections.deque(maxlen=64)  # Import/__init__/_migration records until configure() has a sink.
# Reentrant: a garbage-collected failed task is logged (_AsyncioFilter -> event) on whatever thread
# allocated, possibly inside another event's deduplication; a plain Lock would freeze that thread.
_lock = threading.RLock()
_HOME = os.path.dirname(os.path.abspath(__file__))


def safe_text(value, limit=256):
    """Defence in depth for text, not permission to log arguments or settings."""
    value = str(value)[:4096]
    value = re.sub(r'https?://[^\s<>\"\']+', '[url]', value, flags=re.I)
    value = re.sub(r'Bearer\s+\S+', 'Bearer [redacted]', value, flags=re.I)
    value = re.sub(r'(?i)\b(password|passwd|psk|token|secret|authorization|cookie|ssid|serial|uuid|app_?id)\b\s*[:=]\s*[^\r\n]+',
                   r'\1=[redacted]', value)
    value = re.sub(r'(?:[A-Za-z]:[\\/]|/(?:home|Users|root)/)[^\s\"\']+', '[private-path]', value)
    value = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[address]', value)
    value = re.sub(r'(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', '[id]', value)
    return re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', value)[:limit]


def _name(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value) else 'unknown'


def _ours(error):
    module = sys.modules.get(type(error).__module__)
    return os.path.dirname(os.path.abspath(getattr(module, '__file__', None) or os.sep)) == _HOME


def _error(error):
    if isinstance(error, BaseException):
        # Exception strings can include a whole command, credential or packet.
        # Keep type, errno and frame locations; never locals, argv or source lines.
        # Only this plugin's own exception classes carry product text worth a message.
        result = {'type': type(error).__name__}
        if isinstance(getattr(error, 'errno', None), int):
            result['errno'] = error.errno
        if _ours(error):
            result['message'] = safe_text(error, 160)
        frames, tb = [], error.__traceback__
        while tb is not None:
            frames.append({'file': safe_text(Path(tb.tb_frame.f_code.co_filename).name, 80),
                           'function': safe_text(tb.tb_frame.f_code.co_name, 80), 'line': tb.tb_lineno})
            tb = tb.tb_next
        if frames:
            result['frames'] = frames[-8:]
        causes, cause = [], error.__cause__ or error.__context__
        while cause is not None and cause is not error and len(causes) < 3:
            item = {'type': type(cause).__name__}
            if isinstance(getattr(cause, 'errno', None), int):
                item['errno'] = cause.errno
            causes.append(item)
            cause = cause.__cause__ or cause.__context__
        if causes:
            result['causes'] = causes
        return result
    if error:
        return {'message': safe_text(error, 512)}
    return None


def _fields(fields):
    return {key: safe_text(value) if isinstance(value, str) else value
            for key, value in fields.items() if key in _FIELDS and
            (value is None or type(value) in (bool, int, str) or
             type(value) is float and math.isfinite(value))}


def _context(fields):
    return _fields({'source': _source.get(), 'operation': _method.get(), **fields})


def _key(component, action, fields):
    return (component, action, *(fields.get(key) for key in _IDENTITY))


def _encode(record):
    text = json.dumps(record, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    if len(text) > MAX_RECORD:
        record = dict(record)
        record['context'] = {key: value[:64] if isinstance(value, str) else value
                             for key, value in record.get('context', {}).items()}
        record['truncated'] = True
        text = json.dumps(record, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
        while len(text) > MAX_RECORD and record['context']:
            record['context'].popitem()
            text = json.dumps(record, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
        if len(text) > MAX_RECORD:
            record['error'] = {'type': record.get('error', {}).get('type', 'DiagnosticTextTruncated')}
            text = json.dumps(record, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    return text


def _fallback(text, level):
    try:
        logger = _legacy
        if logger is None:
            import decky
            logger = decky.logger
        getattr(logger, level if level != 'critical' else 'error')(text)
    except Exception:
        pass


def event(component, action, /, *, level='info', error=None, repeat=0, **fields):
    """Emit a safe scalar summary. Logging failure never changes the operation."""
    try:
        if level == 'debug':
            return  # No per-attribute/sensor/RPC success traffic in normal logs.
        level = level if level in _LEVELS else 'info'
        context = _context(fields)
        details = _error(error)
        suppressed = 0
        if repeat:
            key = ('repeat', *_key(component, action, context), level,
                   json.dumps(details, sort_keys=True))
            now = time.monotonic()
            with _lock:
                previous = _seen.pop(key, None)
                if previous and now - previous[0] < repeat:
                    _seen[key] = (previous[0], previous[1] + 1)
                    return
                suppressed = previous[1] if previous else 0
                _seen[key] = (now, 0)
                while len(_seen) > 256:
                    _seen.popitem(last=False)
        record = {'time': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds'),
                  'level': level, 'component': _name(component), 'event': _name(action),
                  'pid': os.getpid(), 'session': _session}
        operation = _operation.get()
        if operation:
            record['operation_id'] = operation
        if context:
            record['context'] = context
        if details:
            record['error'] = details
        if suppressed:
            record['suppressed'] = suppressed
        text = _encode(record)
        sink = _sink
        if sink is not None:
            sink(text, level)
        elif _early is not None:
            # Never synchronously call an arbitrary Decky handler from a
            # hardware/asyncio transaction; configure() writes these later.
            _early.append((text, level))
    except Exception:
        pass


def transition(component, action, state, /, *, level='info', error=None, initial=True, **fields):
    """Record a changed state, including one recovery, without poll-time spam.

    initial=False: a first healthy (info) state is only a baseline, not news;
    a failure is still recorded at once, and so is the recovery after it.
    """
    try:
        key = ('state', *_key(component, action, _context(fields)))
        value = (safe_text(state, 128), type(error).__name__ if isinstance(error, BaseException) else safe_text(error or '', 128),
                 getattr(error, 'errno', None))
        now = time.monotonic()
        repeated = 0
        with _lock:
            previous = _seen.pop(key, None)
            if previous is None and not initial and level not in ('warning', 'error', 'critical'):
                _seen[key] = (value, now, 0)
                return
            if previous and previous[0] == value:
                if level not in ('warning', 'error', 'critical'):
                    _seen[key] = previous
                    return
                repeated = previous[2] + 1
                if now - previous[1] < 60:
                    _seen[key] = (value, previous[1], repeated)
                    return
            _seen[key] = (value, now, 0)
            while len(_seen) > 256:
                _seen.popitem(last=False)
        if repeated:
            fields = {**fields, 'repeated': repeated}
        event(component, action, level=level, error=error, **{**fields, 'status': str(state)})
    except Exception:
        pass


@contextlib.contextmanager
def operation(source=None, name='operation'):
    """Correlate one RPC and its to_thread work; nested calls keep its owner."""
    existing = _operation.get()
    token = None if existing else _operation.set(uuid.uuid4().hex[:12])
    method = _method.set(name) if not existing else None
    origin = _source.set(source) if source is not None else None
    try:
        yield _operation.get()
    finally:
        if token is not None:
            _operation.reset(token)
        if method is not None:
            _method.reset(method)
        if origin is not None:
            _source.reset(origin)


@contextlib.contextmanager
def source_context(source):
    token = _source.set(_name(source))
    try:
        yield
    finally:
        _source.reset(token)


class UnsafeLogPath(OSError):
    pass


class _PrivateHandler(logging.handlers.RotatingFileHandler):
    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0)
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or hasattr(os, 'geteuid') and info.st_uid != os.geteuid():
                raise UnsafeLogPath('Unsafe diagnostic log file.')
            if hasattr(os, 'fchmod'):
                os.fchmod(fd, 0o600)
            return os.fdopen(fd, 'a', encoding='utf-8')
        except BaseException:
            os.close(fd)
            raise

    def doRollover(self):
        # close() can raise (ENOSPC flush) after the stream is already closed.
        # Detach first so a later emit reopens instead of failing forever.
        stream, self.stream = self.stream, None
        if stream is not None:
            stream.close()
        super().doRollover()

    def handleError(self, record):
        raise OSError('Diagnostic file write unavailable.') from None


class _Writer:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise UnsafeLogPath('Diagnostic log directory is not a real directory.')
        if hasattr(os, 'geteuid') and info.st_uid != os.geteuid():
            raise UnsafeLogPath('Diagnostic log directory has an unexpected owner.')
        os.chmod(directory, 0o700)
        for name in ('events.jsonl', 'events.jsonl.1', 'events.jsonl.2'):
            path = directory / name
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or hasattr(os, 'geteuid') and info.st_uid != os.geteuid():
                    raise UnsafeLogPath('Diagnostic log history is not a private regular file.')
        self.handler = _PrivateHandler(directory/'events.jsonl', maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding='utf-8')
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self.queue = queue.Queue(QUEUE_SIZE)
        self.stop = threading.Event()
        self.dropped = 0
        self.thread = threading.Thread(target=self.run, name='CompanionDiagnosticLog', daemon=True)
        self.thread.start()

    def emit(self, text, level):
        try:
            self.queue.put_nowait((text, level))
        except queue.Full:
            self.dropped += 1

    def run(self):
        failed = False
        try:
            while not self.stop.is_set() or not self.queue.empty():
                try:
                    text, level = self.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                try:
                    if self.dropped:
                        count, self.dropped = self.dropped, 0
                        summary = json.dumps({'time': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds'),
                            'level': 'warning', 'component': 'diagnostics', 'event': 'diagnostic_events_dropped',
                            'context': {'count': count}, 'pid': os.getpid(), 'session': _session})
                        self.handler.emit(logging.LogRecord('companion', logging.WARNING, '', 0, summary, (), None))
                    self.handler.emit(logging.LogRecord('companion', _LEVELS[level], '', 0, text, (), None))
                    if failed:
                        _fallback('Companion diagnostic file is writable again.', 'info')
                    failed = False
                except Exception:
                    if not failed:
                        _fallback('Companion diagnostic file is unavailable; hardware controls continue.', 'warning')
                    failed = True
                finally:
                    self.queue.task_done()
        finally:
            self.handler.close()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)


class _LegacyFilter(logging.Filter):
    def filter(self, record):
        try:
            record.msg, record.args = safe_text(record.getMessage(), 1500), ()
            # Third-party traceback formatting can expose source/argument data.
            if record.exc_info:
                record.msg += ' ' + json.dumps(_error(record.exc_info[1]))
                record.exc_info = None
            record.exc_text = None
        except Exception:
            record.msg, record.args, record.exc_info = 'Companion diagnostic message unavailable.', (), None
        return True


def _from_decky_socket(error):
    frame = error.__traceback__
    while frame is not None:
        if os.path.basename(frame.tb_frame.f_code.co_filename) == 'localsocket.py':
            return True
        frame = frame.tb_next
    return False


class _AsyncioFilter(logging.Filter):
    def filter(self, record):
        if record.levelno < logging.ERROR:
            return True
        # asyncio's default exception handler includes Task repr/context in the
        # message, even for a shielded future whose exception was retrieved.
        # Preserve that handler and its delivery, but never format its text.
        error = record.exc_info[1] if record.exc_info else None
        if isinstance(error, asyncio.CancelledError):
            return False  # A callback cancelled during shutdown (e.g. Decky's RPC listener at EOF) is not an error.
        if isinstance(error, (BrokenPipeError, ConnectionResetError)) and _from_decky_socket(error):
            return False  # Decky closed its RPC socket while stopping the plugin; there is nothing to recover.
        event('runtime', 'asyncio_exception', level='error', error=error, repeat=60)
        record.msg = 'Companion asynchronous task error; see diagnostic events.'
        record.args, record.exc_info, record.exc_text, record.stack_info = (), None, None, None
        return True


def configure(directory, logger=None):
    """Start once from the backend, outside hardware locks. Failure is advisory."""
    global _writer, _sink, _legacy, _filter, _asyncio_filter
    if _writer is not None:
        return True
    _legacy = logger
    if _asyncio_filter is None:
        _asyncio_filter = _AsyncioFilter()
        logging.getLogger('asyncio').addFilter(_asyncio_filter)
    target = logger.logger if isinstance(logger, logging.LoggerAdapter) else logger
    if isinstance(target, logging.Logger) and _filter is None:
        _filter = _LegacyFilter()
        target.addFilter(_filter)
    try:
        _writer = _Writer(directory)
        _sink = _writer.emit
        _flush_early()
        return True
    except Exception as exc:
        # OSError text names the path; our own checks say which one failed.
        reason = str(exc) if isinstance(exc, UnsafeLogPath) else type(exc).__name__ + (
            ': ' + exc.strerror if isinstance(getattr(exc, 'strerror', None), str) else '')
        _fallback('Companion diagnostic file could not start (' + safe_text(reason, 160)
                  + '); events go to the system journal.', 'warning')
        if directory is not None:
            configure_worker('backend')  # Nonblocking journal fallback, never a synchronous producer handler.
            _flush_early()
            event('diagnostics', 'file_sink_unavailable', level='warning', error=exc)
        return False


def _flush_early():
    global _early
    early, _early = _early, None
    for text, level in early or ():
        try:
            _sink(text, level)
        except Exception:
            pass


def close():
    global _writer, _sink, _asyncio_filter
    writer, _writer = _writer, None
    _sink = None
    if writer is not None:
        try:
            writer.close()
        except Exception:
            pass
    if _asyncio_filter is not None:
        logging.getLogger('asyncio').removeFilter(_asyncio_filter)
        _asyncio_filter = None


def ensure_fallback():
    """Uninstall may run after normal unload has already closed the file sink."""
    if _sink is None:
        configure_worker('backend')


def configure_worker(component):
    """Nonblocking native-journal datagrams; no dependence on redirected stderr."""
    global _sink
    _source.set('worker')
    identifier = 'legiongo2companion-' + _name(component)
    def journal(text, level):
        priority = '3' if level in ('error', 'critical') else '4' if level == 'warning' else '6'
        data = ('SYSLOG_IDENTIFIER='+identifier+'\nPRIORITY='+priority+'\nMESSAGE='+text+'\n').encode('utf-8')
        # A fresh nonblocking datagram also tolerates journald restart and fork;
        # never retain a descriptor shared between the two EC safety owners.
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as stream:
            stream.setblocking(False)
            stream.sendto(data, '/run/systemd/journal/socket')
    _sink = journal

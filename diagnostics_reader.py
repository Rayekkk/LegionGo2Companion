# SPDX-License-Identifier: BSD-3-Clause
"""Read a finite, private snapshot of Companion diagnostics for the desktop UI.

No caller-controlled command, filename, journal match or message is accepted.
This reader has no write, polling, hardware, settings or lifecycle side effects.
"""
from __future__ import annotations

import datetime
import heapq
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time

from diagnostics import MAX_BYTES, MAX_RECORD, safe_text
from system_process import system_env

MAX_EVENTS = 1000
MAX_RESPONSE_BYTES = 400 * 1024
JOURNAL_BYTES = 6 * 1024 * 1024
JOURNAL_STDERR_BYTES = 16 * 1024
JOURNAL_SECONDS = 3.0  # Plus at most .5 seconds to reap this read-only child.
_IDENTIFIERS = {f'legiongo2companion-{name}': name for name in ('fan', 'wifi', 'firmware', 'backend')}
_JOURNAL_ARGV = ('/usr/bin/journalctl', '--since', '7 days ago', '-n', '1000',
                 '--output=json', '--no-pager', '--quiet',
                 *(item for name in _IDENTIFIERS for item in ('-t', name)), '_UID=0')
_NAME = re.compile(r'[A-Za-z0-9_.-]{1,80}\Z')
_LEVELS = frozenset(('debug', 'info', 'warning', 'error', 'critical'))
_CONTEXT = frozenset(('operation', 'stage', 'outcome', 'reason', 'scope', 'attribute',
    'store', 'phase', 'component', 'job_id', 'worker_id', 'errno', 'returncode',
    'duration_ms', 'pending', 'enabled', 'restored', 'source', 'count', 'retryable',
    'supported', 'version', 'code', 'status', 'bytes', 'repeated', 'kernel', 'python', 'architecture'))


def _integer(value):
    return type(value) is int and -(2 ** 63) <= value < 2 ** 63


def _exception(value):
    """Only exception identity and code locations; never formatted messages."""
    if not isinstance(value, dict):
        return None
    result = {}
    if isinstance(value.get('type'), str) and _NAME.fullmatch(value['type']):
        result['type'] = value['type']
    if _integer(value.get('errno')):
        result['errno'] = value['errno']
    frames = []
    for frame in value.get('frames', [])[-8:] if isinstance(value.get('frames'), list) else ():
        if not isinstance(frame, dict):
            continue
        name, function, line = frame.get('file'), frame.get('function'), frame.get('line')
        if (isinstance(name, str) and _NAME.fullmatch(name)
                and isinstance(function, str) and _NAME.fullmatch(function)
                and _integer(line) and line > 0):
            frames.append({'file': name, 'function': function, 'line': line})
    if frames:
        result['frames'] = frames
    causes = []
    for cause in value.get('causes', [])[:3] if isinstance(value.get('causes'), list) else ():
        if isinstance(cause, dict) and isinstance(cause.get('type'), str) and _NAME.fullmatch(cause['type']):
            item = {'type': cause['type']}
            if _integer(cause.get('errno')):
                item['errno'] = cause['errno']
            causes.append(item)
    if causes:
        result['causes'] = causes
    return result or None


def normalize_event(value, source):
    """Canonical schema shared by file and worker records, not a raw-log API."""
    if not isinstance(value, dict) or source not in ('backend', 'fan', 'wifi', 'firmware'):
        return None
    stamp = value.get('time')
    if not isinstance(stamp, str) or len(stamp) > 48:
        return None
    try:
        moment = datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            return None
        stamp = moment.astimezone(datetime.timezone.utc).isoformat(timespec='milliseconds')
    except (ValueError, OverflowError):
        return None
    level, component, action = value.get('level'), value.get('component'), value.get('event')
    if (not isinstance(level, str) or level not in _LEVELS or not isinstance(component, str) or not _NAME.fullmatch(component)
            or not isinstance(action, str) or not _NAME.fullmatch(action)):
        return None
    result = {'time': stamp, 'level': level, 'component': component, 'event': action, 'source': source}
    for name in ('pid', 'suppressed'):
        if _integer(value.get(name)) and value[name] >= 0:
            result[name] = value[name]
    for name in ('session', 'operation_id'):
        if isinstance(value.get(name), str) and _NAME.fullmatch(value[name]):
            result[name] = value[name]
    if value.get('truncated') is True:
        result['truncated'] = True
    context = value.get('context')
    if isinstance(context, dict):
        result['context'] = {name: safe_text(item) if isinstance(item, str) else item
            for name, item in context.items() if name in _CONTEXT and
            (item is None or type(item) in (bool, str) or _integer(item)
             or type(item) is float and math.isfinite(item))}
    error = _exception(value.get('error'))
    if error:
        result['error'] = error
    return result


def _owned(info, *, directory=False, private=True, owners=None):
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        return False
    if not directory and info.st_nlink != 1:
        return False
    if os.name == 'posix':
        if info.st_uid not in (owners if owners is not None else {os.geteuid()}):
            return False
        if info.st_mode & (0o077 if private else 0o022):
            return False
    return True


def _read_fd(fd, owners=None):
    info = os.fstat(fd)
    if not _owned(info, owners=owners):
        raise PermissionError('unsafe')
    trimmed = info.st_size > MAX_BYTES
    if trimmed:
        os.lseek(fd, -MAX_BYTES, os.SEEK_END)
    remaining, parts = MAX_BYTES, []
    while remaining:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        parts.append(chunk)
    data = b''.join(parts)
    if trimmed:
        data = data.partition(b'\n')[2]  # The tail can begin inside a record.
    return data, trimmed


def _file_snapshots(log_directory, warnings):
    if not isinstance(log_directory, (str, os.PathLike)):
        warnings.add('backend_missing')
        return [], False
    base_fd = directory_fd = None
    owners = None
    snapshots, accessible = [], False
    try:
        base = Path(log_directory)
        if os.name == 'posix':
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            base_fd = os.open(base, flags)
            base_info = os.fstat(base_fd)
            # Decky creates its SDK-provided log directory as the desktop user
            # even when the plugin process runs as root. Trust that fixed SDK
            # base owner, plus our UID for files produced before/after reload;
            # no UI argument can select this directory or an additional owner.
            owners = {base_info.st_uid, os.geteuid()}
            if not _owned(base_info, directory=True, private=False, owners=owners):
                raise PermissionError('unsafe')
            directory_fd = os.open('diagnostics', flags, dir_fd=base_fd)
            if not _owned(os.fstat(directory_fd), directory=True, owners=owners):
                raise PermissionError('unsafe')
        else:
            child = base / 'diagnostics'
            if base.is_symlink() or child.is_symlink() or not _owned(child.lstat(), directory=True):
                raise PermissionError('unsafe')
        for name in ('events.jsonl.2', 'events.jsonl.1', 'events.jsonl'):
            fd = None
            try:
                if directory_fd is not None:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
                else:
                    path = child / name
                    if path.is_symlink() or not _owned(path.lstat()):
                        raise PermissionError('unsafe')
                    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
                snapshots.append(_read_fd(fd, owners))
                accessible = True
            except FileNotFoundError:
                pass  # Rotation can move a fixed history file during a snapshot.
            except OSError:
                warnings.add('backend_unsafe')
            finally:
                if fd is not None:
                    os.close(fd)
        if not accessible:
            warnings.add('backend_missing')
    except FileNotFoundError:
        warnings.add('backend_missing')
    except (OSError, ValueError):
        warnings.add('backend_unsafe')
    finally:
        for fd in (directory_fd, base_fd):
            if fd is not None:
                os.close(fd)
    return snapshots, accessible


def _query_journal():
    """Bound both pipes while reading; communicate(capture_output) is unbounded."""
    if os.name != 'posix':
        return b'', {'journal_unavailable'}, False, False
    worker = selector = None
    data, stderr_bytes = bytearray(), 0
    warnings, truncated = set(), False
    deadline = time.monotonic() + JOURNAL_SECONDS
    try:
        selector = selectors.DefaultSelector()
        worker = subprocess.Popen(_JOURNAL_ARGV, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=system_env(LC_ALL='C'), close_fds=True)
        for stream in (worker.stdout, worker.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                warnings.add('journal_timeout')
                break
            ready = selector.select(min(.1, remaining))
            for key, _ in ready:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is worker.stdout:
                    capacity = JOURNAL_BYTES - len(data)
                    data.extend(chunk[:capacity])
                    exceeded = len(chunk) > capacity
                else:
                    stderr_bytes += len(chunk)
                    exceeded = stderr_bytes > JOURNAL_STDERR_BYTES
                if exceeded:
                    warnings.add('journal_output_limit')
                    truncated = True
                    break
            if truncated:
                break
        if not warnings:
            try:
                worker.wait(timeout=max(.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                warnings.add('journal_timeout')
            if worker.returncode not in (None, 0):
                warnings.add('journal_unavailable')
            elif stderr_bytes:
                warnings.add('journal_partial')
        available = worker.returncode == 0 and not warnings.intersection({'journal_timeout', 'journal_output_limit'})
        return bytes(data), warnings, truncated, available
    except (OSError, ValueError):
        return bytes(data), {'journal_unavailable'}, truncated, False
    finally:
        if selector is not None:
            selector.close()
        if worker is not None:
            if worker.poll() is None:
                worker.kill()  # Only this fixed, read-only journalctl child.
            try:
                worker.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                pass
            for stream in (worker.stdout, worker.stderr):
                if stream is not None:
                    stream.close()


def _lines(data, warnings, *, maximum=MAX_RECORD):
    if data and not data.endswith(b'\n'):
        warnings.add('partial_records')
        data = data.rpartition(b'\n')[0]
    for line in data.splitlines():
        if not line.strip():
            continue
        if len(line) > maximum:
            warnings.add('malformed_records')
            continue
        try:
            yield json.loads(line)
        except (ValueError, RecursionError, UnicodeError):
            warnings.add('malformed_records')


def read_events(log_directory, *, limit=MAX_EVENTS):
    """Return newest normalized events in chronological order, within 400 KiB."""
    if type(limit) is not int:
        limit = MAX_EVENTS
    limit = max(1, min(MAX_EVENTS, limit))
    warnings, sources, newest, sequence = set(), set(), [], 0
    truncated = False

    def add(value, source):
        nonlocal sequence, truncated
        record = normalize_event(value, source)
        if record is None:
            warnings.add('malformed_records')
            return
        item = (record['time'], sequence, record)
        sequence += 1
        if len(newest) < limit:
            heapq.heappush(newest, item)
        else:
            heapq.heappushpop(newest, item)
            truncated = True

    snapshots, accessible = _file_snapshots(log_directory, warnings)
    if accessible:
        sources.add('backend')
    for data, trimmed in snapshots:
        truncated |= trimmed
        for value in _lines(data, warnings):
            add(value, 'backend')
    journal, journal_warnings, journal_truncated, journal_available = _query_journal()
    warnings.update(journal_warnings)
    truncated |= journal_truncated
    if journal_available:
        sources.update(_IDENTIFIERS.values())
    for entry in _lines(journal, warnings, maximum=16384):
        if not isinstance(entry, dict):
            warnings.add('malformed_records')
            continue
        identifier, message = entry.get('SYSLOG_IDENTIFIER'), entry.get('MESSAGE')
        source = _IDENTIFIERS.get(identifier) if isinstance(identifier, str) else None
        if not source or entry.get('_UID') != '0' or not isinstance(message, str):
            continue
        if len(message.encode('utf-8')) > MAX_RECORD:
            warnings.add('malformed_records')
            continue
        try:
            add(json.loads(message), source)
        except (ValueError, RecursionError):
            warnings.add('malformed_records')
    events = [item[2] for item in sorted(newest)]
    result = {'events': events, 'available_sources': sorted(sources),
              'warnings': sorted(warnings), 'truncated': bool(truncated)}
    # Budget against json.dumps' larger default spacing, not just compact JSON.
    sizes = [len(json.dumps(row, ensure_ascii=True).encode('utf-8')) + 2 for row in events]
    total = len(json.dumps({**result, 'events': []}).encode('utf-8')) + sum(sizes)
    while total > MAX_RESPONSE_BYTES and events:
        total -= sizes.pop(0)
        events.pop(0)
        result['truncated'] = True
    return result

# SPDX-License-Identifier: BSD-3-Clause
"""Bounded atomic transaction journal, not a resume instruction.

Filesystem implementation is POSIX-only: private owner directory, anchored
O_NOFOLLOW dirfds, flock single-writer, private regular files and fsync+replace.
The independent worker uses it for durable intent and outcome evidence.
Recovered records are display-only and cannot resume or repeat a write.
"""
import copy
import json
import os
from pathlib import Path
import re
import stat
import uuid

from controller_firmware import FirmwareError, release_for, version_number, MIN_IMAGE_BYTES, MAX_IMAGE_BYTES

MAX_JOURNAL = 16 * 1024
PHASES = frozenset(('idle', 'ready_start', 'ready_metadata', 'ready_boundary',
    'ready_block', 'ready_finish', 'awaiting_send', 'awaiting_reply',
    'awaiting_completion', 'verifying_version', 'succeeded', 'failed_before_write', 'uncertain'))
FIELDS = frozenset(('schema', 'job_id', 'component', 'image_sha256', 'offered_version',
    'total_bytes', 'phase', 'write_attempted', 'sent_bytes', 'acknowledged_bytes',
    'sequence', 'boundary', 'device_binding', 'connection_epoch', 'verification_token',
    'error', 'journal_error'))


def _text(value, maximum):
    return isinstance(value, str) and len(value) <= maximum and '\x00' not in value


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or set(snapshot) != FIELDS:
        raise FirmwareError('Invalid firmware journal schema.')
    release = release_for(snapshot['component'])
    version_number(snapshot['offered_version'])
    total = snapshot['total_bytes']
    if (type(snapshot['schema']) is not int or snapshot['schema'] != 1 or
            not isinstance(snapshot['job_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', snapshot['job_id']) or
            not isinstance(snapshot['image_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',snapshot['image_sha256']) or
            type(total) is not int or not MIN_IMAGE_BYTES <= total <= MAX_IMAGE_BYTES or
            not isinstance(snapshot['phase'], str) or snapshot['phase'] not in PHASES or
            type(snapshot['write_attempted']) is not bool or type(snapshot['journal_error']) is not bool):
        raise FirmwareError('Firmware journal identity or state is invalid.')
    for key in ('sent_bytes', 'acknowledged_bytes', 'sequence'):
        if type(snapshot[key]) is not int or not 0 <= snapshot[key] <= total:
            raise FirmwareError('Firmware journal counters are invalid.')
    if snapshot['acknowledged_bytes'] > snapshot['sent_bytes']:
        raise FirmwareError('Firmware journal acknowledgements exceed transmitted bytes.')
    boundary = snapshot['boundary']
    if boundary is not None and (type(boundary) is not int or not 1 <= boundary <= 0x7fffffff):
        raise FirmwareError('Firmware journal boundary is invalid.')
    binding = snapshot['device_binding']
    if binding != '' and (not isinstance(binding, str) or not re.fullmatch(r'[0-9a-f]{64}', binding)):
        raise FirmwareError('Firmware journal device identity is invalid.')
    token = snapshot['verification_token']
    if token != '' and (not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{32}', token)):
        raise FirmwareError('Firmware journal observation token is invalid.')
    if not _text(snapshot['connection_epoch'], 256) or not _text(snapshot['error'], 1024):
        raise FirmwareError('Firmware journal text exceeds its limits.')
    if snapshot['sent_bytes'] and not snapshot['write_attempted']:
        raise FirmwareError('Firmware journal operation state is inconsistent.')
    if snapshot['phase'] in ('idle', 'ready_start', 'failed_before_write') and (
            snapshot['write_attempted'] or snapshot['sent_bytes'] or snapshot['acknowledged_bytes']):
        raise FirmwareError('Firmware journal pre-write state is inconsistent.')
    if snapshot['phase'] not in ('idle', 'ready_start', 'failed_before_write') and (
            not snapshot['write_attempted'] or not binding or not snapshot['connection_epoch']):
        raise FirmwareError('Firmware journal active state has no confirmed identity or operation.')
    if snapshot['phase'] in ('verifying_version', 'succeeded') and not token:
        raise FirmwareError('Firmware journal verification state has no request token.')
    if snapshot['phase'] == 'succeeded' and (not snapshot['write_attempted'] or
            snapshot['sent_bytes'] != total or snapshot['acknowledged_bytes'] != total or
            not token or not binding or snapshot['journal_error']):
        raise FirmwareError('Firmware journal success lacks required completion state.')
    return copy.deepcopy(snapshot)


def encode_record(snapshot, boot_id):
    if not isinstance(boot_id, str):
        raise FirmwareError('Firmware journal boot identity is invalid.')
    try:
        if str(uuid.UUID(boot_id)) != boot_id: raise ValueError('boot')
    except ValueError as exc:
        raise FirmwareError('Firmware journal boot identity is invalid.') from exc
    record = {'schema': 1, 'boot_id': boot_id, 'snapshot': validate_snapshot(snapshot)}
    encoded = (json.dumps(record, sort_keys=True, ensure_ascii=True) + '\n').encode('ascii')
    if len(encoded) > MAX_JOURNAL:
        raise FirmwareError('Firmware journal exceeds its size limit.')
    return encoded


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate')
        result[key] = value
    return result


def recovered_record(data):
    """Display-only recovery. No command replay, even on the same boot."""
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_JOURNAL:
        raise FirmwareError('Firmware journal size is invalid.')
    try:
        record = json.loads(data, object_pairs_hook=_unique,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if (not isinstance(record, dict) or set(record) != {'schema', 'boot_id', 'snapshot'} or
                type(record['schema']) is not int or record['schema'] != 1):
            raise ValueError('schema')
        encode_record(record['snapshot'], record['boot_id'])
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise FirmwareError('Firmware journal data is invalid.') from exc
    snapshot = record['snapshot']
    # Every recovered nonterminal job is stale. Ready_start has no attempted
    # output; an active/attempted job is uncertain, never automatically resumed.
    if snapshot['phase'] not in ('succeeded', 'uncertain', 'failed_before_write'):
        snapshot['phase'] = 'uncertain' if snapshot['write_attempted'] else 'failed_before_write'
        snapshot['error'] = 'The previous firmware worker ended without a final result. No retry was started.'
    return record


class FirmwareJournal:
    """Single POSIX writer; keep the lock inode for the lifetime of the worker.

    The supplied directory must already be owned by the worker with mode0700.
    Never delete/recreate the lock file while processes can still use it.
    flock excludes cooperating workers, not unrelated controller writers.
    """
    def __init__(self, directory, boot_id):
        if os.name != 'posix':
            raise FirmwareError('Protected firmware journaling requires Linux/POSIX validation.')
        import fcntl
        # Validate before creating or opening anything.
        encode_record(_empty_snapshot(), boot_id)
        path = Path(directory)
        if not path.is_absolute() or '..' in path.parts:
            raise FirmwareError('Firmware journal requires an absolute private directory.')
        self._directory = self._lock = None
        self._boot = boot_id
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open('/', flags)
        try:
            for part in path.parts[1:]:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd); fd = child
                info = os.fstat(fd)
                # Root-owned sticky /tmp is acceptable for isolated tests;
                # the final directory must still be private and owned by us.
                if (info.st_uid not in (0, os.geteuid()) or
                        (info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
                    raise FirmwareError('Firmware journal parent directory is not trusted.')
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise FirmwareError('Firmware journal directory must be private and owned by the worker.')
            self._directory, fd = fd, None
            self._lock = os.open('worker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=self._directory)
            self._private_file(self._lock)
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, FirmwareError) as exc:
            self.close()
            raise FirmwareError('Firmware journal ownership could not be acquired.') from exc
        finally:
            if fd is not None: os.close(fd)

    @staticmethod
    def _private_file(fd):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise FirmwareError('Firmware journal file is not private and regular.')

    def _held(self):
        if self._directory is None or self._lock is None:
            raise FirmwareError('Firmware journal is closed.')
        # Detect accidental unlink/replacement of the cooperating-worker lock.
        path_info = os.stat('worker.lock', dir_fd=self._directory, follow_symlinks=False)
        lock_info = os.fstat(self._lock)
        if (path_info.st_dev, path_info.st_ino) != (lock_info.st_dev, lock_info.st_ino):
            raise FirmwareError('Firmware journal lock identity changed.')
        self._private_file(self._lock)

    def save(self, snapshot):
        self._held()
        data = encode_record(snapshot, self._boot)
        # Refuse unexpected existing objects; replace only our private record.
        existing = None
        try:
            existing = os.open('journal.json', os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self._directory)
            self._private_file(existing)
        except FileNotFoundError:
            pass
        finally:
            if existing is not None: os.close(existing)
        name = '.journal-' + uuid.uuid4().hex
        fd = None
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=self._directory)
            self._private_file(fd)
            offset = 0
            while offset < len(data):
                count = os.write(fd, data[offset:])
                if not count: raise OSError('short journal write')
                offset += count
            os.fsync(fd)
            os.close(fd); fd = None
            self._held()
            os.replace(name, 'journal.json', src_dir_fd=self._directory, dst_dir_fd=self._directory)
            os.fsync(self._directory)
        finally:
            if fd is not None: os.close(fd)
            try: os.unlink(name, dir_fd=self._directory)
            except FileNotFoundError: pass

    def load(self):
        self._held()
        try:
            fd = os.open('journal.json', os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self._directory)
        except FileNotFoundError:
            return None
        try:
            self._private_file(fd)
            if os.fstat(fd).st_size > MAX_JOURNAL:
                raise FirmwareError('Firmware journal exceeds its size limit.')
            chunks, size = [], 0
            while size <= MAX_JOURNAL:
                chunk = os.read(fd, min(4096, MAX_JOURNAL+1-size))
                if not chunk: break
                chunks.append(chunk); size += len(chunk)
            return recovered_record(b''.join(chunks))
        finally:
            os.close(fd)

    def close(self):
        for name in ('_lock', '_directory'):
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd); setattr(self, name, None)

    def __enter__(self): return self
    def __exit__(self, *unused): self.close()


def _empty_snapshot():
    release = release_for('left')
    return {'schema': 1, 'job_id': '0'*32, 'component': 'left', 'image_sha256': release.sha256,
        'offered_version': release.version, 'total_bytes': release.size, 'phase': 'idle',
        'write_attempted': False, 'sent_bytes': 0, 'acknowledged_bytes': 0, 'sequence': 0,
        'boundary': None, 'device_binding': '', 'connection_epoch': '', 'verification_token': '',
        'error': '', 'journal_error': False}

# SPDX-License-Identifier: BSD-3-Clause
"""Persistent cooperating-Companion admission fence, never a USB ownership lock.

A root-private marker keeps new backend instances paused after the original
process dies. Process death, file age and a missing PID never authorize clearing
it. A future handoff guardian must independently restore USB/InputPlumber before
releasing its exact claim. There is no startup writer, RPC or automatic recovery.
Other plugins/kernel/USB consumers are not excluded by this filesystem fence.
"""
import json
import os
import stat
import uuid
import asyncio
import socket
import struct
from diagnostics import event

SOCKET_NAME = 'controller-updater.sock'

ROOT = '/run/legiongo2companion'
NAME = 'controller-maintenance.claim'


class MaintenanceError(RuntimeError):
    pass


def _directory(path, owner):
    if os.name != 'posix' or not path.startswith('/') or any(part in ('.', '..', '') for part in path.split('/')[1:]):
        raise MaintenanceError('Maintenance requires a protected absolute POSIX directory.')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open('/', flags)
    try:
        for part in path.split('/')[1:]:
            child = os.open(part, flags, dir_fd=current)
            os.close(current); current = child
            info = os.fstat(current)
            if (info.st_uid not in (0, owner) or
                    info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)):
                raise MaintenanceError('Maintenance directory ancestry is not trusted.')
        info = os.fstat(current)
        if info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o700:
            raise MaintenanceError('Maintenance directory is not private.')
        result, current = current, None
        return result
    finally:
        if current is not None: os.close(current)


def _read(parent, owner):
    file = os.open(NAME, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        info = os.fstat(file)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or not 1 <= info.st_size <= 1024):
            raise MaintenanceError('Maintenance claim is not private and bounded.')
        raw = os.read(file, 1025)
        if len(raw) != info.st_size or len(raw) > 1024:
            raise MaintenanceError('Maintenance claim changed during its read.')
        data = json.loads(raw)
        if (not isinstance(data, dict) or set(data) != {'schema', 'token'} or
                type(data['schema']) is not int or data['schema'] != 1 or
                not isinstance(data['token'], str) or len(data['token']) != 32 or
                any(character not in '0123456789abcdef' for character in data['token'])):
            raise MaintenanceError('Maintenance claim contents are invalid.')
        return info, data['token']
    except (ValueError, UnicodeError, TypeError) as exc:
        raise MaintenanceError('Maintenance claim contents are invalid.') from exc
    finally:
        os.close(file)


def _present(root, owner):
    """Private test seam: errors fail closed; only a trusted absence is clear."""
    parent = None
    try:
        parent = _directory(root, owner)
        try:
            _read(parent, owner)
        except FileNotFoundError:
            return False
        return True
    except FileNotFoundError:
        # A missing directory is clear only after protected ancestors were
        # checked. _directory propagates an untrusted parent before absence.
        return False
    except (OSError, MaintenanceError, RecursionError) as exc:
        event('maintenance','fence_read_failed',level='warning',error=exc,repeat=60,stage='fence')
        return True
    finally:
        if parent is not None: os.close(parent)


def maintenance_present():
    return os.name == 'posix' and _present(ROOT, 0)


class MaintenanceClaim:
    """An exact marker owner; no timeout, PID guessing, overwrite or destructor cleanup."""
    def __init__(self, parent, owner, token, identity):
        self._parent, self._owner, self._token = parent, owner, token
        self._identity, self._released = identity, False
        self._unlinked = False

    @classmethod
    def create(cls):
        # Provisioning is deliberately separate and not a backend startup side
        # effect. A handoff must close/drain admission before publishing a claim.
        return cls._create(ROOT, 0)

    @classmethod
    def _create(cls, root, owner):
        parent = _directory(root, owner)
        file = None
        try:
            token = uuid.uuid4().hex
            # Exclusivity survives the writer dying; a partial claim remains a
            # fail-closed fence, never a signal that the hardware was restored.
            file = os.open(NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=parent)
            os.fchmod(file, 0o600)
            payload = json.dumps({'schema': 1, 'token': token}, separators=(',', ':')).encode()
            if os.write(file, payload) != len(payload):
                raise MaintenanceError('Maintenance claim publication was incomplete.')
            os.fsync(file); os.fsync(parent)
            info = os.fstat(file)
            result = cls(parent, owner, token, (info.st_dev, info.st_ino))
            parent = None
            return result
        finally:
            if file is not None: os.close(file)
            if parent is not None: os.close(parent)

    def release(self, *, restored):
        """Trusted coordinator attestation, not independent hardware readback."""
        if restored is not True or self._parent is None or self._released:
            raise MaintenanceError('Maintenance restoration/owner is not confirmed.')
        if not self._unlinked:
            info, token = _read(self._parent, self._owner)
            if token != self._token or (info.st_dev, info.st_ino) != self._identity:
                raise MaintenanceError('Maintenance claim owner changed.')
            os.unlink(NAME, dir_fd=self._parent)
            self._unlinked = True
        # A directory fsync may fail after our exact unlink succeeded. Retain
        # that local fact so recovery can finish durability without replaying
        # restoration. Any replacement marker belongs to another claim.
        def absent():
            try: os.stat(NAME,dir_fd=self._parent,follow_symlinks=False)
            except FileNotFoundError: return
            raise MaintenanceError('Maintenance claim owner changed.')
        absent()
        os.fsync(self._parent)
        absent()
        self._released = True
        self.close()
        event('maintenance','claim_released',restored=True)

    def close(self):
        """Close this owner's directory FD without removing a retained marker."""
        parent, self._parent = self._parent, None
        if parent is not None: os.close(parent)


class ControllerUpdaterGate:
    """Root-worker IPC for the existing backend gate; never a frontend RPC.

    The worker must restore and independently read back USB and InputPlumber
    before sending ``restored``. This service only authenticates that trusted
    coordinator, drains Companion and owns its exact persistent admission fence.
    A disconnect/failure keeps admission closed; it never guesses a rollback.
    No USB, input configuration, settings or firmware operation occurs here.
    """
    def __init__(self, pause, resume, *, root=ROOT, owner=0):
        self._pause, self._resume = pause, resume
        self._root, self._owner = root, owner
        self._server = self._identity = self._parent = None
        self._active = False
        self._pending = None
        self._resumed_claim = None
        self._writers = set()
        self._tasks = set()
        self._closing = False

    async def start(self):
        if os.name != 'posix' or os.geteuid() != self._owner:
            raise MaintenanceError('Updater coordination requires the trusted backend owner.')
        if self._server is not None:
            raise MaintenanceError('Updater coordination is already listening.')
        try: os.mkdir(self._root, 0o700)
        except FileExistsError: pass
        self._parent = _directory(self._root, self._owner)
        path = self._root + '/' + SOCKET_NAME
        # Never remove/adopt a socket from another backend, even after its death.
        if os.path.lexists(path):
            self._close_parent()
            raise MaintenanceError('An earlier updater endpoint needs inspection.')
        try:
            self._server = await asyncio.start_unix_server(self._client, path, limit=1024)
            os.chmod(path, 0o600)
            info = os.stat(SOCKET_NAME, dir_fd=self._parent, follow_symlinks=False)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self._owner:
                raise MaintenanceError('Updater endpoint ownership is invalid.')
            self._identity = (info.st_dev, info.st_ino)
        except BaseException:
            await self.close()
            raise

    def _close_parent(self):
        parent, self._parent = self._parent, None
        if parent is not None: os.close(parent)

    async def _client(self, reader, writer):
        claim = hold = None
        admitted = False
        task = asyncio.current_task()
        self._tasks.add(task)
        stage = 'peer_admission'
        try:
            peer = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != self._owner or self._active or self._closing:
                raise MaintenanceError('Only one trusted updater worker may enter.')
            # Admission happens before an await, including the first input read.
            self._active, admitted = True, True
            self._writers.add(writer)
            stage = 'request'
            raw = await asyncio.wait_for(reader.readline(), 3)
            if raw == b'{"action":"recover"}\n' and self._pending is None:
                # A failed pause callback may have closed local admission before
                # returning a hold. Only the trusted backend can drain and
                # reconcile that own, unclaimed pause; no marker is cleared here.
                stage = 'unclaimed_recovery'
                await self._resume(None)
                event('maintenance','unclaimed_recovery_finished',restored=True)
                writer.write(b'{"resumed":true}\n')
                await asyncio.wait_for(writer.drain(), 3)
                return
            elif raw == b'{"action":"recover"}\n' and self._pending is not None:
                # A separate trusted recovery process may attest restoration
                # after the original worker has exited. The coordinator must
                # prove that exit and the actual USB/input state independently.
                # No second worker can enter while the old connection is live.
                hold, claim = self._pending
            elif raw == b'{"action":"pause"}\n' and self._pending is None:
                stage = 'pause_drain'
                hold = await self._pause()
                if self._closing: raise MaintenanceError('Updater endpoint is stopping.')
                # A restart must remain paused only after existing work is drained.
                stage = 'claim_publication'
                claim = MaintenanceClaim._create(self._root, self._owner)
                self._pending = (hold, claim)
                event('maintenance','controller_paused')
                writer.write(b'{"paused":true}\n')
                await asyncio.wait_for(writer.drain(), 3)
                stage = 'restoration_attestation'
                raw = await asyncio.wait_for(reader.readline(), 720)
                if raw != b'{"action":"restored"}\n':
                    raise MaintenanceError('Updater restoration was not confirmed.')
            else:
                raise MaintenanceError('Invalid updater admission request.')
            if self._closing: raise MaintenanceError('Updater endpoint is stopping.')
            # Trusted callback must verify/reconcile the restored consumer state
            # while normal admission is still closed. It cannot use public RPCs.
            if self._resumed_claim is not claim:
                stage = 'backend_restoration'
                await self._resume(hold)
                self._resumed_claim = claim
            stage = 'claim_release'
            claim.release(restored=True)
            self._pending = None
            self._resumed_claim = None
            event('maintenance','controller_resumed',restored=True)
            writer.write(b'{"resumed":true}\n')
            await asyncio.wait_for(writer.drain(), 3)
        except Exception as exc:
            event('maintenance','coordination_failed',level='warning',error=exc,repeat=60,stage=stage,
                pending=self._pending is not None)
            # Failure/disconnect leaves a claimed or process-local fence closed.
            # Do not put request bytes, credentials or kernel IDs into responses.
            try:
                writer.write(b'{"error":"Updater coordination did not complete."}\n')
                await asyncio.wait_for(writer.drain(), 1)
            except Exception: pass
        finally:
            if claim is not None and self._pending is None: claim.close()
            if admitted: self._active = False
            self._writers.discard(writer)
            writer.close()
            try: await writer.wait_closed()
            except (ConnectionError, OSError): pass
            self._tasks.discard(task)

    async def close(self):
        self._closing = True
        if self._server is not None:
            self._server.close()
            # Python 3.13+ wait_closed also waits for accepted connections.
            # End our readers before waiting for the listener to finish.
            for writer in tuple(self._writers): writer.close()
            await self._server.wait_closed()
            self._server = None
        for writer in tuple(self._writers): writer.close()
        if self._tasks:
            # Draining cannot be cancelled into a hidden hardware worker. The
            # pause callback has its own bounded drain; socket reads end at EOF.
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        try:
            if self._identity is not None and self._parent is not None:
                try:
                    info = os.stat(SOCKET_NAME, dir_fd=self._parent, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == self._identity and stat.S_ISSOCK(info.st_mode):
                        os.unlink(SOCKET_NAME, dir_fd=self._parent)
                except FileNotFoundError: pass
        finally:
            self._identity = None
            try:
                if self._pending is not None:
                    # Keep the durable fence even if removing the socket fails.
                    self._pending[1].close()
            finally:
                self._pending = None
                self._resumed_claim = None
                self._close_parent()

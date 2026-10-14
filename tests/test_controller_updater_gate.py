# SPDX-License-Identifier: BSD-3-Clause
"""Real POSIX socket, peer credentials and private persistent-fence tests."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from controller_maintenance import ControllerUpdaterGate, MaintenanceClaim, MaintenanceError, NAME, SOCKET_NAME


@unittest.skipUnless(os.name == 'posix', 'Updater IPC requires POSIX peer credentials')
class UpdaterGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name) / 'private'
        self.pause, self.resume = AsyncMock(return_value=object()), AsyncMock()
        def resume(hold):
            if hold is None:raise MaintenanceError('No own unclaimed pause')
        self.resume.side_effect=resume
        self.gate = ControllerUpdaterGate(self.pause, self.resume,
                                         root=str(self.root), owner=os.geteuid())
        self.clients = []
        await self.gate.start()

    async def asyncTearDown(self):
        for _, writer in self.clients:
            writer.close()
            try: await writer.wait_closed()
            except ConnectionResetError: pass
        await self.gate.close()
        self.folder.cleanup()

    async def client(self):
        pair = await asyncio.open_unix_connection(str(self.root / SOCKET_NAME))
        self.clients.append(pair)
        return pair

    async def send(self, pair, raw):
        reader, writer = pair
        writer.write(raw); await writer.drain()
        return await asyncio.wait_for(reader.readline(), 2)

    async def admitted(self):
        pair = await self.client()
        self.assertEqual(await self.send(pair, b'{"action":"pause"}\n'), b'{"paused":true}\n')
        self.assertTrue((self.root / NAME).exists())
        return pair

    async def disconnected(self):
        async def wait():
            while self.gate._active:
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), 3)

    async def test_exact_success_sequence_preserves_claim_through_restoration(self):
        pair = await self.admitted()
        self.resume.side_effect = lambda hold: self.assertTrue((self.root / NAME).exists())
        self.assertEqual(await self.send(pair, b'{"action":"restored"}\n'), b'{"resumed":true}\n')
        self.resume.assert_awaited_once_with(self.pause.return_value)
        self.assertFalse((self.root / NAME).exists())
        self.assertEqual((self.root / SOCKET_NAME).stat().st_mode & 0o777, 0o600)

    async def test_disconnect_never_clears_persistent_fence_or_resumes(self):
        reader, writer = await self.admitted()
        writer.close(); await writer.wait_closed()
        await self.gate.close()
        self.assertTrue((self.root / NAME).exists())
        self.resume.assert_not_awaited()

    async def test_trusted_recovery_can_finish_disconnected_exact_live_claim(self):
        _,writer=await self.admitted()
        inode=(self.root/NAME).stat().st_ino
        writer.close();await writer.wait_closed()
        await self.disconnected()
        self.assertEqual((self.root/NAME).stat().st_ino,inode)
        self.assertEqual(await self.send(await self.client(),b'{"action":"recover"}\n'),b'{"resumed":true}\n')
        self.pause.assert_awaited_once()
        self.resume.assert_awaited_once_with(self.pause.return_value)
        self.assertFalse((self.root/NAME).exists())

    async def test_recovery_cannot_displace_live_worker_or_resume_without_claim(self):
        self.assertIn(b'error',await self.send(await self.client(),b'{"action":"recover"}\n'))
        self.resume.assert_awaited_once_with(None);self.resume.reset_mock()
        first=await self.admitted()
        self.assertIn(b'error',await self.send(await self.client(),b'{"action":"recover"}\n'))
        self.resume.assert_not_awaited()
        self.assertEqual(await self.send(first,b'{"action":"restored"}\n'),b'{"resumed":true}\n')

    async def test_recovery_finishes_claim_without_repeating_completed_resume(self):
        pair=await self.admitted()
        real=MaintenanceClaim.release;calls=[0]
        def release(claim,*,restored):
            calls[0]+=1
            if calls[0]==1:raise OSError('Injected marker unlink failure')
            return real(claim,restored=restored)
        with patch.object(MaintenanceClaim,'release',autospec=True,side_effect=release):
            self.assertIn(b'error',await self.send(pair,b'{"action":"restored"}\n'))
            self.assertTrue((self.root/NAME).exists())
            await self.disconnected()
            self.assertEqual(await self.send(await self.client(),b'{"action":"recover"}\n'),b'{"resumed":true}\n')
        self.resume.assert_awaited_once()
        self.assertFalse((self.root/NAME).exists())

    async def test_recovery_finishes_post_unlink_directory_sync_without_repeating_resume(self):
        pair=await self.admitted()
        parent=self.gate._pending[1]._parent
        original=os.fsync
        def sync(fd):
            if fd==parent:raise OSError('Injected directory sync failure after unlink')
            return original(fd)
        with patch('controller_maintenance.os.fsync',side_effect=sync):
            self.assertIn(b'error',await self.send(pair,b'{"action":"restored"}\n'))
            await self.disconnected()
            self.assertFalse((self.root/NAME).exists());self.assertIsNotNone(self.gate._pending)
            self.assertIn(b'error',await self.send(await self.client(),b'{"action":"recover"}\n'))
            await self.disconnected()
            self.assertIsNotNone(self.gate._pending)
        self.assertEqual(await self.send(await self.client(),b'{"action":"recover"}\n'),b'{"resumed":true}\n')
        self.resume.assert_awaited_once_with(self.pause.return_value)
        self.assertIsNone(self.gate._pending);self.assertFalse((self.root/NAME).exists())

    async def test_failed_restoration_keeps_exact_claim(self):
        pair = await self.admitted()
        inode = (self.root / NAME).stat().st_ino
        self.resume.side_effect = RuntimeError('Injected restore fault')
        self.assertIn(b'error', await self.send(pair, b'{"action":"restored"}\n'))
        self.assertEqual((self.root / NAME).stat().st_ino, inode)

    async def test_untrusted_peer_is_rejected_before_pause(self):
        self.gate._owner += 1
        self.assertIn(b'error', await self.send(await self.client(), b'{"action":"pause"}\n'))
        self.pause.assert_not_awaited()
        self.assertFalse((self.root / NAME).exists())

    async def test_second_worker_cannot_enter_or_complete_first_claim(self):
        first = await self.admitted()
        self.assertIn(b'error', await self.send(await self.client(), b'{"action":"restored"}\n'))
        self.assertTrue((self.root / NAME).exists())
        self.assertEqual(await self.send(first, b'{"action":"restored"}\n'), b'{"resumed":true}\n')
        self.pause.assert_awaited_once()

    async def test_invalid_and_oversized_requests_never_pause(self):
        for raw in (b'{"action":"restored"}\n', b'x'*1025+b'\n'):
            self.assertIn(b'error', await self.send(await self.client(), raw))
        self.pause.assert_not_awaited()

    async def test_failed_pause_never_admits_worker_or_creates_claim(self):
        self.pause.side_effect = RuntimeError('Failed drain')
        self.assertIn(b'error', await self.send(await self.client(), b'{"action":"pause"}\n'))
        self.assertFalse((self.root / NAME).exists())
        self.resume.assert_not_awaited()

    async def test_trusted_recovery_reconciles_failed_callback_without_creating_a_marker(self):
        self.pause.side_effect=RuntimeError('Failed post-drain observation')
        self.assertIn(b'error',await self.send(await self.client(),b'{"action":"pause"}\n'))
        await self.disconnected()
        self.assertFalse((self.root/NAME).exists())
        self.resume.side_effect=None
        self.assertEqual(await self.send(await self.client(),b'{"action":"recover"}\n'),b'{"resumed":true}\n')
        self.resume.assert_awaited_once_with(None)
        self.assertFalse((self.root/NAME).exists())

    async def test_rejected_unclaimed_recovery_keeps_admission_without_creating_or_clearing_a_marker(self):
        self.resume.side_effect=MaintenanceError('Original controller is still missing')
        self.assertIn(b'error',await self.send(await self.client(),b'{"action":"recover"}\n'))
        self.resume.assert_awaited_once_with(None)
        self.assertFalse((self.root/NAME).exists())

    async def test_close_during_drain_never_admits_or_resumes(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def drain():
            entered.set(); await finish.wait(); return object()
        self.pause.side_effect = drain
        reader, writer = await self.client()
        writer.write(b'{"action":"pause"}\n'); await writer.drain()
        await entered.wait()
        closed = asyncio.create_task(self.gate.close())
        await asyncio.sleep(0)
        finish.set(); await closed
        self.assertNotIn(b'paused', await reader.readline())
        self.assertFalse((self.root / NAME).exists())
        self.resume.assert_not_awaited()

    async def test_stale_or_replaced_endpoint_is_never_removed_or_adopted(self):
        second = ControllerUpdaterGate(self.pause, self.resume,
                                      root=str(self.root), owner=os.geteuid())
        with self.assertRaises(MaintenanceError): await second.start()
        original = self.root / SOCKET_NAME
        original.unlink(); original.write_text('replacement')
        await self.gate.close()
        self.assertEqual(original.read_text(), 'replacement')

    async def test_endpoint_filesystem_error_closes_descriptors_but_keeps_recovery_fence(self):
        await self.admitted()
        parent = self.gate._parent
        original = os.stat
        def unavailable(path, *args, **kwargs):
            if path == SOCKET_NAME and kwargs.get('dir_fd') == parent:
                raise PermissionError('Endpoint directory became unavailable')
            return original(path, *args, **kwargs)
        with patch('controller_maintenance.os.stat', side_effect=unavailable):
            with self.assertRaises(PermissionError):
                await self.gate.close()
        self.assertIsNone(self.gate._parent)
        self.assertIsNone(self.gate._pending)
        with self.assertRaises(OSError):
            os.fstat(parent)
        self.assertTrue((self.root / NAME).exists())
        self.resume.assert_not_awaited()

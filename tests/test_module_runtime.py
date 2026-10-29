# SPDX-License-Identifier: BSD-3-Clause
"""Blocking hardware I/O offloaded through module_runtime must not stall the event loop."""
import asyncio
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import support  # Install isolated Decky stubs before backend imports.
import battery_backend
import module_runtime
import tdp_backend


class OffloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await module_runtime.drain('battery')

    async def test_blocking_work_lets_the_event_loop_run(self):
        loop = asyncio.get_running_loop()
        def blocked():
            beat = threading.Event()
            loop.call_soon_threadsafe(beat.set)
            return beat.wait(.5), 'done'
        self.assertEqual(await module_runtime.offload('battery', blocked), (True, 'done'))


class ControllerMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    """fwupd must never write a controller while Companion's own controller work runs."""

    def setUp(self):
        self.addCleanup(module_runtime.resume_controller)

    async def test_maintenance_waits_for_running_controller_work_then_refuses_new_work(self):
        release = threading.Event()
        worker = asyncio.create_task(module_runtime.offload('rgb', release.wait, 5))
        await asyncio.sleep(0.05)
        module_runtime.pause_controller()
        drained = asyncio.create_task(module_runtime.drain_controller())
        await asyncio.sleep(0.1)
        self.assertFalse(drained.done(), 'maintenance began while a controller write was running')
        release.set()
        await drained
        self.assertTrue(await worker)
        ran = []
        with self.assertRaises(module_runtime.ControllerPaused):
            await module_runtime.offload('rgb', ran.append, 1)
        self.assertEqual(ran, [])
        self.assertTrue(await module_runtime.offload('battery', lambda: True))

    async def test_an_operation_begun_before_maintenance_cannot_continue_after_it(self):
        for _ in range(2):  # every maintenance, not only the first
            with module_runtime.controller_scope('rgb'):
                module_runtime.pause_controller()
                module_runtime.resume_controller()
                with self.assertRaises(module_runtime.ControllerPaused):
                    module_runtime.require_available('rgb')
            module_runtime.require_available('rgb')


class PlatformProbeTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(time, 'CLOCK_BOOTTIME'), 'Linux boot-time clock')
    def test_suspend_offset_is_the_time_spent_suspended(self):
        offset = module_runtime.suspend_offset()
        self.assertIsNotNone(offset)
        self.assertGreaterEqual(offset, -0.01)
        self.assertLessEqual(offset, time.clock_gettime(time.CLOCK_BOOTTIME))

    @unittest.skipUnless(os.name == 'posix', 'HID device names and driver links')
    def test_kernel_controller_follows_the_real_driver_link(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for driver in ('hid-lenovo-go', 'hid-generic'):
                (root / 'drivers' / driver).mkdir(parents=True)
            devices = root / 'devices'
            devices.mkdir()
            def device(name, driver):
                (devices / name).mkdir()
                (devices / name / 'driver').symlink_to(root / 'drivers' / driver)
            device('0003:17EF:61EC.0002', 'hid-generic')
            self.assertFalse(module_runtime.kernel_controller(str(devices)))
            device('0003:17EF:61EB.0003', 'hid-lenovo-go')
            self.assertTrue(module_runtime.kernel_controller(str(devices)))

    def test_require_success_raises_not_confirmed_for_any_unconfirmed_reply(self):
        for reply, message in ((None, 'not confirmed'), ('ok', 'not confirmed'), ({'ok': 'true'}, 'not confirmed'),
                               ({'enabled': True}, 'not confirmed'), ({'ok': False, 'error': 'Refused.'}, 'Refused')):
            with self.subTest(reply=reply), self.assertRaisesRegex(module_runtime.NotConfirmed, message):
                module_runtime.require_success(reply)
        with self.assertRaisesRegex(module_runtime.NotConfirmed, 'No WiFi') as refused:
            module_runtime.require_success({'ok': False, 'error': 'No WiFi.', 'state': {'error': 'no_wifi'}})
        self.assertEqual(refused.exception.code, 'no_wifi')
        module_runtime.require_success({'ok': True, 'state': {}})
        module_runtime.require_success({'ok': True})



class FirmwareLockTests(unittest.TestCase):
    """The Legion's AML aborts EC/WMI methods that run at once; Companion's own writes take turns."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        self.charge_types = root / 'charge_types'
        self.charge_types.write_text('[Standard] Fast\n')
        for patcher in (patch.object(battery_backend, 'SYS_ROOT', root),
                        patch.object(battery_backend.os, 'write', self.firmware_write),
                        patch.object(tdp_backend, 'open', self.firmware_file, create=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.inside, self.peak, self.count = 0, 0, threading.Lock()
        self.fail_with = None

    def firmware_write(self, _fd, payload):
        with self.count:
            self.inside += 1
            self.peak = max(self.peak, self.inside)
        try:
            time.sleep(.001)
            if self.fail_with:
                raise self.fail_with
            return len(payload)
        finally:
            with self.count:
                self.inside -= 1

    def firmware_file(self, _path, _mode='r', **_kwargs):
        test = self
        class File:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def write(self, value): test.firmware_write(None, value)
        return File()

    def test_tdp_and_battery_writes_never_overlap(self):
        def tdp():
            for _ in range(30):
                tdp_backend._write_ppt(15, 18, 25)
                tdp_backend._write_profile('/fixture/profile', 'custom')
        def battery():
            for _ in range(60):
                battery_backend._write_mode(self.charge_types, 'Fast')
        workers = [threading.Thread(target=target) for target in (tdp, battery, tdp, battery)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
        self.assertEqual(self.peak, 1)
        self.assertFalse(module_runtime.firmware_lock.locked())

    def test_a_failed_write_releases_the_lock(self):
        self.fail_with = OSError(5, 'firmware refused')
        self.assertFalse(tdp_backend._write_profile('/fixture/profile', 'custom'))
        self.assertFalse(module_runtime.firmware_lock.locked())
        tdp_backend._write_ppt(15, 18, 25)
        self.assertFalse(module_runtime.firmware_lock.locked())
        with self.assertRaises(OSError):
            battery_backend._write_mode(self.charge_types, 'Fast')
        self.assertFalse(module_runtime.firmware_lock.locked())


class SysfsReadTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_reads_a_stripped_ascii_value_up_to_the_limit(self):
        path = self.root / 'attr'
        path.write_bytes(b'  Standard \n')
        self.assertEqual(module_runtime.read_sysfs_text(path), 'Standard')
        path.write_bytes(b'x' * 512)
        self.assertEqual(len(module_runtime.read_sysfs_text(path)), 512)
        self.assertEqual(len(module_runtime.read_sysfs_text(path, 600)), 512)

    def test_refuses_oversized_values_with_the_callers_error(self):
        path = self.root / 'attr'
        path.write_bytes(b'x' * 513)
        with self.assertRaises(OSError):
            module_runtime.read_sysfs_text(path)
        with self.assertRaisesRegex(RuntimeError, 'Thing returned an oversized value'):
            module_runtime.read_sysfs_text(path, error=RuntimeError, subject='Thing')

    def test_refuses_non_ascii_and_missing_paths(self):
        path = self.root / 'attr'
        path.write_bytes(b'caf\xc3\xa9')
        with self.assertRaises(UnicodeError):
            module_runtime.read_sysfs_text(path)
        with self.assertRaises(FileNotFoundError):
            module_runtime.read_sysfs_text(self.root / 'missing')

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'POSIX symlink semantics')
    def test_never_follows_a_final_symlink_or_reads_a_directory(self):
        target = self.root / 'target'
        target.write_text('Fast')
        link = self.root / 'link'
        link.symlink_to(target)
        with self.assertRaises(OSError):
            module_runtime.read_sysfs_text(link)
        with self.assertRaisesRegex(RuntimeError, 'Thing changed during access'):
            module_runtime.read_sysfs_text(self.root, error=RuntimeError, subject='Thing')


if __name__ == '__main__':
    unittest.main()

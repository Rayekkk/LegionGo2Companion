# SPDX-License-Identifier: BSD-3-Clause
"""Blocking hardware I/O offloaded through module_runtime must not stall the event loop."""
import asyncio
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


if __name__ == '__main__':
    unittest.main()

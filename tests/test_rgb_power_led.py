# SPDX-License-Identifier: BSD-3-Clause
"""RGB power-LED lock safety and boot settling of the RGB drift loop."""
import asyncio
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch, Mock

import support
import rgb_backend


@unittest.skipUnless(os.name == 'posix', 'Real POSIX power LED lock')
class PowerLedLockTests(unittest.TestCase):
    def test_busy_power_led_lock_returns_without_entering_hardware_operation(self):
        import fcntl
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'power.lock'
            fd = os.open(path, os.O_RDWR|os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            # Keep the lock held for the complete attempted operation. A timer
            # releasing it after 1.5 s could turn a correct timeout into success
            # merely because a busy test runner was descheduled.
            finished = threading.Event()
            errors = []
            operation = Mock()
            def attempt():
                try:
                    with patch.object(rgb_backend, 'POWER_LOCK_PATH', str(path)):
                        rgb_backend._with_power_lock(operation)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()
            worker = threading.Thread(target=attempt)
            worker.start()
            try:
                self.assertTrue(finished.wait(5), 'A busy lock must not wait indefinitely')
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], TimeoutError)
                operation.assert_not_called()
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                worker.join(5)
                os.close(fd)
            self.assertFalse(worker.is_alive())

    def test_symlink_and_fifo_lock_never_enter_hardware_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'target').write_text('preserve')
            (root/'link').symlink_to(root/'target')
            os.mkfifo(root/'fifo', 0o600)
            for name in ('link', 'fifo'):
                operation = Mock()
                with patch.object(rgb_backend, 'POWER_LOCK_PATH', str(root/name)):
                    with self.assertRaises((ValueError, OSError)):
                        rgb_backend._with_power_lock(operation)
                operation.assert_not_called()
            self.assertEqual((root/'target').read_text(), 'preserve')


class RgbBootSettleTests(unittest.TestCase):
    def test_controller_healthy_at_boot_is_rechecked_during_settling_with_short_sleeps(self):
        plugin = support.rgb_backend.Plugin()
        plugin._settle_until = 30
        now = [0.0]; delays = []; observed = []
        async def sleep(delay):
            delays.append(delay)
            if not 0 < delay <= 10 or now[0] > 100: raise asyncio.CancelledError
            now[0] += delay
        async def offload(fn):
            observed.append(now[0]); return True
        rgb = support.rgb_backend
        with patch.object(rgb.asyncio, "sleep", side_effect=sleep),              patch.object(rgb, "time", types.SimpleNamespace(monotonic=lambda: now[0])),              patch.object(rgb, "_resume_detected", return_value=False),              patch.object(rgb, "_offload", side_effect=offload):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(plugin._drift_loop())
        self.assertTrue(all(0 < delay <= 10 for delay in delays), delays)
        boot = [at for at in observed if at < 30]
        self.assertGreaterEqual(len(boot), 2, observed)
        self.assertLessEqual(len(boot), 7)


if __name__ == '__main__':
    unittest.main()

# SPDX-License-Identifier: BSD-3-Clause
import asyncio
import io
import os
import tempfile
import threading
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_integration
import fan_backend
import main
import rgb_backend
import companion_updates
import controller_firmware
import controller_firmware_catalog
import tdp_updater
import tdp_backend
import urllib.request


class RedirectResourceTests(unittest.TestCase):
    def test_rejected_redirect_closes_the_original_response(self):
        url = 'https://github.com/test/asset'
        handlers = (
            (tdp_updater._HelperRedirect(), ValueError),
            (companion_updates._ReleaseRedirect(url, False), ValueError),
            (controller_firmware._NoRedirect(), controller_firmware.FirmwareError),
            (controller_firmware_catalog._NoRedirect(), controller_firmware_catalog.CatalogError),
        )
        for handler, exception in handlers:
            with self.subTest(handler=type(handler).__module__):
                response = io.BytesIO(b'redirect body')
                with self.assertRaises(exception):
                    handler.redirect_request(urllib.request.Request(url), response, 302,
                                             'Found', {}, 'https://foreign.example/asset')
                self.assertTrue(response.closed)

    def test_allowed_redirect_leaves_response_cleanup_to_urllib(self):
        url = 'https://github.com/test/asset'
        for handler in (tdp_updater._HelperRedirect(), companion_updates._ReleaseRedirect(url, False)):
            with self.subTest(handler=type(handler).__module__), io.BytesIO() as response:
                handler.redirect_request(urllib.request.Request(url), response, 302,
                                         'Found', {}, 'https://release-assets.githubusercontent.com/asset')
                self.assertFalse(response.closed)


class TelemetryFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_tdp_getter_does_not_refresh_the_age_of_a_cached_hardware_sample(self):
        plugin = tdp_backend.Plugin()
        plugin._ready = True
        with patch.object(tdp_backend, '_info_cache', {'package_draw': 12}), \
                patch.object(tdp_backend, '_info_cache_ts', 10), \
                patch.object(tdp_backend.time, 'monotonic', return_value=11):
            fresh = await plugin.get_tdp_info()
            self.assertEqual(fresh['values']['package_draw'], 12)
            self.assertEqual(fresh['sample_age_seconds'], 1)
        with patch.object(tdp_backend, '_info_cache', {'package_draw': 12}), \
                patch.object(tdp_backend, '_info_cache_ts', 10), \
                patch.object(tdp_backend.time, 'monotonic', return_value=100):
            stale = await plugin.get_tdp_info()
            self.assertEqual(stale['values'], {})

    async def test_failed_power_and_limits_read_cannot_keep_previous_measurements(self):
        old = {'package_draw': 12, 'spl_limit': 35}
        with patch.object(tdp_backend, '_info_cache', old), \
                patch.object(tdp_backend, '_read_limits', return_value={}), \
                patch.object(tdp_backend, '_rapl_watts', return_value=None):
            tdp_backend._refresh_info_cache()
            self.assertNotIn('package_draw', old)
            self.assertNotIn('spl_limit', old)


class FullPluginLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_unload_withdraws_fan_lease_before_waiting_for_desktop_operations(self):
        plugin = main.Plugin()
        client = fan_backend.Client()
        plugin._fan._client = client
        entered, release = asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await release.wait()
        plugin._desktop = SimpleNamespace(close=close)
        plugin._firmware = Mock()
        task = asyncio.create_task(plugin._unload())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertTrue(client._stop.is_set(), 'Fan lease must be withdrawn before unrelated drains')
            self.assertFalse(task.done())
        finally:
            release.set()
            await task

    async def test_conflict_withdraws_fan_before_an_admitted_rpc_finishes(self):
        plugin = main.Plugin()
        client = fan_backend.Client()
        plugin._fan._client = client
        plugin._run_stage = AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()
        request = asyncio.create_task(release.wait())
        plugin._rpc_jobs.add(request)
        drain = plugin._drain_rpc_jobs
        async def observed_drain():
            entered.set()
            await drain()
        with patch.object(plugin, '_drain_rpc_jobs', side_effect=observed_drain):
            task = asyncio.create_task(plugin._stop_modules())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertTrue(client._stop.is_set())
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

    async def test_transient_incomplete_plugin_manifest_does_not_unload_healthy_modules(self):
        plugin = main.Plugin()
        plugin._modules_started = True
        plugin._stop_modules = AsyncMock()
        with patch.object(main, '_installed_standalone_plugins', side_effect=[ValueError('Incomplete plugin manifest'), []]) as scan:
            await plugin._check_guard()
        self.assertEqual(scan.call_count, 2)
        self.assertFalse(plugin._guard_blocked)
        self.assertEqual(plugin._guard_error, '')
        plugin._stop_modules.assert_not_awaited()

    async def test_real_conflict_does_not_wait_for_manifest_retry(self):
        plugin = main.Plugin()
        with patch.object(main, '_installed_standalone_plugins', return_value=['LeGoTDP']) as scan:
            await plugin._inspect_guard()
        scan.assert_called_once()
        self.assertTrue(plugin._guard_blocked)

    async def test_persistent_scan_failure_still_withdraws_unverifiable_ownership(self):
        plugin = main.Plugin()
        plugin._modules_started = True
        plugin._stop_modules = AsyncMock()
        with patch.object(main, '_installed_standalone_plugins', side_effect=PermissionError('denied')):
            await plugin._check_guard()
        self.assertTrue(plugin._guard_blocked)
        plugin._stop_modules.assert_awaited_once()

    async def test_updater_socket_cleanup_failure_cannot_skip_hardware_cleanup(self):
        plugin = main.Plugin()
        plugin._controller_updater_gate = SimpleNamespace(close=AsyncMock(side_effect=PermissionError('Endpoint unlink failed')))
        plugin._modules_started = True
        plugin._stop_modules = AsyncMock()
        plugin._firmware = Mock()
        await plugin._unload()
        plugin._stop_modules.assert_awaited_once()
        plugin._firmware.close.assert_called_once()


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

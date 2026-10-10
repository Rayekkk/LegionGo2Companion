# SPDX-License-Identifier: BSD-3-Clause
"""Audit regressions with controlled workers, temporary settings and no hardware."""
import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, patch

import test_modules as fixtures
import test_second_hardware_audit as display_fixtures
import display_backend as display
import main
import module_runtime
import rgb_backend as rgb


class ModuleResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ModuleGateTests.setUp
    make_plugin = fixtures.ModuleGateTests.make_plugin
    asyncTearDown = fixtures.ModuleGateTests.asyncTearDown

    async def test_slow_wifi_does_not_delay_independent_rgb_read(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
            return {'success': True}
        self.plugin._wifi.get_status.side_effect = slow
        task = asyncio.create_task(self.plugin.wifi_get_status())
        await entered.wait()
        try:
            result = await asyncio.wait_for(self.plugin.rgb_get_status(), .5)
            self.assertTrue(result['success'])
        finally:
            release.set()
            await task

    async def test_concurrent_rpc_admission_shares_one_inflight_conflict_scan(self):
        entered, release = threading.Event(), threading.Event()
        def scan():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test scan timeout')
            return []
        with patch.object(main, '_installed_standalone_plugins', side_effect=scan) as scanning:
            tasks = [asyncio.create_task(getattr(self.plugin, name)()) for name in
                     ('rgb_get_status', 'wifi_get_status', 'battery_get_status')]
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                # Give all callers a chance to enter the same held scan.
                for _ in range(20):
                    await asyncio.sleep(0)
            finally:
                release.set()
                await asyncio.gather(*tasks)
        self.assertEqual(scanning.call_count, 1)

    async def test_cancelled_scan_waiter_does_not_cache_the_completed_scan(self):
        entered, release = threading.Event(), threading.Event()
        def scan():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test scan timeout')
            return []
        with patch.object(main, '_installed_standalone_plugins', side_effect=scan):
            caller = asyncio.create_task(self.plugin._inspect_guard())
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            release.set()
            await self.plugin._guard_scan
        with patch.object(main, '_installed_standalone_plugins', return_value=['LeGoTDP']) as scanning:
            await self.plugin._inspect_guard()
        scanning.assert_called_once()
        self.assertTrue(self.plugin._guard_blocked)

    async def test_failed_resume_drains_worker_before_reporting_error(self):
        entered, release = threading.Event(), threading.Event()
        writes = []
        def worker():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test worker timeout')
            writes.append('finished')
        component = rgb.Plugin()
        async def start():
            rgb._drift_task = asyncio.create_task(module_runtime.offload('rgb', worker))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        component._main = start
        self.plugin._rgb = component
        self.plugin._module_state['rgb'] = {'enabled': False, 'resume': {'control_enabled': True}}
        self.plugin._save_modules()
        with patch.object(rgb, '_drift_task', None), \
                patch.object(main, 'restore_intent', new=AsyncMock(side_effect=RuntimeError('resume failed'))):
            task = asyncio.create_task(self.plugin.modules_set_enabled('rgb', True))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                for _ in range(25):
                    await asyncio.sleep(0)
                self.assertFalse(task.done(), 'failed resume must wait for its hardware worker')
            finally:
                release.set()
                with self.assertRaisesRegex(RuntimeError, 'resume failed'):
                    await task
                await module_runtime.drain('rgb')
        self.assertEqual(writes, ['finished'])
        self.assertFalse(module_runtime._workers.get('rgb'))

    async def test_same_module_calls_remain_serial(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
            return {'success': True}
        self.plugin._rgb.get_status.side_effect = slow
        self.plugin._rgb.set_brightness = AsyncMock(return_value={'success': True})
        first = asyncio.create_task(self.plugin.rgb_get_status())
        await entered.wait()
        second = asyncio.create_task(self.plugin.rgb_set_brightness(80))
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            self.plugin._rgb.set_brightness.assert_not_awaited()
        finally:
            release.set()
            await asyncio.gather(first, second)
        self.plugin._rgb.set_brightness.assert_awaited_once_with(80)

    async def test_inputplumber_and_controller_battery_reads_share_resource_lock(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
            return {'success': True}
        self.plugin._remap.get_status.side_effect = slow
        self.plugin._controller.get_battery_levels = AsyncMock(return_value={'available': True})
        first = asyncio.create_task(self.plugin.remap_get_status())
        await entered.wait()
        second = asyncio.create_task(self.plugin.battery_get_controller_levels())
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            self.plugin._controller.get_battery_levels.assert_not_awaited()
        finally:
            release.set()
            await asyncio.gather(first, second)
        self.plugin._controller.get_battery_levels.assert_awaited_once()

    async def test_disable_waits_for_admitted_rpc_and_rejects_future_calls(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
            return {'success': True}
        self.plugin._rgb.get_status.side_effect = slow
        rpc = asyncio.create_task(self.plugin.rgb_get_status())
        await entered.wait()
        with patch.object(main, 'withdraw', new=AsyncMock(return_value='')) as stop:
            disabling = asyncio.create_task(self.plugin.modules_set_enabled('rgb', False))
            try:
                for _ in range(20):
                    await asyncio.sleep(0)
                stop.assert_not_awaited()
            finally:
                release.set()
                await asyncio.gather(rpc, disabling)
            stop.assert_awaited_once()
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            await self.plugin.rgb_get_status()

    async def test_cancelled_rpc_remains_tracked_until_shutdown_drains_it(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
            return {'success': True}
        self.plugin._rgb.get_status.side_effect = slow
        rpc = asyncio.create_task(self.plugin.rgb_get_status())
        await entered.wait()
        rpc.cancel()
        stopping = asyncio.create_task(self.plugin._unload())
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertFalse(rpc.done())
            self.assertFalse(stopping.done())
            self.assertTrue(self.plugin._rpc_jobs)
        finally:
            release.set()
            await stopping
            with self.assertRaises(asyncio.CancelledError):
                await rpc
        self.assertFalse(self.plugin._rpc_jobs)

    async def test_cleanup_error_still_drains_an_offloaded_worker(self):
        entered, release = threading.Event(), threading.Event()
        def worker():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test worker timeout')
        job = asyncio.create_task(module_runtime.offload('rgb', worker))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        self.plugin._rgb._unload.side_effect = RuntimeError('unload failed')
        cleanup = asyncio.create_task(self.plugin._stop_component('rgb'))
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertFalse(cleanup.done())
        finally:
            release.set()
            with self.assertRaisesRegex(RuntimeError, 'unload failed'):
                await cleanup
            await job
        self.assertFalse(module_runtime._workers.get('rgb'))

    async def test_normal_shutdown_drains_cancelled_background_workers(self):
        entered, release = threading.Event(), threading.Event()
        def worker():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test worker timeout')
        job = asyncio.create_task(module_runtime.offload('rgb', worker))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.plugin._modules_started = True
        stopping = asyncio.create_task(self.plugin._unload())
        try:
            for _ in range(30):
                await asyncio.sleep(0)
            self.assertFalse(stopping.done(), 'normal unload must drain background writes too')
        finally:
            release.set()
            await stopping
            await module_runtime.drain('rgb')

    async def test_cancelled_drain_does_not_hide_running_thread(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def worker():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test worker timeout')
            finished.set()
        job = asyncio.create_task(module_runtime.offload('rgb', worker))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        draining = asyncio.create_task(module_runtime.drain('rgb'))
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            draining.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(draining.done(), 'cancellation must wait for actual worker completion')
            self.assertTrue(module_runtime._workers.get('rgb'))
            self.assertFalse(finished.is_set())
        finally:
            release.set()
            await asyncio.gather(draining, job, return_exceptions=True)
            await module_runtime.drain('rgb')
        self.assertTrue(finished.is_set())


class DisplayReleaseTests(unittest.TestCase):
    setUp = display_fixtures.DisplayBaselineTests.setUp

    def test_public_disable_preserves_new_external_brightness(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        self.props.update({display.ATOM_IS_EXTERNAL: 1, display.ATOM_SDR_NITS: display._float_raw(700)})
        with patch.object(display.Plugin, '_refresh_native_support'), \
                patch.object(display, '_write_nits') as writing:
            result = asyncio.run(display.Plugin().set_enabled(False))
        writing.assert_not_called()
        self.assertFalse(result['active'])
        self.assertEqual(display._as_float(self.props[display.ATOM_SDR_NITS]), 700)

    def test_unknown_live_output_defers_restoration_without_forgetting_ownership(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        self.props.pop(display.ATOM_IS_EXTERNAL)
        with patch.object(display, '_write_nits') as writing:
            self.assertFalse(display.Plugin._release())
        writing.assert_not_called()
        self.assertEqual(display.Plugin._last_written, 120.0)
        self.assertTrue(self.state['active'])

    def test_confirmed_owned_brightness_restores_baseline_and_bypasses_watch_cache(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        self.props[display.ATOM_SDR_NITS] = display._float_raw(120)
        with patch.object(display.Plugin, '_session_props', return_value=dict(self.props)) as reading, \
                patch.object(display, '_write_nits', return_value=True) as writing:
            self.assertTrue(display.Plugin._release())
        reading.assert_called_once_with([display.ATOM_IS_EXTERNAL, display.ATOM_SDR_NITS], fresh=True)
        writing.assert_called_once_with(250.0)

    def test_session_change_during_live_read_cannot_restore_old_baseline(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        self.props[display.ATOM_SDR_NITS] = display._float_raw(120)
        with patch.object(display, '_gamescope_start_time', side_effect=[100.0, 200.0]), \
                patch.object(display, '_write_nits') as writing:
            self.assertTrue(display.Plugin._release())
        writing.assert_not_called()
        self.assertIsNone(display.Plugin._last_written)

    def test_backlight_event_after_dock_or_hdr_change_cannot_use_an_old_gate(self):
        for atom, value in ((display.ATOM_IS_EXTERNAL, 1),
                            (display.ATOM_HDR_APP, 1), (display.ATOM_HDR_OUT, 0)):
            with self.subTest(atom=atom):
                display.Plugin._baseline = 250.0
                display.Plugin._last_written = 120.0
                self.state['active'] = True
                self.props.update({display.ATOM_IS_EXTERNAL: 0, display.ATOM_HDR_APP: 0,
                                   display.ATOM_HDR_OUT: 1, display.ATOM_SDR_NITS: display._float_raw(700)})
                self.props[atom] = value
                with patch.object(display, '_write_nits') as writing:
                    self.assertIsNone(display.Plugin._forward_nits(180.0))
                writing.assert_not_called()


if __name__ == '__main__':
    unittest.main()

# SPDX-License-Identifier: BSD-3-Clause
"""Management reads and controller monitoring must not stall unrelated work."""
import asyncio
import threading
import types
import tempfile
import json
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import test_modules as fixtures
import controller_backend as controller
import main
import wifi_backend as wifi


class ModuleResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ModuleGateTests.setUp
    make_plugin = fixtures.ModuleGateTests.make_plugin
    asyncTearDown = fixtures.ModuleGateTests.asyncTearDown

    async def test_module_status_does_not_wait_for_slow_hardware_rpc(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_read():
            entered.set()
            await release.wait()
            return {'success': True}
        self.plugin._wifi.get_status = slow_read
        hardware = asyncio.create_task(self.plugin.wifi_get_status())
        await asyncio.wait_for(entered.wait(), 1)
        status = asyncio.create_task(self.plugin.modules_get_status())
        try:
            done, _ = await asyncio.wait([status], timeout=.2)
            self.assertIn(status, done, 'Management status must not drain unrelated hardware reads.')
            self.assertTrue(status.result()['wifi']['enabled'])
        finally:
            release.set()
            await asyncio.gather(hardware, status, return_exceptions=True)

    async def test_module_status_reports_pending_cleanup_without_waiting_for_it(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_cleanup(*args):
            entered.set()
            await release.wait()
            return 'restored'
        with patch.object(main, 'withdraw', new=slow_cleanup):
            change = asyncio.create_task(self.plugin.modules_set_enabled('wifi', False))
            await asyncio.wait_for(entered.wait(), 1)
            status = asyncio.create_task(self.plugin.modules_get_status())
            try:
                done, _ = await asyncio.wait([status], timeout=.2)
                self.assertIn(status, done, 'Pending restoration must remain visible while the management gate is held.')
                self.assertTrue(status.result()['wifi']['pending'])
            finally:
                release.set()
                await asyncio.gather(change, status, return_exceptions=True)

    async def test_module_mutation_still_waits_for_admitted_hardware_rpc(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_read():
            entered.set()
            await release.wait()
            return {'success': True}
        self.plugin._wifi.get_status = slow_read
        hardware = asyncio.create_task(self.plugin.wifi_get_status())
        await asyncio.wait_for(entered.wait(), 1)
        with patch.object(main, 'withdraw', new=AsyncMock(return_value='restored')) as cleanup:
            change = asyncio.create_task(self.plugin.modules_set_enabled('wifi', False))
            try:
                done, _ = await asyncio.wait([change], timeout=.05)
                self.assertFalse(done)
                cleanup.assert_not_awaited()
            finally:
                release.set()
                await asyncio.gather(hardware, change)
            cleanup.assert_awaited_once()

    async def test_read_only_module_status_still_rejects_conflict_and_shutdown(self):
        for flag in ('_guard_blocked', '_closing'):
            with self.subTest(flag=flag):
                setattr(self.plugin, flag, True)
                try:
                    with self.assertRaises(RuntimeError):
                        await self.plugin.modules_get_status()
                finally:
                    setattr(self.plugin, flag, False)


class ControllerMonitorResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_settings_read_allows_event_loop_heartbeat(self):
        plugin = controller.Plugin()
        loop = asyncio.get_running_loop()
        observations = []
        def slow_state():
            heartbeat = threading.Event()
            loop.call_soon_threadsafe(heartbeat.set)
            observations.append(heartbeat.wait(.5))
            return {'gyro_source': 'system'}
        # End immediately after one scheduled reconciliation tick; no hardware
        # discovery or actual settings writes are involved in this test.
        with patch.object(controller, '_state', side_effect=slow_state), \
                patch.object(controller, '_suspend_offset', return_value=0), \
                patch.object(controller, 'time', types.SimpleNamespace(monotonic=iter([0, 61]).__next__)), \
                patch.object(controller.asyncio, 'sleep', new=AsyncMock(side_effect=[None, asyncio.CancelledError()])):
            with self.assertRaises(asyncio.CancelledError):
                await plugin._watch()
        self.assertEqual(observations, [True], 'Settings recovery and its file lock must not block the event loop.')


class WifiResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def held_io(self, observations, result):
        loop = asyncio.get_running_loop()
        def run(*args, **kwargs):
            heartbeat = threading.Event()
            loop.call_soon_threadsafe(heartbeat.set)
            observations.append(heartbeat.wait(.5))
            return result
        return run

    async def test_power_save_settings_transaction_allows_heartbeat(self):
        plugin = wifi.Plugin()
        observed = []
        with patch.object(plugin, '_load_band_policy_journal', return_value={}), \
                patch.object(plugin, '_load_power_save_journal', return_value={}), \
                patch.object(wifi, '_load_settings', side_effect=self.held_io(observed, dict(wifi.DEFAULT_SETTINGS))):
            result = await plugin.set_power_save(False)
        self.assertTrue(result['success'])
        self.assertEqual(observed, [True])

    async def test_device_discovery_command_allows_heartbeat(self):
        plugin = wifi.Plugin()
        observed = []
        with patch.object(plugin, '_detect_device_family', return_value=('83N0', 'legion_go_2', 'Legion Go 2')), \
                patch.object(plugin, '_run_cmd', side_effect=self.held_io(observed, {'success': True, 'stdout': ''})):
            result = await plugin.get_device_info()
        self.assertTrue(result['success'])
        self.assertEqual(observed, [True])

    async def test_cancelled_power_save_keeps_network_lock_until_worker_finishes(self):
        plugin = wifi.Plugin()
        entered, release = threading.Event(), threading.Event()
        reads = []
        def slow_settings():
            reads.append(True)
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test transaction timeout')
            return dict(wifi.DEFAULT_SETTINGS)
        with patch.object(plugin, '_load_band_policy_journal', return_value={}), \
                patch.object(plugin, '_load_power_save_journal', return_value={}), \
                patch.object(wifi, '_load_settings', side_effect=slow_settings):
            first = asyncio.create_task(plugin.set_power_save(False))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            second = asyncio.create_task(plugin.set_power_save(False))
            try:
                for _ in range(2):
                    first.cancel()
                    await asyncio.sleep(0)
                self.assertFalse(first.done(), 'Cancellation must wait for the transaction worker.')
                self.assertFalse(second.done(), 'The next network mutation must remain outside the lock.')
                self.assertEqual(len(reads), 1)
            finally:
                release.set()
                results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertTrue(results[1]['success'])
        self.assertEqual(len(reads), 2)

    async def test_startup_sanity_command_allows_heartbeat(self):
        plugin = wifi.Plugin()
        observed = []
        with ExitStack() as stack:
            folder = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(patch.object(wifi, 'HEALTH_MARKER_FILE', str(Path(folder) / 'health.json')))
            for name, result in {
                '_rotate_logs': None, '_acquire_band_policy_file_lock': object(),
                '_release_band_policy_file_lock': None,
                '_recover_power_save_journal': {'success': True}, '_recover_band_policy_journal': {'success': True},
                '_detect_distro': {'id': 'steamos', 'name': 'SteamOS'}, '_get_wifi_interface': None,
                '_get_current_backend': 'iwd', '_atomic_write_json': None,
            }.items():
                stack.enter_context(patch.object(plugin, name, return_value=result))
            stack.enter_context(patch.object(plugin, 'get_device_info', new=AsyncMock(return_value={})))
            stack.enter_context(patch.object(wifi, '_load_settings', return_value=dict(wifi.DEFAULT_SETTINGS)))
            stack.enter_context(patch.object(wifi, '_save_settings'))
            stack.enter_context(patch.object(plugin, '_run_cmd', side_effect=self.held_io(observed, {'success': True, 'stdout': 'active'})))
            result = await plugin._main()
        self.assertTrue(result['success'])
        self.assertEqual(observed, [True])

    async def test_reset_and_uninstall_legacy_restore_allow_heartbeat(self):
        for operation in ('reset_settings', '_uninstall'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as folder:
                plugin = wifi.Plugin()
                observed = []
                with patch.object(plugin, 'set_band_policy', new=AsyncMock(return_value={'success': True})), \
                        patch.object(plugin, 'set_power_save', new=AsyncMock(return_value={'success': True})), \
                        patch.object(plugin, '_remove_dispatcher'), \
                        patch.object(wifi, 'ENFORCED_FILE', str(Path(folder) / 'enforced.json')), \
                        patch.object(plugin, '_restore_legacy_bssid_lock', side_effect=self.held_io(observed, {'success': False})):
                    await getattr(plugin, operation)()
                self.assertEqual(observed, [True])

    async def test_worker_power_save_roundtrip_and_failed_apply_restore_durable_baselines(self):
        for fail_runtime in (False, True):
            with self.subTest(fail_runtime=fail_runtime), ExitStack() as stack:
                folder = Path(stack.enter_context(tempfile.TemporaryDirectory()))
                settings_file = folder / 'wifi_settings.json'
                journal_dir = folder / 'journal'
                journal = journal_dir / 'power-save.json'
                settings_file.write_text(json.dumps(wifi.DEFAULT_SETTINGS), encoding='utf-8')
                for name, value in {
                    'SETTINGS_FILE': str(settings_file), 'BAND_POLICY_STATE_DIR': str(journal_dir),
                    'POWER_SAVE_JOURNAL_FILE': str(journal), 'BAND_POLICY_JOURNAL_FILE': str(journal_dir / 'band.json'),
                    '_SETTINGS_MANAGER': None, '_SETTINGS_MANAGER_PATH': '',
                }.items():
                    stack.enter_context(patch.object(wifi, name, value))
                plugin = wifi.Plugin()
                live = {'nm': '3', 'runtime': 'on'}
                worker_threads = []
                def modify(_uuid, _key, value, **kwargs):
                    worker_threads.append(threading.get_ident())
                    live['nm'] = value
                    return {'success': True}
                def run(argv, **kwargs):
                    worker_threads.append(threading.get_ident())
                    if fail_runtime and argv[-1] == 'off':
                        return {'success': False, 'stderr': 'simulated iw failure'}
                    live['runtime'] = argv[-1]
                    return {'success': True}
                for name, result in {'_get_wifi_interface': 'wlan0',
                    '_get_active_connection_uuid': 'da58df8b-cdb1-447d-80ab-19d11a634e7e'}.items():
                    stack.enter_context(patch.object(plugin, name, return_value=result))
                stack.enter_context(patch.object(plugin, '_nmcli_get', side_effect=lambda *args: (live['nm'], {'success': True})))
                stack.enter_context(patch.object(plugin, '_get_runtime_power_save', side_effect=lambda *args: (live['runtime'], {'success': True})))
                stack.enter_context(patch.object(plugin, '_nmcli_modify', side_effect=modify))
                stack.enter_context(patch.object(plugin, '_run_cmd', side_effect=run))
                result = await plugin.set_power_save(True)
                self.assertEqual(result['success'], not fail_runtime, result)
                if fail_runtime:
                    self.assertTrue(result['rolled_back'], result)
                else:
                    saved = json.loads(settings_file.read_text(encoding='utf-8'))
                    self.assertTrue(saved['power_save_disabled'])
                    self.assertEqual((saved['power_save_previous'], saved['power_save_runtime_previous']), ('3', 'on'))
                    self.assertEqual(live, {'nm': '2', 'runtime': 'off'})
                    self.assertTrue((await plugin.set_power_save(False))['success'])
                self.assertEqual(live, {'nm': '3', 'runtime': 'on'})
                self.assertFalse(json.loads(settings_file.read_text(encoding='utf-8'))['power_save_disabled'])
                self.assertFalse(journal.exists())
                self.assertTrue(worker_threads)
                self.assertNotIn(threading.get_ident(), worker_threads, 'nmcli/iw must run outside the event loop.')

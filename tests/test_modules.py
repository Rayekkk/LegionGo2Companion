# SPDX-License-Identifier: BSD-3-Clause
import asyncio
import os
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, patch, Mock
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import support
import main
import module_runtime
import module_control
import display_backend as display
from safe_settings import AtomicSettingsManager
import rgb_backend as rgb
import fan_backend


class ModuleGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.directory = patch.object(main.decky, 'DECKY_PLUGIN_SETTINGS_DIR', self.folder.name)
        self.directory.start()
        self.guard = patch.object(main, '_installed_standalone_plugins', return_value=[])
        self.guard.start()
        self.intent = patch.object(main, 'capture_intent', return_value={})
        self.intent.start()
        self.plugin = self.make_plugin()

    def make_plugin(self):
        plugin = main.Plugin()
        for name in main.MODULE_NAMES:
            setattr(plugin, '_' + name, types.SimpleNamespace(_main=AsyncMock(), _migration=AsyncMock(),
                _unload=AsyncMock(), _uninstall=AsyncMock(), get_status=AsyncMock(return_value={'success': True})))
        return plugin

    async def asyncTearDown(self):
        await self.plugin._unload()
        self.intent.stop(); self.guard.stop(); self.directory.stop(); self.folder.cleanup()

    async def test_start_and_stop_log_a_summary_not_a_record_per_healthy_module(self):
        import collections, json, diagnostics
        records = []
        self.plugin._battery._main.side_effect = RuntimeError('fixture failure')
        self.plugin._start_desktop = AsyncMock()
        with patch.object(diagnostics, '_sink', lambda text, level: records.append(json.loads(text))), \
                patch.object(diagnostics, '_seen', collections.OrderedDict()), \
                patch.object(main, '_migrate_legacy_settings'), patch.object(main, '_reload_component_settings'), \
                patch.object(main, '_runtime_identity', return_value={'model': '83N0'}):
            await self.plugin._main()
            await self.plugin._unload()
        self.assertEqual([(r['component'], r['event']) for r in records],
                         [('plugin', 'startup'), ('plugin', 'runtime'), ('battery', 'module_start_failed'),
                          ('battery', 'module_stopped'), ('battery', 'module_stage_failed'),
                          ('plugin', 'startup_complete'),
                          ('plugin', 'shutdown_started'), ('plugin', 'shutdown_completed')])
        summary = records[5]['context']
        self.assertEqual(summary['failed'], 'battery')
        self.assertEqual(summary['ready'], ','.join(name for name in main.MODULE_NAMES if name != 'battery'))
        self.assertEqual(records[1]['context']['model'], '83N0')

    async def test_disabled_module_is_persisted_gated_and_skipped_on_restart(self):
        with patch.object(main, 'withdraw', new=AsyncMock(return_value='')) as stop:
            result = await self.plugin.modules_set_enabled('battery', False)
        self.assertFalse(result['battery']['enabled']); stop.assert_awaited_once()
        with self.assertRaisesRegex(RuntimeError, 'module is disabled'):
            await self.plugin.battery_get_status()
        self.plugin._battery.get_status.assert_not_awaited()
        fresh = self.make_plugin()
        await fresh._run_stage('_main'); await fresh._run_stage('_migration')
        fresh._battery._main.assert_not_awaited(); fresh._battery._migration.assert_not_awaited()
        fresh._rgb._main.assert_awaited_once()
        await fresh.modules_set_enabled('battery', True)
        fresh._battery._main.assert_awaited_once()
        self.assertTrue((await fresh.battery_get_status())['success'])
        await fresh._unload()

    async def test_failed_cleanup_stays_pending_and_retry_is_required(self):
        with patch.object(main, 'withdraw', new=AsyncMock(side_effect=RuntimeError('device missing'))):
            result = await self.plugin.modules_set_enabled('rgb', False)
        self.assertFalse(result['rgb']['enabled']); self.assertTrue(result['rgb']['pending'])
        self.assertIn('device missing', result['rgb']['error'])
        with self.assertRaisesRegex(RuntimeError, 'finish restoration'):
            await self.plugin.modules_set_enabled('rgb', True)
        with patch.object(main, 'withdraw', new=AsyncMock(return_value='')):
            result = await self.plugin.modules_set_enabled('rgb', False)
        self.assertFalse(result['rgb']['pending'])

    async def test_write_failure_does_not_stop_the_module(self):
        with patch.object(self.plugin._module_store, 'commit', side_effect=OSError('disk full')), \
                patch.object(main, 'withdraw', new=AsyncMock()) as stop:
            with self.assertRaises(OSError):
                await self.plugin.modules_set_enabled('tdp', False)
        stop.assert_not_awaited(); self.assertTrue(self.plugin._module_enabled('tdp'))

    async def test_notification_failure_cannot_abort_hardware_cleanup(self):
        with patch.object(main.decky, 'emit', new=AsyncMock(side_effect=RuntimeError('offline'))), \
                patch.object(main, 'withdraw', new=AsyncMock(return_value='')) as stop:
            result = await self.plugin.modules_set_enabled('controller', False)
        stop.assert_awaited_once(); self.assertFalse(result['controller']['pending'])

    async def test_persisted_pending_cleanup_retries_without_starting_module(self):
        self.plugin._module_state['wifi'] = {'enabled': False, 'pending': True}
        self.plugin._save_modules()
        with patch.object(main, 'withdraw', new=AsyncMock(return_value='')) as stop, \
                patch.object(main, '_migrate_legacy_settings'), patch.object(main, '_reload_component_settings'):
            await self.plugin._main()
        stop.assert_awaited_once_with('wifi', self.plugin._wifi)
        self.plugin._wifi._main.assert_not_awaited()

    async def test_all_module_rpcs_are_gated_but_management_remains_available(self):
        for name in main.MODULE_NAMES:
            self.plugin._module_state[name] = {'enabled': False}
        for method in ['get_settings', 'vibe_get_settings', 'display_get_state', 'display_reset_settings', 'wifi_get_status',
                       'rgb_get_status', 'remap_get_status', 'battery_get_status',
                       'battery_get_controller_levels', 'controller_get_status']:
            with self.subTest(method=method), self.assertRaisesRegex(RuntimeError, 'disabled'):
                await getattr(self.plugin, method)()
        self.assertEqual(len(await self.plugin.modules_get_status()), 9)

    async def test_uninstall_retries_pending_cleanup_without_starting_module(self):
        resume = {'source': 'right'}
        self.plugin._module_state['controller'] = {'enabled': False, 'pending': True, 'resume': resume}
        self.plugin._save_modules()
        self.plugin._ever_started = self.plugin._modules_started = True
        with patch.object(main, 'withdraw', new=AsyncMock(return_value='restored')) as stop:
            await self.plugin._uninstall()
        stop.assert_awaited_once_with('controller', self.plugin._controller)
        self.plugin._controller._main.assert_not_awaited()
        self.plugin._controller._uninstall.assert_not_awaited()
        self.plugin._rgb._uninstall.assert_awaited_once()
        fresh = self.make_plugin()
        self.assertEqual(fresh._module_state['controller'],
                         {'enabled': False, 'note': 'restored', 'resume': resume})

    async def test_uninstall_keeps_failed_cleanup_pending_and_finishes_other_modules(self):
        self.plugin._module_state['rgb'] = {'enabled': False, 'pending': True, 'resume': {'control_enabled': True}}
        self.plugin._save_modules()
        self.plugin._ever_started = self.plugin._modules_started = True
        with patch.object(main, 'withdraw', new=AsyncMock(side_effect=RuntimeError('device missing'))) as stop:
            await self.plugin._uninstall()
        stop.assert_awaited_once_with('rgb', self.plugin._rgb)
        self.plugin._tdp._uninstall.assert_awaited_once()
        fresh = self.make_plugin()
        self.assertTrue(fresh._module_status()['rgb']['pending'])
        self.assertIn('device missing', fresh._module_status()['rgb']['error'])
        self.assertTrue(fresh._module_state['rgb']['resume']['control_enabled'])

    async def test_conflict_blocked_uninstall_never_retries_hardware_cleanup(self):
        self.plugin._module_state['rgb'] = {'enabled': False, 'pending': True}
        self.plugin._ever_started = self.plugin._guard_blocked = True
        with patch.object(main, 'withdraw', new=AsyncMock()) as stop:
            await self.plugin._uninstall()
        stop.assert_not_awaited()
        self.plugin._rgb._uninstall.assert_not_awaited()
        self.assertTrue(self.plugin._module_status()['rgb']['pending'])

    async def test_cancelled_uninstall_finishes_pending_restoration(self):
        entered, finish, other_uninstall = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def stop(*args):
            entered.set()
            await finish.wait()
            return ''
        self.plugin._module_state['rgb'] = {'enabled': False, 'pending': True}
        self.plugin._ever_started = self.plugin._modules_started = True
        self.plugin._tdp._uninstall.side_effect = other_uninstall.set
        with patch.object(main, 'withdraw', side_effect=stop):
            task = asyncio.create_task(self.plugin._uninstall())
            await entered.wait()
            try:
                await asyncio.wait_for(other_uninstall.wait(), 1)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.plugin._tdp._uninstall.assert_awaited_once()
        self.assertFalse(self.make_plugin()._module_status()['rgb']['pending'])

    async def test_duplicate_disable_and_enable_do_not_repeat_work(self):
        with patch.object(main, 'withdraw', new=AsyncMock(return_value='')) as stop:
            await self.plugin.modules_set_enabled('rgb', False)
            await self.plugin.modules_set_enabled('rgb', False)
        stop.assert_awaited_once()
        await self.plugin.modules_set_enabled('rgb', True)
        await self.plugin.modules_set_enabled('rgb', True)
        self.plugin._rgb._main.assert_awaited_once()

    async def test_startup_failure_is_visible_gated_and_retryable_without_losing_intent(self):
        self.plugin._module_state = {name: {'enabled': name == 'rgb'} for name in main.MODULE_NAMES}
        self.plugin._save_modules()
        self.plugin._rgb._main.side_effect = [RuntimeError('RGB settings unreadable'), None]
        await self.plugin._run_stage('_main')
        status = self.plugin._module_status()['rgb']
        self.assertTrue(status['enabled'], 'The saved user choice is retained')
        self.assertFalse(status['ready'])
        self.assertFalse(status['pending'])
        self.assertIn('RGB settings unreadable', status['error'])
        self.assertTrue(self.make_plugin()._module_enabled('rgb'))
        with self.assertRaisesRegex(RuntimeError, 'could not start'):
            await self.plugin.rgb_get_status()
        self.plugin._rgb.get_status.assert_not_awaited()
        result = await self.plugin.modules_set_enabled('rgb', True)
        self.assertTrue(result['rgb']['ready'])
        self.assertEqual(result['rgb']['error'], '')
        self.assertEqual(self.plugin._rgb._main.await_count, 2)
        self.assertEqual(self.plugin._rgb._unload.await_count, 2, 'Cleanup precedes the retry')
        self.assertTrue((await self.plugin.rgb_get_status())['success'])

    async def test_explicit_unsuccessful_start_result_is_exposed(self):
        self.plugin._wifi._main.return_value = {'success': False, 'error': 'Recovery journal needs attention'}
        await self.plugin._run_stage('_main')
        self.assertFalse(self.plugin._module_status()['wifi']['ready'])
        self.assertIn('Recovery journal', self.plugin._module_status()['wifi']['error'])
        self.plugin._wifi._unload.assert_awaited_once()
        self.assertTrue(self.plugin._module_status()['rgb']['ready'])

    async def test_remapper_and_battery_start_errors_are_independent_and_retryable(self):
        for name in ('remap', 'battery'):
            getattr(self.plugin, '_' + name)._main.side_effect = [OSError(name + ' settings unreadable'), None]
        await self.plugin._run_stage('_main')
        self.assertTrue(self.plugin._module_status()['tdp']['ready'])
        for name in ('remap', 'battery'):
            with self.subTest(name=name):
                self.assertFalse(self.plugin._module_status()[name]['ready'])
                self.assertIn('settings unreadable', self.plugin._module_status()[name]['error'])
                result = await self.plugin.modules_set_enabled(name, True)
                self.assertTrue(result[name]['ready'])
                self.assertEqual(result[name]['error'], '')
                self.assertEqual(getattr(self.plugin, '_' + name)._unload.await_count, 2)

    async def test_retry_cannot_start_over_workers_whose_cleanup_failed(self):
        self.plugin._rgb._main.side_effect = RuntimeError('startup failed')
        self.plugin._rgb._unload.side_effect = RuntimeError('worker cleanup failed')
        await self.plugin._run_stage('_main')
        self.assertIn('worker cleanup failed', self.plugin._module_status()['rgb']['error'])
        with self.assertRaisesRegex(RuntimeError, 'worker cleanup failed'):
            await self.plugin.modules_set_enabled('rgb', True)
        self.plugin._rgb._main.assert_awaited_once()
        self.assertTrue(self.plugin._module_status()['rgb']['pending'])

    async def test_extras_network_retry_does_not_hold_other_hardware_rpcs(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def retry():
            entered.set()
            await finish.wait()
            return {'success': True}
        self.plugin._tdp.retry_extras = AsyncMock(side_effect=retry)
        task = asyncio.create_task(self.plugin.retry_extras())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(self.plugin._guard_lock.locked())
            self.assertEqual(len(self.plugin._update_jobs), 1)
            self.assertTrue((await asyncio.wait_for(self.plugin.battery_get_status(), 1))['success'])
        finally:
            finish.set()
            self.assertTrue((await task)['success'])

    async def test_enabling_extras_download_does_not_block_other_tdp_rpcs(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def slow_enable(enabled):
            entered.set()
            await finish.wait()
            return {'success': True}
        self.plugin._tdp.set_extras_unlocked = AsyncMock(side_effect=slow_enable)
        self.plugin._tdp.set_active_app = AsyncMock(return_value={'success': True})
        task = asyncio.create_task(self.plugin.set_extras_unlocked(True))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(self.plugin._rpc_locks['tdp'].locked())
            self.assertTrue((await asyncio.wait_for(self.plugin.set_active_app('123'), 1))['success'])
        finally:
            finish.set()
            self.assertTrue((await task)['success'])

    async def test_invalid_enabled_and_unknown_module_are_rejected(self):
        for name, value in [('unknown', False), ('rgb', 0), ('rgb', 'false')]:
            with self.assertRaises(ValueError): await self.plugin.modules_set_enabled(name, value)

    async def test_preferences_survive_restart_and_reenable_without_old_ownership(self):
        intent = {'managed': True, 'enabled': True}
        with patch.object(main, 'capture_intent', return_value=intent), \
                patch.object(main, 'withdraw', new=AsyncMock(return_value='')):
            await self.plugin.modules_set_enabled('battery', False)
        fresh = self.make_plugin()
        fresh._battery.set_enabled = AsyncMock(return_value={'success': True})
        await fresh.modules_set_enabled('battery', True)
        fresh._battery.set_enabled.assert_awaited_once_with(True)
        self.assertEqual(fresh._module_state['battery'], {'enabled': True})

    async def test_failed_reenable_keeps_intent_and_stops_module(self):
        self.plugin._module_state['battery'] = {'enabled': False, 'resume': {'managed': True, 'enabled': True}}
        self.plugin._battery.set_enabled = AsyncMock(return_value={'success': False, 'error': 'unavailable'})
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            await self.plugin.modules_set_enabled('battery', True)
        self.plugin._battery._unload.assert_awaited_once()
        fresh = self.make_plugin()
        self.assertTrue(fresh._module_state['battery']['pending'])
        self.assertTrue(fresh._module_state['battery']['resume']['enabled'])

    async def test_controller_modules_wait_for_an_updater_left_by_a_previous_process(self):
        # fwupdtool outlives a plugin restart; the new process must not write the controllers meanwhile.
        self.addCleanup(module_runtime.resume_controller)
        self.plugin._module_state['controller'] = {'enabled': True, 'resume': {'source': 'right'}}
        self.plugin._controller.set_gyro_source = AsyncMock()
        finished = threading.Event()
        controllers = [getattr(self.plugin, '_' + name) for name in sorted(module_runtime.CONTROLLER_MODULES)]
        with patch.object(main, 'updater_running', side_effect=lambda: not finished.is_set()), \
                patch.object(main, 'UPDATER_POLL_S', 0.01), \
                patch.object(main, '_migrate_legacy_settings'), patch.object(main, '_reload_component_settings'):
            await self.plugin._main()
            await asyncio.sleep(0.05)
            for component in controllers:
                component._main.assert_not_awaited()
            self.plugin._controller.set_gyro_source.assert_not_awaited()
            self.plugin._tdp._main.assert_awaited_once(); self.plugin._battery._main.assert_awaited_once()
            self.assertTrue(self.plugin._module_status()['vibration']['maintenance'])
            with self.assertRaises(module_runtime.ControllerPaused):
                await self.plugin.rgb_get_status()
            finished.set()
            await asyncio.wait_for(self.plugin._updater_wait, 5)
        self.assertFalse(module_runtime.is_paused('rgb'))
        for component in controllers:
            component._main.assert_awaited_once()
        self.plugin._controller.set_gyro_source.assert_awaited_once_with('right')
        self.plugin._tdp._main.assert_awaited_once()

    async def test_updater_that_never_ends_keeps_controllers_paused(self):
        self.addCleanup(module_runtime.resume_controller)
        with patch.object(main, 'updater_running', return_value=True), patch.object(main, 'UPDATER_WAIT_S', 0), \
                patch.object(main, '_migrate_legacy_settings'), patch.object(main, '_reload_component_settings'):
            await self.plugin._main()
            await asyncio.wait_for(self.plugin._updater_wait, 5)
        self.assertTrue(module_runtime.is_paused('vibration'))
        self.plugin._vibration._main.assert_not_awaited()

    async def test_interrupted_reenable_finishes_saved_intent_on_startup(self):
        self.plugin._module_state['controller'] = {'enabled': True, 'resume': {'source': 'right'}}
        self.plugin._controller.set_gyro_source = AsyncMock()
        with patch.object(main, '_migrate_legacy_settings'), patch.object(main, '_reload_component_settings'):
            await self.plugin._main()
        self.plugin._controller.set_gyro_source.assert_awaited_once_with('right')
        self.assertEqual(self.plugin._module_state['controller'], {'enabled': True})

    async def test_malformed_resume_does_not_start_module(self):
        self.plugin._module_state['battery'] = {'enabled': True, 'resume': 'invalid'}
        self.plugin._save_modules()
        fresh = self.make_plugin()
        self.assertFalse(fresh._module_enabled('battery'))
        self.assertTrue(fresh._module_status()['battery']['error'])
        self.assertFalse(fresh._module_status()['battery']['pending'], 'Unknown settings must not trigger an automatic hardware rollback')

    async def test_lost_module_settings_never_turn_disabled_modules_back_on(self):
        from pathlib import Path
        for suffix in ('', '.bak'):
            Path(self.folder.name, 'module_settings.json' + suffix).write_text('{broken')
        fresh = self.make_plugin()
        with patch.object(main, 'withdraw', new=AsyncMock()) as stop, \
                patch.object(main, '_migrate_legacy_settings'), patch.object(main, '_reload_component_settings'):
            await fresh._main()
        for name in main.MODULE_NAMES:
            self.assertFalse(fresh._module_enabled(name))
            self.assertTrue(fresh._module_status()[name]['error'])
            getattr(fresh, '_' + name)._main.assert_not_awaited()
        stop.assert_not_awaited()
        await fresh._unload()
        again = self.make_plugin()
        self.assertTrue(all(not x['enabled'] for x in again._module_status().values()))

    async def test_old_logs_are_pruned_at_startup_with_wifi_disabled(self):
        self.plugin._module_state['wifi'] = {'enabled': False}
        with tempfile.TemporaryDirectory() as logs:
            for age in range(15):
                path = Path(logs, f'{age:02}.log')
                path.write_text('x')
                os.utime(path, (1_000_000 - age, 1_000_000 - age))
            with patch.object(main.decky, 'DECKY_PLUGIN_LOG_DIR', logs, create=True),                     patch.object(main.diagnostics, 'configure'),                     patch.object(main, '_migrate_legacy_settings'), patch.object(main, '_reload_component_settings'):
                await self.plugin._main()
            self.assertEqual(sorted(p.name for p in Path(logs).glob('*.log')), [f'{i:02}.log' for i in range(10)])
        self.plugin._wifi._main.assert_not_awaited()

    async def test_cleanup_commit_failure_does_not_prevent_other_modules_starting(self):
        self.plugin._module_state['wifi'] = {'enabled': False, 'pending': True}
        self.plugin._save_modules()
        with patch.object(main, 'withdraw', new=AsyncMock(return_value='')), \
                patch.object(self.plugin, '_save_modules', side_effect=OSError('disk full')), \
                patch.object(main, '_migrate_legacy_settings'), patch.object(main, '_reload_component_settings'):
            await self.plugin._main()
        self.plugin._tdp._main.assert_awaited_once()
        self.plugin._wifi._main.assert_not_awaited()
        self.assertTrue(self.plugin._module_status()['wifi']['pending'])
        self.assertIn('disk full', self.plugin._module_status()['wifi']['error'])

    async def test_repeated_cancellation_cannot_release_the_hardware_gate_early(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def stop(*args):
            entered.set(); await finish.wait(); return ''
        with patch.object(main, 'withdraw', side_effect=stop):
            task = asyncio.create_task(self.plugin.modules_set_enabled('rgb', False))
            await entered.wait()
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertTrue(self.plugin._guard_lock.locked())
            finish.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.plugin._module_status()['rgb']['pending'])


class WorkerDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_display_reenable_keeps_edid_module_running_with_native_support(self):
        for state in ({'native_support_detected': True}, {'native_cleanup_pending': True}):
            c = types.SimpleNamespace(set_panel_mode=AsyncMock(return_value={
                **state, 'setup_done': False, 'native_cleanup_error': 'retrying'}))
            await module_control.restore_intent('display', c, {'panel_mode': 'hybrid'})
            c.set_panel_mode.assert_awaited_once_with('hybrid')

    async def test_native_display_withdraw_only_restores_edid(self):
        import display_backend
        restored = []
        c = types.SimpleNamespace(
            _unload=AsyncMock(), native_display_active=lambda: True,
            cleanup_native_display=AsyncMock(return_value={'native_restart_pending': True}),
            _edid_restore=lambda: restored.append('edid') or True)
        with patch.object(display_backend, '_write_atom_int') as write, \
             patch.object(display_backend, '_uninstall_script') as uninstall:
            note = await module_control.withdraw('display', c)
        self.assertEqual(restored, ['edid'])
        c._unload.assert_awaited_once_with(uninstalling=True)
        write.assert_not_called()
        uninstall.assert_not_called()
        self.assertIn('Restart', note)

    async def test_native_cleanup_failure_keeps_module_withdrawal_pending(self):
        c = types.SimpleNamespace(
            _unload=AsyncMock(), native_display_active=lambda: True,
            cleanup_native_display=AsyncMock(return_value={
                'native_cleanup_pending': True, 'native_cleanup_error': 'disk full'}))
        with self.assertRaisesRegex(RuntimeError, 'disk full'):
            await module_control.withdraw('display', c)

    async def test_pending_display_withdrawal_probes_native_before_first_unload(self):
        import display_backend
        detected = False
        events = []

        async def probe():
            nonlocal detected
            events.append('probe')
            detected = True
            return {'native_support_detected': True}

        async def unload(uninstalling=False):
            self.assertTrue(detected, 'Legacy unload must not run before native discovery')
            events.append('unload')

        c = types.SimpleNamespace(
            cleanup_native_display=probe, _unload=unload,
            native_display_active=lambda: detected, _edid_restore=lambda: True)
        with patch.object(display_backend, '_write_atom_int') as write, \
             patch.object(display_backend, '_uninstall_script') as uninstall:
            await module_control.withdraw('display', c)
        self.assertEqual(events[0:2], ['probe', 'unload'])
        write.assert_not_called()
        uninstall.assert_not_called()

    async def test_native_detected_during_unload_fences_final_legacy_writes(self):
        import display_backend
        detected = False

        async def unload(uninstalling=False):
            nonlocal detected
            if uninstalling:
                detected = True

        c = types.SimpleNamespace(
            cleanup_native_display=AsyncMock(return_value={}), _unload=unload,
            native_display_active=lambda: detected, _edid_restore=lambda: True)
        with patch.object(display_backend, '_write_atom_int') as write, \
             patch.object(display_backend, '_uninstall_script') as uninstall:
            await module_control.withdraw('display', c)
        self.assertTrue(detected)
        write.assert_not_called()
        uninstall.assert_not_called()

    async def test_display_reenable_reinstalls_saved_mode_and_checks_result(self):
        c = types.SimpleNamespace(set_panel_mode=AsyncMock(return_value={'setup_done': True}))
        await module_control.restore_intent('display', c, {'panel_mode': 'hybrid'})
        c.set_panel_mode.assert_awaited_once_with('hybrid')
        c.set_panel_mode.return_value = {'setup_done': False, 'setup_error': 'disk full'}
        with self.assertRaisesRegex(RuntimeError, 'disk full'):
            await module_control.restore_intent('display', c, {'panel_mode': 'hybrid'})

    async def test_capture_rgb_intent_excludes_old_hardware_baselines(self):
        import rgb_backend
        state = {'control_enabled': True, 'power_led_managed': True, 'power_led_enabled': False,
                 'original_rgb': {'old': 1}, 'power_led_original_enabled': True}
        with patch.object(rgb_backend, '_load_state', return_value=state):
            intent = module_control.capture_intent('rgb')
        self.assertEqual(set(intent), {'control_enabled', 'power_led_managed', 'power_led_enabled'})
        c = types.SimpleNamespace(set_control_enabled=AsyncMock(return_value={'success': True}),
                                  set_power_led=AsyncMock(return_value={'success': True}))
        await module_control.restore_intent('rgb', c, intent)
        c.set_control_enabled.assert_awaited_once_with(True)
        c.set_power_led.assert_awaited_once_with(False)

    async def test_display_failed_restoration_is_not_reported_as_disabled(self):
        c = types.SimpleNamespace(_unload=AsyncMock(), cleanup_native_display=AsyncMock(return_value={}),
                                  _release=lambda: False, _edid_restore=lambda: True)
        with self.assertRaisesRegex(RuntimeError, 'restoration'):
            await module_control.withdraw('display', c)

    async def test_cancelled_await_does_not_hide_still_running_hardware_worker(self):
        entered, finish = threading.Event(), threading.Event()
        def worker(): entered.set(); finish.wait(2)
        task = asyncio.create_task(module_runtime.offload('test', worker))
        while not entered.is_set(): await asyncio.sleep(.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        draining = asyncio.create_task(module_runtime.drain('test')); await asyncio.sleep(.01)
        self.assertFalse(draining.done()); finish.set(); await draining

    async def test_controller_withdraws_after_close_without_restarting_monitor(self):
        c = types.SimpleNamespace(_unload=AsyncMock(), _release=lambda **kw: None)
        await module_control.withdraw('controller', c)
        c._unload.assert_awaited_once()

    async def test_wifi_cleanup_keeps_saved_profiles(self):
        import wifi_backend
        c = wifi_backend.Plugin()
        with patch.object(c, '_unload', AsyncMock()), \
                patch.object(c, 'set_band_policy', AsyncMock(return_value={'success':True})), \
                patch.object(wifi_backend.os, 'remove') as remove:
            await module_control.withdraw('wifi', c)
        remove.assert_not_called()


class DisplayStartupHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_display_start_is_gated_and_retries_after_storage_recovers(self):
        with tempfile.TemporaryDirectory() as raw, ExitStack() as stack:
            stack.enter_context(patch.object(main.decky, 'DECKY_PLUGIN_SETTINGS_DIR', raw))
            stack.enter_context(patch.object(main, '_installed_standalone_plugins', return_value=[]))
            store = AtomicSettingsManager('display_settings', raw)
            store.replace(dict(display.DEFAULT_SETTINGS))
            stack.enter_context(patch.object(display, 'settings', store))
            state = dict(display.Plugin._state, settings_error='', setup_error='')
            stack.enter_context(patch.object(display.Plugin, '_state', state))
            stack.enter_context(patch.object(display.Plugin, '_task', None))
            stack.enter_context(patch.object(display.Plugin, '_prop_task', None))
            loader = main.Plugin()
            with patch.object(display.Plugin, '_read_settings', side_effect=OSError('disk unavailable')):
                with self.assertRaisesRegex(OSError, 'disk unavailable'):
                    await loader._start_module('display')
                status = loader._module_status()['display']
                self.assertTrue(status['enabled'])
                self.assertFalse(status['ready'])
                self.assertIn('disk unavailable', status['error'])
                with self.assertRaisesRegex(RuntimeError, 'could not start'):
                    await loader.display_get_state()
                self.assertIsNone(loader._display._task)
                self.assertIsNone(loader._display._prop_task)
            for name, value in (('_refresh_native_support', None), ('_refresh_setup', None),
                                ('_release', True), ('_edid_restore', True), ('_edid_pass', None)):
                stack.enter_context(patch.object(display.Plugin, name, return_value=value))
            stack.enter_context(patch.object(display.Plugin, '_session_props', return_value={}))
            stack.enter_context(patch.object(display, '_pick_display', return_value=False))
            stack.enter_context(patch.object(display, '_read_bytes', return_value=None))
            stack.enter_context(patch.object(display, '_identify_panel', return_value=(False, 'fixture')))
            stack.enter_context(patch.object(display, '_find_backlight', return_value=''))
            stack.enter_context(patch.object(display, '_watch_properties', new=AsyncMock()))
            stack.enter_context(patch.object(loader._display, '_loop', new=AsyncMock()))
            stack.enter_context(patch.object(main, 'restore_intent', new=AsyncMock()))
            try:
                result = await loader.modules_set_enabled('display', True)
                self.assertTrue(result['display']['ready'])
                self.assertEqual(result['display']['error'], '')
                self.assertEqual(state['settings_error'], '')
                self.assertEqual(state['setup_error'], '')
                self.assertIsNotNone(loader._display._task)
                self.assertIsNotNone(loader._display._prop_task)
                self.assertEqual((await loader.display_get_state())['settings_error'], '')
            finally:
                await loader._display._unload()
                await module_runtime.drain('display')


class ModuleResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    setUp = ModuleGateTests.setUp
    make_plugin = ModuleGateTests.make_plugin
    asyncTearDown = ModuleGateTests.asyncTearDown

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


class ModuleEventLoopTests(unittest.IsolatedAsyncioTestCase):
    setUp = ModuleGateTests.setUp
    make_plugin = ModuleGateTests.make_plugin
    asyncTearDown = ModuleGateTests.asyncTearDown

    def held_io(self, observations, result=None):
        loop = asyncio.get_running_loop()
        def run():
            heartbeat = threading.Event()
            loop.call_soon_threadsafe(heartbeat.set)
            observations.append(heartbeat.wait(2))
            return result
        return run

    async def test_slow_session_status_probe_keeps_event_loop_responsive(self):
        self.plugin._module_state['display'] = {'enabled': False, 'session': 100.0}
        observed = []
        with patch.object(display, '_gamescope_start_time', side_effect=self.held_io(observed, 200.0)):
            for operation in (self.plugin.modules_get_status, self.plugin.get_version, self.plugin._publish_modules):
                await operation()
        self.assertEqual(observed, [True, True, True], 'Status probes must allow an event-loop heartbeat while I/O waits.')

    async def test_display_withdrawal_session_probe_keeps_event_loop_responsive(self):
        observed = []
        with patch.object(display, '_gamescope_start_time', side_effect=self.held_io(observed, 200.0)), \
                patch.object(main, 'withdraw', new=AsyncMock(return_value='Restart Game Mode')):
            result = await self.plugin.modules_set_enabled('display', False)
        self.assertTrue(observed)
        self.assertTrue(all(observed), 'Recording the withdrawn session must also allow a heartbeat.')
        self.assertEqual(self.plugin._module_state['display']['session'], 200.0)
        self.assertFalse(result['display']['enabled'])

    async def test_cancelled_module_change_finishes_commit_before_withdrawal(self):
        entered, release = threading.Event(), threading.Event()
        original_commit = self.plugin._module_store.commit
        def commit():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test commit timeout')
            return original_commit()
        with patch.object(self.plugin._module_store, 'commit', side_effect=commit), \
                patch.object(main, 'withdraw', new=AsyncMock(return_value='restored')) as withdrawal:
            task = asyncio.create_task(self.plugin.modules_set_enabled('battery', False))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                withdrawal.assert_not_awaited()
            finally:
                release.set()
                result = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        withdrawal.assert_awaited_once()
        saved = self.make_plugin()._module_state['battery']
        self.assertFalse(saved['enabled'])
        self.assertFalse(saved.get('pending'))


class DisplayWithdrawalTests(unittest.IsolatedAsyncioTestCase):
    def component(self, probe, session):
        return types.SimpleNamespace(
            cleanup_native_display=AsyncMock(return_value={}), _unload=AsyncMock(),
            native_display_active=lambda: False, _release=lambda: True,
            _edid_restore=lambda: True, _session_props=probe,
            _ownership_session_current=session)

    async def test_withdrawal_ignores_stale_internal_output_snapshot(self):
        reads = []
        def probe(names, *, fresh=False):
            reads.append(fresh)
            return {display.ATOM_IS_EXTERNAL: '1' if fresh else '0'}
        c = self.component(probe, lambda: True)
        with patch.object(display, '_write_atom_int', return_value=True) as write, \
                patch.object(display, '_uninstall_script', return_value='removed'):
            await module_control.withdraw('display', c)
        self.assertEqual(reads, [True])
        write.assert_called_once_with(display.ATOM_FORCE_HDR_SUPPORT, 0)

    async def test_new_session_during_probe_prevents_atom_writes(self):
        c = self.component(Mock(return_value={display.ATOM_IS_EXTERNAL: '0'}),
                           Mock(side_effect=[True, False]))
        with patch.object(display, '_write_atom_int', return_value=True) as write, \
                patch.object(display, '_uninstall_script', return_value='removed') as uninstall:
            await module_control.withdraw('display', c)
        write.assert_not_called()
        uninstall.assert_called_once()

    async def test_unknown_output_keeps_cleanup_pending(self):
        for props in ({}, {display.ATOM_IS_EXTERNAL: 'invalid'}):
            c = self.component(Mock(return_value=props), lambda: True)
            with self.subTest(props=props), \
                    patch.object(display, '_write_atom_int', return_value=True) as write, \
                    patch.object(display, '_uninstall_script') as uninstall:
                with self.assertRaisesRegex(RuntimeError, 'verify'):
                    await module_control.withdraw('display', c)
            write.assert_not_called()
            uninstall.assert_not_called()

    async def test_unbound_recovery_removes_script_without_changing_session_atoms(self):
        # Pending cleanup runs before _main and may have no bound compositor.
        c = self.component(Mock(), lambda: None)
        with patch.object(display, '_write_atom_int', return_value=True) as write, \
                patch.object(display, '_uninstall_script', return_value='removed') as uninstall:
            note = await module_control.withdraw('display', c)
        write.assert_not_called()
        c._session_props.assert_not_called()
        uninstall.assert_called_once()
        self.assertIn('Restart', note)

    async def test_confirmed_internal_session_releases_both_atoms(self):
        c = self.component(Mock(return_value={display.ATOM_IS_EXTERNAL: '0'}), lambda: True)
        with patch.object(display, '_write_atom_int', return_value=True) as write, \
                patch.object(display, '_uninstall_script', return_value='removed'):
            await module_control.withdraw('display', c)
        self.assertEqual([call.args for call in write.call_args_list], [
            (display.ATOM_FORCE_HDR_SUPPORT, 0), (display.ATOM_HDR_ENABLED, 0)])


class UnloadLifecycleTests(unittest.IsolatedAsyncioTestCase):
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

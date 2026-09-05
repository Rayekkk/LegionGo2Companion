# SPDX-License-Identifier: BSD-3-Clause
import asyncio
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, patch
import test_integration
import main
import module_runtime
import module_control


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
        for method in ['get_settings', 'vibe_get_settings', 'display_get_state', 'wifi_get_status',
                       'rgb_get_status', 'remap_get_status', 'battery_get_status', 'controller_get_status']:
            with self.subTest(method=method), self.assertRaisesRegex(RuntimeError, 'disabled'):
                await getattr(self.plugin, method)()
        self.assertEqual(len(await self.plugin.modules_get_status()), 8)

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

    async def test_cancelled_request_finishes_cleanup_before_releasing_gate(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def stop(*args): entered.set(); await finish.wait(); return ''
        with patch.object(main, 'withdraw', side_effect=stop):
            task = asyncio.create_task(self.plugin.modules_set_enabled('rgb', False))
            await entered.wait(); task.cancel(); await asyncio.sleep(0)
            self.assertTrue(self.plugin._guard_lock.locked())
            finish.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.plugin._module_status()['rgb']['pending'])

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
        c = types.SimpleNamespace(_unload=AsyncMock(), _release=lambda: False, _edid_restore=lambda: True)
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
        c = types.SimpleNamespace(_unload=AsyncMock(), set_band_policy=AsyncMock(return_value={'success':True}),
            set_power_save=AsyncMock(return_value={'success':True}),
            _restore_legacy_bssid_lock=lambda: {'success':True}, _remove_dispatcher=lambda: {'success':True})
        with patch.object(module_control.os, 'remove') as remove:
            await module_control.withdraw('wifi', c)
        self.assertEqual(remove.call_count, 1)
        self.assertNotIn('settings.json', str(remove.call_args))

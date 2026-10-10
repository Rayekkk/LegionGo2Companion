# SPDX-License-Identifier: BSD-3-Clause
"""Second-audit hardware regressions; real settings, isolated device access."""
import asyncio
import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_integration
import display_backend as display
import main
import module_runtime
import safe_settings
import vibration_backend as vibe
from safe_settings import AtomicSettingsManager


class DisplayBaselineTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.store = AtomicSettingsManager('display_settings', self.folder)
        self.store.replace({**display.DEFAULT_SETTINGS, 'panel_mode': 'pq', 'active_mode': 'pq'})
        self.state = dict(display.Plugin._state, active_mode='pq', panel_mode='pq',
            setup_done=True, enabled=True, active=True, panel_ok=True,
            reset_in_progress=False, native_support_detected=False)
        self.props = {display.ATOM_IS_EXTERNAL: 0, display.ATOM_SDR_NITS: display._float_raw(250),
            display.ATOM_HDR_OUT: 1, display.ATOM_HDR_APP: 0,
            display.ATOM_FORCE_HDR_SUPPORT: 0, display.ATOM_HDR_ENABLED: 1}
        self.stack.enter_context(patch.object(display, 'settings', self.store))
        self.stack.enter_context(patch.object(display.Plugin, '_state', self.state))
        self.stack.enter_context(patch.object(display.Plugin, '_baseline', None))
        self.stack.enter_context(patch.object(display.Plugin, '_hdr_baseline', None))
        self.stack.enter_context(patch.object(display.Plugin, '_last_written', None))
        self.stack.enter_context(patch.object(display.Plugin, '_gamescope_started_at', 100.0))
        self.stack.enter_context(patch.object(display.Plugin, '_loaded_script_variant', display.MODE_PQ))
        self.stack.enter_context(patch.object(display.Plugin, '_hybrid_suspended_external', False))
        self.stack.enter_context(patch.object(display.Plugin, '_hybrid_pq_hold_until', 0.0))
        self.stack.enter_context(patch.object(display.Plugin, '_hybrid_sdr_since', None))
        self.stack.enter_context(patch.object(display.Plugin, '_hdr_fights', 0))
        self.stack.enter_context(patch.object(display.Plugin, '_hdr_backoff_until', 0.0))
        self.stack.enter_context(patch.object(display.Plugin, 'native_display_active', return_value=False))
        self.stack.enter_context(patch.object(display.Plugin, '_session_props', side_effect=lambda names, **kwargs: dict(self.props)))
        self.stack.enter_context(patch.object(display, '_gamescope_start_time', return_value=100.0))
        self.stack.enter_context(patch.object(display, '_loaded_script_variant', return_value=display.MODE_PQ))

    def persisted(self):
        return AtomicSettingsManager('display_settings', self.folder).settings

    def fail_primary(self):
        real_write = safe_settings._atomic_write_bytes
        def write(path, data):
            if Path(path) == Path(self.store.path):
                raise OSError('disk full')
            return real_write(path, data)
        return patch.object(safe_settings, '_atomic_write_bytes', side_effect=write)

    def test_brightness_commit_failure_blocks_writes_then_retry_and_reload_restore_original(self):
        writes = []
        def write(value):
            saved = self.persisted()
            self.assertEqual(saved['brightness_baseline'], 250.0,
                             'Baseline must already be durable before an atom changes')
            self.assertEqual(saved['brightness_baseline_session'], 100.0)
            writes.append(value)
            self.props[display.ATOM_SDR_NITS] = display._float_raw(value)
            return True
        with patch.object(display, '_write_nits', side_effect=write) as hardware:
            with self.fail_primary(), self.assertRaisesRegex(RuntimeError, 'recovery state.*disk full'):
                display.Plugin._refresh_gate()
            self.assertFalse(self.state['active'])
            self.assertIsNone(display.Plugin._forward_nits(120.0))
            # Even a stale gate which sets active again must not authorize a
            # write until the missing durable baseline has been captured.
            self.state['active'] = True
            self.assertIsNone(display.Plugin._forward_nits(120.0))
            hardware.assert_not_called()
            self.assertIsNone(display.Plugin._baseline)
            self.assertIsNone(self.store.getSetting('brightness_baseline'))
            self.assertIsNone(self.persisted()['brightness_baseline'])
            self.assertTrue(display.Plugin._refresh_gate())
            self.state['active'] = True
            self.assertTrue(display.Plugin._forward_nits(120.0))
            # A fresh Decky process reloads the same compositor's baseline from
            # the settings, rather than learning its own forwarded value.
            display.Plugin._baseline = None
            display.Plugin._last_written = None
            display.Plugin._gamescope_started_at = None
            display.Plugin._sync_loaded_script()
            self.assertEqual(display.Plugin._baseline, 250.0)
            self.assertTrue(display.Plugin._refresh_gate())
            self.state['active'] = True
            self.assertTrue(display.Plugin._forward_nits(120.0))
            self.assertTrue(display.Plugin._release())
        self.assertEqual(writes, [120.0, 120.0, 250.0])
        self.assertEqual(display._as_float(self.props[display.ATOM_SDR_NITS]), 250.0)

    def test_hdr_commit_failure_blocks_force_and_mode_writes_then_retry_has_durable_baseline(self):
        self.store.replace({**display.DEFAULT_SETTINGS, 'panel_mode': 'hybrid', 'active_mode': 'hybrid'})
        self.state.update(active_mode='hybrid', panel_mode='hybrid')
        self.props.update({display.ATOM_HDR_APP: 1, display.ATOM_HDR_ENABLED: 0, display.ATOM_HDR_OUT: 0})
        writes = []
        def write(name, value):
            saved = self.persisted()
            self.assertIs(saved['hdr_baseline'], False)
            self.assertEqual(saved['hdr_baseline_session'], 100.0)
            writes.append((name, value))
            self.props[name] = value
            return True
        with patch.object(display, '_write_atom_int', side_effect=write) as hardware:
            with self.fail_primary(), self.assertRaisesRegex(RuntimeError, 'recovery state.*disk full'):
                display.Plugin._hybrid_pass()
            hardware.assert_not_called()
            self.assertIsNone(display.Plugin._hdr_baseline)
            self.assertIsNone(self.store.getSetting('hdr_baseline'))
            self.assertIsNone(self.persisted()['hdr_baseline'])
            display.Plugin._hybrid_pass()
            self.assertEqual(writes, [(display.ATOM_FORCE_HDR_SUPPORT, 1), (display.ATOM_HDR_ENABLED, 1)])
            display.Plugin._hdr_baseline = None
            display.Plugin._gamescope_started_at = None
            display.Plugin._sync_loaded_script()
            self.assertIs(display.Plugin._hdr_baseline, False)
            self.assertTrue(display.Plugin._hybrid_release())
        self.assertEqual(writes[-2:], [(display.ATOM_FORCE_HDR_SUPPORT, 0), (display.ATOM_HDR_ENABLED, 0)])
        self.assertIsNone(self.persisted()['hdr_baseline'])

    def test_advisory_metadata_failure_remains_best_effort_without_retaining_rejected_settings(self):
        before = copy.deepcopy(self.store.settings)
        with self.fail_primary():
            display.Plugin._store_settings({'active_mode': 'hybrid'})
        self.assertEqual(self.store.settings, before)
        self.assertEqual(self.persisted(), before)


class VibrationProfileContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.store = AtomicSettingsManager('vibration_settings', self.folder)
        self.profiles = {key: {'overwrite': key != '0', 'settings': {**vibe.DEFAULT_PROFILE, 'level': level}}
            for key, level in (('0', 2), ('111', 0), ('222', 1))}
        self.store.replace({'game_profiles': self.profiles})
        self.stack.enter_context(patch.object(vibe, 'settings', self.store))
        self.stack.enter_context(patch.object(vibe, '_active_app_ts', None))
        self.plugin = vibe.Plugin()

    async def asyncTearDown(self):
        await module_runtime.drain('vibration')

    def persisted(self):
        return AtomicSettingsManager('vibration_settings', self.folder).getSetting('game_profiles')

    async def test_expected_game_and_mutation_share_one_process_lookup(self):
        with patch.object(vibe, 'scan_proc_for_appid', side_effect=['111', '222']) as process, \
                patch.object(vibe, '_apply_settings', return_value=True) as apply:
            result = await self.plugin.set_intensity(3, '111', '111')
        self.assertTrue(result['success'])
        self.assertEqual((result['app_id'], result['profile_id']), ('111', '111'))
        self.assertEqual(process.call_count, 1)
        saved = self.persisted()
        self.assertEqual(saved['111']['settings']['level'], 3)
        self.assertEqual(saved['222']['settings']['level'], 1)
        apply.assert_called_once_with(saved['111']['settings'])

    async def test_expected_global_profile_cannot_be_replaced_by_new_game_override_between_reads(self):
        self.profiles['111']['overwrite'] = False
        self.store.replace({'game_profiles': self.profiles})
        with patch.object(vibe, 'scan_proc_for_appid', side_effect=['111', '222']) as process, \
                patch.object(vibe, '_apply_settings', return_value=True):
            result = await self.plugin.set_touchpad_enabled(False, '111', '0')
        self.assertTrue(result['success'])
        self.assertEqual(process.call_count, 1)
        saved = self.persisted()
        self.assertFalse(saved['0']['settings']['touchpadEnabled'])
        self.assertTrue(saved['222']['settings']['touchpadEnabled'])

    async def test_stale_reset_rejects_game_change_without_saving_or_writing(self):
        before = self.persisted()
        with patch.object(vibe, 'scan_proc_for_appid', return_value='222'), \
                patch.object(vibe, '_save_profiles') as save, \
                patch.object(vibe, '_apply_settings') as apply:
            result = await self.plugin.reset_to_default('111', '111')
        self.assertFalse(result['success'])
        self.assertEqual(result['profile_id'], '222')
        save.assert_not_called()
        apply.assert_not_called()
        self.assertEqual(self.persisted(), before)

    async def test_stale_reset_rejects_overwrite_change_for_the_same_game(self):
        self.profiles['111']['overwrite'] = False
        self.store.replace({'game_profiles': self.profiles})
        before = self.persisted()
        with patch.object(vibe, 'scan_proc_for_appid', return_value='111'), \
                patch.object(vibe, '_save_profiles') as save, \
                patch.object(vibe, '_apply_settings') as apply:
            result = await self.plugin.reset_to_default('111', '111')
        self.assertFalse(result['success'])
        self.assertEqual(result['profile_id'], '0')
        save.assert_not_called()
        apply.assert_not_called()
        self.assertEqual(self.persisted(), before)

    async def test_reset_uses_checked_context_once_and_remains_compatible_without_arguments(self):
        with patch.object(vibe, 'scan_proc_for_appid', side_effect=['111', '222']) as process, \
                patch.object(vibe, '_apply_settings', return_value=True) as apply:
            result = await self.plugin.reset_to_default('111', '111')
            self.assertEqual(process.call_count, 1)
            self.assertEqual(self.persisted()['111']['settings'], vibe.DEFAULT_PROFILE)
            self.assertEqual(self.persisted()['222']['settings']['level'], 1)
            compatible = await self.plugin.reset_to_default()
        self.assertTrue(result['success'])
        self.assertTrue(compatible['success'])
        self.assertEqual(compatible['profile_id'], '222')
        self.assertEqual(apply.call_count, 2)


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


if __name__ == '__main__':
    unittest.main()

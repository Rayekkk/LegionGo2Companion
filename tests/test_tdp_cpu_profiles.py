# SPDX-License-Identifier: BSD-3-Clause
"""CPU profile behavior through real JSON and a temporary CPUFreq hierarchy."""
import asyncio
import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import support
import main
import module_control
import tdp_backend as tdp
from safe_settings import AtomicSettingsManager


class CpuProfileTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.cpu = self.root / 'cpu'
        self.boost = self.cpu / 'cpufreq/boost'
        self.boost.parent.mkdir(parents=True)
        self.boost.write_text('0')
        self.epp_paths = []
        for index in range(2):
            policy = self.cpu / f'cpufreq/policy{index}'
            policy.mkdir()
            epp = policy / 'energy_performance_preference'
            epp.write_text('77')
            self.epp_paths.append(epp)
            (policy / 'energy_performance_available_preferences').write_text(
                'default performance balance_performance balance_power power custom')
        self.store = AtomicSettingsManager('tdp_settings', str(self.root / 'settings'))
        self.base = {'enabled': True, 'extras_unlocked': False,
                     'spl': 25000, 'sppt': 28000, 'fppt': 35000,
                     'cpu_boost_enabled': False, 'epp': '77'}
        self.store.replace({'settings': self.base, 'game_profiles': {}, 'schema_version': 2})
        self.app = ''
        self.ac = False
        self.resumed = False
        self.limits = [25000, 28000, 35000]
        self.applies = []
        def apply(*values, extras=False):
            self.limits[:] = values
            self.applies.append(tuple(values))
            # Firmware changing the platform profile can reset these controls.
            self.hardware(True, '128')
            return {'success': True, 'returncode': 0, 'stdout': '', 'stderr': ''}
        for name, value in {
            'settings': self.store, 'CPU_SYS_ROOT': str(self.cpu), '_current_game_id': '',
            '_current_ac_online': False, '_last_cpu_power_check': 0.0,
            '_transition_key': None, '_transition_failures': 0, '_transition_retry_at': 0.0,
            '_ac_target': (), '_ac_generation': 0, '_ac_changed_at': float('-inf'), '_last_source': 'wmi',
            '_drift_target': (), '_drift_settled': (), '_drift_attempts': 0,
        }.items():
            self.stack.enter_context(patch.object(tdp, name, value))
        for name, callback in {
            '_get_running_appid': lambda: self.app, '_get_ac_online': lambda: self.ac,
            '_resume_detected': lambda: self.resumed,
            '_allowed_ceilings_mw': lambda _state: (35000, 37000, 45000),
            '_apply_limits': apply,
            '_restore_defaults_locked': lambda: tdp._apply_limits(25000, 28000, 35000),
            '_read_limits': lambda: {key + '_limit': self.limits[i] / 1000
                                    for i, key in enumerate(('spl', 'sppt', 'fppt'))},
            '_wmi_profile_lost': lambda: False,
            '_wmi_limits_overridden': lambda _want: False,
        }.items():
            self.stack.enter_context(patch.object(tdp, name, side_effect=callback))
        self.plugin = tdp.Plugin()
        self.plugin._ready = True

    def hardware(self, boost, epp):
        self.boost.write_text('1' if boost else '0')
        for path in self.epp_paths:
            path.write_text(epp)

    def assert_hardware(self, boost, epp):
        self.assertEqual(self.boost.read_text().strip(), '1' if boost else '0')
        self.assertEqual([path.read_text().strip() for path in self.epp_paths], [epp, epp])

    def payload(self):
        fresh = AtomicSettingsManager('tdp_settings', str(self.root / 'settings'))
        return {'settings': fresh.getSetting('settings'),
                'game_profiles': fresh.getSetting('game_profiles'),
                'inactive_game_profiles': fresh.getSetting('inactive_game_profiles')}

    def seed(self, profiles, **global_values):
        self.store.replace({'settings': {**self.base, **global_values},
                            'game_profiles': copy.deepcopy(profiles), 'schema_version': 2})

    def rpc(self, method, *args):
        result = asyncio.run(getattr(self.plugin, method)(*args))
        self.assertTrue(result['ok'], result)
        return result.get('state')

    def game(self, boost=True, epp='204', **values):
        return {'spl': 15000, 'sppt': 18000, 'fppt': 25000,
                'cpu_boost_enabled': boost, 'epp': epp, **values}

    def test_new_preset_ladder_and_legacy_profile_values(self):
        self.assertEqual(tdp.PRESETS_DEFAULT, {
            'silent': {'spl': 8, 'sppt': 15, 'fppt': 20},
            'balanced': {'spl': 16, 'sppt': 25, 'fppt': 30},
            'performance': {'spl': 20, 'sppt': 32, 'fppt': 35},
            'max': {'spl': 35, 'sppt': 37, 'fppt': 45},
        })
        self.seed({'123': self.game(preset='minimum')})
        self.assertEqual(tdp._load_profiles()['123']['spl'], 15000)
        simplified = {**self.base, 'advanced_tdp_control': False}
        self.assertEqual(tdp._profile_target(
            simplified, self.game(preset='balanced'), False),
            (15000, 25000, 30000))
        self.assertEqual(tdp._target_for_preset(
            simplified, 20000, 32000, 35000, 'performance'),
            (20000, 32000, 35000))

    def test_cpu_controls_keep_their_rpc_shape_when_a_read_fails(self):
        status = self.rpc('get_cpu_power_controls')
        self.assertEqual(status['cpu_boost'], {'available': True, 'enabled': False, 'error': ''})
        self.assertEqual(status['epp'], {
            'available': True, 'value': '77', 'numeric_value': 77, 'numeric_supported': True,
            'profiles': ['default', 'performance', 'balance_performance', 'balance_power', 'power'],
            'min': 0, 'max': 255, 'error': ''})
        with patch.object(tdp, '_capture_epp', side_effect=OSError('gone')):
            failed = asyncio.run(self.plugin.get_cpu_power_controls())
        def shape(value):
            return {key: shape(item) for key, item in value.items()} if isinstance(value, dict) else None
        self.assertEqual(shape(failed['state']), shape(status))
        self.assertFalse(failed['ok'])
        self.assertEqual(failed['error'], failed['state']['error'])
        failed = failed['state']
        self.assertEqual((failed['available'], failed['epp']['available'],
                          failed['cpu_boost']['enabled'], failed['epp']['profiles']),
                         (False, False, None, []))

    def test_uninstall_restores_managed_cpu_controls_and_preserves_settings(self):
        before = self.payload()
        asyncio.run(self.plugin._uninstall())
        self.assert_hardware(True, 'balance_performance')
        self.assertEqual(self.payload(), before)

    def test_disabled_tdp_still_releases_cpu_controls_from_game_ac_profile(self):
        self.seed({'111': self.game(boost=None, epp=None,
            ac_cpu_boost_enabled=False, ac_epp='77')}, enabled=False,
            cpu_boost_enabled=None, epp=None)
        before = self.payload()
        asyncio.run(module_control.withdraw('tdp', self.plugin))
        self.assert_hardware(True, 'balance_performance')
        tdp._restore_defaults_locked.assert_not_called()
        self.assertEqual(self.payload(), before)

    def test_failed_firmware_cleanup_does_not_skip_cpu_restoration(self):
        before = self.payload()
        with patch.object(tdp, '_restore_defaults_locked', return_value={
                'success': False, 'stderr': 'firmware did not confirm'}):
            with self.assertRaisesRegex(RuntimeError, 'firmware did not confirm'):
                asyncio.run(self.plugin._uninstall())
        self.assert_hardware(True, 'balance_performance')
        self.assertEqual(self.payload(), before)

    def test_unmanaged_cpu_controls_are_not_changed_by_uninstall(self):
        self.seed({}, enabled=False, cpu_boost_enabled=None, epp=None)
        asyncio.run(self.plugin._uninstall())
        self.assert_hardware(False, '77')

    def test_failed_cpu_cleanup_is_reported_and_remaining_controls_are_attempted(self):
        with patch.object(tdp, '_apply_cpu_boost_hardware', return_value={
                'success': False, 'error': 'boost readback mismatch'}):
            with self.assertRaisesRegex(RuntimeError, 'boost readback mismatch'):
                asyncio.run(module_control.withdraw('tdp', self.plugin))
        self.assertEqual([path.read_text().strip() for path in self.epp_paths],
                         ['balance_performance'] * 2)

    def test_simplified_custom_derives_limits_and_persists_preference(self):
        self.assertTrue(asyncio.run(self.plugin.get_settings())['state']['advanced_tdp_control'])
        self.rpc('set_advanced_tdp_control', False)
        self.assertFalse(self.payload()['settings']['advanced_tdp_control'])
        for spl, expected in ((5000, (5000, 15000, 20000)),
                              (27000, (27000, 37000, 42000)),
                              (35000, (35000, 37000, 45000))):
            with self.subTest(spl=spl):
                self.rpc('apply_tdp', spl, spl, spl, '', 'custom')
                self.assertEqual(tuple(self.limits), expected)
                self.assertEqual(tdp._global_triplet(tdp._load_settings()), expected)
        self.rpc('set_advanced_tdp_control', True)
        self.rpc('apply_tdp', 20000, 21000, 22000, '', 'custom')
        self.assertEqual(tuple(self.limits), (20000, 21000, 22000))

    def test_simplified_custom_game_and_unlocked_ceiling(self):
        state = {**self.base, 'advanced_tdp_control': False}
        profile = self.game(spl=30000, sppt=30000, fppt=30000,
                            preset='custom', ac_separate=True,
                            ac_spl=35000, ac_sppt=35000, ac_fppt=35000,
                            ac_preset='custom')
        self.assertEqual(tdp._profile_target(state, profile, False),
                         (30000, 37000, 45000))
        self.assertEqual(tdp._profile_target(state, profile, True),
                         (35000, 37000, 45000))
        with patch.object(tdp, '_allowed_ceilings_mw', return_value=(50000, 50000, 50000)):
            self.assertEqual(tdp._target_for_preset(
                {**state, 'extras_unlocked': True}, 50000, 50000, 50000, 'custom'),
                (50000, 50000, 50000))

    def test_switching_custom_control_reapplies_without_losing_advanced_values(self):
        self.seed({}, spl=20000, sppt=21000, fppt=22000,
                  active_preset='custom', advanced_tdp_control=True)
        self.rpc('set_advanced_tdp_control', False)
        self.assertEqual(tuple(self.limits), (20000, 30000, 35000))
        self.assertEqual(tuple(self.payload()['settings'][key]
                               for key in ('spl', 'sppt', 'fppt')),
                         (20000, 21000, 22000))
        self.rpc('set_advanced_tdp_control', True)
        self.assertEqual(tuple(self.limits), (20000, 21000, 22000))

    def test_locking_extras_reapplies_simplified_custom_target(self):
        self.seed({}, spl=20000, sppt=21000, fppt=22000,
                  active_preset='custom', advanced_tdp_control=False,
                  extras_unlocked=True)
        self.rpc('set_extras_unlocked', False)
        self.assertEqual(tuple(self.limits), (20000, 30000, 35000))
        self.assertEqual(tuple(self.payload()['settings'][key]
                               for key in ('active_spl', 'active_sppt', 'active_fppt')),
                         (20000, 30000, 35000))

    def test_default_epp_resolves_readback_but_preserves_saved_default(self):
        for custom, readback in ((False, 'balance_performance'), (True, '128')):
            with self.subTest(custom=custom):
                self.seed({}, epp='performance')
                self.hardware(False, 'performance')
                for path in self.epp_paths:
                    path.with_name('energy_performance_available_preferences').write_text(
                        'default performance balance_performance balance_power power' +
                        (' custom' if custom else ''))
                writer = tdp._write_cpu_power_text
                def kernel_write(path, value):
                    writer(path, readback if value == 'default' else value)
                with patch.object(tdp, '_write_cpu_power_text', side_effect=kernel_write):
                    status = self.rpc('set_epp', 'default')
                    self.assertEqual(status['epp']['value'], readback)
                    self.assertEqual(status['profile']['epp'], 'default')
                    self.assertEqual(self.payload()['settings']['epp'], 'default')
                    self.assert_hardware(False, readback)
                    self.hardware(False, 'performance')
                    self.assertEqual(tdp._best_effort_reapply_saved_cpu_power_locked(
                        self.payload()['settings']), [])
                    self.assert_hardware(False, readback)
                    self.rpc('set_epp', 'performance')
                    self.assert_hardware(False, 'performance')

    def test_default_epp_partial_write_failure_restores_concrete_readback(self):
        self.seed({}, epp='performance')
        self.hardware(False, 'performance')
        writer = tdp._write_cpu_power_text
        def kernel_write(path, value):
            if Path(path) == self.epp_paths[1] and value == 'default':
                raise OSError('isolated write failure')
            writer(path, 'balance_performance' if value == 'default' else value)
        with patch.object(tdp, '_write_cpu_power_text', side_effect=kernel_write):
            result = asyncio.run(self.plugin.set_epp('default'))
        self.assertFalse(result['ok'])
        self.assertIn('previous state restored', result['error'])
        self.assert_hardware(False, 'performance')
        self.assertEqual(self.payload()['settings']['epp'], 'performance')

    def test_refused_second_policy_rolls_back_the_first(self):
        self.seed({}, epp='performance')
        self.hardware(False, 'performance')
        writer, written = tdp._write_cpu_power_text, []
        def second_refused(path, value):
            written.append(path)
            if len(written) == 2:
                raise OSError('isolated write failure')  # refused before it took effect
            writer(path, value)
        with patch.object(tdp, '_write_cpu_power_text', side_effect=second_refused):
            result = asyncio.run(self.plugin.set_epp('power'))
        self.assertFalse(result['ok'])
        self.assertIn('previous state restored', result['error'])
        self.assertEqual([p.read_text() for p in self.epp_paths], ['performance'] * 2)

    def test_a_policy_already_at_the_choice_is_not_a_failure(self):
        self.seed({}, epp='performance')
        self.hardware(False, 'performance')
        self.epp_paths[0].write_text('power')
        self.assertTrue(asyncio.run(self.plugin.set_epp('power'))['ok'])
        self.epp_paths[1].write_text('performance')
        self.assertTrue(asyncio.run(self.plugin.set_epp('performance'))['ok'])
        self.assertEqual([p.read_text() for p in self.epp_paths], ['performance'] * 2)

    def test_epp_persistence_failure_never_touches_the_hardware(self):
        self.seed({}, epp='performance')
        self.hardware(False, 'performance')
        with patch.object(tdp, '_write_cpu_power_text') as write, \
                patch.object(tdp, '_write_keys', side_effect=OSError('isolated persistence failure')):
            result = asyncio.run(self.plugin.set_epp('default'))
        self.assertFalse(result['ok'])
        self.assertIn('settings persist failed', result['error'])
        write.assert_not_called()
        self.assert_hardware(False, 'performance')
        self.assertEqual(self.payload()['settings']['epp'], 'performance')

    def test_first_cpu_change_interrupted_mid_write_is_still_released(self):
        # R02: with no earlier choice, only the saved choice makes release restore the control.
        self.seed({}, cpu_boost_enabled=None, epp=None)
        self.hardware(False, '77')
        writer = tdp._write_cpu_power_text
        def crash_after_first_write(path, value):
            writer(path, value)
            raise SystemExit('plugin killed mid-write')
        with patch.object(tdp, '_write_cpu_power_text', side_effect=crash_after_first_write):
            with self.assertRaises(SystemExit):
                asyncio.run(self.plugin.set_epp('power'))
        self.assertEqual(self.epp_paths[0].read_text(), 'power')
        self.assertTrue(tdp._release_controls_locked()['success'])
        self.assertEqual([p.read_text() for p in self.epp_paths], ['balance_performance'] * 2)

    def test_incomplete_rollback_keeps_the_choice_so_release_restores(self):
        self.seed({}, cpu_boost_enabled=None, epp=None)
        self.hardware(False, '77')
        reader, writer = tdp._read_cpu_power_text, tdp._write_cpu_power_text
        written = []
        def write(path, value):
            writer(path, value)
            written.append(path)
        def flaky_read(path):
            if written:  # the write landed, then sysfs reads start failing
                raise OSError('isolated read failure')
            return reader(path)
        with patch.object(tdp, '_read_cpu_power_text', side_effect=flaky_read), \
                patch.object(tdp, '_write_cpu_power_text', side_effect=write):
            result = asyncio.run(self.plugin.set_epp('power'))
        self.assertFalse(result['ok'])
        self.assertIn('rollback incomplete', result['error'])
        self.assertEqual(self.payload()['settings']['epp'], 'power')
        self.assertTrue(tdp._release_controls_locked()['success'])
        self.assertEqual([p.read_text() for p in self.epp_paths], ['balance_performance'] * 2)

    def test_unresolved_default_epp_readback_failure_keeps_the_choice(self):
        self.seed({}, cpu_boost_enabled=None, epp=None)
        self.hardware(False, '77')
        reader, writer = tdp._read_cpu_power_text, tdp._write_cpu_power_text
        failed = []
        def write(path, value):
            writer(path, 'balance_performance' if value == 'default' else value)
        def read_once_failing(path):
            if Path(path) == self.epp_paths[0] and self.epp_paths[0].read_text() != '77' and not failed:
                failed.append(path)
                raise OSError('isolated read failure')
            return reader(path)
        with patch.object(tdp, '_read_cpu_power_text', side_effect=read_once_failing),                 patch.object(tdp, '_write_cpu_power_text', side_effect=write):
            result = asyncio.run(self.plugin.set_epp('default'))
        self.assertFalse(result['ok'])
        self.assertEqual(self.epp_paths[0].read_text(), 'balance_performance')
        self.assertEqual(self.payload()['settings']['epp'], 'default')

    def test_failed_settings_revert_reports_the_hardware_error(self):
        self.seed({}, cpu_boost_enabled=None, epp=None)
        self.hardware(False, '77')
        writer, persist = tdp._write_cpu_power_text, tdp._write_keys
        def refused(path, value):
            writer(path, value)
            if value != '77':
                raise OSError('isolated write failure')
        writes = []
        def persist_once(values):
            writes.append(values)
            if len(writes) > 1:
                raise OSError('disk full on revert')
            persist(values)
        with patch.object(tdp, '_write_cpu_power_text', side_effect=refused),                 patch.object(tdp, '_write_keys', side_effect=persist_once):
            result = asyncio.run(self.plugin.set_epp('power'))
        self.assertFalse(result['ok'])
        self.assertIn('isolated write failure', result['error'])
        self.assertIn('settings revert failed: disk full on revert', result['error'])
        self.assert_hardware(False, '77')

    def test_refused_cpu_change_with_complete_rollback_keeps_the_old_choice(self):
        self.seed({}, cpu_boost_enabled=None, epp=None)
        self.hardware(False, '77')
        writer = tdp._write_cpu_power_text
        def refused(path, value):
            writer(path, value)
            if value != '77':
                raise OSError('isolated write failure')
        with patch.object(tdp, '_write_cpu_power_text', side_effect=refused):
            result = asyncio.run(self.plugin.set_epp('power'))
        self.assertFalse(result['ok'])
        self.assertIn('previous state restored', result['error'])
        self.assertIsNone(self.payload()['settings']['epp'])
        self.assert_hardware(False, '77')

    def test_ignored_write_shows_the_real_hardware_and_keeps_the_old_choice(self):
        self.seed({}, cpu_boost_enabled=False)
        self.hardware(False, '77')
        with patch.object(tdp, '_write_cpu_power_text'):  # the driver accepts and ignores the write
            result = asyncio.run(self.plugin.set_cpu_boost(True))
        self.assertFalse(result['ok'])
        self.assertIn('readback mismatch; previous state restored', result['error'])
        self.assertIs(result['state']['cpu_boost']['enabled'], False)
        self.assertIs(self.payload()['settings']['cpu_boost_enabled'], False)

    def test_default_epp_rollback_preserves_external_writer(self):
        self.seed({}, epp='performance')
        self.hardware(False, 'performance')
        writer = tdp._write_cpu_power_text
        def kernel_write(path, value):
            writer(path, 'balance_performance' if value == 'default' else value)
            if Path(path) == self.epp_paths[1] and value == 'default':
                writer(str(self.epp_paths[0]), 'power')
        with patch.object(tdp, '_write_cpu_power_text', side_effect=kernel_write):
            result = asyncio.run(self.plugin.set_epp('default'))
        self.assertFalse(result['ok'])
        self.assertIn('changed externally', result['error'])
        self.assertEqual([p.read_text() for p in self.epp_paths], ['power', 'performance'])
        self.assertEqual(self.payload()['settings']['epp'], 'performance')

    def test_named_epp_repairs_previous_default_readback_inconsistency(self):
        self.seed({}, epp='performance')
        self.hardware(False, 'performance')
        self.epp_paths[0].write_text('balance_performance')
        self.assertFalse(asyncio.run(self.plugin.get_cpu_power_controls())['ok'])
        self.rpc('set_epp', 'balance_power')
        self.assert_hardware(False, 'balance_power')

    def test_kernel_rollback_quantizes_only_hardware_and_upgrade_restores_exact_values(self):
        self.seed({'111': self.game(ac_separate=True, ac_spl=25000, ac_sppt=28000,
                                   ac_fppt=35000, ac_cpu_boost_enabled=False, ac_epp='26')})
        saved = self.payload()
        for path in self.epp_paths:
            path.with_name('energy_performance_available_preferences').write_text(
                'default performance balance_performance balance_power power')
        self.hardware(False, 'balance_performance')
        self.assertEqual(tdp._best_effort_reapply_saved_cpu_power_locked(tdp._load_settings()), [])
        self.assert_hardware(False, 'balance_performance')
        self.assertIn('Saved EPP 77', self.rpc('get_cpu_power_controls')['epp']['compatibility_note'])
        self.app = '111'
        tdp._check_and_enforce()
        self.assert_hardware(True, 'balance_power')
        self.ac = True
        tdp._check_and_enforce()
        self.assert_hardware(False, 'performance')
        self.assertEqual(self.payload()['game_profiles'], saved['game_profiles'])
        self.assertEqual({key: self.payload()['settings'][key] for key in saved['settings']}, saved['settings'])
        for path in self.epp_paths:
            path.with_name('energy_performance_available_preferences').write_text(
                'default performance balance_performance balance_power power custom')
        self.assertEqual(tdp._best_effort_reapply_saved_cpu_power_locked(tdp._load_settings()), [])
        self.assert_hardware(False, '26')
        self.ac = False
        tdp._check_and_enforce()
        self.assert_hardware(True, '204')
        self.app = ''
        tdp._check_and_enforce()
        self.assert_hardware(False, '77')
        self.assertFalse(self.rpc('get_cpu_power_controls')['epp'].get('compatibility_note'))
        self.assertEqual(self.payload()['game_profiles'], saved['game_profiles'])
        self.assertEqual({key: self.payload()['settings'][key] for key in saved['settings']}, saved['settings'])

    def test_legacy_epp_does_not_advertise_or_save_unsupported_custom_requests(self):
        for path in self.epp_paths:
            path.with_name('energy_performance_available_preferences').write_text('default performance power')
        self.hardware(False, 'performance')
        before = self.payload()
        result = asyncio.run(self.plugin.set_epp('77'))
        self.assertFalse(result['ok'])
        self.assertEqual(self.payload(), before)
        self.assert_hardware(False, 'performance')
        self.assertIsNone(tdp._compatible_saved_epp({'profiles': ['default'], 'numeric_supported': False}, '77'))
        self.assertIsNone(tdp._compatible_saved_epp({'profiles': ['power'], 'numeric_supported': False}, '256'))

    def test_game_cpu_edits_create_profile_without_overwriting_globals(self):
        self.app = '111'
        self.rpc('set_cpu_boost', True, '111', False, '111')
        self.rpc('set_epp', '204', '111', False, '111')
        saved = self.payload()
        self.assertEqual(saved['settings']['cpu_boost_enabled'], False)
        self.assertEqual(saved['settings']['epp'], '77')
        profile = saved['game_profiles']['111']
        self.assertEqual((profile['cpu_boost_enabled'], profile['epp']), (True, '204'))
        self.assertEqual(tuple(profile[key] for key in ('spl', 'sppt', 'fppt')), (25000, 28000, 35000))
        self.assert_hardware(True, '204')

    def test_global_tdp_edit_saves_future_policy_without_displacing_game_or_ac(self):
        for ac_online in (False, True):
            with self.subTest(ac_online=ac_online):
                game = self.game(preset='custom', ac_separate=True,
                                 ac_spl=20000, ac_sppt=27000, ac_fppt=32000,
                                 ac_preset='custom', ac_cpu_boost_enabled=False, ac_epp='26')
                self.seed({'111': game}, active_preset='custom')
                self.app = '111'; self.ac = ac_online
                tdp._current_game_id = ''; tdp._transition_key = None
                tdp._check_and_enforce()
                target = tuple(self.limits)
                generation = tdp._arm_ac_settle(target)
                applies_before = len(self.applies)
                result = self.rpc('apply_tdp', 35000, 37000, 45000, '', 'max', '111')
                self.assertTrue(result['saved_only'])
                self.assertFalse(result['applied'])
                self.assertIn('saved', result['message'])
                self.assertEqual(tuple(self.limits), target)
                self.assertEqual(len(self.applies), applies_before)
                self.assertEqual((tdp._ac_generation, tdp._ac_target), (generation, target))
                saved = self.payload()
                self.assertEqual(saved['game_profiles']['111'], game)
                self.assertEqual(tuple(saved['settings'][key] for key in ('spl', 'sppt', 'fppt')),
                                 (35000, 37000, 45000))
                self.assertEqual(saved['settings']['active_preset'], 'max')
                self.assertEqual(tuple(saved['settings']['active_' + key] for key in ('spl', 'sppt', 'fppt')),
                                 target)
                self.assert_hardware(not ac_online, '26' if ac_online else '204')
                tdp._check_and_enforce()
                self.assertEqual(tuple(self.limits), target)
                self.app = ''
                tdp._check_and_enforce()
                self.assertEqual(self.limits, [35000, 37000, 45000])
                self.assert_hardware(False, '77')

    def test_global_tdp_edit_applies_without_a_game_override(self):
        for running in ('', '111'):
            with self.subTest(running=running):
                self.seed({})
                self.app = running
                result = self.rpc('apply_tdp', 30000, 32000, 40000, '', 'custom', running)
                self.assertTrue(result['applied'])
                self.assertFalse(result['saved_only'])
                self.assertEqual(self.limits, [30000, 32000, 40000])
                self.assertEqual(tuple(self.payload()['settings']['active_' + key]
                                       for key in ('spl', 'sppt', 'fppt')), (30000, 32000, 40000))

    def test_failed_inactive_global_save_preserves_store_hardware_and_ac_ladder(self):
        import safe_settings
        self.seed({'111': self.game()})
        self.app = '111'
        tdp._check_and_enforce()
        target = tuple(self.limits)
        generation = tdp._arm_ac_settle(target)
        before = self.payload()
        applies_before = len(self.applies)
        with patch.object(safe_settings.dk_fs, 'write_atomic', side_effect=OSError('disk full')):
            result = asyncio.run(self.plugin.apply_tdp(30000, 32000, 40000, '', 'custom', '111'))
        self.assertEqual(result, {'ok': False, 'error': 'Could not save global TDP settings: disk full'})
        self.assertEqual(self.payload(), before)
        self.assertEqual(tuple(self.limits), target)
        self.assertEqual(len(self.applies), applies_before, 'A save-only failure must not write hardware during rollback')
        self.assertEqual((tdp._ac_generation, tdp._ac_target), (generation, target))
        self.assert_hardware(True, '204')

    def test_inactive_global_edit_rejects_stale_foreground_before_saving(self):
        self.seed({'111': self.game(), '222': self.game(False, '26')})
        self.app = '222'
        before = self.payload()
        result = asyncio.run(self.plugin.apply_tdp(30000, 32000, 40000, '', 'custom', '111'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'foreground game changed')
        self.assertEqual(self.payload(), before)
        self.assertEqual(self.applies, [])

    def test_active_global_hardware_failure_still_restores_prior_limits(self):
        before = self.payload()
        apply = tdp._apply_limits
        first = True
        def refused(*values, **_extras):
            nonlocal first
            result = apply(*values)
            if first:
                first = False
                return {'success': False, 'returncode': -1, 'stdout': '', 'stderr': 'firmware refused'}
            return result
        with patch.object(tdp, '_apply_limits', side_effect=refused):
            result = asyncio.run(self.plugin.apply_tdp(30000, 32000, 40000, '', 'custom', ''))
        self.assertFalse(result['ok'])
        self.assertIn('previous target restored', result['error'])
        self.assertEqual(self.payload(), before)
        self.assertEqual(self.limits, [25000, 28000, 35000])
        self.assert_hardware(False, '77')

    def test_game_exit_other_game_profile_deletion_and_disabling_restore_global_cpu(self):
        for destination in ('', '222', 'delete', 'disable'):
            with self.subTest(destination=destination):
                self.seed({'111': self.game()})
                self.app = '111'
                tdp._current_game_id = ''
                tdp._transition_key = None
                tdp._check_and_enforce()
                self.assert_hardware(True, '204')
                if destination == 'delete':
                    self.rpc('delete_game_profile', '111')
                elif destination == 'disable':
                    self.rpc('set_plugin_enabled', False)
                else:
                    self.app = destination
                    tdp._check_and_enforce()
                self.assert_hardware(False, '77')
                self.assertEqual(self.limits, [25000, 28000, 35000])

    def test_switch_between_two_games_includes_cpu_even_when_tdp_is_identical(self):
        self.seed({'111': self.game(), '222': self.game(False, '26')})
        self.app = '111'
        tdp._check_and_enforce()
        self.assert_hardware(True, '204')
        self.app = '222'
        tdp._check_and_enforce()
        self.assert_hardware(False, '26')

    def test_ac_profile_edits_do_not_change_active_battery_cpu(self):
        self.seed({'111': self.game()})
        self.app = '111'
        tdp._check_and_enforce()
        self.rpc('set_game_ac_profile', '111', 25000, 28000, 35000, True)
        self.rpc('set_cpu_boost', False, '111', True, '111')
        status = self.rpc('set_epp', '26', '111', True, '111')
        self.assert_hardware(True, '204')
        self.assertFalse(status['profile']['active'])
        self.assertEqual(status['profile']['epp'], '26')
        self.ac = True
        tdp._check_and_enforce()
        self.assert_hardware(False, '26')
        self.ac = False
        tdp._check_and_enforce()
        self.assert_hardware(True, '204')

    def test_battery_edit_on_ac_preserves_live_ac_and_disable_separation_uses_battery(self):
        self.seed({'111': self.game(ac_separate=True, ac_spl=25000, ac_sppt=28000,
                                   ac_fppt=35000, ac_cpu_boost_enabled=False, ac_epp='26')})
        self.app = '111'; self.ac = True
        tdp._check_and_enforce()
        self.rpc('set_epp', '153', '111', False, '111')
        self.assert_hardware(False, '26')
        self.rpc('set_game_ac_profile', '111', 25000, 28000, 35000, False)
        self.assert_hardware(True, '153')

    def test_new_tdp_and_ac_profiles_snapshot_cpu_before_firmware_bounce(self):
        self.app = '111'
        self.rpc('apply_tdp', 15000, 18000, 25000, '111', 'balanced', '111')
        saved = self.payload()['game_profiles']['111']
        self.assertEqual((saved['cpu_boost_enabled'], saved['epp']), (False, '77'))
        self.rpc('set_epp', '204', '111', False, '111')
        self.rpc('set_game_ac_profile', '111', 25000, 28000, 35000, True)
        saved = self.payload()['game_profiles']['111']
        self.assertEqual((saved['ac_cpu_boost_enabled'], saved['ac_epp']), (False, '204'))

    def test_legacy_ac_values_stay_independent_after_battery_cpu_edits(self):
        self.seed({'111': self.game(False, '77', ac_separate=True,
                                   ac_spl=25000, ac_sppt=28000, ac_fppt=35000)})
        self.app = '111'; self.ac = True
        tdp._check_and_enforce()
        self.rpc('set_cpu_boost', True, '111', False, '111')
        self.rpc('set_epp', '204', '111', False, '111')
        self.assert_hardware(False, '77')
        profile = self.payload()['game_profiles']['111']
        self.assertEqual((profile['ac_cpu_boost_enabled'], profile['ac_epp']), (False, '77'))
        # A later drift repair must still use AC's pre-edit settings.
        self.hardware(True, '128')
        self.rpc('reapply')
        self.assert_hardware(False, '77')
        self.ac = False
        tdp._check_and_enforce()
        self.assert_hardware(True, '204')

    def test_turning_a_game_profile_off_keeps_it_unused_until_turned_on(self):
        saved = self.game(ac_separate=True, ac_spl=20000, ac_sppt=23000, ac_fppt=30000, ac_epp='26')
        self.seed({'111': saved})
        self.app = '111'
        tdp._check_and_enforce()
        self.assert_hardware(True, '204')
        self.rpc('set_game_profile_active', '111', False)
        payload = self.payload()
        self.assertEqual(payload['game_profiles'], {})
        self.assertEqual({k: payload['inactive_game_profiles']['111'][k] for k in saved}, saved, 'nothing is lost')
        self.assertEqual(self.limits, [25000, 28000, 35000], 'the running game falls back to Global now')
        self.assert_hardware(False, '77')
        profile = self.rpc_profile('111')
        self.assertEqual((profile['exists'], profile['active'], profile['profile']['spl']), (True, False, 15000))
        # Every existing lookup ignores the unused profile: enforce pass, game switch, AC change.
        for change in ('enforce', 'switch', 'ac'):
            with self.subTest(change=change):
                tdp._current_game_id = ''; tdp._transition_key = None
                if change == 'switch':
                    self.app = ''; asyncio.run(self.plugin.set_active_app('')); self.app = '111'
                    asyncio.run(self.plugin.set_active_app('111'))
                self.ac = change == 'ac'
                tdp._check_and_enforce()
                self.assertEqual(self.limits, [25000, 28000, 35000])
                self.assert_hardware(False, '77')
        self.ac = False
        self.rpc('set_game_profile_active', '111', True)
        payload = self.payload()
        self.assertEqual(payload['inactive_game_profiles'], {})
        self.assertEqual({k: payload['game_profiles']['111'][k] for k in saved}, saved)
        self.assertEqual(self.limits, [15000, 18000, 25000], 'turning it on applies it to the running game')
        self.assert_hardware(True, '204')
        self.assertTrue(self.rpc_profile('111')['active'])

    def rpc_profile(self, app_id):
        return asyncio.run(self.plugin.get_game_profile(app_id))['state']

    def test_turning_on_a_game_without_a_profile_starts_from_global(self):
        self.app = '111'
        self.rpc('set_game_profile_active', '111', True)
        saved = self.payload()['game_profiles']['111']
        self.assertEqual((saved['spl'], saved['sppt'], saved['fppt']), (25000, 28000, 35000))
        self.assertEqual((saved['cpu_boost_enabled'], saved['epp']), (False, '77'))
        self.rpc('set_game_profile_active', '222', False)  # nothing to turn off
        self.assertNotIn('222', self.payload()['inactive_game_profiles'])

    def test_profile_cap_counts_unused_profiles_and_delete_clears_both_keys(self):
        self.store.replace({'settings': self.base, 'game_profiles': {'111': self.game()},
                            'inactive_game_profiles': {'222': self.game(), '333': self.game()}, 'schema_version': 2})
        self.app = '444'
        with patch.object(tdp, 'MAX_GAME_PROFILES', 3):
            for method, args in (('apply_tdp', (15000, 18000, 25000, '444', 'custom', '444')),
                                 ('set_game_profile_active', ('444', True))):
                with self.subTest(method=method), self.assertRaisesRegex(RuntimeError, 'maximum number of game profiles'):
                    asyncio.run(getattr(self.plugin, method)(*args))
            self.app = '222'
            self.rpc('set_game_profile_active', '222', True)  # moving one back does not add a profile
        self.rpc('delete_game_profile', '222')
        self.rpc('delete_game_profile', '333')
        payload = self.payload()
        self.assertEqual((list(payload['game_profiles']), payload['inactive_game_profiles']), (['111'], {}))
        self.assertFalse(self.rpc_profile('333')['exists'])

    def test_profile_list_shows_active_charger_and_kept_profiles(self):
        self.store.replace({'settings': self.base, 'schema_version': 2,
                            'game_profiles': {'111': self.game(preset='balanced'),
                                              '222': self.game(ac_separate=True, ac_spl=25000, ac_preset='performance')},
                            'inactive_game_profiles': {'333': self.game(preset='custom'), '111': self.game()}})
        rows = {row['app_id']: row for row in asyncio.run(self.plugin.list_game_profiles())['state']['profiles']}
        self.assertEqual(sorted(rows), ['111', '222', '333'])
        self.assertEqual((rows['111']['active'], rows['111']['preset'], rows['111']['spl']), (True, 'balanced', 15000))
        self.assertEqual((rows['222']['ac_separate'], rows['222']['ac_spl'], rows['222']['ac_preset']), (True, 25000, 'performance'))
        self.assertEqual((rows['333']['active'], rows['333']['ac_spl']), (False, 15000))

    def test_saved_profile_edits_write_no_hardware_and_never_the_running_game(self):
        self.seed({'111': self.game(preset='balanced'), '222': self.game()})
        self.store.replace({**self.payload(), 'schema_version': 2, 'inactive_game_profiles': {'333': self.game()}})
        self.app = '222'
        self.hardware(False, '77')
        before = list(self.limits)
        result = self.rpc('apply_tdp', 20000, 25000, 30000, '111', 'custom', '222', True)
        self.assertEqual((result['applied'], result['stored']), (False, True))
        self.rpc('set_game_ac_profile', '111', 30000, 35000, 40000, True, 'performance', True)
        self.rpc('set_cpu_boost', False, '111', True, '222', True)
        self.rpc('set_epp', 'power', '111', False, '222', True)
        saved = self.payload()['game_profiles']['111']
        self.assertEqual((saved['spl'], saved['sppt'], saved['fppt'], saved['preset']), (20000, 25000, 30000, 'custom'))
        self.assertEqual((saved['ac_separate'], saved['ac_spl'], saved['ac_preset'], saved['ac_cpu_boost_enabled'], saved['epp']),
                         (True, 30000, 'performance', False, 'power'))
        self.assertEqual(self.limits, before)
        self.assert_hardware(False, '77')
        # The running game, a kept (Global) profile and a game without a profile are refused unchanged.
        snapshot = self.payload()
        for app_id in ('222', '333', '444'):
            with self.subTest(app_id=app_id):
                self.assertFalse(asyncio.run(self.plugin.apply_tdp(9000, 9000, 9000, app_id, 'custom', '222', True))['ok'])
                self.assertFalse(asyncio.run(self.plugin.set_game_ac_profile(app_id, 9000, 9000, 9000, True, '', True))['ok'])
                self.assertFalse(asyncio.run(self.plugin.set_cpu_boost(True, app_id, False, '222', True))['ok'])
        self.assertFalse(asyncio.run(self.plugin.apply_tdp(9000, 9000, 9000, '', 'custom', '222', True))['ok'])
        self.assertEqual(self.payload(), snapshot)

    def test_a_store_above_the_cap_keeps_every_profile_when_one_is_edited(self):
        many = {str(i): self.game() for i in range(1, 515)}
        self.store.replace({'settings': self.base, 'game_profiles': many,
                            'inactive_game_profiles': {'900': self.game()}, 'schema_version': 2})
        self.assertEqual(len(asyncio.run(self.plugin.list_game_profiles())['state']['profiles']), 515)
        self.rpc('apply_tdp', 20000, 22000, 25000, '1', 'custom', '', True)
        payload = self.payload()
        self.assertEqual((len(payload['game_profiles']), len(payload['inactive_game_profiles'])), (514, 1))
        self.assertEqual(payload['game_profiles']['1']['spl'], 20000)
        self.app = '2000'
        with self.assertRaisesRegex(RuntimeError, 'maximum number of game profiles'):
            asyncio.run(self.plugin.set_game_profile_active('2000', True))

    def test_profile_activation_refuses_bad_input_without_writing(self):
        before = self.payload()
        for args in (('', True), ('../x', False), ('111', 'yes'), ('111', 1)):
            with self.subTest(args=args):
                result = asyncio.run(self.plugin.set_game_profile_active(*args))
                self.assertFalse(result['ok'])
        self.assertEqual(self.payload(), before)

    def test_unused_profile_above_standard_limits_is_clamped_when_turned_on(self):
        self.store.replace({'settings': self.base, 'game_profiles': {},
                            'inactive_game_profiles': {'111': self.game(spl=45000, sppt=48000, fppt=50000, preset='custom')},
                            'schema_version': 2})
        self.app = '111'
        with patch.object(tdp, '_standard_ceilings_mw', return_value=(35000, 37000, 45000)):
            self.rpc('set_game_profile_active', '111', True)
        saved = self.payload()['game_profiles']['111']
        self.assertEqual((saved['spl'], saved['sppt'], saved['fppt']), (35000, 37000, 45000))
        self.assertEqual(self.limits, [35000, 37000, 45000])

    def test_saved_extras_spl_above_firmware_ceiling_reads_and_runs_at_the_ceiling(self):
        # 2.2.0 saved SPL up to 50 W with Extras; the SMU never keeps more than 35 W.
        self.seed({'111': self.game(spl=45000, sppt=48000, fppt=50000, preset='custom',
                                    ac_separate=True, ac_spl=45000, ac_sppt=47000,
                                    ac_fppt=50000, ac_preset='custom')},
                  extras_unlocked=True, spl=45000, sppt=48000, fppt=50000)
        with patch.object(tdp, '_allowed_ceilings_mw', return_value=(35000, 50000, 50000)):
            state = asyncio.run(self.plugin.get_settings())['state']
            self.assertEqual((state['spl'], state['sppt'], state['fppt']), (35000, 48000, 50000))
            game = asyncio.run(self.plugin.get_game_profile('111'))['state']
            self.assertEqual(game['profile']['spl'], 35000)
            self.assertEqual((game['ac_profile']['spl'], game['ac_profile']['sppt']), (35000, 47000))
            tdp._check_and_enforce()
            self.assertEqual(self.limits, [35000, 48000, 50000])
            self.app, self.ac = '111', True
            tdp._check_and_enforce()
            self.assertEqual(self.limits, [35000, 47000, 50000])

    def test_failed_tdp_result_preserves_saved_and_running_game_cpu(self):
        for operation, args in (
            ('delete_game_profile', ('111',)),
            ('set_game_profile_active', ('111', False)),
            ('set_game_ac_profile', ('111', 25000, 28000, 35000, False)),
            ('set_plugin_enabled', (False,)),
        ):
            with self.subTest(operation=operation):
                self.seed({'111': self.game(False, '26', ac_separate=True,
                    ac_spl=25000, ac_sppt=28000, ac_fppt=35000,
                    ac_cpu_boost_enabled=True, ac_epp='204')})
                self.app = '111'; self.ac = True
                tdp._current_game_id = ''; tdp._transition_key = None
                tdp._check_and_enforce()
                before = self.payload()
                def failed_apply(*_values, **_extras):
                    self.hardware(True, '128')
                    return {'success': False, 'returncode': -1, 'stdout': '',
                            'stderr': 'firmware refused TDP'}
                with patch.object(tdp, '_apply_limits', side_effect=failed_apply):
                    result = asyncio.run(getattr(self.plugin, operation)(*args))
                self.assertFalse(result['ok'], result)
                self.assertEqual(self.payload(), before)
                self.assert_hardware(True, '204')

    def test_legacy_profiles_inherit_global_without_changing_tdp_or_labels(self):
        old = {'spl': 15000, 'sppt': 18000, 'fppt': 25000, 'preset': 'Legacy',
               'ac_separate': True, 'ac_spl': 30000, 'ac_sppt': 32000, 'ac_fppt': 35000}
        self.seed({'111': old})
        before = self.payload()
        self.app = '111'; self.ac = True
        status = self.rpc('get_cpu_power_controls', '111', True)
        self.assertEqual((status['profile']['cpu_boost_enabled'], status['profile']['epp']), (False, '77'))
        self.assertEqual(self.payload(), before, 'Reading an old profile must not migrate user choices')
        tdp._check_and_enforce()
        self.assert_hardware(False, '77')
        self.assertEqual(self.limits, [30000, 32000, 35000])

    def test_unmanaged_global_values_capture_a_return_baseline(self):
        self.seed({}, cpu_boost_enabled=None, epp=None)
        self.hardware(False, '77')
        self.app = '111'
        self.rpc('set_cpu_boost', True, '111', False, '111')
        self.rpc('set_epp', '204', '111', False, '111')
        saved = self.payload()
        self.assertEqual((saved['settings']['cpu_boost_enabled'], saved['settings']['epp']), (False, '77'))
        tdp._current_game_id = '111'; self.app = ''
        tdp._check_and_enforce()
        self.assert_hardware(False, '77')

    def test_stale_game_invalid_ac_and_invalid_values_never_write(self):
        self.seed({'111': self.game()}); self.app = '222'
        before = self.payload()
        for method, args in (
            ('set_epp', ('204', '111', False, '111')),
            ('set_cpu_boost', (True, '', False, '111')),
            ('set_epp', ('204', '222', True, '222')),
            ('set_epp', ('999', '222', False, '222')),
            ('set_cpu_boost', (1, '222', False, '222')),
        ):
            with self.subTest(method=method, args=args):
                result = asyncio.run(getattr(self.plugin, method)(*args))
                self.assertFalse(result['ok'], result)
                self.assertEqual(self.payload(), before)
                self.assert_hardware(False, '77')

    def test_scope_refusals_keep_their_order_and_reason_and_never_write(self):
        self.seed({'111': self.game(), '222': self.game()}); self.app = '222'
        running = 'game is running; change its settings as the running game'
        cases = (
            ('apply_tdp', (9000, 9000, 9000, '111', '', '111'), 'foreground game changed'),
            ('apply_tdp', (9000, 9000, 9000, '111', '', '111', True), 'foreground game changed'),
            ('apply_tdp', (9000, 9000, 9000, '111'), 'game is no longer active'),
            ('apply_tdp', (9000, 9000, 9000, '222', '', '222', True), running),
            ('apply_tdp', (9000, 9000, 9000, '333', '', '222', True), 'this game has no profile in use'),
            ('apply_tdp', (9000, 9000, 9000, '', '', '222', True), 'invalid TDP request'),
            ('set_cpu_boost', (True, '111', False, '111'), 'foreground game changed'),
            ('set_cpu_boost', (True, '111', False, '111', True), 'foreground game changed'),
            ('set_cpu_boost', (True, '111', False, None), 'game is no longer active'),
            ('set_epp', ('power', '222', False, '222', True), running),
            ('set_epp', ('power', '333', False, '222', True), 'this game has no profile in use'),
            ('set_epp', ('power', '', False, '222', True), 'invalid CPU profile context'),
            ('set_epp', ('power', '111', True, '222', True), 'separate AC profile is disabled'),
            ('set_game_ac_profile', ('111', 9000, 9000, 9000, True), 'game is no longer active'),
            ('set_game_ac_profile', ('222', 9000, 9000, 9000, True, '', True), running),
        )
        before = self.payload()
        for method, args, reason in cases:
            with self.subTest(method=method, args=args):
                result = asyncio.run(getattr(self.plugin, method)(*args))
                self.assertFalse(result['ok'], result)
                self.assertEqual(result.get('error', result.get('stderr')), reason)
                self.assertEqual(self.payload(), before)
                self.assertEqual(self.applies, [])
                self.assert_hardware(False, '77')
        # Disabled TDP refuses a TDP edit first, but a CPU edit checks its scope first.
        self.seed({'111': self.game(), '222': self.game()}, enabled=False)
        self.assertEqual(asyncio.run(self.plugin.apply_tdp(9000, 9000, 9000, '111', '', '111'))['error'],
                         'plugin disabled')
        self.assertEqual(asyncio.run(self.plugin.set_epp('power', '111', False, '111'))['error'],
                         'foreground game changed')
        self.assertEqual(asyncio.run(self.plugin.set_epp('power', '222', False, '222'))['error'],
                         'plugin disabled')

    def test_game_tdp_edit_on_ac_saves_battery_values_and_runs_the_ac_profile(self):
        self.seed({'111': self.game(ac_separate=True, ac_spl=30000, ac_sppt=32000,
                                    ac_fppt=40000, ac_preset='custom')})
        self.app, self.ac = '111', True
        result = self.rpc('apply_tdp', 20000, 22000, 25000, '111', 'custom', '111')
        self.assertEqual((result['applied'], result['saved_only']), (True, False))
        self.assertEqual(self.applies[-1], (30000, 32000, 40000))
        saved = self.payload()
        profile, state = saved['game_profiles']['111'], saved['settings']
        self.assertEqual((profile['spl'], profile['sppt'], profile['fppt'], profile['preset'], profile['ac_spl']),
                         (20000, 22000, 25000, 'custom', 30000))
        self.assertEqual((state['active_spl'], state['active_sppt'], state['active_fppt']), (30000, 32000, 40000))
        self.assertEqual((state['spl'], state['sppt'], state['fppt']), (25000, 28000, 35000))

    def test_cpu_commit_failure_restores_hardware_and_preserves_profiles(self):
        self.seed({'111': self.game(False, '77')}); self.app = '111'
        before = self.payload()
        with patch.object(self.store, 'commit', side_effect=OSError('disk full')):
            for method, value in (('set_cpu_boost', True), ('set_epp', '204')):
                result = asyncio.run(getattr(self.plugin, method)(value, '111', False, '111'))
                self.assertFalse(result['ok'], result)
                self.assert_hardware(False, '77')
                self.assertEqual(self.payload(), before)

    def test_partial_epp_write_failure_restores_every_policy_and_does_not_save(self):
        self.seed({'111': self.game(False, '77')}); self.app = '111'
        before = self.payload()
        real_write = tdp._write_cpu_power_text
        def write(path, value):
            if Path(path) == self.epp_paths[1] and value == '204':
                raise OSError('policy disappeared')
            real_write(path, value)
        with patch.object(tdp, '_write_cpu_power_text', side_effect=write):
            result = asyncio.run(self.plugin.set_epp('204', '111', False, '111'))
        self.assertFalse(result['ok'], result)
        self.assert_hardware(False, '77')
        self.assertEqual(self.payload(), before)

    def test_profile_delete_commit_failure_restores_previous_game_cpu(self):
        self.seed({'111': self.game()}); self.app = '111'
        tdp._check_and_enforce()
        before = self.payload()
        with patch.object(self.store, 'commit', side_effect=OSError('disk full')):
            with self.assertRaises(RuntimeError):
                asyncio.run(self.plugin.delete_game_profile('111'))
        self.assert_hardware(True, '204')
        self.assertEqual(self.payload(), before)

    def test_resume_periodic_drift_and_ac_settle_keep_the_active_cpu_profile(self):
        # A freshly booted CI runner may be younger than the drift interval.
        self.stack.enter_context(patch.object(tdp.time, 'monotonic', return_value=1.0))
        self.seed({'111': self.game(ac_separate=True, ac_spl=25000, ac_sppt=28000,
                                   ac_fppt=35000, ac_cpu_boost_enabled=False, ac_epp='26')})
        self.app = '111'; self.ac = True
        tdp._check_and_enforce()
        for trigger in ('resume', 'periodic', 'reapply', 'settle'):
            self.hardware(True, '128')
            self.resumed = trigger == 'resume'
            if trigger == 'periodic':
                tdp._last_cpu_power_check = tdp.time.monotonic() - tdp.CPU_POWER_DRIFT_CHECK_S - 1
            if trigger in ('resume', 'periodic'):
                tdp._check_and_enforce()
            elif trigger == 'reapply':
                self.rpc('reapply')
            else:
                generation = tdp._arm_ac_settle(tuple(self.limits))
                tdp._reapply_current_target(generation)
            self.assert_hardware(False, '26')
        self.resumed = False

    def test_main_passes_scope_and_expected_game_without_dropping_arguments(self):
        plugin = main.Plugin()
        plugin._check_guard = AsyncMock()
        plugin._startup.set()  # RPCs without _main
        plugin._tdp = type('FakeTdp', (), {
            'get_cpu_power_controls': AsyncMock(return_value={}),
            'set_cpu_boost': AsyncMock(return_value={}),
            'set_epp': AsyncMock(return_value={}),
        })()
        async def scenario():
            await plugin.get_cpu_power_controls('111', True)
            await plugin.set_cpu_boost(False, '111', True, '111')
            await plugin.set_epp('26', '111', True, '111', True)
        asyncio.run(scenario())
        plugin._tdp.get_cpu_power_controls.assert_awaited_once_with('111', True)
        plugin._tdp.set_cpu_boost.assert_awaited_once_with(False, '111', True, '111', False)
        plugin._tdp.set_epp.assert_awaited_once_with('26', '111', True, '111', True)

    def test_failed_new_profile_commit_restores_unmanaged_cpu_before_firmware_bounce(self):
        self.seed({}, cpu_boost_enabled=None, epp=None)
        self.app = '111'
        before = self.payload()
        with patch.object(self.store, 'commit', side_effect=OSError('disk full')):
            with self.assertRaises(RuntimeError):
                asyncio.run(self.plugin.apply_tdp(15000, 18000, 25000, '111', 'balanced', '111'))
        self.assert_hardware(False, '77')
        self.assertEqual(self.payload(), before)

    def test_first_ac_separation_on_unmanaged_legacy_profile_captures_pre_bounce_cpu(self):
        self.seed({'111': {'spl': 15000, 'sppt': 18000, 'fppt': 25000}},
                  cpu_boost_enabled=None, epp=None)
        self.app = '111'; self.ac = True
        self.rpc('set_game_ac_profile', '111', 25000, 28000, 35000, True)
        profile = self.payload()['game_profiles']['111']
        self.assertEqual((profile['ac_cpu_boost_enabled'], profile['ac_epp']), (False, '77'))
        self.assert_hardware(False, '77')

    def test_cold_start_with_running_game_applies_its_cpu_profile(self):
        self.seed({'111': self.game(ac_separate=True, ac_spl=25000, ac_sppt=28000,
                                   ac_fppt=35000, ac_cpu_boost_enabled=True, ac_epp='26')})
        self.app = '111'; self.ac = True
        self.plugin._enforce_loop = AsyncMock()
        self.plugin._info_loop = AsyncMock()
        async def scenario():
            try:
                await self.plugin._main()
                self.assertTrue(self.plugin._ready, self.plugin._setup_error)
                self.assert_hardware(True, '26')
            finally:
                await self.plugin._unload()
        with patch.object(tdp.dk_net, 'tls_context'), \
             patch.object(tdp, '_ensure_ryzenadj'), \
             patch.object(tdp, '_wmi_caps', return_value={
                 key: {'min': 5, 'max': top} for key, top in (('spl', 35), ('sppt', 37), ('fppt', 45))}):
            asyncio.run(scenario())

    def test_invalid_legacy_cpu_fields_inherit_without_changing_unrelated_values(self):
        self.seed({'111': self.game(ac_separate=True, ac_spl=25000, ac_sppt=28000,
                                   ac_fppt=35000, ac_cpu_boost_enabled='false', ac_epp='custom')})
        self.app = '111'; self.ac = True
        before = self.payload()
        status = self.rpc('get_cpu_power_controls', '111', True)
        self.assertEqual((status['profile']['cpu_boost_enabled'], status['profile']['epp']), (True, '204'))
        self.assertEqual(self.payload(), before)


class D2AnswerTests(unittest.TestCase):
    """Every TDP RPC answers D2 itself (DeckyKit PLAN Etap C2, D2 step 2)."""

    def test_a_cpu_status_is_the_state_and_a_failed_outcome_a_refusal_that_carries_it(self):
        status = {'available': True, 'cpu_boost': {}, 'epp': {}, 'error': ''}
        self.assertEqual(tdp._cpu_answer({**status, 'success': True}), {'ok': True, 'state': status})
        refused = {**status, 'error': 'foreground game changed'}
        self.assertEqual(tdp._cpu_answer({**refused, 'success': False}),
                         {'ok': False, 'error': 'foreground game changed', 'state': refused})
        self.assertEqual(tdp._cpu_answer({**status, 'success': False})['error'], tdp.NOT_CONFIRMED)

    def test_an_extras_result_answers_with_its_text_and_no_command_output(self):
        self.assertEqual(tdp._answer(tdp._ok()), {'ok': True})
        self.assertEqual(tdp._answer(tdp._fail('enabled must be a boolean')),
                         {'ok': False, 'error': 'enabled must be a boolean'})
        self.assertEqual(tdp._answer({'success': False}), {'ok': False, 'error': tdp.NOT_CONFIRMED})

    def test_a_tdp_result_keeps_its_fields_as_the_state_and_a_failure_only_its_text(self):
        self.assertEqual(tdp._answer(tdp._ok(applied=False, saved_only=True, stored=True)),
                         {'ok': True, 'state': {'applied': False, 'saved_only': True, 'stored': True}})
        self.assertEqual(tdp._answer(tdp._fail('plugin disabled', applied=False, saved_only=False)),
                         {'ok': False, 'error': 'plugin disabled'})
        self.assertEqual(tdp._answer({'success': False, 'error': 'TDP: restoration was not confirmed'}),
                         {'ok': False, 'error': 'TDP: restoration was not confirmed'})

    def test_reads_answer_their_payload_as_the_state(self):
        plugin = tdp.Plugin()
        self.assertEqual(asyncio.run(plugin.is_ready()), {'ok': True, 'state': {'ready': False, 'error': ''}})
        self.assertEqual(asyncio.run(plugin.set_panel_active(False)), {'ok': True})
        self.assertEqual(asyncio.run(plugin.get_game_profile('../x')), {'ok': False, 'error': 'invalid app id'})
        self.assertEqual(asyncio.run(plugin.apply_tdp(1, 2, 3)), {'ok': False, 'error': 'not ready'})

    def test_power_info_before_the_start_is_a_refusal_without_a_sample(self):
        self.assertEqual(asyncio.run(tdp.Plugin().get_tdp_info()), {'ok': False, 'error': 'not ready'})

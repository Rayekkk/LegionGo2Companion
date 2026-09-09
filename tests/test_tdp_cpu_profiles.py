# SPDX-License-Identifier: BSD-3-Clause
"""CPU profile behavior through real JSON and a temporary CPUFreq hierarchy."""
import asyncio
import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_integration
import main
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
        def apply(*values):
            self.limits[:] = values
            self.applies.append(tuple(values))
            # Firmware changing the platform profile can reset these controls.
            self.hardware(True, '128')
            return {'success': True, 'returncode': 0, 'stdout': '', 'stderr': ''}
        for name, value in {
            'settings': self.store, 'CPU_SYS_ROOT': str(self.cpu), '_current_game_id': '',
            '_current_ac_online': False, '_last_cpu_power_check': 0.0,
            '_transition_key': None, '_transition_failures': 0, '_transition_retry_at': 0.0,
            '_ac_target': (), '_ac_generation': 0, '_last_source': 'wmi',
            '_drift_target': (), '_drift_settled': (), '_drift_attempts': 0,
        }.items():
            self.stack.enter_context(patch.object(tdp, name, value))
        for name, callback in {
            '_get_running_appid': lambda: self.app, '_get_ac_online': lambda: self.ac,
            '_resume_detected': lambda: self.resumed, '_wmi_only': lambda: False,
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
                'game_profiles': fresh.getSetting('game_profiles')}

    def seed(self, profiles, **global_values):
        self.store.replace({'settings': {**self.base, **global_values},
                            'game_profiles': copy.deepcopy(profiles), 'schema_version': 2})

    def rpc(self, method, *args):
        result = asyncio.run(getattr(self.plugin, method)(*args))
        self.assertTrue(result['success'], result)
        return result

    def game(self, boost=True, epp='204', **values):
        return {'spl': 15000, 'sppt': 18000, 'fppt': 25000,
                'cpu_boost_enabled': boost, 'epp': epp, **values}

    def test_kernel_rollback_quantizes_only_hardware_and_upgrade_restores_exact_values(self):
        self.seed({'111': self.game(ac_separate=True, ac_spl=25000, ac_sppt=28000,
                                   ac_fppt=35000, ac_cpu_boost_enabled=False, ac_epp='26')})
        saved = self.payload()
        for path in self.epp_paths:
            path.with_name('energy_performance_available_preferences').write_text(
                'default performance balance_performance balance_power power')
        self.hardware(False, 'balance_performance')
        self.assertEqual(tdp._reapply_saved_cpu_power_controls_locked(tdp._load_settings()), [])
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
        self.assertEqual(tdp._reapply_saved_cpu_power_controls_locked(tdp._load_settings()), [])
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
        self.assertFalse(result['success'])
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

    def test_game_exit_other_game_and_profile_deletion_restore_global_cpu(self):
        for destination in ('', '222', 'delete'):
            with self.subTest(destination=destination):
                self.seed({'111': self.game()})
                self.app = '111'
                tdp._current_game_id = ''
                tdp._transition_key = None
                tdp._check_and_enforce()
                self.assert_hardware(True, '204')
                if destination == 'delete':
                    self.rpc('delete_game_profile', '111')
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

    def test_failed_tdp_result_preserves_saved_and_running_game_cpu(self):
        for operation, args in (
            ('delete_game_profile', ('111',)),
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
                def failed_apply(*_values):
                    self.hardware(True, '128')
                    return {'success': False, 'returncode': -1, 'stdout': '',
                            'stderr': 'firmware refused TDP'}
                with patch.object(tdp, '_apply_limits', side_effect=failed_apply):
                    result = asyncio.run(getattr(self.plugin, operation)(*args))
                self.assertFalse(result['success'], result)
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
                self.assertFalse(result['success'], result)
                self.assertEqual(self.payload(), before)
                self.assert_hardware(False, '77')

    def test_cpu_commit_failure_restores_hardware_and_preserves_profiles(self):
        self.seed({'111': self.game(False, '77')}); self.app = '111'
        before = self.payload()
        with patch.object(self.store, 'commit', side_effect=OSError('disk full')):
            for method, value in (('set_cpu_boost', True), ('set_epp', '204')):
                result = asyncio.run(getattr(self.plugin, method)(value, '111', False, '111'))
                self.assertFalse(result['success'], result)
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
        self.assertFalse(result['success'], result)
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
        plugin._tdp = type('FakeTdp', (), {
            'get_cpu_power_controls': AsyncMock(return_value={}),
            'set_cpu_boost': AsyncMock(return_value={}),
            'set_epp': AsyncMock(return_value={}),
        })()
        async def scenario():
            await plugin.get_cpu_power_controls('111', True)
            await plugin.set_cpu_boost(False, '111', True, '111')
            await plugin.set_epp('26', '111', True, '111')
        asyncio.run(scenario())
        plugin._tdp.get_cpu_power_controls.assert_awaited_once_with('111', True)
        plugin._tdp.set_cpu_boost.assert_awaited_once_with(False, '111', True, '111')
        plugin._tdp.set_epp.assert_awaited_once_with('26', '111', True, '111')

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
        with patch.object(tdp.updater, 'ssl_context'), \
             patch.object(tdp, '_ensure_ryzenadj'), \
             patch.object(tdp, '_wmi_caps', return_value={'present': True}):
            asyncio.run(scenario())

    def test_invalid_legacy_cpu_fields_inherit_without_changing_unrelated_values(self):
        self.seed({'111': self.game(ac_separate=True, ac_spl=25000, ac_sppt=28000,
                                   ac_fppt=35000, ac_cpu_boost_enabled='false', ac_epp='custom')})
        self.app = '111'; self.ac = True
        before = self.payload()
        status = self.rpc('get_cpu_power_controls', '111', True)
        self.assertEqual((status['profile']['cpu_boost_enabled'], status['profile']['epp']), (True, '204'))
        self.assertEqual(self.payload(), before)

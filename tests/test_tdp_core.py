# SPDX-License-Identifier: BSD-3-Clause
"""TDP core behaviour on a fake firmware-attributes / platform-profile sysfs.

The hardware boundary is files plus a fake ryzenadj runner; only time-like
inputs (foreground game, charger, suspend clock) are injected directly.
"""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import copy
import threading

import support  # noqa: F401 - installs the fake decky module
import tdp_backend as tdp
from safe_settings import AtomicSettingsManager
import module_control
import module_runtime
import tdp_backend
import tdp_updater

REAL_RUNNING_APPID = tdp._get_running_appid

INFO = """| STAPM LIMIT | 25.000 | stapm-limit |
| PPT LIMIT FAST | {fast:.3f} | fast-limit |
| PPT LIMIT SLOW | {slow:.3f} | slow-limit |
"""


class TdpCoreTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.firmware = self.root / 'firmware'
        for key, attr in tdp.WMI_ATTRS.items():
            entry = self.firmware / attr
            entry.mkdir(parents=True)
            (entry / 'min_value').write_text('5')
            (entry / 'max_value').write_text(str(tdp.FALLBACK_STD_W[key]))
            (entry / 'current_value').write_text('10')
        node = self.root / 'platform-profile/platform-profile-0'
        node.mkdir(parents=True)
        self.profile = node / 'profile'
        self.profile.write_text('balanced')
        (node / 'choices').write_text('low-power balanced performance custom')
        self.cpu = self.root / 'cpu'
        self.cpu.mkdir()
        self.store = AtomicSettingsManager('tdp_settings', str(self.root / 'settings'))
        self.seed()
        self.app, self.ac = '', False
        self.ryzenadj = []
        self.profile_writes = []
        real_write_profile = tdp._write_profile

        def write_profile(path, value):
            self.profile_writes.append(value)
            return real_write_profile(path, value)

        test = self

        class Ryzenadj:  # the helper process; the real availability gate stays in front
            def __init__(self, argv, **_options):
                test.ryzenadj.append(argv[1:])
                self.returncode = 0

            def communicate(self, timeout=None):
                if test.ryzenadj[-1] == ['--info']:
                    return INFO.format(slow=test.ppt('sppt'), fast=test.ppt('fppt')).encode(), b''
                return b'', b''

        for name, value in {
            'settings': self.store, 'WMI_ROOT': str(self.firmware),
            'PLATFORM_PROFILE_GLOB': str(self.root / 'platform-profile/*/profile'),
            'CPU_SYS_ROOT': str(self.cpu), 'BIN_PATH': str(self.root / 'ryzenadj'),
            '_ryzenadj_available': False,
            '_current_game_id': '', '_current_ac_online': False, '_last_cpu_power_check': 0.0,
            '_last_suspend_offset': 0.0, '_transition_key': None, '_transition_failures': 0,
            '_transition_retry_at': 0.0, '_ac_target': (), '_ac_generation': 0, '_ac_changed_at': float('-inf'),
            '_last_source': '', '_applied_mw': (), '_applied_at': 0.0,
            '_drift_target': (), '_drift_settled': (), '_drift_attempts': 0,
            '_drift_context': (False, ()), '_wmi_verified_at': None, '_wmi_live_limits': (),
            '_limits_cache': {}, '_limits_cache_ts': 0.0, '_panel_active': False,
        }.items():
            self.stack.enter_context(patch.object(tdp, name, value))
        for name, callback in {
            '_get_running_appid': lambda: self.app, '_get_ac_online': lambda: self.ac,
            '_suspend_offset': lambda: 0.0, '_write_profile': write_profile,
            '_legion_go_2': lambda: True,
        }.items():
            self.stack.enter_context(patch.object(tdp, name, side_effect=callback))
        self.stack.enter_context(patch.object(tdp.subprocess, 'Popen', Ryzenadj))
        (self.root / 'ryzenadj').write_bytes(b'')
        self.plugin = tdp.Plugin()
        self.plugin._ready = True

    def seed(self, profiles=None, **settings):
        state = {'enabled': True, 'extras_unlocked': False,
                 'spl': 15000, 'sppt': 20000, 'fppt': 25000, **settings}
        self.store.replace({'settings': state, 'game_profiles': profiles or {},
                            'schema_version': 2})

    def ppt(self, key):
        return int((self.firmware / tdp.WMI_ATTRS[key] / 'current_value').read_text())

    def ppts(self):
        return tuple(self.ppt(key) for key in ('spl', 'sppt', 'fppt'))

    def set_ppts(self, *values):
        for key, value in zip(('spl', 'sppt', 'fppt'), values):
            (self.firmware / tdp.WMI_ATTRS[key] / 'current_value').write_text(str(value))

    def applies(self):
        return [args for args in self.ryzenadj if args != ['--info']]

    def rpc(self, method, *args, **kwargs):
        return asyncio.run(getattr(self.plugin, method)(*args, **kwargs))

    # Firmware path ---------------------------------------------------------

    def test_firmware_apply_bounces_through_low_power_and_reads_back(self):
        result = tdp._apply_limits(18000, 22000, 30000)
        self.assertTrue(result['success'], result)
        self.assertEqual(self.profile_writes, ['low-power', 'custom'])
        self.assertEqual(self.profile.read_text(), 'custom')
        self.assertEqual(self.ppts(), (18, 22, 30))
        self.assertEqual(tdp._last_source, 'wmi')
        self.assertEqual(self.applies(), [])

    def test_firmware_apply_in_custom_writes_in_place_when_it_latches(self):
        self.profile.write_text('custom')
        self.assertTrue(tdp._apply_limits(18000, 22000, 30000)['success'])
        self.assertEqual(self.profile_writes, [])
        self.assertEqual(self.ppts(), (18, 22, 30))

    def test_dropped_in_place_write_falls_back_to_a_bounce(self):
        self.profile.write_text('custom')
        real_write_ppt, calls = tdp._write_ppt, []

        def firmware_drops_first_write(*values, **options):
            calls.append(values)
            if len(calls) > 1:
                real_write_ppt(*values, **options)

        with patch.object(tdp, '_write_ppt', side_effect=firmware_drops_first_write):
            self.assertTrue(tdp._apply_limits(18000, 22000, 30000)['success'])
        self.assertEqual(self.profile_writes, ['low-power', 'custom'])
        self.assertEqual(self.ppts(), (18, 22, 30))

    def test_firmware_readback_mismatch_is_a_failure_naming_the_attribute(self):
        with patch.object(tdp, '_write_ppt'):
            result = tdp._apply_limits(18000, 22000, 30000)
        self.assertFalse(result['success'])
        self.assertIn('ppt_pl1_spl=10 want 18', result['stderr'])
        self.assertEqual(tdp._last_source, '')

    # Clamp and path choice -------------------------------------------------

    def test_hardware_clamp_floor_and_ordering(self):
        self.assertTrue(tdp._apply_limits(1000, 900, 800)['success'])
        self.assertEqual(self.ppts(), (5, 5, 5))
        self.assertTrue(tdp._apply_limits(20000, 10000, 15000)['success'])
        self.assertEqual(self.ppts(), (20, 20, 20))

    def test_out_of_firmware_range_uses_verified_ryzenadj_clamped_to_50w(self):
        tdp._ryzenadj_available = True
        result = tdp._apply_limits(60000, 70000, 80000)
        self.assertTrue(result['success'], result)
        self.assertEqual(self.applies(), [[
            '--stapm-limit=50000', '--slow-limit=50000', '--fast-limit=50000']])
        self.assertEqual(tdp._last_source, 'ryzenadj')
        self.assertEqual(self.profile_writes, [])

    def test_out_of_range_without_verified_helper_writes_nothing(self):
        result = tdp._apply_limits(40000, 45000, 50000)
        self.assertFalse(result['success'])
        self.assertEqual(self.applies(), [])
        self.assertEqual(self.ppts(), (10, 10, 10))

    def test_firmware_failure_inside_range_never_falls_back_to_ryzenadj(self):
        tdp._ryzenadj_available = True
        with patch.object(tdp, '_write_ppt'):
            result = tdp._apply_limits(18000, 22000, 30000)
        self.assertFalse(result['success'])
        self.assertEqual(self.applies(), [])

    def test_rpc_without_extras_is_clamped_to_firmware_ceilings(self):
        tdp._ryzenadj_available = True
        result = self.rpc('apply_tdp', 45000, 48000, 50000)
        self.assertTrue(result['success'], result)
        self.assertEqual(self.ppts(), (35, 37, 45))
        self.assertEqual(self.applies(), [])

    def test_rpc_with_extras_reaches_ryzenadj(self):
        tdp._ryzenadj_available = True
        self.seed(extras_unlocked=True)
        self.assertTrue(self.rpc('apply_tdp', 45000, 48000, 50000)['success'])
        self.assertEqual(self.applies(), [[
            '--stapm-limit=45000', '--slow-limit=48000', '--fast-limit=50000']])

    def test_enabling_extras_downloads_the_helper_first(self):
        with patch.object(tdp, '_ensure_ryzenadj', side_effect=OSError('offline')):
            result = self.rpc('set_extras_unlocked', True)
        self.assertFalse(result['success'])
        self.assertIn('offline', result['stderr'])
        self.assertFalse(self.store.getSetting('settings')['extras_unlocked'])
        with patch.object(tdp, '_ensure_ryzenadj', return_value=True) as ensure:
            self.assertTrue(self.rpc('set_extras_unlocked', True)['success'])
        ensure.assert_called_once()
        self.assertTrue(self.store.getSetting('settings')['extras_unlocked'])
        self.assertTrue(self.rpc('get_caps')['extras'])
        with patch.object(tdp, '_ensure_ryzenadj') as ensure:
            self.assertTrue(self.rpc('set_extras_unlocked', False)['success'])
            self.assertTrue(self.rpc('set_extras_unlocked', True)['success'])
        ensure.assert_not_called()

    def test_extras_taps_during_a_helper_download_apply_in_order(self):
        # Taps queue behind the download instead of failing, and the last one wins:
        # ON (download), ON again, OFF -> both ONs succeed, Extras ends locked.
        downloading, finish = threading.Event(), threading.Event()
        def ensure(cancel=None):
            downloading.set()
            return finish.wait(5)
        async def scenario():
            first = asyncio.create_task(self.plugin.set_extras_unlocked(True))
            await asyncio.to_thread(downloading.wait, 5)
            second = asyncio.create_task(self.plugin.set_extras_unlocked(True))
            off = asyncio.create_task(self.plugin.set_extras_unlocked(False))
            await asyncio.sleep(0.05)
            self.assertFalse(off.done(), 'OFF must not commit before the earlier ON')
            finish.set()
            return [(await task)['success'] for task in (first, second, off)]
        with patch.object(tdp, '_ensure_ryzenadj', side_effect=ensure) as verified:
            self.assertEqual(asyncio.run(scenario()), [True, True, True])
        verified.assert_called_once()
        self.assertFalse(self.store.getSetting('settings')['extras_unlocked'])

    def test_unsupported_model_writes_nothing_and_says_so(self):
        boost = self.cpu / 'cpufreq/boost'
        boost.parent.mkdir(parents=True)
        boost.write_text('0')
        tdp._ryzenadj_available = True
        self.seed(cpu_boost_enabled=True)
        with patch.object(tdp, '_legion_go_2', return_value=False), \
                patch.object(tdp, '_ensure_ryzenadj') as ensure, \
                patch.object(tdp.Plugin, '_ready', False), \
                patch.object(tdp.Plugin, '_setup_error', None):
            self.assertIn('Legion Go 2', tdp._apply_limits(15000, 20000, 25000)['stderr'])
            self.assertFalse(self.rpc('set_cpu_boost', True)['success'])
            caps = self.rpc('get_caps')
            self.assertFalse(caps['tdp_available'])
            self.assertIn('Legion Go 2', caps['tdp_error'])
            self.assertFalse(self.rpc('retry_extras')['success'])
            with tdp._mutation_lock:
                self.assertTrue(tdp._release_controls_locked()['success'])
            plugin = tdp.Plugin()
            asyncio.run(plugin._main())
            self.assertEqual(asyncio.run(plugin.is_ready()),
                             {'ready': False, 'error': tdp.UNSUPPORTED_MODEL})
            self.assertEqual(plugin._tasks, [])
        ensure.assert_not_called()
        self.assertEqual((self.profile.read_text(), boost.read_text()), ('balanced', '0'))
        self.assertEqual((self.ppts(), self.ryzenadj), ((10, 10, 10), []))

    def test_locking_extras_clamps_saved_profiles_and_reapplies(self):
        tdp._ryzenadj_available = True
        self.seed({'123': {'spl': 45000, 'sppt': 48000, 'fppt': 50000, 'preset': 'max'}},
                  extras_unlocked=True, spl=40000, sppt=45000, fppt=50000)
        self.assertTrue(self.rpc('set_extras_unlocked', False)['success'])
        saved = self.store.getSetting('settings')
        self.assertEqual((saved['spl'], saved['sppt'], saved['fppt']), (35000, 37000, 45000))
        game = self.store.getSetting('game_profiles')['123']
        self.assertEqual((game['spl'], game['sppt'], game['fppt'], game['preset']),
                         (35000, 37000, 45000, 'custom'))
        self.assertEqual(self.ppts(), (35, 37, 45))

    # Enforce loop ----------------------------------------------------------

    def test_game_switch_applies_profile_and_exit_restores_global(self):
        self.seed({'123': {'spl': 22000, 'sppt': 26000, 'fppt': 32000}})
        self.app = '123'
        tdp._check_and_enforce()
        self.assertEqual(self.ppts(), (22, 26, 32))
        self.assertEqual(tdp._current_game_id, '123')
        self.app = ''
        tdp._check_and_enforce()
        self.assertEqual(self.ppts(), (15, 20, 25))
        saved = self.store.getSetting('settings')
        self.assertEqual((saved['active_spl'], saved['active_sppt'], saved['active_fppt']),
                         (15000, 20000, 25000))

    def test_game_ac_profile_follows_the_charger(self):
        self.seed({'123': {'spl': 12000, 'sppt': 15000, 'fppt': 20000, 'ac_separate': True,
                           'ac_spl': 30000, 'ac_sppt': 32000, 'ac_fppt': 40000}})
        self.app = '123'
        tdp._check_and_enforce()
        self.assertEqual(self.ppts(), (12, 15, 20))
        self.ac = True
        events = tdp._check_and_enforce()
        self.assertEqual(events['power_source'], {'ac': True})
        self.assertEqual(self.ppts(), (30, 32, 40))

    def test_charger_edge_survives_a_failed_settings_write(self):
        self.seed({'123': {'spl': 12000, 'sppt': 15000, 'fppt': 20000, 'ac_separate': True,
                           'ac_spl': 30000, 'ac_sppt': 32000, 'ac_fppt': 40000}})
        self.app = '123'
        tdp._check_and_enforce()
        self.ac = True
        with patch('safe_settings._atomic_write_bytes', side_effect=OSError(28, 'No space left')):
            with self.assertRaises(OSError):
                tdp._check_and_enforce()  # limits written, then the save fails
        tdp._transition_retry_at = 0.0  # the backoff has passed
        events = tdp._check_and_enforce()
        self.assertEqual(events['power_source'], {'ac': True})
        self.assertEqual(self.ppts(), (30, 32, 40))
        saved = self.store.getSetting('settings')
        self.assertEqual((saved['active_spl'], saved['active_sppt'], saved['active_fppt']),
                         (30000, 32000, 40000))

    def test_charger_ladder_reasserts_after_firmware_reverts_and_stops_when_settled(self):
        tdp._check_and_enforce()
        self.ac = True
        events = tdp._check_and_enforce()
        generation = events['_resettle_generation']
        self.assertEqual(self.ppts(), (15, 20, 25))
        # The firmware applies its own profile a moment after the transition.
        self.profile.write_text('low-power')
        self.set_ppts(10, 15, 20)
        self.profile_writes.clear()
        self.assertTrue(tdp._reapply_current_target(generation))
        self.assertEqual(self.ppts(), (15, 20, 25))
        self.assertEqual(self.profile_writes, ['low-power', 'custom'])
        self.profile_writes.clear()
        self.assertTrue(tdp._reapply_current_target(generation))
        self.assertEqual(self.profile_writes, [])

    def test_game_switch_right_after_plugging_in_restarts_the_charger_ladder(self):
        self.seed({'123': {'spl': 22000, 'sppt': 26000, 'fppt': 32000}})
        tdp._check_and_enforce()
        self.ac = True
        first = tdp._check_and_enforce()['_resettle_generation']
        self.app = '123'
        events = tdp._check_and_enforce()
        self.assertNotIn('power_source', events)
        generation = events['_resettle_generation']
        self.assertNotEqual(generation, first)
        self.set_ppts(10, 15, 20)  # the firmware's late AC write
        self.assertTrue(tdp._reapply_current_target(generation))
        self.assertEqual(self.ppts(), (22, 26, 32))

    def test_stale_charger_ladder_never_reaches_hardware(self):
        tdp._check_and_enforce()
        self.ac = True
        generation = tdp._check_and_enforce()['_resettle_generation']
        self.assertTrue(self.rpc('apply_tdp', 20000, 25000, 30000)['success'])
        self.set_ppts(10, 15, 20)
        self.assertTrue(tdp._reapply_current_target(generation))
        self.assertEqual(self.ppts(), (10, 15, 20))

    def test_drift_is_reasserted_then_bounded(self):
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.assertEqual(self.ppts(), (15, 20, 25))
        self.profile.write_text('balanced')  # something knocked us off custom
        tdp._check_and_enforce()
        self.assertEqual(self.profile.read_text(), 'custom')
        with patch.object(tdp, '_write_ppt'):
            self.set_ppts(8, 8, 8)
            for _ in range(tdp.DRIFT_MAX_ATTEMPTS + 3):
                tdp._check_and_enforce()
        self.assertEqual(tdp._drift_attempts, tdp.DRIFT_MAX_ATTEMPTS)

    def test_unreachable_target_with_a_persistent_live_mismatch_stays_bounded(self):
        self.profile.write_text('custom')
        self.set_ppts(15, 20, 25)
        clock = [100.0]
        with patch.object(tdp, '_ryzenadj_available', True), patch.object(tdp, '_last_source', 'wmi'), \
                patch.object(tdp.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(tdp, '_write_ppt'), \
                patch.object(tdp, '_apply_limits_with_saved_cpu_power',
                             wraps=tdp._apply_limits_with_saved_cpu_power) as apply:
            # 5 s passes; the live cross-check only reads every 30 s (True, None, None, ...).
            for seconds in range(0, 106, 5):
                clock[0] = 100.0 + seconds
                tdp._enforce_target((25000, 30000, 35000))
            self.assertEqual(apply.call_count, tdp.DRIFT_MAX_ATTEMPTS)
            self.set_ppts(15, 22, 27)  # the hardware really moved: a fresh budget
            clock[0] += 30
            tdp._enforce_target((25000, 30000, 35000))
            self.assertEqual(apply.call_count, tdp.DRIFT_MAX_ATTEMPTS + 1)

    def test_disabled_module_writes_no_limits(self):
        self.seed(enabled=False)
        self.app = '123'
        tdp._check_and_enforce()
        self.assertEqual(self.profile_writes, [])
        self.assertEqual(self.ppts(), (10, 10, 10))

    def test_unchanged_heartbeat_while_disabled_runs_no_pass(self):
        self.seed({'222': {'spl': 22000, 'sppt': 26000, 'fppt': 32000}}, enabled=False)
        self.report_games_from_frontend()
        tdp._current_game_id = '\x00'  # what startup leaves with TDP control off
        with patch.object(tdp, '_check_and_enforce', wraps=tdp._check_and_enforce) as enforce:
            for _ in range(3):
                self.rpc('set_active_app', '123')
        self.assertEqual(enforce.call_count, 1)
        self.assertEqual(self.ppts(), (10, 10, 10))
        self.assertTrue(self.rpc('set_plugin_enabled', True)['success'])
        self.rpc('set_active_app', '222')
        self.assertEqual(self.ppts(), (22, 26, 32))

    def report_games_from_frontend(self):
        """Resolve the game from set_active_app reports, as on the device."""
        for name, value in {'_get_running_appid': REAL_RUNNING_APPID,
                            '_scan_proc_for_appid': lambda: '',
                            '_frontend_appid': '', '_frontend_appid_ts': None}.items():
            self.stack.enter_context(patch.object(tdp, name, value))

    def test_reported_game_switch_applies_within_the_call(self):
        self.seed({'123': {'spl': 22000, 'sppt': 26000, 'fppt': 32000}})
        self.report_games_from_frontend()
        self.rpc('set_active_app', '123')  # no enforce loop runs in this test
        self.assertEqual(self.ppts(), (22, 26, 32))
        self.profile_writes.clear()
        self.rpc('set_active_app', '123')  # the 2 s heartbeat for the same game
        self.assertEqual(self.profile_writes, [])
        self.rpc('set_active_app', '')
        self.assertEqual(self.ppts(), (15, 20, 25))
        saved = self.store.getSetting('settings')
        self.assertEqual((saved['active_spl'], saved['active_sppt'], saved['active_fppt']),
                         (15000, 20000, 25000))

    def test_reported_game_profile_is_clamped_without_extras(self):
        self.seed({'123': {'spl': 45000, 'sppt': 48000, 'fppt': 50000}})
        self.report_games_from_frontend()
        self.rpc('set_active_app', '123')
        self.assertEqual(self.ppts(), (35, 37, 45))

    def test_edit_for_the_previous_game_is_refused_after_a_reported_switch(self):
        self.seed({'123': {'spl': 22000, 'sppt': 26000, 'fppt': 32000},
                   '222': {'spl': 12000, 'sppt': 15000, 'fppt': 20000}})
        self.report_games_from_frontend()
        self.rpc('set_active_app', '123')
        self.rpc('set_active_app', '222')
        self.assertEqual(self.ppts(), (12, 15, 20))
        stale = self.rpc('apply_tdp', 30000, 30000, 30000, '123', '', '123')
        self.assertEqual((stale['success'], stale['stderr']), (False, 'foreground game changed'))
        self.assertEqual(self.ppts(), (12, 15, 20))
        self.assertEqual(self.store.getSetting('game_profiles')['123']['spl'], 22000)

    # Release ---------------------------------------------------------------

    def test_release_returns_platform_profile_to_balanced(self):
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.assertEqual(self.profile.read_text(), 'custom')
        with tdp._mutation_lock:
            self.assertTrue(tdp._release_controls_locked()['success'])
        self.assertEqual(self.profile.read_text(), 'balanced')
        self.assertEqual(self.applies(), [])

    def test_release_restores_managed_cpu_controls(self):
        boost = self.cpu / 'cpufreq/boost'
        boost.parent.mkdir(parents=True)
        boost.write_text('0')
        policy = self.cpu / 'cpufreq/policy0'
        policy.mkdir()
        (policy / 'energy_performance_preference').write_text('power')
        (policy / 'energy_performance_available_preferences').write_text(
            'performance balance_performance balance_power power')
        self.seed(cpu_boost_enabled=False, epp='power')
        with tdp._mutation_lock:
            self.assertTrue(tdp._release_controls_locked()['success'])
        self.assertEqual(self.profile.read_text(), 'balanced')
        self.assertEqual(boost.read_text(), '1')
        self.assertEqual((policy / 'energy_performance_preference').read_text(),
                         'balance_performance')


    # Baseline (what release restores) ---------------------------------------

    def cpu_controls(self, boost='0', epp='power'):
        boost_path = self.cpu / 'cpufreq/boost'
        boost_path.parent.mkdir(parents=True, exist_ok=True)
        boost_path.write_text(boost)
        policy = self.cpu / 'cpufreq/policy0'
        policy.mkdir(exist_ok=True)
        (policy / 'energy_performance_available_preferences').write_text(
            'performance balance_performance balance_power power')
        epp_path = policy / 'energy_performance_preference'
        epp_path.write_text(epp)
        return boost_path, epp_path

    def release(self):
        with tdp._mutation_lock:
            self.assertTrue(tdp._release_controls_locked()['success'])

    def test_fresh_install_restores_what_it_found_before_the_first_write(self):
        boost, epp = self.cpu_controls()
        self.profile.write_text('performance')
        self.store.replace({})
        tdp._migrate()
        self.assertEqual(self.store.getSetting('baseline'), {'pending': True})
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.assertTrue(self.rpc('set_cpu_boost', True)['success'])
        self.assertTrue(self.rpc('set_epp', 'balance_power')['success'])
        self.assertEqual(self.store.getSetting('baseline'), {
            'platform_profile': 'performance', 'cpu_boost_enabled': False, 'epp': 'power'})
        self.release()
        self.assertEqual((self.profile.read_text(), boost.read_text(), epp.read_text()),
                         ('performance', '0', 'power'))

    def test_existing_install_captures_only_after_the_next_boot(self):
        boot = self.root / 'boot_id'
        boot.write_text('boot-1')
        self.stack.enter_context(patch.object(tdp, 'BOOT_ID_PATH', str(boot)))
        boost, epp = self.cpu_controls('1', 'balance_power')  # set by the old version
        self.seed(cpu_boost_enabled=True, epp='balance_power')
        tdp._migrate()
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.assertEqual(self.store.getSetting('baseline'), {'pending': True, 'after_boot': 'boot-1'})
        self.release()  # nothing known yet: today's defaults
        self.assertEqual((self.profile.read_text(), boost.read_text(), epp.read_text()),
                         ('balanced', '1', 'balance_performance'))
        boot.write_text('boot-2')  # the reboot reset the controls
        self.profile.write_text('custom')
        boost.write_text('0')
        epp.write_text('power')
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.assertEqual(self.store.getSetting('baseline'), {
            'platform_profile': None, 'cpu_boost_enabled': False, 'epp': 'power'})
        self.release()
        self.assertEqual((self.profile.read_text(), boost.read_text(), epp.read_text()),
                         ('balanced', '0', 'power'))

    def test_release_before_any_write_restores_the_system_value_it_found(self):
        boot = self.root / 'boot_id'
        boot.write_text('boot-1')
        self.stack.enter_context(patch.object(tdp, 'BOOT_ID_PATH', str(boot)))
        boost, _epp = self.cpu_controls('1', 'balance_power')
        self.seed({'123': {'spl': 15000, 'sppt': 20000, 'fppt': 25000,
                           'cpu_boost_enabled': True}}, enabled=False)
        tdp._migrate()
        boot.write_text('boot-2')  # rebooted: the system default boost is off, nothing written yet
        boost.write_text('0')
        self.release()
        self.assertEqual(boost.read_text(), '0')

    def test_invalid_saved_profile_falls_back_to_balanced(self):
        self.store.replace({'settings': {'enabled': True}, 'schema_version': 2,
                            'baseline': {'platform_profile': 'turbo'}})
        self.assertTrue(self.rpc('apply_tdp', 15000, 20000, 25000)['success'])
        self.release()
        self.assertEqual(self.profile.read_text(), 'balanced')


class TdpRecoveryTests(unittest.TestCase):
    def test_tdp_failed_game_transition_retries_desired_target(self):
        old = (15000, 18000, 25000)
        desired = (25000, 28000, 35000)
        state = dict(zip(("spl", "sppt", "fppt"), old))
        state.update(dict(zip(("active_spl", "active_sppt", "active_fppt"), old)))
        state["enabled"] = True
        failed = {"success": False, "returncode": -1, "stderr": "device waking"}
        with patch.object(tdp, "_current_game_id", ""), \
             patch.object(tdp, "_current_ac_online", False), \
             patch.object(tdp, "_load_settings", side_effect=lambda: copy.deepcopy(state)), \
             patch.object(tdp, "_resume_detected", return_value=False), \
             patch.object(tdp, "_best_effort_reapply_saved_cpu_power_locked"), \
             patch.object(tdp, "_get_running_appid", return_value="123"), \
             patch.object(tdp, "_get_ac_online", return_value=False), \
             patch.object(tdp, "_load_profiles", return_value={"123": dict(zip(("spl", "sppt", "fppt"), desired))}), \
             patch.object(tdp, "_clamp_for_settings", side_effect=lambda s, *x: x), \
             patch.object(tdp, "_apply_and_record", return_value=failed) as apply, \
             patch.object(tdp, "_enforce_target") as enforce:
            tdp._transition_key = None
            tdp._check_and_enforce_locked()
            tdp._transition_retry_at = 0
            tdp._check_and_enforce_locked()
            self.assertNotEqual(tdp._current_game_id, "123")
            self.assertEqual(apply.call_count, 2)
            self.assertEqual(apply.call_args.args[:3], desired)
            enforce.assert_not_called()

    def test_tdp_disk_failure_restores_previous_hardware_value(self):
        state = {"enabled": True, "spl": 15000, "sppt": 18000, "fppt": 25000}
        hardware = [15000, 18000, 25000]
        def apply(s, *values, reason=None):
            hardware[:] = values
            return {"success": True, "stderr": "", "stdout": "", "returncode": 0}
        plugin = tdp.Plugin()
        plugin._ready = True
        with patch.object(tdp, "_load_settings", side_effect=lambda: copy.deepcopy(state)), \
             patch.object(tdp, "_get_running_appid", return_value=""), \
             patch.object(tdp, "_clamp_for_settings", side_effect=lambda s, *x: x), \
             patch.object(tdp, "_apply_limits_with_saved_cpu_power", side_effect=apply), \
             patch.object(tdp, "_write_keys", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "disk full.*previous target restored"):
                asyncio.run(plugin.apply_tdp(8000, 10000, 15000))
        self.assertEqual(hardware, [15000, 18000, 25000])
        self.assertEqual(state["spl"], 15000)

    def test_tdp_offline_start_preserves_saved_profiles(self):
        state = {"enabled": True, "extras_unlocked": True,
                 "spl": 40000, "sppt": 45000, "fppt": 50000}
        profiles = {"123": {"spl": 40000, "sppt": 45000, "fppt": 50000}}
        caps = {k: {"min": 5, "max": v} for k,v in (("spl",35),("sppt",37),("fppt",45))}
        plugin = tdp.Plugin()
        async def start():
            await plugin._main()
            await asyncio.gather(*plugin._tasks)
        with patch.object(tdp.updater, "ssl_context"), \
             patch.object(tdp, "_get_ac_online", return_value=False), \
             patch.object(tdp, "_wmi_caps", return_value=caps), \
             patch.object(tdp, "_ryzenadj_available", True), \
             patch.object(tdp, "_ensure_ryzenadj", side_effect=OSError("network unavailable")), \
             patch.object(tdp, "_load_settings", return_value=state), \
             patch.object(tdp, "_load_profiles", return_value=profiles), \
             patch.object(tdp, "_write_keys") as write, \
             patch.object(tdp, "_startup_context_target", return_value=("",(35000,37000,45000),False)), \
             patch.object(tdp, "_apply_limits_with_saved_cpu_power", return_value={"success":True}), \
             patch.object(tdp, "_save_active"), \
             patch.object(plugin, "_enforce_loop", new_callable=AsyncMock), \
             patch.object(plugin, "_info_loop", new_callable=AsyncMock):
            asyncio.run(start())
        write.assert_not_called()
        self.assertTrue(state["extras_unlocked"])
        self.assertEqual(profiles["123"]["spl"], 40000)

    def test_tdp_profile_mutations_restore_hardware_after_commit_failure(self):
        before = {"enabled": True, "spl": 15000, "sppt": 18000, "fppt": 25000,
                  "active_spl": 25000, "active_sppt": 28000, "active_fppt": 35000,
                  "extras_unlocked": True}
        profiles = {"123": {"spl": 25000, "sppt": 28000, "fppt": 35000}}
        for method, args in (("delete_game_profile", ("123",)),
                             ("set_game_ac_profile", ("123", 20000, 23000, 30000, True)),
                             ("set_extras_unlocked", (False,))):
            with self.subTest(method=method):
                hardware = [25000,28000,35000]
                def apply(state, *values, reason=None):
                    hardware[:] = values
                    return {"success": True}
                with patch.object(tdp, "_load_settings", side_effect=lambda: copy.deepcopy(before)), \
                     patch.object(tdp, "_load_profiles", side_effect=lambda: copy.deepcopy(profiles)), \
                     patch.object(tdp, "_get_running_appid", return_value="123"), \
                     patch.object(tdp, "_get_ac_online", return_value=True), \
                     patch.object(tdp, "_clamp_for_settings", side_effect=lambda s,*v: v), \
                     patch.object(tdp, "_apply_limits_with_saved_cpu_power", side_effect=apply), \
                     patch.object(tdp, "_write_keys", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(RuntimeError, "previous target restored"):
                        asyncio.run(getattr(tdp.Plugin(), method)(*args))
                self.assertEqual(hardware, [25000,28000,35000])


class PowerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await module_runtime.drain('tdp')

    async def test_release_uses_balanced_when_a_ppt_capability_is_missing(self):
        for operation in ('uninstall', 'module_disable'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as raw:
                profile = Path(raw, 'platform_profile')
                profile.write_text('custom')
                Path(raw, 'choices').write_text('balanced custom')
                firmware = Path(raw, 'firmware')
                for key, directory in tdp.WMI_ATTRS.items():
                    entry = firmware / directory
                    entry.mkdir(parents=True)
                    (entry / 'min_value').write_text('5')
                    if key != 'fppt':
                        (entry / 'max_value').write_text('35')
                plugin = tdp.Plugin()
                with patch.object(tdp, 'PLATFORM_PROFILE_GLOB', str(profile)), \
                        patch.object(tdp, 'WMI_ROOT', str(firmware)), \
                        patch.object(tdp, '_ryzenadj_available', False), \
                        patch.object(tdp, '_load_settings', return_value={'enabled': True}), \
                        patch.object(tdp, '_load_profiles', return_value={}), \
                        patch.object(tdp, '_run_ryzenadj') as helper:
                    self.assertEqual(tdp._wmi_caps(), {})
                    if operation == 'uninstall':
                        await plugin._uninstall()
                    else:
                        await module_control.withdraw('tdp', plugin)
                    self.assertEqual(profile.read_text(), 'balanced')
                    helper.assert_not_called()

    async def test_incomplete_caps_do_not_turn_unconfirmed_release_into_success(self):
        with tempfile.TemporaryDirectory() as raw:
            profile = Path(raw, 'platform_profile')
            profile.write_text('custom')
            with patch.object(tdp, '_profile_path', return_value=str(profile)), \
                    patch.object(tdp, '_wmi_caps', return_value={}), \
                    patch.object(tdp, '_write_profile', return_value=True), \
                    patch.object(tdp, '_load_settings', return_value={'enabled': True}), \
                    patch.object(tdp, '_load_profiles', return_value={}), \
                    patch.object(tdp, '_run_ryzenadj') as helper:
                with self.assertRaisesRegex(RuntimeError, 'Balanced firmware profile was not confirmed'):
                    await tdp.Plugin()._uninstall()
                self.assertEqual(profile.read_text(), 'custom')
                helper.assert_not_called()

    async def test_caps_distinguish_both_missing_paths_from_firmware_and_helper(self):
        plugin = tdp.Plugin()
        firmware = {key: {'min': 5, 'max': 35} for key in tdp.WMI_ATTRS}
        with patch.object(tdp, '_wmi_caps', return_value={}) as caps, \
                patch.object(tdp, '_profile_path', return_value='/mock/profile') as profile, \
                patch.object(tdp, '_ryzenadj_available', False), \
                patch.object(tdp.os.path, 'isfile', return_value=False):
            unavailable = await plugin.get_caps()
            self.assertFalse(unavailable['tdp_available'])
            self.assertTrue(unavailable['helper_retry_available'])
            self.assertFalse(unavailable['extras'])
            self.assertIn('unavailable', unavailable['tdp_error'])
            caps.return_value = firmware
            available = await plugin.get_caps()
            self.assertTrue(available['tdp_available'])
            self.assertFalse(available['extras'])
            self.assertEqual(available['tdp_error'], '')
            profile.return_value = None
            self.assertFalse((await plugin.get_caps())['tdp_available'])
        with patch.object(tdp, '_wmi_caps', return_value={}), \
                patch.object(tdp, '_ryzenadj_available', True), \
                patch.object(tdp.os.path, 'isfile', return_value=True):
            helper = await plugin.get_caps()
            self.assertTrue(helper['tdp_available'])
            self.assertTrue(helper['extras'])
            self.assertFalse(helper['helper_retry_available'])

    async def test_failed_download_is_visible_then_verified_retry_updates_caps(self):
        plugin = tdp.Plugin()
        with patch.object(tdp, '_wmi_caps', return_value={}), \
                patch.object(tdp, '_ryzenadj_available', False), \
                patch.object(tdp, '_current_game_id', ''), \
                patch.object(tdp, '_transition_key', None), \
                patch.object(tdp.os.path, 'isfile', return_value=False) as present, \
                patch.object(tdp, '_ensure_ryzenadj', side_effect=TimeoutError('offline')) as ensure:
            self.assertFalse((await plugin.retry_extras())['success'])
            caps = await plugin.get_caps()
            self.assertFalse(caps['tdp_available'])
            self.assertIn('offline', caps['tdp_error'])
            ensure.side_effect = None
            ensure.return_value = True
            present.return_value = True
            self.assertTrue((await plugin.retry_extras())['success'])
            caps = await plugin.get_caps()
            self.assertTrue(caps['tdp_available'])
            self.assertEqual(caps['tdp_error'], '')
            self.assertTrue(caps['extras'])
            self.assertFalse(caps['helper_retry_available'])

    async def test_start_without_firmware_or_helper_keeps_cpu_module_ready_but_reports_tdp_unavailable(self):
        def ensure(cancel=None, download=True):
            self.assertFalse(download)
            return False
        plugin = tdp.Plugin()
        with ExitStack() as stack:
            for name, value in {'_wmi_caps': {}, '_profile_path': None,
                    '_get_ac_online': False, '_get_running_appid': '', '_suspend_offset': 0,
                    '_load_settings': {'enabled': True, 'spl': 16000, 'sppt': 25000,
                        'fppt': 30000, 'cpu_boost_enabled': None, 'epp': None},
                    '_load_profiles': {}, '_cancel_ac_settle': None}.items():
                stack.enter_context(patch.object(tdp, name, return_value=value))
            stack.enter_context(patch.object(tdp.Plugin, '_ready', False))
            stack.enter_context(patch.object(tdp.Plugin, '_setup_error', None))
            stack.enter_context(patch.object(tdp, '_ryzenadj_available', False))
            for name, value in {'_current_game_id': '', '_transition_key': None,
                    '_last_cpu_power_check': 0, '_current_ac_online': False,
                    '_last_suspend_offset': None}.items():
                stack.enter_context(patch.object(tdp, name, value))
            stack.enter_context(patch.object(tdp.updater, 'ssl_context'))
            stack.enter_context(patch.object(tdp, '_ensure_ryzenadj', side_effect=ensure))
            stack.enter_context(patch.object(plugin, '_enforce_loop', new=AsyncMock()))
            stack.enter_context(patch.object(plugin, '_info_loop', new=AsyncMock()))
            try:
                await plugin._main()
                await asyncio.gather(*plugin._tasks)
                self.assertTrue((await plugin.is_ready())['ready'])
                caps = await plugin.get_caps()
                self.assertFalse(caps['tdp_available'])
                self.assertTrue(caps['helper_retry_available'])
                self.assertFalse(tdp._apply_limits(16000, 25000, 30000)['success'])
            finally:
                await plugin._unload()


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


class HelperDownloadTests(unittest.TestCase):
    def helper(self):
        return tdp_updater.Updater(user_agent='test')

    def test_partial_archive_is_removed_without_replacing_existing_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            binary = Path(raw, 'ryzenadj')
            binary.write_bytes(b'previous verified helper')
            mkstemp = tempfile.mkstemp
            def temporary(**kwargs):
                kwargs.setdefault('dir', raw)
                return mkstemp(**kwargs)
            def stalled(_url, out, **_kwargs):
                out.write(b'partial archive')
                raise TimeoutError('total time limit')
            with patch.object(tdp, 'BIN_DIR', raw), patch.object(tdp, 'BIN_PATH', str(binary)), \
                    patch.object(tdp.tempfile, 'mkstemp', side_effect=temporary), \
                    patch.object(tdp.updater, 'download_to', side_effect=stalled):
                with self.assertRaises(TimeoutError):
                    tdp._download_ryzenadj()
            self.assertEqual(binary.read_bytes(), b'previous verified helper')
            self.assertEqual([path.name for path in Path(raw).iterdir()], ['ryzenadj'])

    def test_firmware_handoff_requires_balanced_readback(self):
        with tempfile.TemporaryDirectory() as raw:
            profile = Path(raw, 'platform_profile')
            profile.write_text('custom')
            with patch.object(tdp, '_profile_path', return_value=str(profile)), \
                    patch.object(tdp, '_wmi_caps', return_value={'supported': True}), \
                    patch.object(tdp, '_write_profile', return_value=True), \
                    patch.object(tdp, '_run_ryzenadj') as helper:
                self.assertFalse(tdp._restore_defaults_locked()['success'])
            helper.assert_not_called()


class HelperLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_retry_adopts_verified_helper_without_duplicate_downloads(self):
        entered, finish = threading.Event(), threading.Event()
        def ensure(**_kwargs):
            entered.set()
            if not finish.wait(2):
                raise TimeoutError('test did not release download')
            return True
        plugin = tdp.Plugin()
        with patch.object(tdp, '_ryzenadj_available', False), \
                patch.object(tdp, '_current_game_id', '111'), \
                patch.object(tdp, '_transition_key', ('111',)), \
                patch.object(tdp, '_ensure_ryzenadj', side_effect=ensure) as verified:
            task = asyncio.create_task(plugin.retry_extras())
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.005)
                self.assertTrue(entered.is_set())
                again = asyncio.create_task(plugin.retry_extras())  # waits for the running one
            finally:
                finish.set()
                result = await asyncio.wait_for(task, 1)
            self.assertTrue(result['success'])
            self.assertTrue((await asyncio.wait_for(again, 1))['success'])
            verified.assert_called_once()
            self.assertTrue(tdp._ryzenadj_available)
            self.assertIsNone(tdp._transition_key)

    async def test_startup_only_verifies_an_existing_helper_and_never_downloads(self):
        calls = []
        def ensure(cancel=None, download=True):
            calls.append(download)
            return False
        plugin = tdp.Plugin()
        with ExitStack() as stack:
            for name, value in {'_ryzenadj_available': False, '_current_game_id': '', '_transition_key': None}.items():
                stack.enter_context(patch.object(tdp, name, value))
            for name, value in {'_wmi_caps': {'supported': True}, '_get_ac_online': False,
                    '_suspend_offset': 0, '_load_settings': {'enabled': False},
                    '_startup_context_target': ('', (16000, 25000, 30000), False),
                    '_reapply_saved_cpu_power_controls_locked': [], '_cancel_ac_settle': None}.items():
                stack.enter_context(patch.object(tdp, name, return_value=value))
            stack.enter_context(patch.object(tdp.updater, 'ssl_context'))
            stack.enter_context(patch.object(tdp, '_ensure_ryzenadj', side_effect=ensure))
            stack.enter_context(patch.object(plugin, '_enforce_loop', new=AsyncMock()))
            stack.enter_context(patch.object(plugin, '_info_loop', new=AsyncMock()))
            try:
                await asyncio.wait_for(plugin._main(), 1)
                await asyncio.gather(*plugin._tasks)
                self.assertTrue(tdp.Plugin._ready)
                self.assertEqual(calls, [False])
                self.assertFalse(tdp._ryzenadj_available)
            finally:
                await plugin._unload()
                await module_runtime.drain('tdp')


if __name__ == '__main__':
    unittest.main()

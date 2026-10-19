# SPDX-License-Identifier: BSD-3-Clause
"""Settings files written by 2.1.0 and 2.2.0 load through the current code without losing the user's values.

The fixtures are the files those releases wrote: each tag's tree (`git archive <tag>`) was imported with a
stub `decky` and its own save path run in a temp dir (TDP, Fan, Remap and Wi-Fi store functions; the
Vibration RPCs), then the resulting JSON was copied here.

Not covered, because the code that reads them is unchanged since 2.1.0: battery, RGB and controller (their
state validators are identical), display (startup reads enabled / edid_fix / panel_mode / active_mode the
same way; 2.1-era reset and session records are covered in test_display_lifecycle) and module_settings
(main reads `modules` the same way, with the same module names).
"""
import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import support  # noqa: F401 - installs the fake decky module
from safe_settings import AtomicSettingsManager
import fan_backend
import remap_backend
import tdp_backend
import vibration_backend as vibe
import wifi_backend
import test_vibration_backend as vibration_tests


def stored(test, module, name, payload):
    """Point `module.settings` at a temp store holding `payload`, as the release left it on disk."""
    directory = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, directory, True)
    with open(os.path.join(directory, name + '.json'), 'w', encoding='utf-8') as handle:
        json.dump(payload, handle)
    manager = AtomicSettingsManager(name, directory)
    patcher = patch.object(module, 'settings', manager)
    patcher.start()
    test.addCleanup(patcher.stop)
    return directory


# --- TDP ------------------------------------------------------------------- #
TDP_GLOBAL = {'spl': 12000, 'sppt': 18000, 'fppt': 24000, 'enabled': True, 'extras_unlocked': True,
              'advanced_tdp_control': False, 'cpu_boost_enabled': False, 'epp': 'balance_power',
              'active_spl': 12000, 'active_sppt': 18000, 'active_fppt': 24000, 'active_preset': 'silent'}
TDP_GAME = {'spl': 20000, 'sppt': 25000, 'fppt': 30000, 'preset': 'performance', 'ac_separate': True,
            'ac_spl': 25000, 'ac_sppt': 30000, 'ac_fppt': 35000, 'ac_preset': 'max',
            'cpu_boost_enabled': True, 'epp': 'performance', 'ac_cpu_boost_enabled': True, 'ac_epp': 'performance'}
TDP = {
    '2.1.0': {'schema_version': 2, 'settings': TDP_GLOBAL, 'game_profiles': {'1091500': TDP_GAME}},
    # 2.2.0 added the CPU baseline record and profiles kept while their game's override is off.
    '2.2.0': {'schema_version': 2, 'baseline': {'pending': True}, 'settings': TDP_GLOBAL,
              'game_profiles': {'1091500': TDP_GAME},
              'inactive_game_profiles': {'570': {**TDP_GAME, 'ac_separate': False, 'preset': 'balanced'}}},
}


class TdpSettingsCompatTests(unittest.TestCase):
    def test_released_stores_keep_global_limits_cpu_choices_and_game_profiles(self):
        for tag, payload in TDP.items():
            with self.subTest(tag=tag):
                directory = stored(self, tdp_backend, 'tdp_settings', copy.deepcopy(payload))
                boot = os.path.join(directory, 'boot_id')
                with open(boot, 'w', encoding='ascii') as handle:
                    handle.write('boot-b\n')
                with patch.object(tdp_backend, 'BOOT_ID_PATH', boot):
                    tdp_backend._migrate()
                loaded = tdp_backend._load_settings()
                self.assertEqual({key: loaded[key] for key in TDP_GLOBAL}, TDP_GLOBAL)
                game = tdp_backend._load_profiles()['1091500']
                self.assertEqual({key: game[key] for key in TDP_GAME}, TDP_GAME)
                tdp_backend.settings.read()
                if tag == '2.1.0':
                    # Not a fresh store: the CPU controls may still hold our values until the next boot.
                    self.assertEqual(tdp_backend.settings.getSetting('baseline'),
                                     {'pending': True, 'after_boot': 'boot-b'})
                else:
                    self.assertEqual(tdp_backend.settings.getSetting('baseline'), {'pending': True})
                    inactive = tdp_backend._load_inactive_profiles()['570']
                    self.assertEqual({key: inactive[key] for key in TDP_GAME},
                                     payload['inactive_game_profiles']['570'])


# --- Fan ------------------------------------------------------------------- #
FAN_GLOBAL = [[30, 0], [40, 1500], [50, 1800], [60, 2200], [70, 2800], [80, 3400], [90, 4200], [100, 4800]]
FAN_PERFORMANCE = [[30, 1500], [40, 1600], [50, 2000], [60, 2400], [70, 2800], [80, 3000], [90, 3200], [100, 4800]]
FAN_SILENT = [[30, 0], [40, 0], [50, 1500], [60, 1500], [70, 1500], [80, 1600], [90, 1700], [100, 4800]]


def fan_state(silent_last):
    return {'schema': 4, 'enabled': True, 'revision': 7, 'display_unit': 'percent', 'assign_tdp_presets': True,
            'global_curve': FAN_GLOBAL,
            'presets': {'performance': FAN_PERFORMANCE, 'silent': [*FAN_SILENT[:7], [100, silent_last]]}}


class FanSettingsCompatTests(unittest.IsolatedAsyncioTestCase):
    async def test_released_curves_assignments_and_unit_load_unchanged(self):
        # 2.1.0 required full speed at 100 C; 2.2.0 allowed down to 1500 RPM, now lifted to 2000.
        for tag, saved, expected in (('2.1.0', 4800, 4800), ('2.2.0', 1800, 2000)):
            with self.subTest(tag=tag):
                stored(self, fan_backend, 'fan_settings', {'state': fan_state(saved)})
                state = await fan_backend.Plugin().get_state()
                self.assertFalse(state['needs_curve_review'])
                self.assertEqual(state['settings'], fan_state(expected))


# --- Vibration ------------------------------------------------------------- #
VIBRATION_PROFILES = {
    '0': {'overwrite': False, 'settings': {'level': 3, 'mode': 2, 'touchpadEnabled': False, 'touchpadIntensity': 2}},
    '1091500': {'name': 'Cyberpunk 2077', 'overwrite': True,
                'settings': {'level': 1, 'mode': 2, 'touchpadEnabled': False, 'touchpadIntensity': 0}},
}
VIBRATION = {
    '2.1.0': {'schema_version': 2, 'game_profiles': VIBRATION_PROFILES},
    # 2.2.0 started recording whether the driver values found at install can be restored.
    '2.2.0': {'schema_version': 2, 'baseline': {'pending': True}, 'game_profiles': VIBRATION_PROFILES},
}


class VibrationSettingsCompatTests(vibration_tests.FakeDevice):
    async def test_released_global_and_game_profiles_are_what_the_panel_shows(self):
        for tag, payload in VIBRATION.items():
            with self.subTest(tag=tag):
                self.store.replace(copy.deepcopy(payload))
                vibe._migrate()
                for game, profile in (('', '0'), ('1091500', '1091500')):
                    self.running = game
                    current = await self.plugin.get_settings()
                    self.assertEqual((current['profile_id'], current['settings']),
                                     (profile, VIBRATION_PROFILES[profile]['settings']))
                self.assertEqual(self.persisted(), VIBRATION_PROFILES)
                # An upgraded 2.1.0 store cannot prove the driver values; uninstall then uses the defaults.
                self.assertEqual(self.store.getSetting('baseline'), {} if tag == '2.1.0' else {'pending': True})


# --- Remap (2.1.0 and 2.2.0 wrote the same state) -------------------------- #
REMAP = {'state': {'schema_version': 1, 'enabled': True, 'desktop_action': 'screenshot', 'page_action': 'alt_tab',
                   'previous_actions': None,
                   'baseline_profile': 'version: 1\nkind: DeviceProfile\nname: Default\nmapping:\n'
                                       '  - name: Quick Access\n    source_events:\n      - gamepad:\n'
                                       '          button: QuickAccess2\n'}}


class RemapSettingsCompatTests(unittest.TestCase):
    def test_released_ownership_and_actions_are_kept(self):
        stored(self, remap_backend, 'remap_settings', copy.deepcopy(REMAP))
        state = remap_backend._load_state()
        self.assertEqual({key: state[key] for key in ('enabled', 'desktop_action', 'page_action', 'baseline_profile')},
                         {key: REMAP['state'][key] for key in ('enabled', 'desktop_action', 'page_action', 'baseline_profile')})


# --- Wi-Fi ----------------------------------------------------------------- #
WIFI_STATE = {'schema': 1, 'mode': 'five_six_no_24', 'connection_uuid': '3f1c2a9e-8f4b-4f0e-9a51-2b7c6d0e1f23',
              'original': {'nm_band': '', 'iwd': {'modifier_present': False, 'modifier_value': ''}},
              'owns_nm_band': False, 'owns_iwd': True,
              'applied': {'nm_band': '', 'iwd_modifier_present': True, 'iwd_modifier_value': '0.0'},
              'activated_at': 1760000000}
WIFI_KEPT = {'driver': 'mt7925e', 'device_family': 'mt7925', 'device_label': 'MediaTek MT7925',
             'band_policy': 'five_six_no_24', 'band_policy_state': WIFI_STATE, 'last_applied': 1760000000}
WIFI = {
    # Power save and BSSID lock left in 2.2.0; their (inactive) fields are dropped on load.
    '2.1.0': {**WIFI_KEPT, 'model': 'unknown', 'chip_label': 'unknown', 'supports_6ghz': False,
              'power_save_disabled': False, 'power_save_connection_uuid': '', 'power_save_previous': '',
              'power_save_runtime_previous': '', 'power_save_legacy_detected': False,
              'bssid_lock_enabled': False, 'bssid_lock_value': '', 'bssid_lock_connection_uuid': '',
              'band_policy_legacy_detected': False, 'band_policy_legacy_band': '',
              'band_policy_legacy_connection_uuid': '', 'band_preference': '5_6', 'band_preference_enabled': True,
              'last_connection_uuid': '', 'distro_id': 'unknown', 'distro_name': 'Unknown'},
    '2.2.0': {**WIFI_KEPT, 'band_preference_enabled': True},
}


class WifiSettingsCompatTests(unittest.TestCase):
    def test_released_band_policy_and_its_recovery_state_are_kept(self):
        for tag, payload in WIFI.items():
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, 'wifi_settings.json')
                with open(path, 'w', encoding='utf-8') as handle:
                    json.dump(payload, handle)
                with patch.object(wifi_backend, 'SETTINGS_FILE', path):
                    self.assertEqual(wifi_backend._load_settings(), WIFI_KEPT)
                    with open(path, encoding='utf-8') as handle:
                        self.assertEqual(json.load(handle), WIFI_KEPT)


if __name__ == '__main__':
    unittest.main()

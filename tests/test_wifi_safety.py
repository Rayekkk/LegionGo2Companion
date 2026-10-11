# SPDX-License-Identifier: BSD-3-Clause
"""Offline WiFi input and durable ownership recovery regressions."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from contextlib import ExitStack

import test_integration  # Install isolated Decky stubs before backend imports.
import wifi_backend as wifi
from safe_settings import CorruptSettings


class WifiSafetyTests(unittest.TestCase):
    def test_power_save_recovery_is_bound_to_the_original_connection(self):
        for active, expected_success, expected_writes in (
            ('original', True, 1), ('different', True, 0), (None, False, 0),
        ):
            with self.subTest(active=active), ExitStack() as stack:
                plugin = wifi.Plugin()
                journal = {'phase': 'applying', 'profile_before': {'uuid': 'original'},
                           'runtime_before': {'iface': 'wlan0', 'power_save': 'on'},
                           'mutations': {'runtime_power_save': {'planned': True, 'expected': 'off'}},
                           'settings_before': {}}
                stack.enter_context(patch.object(plugin, '_save_power_save_journal'))
                stack.enter_context(patch.object(plugin, '_write_band_settings_fields'))
                stack.enter_context(patch.object(plugin, '_get_active_connection_uuid', return_value=active))
                read = stack.enter_context(patch.object(plugin, '_get_runtime_power_save',
                    side_effect=[('off', {'success': True}), ('on', {'success': True})]))
                write = stack.enter_context(patch.object(plugin, '_run_cmd', return_value={'success': True}))
                result = plugin._restore_power_save_transaction(journal, 'interrupted operation')
                self.assertEqual(result['success'], expected_success, result)
                self.assertEqual(write.call_count, expected_writes)
                if not expected_writes:
                    read.assert_not_called()
                if active is None:
                    self.assertEqual(journal['phase'], 'rollback_failed')

    def test_power_save_recovery_rechecks_profile_after_read(self):
        plugin = wifi.Plugin()
        journal = {'profile_before': {'uuid': 'original'},
                   'runtime_before': {'iface': 'wlan0', 'power_save': 'on'},
                   'mutations': {'runtime_power_save': {'planned': True, 'expected': 'off'}}}
        with patch.object(plugin, '_save_power_save_journal'), \
             patch.object(plugin, '_get_active_connection_uuid', side_effect=['original', 'different']), \
             patch.object(plugin, '_get_runtime_power_save', return_value=('off', {'success': True})), \
             patch.object(plugin, '_run_cmd') as write:
            result = plugin._restore_power_save_transaction(journal, 'interrupted operation')
        self.assertFalse(result['success'])
        write.assert_not_called()

    def test_power_save_apply_does_not_write_runtime_after_profile_switch(self):
        for phase in ('profile_write', 'journal', 'readback'):
            with self.subTest(phase=phase):
                self._power_save_profile_switch(phase)

    def _power_save_profile_switch(self, phase):
        plugin = wifi.Plugin()
        active = 'original'
        nm_value = '3'
        writes = []
        def modify(_uuid, _key, value, **_kwargs):
            nonlocal active, nm_value
            nm_value = value
            if phase == 'profile_write':
                active = 'different'
            return {'success': True}
        def save(journal):
            nonlocal active
            if phase == 'journal' and journal['phase'] == 'applying' and 'runtime_power_save' in journal['mutations']:
                active = 'different'
        def read_runtime(*_args):
            nonlocal active
            if phase == 'readback' and writes:
                active = 'different'
                return 'off', {'success': True}
            return 'on', {'success': True}
        with ExitStack() as stack:
            stack.enter_context(patch.object(plugin, '_save_power_save_journal', side_effect=save))
            for name in ('_remove_power_save_journal', '_write_band_settings_fields'):
                stack.enter_context(patch.object(plugin, name))
            for name in ('_load_band_policy_journal', '_load_power_save_journal'):
                stack.enter_context(patch.object(plugin, name, return_value=None))
            stack.enter_context(patch.object(wifi, '_load_settings', return_value=dict(wifi.DEFAULT_SETTINGS)))
            stack.enter_context(patch.object(plugin, '_get_wifi_interface', return_value='wlan0'))
            stack.enter_context(patch.object(plugin, '_get_active_connection_uuid', side_effect=lambda *a, **k: active))
            stack.enter_context(patch.object(plugin, '_nmcli_get', side_effect=lambda *a: (nm_value, {'success': True})))
            stack.enter_context(patch.object(plugin, '_nmcli_modify', side_effect=modify))
            stack.enter_context(patch.object(plugin, '_get_runtime_power_save', side_effect=read_runtime))
            stack.enter_context(patch.object(plugin, '_run_cmd', side_effect=lambda args, **k: writes.append(args) or {'success': True}))
            result = plugin._set_power_save_sync(True)
        self.assertFalse(result['success'])
        self.assertTrue(result['rolled_back'], result)
        self.assertEqual(nm_value, '3', 'The original profile still rolls back its persisted change.')
        self.assertEqual(len(writes), 1 if phase == 'readback' else 0,
                         'No runtime write belongs to the new connection, including recovery.')

    def test_retired_wake_dispatcher_flag_is_migrated_without_losing_ownership(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'wifi_settings.json'
            original = dict(wifi.DEFAULT_SETTINGS,
                            auto_fix_on_wake=True,
                            power_save_disabled=True,
                            power_save_connection_uuid='dda82d97-6914-4788-802f-fa1ba9a1515f',
                            power_save_previous='0',
                            power_save_runtime_previous='on',
                            band_policy=wifi.BAND_POLICY_HIGH_ONLY,
                            band_policy_state={'mode': wifi.BAND_POLICY_HIGH_ONLY,
                                               'owns_iwd': True,
                                               'original': {'iwd': {'modifier_present': False}},
                                               'applied': {'iwd_modifier_present': True,
                                                           'iwd_modifier_value': '0.01'}})
            path.write_text(json.dumps(original), encoding='utf-8')
            with patch.object(wifi, 'SETTINGS_FILE', str(path)), \
                 patch.object(wifi, '_SETTINGS_MANAGER', None), \
                 patch.object(wifi, '_SETTINGS_MANAGER_PATH', ''):
                migrated = wifi._load_settings()
                self.assertNotIn('auto_fix_on_wake', migrated)
                for field in ('power_save_disabled', 'power_save_connection_uuid',
                              'power_save_previous', 'power_save_runtime_previous',
                              'band_policy', 'band_policy_state'):
                    self.assertEqual(migrated[field], original[field])
                self.assertFalse(migrated['power_save_legacy_detected'])
                self.assertNotIn('auto_fix_on_wake', json.loads(path.read_text()))
                with patch.object(wifi._get_settings_manager(), 'replace') as rewrite:
                    self.assertEqual(wifi._load_settings(), migrated)
                    rewrite.assert_not_called()

    def test_status_does_not_show_a_configuring_connection_as_connected(self):
        plugin = wifi.Plugin()
        uuid = 'dda82d97-6914-4788-802f-fa1ba9a1515f'
        with patch.object(wifi, '_load_settings', return_value=dict(wifi.DEFAULT_SETTINGS)), \
             patch.object(plugin, '_get_wifi_interface', return_value='wlan0'), \
             patch.object(plugin, '_get_network_recovery_issues', return_value=[]), \
             patch.object(plugin, '_get_current_backend', return_value='iwd'), \
             patch.object(plugin, '_run_cmd', return_value={
                 'success': True, 'stdout': '50 (connecting (configuring))\n' + uuid}) as command:
            status = plugin._get_status_sync()
        self.assertTrue(status['success'], status)
        self.assertFalse(status['connected'])
        self.assertNotIn('frequency', status['live'])
        self.assertEqual(command.call_count, 1)

    def test_status_uses_one_settings_snapshot_for_support_and_ownership(self):
        plugin = wifi.Plugin()
        state = dict(wifi.DEFAULT_SETTINGS, driver="mt7921e", device_family="legion_go_2")
        with patch.object(wifi, "_load_settings", return_value=state) as read, \
             patch.object(plugin, "_get_wifi_interface", return_value=None), \
             patch.object(plugin, "_get_active_connection_uuid", return_value=None), \
             patch.object(plugin, "_get_network_recovery_issues", return_value=[]), \
             patch.object(plugin, "_get_current_backend", return_value="iwd"):
            status = plugin._get_status_sync()
        self.assertTrue(status["success"])
        self.assertEqual(status["support_tier"], 1)
        self.assertIs(status["settings"], state)
        read.assert_called_once_with()

    def test_os_rollback_baseline_can_be_recovered_only_by_explicit_transition(self):
        plugin = wifi.Plugin()
        baseline = {'modifier_present': False, 'modifier_value': ''}
        settings = {'band_policy': wifi.BAND_POLICY_HIGH_ONLY, 'band_policy_state': {
            'mode': wifi.BAND_POLICY_HIGH_ONLY, 'owns_iwd': True,
            'original': {'iwd': baseline},
            'applied': {'iwd_modifier_present': True, 'iwd_modifier_value': '0.01'}}}
        with patch.object(plugin, '_get_iwd_rank_modifier_snapshot', return_value=baseline):
            self.assertTrue(plugin._band_policy_ownership_error(settings))
            self.assertEqual(plugin._band_policy_ownership_error(settings, allow_restored_iwd=True), '')
        for external in ({'modifier_present': True, 'modifier_value': '0.5'},
                         {'modifier_present': False, 'ambiguous': True}):
            with patch.object(plugin, '_get_iwd_rank_modifier_snapshot', return_value=external):
                self.assertTrue(plugin._band_policy_ownership_error(settings, allow_restored_iwd=True))
        settings['band_policy_state']['original'] = {}
        with patch.object(plugin, '_get_iwd_rank_modifier_snapshot', return_value=baseline):
            self.assertTrue(plugin._band_policy_ownership_error(settings, allow_restored_iwd=True))

    def test_invalid_preference_never_starts_a_network_transaction(self):
        plugin = wifi.Plugin()
        with patch.object(plugin, "set_band_policy", new_callable=AsyncMock) as change:
            for invalid in ("false", "true", 0, 1, None, [], {}):
                result = asyncio.run(plugin.set_band_preference(invalid))
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "invalid_band_preference_state")
            change.assert_not_awaited()

    def test_boolean_preferences_still_select_the_expected_policy(self):
        plugin = wifi.Plugin()
        with patch.object(plugin, "set_band_policy", new_callable=AsyncMock) as change:
            for enabled, target in ((True, wifi.BAND_POLICY_HIGH_ONLY),
                                    (False, wifi.BAND_POLICY_OFF)):
                asyncio.run(plugin.set_band_preference(enabled))
                change.assert_awaited_with(target)

    def test_lost_ownership_cannot_be_reported_as_preference_off(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wifi_settings.json"
            backup = Path(str(path) + ".bak")
            path.write_text("{broken", encoding="utf-8")
            backup.write_text("[]", encoding="utf-8")
            with patch.object(wifi, "SETTINGS_FILE", str(path)), \
                 patch.object(wifi, "_SETTINGS_MANAGER", None), \
                 patch.object(wifi, "_SETTINGS_MANAGER_PATH", ""):
                for _ in range(2):
                    with self.assertRaisesRegex(CorruptSettings, "network ownership"):
                        wifi._load_settings()
                plugin = wifi.Plugin()
                with patch.object(plugin, "_run_cmd") as command:
                    status = plugin._get_status_sync()
                self.assertFalse(status["success"])
                self.assertIn("network ownership", status["message"])
                command.assert_not_called()
                self.assertEqual(path.read_text(encoding="utf-8"), "{broken")
                self.assertEqual(backup.read_text(encoding="utf-8"), "[]")

    def test_recovered_backup_retains_wifi_ownership(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "wifi_settings.json"
            path.write_text("{broken", encoding="utf-8")
            state = dict(wifi.DEFAULT_SETTINGS)
            state.update(band_policy=wifi.BAND_POLICY_HIGH_ONLY,
                         band_preference_enabled=True,
                         band_policy_state={"original": {"iwd": {"modifier_present": False}}})
            Path(str(path) + ".bak").write_text(json.dumps(state), encoding="utf-8")
            with patch.object(wifi, "SETTINGS_FILE", str(path)), \
                 patch.object(wifi, "_SETTINGS_MANAGER", None), \
                 patch.object(wifi, "_SETTINGS_MANAGER_PATH", ""):
                loaded = wifi._load_settings()
            self.assertTrue(loaded["band_preference_enabled"])
            self.assertEqual(loaded["band_policy_state"], state["band_policy_state"])


if __name__ == "__main__":
    unittest.main()

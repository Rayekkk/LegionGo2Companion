# SPDX-License-Identifier: BSD-3-Clause
"""Offline WiFi input and durable ownership recovery regressions."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_integration  # Install isolated Decky stubs before backend imports.
import wifi_backend as wifi
from safe_settings import CorruptSettings


class WifiSafetyTests(unittest.TestCase):
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

# SPDX-License-Identifier: BSD-3-Clause
"""Native SteamOS backend/profile changes must not revive an old manager."""
import asyncio
import json
import unittest
from unittest.mock import patch

import test_wifi_strict as strict
import wifi_backend as wifi

NEW = "0f576e6b-2815-4f86-b777-0612eb4987a7"


class BackendSwitchTests(unittest.TestCase):
    setUp = strict.StrictWifiTests.setUp
    change = strict.StrictWifiTests.change

    def use_wpa(self):
        self.plugin._get_current_backend.return_value = "wpa_supplicant"
        self.plugin._get_active_connection_uuid.return_value = NEW
        self.plugin._nmcli_get.side_effect = AssertionError("Inactive iwd cleanup must not read an old NM profile")

    def failed_off_journal(self):
        return {"schema": 1, "phase": "rollback_failed", "operation_id": "old-off",
                "mode_before": wifi.BAND_POLICY_HIGH_ONLY, "mode_target": wifi.BAND_POLICY_OFF,
                "active_uuid_before": strict.UUID, "settings_file": str(self.settings),
                "settings_before": self.plugin._band_settings_fields(wifi._load_settings()),
                "iwd_before": self.plugin._get_iwd_rank_modifier_snapshot(),
                "mutations": {"iwd": {"planned": True, "expected_present": False,
                                        "expected_value": ""}},
                "rollback_iwd_restart_required": True,
                "rollback_reconnect_required": True, "rollback_reconnect_uuid": strict.UUID}

    def assert_wpa_untouched(self):
        self.commands.assert_not_called()
        self.plugin._reconnect_profile.assert_not_called()
        self.plugin._nmcli_get.assert_not_called()
        self.assertEqual(self.plugin._get_active_connection_uuid(), NEW)

    def test_wpa_clears_inactive_iwd_preference_without_touching_new_profile(self):
        self.use_wpa()
        result = self.change(wifi.BAND_POLICY_OFF)
        self.assertTrue(result["success"], result)
        self.assertFalse(result["reconnected"])
        self.assertEqual(result["frequency"], 5320)
        self.assertFalse(self.iwd.exists())
        self.assertEqual(wifi._load_settings()["band_policy"], wifi.BAND_POLICY_OFF)
        self.assert_wpa_untouched()

    def test_disconnected_wpa_clears_inactive_strict_without_old_profile(self):
        self.use_wpa()
        self.plugin._get_active_connection_uuid.return_value = None
        state = wifi._load_settings()
        state["band_policy"] = wifi.BAND_POLICY_STRICT_HIGH
        state["band_policy_state"]["mode"] = wifi.BAND_POLICY_STRICT_HIGH
        state["band_policy_state"]["applied"]["iwd_modifier_value"] = "0.0"
        self.iwd.write_text("[Rank]\nBandModifier2_4GHz=0.0\n")
        wifi._save_settings(state)
        result = self.change(wifi.BAND_POLICY_OFF)
        self.assertTrue(result["success"], result)
        self.commands.assert_not_called()
        self.plugin._reconnect_profile.assert_not_called()
        self.plugin._nmcli_get.assert_not_called()

    def test_wpa_retries_old_failed_off_journal_then_clears_inactive_preference(self):
        self.use_wpa()
        journal = self.failed_off_journal()
        self.plugin._save_band_policy_journal(journal)
        result = self.change(wifi.BAND_POLICY_OFF)
        self.assertTrue(result["success"], result)
        self.assertIsNone(self.plugin._load_band_policy_journal())
        self.assertFalse(self.iwd.exists())
        self.assert_wpa_untouched()

    def test_wpa_rollback_restores_only_file_without_reviving_iwd_or_old_uuid(self):
        self.use_wpa()
        journal = self.failed_off_journal()
        self.iwd.unlink()
        result = self.plugin._restore_band_policy_transaction(journal, "native backend changed")
        self.assertTrue(result["success"], result)
        self.assertIn("BandModifier2_4GHz=0.01", self.iwd.read_text())
        self.assertFalse(journal["rollback_iwd_restart_required"])
        self.assertFalse(journal["rollback_reconnect_required"])
        self.assert_wpa_untouched()

    def test_native_backend_change_after_file_write_rolls_back_without_service_start(self):
        original = self.plugin._set_iwd_rank_modifier
        def write(value, **kwargs):
            result = original(value, **kwargs)
            self.plugin._get_current_backend.return_value = "wpa_supplicant"
            self.plugin._get_active_connection_uuid.return_value = NEW
            return result
        with patch.object(self.plugin, "_set_iwd_rank_modifier", side_effect=write):
            result = self.change(wifi.BAND_POLICY_STRICT_HIGH)
        self.assertFalse(result["success"])
        self.assertTrue(result["rolled_back"], result)
        self.assertTrue(all(call.args[0][:2] == ["/usr/bin/systemctl", "is-active"]
                            for call in self.commands.call_args_list))
        self.plugin._reconnect_profile.assert_not_called()
        self.assertIn("BandModifier2_4GHz=0.01", self.iwd.read_text())

    def test_same_saved_iwd_policy_on_wpa_is_rejected_before_success_noop(self):
        self.use_wpa()
        result = self.change(wifi.BAND_POLICY_HIGH_ONLY)
        self.assertEqual(result["error"], "unsupported_backend")
        self.assert_wpa_untouched()

    def test_unknown_backend_blocks_even_off_without_mutations(self):
        self.plugin._get_current_backend.return_value = None
        before = self.settings.read_bytes(), self.iwd.read_bytes()
        result = self.change(wifi.BAND_POLICY_OFF)
        self.assertEqual(result["error"], "unsupported_backend")
        self.assertEqual(before, (self.settings.read_bytes(), self.iwd.read_bytes()))
        self.assertIsNone(self.plugin._load_band_policy_journal())
        self.commands.assert_not_called()

    def test_inactive_iwd_is_never_started_by_reload(self):
        self.commands.return_value = {"success": False, "stdout": "inactive"}
        result = self.plugin._restart_selected_iwd()
        self.assertFalse(result["success"])
        self.assertEqual(self.commands.call_count, 1)
        self.assertEqual(self.commands.call_args.args[0], ["/usr/bin/systemctl", "is-active", "iwd"])

    def test_inactive_iwd_race_during_try_restart_is_reported(self):
        self.commands.side_effect = [{"success": True, "stdout": "active"},
                                     {"success": True}, {"success": False, "stdout": "inactive"}]
        result = self.plugin._restart_selected_iwd()
        self.assertFalse(result["success"])
        self.assertEqual(self.commands.call_args_list[1].args[0],
                         ["/usr/bin/systemctl", "try-restart", "iwd"])

    def test_incomplete_setter_rollback_keeps_independent_watchdog(self):
        with patch.object(self.plugin, "_verify_band_policy", side_effect=RuntimeError("link lost")), \
             patch.object(self.plugin, "_restore_band_policy_transaction", return_value={"success": False}):
            result = self.change(wifi.BAND_POLICY_STRICT_HIGH)
        self.assertFalse(result["success"])
        self.assertFalse(result["rolled_back"])
        self.plugin._cancel_band_policy_rollback.assert_not_called()
        self.assertIsNotNone(self.plugin._load_band_policy_journal())

    def test_generic_rollback_does_not_replace_new_user_connection(self):
        journal = self.failed_off_journal()
        journal["mutations"] = {"nm_band": {"planned": True, "expected": "a"}}
        journal["profile_before"] = {"uuid": strict.UUID, "band": "", "was_active": True}
        journal["rollback_iwd_restart_required"] = False
        self.plugin._get_active_connection_uuid.return_value = NEW
        self.plugin._nmcli_get.return_value = ("", {"success": True})
        with patch.object(self.plugin, "_profile_is_active", return_value=False):
            result = self.plugin._restore_band_policy_transaction(journal, "user changed profile")
        self.assertTrue(result["success"], result)
        self.plugin._reconnect_profile.assert_not_called()

    def test_automatic_iwd_recovery_does_not_interrupt_native_wpa_activation(self):
        self.use_wpa()
        self.plugin._get_active_connection_uuid.return_value = None
        journal = self.failed_off_journal()
        journal.update(operation="automatic_high_band_reconnect", mutations={})
        result = self.plugin._restore_band_policy_transaction(journal, "native WPA activation")
        self.assertTrue(result["success"], result)
        self.commands.assert_not_called()
        self.plugin._reconnect_profile.assert_not_called()

    def test_unknown_backend_preserves_unfinished_recovery_without_reconnecting(self):
        self.plugin._get_current_backend.return_value = None
        journal = self.failed_off_journal()
        journal.update(operation="automatic_high_band_reconnect", mutations={}, rollback_iwd_restart_required=False)
        result = self.plugin._restore_band_policy_transaction(journal, "backend unknown")
        self.assertFalse(result["success"])
        self.assertEqual(journal["phase"], "rollback_failed")
        self.commands.assert_not_called()
        self.plugin._reconnect_profile.assert_not_called()

    def status_commands(self, argv, **kwargs):
        if "GENERAL.STATE,GENERAL.CON-UUID" in argv:
            return {"success": True, "stdout": "100 (connected)\n" + NEW}
        if argv[-1] == "link":
            return {"success": True, "stdout": "Connected to aa:bb:cc:dd:ee:ff (on wlan0)\n\tSSID:  spaced name \n\tfreq: 2412.0\n\tsignal: -40 dBm\n"}
        if argv[-1] == "info":
            return {"success": True, "stdout": "channel 1 (2412 MHz), width: 20 MHz"}
        raise AssertionError(argv)

    def test_actual_wpa_status_exposes_inactive_policy_and_preserves_ssid_spaces(self):
        self.use_wpa()
        state = wifi._load_settings()
        state.update(device_family="legion_go_2", driver="mt7921e")
        wifi._save_settings(state)
        with patch.object(self.plugin, "_get_wifi_interface", return_value="wlan0"), \
             patch.object(self.plugin, "_get_support_tier", return_value=2), \
             patch.object(self.plugin, "_run_cmd", side_effect=self.status_commands):
            result = self.plugin._get_status_sync()
        self.assertTrue(result["success"], result)
        self.assertTrue(result["connected"])
        self.assertEqual(result["live"]["connection_state"], "connected")
        self.assertEqual(result["live"]["ssid"], " spaced name ")
        self.assertEqual(result["live"]["band_policy"], wifi.BAND_POLICY_OFF)
        self.assertEqual(result["live"]["saved_band_policy"], wifi.BAND_POLICY_HIGH_ONLY)
        self.assertTrue(result["live"]["saved_policy_inactive"])
        self.assertTrue(result["live"]["band_policy_restore_available"])

    def test_profile_change_during_status_invalidates_cached_radio_and_ssid(self):
        self.use_wpa()
        self.plugin._get_active_connection_uuid.return_value = strict.UUID
        with patch.object(self.plugin, "_get_wifi_interface", return_value="wlan0"), \
             patch.object(self.plugin, "_get_support_tier", return_value=2), \
             patch.object(self.plugin, "_run_cmd", side_effect=self.status_commands):
            result = self.plugin._get_status_sync()
        self.assertTrue(result["success"], result)
        self.assertFalse(result["connected"])
        self.assertEqual(result["live"]["connection_state"], "unknown")
        self.assertNotIn("ssid", result["live"])
        self.assertNotIn("frequency", result["live"])


class ConnectionStateTests(unittest.TestCase):
    def test_missing_or_failed_inventory_is_unavailable_not_disconnected(self):
        plugin = wifi.Plugin()
        for response in ({"success": False}, {"success": True, "stdout": "eth0:ethernet"}):
            with self.subTest(response=response), \
                 patch.object(wifi, "_load_settings", return_value=dict(wifi.DEFAULT_SETTINGS)), \
                 patch.object(plugin, "_get_current_backend", return_value="iwd"), \
                 patch.object(plugin, "_get_network_recovery_issues", return_value=[]), \
                 patch.object(plugin, "_load_band_policy_journal", return_value=None), \
                 patch.object(plugin, "_load_power_save_journal", return_value=None), \
                 patch.object(plugin, "_run_cmd", return_value=response):
                status = plugin._get_status_sync()
            self.assertTrue(status["success"], status)
            self.assertFalse(status["connected"])
            self.assertEqual(status["live"]["connection_state"], "unknown")

    def test_state_names_distinguish_activation_from_connection(self):
        plugin = wifi.Plugin()
        for state, expected in (("30 (disconnected)", "disconnected"),
                                ("50 (connecting (configuring))", "connecting"),
                                ("70", "connecting"), ("100 (connected)", "connected"),
                                ("invalid", "unknown")):
            with self.subTest(state=state), patch.object(plugin, "_run_cmd", return_value={
                    "success": True, "stdout": state + "\n" + NEW}):
                self.assertEqual(plugin._get_connection_state("wlan0")["connection_state"], expected)
        with patch.object(plugin, "_run_cmd", return_value={"success": False}):
            self.assertEqual(plugin._get_connection_state("wlan0")["connection_state"], "unknown")

    def test_unconfigured_backend_with_competing_services_is_unknown(self):
        plugin = wifi.Plugin()
        with patch("builtins.open", side_effect=FileNotFoundError), \
             patch.object(plugin, "_run_cmd", return_value={"success": True, "stdout": "active"}):
            self.assertIsNone(plugin._get_current_backend())

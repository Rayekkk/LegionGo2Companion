# SPDX-License-Identifier: BSD-3-Clause
"""Native SteamOS backend/profile changes must not revive an old manager."""
import subprocess
import unittest
from unittest.mock import mock_open, patch

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

    def test_inactive_iwd_is_never_started_and_a_race_is_reported(self):
        self.commands.return_value = {"success": False, "stdout": "inactive"}
        self.assertFalse(self.plugin._restart_selected_iwd()["success"])
        self.assertEqual([call.args[0] for call in self.commands.call_args_list],
                         [["/usr/bin/systemctl", "is-active", "iwd"]])
        # iwd stopping during the reload is reported, never answered with a start.
        self.commands.reset_mock()
        self.commands.side_effect = [{"success": True, "stdout": "active"},
                                     {"success": True}, {"success": False, "stdout": "inactive"}]
        self.assertFalse(self.plugin._restart_selected_iwd()["success"])
        self.assertEqual([call.args[0] for call in self.commands.call_args_list][:2],
                         [["/usr/bin/systemctl", "is-active", "iwd"], ["/usr/bin/systemctl", "try-restart", "iwd"]])
        self.assertFalse(any("start" in call.args[0] for call in self.commands.call_args_list))

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
        self.plugin._profile_is_active.return_value = False
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

    def test_missing_or_failed_inventory_is_unavailable_not_disconnected(self):
        for response in ({"success": False}, {"success": True, "stdout": "eth0:ethernet"}):
            with self.subTest(response=response):
                self.commands.return_value = response
                status = self.plugin._get_status_sync()
                self.assertTrue(status["success"], status)
                self.assertFalse(status["connected"])
                self.assertEqual(status["live"]["connection_state"], "unknown")

    def test_restore_is_offered_only_while_our_iwd_change_is_recoverable(self):
        state = wifi._load_settings()
        state.update(device_family="legion_go_2", driver="mt7921e")
        wifi._save_settings(state)
        for iwd, available, drift in (("[Rank]\nBandModifier2_4GHz=0.01\n", True, False),  # still ours
                                      (None, True, True),     # an OS rollback put the baseline back
                                      ("[Rank]\nBandModifier2_4GHz=0.5\n", False, True)):  # someone else's
            with self.subTest(iwd=iwd):
                self.iwd.unlink(missing_ok=True)
                if iwd:
                    self.iwd.write_text(iwd, encoding="utf-8")
                live = self.plugin._get_status_sync()["live"]
                self.assertIs(live["band_policy_restore_available"], available)
                self.assertIs("band_policy_error" in live, drift)

    def status_commands(self, argv, **kwargs):
        if argv[-2:] == ["dev", "status"]:
            return {"success": True, "stdout": "wlan0:wifi\n"}
        if argv == ["/usr/bin/nmcli", "--version"]:
            return {"success": True, "stdout": "nmcli tool, version 1.52.1"}
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
        self.commands.side_effect = self.status_commands
        result = self.plugin._get_status_sync()
        self.assertTrue(result["success"], result)
        self.assertTrue(result["connected"])
        self.assertEqual(result["live"]["connection_state"], "connected")
        self.assertEqual(result["live"]["ssid"], " spaced name ")
        self.assertEqual(result["live"]["band_policy"], wifi.BAND_POLICY_OFF)
        self.assertEqual(result["settings"]["band_policy"], wifi.BAND_POLICY_HIGH_ONLY)
        self.assertTrue(result["live"]["saved_policy_inactive"])
        self.assertTrue(result["live"]["band_policy_restore_available"])
        self.assertIs(result["live"]["six_ghz_only_supported"], False)

    def test_profile_change_during_status_invalidates_cached_radio_and_ssid(self):
        self.use_wpa()
        self.plugin._get_active_connection_uuid.return_value = strict.UUID
        self.commands.side_effect = self.status_commands
        result = self.plugin._get_status_sync()
        self.assertTrue(result["success"], result)
        self.assertFalse(result["connected"])
        self.assertEqual(result["live"]["connection_state"], "unknown")
        self.assertNotIn("ssid", result["live"])
        self.assertNotIn("frequency", result["live"])

    def test_status_link_and_channel_fields(self):
        self.use_wpa()
        link = ("Connected to aa:bb:cc:dd:ee:ff (on wlan0)\n\tSSID: Home\n\tfreq: 5180.0\n"
                "\tsignal: -52 dBm\n\ttx bitrate: 866.7 MBit/s VHT-MCS 9 80MHz\n")
        for info, channel in (
            ("channel 1 (2412 MHz), width: 20 MHz", "1 (20 MHz)"),
            ("\tchannel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz\n", "36 (80 MHz)"),
            ("channel 36 (5180 MHz)", "36"),
            ("channel", "channel"),
            ("channel 6, width: 20 MHz\n\tchannel 11, width: 40 MHz, width: 20 MHz", "11 (20 MHz)"),
            ("type managed\n", None),
        ):
            def command(argv, **kwargs):
                if argv[-1] == "link":
                    return {"success": True, "stdout": link}
                if argv[-1] == "info":
                    return {"success": True, "stdout": info}
                return self.status_commands(argv, **kwargs)

            with self.subTest(info=info):
                self.commands.side_effect = command
                live = self.plugin._get_status_sync()["live"]
            self.assertEqual({key: live.get(key) for key in
                              ("ssid", "frequency", "signal_dbm", "tx_bitrate", "channel")},
                             {"ssid": "Home", "frequency": "5180.0", "signal_dbm": "-52 dBm",
                              "tx_bitrate": "866.7 MBit/s VHT-MCS 9 80MHz", "channel": channel})


class ConnectionStateTests(unittest.TestCase):
    def test_state_names_distinguish_activation_from_connection(self):
        plugin = wifi.Plugin()
        # Real nmcli -g output: a device without an active profile prints an empty CON-UUID line.
        for stdout, expected in (("30 (disconnected)\n\n", "disconnected"),
                                 ("20 (unavailable)\n\n", "disconnected"),
                                 ("50 (connecting (configuring))\n" + NEW + "\n", "connecting"),
                                 ("70\n" + NEW + "\n", "connecting"),
                                 ("100 (connected)\n" + NEW + "\n", "connected"),
                                 ("invalid\n\n", "unknown")):
            done = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
            with self.subTest(stdout=stdout), patch.object(wifi.subprocess, "run", return_value=done):
                self.assertEqual(plugin._get_connection_state("wlan0")["connection_state"], expected)
        with patch.object(plugin, "_run_cmd", return_value={"success": False}):
            self.assertEqual(plugin._get_connection_state("wlan0")["connection_state"], "unknown")

    def test_effective_networkmanager_config_overrides_distribution_file(self):
        plugin = wifi.Plugin()
        distro = "[device]\nwifi.backend=iwd\n"  # /usr/lib default; an /etc override wins in NM
        effective = ("# NetworkManager configuration: /usr/lib/NetworkManager/conf.d/"
                     "10-steamos-defaults.conf, /etc/NetworkManager/conf.d/zz-user.conf\n\n"
                     "[main]\ndns=systemd-resolved\n\n[device-wlan1]\nwifi.backend=iwd\n\n"
                     "[device]\nwifi.backend=wpa_supplicant\nwifi.iwd.autoconnect=yes\n")
        services = {"success": True, "stdout": "active"}

        def run(cmd, timeout=5, preserve_stdout=False):
            if cmd[-1] == "--print-config":
                return {"success": True, "stdout": effective}
            return services

        opener = patch("builtins.open", mock_open(read_data=distro))
        with opener, patch.object(plugin, "_run_cmd", side_effect=run):
            self.assertEqual(plugin._get_current_backend(), "wpa_supplicant")
        effective = effective.replace("wifi.backend=wpa_supplicant", "wifi.backend=iwd")
        with opener, patch.object(plugin, "_run_cmd", side_effect=run):
            self.assertEqual(plugin._get_current_backend(), "iwd")
        # Without wifi.backend NM uses its built-in default: only one running service decides.
        effective = "[device]\nwifi.iwd.autoconnect=yes\n"
        with opener, patch.object(plugin, "_run_cmd", side_effect=run):
            self.assertIsNone(plugin._get_current_backend())
        # print-config failing keeps the file-based detection.
        with opener, patch.object(plugin, "_run_cmd", return_value={"success": False, "stdout": ""}):
            self.assertEqual(plugin._get_current_backend(), "iwd")

    def test_unconfigured_backend_with_competing_services_is_unknown(self):
        plugin = wifi.Plugin()
        with patch("builtins.open", side_effect=FileNotFoundError), \
             patch.object(plugin, "_run_cmd", return_value={"success": True, "stdout": "active"}):
            self.assertIsNone(plugin._get_current_backend())

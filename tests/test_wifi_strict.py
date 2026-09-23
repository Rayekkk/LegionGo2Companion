# SPDX-License-Identifier: BSD-3-Clause
"""Strict WiFi policy transitions use one owned iwd write and recover safely."""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_integration  # Install isolated Decky stubs before backend imports.
import module_control
import wifi_backend as wifi


UUID = "da58df8b-cdb1-447d-80ab-19d11a634e7e"


class StrictWifiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.iwd = self.root / "main.conf"
        self.settings = self.root / "wifi_settings.json"
        self.journal_dir = self.root / "journal"
        self.runtime_dir = self.root / "runtime"
        for name, value in {
            "IWD_MAIN_CONF": str(self.iwd), "SETTINGS_FILE": str(self.settings),
            "BAND_POLICY_STATE_DIR": str(self.journal_dir),
            "BAND_POLICY_RUNTIME_DIR": str(self.runtime_dir),
            "BAND_POLICY_JOURNAL_FILE": str(self.journal_dir / "band-policy-transaction.json"),
            "_SETTINGS_MANAGER": None, "_SETTINGS_MANAGER_PATH": "",
        }.items():
            self.stack.enter_context(patch.object(wifi, name, value))
        self.plugin = wifi.Plugin()
        self.baseline = self.plugin._get_iwd_rank_modifier_snapshot()
        self.iwd.write_text("[Rank]\nBandModifier2_4GHz=0.01\n", encoding="utf-8")
        state = dict(wifi.DEFAULT_SETTINGS)
        state.update({
            "band_policy": wifi.BAND_POLICY_HIGH_ONLY,
            "band_preference_enabled": True,
            "band_policy_state": {
                "schema": 1, "mode": wifi.BAND_POLICY_HIGH_ONLY,
                "connection_uuid": UUID,
                "original": {"nm_band": "", "iwd": self.baseline},
                "owns_nm_band": False, "owns_iwd": True,
                "applied": {"nm_band": "", "iwd_modifier_present": True,
                            "iwd_modifier_value": "0.01"},
            },
        })
        self.settings.write_text(json.dumps(state), encoding="utf-8")
        for name, value in {
            "_detect_device_family": ("83N0", "legion_go_2", "Legion Go 2"),
            "_detect_wifi_driver": "mt7921e", "_get_current_backend": "iwd",
            "_get_active_connection_uuid": UUID,
            "_nmcli_get": ("", {"success": True}),
            "_profile_is_active": True, "_get_link_frequency": 5320,
            "_get_band_policy_capabilities_sync": {
                "five_six_only_available": True,
            },
        }.items():
            self.stack.enter_context(patch.object(self.plugin, name, return_value=value))
        self.commands = self.stack.enter_context(patch.object(
            self.plugin, "_run_cmd", return_value={"success": True, "stdout": "active\n"}))
        self.stack.enter_context(patch.object(
            self.plugin, "_schedule_band_policy_rollback", return_value={"success": True}))
        self.stack.enter_context(patch.object(self.plugin, "_cancel_band_policy_rollback"))
        self.stack.enter_context(patch.object(
            self.plugin, "_reconnect_profile", return_value={"success": True}))

    def change(self, mode, allow_unverified_scan=False):
        return asyncio.run(self.plugin._set_band_policy_impl(
            mode, lock_already_held=True,
            allow_unverified_scan=allow_unverified_scan))

    def test_preference_to_strict_writes_iwd_once_and_preserves_baseline(self):
        original_write = self.plugin._set_iwd_rank_modifier
        with patch.object(self.plugin, "_set_iwd_rank_modifier", wraps=original_write) as write:
            result = self.change(wifi.BAND_POLICY_STRICT_HIGH)
        self.assertTrue(result["success"], result)
        write.assert_called_once_with("0.0")
        self.assertIn("BandModifier2_4GHz=0.0", self.iwd.read_text(encoding="utf-8"))
        saved = wifi._load_settings()
        self.assertEqual(saved["band_policy"], wifi.BAND_POLICY_STRICT_HIGH)
        self.assertFalse(saved["band_preference_enabled"])
        self.assertEqual(saved["band_policy_state"]["original"]["iwd"], self.baseline)
        self.assertFalse((self.journal_dir / "band-policy-transaction.json").exists())

    def test_failed_strict_connection_rolls_back_preference_and_iwd_file(self):
        with patch.object(self.plugin, "_verify_band_policy", new_callable=AsyncMock,
                          return_value={"success": False, "message": "Stayed on 2.4 GHz"}):
            result = self.change(wifi.BAND_POLICY_STRICT_HIGH)
        self.assertFalse(result["success"])
        self.assertTrue(result["rolled_back"], result)
        self.assertEqual(wifi._load_settings()["band_policy"], wifi.BAND_POLICY_HIGH_ONLY)
        self.assertIn("BandModifier2_4GHz=0.01", self.iwd.read_text(encoding="utf-8"))
        self.assertFalse((self.journal_dir / "band-policy-transaction.json").exists())

    def test_strict_can_be_turned_off_after_wifi_disconnects(self):
        self.assertTrue(self.change(wifi.BAND_POLICY_STRICT_HIGH)["success"])
        with patch.object(self.plugin, "_get_active_connection_uuid", return_value=None), \
             patch.object(self.plugin, "_profile_is_active", return_value=False):
            result = self.change(wifi.BAND_POLICY_OFF)
        self.assertTrue(result["success"], result)
        self.assertEqual(wifi._load_settings()["band_policy"], wifi.BAND_POLICY_OFF)
        self.assertFalse(self.iwd.exists(), "the pre-plugin iwd baseline is restored")

    def test_strict_rejected_before_any_file_write_when_high_band_absent(self):
        with patch.object(self.plugin, "_get_band_policy_capabilities_sync",
                          return_value={"five_six_only_available": False,
                                        "reason_five_six_only": "No high-band BSS"}):
            result = self.change(wifi.BAND_POLICY_STRICT_HIGH)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "preflight_failed")
        self.assertIn("BandModifier2_4GHz=0.01", self.iwd.read_text(encoding="utf-8"))
        self.assertEqual(wifi._load_settings()["band_policy"], wifi.BAND_POLICY_HIGH_ONLY)
        self.assertFalse((self.journal_dir / "band-policy-transaction.json").exists())

    def test_confirmed_attempt_can_try_without_a_visible_high_band(self):
        with patch.object(self.plugin, "_get_band_policy_capabilities_sync",
                          return_value={"five_six_only_available": False,
                                        "five_six_only_try_available": True}) as preflight:
            result = self.change(wifi.BAND_POLICY_STRICT_HIGH,
                                 allow_unverified_scan=True)
        self.assertTrue(result["success"], result)
        preflight.assert_called_once_with(False)
        self.assertEqual(wifi._load_settings()["band_policy"],
                         wifi.BAND_POLICY_STRICT_HIGH)
        self.assertIn("BandModifier2_4GHz=0.0", self.iwd.read_text(encoding="utf-8"))

    def test_confirmed_attempt_still_rejects_hard_blockers(self):
        with patch.object(self.plugin, "_get_band_policy_capabilities_sync",
                          return_value={"five_six_only_try_available": False,
                                        "reason_five_six_only_try": "Watchdog unavailable"}):
            result = self.change(wifi.BAND_POLICY_STRICT_HIGH,
                                 allow_unverified_scan=True)
        self.assertEqual(result["error"], "preflight_failed")
        self.assertEqual(result["message"], "Watchdog unavailable")
        self.assertEqual(wifi._load_settings()["band_policy"], wifi.BAND_POLICY_HIGH_ONLY)
        self.assertFalse((self.journal_dir / "band-policy-transaction.json").exists())

    def test_confirmed_attempt_rolls_back_if_actual_link_is_not_high_band(self):
        with patch.object(self.plugin, "_get_band_policy_capabilities_sync",
                          return_value={"five_six_only_try_available": True}), \
             patch.object(self.plugin, "_verify_band_policy", new_callable=AsyncMock,
                          return_value={"success": False,
                                        "message": "No 5/6 GHz connection"}):
            result = self.change(wifi.BAND_POLICY_STRICT_HIGH,
                                 allow_unverified_scan=True)
        self.assertFalse(result["success"])
        self.assertTrue(result["rolled_back"], result)
        self.assertEqual(wifi._load_settings()["band_policy"], wifi.BAND_POLICY_HIGH_ONLY)
        self.assertIn("BandModifier2_4GHz=0.01", self.iwd.read_text(encoding="utf-8"))

    def test_strict_preflight_does_not_accept_cached_high_band(self):
        original_isfile = wifi.os.path.isfile

        def isfile(path):
            if path in ("/usr/bin/systemd-run", "/usr/bin/python3"):
                return True
            return original_isfile(path)

        def command(argv, **_kwargs):
            if argv[:2] == ["/usr/bin/iw", "list"]:
                return {"success": True, "stdout": "* 5320 MHz [64]\n"}
            if argv[:2] == ["/usr/bin/systemctl", "is-active"]:
                return {"success": True, "stdout": "active\n"}
            return {"success": True, "stdout": "1.58.0\n"}

        visible = {"two_ghz": 1, "five_ghz": 1, "six_ghz": 0,
                   "scan_ok": True, "fresh_high_band": False}
        with patch.object(self.plugin, "_get_band_policy_capabilities_sync",
                          wraps=wifi.Plugin._get_band_policy_capabilities_sync.__get__(self.plugin)), \
             patch.object(self.plugin, "_get_wifi_interface", return_value="wlan0"), \
             patch.object(self.plugin, "_get_visible_bands_for_active_ssid", return_value=visible), \
             patch.object(wifi.os.path, "isfile", side_effect=isfile):
            self.commands.side_effect = command
            cached = self.plugin._get_band_policy_capabilities_sync(True)
            self.assertTrue(cached["five_six_no_24_available"])
            self.assertFalse(cached["five_six_only_available"])
            self.assertTrue(cached["five_six_only_try_available"])
            self.assertIn("fresh", cached["reason_five_six_only"])
            visible["fresh_high_band"] = True
            fresh = self.plugin._get_band_policy_capabilities_sync(True)
            self.assertTrue(fresh["five_six_only_available"])

    def test_invalid_policy_types_do_not_start_network_transaction(self):
        for value in (None, 1, [], {}, "5ghz"):
            result = self.change(value)
            self.assertEqual(result["error"], "invalid_band_policy")
        self.commands.assert_not_called()
        self.assertIn("BandModifier2_4GHz=0.01", self.iwd.read_text(encoding="utf-8"))

    def test_invalid_scan_override_does_not_start_network_transaction(self):
        for mode, override in ((wifi.BAND_POLICY_STRICT_HIGH, "true"),
                               (wifi.BAND_POLICY_HIGH_ONLY, True),
                               (wifi.BAND_POLICY_OFF, True)):
            result = self.change(mode, allow_unverified_scan=override)
            self.assertEqual(result["error"], "invalid_scan_override")
        self.commands.assert_not_called()

    def test_strict_verification_rejects_2_4_and_accepts_5_ghz(self):
        with patch.object(self.plugin, "_get_link_frequency", return_value=2437):
            rejected = asyncio.run(self.plugin._verify_band_policy(
                wifi.BAND_POLICY_STRICT_HIGH, UUID, timeout=1))
        self.assertFalse(rejected["success"])
        accepted = asyncio.run(self.plugin._verify_band_policy(
            wifi.BAND_POLICY_STRICT_HIGH, UUID, timeout=1))
        self.assertTrue(accepted["success"])
        self.assertEqual(accepted["frequency"], 5320)

    def test_status_reports_strict_drift_even_when_wifi_disconnected(self):
        self.assertTrue(self.change(wifi.BAND_POLICY_STRICT_HIGH)["success"])
        self.iwd.write_text("[Rank]\nBandModifier2_4GHz=0.5\n", encoding="utf-8")
        with patch.object(self.plugin, "_get_wifi_interface", return_value=None), \
             patch.object(self.plugin, "_get_active_connection_uuid", return_value=None), \
             patch.object(self.plugin, "_get_support_tier", return_value=2):
            status = self.plugin._get_status_sync()
        self.assertTrue(status["success"])
        self.assertFalse(status["connected"])
        self.assertTrue(status["drift"]["band_policy"])
        self.assertIn("outside WiFi Optimizer", status["live"]["band_policy_error"])

    def test_module_resume_remembers_exact_strict_policy(self):
        state = wifi._load_settings()
        state["band_policy"] = wifi.BAND_POLICY_STRICT_HIGH
        wifi._save_settings(state)
        intent = module_control.capture_intent("wifi")
        self.assertEqual(intent["band_policy"], wifi.BAND_POLICY_STRICT_HIGH)
        component = Mock()
        component.set_band_policy = AsyncMock(return_value={"success": True})
        component.set_power_save = AsyncMock(return_value={"success": True})
        asyncio.run(module_control.restore_intent("wifi", component, intent))
        component.set_band_policy.assert_awaited_once_with(wifi.BAND_POLICY_STRICT_HIGH)


class ReconnectPacingTests(unittest.TestCase):
    def test_fast_activation_failures_are_spaced_while_autoconnect_is_checked(self):
        plugin = wifi.Plugin()
        clock = [0.0]
        activations = []
        checks = []

        def run_cmd(argv, **_kwargs):
            if argv[:3] == ["/usr/bin/nmcli", "con", "up"]:
                activations.append(clock[0])
                return {"success": False, "stderr": "No AP yet"}
            return {"success": True, "stdout": "30 (disconnected)\n"}

        def is_active(_uuid):
            checks.append(clock[0])
            return clock[0] >= 13.0

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(plugin, "_run_cmd", side_effect=run_cmd), \
             patch.object(plugin, "_get_wifi_interface", return_value="wlan0"), \
             patch.object(plugin, "_profile_is_active", side_effect=is_active), \
             patch.object(wifi.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(wifi.time, "sleep", side_effect=sleep):
            result = plugin._reconnect_profile(UUID, cycle=False)

        self.assertTrue(result["success"], result)
        self.assertEqual(len(activations), 2)
        self.assertGreaterEqual(activations[0], 8.0)
        self.assertGreaterEqual(activations[1] - activations[0], 3.0)
        self.assertGreater(len(checks), len(activations))

    def test_iwd_autoconnect_before_manual_activation_is_left_alone(self):
        plugin = wifi.Plugin()
        clock = [0.0]
        activations = []

        def run_cmd(argv, **_kwargs):
            if argv[:3] == ["/usr/bin/nmcli", "con", "up"]:
                activations.append(clock[0])
            return {"success": False, "stderr": "unexpected command"}

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(plugin, "_run_cmd", side_effect=run_cmd), \
             patch.object(plugin, "_profile_is_active",
                          side_effect=lambda _uuid: clock[0] >= 4.0), \
             patch.object(wifi.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(wifi.time, "sleep", side_effect=sleep):
            result = plugin._reconnect_profile(UUID, cycle=False)

        self.assertTrue(result["success"], result)
        self.assertEqual(activations, [])


if __name__ == "__main__":
    unittest.main()

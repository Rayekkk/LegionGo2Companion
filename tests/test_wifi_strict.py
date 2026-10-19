# SPDX-License-Identifier: BSD-3-Clause
"""Strict WiFi policy transitions use one owned iwd write and recover safely."""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import support  # Install isolated Decky stubs before backend imports.
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
            "BAND_POLICY_LOCK_FILE": str(self.runtime_dir / "band-policy.lock"),
            "_SETTINGS_MANAGER": None, "_SETTINGS_MANAGER_PATH": "",
        }.items():
            self.stack.enter_context(patch.object(wifi, name, value))
        self.plugin = wifi.Plugin()
        self.baseline = self.plugin._get_iwd_rank_modifier_snapshot()
        self.iwd.write_text("[Rank]\nBandModifier2_4GHz=0.01\n", encoding="utf-8")
        state = dict(wifi.DEFAULT_SETTINGS)
        state.update({
            "band_policy": wifi.BAND_POLICY_HIGH_ONLY,
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
            "_get_iwd_profile_conflict": "",
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
        return self.plugin._set_band_policy_impl(mode, allow_unverified_scan=allow_unverified_scan)

    def test_preference_to_strict_writes_iwd_once_and_preserves_baseline(self):
        original_write = self.plugin._set_iwd_rank_modifier
        with patch.object(self.plugin, "_set_iwd_rank_modifier", wraps=original_write) as write:
            result = self.change(wifi.BAND_POLICY_STRICT_HIGH)
        self.assertTrue(result["success"], result)
        write.assert_called_once_with("0.0")
        self.assertIn("BandModifier2_4GHz=0.0", self.iwd.read_text(encoding="utf-8"))
        saved = wifi._load_settings()
        self.assertEqual(saved["band_policy"], wifi.BAND_POLICY_STRICT_HIGH)
        self.assertEqual(saved["band_policy_state"]["original"]["iwd"], self.baseline)
        self.assertFalse((self.journal_dir / "band-policy-transaction.json").exists())

    def test_turning_a_policy_off_is_not_rolled_back_by_a_failed_reconnect(self):
        failed = {"success": False, "stderr": "No network with SSID found."}
        with patch.object(self.plugin, "_reconnect_profile", return_value=failed):
            result = self.change(wifi.BAND_POLICY_OFF)
        self.assertTrue(result["success"], result)
        self.assertFalse(result["rolled_back"])
        self.assertFalse(result["reconnected"])
        self.assertFalse(self.iwd.exists() and "BandModifier2_4GHz" in self.iwd.read_text(encoding="utf-8"))
        self.assertEqual(wifi._load_settings()["band_policy"], wifi.BAND_POLICY_OFF)
        self.assertFalse((self.journal_dir / "band-policy-transaction.json").exists())

    def test_six_only_off_keeps_the_cleared_band_when_no_ap_is_reachable(self):
        self.iwd.unlink()
        state = dict(wifi.DEFAULT_SETTINGS)
        state.update(band_policy=wifi.BAND_POLICY_SIX_ONLY, band_policy_state={
            "schema": 1, "mode": wifi.BAND_POLICY_SIX_ONLY, "connection_uuid": UUID,
            "original": {"nm_band": "", "iwd": self.baseline},
            "owns_nm_band": True, "owns_iwd": False,
            "applied": {"nm_band": "6GHz", "iwd_modifier_present": False, "iwd_modifier_value": ""}})
        self.settings.write_text(json.dumps(state), encoding="utf-8")
        fields = {"802-11-wireless.band": "6GHz"}
        plugin = self.plugin
        with patch.object(plugin, "_get_current_backend", return_value="wpa_supplicant"), \
             patch.object(plugin, "_nmcli_get", side_effect=lambda _u, f, **k: (fields.get(f, ""), {"success": True})), \
             patch.object(plugin, "_nmcli_modify", side_effect=lambda _u, f, v, **k: (fields.__setitem__(f, v), {"success": True})[1]), \
             patch.object(plugin, "_get_link_frequency", return_value=None), \
             patch.object(plugin, "_reconnect_profile", return_value={"success": False, "stderr": "No AP"}):
            result = self.change(wifi.BAND_POLICY_OFF)
        self.assertTrue(result["success"], result)
        self.assertEqual(fields["802-11-wireless.band"], "")
        self.assertEqual(wifi._load_settings()["band_policy"], wifi.BAND_POLICY_OFF)

    def test_failed_strict_connection_rolls_back_preference_and_iwd_file(self):
        with patch.object(self.plugin, "_verify_band_policy",
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
             patch.object(self.plugin, "_verify_band_policy",
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

    def test_six_ghz_only_preflight_skips_the_5ghz_searches(self):
        scans = []
        def command(argv, **_kwargs):
            scans.append(argv)
            return {"success": True, "stdout": "first" if len(scans) == 1 else ""}
        five = [5180, 5200] + list(range(5260, 5340, 20)) + list(range(5500, 5740, 20))
        with patch.object(self.plugin, "_get_profile_ssid", return_value="Home"), \
             patch.object(self.plugin, "_iw_scan_command", side_effect=lambda _i, _s, freqs: ["iw", *map(str, freqs)]), \
             patch.object(self.plugin, "_iw_scan_bss", side_effect=lambda out, _s: [{"frequency": 6295}] if out == "first" else []), \
             patch.object(self.plugin, "_nmcli_wifi_bss", return_value=None):
            self.commands.side_effect = command
            for need_five, expected in ((False, 1), (True, 2 + 16)):
                with self.subTest(need_five=need_five):
                    scans.clear(); self.plugin._visible_band_cache = None
                    bands = self.plugin._get_visible_bands_for_active_ssid(
                        "wlan0", "uuid", True, five_ghz_frequencies=five, six_ghz_frequencies=[6295],
                        need_five=need_five)
                    self.assertEqual((bands["six_ghz"], len(scans)), (1, expected))

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
            rejected = self.plugin._verify_band_policy(wifi.BAND_POLICY_STRICT_HIGH, UUID, timeout=1)
        self.assertFalse(rejected["success"])
        accepted = self.plugin._verify_band_policy(wifi.BAND_POLICY_STRICT_HIGH, UUID, timeout=1)
        self.assertTrue(accepted["success"])
        self.assertEqual(accepted["frequency"], 5320)

    def test_automatic_restores_the_iwd_original_over_an_outside_change(self):
        self.assertTrue(self.change(wifi.BAND_POLICY_STRICT_HIGH)["success"])
        self.iwd.write_text("[Rank]\nBandModifier2_4GHz=0.5\n", encoding="utf-8")   # someone else's value
        self.assertEqual(self.change(wifi.BAND_POLICY_STRICT_HIGH)["error"], "ownership_conflict")
        result = self.change(wifi.BAND_POLICY_OFF)
        self.assertTrue(result["success"], result)
        self.assertIn("changed outside Companion was put back", result["message"])
        self.assertEqual(wifi._load_settings()["band_policy"], wifi.BAND_POLICY_OFF)
        self.assertFalse(self.iwd.exists(), "the pre-plugin iwd baseline is restored")

    def test_status_reports_strict_drift_even_when_wifi_disconnected(self):
        self.assertTrue(self.change(wifi.BAND_POLICY_STRICT_HIGH)["success"])
        self.iwd.write_text("[Rank]\nBandModifier2_4GHz=0.5\n", encoding="utf-8")
        with patch.object(self.plugin, "_get_wifi_interface", return_value=None), \
             patch.object(self.plugin, "_get_active_connection_uuid", return_value=None):
            status = self.plugin._get_status_sync()
        self.assertTrue(status["success"])
        self.assertFalse(status["connected"])
        self.assertTrue(status["drift"]["band_policy"])
        self.assertIn("outside Legion Go 2 Companion", status["live"]["band_policy_error"])

    def test_module_resume_remembers_exact_strict_policy(self):
        state = wifi._load_settings()
        state["band_policy"] = wifi.BAND_POLICY_STRICT_HIGH
        wifi._save_settings(state)
        intent = module_control.capture_intent("wifi")
        self.assertEqual(intent["band_policy"], wifi.BAND_POLICY_STRICT_HIGH)
        component = Mock()
        component.set_band_policy = AsyncMock(return_value={"success": True})
        asyncio.run(module_control.restore_intent("wifi", component, intent))
        component.set_band_policy.assert_awaited_once_with(wifi.BAND_POLICY_STRICT_HIGH)


class ReconnectPacingTests(unittest.TestCase):
    def test_slow_cli_failure_leaves_time_for_verified_native_recovery(self):
        plugin = wifi.Plugin()
        clock = [0.0]
        budgets = []
        activation_starts = []
        def command(argv, **kwargs):
            if argv[:3] == ['/usr/bin/nmcli', 'con', 'up']:
                activation_starts.append(clock[0])
                budgets.append(kwargs['timeout'])
                clock[0] += kwargs['timeout']
                return {'success': False, 'stderr': 'Secrets were required, but not provided'}
            return {'success': True, 'stdout': '30 (disconnected)'}
        with patch.object(plugin, '_get_current_backend', return_value='wpa_supplicant'), \
             patch.object(plugin, '_get_wifi_interface', return_value='wlan0'), \
             patch.object(plugin, '_run_cmd', side_effect=command), \
             patch.object(plugin, '_profile_is_active', side_effect=lambda *_: clock[0] >= 23), \
             patch.object(plugin, '_get_link_frequency', return_value=6295), \
             patch.object(wifi.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(wifi.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            result = plugin._reconnect_profile(UUID, cycle=False)
        self.assertTrue(result['success'], result)
        self.assertTrue(budgets, 'the slow activation failure must be exercised')
        self.assertTrue(all(0 < budget < 25 - started
                            for started, budget in zip(activation_starts, budgets)), budgets)
        self.assertGreaterEqual(clock[0], 23, 'success must wait for the verified native link')
        self.assertLessEqual(clock[0], 25)

    def test_verified_link_at_deadline_wins_over_earlier_cli_failure(self):
        plugin = wifi.Plugin()
        clock = [0.0]
        def command(argv, **kwargs):
            if argv[:3] == ['/usr/bin/nmcli', 'con', 'up']:
                clock[0] = 25.0
                return {'success': False, 'stderr': 'Activation failed'}
            return {'success': True, 'stdout': '30 (disconnected)'}
        with patch.object(plugin, '_get_current_backend', return_value='wpa_supplicant'), \
             patch.object(plugin, '_get_wifi_interface', return_value='wlan0'), \
             patch.object(plugin, '_run_cmd', side_effect=command), \
             patch.object(plugin, '_profile_is_active', side_effect=lambda *_: clock[0] >= 25), \
             patch.object(plugin, '_get_link_frequency', return_value=6295), \
             patch.object(wifi.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(wifi.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            result = plugin._reconnect_profile(UUID, cycle=False)
        self.assertTrue(result['success'], result)
        self.assertTrue(result['reconnected'])

    def test_fast_activation_failures_are_spaced_while_autoconnect_is_checked(self):
        plugin = wifi.Plugin()
        clock = [0.0]
        activations = []
        checks = []
        radio_checks = []

        def run_cmd(argv, **_kwargs):
            if argv[:3] == ["/usr/bin/nmcli", "con", "up"]:
                activations.append(clock[0])
                return {"success": False, "stderr": "No AP yet"}
            return {"success": True, "stdout": "30 (disconnected)\n"}

        def is_active(*_):
            checks.append(clock[0])
            return clock[0] >= 13.0

        def sleep(seconds):
            clock[0] += seconds

        def frequency(*_):
            radio_checks.append(clock[0])
            return 5320

        with patch.object(plugin, "_run_cmd", side_effect=run_cmd), \
             patch.object(plugin, "_get_wifi_interface", return_value="wlan0"), \
             patch.object(plugin, "_get_link_frequency", side_effect=frequency), \
             patch.object(plugin, "_profile_is_active", side_effect=is_active), \
             patch.object(wifi.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(wifi.time, "sleep", side_effect=sleep):
            result = plugin._reconnect_profile(UUID, cycle=False)

        self.assertTrue(result["success"], result)
        self.assertTrue(result["reconnected"])
        # Recovery at 13 s can precede a second retry with a safe slower
        # backoff. Bound churn and verify the live result, not an exact count.
        self.assertGreaterEqual(len(activations), 1)
        self.assertLessEqual(len(activations), 4)
        self.assertGreaterEqual(activations[0], 8.0)
        self.assertTrue(all(b - a >= 3.0 for a, b in zip(activations, activations[1:])), activations)
        self.assertTrue(all(at < 13.0 for at in activations), activations)
        self.assertGreater(len(checks), len(activations))
        self.assertGreaterEqual(checks[-1], 13.0)
        self.assertTrue(radio_checks)
        self.assertGreaterEqual(radio_checks[-1], 13.0)
        self.assertLessEqual(clock[0], 14.0, "native recovery must be observed promptly")

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
             patch.object(plugin, "_get_link_frequency", return_value=5320), \
             patch.object(plugin, "_profile_is_active",
                          side_effect=lambda *_: clock[0] >= 4.0), \
             patch.object(wifi.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(wifi.time, "sleep", side_effect=sleep):
            result = plugin._reconnect_profile(UUID, cycle=False)

        self.assertTrue(result["success"], result)
        self.assertEqual(activations, [])


class BandPolicyCapabilityReasonTests(unittest.TestCase):
    """Each blocker wins in a fixed order; pins all four reasons the UI shows."""
    R = {
        "connect": "Connect to WiFi first.",
        "target": "Band changes are enabled only on a Legion Go 2 with the "
                  "MediaTek MT7922 (mt7921e) driver.",
        "bssid": "Clear the fixed BSSID before changing band policy.",
        "channel": "Clear the fixed WiFi channel before changing band policy.",
        "symlink": "Refusing a symlinked /etc/iwd/main.conf.",
        "ambiguous": "The iwd config contains duplicate [Rank] sections or band keys.",
        "watchdog": "The automatic rollback watchdog is unavailable.",
        "iwd_nm": "iwd does not enforce NetworkManager's per-profile band setting. "
                  "Use the iwd 5/6 GHz policy instead.",
        "backend": "The WiFi backend cannot enforce a per-profile band setting.",
        "nm": "NetworkManager 1.58 or newer is required for 6 GHz only.",
        "no6": "No usable 6 GHz channels are exposed by the radio and regdomain.",
        "ext_band": "The active profile already has an external band setting.",
        "ext_iwd": "An external iwd band policy is already configured.",
        "vis6": "No 6 GHz BSS for the active SSID is currently visible.",
        "need_iwd": "The 5/6 GHz policy requires the iwd WiFi backend.",
        "iwd_missing": "iwd is not installed.",
        "iwd_inactive": "The iwd service is not active.",
        "no_high": "No usable 5 or 6 GHz channels are exposed by the radio.",
        "vis_high": "No 5 or 6 GHz BSS for the active SSID is currently visible.",
        "fresh": "No fresh 5 or 6 GHz scan result for the active SSID was verified.",
        "E5": "E5", "E6": "E6", "conflict": "conflict", "": "",
    }

    def reasons(self, backend="iwd", iface="wlan0", uuid=UUID, driver="mt7921e",
                profile=None, policy=wifi.BAND_POLICY_OFF, snapshot=None, matches=True,
                errors=None, files=None, iwd_version=True, iwd_state="active", nm6=True,
                freqs=([5320], [6295]), visible=None, conflict=""):
        plugin = wifi.Plugin()
        profile = profile or {}
        errors = errors or {}
        visible = dict({"two_ghz": 1, "five_ghz": 1, "six_ghz": 1, "fresh_high_band": True},
                       **(visible or {}))

        def command(argv, **kwargs):
            if argv[0] == "/usr/lib/iwd/iwd":
                return {"success": iwd_version, "stdout": ""}
            return {"success": True, "stdout": iwd_state + "\n"}

        with patch.object(plugin, "_get_wifi_interface", return_value=iface), \
             patch.object(plugin, "_get_active_connection_uuid", return_value=uuid), \
             patch.object(plugin, "_get_current_backend", return_value=backend), \
             patch.object(plugin, "_detect_device_family", return_value=("", "legion_go_2", "")), \
             patch.object(plugin, "_detect_wifi_driver", return_value=driver), \
             patch.object(plugin, "_nm_supports_six_ghz_only", return_value=nm6), \
             patch.object(plugin, "_run_cmd", side_effect=command), \
             patch.object(plugin, "_get_enabled_high_band_frequencies", return_value=freqs), \
             patch.object(plugin, "_get_visible_bands_for_active_ssid", return_value=visible), \
             patch.object(plugin, "_nmcli_get",
                          side_effect=lambda _u, key: (profile.get(key.rsplit(".", 1)[1], ""), {})), \
             patch.object(wifi, "_load_settings", return_value={"band_policy": policy}), \
             patch.object(plugin, "_get_iwd_rank_modifier_snapshot", return_value=snapshot or {}), \
             patch.object(plugin, "_iwd_band_modifier_error",
                          side_effect=lambda _s, band: errors.get(band, "")), \
             patch.object(plugin, "_iwd_snapshot_matches", return_value=matches), \
             patch.object(plugin, "_get_iwd_profile_conflict", return_value=conflict), \
             patch.object(wifi.os.path, "isfile", side_effect=lambda p: (files or {}).get(p, True)):
            cap = plugin._get_band_policy_capabilities_sync(False)
        keys = ("six_ghz_only", "five_six_no_24", "five_six_only", "five_six_only_try")
        for key in keys:
            self.assertIs(cap[key + "_available"], not cap["reason_" + key])
        return tuple(cap["reason_" + key] for key in keys)

    def test_reason_order(self):
        iwd_unit = "/usr/lib/systemd/system/iwd.service"
        cases = (
            ({}, ("iwd_nm", "", "", "")),
            ({"backend": "wpa_supplicant"}, ("", "need_iwd", "need_iwd", "need_iwd")),
            ({"backend": "other"}, ("backend", "need_iwd", "need_iwd", "need_iwd")),
            ({"iface": None}, ("connect",) * 4),
            ({"uuid": None}, ("connect",) * 4),
            ({"driver": "ath11k", "profile": {"bssid": "x"}}, ("target",) * 4),
            ({"profile": {"bssid": "x", "channel": "36"}}, ("bssid",) * 4),
            ({"profile": {"channel": "36"}}, ("channel",) * 4),
            ({"profile": {"channel": "0"}}, ("iwd_nm", "", "", "")),
            ({"snapshot": {"unsafe_symlink": True, "ambiguous": True}}, ("symlink",) * 4),
            ({"snapshot": {"ambiguous": True}}, ("ambiguous",) * 4),
            ({"files": {"/usr/bin/python3": False}}, ("watchdog",) * 4),
            ({"backend": "wpa_supplicant", "nm6": False, "freqs": ([5320], [])}, ("nm", "need_iwd", "need_iwd", "need_iwd")),
            ({"backend": "wpa_supplicant", "freqs": ([5320], []), "errors": {"six": "E6"}}, ("no6", "need_iwd", "need_iwd", "need_iwd")),
            ({"backend": "wpa_supplicant", "errors": {"six": "E6"}, "profile": {"band": "a"}}, ("E6", "need_iwd", "need_iwd", "need_iwd")),
            ({"backend": "wpa_supplicant", "profile": {"band": "a"}, "snapshot": {"modifier_present": True}}, ("ext_band", "need_iwd", "need_iwd", "need_iwd")),
            ({"backend": "wpa_supplicant", "policy": wifi.BAND_POLICY_HIGH_ONLY, "profile": {"band": "a"}}, ("ext_band", "need_iwd", "need_iwd", "need_iwd")),
            ({"backend": "wpa_supplicant", "policy": wifi.BAND_POLICY_SIX_ONLY, "profile": {"band": "a"}}, ("", "need_iwd", "need_iwd", "need_iwd")),
            ({"backend": "wpa_supplicant", "snapshot": {"modifier_present": True}, "visible": {"six_ghz": 0}}, ("ext_iwd", "need_iwd", "need_iwd", "need_iwd")),
            ({"backend": "wpa_supplicant", "visible": {"six_ghz": 0}}, ("vis6", "need_iwd", "need_iwd", "need_iwd")),
            ({"iwd_version": False, "files": {iwd_unit: False}, "iwd_state": "inactive"}, ("iwd_nm", "iwd_missing", "iwd_missing", "iwd_missing")),
            ({"iwd_version": False, "iwd_state": "inactive"}, ("iwd_nm", "iwd_inactive", "iwd_inactive", "iwd_inactive")),
            ({"freqs": ([], []), "errors": {"five": "E5"}}, ("iwd_nm", "no_high", "no_high", "no_high")),
            ({"errors": {"five": "E5", "six": "E6"}}, ("iwd_nm", "E5", "E5", "E5")),
            ({"errors": {"six": "E6"}, "profile": {"band": "a"}}, ("iwd_nm", "E6", "E6", "E6")),
            ({"profile": {"band": "a"}, "snapshot": {"modifier_present": True}}, ("iwd_nm", "ext_band", "ext_band", "ext_band")),
            ({"policy": wifi.BAND_POLICY_HIGH_ONLY, "profile": {"band": "a"}}, ("iwd_nm", "", "", "")),
            ({"snapshot": {"modifier_present": True}, "conflict": "conflict"}, ("iwd_nm", "ext_iwd", "ext_iwd", "ext_iwd")),
            ({"policy": wifi.BAND_POLICY_SIX_ONLY, "matches": False}, ("iwd_nm", "ext_iwd", "ext_iwd", "ext_iwd")),
            ({"policy": wifi.BAND_POLICY_HIGH_ONLY, "matches": False}, ("iwd_nm", "", "", "")),
            ({"conflict": "conflict", "visible": {"five_ghz": 0, "six_ghz": 0}}, ("iwd_nm", "conflict", "conflict", "conflict")),
            ({"visible": {"five_ghz": 0, "six_ghz": 0}}, ("iwd_nm", "vis_high", "vis_high", "")),
            ({"visible": {"five_ghz": 0}}, ("iwd_nm", "", "", "")),
            ({"visible": {"fresh_high_band": False}}, ("iwd_nm", "", "fresh", "")),
        )
        for kwargs, expected in cases:
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                self.assertEqual(self.reasons(**kwargs), tuple(self.R[key] for key in expected))


if __name__ == "__main__":
    unittest.main()

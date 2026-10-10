# SPDX-License-Identifier: BSD-3-Clause
"""Automatic iwd discovery after quick autoconnect, boot and resume."""
import asyncio
import builtins
from contextlib import ExitStack
import io
import json
import subprocess
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_integration
import wifi_backend as wifi
import test_wifi_strict as strict_tests

UUID = "dda82d97-6914-4788-802f-fa1ba9a1515f"
SETTINGS = {"device_family": "legion_go_2", "driver": "mt7921e",
            "band_policy": wifi.BAND_POLICY_HIGH_ONLY,
            "band_policy_state": {"mode": wifi.BAND_POLICY_HIGH_ONLY, "owns_iwd": True}}


def link(freq=2412, path=5):
    return {"iface": "wlan0", "uuid": UUID, "frequency": freq, "ssid": "NETIASPOT-V3fY",
            "episode": ("wlan0", 42, UUID, f"/org/freedesktop/NetworkManager/ActiveConnection/{path}")}


class NativeScanTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.plugin = wifi.Plugin()
        self.stop = threading.Event()
        self.clock = [0.0]
        self.commands = []
        self.scanning = ["b true", "b false"]
        self.scan_ok = True
        self.station_iface = "wlan0"
        self.links = [link(), link(), link(6295)]
        for name, value in {"_get_current_backend": "iwd", "_band_policy_ownership_error": "",
                            "_get_network_recovery_issues": []}.items():
            self.stack.enter_context(patch.object(self.plugin, name, return_value=value))
        self.stack.enter_context(patch.object(wifi, "_load_settings", return_value=SETTINGS))
        self.stack.enter_context(patch.object(wifi.time, "monotonic", side_effect=lambda: self.clock[0]))
        self.stack.enter_context(patch.object(self.stop, "wait", side_effect=self.wait))
        self.stack.enter_context(patch.object(self.plugin, "_get_high_band_link", side_effect=lambda *_args: self.links.pop(0)))
        self.stack.enter_context(patch.object(self.plugin, "_run_cmd", side_effect=self.command))
        self.stack.enter_context(patch.object(self.plugin, "_reselect_discovered_high_band",
                                              side_effect=lambda after, _stop: {
                                                  "success": True, "frequency": after["frequency"],
                                                  "reselected": False}))

    def wait(self, seconds):
        self.clock[0] += seconds
        return False

    def command(self, args, **kwargs):
        self.commands.append(args)
        self.assertLessEqual(kwargs["timeout"], 2)
        if "GetManagedObjects" in args:
            tree = {"/net/connman/iwd/phy0/42": {
                "net.connman.iwd.Device": {"Name": {"data": self.station_iface}},
                "net.connman.iwd.Station": {}}}
            return {"success": True, "stdout": json.dumps({"data": [tree]})}
        if args[-1] == "Scan":
            return {"success": self.scan_ok, "stderr": "Operation already in progress"}
        self.assertEqual(args[-1], "Scanning")
        state = self.scanning.pop(0) if len(self.scanning) > 1 else self.scanning[0]
        return {"success": True, "stdout": state}

    def test_stale_24_cache_is_refreshed_without_forcing_connection(self):
        result = self.plugin._discover_high_bands(link(), self.stop)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["frequency"], 6295)
        self.assertEqual(sum(args[-1] == "Scan" for args in self.commands), 1)
        self.assertTrue(all("busctl" in args[0] for args in self.commands))
        self.assertFalse(any("Connect" in args or "Disconnect" in args for args in self.commands))

    def test_24_only_ap_remains_a_valid_fallback(self):
        self.links[-1] = link()
        result = self.plugin._discover_high_bands(link(), self.stop)
        self.assertTrue(result["success"])
        self.assertEqual(result["frequency"], 2412)

    def test_scan_does_not_accept_another_adapter(self):
        self.station_iface = "wlan1"
        self.assertEqual(self.plugin._discover_high_bands(link(), self.stop)["error"], "station_unavailable")
        self.assertFalse(any(args[-1] == "Scan" for args in self.commands))

    def test_connection_change_before_scan_aborts(self):
        self.links[1] = link(path=6)
        self.assertEqual(self.plugin._discover_high_bands(link(), self.stop)["error"], "connection_changed")
        self.assertFalse(any(args[-1] == "Scan" for args in self.commands))

    def test_changed_connection_after_scan_is_not_reported_as_success(self):
        self.links[-1] = link(6295, path=6)
        self.assertEqual(self.plugin._discover_high_bands(link(), self.stop)["error"], "connection_changed")

    def test_existing_scan_is_joined_only_with_scanning_true(self):
        self.scan_ok = False
        self.assertTrue(self.plugin._discover_high_bands(link(), self.stop)["success"])

    def test_busy_station_without_a_scan_is_not_success(self):
        self.scan_ok = False
        self.scanning = ["b false"]
        self.assertEqual(self.plugin._discover_high_bands(link(), self.stop)["error"], "scan_failed")

    def test_never_finishing_scan_has_a_deadline(self):
        self.scanning = ["b true"]
        self.assertEqual(self.plugin._discover_high_bands(link(), self.stop)["error"], "scan_timeout")
        self.assertEqual(self.clock[0], wifi.HIGH_BAND_SCAN_SECONDS)

    def test_stop_interrupts_scan_polling(self):
        self.scanning = ["b true"]
        self.stop.wait.side_effect = lambda seconds: self.stop.set()
        self.assertEqual(self.plugin._discover_high_bands(link(), self.stop)["error"], "discovery_cancelled")

    def test_six_ghz_needs_no_discovery(self):
        self.links[0] = link(6295)
        result = self.plugin._discover_high_bands(link(), self.stop)
        self.assertTrue(result["success"])
        self.assertFalse(result["scanned"])
        self.assertEqual(self.commands, [])

    def test_external_ownership_change_prevents_scan(self):
        with patch.object(self.plugin, "_band_policy_ownership_error", return_value="Changed externally"):
            self.assertEqual(self.plugin._discover_high_bands(link(), self.stop)["error"], "discovery_unavailable")
        self.assertEqual(self.commands, [])


class LinkObservationTests(unittest.TestCase):
    def observe(self, fields, driver="mt7921e"):
        plugin = wifi.Plugin()
        results = [{"success": True, "stdout": "wlan0:wifi\neth0:ethernet"},
                   {"success": True, "stdout": fields},
                   {"success": True, "stdout": "Connected to aa:bb:cc:dd:ee:ff\n freq: 2412.0"}]
        with patch.object(plugin, "_run_cmd", side_effect=results), \
             patch.object(wifi.os.path, "realpath", return_value="/drivers/" + driver), \
             patch("builtins.open", return_value=io.StringIO("42\n")):
            return plugin._get_high_band_link()

    def test_connection_path_distinguishes_same_uuid_reactivation(self):
        fields = "100 (connected)\n" + UUID + "\n/org/freedesktop/NetworkManager/ActiveConnection/5"
        expected = link()
        expected.pop("ssid")
        self.assertEqual(self.observe(fields), expected)
        self.assertNotEqual(self.observe(fields)["episode"], self.observe(fields[:-1] + "6")["episode"])

    def test_activating_or_other_driver_is_not_a_live_target(self):
        fields = "100 (connected)\n" + UUID + "\n/org/freedesktop/NetworkManager/ActiveConnection/5"
        self.assertIsNone(self.observe(fields.replace("100 (connected)", "50 (connecting)")))
        self.assertIsNone(self.observe(fields, driver="iwlwifi"))


class ReselectionTests(unittest.TestCase):
    def setUp(self):
        strict_tests.StrictWifiTests.setUp(self)
        settings = wifi._load_settings()
        settings.update(device_family="legion_go_2", driver="mt7921e")
        self.stack.enter_context(patch.object(wifi, "_load_settings", return_value=settings))
        self.commands.return_value = {"success": True, "stdout": "NETIASPOT-V3fY"}
        self.scan = self.stack.enter_context(patch.object(self.plugin, "_run_discovery_scan", return_value={
            "success": True, "stdout": "BSS aa:29:48:e1:7e:03(on wlan0)\n freq: 6295.0\n SSID: NETIASPOT-V3fY\n"}))
        self.current = self.stack.enter_context(patch.object(self.plugin, "_get_high_band_link",
                                                           side_effect=[link(), link(6295, path=6)]))
        self.reconnect = self.plugin._reconnect_profile

    def test_verified_high_bss_uses_same_uuid_without_profile_writes(self):
        with patch.object(self.plugin, "_nmcli_modify") as modify:
            result = self.plugin._reselect_discovered_high_band(link(), threading.Event())
            self.assertTrue(result["reselected"], result)
            self.assertEqual(result["frequency"], 6295)
            self.assertEqual(result["_reselection_from_episode"], link()["episode"])
            self.reconnect.assert_called_once_with(UUID, cycle=True, activation_timeout=5, bound_iface="wlan0")
            modify.assert_not_called()
            self.assertFalse((self.journal_dir / "band-policy-transaction.json").exists())

    def test_only_24_or_another_ssid_never_disconnects(self):
        self.scan.return_value["stdout"] = "BSS aa:bb:cc:dd:ee:ff\n freq: 6295\n SSID: Another AP\n"
        result = self.plugin._reselect_discovered_high_band(link(), threading.Event())
        self.assertFalse(result["reselected"])
        self.reconnect.assert_not_called()

    def test_five_ghz_is_not_cycled_for_another_five_ghz_bss(self):
        self.scan.return_value["stdout"] = self.scan.return_value["stdout"].replace("6295.0", "5260.0")
        self.assertFalse(self.plugin._reselect_discovered_high_band(link(5260), threading.Event())["reselected"])
        self.reconnect.assert_not_called()

    def test_failed_watchdog_prevents_disconnection(self):
        self.plugin._schedule_band_policy_rollback.return_value = {"success": False}
        result = self.plugin._reselect_discovered_high_band(link(), threading.Event())
        self.assertEqual(result["error"], "rollback_watchdog_failed")
        self.reconnect.assert_not_called()

    def test_profile_band_or_cached_mirror_conflict_prevents_reselection(self):
        with patch.object(self.plugin, "_get_iwd_profile_conflict", return_value="Ambiguous"):
            self.assertFalse(self.plugin._reselect_discovered_high_band(link(), threading.Event())["reselected"])
        self.reconnect.assert_not_called()

    def test_explicit_profile_bssid_prevents_reselection(self):
        with patch.object(self.plugin, "_nmcli_get", return_value=("aa:bb:cc:dd:ee:ff", {"success": True})):
            self.assertFalse(self.plugin._reselect_discovered_high_band(link(), threading.Event())["reselected"])
        self.reconnect.assert_not_called()

    def test_user_reactivation_during_directed_scan_is_not_overridden(self):
        self.current.side_effect = None
        self.current.return_value = link(path=6)
        self.assertFalse(self.plugin._reselect_discovered_high_band(link(), threading.Event())["reselected"])
        self.reconnect.assert_not_called()

    def test_native_roam_during_scan_does_not_disconnect_an_already_best_link(self):
        for frequency, scan_frequency in ((6295, 6295), (5260, 5260)):
            with self.subTest(frequency=frequency):
                self.current.side_effect = None
                self.current.return_value = link(frequency)
                self.scan.return_value['stdout'] = (
                    'BSS aa:29:48:e1:7e:03(on wlan0)\n freq: ' + str(scan_frequency)
                    + '\n SSID: NETIASPOT-V3fY\n')
                result = self.plugin._reselect_discovered_high_band(link(), threading.Event())
                self.assertFalse(result['reselected'])
                self.assertEqual(result['frequency'], frequency)
                self.reconnect.assert_not_called()
                self.plugin._schedule_band_policy_rollback.assert_not_called()

    def test_reconnect_failure_recovers_without_leaving_a_profile_mutation(self):
        self.reconnect.return_value = {"success": False}
        self.current.side_effect = [link(), None]
        result = self.plugin._reselect_discovered_high_band(link(), threading.Event())
        self.assertEqual(result["error"], "automatic_reselection_failed")
        self.assertTrue(result["rolled_back"])
        self.assertFalse((self.journal_dir / "band-policy-transaction.json").exists())

    def test_failed_recovery_keeps_watchdog_and_retryable_journal(self):
        self.reconnect.return_value = {"success": False}
        self.current.side_effect = [link(), None]
        with patch.object(self.plugin, "_restore_band_policy_transaction", return_value={"success": False}):
            result = self.plugin._reselect_discovered_high_band(link(), threading.Event())
        self.assertFalse(result["rolled_back"])
        self.assertTrue((self.journal_dir / "band-policy-transaction.json").exists())
        self.plugin._cancel_band_policy_rollback.assert_not_called()

    def test_interrupted_connection_only_journal_is_recovered_at_startup(self):
        journal = {"schema": 1, "operation_id": "discovery-test", "phase": "rollback_failed",
                   "operation": "automatic_high_band_reconnect", "settings_before": {"band_policy": "off"},
                   "mutations": {}, "rollback_reconnect_required": True,
                   "rollback_reconnect_uuid": UUID, "profile_before": {"uuid": UUID}}
        self.plugin._save_band_policy_journal(journal)
        with patch.object(self.plugin, "_restore_band_policy_transaction", return_value={"success": True}) as restore:
            self.assertTrue(self.plugin._recover_band_policy_journal()["success"])
            restore.assert_called_once()

    def test_cancelled_directed_scan_does_not_start_a_transaction(self):
        stop = threading.Event()
        def scan(*_args):
            stop.set()
            return {"success": True, "stdout": ""}
        self.scan.side_effect = scan
        self.assertFalse(self.plugin._reselect_discovered_high_band(link(), stop)["reselected"])
        self.reconnect.assert_not_called()

    def test_changed_user_network_is_preserved_during_recovery(self):
        self.reconnect.return_value = {"success": False}
        self.current.side_effect = [link(), None]
        with patch.object(self.plugin, "_get_active_connection_uuid", return_value="a-different-user-profile"):
            result = self.plugin._reselect_discovered_high_band(link(), threading.Event())
        self.assertTrue(result["rolled_back"])
        self.assertEqual(self.reconnect.call_count, 1)

    def test_interrupted_disconnected_operation_activates_without_another_down(self):
        self.reconnect.side_effect = [{"success": False}, {"success": True}]
        self.current.side_effect = [link(), None]
        original_open = builtins.open
        def opened(path, *args, **kwargs):
            if str(path) == "/sys/class/net/wlan0/ifindex":
                return io.StringIO("42")
            return original_open(path, *args, **kwargs)
        with patch.object(self.plugin, "_get_active_connection_uuid", return_value=None), \
             patch.object(self.plugin, "_profile_is_active", return_value=False), \
             patch.object(self.plugin, "_boot_id", return_value="test-boot"), \
             patch.object(wifi.os.path, "realpath", return_value="/drivers/mt7921e"), \
             patch("builtins.open", side_effect=opened):
            self.assertTrue(self.plugin._reselect_discovered_high_band(link(), threading.Event())["rolled_back"])
        self.assertEqual(self.reconnect.call_count, 2)
        self.assertEqual(self.reconnect.call_args.kwargs, {
            "cycle": False, "bound_iface": "wlan0", "activation_timeout": 5})

    def test_directed_ssid_spaces_are_preserved(self):
        before = link()
        before["ssid"] = " target with spaces "
        self.scan.return_value["stdout"] = "BSS aa:bb:cc:dd:ee:ff\n freq: 6295\n SSID:  target with spaces \n"
        self.plugin._reselect_discovered_high_band(before, threading.Event())
        self.reconnect.assert_called_once()
        self.assertEqual(self.scan.call_args.args[1], before["ssid"])


class IndependentWatchdogTests(unittest.TestCase):
    setUp = strict_tests.StrictWifiTests.setUp

    def journal(self):
        return {"schema": 1, "phase": "rollback_failed",
                "operation": "automatic_high_band_reconnect", "mutations": {},
                "settings_file": str(self.settings), "settings_before": {"band_policy": "off"},
                "rollback_reconnect_required": True, "rollback_reconnect_uuid": UUID,
                "profile_before": {"uuid": UUID}}

    def run_cli(self, journal, success=True):
        self.plugin._save_band_policy_journal(journal)
        path = str(self.journal_dir / "band-policy-transaction.json")
        with patch.object(wifi, "BAND_POLICY_LOCK_FILE", str(self.runtime_dir / "band-policy.lock")), \
             patch.object(wifi, "Plugin", return_value=self.plugin), \
             patch.object(self.plugin, "_restore_band_policy_transaction",
                          return_value={"success": success}) as restore:
            result = wifi._run_band_policy_rollback_cli(path)
        return result, restore, path

    def test_watchdog_retries_failed_connection_only_and_policy_rollbacks(self):
        for mutations in ({}, {"iwd": {"planned": True}}):
            with self.subTest(mutations=mutations):
                journal = self.journal()
                journal["mutations"] = mutations
                result, restore, path = self.run_cli(journal)
                self.assertEqual(result, 0)
                restore.assert_called_once_with(journal, "automatic watchdog timeout", journal_path=path)

    def test_watchdog_keeps_failed_recovery_for_later_startup(self):
        result, restore, _path = self.run_cli(self.journal(), success=False)
        self.assertEqual(result, 1)
        restore.assert_called_once()
        self.assertEqual(self.plugin._load_band_policy_journal()["phase"], "rollback_failed")

    def test_unrecognized_failed_journal_never_triggers_a_reconnect(self):
        for profile in ("malformed", {}, {"uuid": "other"}):
            with self.subTest(profile=profile):
                journal = self.journal()
                journal["profile_before"] = profile
                result, restore, _path = self.run_cli(journal)
                self.assertEqual(result, 1)
                restore.assert_not_called()

    def test_a_committed_policy_is_preserved_when_timer_races_commit(self):
        journal = self.journal()
        journal["phase"] = "committed"
        result, restore, _path = self.run_cli(journal)
        self.assertEqual(result, 0)
        restore.assert_not_called()

    def test_removed_journal_is_a_completed_timer_race(self):
        with patch.object(wifi, "Plugin") as constructor:
            result = wifi._run_band_policy_rollback_cli(
                str(self.journal_dir / "band-policy-transaction.json"))
        self.assertEqual(result, 0)
        constructor.assert_not_called()


class InterruptibleScanTests(unittest.TestCase):
    def test_stop_terminates_only_our_scan_child(self):
        stop = threading.Event()
        child = Mock()
        child.poll.return_value = None
        def communicate(**_kwargs):
            if not stop.is_set():
                stop.set()
                raise subprocess.TimeoutExpired("iw", 0.2)
            return "", ""
        child.communicate.side_effect = communicate
        with patch.object(wifi.subprocess, "Popen", return_value=child) as popen:
            self.assertFalse(wifi.Plugin()._run_discovery_scan("wlan0", " target ", stop)["success"])
            self.assertEqual(popen.call_args.args[0][-1], " target ")
            child.terminate.assert_called_once()
            child.kill.assert_not_called()

    def test_missing_scan_tool_does_not_kill_monitor(self):
        with patch.object(wifi.subprocess, "Popen", side_effect=FileNotFoundError):
            self.assertFalse(wifi.Plugin()._run_discovery_scan("wlan0", "ssid", threading.Event())["success"])


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These cases exercise active iwd discovery; a native WPA selection is
        # covered separately and must never probe/reconnect the radio.
        backend = patch.object(wifi.Plugin, "_get_current_backend", return_value="iwd")
        backend.start()
        self.addCleanup(backend.stop)

    async def test_native_wpa_pauses_radio_work_and_native_iwd_return_resumes_it(self):
        plugin = wifi.Plugin()
        stop = threading.Event()
        sleeps = [0]
        async def sleep(_seconds):
            sleeps[0] += 1
            if sleeps[0] == 1:
                radio.assert_not_called()
                scan.assert_not_called()
                backend.return_value = "iwd"
            else:
                stop.set()
        with patch.object(wifi, "_load_settings", return_value=SETTINGS), \
             patch.object(plugin, "_get_current_backend", return_value="wpa_supplicant") as backend, \
             patch.object(plugin, "_get_high_band_link", return_value=link()) as radio, \
             patch.object(plugin, "_discover_high_bands", return_value={"success": True, "frequency": 2412}) as scan, \
             patch.object(plugin, "_acquire_band_policy_file_lock", return_value=object()), \
             patch.object(plugin, "_release_band_policy_file_lock"), \
             patch.object(wifi.asyncio, "sleep", side_effect=sleep):
            await plugin._high_band_monitor(stop)
        self.assertEqual(radio.call_count, 1)
        self.assertEqual(scan.call_count, 1)

    async def monitor(self, observations, offsets=None, results=None):
        plugin = wifi.Plugin()
        stop = threading.Event()
        clock = [0.0]
        index = [0]
        async def worker(fn, *args, **_kwargs):
            return fn(*args)
        async def sleep(seconds):
            clock[0] += seconds
            index[0] += 1
            if index[0] >= len(observations):
                stop.set()
        with patch.object(wifi, "_load_settings", return_value=SETTINGS), \
             patch.object(wifi.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(wifi.asyncio, "sleep", side_effect=sleep), \
             patch.object(plugin, "_worker_complete", side_effect=worker), \
             patch.object(plugin, "_get_high_band_link", side_effect=observations), \
             patch.object(plugin, "_acquire_band_policy_file_lock", return_value=object()), \
             patch.object(plugin, "_release_band_policy_file_lock"), \
             patch.object(plugin, "_suspend_offset", side_effect=offsets or [0.0] * (len(observations) + 1)), \
             patch.object(plugin, "_discover_high_bands", side_effect=results or (
                 lambda *_args: {"success": True, "frequency": 2412})) as scan:
            await plugin._high_band_monitor(stop)
            return scan.call_count

    async def test_24_fallback_gets_two_bounded_probes_not_endless_scanning(self):
        self.assertEqual(await self.monitor([link()] * 30), 2)

    async def test_early_boot_miss_then_five_ghz_still_allows_six_discovery(self):
        observations = [link()] * 7 + [link(5260, path=6)] * 7 + [link(6295, path=7)] * 10
        results = [
            {"success": True, "frequency": 2412, "reselected": False},
            {"success": True, "frequency": 5260, "reselected": True,
             "_reselection_from_episode": link()["episode"]},
            {"success": True, "frequency": 6295, "reselected": True,
             "_reselection_from_episode": link(5260, path=6)["episode"]},
        ]
        self.assertEqual(await self.monitor(observations, results=results), 3)

    async def test_five_ghz_promotion_never_permits_a_third_reconnect(self):
        observations = [link()] + [link(5260, path=6)] * 7 + [link(5260, path=7)] * 30
        def own(observed, *_args):
            return {"success": True, "frequency": 5260, "reselected": True,
                    "_reselection_from_episode": observed["episode"]}
        self.assertEqual(await self.monitor(observations, results=own), 2)

    async def test_reactivation_same_uuid_and_interface_gets_new_discovery(self):
        self.assertEqual(await self.monitor([link(), link(), link(path=6)]), 2)

    async def test_suspend_resume_resets_discovery_budget(self):
        self.assertEqual(await self.monitor([link(), link()], [0.0, 0.0, 8.0]), 2)

    async def test_boot_on_five_ghz_still_discovers_six(self):
        self.assertEqual(await self.monitor([link(5260)]), 1)

    async def test_already_six_ghz_does_not_scan(self):
        self.assertEqual(await self.monitor([link(6295)] * 5), 0)

    async def test_disconnect_and_lower_band_after_six_reset_budget(self):
        self.assertEqual(await self.monitor([link(), None, link(), link(6295), link()]), 3)

    async def test_own_reactivation_paths_do_not_reset_attempt_budget(self):
        observations = [link(path=5)] + [link(path=6)] * 7 + [link(path=7)] * 30
        own = lambda observed, *_args: {"success": True, "frequency": 2412, "_reselection_from_episode": observed["episode"]}
        self.assertEqual(await self.monitor(observations, results=own), 2)

    async def test_own_failed_reselection_and_recovery_share_budget(self):
        observations = [link()] + [None] * 4 + [link(path=6)] * 5 + [link(path=7)] * 30
        own = lambda observed, *_args: {"success": False, "_reselection_from_episode": observed["episode"]}
        self.assertEqual(await self.monitor(observations, results=own), 2)

    async def test_off_and_ordinary_steamos_do_not_start_monitor(self):
        plugin = wifi.Plugin()
        plugin._high_band_monitor_ready = True
        for changes in ({"band_policy": "off"}, {"device_family": "deck_oled"}, {"driver": "iwlwifi"}):
            plugin._start_high_band_monitor(dict(SETTINGS, **changes))
            self.assertIsNone(getattr(plugin, "_high_band_monitor_task", None))

    async def test_unload_interrupts_worker_and_waits_for_completion(self):
        plugin = wifi.Plugin()
        entered = threading.Event()
        finished = threading.Event()
        def scan(_link, stop):
            entered.set()
            stop.wait(5)
            finished.set()
            return {"success": False}
        with patch.object(wifi, "_load_settings", return_value=SETTINGS), \
             patch.object(plugin, "_get_high_band_link", return_value=link()), \
             patch.object(plugin, "_discover_high_bands", side_effect=scan), \
             patch.object(plugin, "_acquire_band_policy_file_lock", return_value=object()), \
             patch.object(plugin, "_release_band_policy_file_lock"), \
             patch.object(wifi.os, "remove"):
            plugin._high_band_monitor_ready = True
            plugin._start_high_band_monitor(SETTINGS)
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await asyncio.wait_for(plugin._unload(), 1)
            self.assertTrue(finished.is_set())
            self.assertFalse(plugin._high_band_monitor_ready)
            self.assertIsNone(plugin._high_band_monitor_task)

    async def test_main_starts_discovery_without_frontend_call(self):
        plugin = wifi.Plugin()
        with ExitStack() as stack:
            for name, value in {"_rotate_logs": None, "_acquire_band_policy_file_lock": object(),
                                "_release_band_policy_file_lock": None,
                                "_recover_power_save_journal": {"success": True},
                                "_recover_band_policy_journal": {"success": True},
                                "_detect_distro": {"id": "steamos", "name": "SteamOS"},
                                "_get_wifi_interface": None, "_get_current_backend": None,
                                "_atomic_write_json": None}.items():
                stack.enter_context(patch.object(plugin, name, return_value=value))
            stack.enter_context(patch.object(plugin, "get_device_info", new=AsyncMock(return_value=SETTINGS)))
            stack.enter_context(patch.object(wifi, "_load_settings", return_value=dict(SETTINGS)))
            stack.enter_context(patch.object(wifi, "_save_settings"))
            stack.enter_context(patch.object(wifi.os, "remove"))
            start = stack.enter_context(patch.object(plugin, "_start_high_band_monitor"))
            self.assertTrue((await plugin._main())["success"])
            start.assert_called_once()
            self.assertTrue(plugin._high_band_monitor_ready)

    async def test_apply_starts_monitor_on_persistent_loop_not_worker_loop(self):
        plugin = wifi.Plugin()
        plugin._high_band_monitor_ready = True
        entered = asyncio.Event()
        loops = []
        async def monitor(_stop):
            loops.append(asyncio.get_running_loop())
            entered.set()
            await asyncio.Event().wait()
        with patch.object(plugin, "_acquire_band_policy_file_lock", return_value=object()), \
             patch.object(plugin, "_release_band_policy_file_lock"), \
             patch.object(plugin, "_set_band_policy_impl", new=AsyncMock(return_value={"success": True})), \
             patch.object(wifi, "_load_settings", return_value=SETTINGS), \
             patch.object(plugin, "_high_band_monitor", side_effect=monitor):
            self.assertTrue((await plugin.set_band_policy(wifi.BAND_POLICY_HIGH_ONLY))["success"])
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(loops, [asyncio.get_running_loop()])
            await plugin._stop_high_band_monitor()

    async def test_off_cancels_scan_before_waiting_for_network_lock(self):
        plugin = wifi.Plugin()
        entered = threading.Event()
        finished = threading.Event()
        def scan(_link, stop):
            entered.set()
            stop.wait(5)
            finished.set()
            return {"success": False}
        async def transaction(*_args, **_kwargs):
            self.assertTrue(finished.is_set())
            return {"success": True}
        with patch.object(wifi, "_load_settings", return_value=SETTINGS) as settings, \
             patch.object(plugin, "_get_high_band_link", return_value=link()), \
             patch.object(plugin, "_discover_high_bands", side_effect=scan), \
             patch.object(plugin, "_acquire_band_policy_file_lock", return_value=object()), \
             patch.object(plugin, "_release_band_policy_file_lock"), \
             patch.object(plugin, "_set_band_policy_impl", side_effect=transaction):
            plugin._high_band_monitor_ready = True
            plugin._start_high_band_monitor(SETTINGS)
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            settings.return_value = dict(SETTINGS, band_policy="off")
            self.assertTrue((await asyncio.wait_for(plugin.set_band_policy("off"), 1))["success"])
            self.assertIsNone(plugin._high_band_monitor_task)

    async def test_monitor_waiting_filegate_is_cancelled_before_off_waits(self):
        plugin = wifi.Plugin()
        entered = threading.Event()
        gate = plugin._get_band_policy_file_gate()
        await gate.acquire()
        def observed(*_args):
            entered.set()
            return link()
        with patch.object(wifi, "_load_settings", return_value=SETTINGS) as settings, \
             patch.object(plugin, "_get_high_band_link", side_effect=observed), \
             patch.object(plugin, "_acquire_band_policy_file_lock", return_value=object()), \
             patch.object(plugin, "_release_band_policy_file_lock"), \
             patch.object(plugin, "_set_band_policy_impl", new=AsyncMock(return_value={"success": True})):
            plugin._high_band_monitor_ready = True
            plugin._start_high_band_monitor(SETTINGS)
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            settings.return_value = dict(SETTINGS, band_policy="off")
            off = asyncio.create_task(plugin.set_band_policy("off"))
            await asyncio.sleep(0.05)
            self.assertIsNone(plugin._high_band_monitor_task)
            gate.release()
            self.assertTrue((await asyncio.wait_for(off, 1))["success"])


if __name__ == "__main__":
    unittest.main()

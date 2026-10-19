# SPDX-License-Identifier: BSD-3-Clause
"""Console regressions: NM UUID selection and iwd band enforcement."""
import asyncio
import json
import unittest
from unittest.mock import patch

import support
import wifi_backend as wifi
import test_wifi_strict as strict_tests

UUID = strict_tests.UUID


class ProfileSelectionTests(unittest.TestCase):
    def execute(self, succeeds, frequency=6295):
        plugin = wifi.Plugin()
        clock = [0.0]
        commands = []
        activated = [False]

        def command(argv, **kwargs):
            commands.append(argv)
            if argv[:3] == ["/usr/bin/nmcli", "con", "up"]:
                self.assertEqual(argv[3:], ["uuid", UUID, "ifname", "wlan0"])
                activated[0] = succeeds
                return {"success": succeeds, "stderr": "Requested SAE profile unavailable"}
            return {"success": True, "stdout": "100 (connected)\n"}

        def active(uuid, *_):
            self.assertEqual(uuid, UUID)
            return activated[0]

        with patch.object(plugin, "_run_cmd", side_effect=command), \
             patch.object(plugin, "_get_wifi_interface", return_value="wlan0"), \
             patch.object(plugin, "_get_link_frequency", return_value=frequency), \
             patch.object(plugin, "_profile_is_active", side_effect=active), \
             patch.object(wifi.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(wifi.time, "sleep", side_effect=lambda n: clock.__setitem__(0, clock[0] + n)):
            result = plugin._reconnect_profile(UUID, cycle=False)
        return result, commands

    def test_other_same_ssid_profile_is_not_accepted_as_target(self):
        result, commands = self.execute(True)
        self.assertTrue(result["success"], result)
        self.assertTrue(any("up" in argv for argv in commands))
        self.assertFalse(any("busctl" in argv[0] for argv in commands))
        self.assertFalse(any("modify" in argv for argv in commands))

    def test_wrong_profile_and_failed_activation_fail_closed(self):
        result, _ = self.execute(False)
        self.assertFalse(result["success"])
        self.assertIn("SAE", result["stderr"])

    def test_nm_activation_without_radio_link_is_not_a_success(self):
        result, commands = self.execute(True, frequency=None)
        self.assertFalse(result["success"], result)
        self.assertEqual(sum("up" in args for args in commands), 1)


class IwdNativeFallbackTests(unittest.TestCase):
    """NM's iwd backend can refuse every activation of a profile; iwd itself still joins."""
    STATION = "/net/connman/iwd/0/9"
    NETWORK = STATION + "/4e45_psk"

    def tree(self, known=True, duplicate=False):
        objects = {self.STATION: {"net.connman.iwd.Device": {"Name": {"data": "wlan0"}},
                                  "net.connman.iwd.Station": {}}}
        network = {"Name": {"data": "Home"}, "Device": {"data": self.STATION}}
        if known:
            network["KnownNetwork"] = {"data": "/net/connman/iwd/4e45_psk"}
        objects[self.NETWORK] = {"net.connman.iwd.Network": network}
        if duplicate:
            objects[self.NETWORK + "x"] = {"net.connman.iwd.Network": dict(network)}
        return json.dumps({"type": "a{oa{sa{sv}}}", "data": [objects]})

    def execute(self, bssid="", **tree):
        plugin = wifi.Plugin()
        clock, commands, activated = [0.0], [], [False]

        def command(argv, **kwargs):
            commands.append(argv)
            if argv[:3] == ["/usr/bin/nmcli", "con", "up"]:
                return {"success": False, "stderr": "Connection activation failed: No reason given"}
            if "GetManagedObjects" in argv:
                return {"success": True, "stdout": self.tree(**tree)}
            if "802-11-wireless.bssid" in argv:
                return {"success": True, "stdout": bssid}
            if argv[-1] == "Connect":
                activated[0] = True
                return {"success": True, "stdout": ""}
            return {"success": True, "stdout": "30 (disconnected)\n"}

        with patch.object(plugin, "_run_cmd", side_effect=command), \
             patch.object(plugin, "_get_current_backend", return_value="iwd"), \
             patch.object(plugin, "_get_iwd_profile_conflict", return_value=""), \
             patch.object(plugin, "_get_profile_ssid", return_value="Home"), \
             patch.object(plugin, "_get_wifi_interface", return_value="wlan0"), \
             patch.object(plugin, "_get_link_frequency", return_value=5260), \
             patch.object(plugin, "_profile_is_active", side_effect=lambda *_: activated[0]), \
             patch.object(wifi.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(wifi.time, "sleep", side_effect=lambda n: clock.__setitem__(0, clock[0] + n)):
            result = plugin._reconnect_profile(UUID, cycle=True)
        return result, commands

    def test_refused_nm_activation_falls_back_to_iwd_once(self):
        result, commands = self.execute()
        self.assertTrue(result["success"], result)
        connects = [argv for argv in commands if argv[-1] == "Connect"]
        self.assertEqual(len(connects), 1)
        self.assertIn(self.NETWORK, connects[0])
        first_up = next(i for i, argv in enumerate(commands) if argv[:3] == ["/usr/bin/nmcli", "con", "up"])
        self.assertGreater(commands.index(connects[0]), first_up)  # NetworkManager is always tried first.

    def test_a_pinned_bssid_is_never_handed_to_iwd(self):
        result, commands = self.execute(bssid="A8:29:48:E1:7E:01")
        self.assertFalse(result["success"])
        self.assertFalse(any(argv[-1] == "Connect" for argv in commands))

    def test_no_unique_known_network_never_calls_iwd_connect(self):
        for tree in ({"known": False}, {"duplicate": True}):
            with self.subTest(**tree):
                result, commands = self.execute(**tree)
                self.assertFalse(result["success"])
                self.assertFalse(any(argv[-1] == "Connect" for argv in commands))


class BandEnforcementTests(unittest.TestCase):
    setUp = strict_tests.StrictWifiTests.setUp
    change = strict_tests.StrictWifiTests.change

    def capabilities(self, backend):
        def command(argv, **kwargs):
            if argv[:2] == ["/usr/bin/iw", "list"]:
                return {"success": True, "stdout": "* 5320 MHz [64]\n* 6295 MHz [69]\n"}
            return {"success": True, "stdout": "1.58.0\n"}

        visible = {"two_ghz": 1, "five_ghz": 1, "six_ghz": 1,
                   "scan_ok": True, "fresh_high_band": True}
        with patch.object(self.plugin, "_get_current_backend", return_value=backend), \
             patch.object(self.plugin, "_get_band_policy_capabilities_sync",
                          wraps=wifi.Plugin._get_band_policy_capabilities_sync.__get__(self.plugin)), \
             patch.object(self.plugin, "_get_wifi_interface", return_value="wlan0"), \
             patch.object(self.plugin, "_get_visible_bands_for_active_ssid", return_value=visible), \
             patch.object(wifi.os.path, "isfile", return_value=True):
            self.commands.side_effect = command
            return self.plugin._get_band_policy_capabilities_sync(False)

    def test_iwd_does_not_advertise_an_unenforced_nm_six_band(self):
        cap = self.capabilities("iwd")
        self.assertFalse(cap["six_ghz_only_available"])
        self.assertIn("does not enforce", cap["reason_six_ghz_only"])

    def test_wpa_can_advertise_six_band_without_active_iwd(self):
        cap = self.capabilities("wpa_supplicant")
        self.assertTrue(cap["six_ghz_only_available"], cap)
        self.assertFalse(cap["five_six_no_24_available"])

    def test_manual_band_is_rejected_before_profile_or_iwd_mutations(self):
        with patch.object(self.plugin, "_nmcli_get", return_value=("a", {"success": True})), \
             patch.object(self.plugin, "_nmcli_modify") as modify, \
             patch.object(self.plugin, "_set_iwd_rank_modifier") as write:
            result = self.change(wifi.BAND_POLICY_STRICT_HIGH)
            self.assertFalse(result["success"], result)
            modify.assert_not_called()
            write.assert_not_called()

    def test_off_and_preference_require_a_radio_link_for_active_uuid(self):
        for mode in (wifi.BAND_POLICY_OFF, wifi.BAND_POLICY_HIGH_ONLY):
            with self.subTest(mode=mode), \
                 patch.object(self.plugin, "_get_link_frequency", return_value=None):
                result = self.plugin._verify_band_policy(mode, UUID, timeout=0.01)
            self.assertFalse(result["success"], result)

    def test_verification_never_sleeps_past_its_deadline(self):
        sleeps = []
        with patch.object(self.plugin, "_get_link_frequency", return_value=None), \
             patch.object(wifi.time, "sleep", side_effect=sleeps.append):
            self.plugin._verify_band_policy(wifi.BAND_POLICY_OFF, UUID, timeout=0.01)
        self.assertTrue(sleeps and max(sleeps) < 0.5, sleeps)  # not the full 1 s step

    def test_preference_allows_a_connected_2_4_ghz_fallback(self):
        with patch.object(self.plugin, "_get_link_frequency", return_value=2412):
            result = self.plugin._verify_band_policy(wifi.BAND_POLICY_HIGH_ONLY, UUID, timeout=0.01)
        self.assertTrue(result["success"], result)

    def test_ambiguity_blocks_an_iwd_restart_without_touching_recovery(self):
        with patch.object(self.plugin, "_get_iwd_profile_conflict", return_value="Ambiguous profiles"), \
             patch.object(self.plugin, "_schedule_band_policy_rollback") as schedule, \
             patch.object(self.plugin, "_set_iwd_rank_modifier") as write:
            result = self.change(wifi.BAND_POLICY_STRICT_HIGH)
        self.assertFalse(result["success"], result)
        self.assertEqual(result["error"], "ambiguous_iwd_profiles")
        schedule.assert_not_called()
        write.assert_not_called()
        self.assertIn("BandModifier2_4GHz=0.01", self.iwd.read_text(encoding="utf-8"))
        self.assertFalse((self.journal_dir / "band-policy-transaction.json").exists())

    def test_off_and_uninstall_restore_iwd_for_any_saved_profile_count(self):
        # 33 saved profiles exceed the verification bound while connected.
        saved = "".join(f"{UUID[:-2]}{i:02x}:802-11-wireless\n" for i in range(32))
        saved += f"{UUID}:802-11-wireless\n"

        def command(argv, **kwargs):
            if argv[-3:] == ["UUID,TYPE", "con", "show"]:
                return {"success": True, "stdout": saved}
            return {"success": True, "stdout": "active\n"}

        self.commands.side_effect = command
        conflict = wifi.Plugin._get_iwd_profile_conflict.__get__(self.plugin)
        self.assertTrue(conflict(UUID, allow_disabled_duplicates=True))
        # _uninstall and module withdrawal both release through set_band_policy(OFF).
        with patch.object(self.plugin, "_get_iwd_profile_conflict", side_effect=conflict), \
             patch.object(self.plugin, "set_band_policy", side_effect=self.plugin._set_band_policy_impl):
            asyncio.run(self.plugin._uninstall())
        self.assertFalse(self.iwd.exists(), "the pre-plugin iwd baseline is restored")
        self.assertFalse(self.settings.exists(), "uninstall confirmed the restore")
        self.assertFalse((self.journal_dir / "band-policy-transaction.json").exists())


class ProfileInventoryTests(unittest.TestCase):
    OTHER = "ba58df8b-cdb1-447d-80ab-19d11a634e7e"

    def inventory(self, entries, *, listing=None, body=None, success=True, preferences=None,
                  allow_disabled_duplicates=True):
        plugin = wifi.Plugin()
        entries = [(*entry, True, False) if len(entry) == 3 else entry for entry in entries]
        if listing is None:
            listing = "\n".join(entry[0] + ":802-11-wireless" for entry in entries)
        if body is None:
            # `nmcli -t -f` output: "key:value" lines, blank line between profiles,
            # a profile without security (open network) has no key-mgmt line.
            body = "\n\n".join("\n".join(filter(None, (
                f"connection.uuid:{uid}", "connection.type:802-11-wireless",
                f"connection.autoconnect:{'yes' if auto else 'no'}",
                f"802-11-wireless.ssid:{ssid}", f"802-11-wireless.hidden:{'yes' if hidden else 'no'}",
                f"802-11-wireless-security.key-mgmt:{key}" if key else "")))
                for uid, ssid, key, auto, hidden in entries) + "\n"
        with patch.object(plugin, "_get_iwd_known_network_preferences", return_value=preferences), \
             patch.object(plugin, "_run_cmd", side_effect=[
            {"success": True, "stdout": listing},
            {"success": success, "stdout": body},
        ]) as run:
            result = plugin._get_iwd_profile_conflict(UUID, allow_disabled_duplicates=allow_disabled_duplicates)
        self.assertFalse(any("--show-secrets" in call.args[0] for call in run.call_args_list))
        self.assertLessEqual(run.call_count, 2)
        return result

    def test_single_sae_or_psk_profile_is_unchanged(self):
        for key in ("sae", "wpa-psk"):
            self.assertEqual(self.inventory([(UUID, "Target", key)]), "")

    def test_open_profile_in_any_position_does_not_block_verification(self):
        open_entry = (self.OTHER, "Cafe", "")
        for entries in ([(UUID, "Home", "sae"), open_entry], [open_entry, (UUID, "Home", "sae")]):
            self.assertEqual(self.inventory(entries), "")
        self.assertEqual(self.inventory([(UUID, "Cafe", ""), (self.OTHER, "Home", "sae")]), "")

    def test_other_ssid_or_different_security_family_does_not_collide(self):
        for ssid, key in (("Other", "wpa-psk"), ("Target", "wpa-eap")):
            self.assertEqual(self.inventory([(UUID, "Target", "sae"),
                                             (self.OTHER, ssid, key)]), "")

    def test_same_ssid_sae_and_psk_collide_regardless_of_order(self):
        entries = [(UUID, "Target", "sae"), (self.OTHER, "Target", "wpa-psk")]
        for value in (entries, entries[::-1]):
            self.assertIn("Multiple", self.inventory(value))

    def test_escaped_ssid_is_compared_without_splitting_colons(self):
        entries = [(UUID, r"Target\:network", "sae"),
                   (self.OTHER, r"Target\:network", "sae")]
        self.assertIn("Multiple", self.inventory(entries))

    def test_disabled_duplicate_allows_only_the_unique_exact_mirror(self):
        entries = [(UUID, "Target", "wpa-psk", True, False),
                   (self.OTHER, "Target", "sae", False, False)]
        for value in (entries, entries[::-1]):
            self.assertEqual(self.inventory(value, preferences=(True, False)), "")
        for preferences in (None, (False, False), (True, True)):
            self.assertTrue(self.inventory(entries, preferences=preferences))

    def test_two_matching_autoconnect_profiles_remain_ambiguous(self):
        entries = [(UUID, "Target", "wpa-psk"), (self.OTHER, "Target", "sae")]
        self.assertTrue(self.inventory(entries, preferences=(True, False)))

    def test_profile_only_mutations_cannot_assume_a_rebuilt_mirror_binding(self):
        entries = [(UUID, "Target", "wpa-psk", True, False),
                   (self.OTHER, "Target", "sae", False, False)]
        self.assertTrue(self.inventory(entries, preferences=(True, False),
                                       allow_disabled_duplicates=False))

    def test_hidden_setting_is_part_of_the_exact_mirror_match(self):
        entries = [(UUID, "Target", "wpa-psk", True, False),
                   (self.OTHER, "Target", "sae", True, True)]
        self.assertEqual(self.inventory(entries, preferences=(True, False)), "")
        self.assertTrue(self.inventory(entries, preferences=(True, True)))

    def test_disabled_target_cannot_use_another_profiles_mirror(self):
        entries = [(UUID, "Target", "wpa-psk", False, False),
                   (self.OTHER, "Target", "sae", True, False)]
        self.assertTrue(self.inventory(entries, preferences=(True, False)))

    def test_invalid_or_incomplete_metadata_fail_closed(self):
        entries = [(UUID, "Target", "sae")]
        for body in ("", UUID + "\n802-11-wireless\nTarget\n", "unparseable",
                     "\n".join((self.OTHER, "802-11-wireless", "Target", "sae")),
                     "\n\n".join(["\n".join((UUID, "802-11-wireless", "Target", "sae"))] * 2)):
            with self.subTest(body=body):
                self.assertTrue(self.inventory(entries, body=body))
        self.assertTrue(self.inventory(entries, success=False))

    def test_oversized_inventory_is_bounded_before_metadata_read(self):
        entries = [(UUID, "Target", "sae")]
        listing = "\n".join(f"{n:08x}-0000-0000-0000-000000000000:802-11-wireless"
                            for n in range(33)) + "\n" + UUID + ":802-11-wireless"
        self.assertTrue(self.inventory(entries, listing=listing))


class PublicIwdMetadataTests(unittest.TestCase):
    def preferences(self, known, success=True):
        plugin = wifi.Plugin()
        body = json.dumps({"data": [{"/network": {"net.connman.iwd.KnownNetwork": known}}]})
        with patch.object(plugin, "_run_cmd", return_value={"success": success, "stdout": body}) as run:
            value = plugin._get_iwd_known_network_preferences("Target")
        self.assertIn("GetManagedObjects", run.call_args.args[0])
        self.assertFalse(any("secret" in arg.lower() for arg in run.call_args.args[0]))
        return value

    def test_public_auto_and_optional_hidden_are_read_without_credentials(self):
        known = {"Name": {"data": "Target"}, "Type": {"data": "psk"},
                 "AutoConnect": {"data": True}}
        self.assertEqual(self.preferences(known), (True, False))
        known["Hidden"] = {"data": True}
        self.assertEqual(self.preferences(known), (True, True))

    def test_unknown_malformed_or_different_family_fails_closed(self):
        base = {"Name": {"data": "Target"}, "Type": {"data": "psk"},
                "AutoConnect": {"data": True}}
        for change in ({"AutoConnect": {"data": "true"}}, {"AutoConnect": {}},
                       {"Hidden": {"data": 1}}, {"Type": {"data": "8021x"}},
                       {"Name": {"data": "Other"}}):
            self.assertIsNone(self.preferences({**base, **change}))
        self.assertIsNone(self.preferences(base, success=False))


class RecoveryCheckpointTests(unittest.TestCase):
    setUp = strict_tests.StrictWifiTests.setUp

    def journal(self):
        before = self.plugin._get_iwd_rank_modifier_snapshot()
        self.iwd.unlink()
        return {"schema": 1, "phase": "rollback_failed", "active_uuid_before": UUID,
                "settings_before": self.plugin._band_settings_fields(wifi._load_settings()),
                "settings_file": str(self.settings), "iwd_before": before,
                "mutations": {"iwd": {"planned": True, "expected_present": False,
                                        "expected_value": ""}},
                "rollback_iwd_restart_required": True, "rollback_iwd_restart_completed": True,
                "rollback_reconnect_required": True, "rollback_reconnect_completed": True,
                "rollback_reconnect_uuid": UUID}

    def test_rewriting_restored_file_invalidates_old_service_checkpoint(self):
        result = self.plugin._restore_band_policy_transaction(self.journal(), "retry")
        self.assertTrue(result["success"], result)
        self.assertTrue(any(call.args[0] == ["/usr/bin/systemctl", "try-restart", "iwd"]
                            for call in self.commands.call_args_list))
        self.assertIn("BandModifier2_4GHz=0.01", self.iwd.read_text())

    def test_active_nm_uuid_without_radio_does_not_complete_recovery(self):
        journal = self.journal()
        with patch.object(self.plugin, "_get_link_frequency", return_value=None), \
             patch.object(self.plugin, "_reconnect_profile", return_value={"success": False}) as reconnect:
            result = self.plugin._restore_band_policy_transaction(journal, "retry")
        self.assertFalse(result["success"], result)
        reconnect.assert_called_once()
        self.assertFalse(journal["rollback_reconnect_completed"])
        self.assertEqual(journal["phase"], "rollback_failed")


class ReconnectAmbiguityTests(unittest.TestCase):
    def test_activating_profile_is_not_a_completed_connection(self):
        plugin = wifi.Plugin()
        for state, expected in (("50 (connecting)", False), ("110 (disconnecting)", False),
                                ("100 (connected)", True), ("", False)):
            with patch.object(plugin, "_run_cmd", return_value={
                    "success": True, "stdout": f"{state}\n{UUID}"}):
                self.assertEqual(plugin._profile_is_active(UUID, 'wlan0'), expected)

    def test_connected_other_profile_on_same_adapter_is_not_accepted(self):
        plugin = wifi.Plugin()
        with patch.object(plugin, "_run_cmd", return_value={
                "success": True, "stdout": f"100 (connected)\n{ProfileInventoryTests.OTHER}"}):
            self.assertFalse(plugin._profile_is_active(UUID, 'wlan0'))

    def test_collision_blocks_activation_before_nm_down_or_up(self):
        plugin = wifi.Plugin()
        for cycle in (True, False):
            with patch.object(plugin, "_get_current_backend", return_value="iwd"), \
                 patch.object(plugin, "_get_iwd_profile_conflict", return_value="Multiple profiles"), \
                 patch.object(plugin, "_profile_is_active", return_value=False), \
                 patch.object(plugin, "_run_cmd") as run:
                result = plugin._reconnect_profile(UUID, cycle=cycle)
            self.assertFalse(result["success"])
            run.assert_not_called()


class WifiConnectionIdentityTests(unittest.TestCase):
    OWN = 'da58df8b-cdb1-447d-80ab-19d11a634e7e'
    OTHER = '85194f4c-d496-4eec-bae0-d880b4cbcf26'

    def test_ui_connection_requires_completed_networkmanager_activation(self):
        plugin = wifi.Plugin()
        for state, expected in (('50 (connecting (configuring))', None),
                                ('70 (connecting (getting IP configuration))', None),
                                ('100 (connected)', self.OWN), ('100', self.OWN),
                                ('1000', None), ('', None)):
            with self.subTest(state=state), patch.object(plugin, '_run_cmd', return_value={
                    'success': True, 'stdout': state + '\n' + self.OWN}) as command:
                self.assertEqual(plugin._get_active_connection_uuid(
                    'wlan0', require_connected=True), expected)
                self.assertEqual(command.call_count, 1)
                self.assertIn('GENERAL.STATE,GENERAL.CON-UUID', command.call_args.args[0])
                self.assertEqual(command.call_args.kwargs['timeout'], 2)

    def responses(self, device_uuid):
        def run(argv, **kwargs):
            if argv[-2:] == ['device', 'show']:
                raise AssertionError('A UUID read must name its interface.')
            if 'GENERAL.CON-UUID' in argv:
                self.assertEqual(argv[-1], 'wlan0')
                return {'success': True, 'stdout': device_uuid + '\n'}
            if 'GENERAL.STATE,GENERAL.CON-UUID' in argv:
                self.assertEqual(argv[-1], 'wlan0')
                return {'success': True, 'stdout': f'100 (connected)\n{device_uuid}\n'}
            raise AssertionError(argv)
        return run

    def test_selected_interface_cannot_inherit_another_adapters_profile(self):
        plugin = wifi.Plugin()
        with patch.object(plugin, '_get_wifi_interface', return_value='wlan0'), \
                patch.object(plugin, '_run_cmd', side_effect=self.responses(self.OWN)):
            self.assertEqual(plugin._get_active_connection_uuid(), self.OWN)

    def test_disconnected_interface_cannot_borrow_another_active_profile(self):
        plugin = wifi.Plugin()
        for value in ('', '--', 'invalid', self.OWN + '\n' + self.OTHER):
            with self.subTest(value=value), \
                    patch.object(plugin, '_get_wifi_interface', return_value='wlan0'), \
                    patch.object(plugin, '_run_cmd', side_effect=self.responses(value)):
                self.assertIsNone(plugin._get_active_connection_uuid())

    def test_captured_interface_reuses_discovery_and_activity_belongs_to_that_adapter(self):
        plugin = wifi.Plugin()
        with patch.object(plugin, '_get_wifi_interface') as discovery, \
                patch.object(plugin, '_run_cmd', side_effect=self.responses(self.OWN)) as command:
            self.assertEqual(plugin._get_active_connection_uuid('wlan0'), self.OWN)
            discovery.assert_not_called()
            self.assertEqual(command.call_count, 1, 'A captured interface needs one device UUID query.')
            self.assertTrue(plugin._profile_is_active(self.OWN, 'wlan0'))
            self.assertFalse(plugin._profile_is_active(self.OTHER, 'wlan0'))
            self.assertFalse(plugin._profile_is_active('unknown', 'wlan0'))
            discovery.assert_not_called()


if __name__ == "__main__":
    unittest.main()

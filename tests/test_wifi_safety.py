# SPDX-License-Identifier: BSD-3-Clause
"""Offline WiFi input and durable ownership recovery regressions."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import ANY, AsyncMock, patch
from contextlib import ExitStack
import threading

import support  # Install isolated Decky stubs before backend imports.
import wifi_backend as wifi
from safe_settings import CorruptSettings, UnsafeSettingsPath


class WifiSafetyTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'POSIX mode, owner and symlink semantics')
    def test_system_file_write_keeps_mode_and_owner_and_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'main.conf'
            target.write_text('old', encoding='utf-8')
            target.chmod(0o640)
            with patch.object(os, 'fchown') as chown:
                wifi.Plugin._atomic_write_text(str(target), 'new')
            info = target.stat()
            self.assertEqual(target.read_text(encoding='utf-8'), 'new')
            self.assertEqual(info.st_mode & 0o777, 0o640)
            chown.assert_called_once_with(ANY, info.st_uid, info.st_gid)
            link = Path(folder) / 'link.conf'
            link.symlink_to(target)
            with self.assertRaises(UnsafeSettingsPath):
                wifi.Plugin._atomic_write_text(str(link), 'hijack')
            self.assertEqual(target.read_text(encoding='utf-8'), 'new')

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
            self.assertEqual(plugin._band_policy_ownership_error(settings, allow_restored=True), '')
        for external in ({'modifier_present': True, 'modifier_value': '0.5'},
                         {'modifier_present': False, 'ambiguous': True}):
            with patch.object(plugin, '_get_iwd_rank_modifier_snapshot', return_value=external):
                self.assertTrue(plugin._band_policy_ownership_error(settings, allow_restored=True))
        settings['band_policy_state']['original'] = {}
        with patch.object(plugin, '_get_iwd_rank_modifier_snapshot', return_value=baseline):
            self.assertTrue(plugin._band_policy_ownership_error(settings, allow_restored=True))

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


class WifiResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def held_io(self, observations, result):
        loop = asyncio.get_running_loop()
        def run(*args, **kwargs):
            heartbeat = threading.Event()
            loop.call_soon_threadsafe(heartbeat.set)
            observations.append(heartbeat.wait(.5))
            return result
        return run

    async def test_device_discovery_command_allows_heartbeat(self):
        plugin = wifi.Plugin()
        observed = []
        with patch.object(plugin, '_detect_device_family', return_value=('83N0', 'legion_go_2', 'Legion Go 2')), \
                patch.object(plugin, '_run_cmd', side_effect=self.held_io(observed, {'success': True, 'stdout': ''})):
            result = await plugin.get_device_info()
        self.assertTrue(result['success'])
        self.assertEqual(observed, [True])

    async def test_cancelled_band_change_keeps_network_lock_until_worker_finishes(self):
        plugin = wifi.Plugin()
        entered, release = threading.Event(), threading.Event()
        runs = []

        async def transaction(mode, **kwargs):
            runs.append(mode)
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test transaction timeout')
            return {'success': True}

        with patch.object(plugin, '_set_band_policy_impl', transaction),                 patch.object(plugin, '_acquire_band_policy_file_lock', return_value=object()),                 patch.object(plugin, '_release_band_policy_file_lock'),                 patch.object(plugin, '_stop_high_band_monitor', AsyncMock()),                 patch.object(plugin, '_restart_high_band_monitor', AsyncMock()):
            first = asyncio.create_task(plugin.set_band_policy(wifi.BAND_POLICY_OFF))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            second = asyncio.create_task(plugin.set_band_policy(wifi.BAND_POLICY_OFF))
            try:
                for _ in range(2):
                    first.cancel()
                    await asyncio.sleep(0)
                self.assertFalse(first.done(), 'Cancellation must wait for the transaction worker.')
                self.assertFalse(second.done(), 'The next network mutation must remain outside the lock.')
                self.assertEqual(len(runs), 1)
            finally:
                release.set()
                results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertTrue(results[1]['success'])
        self.assertEqual(len(runs), 2)


class IwdMainConfEditorTests(unittest.TestCase):
    """The root-owned /etc/iwd/main.conf editor changes only its own key."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.iwd = Path(folder.name) / "main.conf"
        patcher = patch.object(wifi, "IWD_MAIN_CONF", str(self.iwd))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.plugin = wifi.Plugin()

    def edit(self, original, value="0.01"):
        self.iwd.write_bytes(original.encode("utf-8"))
        self.plugin._set_iwd_rank_modifier(value)
        return self.iwd.read_bytes().decode("utf-8")

    def test_insert_into_existing_rank_keeps_everything_else_byte_exact(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                head = newline.join(["# top comment", "[General]", "EnableNetworkConfiguration=true",
                                     "", "[Rank]", "BandModifier5GHz=1.0", ""])
                tail = newline.join(["[Network]", "; keep me", "NameResolvingService=systemd", ""])
                self.assertEqual(self.edit(head + tail),
                                 head + f"BandModifier2_4GHz=0.01{newline}" + tail)
                self.assertEqual(self.edit(head + tail, "0.5").count("BandModifier2_4GHz=0.5"), 1)

    def test_insert_after_last_line_without_trailing_newline_starts_a_new_line(self):
        self.assertEqual(self.edit("[General]\nA=1\n\n[Rank]\nBandModifier5GHz=1.0"),
                         "[General]\nA=1\n\n[Rank]\nBandModifier5GHz=1.0\nBandModifier2_4GHz=0.01\n")
        self.assertEqual(self.edit("[Rank]"), "[Rank]\nBandModifier2_4GHz=0.01\n")
        self.assertEqual(self.edit("[Rank]\r\nBandModifier5GHz=1.0"),
                         "[Rank]\r\nBandModifier5GHz=1.0\r\nBandModifier2_4GHz=0.01\r\n")
        snapshot = self.plugin._get_iwd_rank_modifier_snapshot()
        self.assertEqual((snapshot["modifier_value"], snapshot["five_modifier_value"]), ("0.01", "1.0"))

    def test_existing_key_is_replaced_in_place_and_removed_cleanly(self):
        original = "[Rank]\r\n# c\r\nBandModifier2_4GHz=0.01 # note\r\nBandModifier5GHz=1.0\r\n"
        self.assertEqual(self.edit(original, "0.0"),
                         "[Rank]\r\n# c\r\nBandModifier2_4GHz=0.0\r\nBandModifier5GHz=1.0\r\n")
        self.plugin._set_iwd_rank_modifier(None)
        self.assertEqual(self.iwd.read_bytes().decode("utf-8"),
                         "[Rank]\r\n# c\r\nBandModifier5GHz=1.0\r\n")

    def test_section_and_key_names_are_case_sensitive_like_iwd(self):
        original = "[rank]\nBandModifier2_4GHz=0.5\nbandmodifier2_4ghz=0.7\n"
        self.iwd.write_bytes(original.encode())
        snapshot = self.plugin._get_iwd_rank_modifier_snapshot()
        self.assertFalse(snapshot["rank_section_present"])
        self.assertFalse(snapshot["modifier_present"])
        self.assertEqual(self.edit(original),
                         original + "\n[Rank]\nBandModifier2_4GHz=0.01\n")
        self.assertEqual(self.edit("[Rank]\nbandmodifier2_4ghz=0.7\n"),
                         "[Rank]\nbandmodifier2_4ghz=0.7\nBandModifier2_4GHz=0.01\n")

    def test_duplicate_rank_section_or_key_is_refused_without_writing(self):
        for original in ("[Rank]\nBandModifier5GHz=1.0\n[Rank]\nBandModifier6GHz=1.0\n",
                         "[Rank]\nBandModifier2_4GHz=0.5\nBandModifier2_4GHz=0.6\n"):
            self.iwd.write_bytes(original.encode())
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                self.plugin._set_iwd_rank_modifier("0.01")
            self.assertEqual(self.iwd.read_bytes().decode(), original)

    @unittest.skipUnless(os.name == "posix", "symlink semantics")
    def test_symlinked_main_conf_is_refused_without_touching_the_target(self):
        target = self.iwd.with_name("target.conf")
        target.write_text("[Rank]\n", encoding="utf-8")
        self.iwd.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.plugin._set_iwd_rank_modifier("0.01")
        self.assertEqual(target.read_text(encoding="utf-8"), "[Rank]\n")
        self.assertTrue(self.plugin._get_iwd_rank_modifier_snapshot()["ambiguous"])

    def test_high_band_modifiers_at_or_below_zero_are_refused(self):
        for key, check in (("BandModifier5GHz", wifi.Plugin._iwd_five_ghz_modifier_error),
                           ("BandModifier6GHz", wifi.Plugin._iwd_six_ghz_modifier_error)):
            for value, refused in (("1.0", False), ("0", True), ("0.0", True), ("-1", True), ("fast", True)):
                with self.subTest(key=key, value=value):
                    self.iwd.write_bytes(f"[Rank]\n{key}={value}\n".encode())
                    self.assertEqual(bool(check(self.plugin._get_iwd_rank_modifier_snapshot())), refused)


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: BSD-3-Clause
"""Offline WiFi input and durable ownership recovery regressions."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import ANY, AsyncMock, patch
import threading

import support  # Install isolated Decky stubs before backend imports.
import wifi_backend as wifi
from test_wifi_strict import isolate_files
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
        isolate_files(self)
        plugin = wifi.Plugin()
        uuid = 'dda82d97-6914-4788-802f-fa1ba9a1515f'

        def command(argv, **_kwargs):
            if argv[-2:] == ['dev', 'status']:
                return {'success': True, 'stdout': 'wlan0:wifi\n'}
            return {'success': True, 'stdout': '50 (connecting (configuring))\n' + uuid}

        with patch.object(plugin, '_get_current_backend', return_value='iwd'), \
             patch.object(plugin, '_run_cmd', side_effect=command) as run:
            status = plugin._get_status_sync()
        self.assertTrue(status['success'], status)
        self.assertFalse(status['connected'])
        self.assertNotIn('frequency', status['live'])
        self.assertFalse(any(call.args[0][0] == '/usr/bin/iw' for call in run.call_args_list))

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
            self.assertEqual(loaded["band_policy"], wifi.BAND_POLICY_HIGH_ONLY)
            self.assertNotIn("band_preference_enabled", loaded)  # pre-2.2.1 field dropped
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

        def transaction(mode, **kwargs):
            runs.append(mode)
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test transaction timeout')
            return {'success': True}

        isolate_files(self)
        with patch.object(plugin, '_set_band_policy_impl', transaction), \
                patch.object(plugin, '_stop_high_band_monitor', AsyncMock()), \
                patch.object(plugin, '_restart_high_band_monitor', AsyncMock()):
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

    def test_value_with_a_line_break_cannot_inject_config(self):
        original = "[Rank]\nBandModifier2_4GHz=0.5 # mine\n"
        for value in ("0.5\n[General]\nEnableNetworkConfiguration=true", "0.5\r", "0.5\x00"):
            self.iwd.write_bytes(original.encode())
            with self.assertRaisesRegex(ValueError, "control characters"):
                self.plugin._set_iwd_rank_modifier(value)
            self.assertEqual(self.iwd.read_bytes().decode(), original)
        self.assertEqual(self.edit(original, "0.5 # mine"), original)  # A user's one-line value still restores.

    def test_restore_recreates_only_a_whitespace_only_original(self):
        original = {"file_exists": True, "rank_section_present": False, "modifier_present": False}
        self.iwd.write_text("[Rank]\nBandModifier2_4GHz=0.0\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "saved content"):
            self.plugin._restore_iwd_rank_modifier(dict(original, empty_file_content="[General]\nEnableNetworkConfiguration=true\n"))
        self.assertEqual(self.iwd.read_text(encoding="utf-8"), "[Rank]\nBandModifier2_4GHz=0.0\n")  # Refused before any change.
        self.iwd.write_text("[Rank]\nBandModifier2_4GHz=0.0\n", encoding="utf-8")
        self.plugin._restore_iwd_rank_modifier(dict(original, empty_file_content="\n"))
        self.assertEqual(self.iwd.read_text(encoding="utf-8"), "\n")

    @unittest.skipUnless(os.name == "posix", "symlink semantics")
    def test_symlinked_main_conf_is_refused_without_touching_the_target(self):
        target = self.iwd.with_name("target.conf")
        target.write_text("[Rank]\n", encoding="utf-8")
        self.iwd.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.plugin._set_iwd_rank_modifier("0.01")
        self.assertEqual(target.read_text(encoding="utf-8"), "[Rank]\n")
        self.assertTrue(self.plugin._get_iwd_rank_modifier_snapshot()["ambiguous"])

    def test_os_rollback_baseline_can_be_recovered_only_by_explicit_transition(self):
        baseline = self.plugin._get_iwd_rank_modifier_snapshot()   # no main.conf before Companion
        settings = {'band_policy': wifi.BAND_POLICY_HIGH_ONLY, 'band_policy_state': {
            'mode': wifi.BAND_POLICY_HIGH_ONLY, 'owns_iwd': True,
            'original': {'iwd': baseline},
            'applied': {'iwd_modifier_present': True, 'iwd_modifier_value': '0.01'}}}
        self.assertTrue(self.plugin._band_policy_ownership_error(settings))
        self.assertEqual(self.plugin._band_policy_ownership_error(settings, allow_restored=True), '')
        for external in ("[Rank]\nBandModifier2_4GHz=0.5\n",
                         "[Rank]\nBandModifier2_4GHz=0.5\nBandModifier2_4GHz=0.6\n"):
            self.iwd.write_text(external, encoding="utf-8")
            self.assertTrue(self.plugin._band_policy_ownership_error(settings, allow_restored=True))
        self.iwd.unlink()
        settings['band_policy_state']['original'] = {}
        self.assertTrue(self.plugin._band_policy_ownership_error(settings, allow_restored=True))

    def test_high_band_modifiers_at_or_below_zero_are_refused(self):
        for key, band in (("BandModifier5GHz", "five"), ("BandModifier6GHz", "six")):
            for value, refused in (("1.0", False), ("0", True), ("0.0", True), ("-1", True), ("fast", True)):
                with self.subTest(key=key, value=value):
                    self.iwd.write_bytes(f"[Rank]\n{key}={value}\n".encode())
                    error = wifi.Plugin._iwd_band_modifier_error(
                        self.plugin._get_iwd_rank_modifier_snapshot(), band)
                    self.assertEqual(bool(error), refused)
                    if refused:
                        self.assertIn(key, error)


if __name__ == "__main__":
    unittest.main()

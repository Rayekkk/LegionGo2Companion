# SPDX-License-Identifier: BSD-3-Clause

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_ROOT = Path(tempfile.mkdtemp(prefix="legiongo2companion-tests-"))
CURRENT_SETTINGS = SETTINGS_ROOT / "LegionGo2Companion"
CURRENT_SETTINGS.mkdir()


class Logger:
    def info(self, _message):
        pass

    def warning(self, _message):
        pass

    def error(self, _message):
        pass

    def debug(self, _message):
        pass


async def emit(_event, *_args):
    pass


decky = types.ModuleType("decky")
decky.logger = Logger()
decky.emit = emit
decky.DECKY_PLUGIN_SETTINGS_DIR = str(CURRENT_SETTINGS)
sys.modules["decky"] = decky


class SettingsManager:
    def __init__(self, name, settings_directory):
        self.path = os.path.join(settings_directory, f"{name}.json")
        self.data = {}

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                self.data = json.load(handle)
        except (OSError, ValueError):
            self.data = {}

    def getSetting(self, key, default=None):
        return self.data.get(key, default)

    def setSetting(self, key, value):
        self.data[key] = value

    def commit(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle)


settings_module = types.ModuleType("settings")
settings_module.SettingsManager = SettingsManager
sys.modules["settings"] = settings_module

for unix_only in ("fcntl", "pwd"):
    try:
        __import__(unix_only)
    except ImportError:
        stub = types.ModuleType(unix_only)
        if unix_only == "pwd":
            stub.struct_passwd = object
            stub.getpwall = lambda: []
            stub.getpwnam = lambda _name: None
        sys.modules[unix_only] = stub

sys.path.insert(0, str(ROOT))
import display_backend  # noqa: E402
import main  # noqa: E402
import battery_backend  # noqa: E402
import controller_backend  # noqa: E402
import remap_backend  # noqa: E402
import rgb_backend  # noqa: E402
import tdp_backend  # noqa: E402
import vibration_backend  # noqa: E402
import wifi_backend  # noqa: E402


class IntegrationTests(unittest.TestCase):
    def test_component_settings_are_namespaced(self):
        paths = {
            Path(tdp_backend.settings.path).name,
            Path(vibration_backend.settings.path).name,
            Path(display_backend.settings.path).name,
            Path(wifi_backend.SETTINGS_FILE).name,
            Path(rgb_backend.settings.path).name,
            Path(remap_backend.settings.path).name,
            Path(battery_backend.settings.path).name,
            Path(controller_backend.settings.path).name,
        }
        self.assertEqual(
            paths,
            {
                "tdp_settings.json",
                "vibration_settings.json",
                "display_settings.json",
                "wifi_settings.json",
                "rgb_settings.json",
                "remap_settings.json",
                "battery_settings.json",
                "controller_settings.json",
            },
        )

    def test_tdp_cold_start_ignores_stale_active_limits_without_a_game(self):
        state = {
            "spl": 15000, "sppt": 18000, "fppt": 25000,
            "active_spl": 35000, "active_sppt": 40000, "active_fppt": 45000,
            "extras_unlocked": True,
        }
        with patch.object(tdp_backend, "_get_running_appid", return_value=""), \
                patch.object(tdp_backend, "_allowed_ceilings_mw",
                             return_value=(50000, 50000, 50000)):
            app_id, target, profile = tdp_backend._startup_context_target(state)
        self.assertEqual(app_id, "")
        self.assertFalse(profile)
        self.assertEqual(target, (15000, 18000, 25000))

    def test_tdp_cold_start_uses_running_game_profile(self):
        state = {"spl": 15000, "sppt": 18000, "fppt": 25000,
                 "extras_unlocked": True}
        game = {"spl": 22000, "sppt": 26000, "fppt": 32000}
        with patch.object(tdp_backend, "_get_running_appid", return_value="123"), \
                patch.object(tdp_backend, "_load_profiles", return_value={"123": game}), \
                patch.object(tdp_backend, "_allowed_ceilings_mw",
                             return_value=(50000, 50000, 50000)):
            app_id, target, profile = tdp_backend._startup_context_target(state)
        self.assertEqual(app_id, "123")
        self.assertTrue(profile)
        self.assertEqual(target, (22000, 26000, 32000))

    def test_malformed_profiles_are_dropped_before_use(self):
        tdp_payload = {
            "123": "broken",
            "not/an/app": {"spl": 9000},
            "456": {"spl": 12000, "sppt": 15000, "fppt": 20000},
        }
        with patch.object(tdp_backend, "_read_key", return_value=tdp_payload), \
                patch.object(tdp_backend, "_ceilings_mw",
                             return_value=(50000, 50000, 50000)):
            self.assertEqual(list(tdp_backend._load_profiles()), ["456"])

        vibe_payload = {
            "0": {"overwrite": True, "settings": {"touchpadEnabled": "false"}},
            "bad/app": {"overwrite": True, "settings": {}},
            "789": "broken",
        }
        with patch.object(vibration_backend.settings, "read"), \
                patch.object(vibration_backend.settings, "getSetting",
                             return_value=vibe_payload):
            profiles = vibration_backend._load_profiles()
        self.assertEqual(list(profiles), ["0"])
        self.assertFalse(profiles["0"]["overwrite"])
        self.assertFalse(profiles["0"]["settings"]["touchpadEnabled"])

    def test_vibration_cache_is_confirmed_against_hardware(self):
        with tempfile.TemporaryDirectory(prefix="lego-vibe-sysfs-") as root:
            sys_path = Path(root)
            attribute = sys_path / "rumble_intensity"
            attribute.write_text("low\n", encoding="ascii")
            key = (str(sys_path), "rumble_intensity")
            vibration_backend._attr_cache[key] = "medium"
            try:
                self.assertTrue(vibration_backend._write_attr(
                    str(sys_path), "rumble_intensity", "medium"))
                self.assertEqual(attribute.read_text(encoding="ascii"), "medium\n")
            finally:
                vibration_backend._attr_cache.pop(key, None)

    def test_gamescope_property_parser_tracks_values_and_removals(self):
        self.assertEqual(
            display_backend._parse_prop_line(
                "GAMESCOPE_DISPLAY_HDR_ENABLED(CARDINAL) = 1"),
            ("GAMESCOPE_DISPLAY_HDR_ENABLED", "1"),
        )
        self.assertEqual(
            display_backend._parse_prop_line(
                "GAMESCOPE_COLOR_APP_WANTS_HDR_FEEDBACK:  not found."),
            ("GAMESCOPE_COLOR_APP_WANTS_HDR_FEEDBACK", None),
        )

    def test_tdp_resume_detector_uses_boottime_gap(self):
        tdp_backend._last_suspend_offset = None
        with patch.object(tdp_backend, "_suspend_offset",
                          side_effect=[20.0, 20.2, 22.0]):
            self.assertFalse(tdp_backend._resume_detected())
            self.assertFalse(tdp_backend._resume_detected())
            self.assertTrue(tdp_backend._resume_detected())

    def test_vibration_resume_detector_uses_boottime_gap(self):
        vibration_backend._last_suspend_offset = None
        with patch.object(vibration_backend, "_suspend_offset",
                          side_effect=[8.0, 8.3, 10.0]):
            self.assertFalse(vibration_backend._resume_detected())
            self.assertFalse(vibration_backend._resume_detected())
            self.assertTrue(vibration_backend._resume_detected())

    def test_unsafe_legacy_bulk_profile_rpc_is_not_exposed(self):
        self.assertFalse(hasattr(main.Plugin(), "vibe_set_game_profiles"))

    def test_tdp_and_namespaced_wrappers_delegate(self):
        plugin = main.Plugin()
        plugin._tdp.get_caps = AsyncMock(return_value={"min": 5})
        plugin._vibration.get_driver_status = AsyncMock(return_value={"found": True})
        plugin._display.get_state = AsyncMock(return_value={"setup_done": True})
        plugin._wifi.get_status = AsyncMock(return_value={"success": True})
        plugin._wifi.rescan_and_reconnect = AsyncMock(
            return_value={"success": True, "frequency": 5320}
        )
        plugin._rgb.get_status = AsyncMock(return_value={"success": True})

        self.assertEqual(asyncio.run(plugin.get_caps()), {"min": 5})
        self.assertEqual(
            asyncio.run(plugin.vibe_get_driver_status()), {"found": True}
        )
        self.assertEqual(
            asyncio.run(plugin.display_get_state()), {"setup_done": True}
        )
        self.assertEqual(
            asyncio.run(plugin.wifi_get_status()), {"success": True}
        )
        self.assertEqual(
            asyncio.run(plugin.wifi_rescan_and_reconnect())["frequency"], 5320
        )
        self.assertTrue(asyncio.run(plugin.rgb_get_status())["success"])

    def test_lifecycle_tolerates_component_without_migration(self):
        plugin = main.Plugin()
        plugin._tdp = types.SimpleNamespace(_migration=AsyncMock())
        plugin._vibration = types.SimpleNamespace(_migration=AsyncMock())
        plugin._display = types.SimpleNamespace()
        asyncio.run(plugin._run_stage("_migration"))
        plugin._tdp._migration.assert_awaited_once()
        plugin._vibration._migration.assert_awaited_once()

    def test_legacy_migration_copies_and_never_overwrites(self):
        with tempfile.TemporaryDirectory(prefix="legiongo2-settings-") as raw:
            root = Path(raw)
            current = root / "LegionGo2Companion"
            legacy = root / "LeGoTDP"
            current.mkdir()
            legacy.mkdir()
            source = legacy / "settings.json"
            source.write_text('{"settings": {"spl": 25000}}', encoding="utf-8")

            old_dir = decky.DECKY_PLUGIN_SETTINGS_DIR
            decky.DECKY_PLUGIN_SETTINGS_DIR = str(current)
            try:
                main._migrate_legacy_settings()
                destination = current / "tdp_settings.json"
                self.assertEqual(
                    json.loads(destination.read_text(encoding="utf-8"))["settings"]["spl"],
                    25000,
                )
                source.write_text('{"settings": {"spl": 35000}}', encoding="utf-8")
                main._migrate_legacy_settings()
                self.assertEqual(
                    json.loads(destination.read_text(encoding="utf-8"))["settings"]["spl"],
                    25000,
                )
            finally:
                decky.DECKY_PLUGIN_SETTINGS_DIR = old_dir

    def test_legacy_migration_repairs_missing_top_level_values(self):
        with tempfile.TemporaryDirectory(prefix="legiongo2-settings-") as raw:
            root = Path(raw)
            current = root / "LegionGo2Companion"
            legacy = root / "LeGo2BrightnessFix"
            current.mkdir()
            legacy.mkdir()
            (legacy / "settings.json").write_text(
                '{"panel_mode": "hybrid", "active_mode": "hybrid"}',
                encoding="utf-8",
            )
            destination = current / "display_settings.json"
            destination.write_text('{"active_mode": null}', encoding="utf-8")

            old_dir = decky.DECKY_PLUGIN_SETTINGS_DIR
            decky.DECKY_PLUGIN_SETTINGS_DIR = str(current)
            try:
                main._migrate_legacy_settings()
                repaired = json.loads(destination.read_text(encoding="utf-8"))
                self.assertEqual(repaired["panel_mode"], "hybrid")
                self.assertIsNone(repaired["active_mode"])
            finally:
                decky.DECKY_PLUGIN_SETTINGS_DIR = old_dir

    def test_legacy_migration_does_not_follow_settings_directory_symlink(self):
        with tempfile.TemporaryDirectory(prefix="legiongo2-settings-") as raw:
            root = Path(raw)
            legacy = root / "LeGoTDP"
            legacy.mkdir()
            (legacy / "settings.json").write_text(
                '{"settings": {"spl": 25000}}', encoding="utf-8")
            victim = root / "victim"
            victim.mkdir()
            current = root / "LegionGo2Companion"
            try:
                current.symlink_to(victim, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            old_dir = decky.DECKY_PLUGIN_SETTINGS_DIR
            decky.DECKY_PLUGIN_SETTINGS_DIR = str(current)
            try:
                main._migrate_legacy_settings()
                self.assertFalse((victim / "tdp_settings.json").exists())
            finally:
                decky.DECKY_PLUGIN_SETTINGS_DIR = old_dir

    def test_wifi_migration_preserves_orphaned_ownership_state(self):
        with tempfile.TemporaryDirectory(prefix="legiongo2-settings-") as raw:
            root = Path(raw)
            current = root / "LegionGo2Companion"
            legacy = root / "WiFi Optimizer Go 2"
            current.mkdir()
            legacy.mkdir()
            source_payload = {
                "band_policy": "five_six_no_24",
                "band_preference_enabled": True,
                "band_policy_state": {
                    "mode": "five_six_no_24",
                    "owns_iwd": True,
                    "original": {"iwd": {"modifier_present": False}},
                    "applied": {
                        "iwd_modifier_present": True,
                        "iwd_modifier_value": "0.01",
                    },
                },
            }
            (legacy / "settings.json").write_text(
                json.dumps(source_payload), encoding="utf-8"
            )

            old_dir = decky.DECKY_PLUGIN_SETTINGS_DIR
            decky.DECKY_PLUGIN_SETTINGS_DIR = str(current)
            try:
                main._migrate_legacy_settings()
                migrated = json.loads(
                    (current / "wifi_settings.json").read_text(encoding="utf-8")
                )
                self.assertEqual(migrated, source_payload)
                self.assertEqual(
                    json.loads((legacy / "settings.json").read_text(encoding="utf-8")),
                    source_payload,
                )
            finally:
                decky.DECKY_PLUGIN_SETTINGS_DIR = old_dir

    def test_migrated_wifi_ownership_matches_active_iwd_setting(self):
        with tempfile.TemporaryDirectory(prefix="legiongo2-wifi-") as raw:
            root = Path(raw)
            settings_path = root / "wifi_settings.json"
            iwd_path = root / "main.conf"
            payload = dict(wifi_backend.DEFAULT_SETTINGS)
            payload["band_policy"] = wifi_backend.BAND_POLICY_HIGH_ONLY
            payload["band_preference_enabled"] = True
            payload["band_policy_state"] = {
                "schema": 1,
                "mode": wifi_backend.BAND_POLICY_HIGH_ONLY,
                "owns_iwd": True,
                "owns_nm_band": False,
                "original": {"nm_band": "", "iwd": {"modifier_present": False}},
                "applied": {
                    "nm_band": "",
                    "iwd_modifier_present": True,
                    "iwd_modifier_value": "0.01",
                },
            }
            settings_path.write_text(json.dumps(payload), encoding="utf-8")
            iwd_path.write_text(
                "[Rank]\nBandModifier2_4GHz=0.01\n", encoding="utf-8"
            )
            with patch.object(wifi_backend, "SETTINGS_FILE", str(settings_path)), \
                    patch.object(wifi_backend, "IWD_MAIN_CONF", str(iwd_path)):
                loaded = wifi_backend._load_settings()
                error = wifi_backend.Plugin()._band_policy_ownership_error(loaded)
            self.assertEqual(error, "")

    def test_wifi_watchdog_invokes_packaged_backend(self):
        plugin = wifi_backend.Plugin()
        observed = {}

        def run_cmd(command, **_kwargs):
            observed["command"] = command
            return {"success": True, "stdout": "", "stderr": "", "returncode": 0}

        with patch.object(plugin, "_save_band_policy_journal"), \
                patch.object(plugin, "_run_cmd", side_effect=run_cmd):
            result = plugin._schedule_band_policy_rollback({})

        self.assertTrue(result["success"])
        self.assertIn(
            str(ROOT / "wifi_backend.py"),
            observed["command"],
        )

    def test_wifi_normalizes_nmcli_escaped_bssid(self):
        self.assertEqual(
            wifi_backend.Plugin._normalize_bssid(r"AA\:29\:48\:E1\:7E\:03"),
            "aa:29:48:e1:7e:03",
        )

    def test_manifest_and_package_versions_match(self):
        plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["version"], package["version"])
        self.assertEqual(plugin["main"], "main.py")
        self.assertEqual(plugin["bin"], "dist/index.js")

    def test_get_version_reports_standalone_conflicts(self):
        response = asyncio.run(main.Plugin().get_version())
        self.assertEqual(response["version"], json.loads((ROOT / "plugin.json").read_text())["version"])
        self.assertIsInstance(response["standalone_plugins"], list)


if __name__ == "__main__":
    unittest.main()

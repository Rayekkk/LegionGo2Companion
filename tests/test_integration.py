# SPDX-License-Identifier: BSD-3-Clause

import asyncio
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from support import ROOT, decky  # Installs the Decky/settings stubs first.
import battery_backend
import controller_backend
import display_backend
import main
import remap_backend
import rgb_backend
import tdp_backend
import vibration_backend
import wifi_backend
import module_runtime


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
        with patch.object(tdp_backend, "_read_key", return_value=tdp_payload):
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

    def test_resume_is_a_boottime_gap_of_at_least_a_second(self):
        import module_runtime
        self.assertEqual(module_runtime.resumed(None, 20.0), (20.0, False))
        self.assertEqual(module_runtime.resumed(20.0, 20.2), (20.2, False))
        self.assertEqual(module_runtime.resumed(20.2, 22.0), (22.0, True))
        self.assertEqual(module_runtime.resumed(22.0, None), (22.0, False))

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

    def test_saved_wifi_ownership_matches_active_iwd_setting(self):
        with tempfile.TemporaryDirectory(prefix="legiongo2-wifi-") as raw:
            root = Path(raw)
            settings_path = root / "wifi_settings.json"
            iwd_path = root / "main.conf"
            payload = dict(wifi_backend.DEFAULT_SETTINGS)
            payload["band_policy"] = wifi_backend.BAND_POLICY_HIGH_ONLY
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

    def test_plugin_version_falls_back_for_non_object_manifest(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(main,"PLUGIN_DIR",folder):
            for raw in ('null','[]','"text"','42','true'):
                with self.subTest(raw=raw):
                    (Path(folder)/"plugin.json").write_text(raw,encoding="utf-8")
                    self.assertEqual(main._plugin_version(),"0.0.0")
            (Path(folder)/"plugin.json").write_text('{"version":"2.1.0"}',encoding="utf-8")
            self.assertEqual(main._plugin_version(),"2.1.0")

    def test_get_version_reports_standalone_conflicts(self):
        response = asyncio.run(main.Plugin().get_version())
        self.assertEqual(response["version"], json.loads((ROOT / "plugin.json").read_text())["version"])
        self.assertIsInstance(response["standalone_plugins"], list)


class RootForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await module_runtime.drain('tdp')

    async def test_root_forwards_reset_context_guards(self):
        plugin = main.Plugin()
        plugin._vibration.reset_to_default = AsyncMock(return_value={'success': True})
        await plugin.vibe_reset_to_default('111', '111')
        plugin._vibration.reset_to_default.assert_awaited_once_with('111', '111')


if __name__ == "__main__":
    unittest.main()

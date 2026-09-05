import asyncio
import inspect
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_integration  # Install the Decky/settings stubs before importing main.
import main
from conflict_guard import installed_conflicts


class DetectionTests(unittest.TestCase):
    def test_only_three_public_plugins_block_including_renamed_installs(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); current = root / "LegionGo2Companion"; current.mkdir()
            for name in ("HueSync", "WifiOptimizerGo2", "WiFi Optimizer Go 2"):
                directory = root / name; directory.mkdir()
                (directory / "plugin.json").write_text(json.dumps({"name": name}))
            self.assertEqual(installed_conflicts(current), [])
            (root / "LeGoTDP").mkdir()
            renamed = root / "renamed-plugin"; renamed.mkdir()
            (renamed / "plugin.json").write_text(json.dumps({"name": "LeGo Vibe Control"}))
            (root / "LeGo2 Brightness Fix").mkdir()
            self.assertEqual(installed_conflicts(current),
                             ["LeGo Vibe Control", "LeGo2 Brightness Fix", "LeGoTDP"])

    def test_settings_siblings_are_not_installed_plugins(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); current = root / "plugins" / "LegionGo2Companion"
            current.mkdir(parents=True)
            (root / "settings" / "LeGoTDP").mkdir(parents=True)
            self.assertEqual(installed_conflicts(current), [])

    def test_unreadable_or_incomplete_identity_is_not_treated_as_safe(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); current = root / "LegionGo2Companion"; current.mkdir()
            other = root / "renamed"; other.mkdir()
            (other / "plugin.json").write_text('{"name":')
            with self.assertRaises(ValueError): installed_conflicts(current)


class GateTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        plugin = main.Plugin()
        self.components = []
        for name in ("tdp", "vibration", "display", "wifi", "rgb", "remap", "battery", "controller"):
            component = types.SimpleNamespace(_main=AsyncMock(), _migration=AsyncMock(),
                                              _unload=AsyncMock(), _uninstall=AsyncMock())
            setattr(plugin, "_" + name, component); self.components.append(component)
        return plugin

    async def test_blocked_start_migration_unload_and_uninstall_never_enter_modules(self):
        plugin = self.make_plugin()
        with patch.object(main, "_installed_standalone_plugins", return_value=["LeGoTDP"]), \
             patch.object(main, "_migrate_legacy_settings") as migrate:
            await plugin._migration(); await plugin._main()
            status = await plugin.get_version()
            self.assertTrue(status["blocked"])
            await plugin._unload(); await plugin._uninstall()
            migrate.assert_not_called()
        for c in self.components:
            for stage in ("_main", "_migration", "_unload", "_uninstall"):
                getattr(c, stage).assert_not_awaited()

    async def test_every_component_rpc_is_rejected_before_delegation(self):
        plugin = self.make_plugin(); checked = []
        with patch.object(main, "_installed_standalone_plugins", return_value=["LeGo Vibe Control"]):
            for name, fn in vars(main.Plugin).items():
                if name.startswith("_") or name == "get_version" or not inspect.iscoroutinefunction(fn):
                    continue
                required = [p for p in inspect.signature(fn).parameters.values()
                            if p.name != "self" and p.default is inspect.Parameter.empty]
                with self.assertRaisesRegex(RuntimeError, "All Companion modules are paused"):
                    await getattr(plugin, name)(*[None for _ in required])
                checked.append(name)
        self.assertGreater(len(checked), 50)

    async def test_live_conflict_stops_all_modules_and_latches_until_process_restart(self):
        plugin = self.make_plugin(); conflicts = []
        with patch.object(main, "_installed_standalone_plugins", side_effect=lambda: list(conflicts)), \
             patch.object(main, "_migrate_legacy_settings"), \
             patch.object(main, "_reload_component_settings"):
            await plugin._main()
            for c in self.components: c._main.assert_awaited_once()
            conflicts.append("LeGo2 Brightness Fix")
            await plugin._check_guard()
            for c in self.components: c._unload.assert_awaited_once()
            conflicts.clear()
            status = await plugin.get_version()
            self.assertTrue(status["blocked"]); self.assertTrue(status["restart_required"])
            await plugin._main()
            for c in self.components: c._main.assert_awaited_once()
            await plugin._unload(); await plugin._uninstall()
            for c in self.components:
                c._unload.assert_awaited_once(); c._uninstall.assert_not_awaited()

    async def test_normal_lifecycle_still_runs_all_modules(self):
        plugin = self.make_plugin()
        with patch.object(main, "_installed_standalone_plugins", return_value=[]), \
             patch.object(main, "_migrate_legacy_settings"), \
             patch.object(main, "_reload_component_settings"):
            await plugin._migration(); await plugin._main(); await plugin._unload(); await plugin._uninstall()
        for c in self.components:
            for stage in ("_migration", "_main", "_unload", "_uninstall"):
                getattr(c, stage).assert_awaited_once()

    async def test_failed_scan_blocks_hardware(self):
        plugin = self.make_plugin()
        with patch.object(main, "_installed_standalone_plugins", side_effect=PermissionError("denied")):
            status = await plugin.get_version()
            self.assertTrue(status["blocked"]); self.assertIn("denied", status["guard_error"])
            with self.assertRaisesRegex(RuntimeError, "could not be verified"):
                await plugin.rgb_set_brightness(20)

    async def test_monitor_catches_installation_without_frontend(self):
        plugin = self.make_plugin(); conflicts = []
        with patch.object(main, "_installed_standalone_plugins", side_effect=lambda: list(conflicts)), \
             patch.object(main, "CHECK_INTERVAL_S", 0.01), \
             patch.object(main, "_migrate_legacy_settings"), \
             patch.object(main, "_reload_component_settings"):
            await plugin._main(); conflicts.append("LeGoTDP")
            for _ in range(100):
                if not plugin._modules_started: break
                await asyncio.sleep(0.01)
            self.assertFalse(plugin._modules_started)
            await plugin._unload()
        for c in self.components: c._unload.assert_awaited_once()

    async def test_inflight_rpc_finishes_before_modules_stop(self):
        plugin = self.make_plugin(); conflicts = []; entered = asyncio.Event(); finish = asyncio.Event(); order = []
        async def operation():
            entered.set(); await finish.wait(); order.append("rpc finished"); return {"success": True}
        async def stop(): order.append("modules stopped")
        plugin._wifi.rescan_and_reconnect = operation
        plugin._wifi._unload = AsyncMock(side_effect=stop)
        plugin._modules_started = True
        with patch.object(main, "_installed_standalone_plugins", side_effect=lambda: list(conflicts)):
            rpc = asyncio.create_task(plugin.wifi_rescan_and_reconnect()); await entered.wait()
            conflicts.append("LeGoTDP"); checking = asyncio.create_task(plugin._check_guard())
            for _ in range(100):
                if plugin._guard_blocked: break
                await asyncio.sleep(0.001)
            self.assertTrue(plugin._guard_blocked); self.assertEqual(order, [])
            finish.set(); await rpc; await checking
            self.assertEqual(order, ["rpc finished", "modules stopped"])

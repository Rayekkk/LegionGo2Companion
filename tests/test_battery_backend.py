# SPDX-License-Identifier: BSD-3-Clause

import asyncio
import copy
import json
import itertools
import os
import sys
import types
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if "decky" not in sys.modules:
    _settings_temp = tempfile.TemporaryDirectory(prefix="companion-battery-tests-")
    _decky = types.ModuleType("decky")
    _decky.DECKY_PLUGIN_SETTINGS_DIR = _settings_temp.name
    _decky.logger = types.SimpleNamespace(**{
        level: lambda _message: None for level in ("info", "warning", "error", "debug")
    })
    sys.modules["decky"] = _decky
import battery_backend as battery
from safe_settings import SettingsManager


class BatteryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "sys"
        identity = self.root / "class/dmi/id"
        identity.mkdir(parents=True)
        (identity / "sys_vendor").write_text("LENOVO", encoding="ascii")
        (identity / "product_name").write_text("83N0", encoding="ascii")
        self.supplies = self.root / "class/power_supply"
        self.supplies.mkdir()
        self.device = self.make_battery("BAT0")
        self.store = SettingsManager("battery_settings", str(Path(self.temp.name) / "settings"))
        for name, value in (("SYS_ROOT", self.root), ("settings", self.store)):
            patcher = patch.object(battery, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.writes = []
        patcher = patch.object(battery, "_write_mode", side_effect=self.write_mode)
        self.writer = patcher.start()
        self.addCleanup(patcher.stop)
        self.plugin = battery.Plugin()

    def make_battery(self, name, scope=None):
        dev = self.supplies / name
        dev.mkdir()
        for key, value in {"type": "Battery", "present": "1", "capacity": "98",
                           "status": "Not charging", "charge_types": "[Fast] Standard Long_Life"}.items():
            (dev / key).write_text(value, encoding="ascii")
        if scope is not None:
            (dev / "scope").write_text(scope, encoding="ascii")
        return dev

    def current(self):
        return battery._parse_modes((self.device / "charge_types").read_text())[1]

    def externally_select(self, mode):
        value = " ".join(f"[{option}]" if option == mode else option
                         for option in ("Fast", "Standard", "Long_Life"))
        (self.device / "charge_types").write_text(value, encoding="ascii")

    def write_mode(self, path, mode):
        self.assertEqual(path, self.device / "charge_types")
        self.writes.append(mode)
        self.externally_select(mode)

    async def asyncTearDown(self):
        await self.plugin._unload()

    async def test_unconfigured_start_status_and_shutdown_never_write(self):
        await self.plugin._main()
        status = await self.plugin.get_status()
        await self.plugin._unload()
        await self.plugin._uninstall()
        self.assertTrue(status["supported"])
        self.assertFalse(status["managed"])
        self.assertFalse(status["enabled"])
        self.assertIsNone(status["requested_enabled"])
        self.assertEqual(status["capacity"], 98)
        self.assertEqual(status["charging_status"], "Not charging")
        self.assertEqual(self.writes, [])
        self.assertFalse(Path(self.store.path).exists())

    async def test_first_disabled_choice_preserves_fast_without_hardware_write(self):
        result = await self.plugin.set_enabled(False)
        self.assertTrue(result["success"])
        self.assertEqual(self.writes, [])
        self.assertEqual(result["status"]["baseline"], "Fast")
        self.assertFalse(result["status"]["requested_enabled"])

    async def test_enable_disable_preserves_original_fast_and_commits_real_state(self):
        result = await self.plugin.set_enabled(True)
        self.assertTrue(result["success"])
        self.assertEqual(self.current(), "Long_Life")
        self.assertEqual(result["status"]["baseline"], "Fast")
        self.assertTrue(result["status"]["enabled"])
        saved = json.loads(Path(self.store.path).read_text())
        self.assertTrue(saved["state"]["requested_enabled"])
        result = await self.plugin.set_enabled(False)
        self.assertTrue(result["success"])
        self.assertEqual(self.current(), "Fast")
        self.assertEqual(self.writes, ["Long_Life", "Fast"])

    async def test_same_selection_does_not_rewrite_hardware(self):
        await self.plugin.set_enabled(True)
        await self.plugin.set_enabled(True)
        await self.plugin._main()
        self.plugin._repair()
        self.assertEqual(self.writes, ["Long_Life"])

    async def test_readback_mismatch_rolls_back_hardware_and_settings(self):
        snapshot = copy.deepcopy(self.store.settings)
        original = self.write_mode
        def misapply(path, mode):
            if mode == "Long_Life":
                self.writes.append(mode)  # Kernel ignored the requested change.
            else:
                original(path, mode)
        self.writer.side_effect = misapply
        result = await self.plugin.set_enabled(True)
        self.assertFalse(result["success"])
        self.assertIn("did not confirm", result["error"])
        self.assertEqual(self.current(), "Fast")
        self.assertEqual(self.store.settings, snapshot)
        self.assertFalse(result["status"]["managed"])

    async def test_readback_error_after_write_restores_original_mode(self):
        original_read = battery._read_attr
        failed = False
        def read(path, *args):
            nonlocal failed
            if path == self.device / "charge_types" and self.writes and not failed:
                failed = True
                raise OSError("temporary firmware read failure")
            return original_read(path, *args)
        with patch.object(battery, "_read_attr", side_effect=read):
            result = await self.plugin.set_enabled(True)
        self.assertFalse(result["success"])
        self.assertEqual(self.current(), "Fast")
        self.assertEqual(self.writes, ["Long_Life", "Fast"])
        self.assertFalse(self.plugin._load()["managed"])

    async def test_commit_failure_restores_hardware_and_durable_settings(self):
        self.store.replace({"unrelated": "keep"})
        snapshot = copy.deepcopy(self.store.settings)
        real_commit = self.store.commit
        commits = 0
        def fail_after_commit():
            nonlocal commits
            real_commit()
            commits += 1
            if commits == 2:  # The journal succeeded; the final commit failed.
                raise OSError("fsync failed after rename")
        with patch.object(self.store, "commit", side_effect=fail_after_commit):
            result = await self.plugin.set_enabled(True)
        self.assertFalse(result["success"])
        self.assertEqual(self.current(), "Fast")
        self.assertEqual(self.store.settings, snapshot)
        self.assertEqual(json.loads(Path(self.store.path).read_text()), snapshot)

    async def test_failed_hardware_rollback_is_explicit_and_does_not_save_desired_state(self):
        def fail_restore(path, mode):
            if mode == "Fast":
                raise OSError("firmware restore failed")
            self.write_mode(path, mode)
        self.writer.side_effect = fail_restore
        with patch.object(self.plugin, "_save", side_effect=OSError("disk full")):
            result = await self.plugin.set_enabled(True)
        self.assertFalse(result["success"])
        self.assertIn("Hardware rollback failed", result["error"])
        self.assertTrue(result["status"]["enabled"])
        self.assertFalse(result["status"]["managed"])
        self.assertTrue(result["status"]["recovery_pending"])
        self.writer.side_effect = self.write_mode
        result = await self.plugin.release_control()
        self.assertTrue(result["success"])
        self.assertEqual(self.current(), "Fast")
        self.assertFalse(result["status"]["recovery_pending"])

    async def test_release_restores_fast_only_while_own_setting_is_active(self):
        await self.plugin.set_enabled(True)
        result = await self.plugin.release_control()
        self.assertTrue(result["success"])
        self.assertEqual(self.current(), "Fast")
        self.assertFalse(result["status"]["managed"])
        await self.plugin.set_enabled(True)
        self.externally_select("Standard")
        count = len(self.writes)
        result = await self.plugin.release_control()
        self.assertTrue(result["success"])
        self.assertEqual(self.current(), "Standard")
        self.assertEqual(len(self.writes), count)
        self.assertFalse(result["status"]["managed"])

    async def test_uninstall_restores_baseline_but_unload_only_stops_monitor(self):
        await self.plugin.set_enabled(True)
        await self.plugin._main()
        await self.plugin._unload()
        self.assertIsNone(self.plugin._task)
        self.assertFalse(self.plugin._running)
        self.assertEqual(self.current(), "Long_Life")
        await self.plugin._uninstall()
        self.assertEqual(self.current(), "Fast")

    async def test_restart_reapplies_saved_choice_with_baseline_intact(self):
        await self.plugin.set_enabled(True)
        self.externally_select("Standard")
        self.store.read()
        restarted = battery.Plugin()
        try:
            await restarted._main()
            self.assertEqual(self.current(), "Long_Life")
            self.assertEqual(restarted._load()["baseline"], "Fast")
        finally:
            await restarted._unload()

    async def test_wake_reapplies_but_idle_checks_do_not_poll_hardware_every_tick(self):
        await self.plugin.set_enabled(True)
        await self.plugin._main()
        self.externally_select("Standard")
        with patch.object(battery, "RESUME_CHECK_S", 0.01), \
             patch.object(battery, "_suspend_offset", side_effect=itertools.chain([0, 0], itertools.repeat(2))), \
             patch.object(self.plugin, "_repair", wraps=self.plugin._repair) as repair:
            # Replace the just-created watcher before its first tick.
            await self.plugin._unload()
            self.plugin._running = True
            self.plugin._task = asyncio.create_task(self.plugin._watch())
            await asyncio.sleep(0.045)
            await self.plugin._unload()
            self.assertEqual(repair.call_count, 1)
        self.assertEqual(self.current(), "Long_Life")

    async def test_saved_preference_and_actual_state_are_reported_separately(self):
        await self.plugin.set_enabled(True)
        self.externally_select("Standard")
        result = await self.plugin.get_status()
        self.assertTrue(result["requested_enabled"])
        self.assertFalse(result["enabled"])
        self.assertEqual(self.current(), "Standard")  # A status read never enforces.

    async def test_missing_or_ambiguous_system_battery_disables_controls(self):
        (self.device / "present").write_text("0")
        status = await self.plugin.get_status()
        self.assertFalse(status["supported"])
        self.assertIn("No system battery", status["reason"])
        (self.device / "present").write_text("1")
        self.make_battery("BAT1", "System")
        result = await self.plugin.set_enabled(True)
        self.assertFalse(result["success"])
        self.assertIn("More than one", result["error"])
        self.assertEqual(self.writes, [])

    async def test_controller_battery_is_not_selected(self):
        self.make_battery("hid-controller", "Device")
        self.make_battery("BAT2", "Device")
        status = await self.plugin.get_status()
        self.assertTrue(status["supported"])

    async def test_missing_interface_and_other_device_never_write(self):
        (self.device / "charge_types").unlink()
        result = await self.plugin.set_enabled(True)
        self.assertFalse(result["success"])
        self.externally_select("Fast")
        (self.root / "class/dmi/id/product_name").write_text("83N6")
        result = await self.plugin.set_enabled(True)
        self.assertFalse(result["success"])
        self.assertIn("only on Lenovo Legion Go 2", result["error"])
        self.assertEqual(self.writes, [])

    @unittest.skipUnless(os.name == "posix", "sysfs Unix mode permissions")
    async def test_read_only_kernel_attribute_does_not_advertise_writable_protection(self):
        path = self.device / "charge_types"
        path.chmod(0o444)
        try:
            status = await self.plugin.get_status()
            self.assertFalse(status["supported"])
            self.assertIn("read-only", status["reason"])
            result = await self.plugin.set_enabled(True)
            self.assertFalse(result["success"])
            self.assertEqual(self.writes, [])
        finally:
            path.chmod(0o644)

    async def test_malformed_modes_and_non_boolean_input_fail_closed(self):
        for text in ("Standard Long_Life", "[Fast] [Long_Life] Standard", "[Standard] Fast",
                     "[Unknown] Standard Long_Life", "[Standard] Standard Long_Life", "[Fast Standard Long_Life"):
            with self.subTest(text=text):
                (self.device / "charge_types").write_text(text)
                result = await self.plugin.set_enabled(True)
                self.assertFalse(result["success"])
        self.externally_select("Fast")
        for value in (1, 0, "true", None, {}, []):
            result = await self.plugin.set_enabled(value)
            self.assertFalse(result["success"])
        self.assertEqual(self.writes, [])

    async def test_invalid_saved_baseline_never_applies(self):
        self.store.replace({"state": {"managed": True, "requested_enabled": True,
                                      "baseline": "Bypass"}})
        await self.plugin._main()
        status = await self.plugin.get_status()
        self.assertFalse(status["success"])
        self.assertIn("restoration state", status["error"])
        self.assertEqual(self.writes, [])

    async def test_release_commit_failure_preserves_managed_hardware_and_setting(self):
        await self.plugin.set_enabled(True)
        snapshot = copy.deepcopy(self.store.settings)
        with patch.object(self.plugin, "_save", side_effect=OSError("disk full")):
            result = await self.plugin.release_control()
        self.assertFalse(result["success"])
        self.assertEqual(self.current(), "Long_Life")
        self.assertEqual(self.store.settings, snapshot)

    async def test_unload_prevents_later_repairs(self):
        await self.plugin.set_enabled(True)
        await self.plugin._main()
        await self.plugin._unload()
        self.externally_select("Standard")
        self.plugin._repair()
        self.assertEqual(self.current(), "Standard")

    async def test_baseline_long_life_is_preserved_without_inventing_fast_mode(self):
        self.externally_select("Long_Life")
        result = await self.plugin.set_enabled(False)
        self.assertTrue(result["success"])
        self.assertEqual(self.current(), "Standard")
        await self.plugin.release_control()
        self.assertEqual(self.current(), "Long_Life")

    async def test_crash_after_hardware_write_recovers_fast_and_uncommitted_settings(self):
        class Crash(BaseException):
            pass
        with patch.object(self.plugin, "_save", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.plugin._set(True)
        self.assertEqual(self.current(), "Long_Life")
        durable = json.loads(Path(self.store.path).read_text())
        self.assertEqual(durable["pending"]["before_mode"], "Fast")
        count = len(self.writes)
        status = await self.plugin.get_status()
        self.assertTrue(status["recovery_pending"])
        self.assertTrue(status["enabled"])
        self.assertFalse(status["managed"])
        self.assertEqual(len(self.writes), count)  # Read-only status cannot recover.
        self.store.read()
        restarted = battery.Plugin()
        try:
            await restarted._main()
            self.assertEqual(self.current(), "Fast")
            self.assertFalse(restarted._load()["managed"])
            self.assertNotIn("pending", json.loads(Path(self.store.path).read_text()))
        finally:
            await restarted._unload()

    async def test_crash_before_hardware_write_recovers_without_a_hardware_write(self):
        class Crash(BaseException):
            pass
        self.writer.side_effect = Crash()
        with self.assertRaises(Crash):
            self.plugin._set(True)
        self.writer.side_effect = self.write_mode
        self.store.read()
        await self.plugin._main()
        self.assertEqual(self.current(), "Fast")
        self.assertEqual(self.writes, [])
        self.assertFalse(self.plugin._load()["managed"])

    async def test_crash_during_release_recovers_previous_managed_preference(self):
        class Crash(BaseException):
            pass
        await self.plugin.set_enabled(True)
        with patch.object(self.plugin, "_save", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.plugin._release()
        self.assertEqual(self.current(), "Fast")
        self.store.read()
        await self.plugin._main()
        self.assertEqual(self.current(), "Long_Life")
        self.assertTrue(self.plugin._load()["managed"])
        result = await self.plugin.release_control()
        self.assertTrue(result["success"])
        self.assertEqual(self.current(), "Fast")

    async def test_pending_recovery_preserves_a_third_mode_selected_externally(self):
        class Crash(BaseException):
            pass
        with patch.object(self.plugin, "_save", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.plugin._set(True)
        self.externally_select("Standard")
        count = len(self.writes)
        self.store.read()
        await self.plugin._main()
        self.assertEqual(self.current(), "Standard")
        self.assertEqual(len(self.writes), count)
        self.assertFalse(self.plugin._load()["managed"])

    async def test_malformed_recovery_record_is_not_used_for_hardware_writes(self):
        self.store.replace({"pending": {
            "version": 1, "before_mode": "Fast", "target": "Long_Life",
            "previous": {"state": {"managed": True, "baseline": "Fast"}},
        }})
        self.externally_select("Long_Life")
        await self.plugin._main()
        result = await self.plugin.release_control()
        self.assertFalse(result["success"])
        self.assertEqual(self.current(), "Long_Life")
        self.assertEqual(self.writes, [])

    async def test_cancelling_rpc_waits_for_transaction_to_complete(self):
        entered = threading.Event()
        finish = threading.Event()
        def delayed(path, mode):
            entered.set()
            if not finish.wait(3):
                raise TimeoutError("test transaction was not released")
            self.write_mode(path, mode)
        self.writer.side_effect = delayed
        task = asyncio.create_task(self.plugin.set_enabled(True))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.current(), "Long_Life")
        self.assertTrue(self.plugin._load()["managed"])

    def test_regular_attribute_and_realpath_checks_reject_unsafe_paths(self):
        with self.assertRaises(battery.BatteryError):
            battery._read_attr(self.device)
        outside = Path(self.temp.name) / "outside"
        outside.write_text("x")
        with self.assertRaises(battery.BatteryError):
            battery._read_attr(outside)


if __name__ == "__main__":
    unittest.main()

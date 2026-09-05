"""Regression coverage for the September reliability audit. Hardware is mocked."""
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import test_integration as fixture
from test_remap_backend import BASE_PROFILE, MemorySettings
import tdp_backend as tdp
import vibration_backend as vibe
import remap_backend as remap
import display_backend as display
import wifi_backend as wifi
from safe_settings import AtomicSettingsManager


class AuditProbes(unittest.TestCase):
    def test_A01_display_writer_ignores_preexisting_staging_link(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            victim = root / "unrelated-file"
            victim.write_bytes(b"original unrelated data")
            target = root / "edid-original.bin"
            staging = Path(str(target) + ".lego2brightnessfix.tmp")
            # Hard links work without Windows symlink privileges. This tests
            # following an existing inode, not Linux privilege escalation.
            os.link(victim, staging)
            self.assertTrue(display._write_file_atomically(str(target), b"EDID"))
            self.assertEqual(victim.read_bytes(), b"original unrelated data")

    def test_A02_wifi_journal_uses_persistent_storage(self):
        plugin = wifi.Plugin()
        self.assertTrue(wifi.BAND_POLICY_JOURNAL_FILE.startswith("/var/lib/"))
        with patch.object(plugin, "_load_band_policy_journal", return_value=None), \
             patch.object(plugin, "_nmcli_get") as read_profile, \
             patch.object(plugin, "_restore_band_policy_transaction") as recover:
            result = plugin._recover_band_policy_journal()
        self.assertEqual(result, {"success": True, "recovered": False})
        read_profile.assert_not_called()
        recover.assert_not_called()

    def test_A03_tdp_failed_game_transition_retries_desired_target(self):
        old = (15000, 18000, 25000)
        desired = (25000, 28000, 35000)
        state = dict(zip(("spl", "sppt", "fppt"), old))
        state.update(dict(zip(("active_spl", "active_sppt", "active_fppt"), old)))
        state["enabled"] = True
        failed = {"success": False, "returncode": -1, "stderr": "device waking"}
        with patch.object(tdp, "_current_game_id", ""), \
             patch.object(tdp, "_current_ac_online", False), \
             patch.object(tdp, "_load_settings", side_effect=lambda: copy.deepcopy(state)), \
             patch.object(tdp, "_resume_detected", return_value=False), \
             patch.object(tdp, "_best_effort_reapply_saved_cpu_power_locked"), \
             patch.object(tdp, "_get_running_appid", return_value="123"), \
             patch.object(tdp, "_get_ac_online", return_value=False), \
             patch.object(tdp, "_load_profiles", return_value={"123": dict(zip(("spl", "sppt", "fppt"), desired))}), \
             patch.object(tdp, "_clamp_for_settings", side_effect=lambda s, *x: x), \
             patch.object(tdp, "_apply_and_record", return_value=failed) as apply, \
             patch.object(tdp, "_enforce_target") as enforce:
            tdp._transition_key = None
            tdp._check_and_enforce_locked()
            tdp._transition_retry_at = 0
            tdp._check_and_enforce_locked()
            self.assertNotEqual(tdp._current_game_id, "123")
            self.assertEqual(apply.call_count, 2)
            self.assertEqual(apply.call_args.args[:3], desired)
            enforce.assert_not_called()

    def test_A04_tdp_disk_failure_restores_previous_hardware_value(self):
        state = {"enabled": True, "spl": 15000, "sppt": 18000, "fppt": 25000}
        hardware = [15000, 18000, 25000]
        def apply(s, *values):
            hardware[:] = values
            return {"success": True, "stderr": "", "stdout": "", "returncode": 0}
        plugin = tdp.Plugin()
        plugin._ready = True
        with patch.object(tdp, "_load_settings", side_effect=lambda: copy.deepcopy(state)), \
             patch.object(tdp, "_get_running_appid", return_value=""), \
             patch.object(tdp, "_clamp_for_settings", side_effect=lambda s, *x: x), \
             patch.object(tdp, "_apply_limits_with_saved_cpu_power", side_effect=apply), \
             patch.object(tdp, "_write_keys", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "disk full.*previous target restored"):
                asyncio.run(plugin.apply_tdp(8000, 10000, 15000))
        self.assertEqual(hardware, [15000, 18000, 25000])
        self.assertEqual(state["spl"], 15000)

    def test_A05_vibration_concurrent_fields_preserve_both_edits(self):
        with tempfile.TemporaryDirectory() as raw:
            manager = AtomicSettingsManager("vibration_settings", raw)
            manager.replace({"game_profiles": {"0": {
                "overwrite": False, "settings": dict(vibe.DEFAULT_PROFILE)}}})
            actual_load = vibe._load_profiles
            def simultaneous_read():
                snapshot = actual_load()
                __import__("time").sleep(0.03)
                return snapshot
            with patch.object(vibe, "settings", manager), \
                 patch.object(vibe, "_active_app_id", "0"), \
                 patch.object(vibe, "_load_profiles", side_effect=simultaneous_read):
                with ThreadPoolExecutor(max_workers=2) as workers:
                    a = workers.submit(vibe._update_active, "level", 3)
                    b = workers.submit(vibe._update_active, "touchpadEnabled", False)
                    a.result(timeout=5)
                    b.result(timeout=5)
                manager.read()
                saved = manager.getSetting("game_profiles")["0"]["settings"]
            self.assertEqual((saved["level"], saved["touchpadEnabled"]), (3, False))

    def test_A06_vibration_delayed_edit_rejects_new_game(self):
        profiles = {key: {"overwrite": key != "0", "settings": dict(vibe.DEFAULT_PROFILE)}
                    for key in ("0", "111", "222")}
        with patch.object(vibe, "_active_app_id", "222"), \
             patch.object(vibe, "_load_profiles", return_value=copy.deepcopy(profiles)), \
             patch.object(vibe, "_save_profiles") as save, \
             patch.object(vibe, "_apply_settings", return_value=True):
            # The RPC generated on game 111's page carries only its level.
            result = asyncio.run(vibe.Plugin().set_intensity(3, "111", "111"))
        self.assertFalse(result["success"])
        save.assert_not_called()

    def test_A07_remap_unchanged_profile_is_not_reloaded(self):
        state = remap._sanitize_state({"enabled": True, "baseline_profile": BASE_PROFILE})
        current = remap._build_profile(BASE_PROFILE, state)
        with patch.object(remap, "settings", MemorySettings(state)), \
             patch.object(remap, "_find_device", return_value=("device", "Go2")), \
             patch.object(remap, "_get_profile", return_value=current), \
             patch.object(remap._service_watch, "capture"), \
             patch.object(remap, "_load_profile", return_value=current) as load:
            remap._repair_sync()
        load.assert_not_called()

    def test_A08_remap_preserves_unrelated_edit_under_same_profile_name(self):
        state = remap._sanitize_state({"enabled": True, "baseline_profile": BASE_PROFILE})
        original = remap._build_profile(BASE_PROFILE, state)
        externally_edited = original.replace("LeftPaddle1", "RightPaddle1")
        self.assertTrue(remap._profile_matches(externally_edited, state))
        with patch.object(remap, "settings", MemorySettings(state)), \
             patch.object(remap, "_find_device", return_value=("device", "Go2")), \
             patch.object(remap, "_get_profile", return_value=externally_edited), \
             patch.object(remap._service_watch, "capture"), \
             patch.object(remap, "_load_profile", side_effect=lambda p, data: data) as load:
            remap._repair_sync()
        load.assert_not_called()

    def test_A09_wifi_cancel_retains_lock_until_worker_stops(self):
        plugin = wifi.Plugin()
        started, finish, ended = threading.Event(), threading.Event(), threading.Event()
        released_while_running = []
        def worker():
            started.set()
            finish.wait(timeout=5)
            ended.set()
            return {"success": True}
        def unlock(handle):
            released_while_running.append(started.is_set() and not ended.is_set())
        async def scenario():
            task = asyncio.create_task(plugin.rescan_and_reconnect())
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(started.is_set())
            try:
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                self.assertEqual(released_while_running, [])
                finish.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                finish.set()
        with patch.object(plugin, "_acquire_band_policy_file_lock", return_value=object()), \
             patch.object(plugin, "_release_band_policy_file_lock", side_effect=unlock), \
             patch.object(plugin, "_rescan_and_reconnect_sync", side_effect=worker):
            asyncio.run(scenario())
        self.assertEqual(released_while_running, [False])

    def test_A10_remap_malformed_action_uses_default(self):
        for invalid in ([], {}, None, 3, False):
            self.assertEqual(remap._sanitize_state({"desktop_action": invalid})["desktop_action"],
                             remap.DEFAULT_ACTIONS["desktop"])

    def test_A12_tdp_offline_start_preserves_saved_profiles(self):
        state = {"enabled": True, "extras_unlocked": True,
                 "spl": 40000, "sppt": 45000, "fppt": 50000}
        profiles = {"123": {"spl": 40000, "sppt": 45000, "fppt": 50000}}
        caps = {k: {"min": 5, "max": v} for k,v in (("spl",35),("sppt",37),("fppt",45))}
        plugin = tdp.Plugin()
        async def start():
            await plugin._main()
            await asyncio.gather(*plugin._tasks)
        with patch.object(tdp.updater, "ssl_context"), \
             patch.object(tdp, "_get_ac_online", return_value=False), \
             patch.object(tdp, "_wmi_caps", return_value=caps), \
             patch.object(tdp, "_wmi_only", return_value=False), \
             patch.object(tdp, "_ryzenadj_available", True), \
             patch.object(tdp, "_ensure_ryzenadj", side_effect=OSError("network unavailable")), \
             patch.object(tdp, "_load_settings", return_value=state), \
             patch.object(tdp, "_load_profiles", return_value=profiles), \
             patch.object(tdp, "_write_keys") as write, \
             patch.object(tdp, "_startup_context_target", return_value=("",(35000,37000,45000),False)), \
             patch.object(tdp, "_apply_limits_with_saved_cpu_power", return_value={"success":True}), \
             patch.object(tdp, "_save_active"), \
             patch.object(plugin, "_enforce_loop", new_callable=AsyncMock), \
             patch.object(plugin, "_info_loop", new_callable=AsyncMock):
            asyncio.run(start())
        write.assert_not_called()
        self.assertTrue(state["extras_unlocked"])
        self.assertEqual(profiles["123"]["spl"], 40000)



class RecoveryTests(unittest.TestCase):
    def test_journal_survives_runtime_directory_loss(self):
        import shutil
        with tempfile.TemporaryDirectory() as raw:
            durable = Path(raw) / "state"
            runtime = Path(raw) / "run"
            runtime.mkdir()
            journal = {"schema": 1, "phase": "prepared", "saved_bssid": ""}
            target = str(durable / "band-policy-transaction.json")
            with patch.object(wifi, "BAND_POLICY_STATE_DIR", str(durable)), \
                 patch.object(wifi, "BAND_POLICY_RUNTIME_DIR", str(runtime)), \
                 patch.object(wifi, "BAND_POLICY_JOURNAL_FILE", target):
                wifi.Plugin()._save_band_policy_journal(journal)
                shutil.rmtree(runtime)
                self.assertEqual(wifi.Plugin()._load_band_policy_journal(), journal)
                wifi.Plugin()._remove_band_policy_journal()
                self.assertFalse(Path(target).exists())

    def test_legacy_journal_migrates_before_removal(self):
        from safe_settings import atomic_write_json
        with tempfile.TemporaryDirectory() as raw:
            durable, runtime = Path(raw)/"state", Path(raw)/"run"
            runtime.mkdir()
            leaf = "band-policy-transaction.json"
            journal = {"schema": 1, "phase": "prepared"}
            atomic_write_json(str(runtime/leaf), journal)
            with patch.object(wifi, "BAND_POLICY_STATE_DIR", str(durable)), \
                 patch.object(wifi, "BAND_POLICY_RUNTIME_DIR", str(runtime)), \
                 patch.object(wifi, "BAND_POLICY_JOURNAL_FILE", str(durable/leaf)):
                self.assertEqual(wifi.Plugin()._load_band_policy_journal(), journal)
            self.assertTrue((durable/leaf).is_file())
            self.assertFalse((runtime/leaf).exists())

    def test_remap_restore_preserves_unmanaged_and_externally_changed_buttons(self):
        state = remap._sanitize_state({"enabled": True, "baseline_profile": BASE_PROFILE,
                                       "desktop_action": "disabled", "page_action": "disabled"})
        current = remap._build_profile(BASE_PROFILE, state).replace("LeftPaddle1", "RightPaddle1")
        current = current.replace(remap._render_mapping("desktop", "disabled"),
                                  remap._render_mapping("desktop", "f5"))
        with patch.object(remap, "_find_device", return_value=("device", "Go2")), \
             patch.object(remap, "_get_profile", return_value=current), \
             patch.object(remap, "_load_profile", side_effect=lambda p, text: text) as load:
            self.assertTrue(remap._restore_if_owned(state))
        restored = load.call_args.args[1]
        self.assertIn("RightPaddle1", restored)
        self.assertIn("KeyF5", restored)
        self.assertIn("Screenshot", restored)

    def test_remap_repair_merges_live_unmanaged_mappings(self):
        state = remap._sanitize_state({"enabled": True, "baseline_profile": BASE_PROFILE})
        current = remap._build_profile(BASE_PROFILE, state).replace("LeftPaddle1", "RightPaddle1")
        current = current.replace("button: Screenshot", "button: South")
        with patch.object(remap, "_find_device", return_value=("device", "Go2")), \
             patch.object(remap, "_get_profile", return_value=current), \
             patch.object(remap, "_load_profile", side_effect=lambda p, text: text) as load:
            remap._apply_state(state, adopt_external=False)
        self.assertIn("RightPaddle1", load.call_args.args[1])
        self.assertTrue(remap._profile_matches(load.call_args.args[1], state))

    def test_tdp_transition_backoff_and_new_context(self):
        with patch.object(tdp, "_transition_key", None), \
             patch.object(tdp.time, "monotonic", return_value=100), \
             patch.object(tdp, "_apply_and_record", return_value={"success": False}) as apply:
            key = ("123", False, (15,18,25))
            tdp._transition_apply(key[2], "test", key)
            tdp._transition_apply(key[2], "test", key)
            self.assertEqual(apply.call_count, 1)
            for _ in range(2):
                tdp._transition_retry_at = 0
                tdp._transition_apply(key[2], "test", key)
            self.assertEqual(tdp._transition_retry_at, 160)
            tdp._transition_apply(key[2], "new game", ("456", False, key[2]))
            self.assertEqual(apply.call_count, 4)

    def test_vibration_overwrite_change_rejects_queued_edit(self):
        profiles = {key: {"overwrite": False, "settings": dict(vibe.DEFAULT_PROFILE)}
                    for key in ("0", "111")}
        with patch.object(vibe, "_active_app_id", "111"), \
             patch.object(vibe, "_load_profiles", return_value=profiles), \
             patch.object(vibe, "_save_profiles") as save:
            result = asyncio.run(vibe.Plugin().set_intensity(3, "111", "111"))
            self.assertFalse(result["success"])
            save.assert_not_called()

    def test_tdp_profile_mutations_restore_hardware_after_commit_failure(self):
        before = {"enabled": True, "spl": 15000, "sppt": 18000, "fppt": 25000,
                  "active_spl": 25000, "active_sppt": 28000, "active_fppt": 35000,
                  "extras_unlocked": True}
        profiles = {"123": {"spl": 25000, "sppt": 28000, "fppt": 35000}}
        for method, args in (("delete_game_profile", ("123",)),
                             ("set_game_ac_profile", ("123", 20000, 23000, 30000, True)),
                             ("set_extras_unlocked", (False,))):
            with self.subTest(method=method):
                hardware = [25000,28000,35000]
                def apply(state, *values):
                    hardware[:] = values
                    return {"success": True}
                with patch.object(tdp, "_load_settings", side_effect=lambda: copy.deepcopy(before)), \
                     patch.object(tdp, "_load_profiles", side_effect=lambda: copy.deepcopy(profiles)), \
                     patch.object(tdp, "_get_running_appid", return_value="123"), \
                     patch.object(tdp, "_get_ac_online", return_value=True), \
                     patch.object(tdp, "_clamp_for_settings", side_effect=lambda s,*v: v), \
                     patch.object(tdp, "_apply_limits_with_saved_cpu_power", side_effect=apply), \
                     patch.object(tdp, "_write_keys", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(RuntimeError, "previous target restored"):
                        asyncio.run(getattr(tdp.Plugin(), method)(*args))
                self.assertEqual(hardware, [25000,28000,35000])

    def test_cancel_during_wifi_lock_acquisition_releases_obtained_handle(self):
        plugin = wifi.Plugin()
        started, finish = threading.Event(), threading.Event()
        handle = object()
        def acquire():
            started.set()
            finish.wait(3)
            return handle
        async def scenario():
            task = asyncio.create_task(plugin.rescan_and_reconnect())
            while not started.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with patch.object(plugin, "_acquire_band_policy_file_lock", side_effect=acquire), \
             patch.object(plugin, "_release_band_policy_file_lock") as release, \
             patch.object(plugin, "_rescan_and_reconnect_sync") as mutate:
            asyncio.run(scenario())
            release.assert_called_once_with(handle)
            mutate.assert_not_called()

    def test_rgb_settles_at_boot_then_returns_to_slow_checks(self):
        rgb = fixture.rgb_backend
        plugin = rgb.Plugin()
        plugin._settle_until = 30
        now = [0.0]
        ticks = iter((5, 10, 35, 65, 70))
        async def sleep(_delay):
            try:
                now[0] = next(ticks)
            except StopIteration:
                raise asyncio.CancelledError
        observed = []
        async def offload(fn):
            observed.append(now[0])
            return True
        with patch.object(rgb.asyncio, "sleep", side_effect=sleep), \
             patch.object(rgb.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(rgb, "_resume_detected", return_value=False), \
             patch.object(rgb, "_offload", side_effect=offload):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(plugin._drift_loop())
        self.assertEqual(observed, [5, 10, 70])

    def test_remap_settles_at_boot_then_returns_to_slow_checks(self):
        now = [0.0]
        ticks = iter((5,10,35,65,70))
        async def sleep(_):
            try:
                now[0] = next(ticks)
            except StopIteration:
                raise asyncio.CancelledError
        observed = []
        async def offload(fn):
            observed.append(now[0])
        with patch.object(remap.asyncio, "sleep", side_effect=sleep), \
             patch.object(remap.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(remap, "_resume_detected", return_value=False), \
             patch.object(remap.asyncio, "to_thread", side_effect=offload):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(remap._watch_loop())
        self.assertEqual(observed, [5,10,70])

    def test_remap_rpc_rejects_unhashable_arguments(self):
        plugin = remap.Plugin()
        with patch.object(plugin, "get_status", new_callable=AsyncMock, return_value={}):
            for button, action in (([], "default"), ("desktop", {})):
                self.assertFalse(asyncio.run(plugin.set_action(button,action))["success"])

    def test_script_cache_invalidates_after_replace(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)/"display.lua"
            path.write_bytes(b"first")
            actual = display._read_bytes
            with patch.object(display, "_read_bytes", wraps=actual) as read:
                self.assertEqual(display._read_script_bytes(str(path)), b"first")
                self.assertEqual(display._read_script_bytes(str(path)), b"first")
                self.assertEqual(read.call_count, 1)
                other = Path(raw)/"replacement"
                other.write_bytes(b"second")
                os.replace(other, path)
                self.assertEqual(display._read_script_bytes(str(path)), b"second")
                self.assertEqual(read.call_count, 2)

    @unittest.skipUnless(os.name == "posix", "Linux symlink and mode semantics")
    def test_display_writer_refuses_target_symlink_and_preserves_reader_access(self):
        with tempfile.TemporaryDirectory() as raw:
            victim, target = Path(raw)/"victim", Path(raw)/"script.lua"
            victim.write_bytes(b"keep")
            target.symlink_to(victim)
            self.assertFalse(display._write_file_atomically(str(target), b"new"))
            self.assertEqual(victim.read_bytes(), b"keep")
            target.unlink()
            self.assertTrue(display._write_file_atomically(str(target), b"lua"))
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main(verbosity=2)

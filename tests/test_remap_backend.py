# SPDX-License-Identifier: BSD-3-Clause

import asyncio
import copy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, mock_open, patch


if "decky" not in sys.modules:
    class _Logger:
        def info(self, _message): pass
        def warning(self, _message): pass
        def error(self, _message): pass
        def debug(self, _message): pass

    decky = types.ModuleType("decky")
    decky.logger = _Logger()
    decky.DECKY_PLUGIN_SETTINGS_DIR = tempfile.mkdtemp(prefix="lego-remap-settings-")
    sys.modules["decky"] = decky

import remap_backend


BASE_PROFILE = """version: 1
kind: DeviceProfile
name: Custom User Profile
description: keep this
mapping:
- name: LeftTop
  source_event:
    gamepad:
      button: LeftTop
  target_events:
  - gamepad:
      button: LeftPaddle1
- name: Keyboard
  source_event:
    gamepad:
      button: Keyboard
  target_events:
  - gamepad:
      button: Guide
  - gamepad:
      button: North
- name: QuickAccess2
  source_event:
    gamepad:
      button: QuickAccess2
  target_events:
  - gamepad:
      button: Screenshot
"""


class MemorySettings:
    def __init__(self, state=None):
        self.data = {"state": copy.deepcopy(state or remap_backend.DEFAULT_STATE)}

    def getSetting(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    def setSetting(self, key, value):
        self.data[key] = copy.deepcopy(value)

    def commit(self):
        pass

    def read(self):
        pass


class RemapBackendTests(unittest.TestCase):
    def test_default_baseline_without_final_newline_separates_appended_mapping(self):
        baseline = """version: 1
kind: DeviceProfile
name: Original
mapping:
- name: Desktop
  source_event:
    gamepad:
      button: Keyboard
  target_events:
  - keyboard: KeyF7"""
        state = remap_backend._sanitize_state({
            'enabled': True, 'baseline_profile': baseline,
            'desktop_action': 'default', 'page_action': 'f2',
        })
        generated = remap_backend._build_profile(baseline, state)
        self.assertIn('keyboard: KeyF7\n- name: Companion Page', generated)
        self.assertTrue(remap_backend._profile_matches(generated, state))
        self.assertEqual(len(remap_backend._mapping_blocks(generated)[1]), 2)

    def test_unfamiliar_default_mapping_requires_unchanged_complete_block(self):
        baseline = BASE_PROFILE.replace('button: Screenshot', 'axis: MouseWheel')
        state = remap_backend._sanitize_state({'enabled': True, 'baseline_profile': baseline})
        generated = remap_backend._build_profile(BASE_PROFILE, state)
        self.assertIn('axis: MouseWheel', generated)
        self.assertTrue(remap_backend._profile_matches(generated, state))
        self.assertFalse(remap_backend._profile_matches(generated.replace('axis: MouseWheel', 'axis: MouseMotion'), state))

    def setUp(self):
        watcher = patch.object(remap_backend, "_service_watch", remap_backend.ProcessWatch())
        watcher.start()
        self.addCleanup(watcher.stop)
        # Existing profile tests fake InputPlumber. Optional process metadata
        # must not query a developer's real system bus during those tests.
        capture = patch.object(remap_backend._service_watch, "capture")
        self.capture = capture.start()
        self.addCleanup(capture.stop)
        cached = patch.object(remap_backend, "_device_path", "")
        cached.start()
        self.addCleanup(cached.stop)

    def test_discovery_skips_foreign_capabilities_and_continues_after_property_errors(self):
        paths = [f"/org/shadowblip/InputPlumber/CompositeDevice{number}" for number in range(4)]
        required = ["Gamepad:Button:Keyboard", "Gamepad:Button:QuickAccess2"]

        def property_value(path, name):
            if path == paths[1] or (path == paths[2] and name == "Capabilities"):
                raise remap_backend.RemapError("Controller disconnected")
            if name == "Name":
                return "Another controller" if path == paths[0] else "Lenovo Legion Go 2"
            if path == paths[0]:
                self.fail("Capabilities must not be requested for an unrelated controller")
            return required

        identity = [mock_open(read_data=value).return_value for value in ("LENOVO", "83N0")]
        with patch("builtins.open", side_effect=identity), \
                patch.object(remap_backend, "_run_busctl", return_value="\n".join(reversed(paths))), \
                patch.object(remap_backend, "_get_property", side_effect=property_value) as read:
            self.assertEqual(remap_backend._find_device(), (paths[3], "Lenovo Legion Go 2"))
        self.assertEqual([item.args for item in read.call_args_list], [
            (paths[0], "Name"), (paths[1], "Name"),
            (paths[2], "Name"), (paths[2], "Capabilities"),
            (paths[3], "Name"), (paths[3], "Capabilities"),
        ])

    def test_discovery_reuses_a_verified_device_and_rescans_only_when_it_fails(self):
        first, second = (f"/org/shadowblip/InputPlumber/CompositeDevice{number}" for number in range(2))
        required = ["Gamepad:Button:Keyboard", "Gamepad:Button:QuickAccess2"]
        present = {first}
        tree = Mock(side_effect=lambda _args: "\n".join(sorted(present)))

        def property_value(path, name):
            if path not in present:
                raise remap_backend.RemapError("Unknown object '%s'" % path)
            return "Lenovo Legion Go 2" if name == "Name" else required

        with patch.object(remap_backend.module_runtime, "is_legion_go_2", return_value=True), \
                patch.object(remap_backend, "_run_busctl", tree), \
                patch.object(remap_backend, "_get_property", side_effect=property_value) as read:
            for _ in range(3):
                self.assertEqual(remap_backend._find_device(), (first, "Lenovo Legion Go 2"))
            self.assertEqual(tree.call_count, 1)
            self.assertEqual(read.call_count, 6)  # same Name/Capabilities checks on every call
            present = {second}  # InputPlumber re-enumerated the controller
            self.assertEqual(remap_backend._find_device(), (second, "Lenovo Legion Go 2"))
            self.assertEqual(tree.call_count, 2)
            self.assertEqual(remap_backend._find_device(), (second, "Lenovo Legion Go 2"))
            self.assertEqual(tree.call_count, 2)
            present = set()
            with self.assertRaisesRegex(remap_backend.RemapError, "not managing a compatible"):
                remap_backend._find_device()
            self.assertEqual(tree.call_count, 3)

    def test_discovery_does_not_retry_a_stalled_cached_device(self):
        path = "/org/shadowblip/InputPlumber/CompositeDevice0"
        with patch.object(remap_backend.module_runtime, "is_legion_go_2", return_value=True), \
                patch.object(remap_backend, "_device_path", path), \
                patch.object(remap_backend, "_run_busctl", return_value=path) as tree, \
                patch.object(remap_backend, "_get_property", side_effect=remap_backend.RemapError("Connection timed out")) as read:
            with self.assertRaisesRegex(remap_backend.RemapError, "timed out"):
                remap_backend._find_device()
        tree.assert_called_once()
        read.assert_called_once()

    def test_retry_reads_a_repaired_file_after_unrecoverable_storage_error(self):
        from safe_settings import SettingsManager, atomic_write_json
        with tempfile.TemporaryDirectory(prefix="lego-remap-recovery-") as raw:
            path = Path(raw, "remap_settings.json")
            path.write_text('{"state":', encoding="utf-8")
            store = SettingsManager("remap_settings", raw)
            self.assertTrue(store.recovery_error)
            path.unlink()
            with patch.object(remap_backend, "settings", store):
                with self.assertRaises(remap_backend.RemapError):
                    remap_backend._load_state()
            recovered = {**remap_backend.DEFAULT_STATE, "desktop_action": "f4"}
            atomic_write_json(str(path), {"state": recovered})
            with patch.object(remap_backend, "settings", store):
                self.assertEqual(remap_backend._load_state()["desktop_action"], "f4")
            self.assertFalse(store.recovery_error)

    def test_missing_ownership_after_storage_corruption_is_not_a_successful_release(self):
        memory = MemorySettings()
        memory.recovery_error = "Primary and backup settings are corrupt"
        with patch.object(remap_backend, "settings", memory), patch.object(remap_backend, "_find_device") as discover:
            with self.assertRaises(remap_backend.RemapError):
                remap_backend._mutate_sync({"enabled": False})
        discover.assert_not_called()

    def test_failed_action_change_can_still_restore_the_last_applied_mapping(self):
        initial = remap_backend._sanitize_state({
            "enabled": True, "desktop_action": "f1", "page_action": "default",
            "baseline_profile": BASE_PROFILE,
        })
        memory = MemorySettings(initial)
        hardware = [remap_backend._build_profile(BASE_PROFILE, initial)]

        def load(_path, profile):
            hardware[0] = profile
            return profile

        with (
            patch.object(remap_backend, "settings", memory),
            patch.object(remap_backend, "_find_device", return_value=("/org/test", "Lenovo Legion Go 2")),
            patch.object(remap_backend, "_get_profile", side_effect=lambda _: hardware[0]),
            patch.object(remap_backend, "_status_sync", return_value={}),
        ):
            with patch.object(remap_backend, "_load_profile", side_effect=remap_backend.RemapError("D-Bus temporarily unavailable")):
                for action in ("f2", "f3"):
                    with self.assertRaises(remap_backend.RemapError):
                        remap_backend._mutate_sync({"desktop_action": action})
            self.assertEqual(memory.data["state"]["desktop_action"], "f3")
            self.assertIn("keyboard: KeyF1", hardware[0])
            # A new process has only the persisted transition, not local state.
            memory.data["state"] = remap_backend._sanitize_state(copy.deepcopy(memory.data["state"]))
            with patch.object(remap_backend, "_load_profile", side_effect=load):
                remap_backend._mutate_sync({"enabled": False})
        self.assertEqual(hardware[0], BASE_PROFILE)
        self.assertFalse(memory.data["state"]["enabled"])
        self.assertEqual(memory.data["state"]["desktop_action"], "f3")

    def test_unconfirmed_applied_change_restores_our_button_and_preserves_external_mapping(self):
        initial = remap_backend._sanitize_state({
            "enabled": True, "desktop_action": "f1", "page_action": "default",
            "baseline_profile": BASE_PROFILE,
        })
        memory = MemorySettings(initial)
        hardware = [remap_backend._build_profile(BASE_PROFILE, initial)]

        def load(_path, profile):
            hardware[0] = profile
            return profile

        def unconfirmed(_path, profile):
            hardware[0] = profile
            raise remap_backend.RemapError("The readback reply was lost")

        with (
            patch.object(remap_backend, "settings", memory),
            patch.object(remap_backend, "_find_device", return_value=("/org/test", "Lenovo Legion Go 2")),
            patch.object(remap_backend, "_get_profile", side_effect=lambda _: hardware[0]),
            patch.object(remap_backend, "_status_sync", return_value={}),
        ):
            with patch.object(remap_backend, "_load_profile", side_effect=unconfirmed):
                with self.assertRaises(remap_backend.RemapError):
                    remap_backend._mutate_sync({"desktop_action": "f2"})
            hardware[0] = hardware[0].replace("button: LeftPaddle1", "button: RightPaddle2")
            hardware[0] = hardware[0].replace(
                remap_backend._expected_mapping(initial, "page"),
                remap_backend._render_mapping("page", "f12"),
            )
            with patch.object(remap_backend, "_load_profile", side_effect=load):
                remap_backend._mutate_sync({"enabled": False})
        self.assertNotIn("keyboard: KeyF2", hardware[0])
        self.assertIn("keyboard: KeyF12", hardware[0])
        self.assertIn("button: RightPaddle2", hardware[0])
        self.assertIn("- name: Keyboard", hardware[0])
        self.assertIsNone(memory.data["state"]["previous_actions"])

    def test_retry_commits_saved_intent_and_discards_previous_actions_after_confirmation(self):
        initial = remap_backend._sanitize_state({
            "enabled": True, "desktop_action": "f2", "page_action": "default",
            "baseline_profile": BASE_PROFILE,
            "previous_actions": {"desktop": "f1", "page": "default"},
        })
        memory = MemorySettings(initial)
        hardware = [remap_backend._build_profile(BASE_PROFILE, {**initial, "desktop_action": "f1"})]

        def load(_path, profile):
            hardware[0] = profile
            return profile

        with (
            patch.object(remap_backend, "settings", memory),
            patch.object(remap_backend, "_find_device", return_value=("/org/test", "Lenovo Legion Go 2")),
            patch.object(remap_backend, "_get_profile", side_effect=lambda _: hardware[0]),
            patch.object(remap_backend, "_load_profile", side_effect=load),
        ):
            remap_backend._repair_sync()
        self.assertIn("keyboard: KeyF2", hardware[0])
        self.assertTrue(memory.data["state"]["enabled"])
        self.assertIsNone(memory.data["state"]["previous_actions"])

    def test_build_replaces_only_two_dedicated_mappings(self):
        state = remap_backend._sanitize_state({
            "enabled": True,
            "desktop_action": "show_desktop",
            "page_action": "quick_access",
            "baseline_profile": BASE_PROFILE,
        })
        result = remap_backend._build_profile(BASE_PROFILE, state)
        self.assertIn("description: keep this", result)
        self.assertIn("button: LeftPaddle1", result)
        self.assertIn("keyboard: KeyLeftMeta", result)
        self.assertIn("keyboard: KeyD", result)
        self.assertIn("button: QuickAccess", result)
        self.assertNotIn("button: Screenshot", result)
        self.assertTrue(remap_backend._profile_matches(result, state))

    def test_missing_dedicated_mappings_are_added(self):
        base = """version: 1
kind: DeviceProfile
name: Minimal
mapping:
- name: LeftTop
  source_event:
    gamepad:
      button: LeftTop
  target_events:
  - gamepad:
      button: LeftPaddle1
"""
        state = remap_backend._sanitize_state({
            "enabled": True,
            "baseline_profile": base,
            "desktop_action": "escape",
            "page_action": "disabled",
        })
        result = remap_backend._build_profile(base, state)
        self.assertTrue(remap_backend._profile_matches(result, state))
        self.assertEqual(result.count("button: Keyboard"), 1)
        self.assertEqual(result.count("button: QuickAccess2"), 1)
        # The matcher shares the action table with the builder. Check literal
        # target events too, so a wrong action cannot validate its own output.
        events = {remap_backend._source_for_block(block): remap_backend._events_for_block(block)
                  for block in remap_backend._mapping_blocks(result)[1]}
        self.assertEqual(events["Keyboard"], [("keyboard", "KeyEsc")])
        self.assertEqual(events["QuickAccess2"], [])

    def test_default_restores_custom_baseline_and_preserves_unrelated_live_edits(self):
        baseline = BASE_PROFILE.replace(
            "  - gamepad:\n      button: Guide\n  - gamepad:\n      button: North\n",
            "  - keyboard: KeyF7\n",
        ).replace("  - gamepad:\n      button: Screenshot\n", "  - keyboard: KeyF8\n")
        state = remap_backend._sanitize_state({"enabled": True, "baseline_profile": baseline,
            "desktop_action": "escape", "page_action": "f2"})
        live = remap_backend._build_profile(baseline, state).replace("LeftPaddle1", "RightPaddle2")
        state["desktop_action"] = "default"
        restored = remap_backend._build_profile(live, state)
        self.assertIn("keyboard: KeyF7", restored)
        self.assertIn("keyboard: KeyF2", restored)
        self.assertIn("button: RightPaddle2", restored)
        self.assertNotIn("button: Guide", restored)
        self.assertTrue(remap_backend._profile_matches(restored, state))
        state["page_action"] = "default"
        restored = remap_backend._build_profile(restored, state)
        self.assertIn("keyboard: KeyF8", restored)
        self.assertTrue(remap_backend._profile_matches(restored, state))
        self.assertFalse(remap_backend._profile_matches(restored.replace("KeyF8", "KeyF9"), state))

    def test_default_preserves_absent_baseline_mapping_after_custom_action(self):
        header, blocks = remap_backend._mapping_blocks(BASE_PROFILE)
        baseline = header + "".join(block for block in blocks if not remap_backend._source_for_block(block))
        state = remap_backend._sanitize_state({"enabled": True, "baseline_profile": baseline,
            "desktop_action": "f1", "page_action": "default"})
        live = remap_backend._build_profile(baseline, state)
        self.assertNotIn("button: QuickAccess2", live)
        state["desktop_action"] = "default"
        restored = remap_backend._build_profile(live, state)
        self.assertNotIn("button: Keyboard", restored)
        self.assertTrue(remap_backend._profile_matches(restored, state))

    def test_disabled_accepts_inputplumber_empty_list_serialization(self):
        state = remap_backend._sanitize_state({
            "enabled": True,
            "baseline_profile": BASE_PROFILE,
            "desktop_action": "disabled",
            "page_action": "disabled",
        })
        result = remap_backend._build_profile(BASE_PROFILE, state)
        canonical = result.replace("  target_events:\n- name: Companion Page", "  target_events: []\n- name: Companion Page")
        canonical = canonical.replace("  target_events:\n", "  target_events: []\n", 1)
        self.assertTrue(remap_backend._profile_matches(canonical, state))

    def test_a_valid_empty_profile_is_taken_over_and_restored_as_an_empty_list(self):
        empty = "version: 1\nkind: DeviceProfile\nname: Passthrough\nmapping: []\n"
        header, blocks = remap_backend._mapping_blocks(empty)
        self.assertEqual((header.splitlines()[-1], blocks), ("mapping:", []))
        state = remap_backend._sanitize_state({"enabled": True, "baseline_profile": empty,
            "desktop_action": "insert", "page_action": "default"})
        live = remap_backend._build_profile(empty, state)
        self.assertTrue(remap_backend._valid_profile(live))
        self.assertTrue(remap_backend._profile_matches(live, state))
        self.assertIn("mapping:\n- name:", live)
        self.assertEqual(remap_backend._join_profile(header, []), empty)
        for broken in (empty + "extra: 1\n", empty.replace("mapping: []", "mapping: [x]")):
            with self.subTest(broken=broken), self.assertRaises(remap_backend.RemapError):
                remap_backend._mapping_blocks(broken)

    def test_duplicate_source_mapping_fails_closed(self):
        duplicate = BASE_PROFILE + remap_backend._render_mapping("desktop", "escape")
        state = remap_backend._sanitize_state({
            "enabled": True,
            "baseline_profile": duplicate,
        })
        with self.assertRaises(remap_backend.RemapError):
            remap_backend._build_profile(duplicate, state)

    def test_invalid_persisted_baseline_disables_ownership(self):
        state = remap_backend._sanitize_state({
            "enabled": True,
            "baseline_profile": "not yaml",
            "desktop_action": "escape",
        })
        self.assertFalse(state["enabled"])
        self.assertIsNone(state["baseline_profile"])
        self.assertEqual(state["desktop_action"], "escape")

    def test_enable_captures_profile_before_applying(self):
        memory = MemorySettings()
        hardware = [BASE_PROFILE]

        def load(_path, profile):
            hardware[0] = profile
            return profile

        with (
            patch.object(remap_backend, "settings", memory),
            patch.object(remap_backend, "_inputplumber_version", return_value="test"),
            patch.object(remap_backend, "_find_device", return_value=("/org/test", "Lenovo Legion Go 2")),
            patch.object(remap_backend, "_get_profile", side_effect=lambda _: hardware[0]),
            patch.object(remap_backend, "_load_profile", side_effect=load),
        ):
            status = remap_backend._mutate_sync({"enabled": True})
        self.assertTrue(status["enabled"])
        self.assertNotEqual(hardware[0], BASE_PROFILE)
        self.assertEqual(memory.data["state"]["baseline_profile"], BASE_PROFILE)
        self.assertTrue(memory.data["state"]["enabled"])

    def test_disable_restores_only_when_companion_owns_profile(self):
        owned = remap_backend._sanitize_state({
            "enabled": True,
            "baseline_profile": BASE_PROFILE,
        })
        memory = MemorySettings(owned)
        hardware = [remap_backend._build_profile(BASE_PROFILE, owned)]

        def load(_path, profile):
            hardware[0] = profile
            return profile

        with (
            patch.object(remap_backend, "settings", memory),
            patch.object(remap_backend, "_inputplumber_version", return_value="test"),
            patch.object(remap_backend, "_find_device", return_value=("/org/test", "Lenovo Legion Go 2")),
            patch.object(remap_backend, "_get_profile", side_effect=lambda _: hardware[0]),
            patch.object(remap_backend, "_load_profile", side_effect=load),
        ):
            result = remap_backend._mutate_sync({"enabled": False})
        self.assertFalse(result["enabled"])
        self.assertEqual(hardware[0], BASE_PROFILE)
        self.assertFalse(memory.data["state"]["enabled"])
        self.assertIsNone(memory.data["state"]["baseline_profile"])

    def test_rpc_rejects_unknown_action_without_mutation(self):
        plugin = remap_backend.Plugin()
        with patch.object(plugin, "get_status", return_value={"enabled": False}):
            result = asyncio.run(plugin.set_action("desktop", "shell-command"))
        self.assertFalse(result["success"])

    def test_function_key_actions_cover_f1_through_f12(self):
        for number in range(1, 13):
            action = f"f{number}"
            with self.subTest(action=action):
                self.assertIn(action, remap_backend.ACTION_LABELS)
                self.assertEqual(
                    remap_backend._action_events("desktop", action),
                    [("keyboard", f"KeyF{number}")],
                )

    def test_insert_and_home_use_inputplumber_keyboard_events(self):
        self.assertEqual(
            remap_backend._action_events("desktop", "insert"),
            [("keyboard", "KeyInsert")],
        )
        self.assertEqual(
            remap_backend._action_events("page", "home"),
            [("keyboard", "KeyHome")],
        )

    def test_successful_repair_captures_process_but_status_and_failed_repair_do_not(self):
        state = remap_backend._sanitize_state({"enabled": True, "baseline_profile": BASE_PROFILE})
        with patch.object(remap_backend, "settings", MemorySettings(state)), \
                patch.object(remap_backend, "_apply_state", return_value=(state, BASE_PROFILE)):
            self.assertTrue(remap_backend._repair_sync())
        self.capture.assert_called_once()
        with patch.object(remap_backend, "settings", MemorySettings(state)), \
                patch.object(remap_backend, "_inputplumber_version", return_value="test"), \
                patch.object(remap_backend, "_find_device", return_value=("device", "Go2")), \
                patch.object(remap_backend, "_get_profile", return_value=BASE_PROFILE):
            remap_backend._status_sync()
        self.capture.assert_called_once()
        with patch.object(remap_backend, "settings", MemorySettings(state)), \
                patch.object(remap_backend, "_apply_state", side_effect=remap_backend.RemapError("unavailable")):
            self.assertFalse(remap_backend._repair_sync())
        self.capture.assert_called_once()

    def test_disabled_remapper_clears_process_tracking_without_service_lookups(self):
        with patch.object(remap_backend, "settings", MemorySettings()), \
                patch.object(remap_backend, "_json_value") as query, \
                patch.object(remap_backend._service_watch, "clear") as clear:
            self.assertTrue(remap_backend._repair_sync())
        clear.assert_called_once()
        self.capture.assert_not_called()
        query.assert_not_called()

    def test_restart_hint_advances_repair_once_and_preserves_failure_backoff(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                clock, calls, hint = [0.0], [], [None]

                async def sleep(seconds):
                    self.assertGreater(seconds, 0, "the monitor must not busy-loop")
                    self.assertLessEqual(seconds, 10, "restart detection must stay prompt")
                    clock[0] += seconds
                    if clock[0] > 180:
                        raise asyncio.CancelledError

                def consume_exit():
                    if clock[0] >= 40 and hint[0] is None:
                        hint[0] = clock[0]
                        return True
                    return False

                async def repair(_module, _function):
                    calls.append(clock[0])
                    return not failure or clock[0] < 40

                async def watch():
                    with self.assertRaises(asyncio.CancelledError):
                        await remap_backend._watch_loop()

                with patch.object(remap_backend, "time", types.SimpleNamespace(monotonic=lambda: clock[0])), \
                        patch.object(remap_backend.asyncio, "sleep", side_effect=sleep), \
                        patch.object(remap_backend, "_resume_detected", return_value=False), \
                        patch.object(remap_backend._service_watch, "consume_exit", side_effect=consume_exit), \
                        patch.object(remap_backend.module_runtime, "offload", side_effect=repair), \
                        patch.object(remap_backend, "_json_value") as query:
                    asyncio.run(watch())
                boot = [at for at in calls if at < hint[0]]
                recovery = [at for at in calls if at >= hint[0]]
                self.assertTrue(boot, "initial discovery must not wait for a restart")
                self.assertLessEqual(boot[0], 10)
                self.assertLessEqual(len(boot), 7, "initial discovery must remain bounded")
                self.assertGreaterEqual(len(recovery), 3)
                self.assertLessEqual(recovery[0] - hint[0], 10)
                gaps = [b - a for a, b in zip(recovery, recovery[1:])]
                self.assertTrue(all(0 < gap <= 75 for gap in gaps), recovery)
                if failure:
                    self.assertLessEqual(len(recovery), 8)
                    self.assertLessEqual(gaps[0], 15)
                    # Permit bounded jitter while retaining a slow late retry
                    # rate and the overall attempt budget above.
                    self.assertGreaterEqual(sum(gaps[-2:]), 60, "late retries must remain low-frequency")
                    self.assertGreaterEqual(gaps[-1], 30)
                else:
                    self.assertTrue(all(gap >= 45 for gap in gaps), recovery)
                query.assert_not_called()


if __name__ == "__main__":
    unittest.main()

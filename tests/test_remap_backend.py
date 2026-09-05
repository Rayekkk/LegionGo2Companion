# SPDX-License-Identifier: BSD-3-Clause

import asyncio
import copy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


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
                remap_backend._render_mapping("page", "default"),
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
        applied = {}

        def fake_apply(state, adopt_external):
            self.assertFalse(adopt_external)
            applied["state"] = copy.deepcopy(state)
            return state, remap_backend._build_profile(BASE_PROFILE, state)

        with (
            patch.object(remap_backend, "settings", memory),
            patch.object(remap_backend, "_find_device", return_value=("/org/test", "Lenovo Legion Go 2")),
            patch.object(remap_backend, "_get_profile", return_value=BASE_PROFILE),
            patch.object(remap_backend, "_apply_state", side_effect=fake_apply),
            patch.object(remap_backend, "_status_sync", return_value={"enabled": True}),
        ):
            status = remap_backend._mutate_sync({"enabled": True})
        self.assertTrue(status["enabled"])
        self.assertEqual(applied["state"]["baseline_profile"], BASE_PROFILE)
        self.assertTrue(memory.data["state"]["enabled"])

    def test_disable_restores_only_when_companion_owns_profile(self):
        owned = remap_backend._sanitize_state({
            "enabled": True,
            "baseline_profile": BASE_PROFILE,
        })
        memory = MemorySettings(owned)
        with (
            patch.object(remap_backend, "settings", memory),
            patch.object(remap_backend, "_restore_if_owned", return_value=True) as restore,
            patch.object(remap_backend, "_status_sync", return_value={"enabled": False}),
        ):
            result = remap_backend._mutate_sync({"enabled": False})
        self.assertFalse(result["enabled"])
        restore.assert_called_once()
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

    def test_resume_detector_uses_boottime_gap(self):
        remap_backend._last_suspend_offset = 3.0
        with patch.object(remap_backend, "_suspend_offset", return_value=4.2):
            self.assertTrue(remap_backend._resume_detected())
        with patch.object(remap_backend, "_suspend_offset", return_value=4.3):
            self.assertFalse(remap_backend._resume_detected())


if __name__ == "__main__":
    unittest.main()

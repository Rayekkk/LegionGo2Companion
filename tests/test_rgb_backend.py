# SPDX-License-Identifier: BSD-3-Clause

import asyncio
import copy
import hashlib
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
    decky.DECKY_PLUGIN_SETTINGS_DIR = tempfile.mkdtemp(prefix="lego-rgb-settings-")
    sys.modules["decky"] = decky

import rgb_backend


class MemorySettings:
    def __init__(self, state=None):
        self.path = "/nonexistent/rgb_settings.json"
        self.data = {"state": copy.deepcopy(state or rgb_backend.DEFAULT_STATE)}

    def read(self):
        pass

    def getSetting(self, key, default=None):
        return self.data.get(key, default)

    def setSetting(self, key, value):
        self.data[key] = copy.deepcopy(value)

    def commit(self):
        pass


def make_led(root: Path, *, enabled="false") -> None:
    values = {
        "enabled": enabled,
        "enabled_index": "true false",
        "profile": "3",
        "profile_range": "1-3",
        "mode": "custom",
        "mode_index": "dynamic custom",
        "effect": "monocolor",
        "effect_index": "monocolor breathe chroma rainbow",
        "brightness": "20",
        "max_brightness": "100",
        "multi_intensity": "0 100 100",
        "multi_max_intensity": "100 100 100",
        "speed": "50",
        "speed_range": "0-100",
    }
    for name, value in values.items():
        (root / name).write_text(value + "\n", encoding="ascii")


class RgbBackendTests(unittest.TestCase):
    def setUp(self):
        rgb_backend._power_capability_cache = None
        rgb_backend._last_error = ""

    def test_state_sanitizer_fails_closed_without_restore_data(self):
        state = rgb_backend._sanitize_state({
            "control_enabled": True,
            "power_led_managed": True,
            "effect": "not-real",
            "hue": 999,
            "saturation": -20,
        })
        self.assertFalse(state["control_enabled"])
        self.assertFalse(state["power_led_managed"])
        self.assertEqual(state["effect"], "monocolor")
        self.assertEqual(state["hue"], 359)
        self.assertEqual(state["saturation"], 0)

    def test_hardware_target_uses_one_firmware_profile(self):
        with tempfile.TemporaryDirectory(prefix="lego-rgb-") as raw:
            led = Path(raw)
            make_led(led)
            state = rgb_backend._sanitize_state({
                "control_enabled": False,
                "rings_enabled": True,
                "effect": "breathe",
                "hue": 0,
                "saturation": 100,
                "brightness": 25,
                "speed": 100,
            })
            state["control_enabled"] = True
            state["original_rgb"] = rgb_backend._read_rgb_snapshot(str(led))
            with patch.object(rgb_backend, "_rgb_capability", return_value=(str(led), "")):
                self.assertTrue(rgb_backend._apply_rgb_target(state))
            self.assertEqual((led / "profile").read_text().strip(), "3")
            self.assertEqual((led / "mode").read_text().strip(), "custom")
            self.assertEqual((led / "effect").read_text().strip(), "breathe")
            self.assertEqual((led / "multi_intensity").read_text().strip(), "100 0 0")
            self.assertEqual((led / "brightness").read_text().strip(), "25")
            self.assertEqual((led / "speed").read_text().strip(), "100")
            self.assertEqual((led / "enabled").read_text().strip(), "true")

    def test_enabling_control_captures_a_reversible_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="lego-rgb-") as raw:
            led = Path(raw)
            make_led(led, enabled="false")
            memory = MemorySettings()
            plugin = rgb_backend.Plugin()
            with (
                patch.object(rgb_backend, "settings", memory),
                patch.object(rgb_backend, "_rgb_capability", return_value=(str(led), "")),
                patch.object(rgb_backend, "_power_capability", return_value=(False, "test")),
            ):
                result = asyncio.run(plugin.set_control_enabled(True))
                self.assertTrue(result["success"], result.get("error"))
                saved = memory.data["state"]
                self.assertTrue(saved["control_enabled"])
                self.assertIsNotNone(saved["original_rgb"])
                self.assertIsNone(saved["pending"])

                result = asyncio.run(plugin.set_control_enabled(False))
                self.assertTrue(result["success"], result.get("error"))
                self.assertFalse(memory.data["state"]["control_enabled"])
                self.assertEqual((led / "enabled").read_text().strip(), "false")

    def test_untouched_blank_profile_restores_without_selecting_a_profile(self):
        with tempfile.TemporaryDirectory(prefix="lego-rgb-") as raw:
            led = Path(raw)
            make_led(led, enabled="false")
            (led / "profile").write_text("\n", encoding="ascii")
            memory = MemorySettings()
            plugin = rgb_backend.Plugin()
            with (
                patch.object(rgb_backend, "settings", memory),
                patch.object(rgb_backend, "_rgb_capability", return_value=(str(led), "")),
                patch.object(rgb_backend, "_power_capability", return_value=(False, "test")),
            ):
                result = asyncio.run(plugin.set_control_enabled(True))
                self.assertTrue(result["success"], result.get("error"))

                result = asyncio.run(plugin.set_control_enabled(False))
                self.assertTrue(result["success"], result.get("error"))
                self.assertEqual((led / "profile").read_text(), "\n")
                self.assertEqual((led / "enabled").read_text().strip(), "false")

    def test_interrupted_transaction_restores_the_previous_hardware_state(self):
        with tempfile.TemporaryDirectory(prefix="lego-rgb-") as raw:
            led = Path(raw)
            make_led(led, enabled="false")
            before = rgb_backend._read_rgb_snapshot(str(led))
            previous = rgb_backend._sanitize_state(rgb_backend.DEFAULT_STATE)
            pending_state = copy.deepcopy(previous)
            pending_state["pending"] = {
                "previous": previous,
                "before_rgb": before,
                "before_power": None,
            }
            memory = MemorySettings(pending_state)
            (led / "enabled").write_text("true\n", encoding="ascii")
            (led / "brightness").write_text("99\n", encoding="ascii")
            with (
                patch.object(rgb_backend, "settings", memory),
                patch.object(rgb_backend, "_rgb_capability", return_value=(str(led), "")),
            ):
                self.assertTrue(rgb_backend._recover_pending())
            self.assertEqual((led / "enabled").read_text().strip(), "false")
            self.assertEqual((led / "brightness").read_text().strip(), "20")
            self.assertIsNone(memory.data["state"]["pending"])

    def test_power_led_write_changes_only_the_audited_bit(self):
        class Register:
            def __init__(self, value):
                self.value = value

            def __getitem__(self, _index):
                return self.value

            def __setitem__(self, _index, value):
                self.value = value

        register = Register(0b10100101)

        def fake_map(_write, operation):
            return operation(register, 0)

        with (
            patch.object(rgb_backend, "_power_capability", return_value=(True, "")),
            patch.object(rgb_backend, "_with_power_lock", side_effect=lambda operation: operation()),
            patch.object(rgb_backend, "_map_power_register", side_effect=fake_map),
        ):
            self.assertTrue(rgb_backend._write_power_led(True))
            self.assertEqual(register.value, 0b10100101 & ~(1 << 6))
            preserved = register.value & ~(1 << 6)
            self.assertTrue(rgb_backend._write_power_led(False))
            self.assertEqual(register.value & ~(1 << 6), preserved)
            self.assertTrue(register.value & (1 << 6))

    def test_power_capability_requires_exact_dmi_bios_and_dsdt(self):
        with tempfile.TemporaryDirectory(prefix="lego-dsdt-") as raw:
            root = Path(raw)
            dsdt = root / "DSDT"
            devmem = root / "mem"
            dsdt.write_bytes(b"audited dsdt")
            devmem.write_bytes(bytes(4096))
            digest = hashlib.sha256(dsdt.read_bytes()).hexdigest()
            identity = {
                "product_family": rgb_backend.EXPECTED_FAMILY,
                "product_name": rgb_backend.EXPECTED_PRODUCT,
                "board_name": rgb_backend.EXPECTED_BOARD,
                "bios_version": rgb_backend.AUDITED_BIOS,
            }
            with (
                patch.object(rgb_backend, "_read_identity", return_value=identity),
                patch.object(rgb_backend, "DSDT_PATH", str(dsdt)),
                patch.object(rgb_backend, "DEV_MEM_PATH", str(devmem)),
                patch.object(rgb_backend, "AUDITED_DSDT_SHA256", {digest}),
            ):
                rgb_backend._power_capability_cache = None
                self.assertEqual(rgb_backend._power_capability(), (True, ""))
                rgb_backend._power_capability_cache = None
                identity["bios_version"] = "future"
                supported, _ = rgb_backend._power_capability()
                self.assertFalse(supported)


if __name__ == "__main__":
    unittest.main()

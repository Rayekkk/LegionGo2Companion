# SPDX-License-Identifier: BSD-3-Clause

import asyncio
import copy
import hashlib
import stat
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
        "multi_index": "red green blue",
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

    def test_rgb_capability_accepts_both_go2_models_only_with_verified_interface(self):
        real = "/sys/devices/test/0003:17EF:61EB.0001/leds/go:rgb:joystick_rings"
        with tempfile.TemporaryDirectory() as raw:
            make_led(Path(raw))
            for family, product in (("Legion Go 8ASP2", "83N0"), ("Legion Go 8AHP2", "83N1")):
                with (
                    self.subTest(product=product),
                    patch.object(rgb_backend, "_read_identity", return_value={
                        "product_family": family, "product_name": product,
                    }),
                    patch.object(rgb_backend.os.path, "lexists", return_value=True),
                    patch.object(rgb_backend.os.path, "realpath", side_effect=lambda path:
                                 "/sys/bus/hid/drivers/hid-lenovo-go" if path.endswith("/driver") else real),
                    patch.object(rgb_backend.os, "stat", return_value=types.SimpleNamespace(st_mode=stat.S_IFREG)),
                    patch.object(rgb_backend.os, "access", return_value=True),
                    patch.object(rgb_backend, "_read_small", side_effect=lambda path:
                                 Path(raw, Path(path).name).read_text().strip()),
                ):
                    self.assertEqual(rgb_backend._rgb_capability(), (real, ""))
                    with patch.object(rgb_backend.os.path, "realpath", return_value=real.replace("61EB", "6182")):
                        self.assertIsNone(rgb_backend._rgb_capability()[0])
                    with patch.object(rgb_backend.os.path, "realpath", side_effect=lambda path:
                                      "/sys/bus/hid/drivers/hid-generic" if path.endswith("/driver") else real):
                        self.assertIsNone(rgb_backend._rgb_capability()[0])
                    with patch.object(rgb_backend.os, "stat", side_effect=FileNotFoundError):
                        self.assertIsNone(rgb_backend._rgb_capability()[0])
                    with patch.object(rgb_backend.os, "access", return_value=False):
                        self.assertIsNone(rgb_backend._rgb_capability()[0])
                    with patch.object(rgb_backend, "_rgb_channel_maxima", return_value=(256, 255, 255)):
                        self.assertIsNone(rgb_backend._rgb_capability()[0])

    def test_rgb_capability_rejects_unknown_or_mismatched_model_pairs(self):
        for family, product in (
            ("Legion Go 8ASP2", "83N1"), ("Legion Go 8AHP2", "83N0"),
            ("Legion Go 8AHP2", "unknown"), ("unknown", "83N1"),
        ):
            with (
                self.subTest(family=family, product=product),
                patch.object(rgb_backend, "_read_identity", return_value={
                    "product_family": family, "product_name": product,
                }),
                patch.object(rgb_backend.os.path, "lexists") as probe,
            ):
                self.assertIsNone(rgb_backend._rgb_capability()[0])
                probe.assert_not_called()

    def test_8ahp2_does_not_gain_power_register_access(self):
        with (
            patch.object(rgb_backend, "_read_identity", return_value={
                "product_family": "Legion Go 8AHP2", "product_name": "83N1",
                "board_name": rgb_backend.EXPECTED_BOARD,
                "bios_version": rgb_backend.AUDITED_BIOS,
            }),
            patch.object(rgb_backend, "_map_power_register") as mmio,
        ):
            self.assertFalse(rgb_backend._power_capability()[0])
            self.assertIsNone(rgb_backend._read_power_led())
            self.assertFalse(rgb_backend._write_power_led(False))
            mmio.assert_not_called()

    def test_legacy_led_abi_uses_u8_color_and_preserves_modern_snapshot_on_restore(self):
        with tempfile.TemporaryDirectory() as raw:
            led = Path(raw)
            make_led(led)
            original = rgb_backend._read_rgb_snapshot(raw)
            (led / 'multi_max_intensity').unlink()
            self.assertEqual(rgb_backend._rgb_channel_maxima(raw), (255, 255, 255))
            state = copy.deepcopy(rgb_backend.DEFAULT_STATE)
            state.update(hue=0, saturation=100, brightness=25)
            self.assertEqual(rgb_backend._target_for_state(state, raw)['rgb'], [255, 0, 0])
            self.assertTrue(rgb_backend._apply_rgb_snapshot(raw, original))
            restored = rgb_backend._read_rgb_snapshot(raw)
            self.assertEqual(restored['rgb'], [0, 255, 255])
            self.assertEqual(restored['brightness'], 20)
            self.assertEqual(original['rgb'], [0, 100, 100])
            (led / 'multi_max_intensity').write_text('100 100 100')
            self.assertTrue(rgb_backend._apply_rgb_snapshot(raw, restored))
            self.assertEqual(rgb_backend._read_rgb_snapshot(raw), original)

    def test_unknown_legacy_layout_or_invalid_modern_limits_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            led = Path(raw)
            make_led(led)
            for value in ('', '0 100 100', '256 255 255', 'invalid', '100 100'):
                (led / 'multi_max_intensity').write_text(value)
                with self.assertRaises(ValueError):
                    rgb_backend._rgb_channel_maxima(raw)
            (led / 'multi_max_intensity').unlink()
            (led / 'multi_index').write_text('blue green red')
            with self.assertRaises(ValueError):
                rgb_backend._rgb_channel_maxima(raw)
            (led / 'multi_index').write_text('red green blue')
            (led / 'max_brightness').write_text('255')
            with self.assertRaises(ValueError):
                rgb_backend._rgb_channel_maxima(raw)

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

    def test_lost_settings_do_not_report_successful_restoration(self):
        memory = MemorySettings()
        memory.recovery_error = "Primary and backup settings are corrupt"
        with patch.object(rgb_backend, "settings", memory), patch.object(rgb_backend, "_apply_rgb_target") as apply:
            with self.assertRaises(RuntimeError):
                asyncio.run(rgb_backend.Plugin().restore_original())
        apply.assert_not_called()

    def test_missing_primary_with_corrupt_backup_does_not_seed_default_ownership(self):
        from safe_settings import SettingsManager
        with tempfile.TemporaryDirectory(prefix="lego-rgb-recovery-") as raw:
            Path(raw, "rgb_settings.json.bak").write_text('{"state":', encoding="utf-8")
            store = SettingsManager("rgb_settings", raw)
            with patch.object(rgb_backend, "settings", store):
                with self.assertRaises(RuntimeError):
                    rgb_backend._ensure_settings_file()
            self.assertFalse(Path(store.path).exists())
            self.assertTrue(store.recovery_error)

    def test_failed_setup_propagates_so_module_manager_cannot_report_enabled(self):
        with patch.object(rgb_backend, "_ensure_settings_file", side_effect=OSError("settings disk full")):
            with self.assertRaisesRegex(OSError, "settings disk full"):
                asyncio.run(rgb_backend.Plugin()._main())

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

    def test_boot_retry_continues_past_settling_window_then_returns_to_slow_polling(self):
        now = [0.0]
        attempts = []
        plugin = rgb_backend.Plugin()
        plugin._settle_until = 30.0

        async def advance(seconds):
            now[0] += seconds
            if now[0] >= 105.0:
                raise asyncio.CancelledError

        async def reconcile(_function):
            attempts.append(now[0])
            return now[0] >= 40.0

        fake_time = types.SimpleNamespace(monotonic=lambda: now[0])
        with (
            patch.object(rgb_backend, "time", fake_time),
            patch.object(rgb_backend.asyncio, "sleep", side_effect=advance),
            patch.object(rgb_backend, "_offload", side_effect=reconcile),
            patch.object(rgb_backend, "_resume_detected", return_value=False),
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(plugin._drift_loop())
        self.assertEqual(attempts, [5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 100.0])

    def test_resume_retries_missing_controller_and_force_reapplies_saved_on_or_off(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as raw:
                make_led(Path(raw))
                state = copy.deepcopy(rgb_backend.DEFAULT_STATE)
                state.update(
                    configured=True, control_enabled=True, rings_enabled=enabled,
                    original_rgb=rgb_backend._read_rgb_snapshot(raw),
                )
                # Matching readback must not suppress the writes after resume.
                self.assertTrue(rgb_backend._apply_rgb_snapshot(
                    raw, rgb_backend._target_for_state(state, raw)
                ))
                memory = MemorySettings(state)
                before = copy.deepcopy(memory.data)
                now = [0.0]
                ticks = [0]
                probes = [0]

                async def advance(seconds):
                    now[0] += seconds
                    if seconds == rgb_backend.RESUME_CHECK_S:
                        ticks[0] += 1
                        if ticks[0] > 1:
                            raise asyncio.CancelledError

                def capability():
                    probes[0] += 1
                    return (None, "controller reconnecting") if probes[0] == 1 else (raw, "")

                fake_time = types.SimpleNamespace(
                    CLOCK_BOOTTIME=7, monotonic=lambda: now[0],
                    clock_gettime=lambda _clock: now[0] + 60.0,
                )
                with (
                    patch.object(rgb_backend, "settings", memory),
                    patch.object(rgb_backend, "time", fake_time),
                    patch.object(rgb_backend, "_last_suspend_offset", 0.0),
                    patch.object(rgb_backend.asyncio, "sleep", side_effect=advance),
                    patch.object(rgb_backend, "_rgb_capability", side_effect=capability),
                    patch.object(rgb_backend, "_power_capability", return_value=(False, "test")),
                    patch.object(rgb_backend, "_write_attr", wraps=rgb_backend._write_attr) as writes,
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        asyncio.run(rgb_backend.Plugin()._drift_loop())
                expected = ["profile", "speed", "multi_intensity", "brightness", "effect", "mode", "enabled"]
                self.assertEqual([call.args[1] for call in writes.call_args_list], expected if enabled else ["enabled"])
                self.assertTrue(all(call.kwargs["force"] for call in writes.call_args_list))
                self.assertEqual(writes.call_args_list[-1].args[2], "true" if enabled else "false")
                self.assertEqual(memory.data, before)
                self.assertEqual(rgb_backend._last_error, "")

    def test_transient_rgb_failure_keeps_settings_and_recovers_on_both_led_abis(self):
        for legacy in (True, False):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as raw:
                led = Path(raw)
                make_led(led, enabled="false")
                if legacy:
                    (led / "multi_max_intensity").unlink()
                state = copy.deepcopy(rgb_backend.DEFAULT_STATE)
                state.update(
                    configured=True,
                    control_enabled=True,
                    rings_enabled=True,
                    hue=0,
                    saturation=100,
                    brightness=25,
                    original_rgb=rgb_backend._read_rgb_snapshot(raw),
                )
                memory = MemorySettings(state)
                before = copy.deepcopy(memory.data)
                write = rgb_backend._write_attr
                calls = []
                failed = [False]

                def fail_once(*args, **kwargs):
                    calls.append(args[1])
                    if not failed[0]:
                        failed[0] = True
                        return False
                    return write(*args, **kwargs)

                with (
                    patch.object(rgb_backend, "settings", memory),
                    patch.object(rgb_backend, "_rgb_capability", return_value=(raw, "")),
                    patch.object(rgb_backend, "_write_attr", side_effect=fail_once),
                ):
                    self.assertFalse(rgb_backend._reconcile())
                    self.assertEqual(calls, ["profile"])
                    self.assertIn("could not be applied", rgb_backend._last_error)
                    self.assertEqual(memory.data, before)
                    self.assertTrue(rgb_backend._reconcile())
                    self.assertTrue(rgb_backend._rgb_matches(
                        state, rgb_backend._read_rgb_snapshot(raw), raw
                    ))
                    self.assertEqual(memory.data, before)
                    calls.clear()
                    self.assertTrue(rgb_backend._reconcile())
                    self.assertEqual(calls, [])
                    (led / "enabled").write_text("false\n", encoding="ascii")
                    self.assertTrue(rgb_backend._reconcile())
                    self.assertEqual(calls, [
                        "profile", "speed", "multi_intensity", "brightness",
                        "effect", "mode", "enabled",
                    ])
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

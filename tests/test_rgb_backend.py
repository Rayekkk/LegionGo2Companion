# SPDX-License-Identifier: BSD-3-Clause

import asyncio
import contextlib
import copy
import errno
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_integration  # Shared Decky and Windows/POSIX compatibility fixture.

import module_runtime
import rgb_backend
from support import GO1_POWER_LED_FIELDS, path_field, power_led_dsdt


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


@contextlib.contextmanager
def led_writes(led, *, fail=0):
    """Attribute writes that reach the LED tree; the first `fail` are refused like a busy MCU."""
    real_open = rgb_backend.os.open
    writes, refused = [], [fail]

    def tracked(path, flags, *args):
        if flags & os.O_WRONLY and Path(path).parent == Path(led):
            writes.append(Path(path).name)
            if refused[0]:
                refused[0] -= 1
                raise OSError(errno.EIO, "controller busy")
        return real_open(path, flags, *args)
    with patch.object(rgb_backend.os, "open", side_effect=tracked):
        yield writes


class RgbBackendTests(unittest.TestCase):
    def setUp(self):
        rgb_backend._power_capability_cache = None
        rgb_backend._power_layout = None
        rgb_backend._last_error = ""

    def test_successful_write_allows_bounded_stale_readback_without_replaying_write(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'effect').write_text('breathe')
            with patch.object(rgb_backend, '_read_small', side_effect=['breathe','breathe','chroma']) as read, \
                    patch.object(rgb_backend.time, 'sleep'), \
                    patch.object(rgb_backend.os, 'write', wraps=rgb_backend.os.write) as write:
                self.assertTrue(rgb_backend._write_attr(folder, 'effect', 'chroma'))
            self.assertEqual(read.call_count, 3)
            write.assert_called_once()

    def test_persistent_mismatch_is_bounded_and_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'effect').write_text('breathe')
            with patch.object(rgb_backend, '_read_small', return_value='breathe') as read, \
                    patch.object(rgb_backend.time, 'sleep'), \
                    patch.object(rgb_backend.os, 'write', wraps=rgb_backend.os.write) as write:
                self.assertFalse(rgb_backend._write_attr(folder, 'effect', 'chroma'))
            self.assertLessEqual(read.call_count, 16)
            write.assert_called_once()

    def test_incomplete_snapshot_is_reread_without_using_partial_values(self):
        with tempfile.TemporaryDirectory() as folder:
            make_led(Path(folder))
            reader = rgb_backend._read_small
            attempts = []
            def read(path):
                if Path(path).name == 'multi_intensity':
                    attempts.append(path)
                    if len(attempts) == 1:
                        return ''
                return reader(path)
            with patch.object(rgb_backend, '_read_small', side_effect=read), \
                    patch.object(rgb_backend.time, 'sleep'), \
                    patch.object(rgb_backend.os, 'write') as write:
                result = rgb_backend._read_rgb_snapshot(folder)
            self.assertEqual(result['rgb'], [0, 100, 100])
            self.assertEqual(len(attempts), 2)
            write.assert_not_called()

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
                "bios_version": "RRCN16WW",
            }),
            patch.object(rgb_backend, "_map_power_register") as mmio,
        ):
            self.assertFalse(rgb_backend._power_capability()[0])
            self.assertIsNone(rgb_backend._read_power_led())
            self.assertFalse(rgb_backend._write_power_led(False))
            mmio.assert_not_called()

    def test_both_models_validate_modern_and_legacy_abi_before_acceptance(self):
        real = "/sys/devices/test/0003:17EF:61EB.0001/leds/go:rgb:joystick_rings"
        required_attrs = (
            "enabled", "profile", "mode", "effect", "brightness", "multi_intensity", "speed",
            "enabled_index", "profile_range", "mode_index", "effect_index",
            "max_brightness", "multi_index", "speed_range",
        )
        for family, product in (("Legion Go 8ASP2", "83N0"), ("Legion Go 8AHP2", "83N1")):
            for legacy in (False, True):
                with self.subTest(product=product, legacy=legacy), tempfile.TemporaryDirectory() as raw:
                    led = Path(raw)
                    make_led(led)
                    if legacy:
                        (led / "multi_max_intensity").unlink()
                    with (
                        patch.object(rgb_backend, "_read_identity", return_value={
                            "product_family": family, "product_name": product,
                        }),
                        patch.object(rgb_backend.os.path, "lexists", return_value=True),
                        patch.object(rgb_backend.os.path, "realpath", side_effect=lambda path:
                                     "/sys/bus/hid/drivers/hid-lenovo-go" if path.replace("\\", "/").endswith("/driver") else real),
                        patch.object(rgb_backend.os, "stat", return_value=types.SimpleNamespace(st_mode=stat.S_IFREG)),
                        patch.object(rgb_backend.os, "access", return_value=True),
                        patch.object(rgb_backend, "_read_small", side_effect=lambda path, *args:
                                     (led / Path(path).name).read_text(encoding="ascii").strip()),
                        patch.object(rgb_backend, "_write_attr") as writes,
                    ):
                        self.assertEqual(rgb_backend._rgb_capability(), (real, ""))
                        detached = real.replace("61EB", "61ED")  # the receiver with one pad detached
                        with patch.object(rgb_backend.os.path, "realpath", side_effect=lambda path:
                                          "/sys/bus/hid/drivers/hid-lenovo-go" if path.replace("\\", "/").endswith("/driver") else detached):
                            self.assertEqual(rgb_backend._rgb_capability(), (detached, ""))
                        with patch.object(rgb_backend.os.path, "realpath", return_value=real.replace("61EB", "6182")):
                            self.assertIsNone(rgb_backend._rgb_capability()[0], "foreign HID id")
                        with patch.object(rgb_backend.os.path, "realpath", side_effect=lambda path:
                                          "/sys/bus/hid/drivers/hid-generic" if path.replace("\\", "/").endswith("/driver") else real):
                            self.assertIsNone(rgb_backend._rgb_capability()[0], "foreign driver")
                        with patch.object(rgb_backend.os, "access", return_value=False):
                            self.assertIsNone(rgb_backend._rgb_capability()[0], "read-only attributes")
                        for attr in required_attrs:
                            def missing(path, *, follow_symlinks):
                                self.assertFalse(follow_symlinks)
                                if Path(path).name == attr:
                                    raise FileNotFoundError(path)
                                return types.SimpleNamespace(st_mode=stat.S_IFREG)
                            with self.subTest(missing=attr), patch.object(rgb_backend.os, "stat", side_effect=missing):
                                self.assertIsNone(rgb_backend._rgb_capability()[0])
                        for attr, invalid in (
                            ("enabled_index", "true"), ("profile_range", "1-4"),
                            ("mode_index", "dynamic"), ("effect_index", "monocolor"),
                            ("max_brightness", "256"), ("multi_index", "blue green red"),
                            ("speed_range", "0-255"),
                        ):
                            original = (led / attr).read_bytes()
                            try:
                                (led / attr).write_text(invalid, encoding="ascii")
                                with self.subTest(invalid=attr):
                                    self.assertIsNone(rgb_backend._rgb_capability()[0])
                            finally:
                                (led / attr).write_bytes(original)
                        self.assertEqual(rgb_backend._rgb_capability(), (real, ""))
                        writes.assert_not_called()

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
        state = copy.deepcopy(rgb_backend.DEFAULT_STATE)
        state.update(configured=True, control_enabled=True, rings_enabled=True)
        memory = MemorySettings(state)
        memory.recovery_error = "Primary and backup settings are corrupt"
        with tempfile.TemporaryDirectory() as raw:
            make_led(Path(raw), enabled="true")
            with (
                patch.object(rgb_backend, "settings", memory),
                patch.object(rgb_backend, "_rgb_capability", return_value=(raw, "")),
                led_writes(raw) as writes,
            ):
                with self.assertRaises(RuntimeError):
                    asyncio.run(rgb_backend.Plugin().restore_original())
            self.assertEqual(writes, [])

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
            self.assertGreater(seconds, 0, "the monitor must not busy-loop")
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
        failed = [at for at in attempts if at < 40]
        healthy = [at for at in attempts if at >= 40]
        self.assertTrue(failed)
        self.assertLessEqual(failed[0], 10)
        self.assertLessEqual(len(failed), 10, "unavailable RGB must not be hammered")
        self.assertGreaterEqual(len(healthy), 2, "retry must continue after initial settling")
        self.assertLessEqual(healthy[0], 55, "controller return must be retried promptly")
        self.assertTrue(all(45 <= b - a <= 75 for a, b in zip(healthy, healthy[1:])), healthy)

    def test_start_forces_a_controlled_ring_once_and_leaves_an_uncontrolled_one(self):
        for control in (True, False):
            with self.subTest(control=control), tempfile.TemporaryDirectory() as raw:
                make_led(Path(raw))
                state = copy.deepcopy(rgb_backend.DEFAULT_STATE)
                state.update(configured=True, control_enabled=control, rings_enabled=True,
                             original_rgb=rgb_backend._read_rgb_snapshot(raw))
                self.assertTrue(rgb_backend._apply_rgb_snapshot(raw, rgb_backend._target_for_state(state, raw)))

                async def start():
                    plugin = rgb_backend.Plugin()
                    await plugin._main()
                    await plugin._unload()
                with (
                    patch.object(rgb_backend, "settings", MemorySettings(state)),
                    patch.object(rgb_backend, "_rgb_capability", return_value=(raw, "")),
                    patch.object(rgb_backend, "_power_capability", return_value=(False, "test")),
                    led_writes(raw) as writes,
                ):
                    asyncio.run(start())
                # Everything already matched: a write here is the forced pass alone.
                self.assertEqual(writes, ["profile", "speed", "multi_intensity", "brightness", "effect", "mode", "enabled"]
                                 if control else [])

    def test_a_wake_window_ends_with_one_forced_ring_pass(self):
        for trigger in ("wake", "controller_event"):  # main.py: the controllers re-enumerated
            with self.subTest(trigger=trigger):
                self._wake_window(trigger)

    def _wake_window(self, trigger):
        now, calls = [0.0], []
        plugin = rgb_backend.Plugin()

        async def offload(function, **kwargs):
            calls.append((now[0], kwargs))
            return {"control_enabled": True} if function is rgb_backend._load_state else True

        async def advance(seconds):
            now[0] += seconds
            if now[0] > 60:
                raise asyncio.CancelledError
        with (
            patch.object(rgb_backend, "time", types.SimpleNamespace(monotonic=lambda: now[0])),
            patch.object(rgb_backend.asyncio, "sleep", side_effect=advance),
            patch.object(rgb_backend, "_offload", side_effect=offload),
            patch.object(rgb_backend, "_resume_detected", return_value=False),
            patch.object(plugin, "get_status", return_value={}),
        ):
            async def wake():
                if trigger == "wake":
                    await plugin.reapply()
                else:
                    plugin.controller_event()
                await plugin._drift_loop()
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(wake())
        forced = [at for at, kwargs in calls if kwargs.get("force_ring")]
        self.assertEqual(len(forced), 1, calls)
        self.assertGreaterEqual(forced[0], rgb_backend.STARTUP_SETTLE_S)
        self.assertEqual([at for at, kwargs in calls if not kwargs and at > forced[0]], [],
                         "then the normal interval again")

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
                    patch.object(rgb_backend._resume_detected, "last", 0.0),
                    patch.object(rgb_backend.module_runtime, "suspend_offset", lambda: 60.0),
                    patch.object(rgb_backend.asyncio, "sleep", side_effect=advance),
                    patch.object(rgb_backend, "_rgb_capability", side_effect=capability),
                    patch.object(rgb_backend, "_power_capability", return_value=(False, "test")),
                    led_writes(raw) as writes,
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        asyncio.run(rgb_backend.Plugin()._drift_loop())
                # Every attribute already matched, so each of these writes is a forced re-apply; enable goes last.
                expected = ["profile", "speed", "multi_intensity", "brightness", "effect", "mode", "enabled"]
                self.assertEqual(writes, expected if enabled else ["enabled"])
                self.assertEqual((Path(raw) / "enabled").read_text().strip(), "true" if enabled else "false")
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
                with (
                    patch.object(rgb_backend, "settings", memory),
                    patch.object(rgb_backend, "_rgb_capability", return_value=(raw, "")),
                    led_writes(raw, fail=1) as calls,
                ):
                    self.assertFalse(rgb_backend._reconcile())
                    self.assertEqual(len(calls), 1, "a refused write stops the apply")
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
                    self.assertEqual(calls, ["enabled"], "drift repair rewrites only the drifted attribute")
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

    def test_uninstall_restores_the_rings_and_the_updated_plugin_reapplies_the_choice(self):
        # Decky runs _uninstall before every update: hardware goes back, the choice stays.
        with tempfile.TemporaryDirectory(prefix="lego-rgb-") as raw:
            led = Path(raw)
            make_led(led, enabled="false")
            memory = MemorySettings()
            with (
                patch.object(rgb_backend, "settings", memory),
                patch.object(rgb_backend, "_rgb_capability", return_value=(str(led), "")),
                patch.object(rgb_backend, "_power_capability", return_value=(False, "test")),
            ):
                plugin = rgb_backend.Plugin()
                for change in (plugin.set_control_enabled(True), plugin.set_rings_enabled(True),
                               plugin.set_color(120, 100)):
                    result = asyncio.run(change)
                    self.assertTrue(result["success"], result.get("error"))
                chosen = copy.deepcopy(memory.data["state"])
                asyncio.run(plugin._uninstall())
                self.assertEqual((led / "enabled").read_text().strip(), "false")
                self.assertEqual(memory.data["state"], chosen)

                async def update():
                    updated = rgb_backend.Plugin()
                    await updated._main()
                    await updated._unload()
                asyncio.run(update())
                self.assertEqual((led / "enabled").read_text().strip(), "true")
                self.assertTrue(rgb_backend._rgb_matches(
                    chosen, rgb_backend._read_rgb_snapshot(str(led)), str(led)))
                self.assertEqual(memory.data["state"]["original_rgb"], chosen["original_rgb"])

    def test_uninstall_restores_the_power_led_and_the_updated_plugin_reapplies_it(self):
        register = bytearray([0b10100101 & ~rgb_backend.POWER_LED_GO2.mask])  # The LED is on before Companion.
        with tempfile.TemporaryDirectory(prefix="lego-rgb-") as raw:
            led = Path(raw)
            make_led(led, enabled="false")
            memory = MemorySettings()
            with (
                patch.object(rgb_backend, "settings", memory),
                patch.object(rgb_backend, "_rgb_capability", return_value=(str(led), "")),
                patch.object(rgb_backend, "_power_capability", return_value=(True, "")),
                patch.object(rgb_backend, "_power_layout", rgb_backend.POWER_LED_GO2),
                patch.object(rgb_backend, "_with_power_lock", side_effect=lambda operation: operation()),
                patch.object(rgb_backend, "_map_power_register",
                             side_effect=lambda _write, operation: operation(register, 0)),
            ):
                plugin = rgb_backend.Plugin()
                for change in (plugin.set_control_enabled(True), plugin.set_power_led(False)):
                    result = asyncio.run(change)
                    self.assertTrue(result["success"], result.get("error"))
                self.assertFalse(rgb_backend._read_power_led())
                asyncio.run(plugin._uninstall())
                self.assertTrue(rgb_backend._read_power_led(), "uninstall hands the original LED back")

                async def update():
                    updated = rgb_backend.Plugin()
                    await updated._main()
                    await updated._unload()
                asyncio.run(update())
                self.assertFalse(rgb_backend._read_power_led(), "the updated plugin reapplies the choice")
                self.assertTrue(memory.data["state"]["power_led_managed"])
                self.assertIs(memory.data["state"]["power_led_original_enabled"], True)

    def test_first_takeover_preserves_the_existing_colour_intensity(self):
        cases = (
            ("monocolor", "25 0 0", [100, 0, 0], "5"),
            ("monocolor", "0 0 0", None, "0"),
            ("breathe", "0 50 50", [0, 100, 100], "10"),
            ("rainbow", "0 0 0", None, "20"),
        )
        for effect, before, rgb, brightness in cases:
            with self.subTest(effect=effect, before=before), \
                    tempfile.TemporaryDirectory(prefix="lego-rgb-") as raw:
                led = Path(raw)
                make_led(led, enabled="true")
                (led / "effect").write_text(effect + "\n", encoding="ascii")
                (led / "multi_intensity").write_text(before + "\n", encoding="ascii")
                memory = MemorySettings()
                with (
                    patch.object(rgb_backend, "settings", memory),
                    patch.object(rgb_backend, "_rgb_capability", return_value=(str(led), "")),
                    patch.object(rgb_backend, "_power_capability", return_value=(False, "test")),
                ):
                    result = asyncio.run(rgb_backend.Plugin().set_control_enabled(True))
                self.assertTrue(result["success"], result.get("error"))
                self.assertEqual((led / "effect").read_text().strip(), effect)
                self.assertEqual((led / "brightness").read_text().strip(), brightness)
                if rgb is not None:
                    self.assertEqual(
                        (led / "multi_intensity").read_text().split(), [str(c) for c in rgb])

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

    def test_unreadable_profile_is_written_so_rings_turn_on_and_drift_heals(self):
        real_read = rgb_backend._read_small

        def driver_read(path, limit=512):
            value = real_read(path, limit)
            if os.path.basename(path) == "profile" and (stuck or value.strip() == "0"):
                raise OSError(errno.EINVAL, "Invalid argument")  # hid-lenovo-go rgb_profile_show
            return value

        for invalid in ("0\n", "\n"):
            stuck = False
            with self.subTest(profile=invalid), tempfile.TemporaryDirectory(prefix="lego-rgb-") as raw:
                led = Path(raw)
                make_led(led, enabled="false")
                (led / "profile").write_text(invalid, encoding="ascii")
                memory = MemorySettings()
                plugin = rgb_backend.Plugin()
                with (
                    patch.object(rgb_backend, "settings", memory),
                    patch.object(rgb_backend, "_rgb_capability", return_value=(str(led), "")),
                    patch.object(rgb_backend, "_power_capability", return_value=(False, "test")),
                    patch.object(rgb_backend, "_read_small", side_effect=driver_read),
                    led_writes(raw) as writes,
                    patch.object(rgb_backend.time, "sleep"),
                ):
                    self.assertTrue(asyncio.run(plugin.set_control_enabled(True))["success"])
                    result = asyncio.run(plugin.set_rings_enabled(True))
                    self.assertTrue(result["success"], result.get("error"))
                    self.assertEqual((led / "profile").read_text().strip(), "3")
                    self.assertEqual((led / "enabled").read_text().strip(), "true")

                    # The MCU loses the profile while the rings are owned: drift repair writes it.
                    (led / "profile").write_text(invalid, encoding="ascii")
                    self.assertTrue(rgb_backend._reconcile())
                    self.assertEqual((led / "profile").read_text().strip(), "3")
                    writes.clear()
                    self.assertTrue(rgb_backend._reconcile())
                    self.assertEqual(writes, [])

                    # A profile the firmware never confirms is one write per attempt, never accepted.
                    stuck = True
                    (led / "effect").write_text("breathe\n", encoding="ascii")
                    writes.clear()
                    self.assertFalse(rgb_backend._reconcile())
                    self.assertEqual(writes, ["profile"])

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
            patch.object(rgb_backend, "_power_layout", rgb_backend.POWER_LED_GO2),
            patch.object(rgb_backend, "_with_power_lock", side_effect=lambda operation: operation()),
            patch.object(rgb_backend, "_map_power_register", side_effect=fake_map),
        ):
            self.assertTrue(rgb_backend._write_power_led(True))
            self.assertEqual(register.value, 0b10100101 & ~(1 << 6))
            preserved = register.value & ~(1 << 6)
            self.assertTrue(rgb_backend._write_power_led(False))
            self.assertEqual(register.value & ~(1 << 6), preserved)
            self.assertTrue(register.value & (1 << 6))

    def test_power_capability_needs_a_go_2_model_and_live_dsdt_layout_not_a_bios_version(self):
        with tempfile.TemporaryDirectory(prefix="lego-dsdt-") as raw:
            root = Path(raw)
            dsdt = root / "DSDT"
            devmem = root / "mem"
            dsdt.write_bytes(power_led_dsdt())
            devmem.write_bytes(bytes(4096))
            identity = {
                "product_family": rgb_backend.EXPECTED_FAMILY,
                "product_name": rgb_backend.EXPECTED_PRODUCT,
                "board_name": rgb_backend.EXPECTED_BOARD,
                "bios_version": "RRCN16WW",
            }
            with (
                patch.object(rgb_backend, "_read_identity", return_value=identity),
                patch.object(rgb_backend, "DSDT_PATH", str(dsdt)),
                patch.object(rgb_backend, "DEV_MEM_PATH", str(devmem)),
            ):
                rgb_backend._power_capability_cache = None
                self.assertEqual(rgb_backend._power_capability(), (True, ""))
                rgb_backend._power_capability_cache = None
                identity["bios_version"] = "RRCN99WW"  # A Lenovo update with the same ACPI layout.
                self.assertEqual(rgb_backend._power_capability(), (True, ""))
                rgb_backend._power_capability_cache = None
                identity["board_name"] = "LNVNB161217"
                self.assertFalse(rgb_backend._power_capability()[0])
                # The Go 2 Z2 (83N1): its board name is unknown, the DSDT check still decides.
                for family, product, works in (("Legion Go 8AHP2", "83N1", True), ("Legion Go 8AHP2", "83N0", False),
                                               ("Legion Go 8ASP2", "83N1", False), ("Legion Go S 8ARP1", "83L3", False),
                                               ("Legion Go 8APU1", "83E1", False)):
                    with self.subTest(product=product, family=family):
                        identity.update(product_family=family, product_name=product, bios_version="RQCN17WW")
                        rgb_backend._power_capability_cache = None
                        self.assertIs(rgb_backend._power_capability()[0], works)
                identity.update(product_family="Legion Go 8AHP2", product_name="83N1")
                dsdt.write_bytes(power_led_dsdt(base=0xFE0B0300))  # The Go S's ERAM base
                rgb_backend._power_capability_cache = None
                self.assertFalse(rgb_backend._power_capability()[0])

    def test_power_led_layout_accepts_only_lpbl_at_eram_byte_0x10_bit_6(self):
        layout = rgb_backend._power_led_layout_ok
        names = [b"LSTE", b"LID2", b"EKTS", b"DISV", b"SMAF", b"S4RF", b"LPBL", b"OSRT"]
        self.assertTrue(layout(power_led_dsdt()))
        self.assertTrue(layout(power_led_dsdt(fields=[(None, 0x80), (b"LSTE", 6), (b"LPBL", 1), (b"OSRT", 1)])))
        swapped = names[:6] + [b"OSRT", b"LPBL"]
        rejected = {
            "region moved": power_led_dsdt(base=0xFEEC2400),
            "not SystemMemory": power_led_dsdt(space=0x03),
            "region too short": power_led_dsdt(length=0x10),
            "field moved a byte": power_led_dsdt(fields=[(None, 0x88)] + [(n, 1) for n in names]),
            "bit 7 instead of 6": power_led_dsdt(fields=[(None, 0x80)] + [(n, 1) for n in swapped]),
            "two bits wide": power_led_dsdt(fields=[(None, 0x80), (b"LSTE", 5), (b"LPBL", 2), (b"OSRT", 1)]),
            "LPBL missing": power_led_dsdt(fields=[(None, 0x80)] + [(n, 1) for n in names if n != b"LPBL"]),
            "LPBL twice": power_led_dsdt(fields=[(None, 0x80)] + [(n, 1) for n in names] + [(b"LPBL", 1)]),
            "unknown field element": power_led_dsdt(fields=[(None, 0x80)] + [(n, 1) for n in names[:5]] + [(b"\x02xyz", 1), (b"LPBL", 1), (b"OSRT", 1)]),
            "truncated table": power_led_dsdt()[:-1],
            "not a DSDT": b"SSDT" + power_led_dsdt()[4:],
            "LPBL also in Field(\\_SB.EC0.ERAM)": path_field(power_led_dsdt(), b"\\\x2f\x03_SB_EC0_", b"LPBL"),
        }
        for reason, table in rejected.items():
            with self.subTest(reason):
                self.assertFalse(layout(table))

    def test_transient_power_capability_failure_recovers_without_restart_after_bounded_retry(self):
        for failure in ('dsdt_read','missing_mem','missing_identity'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as raw:
                root=Path(raw);dsdt=root/'DSDT';devmem=root/'mem'
                dsdt.write_bytes(power_led_dsdt());devmem.write_bytes(bytes(4096))
                identity={'product_family':rgb_backend.EXPECTED_FAMILY,'product_name':rgb_backend.EXPECTED_PRODUCT,
                          'board_name':rgb_backend.EXPECTED_BOARD,'bios_version':'RRCN16WW'}
                original=dict(identity)
                rgb_backend._power_capability_cache=None
                if failure=='dsdt_read':dsdt.unlink()
                elif failure=='missing_mem':devmem.unlink()
                else:identity['board_name']=''
                with patch.object(rgb_backend,'_read_identity',return_value=identity), \
                        patch.object(rgb_backend,'DSDT_PATH',str(dsdt)),patch.object(rgb_backend,'DEV_MEM_PATH',str(devmem)), \
                        patch.object(rgb_backend.time,'monotonic',return_value=100) as clock, \
                        patch.object(rgb_backend,'_map_power_register') as mmio:
                    self.assertFalse(rgb_backend._power_capability()[0])
                    dsdt.write_bytes(power_led_dsdt());devmem.write_bytes(bytes(4096));identity.update(original)
                    clock.return_value=101
                    self.assertFalse(rgb_backend._power_capability()[0],'Do not retry every UI poll')
                    clock.return_value=110
                    self.assertEqual(rgb_backend._power_capability(),(True,''))
                    mmio.assert_not_called()

    def test_power_retry_does_not_accept_unknown_dsdt_or_model(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);dsdt=root/'DSDT';devmem=root/'mem'
            dsdt.write_bytes(b'unqualified');devmem.write_bytes(bytes(4096))
            identity={'product_family':rgb_backend.EXPECTED_FAMILY,'product_name':rgb_backend.EXPECTED_PRODUCT,
                      'board_name':rgb_backend.EXPECTED_BOARD,'bios_version':'RRCN16WW'}
            with patch.object(rgb_backend,'_read_identity',return_value=identity), \
                    patch.object(rgb_backend,'DSDT_PATH',str(dsdt)),patch.object(rgb_backend,'DEV_MEM_PATH',str(devmem)), \
                    patch.object(rgb_backend.time,'monotonic',return_value=100) as clock, \
                    patch.object(rgb_backend,'_map_power_register') as mmio:
                rgb_backend._power_capability_cache=None
                self.assertFalse(rgb_backend._power_capability()[0])
                clock.return_value=110
                self.assertFalse(rgb_backend._power_capability()[0])
                mmio.assert_not_called()


def go_s_dsdt(**changes):
    return power_led_dsdt(**{"base": 0xFE0B0300, **changes})


def go1_dsdt(**changes):
    return power_led_dsdt(**{"base": 0xFE0B0300, "region": b"ECMM", "fields": GO1_POWER_LED_FIELDS, **changes})


GO2_DMI = dict(product_family="Legion Go 8ASP2", product_name="83N0", board_name="LNVNB161216")


class PowerLedBetaTests(unittest.TestCase):
    """Owner's decision D1: the power-button light on the Go S and Go 1, each gated by DMI and its own DSDT."""

    class Register:
        def __init__(self, value, drop=False):
            self.value, self.drop = value, drop

        def __getitem__(self, _index):
            return self.value

        def __setitem__(self, _index, value):
            if not self.drop:
                self.value = value

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "dmi").mkdir()
        (self.root / "mem").write_bytes(bytes(4096))
        self.register = self.Register(0xFF)
        self.mapped = []
        for target, name, value in (
            (module_runtime, "DMI_ROOT", str(self.root / "dmi")),
            (rgb_backend, "DSDT_PATH", str(self.root / "DSDT")),
            (rgb_backend, "DEV_MEM_PATH", str(self.root / "mem")),
            (rgb_backend, "_power_capability_cache", None),
            (rgb_backend, "_power_layout", None),
            (rgb_backend, "_last_error", ""),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, kwargs in (
            ("_rgb_capability", dict(return_value=(None, "test"))),
            ("_with_power_lock", dict(side_effect=lambda operation: operation())),
            ("_map_power_register", dict(side_effect=lambda write, operation: self.mapped.append(
                rgb_backend._power_layout.address) or operation(self.register, 0))),
        ):
            patcher = patch.object(rgb_backend, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def console(self, dsdt, vendor="LENOVO", **dmi):
        for name, value in dict(sys_vendor=vendor, bios_version="TEST01WW", **dmi).items():
            (self.root / "dmi" / name).write_text(value + "\n", encoding="ascii")
        (self.root / "DSDT").write_bytes(dsdt)
        rgb_backend._power_capability_cache = None
        rgb_backend._power_layout = None
        self.mapped.clear()

    def test_each_console_sets_only_its_own_bit_at_its_own_address(self):
        for dmi, dsdt, address, bit, beta in (
            (GO2_DMI, power_led_dsdt(), 0xFEEC2310, 6, False),  # Go 2 unchanged
            (dict(product_family="Legion Go S 8ARP1", product_name="83L3"), go_s_dsdt(), 0xFE0B0310, 6, True),
            (dict(product_family="Legion Go S 8APU1", product_name="83N6"), go_s_dsdt(), 0xFE0B0310, 6, True),
            (dict(product_family="Legion Go 8APU1", product_name="83E1"), go1_dsdt(), 0xFE0B0352, 5, True),
        ):
            with self.subTest(model=dmi["product_name"]), patch.object(rgb_backend, "settings", MemorySettings()):
                self.console(dsdt, **dmi)
                self.register.value = 0xFF  # off, every other bit set
                plugin = rgb_backend.Plugin()
                result = asyncio.run(plugin.set_power_led(True))
                self.assertTrue(result["success"], result["error"])
                self.assertEqual(self.register.value, 0xFF & ~(1 << bit))
                self.assertEqual(set(self.mapped), {address})
                power = result["status"]["power_led"]
                self.assertEqual((power["supported"], power["current"], power["beta"]), (True, True, beta))
                self.assertTrue(asyncio.run(plugin.set_power_led(False))["success"])
                self.assertEqual(self.register.value, 0xFF)

    def test_a_dsdt_that_does_not_describe_the_models_field_hides_the_light_without_mmio(self):
        go_s = dict(product_family="Legion Go S 8ARP1", product_name="83L3")
        go1 = dict(product_family="Legion Go 8APU1", product_name="83E1")
        lpbl_bit7 = [(None, 0x80)] + [(n, 1) for n in (b"LSTE", b"LID2", b"EKTS", b"DISV", b"SMAF", b"S4RF", b"OSRT", b"LPBL")]
        for reason, dmi, dsdt in (
            ("Go S with the Go 2's ERAM base", go_s, power_led_dsdt()),
            ("Go S with LPBL at bit 7", go_s, go_s_dsdt(fields=lpbl_bit7)),
            ("Go S with its region renamed", go_s, go_s_dsdt(region=b"ECMM")),
            ("Go S with the Go 1's table", go_s, go1_dsdt()),
            ("Go 1 with the Go S's table", go1, go_s_dsdt()),
            ("Go 1 with LEDP where LEDM is (0x58 bit 0)", go1, go1_dsdt(fields=[(None, 0x58 * 8), (b"LEDP", 1)])),
            ("Go 1 with LEDP two bits wide", go1, go1_dsdt(fields=GO1_POWER_LED_FIELDS[:-3] + [(b"LEDP", 2), (b"RGHD", 1)])),
            ("Go 1 without LEDP", go1, go1_dsdt(fields=GO1_POWER_LED_FIELDS[:-3])),
            ("Go 1 with LEDP twice", go1, go1_dsdt(fields=GO1_POWER_LED_FIELDS + [(b"LEDP", 1)])),
            ("Go 1 region moved", go1, go1_dsdt(base=0xFE0B0400)),
            ("Go 1 region too short", go1, go1_dsdt(length=0x52)),
            ("not a Lenovo", dict(go1, vendor="OTHER"), go1_dsdt()),
            ("machine type outside the beta", dict(product_family="Legion Go S 8ARP1", product_name="83Q2"), go_s_dsdt()),
        ):
            with self.subTest(reason), patch.object(rgb_backend, "settings", MemorySettings()):
                self.console(dsdt, **dmi)
                result = asyncio.run(rgb_backend.Plugin().set_power_led(False))
                self.assertFalse(result["success"])
                power = result["status"]["power_led"]
                self.assertEqual((power["supported"], power["beta"]), (False, False))
                self.assertEqual((self.mapped, self.register.value), ([], 0xFF))

    def test_an_unconfirmed_write_restores_the_bit_and_reports_the_error(self):
        memory = MemorySettings()
        with patch.object(rgb_backend, "settings", memory):
            self.console(go1_dsdt(), product_family="Legion Go 8APU1", product_name="83E1")
            self.register.value = 0xFF & ~(1 << 5)  # on
            writes = []
            real_set = self.Register.__setitem__

            def ec_drops_our_bit(register, index, value):  # the EC rewrites the byte with the old bit
                writes.append(value)
                real_set(register, index, value & ~(1 << 5) if len(writes) == 1 else value)
            with patch.object(self.Register, "__setitem__", ec_drops_our_bit):
                result = asyncio.run(rgb_backend.Plugin().set_power_led(False))
            self.assertFalse(result["success"])
            self.assertIn("did not confirm", result["error"])
            self.assertEqual(writes[0], 0xFF, "only bit 5 was asked to change")
            self.assertEqual(self.register.value, 0xFF & ~(1 << 5), "the original bit is back")
            self.assertFalse(memory.data["state"]["power_led_managed"])
            self.assertTrue(result["status"]["power_led"]["current"])


if __name__ == "__main__":
    unittest.main()

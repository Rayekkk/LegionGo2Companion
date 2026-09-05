# SPDX-License-Identifier: BSD-3-Clause

import asyncio
import copy
import struct
import threading
import types
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import test_integration  # Install the existing Decky test environment.
import controller_backend as module


class MemorySettings:
    def __init__(self):
        self.data = {}
        self.committed = {}
        self.fail_commit = None
        self.commits = 0

    def getSetting(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    def read(self):
        pass

    def setSetting(self, key, value):
        self.data[key] = copy.deepcopy(value)

    def commit(self):
        self.commits += 1
        if self.fail_commit == self.commits:
            self.data = copy.deepcopy(self.committed)
            raise OSError("disk full")
        self.committed = copy.deepcopy(self.data)


PHYSICAL = {"path": "/dev/hidraw1", "identity": "/sys/devices/usb/1-1:1.2",
            "sysfs": "/sys/devices/usb/1-1:1.2/0003:17EF:61EB.0002", "pid": "61eb"}
SOURCE = "hidraw://hidraw1"
DEVICE = {"path": "/org/shadowblip/InputPlumber/CompositeDevice0", "source": SOURCE,
          "identity": PHYSICAL["identity"], "generation": PHYSICAL["sysfs"]}


class ParserTests(unittest.TestCase):
    def test_native_packet_endianness_and_right_axis_order(self):
        report = bytearray(64)
        report[:3] = b"\x04\x3c\x74"
        report[5], report[7] = 99, 101
        report[12], report[13] = 2, 255
        struct.pack_into(">HH", report, 26, 512, 1024)
        struct.pack_into(">hhh", report, 41, -32768, 258, -1)
        struct.pack_into(">hhh", report, 54, 123, -456, 32767)
        sample = module.parse_physical_report(report)
        self.assertEqual(sample["gyro_left"], {"x": -32768, "y": 258, "z": -1})
        self.assertEqual(sample["gyro_right"], {"x": -456, "y": 123, "z": 32767})
        self.assertEqual(sample["touchpad"]["x"], 0.5)
        self.assertEqual(sample["touchpad"]["y"], 1)
        self.assertEqual(sample["battery_left"], 99)
        self.assertIsNone(sample["battery_right"])
        self.assertEqual(sample["connection_left"], "attached")
        self.assertIsNone(sample["connection_right"])
        self.assertEqual(sample, module.parse_physical_report(report[:60]))

    def test_configuration_and_unknown_frames_are_not_zero_measurements(self):
        for report in (bytes(64), b"\x04\x00\x05" + bytes(61),
                       b"\x04\x3c\x74" + bytes(56), b"\x04\x40\x74" + bytes(61)):
            self.assertIsNone(module.parse_physical_report(report))
        self.assertIsNone(module.parse_virtual_report(bytes(64)))

    def test_touch_release_invalid_coordinates_and_virtual_touch_bit(self):
        report = bytearray(b"\x04\x3c\x74" + bytes(61))
        self.assertIsNone(module.parse_physical_report(report)["touchpad"]["x"])
        struct.pack_into(">H", report, 26, 65535)
        self.assertIsNone(module.parse_physical_report(report)["touchpad"])
        virtual = bytearray(b"\x01\x00\x09\x40" + bytes(60))
        virtual[10] = 0x10
        struct.pack_into("<hh", virtual, 20, -32767, 32767)
        struct.pack_into("<hhh", virtual, 30, -300, 400, -500)
        sample = module.parse_virtual_report(virtual)
        self.assertEqual(sample["gyro"], {"x": -300, "y": 400, "z": -500})
        self.assertEqual(sample["touchpad"]["x"], 0)
        self.assertEqual(sample["touchpad"]["y"], 0)
        virtual[10] = 0x08
        self.assertFalse(module.parse_virtual_report(virtual)["touchpad"]["is_touching"])


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.store = MemorySettings()
        self.backend = module.Plugin()
        self.current = {SOURCE: module._desired_filters("combined") + ["Mouse:Wheel"],
                        "evdev://event7": ["Keyboard:KeyA"]}
        self.writes = []
        self.imu = {"left": False, "right": False}
        self.patches = [patch.object(module, "settings", self.store),
                        patch.object(module, "_hid_inventory", return_value=(PHYSICAL, None)),
                        patch.object(module, "_discover", side_effect=self.discover),
                        patch.object(module, "_filters", side_effect=lambda _: copy.deepcopy(self.current)),
                        patch.object(module, "_run_busctl", side_effect=self.busctl),
                        patch.object(module.controller_imu, "read_status", side_effect=lambda: {"available": True, "actual": copy.deepcopy(self.imu)}),
                        patch.object(module.controller_imu, "apply", side_effect=self.apply_imu)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def apply_imu(self, expected, desired):
        self.assertEqual(expected, self.imu)
        self.imu = copy.deepcopy(desired)

    def discover(self, physical, *, require_exclusive=True):
        return {**DEVICE, "source": "hidraw://" + physical["path"].rsplit("/", 1)[-1],
                "generation": physical["sysfs"], "filters": copy.deepcopy(self.current)}

    def busctl(self, args):
        self.assertEqual(args[:6], ["set-property", module.SERVICE, DEVICE["path"],
                                   module.INTERFACE, "FilteredEvents", "a{sas}"])
        entries, index, result = int(args[6]), 7, {}
        for _ in range(entries):
            source, count = args[index], int(args[index + 1])
            result[source] = args[index + 2:index + 2 + count]
            index += 2 + count
        self.assertEqual(index, len(args))
        self.writes.append(copy.deepcopy(result))
        self.current = result
        return ""

    def test_system_default_makes_no_writes_or_device_queries(self):
        with patch.object(module, "_hid_inventory") as inventory:
            self.backend._select("system")
        inventory.assert_not_called()
        self.assertEqual(self.store.commits, 0)
        self.assertEqual(self.writes, [])

    def test_lost_settings_never_claim_an_unowned_successful_release(self):
        self.store.recovery_error = "Primary and backup settings are corrupt"
        with self.assertRaises(module.ControllerError):
            self.backend._release(persist=True)
        self.assertEqual(self.store.commits, 0)
        self.assertEqual(self.writes, [])

    def test_lost_settings_fail_initialization_before_starting_a_monitor(self):
        self.store.recovery_error = "Primary and backup settings are corrupt"
        with patch.object(self.backend, "_ensure_watch") as start:
            with self.assertRaises(module.ControllerError):
                asyncio.run(self.backend._main())
        start.assert_not_called()
        self.assertEqual(self.writes, [])

    def test_valid_restoration_file_can_be_reloaded_without_restarting_decky(self):
        from safe_settings import SettingsManager, atomic_write_json
        with tempfile.TemporaryDirectory(prefix="lego-controller-recovery-") as raw:
            path = Path(raw, "controller_settings.json")
            path.write_text('{"state":', encoding="utf-8")
            store = SettingsManager("controller_settings", raw)
            self.assertTrue(store.recovery_error)
            path.unlink()
            with patch.object(module, "settings", store):
                with self.assertRaises(module.ControllerError):
                    module._state()
            atomic_write_json(str(path), {"state": {"gyro_source": "system", "ownership": None}})
            with patch.object(module, "settings", store):
                self.assertEqual(module._state()["gyro_source"], "system")
            self.assertFalse(store.recovery_error)

    def test_temporary_controller_absence_keeps_the_monitor_and_saved_source(self):
        self.backend._select("left")
        async def lifecycle():
            await self.backend._unload()
            with patch.object(module, "_hid_inventory", return_value=(None, None)):
                await self.backend._main()
            self.assertFalse(self.backend._watch_task.done())
            self.assertEqual(module._state()["gyro_source"], "left")
            await asyncio.to_thread(self.backend._select, "left", reconcile=True)
            self.assertEqual(self.imu, {"left": True, "right": False})
            self.assertFalse(self.backend._error)
            await self.backend._unload()
        asyncio.run(lifecycle())

    def test_selection_preserves_all_unowned_filters_and_releases_exact_baseline(self):
        baseline = copy.deepcopy(self.current)
        self.backend._select("left")
        self.assertEqual(self.imu, {"left": True, "right": False})
        self.assertEqual(module._imu_values(self.current, SOURCE), module._desired_filters("left"))
        self.assertIn("Mouse:Wheel", self.current[SOURCE])
        self.assertEqual(self.current["evdev://event7"], ["Keyboard:KeyA"])
        self.current["evdev://event9"] = ["Keyboard:KeyB"]
        self.current[SOURCE].append("Gamepad:Button:North")
        self.backend._release(persist=True)
        self.assertEqual(module._imu_values(self.current, SOURCE), module._imu_values(baseline, SOURCE))
        self.assertEqual(self.current["evdev://event9"], ["Keyboard:KeyB"])
        self.assertIn("Gamepad:Button:North", self.current[SOURCE])
        self.assertEqual(module._state()["gyro_source"], "system")
        self.assertEqual(self.imu, {"left": False, "right": False})

    def test_external_owned_change_is_never_overwritten_on_release_or_watch(self):
        self.backend._select("left")
        self.current[SOURCE].remove("Gyroscope:Right")
        external = copy.deepcopy(self.current)
        with self.assertRaises(module.ControllerError):
            self.backend._select("left", reconcile=True)
        self.assertEqual(self.current, external)
        self.backend._release(persist=True)
        self.assertEqual(self.current, external)
        self.assertFalse(self.backend._conflict)

    def test_save_failure_after_hardware_change_rolls_back_both_state_and_hardware(self):
        baseline = copy.deepcopy(self.current)
        self.store.fail_commit = 2
        with self.assertRaisesRegex(OSError, "disk full"):
            self.backend._select("right")
        self.assertEqual(module._filter_map(self.current), module._filter_map(baseline))
        self.assertEqual(module._state()["gyro_source"], "system")
        self.assertEqual(self.imu, {"left": False, "right": False})

    def test_external_imu_change_blocks_monitor_but_release_preserves_changed_side(self):
        self.backend._select("left")
        self.imu["right"] = True
        with self.assertRaises(module.ControllerError):
            self.backend._select("left", reconcile=True)
        self.backend._release(persist=True)
        self.assertEqual(self.imu, {"left": False, "right": True})

    def test_delayed_monitor_does_not_restore_a_stale_user_choice(self):
        self.backend._select("right")
        self.backend._select("left", reconcile=True)
        self.assertEqual(module._state()["gyro_source"], "right")
        self.assertEqual(self.imu, {"left": False, "right": True})

    def test_corrupt_nested_state_is_rejected_without_hardware_write(self):
        for raw in ({"gyro_source": []}, {"gyro_source": "left", "ownership": {"identity": "x", "baseline": [{}]}}):
            self.store.data["state"] = raw
            self.assertEqual(module._state()["gyro_source"], "system")
        self.assertEqual(self.writes, [])

    def test_journal_flags_are_strict_booleans_and_pending_is_not_controlled(self):
        self.backend._select("left")
        owner = module._state()["ownership"]
        for field in ("pending", "release_pending"):
            corrupted = {**owner, field: "false"}
            self.store.data["state"] = {"gyro_source": "left", "ownership": corrupted}
            self.assertEqual(module._state()["gyro_source"], "system")
        self.store.data["state"] = {"gyro_source": "left", "ownership": {**owner, "pending": True}}
        with patch.object(module, "_iio_inventory", return_value=[]):
            status = self.backend._status(True)
        self.assertFalse(status["controlled"])
        self.assertTrue(status["recovery_pending"])

    def test_journal_failure_prevents_first_hardware_write(self):
        self.store.fail_commit = 1
        with self.assertRaises(OSError):
            self.backend._select("left")
        self.assertEqual(self.writes, [])

    def test_readback_failure_rolls_back_and_does_not_claim_success(self):
        baseline = copy.deepcopy(self.current)
        with patch.object(module, "_run_busctl", return_value=""), patch.object(module.time, "sleep"):
            with self.assertRaisesRegex(module.ControllerError, "confirm"):
                self.backend._select("right")
        self.assertEqual(self.current, baseline)
        self.assertEqual(module._state()["gyro_source"], "system")

    def test_pending_journal_recovers_transition_after_process_restart(self):
        self.store.fail_commit = 2
        with patch.object(module, "_write_owned", side_effect=SystemExit("process killed")):
            with self.assertRaises(SystemExit):
                self.backend._select("left")
        self.assertTrue(module._state()["ownership"]["pending"])
        self.store.fail_commit = None
        self.backend = module.Plugin()
        self.backend._select("left", reconcile=True)
        self.assertEqual(module._state()["gyro_source"], "left")
        self.assertFalse(module._state()["ownership"]["pending"])

    def test_pending_journal_recovers_kill_between_left_and_right_imu_writes(self):
        def partial_write(_expected, _desired):
            self.imu["left"] = True
            raise SystemExit("process killed")
        with patch.object(module.controller_imu, "apply", side_effect=partial_write):
            with self.assertRaises(SystemExit):
                self.backend._select("combined")
        self.backend = module.Plugin()
        self.backend._select("combined", reconcile=True)
        self.assertEqual(self.imu, {"left": True, "right": True})
        self.backend._release(persist=True)
        self.assertEqual(self.imu, {"left": False, "right": False})

    def test_release_save_failure_restores_both_previous_user_settings(self):
        self.backend._select("right")
        before = copy.deepcopy(self.current)
        self.store.fail_commit = self.store.commits + 2
        with self.assertRaises(OSError):
            self.backend._release(persist=True)
        self.assertEqual(self.current, before)
        self.assertEqual(self.imu, {"left": False, "right": True})
        self.assertEqual(module._state()["gyro_source"], "right")

    def test_release_journal_failure_does_not_touch_hardware(self):
        self.backend._select("combined")
        before = copy.deepcopy(self.current)
        self.store.fail_commit = self.store.commits + 1
        with self.assertRaises(OSError):
            self.backend._release(persist=True)
        self.assertEqual(self.current, before)
        self.assertEqual(self.imu, {"left": True, "right": True})

    def test_kill_between_release_imu_writes_recovers_the_previous_choice(self):
        self.backend._select("combined")
        def interrupted_release(_expected, _desired):
            self.imu["left"] = False
            raise SystemExit("process killed")
        with patch.object(module.controller_imu, "apply", side_effect=interrupted_release):
            with self.assertRaises(SystemExit):
                self.backend._release(persist=True)
        self.assertTrue(module._state()["ownership"]["release_pending"])
        self.assertEqual(self.imu, {"left": False, "right": True})
        self.backend = module.Plugin()
        self.backend._select("combined", reconcile=True)
        self.assertEqual(self.imu, {"left": True, "right": True})
        self.assertFalse(module._state()["ownership"].get("release_pending", False))
        self.backend._release(persist=True)
        self.assertEqual(self.imu, {"left": False, "right": False})

    def test_kill_after_release_filters_preserves_original_ownership_and_recovers(self):
        self.backend._select("right")
        original = copy.deepcopy(self.current)
        with patch.object(module.controller_imu, "apply", side_effect=SystemExit("process killed")):
            with self.assertRaises(SystemExit):
                self.backend._release(persist=False)
        self.assertEqual(module._imu_values(self.current, SOURCE), module._desired_filters("combined"))
        self.backend = module.Plugin()
        self.backend._select("right", reconcile=True)
        self.assertEqual(self.current, original)
        self.assertEqual(self.imu, {"left": False, "right": True})

    def test_release_recovery_never_claims_filters_that_were_already_external(self):
        self.backend._select("left")
        self.current[SOURCE].remove("Gyroscope:Right")
        external = copy.deepcopy(self.current)
        with patch.object(module.controller_imu, "apply", side_effect=SystemExit("process killed")):
            with self.assertRaises(SystemExit):
                self.backend._release(persist=True)
        self.backend = module.Plugin()
        with self.assertRaises(module.ControllerError):
            self.backend._select("left", reconcile=True)
        self.assertEqual(self.current, external)

    def test_service_restart_without_hidraw_change_reapplies_saved_source(self):
        self.backend._select("right")
        self.current[SOURCE] = module._desired_filters("combined")
        def restarted(physical, *, require_exclusive=True):
            return {**self.discover(physical), "generation": PHYSICAL["sysfs"] + "|new-dbus-owner"}
        with patch.object(module, "_discover", side_effect=restarted):
            self.backend._select("right", reconcile=True)
        self.assertEqual(module._imu_values(self.current, SOURCE), module._desired_filters("right"))

    def test_process_watch_is_captured_only_after_success_and_is_not_refreshed_by_status(self):
        with patch.object(self.backend._service_watch, "capture") as capture, \
                patch.object(self.backend._service_watch, "clear") as clear, \
                patch.object(module, "_iio_inventory", return_value=[]):
            self.backend._select("left")
            capture.assert_called_once()
            self.backend._status(True)
            capture.assert_called_once()
            self.backend._select("system")
            clear.assert_called_once()
            capture.assert_called_once()

    def test_failed_selection_cannot_capture_a_new_service_process(self):
        self.store.fail_commit = 1
        with patch.object(self.backend._service_watch, "capture") as capture:
            with self.assertRaises(OSError):
                self.backend._select("left")
        capture.assert_not_called()

    def test_process_exit_hint_cannot_authorize_overwriting_same_generation_external_filters(self):
        self.backend._select("left")
        self.current[SOURCE] = module._desired_filters("combined")
        original = copy.deepcopy(self.current)
        clock = [0.0]

        async def sleep(_seconds):
            clock[0] += 5
            if clock[0] > 5:
                raise asyncio.CancelledError

        async def watch():
            with self.assertRaises(asyncio.CancelledError):
                await self.backend._watch()

        with patch.object(module, "time", types.SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(module, "_suspend_offset", return_value=0), \
                patch.object(module.asyncio, "sleep", side_effect=sleep), \
                patch.object(self.backend._service_watch, "consume_exit", return_value=True):
            asyncio.run(watch())
        self.assertTrue(self.backend._conflict)
        self.assertEqual(self.current, original)
        self.assertEqual(self.backend._generation, DEVICE["generation"])

    def test_release_with_another_active_imu_restores_only_our_controls(self):
        self.backend._select("left")
        self.current["iio://device4"] = ["Accelerometer:Center"]
        def with_other_sensor(physical, *, require_exclusive=True):
            if require_exclusive:
                raise module.ControllerError("Another motion sensor is active")
            return self.discover(physical, require_exclusive=False)
        with patch.object(module, "_discover", side_effect=with_other_sensor):
            self.backend._release(persist=True)
        self.assertEqual(module._imu_values(self.current, SOURCE), module._desired_filters("combined"))
        self.assertEqual(self.current["iio://device4"], ["Accelerometer:Center"])
        self.assertEqual(self.imu, {"left": False, "right": False})
        self.assertEqual(module._state()["gyro_source"], "system")

    def test_pending_release_recovery_preserves_another_active_imu(self):
        self.backend._select("right")
        with patch.object(module.controller_imu, "apply", side_effect=SystemExit("process killed")):
            with self.assertRaises(SystemExit):
                self.backend._release(persist=True)
        self.current["iio://device4"] = []
        def with_other_sensor(physical, *, require_exclusive=True):
            if require_exclusive:
                raise module.ControllerError("Another motion sensor is active")
            return self.discover(physical, require_exclusive=False)
        self.backend = module.Plugin()
        with patch.object(module, "_discover", side_effect=with_other_sensor):
            self.backend._release(persist=True)
        self.assertEqual(self.current["iio://device4"], [])
        self.assertEqual(self.imu, {"left": False, "right": False})
        self.assertEqual(module._state()["gyro_source"], "system")

    def test_unload_restores_baseline_and_retains_saved_intent_for_startup(self):
        self.backend._select("combined")
        asyncio.run(self.backend._unload())
        self.assertEqual(self.imu, {"left": False, "right": False})
        self.assertEqual(module._state()["gyro_source"], "combined")
        async def lifecycle():
            await self.backend._main()
            self.assertEqual(self.imu, {"left": True, "right": True})
            await self.backend._unload()
        asyncio.run(lifecycle())

    def test_hotplug_rediscovers_changed_hidraw_and_preserves_other_devices(self):
        self.backend._select("left")
        new_physical = {**PHYSICAL, "path": "/dev/hidraw9", "sysfs": PHYSICAL["sysfs"] + "new"}
        self.current = {"hidraw://hidraw9": module._desired_filters("combined"),
                        "evdev://event7": ["Keyboard:KeyA"]}
        with patch.object(module, "_hid_inventory", return_value=(new_physical, None)):
            self.backend._select("left", reconcile=True)
        self.assertEqual(module._imu_values(self.current, "hidraw://hidraw9"), module._desired_filters("left"))
        self.assertEqual(self.current["evdev://event7"], ["Keyboard:KeyA"])

    def test_resume_retries_missing_controller_at_next_poll_then_returns_to_normal_interval(self):
        self.backend._select("left")
        clock = [0.0]
        calls = []
        new_physical = {**PHYSICAL, "sysfs": PHYSICAL["sysfs"] + "new"}
        select = self.backend._select

        def observed_select(*args, **kwargs):
            calls.append(clock[0])
            return select(*args, **kwargs)

        async def sleep(_seconds):
            clock[0] += 5
            if clock[0] == 5:
                self.current[SOURCE] = module._desired_filters("combined") + ["Mouse:Wheel"]
            if clock[0] > 70:
                raise asyncio.CancelledError

        async def watch():
            with self.assertRaises(asyncio.CancelledError):
                await self.backend._watch()

        with patch.object(module, "time", types.SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(module, "_suspend_offset", side_effect=lambda: 2.0 if clock[0] >= 5 else 0.0), \
                patch.object(module.asyncio, "sleep", side_effect=sleep), \
                patch.object(module, "_hid_inventory", side_effect=lambda: (None, None) if clock[0] < 10 else (new_physical, None)), \
                patch.object(self.backend, "_select", side_effect=observed_select):
            asyncio.run(watch())
        self.assertEqual(calls, [5, 10, 70])
        self.assertEqual(module._imu_values(self.current, SOURCE), module._desired_filters("left"))
        self.assertIn("Mouse:Wheel", self.current[SOURCE])
        self.assertEqual(self.current["evdev://event7"], ["Keyboard:KeyA"])
        self.assertEqual(self.imu, {"left": True, "right": False})
        self.assertEqual(module._state()["gyro_source"], "left")
        self.assertFalse(self.backend._error)


class WatchScheduleTests(unittest.TestCase):
    def test_recovery_backoff_and_healthy_polling_are_bounded(self):
        cases = (
            ("healthy", False, "", 120, [60, 120]),
            ("persistent_failure", True, "", 145, [5, 10, 20, 40, 80, 140]),
            ("startup_retry", False, "controller unavailable", 70, [5, 10, 70]),
            ("external_conflict", True, "", 70, [5, 65]),
            ("service_restart", False, "", 70, [5, 65]),
            ("service_failure", False, "", 145, [5, 10, 20, 40, 80, 140]),
        )
        for kind, resume, initial_error, end, expected in cases:
            with self.subTest(kind=kind):
                backend = module.Plugin()
                backend._error = initial_error
                clock, calls = [0.0], []

                async def sleep(_seconds):
                    clock[0] += 5
                    if clock[0] > end:
                        raise asyncio.CancelledError

                def select(*_args, **_kwargs):
                    calls.append(clock[0])
                    if kind == "external_conflict":
                        backend._conflict = True
                        raise module.ControllerError("An external filter changed")
                    if kind in {"persistent_failure", "service_failure"} or (kind == "startup_retry" and clock[0] < 10):
                        raise module.ControllerError("The controller is unavailable")
                    backend._error = ""

                async def watch():
                    with self.assertRaises(asyncio.CancelledError):
                        await backend._watch()

                with patch.object(module, "time", types.SimpleNamespace(monotonic=lambda: clock[0])), \
                        patch.object(module, "_suspend_offset", side_effect=lambda: 2.0 if resume and clock[0] >= 5 else 0.0), \
                        patch.object(module.asyncio, "sleep", side_effect=sleep), \
                        patch.object(module, "_state", return_value={"gyro_source": "left"}), \
                        patch.object(backend._service_watch, "consume_exit", side_effect=lambda: kind.startswith("service_") and clock[0] == 5), \
                        patch.object(module, "_json_value") as query, \
                        patch.object(backend, "_select", side_effect=select):
                    asyncio.run(watch())
                self.assertEqual(calls, expected)
                query.assert_not_called()


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.backend = module.Plugin()

    def capture(self, now=100):
        stream = {"reports": 0, "invalid_reports": 0, "sample": None, "last": None, "error": ""}
        self.backend._capture = {"token": "test-token", "active": True, "reason": "",
                                 "started": now, "deadline": now + 30, "lease": now + 3,
                                 "physical": copy.deepcopy(stream), "virtual": copy.deepcopy(stream)}
        return self.backend._capture

    def test_missing_or_wrong_device_never_opens_hardware(self):
        with patch.object(module, "_hid_inventory", return_value=(None, None)), patch.object(module.os, "open") as opener:
            with self.assertRaises(module.ControllerError):
                self.backend._start_capture()
        opener.assert_not_called()

    def test_verified_hidraw_opens_only_readonly_nonblocking(self):
        physical = {**PHYSICAL, "devnum": "240:1"}
        info = types.SimpleNamespace(st_mode=module.stat.S_IFCHR, st_rdev=1234)
        with patch.object(module, "_is_go2", return_value=True), \
                patch.object(module, "_hid_inventory", return_value=(physical, None)), \
                patch.object(module.os, "open", return_value=8) as opener, \
                patch.object(module.os, "fstat", return_value=info), \
                patch.object(module.os, "O_NONBLOCK", 2048, create=True), \
                patch.object(module.os, "makedev", return_value=1234, create=True):
            self.assertEqual(module._open_reader(physical), 8)
        flags = opener.call_args.args[1]
        self.assertEqual(flags & (module.os.O_WRONLY | module.os.O_RDWR), 0)
        self.assertTrue(flags & 2048)
        with patch.object(module, "_is_go2", return_value=False), patch.object(module.os, "open") as opener:
            with self.assertRaises(module.ControllerError):
                module._open_reader(PHYSICAL)
        opener.assert_not_called()

    def test_expired_lease_cannot_be_revived_by_late_poll(self):
        self.capture()
        with patch.object(module, "_capture_time", return_value=104):
            result = self.backend._snapshot("test-token", renew=True)
        self.assertFalse(result["active"])
        self.assertEqual(result["reason"], "lease_expired")
        self.assertTrue(self.backend._stop.is_set())
        self.assertIsNone(result["physical"]["sample"])
        self.assertIsNone(result["physical"]["rate_hz"])

    def test_wrong_token_never_extends_lease(self):
        capture = self.capture()
        with self.assertRaises(ValueError):
            self.backend._snapshot("another-token", renew=True)
        self.assertEqual(capture["lease"], 103)

    def test_heartbeat_does_not_extend_hard_limit(self):
        capture = self.capture()
        with patch.object(module, "_capture_time", return_value=102):
            self.assertTrue(self.backend._snapshot("test-token", renew=True)["active"])
        self.assertEqual(capture["lease"], 105)
        self.assertEqual(capture["deadline"], 130)
        capture["lease"] = 132
        with patch.object(module, "_capture_time", return_value=130):
            self.assertEqual(self.backend._snapshot("test-token", renew=True)["reason"], "completed")

    def test_worker_closes_fd_on_lease_expiry_and_open_failure(self):
        capture = self.capture()
        with patch.object(module, "_open_reader", side_effect=[7, OSError("missing")]), \
                patch.object(module.os, "close") as close, \
                patch.object(module, "_capture_time", return_value=104), \
                patch.object(module.select, "select") as poll:
            self.backend._worker((PHYSICAL, PHYSICAL), capture, threading.Event())
        close.assert_called_once_with(7)
        poll.assert_not_called()
        self.assertFalse(capture["active"])
        self.assertEqual(capture["virtual"]["error"], "missing")


if __name__ == "__main__":
    unittest.main()

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
# Independent InputPlumber routing oracle: filter the other two sources while
# allowing the selected one. Do not derive expected behavior from _desired_filters.
FILTERS = {
    "combined": ["Accelerometer:Left", "Accelerometer:Right", "Gyroscope:Left", "Gyroscope:Right"],
    "left": ["Accelerometer:Center", "Accelerometer:Right", "Gyroscope:Center", "Gyroscope:Right"],
    "right": ["Accelerometer:Center", "Accelerometer:Left", "Gyroscope:Center", "Gyroscope:Left"],
}
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


class FakeSysfs:
    """A POSIX sysfs path: resolve() follows `links`, file text comes from `files`."""
    def __init__(self, path, links, files):
        self.path, self.links, self.files = path, links, files
    def __truediv__(self, part):
        return FakeSysfs(f"{self.path}/{part}", self.links, self.files)
    def resolve(self):
        return FakeSysfs(self.links.get(self.path, self.path), self.links, self.files)
    def glob(self, _pattern):
        return [FakeSysfs(f"{self.path}/{name}", self.links, self.files)
                for name in sorted({key[len(self.path) + 1:].split("/")[0] for key in self.links
                                    if key.startswith(self.path + "/")})]
    @property
    def name(self):
        return self.path.rsplit("/", 1)[1]
    @property
    def parent(self):
        return FakeSysfs(self.path.rsplit("/", 1)[0], self.links, self.files)
    def __str__(self):
        return self.path


class InventoryTests(unittest.TestCase):
    """Which hidraw nodes count as the console's controller and InputPlumber's virtual one."""

    def inventory(self, console, virtual_pid, virtual_name):
        uhid = "/sys/devices/virtual/misc/uhid/0003:28DE:%04X.0009" % virtual_pid
        usb = "/sys/devices/pci0000:00/usb1/1-1/1-1:1.2/0003:17EF:61EB.0002"
        links = {"/sys/class/hidraw/hidraw1/device": usb, usb + "/driver": "/sys/bus/hid/drivers/hid-lenovo-go",
                 "/sys/class/hidraw/hidraw7/device": uhid, uhid + "/driver": "/sys/bus/hid/drivers/hid-steam"}
        files = {usb + "/uevent": "HID_ID=0003:000017EF:000061EB\nHID_NAME=Lenovo Legion Controller for Windows",
                 usb.rsplit("/", 1)[0] + "/bInterfaceNumber": "02", "/sys/class/hidraw/hidraw1/dev": "240:1",
                 uhid + "/uevent": f"HID_ID=0003:000028DE:0000{virtual_pid:04X}\nHID_NAME={virtual_name}",
                 "/sys/class/hidraw/hidraw7/dev": "240:7"}
        with patch.object(module, "HID_CLASS", FakeSysfs("/sys/class/hidraw", links, files)), \
                patch.object(module, "_text", side_effect=lambda path: files.get(str(path))), \
                patch.object(module, "_console", return_value=console):
            return module._hid_inventory()

    def test_each_console_finds_its_own_inputplumber_controller(self):
        for console, pid, name, found in (
                ("go2", 0x12FB, "Legion Go 2 Controller", True), ("go1", 0x12FE, "Legion Go Controller", True),
                ("go1", 0x12FB, "Legion Go 2 Controller", False), ("go2", 0x12FE, "Legion Go Controller", False),
                ("go1", 0x12FE, "Legion Go S Controller", False)):
            with self.subTest(console=console, name=name):
                physical, virtual = self.inventory(console, pid, name)
                self.assertEqual(physical["pid"], "61eb")
                self.assertEqual(virtual and virtual["path"], "/dev/hidraw7" if found else None)
        self.assertEqual(self.inventory(None, 0x12FB, "Legion Go 2 Controller"), (None, None))


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.store = MemorySettings()
        self.backend = module.Plugin()
        self.current = {SOURCE: list(FILTERS["combined"]) + ["Mouse:Wheel"],
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
        self.assertEqual(module._imu_values(self.current, SOURCE), list(FILTERS["left"]))
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
        self.assertEqual(module._imu_values(self.current, SOURCE), list(FILTERS["combined"]))
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
        self.current[SOURCE] = list(FILTERS["combined"])
        def restarted(physical, *, require_exclusive=True):
            return {**self.discover(physical), "generation": PHYSICAL["sysfs"] + "|new-dbus-owner"}
        with patch.object(module, "_discover", side_effect=restarted):
            self.backend._select("right", reconcile=True)
        self.assertEqual(module._imu_values(self.current, SOURCE), list(FILTERS["right"]))

    def test_process_watch_is_captured_only_after_success_and_is_not_refreshed_by_status(self):
        with patch.object(self.backend._service_watch, "capture") as capture, \
                patch.object(self.backend._service_watch, "clear") as clear:
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
        self.current[SOURCE] = list(FILTERS["combined"])
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
                patch.object(module.module_runtime, "suspend_offset", return_value=0), \
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
        self.assertEqual(module._imu_values(self.current, SOURCE), list(FILTERS["combined"]))
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

    def test_uninstall_restores_baseline_and_the_updated_plugin_reapplies_the_choice(self):
        # Decky runs _uninstall before every update: hardware goes back, the choice stays.
        baseline = copy.deepcopy(self.current)
        self.backend._select("left")
        asyncio.run(self.backend._uninstall())
        self.assertEqual(self.imu, {"left": False, "right": False})
        self.assertEqual(module._imu_values(self.current, SOURCE), module._imu_values(baseline, SOURCE))
        updated = module.Plugin()
        async def lifecycle():
            await updated._main()
            self.assertEqual(updated._error, "")
            self.assertEqual(self.imu, {"left": True, "right": False})
            self.assertEqual(module._imu_values(self.current, SOURCE), list(FILTERS["left"]))
            await updated._unload()
        asyncio.run(lifecycle())
        self.assertEqual(module._state()["gyro_source"], "left")

    def test_hotplug_rediscovers_changed_hidraw_and_preserves_other_devices(self):
        self.backend._select("left")
        new_physical = {**PHYSICAL, "path": "/dev/hidraw9", "sysfs": PHYSICAL["sysfs"] + "new"}
        self.current = {"hidraw://hidraw9": list(FILTERS["combined"]),
                        "evdev://event7": ["Keyboard:KeyA"]}
        with patch.object(module, "_hid_inventory", return_value=(new_physical, None)):
            self.backend._select("left", reconcile=True)
        self.assertEqual(module._imu_values(self.current, "hidraw://hidraw9"), list(FILTERS["left"]))
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

        resumed = [False]
        async def sleep(seconds):
            self.assertGreater(seconds, 0, "the watcher must yield instead of busy-looping")
            self.assertLessEqual(seconds, 10, "resume detection must stay prompt")
            clock[0] += seconds
            if clock[0] >= 5 and not resumed[0]:
                resumed[0] = True
                self.current[SOURCE] = list(FILTERS["combined"]) + ["Mouse:Wheel"]
            if clock[0] > 90:
                raise asyncio.CancelledError

        async def watch():
            with self.assertRaises(asyncio.CancelledError):
                await self.backend._watch()

        with patch.object(module, "time", types.SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(module.module_runtime, "suspend_offset", side_effect=lambda: 2.0 if clock[0] >= 5 else 0.0), \
                patch.object(module.asyncio, "sleep", side_effect=sleep), \
                patch.object(module, "_hid_inventory", side_effect=lambda: (None, None) if clock[0] < 10 else (new_physical, None)), \
                patch.object(self.backend, "_select", side_effect=observed_select):
            asyncio.run(watch())
        # Recovery must be prompt, then stop probing at the wake retry rate.
        # Exact timestamps depend on harmless changes to tick/backoff cadence.
        self.assertGreaterEqual(len(calls), 3)
        self.assertLessEqual(calls[0], 10)
        self.assertLessEqual(calls[1] - calls[0], 15)
        self.assertGreaterEqual(calls[2] - calls[1], 45)
        self.assertLessEqual(calls[2] - calls[1], 75)
        self.assertEqual(module._imu_values(self.current, SOURCE), list(FILTERS["left"]))
        self.assertIn("Mouse:Wheel", self.current[SOURCE])
        self.assertEqual(self.current["evdev://event7"], ["Keyboard:KeyA"])
        self.assertEqual(self.imu, {"left": True, "right": False})
        self.assertEqual(module._state()["gyro_source"], "left")
        self.assertFalse(self.backend._error)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.backend = module.Plugin()

    def capture(self, now=100):
        stream = {"reports": 0, "invalid_reports": 0, "sample": None, "last": None, "error": "",
                  "times": module.deque(maxlen=4096)}
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
        with patch.object(module, "_console", return_value="go2"), \
                patch.object(module, "_hid_inventory", return_value=(physical, None)), \
                patch.object(module.os, "open", return_value=8) as opener, \
                patch.object(module.os, "fstat", return_value=info), \
                patch.object(module.os, "O_NONBLOCK", 2048, create=True), \
                patch.object(module.os, "makedev", return_value=1234, create=True):
            self.assertEqual(module._open_reader(physical), 8)
        flags = opener.call_args.args[1]
        self.assertEqual(flags & (module.os.O_WRONLY | module.os.O_RDWR), 0)
        self.assertTrue(flags & 2048)
        with patch.object(module, "_console", return_value=None), patch.object(module.os, "open") as opener:
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

    def test_ordinary_report_rate_tracks_the_last_second_without_losing_total_count(self):
        capture = self.capture()
        for kind in ("physical", "virtual"):
            capture[kind].update(reports=100, last=100.995,
                times=module.deque((100.005 + index / 100 for index in range(100))))
        for now, expected_rate in ((101.0, 100.0), (101.5, 50.0), (102.0, 0.0)):
            with self.subTest(now=now), patch.object(module, "_capture_time", return_value=now):
                result = self.backend._snapshot("test-token")
            for kind in ("physical", "virtual"):
                self.assertEqual(result[kind]["rate_hz"], expected_rate)
                self.assertEqual(result[kind]["reports"], 100)

    def test_battery_read_uses_one_native_report_closes_and_caches(self):
        report = bytearray(b"\x04\x3c\x74" + bytes(61))
        report[5], report[7] = 84, 61
        report[12], report[13] = 2, 3
        with patch.object(module, "_hid_inventory", return_value=(PHYSICAL, None)), \
                patch.object(module, "_open_reader", return_value=7) as opener, \
                patch.object(module.select, "select", return_value=([7], [], [])), \
                patch.object(module.os, "read", return_value=bytes(report)) as reader, \
                patch.object(module.os, "close") as close:
            first = self.backend._controller_batteries()
            second = self.backend._controller_batteries()
        self.assertEqual(first, {"available": True, "left": 84, "right": 61,
                                 "connection_left": "attached", "connection_right": "detached",
                                 "reason": ""})
        self.assertEqual(second, first)
        opener.assert_called_once_with(PHYSICAL)
        reader.assert_called_once_with(7, 256)
        close.assert_called_once_with(7)

    def test_firmware_gate_read_never_trusts_a_cached_attach_state(self):
        report = bytearray(b"\x04\x3c\x74" + bytes(61))
        report[5], report[7], report[12], report[13] = 84, 61, 2, 2
        detached = bytearray(report); detached[12] = 3
        with patch.object(module, "_hid_inventory", return_value=(PHYSICAL, None)), \
                patch.object(module, "_open_reader", return_value=7), \
                patch.object(module.select, "select", return_value=([7], [], [])), \
                patch.object(module.os, "read", side_effect=[bytes(report), bytes(detached)]), \
                patch.object(module.os, "close"):
            self.assertEqual(self.backend._controller_batteries()["connection_left"], "attached")
            self.assertEqual(self.backend._controller_batteries(max_age=0)["connection_left"], "detached")

    def test_battery_read_timeout_is_nonfatal_and_closes(self):
        with patch.object(module, "_hid_inventory", return_value=(PHYSICAL, None)), \
                patch.object(module, "_open_reader", return_value=7), \
                patch.object(module.select, "select", return_value=([], [], [])), \
                patch.object(module.os, "close") as close:
            result = self.backend._controller_batteries()
        self.assertFalse(result["available"])
        self.assertIsNone(result["left"])
        self.assertIn("No controller battery report", result["reason"])
        close.assert_called_once_with(7)



class SleepTests(unittest.TestCase):
    """Controller sleep: written only after the user picks a time, then kept without flooding the pads."""

    def setUp(self):
        self.store = MemorySettings()
        self.backend = module.Plugin()
        self.pads = {"left": 0, "right": 0}
        self.responding = True
        self.supported = True
        self.writes = []
        self.reads = 0
        for item in (patch.object(module, "settings", self.store),
                     patch.object(module.controller_imu, "sleep_supported", side_effect=lambda: self.supported),
                     patch.object(module.controller_imu, "apply_sleep", side_effect=self.apply)):
            item.start()
            self.addCleanup(item.stop)

    def apply(self, values):
        # Like hid-lenovo-go: every call reads the pads (a request to each), a pad that already
        # reports the value is not written, and a sleeping pad accepts the write without changing.
        self.reads += 1
        for side, value in values.items():
            if self.pads[side] != value:
                self.writes.append((side, value))
                if self.responding:
                    self.pads[side] = value
        return self.pads == values

    def test_nothing_is_written_or_saved_before_the_user_picks_a_time(self):
        self.assertTrue(self.backend._apply_sleep())
        asyncio.run(self.backend._sleep_check(0.0, True))
        self.assertEqual(self.writes, [])
        self.assertEqual(self.store.commits, 0)
        self.assertEqual(self.backend._sleep_status(), {"available": True, "minutes": None, "error": ""})

    def test_a_choice_writes_both_pads_and_is_saved(self):
        result = asyncio.run(self._set(10))
        self.assertEqual(self.pads, {"left": 10, "right": 10})
        self.assertEqual(result["sleep"], {"available": True, "minutes": 10, "error": ""})
        self.assertEqual(self.store.committed["sleep_minutes"], 10)

    def test_only_listed_minutes_are_accepted(self):
        for value in (7, -1, 256, "10", 10.0, True, False, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                asyncio.run(self.backend.set_sleep_time(value))
        self.assertEqual(self.writes, [])
        self.assertEqual(self.store.commits, 0)

    def test_pads_without_the_timer_are_neither_shown_nor_written(self):
        self.supported = False
        self.assertFalse(self.backend._sleep_status()["available"])
        with self.assertRaises(module.ControllerError):
            asyncio.run(self.backend.set_sleep_time(10))
        self.assertEqual(self.writes, [])
        self.assertEqual(self.store.commits, 0)

    def test_an_unconfirmed_write_keeps_the_choice_and_says_so(self):
        self.responding = False
        with self.assertRaises(module.SleepPending):
            asyncio.run(self.backend.set_sleep_time(15))
        self.assertEqual(self.store.committed["sleep_minutes"], 15)
        self.assertTrue(self.backend._sleep_status()["error"])
        self.responding = True
        self.assertTrue(self.backend._apply_sleep())
        self.assertEqual(self.backend._sleep_status()["error"], "")

    def test_reapplied_only_to_a_pad_that_differs(self):
        asyncio.run(self._set(10))
        self.writes.clear()
        self.assertTrue(self.backend._apply_sleep())
        self.assertEqual(self.writes, [], "a pad already at the chosen time is left alone")
        self.pads["right"] = 0  # the pad came back with another value
        self.assertTrue(self.backend._apply_sleep())
        self.assertEqual(self.writes, [("right", 10)])

    def watch(self, until, events=None, resume_at=None):
        """Run the real watch loop on a fake clock; events maps a time to a callback."""
        clock, events = [0.0], dict(events or {})

        async def sleep(seconds):
            clock[0] += seconds
            for at in [at for at in events if at <= clock[0]]:
                events.pop(at)()
            if clock[0] > until:
                raise asyncio.CancelledError

        async def watch():
            with self.assertRaises(asyncio.CancelledError):
                await self.backend._watch()

        offset = lambda: 2.0 if resume_at is not None and clock[0] >= resume_at else 0.0
        with patch.object(module, "time", types.SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(module.asyncio, "sleep", side_effect=sleep), \
                patch.object(module.module_runtime, "suspend_offset", side_effect=offset), \
                patch.object(self.backend, "_watch_check"):
            asyncio.run(watch())

    def test_a_confirmed_time_is_checked_every_two_minutes_and_on_events(self):
        asyncio.run(self._set(10))
        self.reads = 0
        self.writes.clear()
        self.watch(3600)
        self.assertEqual(self.reads, 30, "at start, then once per SLEEP_CHECK_SECONDS")
        self.assertEqual(self.writes, [])

        def reconnected():
            self.pads["right"] = 0
            self.backend.controller_event()  # main.py: the controllers' udev events settled
        self.reads = 0
        self.watch(130, events={60: reconnected})
        self.assertEqual(self.reads, 2, "start and the controller event, before the periodic check")
        self.assertEqual(self.writes, [("right", 10)], "only the reconnected pad that differs is written")

    def test_a_write_lost_to_a_sleeping_pad_returns_after_it_wakes(self):
        # A waking pad sends no event, and the write it missed reported success.
        asyncio.run(self._set(10))
        self.pads["left"] = 0
        self.responding = False
        self.writes.clear()
        written = []

        def woke():
            self.responding = True
        self.watch(760, events={625: woke, 755: lambda: written.append(dict(self.pads))})
        self.assertEqual(written, [{"left": 10, "right": 10}], "within SLEEP_CHECK_SECONDS of the wake")

    def test_a_sleeping_pad_is_retried_with_backoff_not_flooded(self):
        asyncio.run(self._set(10))
        self.pads["left"] = 0
        self.responding = False
        self.writes.clear()
        self.watch(1200)
        # 5, 10, 20, 40, 80, then every 120 s: 14 attempts in 20 minutes, not 240.
        self.assertLessEqual(len(self.writes), 14)
        self.assertGreaterEqual(len(self.writes), 10)
        self.assertEqual(set(self.writes), {("left", 10)}, "only the pad that differs is written")
        self.responding = True
        self.writes.clear()
        self.watch(3600)
        self.assertEqual(self.writes, [("left", 10)], "after the pad confirms, it is only read")

    def test_update_and_turning_the_module_off_leave_the_pads_time_alone(self):
        asyncio.run(self._set(10))
        self.writes.clear()
        self.reads = 0
        asyncio.run(self.backend._uninstall())  # Decky runs this before every update
        self.assertEqual(module._sleep_minutes(), 10)
        # A value read back to restore could be the driver's stale cache, so nothing is written.
        self.backend._release_sleep()
        self.assertEqual((self.reads, self.writes), (0, []))
        self.assertEqual(self.pads, {"left": 10, "right": 10})
        self.assertIsNone(module._sleep_minutes(), "Companion stops managing the time")

    def test_reenabling_the_module_keeps_a_choice_the_pads_have_not_confirmed(self):
        import module_control
        asyncio.run(self._set(30))
        intent = module_control.capture_intent("controller")
        self.assertEqual(intent["sleep"], 30)
        self.backend._release_sleep()
        self.responding = False
        asyncio.run(module_control.restore_intent("controller", self.backend, intent))
        self.assertEqual(module._sleep_minutes(), 30)

    async def _set(self, minutes):
        with patch.object(module, "_hid_inventory", return_value=(None, None)):
            return await self.backend.set_sleep_time(minutes)


if __name__ == "__main__":
    unittest.main()

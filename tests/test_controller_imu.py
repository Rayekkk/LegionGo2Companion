# SPDX-License-Identifier: BSD-3-Clause

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import controller_imu as imu


class HidEntry:
    def __init__(self, path):
        self.path = path
        self.name = path.name.replace("_", ":")

    def resolve(self, strict=True):
        return self.path.resolve(strict=strict)


class HidInventory:
    def __init__(self, path):
        self.path = path

    def iterdir(self):
        return [HidEntry(path) for path in self.path.iterdir()]


class ImuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Windows CI may return an 8.3 alias for TEMP. Match the canonical paths
        # returned by HID discovery without weakening device identity assertions.
        self.root = Path(self.temp.name).resolve() / "sys"
        self.dmi = self.root / "class/dmi/id"
        self.dmi.mkdir(parents=True)
        self.put(self.dmi / "sys_vendor", "LENOVO")
        self.put(self.dmi / "product_name", "83N0")
        # Production enumerates HID symlinks. Use their physical target as the
        # test inventory so Windows does not require symlink privileges.
        self.interface = self.root / "devices/usb3/3-1/interface2"
        self.interface.mkdir(parents=True)
        self.put(self.interface / "bInterfaceNumber", "02")
        self.device = self.make_device("0003:17EF:61EB.0002")
        for name, value in (("SYS_ROOT", self.root), ("HID_DEVICES", HidInventory(self.interface))):
            patcher = patch.object(imu, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(imu, "_driver_name", return_value="hid-lenovo-go")
        self.driver = patcher.start()
        self.addCleanup(patcher.stop)
        self.writes = []
        patcher = patch.object(imu, "_write", side_effect=self.write)
        self.writer = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def put(path, value):
        path.write_text(value + "\n", encoding="ascii")

    def make_device(self, name):
        device = self.interface / name.replace(":", "_")
        device.mkdir()
        self.put(device / "hardware_generation", "2")
        for side in imu.SIDES:
            handle = device / f"{side}_handle"
            handle.mkdir()
            self.put(handle / "imu_bypass_enabled", "false")
            self.put(handle / "imu_bypass_enabled_index", "true false")
            # A dangerous alias must never be opened or changed.
            (handle / "imu_enabled").mkdir()
        return device

    def actual(self):
        return imu._values(self.device)

    def external(self, side, enabled):
        self.put(imu._attribute(self.device, side), "true" if enabled else "false")

    def write(self, device, side, enabled):
        self.assertEqual(device, self.device)
        self.assertIs(type(enabled), bool)
        self.writes.append((side, enabled))
        self.external(side, enabled)

    def test_read_status_discovers_both_sides_without_writing(self):
        result = imu.read_status()
        self.assertTrue(result["available"])
        self.assertEqual(result["actual"], {"left": False, "right": False})
        self.assertEqual(result["identity"], str(self.device))
        self.assertEqual(self.writes, [])

    def test_other_go2_dmi_is_supported(self):
        self.put(self.dmi / "product_name", "83N1")
        self.assertTrue(imu.read_status()["available"])

    def test_wrong_dmi_is_rejected_without_writing(self):
        for attr, value in (("sys_vendor", "OTHER"), ("product_name", "83E1")):
            with self.subTest(attribute=attr):
                original = (self.dmi / attr).read_text()
                self.put(self.dmi / attr, value)
                result = imu.read_status()
                self.assertFalse(result["available"])
                self.assertIsNone(result["actual"])
                (self.dmi / attr).write_text(original, encoding="ascii")
        self.assertEqual(self.writes, [])

    def test_wrong_driver_interface_or_generation_is_rejected(self):
        self.driver.return_value = "hid-generic"
        self.assertFalse(imu.read_status()["available"])
        self.driver.return_value = "hid-lenovo-go"
        self.put(self.interface / "bInterfaceNumber", "03")
        self.assertFalse(imu.read_status()["available"])
        self.put(self.interface / "bInterfaceNumber", "02")
        self.put(self.device / "hardware_generation", "1")
        self.assertFalse(imu.read_status()["available"])

    def test_missing_or_ambiguous_hardware_is_rejected(self):
        self.make_device("0003:17EF:61EC.0003")
        self.assertIn("More than one", imu.read_status()["reason"])
        with patch.object(imu, "HID_DEVICES", self.dmi):
            self.assertIn("not found", imu.read_status()["reason"])
        self.assertEqual(self.writes, [])

    def test_known_name_outside_physical_sysfs_is_rejected(self):
        other = self.root / "class/fake"
        other.mkdir()
        (other / "0003_17EF_61EB.0002").mkdir()
        with patch.object(imu, "HID_DEVICES", HidInventory(other)):
            self.assertIn("outside physical", imu.read_status()["reason"])

    def test_malformed_or_missing_attributes_are_rejected(self):
        bypass = imu._attribute(self.device, "right")
        for value in ("1", "True", "true false", "", "x" * 513):
            with self.subTest(value=value):
                self.put(bypass, value)
                self.assertFalse(imu.read_status()["available"])
        self.put(bypass, "false")
        options = bypass.with_name("imu_bypass_enabled_index")
        for value in ("false", "true false auto", "true false false"):
            self.put(options, value)
            self.assertFalse(imu.read_status()["available"])
        self.put(options, "true false")
        bypass.unlink()
        self.assertFalse(imu.read_status()["available"])
        self.assertEqual(self.writes, [])

    def test_directory_and_outside_attributes_are_rejected(self):
        bypass = imu._attribute(self.device, "right")
        bypass.unlink()
        bypass.mkdir()
        self.assertFalse(imu.read_status()["available"])
        with self.assertRaises(imu.ImuError):
            imu._regular(self.dmi / "sys_vendor", self.device)

    def test_noop_and_mismatched_expected_state_never_write(self):
        unchanged = {"left": False, "right": False}
        imu.apply(unchanged, unchanged)
        with self.assertRaisesRegex(imu.ImuError, "changed before"):
            imu.apply({"left": True, "right": False}, unchanged)
        self.assertEqual(self.writes, [])

    def test_apply_changes_only_requested_sides_and_verifies_both(self):
        imu.apply(self.actual(), {"left": True, "right": False})
        self.assertEqual(self.writes, [("left", True)])
        imu.apply(self.actual(), {"left": True, "right": True})
        self.assertEqual(self.writes, [("left", True), ("right", True)])
        self.assertEqual(self.actual(), {"left": True, "right": True})

    def test_second_write_failure_rolls_back_partial_first_write(self):
        def write(device, side, value):
            if side == "right" and value:
                raise OSError("kernel refused right")
            self.write(device, side, value)
        self.writer.side_effect = write
        with self.assertRaisesRegex(imu.ImuError, "Previous IMU values were restored"):
            imu.apply(self.actual(), {"left": True, "right": True})
        self.assertEqual(self.actual(), {"left": False, "right": False})
        self.assertEqual(self.writes, [("left", True), ("left", False)])

    def test_write_that_changes_then_raises_is_also_rolled_back(self):
        def write(device, side, value):
            self.write(device, side, value)
            if side == "right" and value:
                raise OSError("disconnected after write")
        self.writer.side_effect = write
        with self.assertRaises(imu.ImuError):
            imu.apply(self.actual(), {"left": True, "right": True})
        self.assertEqual(self.actual(), {"left": False, "right": False})
        self.assertEqual(self.writes[-2:], [("right", False), ("left", False)])

    def test_ignored_second_write_fails_readback_and_rolls_back(self):
        def write(device, side, value):
            if side != "right":
                self.write(device, side, value)
        self.writer.side_effect = write
        with self.assertRaisesRegex(imu.ImuError, "did not confirm"):
            imu.apply(self.actual(), {"left": True, "right": True})
        self.assertEqual(self.actual(), {"left": False, "right": False})

    def test_concurrent_external_change_on_untouched_side_is_preserved(self):
        def write(device, side, value):
            self.write(device, side, value)
            if side == "left" and value:
                self.external("right", True)
        self.writer.side_effect = write
        with self.assertRaisesRegex(imu.ImuError, "changed externally"):
            imu.apply(self.actual(), {"left": True, "right": True})
        self.assertEqual(self.actual(), {"left": False, "right": True})
        self.assertEqual(self.writes, [("left", True), ("left", False)])

    def test_failed_rollback_is_explicit_for_durable_journal_recovery(self):
        def write(device, side, value):
            if side == "right" or not value:
                raise OSError("not writable")
            self.write(device, side, value)
        self.writer.side_effect = write
        with self.assertRaisesRegex(imu.ImuError, "Rollback failed: left"):
            imu.apply(self.actual(), {"left": True, "right": True})
        self.assertEqual(self.actual(), {"left": True, "right": False})

    def test_first_selection_preserves_unselected_original_state(self):
        for side, other in (("left", "right"), ("right", "left")):
            for original in (False, True):
                actual = {side: False, other: original}
                desired = imu.desired_for_source(side, actual, actual)
                self.assertTrue(desired[side])
                self.assertEqual(desired[other], original)
        actual = {"left": False, "right": True}
        self.assertEqual(imu.desired_for_source("system", actual, actual), actual)

    def test_switch_and_release_restore_only_still_owned_values(self):
        baseline = {"left": False, "right": False}
        owned = {"left": True, "right": True}
        self.assertEqual(imu.desired_for_source("left", baseline, owned, owned),
                         {"left": True, "right": False})
        self.assertEqual(imu.desired_for_source("right", baseline, owned, owned),
                         {"left": False, "right": True})
        self.assertEqual(imu.desired_for_source("system", baseline, owned, owned), baseline)
        external = {"left": False, "right": True}
        baseline = {"left": True, "right": False}
        self.assertEqual(imu.desired_for_source("system", baseline, external, owned),
                         {"left": False, "right": False})

    def test_strict_boolean_maps_and_sources_reject_invalid_input(self):
        valid = {"left": False, "right": False}
        for invalid in ({"left": 1, "right": False}, {"left": False},
                        {**valid, "path": "/arbitrary"}, None, [False, False]):
            with self.subTest(value=invalid):
                with self.assertRaises(imu.ImuError):
                    imu.apply(valid, invalid)
                with self.assertRaises(imu.ImuError):
                    imu.desired_for_source("combined", invalid, valid)
        for source in ("invalid", "../imu_enabled", [], True):
            with self.assertRaises(imu.ImuError):
                imu.desired_for_source(source, valid, valid)
        self.assertEqual(self.writes, [])


if __name__ == "__main__":
    unittest.main()

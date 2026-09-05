# SPDX-License-Identifier: BSD-3-Clause
"""Regression fixtures captured from Legion Go 2 reports on SteamOS.

These are report bytes only: no device serial numbers, account information,
local paths or settings. Captured on 2026-09-05 while moving the controllers
and touching the pad. Motion fields were zero in this capture; a valid zero
sample must remain distinct from a missing or unrecognized input report.
"""

import unittest

import test_integration  # Install the existing isolated Decky environment.
import controller_backend as controller


NATIVE_TOUCH = bytes.fromhex(
    "043c7401006404640401010102028080808000000000000002800213018e8080"
    "8080000000000000000000000000000000000000000000000000000000000000"
)
NATIVE_RELEASE = bytes.fromhex(
    "043c740100640464040101010202808080800000000000000280000000008080"
    "8080000000000000000000000000000000000000000000000000000000000000"
)
CONFIG_REPLY = bytes.fromhex(
    "0400050403020000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
)
OTHER_INTERFACE_TOUCH = bytes.fromhex("010313028e010000000000000000000004530100")
VIRTUAL_TOUCH = bytes.fromhex(
    "01000940749d05000000100000000000000000003f01bf140000000000000000"
    "0000000000000000000000000000000000000000800080ff0000000000000000"
)
VIRTUAL_RELEASE = bytes.fromhex(
    "01000940039f05000000000000000000000000003f5e7f780000000000000000"
    "0000000000000000000000000000000000000000800080ff0000000000000000"
)


class CapturedControllerReportTests(unittest.TestCase):
    def test_native_kernel_padding_and_hidapi_short_read_match(self):
        self.assertEqual(len(NATIVE_TOUCH), 64)
        full = controller.parse_physical_report(NATIVE_TOUCH)
        short = controller.parse_physical_report(NATIVE_TOUCH[:60])
        self.assertIsNotNone(full)
        self.assertEqual(full, short)
        self.assertEqual(full["touchpad"], {
            "is_touching": True, "raw_x": 531, "raw_y": 398,
            "x": 531 / 1024, "y": 398 / 1024,
        })
        self.assertEqual(full["gyro_left"], {"x": 0, "y": 0, "z": 0})
        self.assertEqual(full["gyro_right"], {"x": 0, "y": 0, "z": 0})
        self.assertEqual(full["battery_left"], 100)
        self.assertEqual(full["battery_right"], 100)
        self.assertEqual(full["connection_left"], "attached")
        self.assertEqual(full["connection_right"], "attached")

    def test_native_release_is_a_valid_report_with_absent_coordinates(self):
        sample = controller.parse_physical_report(NATIVE_RELEASE)
        self.assertIsNotNone(sample)
        self.assertFalse(sample["touchpad"]["is_touching"])
        self.assertIsNone(sample["touchpad"]["x"])
        self.assertIsNone(sample["touchpad"]["y"])
        self.assertEqual(sample["gyro_left"], {"x": 0, "y": 0, "z": 0})

    def test_real_configuration_reply_and_neighboring_interface_are_not_input(self):
        self.assertEqual(len(CONFIG_REPLY), 64)
        for report in (CONFIG_REPLY, CONFIG_REPLY[:60], OTHER_INTERFACE_TOUCH):
            with self.subTest(report=report.hex()):
                self.assertIsNone(controller.parse_physical_report(report))
                self.assertIsNone(controller.parse_virtual_report(report))

    def test_virtual_touch_bit_and_stale_coordinates_from_actual_reports(self):
        self.assertEqual(len(VIRTUAL_TOUCH), 64)
        touched = controller.parse_virtual_report(VIRTUAL_TOUCH)
        self.assertIsNotNone(touched)
        self.assertEqual(touched["touchpad"]["raw_x"], 319)
        self.assertEqual(touched["touchpad"]["raw_y"], 5311)
        self.assertTrue(touched["touchpad"]["is_touching"])
        self.assertEqual(touched["touchpad"]["x"], (32767 + 319) / 65534)
        self.assertEqual(touched["touchpad"]["y"], (32767 - 5311) / 65534)
        self.assertEqual(touched["gyro"], {"x": 0, "y": 0, "z": 0})
        released = controller.parse_virtual_report(VIRTUAL_RELEASE)
        self.assertIsNotNone(released)
        self.assertFalse(released["touchpad"]["is_touching"])
        self.assertNotEqual(released["touchpad"]["raw_x"], 0)
        self.assertIsNone(released["touchpad"]["x"])
        self.assertIsNone(released["touchpad"]["y"])


if __name__ == "__main__":
    unittest.main()

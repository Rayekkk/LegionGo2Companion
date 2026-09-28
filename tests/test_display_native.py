# SPDX-License-Identifier: BSD-3-Clause
"""The native profile remains authoritative across upstream implementations."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import display_native as native


class NativeDisplayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.script = Path(directory.name) / "native.lua"
        patched = patch.object(native, "SYSTEM_SCRIPT", str(self.script))
        patched.start()
        self.addCleanup(patched.stop)

    def test_any_file_contents_indicate_support_without_reading_or_executing(self):
        for contents in (b"", b"-- Future native PQ support, no workaround flags",
                         b"content_driven = false", b"\xff\x00"):
            with self.subTest(contents=contents):
                self.script.write_bytes(contents)
                with patch("builtins.open", side_effect=AssertionError("No content reads")):
                    result = native.detect_native_display_support()
                self.assertTrue(result["supported"])
                self.assertEqual(result["status"], "supported")
                self.assertEqual(result["script_path"], str(self.script))

    def test_profile_appearance_and_removal_are_observed(self):
        self.assertEqual(native.detect_native_display_support()["status"], "absent")
        self.script.touch()
        self.assertTrue(native.detect_native_display_support()["supported"])
        self.script.unlink()
        self.assertEqual(native.detect_native_display_support()["status"], "absent")

    def test_directory_is_not_a_profile_file(self):
        self.script.mkdir()
        result = native.detect_native_display_support()
        self.assertFalse(result["supported"])
        self.assertEqual(result["status"], "inconclusive")

    def test_access_error_does_not_report_profile_removed(self):
        for error in (PermissionError, OSError):
            with self.subTest(error=error), patch.object(native.os, "stat", side_effect=error):
                result = native.detect_native_display_support()
                self.assertFalse(result["supported"])
                self.assertEqual(result["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()

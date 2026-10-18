# SPDX-License-Identifier: BSD-3-Clause
"""Version labels may be cached; live controller profiles must not be."""
from contextlib import ExitStack
import subprocess
import threading
import unittest
from unittest.mock import patch

import test_remap_backend  # Isolated Decky stub and a valid profile fixture.
import remap_backend as remap


class RemapMetadataTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(remap, "_version_cache", None))
        stack.enter_context(patch.object(remap, "_version_lock", threading.Lock()))
        self.now = 100.0
        stack.enter_context(patch.object(remap.time, "monotonic", side_effect=lambda: self.now))
        self.run = stack.enter_context(patch.object(remap.subprocess, "run", return_value=self.result("inputplumber 1.0")))

    @staticmethod
    def result(value, returncode=0):
        return subprocess.CompletedProcess(["inputplumber", "--version"], returncode, value + "\n")

    def test_repeated_statuses_cache_version_but_read_the_current_profile(self):
        profile = test_remap_backend.BASE_PROFILE
        with patch.object(remap, "_load_state", return_value=dict(remap.DEFAULT_STATE)),              patch.object(remap, "_find_device", return_value=("device", "source")),              patch.object(remap, "_get_profile", side_effect=[profile, profile.replace("Custom User Profile", "Changed Profile")]) as read:
            first, second = remap._status_sync(), remap._status_sync()
        self.assertEqual(first["inputplumber_version"], "inputplumber 1.0")
        self.assertEqual(second["inputplumber_version"], first["inputplumber_version"])
        self.assertEqual(first["profile_name"], "Custom User Profile")
        self.assertEqual(second["profile_name"], "Changed Profile")
        self.assertEqual(read.call_count, 2)
        self.run.assert_called_once()

    def test_metadata_failure_does_not_hide_a_working_controller(self):
        self.run.side_effect = subprocess.TimeoutExpired("inputplumber", 3)
        with patch.object(remap, "_load_state", return_value=dict(remap.DEFAULT_STATE)),              patch.object(remap, "_find_device", return_value=("device", "source")),              patch.object(remap, "_get_profile", return_value=test_remap_backend.BASE_PROFILE):
            status = remap._status_sync()
        self.assertTrue(status["supported"])
        self.assertEqual(status["inputplumber_version"], "")
        self.assertEqual(status["profile_name"], "Custom User Profile")


if __name__ == "__main__":
    unittest.main()

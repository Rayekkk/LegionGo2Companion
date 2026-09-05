# SPDX-License-Identifier: BSD-3-Clause
"""Version labels may be cached; live controller profiles must not be."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_remap_backend  # Isolated Decky stub and a valid profile fixture.
import remap_backend as remap


class RemapMetadataTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = stack.enter_context(tempfile.TemporaryDirectory())
        self.binary = Path(directory) / "inputplumber"
        self.binary.write_bytes(b"binary one")
        stack.enter_context(patch.object(remap, "INPUTPLUMBER", str(self.binary)))
        stack.enter_context(patch.object(remap, "_version_cache", None))
        stack.enter_context(patch.object(remap, "_version_lock", threading.Lock()))
        self.now = 100.0
        stack.enter_context(patch.object(remap.time, "monotonic", side_effect=lambda: self.now))
        self.run = stack.enter_context(patch.object(remap.subprocess, "run", return_value=self.result("inputplumber 1.0")))

    @staticmethod
    def result(value, returncode=0):
        return subprocess.CompletedProcess(["inputplumber", "--version"], returncode, value + "\n")

    def replace_binary(self):
        previous = self.binary.stat()
        replacement = self.binary.with_name("replacement")
        replacement.write_bytes(b"binary two")
        os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        os.replace(replacement, self.binary)

    def test_repeated_statuses_cache_version_but_read_the_current_profile(self):
        profile = test_remap_backend.BASE_PROFILE
        with patch.object(remap, "_load_state", return_value=dict(remap.DEFAULT_STATE)), \
             patch.object(remap, "_find_device", return_value=("device", "source")), \
             patch.object(remap, "_get_profile", side_effect=[profile, profile.replace("Custom User Profile", "Changed Profile")]) as read:
            first, second = remap._status_sync(), remap._status_sync()
        self.assertEqual(first["inputplumber_version"], "inputplumber 1.0")
        self.assertEqual(second["inputplumber_version"], first["inputplumber_version"])
        self.assertEqual(first["profile_name"], "Custom User Profile")
        self.assertEqual(second["profile_name"], "Changed Profile")
        self.assertEqual(read.call_count, 2)
        self.run.assert_called_once()

    def test_success_expires_after_the_bounded_ttl(self):
        self.assertEqual(remap._inputplumber_version(), "inputplumber 1.0")
        self.now += remap.VERSION_CACHE_TTL_S - 0.01
        self.assertEqual(remap._inputplumber_version(), "inputplumber 1.0")
        self.run.return_value = self.result("inputplumber 2.0")
        self.now += 0.01
        self.assertEqual(remap._inputplumber_version(), "inputplumber 2.0")
        self.assertEqual(self.run.call_count, 2)

    def test_same_size_and_timestamp_replacement_invalidates_immediately(self):
        remap._inputplumber_version()
        self.replace_binary()
        self.run.return_value = self.result("inputplumber 2.0")
        self.assertEqual(remap._inputplumber_version(), "inputplumber 2.0")
        self.assertEqual(self.run.call_count, 2)

    def test_missing_binary_clears_the_label_and_reappearance_is_probed(self):
        remap._inputplumber_version()
        self.binary.unlink()
        self.assertEqual(remap._inputplumber_version(), "")
        self.assertIsNone(remap._version_cache)
        self.run.assert_called_once()
        self.binary.write_bytes(b"binary two")
        self.run.return_value = self.result("inputplumber 2.0")
        self.assertEqual(remap._inputplumber_version(), "inputplumber 2.0")

    def test_stat_failure_discards_a_previously_confirmed_label(self):
        remap._inputplumber_version()
        with patch.object(remap.os, "stat", side_effect=PermissionError("unavailable")):
            self.assertEqual(remap._inputplumber_version(), "")
        self.assertIsNone(remap._version_cache)

    def test_probe_failure_clears_old_label_and_retries_with_a_short_delay(self):
        failures = [subprocess.TimeoutExpired("inputplumber", 3), OSError("not executable"),
                    self.result("failed", 1), self.result("")]
        for failure in failures:
            with self.subTest(failure=failure):
                remap._version_cache = None
                self.run.reset_mock(side_effect=True, return_value=True)
                self.run.return_value = self.result("inputplumber 1.0")
                remap._inputplumber_version()
                self.now += remap.VERSION_CACHE_TTL_S
                if isinstance(failure, Exception):
                    self.run.side_effect = failure
                else:
                    self.run.return_value = failure
                self.assertEqual(remap._inputplumber_version(), "")
                self.assertEqual(remap._inputplumber_version(), "")
                self.assertEqual(self.run.call_count, 2)
                self.run.side_effect = None
                self.run.return_value = self.result("inputplumber 2.0")
                self.now += remap.VERSION_RETRY_S
                self.assertEqual(remap._inputplumber_version(), "inputplumber 2.0")
                self.assertEqual(self.run.call_count, 3)

    def test_metadata_failure_does_not_hide_a_working_controller(self):
        self.run.side_effect = subprocess.TimeoutExpired("inputplumber", 3)
        with patch.object(remap, "_load_state", return_value=dict(remap.DEFAULT_STATE)), \
             patch.object(remap, "_find_device", return_value=("device", "source")), \
             patch.object(remap, "_get_profile", return_value=test_remap_backend.BASE_PROFILE):
            status = remap._status_sync()
        self.assertTrue(status["supported"])
        self.assertEqual(status["inputplumber_version"], "")
        self.assertEqual(status["profile_name"], "Custom User Profile")

    def test_concurrent_readers_share_one_probe(self):
        start, entered, release = threading.Barrier(9), threading.Event(), threading.Event()
        def probe(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("probe was not released")
            return self.result("inputplumber 1.0")
        def read():
            start.wait(timeout=2)
            return remap._inputplumber_version()
        self.run.side_effect = probe
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(read) for _ in range(8)]
            try:
                start.wait(timeout=2)
                self.assertTrue(entered.wait(2))
            finally:
                release.set()
            self.assertEqual([future.result(timeout=2) for future in futures], ["inputplumber 1.0"] * 8)
        self.run.assert_called_once()

    def test_replacement_during_probe_is_not_cached_under_the_new_binary(self):
        def probe(*args, **kwargs):
            self.replace_binary()
            return self.result("inputplumber 1.0")
        self.run.side_effect = probe
        self.assertEqual(remap._inputplumber_version(), "")
        self.assertIsNone(remap._version_cache)
        self.run.side_effect = None
        self.run.return_value = self.result("inputplumber 2.0")
        self.assertEqual(remap._inputplumber_version(), "inputplumber 2.0")


if __name__ == "__main__":
    unittest.main()

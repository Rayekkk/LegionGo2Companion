# SPDX-License-Identifier: BSD-3-Clause
"""Real process termination during isolated settings writes, not power-loss tests.

Only the checkpoint handshake is injected. JSON, temporary files, flush/fsync,
rename, and subsequent recovery use the production storage implementation.
The child is killed without Python cleanup (SIGKILL on POSIX, TerminateProcess
on Windows); another fresh process then opens the same temporary directory.
No backend or hardware module is imported.
"""

import copy
import json
import multiprocessing
import os
import signal
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

import safe_settings


OLD = {"state": {"gyro_source": "system", "ownership": None},
       "unrelated": {"label": "zażółć", "values": list(range(128))}}
PENDING = copy.deepcopy(OLD)
PENDING["state"] = {
    "gyro_source": "left",
    "ownership": {
        "identity": "isolated-test-device", "pending": True,
        "baseline": [], "previous": [],
        "applied": ["Accelerometer:Right", "Gyroscope:Right"],
        "imu_baseline": {"left": False, "right": False},
        "imu_previous": {"left": False, "right": False},
        "imu_applied": {"left": True, "right": False},
    },
}


def _storage_child(connection, directory, operation, checkpoint, payload):
    """Pause inside a real write until the parent forcibly kills this process."""
    def pause(reached):
        if reached == checkpoint:
            connection.send({"checkpoint": reached})
            connection.recv()  # Parent kills us; it never acknowledges this.
            raise AssertionError("a crash checkpoint must not resume")

    original_replace = os.replace
    original_fdopen = os.fdopen

    def replace(source, destination, *args, **kwargs):
        leaf = os.path.basename(os.fspath(destination))
        pause("before_replace:" + leaf)
        result = original_replace(source, destination, *args, **kwargs)
        if leaf.startswith("module.json.corrupt-"):
            pause("after_quarantine")
        pause("after_replace:" + leaf)
        return result

    class PartialWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def write(self, data):
            middle = len(data) // 2
            self.handle.write(data[:middle])
            self.handle.flush()
            pause("partial_temporary")
            return middle + self.handle.write(data[middle:])

    def fdopen(fd, mode, *args, **kwargs):
        handle = original_fdopen(fd, mode, *args, **kwargs)
        return PartialWriter(handle) if checkpoint == "partial_temporary" and mode == "wb" else handle

    try:
        with patch.object(safe_settings.os, "replace", side_effect=replace), \
             patch.object(safe_settings.os, "fdopen", side_effect=fdopen):
            if operation == "atomic":
                safe_settings.atomic_write_json(os.path.join(directory, "module.json"), payload)
            else:
                manager = safe_settings.AtomicSettingsManager("module", directory)
                if operation == "commit":
                    manager.replace(payload)
                connection.send({"settings": manager.settings, "recovery_error": manager.recovery_error})
    except BaseException:
        connection.send({"error": traceback.format_exc()})
    finally:
        connection.close()


class SettingsProcessCrashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="legiongo2-process-crash-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.context = multiprocessing.get_context("spawn")

    def seed(self, payload):
        safe_settings.AtomicSettingsManager("module", str(self.directory)).replace(payload)

    def run_child(self, operation, checkpoint=None, payload=None):
        parent, child = self.context.Pipe()
        process = self.context.Process(target=_storage_child,
                                       args=(child, str(self.directory), operation, checkpoint, payload))
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(10), f"child did not reach {checkpoint or operation} within 10 seconds")
            message = parent.recv()
            self.assertNotIn("error", message, message.get("error"))
            if checkpoint:
                self.assertEqual(message, {"checkpoint": checkpoint})
                self.assertTrue(process.is_alive(), "writer must be alive at the requested checkpoint")
                process.kill()
                process.join(5)
                self.assertFalse(process.is_alive(), "killed writer did not exit")
                self.assertNotEqual(process.exitcode, 0)
                if os.name == "posix":
                    self.assertEqual(process.exitcode, -signal.SIGKILL)
            else:
                process.join(5)
                self.assertEqual(process.exitcode, 0)
            return message
        finally:
            if process.is_alive():
                process.kill()
                process.join(5)
            parent.close()
            process.close()

    def assert_reopened(self, expected):
        recovered = self.run_child("read")
        self.assertEqual(recovered["settings"], expected)
        self.assertEqual(recovered["recovery_error"], "")
        self.assertEqual(json.loads((self.directory / "module.json").read_text(encoding="utf-8")), expected)

    def test_kill_with_partial_temporary_json_keeps_the_complete_previous_state(self):
        self.seed(OLD)
        self.run_child("atomic", "partial_temporary", PENDING)
        temporaries = list(self.directory.glob("*.tmp"))
        self.assertEqual(len(temporaries), 1, "abrupt termination must bypass temporary-file cleanup")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(temporaries[0].read_text(encoding="utf-8"))
        self.assert_reopened(OLD)

    def test_kill_at_primary_and_backup_boundaries_keeps_a_complete_transaction(self):
        for checkpoint, primary, backup in (
            ("before_replace:module.json", OLD, OLD),
            ("after_replace:module.json", PENDING, OLD),
            ("before_replace:module.json.bak", PENDING, OLD),
            ("after_replace:module.json.bak", PENDING, PENDING),
        ):
            with self.subTest(checkpoint=checkpoint):
                self.seed(OLD)
                self.run_child("commit", checkpoint, PENDING)
                self.assertEqual(json.loads((self.directory / "module.json.bak").read_text(encoding="utf-8")), backup)
                self.assert_reopened(primary)

    def test_kill_before_journal_completion_preserves_pending_intent_and_baseline(self):
        self.seed(PENDING)
        completed = copy.deepcopy(PENDING)
        completed["state"]["ownership"]["pending"] = False
        self.run_child("commit", "before_replace:module.json", completed)
        self.assert_reopened(PENDING)

    def test_kill_during_backup_recovery_does_not_turn_lost_primary_into_first_install(self):
        for checkpoint in ("after_quarantine", "before_replace:module.json"):
            with self.subTest(checkpoint=checkpoint):
                self.seed(PENDING)
                (self.directory / "module.json").write_text("{broken", encoding="utf-8")
                self.run_child("read", checkpoint)
                self.assertFalse((self.directory / "module.json").exists())
                self.assert_reopened(PENDING)

    def test_kill_before_explicit_repair_keeps_unrecoverable_storage_distinct_from_new_install(self):
        primary = self.directory / "module.json"
        backup = self.directory / "module.json.bak"
        primary.write_text("{broken", encoding="utf-8")
        backup.write_text("[]", encoding="utf-8")
        self.run_child("commit", "before_replace:module.json", PENDING)
        recovered = self.run_child("read")
        self.assertEqual(recovered["settings"], {})
        self.assertIn("primary:", recovered["recovery_error"])
        self.assertIn("backup:", recovered["recovery_error"])
        self.assertEqual(primary.read_text(encoding="utf-8"), "{broken")
        self.assertEqual(backup.read_text(encoding="utf-8"), "[]")


if __name__ == "__main__":
    unittest.main()

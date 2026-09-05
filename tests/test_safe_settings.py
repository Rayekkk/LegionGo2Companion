# SPDX-License-Identifier: BSD-3-Clause

import json
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import safe_settings


class AtomicSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="legiongo2-safe-settings-")
        self.root = Path(self.temp.name) / "settings"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def manager(self):
        return safe_settings.AtomicSettingsManager("module", str(self.root))

    def test_set_is_memory_only_and_commit_writes_complete_snapshot(self):
        manager = self.manager()
        manager.setSetting("first", {"value": 1})
        manager.setSetting("second", {"value": 2})
        self.assertFalse(Path(manager.path).exists())

        manager.commit()
        self.assertEqual(
            json.loads(Path(manager.path).read_text(encoding="utf-8")),
            {"first": {"value": 1}, "second": {"value": 2}},
        )
        self.assertEqual(
            json.loads(Path(manager.backup_path).read_text(encoding="utf-8")),
            {"first": {"value": 1}, "second": {"value": 2}},
        )

    def test_failed_replace_keeps_previous_primary_and_cleans_temporary(self):
        manager = self.manager()
        manager.setSetting("value", "old")
        manager.commit()
        real_replace = safe_settings.os.replace

        def fail_primary(source, destination, *args, **kwargs):
            if os.path.basename(os.fspath(destination)) == "module.json":
                raise OSError("injected replace failure")
            return real_replace(source, destination, *args, **kwargs)

        manager.setSetting("value", "new")
        with patch.object(safe_settings.os, "replace", side_effect=fail_primary):
            with self.assertRaises(OSError):
                manager.commit()

        self.assertEqual(
            json.loads(Path(manager.path).read_text(encoding="utf-8"))["value"],
            "old",
        )
        self.assertEqual(manager.getSetting("value"), "old")
        self.assertFalse(any(path.suffix == ".tmp" for path in self.root.iterdir()))

        # A later successful transaction must not accidentally persist the
        # value rejected above.
        manager.setSetting("other", True)
        manager.commit()
        self.assertEqual(
            json.loads(Path(manager.path).read_text(encoding="utf-8")),
            {"value": "old", "other": True},
        )

    def test_corrupt_primary_is_quarantined_and_restored_from_backup(self):
        manager = self.manager()
        manager.setSetting("value", {"kept": True})
        manager.commit()
        Path(manager.path).write_text("{broken", encoding="utf-8")

        recovered = self.manager()
        self.assertEqual(recovered.getSetting("value"), {"kept": True})
        self.assertEqual(
            json.loads(Path(recovered.path).read_text(encoding="utf-8"))["value"],
            {"kept": True},
        )
        self.assertEqual(len(list(self.root.glob("module.json.corrupt-*"))), 1)

    @unittest.skipUnless(os.name == "posix", "POSIX directory fsync")
    def test_directory_fsync_failure_after_replace_keeps_committed_value(self):
        manager = self.manager()
        manager.replace({"value": "old"})
        original_fsync = safe_settings.os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected directory fsync failure")
            return original_fsync(fd)
        with patch.object(safe_settings.os, "fsync", side_effect=fail_directory), \
             patch.object(safe_settings, "_log") as log:
            manager.replace({"value": "new"})
        self.assertEqual(manager.getSetting("value"), "new")
        self.assertEqual(json.loads(Path(manager.path).read_text(encoding="utf-8")),
                         {"value": "new"})
        self.assertTrue(any("durability could not be confirmed" in call.args[1]
                            for call in log.call_args_list))

    def test_existing_primary_gets_a_recovery_backup_on_first_read(self):
        path = self.root / "module.json"
        path.write_text('{"value": 7}', encoding="utf-8")
        manager = self.manager()
        self.assertEqual(manager.getSetting("value"), 7)
        self.assertEqual(
            json.loads(Path(manager.backup_path).read_text(encoding="utf-8")),
            {"value": 7},
        )

    @unittest.skipUnless(os.name == "posix", "POSIX permission check")
    def test_existing_primary_is_made_private_on_first_read(self):
        path = self.root / "module.json"
        path.write_text('{"value": 7}', encoding="utf-8")
        os.chmod(path, 0o644)
        self.manager()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_corrupt_file_without_backup_preserves_evidence_across_restarts(self):
        path = self.root / "module.json"
        path.write_text("[]", encoding="utf-8")
        manager = self.manager()
        self.assertEqual(manager.settings, {})
        self.assertEqual(path.read_text(encoding="utf-8"), "[]")
        self.assertEqual(len(list(self.root.glob("module.json.corrupt-*"))), 0)
        self.assertIn("primary:", manager.recovery_error)
        # Neither a component reload nor a new process may treat data loss as
        # first install before the caller persists its recovery choice.
        manager.read()
        self.assertTrue(manager.recovery_error)
        self.assertTrue(self.manager().recovery_error)

    def test_missing_settings_are_distinct_from_failed_recovery(self):
        self.assertEqual(self.manager().recovery_error, "")

    def test_both_corrupt_copies_report_lost_intent_until_a_successful_commit(self):
        (self.root / "module.json").write_text("{broken", encoding="utf-8")
        (self.root / "module.json.bak").write_text("[]", encoding="utf-8")
        manager = self.manager()
        self.assertEqual(manager.settings, {})
        self.assertIn("primary:", manager.recovery_error)
        self.assertIn("backup:", manager.recovery_error)
        manager.setSetting("enabled", False)
        with patch.object(safe_settings, "atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                manager.commit()
        self.assertTrue(manager.recovery_error)
        manager.setSetting("enabled", False)
        manager.commit()
        self.assertEqual(manager.recovery_error, "")
        self.assertEqual(self.manager().settings, {"enabled": False})

    def test_backup_recovery_does_not_report_lost_intent(self):
        (self.root / "module.json").write_text("{broken", encoding="utf-8")
        (self.root / "module.json.bak").write_text('{"enabled": false}', encoding="utf-8")
        manager = self.manager()
        self.assertFalse(manager.getSetting("enabled"))
        self.assertEqual(manager.recovery_error, "")

    def test_corrupt_backup_without_primary_reports_lost_intent(self):
        (self.root / "module.json.bak").write_text("null", encoding="utf-8")
        manager = self.manager()
        self.assertIn("backup:", manager.recovery_error)

    def test_excessively_nested_primary_recovers_valid_backup(self):
        # 2000 trips JSON's parser; 600 used to parse successfully and fail in
        # deepcopy instead, bypassing the otherwise valid recovery backup.
        for depth in (600, 2000):
            with self.subTest(depth=depth):
                (self.root / "module.json").write_text(
                    '{"value":' + '[' * depth + '0' + ']' * depth + '}', encoding="utf-8")
                (self.root / "module.json.bak").write_text('{"enabled": false}', encoding="utf-8")
                manager = self.manager()
                self.assertEqual(manager.settings, {"enabled": False})
                self.assertEqual(manager.recovery_error, "")

    def test_read_enforces_byte_limit_when_file_grows_after_stat(self):
        path = self.root / "module.json"
        path.write_text("{}", encoding="utf-8")
        original_fdopen = safe_settings.os.fdopen
        reads = []
        class GrowingFile(io.BytesIO):
            def read(self, size=-1):
                reads.append(size)
                return super().read(size)
        def grown_after_stat(fd, *args, **kwargs):
            # Close the real local descriptor and emulate content appended
            # after its bounded fstat. No shared or device paths are touched.
            with original_fdopen(fd, "rb"):
                pass
            return GrowingFile(b" " * (safe_settings.MAX_SETTINGS_BYTES + 2))
        with patch.object(safe_settings.os, "fdopen", side_effect=grown_after_stat):
            with self.assertRaises(safe_settings.CorruptSettings):
                safe_settings.load_json_object(str(path))
        self.assertEqual(reads, [safe_settings.MAX_SETTINGS_BYTES + 1])

    def test_symlink_target_is_blocked_without_touching_its_destination(self):
        victim = Path(self.temp.name) / "victim"
        victim.write_text("do not change", encoding="utf-8")
        link = self.root / "module.json"
        try:
            link.symlink_to(victim)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        manager = self.manager()
        with self.assertRaises(safe_settings.UnsafeSettingsPath):
            manager.setSetting("attack", True)
        self.assertEqual(victim.read_text(encoding="utf-8"), "do not change")

    @unittest.skipUnless(os.name == "posix", "POSIX permission check")
    def test_committed_files_are_private(self):
        manager = self.manager()
        manager.setSetting("value", 1)
        manager.commit()
        self.assertEqual(stat.S_IMODE(os.stat(manager.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(manager.backup_path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()

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

        with patch.object(safe_settings.dk_fs, "write_json", wraps=safe_settings.dk_fs.write_json) as write:
            manager.commit()
        self.assertEqual(write.call_count, 1)
        self.assertEqual(Path(manager.path).read_bytes(), Path(manager.backup_path).read_bytes())
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

    def test_invalid_pending_json_restores_an_independent_committed_snapshot(self):
        manager = self.manager()
        manager.replace({"nested": {"values": [1, 2]}})
        before = Path(manager.path).read_bytes()
        manager.getSetting("nested")["values"].append(3)
        manager.setSetting("invalid", {1, 2})
        with self.assertRaises(TypeError):
            manager.commit()
        self.assertEqual(manager.settings, {"nested": {"values": [1, 2]}})
        self.assertEqual(Path(manager.path).read_bytes(), before)
        self.assertEqual(Path(manager.backup_path).read_bytes(), before)
        manager.setSetting("other", True)
        manager.commit()
        self.assertEqual(self.manager().settings, {"nested": {"values": [1, 2]}, "other": True})

    def test_backup_write_failure_keeps_primary_and_in_memory_commit(self):
        manager = self.manager()
        manager.replace({"value": "old"})
        write_atomic = safe_settings.dk_fs.write_atomic
        def fail_backup(path, payload, *args, **kwargs):
            if path == manager.backup_path:
                raise OSError("backup unavailable")
            return write_atomic(path, payload, *args, **kwargs)
        with patch.object(safe_settings.dk_fs, "write_atomic", side_effect=fail_backup), \
             patch.object(safe_settings, "transition") as log:
            manager.replace({"value": "new"})
        self.assertEqual(manager.getSetting("value"), "new")
        self.assertEqual(self.manager().getSetting("value"), "new")
        self.assertEqual(json.loads(Path(manager.backup_path).read_text(encoding="utf-8")), {"value": "old"})
        log.assert_called_once()
        self.assertEqual(log.call_args.args, ("settings", "backup_refresh", False))
        self.assertEqual(log.call_args.kwargs["store"], "module")
        self.assertEqual(log.call_args.kwargs["level"], "warning")
        manager.commit()
        self.assertEqual(Path(manager.path).read_bytes(), Path(manager.backup_path).read_bytes())

    @unittest.skipUnless(os.name == "posix", "POSIX directory fsync")
    def test_directory_fsync_failure_after_replace_keeps_committed_value(self):
        manager = self.manager()
        manager.replace({"value": "old"})
        original_fsync = safe_settings.dk_fs.os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected directory fsync failure")
            return original_fsync(fd)
        with patch.object(safe_settings.dk_fs.os, "fsync", side_effect=fail_directory), \
             patch.object(safe_settings, "_log") as log:
            manager.replace({"value": "new"})
            safe_settings._atomic_write_bytes(str(self.root / "journal.json"), b"{}")
            with self.assertRaises(OSError):
                safe_settings._atomic_write_bytes(str(self.root / "journal.json"), b"{}", strict_sync=True)
        self.assertEqual(manager.getSetting("value"), "new")
        self.assertEqual(json.loads(Path(manager.path).read_text(encoding="utf-8")),
                         {"value": "new"})
        self.assertTrue(any(call.args == ("warning", "directory_durability_unconfirmed")
                            and call.kwargs.get("store") == "journal.json"
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
        evidence = list(self.root.glob("module.json.corrupt-*"))  # kept: the repair commit replaces the file
        self.assertEqual([copy.read_text(encoding="utf-8") for copy in evidence], ["[]"])
        self.assertIn("module.json:", manager.recovery_error)
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
        self.assertIn("module.json:", manager.recovery_error)
        self.assertIn("last-good copy:", manager.recovery_error)
        manager.setSetting("enabled", False)
        with patch.object(safe_settings.dk_fs, "write_atomic", side_effect=OSError("disk full")):
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
        self.assertIn("last-good copy:", manager.recovery_error)

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
        reads, consumed = [], []
        class GrowingFile(io.BytesIO):
            def read(self, size=-1):
                reads.append(size)
                chunk = super().read(size)
                consumed.append(len(chunk))
                return chunk
        def grown_after_stat(fd, *args, **kwargs):
            # Close the real local descriptor and emulate content appended
            # after its bounded fstat. No shared or device paths are touched.
            with original_fdopen(fd, "rb"):
                pass
            return GrowingFile(b" " * (safe_settings.MAX_SETTINGS_BYTES + 2))
        with patch.object(safe_settings.os, "fdopen", side_effect=grown_after_stat):
            with self.assertRaises(safe_settings.CorruptSettings):
                safe_settings.load_json_object(str(path))
        self.assertTrue(reads)
        self.assertTrue(all(0 < size <= safe_settings.MAX_SETTINGS_BYTES + 1 for size in reads), reads)
        self.assertLessEqual(sum(consumed), safe_settings.MAX_SETTINGS_BYTES + 1)

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

    @unittest.skipUnless(os.name == "posix", "POSIX permission check")
    def test_a_rewrite_is_private_unless_the_caller_keeps_a_system_files_mode(self):
        path = self.root / "file.json"
        path.write_text("{}"); path.chmod(0o644)
        safe_settings.atomic_write_json(str(path), {"a": 1})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        path.chmod(0o644)
        safe_settings._atomic_write_bytes(str(path), b"{}\n", keep_existing=True)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    @unittest.skipUnless(os.name == "posix", "POSIX symlinks")
    def test_an_atomic_write_never_replaces_a_link_at_the_target(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text("{}")
        link = self.root / "module.json"
        link.symlink_to(outside)
        with self.assertRaises(safe_settings.UnsafeSettingsPath):
            safe_settings.atomic_write_json(str(link), {"a": 1})
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_text(), "{}")

    def test_a_first_install_creates_its_own_settings_directory(self):
        fresh = self.root / "fresh"
        manager = safe_settings.AtomicSettingsManager("module", str(fresh))
        manager.setSetting("a", 1)
        manager.commit()
        self.assertEqual(json.loads((fresh / "module.json").read_text(encoding="utf-8")), {"a": 1})
        self.assertFalse((self.root / "module.json").exists())
        if os.name == "posix":
            mask = os.umask(0); os.umask(mask)
            self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o755 & ~mask)

    def test_settings_names_are_one_plain_path_component(self):
        for name in ("", ".", "..", "a/b", "a\x00b", None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_settings.AtomicSettingsManager(name, str(self.root))

    def test_nesting_is_bounded_anywhere_in_the_file(self):
        deep = "[" * 100 + "]" * 100
        (self.root / "module.json").write_text('{"first": [], "zero": 0, "second": ' + deep + '}', encoding="utf-8")
        with self.assertRaises(safe_settings.CorruptSettings):
            safe_settings.load_json_object(str(self.root / "module.json"))

    def test_stored_values_never_alias_the_callers_objects_and_defaults_apply(self):
        manager = self.manager()
        value = {"nested": [1]}
        manager.setSetting("a", value)
        value["nested"].append(2)
        self.assertEqual(manager.getSetting("a"), {"nested": [1]})
        payload = {"b": {"nested": [1]}}
        manager.replace(payload)
        payload["b"]["nested"].append(2)
        self.assertEqual((manager.getSetting("a"), manager.getSetting("b")), (None, {"nested": [1]}))
        self.assertEqual(manager.getSetting("missing", 5), 5)

    def test_a_rejected_change_leaves_the_saved_settings_in_use(self):
        (self.root / "module.json").write_text('{"kept": 1}', encoding="utf-8")
        manager = self.manager()
        too_big = "x" * (safe_settings.MAX_SETTINGS_BYTES + 1)
        manager.setSetting("big", too_big)
        with self.assertRaises(ValueError):
            manager.commit()
        self.assertEqual(manager.settings, {"kept": 1})
        manager.setSetting("pending", 2)
        with self.assertRaises(ValueError):
            manager.replace({"big": too_big})
        self.assertEqual(manager.settings, {"kept": 1, "pending": 2})


class LegacyBackupTests(unittest.TestCase):
    """2.4.0 kept <file>.bak; dk_fs keeps <file>.last-good."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_a_damaged_file_right_after_the_update_is_restored_from_the_old_backup(self):
        (self.root / "module.json").write_text("{broken", encoding="utf-8")
        (self.root / "module.json.bak").write_text('{"enabled": true}', encoding="utf-8")
        manager = safe_settings.AtomicSettingsManager("module", str(self.root))
        self.assertEqual((manager.settings, manager.recovery_error), ({"enabled": True}, ""))
        self.assertFalse((self.root / "module.json.bak").exists())
        self.assertEqual(json.loads((self.root / "module.json.last-good").read_text(encoding="utf-8")),
                         {"enabled": True})

    def test_a_damaged_old_backup_never_replaces_a_good_copy(self):
        safe_settings.AtomicSettingsManager("module", str(self.root)).replace({"enabled": True})
        good = self.root / "module.json.last-good"
        os.utime(good, (1, 1))
        (self.root / "module.json.bak").write_text("{broken", encoding="utf-8")
        (self.root / "module.json").write_text("{broken", encoding="utf-8")
        manager = safe_settings.AtomicSettingsManager("module", str(self.root))
        self.assertEqual(manager.settings, {"enabled": True})


@unittest.skipUnless(os.name == "posix", "POSIX dir_fd and symlink semantics")
class OpenDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fds = len(os.listdir("/proc/self/fd"))
        self.addCleanup(lambda: self.assertEqual(len(os.listdir("/proc/self/fd")), self.fds, "descriptor leaked"))

    def walk(self, *parts, **options):
        return safe_settings.open_directory(parts, os.open(self.root, os.O_RDONLY | os.O_DIRECTORY), **options)

    def test_symlink_file_missing_and_vetoed_components_are_refused(self):
        outside = self.root / "outside"; outside.mkdir()
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        (self.root / "file").write_text("")
        made = lambda part: (0o700, os.getuid(), os.getgid())
        for name, error in (("link", OSError), ("file", NotADirectoryError)):
            with self.subTest(name), self.assertRaises(error):
                self.walk(name, "child", create=made)
        with self.assertRaises(FileNotFoundError):
            self.walk("missing")
        def foreign(fd):
            raise ValueError("foreign owner")
        with self.assertRaisesRegex(ValueError, "foreign owner"):
            self.walk("vetoed", "child", create=made, check=foreign)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["file", "link", "outside", "vetoed"])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list((self.root / "vetoed").iterdir()), [])

    def test_without_a_starting_directory_the_walk_starts_at_root(self):
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        (self.root / "tmp").mkdir()  # the same name below the working directory must not be taken
        fd = safe_settings.open_directory(["tmp"])
        try:
            self.assertEqual(os.fstat(fd).st_ino, os.stat("/tmp").st_ino)
        finally:
            os.close(fd)

    def test_missing_components_are_created_with_their_mode_and_each_is_vetted(self):
        seen, mask = [], os.umask(0)
        os.umask(mask)
        fd = self.walk("a", "b", create=lambda part: (0o700 if part == "b" else 0o755, os.getuid(), os.getgid()),
                       check=lambda fd: seen.append(os.fstat(fd).st_ino))
        try:
            self.assertEqual(os.fstat(fd).st_ino, (self.root / "a/b").stat().st_ino)
        finally:
            os.close(fd)
        self.assertEqual([stat.S_IMODE((self.root / p).stat().st_mode) for p in ("a", "a/b")], [0o755 & ~mask, 0o700])
        self.assertEqual(seen, [(self.root / p).stat().st_ino for p in ("a", "a/b")])

if __name__ == "__main__":
    unittest.main()

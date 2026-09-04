# SPDX-License-Identifier: BSD-3-Clause

import json
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

    def test_corrupt_file_without_backup_starts_clean_and_preserves_evidence(self):
        path = self.root / "module.json"
        path.write_text("[]", encoding="utf-8")
        manager = self.manager()
        self.assertEqual(manager.settings, {})
        self.assertFalse(path.exists())
        self.assertEqual(len(list(self.root.glob("module.json.corrupt-*"))), 1)

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

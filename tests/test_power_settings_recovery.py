# SPDX-License-Identifier: BSD-3-Clause
"""Offline regressions for lost TDP and vibration settings."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_integration  # Isolated Decky stubs.
import tdp_backend as tdp
import vibration_backend as vibe
from safe_settings import AtomicSettingsManager, CorruptSettings


class PowerSettingsRecoveryTests(unittest.TestCase):
    def test_corrupt_tdp_copies_block_migration_reads_and_writes(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "tdp.json"
            path.write_text("{broken", encoding="utf-8")
            Path(str(path) + ".bak").write_text("[]", encoding="utf-8")
            manager = AtomicSettingsManager("tdp", raw)
            with patch.object(tdp, "settings", manager), \
                 patch.object(tdp, "_apply_limits_with_saved_cpu_power") as apply, \
                 patch.object(tdp, "_restore_defaults_locked") as restore:
                for operation in (tdp._migrate, tdp._load_settings, tdp._load_profiles,
                                  lambda: tdp._write_keys({"settings": {"enabled": False}}),
                                  lambda: asyncio.run(tdp.Plugin().restore_defaults()),
                                  lambda: asyncio.run(tdp.Plugin().set_plugin_enabled(True))):
                    with self.subTest(operation=operation):
                        with self.assertRaisesRegex(CorruptSettings, "TDP settings recovery failed"):
                            operation()
                apply.assert_not_called()
                restore.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")
            self.assertEqual(Path(str(path) + ".bak").read_text(encoding="utf-8"), "[]")

    def test_corrupt_vibration_copies_block_migration_profiles_and_hardware(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "vibe.json"
            path.write_text("{broken", encoding="utf-8")
            Path(str(path) + ".bak").write_text("[]", encoding="utf-8")
            manager = AtomicSettingsManager("vibe", raw)
            with patch.object(vibe, "settings", manager), \
                 patch.object(vibe, "_write_attr") as write:
                for operation in (vibe._migrate, vibe._load_profiles,
                                  lambda: vibe._save_profiles({}),
                                  lambda: vibe._apply_settings(dict(vibe.DEFAULT_PROFILE), "mock-device", True),
                                  lambda: asyncio.run(vibe.Plugin().set_intensity(2))):
                    with self.subTest(operation=operation):
                        with self.assertRaisesRegex(CorruptSettings, "Vibration settings recovery failed"):
                            operation()
                write.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")
            self.assertEqual(Path(str(path) + ".bak").read_text(encoding="utf-8"), "[]")

    def test_first_install_still_migrates_and_uses_defaults(self):
        for backend in (tdp, vibe):
            with self.subTest(backend=backend.__name__), tempfile.TemporaryDirectory() as raw:
                manager = AtomicSettingsManager("settings", raw)
                with patch.object(backend, "settings", manager):
                    backend._migrate()
                    if backend is tdp:
                        self.assertTrue(backend._load_settings()["enabled"])
                    else:
                        self.assertIn(vibe.DEFAULT_APP, backend._load_profiles())
                self.assertEqual(manager.recovery_error, "")
                self.assertTrue(Path(manager.path).exists())


if __name__ == "__main__":
    unittest.main()

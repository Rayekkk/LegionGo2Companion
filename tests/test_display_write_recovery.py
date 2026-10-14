# SPDX-License-Identifier: BSD-3-Clause
"""Interrupted in-place EDID writes retain recovery proof across store reloads."""
import builtins
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

import test_display_native_edid as fixtures
import display_backend as display


class DisplayWriteRecoveryTests(unittest.TestCase):
    setUp = fixtures.NativeDisplayEdidTests.setUp
    configure = fixtures.NativeDisplayEdidTests.configure
    assert_patched = fixtures.NativeDisplayEdidTests.assert_patched

    def interrupt_patch(self, rollback_fails=True, restore=False):
        real_open = builtins.open
        target = self.edid
        class Interrupted:
            def __init__(self, handle): self.handle, self.writes = handle, 0
            def __enter__(self): return self
            def __exit__(self, *_args): self.handle.close()
            def __getattr__(self, name): return getattr(self.handle, name)
            def write(self, data):
                self.writes += 1
                if self.writes == 1:
                    self.handle.write(data[:128])
                    raise OSError('Patch storage exhausted after a prefix')
                if rollback_fails: raise OSError('Rollback storage still exhausted')
                return self.handle.write(data)
        def opened(path, mode='r', *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            return Interrupted(handle) if Path(path) == target and mode == 'r+b' else handle
        with patch('builtins.open', side_effect=opened):
            if restore:
                return display.Plugin._edid_restore()
            display.Plugin._edid_pass()

    def test_known_partial_write_recovers_exact_original_after_reload(self):
        self.interrupt_patch()
        partial = self.edid.read_bytes()
        self.assertNotEqual(partial, self.original)
        self.assertNotEqual(partial, self.expected)
        self.store.read()
        pending = self.store.getSetting('edid_write_pending')
        self.assertEqual(pending['partial_sha256'], hashlib.sha256(partial).hexdigest())
        display.Plugin._edid_original = self.backup.read_bytes()
        self.assertTrue(display.Plugin._edid_restore())
        self.assertEqual(self.edid.read_bytes(), self.original)
        self.assertFalse(self.backup.exists())
        self.assertIsNone(self.store.getSetting('edid_write_pending'))

    def test_unrecorded_partial_digest_defers_without_losing_original_after_reload(self):
        store = display.Plugin._store_settings
        def commit(values, **kwargs):
            pending = values.get('edid_write_pending')
            if isinstance(pending, dict) and pending.get('partial_sha256'):
                raise OSError('Settings storage exhausted too')
            return store(values, **kwargs)
        with patch.object(display.Plugin, '_store_settings', side_effect=commit):
            self.interrupt_patch()
        partial = self.edid.read_bytes()
        self.store.read()
        self.assertIsNone(self.store.getSetting('edid_write_pending')['partial_sha256'])
        display.Plugin._edid_original = self.backup.read_bytes()
        self.assertFalse(display.Plugin._edid_restore())
        display.Plugin._edid_pass()
        self.assertEqual(self.edid.read_bytes(), partial)
        self.assertEqual(self.backup.read_bytes(), self.original)
        self.assertIsNotNone(self.store.getSetting('edid_write_pending'))

    def test_failed_recovery_with_verified_partial_rollback_retains_proof_across_reload(self):
        self.interrupt_patch()
        partial = self.edid.read_bytes()
        pending = self.store.getSetting('edid_write_pending')
        self.assertFalse(self.interrupt_patch(rollback_fails=False, restore=True))
        self.assertEqual(self.edid.read_bytes(), partial)
        self.store.read()
        self.assertEqual(self.store.getSetting('edid_write_pending'), pending)
        display.Plugin._edid_original = self.backup.read_bytes()
        self.assertTrue(display.Plugin._edid_restore())
        self.assertEqual(self.edid.read_bytes(), self.original)
        self.assertIsNone(self.store.getSetting('edid_write_pending'))

    def test_failed_partial_rollback_proof_commit_defers_with_original_backup(self):
        self.interrupt_patch()
        partial = self.edid.read_bytes()
        store = display.Plugin._store_settings
        def commit(values, **kwargs):
            pending = values.get('edid_write_pending')
            if isinstance(pending, dict) and pending.get('partial_sha256'):
                raise OSError('Partial rollback proof could not be republished')
            return store(values, **kwargs)
        with patch.object(display.Plugin, '_store_settings', side_effect=commit):
            self.assertFalse(self.interrupt_patch(rollback_fails=False, restore=True))
        self.store.read()
        self.assertIsNone(self.store.getSetting('edid_write_pending')['partial_sha256'])
        self.assertFalse(display.Plugin._edid_restore())
        self.assertEqual(self.edid.read_bytes(), partial)
        self.assertEqual(self.backup.read_bytes(), self.original)

    def test_foreign_replacement_after_partial_recovery_rollback_is_preserved(self):
        self.interrupt_patch()
        partial = self.edid.read_bytes()
        self.assertFalse(self.interrupt_patch(rollback_fails=False, restore=True))
        replacement = self.root / 'replacement'
        replacement.write_bytes(partial)
        replacement.replace(self.edid)
        self.store.read()
        self.assertTrue(display.Plugin._edid_restore())
        self.assertEqual(self.edid.read_bytes(), partial)
        self.assertIsNone(self.store.getSetting('edid_write_pending'))

    def test_atomic_connector_replacement_is_preserved_even_with_identical_partial_bytes(self):
        self.interrupt_patch()
        partial = self.edid.read_bytes()
        replacement = self.root / 'replacement'
        replacement.write_bytes(partial)
        replacement.replace(self.edid)
        self.assertTrue(display.Plugin._edid_restore())
        self.assertEqual(self.edid.read_bytes(), partial)
        self.assertIsNone(self.store.getSetting('edid_write_pending'))

    def test_atomic_replacement_while_partial_recovery_reopens_is_not_written(self):
        self.interrupt_patch()
        partial = self.edid.read_bytes()
        replacement = self.root / 'replacement'
        replacement.write_bytes(partial)
        real_open = builtins.open
        def opened(path, mode='r', *args, **kwargs):
            if Path(path) == self.edid and mode == 'r+b':
                replacement.replace(self.edid)
            return real_open(path, mode, *args, **kwargs)
        with patch('builtins.open', side_effect=opened):
            self.assertTrue(display.Plugin._edid_restore())
        self.assertEqual(self.edid.read_bytes(), partial)
        self.assertIsNone(self.store.getSetting('edid_write_pending'))

    def test_verified_rollback_clears_intent_and_preserves_original(self):
        self.interrupt_patch(rollback_fails=False)
        self.assertEqual(self.edid.read_bytes(), self.original)
        self.assertIsNone(self.store.getSetting('edid_write_pending'))
        self.assertTrue(display.Plugin._edid_restore())
        self.assertEqual(self.edid.read_bytes(), self.original)

    def test_failed_intent_commit_never_starts_an_edid_write(self):
        with patch.object(self.store, 'replace', side_effect=PermissionError('Settings are read only')):
            display.Plugin._edid_pass()
        self.assertEqual(self.edid.read_bytes(), self.original)
        self.assertEqual(self.backup.read_bytes(), self.original)
        self.assertIsNone(self.store.getSetting('edid_write_pending'))
        display.Plugin._edid_pass()
        self.assert_patched()

    def test_failed_clear_keeps_backup_until_a_later_confirmed_cleanup(self):
        store = display.Plugin._store_settings
        def commit(values, **kwargs):
            if values.get('edid_write_pending', 'other') is None:
                raise OSError('Metadata clear failed')
            return store(values, **kwargs)
        with patch.object(display.Plugin, '_store_settings', side_effect=commit):
            display.Plugin._edid_pass()
            self.assertEqual(self.edid.read_bytes(), self.expected)
            self.assertIsNotNone(self.store.getSetting('edid_write_pending'))
            self.assertFalse(display.Plugin._edid_restore())
            self.assertEqual(self.edid.read_bytes(), self.original)
            self.assertEqual(self.backup.read_bytes(), self.original)
        self.assertTrue(display.Plugin._edid_restore())
        self.assertFalse(self.backup.exists())

    def test_healthy_patched_poll_does_not_recommit_recovery_intent(self):
        display.Plugin._edid_pass()
        self.assert_patched()
        with patch.object(self.store, 'replace', side_effect=AssertionError('No settings write on healthy poll')):
            for _ in range(4): display.Plugin._edid_pass()
        self.assert_patched()


if __name__ == '__main__':
    unittest.main()

# SPDX-License-Identifier: BSD-3-Clause
"""Interrupted in-place EDID writes retain recovery proof across store reloads."""
import hashlib
import os
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
        real_open = display._open_regular
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
        def opened(path, writable=False):
            handle = real_open(path, writable)
            return Interrupted(handle) if Path(path) == target and writable else handle
        with patch.object(display, '_open_regular', side_effect=opened):
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

    def test_recovery_of_a_removed_edid_path_lets_the_new_path_be_fixed(self):
        self.interrupt_patch()
        self.assertIsNotNone(self.store.getSetting('edid_write_pending'))
        moved = self.root / 'published-after-session-change.bin'
        moved.write_bytes(self.original)
        self.edid.unlink(); self.published = moved
        display.Plugin._edid_pass()
        self.assertIsNone(self.store.getSetting('edid_write_pending'))
        display.Plugin._edid_pass()
        self.assertEqual(moved.read_bytes(), self.expected)
        self.assertEqual(self.backup.read_bytes(), self.original)

    def test_a_gone_targets_baseline_never_reaches_a_new_file(self):
        # The new file equals the old one trimmed, but equal bytes do not make it ours.
        self.interrupt_patch()
        moved = self.root / 'published-after-session-change.bin'
        moved.write_bytes(self.expected)
        self.edid.unlink(); self.published = moved
        display.Plugin._edid_pass(); display.Plugin._edid_pass()
        self.assertIsNone(self.store.getSetting('edid_write_pending'))
        self.assertFalse(self.backup.exists())
        self.assertIsNone(display.Plugin._edid_original)
        self.assertFalse(self.state['edid_patched'])
        self.assertTrue(display.Plugin._edid_restore())
        self.assertEqual(moved.read_bytes(), self.expected, 'turning the fix off never writes the old original')

    def replace_with_trimmed(self, same_path, restart):
        """Our patched file is replaced by a NEW inode holding equal trimmed bytes."""
        display.Plugin._edid_pass()
        self.assertEqual(self.edid.read_bytes(), self.expected)
        target = self.edid if same_path else self.root / 'published-elsewhere.bin'
        staged = self.root / 'staged.bin'
        staged.write_bytes(self.expected)
        os.replace(staged, target)  # atomic rename: a different inode
        if not same_path:
            self.edid.unlink(); self.published = target
        if restart:  # a plugin start reloads the backup and forgets the path
            display.Plugin._edid_original = self.backup.read_bytes()
            display.Plugin._edid_path = ''
        return target

    def test_trimmed_bytes_in_a_file_we_never_wrote_are_never_restored_over(self):
        for same_path in (False, True):
            for restart in (False, True):
                with self.subTest(same_path=same_path, restart=restart):
                    self.configure({})
                    target = self.replace_with_trimmed(same_path, restart)
                    display.Plugin._edid_pass(); display.Plugin._edid_pass()
                    self.assertFalse(self.state['edid_patched'])
                    self.assertFalse(self.backup.exists())
                    self.assertTrue(display.Plugin._edid_restore())
                    self.assertEqual(target.read_bytes(), self.expected, 'turning the fix off never writes the old original')

    def test_an_interrupted_first_patch_never_hands_its_backup_to_a_replaced_file(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                self.configure({})
                self.interrupt_patch()  # first patch: no identity existed before it
                self.assertIsNotNone(self.store.getSetting('edid_write_pending'))
                staged = self.root / 'staged.bin'
                staged.write_bytes(self.expected)
                os.replace(staged, self.edid)  # same path, new inode, equal trimmed bytes
                if restart:
                    display.Plugin._edid_original = self.backup.read_bytes()
                    display.Plugin._edid_path = ''
                display.Plugin._edid_pass(); display.Plugin._edid_pass()
                self.assertIsNone(self.store.getSetting('edid_write_pending'))
                self.assertFalse(self.state['edid_patched'])
                self.assertTrue(display.Plugin._edid_restore())
                self.assertEqual(self.edid.read_bytes(), self.expected, 'turning the fix off never writes the old original')

    def test_restore_without_a_pass_also_refuses_a_replaced_file(self):
        target = self.replace_with_trimmed(True, False)
        self.assertTrue(display.Plugin._edid_restore())
        self.assertEqual(target.read_bytes(), self.expected)
        self.assertFalse(self.backup.exists())

    def test_a_2_1_patch_without_a_recorded_identity_still_restores(self):
        display.Plugin._edid_pass()
        display.Plugin._store_settings({'edid_patched_identity': None})
        self.assertTrue(display.Plugin._edid_restore())
        self.assertEqual(self.edid.read_bytes(), self.original)

    def test_recovery_stays_pending_while_the_old_edid_path_still_exists(self):
        self.interrupt_patch()
        pending = self.store.getSetting('edid_write_pending')
        partial = self.edid.read_bytes()
        moved = self.root / 'published-after-session-change.bin'
        moved.write_bytes(self.original); self.published = moved
        display.Plugin._edid_pass(); display.Plugin._edid_pass()
        self.assertEqual(self.store.getSetting('edid_write_pending'), pending)
        self.assertEqual((self.edid.read_bytes(), moved.read_bytes()), (partial, self.original))

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
        real_open = display._open_regular
        def opened(path, writable=False):
            if Path(path) == self.edid and writable:
                replacement.replace(self.edid)
            return real_open(path, writable)
        with patch.object(display, '_open_regular', side_effect=opened):
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

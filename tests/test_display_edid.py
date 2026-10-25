# SPDX-License-Identifier: BSD-3-Clause
"""EDID parsing and the in-place EDID fix on real files: patch, restore, interrupted writes, unsafe paths."""
import asyncio
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import support  # Decky/Linux doubles before importing the backend.
import display_backend as display
from safe_settings import AtomicSettingsManager


def edid_fixture(product=0x4301, luminance_code=100):
    """Deterministic EDID base + CTA HDR + DisplayID 2.0, not a console dump."""
    def checked(block):
        block[127] = -sum(block[:127]) & 0xFF
        return bytes(block)

    base = bytearray(128)
    base[:8] = b'\x00\xff\xff\xff\xff\xff\xff\x00'
    base[8:12] = b'\x4c\x83' + product.to_bytes(2, 'little')  # SDC
    base[18:20] = b'\x01\x04'
    base[126] = 2
    cta = bytearray(128)
    cta[:4] = b'\x02\x03\x0b\x00'
    cta[4:11] = bytes((0xE6, 6, 0x0F, 1, luminance_code, luminance_code, 0))
    displayid = bytearray(128)
    displayid[:6] = b'\x70\x20\x00\x00\x00\xe0'
    original = checked(base) + checked(cta) + checked(displayid)
    # Construct the expected base independently of the production strip helper.
    base[126] = 1
    expected = checked(base) + checked(cta)
    return original, expected


class EdidFixTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.edid = self.root / 'published-internal-edid.bin'
        self.original, self.expected = edid_fixture()
        self.edid.write_bytes(self.original)
        self.backup = self.root / 'edid-original.bin'
        self.store = AtomicSettingsManager('display_settings', str(self.root / 'settings'))
        self.state = dict(display.Plugin._state)
        self.published = self.edid
        for name, value in {'settings': self.store, 'EDID_BACKUP': str(self.backup)}.items():
            self.stack.enter_context(patch.object(display, name, value))
        for name, value in {'_state': self.state, '_edid_original': None, '_edid_path': '',
                            '_session_retry_after': 0.0}.items():
            self.stack.enter_context(patch.object(display.Plugin, name, value))
        # Only X properties/session identity are mocked. Path validation, parsing,
        # conditional writes, original backups and settings commits stay real.
        self.read_props = self.stack.enter_context(patch.object(
            display, '_read_props', side_effect=lambda names: {
                display.ATOM_EDID_PATH: f'"{self.published}"'}))
        self.stack.enter_context(patch.object(
            display, '_display_owner', return_value=self.edid.stat().st_uid))
        self.plugin = display.Plugin()
        self.configure()

    def configure(self, enabled=True):
        self.state.update(edid_fix=enabled, edid_patched=False, edid_game_nits=0.0, edid_reason='')
        self.saved = {'edid_fix': enabled, 'edid_write_pending': None, 'edid_patched_identity': None}
        self.store.replace(self.saved)
        self.edid.write_bytes(self.original)
        self.backup.unlink(missing_ok=True)
        self.published = self.edid
        display.Plugin._edid_original = None
        display.Plugin._edid_path = ''
        self.read_props.reset_mock()

    def persisted(self):
        return json.loads(Path(self.store.path).read_text(encoding='utf-8'))

    def owned(self, path=None):
        info = (path or self.edid).stat()  # A patch records the inode it wrote in place.
        return {'device': info.st_dev, 'inode': info.st_ino}

    def assert_patched(self, path=None, original=None, expected=None, luminance_code=100):
        path = path or self.edid
        original = original or self.original
        expected = expected or self.expected
        self.assertEqual(path.read_bytes(), expected)
        self.assertEqual(self.backup.read_bytes(), original)
        self.assertEqual(display.Plugin._edid_original, original)
        self.assertEqual(display.Plugin._edid_path, str(path))
        self.assertTrue(self.state['edid_patched'])
        self.assertEqual(self.state['edid_game_nits'], round(50 * 2 ** (luminance_code / 32), 1))
        for offset in range(0, len(expected), 128):
            self.assertEqual(sum(expected[offset:offset + 128]) & 0xFF, 0)
        self.assertEqual(expected[126], 1)
        self.assertEqual(expected[128:], original[128:256], 'CTA HDR data is unchanged.')

    def test_pass_patches_the_published_edid_and_records_what_it_owns(self):
        display.Plugin._edid_pass()
        self.assert_patched()
        self.assertEqual(self.persisted(), {**self.saved, 'edid_patched_identity': self.owned()})
        self.read_props.assert_called_once_with([display.ATOM_EDID_PATH])
        display.Plugin._edid_pass()
        self.assert_patched()

    def test_toggle_persists_and_restores_the_original(self):
        self.configure(enabled=False)
        result = asyncio.run(self.plugin.set_edid_fix(True))
        self.assertTrue(result['edid_fix'])
        self.assert_patched()
        self.assertEqual(self.persisted(), {**self.saved, 'edid_fix': True, 'edid_patched_identity': self.owned()})
        result = asyncio.run(self.plugin.set_edid_fix(False))
        self.assertFalse(result['edid_fix'])
        self.assertFalse(result['edid_patched'])
        self.assertEqual(self.persisted(), self.saved)
        self.assertEqual(self.edid.read_bytes(), self.original)
        self.assertIsNone(display.Plugin._edid_original)
        self.assertFalse(self.backup.exists())
        
    def test_disabled_pass_restores_only_the_owned_edid(self):
        display.Plugin._edid_pass()
        self.assert_patched()
        self.state['edid_fix'] = False
        self.store.replace({**self.saved, 'edid_fix': False})
        display.Plugin._edid_pass()
        self.assertEqual(self.edid.read_bytes(), self.original)
        self.assertFalse(self.state['edid_patched'])
        self.assertIsNone(display.Plugin._edid_original)
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.persisted()['edid_fix'])

    def test_disabling_preserves_an_edid_replaced_by_the_connector(self):
        display.Plugin._edid_pass()
        replacement, _ = edid_fixture(product=0x9999, luminance_code=128)
        self.edid.write_bytes(replacement)
        result = asyncio.run(self.plugin.set_edid_fix(False))
        self.assertEqual(self.edid.read_bytes(), replacement)
        self.assertFalse(result['edid_patched'])
        self.assertIn('changed', result['edid_reason'])
        self.assertFalse(self.persisted()['edid_fix'])

    def test_removed_published_copy_does_not_block_release_or_recreate_the_file(self):
        display.Plugin._edid_pass()
        self.assert_patched()
        self.edid.unlink()
        self.assertTrue(display.Plugin._edid_restore())
        self.assertFalse(self.edid.exists())
        self.assertFalse(self.state['edid_patched'])
        self.assertIn('disappeared', self.state['edid_reason'])
        self.assertEqual(self.backup.read_bytes(), self.original)
        self.assertEqual(display.Plugin._edid_original, self.original)

    def test_access_error_and_nonregular_target_still_defer_edid_restoration(self):
        display.Plugin._edid_pass()
        self.assert_patched()
        with patch.object(display.os, 'lstat', side_effect=PermissionError('not readable')):
            self.assertFalse(display.Plugin._edid_restore())
        self.assertEqual(self.edid.read_bytes(), self.expected)
        self.edid.unlink()
        self.edid.mkdir()
        self.assertFalse(display.Plugin._edid_restore())
        self.assertTrue(self.edid.is_dir())
        self.assertEqual(self.backup.read_bytes(), self.original)

    def test_open_and_read_errors_retain_patched_bytes_and_recovery_until_retry(self):
        real_open = display._open_regular
        for failure in (PermissionError('read access denied'), OSError('read I/O failed')):
            for phase in ('open', 'read'):
                with self.subTest(failure=type(failure).__name__, phase=phase):
                    self.configure()
                    display.Plugin._edid_pass()
                    self.assert_patched()
                    class FailedRead:
                        def __init__(self, handle): self.handle = handle
                        def __enter__(self): return self
                        def __exit__(self, *_args): self.handle.close()
                        def fileno(self): return self.handle.fileno()
                        def read(self, *_args): raise failure
                    def opened(path, writable=False):
                        if Path(path) == self.edid and not writable:
                            if phase == 'open': raise failure
                            return FailedRead(real_open(path, writable))
                        return real_open(path, writable)
                    with patch.object(display, '_open_regular', side_effect=opened):
                        self.assertFalse(display.Plugin._edid_restore())
                    self.assertEqual(self.edid.read_bytes(), self.expected)
                    self.assertEqual(self.backup.read_bytes(), self.original)
                    self.assertEqual(display.Plugin._edid_original, self.original)
                    self.assertTrue(self.state['edid_patched'])
                    self.assertIn('retry', self.state['edid_reason'])
                    self.assertTrue(display.Plugin._edid_restore())
                    self.assertEqual(self.edid.read_bytes(), self.original)
                    self.assertFalse(self.backup.exists())

    def test_path_removed_between_validation_and_read_is_released_without_recreation(self):
        display.Plugin._edid_pass()
        self.assert_patched()
        real_open = display._open_regular
        def opened(path, writable=False):
            if Path(path) == self.edid and not writable:
                self.edid.unlink()
                raise FileNotFoundError('compositor removed old copy')
            return real_open(path, writable)
        with patch.object(display, '_open_regular', side_effect=opened):
            self.assertTrue(display.Plugin._edid_restore())
        self.assertFalse(self.edid.exists())
        self.assertFalse(self.state['edid_patched'])
        self.assertEqual(self.backup.read_bytes(), self.original)

    def test_dock_and_undock_follow_new_published_paths(self):
        display.Plugin._edid_pass()
        self.assert_patched()
        external = self.root / 'published-external-edid.bin'
        external_original, external_expected = edid_fixture(product=0x9999, luminance_code=128)
        external.write_bytes(external_original)
        self.published = external
        display.Plugin._edid_pass()
        self.assert_patched(external, external_original, external_expected, 128)
        self.assertEqual(self.edid.read_bytes(), self.expected)
        undocked = self.root / 'republished-internal-edid.bin'
        undocked.write_bytes(self.original)
        self.published = undocked
        display.Plugin._edid_pass()
        self.assert_patched(undocked)
        asyncio.run(self.plugin.set_edid_fix(False))
        self.assertEqual(undocked.read_bytes(), self.original)
        self.assertEqual(external.read_bytes(), external_expected,
                         'Restoration must not write to the previous connector path.')
        self.assertFalse(self.backup.exists())


class PublishedEdidOpenTests(unittest.TestCase):
    """Root opens of the X-published path never follow links, block or over-read."""
    setUp = EdidFixTests.setUp
    configure = EdidFixTests.configure

    def run_bounded(self, fn, *args):
        result = []
        worker = threading.Thread(target=lambda: result.append(fn(*args)), daemon=True)
        worker.start()
        worker.join(3)
        self.assertFalse(worker.is_alive(), 'A FIFO must not block the root display worker.')
        return result[0]

    def test_oversize_published_file_is_neither_read_nor_backed_up(self):
        huge = self.original + bytes(display.MAX_FILE_BYTES)
        self.edid.write_bytes(huge)
        self.assertIsNone(display._read_bytes(str(self.edid)))
        display.Plugin._edid_pass()
        self.assertEqual(self.edid.read_bytes(), huge)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.state['edid_reason'], 'EDID file unreadable')

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink and FIFO semantics')
    def test_symlink_is_refused_for_reads_and_in_place_writes(self):
        victim = self.root / 'victim'
        victim.write_bytes(self.original)
        link = self.root / 'link'
        link.symlink_to(victim)
        self.assertIsNone(display._read_bytes(str(link)))
        self.assertEqual(display._rewrite_file_if_unchanged(str(link), self.original, b'x'), 'failed')
        self.assertEqual(victim.read_bytes(), self.original)

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink and FIFO semantics')
    def test_fifo_is_refused_without_waiting_for_a_writer(self):
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        self.assertIsNone(self.run_bounded(display._read_bytes, str(fifo)))
        self.assertEqual(self.run_bounded(display._rewrite_file_if_unchanged, str(fifo), b'', b'x'), 'failed')

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink and FIFO semantics')
    def test_published_file_swapped_after_the_path_check_is_refused(self):
        victim = self.root / 'victim'
        victim.write_bytes(self.original)
        checked = display._published_edid_is_safe
        for swap in ('fifo', 'symlink'):
            with self.subTest(swap=swap):
                self.configure()
                def safe_then_swap(path, opened_stat=None):
                    verdict = checked(path, opened_stat)
                    if opened_stat is None and not os.path.islink(path) and os.path.isfile(path):
                        self.edid.unlink()
                        os.mkfifo(self.edid) if swap == 'fifo' else self.edid.symlink_to(victim)
                    return verdict
                with patch.object(display, '_published_edid_is_safe', side_effect=safe_then_swap):
                    self.run_bounded(display.Plugin._edid_pass)
                self.assertEqual(self.state['edid_reason'], 'EDID file unreadable')
                self.assertEqual(victim.read_bytes(), self.original)
                self.assertFalse(self.backup.exists())
                self.edid.unlink()


class DisplayWriteRecoveryTests(unittest.TestCase):
    setUp = EdidFixTests.setUp
    configure = EdidFixTests.configure
    assert_patched = EdidFixTests.assert_patched

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
                    self.configure()
                    target = self.replace_with_trimmed(same_path, restart)
                    display.Plugin._edid_pass(); display.Plugin._edid_pass()
                    self.assertFalse(self.state['edid_patched'])
                    self.assertFalse(self.backup.exists())
                    self.assertTrue(display.Plugin._edid_restore())
                    self.assertEqual(target.read_bytes(), self.expected, 'turning the fix off never writes the old original')

    def test_an_interrupted_first_patch_never_hands_its_backup_to_a_replaced_file(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                self.configure()
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


def checksum_ok(edid):
    return all(sum(edid[i:i + 128]) & 0xFF == 0 for i in range(0, len(edid), 128))


class EdidParsingTests(unittest.TestCase):
    def test_every_displayid_block_is_dropped_with_count_and_checksum_fixed(self):
        original, expected = edid_fixture()
        displayid = original[256:]
        doubled = bytearray(original[:128]) + original[128:256] + displayid + displayid
        doubled[126] = 3
        doubled[127] = -sum(doubled[:127]) & 0xFF
        for edid in (original, bytes(doubled)):
            with self.subTest(blocks=len(edid) // 128):
                stripped = display._strip_displayid(edid)
                self.assertEqual(stripped, expected)
                self.assertTrue(checksum_ok(stripped))
                self.assertFalse(display._has_displayid(stripped))

    def test_nothing_to_strip_or_malformed_length_returns_none(self):
        original, expected = edid_fixture()
        self.assertIsNone(display._strip_displayid(expected))
        self.assertIsNone(display._strip_displayid(original[:128]))
        self.assertIsNone(display._strip_displayid(original + b'\0'))

    def test_cta_hdr_luminance_and_undefined_code(self):
        for code in (100, 128):
            with self.subTest(code=code):
                original, _ = edid_fixture(luminance_code=code)
                self.assertAlmostEqual(display._cta_max_luminance(original), 50 * 2 ** (code / 32))
        undefined, _ = edid_fixture(luminance_code=0)
        self.assertIsNone(display._cta_max_luminance(undefined))
        self.assertIsNone(display._cta_max_luminance(undefined[:128]))


class MalformedEdidTests(unittest.TestCase):
    def test_truncated_cta_data_block_is_no_data_not_an_exception(self):
        cta = bytearray(128)
        cta[0], cta[1], cta[2] = 0x02, 0x03, 128
        j = 4
        for size in (31, 31, 31, 26):          # vendor blocks walking to byte 127
            cta[j] = (3 << 5) | size
            j += 1 + size
        cta[127] = (7 << 5) | 4                 # extended-tag header in the last byte
        base = bytearray(128)
        base[0:8] = bytes([0, 255, 255, 255, 255, 255, 255, 0])
        base[0x7E] = 1
        self.assertIsNone(display._cta_max_luminance(bytes(base) + bytes(cta)))


if __name__ == '__main__':
    unittest.main()

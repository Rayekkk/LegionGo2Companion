# SPDX-License-Identifier: BSD-3-Clause
"""EDID remains independent of legacy/native display setup, using real files."""
import asyncio
from contextlib import ExitStack
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


DISPLAY_STATES = (
    ('no_mode_or_script', {}),
    ('mode_without_setup', {'panel_mode': 'hybrid'}),
    ('legacy_restart', {'panel_mode': 'pq', 'restart_pending': True}),
    ('native_cleanup', {'native_support_detected': True, 'native_cleanup_pending': True}),
    ('native_cleanup_error', {'native_support_detected': True, 'native_cleanup_pending': True,
                              'native_cleanup_error': 'Could not remove the old script.'}),
    ('native_restart', {'native_support_detected': True, 'native_restart_pending': True}),
    ('native_complete', {'native_support_detected': True}),
    ('pending_without_detection', {'native_cleanup_pending': True}),
    ('native_after_interrupted_reset', {'native_support_detected': True,
                                       'native_cleanup_pending': True,
                                       'native_cleanup_error': 'Cleanup is retrying.',
                                       'reset_in_progress': True}),
)


class NativeDisplayEdidTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.edid = self.root / 'published-internal-edid.bin'
        self.original, self.expected = edid_fixture()
        self.edid.write_bytes(self.original)
        self.backup = self.root / 'edid-original.bin'
        self.script = self.root / 'lenovo.legiongo2.oled.lua'
        self.store = AtomicSettingsManager('display_settings', str(self.root / 'settings'))
        self.state = dict(display.Plugin._state)
        self.initial = dict(self.state, panel_mode=None, active_mode=None, setup_done=False,
                            enabled=False, active=False, restart_pending=False,
                            reset_in_progress=False, reset_restart_pending=False,
                            settings_error='', native_support_detected=False,
                            native_support_reason='', native_cleanup_pending=False,
                            native_cleanup_error='', native_restart_pending=False,
                            edid_fix=True, edid_patched=False, edid_game_nits=0.0,
                            edid_reason='')
        self.published = self.edid
        for name, value in {'settings': self.store, 'INSTALLED_SCRIPT': str(self.script),
                            'EDID_BACKUP': str(self.backup)}.items():
            self.stack.enter_context(patch.object(display, name, value))
        for name, value in {'_state': self.state, '_edid_original': None, '_edid_path': '',
                            '_session_retry_after': 0.0, '_session_note': ''}.items():
            self.stack.enter_context(patch.object(display.Plugin, name, value))
        # Only X properties/session identity are mocked. Path validation, parsing,
        # conditional writes, original backups and settings commits stay real.
        self.read_props = self.stack.enter_context(patch.object(
            display, '_read_props', side_effect=lambda names: {
                display.ATOM_EDID_PATH: f'"{self.published}"'}))
        self.stack.enter_context(patch.object(
            display, '_display_owner', return_value=self.edid.stat().st_uid))
        self.plugin = display.Plugin()
        self.configure({})

    def configure(self, fields, enabled=True):
        self.state.clear()
        self.state.update(self.initial, **fields)
        self.state['edid_fix'] = enabled
        self.saved = {**display.DEFAULT_SETTINGS, 'panel_mode': self.state['panel_mode'],
                      'edid_fix': enabled, 'reset_in_progress': self.state['reset_in_progress']}
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
        self.assertFalse(self.script.exists())
        for offset in range(0, len(expected), 128):
            self.assertEqual(sum(expected[offset:offset + 128]) & 0xFF, 0)
        self.assertEqual(expected[126], 1)
        self.assertEqual(expected[128:], original[128:256], 'CTA HDR data is unchanged.')

    def test_pass_patches_without_legacy_setup_through_native_handover(self):
        for name, fields in DISPLAY_STATES:
            with self.subTest(state=name):
                self.configure(fields)
                display.Plugin._edid_pass()
                self.assert_patched()
                self.assertEqual(self.persisted(), {**self.saved, 'edid_patched_identity': self.owned()})
                for key, value in fields.items():
                    self.assertEqual(self.state[key], value)
                self.read_props.assert_called_once_with([display.ATOM_EDID_PATH])
                display.Plugin._edid_pass()
                self.assert_patched()

    def test_toggle_persists_and_restores_independently_of_native_handover(self):
        for name, fields in DISPLAY_STATES:
            with self.subTest(state=name):
                self.configure(fields, enabled=False)
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
                self.assertFalse(self.script.exists())

    def test_disabled_pass_restores_only_the_owned_edid(self):
        self.configure({'native_support_detected': True, 'native_cleanup_pending': True})
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
        self.configure({'native_support_detected': True})
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
                    self.configure({})
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

    def test_dock_and_undock_follow_new_published_paths_without_setup(self):
        self.configure({'native_support_detected': True, 'native_restart_pending': True})
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

    def test_legacy_reset_still_pauses_edid_until_reset_finishes(self):
        self.configure({'reset_in_progress': True})
        display.Plugin._edid_pass()
        self.assertEqual(self.edid.read_bytes(), self.original)
        self.assertFalse(self.backup.exists())
        self.read_props.assert_not_called()


class PublishedEdidOpenTests(unittest.TestCase):
    """Root opens of the X-published path never follow links, block or over-read."""
    setUp = NativeDisplayEdidTests.setUp
    configure = NativeDisplayEdidTests.configure

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
                self.configure({})
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


if __name__ == '__main__':
    unittest.main()

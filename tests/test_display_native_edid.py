# SPDX-License-Identifier: BSD-3-Clause
"""EDID remains independent of legacy/native display setup, using real files."""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_integration  # Decky/Linux doubles before importing the backend.
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
            display, '_gamescope_owner_uid', return_value=self.edid.stat().st_uid))
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
                self.assertEqual(self.persisted(), self.saved)
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
                self.assertEqual(self.persisted(), {**self.saved, 'edid_fix': True})
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


if __name__ == '__main__':
    unittest.main()

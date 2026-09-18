# SPDX-License-Identifier: BSD-3-Clause
"""Display reset uses real files/JSON while isolating Gamescope atom writes."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_integration
import display_backend as display
from safe_settings import AtomicSettingsManager


class DisplayResetTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.script = self.root / 'lenovo.legiongo2.oled.lua'
        self.backup = Path(str(self.script) + '.backup')
        self.script.write_bytes(Path(display.BUNDLED_SCRIPTS[display.MODE_PQ]).read_bytes())
        self.edid = self.root / 'published-edid.bin'
        self.edid.write_bytes(b'patched-edid')
        self.edid_backup = self.root / 'edid-original.bin'
        self.edid_backup.write_bytes(b'original-edid')
        self.store = AtomicSettingsManager('display_settings', str(self.root / 'settings'))
        self.saved = {**display.DEFAULT_SETTINGS, 'panel_mode': 'hybrid',
                      'active_mode': 'hybrid', 'enabled': False, 'edid_fix': True,
                      'brightness_baseline': 250.0, 'brightness_baseline_session': 100.0,
                      'hdr_baseline': False, 'hdr_baseline_session': 100.0}
        self.store.replace(self.saved)
        self.state = dict(display.Plugin._state, panel_mode='hybrid', active_mode='hybrid',
                          setup_done=True, active=True, reset_in_progress=False,
                          reset_restart_pending=False, reset_error='', settings_error='')
        self.atoms = []
        self.nits = []
        self.session = 100.0
        self.external = 0
        for name, value in {'settings': self.store, 'INSTALLED_SCRIPT': str(self.script),
                            'EDID_BACKUP': str(self.edid_backup)}.items():
            self.stack.enter_context(patch.object(display, name, value))
        for name, value in {'_state': self.state, '_baseline': 250.0,
                            '_hdr_baseline': False, '_last_written': 120.0,
                            '_edid_original': b'original-edid', '_edid_path': str(self.edid),
                            '_loaded_script_variant': display.MODE_PQ,
                            '_gamescope_started_at': 100.0, '_mode_lock': asyncio.Lock()}.items():
            self.stack.enter_context(patch.object(display.Plugin, name, value))
        self.stack.enter_context(patch.object(display, '_gamescope_start_time', side_effect=lambda: self.session))
        self.stack.enter_context(patch.object(display, '_strip_displayid', return_value=b'patched-edid'))
        self.stack.enter_context(patch.object(display, '_published_edid_is_safe', return_value=True))
        self.stack.enter_context(patch.object(display.Plugin, '_session_props',
                                            side_effect=lambda _names: {display.ATOM_IS_EXTERNAL: self.external}))
        self.stack.enter_context(patch.object(display, '_write_nits',
                                            side_effect=lambda value: self.nits.append(value) or True))
        self.stack.enter_context(patch.object(display, '_write_atom_int',
                                            side_effect=lambda name, value: self.atoms.append((name, value)) or True))
        self.plugin = display.Plugin()

    def reset(self):
        return asyncio.run(self.plugin.reset_settings())

    def persisted(self):
        fresh = AtomicSettingsManager('display_settings', str(self.root / 'settings'))
        return fresh.settings

    def assert_paused(self):
        self.assertTrue(self.persisted()['reset_in_progress'])
        self.assertEqual(self.persisted()['panel_mode'], 'hybrid')
        self.assertFalse(self.state['setup_done'])
        previous = (list(self.atoms), list(self.nits), self.edid.read_bytes())
        self.assertFalse(display.Plugin._refresh_gate())
        display.Plugin._hybrid_pass()
        display.Plugin._edid_pass()
        display.Plugin._forward_nits(12.0)
        self.assertEqual(previous, (self.atoms, self.nits, self.edid.read_bytes()))

    def test_reset_restores_atoms_edid_and_initial_settings_removes_owned_script(self):
        result = self.reset()
        self.assertEqual(result['reset_error'], '')
        self.assertIsNone(result['panel_mode'])
        self.assertIsNone(result['active_mode'])
        self.assertTrue(result['reset_restart_pending'])
        self.assertFalse(self.script.exists())
        self.assertFalse(self.edid_backup.exists())
        self.assertEqual(self.edid.read_bytes(), b'original-edid')
        self.assertEqual(self.nits, [250.0])
        self.assertEqual(self.atoms, [(display.ATOM_FORCE_HDR_SUPPORT, 0), (display.ATOM_HDR_ENABLED, 0)])
        self.assertEqual(self.persisted(), {**display.DEFAULT_SETTINGS, 'reset_session': 100.0})
        # A loop iteration may still carry a gate result from before the reset.
        self.state['active'] = True
        display.Plugin._forward_nits(12.0)
        self.assertFalse(self.state['active'])
        self.assertEqual(self.nits, [250.0])
        display.Plugin._refresh_setup()
        self.assertTrue(self.state['reset_restart_pending'])
        self.session = 200.0
        display.Plugin._refresh_setup()
        self.assertFalse(self.state['reset_restart_pending'])
        self.assertEqual(self.persisted(), display.DEFAULT_SETTINGS)

    def test_reset_restores_preexisting_script_and_preserves_external_display(self):
        self.backup.write_bytes(b'previous-third-party-script')
        self.external = 1
        self.edid.write_bytes(b'new-display-edid')
        result = self.reset()
        self.assertEqual(result['reset_error'], '')
        self.assertEqual(self.script.read_bytes(), b'previous-third-party-script')
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.edid.read_bytes(), b'new-display-edid')
        self.assertFalse(self.edid_backup.exists())
        self.assertEqual(self.nits, [])
        self.assertEqual(self.atoms, [(display.ATOM_FORCE_HDR_SUPPORT, 0)])

    def test_reset_preserves_script_replaced_by_someone_else(self):
        self.script.write_bytes(b'new-third-party-script')
        result = self.reset()
        self.assertEqual(result['reset_error'], '')
        self.assertEqual(self.script.read_bytes(), b'new-third-party-script')
        self.assertIn('third-party', result['reset_note'])

    def test_failed_journal_commit_does_not_touch_display_or_files(self):
        script_before = self.script.read_bytes()
        with patch.object(self.store, 'commit', side_effect=OSError('journal failure')):
            result = self.reset()
        self.assertIn('journal failure', result['reset_error'])
        self.assertFalse(result['reset_in_progress'])
        self.assertEqual(self.persisted(), self.saved)
        self.assertEqual(self.script.read_bytes(), script_before)
        self.assertEqual(self.edid.read_bytes(), b'patched-edid')
        self.assertEqual(self.atoms, [])
        self.assertEqual(self.nits, [])

    def test_failed_atom_restoration_keeps_journal_and_suppresses_hardware_passes(self):
        with patch.object(display, '_write_atom_int', return_value=False):
            result = self.reset()
        self.assertIn('HDR support', result['reset_error'])
        self.assert_paused()
        self.assertTrue(self.script.exists())
        self.assertEqual(self.reset()['reset_error'], '')

    def test_failed_edid_restoration_keeps_original_for_retry(self):
        with patch.object(display, '_rewrite_file_if_unchanged', return_value='failed'):
            result = self.reset()
        self.assertIn('EDID', result['reset_error'])
        self.assert_paused()
        self.assertTrue(self.edid_backup.exists())
        self.assertEqual(self.reset()['reset_error'], '')
        self.assertEqual(self.edid.read_bytes(), b'original-edid')

    def test_final_commit_failure_can_retry_after_script_and_edid_are_restored(self):
        with patch.object(self.store, 'replace', side_effect=OSError('final commit failure')):
            result = self.reset()
        self.assertIn('final commit failure', result['reset_error'])
        self.assert_paused()
        self.assertFalse(self.script.exists())
        result = self.reset()
        self.assertEqual(result['reset_error'], '')
        self.assertTrue(result['reset_restart_pending'])
        self.assertEqual(self.persisted(), {**display.DEFAULT_SETTINGS, 'reset_session': 100.0})

    def test_recovery_in_fresh_session_does_not_carry_old_atom_baselines(self):
        with patch.object(self.store, 'replace', side_effect=OSError('interrupted reset')):
            self.reset()
        self.session = 200.0
        display.Plugin._gamescope_started_at = None
        display.Plugin._baseline = None
        display.Plugin._hdr_baseline = None
        self.nits.clear()
        result = self.reset()
        self.assertEqual(result['reset_error'], '')
        self.assertFalse(result['reset_restart_pending'])
        self.assertEqual(self.nits, [])
        self.assertEqual(self.persisted(), display.DEFAULT_SETTINGS)

    def test_unknown_display_keeps_reset_pending_and_does_not_write_atoms(self):
        self.external = None
        result = self.reset()
        self.assertIn('active display', result['reset_error'])
        self.assert_paused()
        self.assertEqual(self.atoms, [])
        self.assertEqual(self.nits, [])

    def test_startup_finishes_durable_reset_before_any_display_pass(self):
        self.store.replace({**self.saved, 'reset_in_progress': True, 'reset_session': 100.0})
        with patch.object(display.updater, 'ssl_context'), \
                patch.object(display, '_pick_display', return_value=True), \
                patch.object(display, '_identify_panel', return_value=(True, 'isolated panel')), \
                patch.object(display, '_find_backlight', return_value=''), \
                patch.object(display, '_watch_properties', new=AsyncMock()), \
                patch.object(self.plugin, '_loop', new=AsyncMock()):
            asyncio.run(self.plugin._main())
        self.assertFalse(self.persisted()['reset_in_progress'])
        self.assertIsNone(self.persisted()['panel_mode'])
        self.assertFalse(self.script.exists())
        self.assertEqual(self.edid.read_bytes(), b'original-edid')
        self.assertEqual(self.atoms, [(display.ATOM_FORCE_HDR_SUPPORT, 0), (display.ATOM_HDR_ENABLED, 0)])

    def test_unknown_session_keeps_reset_pending_without_hardware_writes(self):
        self.session = None
        result = self.reset()
        self.assertIn('Gaming Mode session', result['reset_error'])
        self.assert_paused()
        self.assertEqual(self.atoms, [])
        self.assertEqual(self.nits, [])


if __name__ == '__main__':
    unittest.main()

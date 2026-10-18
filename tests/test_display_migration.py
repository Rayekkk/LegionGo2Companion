# SPDX-License-Identifier: BSD-3-Clause
"""2.1.0 installed the display script under /root; 2.2 moves it where gamescope looks."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import support  # Decky/Linux doubles before importing the backend.
import display_backend as display
from safe_settings import AtomicSettingsManager


class LegacyScriptMigrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.legacy = root / 'root-config' / 'gamescope' / 'scripts'
        self.dir = root / 'deck-config' / 'gamescope' / 'scripts'
        self.legacy.mkdir(parents=True)
        self.script, self.old = self.dir / display.SCRIPT_NAME, self.legacy / display.SCRIPT_NAME
        self.pq = Path(display.BUNDLED_SCRIPTS[display.MODE_PQ]).read_bytes()
        self.g22 = Path(display.BUNDLED_SCRIPTS[display.MODE_G22]).read_bytes()
        for name, value in {'SCRIPT_DIR': str(self.dir), 'INSTALLED_SCRIPT': str(self.script),
                            'SCRIPT_USER': None, 'LEGACY_SCRIPT_DIR': str(self.legacy),
                            'BUNDLED_SCRIPT': display.BUNDLED_SCRIPT}.items():
            self.stack.enter_context(patch.object(display, name, value))
        self.state = {'panel_mode': display.MODE_PQ, 'reset_in_progress': False}
        self.stack.enter_context(patch.object(display.Plugin, '_state', self.state))
        self.stack.enter_context(patch.object(display.Plugin, '_refresh_native_support'))
        self.native = self.stack.enter_context(
            patch.object(display.Plugin, 'native_display_active', return_value=False))
        self.bind = self.stack.enter_context(patch.object(display, '_bind_script_dir', return_value=True))
        self.event = self.stack.enter_context(patch.object(display, 'diagnostic_event'))

    def migrate(self):
        display.Plugin._migrate_legacy_script()

    def test_each_mode_gets_the_variant_it_needs_and_the_root_copy_goes(self):
        for mode, data in ((display.MODE_PQ, self.pq), (display.MODE_G22, self.g22),
                           (display.MODE_HYBRID, self.pq)):
            with self.subTest(mode=mode):
                self.state['panel_mode'] = mode
                self.old.write_bytes(data)
                self.migrate()
                self.assertEqual(self.script.read_bytes(), data)
                self.assertFalse(self.old.exists())
                self.assertFalse(Path(str(self.script) + '.backup').exists())
                self.script.unlink()
        self.event.assert_not_called()

    def test_nothing_happens_without_a_saved_mode_or_during_a_reset(self):
        self.old.write_bytes(self.pq)
        for change in ({'panel_mode': None}, {'reset_in_progress': True}):
            with self.subTest(change=change):
                self.state.update({'panel_mode': display.MODE_PQ, 'reset_in_progress': False}, **change)
                self.migrate()
                self.assertFalse(self.script.exists())
                self.assertTrue(self.old.exists())

    def test_native_support_keeps_the_legacy_path_untouched(self):
        self.native.return_value = True
        self.old.write_bytes(self.pq)
        self.migrate()
        self.assertFalse(self.script.exists())
        self.assertEqual(self.old.read_bytes(), self.pq)

    def test_foreign_or_wrong_variant_legacy_script_is_not_migrated(self):
        for data in (b'-- hand tuned', self.g22, self.pq + b'-- edited'):
            with self.subTest(data=data[:12]):
                self.old.write_bytes(data)
                self.migrate()
                self.assertFalse(self.script.exists())
                self.assertEqual(self.old.read_bytes(), data)

    def test_missing_legacy_script_does_nothing(self):
        self.migrate()
        self.assertFalse(self.dir.exists())

    def test_an_existing_target_is_never_replaced_or_backed_up(self):
        self.dir.mkdir(parents=True)
        self.script.write_bytes(b'-- hand tuned')
        self.old.write_bytes(self.pq)
        self.migrate()
        self.assertEqual(self.script.read_bytes(), b'-- hand tuned')
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [display.SCRIPT_NAME])
        self.assertEqual(self.old.read_bytes(), self.pq)

    def test_unknown_gamescope_user_changes_nothing(self):
        self.bind.return_value = False
        self.old.write_bytes(self.pq)
        self.migrate()
        self.assertFalse(self.dir.exists())
        self.assertEqual(self.old.read_bytes(), self.pq)

    def test_failure_is_reported_and_leaves_setup_available(self):
        self.old.write_bytes(self.pq)
        with patch.object(display, '_write_file_atomically', return_value=False):
            self.migrate()
        self.assertFalse(self.script.exists())
        self.assertEqual(self.state['panel_mode'], display.MODE_PQ)
        self.assertEqual(self.event.call_args.args[:2], ('display', 'script_migration_failed'))
        self.event.reset_mock()
        self.old.write_bytes(self.pq)
        with patch.object(display, '_install_script', side_effect=RuntimeError('boom')):
            self.migrate()
        self.assertEqual(self.event.call_args.args[:2], ('display', 'script_migration_failed'))
        self.assertFalse(self.script.exists())


class MigrationRunsAtStartTests(unittest.TestCase):
    def test_main_migrates_once_before_the_setup_state_is_computed(self):
        with tempfile.TemporaryDirectory() as raw:
            manager = AtomicSettingsManager('display_settings', raw)
            manager.replace({**display.DEFAULT_SETTINGS, 'panel_mode': display.MODE_PQ})
            order = Mock()
            with ExitStack() as stack:
                for name, value in {'settings': manager, '_pick_display': Mock(return_value=True),
                                    '_read_bytes': Mock(return_value=None),
                                    '_identify_panel': Mock(return_value=(False, 'test')),
                                    '_find_backlight': Mock(return_value=''),
                                    '_watch_properties': Mock(return_value=asyncio.sleep(0))}.items():
                    stack.enter_context(patch.object(display, name, value))
                for name, value in {'_state': dict(display.Plugin._state), '_task': None, '_prop_task': None,
                                    '_loop': AsyncMock(), '_session_props': Mock(), '_edid_pass': Mock(),
                                    '_migrate_legacy_script': order.migrate,
                                    '_refresh_setup': order.refresh,
                                    'native_display_active': Mock(return_value=False)}.items():
                    stack.enter_context(patch.object(display.Plugin, name, value))
                plugin = display.Plugin()

                async def run():
                    await plugin._main()
                    await asyncio.gather(plugin._task, plugin._prop_task)
                asyncio.run(run())
        self.assertEqual([call[0] for call in order.mock_calls], ['migrate', 'refresh'])


if __name__ == '__main__':
    unittest.main()

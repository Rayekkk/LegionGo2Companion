# SPDX-License-Identifier: BSD-3-Clause
"""Native display handoff keeps durable ownership and never guesses baselines."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_integration
import display_backend as display
from safe_settings import AtomicSettingsManager


class NativeDisplayMigrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.script = self.root / 'lenovo.legiongo2.oled.lua'
        self.backup = Path(str(self.script) + '.backup')
        self.own_lua = Path(display.BUNDLED_SCRIPTS[display.MODE_PQ]).read_bytes()
        self.script.write_bytes(self.own_lua)
        self.store = AtomicSettingsManager('display_settings', str(self.root / 'settings'))
        self.saved = {**display.DEFAULT_SETTINGS, 'panel_mode': 'hybrid',
                      'active_mode': 'hybrid', 'enabled': False, 'edid_fix': True,
                      'brightness_baseline': 250.0, 'brightness_baseline_session': 100.0,
                      'hdr_baseline': False, 'hdr_baseline_session': 100.0}
        self.store.replace(self.saved)
        self.state = dict(display.Plugin._state, panel_mode='hybrid', active_mode='hybrid',
                          enabled=False, edid_fix=True, setup_done=True, active=True,
                          hdr_now=True, reset_in_progress=False, reset_error='',
                          native_support_detected=False, native_support_reason='',
                          native_cleanup_pending=False, native_cleanup_error='',
                          native_restart_pending=False)
        self.session = 100.0
        self.detection = dict(status='supported', supported=True,
                              reason='system_profile_present')
        self.props = {display.ATOM_IS_EXTERNAL: 0,
                      display.ATOM_SDR_NITS: display._float_raw(120.0),
                      display.ATOM_HDR_ENABLED: 1, display.ATOM_FORCE_HDR_SUPPORT: 1}
        self.atoms = []
        for name, value in {'settings': self.store, 'INSTALLED_SCRIPT': str(self.script),
                            '_gamescope_pid': 123}.items():
            self.stack.enter_context(patch.object(display, name, value))
        for name, value in {'_state': self.state, '_baseline': 250.0,
                            '_hdr_baseline': False, '_last_written': 120.0,
                            '_loaded_script_variant': display.MODE_PQ,
                            '_gamescope_started_at': 100.0, '_mode_lock': asyncio.Lock(),
                            '_native_probe_after': 0.0, '_native_probe_session': None,
                            '_native_detection': {}, '_edid_recheck_until': 0.0,
                            '_hybrid_pq_hold_until': 0.0, '_hybrid_sdr_since': None,
                            '_hdr_fights': 0, '_hdr_backoff_until': 0.0,
                            '_task': None, '_prop_task': None}.items():
            self.stack.enter_context(patch.object(display.Plugin, name, value))
        self.stack.enter_context(patch.object(display, '_gamescope_start_time', side_effect=lambda: self.session))
        self.stack.enter_context(patch.object(display, '_loaded_script_variant', return_value=display.MODE_PQ))
        self.detector = self.stack.enter_context(patch.object(display.display_native,
            'detect_native_display_support', side_effect=lambda: dict(self.detection)))
        self.stack.enter_context(patch.object(display.Plugin, '_session_props', side_effect=lambda _names: dict(self.props)))
        self.stack.enter_context(patch.object(display, '_read_props_uncached', side_effect=lambda _names: dict(self.props)))
        self.stack.enter_context(patch.object(display, '_write_atom_int', side_effect=self.write_atom))
        self.plugin = display.Plugin()

    def write_atom(self, name, value):
        # Every destructive side effect must have a durable recovery journal.
        self.assertIsNotNone(self.persisted()['native_cleanup'])
        self.atoms.append((name, value))
        self.props[name] = value
        return True

    def persisted(self):
        return AtomicSettingsManager('display_settings', str(self.root / 'settings')).settings

    def refresh(self):
        display.Plugin._refresh_native_support(True)
        return dict(self.state)

    def fresh_instance(self):
        display.Plugin._gamescope_started_at = None
        display.Plugin._loaded_script_variant = None
        display.Plugin._baseline = None
        display.Plugin._hdr_baseline = None
        display.Plugin._last_written = None
        self.state.update(active_mode=None, hdr_now=False, native_support_detected=False,
                          native_cleanup_pending=False, native_restart_pending=False)

    def assert_preferences(self):
        for key in ('enabled', 'edid_fix', 'panel_mode'):
            self.assertEqual(self.persisted()[key], self.saved[key])

    def test_same_session_hybrid_restores_owned_values_and_removes_only_owned_files(self):
        self.backup.write_bytes(self.own_lua)
        result = self.refresh()
        self.assertFalse(result['native_cleanup_pending'])
        self.assertTrue(result['native_restart_pending'])
        self.assertEqual(self.atoms, [(display.ATOM_SDR_NITS, display._float_raw(250.0)),
                                     (display.ATOM_FORCE_HDR_SUPPORT, 0), (display.ATOM_HDR_ENABLED, 0)])
        self.assertFalse(self.script.exists())
        self.assertFalse(self.backup.exists())
        self.assertTrue(self.persisted()['native_retired'])
        self.assertIsNone(self.persisted()['native_cleanup'])
        self.assert_preferences()
        self.detector.assert_called_with()
        self.assertIn('no longer needed', result['native_support_reason'])
        self.assertNotIn('system_profile_present', result['native_support_reason'])

    def test_foreign_current_and_backup_are_preserved_and_never_restored(self):
        self.script.write_bytes(b'foreign-current')
        self.backup.write_bytes(b'foreign-backup')
        result = self.refresh()
        self.assertEqual(self.script.read_bytes(), b'foreign-current')
        self.assertEqual(self.backup.read_bytes(), b'foreign-backup')
        self.assertIn('third-party display script', result['native_support_reason'])
        self.assertIn('third-party display-script backup', result['native_support_reason'])
        self.refresh()
        self.assertIn('third-party', self.state['native_support_reason'])

    def test_own_current_is_removed_without_restoring_foreign_backup(self):
        self.backup.write_bytes(b'foreign-backup')
        self.refresh()
        self.assertFalse(self.script.exists())
        self.assertEqual(self.backup.read_bytes(), b'foreign-backup')

    def test_restart_requirement_survives_reload_until_a_new_supported_session(self):
        self.refresh()
        self.fresh_instance()
        self.atoms.clear()
        self.refresh()
        self.assertTrue(self.state['native_restart_pending'])
        self.session = 200.0
        self.refresh()
        self.assertFalse(self.state['native_restart_pending'])
        self.assertEqual(self.atoms, [])

    def test_first_detection_without_legacy_files_still_waits_for_profile_reload(self):
        self.script.unlink()
        self.store.replace(display.DEFAULT_SETTINGS)
        self.fresh_instance()
        self.state.update(panel_mode=None, active_mode=None)
        self.refresh()
        self.assertTrue(self.state['native_support_detected'])
        self.assertTrue(self.state['native_restart_pending'])
        self.assertEqual(self.atoms, [])
        self.session = 200.0
        self.refresh()
        self.assertFalse(self.state['native_restart_pending'])
        self.assertEqual(self.atoms, [])

    def test_completed_retirement_does_not_require_another_restart_after_upgrade(self):
        self.refresh()
        self.session = 200.0
        self.refresh()
        self.fresh_instance()
        self.atoms.clear()
        self.refresh()
        self.assertTrue(self.state['native_support_detected'])
        self.assertFalse(self.state['native_restart_pending'])
        self.assertFalse(self.state['native_cleanup_pending'])
        self.assertEqual(self.atoms, [])

    def test_fresh_reload_recovers_baselines_but_does_not_guess_last_written_value(self):
        self.fresh_instance()
        self.refresh()
        self.assertFalse(self.script.exists())
        journal = self.persisted()['native_cleanup']
        self.assertEqual(journal['brightness']['baseline'], 250.0)
        self.assertIs(journal['hdr']['baseline'], False)
        self.assertTrue(self.state['native_cleanup_pending'])
        self.assertTrue(self.state['native_restart_pending'])
        self.assertEqual(self.atoms, [])
        self.session = 200.0
        self.refresh()
        self.assertFalse(self.state['native_cleanup_pending'])
        self.assertFalse(self.state['native_restart_pending'])
        self.assertEqual(self.atoms, [])
        self.assert_preferences()

    def test_pq_without_hdr_baseline_removes_lua_then_waits_for_new_session(self):
        self.saved.update(panel_mode='pq', active_mode='pq', hdr_baseline=None, hdr_baseline_session=None)
        self.store.replace(self.saved)
        self.state.update(panel_mode='pq', active_mode='pq')
        display.Plugin._hdr_baseline = None
        self.props[display.ATOM_SDR_NITS] = display._float_raw(250.0)
        self.refresh()
        self.assertFalse(self.script.exists())
        self.assertTrue(self.state['native_cleanup_pending'])
        self.assertIn('HDR baseline', self.state['native_cleanup_error'])
        self.assertTrue(self.state['native_restart_pending'])
        self.assertEqual(self.atoms, [])
        self.refresh()
        self.assertEqual(self.atoms, [])
        self.session = 200.0
        self.refresh()
        self.assertFalse(self.state['native_cleanup_pending'])
        self.assertEqual(self.atoms, [])

    def test_unknown_ownership_waits_for_a_change_after_first_observed_session(self):
        self.session = None
        self.refresh()
        self.assertFalse(self.script.exists())
        self.assertTrue(self.state['native_restart_pending'])
        self.session = 100.0
        self.refresh()
        self.refresh()
        self.assertTrue(self.state['native_cleanup_pending'])
        self.assertTrue(self.persisted()['native_cleanup']['wait_for_new_session'])
        self.assertEqual(self.atoms, [])
        self.session = 200.0
        self.refresh()
        self.assertFalse(self.state['native_cleanup_pending'])
        self.assertFalse(self.state['native_restart_pending'])
        self.assertEqual(self.atoms, [])

    def test_journal_failure_prevents_all_destructive_effects_and_retries(self):
        with patch.object(self.store, 'replace', side_effect=OSError('journal failure')):
            self.refresh()
        self.assertEqual(self.script.read_bytes(), self.own_lua)
        self.assertEqual(self.atoms, [])
        self.assertEqual(self.persisted(), self.saved)
        self.assertTrue(display.Plugin.native_display_active())
        self.assertIn('journal failure', self.state['native_cleanup_error'])
        self.refresh()
        self.assertFalse(self.state['native_cleanup_pending'])

    def test_final_commit_failure_preserves_journal_and_retry_is_idempotent(self):
        original = self.store.replace
        def replace(payload):
            if payload.get('native_retired'):
                raise OSError('final commit failure')
            original(payload)
        with patch.object(self.store, 'replace', side_effect=replace):
            self.refresh()
        self.assertFalse(self.script.exists())
        self.assertIsNotNone(self.persisted()['native_cleanup'])
        self.assertIn('final commit failure', self.state['native_cleanup_error'])
        before = list(self.atoms)
        self.refresh()
        self.assertEqual(self.atoms, before)
        self.assertFalse(self.state['native_cleanup_pending'])

    def test_failed_atom_readback_keeps_journal_and_fences_all_legacy_writers(self):
        with patch.object(display, '_write_atom_int', return_value=True):
            self.refresh()
        self.assertTrue(self.state['native_cleanup_pending'])
        self.assertIn('did not confirm', self.state['native_cleanup_error'])
        with patch.object(display, '_write_atom_int') as write, patch.object(display, '_write_nits') as nits:
            self.state['active'] = True
            display.Plugin._forward_nits(20.0)
            display.Plugin._correct_hdr_mode(True)
            display.Plugin._hybrid_pass()
            display.Plugin._hybrid_release()
            display.Plugin._release()
            self.assertFalse(display.Plugin._refresh_gate())
        write.assert_not_called()
        nits.assert_not_called()
        self.refresh()
        self.assertFalse(self.state['native_cleanup_pending'])

    def test_external_display_keeps_its_brightness_and_hdr_mode(self):
        self.props[display.ATOM_IS_EXTERNAL] = 1
        self.refresh()
        self.assertEqual(self.atoms, [(display.ATOM_FORCE_HDR_SUPPORT, 0)])
        self.assertFalse(self.state['native_cleanup_pending'])

    def test_hybrid_without_brightness_ownership_finishes_without_a_missing_baseline_error(self):
        self.saved.update(brightness_baseline=None, brightness_baseline_session=None)
        self.store.replace(self.saved)
        display.Plugin._baseline = None
        display.Plugin._last_written = None
        self.refresh()
        self.assertFalse(self.state['native_cleanup_pending'])
        self.assertEqual(self.state['native_cleanup_error'], '')
        self.assertTrue(self.state['native_restart_pending'])
        self.assertEqual(self.atoms, [(display.ATOM_FORCE_HDR_SUPPORT, 0), (display.ATOM_HDR_ENABLED, 0)])

    def test_values_changed_by_another_writer_are_not_overwritten(self):
        self.props[display.ATOM_SDR_NITS] = display._float_raw(300.0)
        self.props[display.ATOM_HDR_ENABLED] = 0
        self.props[display.ATOM_FORCE_HDR_SUPPORT] = 2
        self.refresh()
        self.assertEqual(self.atoms, [])

    def test_migration_blocks_setup_mode_enabled_and_full_reset_without_changing_preferences(self):
        self.refresh()
        self.atoms.clear()
        async def run():
            await self.plugin.run_setup('gamma22')
            await self.plugin.set_panel_mode('pq')
            await self.plugin.set_enabled(True)
            await self.plugin.reset_settings()
        asyncio.run(run())
        self.assert_preferences()
        self.assertEqual(self.atoms, [])
        self.assertFalse(self.script.exists())
        # A native probe can also finish after the RPC's check but before its
        # worker acquires the shared runtime lock.
        display.Plugin._reset_settings_locked()
        self.assert_preferences()
        self.assertEqual(self.atoms, [])

    def test_missing_profile_reopens_explicit_setup_only_after_restart_without_auto_install(self):
        self.refresh()
        self.detection.update(status='inconclusive', supported=False, reason='read_error')
        self.refresh()
        self.assertTrue(display.Plugin.native_display_active())
        self.assertIn('detected earlier', self.state['native_support_reason'])
        self.detection.update(status='absent', supported=False, reason='system_profile_missing')
        self.refresh()
        self.assertTrue(display.Plugin.native_display_active())
        self.assertTrue(self.state['native_restart_pending'])
        self.assertIn('after changing the installed gamescope version', self.state['native_support_reason'])
        self.session = 200.0
        self.refresh()
        self.assertFalse(display.Plugin.native_display_active())
        self.assertFalse(self.script.exists())
        with patch.object(display, '_install_script', return_value=(True, 'installed')) as install:
            asyncio.run(self.plugin.run_setup('gamma22'))
        install.assert_called_once()

    def test_fresh_uninstall_detects_native_before_default_atom_writes_or_backup_restore(self):
        self.fresh_instance()
        self.backup.write_bytes(b'foreign-backup')
        with patch.object(display.Plugin, '_edid_restore', return_value=True), \
             patch.object(display, '_uninstall_script') as legacy_uninstall:
            asyncio.run(self.plugin._uninstall())
        legacy_uninstall.assert_not_called()
        self.assertEqual(self.atoms, [])
        self.assertFalse(self.script.exists())
        self.assertEqual(self.backup.read_bytes(), b'foreign-backup')
        self.assertTrue(self.state['native_restart_pending'])

    def test_unknown_session_without_atoms_still_keeps_removed_lua_restart_requirement(self):
        self.store.replace({**display.DEFAULT_SETTINGS, 'active_mode': 'gamma22', 'panel_mode': 'gamma22'})
        self.fresh_instance()
        self.state.update(active_mode='gamma22', panel_mode='gamma22')
        self.session = None
        self.refresh()
        self.assertFalse(self.state['native_cleanup_pending'])
        self.assertTrue(self.state['native_restart_pending'])
        self.session = 100.0
        self.refresh()
        self.refresh()
        self.assertTrue(self.state['native_restart_pending'])
        self.session = 200.0
        self.refresh()
        self.assertFalse(self.state['native_restart_pending'])
        self.assertEqual(self.atoms, [])

    def test_missing_file_does_not_prove_previously_loaded_gamma_script_was_unloaded(self):
        self.script.unlink()
        self.store.replace({**display.DEFAULT_SETTINGS, 'active_mode': 'gamma22', 'panel_mode': 'gamma22'})
        self.fresh_instance()
        self.state.update(panel_mode='gamma22')
        with patch.object(display, '_loaded_script_variant', return_value=None):
            self.refresh()
        self.assertTrue(self.state['native_restart_pending'])
        self.assertEqual(self.atoms, [])

    def test_get_state_discovers_native_and_runs_cleanup_before_any_startup(self):
        self.fresh_instance()
        result = asyncio.run(self.plugin.get_state())
        self.assertTrue(result['native_support_detected'])
        self.assertFalse(self.script.exists())
        self.assertEqual(self.atoms, [])

    def test_session_change_during_atom_cleanup_does_not_write_to_new_gamescope(self):
        def change_session(_names):
            self.session = 200.0
            return dict(self.props)
        with patch.object(display, '_read_props_uncached', side_effect=change_session):
            self.refresh()
        self.assertEqual(self.atoms, [])
        self.assertTrue(self.state['native_cleanup_pending'])
        self.refresh()
        self.assertFalse(self.state['native_cleanup_pending'])

    def test_native_loop_closes_existing_backlight_watch_and_never_polls_slider(self):
        real_close = display.os.close

        def close_notify_only(fd):
            if fd != 77:
                real_close(fd)

        async def run():
            with patch.object(display, '_open_notify', return_value=77), \
                 patch.object(display.os, 'close', side_effect=close_notify_only) as close, \
                 patch.object(display.Plugin, '_edid_pass') as edid, \
                 patch.object(display, '_read_int') as read, \
                 patch.object(display, '_wait_for_change') as wait, \
                 patch.object(display.asyncio, 'sleep', side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await self.plugin._loop('fake-backlight')
                self.assertEqual(sum(call.args == (77,) for call in close.call_args_list), 1)
                edid.assert_called()
                read.assert_not_called()
                wait.assert_not_called()
        asyncio.run(run())

    def test_unload_rechecks_native_after_async_release_before_legacy_zero_writes(self):
        self.detection.update(status='absent', supported=False)
        def retire_during_release():
            self.state['native_support_detected'] = True
        with patch.object(display.Plugin, '_release', side_effect=retire_during_release), \
             patch.object(display.Plugin, '_edid_restore', return_value=True), \
             patch.object(display, '_write_atom_int') as write:
            asyncio.run(self.plugin._unload(uninstalling=True))
        write.assert_not_called()

    def test_unreadable_settings_unload_stops_both_workers_without_hardware_writes(self):
        async def run():
            workers = [asyncio.create_task(asyncio.sleep(60)) for _ in range(2)]
            display.Plugin._task, display.Plugin._prop_task = workers
            with patch.object(display.Plugin, '_read_settings', side_effect=OSError('unreadable settings')), \
                 patch.object(display, '_write_atom_int') as write, \
                 patch.object(display, '_write_nits') as nits, \
                 patch.object(display.Plugin, '_edid_restore') as edid:
                with self.assertRaisesRegex(OSError, 'unreadable settings'):
                    await self.plugin._unload(uninstalling=True)
                self.assertTrue(all(task.cancelled() for task in workers))
                self.assertIsNone(display.Plugin._task)
                self.assertIsNone(display.Plugin._prop_task)
                write.assert_not_called()
                nits.assert_not_called()
                edid.assert_not_called()
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()

# SPDX-License-Identifier: BSD-3-Clause
"""Display start, discovery, reset, native-profile migration and shutdown."""
import asyncio
from contextlib import ExitStack
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import support  # Decky/Linux doubles before importing the backend.
import display_backend as display
import display_native as native
import module_runtime
from safe_settings import AtomicSettingsManager, CorruptSettings


REAL_SESSION_PROPS = display.Plugin._session_props


class LegacyHybridFixture:
    """Shared setUp: Hybrid set up the legacy (Lua) way in Gaming Mode session 100.

    Our PQ script is installed, both baselines are saved, and gamescope's session follows `self.session`.
    """
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
                          setup_done=True, active=True, reset_in_progress=False, reset_error='',
                          native_support_detected=False, native_cleanup_pending=False,
                          native_restart_pending=False)
        self.atoms = []
        self.session = 100.0
        self.patch(display, settings=self.store, INSTALLED_SCRIPT=str(self.script))
        self.patch(display.Plugin, _state=self.state, _baseline=250.0, _hdr_baseline=False,
                   _last_written=120.0, _loaded_script_variant=display.MODE_PQ,
                   _gamescope_started_at=100.0, _mode_lock=asyncio.Lock())
        self.stack.enter_context(patch.object(display, '_gamescope_start_time', side_effect=lambda: self.session))

    def patch(self, target, **values):
        for name, value in values.items():
            self.stack.enter_context(patch.object(target, name, value))

    def persisted(self):
        return AtomicSettingsManager('display_settings', str(self.root / 'settings')).settings


class ResetFixture(LegacyHybridFixture):
    """The legacy Hybrid setup with a patched EDID, recording every brightness and atom write."""
    def setUp(self):
        super().setUp()
        self.edid = self.root / 'published-edid.bin'
        self.edid.write_bytes(b'patched-edid')
        self.edid_backup = self.root / 'edid-original.bin'
        self.edid_backup.write_bytes(b'original-edid')
        self.state.update(reset_restart_pending=False, settings_error='')
        self.nits = []
        self.external = 0
        self.patch(display, EDID_BACKUP=str(self.edid_backup))
        self.patch(display.Plugin, _edid_original=b'original-edid', _edid_path=str(self.edid))
        # These fixtures exercise legacy restoration. Native support has its
        # own tests; do not probe or select the actual host's Gamescope here.
        self.stack.enter_context(patch.object(display.Plugin, '_refresh_native_support'))
        self.stack.enter_context(patch.object(display, '_strip_displayid', return_value=b'patched-edid'))
        self.stack.enter_context(patch.object(display, '_published_edid_is_safe', return_value=True))
        self.stack.enter_context(patch.object(display.Plugin, '_session_props',
                                            side_effect=lambda _names, **kwargs: {display.ATOM_IS_EXTERNAL: self.external}))
        self.stack.enter_context(patch.object(display, '_write_nits',
                                            side_effect=lambda value: self.nits.append(value) or True))
        self.stack.enter_context(patch.object(display, '_write_atom_int',
                                            side_effect=lambda name, value: self.atoms.append((name, value)) or True))
        self.plugin = display.Plugin()

    def reset(self):
        return asyncio.run(self.plugin.reset_settings())


class DisplayResetTests(ResetFixture, unittest.TestCase):
    def test_unknown_partial_edid_keeps_recovery_bytes_and_blocks_reset_completion(self):
        self.edid.write_bytes(b'partial-edid')
        info = self.edid.stat()
        pending = dict(schema=1, path=str(self.edid), device=info.st_dev, inode=info.st_ino,
                       partial_sha256=None, original_sha256=hashlib.sha256(b'original-edid').hexdigest())
        self.store.replace({**self.saved, 'edid_write_pending': pending})
        result = self.reset()
        self.assertIn('original display EDID', result['reset_error'])
        self.assertTrue(result['reset_in_progress'])
        self.assertEqual(self.persisted()['edid_write_pending'], pending)
        self.assertEqual(self.edid_backup.read_bytes(), b'original-edid')
        self.assertEqual(self.edid.read_bytes(), b'partial-edid')
        self.assertTrue(self.script.exists())

    def test_invalid_public_modes_never_read_migrate_install_or_activate(self):
        original = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for native in (False, True):
            self.state['native_support_detected'] = native
            for method in (self.plugin.run_setup, self.plugin.set_panel_mode):
                for invalid in ('typo', '', 'PQ', None, False, 0, [], {}, ['pq']):
                    with self.subTest(native=native, method=method.__name__, value=invalid):
                        with self.assertRaisesRegex(ValueError, 'Unknown display mode'):
                            asyncio.run(method(invalid))
                        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, original)
                        self.assertEqual(self.state['panel_mode'], 'hybrid')
                        self.assertEqual(self.atoms, [])
                        self.assertEqual(self.nits, [])

    def test_valid_public_modes_and_default_setup_keep_the_transaction_path(self):
        for method in (self.plugin.run_setup, self.plugin.set_panel_mode):
            for mode in display.MODES:
                with self.subTest(method=method.__name__, mode=mode), \
                     patch.object(display.Plugin, '_read_settings'), \
                     patch.object(display.Plugin, '_apply_mode_locked', new_callable=AsyncMock, return_value={'panel_mode': mode}) as apply:
                    self.assertEqual(asyncio.run(method(mode)), {'panel_mode': mode})
                    apply.assert_awaited_once_with(mode)
        with patch.object(display.Plugin, '_read_settings'), \
             patch.object(display.Plugin, '_apply_mode_locked', new_callable=AsyncMock, return_value={'panel_mode': 'pq'}) as apply:
            self.assertEqual(asyncio.run(self.plugin.run_setup()), {'panel_mode': 'pq'})
            apply.assert_awaited_once_with('pq')

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

    def test_pending_reset_saved_by_2_1_survives_the_first_refresh_after_update(self):
        # 2.1 stamped the session with the epoch mtime of /proc/<pid>; this one began 500 s ago.
        now = display.time.time()
        self.session = display._IDENTITY_BASE + 6.05
        self.state['panel_mode'] = None
        self.stack.enter_context(patch.object(display, '_session_wall_clock', return_value=now - 500))
        self.store.replace({**display.DEFAULT_SETTINGS, 'reset_session': now - 100})
        display.Plugin._refresh_setup()
        self.assertTrue(self.state['reset_restart_pending'])
        self.assertEqual(self.persisted()['reset_session'], now - 100)
        self.store.replace({**display.DEFAULT_SETTINGS, 'reset_session': now - 1000})
        display.Plugin._refresh_setup()
        self.assertFalse(self.state['reset_restart_pending'], 'Gaming Mode restarted after that reset')
        self.assertIsNone(self.persisted()['reset_session'])

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
        replace = self.store.replace
        def final_commit(payload):
            if payload.get('panel_mode') is None:
                raise OSError('final commit failure')
            return replace(payload)
        with patch.object(self.store, 'replace', side_effect=final_commit):
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
        with patch.object(display, '_pick_display', return_value=True), \
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


class DisplayDockCleanupTests(ResetFixture, unittest.TestCase):
    def prepare_cache(self, external=1):
        cached = {display.ATOM_IS_EXTERNAL: 0,
                  display.ATOM_SDR_NITS: display._float_raw(120),
                  display.ATOM_EDID_PATH: f'"{self.edid}"'}
        for name, value in (('_prop_cache', cached), ('_prop_cache_display', display.DISPLAY),
                            ('_prop_cache_ready', True)):
            self.stack.enter_context(patch.object(display, name, value))
        self.stack.enter_context(patch.object(display.Plugin, '_session_props',
                                             new=staticmethod(REAL_SESSION_PROPS)))
        self.stack.enter_context(patch.object(display, '_read_props_uncached',
            return_value={**cached, display.ATOM_IS_EXTERNAL: external,
                          display.ATOM_SDR_NITS: display._float_raw(700)}))

    def test_reset_does_not_restore_internal_baselines_using_a_predock_cache(self):
        self.prepare_cache()
        self.edid.write_bytes(b'new-display-edid')
        result = self.reset()
        self.assertEqual(result['reset_error'], '')
        self.assertEqual(self.nits, [])
        self.assertEqual(self.atoms, [(display.ATOM_FORCE_HDR_SUPPORT, 0)])

    def test_reset_session_change_during_live_read_defers_all_atom_writes(self):
        self.prepare_cache(external=0)
        def moved(names):
            self.session = 200.0
            return {display.ATOM_IS_EXTERNAL: 0}
        with patch.object(display, '_read_props_uncached', side_effect=moved):
            result = self.reset()
        self.assertTrue(result['reset_error'])
        self.assertEqual(self.nits, [])
        self.assertEqual(self.atoms, [])
        self.assertTrue(self.script.exists())

    def test_uninstall_does_not_restore_internal_hdr_using_a_predock_cache(self):
        self.prepare_cache()
        self.state['active_mode'] = display.MODE_PQ
        with patch.object(display.Plugin, '_task', None), patch.object(display.Plugin, '_prop_task', None), \
                patch.object(display.Plugin, '_refresh_native_support'), \
                patch.object(display.Plugin, '_edid_restore'):
            asyncio.run(self.plugin._unload(uninstalling=True))
        self.assertEqual(self.nits, [])
        self.assertEqual(self.atoms, [(display.ATOM_FORCE_HDR_SUPPORT, 0)])

    def uninstall_on_internal_panel(self, **modes):
        self.prepare_cache(external=0)
        self.state.update(modes)
        with patch.object(display.Plugin, '_task', None), patch.object(display.Plugin, '_prop_task', None),                 patch.object(display.Plugin, '_refresh_native_support'),                 patch.object(display.Plugin, '_edid_restore'):
            asyncio.run(self.plugin._unload(uninstalling=True))

    def test_uninstall_leaves_steams_hdr_alone_when_no_hdr_mode_was_chosen(self):
        for mode in (None, display.MODE_G22):
            with self.subTest(mode=mode):
                self.atoms.clear()
                self.uninstall_on_internal_panel(panel_mode=mode, active_mode=mode)
                self.assertEqual(self.atoms, [(display.ATOM_FORCE_HDR_SUPPORT, 0)])

    def test_uninstall_still_takes_the_panel_out_of_pq_it_was_put_in(self):
        self.uninstall_on_internal_panel(panel_mode=display.MODE_PQ, active_mode=display.MODE_PQ)
        self.assertEqual(self.atoms, [(display.ATOM_FORCE_HDR_SUPPORT, 0), (display.ATOM_HDR_ENABLED, 0)])

    def test_disabling_hybrid_goes_straight_to_gamma_without_restoring_pq_first(self):
        import module_control
        display.Plugin._hdr_baseline = True  # Steam HDR was on when Hybrid took over
        self.plugin.cleanup_native_display = AsyncMock(return_value={})
        with patch.object(display.Plugin, '_task', None), patch.object(display.Plugin, '_prop_task', None),                 patch.object(display.Plugin, '_release', return_value=True),                 patch.object(display.Plugin, '_edid_restore', return_value=True):
            asyncio.run(module_control.withdraw('display', self.plugin))
        modes = [value for name, value in self.atoms if name == display.ATOM_HDR_ENABLED]
        self.assertNotIn(1, modes, 'a PQ round trip is two needless modesets')
        self.assertEqual(modes[-1], 0)


class DisplayStopDuringStartTests(ResetFixture, unittest.TestCase):
    """A display start still running past its time limit when the plugin stops applies nothing afterwards."""
    def setUp(self):
        super().setUp()
        import main
        self.main = main
        self.edid.write_bytes(b'original-edid')  # What a completed restoration leaves behind.
        self.gate = threading.Event()  # Holds the start in its first gamescope step.
        self.addCleanup(self.gate.set)
        self.addCleanup(module_runtime.begin_start, 'display')
        self.uninstalled = []
        self.edid_writes = []
        self.patch(main.decky, DECKY_PLUGIN_SETTINGS_DIR=self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.patch(main, _installed_standalone_plugins=Mock(return_value=[]), MODULE_START_LIMIT_S=0.05,
                   STARTING_STOP_WAIT_S=0.1, SHUTDOWN_BUDGET_S=0.3)
        self.patch(display.Plugin, _task=None, _prop_task=None)
        self.patch(display, _pick_display=lambda: self.gate.wait(5), _identify_panel=Mock(return_value=(True, 'fixture')),
                   _find_backlight=Mock(return_value=''), _watch_properties=AsyncMock(),
                   _uninstall_script=lambda: self.uninstalled.append(self.gate.is_set()) or 'removed the display script',
                   # The published EDID carries the DisplayID block the EDID pass strips.
                   _has_displayid=lambda data: data == b'original-edid', _cta_max_luminance=Mock(return_value=600.0),
                   _rewrite_file_if_unchanged=lambda path, old, new, **_: self.edid_writes.append(new) or 'written')
        self.patch(self.plugin, _loop=AsyncMock())

    def test_the_same_start_without_a_stop_patches_the_edid_and_advertises_hdr(self):
        self.gate.set()
        asyncio.run(self.plugin._main())
        self.assertEqual(self.edid_writes, [b'patched-edid'])
        self.assertIn((display.ATOM_FORCE_HDR_SUPPORT, 1), self.atoms)

    def stop_during_start(self, uninstall):
        async def scenario():
            loader = self.main.Plugin()
            for name in self.main.MODULE_NAMES:
                setattr(loader, '_' + name, SimpleNamespace(_main=AsyncMock(), _migration=AsyncMock(),
                                                            _unload=AsyncMock(), _uninstall=AsyncMock()))
            loader._display = self.plugin
            loader._start_desktop = AsyncMock()
            loader._publish_modules = AsyncMock()
            await asyncio.wait_for(loader._main(), 2)
            self.assertTrue(loader._module_status()['display']['starting'])
            began = time.monotonic()
            await loader._unload()
            if uninstall:
                await loader._uninstall()
            self.assertLess(time.monotonic() - began, 1)
            self.assertEqual(self.uninstalled, [])  # Never alongside the running start.
            self.gate.set()
            await asyncio.wait_for(loader._late_stops['display'], 5)
            async def uninstalled():
                while uninstall and not self.uninstalled:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(uninstalled(), 5)
        asyncio.run(scenario())
        self.assertEqual(self.edid_writes, [])
        self.assertNotIn((display.ATOM_FORCE_HDR_SUPPORT, 1), self.atoms)
        self.assertIsNone(display.Plugin._task)
        self.assertIsNone(display.Plugin._prop_task)

    def test_unload_during_a_hung_start_leaves_the_edid_and_atoms_restored(self):
        self.stop_during_start(uninstall=False)
        self.assertEqual(self.uninstalled, [])

    def test_uninstall_during_a_hung_start_removes_the_script_only_after_the_start(self):
        self.stop_during_start(uninstall=True)
        self.assertEqual(self.uninstalled, [True])


class NativeDisplayMigrationTests(LegacyHybridFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.state.update(enabled=False, edid_fix=True, hdr_now=True, native_support_reason='',
                          native_cleanup_error='')
        self.detection = dict(status='supported', supported=True,
                              reason='system_profile_present')
        self.props = {display.ATOM_IS_EXTERNAL: 0,
                      display.ATOM_SDR_NITS: display._float_raw(120.0),
                      display.ATOM_HDR_ENABLED: 1, display.ATOM_FORCE_HDR_SUPPORT: 1}
        self.patch(display, _gamescope_pid=123)
        self.patch(display.Plugin, _native_probe_after=0.0, _native_probe_session=None,
                   _native_detection={}, _edid_recheck_until=0.0,
                   _hybrid_pq_hold_until=0.0, _hybrid_sdr_since=None,
                   _hdr_fights=0, _hdr_fight_target=None, _hdr_fight_at=0.0, _hdr_backoff_until=0.0,
                   _task=None, _prop_task=None)
        self.stack.enter_context(patch.object(display, '_loaded_script_variant', return_value=display.MODE_PQ))
        self.detector = self.stack.enter_context(patch.object(display.display_native,
            'detect_native_display_support', side_effect=lambda: dict(self.detection)))
        self.stack.enter_context(patch.object(display.Plugin, '_session_props', side_effect=lambda _names, **kwargs: dict(self.props)))
        self.stack.enter_context(patch.object(display, '_read_props_uncached', side_effect=lambda _names: dict(self.props)))
        self.stack.enter_context(patch.object(display, '_write_atom_int', side_effect=self.write_atom))
        self.plugin = display.Plugin()

    def write_atom(self, name, value):
        # Every destructive side effect must have a durable recovery journal.
        self.assertIsNotNone(self.persisted()['native_cleanup'])
        self.atoms.append((name, value))
        self.props[name] = value
        return True

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
        with patch.object(display.Plugin, '_edid_restore', return_value=True):
            asyncio.run(self.plugin._uninstall())
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


class DisplayDiscoveryTests(unittest.TestCase):
    def test_loop_recovers_nodes_that_appear_after_start_without_steady_scans(self):
        with tempfile.TemporaryDirectory() as raw:
            node = Path(raw) / "amdgpu_bl1"
            node.mkdir()
            (node / "brightness").write_text("200000")
            (node / "max_brightness").write_text("471000")
            now = [2.0]
            scans, writes = [], []
            state = dict(display.Plugin._state, panel_ok=False,
                         panel_desc="no internal panel EDID", backlight="",
                         active_mode=display.MODE_PQ)

            async def offload(fn, *args):
                return fn(*args)

            def advance(seconds):
                now[0] += max(0.01, seconds)
                if now[0] >= 34:
                    raise asyncio.CancelledError()

            async def sleep(seconds):
                advance(seconds)

            def discover():
                scans.append(now[0])
                return str(node) if now[0] >= 12 else ""

            with patch.object(display.Plugin, "_state", state), \
                 patch.object(display.Plugin, "_last_written", None), \
                 patch.object(display, "_offload", offload), \
                 patch.object(display.time, "monotonic", side_effect=lambda: now[0]), \
                 patch.object(display.asyncio, "sleep", sleep), \
                 patch.object(display, "_identify_panel", side_effect=lambda: (now[0] >= 12, "panel")), \
                 patch.object(display, "_find_backlight", side_effect=discover), \
                 patch.object(display, "_open_notify", return_value=None), \
                 patch.object(display, "_wait_for_change", side_effect=lambda fd, timeout: advance(min(timeout, .25))), \
                 patch.object(display.Plugin, "_refresh_setup"), \
                 patch.object(display.Plugin, "_refresh_gate", side_effect=lambda: state["panel_ok"]), \
                 patch.object(display.Plugin, "_edid_pass"), \
                 patch.object(display.Plugin, "_forward_nits", side_effect=lambda value: writes.append(value)):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(display.Plugin()._loop(""))
            self.assertTrue(scans)
            self.assertLessEqual(scans[0], 4, "initial missing hardware should be probed promptly")
            recovered = [at for at in scans if at >= 12]
            self.assertTrue(recovered, "late panel discovery must remain retryable")
            self.assertLessEqual(recovered[0], 22, "available hardware must be found within a bounded retry")
            self.assertLessEqual(len(scans), 5, "missing hardware should not trigger continuous scans")
            self.assertFalse(any(at > recovered[0] for at in scans),
                             "a healthy panel must leave the discovery retry loop")
            self.assertTrue(writes, "brightness must recover without restarting Decky")
            self.assertEqual(state["backlight"], "amdgpu_bl1")
            self.assertEqual(state["max_nits"], 471.0)

    def test_desktop_mode_backs_off_xprop_and_probes_at_once_when_gamescope_returns(self):
        now, spawns, gamescope = [0.0], [], [None]

        def run(argv, **_kw):  # KWin's Xwayland: an X server without GAMESCOPE atoms
            spawns.append(argv)
            return subprocess.CompletedProcess(argv, 0, "_NET_SUPPORTED(ATOM) = x\n", "")

        async def offload(fn, *args):
            return fn(*args)

        async def sleep(seconds):
            now[0] += seconds
            if now[0] >= 600:
                raise asyncio.CancelledError()

        with patch.object(display.Plugin, "_state", dict(display.Plugin._state)), \
             patch.object(display.Plugin, "_session_retry_after", 0.0), \
             patch.object(display.Plugin, "_session_retry_delay", display.GATE_INTERVAL_S), \
             patch.object(display, "_display_candidates", return_value=(":0",)), \
             patch.object(display, "_gamescope_start_time", side_effect=lambda: gamescope[0]), \
             patch.object(display, "_xprop_command", side_effect=lambda args, *_: ["xprop", *args]), \
             patch.object(display, "_xprop_env", return_value={}), \
             patch.object(display, "_xprop_user", return_value={}), \
             patch.object(display.subprocess, "run", side_effect=run), \
             patch.object(display.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(display.asyncio, "sleep", new=sleep), \
             patch.object(display, "_offload", new=offload):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(display._watch_properties())
            for tick in range(300):  # the loop's gate and EDID pass, every 2 s for 10 minutes
                now[0] = tick * display.GATE_INTERVAL_S
                display.Plugin._session_props([display.ATOM_IS_EXTERNAL])
                display.Plugin._session_props([])
            self.assertLess(len(spawns), 100)  # was ~1200: two setuid forks a second
            spawns.clear()
            gamescope[0] = display._IDENTITY_BASE  # Gaming Mode is back
            display.Plugin._session_props([display.ATOM_IS_EXTERNAL])
            self.assertTrue(spawns)

    def test_session_identity_is_the_process_start_not_the_proc_inode_mtime(self):
        def stat_line(ticks):
            # comm may hold spaces and parentheses; starttime is field 22.
            return f"42 (gamescope (wl) S 1 42 42 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {ticks} 0 0"
        files = {"/proc/42/comm": "gamescope-wl\n", "/proc/42/stat": stat_line(1000),
                 "/proc/sys/kernel/random/boot_id": "0a1b2c3d-1111-2222-3333-444455556666\n"}
        def fake_open(path, *args, **kwargs):
            if path not in files:
                raise FileNotFoundError(path)
            return io.StringIO(files[path])
        def no_mtime(path, *args, **kwargs):
            raise AssertionError(f"identity must not use stat({path}) mtime")
        with patch.object(display, "_gamescope_pid", None), patch.object(display, "_gamescope_scan_after", 0.0),              patch.object(display, "_CLK_TCK", 100), patch.object(display, "open", fake_open, create=True),              patch.object(display.os, "listdir", return_value=["1", "42"]), patch.object(display.os, "stat", no_mtime):
            first = display._gamescope_start_time()
            self.assertEqual(display._gamescope_start_time(), first)
            files["/proc/sys/kernel/random/boot_id"] = "ffffffff-1111-2222-3333-444455556666\n"
            other_boot = display._gamescope_start_time()
            files["/proc/42/stat"] = stat_line(1500)
            restarted = display._gamescope_start_time()
        self.assertFalse(display.Plugin._same_session(first, other_boot))
        self.assertFalse(display.Plugin._same_session(other_boot, restarted))
        self.assertAlmostEqual(restarted - other_boot, 5.0)

    def _enter(self, context):  # TestCase.enterContext needs Python 3.11
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def _fake_session_proc(self, boot_id, ticks=605, uptime=1000.0):
        """gamescope-wl started `ticks`/100 s after a boot `uptime` seconds ago."""
        files = {"/proc/2021/comm": "gamescope-wl\n",
                 "/proc/2021/stat": f"2021 (gamescope-wl) S 1 2021 2021 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {ticks} 0 0",
                 "/proc/sys/kernel/random/boot_id": f"{boot_id}-1111-2222-3333-444455556666\n",
                 "/proc/uptime": f"{uptime} 0.0\n"}
        real_open = open
        def fake_open(path, *args, **kwargs):
            return io.StringIO(files[path]) if path in files else real_open(path, *args, **kwargs)
        for target, value in (("_gamescope_pid", 2021), ("_CLK_TCK", 100), ("open", fake_open)):
            self._enter(patch.object(display, target, value, create=True))
        return files

    def test_script_mtime_is_compared_with_the_session_wall_clock_start(self):
        # Live console values: boot_id prefix 5e474f, gamescope-wl started at 605 ticks.
        root = Path(self._enter(tempfile.TemporaryDirectory()))
        script = root / "lenovo.legiongo2.oled.lua"
        script.write_bytes(Path(display.BUNDLED_SCRIPTS[display.MODE_PQ]).read_bytes())
        self._enter(patch.object(display, "INSTALLED_SCRIPT", str(script)))
        files = self._fake_session_proc("5e474f00")
        self.assertIsNone(display._loaded_script_variant(),
                          "a script written after the session started cannot be the loaded one")
        old = display.time.time() - 5000  # before this boot
        os.utime(script, (old, old))
        self.assertEqual(display._loaded_script_variant(), display.MODE_PQ)
        # A boot-id prefix that is small as a number must not make old files look newer.
        files["/proc/sys/kernel/random/boot_id"] = "00012300-1111-2222-3333-444455556666\n"
        self.assertEqual(display._loaded_script_variant(), display.MODE_PQ)

    def test_restart_fallback_compares_the_script_with_the_session_wall_clock_start(self):
        root = Path(self._enter(tempfile.TemporaryDirectory()))
        script, bundled = root / "installed.lua", root / "dev-build.lua"
        script.write_bytes(b"-- not in the production variant table")
        bundled.write_bytes(script.read_bytes())
        self._enter(patch.object(display, "INSTALLED_SCRIPT", str(script)))
        self._enter(patch.object(display, "BUNDLED_SCRIPT", str(bundled)))
        self._fake_session_proc("5e474f00")
        self.assertTrue(display._restart_needed(), "a script written after the session needs a restart")
        old = display.time.time() - 5000
        os.utime(script, (old, old))
        self.assertFalse(display._restart_needed())

    def test_session_stamps_saved_by_2_1_still_match_the_running_session(self):
        # 2.1 saved the epoch mtime of /proc/<pid>; the session below began ~994 s ago.
        self._fake_session_proc("5e474f00")
        current = display._gamescope_start_time()
        now = display.time.time()
        self.assertTrue(display.Plugin._same_session(now - 900, current))
        self.assertFalse(display.Plugin._same_session(now - 2000, current), "a stamp from an earlier session")
        self.assertTrue(display.Plugin._same_session(current, current))
        self.assertFalse(display.Plugin._same_session(current - 1.0, current))

    def test_failed_notification_read_closes_the_descriptor(self):
        with patch.object(display.os, "open", return_value=73), \
             patch.object(display.os, "read", side_effect=OSError("device disappeared")), \
             patch.object(display.os, "close") as close:
            self.assertIsNone(display._open_notify("/test/backlight"))
        close.assert_called_once_with(73)

    def test_lost_settings_block_startup_mutations_and_default_commits(self):
        with tempfile.TemporaryDirectory() as raw:
            primary = Path(raw) / "display_settings.json"
            backup = Path(str(primary) + ".bak")
            primary.write_text("{broken primary")
            backup.write_text("{broken backup")
            manager = AtomicSettingsManager("display_settings", raw)
            before = (primary.read_bytes(), backup.read_bytes())
            self.assertTrue(manager.recovery_error)
            state = dict(display.Plugin._state)
            plugin = display.Plugin()
            with patch.object(display, "settings", manager), \
                 patch.object(display.Plugin, "_state", state), \
                 patch.object(display.Plugin, "_task", None), \
                 patch.object(display.Plugin, "_prop_task", None), \
                 patch.object(display, "_pick_display") as discover, \
                 patch.object(display.Plugin, "_refresh_setup") as setup, \
                 patch.object(display, "_write_atom_int") as write_atom, \
                 patch.object(display, "_write_nits") as write_nits, \
                 patch.object(display, "_install_script") as install, \
                 patch.object(display.Plugin, "_edid_pass") as edid:
                with self.assertRaises(CorruptSettings):
                    asyncio.run(plugin._main())
                self.assertIsNone(plugin._task)
                self.assertIsNone(plugin._prop_task)
                public = asyncio.run(plugin.get_state())
                self.assertIn("could not be recovered", public["settings_error"])
                with self.assertRaises(CorruptSettings):
                    display.Plugin._store_settings({"brightness_baseline": 100.0})
                for operation in (lambda: plugin.set_enabled(True),
                                  lambda: plugin.set_edid_fix(True),
                                  lambda: plugin.set_panel_mode("pq"),
                                  lambda: plugin._unload(uninstalling=True)):
                    with self.assertRaises(CorruptSettings):
                        asyncio.run(operation())
                for operation in (discover, setup, write_atom, write_nits, install, edid):
                    operation.assert_not_called()
            self.assertEqual((primary.read_bytes(), backup.read_bytes()), before)


class NativeDisplayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.script = Path(directory.name) / "native.lua"
        patched = patch.object(native, "SYSTEM_SCRIPT", str(self.script))
        patched.start()
        self.addCleanup(patched.stop)

    def test_any_file_contents_indicate_support_without_reading_or_executing(self):
        for contents in (b"", b"-- Future native PQ support, no workaround flags",
                         b"content_driven = false", b"\xff\x00"):
            with self.subTest(contents=contents):
                self.script.write_bytes(contents)
                with patch("builtins.open", side_effect=AssertionError("No content reads")):
                    result = native.detect_native_display_support()
                self.assertTrue(result["supported"])
                self.assertEqual(result["status"], "supported")
                self.assertEqual(result["script_path"], str(self.script))

    def test_profile_appearance_and_removal_are_observed(self):
        self.assertEqual(native.detect_native_display_support()["status"], "absent")
        self.script.touch()
        self.assertTrue(native.detect_native_display_support()["supported"])
        self.script.unlink()
        self.assertEqual(native.detect_native_display_support()["status"], "absent")

    def test_directory_is_not_a_profile_file(self):
        self.script.mkdir()
        result = native.detect_native_display_support()
        self.assertFalse(result["supported"])
        self.assertEqual(result["status"], "inconclusive")

    def test_access_error_does_not_report_profile_removed(self):
        for error in (PermissionError, OSError):
            with self.subTest(error=error), patch.object(native.os, "stat", side_effect=error):
                result = native.detect_native_display_support()
                self.assertFalse(result["supported"])
                self.assertEqual(result["status"], "inconclusive")


class BacklightWatchShutdownTests(unittest.TestCase):
    def test_cancelled_wait_keeps_its_descriptor_until_the_waiting_thread_returns(self):
        import asyncio, os, tempfile, threading
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        entered, release, seen = threading.Event(), threading.Event(), []

        def wait(fd, _timeout):
            entered.set()
            release.wait(5)
            try:
                os.fstat(fd)
                seen.append('open')
            except OSError as exc:
                seen.append(exc.errno)

        async def run(directory):
            task = asyncio.create_task(display.Plugin()._loop(directory))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            await display.module_runtime.drain('display')
            for _ in range(3):
                await asyncio.sleep(0)

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            open(os.path.join(directory, 'brightness'), 'w').close()
            state = dict(display.Plugin._state, panel_ok=True, active_mode=display.MODE_PQ)
            stack.enter_context(patch.object(display.Plugin, '_state', state))
            stack.enter_context(patch.object(display.Plugin, '_edid_recheck_until', 0.0))
            stack.enter_context(patch.object(display, '_open_notify', return_value=read_fd))
            for name in ('_refresh_setup', '_edid_pass'):
                stack.enter_context(patch.object(display.Plugin, name))
            stack.enter_context(patch.object(display.Plugin, '_refresh_gate', return_value=False))
            stack.enter_context(patch.object(display.Plugin, 'native_display_active', return_value=False))
            stack.enter_context(patch.object(display, '_wait_for_change', side_effect=wait))
            asyncio.run(run(directory))
        self.assertEqual(seen, ['open'])
        with self.assertRaises(OSError):
            os.fstat(read_fd)


if __name__ == '__main__':
    unittest.main()

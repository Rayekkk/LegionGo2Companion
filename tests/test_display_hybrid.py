# SPDX-License-Identifier: BSD-3-Clause
"""Hybrid PQ / gamma 2.2 rules, HDR feedback, brightness forwarding and session-safe release."""
import asyncio
from contextlib import ExitStack
import copy
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import call, patch

import support  # Decky/Linux doubles before importing the backend.
import display_backend as display
import safe_settings
from safe_settings import AtomicSettingsManager


REAL_SESSION_PROPS = display.Plugin._session_props


class DisplayHdrFeedbackTests(unittest.TestCase):
    def setUp(self):
        fixture = DisplayBaselineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.state = fixture.state
        self.props = dict(fixture.props)
        self.props.pop(display.ATOM_HDR_APP)
        self.missing = 'not found.'   # xprop's reply for an atom nobody published
        self.returncode = 0
        self.error = None
        self.hdr_line = None
        stack = fixture.stack
        display.Plugin._baseline = 250.0
        stack.enter_context(patch.object(display.Plugin, '_session_props', new=staticmethod(REAL_SESSION_PROPS)))
        stack.enter_context(patch.object(display, '_prop_cache_ready', False))
        stack.enter_context(patch.object(display.Plugin, '_session_retry_after', 0.0))
        stack.enter_context(patch.object(display.Plugin, '_edid_path', None))
        stack.enter_context(patch.object(display, '_pick_display', return_value=False))
        stack.enter_context(patch.object(display, '_xprop_command', side_effect=lambda args, *unused: ['xprop'] + args))
        stack.enter_context(patch.object(display, '_xprop_env', return_value={}))
        stack.enter_context(patch.object(display.subprocess, 'run', side_effect=self.xprop))
        self.nits = stack.enter_context(patch.object(display, '_write_nits', return_value=True))
        self.atoms = stack.enter_context(patch.object(display, '_write_atom_int', return_value=True))

    def xprop(self, argv, **kwargs):
        if self.error:
            raise self.error
        lines = []
        for name in argv[2:]:
            if name == display.ATOM_HDR_APP and self.hdr_line is not None:
                lines.append(self.hdr_line)
            elif name in self.props:
                lines.append(f'{name}(CARDINAL) = {self.props[name]}')
            elif self.missing is not None:
                lines.append(f'{name}:  {self.missing}')
        return subprocess.CompletedProcess(argv, self.returncode, '\n'.join(lines), '')

    def forward(self):
        self.state.update(panel_mode='pq', active_mode='pq', active=True)
        return display.Plugin._forward_nits(14.13)

    def assert_no_new_brightness(self):
        # A refusal may hand back the previously owned baseline.
        self.assertNotIn(call(14.13), self.nits.call_args_list)

    def hybrid(self):
        self.state.update(panel_mode='hybrid', active_mode='hybrid')
        display.Plugin._hdr_baseline = False
        display.Plugin._hybrid_sdr_since = 1.0
        self.props[display.ATOM_FORCE_HDR_SUPPORT] = 1
        with patch.object(display.time, 'monotonic', return_value=100.0):
            display.Plugin._hybrid_pass_locked()

    def test_missing_hdr_app_is_sdr_for_pq_brightness(self):
        for message in ('not found.', 'no such atom on any window.'):
            with self.subTest(message=message):
                self.missing = message
                self.assertTrue(self.forward())
                self.nits.assert_called_with(14.13)

    def test_missing_hdr_app_is_sdr_for_hybrid(self):
        self.hybrid()
        self.atoms.assert_called_once_with(display.ATOM_HDR_ENABLED, 0)

    def test_explicit_hdr_content_still_withdraws_writer_and_keeps_hybrid_pq(self):
        self.props[display.ATOM_HDR_APP] = 1
        self.assertIsNone(self.forward())
        self.assert_no_new_brightness()
        self.hybrid()
        self.atoms.assert_not_called()

    def test_failed_xprop_never_authorizes_writer(self):
        # Even an explicit 0 is untrustworthy when xprop failed after printing it.
        for explicit in (0, None):
            with self.subTest(explicit=explicit):
                if explicit is not None:
                    self.props[display.ATOM_HDR_APP] = explicit
                self.returncode = 1
                self.assertIsNone(self.forward())
                self.hybrid()
                self.nits.assert_not_called()
                self.atoms.assert_not_called()

    def test_timeout_and_os_error_do_not_become_sdr(self):
        for error in (subprocess.TimeoutExpired('xprop', 3), OSError('unavailable')):
            with self.subTest(error=type(error).__name__):
                self.error = error
                self.assertIsNone(self.forward())
                self.nits.assert_not_called()

    def test_missing_line_without_explicit_absence_is_unknown(self):
        self.missing = None
        self.assertIsNone(self.forward())
        self.hybrid()
        self.assert_no_new_brightness()
        self.atoms.assert_not_called()

    def test_malformed_feedback_does_not_become_sdr(self):
        for line in (f'{display.ATOM_HDR_APP}(CARDINAL) = invalid',
                     f'{display.ATOM_HDR_APP}: unexpected not found text'):
            with self.subTest(line=line):
                self.hdr_line = line
                self.assertIsNone(self.forward())
                self.assert_no_new_brightness()

    def test_absence_is_not_sdr_on_external_or_unidentified_root(self):
        for external in (1, None):
            with self.subTest(external=external):
                self.props[display.ATOM_IS_EXTERNAL] = external
                self.assertIsNone(self.forward())
                self.assert_no_new_brightness()


class DisplayBaselineTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.store = AtomicSettingsManager('display_settings', self.folder)
        self.store.replace({**display.DEFAULT_SETTINGS, 'panel_mode': 'pq', 'active_mode': 'pq'})
        self.state = dict(display.Plugin._state, active_mode='pq', panel_mode='pq',
            setup_done=True, enabled=True, active=True, panel_ok=True,
            reset_in_progress=False, native_support_detected=False)
        self.props = {display.ATOM_IS_EXTERNAL: 0, display.ATOM_SDR_NITS: display._float_raw(250),
            display.ATOM_HDR_OUT: 1, display.ATOM_HDR_APP: 0,
            display.ATOM_FORCE_HDR_SUPPORT: 0, display.ATOM_HDR_ENABLED: 1}
        self.stack.enter_context(patch.object(display, 'settings', self.store))
        self.stack.enter_context(patch.object(display.Plugin, '_state', self.state))
        self.stack.enter_context(patch.object(display.Plugin, '_baseline', None))
        self.stack.enter_context(patch.object(display.Plugin, '_hdr_baseline', None))
        self.stack.enter_context(patch.object(display.Plugin, '_last_written', None))
        self.stack.enter_context(patch.object(display.Plugin, '_gamescope_started_at', 100.0))
        self.stack.enter_context(patch.object(display.Plugin, '_loaded_script_variant', display.MODE_PQ))
        self.stack.enter_context(patch.object(display.Plugin, '_hybrid_suspended_external', False))
        self.stack.enter_context(patch.object(display.Plugin, '_hybrid_pq_hold_until', 0.0))
        self.stack.enter_context(patch.object(display.Plugin, '_hybrid_sdr_since', None))
        self.stack.enter_context(patch.object(display.Plugin, '_hdr_fights', 0))
        self.stack.enter_context(patch.object(display.Plugin, '_hdr_fight_target', None))
        self.stack.enter_context(patch.object(display.Plugin, '_hdr_fight_at', 0.0))
        self.stack.enter_context(patch.object(display.Plugin, '_hdr_backoff_until', 0.0))
        self.stack.enter_context(patch.object(display.Plugin, 'native_display_active', return_value=False))
        self.stack.enter_context(patch.object(display.Plugin, '_session_props', side_effect=lambda names, **kwargs: dict(self.props)))
        self.stack.enter_context(patch.object(display, '_gamescope_start_time', return_value=100.0))
        self.stack.enter_context(patch.object(display, '_loaded_script_variant', return_value=display.MODE_PQ))

    def persisted(self):
        return AtomicSettingsManager('display_settings', self.folder).settings

    def fail_primary(self):
        real_write = safe_settings._atomic_write_bytes
        def write(path, data):
            if Path(path) == Path(self.store.path):
                raise OSError('disk full')
            return real_write(path, data)
        return patch.object(safe_settings, '_atomic_write_bytes', side_effect=write)

    def test_brightness_commit_failure_blocks_writes_then_retry_and_reload_restore_original(self):
        writes = []
        def write(value):
            saved = self.persisted()
            self.assertEqual(saved['brightness_baseline'], 250.0,
                             'Baseline must already be durable before an atom changes')
            self.assertEqual(saved['brightness_baseline_session'], 100.0)
            writes.append(value)
            self.props[display.ATOM_SDR_NITS] = display._float_raw(value)
            return True
        with patch.object(display, '_write_nits', side_effect=write) as hardware:
            with self.fail_primary(), self.assertRaisesRegex(RuntimeError, 'recovery state.*disk full'):
                display.Plugin._refresh_gate()
            self.assertFalse(self.state['active'])
            self.assertIsNone(display.Plugin._forward_nits(120.0))
            # Even a stale gate which sets active again must not authorize a
            # write until the missing durable baseline has been captured.
            self.state['active'] = True
            self.assertIsNone(display.Plugin._forward_nits(120.0))
            hardware.assert_not_called()
            self.assertIsNone(display.Plugin._baseline)
            self.assertIsNone(self.store.getSetting('brightness_baseline'))
            self.assertIsNone(self.persisted()['brightness_baseline'])
            self.assertTrue(display.Plugin._refresh_gate())
            self.state['active'] = True
            self.assertTrue(display.Plugin._forward_nits(120.0))
            # A fresh Decky process reloads the same compositor's baseline from
            # the settings, rather than learning its own forwarded value.
            display.Plugin._baseline = None
            display.Plugin._last_written = None
            display.Plugin._gamescope_started_at = None
            display.Plugin._sync_loaded_script()
            self.assertEqual(display.Plugin._baseline, 250.0)
            self.assertTrue(display.Plugin._refresh_gate())
            self.state['active'] = True
            self.assertTrue(display.Plugin._forward_nits(120.0))
            self.assertTrue(display.Plugin._release())
        self.assertEqual(writes, [120.0, 120.0, 250.0])
        self.assertEqual(display._as_float(self.props[display.ATOM_SDR_NITS]), 250.0)

    def test_gate_reasons_and_when_brightness_is_released(self):
        display.Plugin._baseline = 250.0
        cases = (
            ({}, {}, True, 'forwarding slider to gamescope', None, False),
            ({'reset_in_progress': True}, {}, False, 'start', None, False),
            ({'setup_done': False}, {}, False, 'waiting for the display script', '_release', False),
            ({'active_mode': 'bogus'}, {}, False, 'waiting for gamescope to load a display mode',
             '_release_locked', False),
            ({'active_mode': display.MODE_G22}, {}, False,
             'gamma 2.2 mode - the system slider drives the panel directly', '_release', False),
            ({'active_mode': display.MODE_G22}, {display.ATOM_FORCE_HDR_SUPPORT: 1}, False,
             'gamma 2.2 mode - the system slider drives the panel directly', '_release', True),
            ({'active_mode': display.MODE_G22, 'fail_write': True}, {display.ATOM_FORCE_HDR_SUPPORT: 1},
             False, 'could not clear stale hybrid HDR support', None, True),
            ({'active_mode': display.MODE_HYBRID}, {display.ATOM_FORCE_HDR_SUPPORT: 1}, False,
             'hybrid mode - the panel switches itself, nothing to forward', '_release', False),
            ({'panel_ok': False, 'enabled': False}, {}, False, 'display not on the affected list',
             '_release', False),
            ({'enabled': False}, {}, False, 'turned off', '_release', False),
            ({'empty_props': True}, {}, False, 'gamescope not reachable', None, False),
            ({}, {display.ATOM_HDR_APP: 1}, False, 'HDR content on screen, Steam handles it',
             '_release', False),
        )
        for state, props, allowed, reason, release, wrote in cases:
            with self.subTest(state=state, props=props):
                state = dict(state)
                fail_write = state.pop('fail_write', False)
                empty = state.pop('empty_props', False)
                self.state.update({'setup_done': True, 'enabled': True, 'panel_ok': True,
                                   'reset_in_progress': False, 'active_mode': 'pq', 'reason': 'start',
                                   **state})
                with patch.object(display.Plugin, '_session_props',
                                  return_value={} if empty else dict(self.props, **props)), \
                     patch.object(display.Plugin, '_session_note', ''), \
                     patch.object(display.Plugin, '_edid_recheck_until', 0.0), \
                     patch.object(display.Plugin, '_release') as plain, \
                     patch.object(display.Plugin, '_release_locked') as locked, \
                     patch.object(display, '_write_atom_int', return_value=not fail_write) as atom:
                    self.assertIs(display.Plugin._refresh_gate_locked(), allowed)
                self.assertEqual(self.state['reason'], reason)
                # Whether the gate hands brightness back, not which internal entry point does it.
                self.assertEqual(plain.called or locked.called, bool(release))
                self.assertEqual(atom.called, wrote)

    def test_hdr_commit_failure_blocks_force_and_mode_writes_then_retry_has_durable_baseline(self):
        self.store.replace({**display.DEFAULT_SETTINGS, 'panel_mode': 'hybrid', 'active_mode': 'hybrid'})
        self.state.update(active_mode='hybrid', panel_mode='hybrid')
        self.props.update({display.ATOM_HDR_APP: 1, display.ATOM_HDR_ENABLED: 0, display.ATOM_HDR_OUT: 0})
        writes = []
        def write(name, value):
            saved = self.persisted()
            self.assertIs(saved['hdr_baseline'], False)
            self.assertEqual(saved['hdr_baseline_session'], 100.0)
            writes.append((name, value))
            self.props[name] = value
            return True
        with patch.object(display, '_write_atom_int', side_effect=write) as hardware:
            with self.fail_primary(), self.assertRaisesRegex(RuntimeError, 'recovery state.*disk full'):
                display.Plugin._hybrid_pass()
            hardware.assert_not_called()
            self.assertIsNone(display.Plugin._hdr_baseline)
            self.assertIsNone(self.store.getSetting('hdr_baseline'))
            self.assertIsNone(self.persisted()['hdr_baseline'])
            display.Plugin._hybrid_pass()
            self.assertEqual(writes, [(display.ATOM_FORCE_HDR_SUPPORT, 1), (display.ATOM_HDR_ENABLED, 1)])
            display.Plugin._hdr_baseline = None
            display.Plugin._gamescope_started_at = None
            display.Plugin._sync_loaded_script()
            self.assertIs(display.Plugin._hdr_baseline, False)
            self.assertTrue(display.Plugin._hybrid_release())
        self.assertEqual(writes[-2:], [(display.ATOM_FORCE_HDR_SUPPORT, 0), (display.ATOM_HDR_ENABLED, 0)])
        self.assertIsNone(self.persisted()['hdr_baseline'])

    def test_advisory_metadata_failure_remains_best_effort_without_retaining_rejected_settings(self):
        before = copy.deepcopy(self.store.settings)
        with self.fail_primary():
            display.Plugin._store_settings({'active_mode': 'hybrid'})
        self.assertEqual(self.store.settings, before)
        self.assertEqual(self.persisted(), before)


class DisplaySessionTests(unittest.TestCase):
    def setUp(self):
        DisplayBaselineTests.setUp(self)
        self.stack.enter_context(patch.object(display.Plugin, '_task', None))
        self.stack.enter_context(patch.object(display.Plugin, '_prop_task', None))

    def test_disable_after_compositor_restart_does_not_restore_old_brightness(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        with patch.object(display, '_gamescope_start_time', return_value=200.0), \
                patch.object(display.Plugin, '_refresh_native_support'), \
                patch.object(display, '_write_nits') as writes:
            result = asyncio.run(display.Plugin().set_enabled(False))
        writes.assert_not_called()
        self.assertFalse(result['active'])
        self.assertEqual(result['reason'], 'turned off')
        self.assertIsNone(display.Plugin._baseline)

    def test_unload_after_restart_does_not_restore_old_hdr_atoms(self):
        self.state.update(active_mode='hybrid')
        display.Plugin._hdr_baseline = False
        with patch.object(display, '_gamescope_start_time', return_value=200.0), \
                patch.object(display.Plugin, '_read_settings'), \
                patch.object(display.Plugin, '_refresh_native_support'), \
                patch.object(display.Plugin, '_edid_restore'), \
                patch.object(display, '_write_atom_int') as writes, \
                patch.object(display, '_write_nits') as nits:
            asyncio.run(display.Plugin()._unload())
        writes.assert_not_called()
        nits.assert_not_called()
        self.assertIsNone(display.Plugin._hdr_baseline)

    def test_unknown_session_defers_release_and_same_session_can_retry(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        display.Plugin._hdr_baseline = False
        self.props[display.ATOM_SDR_NITS] = display._float_raw(120.0)
        with patch.object(display, '_gamescope_start_time', return_value=None), \
                patch.object(display, '_write_nits') as nits, \
                patch.object(display, '_write_atom_int') as atoms:
            self.assertFalse(display.Plugin._release())
            self.assertFalse(display.Plugin._hybrid_release())
        nits.assert_not_called()
        atoms.assert_not_called()
        self.assertEqual(display.Plugin._baseline, 250.0)
        self.assertIs(display.Plugin._hdr_baseline, False)
        with patch.object(display, '_write_nits', return_value=True) as nits:
            self.assertTrue(display.Plugin._release())
        nits.assert_called_once_with(250.0)

    def test_stale_hybrid_pass_cannot_write_before_the_new_session_is_adopted(self):
        self.state.update(active_mode='hybrid', panel_mode='hybrid')
        display.Plugin._hdr_baseline = False
        with patch.object(display, '_gamescope_start_time', return_value=200.0), \
                patch.object(display, '_write_atom_int') as writes:
            display.Plugin._hybrid_pass()
        writes.assert_not_called()
        self.assertIsNone(display.Plugin._hdr_baseline)

    def test_new_session_recovers_forwarding_and_restores_its_own_fresh_baseline(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        self.props[display.ATOM_SDR_NITS] = display._float_raw(700.0)
        def write(value):
            self.props[display.ATOM_SDR_NITS] = display._float_raw(value)
            return True
        with patch.object(display, '_gamescope_start_time', return_value=200.0), \
                patch.object(display, '_write_nits', side_effect=write) as writes:
            self.assertIsNone(display.Plugin._forward_nits(180.0), 'a stale gate never reaches a new compositor')
            self.assertFalse(self.state['active'])
            display.Plugin._sync_loaded_script()
            self.assertTrue(display.Plugin._refresh_gate())
            self.state['active'] = True  # The worker admits the fresh gate.
            self.assertTrue(display.Plugin._forward_nits(180.0))
            self.assertTrue(display.Plugin._release())
        self.assertEqual([call.args[0] for call in writes.call_args_list], [180.0, 700.0])
        self.assertEqual(self.store.getSetting('brightness_baseline_session'), 200.0)
        self.assertEqual(self.store.getSetting('brightness_baseline'), 700.0)


class DisplayReleaseTests(unittest.TestCase):
    setUp = DisplayBaselineTests.setUp

    def test_public_disable_preserves_new_external_brightness(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        self.props.update({display.ATOM_IS_EXTERNAL: 1, display.ATOM_SDR_NITS: display._float_raw(700)})
        with patch.object(display.Plugin, '_refresh_native_support'), \
                patch.object(display, '_write_nits') as writing:
            result = asyncio.run(display.Plugin().set_enabled(False))
        writing.assert_not_called()
        self.assertFalse(result['active'])
        self.assertEqual(display._as_float(self.props[display.ATOM_SDR_NITS]), 700)

    def test_unknown_live_output_defers_restoration_without_forgetting_ownership(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        self.props.pop(display.ATOM_IS_EXTERNAL)
        with patch.object(display, '_write_nits') as writing:
            self.assertFalse(display.Plugin._release())
        writing.assert_not_called()
        self.assertEqual(display.Plugin._last_written, 120.0)
        self.assertTrue(self.state['active'])

    def test_session_change_during_live_read_cannot_restore_old_baseline(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        self.props[display.ATOM_SDR_NITS] = display._float_raw(120)
        with patch.object(display, '_gamescope_start_time', side_effect=[100.0, 200.0]), \
                patch.object(display, '_write_nits') as writing:
            self.assertTrue(display.Plugin._release())
        writing.assert_not_called()
        self.assertIsNone(display.Plugin._last_written)

    def test_backlight_event_after_dock_or_hdr_change_cannot_use_an_old_gate(self):
        for atom, value in ((display.ATOM_IS_EXTERNAL, 1),
                            (display.ATOM_HDR_APP, 1), (display.ATOM_HDR_OUT, 0)):
            with self.subTest(atom=atom):
                display.Plugin._baseline = 250.0
                display.Plugin._last_written = 120.0
                self.state['active'] = True
                self.props.update({display.ATOM_IS_EXTERNAL: 0, display.ATOM_HDR_APP: 0,
                                   display.ATOM_HDR_OUT: 1, display.ATOM_SDR_NITS: display._float_raw(700)})
                self.props[atom] = value
                with patch.object(display, '_write_nits') as writing:
                    self.assertIsNone(display.Plugin._forward_nits(180.0))
                writing.assert_not_called()


class DisplayFreshReadBeforeWriteTests(unittest.TestCase):
    """A stale watcher snapshot never authorises an atom write."""
    setUp = DisplayBaselineTests.setUp

    def prepare_cache(self):
        self.props[display.ATOM_EDID_PATH] = '"/tmp/display-edid"'
        for name, value in (('_prop_cache', dict(self.props)),
                            ('_prop_cache_display', display.DISPLAY),
                            ('_prop_cache_ready', True)):
            self.stack.enter_context(patch.object(display, name, value))
        self.stack.enter_context(patch.object(display.Plugin, '_session_props',
                                             new=staticmethod(REAL_SESSION_PROPS)))
        self.stack.enter_context(patch.object(display.Plugin, '_session_retry_after', 0.0))
        self.stack.enter_context(patch.object(display.Plugin, '_session_note', ''))
        self.stack.enter_context(patch.object(display.Plugin, '_edid_path', None))
        self.stack.enter_context(patch.object(display, '_pick_display', return_value=True))
        display.Plugin._baseline = 250.0

    def test_forward_uses_live_output_even_when_watcher_still_shows_internal(self):
        self.prepare_cache()
        live = dict(self.props, **{display.ATOM_IS_EXTERNAL: 1})
        with patch.object(display, '_read_props_uncached', return_value=live), \
                patch.object(display, '_write_nits') as writing:
            self.assertIsNone(display.Plugin._forward_nits(120.0))
        writing.assert_not_called()
        self.assertFalse(self.state['active'])

    def test_release_uses_live_brightness_even_when_watcher_shows_our_value(self):
        self.prepare_cache()
        display.Plugin._last_written = 250.0
        live = dict(self.props, **{display.ATOM_SDR_NITS: display._float_raw(700)})
        with patch.object(display, '_read_props_uncached', return_value=live), \
                patch.object(display, '_write_nits') as writing:
            self.assertTrue(display.Plugin._release())
        writing.assert_not_called()
        self.assertIsNone(display.Plugin._last_written)

    def test_hybrid_release_uses_live_external_output_without_restoring_internal_hdr(self):
        self.prepare_cache()
        display.Plugin._hdr_baseline = False
        live = dict(self.props, **{display.ATOM_IS_EXTERNAL: 1})
        with patch.object(display, '_read_props_uncached', return_value=live), \
                patch.object(display, '_write_atom_int', return_value=True) as writing:
            self.assertTrue(display.Plugin._hybrid_release())
        writing.assert_called_once_with(display.ATOM_FORCE_HDR_SUPPORT, 0)

    def test_hybrid_pass_does_not_enable_internal_hdr_using_a_predock_cache(self):
        self.prepare_cache()
        self.state.update(active_mode=display.MODE_HYBRID, panel_mode=display.MODE_HYBRID)
        display.Plugin._hdr_baseline = False
        live = dict(self.props, **{display.ATOM_IS_EXTERNAL: 1})
        with patch.object(display, '_read_props_uncached', return_value=live), \
                patch.object(display, '_write_atom_int', return_value=True) as writing:
            display.Plugin._hybrid_pass()
        writing.assert_called_once_with(display.ATOM_FORCE_HDR_SUPPORT, 0)

    def test_mode_correction_cannot_write_after_docking_or_content_change(self):
        self.prepare_cache()
        for external, content in ((1, 0), (0, 1)):
            with self.subTest(external=external, content=content):
                self.state['active_mode'] = display.MODE_HYBRID
                live = dict(self.props, **{display.ATOM_IS_EXTERNAL: external, display.ATOM_HDR_APP: content})
                with patch.object(display, '_read_props_uncached', return_value=live), \
                        patch.object(display, '_write_atom_int') as writing:
                    self.assertFalse(display.Plugin._correct_hdr_mode(False))
                writing.assert_not_called()

    def test_hybrid_release_session_change_during_live_read_cannot_write_old_atoms(self):
        self.prepare_cache()
        display.Plugin._hdr_baseline = False
        with patch.object(display, '_gamescope_start_time', side_effect=[100.0, 200.0]), \
                patch.object(display, '_read_props_uncached', return_value=dict(self.props)), \
                patch.object(display, '_write_atom_int', return_value=True) as writing:
            self.assertTrue(display.Plugin._hybrid_release())
        writing.assert_not_called()


# What the device printed in Hybrid with SDR content (2026-10-17, DISPLAY=:0).
DEVICE = {
    display.ATOM_SDR_NITS: 'GAMESCOPE_SDR_ON_HDR_CONTENT_BRIGHTNESS(CARDINAL) = 1128980511',
    display.ATOM_HDR_OUT: 'GAMESCOPE_HDR_OUTPUT_FEEDBACK(CARDINAL) = 1',
    display.ATOM_HDR_APP: 'GAMESCOPE_COLOR_APP_WANTS_HDR_FEEDBACK:  not found.',
    display.ATOM_EDID_PATH: 'GAMESCOPE_DISPLAY_EDID_PATH(UTF8_STRING) = "/home/deck/.config/gamescope/edid.bin"',
    display.ATOM_IS_EXTERNAL: 'GAMESCOPE_DISPLAY_IS_EXTERNAL(CARDINAL) = 0',
    display.ATOM_HDR_ENABLED: 'GAMESCOPE_DISPLAY_HDR_ENABLED(CARDINAL) = 0',
    display.ATOM_FORCE_HDR_SUPPORT: 'GAMESCOPE_DEBUG_FORCE_HDR_SUPPORT(CARDINAL) = 1',
}


class HybridWatcherCacheTests(unittest.TestCase):
    def test_hybrid_ticks_are_served_from_the_watcher_cache_while_hdr_app_is_unset(self):
        self.run_hybrid_ticks()

    def test_one_failed_live_read_keeps_the_running_watchers_snapshot(self):
        def fail_one_live_read_then_notify():
            self.fail_next = True
            with patch.object(display, '_pick_display', return_value=True):
                display.Plugin._session_props([display.ATOM_IS_EXTERNAL], fresh=True)
            # The spy keeps running and reports only later changes, e.g. our next brightness write.
            display._update_prop_cache(DEVICE[display.ATOM_SDR_NITS], ':0')
            display.Plugin._session_retry_after = 0.0
        self.run_hybrid_ticks(fail_one_live_read_then_notify)

    def test_a_cleared_cache_is_not_completed_by_single_notifications(self):
        self.addCleanup(display._invalidate_prop_cache)
        with patch.object(display, 'DISPLAY', ':0'):
            display._set_prop_cache({display.ATOM_IS_EXTERNAL: '0'}, ':0')
            display._invalidate_prop_cache()  # e.g. DISPLAY moved away and back
            display._update_prop_cache(DEVICE[display.ATOM_SDR_NITS], ':0')
            self.assertIsNone(display._cached_props(display.WATCHED_ATOMS))

    def run_hybrid_ticks(self, disturb=lambda: None):
        calls = []
        self.fail_next = False

        def xprop(argv, **kwargs):
            calls.append(argv)
            if self.fail_next:
                self.fail_next = False
                return subprocess.CompletedProcess(argv, 1, '', 'timeout')
            out = '\n'.join(DEVICE[name] for name in argv[2:] if name in DEVICE)
            return subprocess.CompletedProcess(argv, 0, out, '')

        with ExitStack() as stack, tempfile.TemporaryDirectory() as folder:
            store = AtomicSettingsManager('display_settings', folder)
            store.replace({**display.DEFAULT_SETTINGS, 'panel_mode': 'hybrid', 'active_mode': 'hybrid'})
            state = dict(display.Plugin._state, panel_mode='hybrid', active_mode='hybrid',
                         setup_done=True, reset_in_progress=False)
            stack.enter_context(patch.object(display, 'settings', store))
            stack.enter_context(patch.object(display.Plugin, '_state', state))
            stack.enter_context(patch.object(display.Plugin, '_hdr_baseline', False))
            stack.enter_context(patch.object(display.Plugin, '_gamescope_started_at', 100.0))
            stack.enter_context(patch.object(display.Plugin, 'native_display_active', return_value=False))
            stack.enter_context(patch.object(display, '_gamescope_start_time', return_value=100.0))
            stack.enter_context(patch.object(display, '_xprop_command', side_effect=lambda args, *unused: ['xprop'] + args))
            stack.enter_context(patch.object(display, '_xprop_env', return_value={}))
            stack.enter_context(patch.object(display.subprocess, 'run', side_effect=xprop))
            stack.enter_context(patch.object(display, 'DISPLAY', ':0'))
            self.addCleanup(display._invalidate_prop_cache)
            # What _watch_properties does: the initial uncached read, then xprop -spy's replay.
            display._set_prop_cache(display._read_props_uncached(display.WATCHED_ATOMS, ':0'), ':0')
            for name in display.WATCHED_ATOMS:
                display._update_prop_cache(DEVICE[name], ':0')
            for name, value in (('_session_retry_after', 0.0), ('_session_note', ''), ('_edid_path', None)):
                stack.enter_context(patch.object(display.Plugin, name, value))
            disturb()
            calls.clear()
            for _ in range(10):
                display.Plugin._hybrid_pass_locked()
        self.assertEqual(calls, [])

    def test_unset_hdr_app_is_not_sdr_on_external_or_unknown_root(self):
        self.addCleanup(display._invalidate_prop_cache)
        for is_external in ('1', None):
            with self.subTest(is_external=is_external):
                values = {display.ATOM_HDR_ENABLED: '0'}
                if is_external is not None:
                    values[display.ATOM_IS_EXTERNAL] = is_external
                with patch.object(display, 'DISPLAY', ':0'):
                    display._set_prop_cache(values, ':0')
                    self.assertNotIn(display.ATOM_HDR_APP, display._cached_props(display.WATCHED_ATOMS))


class XpropIdentityTests(unittest.TestCase):
    def as_root(self, owner):
        account = type('Account', (), {'pw_gid': 1001})()
        return (patch.object(display.os, 'geteuid', create=True, return_value=0),
                patch.object(display, '_display_owner', return_value=owner),
                patch.object(display.pwd, 'getpwuid', create=True, return_value=account))

    def test_root_drops_to_the_display_owner_without_a_pam_session(self):
        patches = self.as_root(1000)
        with patches[0], patches[1], patches[2]:
            self.assertEqual(display._xprop_user(':0'),
                             {'user': 1000, 'group': 1001, 'extra_groups': []})
        self.assertNotIn('runuser', ' '.join(display._xprop_command(['-root'])))

    def test_no_identity_change_for_root_owned_or_unknown_display(self):
        for owner in (0, None):
            patches = self.as_root(owner)
            with patches[0], patches[1], patches[2]:
                self.assertEqual(display._xprop_user(':0'), {})

    def test_reads_writes_and_the_watcher_all_run_as_the_display_owner(self):
        identity = {'user': 1000, 'group': 1001, 'extra_groups': []}
        done = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(display, '_xprop_user', return_value=identity), \
                patch.object(display, '_xprop_env', return_value={}), \
                patch.object(display.subprocess, 'run', return_value=done) as run:
            display._read_props_uncached([display.ATOM_IS_EXTERNAL], ':0')
            display._probe_gamescope(':0')
            self.assertTrue(display._write_atom_int(display.ATOM_SDR_NITS, 1))
        self.assertEqual(run.call_count, 3)
        for item in run.call_args_list:
            self.assertEqual({k: item.kwargs.get(k) for k in identity}, identity)

        spawned = {}

        class Stdout:
            async def readline(self):
                return b''

        class Process:
            stdout = Stdout()
            returncode = 0
            pid = 1

        async def spawn(*argv, **kwargs):
            spawned.update(argv=argv, kwargs=kwargs)
            return Process()

        async def stop(_seconds):
            raise asyncio.CancelledError()

        async def offload(function, *args):
            return function(*args)

        with patch.object(display, '_xprop_user', return_value=identity), \
                patch.object(display, '_xprop_env', return_value={}), \
                patch.object(display, '_pick_display', return_value=True), \
                patch.object(display, '_read_props_uncached', return_value={}), \
                patch.object(display, '_offload', new=offload), \
                patch.object(display.asyncio, 'create_subprocess_exec', new=spawn), \
                patch.object(display.asyncio, 'sleep', new=stop):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(display._watch_properties())
        self.assertNotIn('runuser', spawned['argv'][0])
        self.assertEqual({k: spawned['kwargs'].get(k) for k in identity}, identity)

    def test_watcher_warns_only_when_it_does_not_reconnect(self):
        import collections, json, diagnostics

        def run(sessions):
            records, clock = [], [0.0]

            class Process:
                returncode = 0
                pid = 1

                def __init__(self, lines):
                    lines = list(lines) + [b'']

                    class Stdout:
                        async def readline(self):
                            return lines.pop(0)
                    self.stdout = Stdout()

            async def spawn(*argv, **kwargs):
                return Process(sessions.pop(0))

            async def tick(_seconds):
                clock[0] += 10.0
                if not sessions:
                    raise asyncio.CancelledError()

            async def offload(function, *args):
                return function(*args)

            with patch.object(diagnostics, '_sink', lambda text, level: records.append(json.loads(text))), \
                    patch.object(diagnostics, '_seen', collections.OrderedDict()), \
                    patch.object(display, '_xprop_user', return_value={}), \
                    patch.object(display, '_pick_display', return_value=True), \
                    patch.object(display, '_read_props_uncached', return_value={}), \
                    patch.object(display, '_update_prop_cache'), \
                    patch.object(display, '_offload', new=offload), \
                    patch.object(display.time, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(display.asyncio, 'create_subprocess_exec', new=spawn), \
                    patch.object(display.asyncio, 'sleep', new=tick):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(display._watch_properties())
            return [row['event'] for row in records if row['level'] != 'info']

        line = [b'GAMESCOPE_HDR_OUTPUT_FEEDBACK(CARDINAL) = 0']
        # Gamescope restarts: each drop reconnects within one session restart.
        self.assertEqual(run([line, [], line, [], line]), [])
        # Gamescope stays reachable but xprop never reconnects.
        self.assertEqual(run([line, [], [], [], [], []]), ['property_watcher_disconnected'])


class HybridTimingTests(unittest.TestCase):
    """PQ at once; gamma 2.2 only after continuous SDR and a minimum PQ stay."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = 0.0
        self.props = {display.ATOM_IS_EXTERNAL: 0, display.ATOM_FORCE_HDR_SUPPORT: 1,
                      display.ATOM_HDR_ENABLED: 0, display.ATOM_HDR_APP: 0, display.ATOM_HDR_OUT: 0}
        self.writes = []
        self.named = []
        self.sticks = True
        state = dict(display.Plugin._state, active_mode=display.MODE_HYBRID,
                     panel_mode=display.MODE_HYBRID, setup_done=True, reset_in_progress=False)
        for name, value in {'_state': state, '_hdr_baseline': False, '_hybrid_suspended_external': False,
                            '_hybrid_pq_hold_until': 0.0, '_hybrid_sdr_since': None,
                            '_hdr_fights': 0, '_hdr_fight_target': None, '_hdr_fight_at': 0.0, '_hdr_backoff_until': 0.0}.items():
            self.stack.enter_context(patch.object(display.Plugin, name, value))
        self.stack.enter_context(patch.object(display.Plugin, 'native_display_active', return_value=False))
        self.stack.enter_context(patch.object(display.Plugin, '_ownership_session_current', return_value=True))
        self.stack.enter_context(patch.object(display.Plugin, '_session_props',
                                              side_effect=lambda names, **kwargs: dict(self.props)))
        self.stack.enter_context(patch.object(display, 'time', types.SimpleNamespace(monotonic=lambda: self.now)))
        def write(name, value):
            self.writes.append((self.now, value))
            self.named.append((name, value))
            if self.sticks:
                self.props[name] = value
            return True
        self.stack.enter_context(patch.object(display, '_write_atom_int', side_effect=write))

    def tick(self, at, content=None):
        self.now = at
        if content is not None:
            self.props[display.ATOM_HDR_APP] = content
        display.Plugin._hybrid_pass()

    def test_one_pass_decides_from_the_atoms_it_read(self):
        F, E, A, O, X = (display.ATOM_FORCE_HDR_SUPPORT, display.ATOM_HDR_ENABLED,
                         display.ATOM_HDR_APP, display.ATOM_HDR_OUT, display.ATOM_IS_EXTERNAL)
        g22, pq = 'no HDR content, panel in gamma 2.2', 'HDR content on screen, panel in PQ'
        wait = 'no HDR content, waiting for a stable switch to gamma 2.2'
        ext = 'external display, not ours to drive'
        # (atoms over the defaults, None = unset; baseline, suspended, SDR since)
        #   -> (writes, reason, hdr_now, baseline after)
        cases = [
            ({}, False, False, None, [], g22, False, False),
            ({A: 1}, False, False, None, [(E, 1)], pq, True, False),
            ({A: 1, E: 1}, False, False, None, [], pq, True, False),
            ({E: 1}, False, False, None, [], wait, True, False),
            ({F: 0}, False, False, None, [(F, 1)], g22, False, False),
            # Forced with the input unset: output feedback is synthetic, so write the mode.
            ({E: None}, False, False, 0.0, [(E, 0)], g22, False, False),
            ({E: None, A: 1, O: 1}, False, False, None, [(E, 1)], pq, True, False),
            # Unforced with the input unset: output feedback is the real starting mode.
            ({F: 0, E: None, O: 1}, False, False, None, [(F, 1)], wait, True, False),
            ({X: 1, O: 1}, False, False, None, [(F, 0)], ext, True, False),
            ({X: 1, O: 1}, False, True, None, [], ext, None, False),
            ({X: None}, False, False, None, [], 'could not verify the active display', None, False),
            ({E: 1}, None, False, None, [], wait, True, True),
            ({E: None, O: 1}, None, False, None, [], wait, True, False),
            # Back from a dock with no baseline: the panel's own output, not the monitor's request.
            ({F: 0, E: 1, O: 0}, None, True, None, [(F, 1)], wait, True, False),
        ]
        self.now = 10.0
        self.stack.enter_context(patch.object(display.Plugin, '_edid_recheck_until', 0.0))
        self.stack.enter_context(patch.object(display.Plugin, '_remember_hdr_baseline',
            side_effect=lambda value: setattr(display.Plugin, '_hdr_baseline', bool(value))))
        defaults = dict(self.props)
        for atoms, baseline, suspended, sdr_since, writes, reason, hdr_now, after in cases:
            with self.subTest(atoms=atoms, baseline=baseline, suspended=suspended):
                self.props = {k: v for k, v in {**defaults, **atoms}.items() if v is not None}
                self.named.clear()
                display.Plugin._state.update(hdr_now=None, hybrid_reason=None)
                display.Plugin._hdr_baseline = baseline
                display.Plugin._hybrid_suspended_external = suspended
                display.Plugin._hybrid_sdr_since = sdr_since
                display.Plugin._hybrid_pq_hold_until = 0.0
                display.Plugin._hdr_fights, display.Plugin._hdr_fight_target = 0, None
                display.Plugin._hybrid_pass()
                self.assertEqual(self.named, writes)
                self.assertEqual(display.Plugin._state['hybrid_reason'], reason)
                self.assertEqual(display.Plugin._state['hdr_now'], hdr_now)
                self.assertEqual(display.Plugin._hdr_baseline, after)

    def test_pq_at_once_and_gamma_only_after_the_hold_and_continuous_sdr(self):
        self.tick(0.0, content=1)
        self.assertEqual(self.writes, [(0.0, 1)])
        self.tick(0.5, content=0)       # SDR from 0.5: debounce alone would allow 2.5
        self.tick(2.5)
        self.tick(2.9)
        self.assertEqual(self.writes, [(0.0, 1)], 'PQ is held for 3 s after entering it')
        self.tick(3.0)
        self.assertEqual(self.writes, [(0.0, 1), (3.0, 0)])

    def test_returning_hdr_restarts_the_sdr_debounce(self):
        self.tick(0.0, content=1)
        self.tick(10.0, content=0)
        self.tick(11.0, content=1)      # back to HDR before 2 s of SDR
        self.tick(11.5, content=0)
        self.tick(13.4)
        self.assertEqual(self.writes, [(0.0, 1)])
        self.tick(13.5)
        self.assertEqual(self.writes, [(0.0, 1), (13.5, 0)])

    def test_four_undone_corrections_then_stand_off_for_sixty_seconds(self):
        self.sticks = False             # another writer keeps reverting the mode
        self.props[display.ATOM_HDR_APP] = 1
        for second in range(8):
            self.tick(float(second))
        self.assertEqual(self.writes, [(0.0, 1), (1.0, 1), (2.0, 1), (3.0, 1)])
        self.assertIn('standing off', display.Plugin._state['hybrid_reason'])
        self.tick(4.0 + display.HDR_BACKOFF_S - 0.1)
        self.assertEqual(len(self.writes), 4)
        self.tick(4.0 + display.HDR_BACKOFF_S)
        self.assertEqual(self.writes[-1], (4.0 + display.HDR_BACKOFF_S, 1))

    def test_undone_gamma_switches_also_stand_off(self):
        self.sticks = False             # another writer keeps putting the panel back into PQ
        self.props[display.ATOM_HDR_ENABLED] = 1
        for step in range(60):
            self.tick(step / 2)
        self.assertEqual(self.writes, [(2.0, 0), (4.5, 0), (7.0, 0), (9.5, 0)])
        self.assertIn('standing off', display.Plugin._state['hybrid_reason'])

    def test_a_writer_that_undoes_us_a_pass_later_still_ends_in_a_stand_off(self):
        # Our write sticks for one pass, so the panel reads as settled before it is undone.
        self.props[display.ATOM_HDR_APP] = 1
        for second in range(40):
            self.tick(float(second))
            if self.props[display.ATOM_HDR_ENABLED] == 1 and second % 2 == 0:
                self.props[display.ATOM_HDR_ENABLED] = 0    # reverted after our pass
        self.assertEqual(len(self.writes), display.HDR_FIGHT_LIMIT)
        self.assertIn('standing off', display.Plugin._state['hybrid_reason'])

    def test_a_display_hand_off_starts_the_argument_afresh(self):
        self.sticks = False             # another writer keeps reverting the mode
        self.props[display.ATOM_HDR_APP] = 1
        for second in range(4):
            self.tick(float(second))
        self.assertEqual(len(self.writes), 4)
        self.props[display.ATOM_IS_EXTERNAL] = 1
        self.tick(4.0)
        self.props[display.ATOM_IS_EXTERNAL] = 0
        self.props[display.ATOM_FORCE_HDR_SUPPORT] = 1
        self.props[display.ATOM_HDR_ENABLED] = 0
        self.tick(5.0)
        self.assertEqual(self.writes[-1], (5.0, 1))  # corrected again, not a stand-off
        self.assertNotIn('standing off', display.Plugin._state['hybrid_reason'])

    def test_corrections_far_apart_or_to_another_target_never_stand_off(self):
        self.props[display.ATOM_HDR_APP] = 1
        for n in range(8):                  # one undone switch every 40 s
            at = n * 40.0
            self.props[display.ATOM_HDR_ENABLED] = 0
            self.tick(at)
            self.assertEqual(self.writes[-1], (at, 1))
        self.assertNotIn('standing off', display.Plugin._state['hybrid_reason'])


if __name__ == '__main__':
    unittest.main()

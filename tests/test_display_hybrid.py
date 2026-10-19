# SPDX-License-Identifier: BSD-3-Clause
"""Hybrid PQ / gamma 2.2 rules, HDR feedback, brightness forwarding and session-safe release.

Driven through the plugin's own passes (gate tick, slider event, Hybrid pass, release, RPCs) against a
fake gamescope: xprop on its root window (atoms, accepted writes, failures) and its process start time,
which identifies the session. Settings live in a real store in a temp dir.
"""
import asyncio
from contextlib import ExitStack
import copy
from pathlib import Path
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import support  # noqa: F401 - Decky/Linux doubles before importing the backend.
import display_backend as display
import safe_settings
from safe_settings import AtomicSettingsManager

P = display.Plugin
X, F, E, A, O, NITS, EDID = (display.ATOM_IS_EXTERNAL, display.ATOM_FORCE_HDR_SUPPORT, display.ATOM_HDR_ENABLED,
                             display.ATOM_HDR_APP, display.ATOM_HDR_OUT, display.ATOM_SDR_NITS, display.ATOM_EDID_PATH)
G22, HYBRID = display.MODE_G22, display.MODE_HYBRID
# Internal panel in PQ showing SDR content at Steam's 250 nits.
PQ_PANEL = {X: 0, NITS: display._float_raw(250), O: 1, A: 0, F: 0, E: 1}
# Internal panel under Hybrid in gamma 2.2: support forced, mode off.
HYBRID_PANEL = {X: 0, F: 1, E: 0, A: 0, O: 0}
# Runtime attributes as a fresh Decky process has them (locks are shared, not state).
PRISTINE = {name: value for name, value in vars(P).items()
            if not name.startswith('__') and 'lock' not in name and not callable(value)
            and not isinstance(value, (staticmethod, classmethod, property))}


class Gamescope:
    """The gamescope root window as xprop sees it, plus the compositor's start time (its session)."""

    def __init__(self, clock, atoms):
        self.clock = clock
        self.atoms = dict(atoms)
        self.started = 100.0
        self.writes = []            # (time, atom, value) gamescope accepted
        self.attempts = 0
        self.sticks = True          # False: another writer puts every value straight back
        self.refuse_writes = False
        self.read_error = None
        self.returncode = 0
        self.fail_reads = 0         # the next N reads exit non-zero
        self.missing = 'not found.'  # xprop's reply for an atom nobody published; None prints nothing
        self.hdr_line = None
        self.reads = 0
        self.on_read = None         # gamescope changing while a read is answered
        self.on_write = None

    def line(self, name):
        if name == A and self.hdr_line is not None:
            return self.hdr_line
        if name in self.atoms:
            value = self.atoms[name]
            return f'{name}({"UTF8_STRING" if isinstance(value, str) else "CARDINAL"}) = {value}'
        return None if self.missing is None else f'{name}:  {self.missing}'

    def run(self, argv, **_kwargs):
        args = list(argv[1:])
        if '-set' in args:
            self.attempts += 1
            if self.refuse_writes:
                raise subprocess.CalledProcessError(1, argv)
            name, value = args[-2], int(args[-1])
            if self.on_write:
                self.on_write(name, value)
            self.writes.append((self.clock(), name, value))
            if self.sticks:
                self.atoms[name] = value
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        if self.read_error is not None:
            raise self.read_error
        self.reads += 1
        code = self.returncode
        if self.fail_reads:
            self.fail_reads, code = self.fail_reads - 1, 1
        names = args[1:] or list(self.atoms)  # a bare -root lists everything (the session probe)
        if self.on_read:
            self.on_read(names)
        out = '\n'.join(line for line in map(self.line, names) if line is not None)
        return subprocess.CompletedProcess(argv, code, out, '')

    def modes(self):
        return [(name, value) for _, name, value in self.writes if name != NITS]

    def nits(self):
        return [round(display._as_float(value), 2) for _, name, value in self.writes if name == NITS]


class DisplayFixture(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.folder = stack.enter_context(tempfile.TemporaryDirectory())
        self.store = AtomicSettingsManager('display_settings', self.folder)
        saved = {name: getattr(P, name) for name in PRISTINE}
        self.addCleanup(lambda: [setattr(P, name, value) for name, value in saved.items()])
        self.addCleanup(display._invalidate_prop_cache)
        self.gs = Gamescope(lambda: self.now, {})
        for target, name, value in (
                (display, 'settings', self.store), (display, 'DISPLAY', ':0'),
                (display, 'INSTALLED_SCRIPT', str(Path(self.folder) / 'no-script.lua')),
                (display, 'time', types.SimpleNamespace(monotonic=lambda: self.now, time=time.time, sleep=time.sleep)),
                (display, '_gamescope_start_time', lambda: self.gs.started),
                (display.subprocess, 'run', lambda *args, **kwargs: self.gs.run(*args, **kwargs)),
                (display.display_native, 'detect_native_display_support',
                 lambda: {'status': 'absent', 'supported': False, 'reason': 'no native profile'})):
            stack.enter_context(patch.object(target, name, value))

    def start(self, mode='pq', atoms=PQ_PANEL, **stored):
        """A chosen mode, then the plugin adopts the running gamescope."""
        self.gs = Gamescope(lambda: self.now, atoms)
        self.store.replace({**display.DEFAULT_SETTINGS, 'panel_mode': mode, 'active_mode': mode, **stored})
        self.reload()

    def reload(self):
        """A new Decky process in the same gamescope session: only the settings file survives."""
        for name, value in PRISTINE.items():
            setattr(P, name, copy.deepcopy(value))
        display._invalidate_prop_cache()
        P._state.update(panel_mode=self.store.getSetting('panel_mode'), active_mode=self.store.getSetting('active_mode'),
                        setup_done=True, enabled=True, panel_ok=True)
        P._sync_loaded_script()

    def gate(self):
        """One gate tick as the worker runs it: an open gate admits the slider."""
        ok = P._refresh_gate()
        if ok:
            P._state['active'] = True
        return ok

    def own_brightness(self, nits=120.0):
        self.assertTrue(self.gate())
        self.assertTrue(P._forward_nits(nits))

    def tick(self, at, **atoms):
        self.now = at
        self.gs.atoms.update(atoms)
        P._hybrid_pass()

    def watcher_snapshot(self):
        """What xprop -spy reported so far; it is not refreshed by later gamescope changes."""
        display._set_prop_cache({name: str(value) for name, value in self.gs.atoms.items()}, ':0')

    def persisted(self):
        return AtomicSettingsManager('display_settings', self.folder).settings

    def disk_full(self):
        real = safe_settings._atomic_write_bytes

        def write(path, data, *args, **kwargs):
            if Path(path) == Path(self.store.path):
                raise OSError('disk full')
            return real(path, data, *args, **kwargs)
        return patch.object(safe_settings, '_atomic_write_bytes', side_effect=write)


class DisplayHdrFeedbackTests(DisplayFixture):
    """HDR_APP is published only on change: its explicit absence on the internal panel is SDR, nothing else is."""

    def settle_hybrid(self, **atoms):
        self.tick(self.now, **atoms)
        self.tick(self.now + display.HYBRID_SDR_DEBOUNCE_S)

    def test_missing_hdr_app_is_sdr_for_pq_brightness(self):
        for message in ('not found.', 'no such atom on any window.'):
            with self.subTest(message=message):
                self.start(atoms={k: v for k, v in PQ_PANEL.items() if k != A})
                self.gs.missing = message
                self.own_brightness(14.13)
                self.assertEqual(self.gs.nits(), [14.13])

    def test_missing_hdr_app_is_sdr_for_hybrid(self):
        self.start(HYBRID, {X: 0, F: 1, E: 1, O: 1})
        self.settle_hybrid()
        self.assertEqual(self.gs.modes(), [(E, 0)])

    def test_explicit_hdr_content_still_withdraws_writer_and_keeps_hybrid_pq(self):
        self.start()
        self.assertTrue(self.gate())
        self.gs.atoms[A] = 1
        self.assertIsNone(P._forward_nits(14.13))
        self.assertNotIn(14.13, self.gs.nits())
        self.start(HYBRID, {**HYBRID_PANEL, A: 1, E: 1, O: 1})
        self.settle_hybrid()
        self.assertEqual(self.gs.modes(), [])

    def test_failed_xprop_never_authorizes_writer(self):
        # Even an explicit 0 is untrustworthy when xprop failed after printing it.
        for explicit in (0, None):
            with self.subTest(explicit=explicit):
                atoms = {k: v for k, v in PQ_PANEL.items() if k != A or explicit is not None}
                self.start(atoms=atoms)
                self.assertTrue(self.gate())
                self.gs.returncode = 1
                self.assertIsNone(P._forward_nits(14.13))
                self.start(HYBRID, {**{k: v for k, v in HYBRID_PANEL.items() if k != A}, E: 1, O: 1})
                self.gs.returncode = 1
                self.settle_hybrid()
                self.assertEqual(self.gs.writes, [])

    def test_timeout_and_os_error_do_not_become_sdr(self):
        for error in (subprocess.TimeoutExpired('xprop', 3), OSError('unavailable')):
            with self.subTest(error=type(error).__name__):
                self.start()
                self.assertTrue(self.gate())
                self.gs.read_error = error
                self.assertIsNone(P._forward_nits(14.13))
                self.assertEqual(self.gs.nits(), [])

    def test_missing_line_without_explicit_absence_is_unknown(self):
        self.start()
        self.assertTrue(self.gate())
        del self.gs.atoms[A]
        self.gs.missing = None
        self.assertIsNone(P._forward_nits(14.13))
        self.assertNotIn(14.13, self.gs.nits())
        self.start(HYBRID, {X: 0, F: 1, E: 1, O: 1})
        self.gs.missing = None
        self.settle_hybrid()
        self.assertEqual(self.gs.writes, [])

    def test_malformed_feedback_does_not_become_sdr(self):
        for line in (f'{A}(CARDINAL) = invalid', f'{A}: unexpected not found text'):
            with self.subTest(line=line):
                self.start()
                self.assertTrue(self.gate())
                self.gs.hdr_line = line
                self.assertIsNone(P._forward_nits(14.13))
                self.assertNotIn(14.13, self.gs.nits())

    def test_absence_is_not_sdr_on_external_or_unidentified_root(self):
        for external in (1, None):
            with self.subTest(external=external):
                self.start()
                self.assertTrue(self.gate())
                del self.gs.atoms[A]
                if external is None:
                    del self.gs.atoms[X]
                else:
                    self.gs.atoms[X] = external
                self.assertIsNone(P._forward_nits(14.13))
                self.assertNotIn(14.13, self.gs.nits())


class DisplayGateTests(DisplayFixture):
    def test_gate_reasons_and_when_brightness_is_handed_back(self):
        g22 = 'gamma 2.2 mode - the system slider drives the panel directly'
        cases = (
            # (state change, gamescope change, gate open, reason, Steam's 250 nits written back, stale force cleared)
            ({}, {}, True, 'forwarding slider to gamescope', False, False),
            ({'reset_in_progress': True}, {}, False, 'forwarding slider to gamescope', False, False),
            ({'setup_done': False}, {}, False, 'waiting for the display script', True, False),
            ({'active_mode': 'bogus'}, {}, False, 'waiting for gamescope to load a display mode', True, False),
            ({'active_mode': G22}, {}, False, g22, True, False),
            ({'active_mode': G22}, {F: 1}, False, g22, True, True),
            ({'active_mode': G22}, {F: 1, 'refuse': True}, False, 'could not clear stale hybrid HDR support', False, False),
            ({'active_mode': HYBRID}, {F: 1}, False,
             'hybrid mode - the panel switches itself, nothing to forward', True, False),
            ({'panel_ok': False, 'enabled': False}, {}, False, 'display not on the affected list', True, False),
            ({'enabled': False}, {}, False, 'turned off', True, False),
            ({}, {'unreachable': True}, False, 'gamescope not reachable', False, False),
            ({}, {A: 1}, False, 'HDR content on screen, Steam handles it', True, False),
        )
        for state, change, allowed, reason, handed_back, cleared in cases:
            with self.subTest(state=state, change=change):
                self.start()
                self.own_brightness(120.0)
                change = dict(change)
                self.gs.refuse_writes = change.pop('refuse', False)
                if change.pop('unreachable', False):
                    self.gs.read_error = OSError('gamescope is gone')
                self.gs.atoms.update(change)
                P._state.update(state)
                self.assertIs(P._refresh_gate(), allowed)
                self.assertEqual(P._state['reason'], reason)
                self.assertEqual(self.gs.nits(), [120.0, 250.0] if handed_back else [120.0])
                self.assertEqual(self.gs.modes(), [(F, 0)] if cleared else [])


class DisplayBaselineTests(DisplayFixture):
    def test_brightness_baseline_is_durable_before_any_write_and_survives_a_reload(self):
        self.start()

        def durable(name, value):
            saved = self.persisted()
            self.assertEqual((saved['brightness_baseline'], saved['brightness_baseline_session']), (250.0, 100.0),
                             'Baseline must already be durable before an atom changes')
        self.gs.on_write = durable
        with self.disk_full(), self.assertRaisesRegex(RuntimeError, 'recovery state.*disk full'):
            P._refresh_gate()
        self.assertFalse(P._state['active'])
        self.assertIsNone(P._forward_nits(120.0))
        # Even a stale gate which sets active again must not authorize a
        # write until the missing durable baseline has been captured.
        P._state['active'] = True
        self.assertIsNone(P._forward_nits(120.0))
        self.assertEqual(self.gs.writes, [])
        self.assertIsNone(self.store.getSetting('brightness_baseline'))
        self.assertIsNone(self.persisted()['brightness_baseline'])
        self.own_brightness(120.0)
        # A fresh Decky process restores the same compositor's baseline from the
        # settings, rather than learning its own forwarded value.
        self.reload()
        self.own_brightness(120.0)
        self.assertTrue(P._release())
        self.assertEqual(self.gs.nits(), [120.0, 120.0, 250.0])
        self.assertEqual(display._as_float(self.gs.atoms[NITS]), 250.0)

    def test_hdr_baseline_is_durable_before_force_and_mode_writes_and_survives_a_reload(self):
        self.start(HYBRID, {**HYBRID_PANEL, F: 0, A: 1})

        def durable(name, value):
            saved = self.persisted()
            self.assertIs(saved['hdr_baseline'], False)
            self.assertEqual(saved['hdr_baseline_session'], 100.0)
        self.gs.on_write = durable
        with self.disk_full(), self.assertRaisesRegex(RuntimeError, 'recovery state.*disk full'):
            P._hybrid_pass()
        self.assertEqual(self.gs.writes, [])
        self.assertIsNone(self.store.getSetting('hdr_baseline'))
        self.assertIsNone(self.persisted()['hdr_baseline'])
        P._hybrid_pass()
        self.assertEqual(self.gs.modes(), [(F, 1), (E, 1)])
        self.reload()
        self.assertTrue(P._hybrid_release())
        self.assertEqual(self.gs.modes()[-2:], [(F, 0), (E, 0)])
        self.assertIsNone(self.persisted()['hdr_baseline'])

    def test_advisory_metadata_failure_remains_best_effort_without_retaining_rejected_settings(self):
        self.start()
        before = copy.deepcopy(self.store.settings)
        with self.disk_full():
            P._store_settings({'active_mode': HYBRID})
        self.assertEqual(self.store.settings, before)
        self.assertEqual(self.persisted(), before)


class DisplaySessionTests(DisplayFixture):
    def test_disable_after_compositor_restart_does_not_restore_old_brightness(self):
        self.start()
        self.own_brightness(120.0)
        self.gs.started = 200.0
        result = asyncio.run(P().set_enabled(False))
        self.assertEqual(self.gs.nits(), [120.0])
        self.assertFalse(result['active'])
        self.assertEqual(result['reason'], 'turned off')

    def test_unload_after_restart_does_not_restore_old_hdr_atoms(self):
        self.start(HYBRID, HYBRID_PANEL)
        self.tick(self.now)  # captures the baseline
        self.gs.started = 200.0
        asyncio.run(P()._unload())
        self.assertEqual(self.gs.writes, [])

    def test_unknown_session_defers_release_and_same_session_can_retry(self):
        self.start()
        self.own_brightness(120.0)
        self.gs.started = None
        self.assertFalse(P._release())
        self.gs.started = 100.0
        self.assertTrue(P._release())
        self.assertEqual(self.gs.nits(), [120.0, 250.0])

        self.start(HYBRID, HYBRID_PANEL)
        self.tick(self.now)
        self.gs.started = None
        self.assertFalse(P._hybrid_release())
        self.assertEqual(self.gs.writes, [])
        self.gs.started = 100.0
        self.assertTrue(P._hybrid_release())
        self.assertEqual(self.gs.modes(), [(F, 0), (E, 0)])

    def test_stale_hybrid_pass_cannot_write_before_the_new_session_is_adopted(self):
        self.start(HYBRID, {**HYBRID_PANEL, F: 0})
        self.gs.started = 200.0
        self.tick(self.now)
        self.assertEqual(self.gs.writes, [])
        self.assertEqual(P._state['hybrid_reason'], 'waiting to verify the current gamescope session')

    def test_new_session_recovers_forwarding_and_restores_its_own_fresh_baseline(self):
        self.start()
        self.own_brightness(120.0)
        self.gs.started = 200.0
        self.gs.atoms[NITS] = display._float_raw(700.0)
        self.assertIsNone(P._forward_nits(180.0), 'a stale gate never reaches a new compositor')
        self.assertFalse(P._state['active'])
        P._sync_loaded_script()  # the worker notices the new gamescope
        self.own_brightness(180.0)
        self.assertTrue(P._release())
        self.assertEqual(self.gs.nits(), [120.0, 180.0, 700.0])
        self.assertEqual(self.store.getSetting('brightness_baseline_session'), 200.0)
        self.assertEqual(self.store.getSetting('brightness_baseline'), 700.0)


class DisplayReleaseTests(DisplayFixture):
    def test_public_disable_preserves_new_external_brightness(self):
        self.start()
        self.own_brightness(120.0)
        self.gs.atoms.update({X: 1, NITS: display._float_raw(700)})
        result = asyncio.run(P().set_enabled(False))
        self.assertEqual(self.gs.nits(), [120.0])
        self.assertFalse(result['active'])
        self.assertEqual(display._as_float(self.gs.atoms[NITS]), 700)

    def test_unknown_live_output_defers_restoration_without_forgetting_ownership(self):
        self.start()
        self.own_brightness(120.0)
        del self.gs.atoms[X]
        self.assertFalse(P._release())
        self.assertTrue(P._state['active'])
        self.gs.atoms[X] = 0
        self.assertTrue(P._release())
        self.assertEqual(self.gs.nits(), [120.0, 250.0])

    def test_session_change_during_live_read_cannot_restore_old_baseline(self):
        self.start()
        self.own_brightness(120.0)
        self.gs.on_read = lambda names: setattr(self.gs, 'started', 200.0)
        self.assertTrue(P._release())
        self.assertEqual(self.gs.nits(), [120.0])

    def test_backlight_event_after_dock_or_hdr_change_cannot_use_an_old_gate(self):
        for atom, value in ((X, 1), (A, 1), (O, 0)):
            with self.subTest(atom=atom):
                self.start()
                self.own_brightness(120.0)
                self.gs.atoms[atom] = value
                self.assertIsNone(P._forward_nits(180.0))
                self.assertNotIn(180.0, self.gs.nits())


class DisplayFreshReadBeforeWriteTests(DisplayFixture):
    """A stale watcher snapshot never authorises an atom write."""

    def test_forward_uses_live_output_even_when_watcher_still_shows_internal(self):
        self.start()
        self.watcher_snapshot()
        self.assertTrue(self.gate())
        self.gs.atoms[X] = 1
        self.assertIsNone(P._forward_nits(120.0))
        self.assertEqual(self.gs.nits(), [])
        self.assertFalse(P._state['active'])

    def test_release_uses_live_brightness_even_when_watcher_shows_our_value(self):
        self.start()
        self.own_brightness(120.0)
        self.watcher_snapshot()
        self.gs.atoms[NITS] = display._float_raw(700)
        self.assertTrue(P._release())
        self.assertEqual(self.gs.nits(), [120.0])

    def test_hybrid_release_uses_live_external_output_without_restoring_internal_hdr(self):
        self.start(HYBRID, HYBRID_PANEL)
        self.tick(self.now)
        self.watcher_snapshot()
        self.gs.atoms[X] = 1
        self.assertTrue(P._hybrid_release())
        self.assertEqual(self.gs.modes(), [(F, 0)])

    def test_hybrid_pass_does_not_enable_internal_hdr_using_a_predock_cache(self):
        self.start(HYBRID, {**HYBRID_PANEL, F: 0}, hdr_baseline=False, hdr_baseline_session=100.0)
        self.watcher_snapshot()
        self.gs.atoms[X] = 1
        self.tick(self.now)
        self.assertEqual(self.gs.modes(), [(F, 0)])

    def test_pq_correction_cannot_write_after_docking(self):
        self.start(atoms={**PQ_PANEL, O: 0})
        self.watcher_snapshot()
        self.gs.atoms[X] = 1
        self.assertFalse(P._refresh_gate())
        self.assertEqual(self.gs.modes(), [])

    def test_hybrid_switch_rechecks_the_display_and_content_just_before_writing(self):
        for change in ({X: 1}, {A: 1}):
            with self.subTest(change=change):
                self.start(HYBRID, {**HYBRID_PANEL, E: 1, O: 1}, hdr_baseline=False, hdr_baseline_session=100.0)
                self.tick(self.now)  # SDR seen in PQ: the switch to gamma 2.2 is due one debounce later

                def change_before_switch(names):
                    if set(names) == {X, A, EDID}:  # the switch's own re-check
                        self.gs.atoms.update(change)
                self.gs.on_read = change_before_switch
                self.tick(self.now + display.HYBRID_SDR_DEBOUNCE_S)
                self.assertEqual(self.gs.writes, [])

    def test_hybrid_release_session_change_during_live_read_cannot_write_old_atoms(self):
        self.start(HYBRID, HYBRID_PANEL)
        self.tick(self.now)
        self.gs.on_read = lambda names: setattr(self.gs, 'started', 200.0)
        self.assertTrue(P._hybrid_release())
        self.assertEqual(self.gs.writes, [])


# What the device printed in Hybrid with SDR content (2026-10-17, DISPLAY=:0); HDR_APP was "not found".
DEVICE = {NITS: 1128980511, O: 1, EDID: '"/home/deck/.config/gamescope/edid.bin"', X: 0, E: 0, F: 1}


class HybridWatcherCacheTests(DisplayFixture):
    def run_hybrid_ticks(self, disturb=lambda: None):
        self.start(HYBRID, DEVICE, hdr_baseline=False, hdr_baseline_session=100.0)
        # What _watch_properties does: the initial uncached read, then xprop -spy's replay.
        display._set_prop_cache(display._read_props_uncached(display.WATCHED_ATOMS, ':0'), ':0')
        for name in display.WATCHED_ATOMS:
            display._update_prop_cache(self.gs.line(name), ':0')
        disturb()
        self.gs.reads = 0
        for _ in range(10):
            self.tick(self.now + display.HYBRID_INTERVAL_S)
        self.assertEqual((self.gs.reads, self.gs.writes), (0, []))

    def test_hybrid_ticks_are_served_from_the_watcher_cache_while_hdr_app_is_unset(self):
        self.run_hybrid_ticks()

    def test_one_failed_live_read_keeps_the_running_watchers_snapshot(self):
        def fail_one_live_read_then_notify():
            self.gs.fail_reads = 1
            P._session_props([X], fresh=True)
            # The spy keeps running and reports only later changes, e.g. our next brightness write.
            display._update_prop_cache(self.gs.line(NITS), ':0')
        self.run_hybrid_ticks(fail_one_live_read_then_notify)

    def test_a_cleared_cache_is_not_completed_by_single_notifications(self):
        display._set_prop_cache({X: '0'}, ':0')
        display._invalidate_prop_cache()  # e.g. DISPLAY moved away and back
        display._update_prop_cache(f'{NITS}(CARDINAL) = {DEVICE[NITS]}', ':0')
        self.assertIsNone(display._cached_props(display.WATCHED_ATOMS))

    def test_unset_hdr_app_is_not_sdr_on_external_or_unknown_root(self):
        for is_external in ('1', None):
            with self.subTest(is_external=is_external):
                values = {E: '0'}
                if is_external is not None:
                    values[X] = is_external
                display._set_prop_cache(values, ':0')
                self.assertNotIn(A, display._cached_props(display.WATCHED_ATOMS))


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


class HybridTimingTests(DisplayFixture):
    """PQ at once; gamma 2.2 only after continuous SDR and a minimum PQ stay."""

    def setUp(self):
        super().setUp()
        self.start(HYBRID, HYBRID_PANEL, hdr_baseline=False, hdr_baseline_session=100.0)
        self.now = 0.0

    def timed(self):
        return [(at, value) for at, _name, value in self.gs.writes]

    def tick(self, at, content=None):
        super().tick(at, **({} if content is None else {A: content}))

    def test_one_pass_decides_from_the_atoms_it_read(self):
        g22, pq = 'no HDR content, panel in gamma 2.2', 'HDR content on screen, panel in PQ'
        wait = 'no HDR content, waiting for a stable switch to gamma 2.2'
        ext = 'external display, not ours to drive'
        # (atoms over HYBRID_PANEL, None = unset; saved baseline; earlier pass: 'docked' or 'same' 2 s before)
        #   -> (writes, reason, hdr_now, saved baseline after)
        cases = [
            ({}, False, None, [], g22, False, False),
            ({A: 1}, False, None, [(E, 1)], pq, True, False),
            ({A: 1, E: 1}, False, None, [], pq, True, False),
            ({E: 1}, False, None, [], wait, True, False),
            ({F: 0}, False, None, [(F, 1)], g22, False, False),
            # Forced with the input unset: output feedback is synthetic, so write the mode.
            ({E: None}, False, 'same', [(E, 0)], g22, False, False),
            ({E: None, A: 1, O: 1}, False, None, [(E, 1)], pq, True, False),
            # Unforced with the input unset: output feedback is the real starting mode.
            ({F: 0, E: None, O: 1}, False, None, [(F, 1)], wait, True, False),
            ({X: 1, O: 1}, False, None, [(F, 0)], ext, True, False),
            ({X: 1, O: 1}, False, 'docked', [], ext, None, False),
            ({X: 2}, False, None, [], 'could not verify the active display', None, False),
            ({E: 1}, None, None, [], wait, True, True),
            ({E: None, O: 1}, None, None, [], wait, True, False),
            # Back from a dock with no baseline: the panel's own output, not the monitor's request.
            ({F: 0, E: 1, O: 0}, None, 'docked', [(F, 1)], wait, True, False),
        ]
        for atoms, baseline, earlier, writes, reason, hdr_now, after in cases:
            with self.subTest(atoms=atoms, baseline=baseline, earlier=earlier):
                case = {k: v for k, v in {**HYBRID_PANEL, **atoms}.items() if v is not None}
                saved = {} if baseline is None else {'hdr_baseline': baseline, 'hdr_baseline_session': 100.0}
                self.start(HYBRID, {**HYBRID_PANEL, X: 1, O: 1} if earlier == 'docked' else case, **saved)
                if earlier:
                    super().tick(8.0)
                self.gs.atoms, self.gs.writes = dict(case), []
                P._state.update(hdr_now=None, hybrid_reason=None)
                super().tick(10.0)
                self.assertEqual(self.gs.modes(), writes)
                self.assertEqual(P._state['hybrid_reason'], reason)
                self.assertEqual(P._state['hdr_now'], hdr_now)
                self.assertIs(self.persisted()['hdr_baseline'], after)

    def test_pq_at_once_and_gamma_only_after_the_hold_and_continuous_sdr(self):
        self.tick(0.0, content=1)
        self.assertEqual(self.timed(), [(0.0, 1)])
        self.tick(0.5, content=0)       # SDR from 0.5: debounce alone would allow 2.5
        self.tick(2.5)
        self.tick(2.9)
        self.assertEqual(self.timed(), [(0.0, 1)], 'PQ is held for 3 s after entering it')
        self.tick(3.0)
        self.assertEqual(self.timed(), [(0.0, 1), (3.0, 0)])

    def test_returning_hdr_restarts_the_sdr_debounce(self):
        self.tick(0.0, content=1)
        self.tick(10.0, content=0)
        self.tick(11.0, content=1)      # back to HDR before 2 s of SDR
        self.tick(11.5, content=0)
        self.tick(13.4)
        self.assertEqual(self.timed(), [(0.0, 1)])
        self.tick(13.5)
        self.assertEqual(self.timed(), [(0.0, 1), (13.5, 0)])

    def test_four_undone_corrections_then_stand_off_for_sixty_seconds(self):
        self.gs.sticks = False          # another writer keeps reverting the mode
        self.gs.atoms[A] = 1
        for second in range(8):
            self.tick(float(second))
        self.assertEqual(self.timed(), [(0.0, 1), (1.0, 1), (2.0, 1), (3.0, 1)])
        self.assertIn('standing off', P._state['hybrid_reason'])
        self.tick(4.0 + display.HDR_BACKOFF_S - 0.1)
        self.assertEqual(len(self.gs.writes), 4)
        self.tick(4.0 + display.HDR_BACKOFF_S)
        self.assertEqual(self.timed()[-1], (4.0 + display.HDR_BACKOFF_S, 1))

    def test_undone_gamma_switches_also_stand_off(self):
        self.gs.sticks = False          # another writer keeps putting the panel back into PQ
        self.gs.atoms[E] = 1
        for step in range(60):
            self.tick(step / 2)
        self.assertEqual(self.timed(), [(2.0, 0), (4.5, 0), (7.0, 0), (9.5, 0)])
        self.assertIn('standing off', P._state['hybrid_reason'])

    def test_a_writer_that_undoes_us_a_pass_later_still_ends_in_a_stand_off(self):
        # Our write sticks for one pass, so the panel reads as settled before it is undone.
        self.gs.atoms[A] = 1
        for second in range(40):
            self.tick(float(second))
            if self.gs.atoms[E] == 1 and second % 2 == 0:
                self.gs.atoms[E] = 0    # reverted after our pass
        self.assertEqual(len(self.gs.writes), display.HDR_FIGHT_LIMIT)
        self.assertIn('standing off', P._state['hybrid_reason'])

    def test_a_display_hand_off_starts_the_argument_afresh(self):
        self.gs.sticks = False          # another writer keeps reverting the mode
        self.gs.atoms[A] = 1
        for second in range(4):
            self.tick(float(second))
        self.assertEqual(len(self.gs.writes), 4)
        self.gs.atoms[X] = 1
        self.tick(4.0)
        self.gs.atoms.update({X: 0, F: 1, E: 0})
        self.tick(5.0)
        self.assertEqual(self.timed()[-1], (5.0, 1))  # corrected again, not a stand-off
        self.assertNotIn('standing off', P._state['hybrid_reason'])

    def test_corrections_far_apart_or_to_another_target_never_stand_off(self):
        self.gs.atoms[A] = 1
        for n in range(8):                  # one undone switch every 40 s
            at = n * 40.0
            self.gs.atoms[E] = 0
            self.tick(at)
            self.assertEqual(self.timed()[-1], (at, 1))
        self.assertNotIn('standing off', P._state['hybrid_reason'])


if __name__ == '__main__':
    unittest.main()

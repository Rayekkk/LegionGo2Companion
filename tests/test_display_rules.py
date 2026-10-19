# SPDX-License-Identifier: BSD-3-Clause
"""Pure EDID rewriting and the Hybrid PQ / gamma 2.2 switching rules."""
from contextlib import ExitStack
import types
import unittest
from unittest.mock import patch

import support  # Decky/Linux doubles before importing the backend.
import display_backend as display
from test_display_native_edid import edid_fixture


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

    def test_panel_id_is_decoded_only_from_a_valid_header(self):
        original, _ = edid_fixture()
        self.assertEqual(display._decode_panel_id(original), ('SDC', 0x4301))
        self.assertIn(display._decode_panel_id(original), display.KNOWN_PANELS)
        self.assertIsNone(display._decode_panel_id(original[:1] + b'\0' + original[2:]))
        self.assertIsNone(display._decode_panel_id(original[:127]))


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

    def test_failing_edid_pass_does_not_starve_hybrid_or_repeat_the_gate(self):
        import asyncio
        counts = {"gate": 0, "hybrid": 0}

        def gate():
            counts["gate"] += 1
            return False

        def hybrid():
            counts["hybrid"] += 1

        def edid():
            raise IndexError("index out of range")

        state = dict(display.Plugin._state, active_mode="hybrid")

        async def run():
            task = asyncio.create_task(display.Plugin()._loop(""))
            await asyncio.sleep(1.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with patch.object(display.Plugin, "_state", state),                 patch.object(display.Plugin, "_refresh_setup", side_effect=lambda: None),                 patch.object(display.Plugin, "_refresh_gate", side_effect=gate),                 patch.object(display.Plugin, "_hybrid_pass", side_effect=hybrid),                 patch.object(display.Plugin, "_edid_pass", side_effect=edid),                 patch.object(display.Plugin, "native_display_active", return_value=True):
            asyncio.run(run())
        self.assertGreater(counts["hybrid"], 0)
        self.assertLessEqual(counts["gate"], 1)


if __name__ == '__main__':
    unittest.main()

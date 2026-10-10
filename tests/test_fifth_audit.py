# SPDX-License-Identifier: BSD-3-Clause
"""Fresh OLED safety reads must preserve the complete watcher snapshot."""
import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, patch

import test_second_hardware_audit as fixtures
import display_backend as display
import test_modules as module_fixtures
import main
import vibration_backend as vibe
import test_display_reset as reset_fixtures


REAL_SESSION_PROPS = display.Plugin._session_props


class VibrationResetStateTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.VibrationProfileContextTests.asyncSetUp
    asyncTearDown = fixtures.VibrationProfileContextTests.asyncTearDown
    persisted = fixtures.VibrationProfileContextTests.persisted

    async def test_failed_reset_hardware_apply_returns_committed_settings(self):
        with patch.object(vibe, 'scan_proc_for_appid', return_value='111'), \
                patch.object(vibe, '_apply_settings', return_value=False):
            result = await self.plugin.reset_to_default('111', '111')
        self.assertFalse(result['success'])
        self.assertEqual(result['settings'], vibe.DEFAULT_PROFILE)
        self.assertEqual(result['settings'], self.persisted()['111']['settings'])
        self.assertEqual((result['app_id'], result['profile_id'], result['overwrite']), ('111', '111', True))


class ModuleEventLoopTests(unittest.IsolatedAsyncioTestCase):
    setUp = module_fixtures.ModuleGateTests.setUp
    make_plugin = module_fixtures.ModuleGateTests.make_plugin
    asyncTearDown = module_fixtures.ModuleGateTests.asyncTearDown

    def held_io(self, observations, result=None):
        loop = asyncio.get_running_loop()
        def run():
            heartbeat = threading.Event()
            loop.call_soon_threadsafe(heartbeat.set)
            observations.append(heartbeat.wait(.5))
            return result
        return run

    async def test_slow_session_status_probe_keeps_event_loop_responsive(self):
        self.plugin._module_state['display'] = {'enabled': False, 'session': 100.0}
        observed = []
        with patch.object(display, '_gamescope_start_time', side_effect=self.held_io(observed, 200.0)):
            for operation in (self.plugin.modules_get_status, self.plugin.get_version, self.plugin._publish_modules):
                await operation()
        self.assertEqual(observed, [True, True, True], 'Status probes must allow an event-loop heartbeat while I/O waits.')

    async def test_slow_module_commit_keeps_event_loop_responsive_and_durable(self):
        observed = []
        original_commit = self.plugin._module_store.commit
        held = self.held_io(observed)
        def commit():
            held()
            return original_commit()
        with patch.object(self.plugin._module_store, 'commit', side_effect=commit), \
                patch.object(main, 'withdraw', new=AsyncMock(return_value='restored')):
            result = await self.plugin.modules_set_enabled('battery', False)
        self.assertTrue(observed)
        self.assertTrue(all(observed), 'Durable module commits must not block the event loop.')
        self.assertFalse(result['battery']['enabled'])
        self.assertFalse(self.make_plugin()._module_state['battery']['enabled'])

    async def test_display_withdrawal_session_probe_keeps_event_loop_responsive(self):
        observed = []
        with patch.object(display, '_gamescope_start_time', side_effect=self.held_io(observed, 200.0)), \
                patch.object(main, 'withdraw', new=AsyncMock(return_value='Restart Game Mode')):
            result = await self.plugin.modules_set_enabled('display', False)
        self.assertTrue(observed)
        self.assertTrue(all(observed), 'Recording the withdrawn session must also allow a heartbeat.')
        self.assertEqual(self.plugin._module_state['display']['session'], 200.0)
        self.assertFalse(result['display']['enabled'])

    async def test_cancelled_module_change_finishes_commit_before_withdrawal(self):
        entered, release = threading.Event(), threading.Event()
        original_commit = self.plugin._module_store.commit
        def commit():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test commit timeout')
            return original_commit()
        with patch.object(self.plugin._module_store, 'commit', side_effect=commit), \
                patch.object(main, 'withdraw', new=AsyncMock(return_value='restored')) as withdrawal:
            task = asyncio.create_task(self.plugin.modules_set_enabled('battery', False))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                withdrawal.assert_not_awaited()
            finally:
                release.set()
                result = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        withdrawal.assert_awaited_once()
        saved = self.make_plugin()._module_state['battery']
        self.assertFalse(saved['enabled'])
        self.assertFalse(saved.get('pending'))


class DisplayWatcherPerformanceTests(unittest.TestCase):
    setUp = fixtures.DisplayBaselineTests.setUp

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

    def test_forward_fresh_guard_preserves_watcher_cache(self):
        self.prepare_cache()
        with patch.object(display, '_read_props_uncached', return_value=dict(self.props)) as read, \
                patch.object(display, '_write_nits', return_value=True):
            self.assertTrue(display.Plugin._forward_nits(120.0))
            read.assert_called_once()
            # An incremental xprop notification must update a complete cache,
            # not turn it into a partial snapshot after every brightness write.
            display._update_prop_cache(f'{display.ATOM_SDR_NITS}(CARDINAL) = {display._float_raw(120)}',
                                       display.DISPLAY)
            props = display.Plugin._session_props([display.ATOM_IS_EXTERNAL, display.ATOM_HDR_OUT])
            self.assertEqual(props[display.ATOM_IS_EXTERNAL], 0)
            self.assertEqual(props[display.ATOM_HDR_OUT], 1)
            self.assertEqual(read.call_count, 1, 'Gate reads reuse the healthy watcher after forwarding.')

    def test_release_fresh_guard_preserves_watcher_cache(self):
        self.prepare_cache()
        display.Plugin._last_written = 250.0
        with patch.object(display, '_read_props_uncached', return_value=dict(self.props)) as read, \
                patch.object(display, '_write_nits', return_value=True):
            self.assertTrue(display.Plugin._release())
            self.assertIsNotNone(display._cached_props([display.ATOM_IS_EXTERNAL]))
            display.Plugin._session_props([display.ATOM_IS_EXTERNAL, display.ATOM_HDR_OUT])
            self.assertEqual(read.call_count, 1, 'Releasing ownership must not erase the watcher snapshot.')

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

    def test_stable_hybrid_tick_reuses_cache_without_hardware_writes(self):
        self.prepare_cache()
        self.state.update(active_mode=display.MODE_HYBRID, panel_mode=display.MODE_HYBRID)
        display.Plugin._hdr_baseline = False
        display._update_prop_cache(f'{display.ATOM_FORCE_HDR_SUPPORT}(CARDINAL) = 1', display.DISPLAY)
        display._update_prop_cache(f'{display.ATOM_HDR_APP}(CARDINAL) = 1', display.DISPLAY)
        with patch.object(display, '_read_props_uncached') as read, \
                patch.object(display, '_write_atom_int') as writing:
            display.Plugin._hybrid_pass()
        read.assert_not_called()
        writing.assert_not_called()

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


class DisplayDockCleanupTests(unittest.TestCase):
    setUp = reset_fixtures.DisplayResetTests.setUp
    reset = reset_fixtures.DisplayResetTests.reset

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

# SPDX-License-Identifier: BSD-3-Clause
"""Vibration driven through its RPCs against a fake hid-lenovo-go attribute tree and a real settings store."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, Mock, mock_open
import asyncio
from concurrent.futures import ThreadPoolExecutor
import struct
import threading
import io
import builtins
import copy
import types
import os

import support  # noqa: F401 - installs the fake decky module
from safe_settings import AtomicSettingsManager
import vibration_backend as vibe
import module_runtime
import active_app

DRIVER = {'rumble_intensity': 'high', 'left_handle/rumble_mode': 'standard',
          'right_handle/rumble_mode': 'racing', 'touchpad/vibration_intensity': 'low',
          'touchpad/vibration_enabled': 'false'}
DEFAULTS = {'rumble_intensity': 'medium', 'left_handle/rumble_mode': 'fps',
            'right_handle/rumble_mode': 'fps', 'touchpad/vibration_intensity': 'medium',
            'touchpad/vibration_enabled': 'true'}
# Global level 2 (medium); game 111 overrides with level 0 (off), game 222 with level 1 (low).
PROFILES = {key: {'overwrite': key != '0', 'settings': {**vibe.DEFAULT_PROFILE, 'level': level}}
            for key, level in (('0', 2), ('111', 0), ('222', 1))}


class FakeDevice(unittest.IsolatedAsyncioTestCase):
    """Fake attribute tree, a real settings store, a controllable clock and running game.

    `self.ignored` holds attributes whose writes the driver accepts but does not apply.
    """
    driver = DRIVER
    profiles = None  # game_profiles to seed; None leaves a fresh store

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.device = self.root / 'hid'
        for name, value in self.driver.items():
            path = self.device / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding='ascii')
        self.store = AtomicSettingsManager('vibration_settings', str(self.root / 'settings'))
        if self.profiles is not None:
            self.store.replace({'schema_version': 2, 'game_profiles': copy.deepcopy(self.profiles)})
        self.clock = 100.0
        self.running = ''
        self.writes = []
        self.ignored = set()
        real_open = builtins.open

        def opened(path, mode='r', *args, **kwargs):
            if 'w' in mode and Path(path).is_relative_to(self.device):
                relative = Path(path).relative_to(self.device).as_posix()
                self.writes.append(relative)
                if relative in self.ignored:
                    return io.StringIO()
            return real_open(path, mode, *args, **kwargs)
        for name, value in {
            'settings': self.store, '_device_path': str(self.device), '_attr_cache': {},
            '_capabilities_cache': {}, '_last_applied_values': None,
            '_active_app_id': '', '_active_app_ts': None, '_drift_interval': vibe._DRIFT_INTERVAL_S,
            '_stopping': threading.Event(),
        }.items():
            self.stack.enter_context(patch.object(vibe, name, value))
        self.stack.enter_context(patch.object(vibe, 'time', types.SimpleNamespace(
            monotonic=lambda: self.clock, sleep=vibe.time.sleep)))
        self.scan = self.stack.enter_context(patch.object(
            vibe, 'scan_proc_for_appid', side_effect=lambda: self.running))
        self.stack.enter_context(patch.object(vibe, '_discover', return_value=(str(self.device), 'fixture')))
        self.stack.enter_context(patch('builtins.open', side_effect=opened))
        self.stack.enter_context(patch.object(vibe.module_runtime, 'CONTROLLER_READ_GAP_S', 0))
        self.plugin = vibe.Plugin()

    async def asyncTearDown(self):
        await module_runtime.drain('vibration')

    def hardware(self):
        return {name: (self.device / name).read_text(encoding='ascii').strip() for name in DRIVER}

    def intensity(self):
        return self.hardware()['rumble_intensity']

    def persisted(self):
        return AtomicSettingsManager('vibration_settings', str(self.root / 'settings')).getSetting('game_profiles')

    def lookups(self, *answers):
        """Running-game answers for successive process scans: a second scan would see a different game."""
        return patch.object(vibe, 'scan_proc_for_appid', side_effect=list(answers))


class VibrationBaselineTests(FakeDevice):
    def test_fresh_install_restores_the_driver_values_found_before_the_first_write(self):
        vibe._migrate()
        self.assertEqual(self.store.getSetting('baseline'), {'pending': True})
        self.assertTrue(vibe._apply_settings(dict(vibe.DEFAULT_PROFILE)))
        self.assertEqual(self.hardware(), DEFAULTS)
        self.assertEqual(self.store.getSetting('baseline'), DRIVER)
        self.assertTrue(vibe._restore_baseline())
        self.assertEqual(self.hardware(), DRIVER)

    def test_release_before_the_first_write_keeps_the_untouched_driver_values(self):
        vibe._migrate()
        self.assertTrue(vibe._restore_baseline())
        self.assertEqual(self.hardware(), DRIVER)

    def test_release_writes_only_what_differs_and_never_a_mode_it_cannot_read(self):
        # Every update and uninstall releases; the pads buzz whenever their rumble mode is set, even unchanged.
        vibe._migrate()
        self.assertTrue(vibe._apply_settings(dict(vibe.DEFAULT_PROFILE)))
        modes = ('left_handle/rumble_mode', 'right_handle/rumble_mode')
        for name in modes:
            (self.device / name).write_text(DRIVER[name], encoding='ascii')
        self.writes.clear()
        self.assertTrue(vibe._restore_baseline())
        self.assertEqual(self.hardware(), DRIVER)
        self.assertEqual(self.writes, ['rumble_intensity', 'touchpad/vibration_intensity', 'touchpad/vibration_enabled'])
        self.assertTrue(vibe._apply_settings(dict(vibe.DEFAULT_PROFILE)))
        self.writes.clear()
        real_read, flips = vibe._read_attr, iter(range(10 ** 6))
        unsettled = lambda path: real_read(path) + str(next(flips)) if 'rumble_mode' in path else real_read(path)
        with patch.object(vibe, '_read_attr', side_effect=unsettled):
            self.assertFalse(vibe._restore_baseline(), 'an unread mode is not confirmed')
        self.assertFalse(set(modes) & set(self.writes))

    def test_existing_install_and_invalid_baseline_fall_back_to_the_default_profile(self):
        for baseline in (None, {**DRIVER, 'rumble_intensity': 'loud'}):
            with self.subTest(baseline=baseline):
                payload = {'schema_version': 2, 'game_profiles': {}}
                if baseline is not None:
                    payload['baseline'] = baseline
                self.store.replace(payload)
                vibe._migrate()
                self.assertTrue(vibe._apply_settings({**vibe.DEFAULT_PROFILE, 'level': 0}))
                self.assertTrue(vibe._restore_baseline())
                self.assertEqual(self.hardware(), DEFAULTS)
        self.store.replace({'schema_version': 2})
        vibe._migrate()
        self.assertEqual(self.store.getSetting('baseline'), {})

    def test_partial_profile_failure_attempts_and_confirms_other_attributes(self):
        self.ignored.add('right_handle/rumble_mode')
        self.assertFalse(vibe._apply_settings(dict(vibe.DEFAULT_PROFILE), str(self.device), True))
        self.assertEqual(self.hardware(), {**DEFAULTS, 'right_handle/rumble_mode': 'racing'})


class VibrationEditTests(unittest.TestCase):
    def test_vibration_concurrent_fields_preserve_both_edits(self):
        with tempfile.TemporaryDirectory() as raw:
            manager = AtomicSettingsManager("vibration_settings", raw)
            manager.replace({"game_profiles": {"0": {
                "overwrite": False, "settings": dict(vibe.DEFAULT_PROFILE)}}})
            actual_load = vibe._load_profiles
            def simultaneous_read():
                snapshot = actual_load()
                __import__("time").sleep(0.03)
                return snapshot
            with patch.object(vibe, "settings", manager), \
                 patch.object(vibe, "_active_app_id", "0"), \
                 patch.object(vibe, "_load_profiles", side_effect=simultaneous_read):
                with ThreadPoolExecutor(max_workers=2) as workers:
                    a = workers.submit(vibe._update_active, "level", 3)
                    b = workers.submit(vibe._update_active, "touchpadEnabled", False)
                    a.result(timeout=5)
                    b.result(timeout=5)
                manager.read()
                saved = manager.getSetting("game_profiles")["0"]["settings"]
            self.assertEqual((saved["level"], saved["touchpadEnabled"]), (3, False))

    def test_saved_profile_levels_are_clamped(self):
        for raw, expected in ((9, 3), (4, 3), (3, 3), (0, 0), (-5, 0)):
            with self.subTest(raw=raw):
                profile = vibe._coerce_profile({vibe.PKEY_LEVEL: raw, vibe.PKEY_TP_INT: raw})
                self.assertEqual((profile[vibe.PKEY_LEVEL], profile[vibe.PKEY_TP_INT]), (expected, expected))


class VibrationProfileTests(FakeDevice):
    profiles = PROFILES

    async def test_live_requests_are_clamped_before_they_are_stored_and_written(self):
        for method, key, attr in (('set_intensity', vibe.PKEY_LEVEL, 'rumble_intensity'),
                                  ('set_touchpad_intensity', vibe.PKEY_TP_INT, 'touchpad/vibration_intensity')):
            for raw, expected, written in ((9, 3, 'high'), (4, 3, 'high'), (-5, 0, 'off')):
                with self.subTest(method=method, raw=raw):
                    self.assertTrue((await getattr(self.plugin, method)(raw))['success'])
                    self.assertEqual(self.persisted()['0']['settings'][key], expected)
                    self.assertEqual(self.hardware()[attr], written)

    async def test_expected_game_and_mutation_share_one_process_lookup(self):
        with self.lookups('111', '222'):
            result = await self.plugin.set_intensity(3, '111', '111')
        self.assertTrue(result['success'])
        self.assertEqual((result['app_id'], result['profile_id']), ('111', '111'))
        saved = self.persisted()
        self.assertEqual(saved['111']['settings']['level'], 3)
        self.assertEqual(saved['222']['settings']['level'], 1)
        self.assertEqual(self.intensity(), 'high')

    async def test_expected_global_profile_cannot_be_replaced_by_new_game_override_between_reads(self):
        self.store.replace({'game_profiles': {**PROFILES, '111': {**PROFILES['111'], 'overwrite': False}}})
        with self.lookups('111', '222'):
            result = await self.plugin.set_touchpad_enabled(False, '111', '0')
        self.assertTrue(result['success'])
        saved = self.persisted()
        self.assertFalse(saved['0']['settings']['touchpadEnabled'])
        self.assertTrue(saved['222']['settings']['touchpadEnabled'])
        self.assertEqual(self.hardware()['touchpad/vibration_enabled'], 'false')

    async def test_overwrite_change_rejects_queued_edit_without_saving_or_writing(self):
        self.store.replace({'game_profiles': {**PROFILES, '111': {**PROFILES['111'], 'overwrite': False}}})
        before = self.persisted()
        self.running = '111'
        result = await self.plugin.set_intensity(3, '111', '111')
        self.assertFalse(result['success'])
        self.assertEqual(self.persisted(), before)
        self.assertEqual((self.writes, self.hardware()), ([], DRIVER))

    async def test_stale_reset_rejects_game_change_without_saving_or_writing(self):
        before = self.persisted()
        self.running = '222'
        result = await self.plugin.reset_to_default('111', '111')
        self.assertFalse(result['success'])
        self.assertEqual(result['profile_id'], '222')
        self.assertEqual(self.persisted(), before)
        self.assertEqual((self.writes, self.hardware()), ([], DRIVER))

    async def test_stale_reset_rejects_overwrite_change_for_the_same_game(self):
        self.store.replace({'game_profiles': {**PROFILES, '111': {**PROFILES['111'], 'overwrite': False}}})
        before = self.persisted()
        self.running = '111'
        result = await self.plugin.reset_to_default('111', '111')
        self.assertFalse(result['success'])
        self.assertEqual(result['profile_id'], '0')
        self.assertEqual(self.persisted(), before)
        self.assertEqual((self.writes, self.hardware()), ([], DRIVER))

    async def test_reset_uses_checked_context_once_and_remains_compatible_without_arguments(self):
        with self.lookups('111', '222'):
            result = await self.plugin.reset_to_default('111', '111')
            self.assertEqual(self.persisted()['111']['settings'], vibe.DEFAULT_PROFILE)
            self.assertEqual(self.persisted()['222']['settings']['level'], 1)
            self.assertEqual(self.hardware(), DEFAULTS)
            (self.device / 'rumble_intensity').write_text('off', encoding='ascii')
            compatible = await self.plugin.reset_to_default()
        self.assertTrue(result['success'])
        self.assertTrue(compatible['success'])
        self.assertEqual(compatible['profile_id'], '222')
        self.assertEqual(self.persisted()['222']['settings'], vibe.DEFAULT_PROFILE)
        self.assertEqual(self.hardware(), DEFAULTS)

    async def test_failed_reset_hardware_apply_returns_committed_settings(self):
        self.running = '111'
        self.ignored.add('rumble_intensity')
        result = await self.plugin.reset_to_default('111', '111')
        self.assertFalse(result['success'])
        self.assertEqual(result['settings'], vibe.DEFAULT_PROFILE)
        self.assertEqual(result['settings'], self.persisted()['111']['settings'])
        self.assertEqual((result['app_id'], result['profile_id'], result['overwrite']), ('111', '111', True))

    async def test_rejected_edit_goes_back_to_the_value_the_controller_has(self):
        # Game 222's own profile, then the global one; a stuck other attribute fails the
        # whole apply even though the edited one was written.
        for game, profile, rejected in (('222', '222', 'right_handle/rumble_mode'), ('', '0', 'rumble_intensity')):
            with self.subTest(game=game, rejected=rejected):
                self.running = game
                self.assertTrue((await self.plugin.set_intensity(1))['success'])
                if rejected != 'rumble_intensity':
                    (self.device / rejected).write_text('racing', encoding='ascii')
                self.ignored = {rejected}
                result = await self.plugin.set_intensity(3)
                self.assertFalse(result['success'])
                self.assertEqual(result['settings']['level'], 1)
                self.assertEqual((await self.plugin.get_settings())['settings']['level'], 1)
                self.assertEqual(self.persisted()[profile]['settings']['level'], 1)
                self.assertEqual(self.intensity(), 'low')
                if game:
                    self.assertEqual(self.persisted()['0']['settings']['level'], 2)
                self.ignored = set()

    async def test_edit_without_a_controller_is_kept_for_when_it_appears(self):
        with patch.object(vibe, '_get_device_path', return_value=None):
            result = await self.plugin.set_intensity(3)
            self.assertEqual((await self.plugin.get_settings())['settings']['level'], 3)
            reset = await self.plugin.reset_to_default()
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], vibe.NOT_CONNECTED)
        self.assertEqual(result['settings']['level'], 3)
        self.assertEqual((reset['success'], reset['error'], reset['settings']),
                         (False, vibe.NOT_CONNECTED, vibe.DEFAULT_PROFILE))
        self.assertEqual(self.persisted()['0']['settings'], vibe.DEFAULT_PROFILE)
        # Both UIs show this text as is; the Desktop app and Decky read it from vibration.mjs too.
        shared = (Path(__file__).resolve().parents[1] / 'desktop/native/vibration.mjs').read_text(encoding='utf-8')
        self.assertIn(f'const NOT_CONNECTED = "{vibe.NOT_CONNECTED}";', shared)

    async def test_profile_toggle_reports_committed_scope_when_hardware_apply_fails(self):
        self.running = '111'
        self.ignored.add('rumble_intensity')
        result = await self.plugin.set_profile_overwrite('111', False)
        self.assertFalse(result['success'])
        self.assertEqual((result['app_id'], result['profile_id'], result['overwrite']), ('111', '0', False))
        self.assertFalse(self.persisted()['111']['overwrite'])

    async def test_toggle_of_inactive_profile_returns_actual_running_scope(self):
        with self.lookups('222', '111'):
            result = await self.plugin.set_profile_overwrite('111', False)
        self.assertEqual((result['app_id'], result['profile_id'], result['overwrite']), ('222', '222', True))
        self.assertEqual(self.intensity(), 'low')

    async def test_new_profile_is_seeded_from_the_single_admitted_running_snapshot(self):
        with self.lookups('222', '111'):
            result = await self.plugin.set_profile_overwrite('333', True)
        self.assertEqual(self.persisted()['333']['settings'], PROFILES['222']['settings'])
        self.assertEqual(result['app_id'], '222')


class VibrationCapacityTests(FakeDevice):
    def fill(self, count):
        self.store.replace({'game_profiles': {str(i): {
            'overwrite': i != 0, 'settings': dict(vibe.DEFAULT_PROFILE), 'name': f'Game {i}'
        } for i in range(count)}})

    async def test_capacity_rejects_creation_before_commit_or_hardware_write(self):
        self.fill(vibe.MAX_GAME_PROFILES)
        before = self.persisted()
        self.running = '9999'
        result = await self.plugin.set_profile_overwrite('9999', True)
        self.assertFalse(result['success'])
        self.assertIn('limit', result['error'])
        self.assertEqual(result['profile_id'], '0')
        self.assertEqual(self.persisted(), before)
        self.assertEqual(self.writes, [])

    async def test_existing_profiles_can_be_edited_at_capacity(self):
        self.fill(vibe.MAX_GAME_PROFILES)
        self.running = '111'
        self.assertTrue((await self.plugin.set_intensity(3, '111', '111'))['success'])
        saved = self.persisted()
        self.assertEqual(len(saved), vibe.MAX_GAME_PROFILES)
        self.assertEqual(saved['111']['settings']['level'], 3)
        self.assertEqual(self.intensity(), 'high')

    async def test_legacy_over_capacity_store_survives_unrelated_edit(self):
        self.fill(vibe.MAX_GAME_PROFILES + 1)
        before = self.persisted()
        self.assertTrue((await self.plugin.set_intensity(3, '0', '0'))['success'])
        after = self.persisted()
        self.assertEqual(set(after), set(before))
        self.assertEqual(after[str(vibe.MAX_GAME_PROFILES)], before[str(vibe.MAX_GAME_PROFILES)])


class VibrationEndpointTests(unittest.TestCase):
    def test_unknown_hid_identity_never_falls_back_to_another_pad(self):
        with patch.object(vibe, '_ff_device_cache', {'node': None}), \
                patch.object(vibe, '_get_device_path', return_value=None), \
                patch.object(vibe, '_event_nodes', return_value=['/dev/input/event9']), \
                patch.object(vibe, '_has_rumble', return_value=True):
            self.assertIsNone(vibe._find_ff_device())

    def test_cached_event_number_reused_by_foreign_pad_is_rejected(self):
        nodes = ['/dev/input/event4', '/dev/input/event9']
        ids = {nodes[0]: ('045e', '02ea'), nodes[1]: ('17ef', '61eb')}
        with patch.object(vibe, '_ff_device_cache', {'node': nodes[0]}), \
                patch.object(vibe.os.path, 'exists', return_value=True), \
                patch.object(vibe, '_get_device_path', return_value='/sys/hid/0003:17EF:61EB.0001'), \
                patch.object(vibe, '_event_nodes', return_value=nodes), \
                patch.object(vibe, '_node_ids', side_effect=ids.get), \
                patch.object(vibe, '_has_rumble', return_value=True):
            self.assertEqual(vibe._find_ff_device(), nodes[1])

    def test_probe_opens_only_matching_controller_endpoints(self):
        nodes = ['/dev/input/event4', '/dev/input/event9']
        ids = {nodes[0]: ('045e', '02ea'), nodes[1]: ('17ef', '61eb')}
        with patch.object(vibe, '_ff_device_cache', {'node': None}), \
                patch.object(vibe, '_get_device_path', return_value='/sys/hid/0003:17EF:61EB.0001'), \
                patch.object(vibe, '_event_nodes', return_value=nodes), \
                patch.object(vibe, '_node_ids', side_effect=ids.get), \
                patch.object(vibe, '_has_rumble', return_value=True) as probe:
            self.assertEqual(vibe._find_ff_device(), nodes[1])
            probe.assert_called_once_with(nodes[1])


class VibrationDescriptorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await module_runtime.drain('vibration')

    @contextmanager
    def playable(self, *, pause=None, start_error=False, id_error=False, ids=(0x17ef, 0x61eb)):
        def ioctl(_fd, request, payload):
            if request == vibe._EVIOCGID:
                if id_error:
                    raise OSError('descriptor disappeared')
                payload[:] = struct.pack('HHHH', 3, *ids, 1)
            elif request == vibe._EVIOCSFF:
                struct.pack_into('<h', payload, 2, 1)
            return 0
        def output(_fd, payload):
            if start_error and struct.unpack('<qqHHi', payload)[-1] == 1:
                raise OSError('effect start failed')
            return len(payload)
        with ExitStack() as stack:
            for name, value in {'_ff_busy': False, '_ff_device_cache': {'node': '/dev/input/event9'}}.items():
                stack.enter_context(patch.object(vibe, name, value))
            stack.enter_context(patch.object(vibe, '_active_values', return_value={vibe.PKEY_LEVEL: 2}))
            stack.enter_context(patch.object(vibe, '_find_ff_device', return_value='/dev/input/event9'))
            stack.enter_context(patch.object(vibe, '_get_device_path', return_value='/sys/hid/0003:17EF:61EB.0001'))
            stack.enter_context(patch.object(vibe.os, 'open', return_value=43))
            close = stack.enter_context(patch.object(vibe.os, 'close'))
            writes = stack.enter_context(patch.object(vibe.os, 'write', side_effect=output))
            calls = stack.enter_context(patch.object(vibe.fcntl, 'ioctl', side_effect=ioctl, create=True))
            stack.enter_context(patch.object(vibe.time, 'sleep', side_effect=pause))
            yield close, writes, calls

    async def test_matching_descriptor_plays_and_cleans_effect_off_event_loop(self):
        loop = asyncio.get_running_loop()
        observed = []
        def pause(_seconds):
            heartbeat = threading.Event()
            loop.call_soon_threadsafe(heartbeat.set)
            observed.append(heartbeat.wait(.5))
        with self.playable(pause=pause) as (close, writes, calls):
            result = await vibe.Plugin().test_vibration(100)
            self.assertTrue(result['success'], result)
            self.assertEqual(observed, [True])
            self.assertEqual([struct.unpack('<qqHHi', call.args[1])[-1] for call in writes.call_args_list], [1, 0])
            self.assertEqual([call.args[1] for call in calls.call_args_list], [vibe._EVIOCGID, vibe._EVIOCSFF, vibe._EVIOCRMFF])
            close.assert_called_once_with(43)
            self.assertFalse(vibe._ff_busy)

    async def test_failed_start_still_stops_erases_and_closes_effect(self):
        with self.playable(start_error=True) as (close, writes, calls):
            result = await vibe.Plugin().test_vibration(100)
            self.assertFalse(result['success'])
            self.assertEqual(writes.call_count, 2)
            self.assertEqual(calls.call_args_list[-1].args, (43, vibe._EVIOCRMFF, 1))
            close.assert_called_once_with(43)

    async def test_identity_read_failure_closes_without_hardware_output(self):
        with self.playable(id_error=True) as (close, writes, calls):
            result = await vibe.Plugin().test_vibration(100)
            self.assertFalse(result['success'])
            writes.assert_not_called()
            self.assertEqual(calls.call_count, 1)
            close.assert_called_once_with(43)

    async def test_test_rechecks_open_descriptor_before_upload_or_output(self):
        self.assertEqual(vibe._EVIOCGID, 0x80084502)  # struct input_id from Linux UAPI.
        with self.playable(ids=(0x045e, 0x02ea)) as (close, writes, calls):
            result = await vibe.Plugin().test_vibration(100)
        self.assertFalse(result['success'], 'A reused descriptor must be rejected before its device receives output.')
        writes.assert_not_called()
        self.assertNotIn(vibe._EVIOCSFF, [call.args[1] for call in calls.call_args_list])
        close.assert_called_once_with(43)

    async def test_repeated_cancellation_waits_for_effect_cleanup_and_keeps_busy_guard(self):
        entered, release = threading.Event(), threading.Event()
        def pause(_seconds):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test effect timeout')
        with self.playable(pause=pause) as (close, writes, calls):
            task = asyncio.create_task(vibe.Plugin().test_vibration(100))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertTrue(vibe._ff_busy)
                self.assertFalse((await vibe.Plugin().test_vibration(100))['success'])
                close.assert_not_called()
            finally:
                release.set()
                result = await asyncio.gather(task, return_exceptions=True)
            self.assertIsInstance(result[0], asyncio.CancelledError)
            self.assertFalse(vibe._ff_busy)
            self.assertEqual(writes.call_count, 2)
            self.assertEqual(calls.call_args_list[-1].args, (43, vibe._EVIOCRMFF, 1))
            close.assert_called_once_with(43)


class GameLeaseTests(unittest.TestCase):
    def test_fresh_reports_expire_and_clock_rewinds_do_not_extend_them(self):
        fallback = Mock(return_value='222')
        self.assertEqual(active_app.resolve_running_app('111', 100, 111, fallback), '111')
        fallback.assert_not_called()
        self.assertEqual(active_app.resolve_running_app('111', 100, 112, fallback), '222')
        self.assertEqual(active_app.resolve_running_app('111', 100, 90, fallback), '222')

    def test_recent_game_exit_is_authoritative_before_fallback(self):
        fallback = Mock(return_value='111')
        self.assertEqual(active_app.resolve_running_app('0', 100, 105, fallback), '')
        fallback.assert_not_called()
        self.assertEqual(active_app.resolve_running_app('0', 100, 113, fallback), '111')

    def test_process_fallback_prefers_valid_reaper_and_ignores_bad_ids(self):
        with tempfile.TemporaryDirectory() as raw:
            bad, reaper, environment = [Path(raw, name) for name in ('bad', 'reaper', 'environment')]
            bad.write_bytes(b'AppId=invalid\x00AppId=0\x00')
            reaper.write_bytes(b'reaper\x00AppId=111\x00')
            environment.write_bytes(b'SteamAppId=222\x00')
            with patch.object(active_app.glob, 'glob', side_effect=[
                    [str(bad), str(reaper)], [str(environment)]]):
                self.assertEqual(active_app.scan_proc_for_appid(), '111')
            with patch.object(active_app.glob, 'glob', side_effect=[
                    [str(bad), str(Path(raw, 'missing'))], [str(environment)]]):
                self.assertEqual(active_app.scan_proc_for_appid(), '222')


class VibrationForegroundTests(FakeDevice):
    driver = DEFAULTS
    profiles = {key: {'overwrite': key != '0', 'settings': {
        'level': level, 'mode': mode, 'touchpadIntensity': 2, 'touchpadEnabled': True}}
        for key, level, mode in (('0', 2, 0), ('111', 0, 0), ('222', 3, 1))}

    def setUp(self):
        super().setUp()
        self.running = '111'

    async def test_vibration_uses_current_process_or_global_after_report_expires(self):
        await self.plugin.set_active_app('111')
        self.clock = 113
        self.running = ''
        payload = await self.plugin.get_settings()
        self.assertEqual((payload['app_id'], payload['profile_id'], payload['settings']['level']), ('0', '0', 2))
        self.running = '111'
        self.assertEqual((await self.plugin.get_settings())['settings']['level'], 0,
                         'A still-running game keeps its profile')
        self.running = '222'
        payload = await self.plugin.get_settings()
        self.assertEqual((payload['app_id'], payload['settings']['level']), ('222', 3))

    async def test_unchanged_heartbeat_renews_lease_without_another_hardware_write_or_process_scan(self):
        await self.plugin.set_active_app('111')
        self.scan.reset_mock()
        count = len(self.writes)
        self.clock = 106
        result = await self.plugin.set_active_app('111')
        self.assertTrue(result['success'])
        self.assertFalse(result['changed'])
        self.scan.assert_not_called()
        self.clock = 117  # Lease renewed at 106, not 100: still the game's profile without a scan.
        self.running = ''
        self.assertEqual((await self.plugin.get_settings())['settings']['level'], 0)
        self.assertEqual(len(self.writes), count)

    async def test_expired_report_with_new_process_foreground_applies_then_heartbeats_stay_idle(self):
        self.assertTrue((await self.plugin.set_active_app('111'))['success'])
        self.assertEqual(self.intensity(), 'off')
        self.clock = 113
        self.running = '222'
        before = copy.deepcopy(self.persisted())
        result = await self.plugin.set_active_app('222')
        self.assertTrue(result['success'])
        self.assertTrue(result['changed'])
        self.assertEqual((result['app_id'], result['profile_id']), ('222', '222'))
        self.assertEqual(self.intensity(), 'high')
        self.assertEqual((self.device / 'right_handle/rumble_mode').read_text().strip(), 'racing')
        count = len(self.writes)
        for clock in (114, 120, 133):
            self.clock = clock
            self.assertTrue((await self.plugin.set_active_app('222'))['success'])
        self.assertEqual(len(self.writes), count)
        self.assertEqual(self.persisted(), before)

    async def test_returning_frontend_reapplies_after_fallback_changed_effective_profile(self):
        await self.plugin.set_active_app('111')
        self.clock = 113
        self.running = ''  # The process scan no longer sees the game; only the frontend report does.
        self.assertEqual((await self.plugin.get_settings())['settings']['level'], 2)
        (self.device / 'rumble_intensity').write_text('medium', encoding='ascii')
        result = await self.plugin.set_active_app('111')
        self.assertTrue(result['changed'])
        self.assertEqual(result['settings']['level'], 0)
        self.assertEqual(self.intensity(), 'off')

    async def test_same_report_after_background_fallback_applied_another_profile_restores_immediately(self):
        await self.plugin.set_active_app('111')
        self.clock = 113
        self.running = ''
        await vibe._offload(lambda: vibe._apply_settings(vibe._active_values()))
        self.assertEqual(self.intensity(), 'medium')
        self.running = '111'
        result = await self.plugin.set_active_app('111')
        self.assertTrue(result['success'])
        self.assertFalse(result['changed'])
        self.assertEqual(self.intensity(), 'off')

    async def test_unchanged_game_after_expiry_does_not_rewrite_attributes(self):
        await self.plugin.set_active_app('111')
        count = len(self.writes)
        self.clock = 113
        result = await self.plugin.set_active_app('111')
        self.assertTrue(result['success'])
        self.assertFalse(result['changed'])
        self.assertEqual(len(self.writes), count)
        self.assertEqual(self.intensity(), 'off')

    async def test_partial_new_profile_failure_is_retried_by_the_next_heartbeat(self):
        await self.plugin.set_active_app('111')
        self.clock = 113
        self.running = '222'
        self.ignored.add('right_handle/rumble_mode')
        failed = await self.plugin.set_active_app('222')
        self.assertFalse(failed['success'])
        self.assertEqual(self.intensity(), 'high')
        self.assertEqual((self.device / 'right_handle/rumble_mode').read_text().strip(), 'fps')
        self.ignored.clear()
        retried = await self.plugin.set_active_app('222')
        self.assertTrue(retried['success'])
        self.assertEqual((self.device / 'right_handle/rumble_mode').read_text().strip(), 'racing')

    async def test_forgetting_a_rebound_device_cannot_keep_a_stale_applied_snapshot(self):
        await self.plugin.set_active_app('111')
        vibe._forget_device()
        (self.device / 'rumble_intensity').write_text('medium', encoding='ascii')
        self.assertTrue((await self.plugin.set_active_app('111'))['success'])
        self.assertEqual(self.intensity(), 'off')

    async def test_new_foreground_still_rejects_a_queued_edit_for_the_previous_game(self):
        await self.plugin.set_active_app('111')
        self.clock = 113
        self.running = '222'
        await self.plugin.set_active_app('222')
        before = copy.deepcopy(self.persisted())
        count = len(self.writes)
        stale = await self.plugin.set_intensity(1, '111', '111')
        self.assertFalse(stale['success'])
        self.assertEqual(self.persisted(), before)
        self.assertEqual(len(self.writes), count)
        self.assertEqual(self.intensity(), 'high')


class VibrationReadbackTests(unittest.TestCase):
    def setUp(self):
        for name, value in (('_attr_cache', {}),):
            patcher = patch.object(vibe, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(vibe.module_runtime, 'CONTROLLER_READ_GAP_S', 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, reads, cached=None):
        opened = mock_open()
        opened.return_value.read.side_effect = [value + '\n' for value in reads]
        vibe._attr_cache.clear()
        if cached:
            vibe._attr_cache[('/mock', 'rumble_intensity')] = cached
        with patch('builtins.open', opened):
            ok = vibe._write_attr('/mock', 'rumble_intensity', 'high')
        writes = [c for c in opened.call_args_list if len(c.args) > 1 and 'w' in c.args[1]]
        return ok, len(writes), opened.return_value.read.call_count

    def test_a_write_is_confirmed_only_by_two_equal_reads(self):
        # hid-lenovo-go can return the previous command's reply first (#7: false "did not confirm").
        self.assertEqual(self.write(['low', 'high', 'high']), (True, 1, 3))
        self.assertEqual(self.write(['high', 'low', 'low']), (False, 1, 3), "one matching read is not enough")
        self.assertEqual(self.write(['low', 'medium', 'low', 'medium']), (False, 1, 4), "bounded")

    def test_a_stale_differing_read_does_not_cost_a_write(self):
        self.assertEqual(self.write(['low', 'high', 'high'], cached='high'), (True, 0, 3))
        self.assertEqual(self.write(['high'], cached='high'), (True, 0, 1))

    def test_device_disappearing_during_readback_is_not_success(self):
        opened = mock_open()
        opened.return_value.read.side_effect = FileNotFoundError('controller disconnected')
        with patch('builtins.open', opened):
            self.assertFalse(vibe._write_attr('/mock', 'rumble_intensity', 'high', True))


class VibrationSleepingPadTests(FakeDevice):
    """A pad that does not take writes (asleep, no uevent) is not flooded: each write may be felt."""

    def setUp(self):
        super().setUp()
        self.ignored.add('right_handle/rumble_mode')

    async def test_wake_retries_rewrite_only_the_attributes_that_failed(self):
        async def sleep(seconds):
            self.clock += seconds
        with patch.object(vibe, '_REAPPLY_WAIT_S', 1.0), patch.object(vibe, '_REAPPLY_STEP_S', 0.5),                 patch.object(vibe, 'asyncio', types.SimpleNamespace(sleep=sleep)):
            self.assertFalse((await self.plugin.reapply())['success'])
        self.assertEqual(len(self.writes), 7, 'three passes: the first writes all five, the others one')
        self.assertEqual(self.writes[5:], ['right_handle/rumble_mode'] * 2)
        self.assertEqual(self.hardware(), {**DEFAULTS, 'right_handle/rumble_mode': 'racing'})

    async def test_a_failing_check_backs_off_and_a_hotplug_resets_it(self):
        ticks = []

        def resumed():
            ticks.append(None)
            self.clock += vibe._DRIFT_INTERVAL_S
            if len(ticks) > 10:
                raise asyncio.CancelledError
            return False
        with patch.object(vibe, '_RESUME_CHECK_S', 0), patch.object(vibe, '_resume_detected', side_effect=resumed):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._drift_loop()
        self.assertEqual(self.writes.count('right_handle/rumble_mode'), 3, 'checks after 30, 60 and 120 s, not ten')
        self.assertEqual(vibe._drift_interval, 8 * vibe._DRIFT_INTERVAL_S)
        with patch.object(vibe, '_emit_device_status', new=AsyncMock()):
            await vibe._handle_device_added(str(self.device), 'change')
        self.assertEqual(vibe._drift_interval, vibe._DRIFT_INTERVAL_S)


class VibrationQuietPassTests(FakeDevice):
    """The pads buzz whenever their rumble mode is set, even to the same value: start, hotplug and wake
    write only what two equal reads show differs."""

    def set_pads(self, values):
        for name, value in values.items():
            (self.device / name).write_text(value, encoding='ascii')

    async def passes(self):
        async def sleep(seconds):
            self.clock += seconds
        with patch.object(vibe, '_monitor_hotplug', new=Mock(return_value=asyncio.sleep(0))), \
                patch.object(vibe.Plugin, '_drift_loop', new=Mock(return_value=asyncio.sleep(0))), \
                patch.object(vibe, '_emit_device_status', new=AsyncMock()), \
                patch.object(vibe, '_REAPPLY_WAIT_S', 1.0), patch.object(vibe, '_REAPPLY_STEP_S', 0.5), \
                patch.object(vibe, 'asyncio', types.SimpleNamespace(sleep=sleep)):
            await self.plugin._main()
            await vibe._handle_device_added(str(self.device), 'add')
            await self.plugin.reapply()

    async def test_pads_that_already_have_the_profile_are_not_written(self):
        self.set_pads(DEFAULTS)
        await self.passes()
        self.assertEqual(self.writes, [])

    async def test_only_the_differing_attributes_are_written(self):
        self.set_pads({**DEFAULTS, 'rumble_intensity': 'low', 'touchpad/vibration_enabled': 'false'})
        await self.passes()
        self.assertEqual(self.writes, ['rumble_intensity', 'touchpad/vibration_enabled'], 'never the rumble mode')
        self.assertEqual(self.hardware(), DEFAULTS)

    async def test_reads_that_do_not_agree_are_never_answered_with_a_write(self):
        self.set_pads(DEFAULTS)
        real_read, flips = vibe._read_attr, iter(range(10 ** 6))
        unsettled = lambda path: real_read(path) + str(next(flips)) if 'rumble_mode' in path else real_read(path)
        with patch.object(vibe, '_read_attr', side_effect=unsettled):
            await self.passes()
            self.assertFalse(vibe._apply_settings(vibe._active_values(), verify=True))  # the periodic check
        self.assertEqual(self.writes, [])


    async def test_another_module_s_command_never_lands_between_an_attribute_s_reads_and_write(self):
        # RGB and the sleep timer send commands to the same pads, and the driver can hand a read their reply.
        self.set_pads({**DEFAULTS, 'rumble_intensity': 'low'})
        with module_runtime.controller_config_lock:  # another module's command in flight
            check = asyncio.ensure_future(asyncio.to_thread(vibe._apply_settings, vibe._active_values(), verify=True))
            await asyncio.sleep(0.3)
            self.assertFalse(check.done())
            self.assertEqual(self.writes, [])
        self.assertTrue(await check)
        self.assertEqual(self.writes, ['rumble_intensity'])

    async def test_a_stop_ends_a_check_before_its_next_attribute(self):
        # Each attribute can take a second or more; Decky SIGKILLs a stop after 5 s, skipping _uninstall.
        self.set_pads({**DEFAULTS, 'rumble_intensity': 'low', 'touchpad/vibration_enabled': 'false'})
        reading, answer = threading.Event(), threading.Event()
        real_read = vibe._read_attr
        def slow_pad(path):
            reading.set()
            answer.wait(5)
            return real_read(path)
        with patch.object(vibe, '_read_attr', side_effect=slow_pad):
            check = asyncio.ensure_future(asyncio.to_thread(vibe._apply_settings, vibe._active_values(), verify=True))
            await asyncio.to_thread(reading.wait, 5)
            await self.plugin._unload()
            answer.set()
            self.assertFalse(await check)
        self.assertEqual(self.writes, ['rumble_intensity'], 'the attribute in progress finishes, no other starts')

class VibrationStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_retry_clears_transient_startup_error(self):
        plugin = vibe.Plugin()
        with patch.object(vibe.Plugin, '_setup_error', None), \
                patch.object(vibe._resume_detected, 'last', None), \
                patch.object(vibe, '_active_app_ts', None), \
                patch.object(vibe, '_monitor_task', None), patch.object(vibe, '_drift_task', None), \
                patch.object(vibe.module_runtime, 'suspend_offset', return_value=0), \
                patch.object(vibe, '_active_values', side_effect=[OSError('settings unreadable'), dict(vibe.DEFAULT_PROFILE)]), \
                patch.object(vibe, '_apply_settings', return_value=True), \
                patch.object(vibe, '_monitor_hotplug', new=AsyncMock()), \
                patch.object(plugin, '_drift_loop', new=AsyncMock()):
            try:
                with self.assertRaisesRegex(OSError, 'settings unreadable'):
                    await plugin._main()
                self.assertFalse((await plugin.is_ready())['ready'])
                await plugin._unload()
                await plugin._main()
                self.assertTrue((await plugin.is_ready())['ready'])
                self.assertEqual((await plugin.is_ready())['error'], '')
            finally:
                await plugin._unload()
                await module_runtime.drain('vibration')


class HotplugMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def monitor(self, events, handle):
        """Run _monitor_hotplug over `events` ((action, sys_path) pairs) with a fake netlink monitor."""
        pending, readers = [types.SimpleNamespace(action=a, sys_path=p) for a, p in events], []
        class Monitor:
            @classmethod
            def from_netlink(cls, _ctx): return cls()
            def filter_by(self, **_): pass
            def start(self): pass
            def fileno(self): return 99
            def poll(self, _timeout): return pending.pop(0) if pending else None
        class Loop:  # Only the reader registration is replaced; the queue runs on the real loop.
            def add_reader(self, _fd, callback): readers.append(callback)
            def remove_reader(self, _fd): readers.clear()
        with patch.object(vibe, '_PYUDEV', True),                 patch.object(vibe, '_pyudev', types.SimpleNamespace(Monitor=Monitor), create=True),                 patch.object(vibe, '_udev_ctx', None, create=True),                 patch.object(vibe, '_handle_device_added', new=handle),                 patch.object(vibe.module_runtime, 'is_paused', return_value=False),                 patch.object(vibe.asyncio, 'get_running_loop', return_value=Loop()):
            task = asyncio.create_task(vibe._monitor_hotplug())
            await asyncio.sleep(0)
            readers[0]()
            for _ in range(10): await asyncio.sleep(0)
            self.assertFalse(task.done()); self.assertEqual(len(readers), 1)
            task.cancel(); await asyncio.gather(task, return_exceptions=True)

    async def test_one_failed_event_keeps_the_monitor_running(self):
        handled = []
        async def handle(path, _action):
            handled.append(path)
            if len(handled) == 1: raise OSError('settings read failed')
        await self.monitor([('change', '/a'), ('change', '/b')], handle)
        self.assertEqual(handled, ['/a', '/b'])

    async def test_a_go_s_driver_rebind_on_the_same_path_restarts_its_setup_window(self):
        for action in ('remove', 'unbind'):
            with self.subTest(action), patch.object(vibe, '_device_path', None),                     patch.object(vibe, '_go_s_seen', (GO_S_HID, 1.0)):
                await self.monitor([(action, '/sys/devices/usb1/0003:1A86:E310.0006')], AsyncMock())
                self.assertEqual(vibe._go_s_seen, (GO_S_HID, 1.0), 'another interface leaves it')
                await self.monitor([(action, GO_S_HID)], AsyncMock())
                self.assertIsNone(vibe._go_s_seen)


# ── Legion Go S (beta): raw frames instead of hid-lenovo-go attributes ────────

GO_S_HID = '/sys/devices/usb1/0003:1A86:E310.0005'


def go_s_frames(level, touchpad):
    return [bytes([0x08, 0x01, level]) + bytes(61), bytes([0x08, 0x03, touchpad]) + bytes(61)]


class GoSFrameTests(unittest.TestCase):
    def test_only_the_two_allowlisted_frames_can_be_built(self):
        self.assertEqual(set(vibe._GO_S_FRAMES), {'strength', 'touchpad'})
        built = [vibe._go_s_frame('strength', level) for level in range(4)]
        built += [vibe._go_s_frame('touchpad', on) for on in (0, 1)]
        self.assertEqual(built, [go_s_frames(level, 0)[0] for level in range(4)] + [go_s_frames(0, on)[1] for on in (0, 1)])
        for frame in built:
            self.assertEqual((len(frame), frame[0]), (64, 0x08), 'never a frame the kernel reads as a report id 0')
        for setting, value in (('strength', 4), ('strength', -1), ('touchpad', 2), ('strength', True),
                               ('touchpad', False), ('strength', 1.0), ('strength', '1'), ('mode', 0),
                               ('notify', 1), ('', 0), (None, 0), (b'strength', 1), (0x01, 1)):
            with self.subTest(setting=setting, value=value), self.assertRaises(ValueError):
                vibe._go_s_frame(setting, value)


@unittest.skipUnless(os.name == 'posix', 'needs sysfs-like symlinks')
class GoSInterfaceTests(unittest.TestCase):
    """The interface is found from the joystick-ring LED, on a fake sysfs tree."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.devices, self.drivers = root / 'devices', root / 'drivers'
        for driver in ('hid-lenovo-go-s', 'hid-generic'):
            (self.drivers / driver).mkdir(parents=True)
        self.led = root / 'class' / 'go_s:rgb:joystick_rings'
        self.led.parent.mkdir()
        for name, value in {'GO_S_LED': str(self.led), 'SYS_DEVICES': str(self.devices) + '/',
                            'GO_S_DRIVERS_DIR': str(self.drivers / 'hid-lenovo-go-s')}.items():
            stack.enter_context(patch.object(vibe, name, value))

    def interface(self, name='0003:1A86:E310.0005', driver='hid-lenovo-go-s',
                  descriptor=b'\x06\xa0\xff\x09\x01\xa1\x01', attributes=True, hidraw=('hidraw3',)):
        hid = self.devices / 'usb1' / name
        (hid / 'leds' / 'go_s:rgb:joystick_rings').mkdir(parents=True)
        (hid / 'driver').symlink_to(self.drivers / driver)
        if attributes:
            (hid / 'touchpad').mkdir()
            (hid / 'touchpad' / 'linux_mode').write_text('relative\n')
        (hid / 'report_descriptor').write_bytes(descriptor)
        for node in hidraw:
            (hid / 'hidraw' / node).mkdir(parents=True)
            (hid / 'hidraw' / node / 'dev').write_text('240:3\n')
        if self.led.is_symlink():
            self.led.unlink()
        self.led.symlink_to(hid / 'leds' / 'go_s:rgb:joystick_rings')
        return str(hid)

    def test_the_led_leads_to_the_configuration_interface_and_its_hidraw_node(self):
        hid = self.interface()
        self.assertEqual(vibe._go_s_device(), hid)
        self.assertEqual(vibe._go_s_hidraw(hid), ('/dev/hidraw3', '240:3'))
        self.assertIsNone(vibe._go_s_hidraw(self.interface(name='0003:1A86:E311.0006', hidraw=('hidraw3', 'hidraw4'))))

    def test_bootloader_foreign_driver_unfinished_setup_or_other_interface_is_refused(self):
        for case, options in (('bootloader', dict(name='0003:1A86:FE10.0007')),
                              ('foreign id', dict(name='0003:17EF:61EB.0008')),
                              ('foreign driver', dict(name='0003:1A86:E310.0009', driver='hid-generic')),
                              ('driver attributes missing', dict(name='0003:1A86:E310.000A', attributes=False)),
                              ('not the 0xFFA0 interface', dict(name='0003:1A86:E310.000B', descriptor=b'\x05\x01\x09\x05'))):
            with self.subTest(case):
                self.interface(**options)
                self.assertIsNone(vibe._go_s_device())

    def test_applies_only_with_the_driver_loaded_and_its_led(self):
        with patch.object(module_runtime, 'legion_device', return_value=('gos', '83L3')):
            self.assertFalse(vibe.Plugin.applicable(), 'no LED')
            self.assertIsNone(vibe._go_s_device())
            self.interface()
            self.assertTrue(vibe.Plugin.applicable())
            with patch.object(vibe, 'GO_S_DRIVERS_DIR', str(self.drivers / 'missing')):
                self.assertFalse(vibe.Plugin.applicable(), 'driver not loaded')

    def test_a_node_that_is_not_that_character_device_is_never_written(self):
        regular = self.devices.parent / 'hidraw3'
        regular.write_bytes(b'')
        self.assertFalse(vibe._go_s_send(str(regular), '240:3', go_s_frames(1, 1)[0]))
        self.assertEqual(regular.read_bytes(), b'')
        link = self.devices.parent / 'hidraw-link'
        link.symlink_to(regular)
        with self.assertRaises(OSError):
            vibe._go_s_send(str(link), '240:3', go_s_frames(1, 1)[0])


class GoSDevice(FakeDevice):
    """The Go S path, faked only where it meets the controller: found, sent to, hhd."""

    def setUp(self):
        super().setUp()
        self.sent, self.sleeps = [], []
        self.present, self.hhd = GO_S_HID, False

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.clock += seconds

        def send(node, number, frame):
            self.assertTrue(module_runtime.controller_config_lock._is_owned(), 'sent under the shared lock')
            self.assertEqual((node, number), ('/dev/hidraw3', '240:3'))
            self.sent.append(frame)
            return True
        self.clock_ns = types.SimpleNamespace(monotonic=lambda: self.clock, sleep=sleep)
        for name, value in {
            'time': self.clock_ns, '_is_go_s': Mock(return_value=True), '_go_s_seen': None, '_device_path': None,
            '_go_s_device': Mock(side_effect=lambda: self.present),
            '_go_s_hidraw': Mock(return_value=('/dev/hidraw3', '240:3')),
            '_go_s_send': Mock(side_effect=send), '_hhd_running': Mock(side_effect=lambda: self.hhd),
        }.items():
            self.stack.enter_context(patch.object(vibe, name, value))
        self.addCleanup(module_runtime.begin_start, 'vibration')

    def configure(self):
        """As if the owner had already changed both settings here."""
        self.store.setSetting('go_s_set', ['level', 'touchpadEnabled'])
        self.store.commit()


class GoSApplyTests(GoSDevice):
    async def test_settings_are_sent_as_frames_once_the_interface_has_settled(self):
        self.assertTrue((await self.plugin.set_intensity(3))['success'])
        self.assertEqual(self.sleeps, [vibe._GO_S_SETTLE_S])
        self.assertEqual(self.sent, go_s_frames(3, 1)[:1], 'only the setting the owner changed')
        self.assertTrue((await self.plugin.set_touchpad_enabled(False))['success'])
        self.assertEqual(self.sleeps, [vibe._GO_S_SETTLE_S], 'the window is waited once per interface')
        self.assertEqual(self.sent[1:], go_s_frames(3, 0), 'now both: each was changed here')
        self.assertEqual((self.writes, self.hardware()), ([], DRIVER), 'no hid-lenovo-go attribute is touched')

    async def test_no_interface_or_hhd_sends_nothing_and_the_choice_is_kept(self):
        self.present = None
        result = await self.plugin.set_intensity(1)
        self.assertEqual((result['success'], result['error']), (False, vibe.NOT_CONNECTED))
        self.assertEqual(self.persisted()['0']['settings']['level'], 1)
        self.assertEqual(self.store.getSetting('go_s_set'), ['level'], 'sent once the controller connects')
        self.present, self.hhd = GO_S_HID, True
        self.assertFalse(vibe._apply_settings(dict(vibe.DEFAULT_PROFILE)))
        self.assertEqual(self.sent, [])

    def test_a_stored_value_outside_the_allowlist_never_reaches_the_controller(self):
        self.configure()
        vibe._go_s_seen = (GO_S_HID, self.clock - 10)
        self.assertFalse(vibe._apply_settings({**vibe.DEFAULT_PROFILE, 'level': 7}))
        self.assertEqual(self.sent, [])

    def test_a_new_interface_or_a_stop_during_the_window_gets_nothing(self):
        self.configure()
        def replaced(seconds):
            self.sleeps.append(seconds)
            self.present = '/sys/devices/usb1/0003:1A86:E310.0006'
        self.clock_ns.sleep = replaced
        self.assertFalse(vibe._apply_settings(dict(vibe.DEFAULT_PROFILE)))
        self.clock_ns.sleep = lambda seconds: module_runtime.request_stop('vibration')
        self.assertFalse(vibe._apply_settings(dict(vibe.DEFAULT_PROFILE)))
        self.assertEqual(self.sent, [])

    def test_frames_wait_for_an_rgb_transaction_on_the_shared_controller_lock(self):
        import rgb_backend
        self.assertIs(rgb_backend._apply_lock, module_runtime.controller_config_lock)
        self.configure()
        vibe._go_s_seen = (GO_S_HID, self.clock - 10)
        held, release = threading.Event(), threading.Event()

        def rgb_transaction():
            with rgb_backend._apply_lock:
                held.set()
                release.wait(5)
        rgb = threading.Thread(target=rgb_transaction)
        rgb.start()
        held.wait(5)
        worker = threading.Thread(target=vibe._apply_settings, args=(dict(vibe.DEFAULT_PROFILE),))
        worker.start()
        worker.join(0.2)
        self.assertTrue(worker.is_alive())
        self.assertEqual(self.sent, [])
        release.set()
        worker.join(5)
        rgb.join(5)
        self.assertEqual(len(self.sent), 2)

    def test_each_frame_takes_the_shared_lock_on_its_own(self):
        # A hung MCU can hold one hidraw write for seconds; RGB must be able to get in between frames.
        self.configure()
        vibe._go_s_seen = (GO_S_HID, self.clock - 10)
        real, holds = module_runtime.controller_config_lock, []

        class Counting:
            def __enter__(lock):
                real.acquire()
                holds.append(len(self.sent))
            def __exit__(lock, *_):
                real.release()
            def _is_owned(lock):
                return real._is_owned()
        with patch.object(module_runtime, 'controller_config_lock', Counting()):
            self.assertTrue(vibe._apply_settings(dict(vibe.DEFAULT_PROFILE)))
        self.assertEqual((holds, len(self.sent)), ([0, 1], 2))

    async def test_hotplug_and_wake_send_again_after_a_fresh_window(self):
        self.configure()
        with patch.object(vibe, '_emit_device_status', new=AsyncMock()):
            await vibe._handle_device_added('/sys/devices/usb1/0003:1A86:E310.0004', 'change')  # another interface
            self.assertEqual(self.sent, [])
            await vibe._handle_device_added(GO_S_HID, 'bind')
        self.assertEqual((self.sleeps, len(self.sent)), ([vibe._GO_S_SETTLE_S], 2))
        await self.plugin.reapply()  # a wake may have reset the controller
        self.assertEqual((self.sleeps, len(self.sent)), ([vibe._GO_S_SETTLE_S] * 2, 4))

    async def test_the_drift_loop_sends_after_a_wake_and_never_periodically(self):
        self.configure()
        checks = []

        def resumed():
            checks.append(None)
            self.clock += 60  # past the periodic interval each time
            if len(checks) > 3:
                raise asyncio.CancelledError
            return len(checks) == 3
        with patch.object(vibe, '_RESUME_CHECK_S', 0), patch.object(vibe, '_resume_detected', side_effect=resumed):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._drift_loop()
        self.assertEqual(len(self.sent), 2)

    async def test_uninstall_sends_nothing_and_keeps_the_settings(self):
        await self.plugin.set_intensity(0)
        sent = list(self.sent)
        with patch.object(vibe, '_restore_baseline') as restore:
            await self.plugin._uninstall()
        restore.assert_not_called()
        self.assertEqual(self.sent, sent)
        self.assertEqual(self.persisted()['0']['settings']['level'], 0)

    async def test_the_test_buzz_is_not_offered(self):
        with patch.object(vibe, '_find_ff_device') as find:
            self.assertFalse((await self.plugin.test_vibration())['success'])
        find.assert_not_called()


class GoSUnconfiguredTests(GoSDevice):
    """V1: the Go S gets nothing until its owner changes a setting here, so Legion Space's values stay."""

    def assertNothingSent(self):
        self.assertEqual((self.sent, self.sleeps), ([], []))

    async def test_start_hotplug_wake_and_the_drift_loop_send_nothing(self):
        plugin = self.plugin
        with patch.object(vibe.Plugin, '_setup_error', None), patch.object(vibe._resume_detected, 'last', None),                 patch.object(vibe, '_monitor_task', None), patch.object(vibe, '_drift_task', None),                 patch.object(vibe.module_runtime, 'suspend_offset', return_value=0),                 patch.object(vibe, '_monitor_hotplug', new=AsyncMock()), patch.object(plugin, '_drift_loop', new=AsyncMock()):
            try:
                await plugin._main()
                self.assertTrue((await plugin.is_ready())['ready'])
            finally:
                await plugin._unload()
        with patch.object(vibe, '_emit_device_status', new=AsyncMock()):
            await vibe._handle_device_added(GO_S_HID, 'bind')
            await vibe._handle_device_added(GO_S_HID, 'change')
        self.assertTrue((await plugin.reapply())['success'])
        self.assertTrue((await plugin.set_active_app('111'))['success'])
        checks = []

        def resumed():
            checks.append(None)
            if len(checks) > 2:
                raise asyncio.CancelledError
            return True
        with patch.object(vibe, '_RESUME_CHECK_S', 0), patch.object(vibe, '_resume_detected', side_effect=resumed):
            with self.assertRaises(asyncio.CancelledError):
                await plugin._drift_loop()
        self.assertNothingSent()
        self.assertIsNone(self.store.getSetting('go_s_set'))
        self.assertIs((await plugin.get_settings())['configured'], False)

    async def test_the_first_change_sends_only_that_setting_and_is_remembered(self):
        result = await self.plugin.set_intensity(1)
        self.assertEqual((result['success'], result['configured']), (True, True))
        self.assertEqual(self.sent, go_s_frames(1, 1)[:1], "Legion Space's touchpad vibration stays")
        self.assertEqual(self.store.getSetting('go_s_set'), ['level'])
        self.assertIs((await self.plugin.get_settings())['configured'], True)
        self.sent.clear()
        self.assertTrue((await self.plugin.set_touchpad_enabled(False))['success'])
        self.assertEqual(self.sent, go_s_frames(1, 0))
        self.assertEqual(self.store.getSetting('go_s_set'), ['level', 'touchpadEnabled'])

    async def test_touchpad_vibration_alone_and_a_reset_send_what_they_change(self):
        self.assertTrue((await self.plugin.set_touchpad_enabled(False))['success'])
        self.assertEqual(self.sent, go_s_frames(2, 0)[1:], "Legion Space's strength stays")
        self.assertEqual(self.store.getSetting('go_s_set'), ['touchpadEnabled'])
        self.sent.clear()
        result = await self.plugin.reset_to_default()
        self.assertEqual((result['success'], result['configured'], self.sent), (True, True, go_s_frames(2, 1)))
        self.assertEqual(self.store.getSetting('go_s_set'), ['level', 'touchpadEnabled'])

    async def test_a_game_profile_alone_changes_nothing_on_the_controller(self):
        result = await self.plugin.set_profile_overwrite('111', True, 'Game')
        self.assertEqual((result['success'], result['configured']), (True, False))
        self.assertIsNone(self.store.getSetting('go_s_set'))
        self.assertNothingSent()

    async def test_a_rejected_or_unsent_change_does_not_count(self):
        self.assertFalse((await self.plugin.set_intensity(True))['success'])
        for result in (await self.plugin.set_rumble_mode(1), await self.plugin.set_touchpad_intensity(1)):
            self.assertEqual((result['success'], result['error'].endswith('not available on this console.')),
                             (False, True))
        self.assertIsNone(self.persisted(), 'nothing is stored either')
        self.assertIsNone(self.store.getSetting('go_s_set'))
        self.assertNothingSent()
        self.hhd = True  # the interface is there but the frame is not sent
        self.assertFalse((await self.plugin.set_intensity(1))['success'])
        self.assertIsNone(self.store.getSetting('go_s_set'))
        self.assertEqual(self.sent, [])

    async def test_after_a_restart_only_the_settings_changed_here_are_sent_again(self):
        await self.plugin.set_intensity(3)
        self.sent.clear()
        vibe._go_s_seen = None  # a new module start: the interface window is waited out again
        self.assertTrue(await asyncio.to_thread(vibe._apply_settings, vibe._active_values(), None, True))
        self.assertEqual(self.sent, go_s_frames(3, 1)[:1])
        with patch.object(vibe, '_emit_device_status', new=AsyncMock()):
            await vibe._handle_device_added(GO_S_HID, 'bind')
        await self.plugin.reapply()
        self.assertEqual(self.sent, go_s_frames(3, 1)[:1] * 3)


class GoSRegressionTests(FakeDevice):
    async def test_the_go_2_keeps_its_attribute_path_and_never_builds_a_frame(self):
        vibe._is_go_s.cache_clear()
        self.addCleanup(vibe._is_go_s.cache_clear)
        with patch.object(module_runtime, 'legion_device', return_value=('go2', '83N0')), \
                patch.object(vibe, '_go_s_device') as found, patch.object(vibe, '_go_s_send') as send:
            vibe._migrate()  # a fresh install records the driver values before the first write
            self.assertTrue((await self.plugin.set_intensity(0))['success'])
            self.assertEqual(self.intensity(), 'off')
            self.assertIs((await self.plugin.get_settings())['configured'], True, 'only a Go S ever holds settings back')
            self.assertIsNone(self.store.getSetting('go_s_set'))
            await self.plugin._uninstall()
        self.assertEqual(self.intensity(), 'high', 'uninstall restored the driver value it found')
        found.assert_not_called()
        send.assert_not_called()


if __name__ == '__main__':
    unittest.main()

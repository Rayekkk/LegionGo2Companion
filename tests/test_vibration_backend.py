# SPDX-License-Identifier: BSD-3-Clause
"""Vibration release against a fake hid-lenovo-go attribute tree."""
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


class VibrationBaselineTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.device = self.root / 'hid'
        for name, value in DRIVER.items():
            path = self.device / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding='ascii')
        self.store = AtomicSettingsManager('vibration_settings', str(self.root / 'settings'))
        for name, value in {'settings': self.store, '_device_path': str(self.device),
                            '_attr_cache': {}, '_capabilities_cache': {},
                            '_last_applied_values': None}.items():
            self.stack.enter_context(patch.object(vibe, name, value))
        self.stack.enter_context(patch.object(vibe, '_discover', return_value=(str(self.device), 'fixture')))

    def hardware(self):
        return {name: (self.device / name).read_text(encoding='ascii').strip() for name in DRIVER}

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

    def test_vibration_overwrite_change_rejects_queued_edit(self):
        profiles = {key: {"overwrite": False, "settings": dict(vibe.DEFAULT_PROFILE)}
                    for key in ("0", "111")}
        with patch.object(vibe, "_active_app_id", "111"), \
             patch.object(vibe, "_load_profiles", return_value=profiles), \
             patch.object(vibe, "_save_profiles") as save:
            result = asyncio.run(vibe.Plugin().set_intensity(3, "111", "111"))
            self.assertFalse(result["success"])
            save.assert_not_called()


class VibrationProfileContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.store = AtomicSettingsManager('vibration_settings', self.folder)
        self.profiles = {key: {'overwrite': key != '0', 'settings': {**vibe.DEFAULT_PROFILE, 'level': level}}
            for key, level in (('0', 2), ('111', 0), ('222', 1))}
        self.store.replace({'game_profiles': self.profiles})
        self.stack.enter_context(patch.object(vibe, 'settings', self.store))
        self.stack.enter_context(patch.object(vibe, '_active_app_ts', None))
        self.plugin = vibe.Plugin()

    async def asyncTearDown(self):
        await module_runtime.drain('vibration')

    def persisted(self):
        return AtomicSettingsManager('vibration_settings', self.folder).getSetting('game_profiles')

    async def test_expected_game_and_mutation_share_one_process_lookup(self):
        with patch.object(vibe, 'scan_proc_for_appid', side_effect=['111', '222']) as process, \
                patch.object(vibe, '_apply_settings', return_value=True) as apply:
            result = await self.plugin.set_intensity(3, '111', '111')
        self.assertTrue(result['success'])
        self.assertEqual((result['app_id'], result['profile_id']), ('111', '111'))
        self.assertEqual(process.call_count, 1)
        saved = self.persisted()
        self.assertEqual(saved['111']['settings']['level'], 3)
        self.assertEqual(saved['222']['settings']['level'], 1)
        apply.assert_called_once_with(saved['111']['settings'])

    async def test_expected_global_profile_cannot_be_replaced_by_new_game_override_between_reads(self):
        self.profiles['111']['overwrite'] = False
        self.store.replace({'game_profiles': self.profiles})
        with patch.object(vibe, 'scan_proc_for_appid', side_effect=['111', '222']) as process, \
                patch.object(vibe, '_apply_settings', return_value=True):
            result = await self.plugin.set_touchpad_enabled(False, '111', '0')
        self.assertTrue(result['success'])
        self.assertEqual(process.call_count, 1)
        saved = self.persisted()
        self.assertFalse(saved['0']['settings']['touchpadEnabled'])
        self.assertTrue(saved['222']['settings']['touchpadEnabled'])

    async def test_stale_reset_rejects_game_change_without_saving_or_writing(self):
        before = self.persisted()
        with patch.object(vibe, 'scan_proc_for_appid', return_value='222'), \
                patch.object(vibe, '_save_profiles') as save, \
                patch.object(vibe, '_apply_settings') as apply:
            result = await self.plugin.reset_to_default('111', '111')
        self.assertFalse(result['success'])
        self.assertEqual(result['profile_id'], '222')
        save.assert_not_called()
        apply.assert_not_called()
        self.assertEqual(self.persisted(), before)

    async def test_stale_reset_rejects_overwrite_change_for_the_same_game(self):
        self.profiles['111']['overwrite'] = False
        self.store.replace({'game_profiles': self.profiles})
        before = self.persisted()
        with patch.object(vibe, 'scan_proc_for_appid', return_value='111'), \
                patch.object(vibe, '_save_profiles') as save, \
                patch.object(vibe, '_apply_settings') as apply:
            result = await self.plugin.reset_to_default('111', '111')
        self.assertFalse(result['success'])
        self.assertEqual(result['profile_id'], '0')
        save.assert_not_called()
        apply.assert_not_called()
        self.assertEqual(self.persisted(), before)

    async def test_reset_uses_checked_context_once_and_remains_compatible_without_arguments(self):
        with patch.object(vibe, 'scan_proc_for_appid', side_effect=['111', '222']) as process, \
                patch.object(vibe, '_apply_settings', return_value=True) as apply:
            result = await self.plugin.reset_to_default('111', '111')
            self.assertEqual(process.call_count, 1)
            self.assertEqual(self.persisted()['111']['settings'], vibe.DEFAULT_PROFILE)
            self.assertEqual(self.persisted()['222']['settings']['level'], 1)
            compatible = await self.plugin.reset_to_default()
        self.assertTrue(result['success'])
        self.assertTrue(compatible['success'])
        self.assertEqual(compatible['profile_id'], '222')
        self.assertEqual(apply.call_count, 2)


class VibrationCapacityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = VibrationProfileContextTests.asyncSetUp
    asyncTearDown = VibrationProfileContextTests.asyncTearDown
    persisted = VibrationProfileContextTests.persisted

    def fill(self, count):
        self.store.replace({'game_profiles': {str(i): {
            'overwrite': i != 0, 'settings': dict(vibe.DEFAULT_PROFILE), 'name': f'Game {i}'
        } for i in range(count)}})

    async def test_capacity_rejects_creation_before_commit_or_hardware_write(self):
        self.fill(vibe.MAX_GAME_PROFILES)
        before = self.persisted()
        with patch.object(vibe, '_running_app_id', return_value='9999'), \
                patch.object(vibe, '_apply_settings') as hardware:
            result = await self.plugin.set_profile_overwrite('9999', True)
        self.assertFalse(result['success'])
        self.assertIn('limit', result['error'])
        self.assertEqual(result['profile_id'], '0')
        self.assertEqual(self.persisted(), before)
        hardware.assert_not_called()

    async def test_existing_profiles_can_be_edited_at_capacity(self):
        self.fill(vibe.MAX_GAME_PROFILES)
        with patch.object(vibe, '_running_app_id', return_value='111'), \
                patch.object(vibe, '_apply_settings', return_value=True):
            self.assertTrue((await self.plugin.set_intensity(3, '111', '111'))['success'])
        saved = self.persisted()
        self.assertEqual(len(saved), vibe.MAX_GAME_PROFILES)
        self.assertEqual(saved['111']['settings']['level'], 3)

    async def test_legacy_over_capacity_store_survives_unrelated_edit(self):
        self.fill(vibe.MAX_GAME_PROFILES + 1)
        before = self.persisted()
        with patch.object(vibe, '_running_app_id', return_value='0'), \
                patch.object(vibe, '_apply_settings', return_value=True):
            self.assertTrue((await self.plugin.set_intensity(3, '0', '0'))['success'])
        after = self.persisted()
        self.assertEqual(set(after), set(before))
        self.assertEqual(after[str(vibe.MAX_GAME_PROFILES)], before[str(vibe.MAX_GAME_PROFILES)])

    async def test_profile_toggle_reports_committed_scope_when_hardware_apply_fails(self):
        with patch.object(vibe, '_running_app_id', return_value='111'), \
                patch.object(vibe, '_apply_settings', return_value=False):
            result = await self.plugin.set_profile_overwrite('111', False)
        self.assertFalse(result['success'])
        self.assertEqual((result['app_id'], result['profile_id'], result['overwrite']), ('111', '0', False))
        self.assertFalse(self.persisted()['111']['overwrite'])

    async def test_toggle_of_inactive_profile_returns_actual_running_scope(self):
        with patch.object(vibe, 'scan_proc_for_appid', side_effect=['222', '111']) as scan, \
                patch.object(vibe, '_apply_settings', return_value=True):
            result = await self.plugin.set_profile_overwrite('111', False)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual((result['app_id'], result['profile_id'], result['overwrite']), ('222', '222', True))

    async def test_new_profile_is_seeded_from_the_single_admitted_running_snapshot(self):
        with patch.object(vibe, 'scan_proc_for_appid', side_effect=['222', '111']) as scan, \
                patch.object(vibe, '_apply_settings', return_value=True):
            result = await self.plugin.set_profile_overwrite('333', True)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(self.persisted()['333']['settings'], self.profiles['222']['settings'])
        self.assertEqual(result['app_id'], '222')


class VibrationResetStateTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = VibrationProfileContextTests.asyncSetUp
    asyncTearDown = VibrationProfileContextTests.asyncTearDown
    persisted = VibrationProfileContextTests.persisted

    async def test_failed_reset_hardware_apply_returns_committed_settings(self):
        with patch.object(vibe, 'scan_proc_for_appid', return_value='111'), \
                patch.object(vibe, '_apply_settings', return_value=False):
            result = await self.plugin.reset_to_default('111', '111')
        self.assertFalse(result['success'])
        self.assertEqual(result['settings'], vibe.DEFAULT_PROFILE)
        self.assertEqual(result['settings'], self.persisted()['111']['settings'])
        self.assertEqual((result['app_id'], result['profile_id'], result['overwrite']), ('111', '111', True))


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
    def playable(self, *, pause=None, start_error=False, id_error=False):
        def ioctl(_fd, request, payload):
            if request == vibe._EVIOCGID:
                if id_error:
                    raise OSError('descriptor disappeared')
                payload[:] = struct.pack('HHHH', 3, 0x17ef, 0x61eb, 1)
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

    async def test_test_rechecks_open_descriptor_before_upload_or_output(self):
        def ioctl(_fd, request, payload):
            if request == 0x80084502:  # EVIOCGID, struct input_id from Linux UAPI.
                payload[:] = struct.pack('HHHH', 3, 0x045e, 0x02ea, 1)
            elif request == vibe._EVIOCSFF:
                struct.pack_into('<h', payload, 2, 1)
            return 0
        with patch.object(vibe, '_ff_busy', False), \
                patch.object(vibe, '_ff_device_cache', {'node': '/dev/input/event9'}), \
                patch.object(vibe, '_active_values', return_value={vibe.PKEY_LEVEL: 2}), \
                patch.object(vibe, '_find_ff_device', return_value='/dev/input/event9'), \
                patch.object(vibe, '_get_device_path', return_value='/sys/hid/0003:17EF:61EB.0001'), \
                patch.object(vibe.os, 'open', return_value=43), \
                patch.object(vibe.os, 'close') as close, \
                patch.object(vibe.os, 'write', return_value=24) as output, \
                patch.object(vibe.fcntl, 'ioctl', side_effect=ioctl, create=True) as calls, \
                patch.object(vibe.asyncio, 'sleep', new=AsyncMock()), \
                patch.object(vibe.time, 'sleep'):
            result = await vibe.Plugin().test_vibration(100)
        self.assertFalse(result['success'], 'A reused descriptor must be rejected before its device receives output.')
        output.assert_not_called()
        self.assertNotIn(vibe._EVIOCSFF, [call.args[1] for call in calls.call_args_list])
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

    def profiles(self):
        return {key: {'overwrite': key != '0', 'settings': {**vibe.DEFAULT_PROFILE, 'level': level}}
                for key, level in (('0', 2), ('111', 0), ('222', 1))}

    def test_vibration_uses_current_process_or_global_after_report_expires(self):
        with patch.object(vibe, '_active_app_id', '111'), patch.object(vibe, '_active_app_ts', 100), \
                patch.object(vibe.time, 'monotonic', return_value=113), \
                patch.object(vibe, '_load_profiles', return_value=self.profiles()), \
                patch.object(vibe, 'scan_proc_for_appid', return_value='') as process:
            self.assertEqual(vibe._active_values()['level'], 2)
            payload = vibe._settings_payload()
            self.assertEqual((payload['app_id'], payload['profile_id']), ('0', '0'))
            process.return_value = '111'
            self.assertEqual(vibe._active_values()['level'], 0, 'A still-running game keeps its profile')
            process.return_value = '222'
            self.assertEqual(vibe._settings_payload()['app_id'], '222')
            self.assertEqual(vibe._active_values()['level'], 1)

    def test_unchanged_heartbeat_renews_lease_without_another_hardware_write(self):
        clock = [106]
        plugin = vibe.Plugin()
        plugin._active_applied = True
        with patch.object(vibe, '_active_app_id', '111'), patch.object(vibe, '_active_app_ts', 100), \
                patch.object(vibe.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(vibe, '_load_profiles', return_value=self.profiles()), \
                patch.object(vibe, '_last_applied_values', self.profiles()['111']['settings']), \
                patch.object(vibe, '_apply_settings') as apply, \
                patch.object(vibe, 'scan_proc_for_appid', return_value='') as process:
            result = asyncio.run(plugin.set_active_app('111'))
            self.assertTrue(result['success'])
            self.assertFalse(result['changed'])
            self.assertEqual(vibe._active_app_ts, 106)
            clock[0] = 117
            self.assertEqual(vibe._active_values()['level'], 0)
            apply.assert_not_called()
            process.assert_not_called()

    def test_returning_frontend_reapplies_after_fallback_changed_effective_profile(self):
        plugin = vibe.Plugin()
        plugin._active_applied = True
        with patch.object(vibe, '_active_app_id', '111'), patch.object(vibe, '_active_app_ts', 100), \
                patch.object(vibe.time, 'monotonic', return_value=113), \
                patch.object(vibe, '_load_profiles', return_value=self.profiles()), \
                patch.object(vibe, 'scan_proc_for_appid', return_value=''), \
                patch.object(vibe, '_apply_settings', return_value=True) as apply:
            self.assertEqual(vibe._active_values()['level'], 2)
            result = asyncio.run(plugin.set_active_app('111'))
            self.assertTrue(result['changed'])
            self.assertEqual(result['settings']['level'], 0)
            apply.assert_called_once()


class VibrationReadbackTests(unittest.TestCase):
    def setUp(self):
        self.cache = patch.object(vibe, '_attr_cache', {})
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_fresh_write_is_confirmed_before_caching(self):
        opened = mock_open(read_data='high\n')
        with patch('builtins.open', opened):
            self.assertTrue(vibe._write_attr('/mock', 'rumble_intensity', 'high', True))
        opened.return_value.read.assert_called_once()
        self.assertEqual(vibe._attr_cache[('/mock', 'rumble_intensity')], 'high')

    def test_delayed_readback_is_bounded_and_can_confirm(self):
        opened = mock_open()
        opened.return_value.read.side_effect = ['low\n', 'high\n']
        with patch('builtins.open', opened), patch.object(vibe.time, 'sleep') as pause:
            self.assertTrue(vibe._write_attr('/mock', 'rumble_intensity', 'high', True))
        self.assertEqual(opened.return_value.read.call_count, 2)
        pause.assert_called_once_with(vibe._READBACK_DELAY_S)

    def test_ignored_write_returns_failure_without_a_confirmed_cache_entry(self):
        opened = mock_open(read_data='low\n')
        with patch('builtins.open', opened), patch.object(vibe.time, 'sleep'):
            self.assertFalse(vibe._write_attr('/mock', 'rumble_intensity', 'high', True))
        self.assertEqual(opened.return_value.read.call_count, vibe._READBACK_ATTEMPTS)
        self.assertNotIn(('/mock', 'rumble_intensity'), vibe._attr_cache)

    def test_device_disappearing_during_readback_is_not_success(self):
        opened = mock_open()
        opened.return_value.read.side_effect = FileNotFoundError('controller disconnected')
        with patch('builtins.open', opened):
            self.assertFalse(vibe._write_attr('/mock', 'rumble_intensity', 'high', True))
        self.assertFalse(vibe._attr_cache)

    def test_partial_profile_failure_attempts_and_confirms_other_attributes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            initial = {'rumble_intensity': 'off', 'left_handle/rumble_mode': 'racing',
                'right_handle/rumble_mode': 'racing', 'touchpad/vibration_intensity': 'off',
                'touchpad/vibration_enabled': 'false'}
            for relative, value in initial.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(value)
            ignored = root / 'right_handle/rumble_mode'
            real_open = open
            def file(path, mode='r', *args, **kwargs):
                if Path(path) == ignored and mode == 'w':
                    return io.StringIO()  # Accept the write but leave sysfs unchanged.
                return real_open(path, mode, *args, **kwargs)
            caps = {'intensity': vibe.LEVEL_NAMES, 'mode': vibe.RUMBLE_MODES,
                    'tp_intensity': vibe.LEVEL_NAMES}
            with patch.object(vibe, '_read_valid_settings'), \
                    patch.object(vibe, '_capabilities', return_value=caps), \
                    patch('builtins.open', side_effect=file), patch.object(vibe.time, 'sleep'):
                self.assertFalse(vibe._apply_settings(dict(vibe.DEFAULT_PROFILE), raw, True))
            self.assertEqual((root / 'left_handle/rumble_mode').read_text().strip(), 'fps')
            self.assertEqual((root / 'right_handle/rumble_mode').read_text().strip(), 'racing')
            self.assertEqual((root / 'touchpad/vibration_enabled').read_text().strip(), 'true')
            self.assertNotIn((raw, 'right_handle/rumble_mode'), vibe._attr_cache)


class VibrationStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_retry_clears_transient_startup_error(self):
        plugin = vibe.Plugin()
        with patch.object(vibe.Plugin, '_setup_error', None), \
                patch.object(vibe, '_last_suspend_offset', None), \
                patch.object(vibe, '_active_app_ts', None), \
                patch.object(vibe, '_monitor_task', None), patch.object(vibe, '_drift_task', None), \
                patch.object(vibe, '_suspend_offset', return_value=0), \
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


class VibrationForegroundEdgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.device = self.root / 'hid'
        self.device.mkdir()
        for name, value in {
            'rumble_intensity': 'medium', 'left_handle/rumble_mode': 'fps',
            'right_handle/rumble_mode': 'fps', 'touchpad/vibration_intensity': 'medium',
            'touchpad/vibration_enabled': 'true',
        }.items():
            path = self.device / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding='ascii')
        self.store = AtomicSettingsManager('vibration_settings', str(self.root / 'settings'))
        self.store.replace({'schema_version': 2, 'game_profiles': {
            key: {'overwrite': key != '0', 'settings': {
                'level': level, 'mode': mode, 'touchpadIntensity': 2, 'touchpadEnabled': True}}
            for key, level, mode in (('0', 2, 0), ('111', 0, 0), ('222', 3, 1))
        }})
        self.clock = 100.0
        self.running = '111'
        self.writes = []
        real_open = builtins.open
        def opened(path, mode='r', *args, **kwargs):
            if 'w' in mode and Path(path).is_relative_to(self.device):
                self.writes.append((Path(path).relative_to(self.device).as_posix(), mode))
            return real_open(path, mode, *args, **kwargs)
        for name, value in {
            'settings': self.store, '_device_path': str(self.device), '_attr_cache': {},
            '_capabilities_cache': {}, '_last_applied_values': None,
            '_active_app_id': '111', '_active_app_ts': None,
        }.items():
            self.stack.enter_context(patch.object(vibe, name, value))
        self.stack.enter_context(patch.object(vibe, 'time', types.SimpleNamespace(
            monotonic=lambda: self.clock, sleep=vibe.time.sleep)))
        self.stack.enter_context(patch.object(vibe, 'scan_proc_for_appid', side_effect=lambda: self.running))
        self.stack.enter_context(patch.object(vibe, '_discover', return_value=(str(self.device), 'fixture')))
        self.stack.enter_context(patch('builtins.open', side_effect=opened))
        self.plugin = vibe.Plugin()

    async def asyncTearDown(self):
        await module_runtime.drain('vibration')

    def intensity(self):
        return (self.device / 'rumble_intensity').read_text(encoding='ascii').strip()

    def persisted(self):
        return AtomicSettingsManager('vibration_settings', str(self.root / 'settings')).settings

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
        real_open = builtins.open
        def ignored(path, mode='r', *args, **kwargs):
            if Path(path) == self.device / 'right_handle/rumble_mode' and mode == 'w':
                return io.StringIO()
            return real_open(path, mode, *args, **kwargs)
        with patch('builtins.open', side_effect=ignored):
            failed = await self.plugin.set_active_app('222')
        self.assertFalse(failed['success'])
        self.assertEqual(self.intensity(), 'high')
        self.assertEqual((self.device / 'right_handle/rumble_mode').read_text().strip(), 'fps')
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


class HotplugMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_failed_event_keeps_the_monitor_running(self):
        events, readers, handled = [], [], []
        class Monitor:
            @classmethod
            def from_netlink(cls, _ctx): return cls()
            def filter_by(self, **_): pass
            def start(self): pass
            def fileno(self): return 99
            def poll(self, _timeout): return events.pop(0) if events else None
        class Loop:  # Only the reader registration is replaced; the queue runs on the real loop.
            def add_reader(self, _fd, callback): readers.append(callback)
            def remove_reader(self, _fd): readers.clear()
        async def handle(path, _action):
            handled.append(path)
            if len(handled) == 1: raise OSError('settings read failed')
        with patch.object(vibe, '_PYUDEV', True),                 patch.object(vibe, '_pyudev', types.SimpleNamespace(Monitor=Monitor), create=True),                 patch.object(vibe, '_udev_ctx', None, create=True),                 patch.object(vibe, '_handle_device_added', new=handle),                 patch.object(vibe.module_runtime, 'is_paused', return_value=False),                 patch.object(vibe.asyncio, 'get_running_loop', return_value=Loop()):
            task = asyncio.create_task(vibe._monitor_hotplug())
            await asyncio.sleep(0)
            events.extend(types.SimpleNamespace(action='change', sys_path=path) for path in ('/a', '/b'))
            readers[0]()
            for _ in range(10): await asyncio.sleep(0)
            self.assertEqual(handled, ['/a', '/b'])
            self.assertFalse(task.done()); self.assertEqual(len(readers), 1)
            task.cancel(); await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()

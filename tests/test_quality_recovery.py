# SPDX-License-Identifier: BSD-3-Clause
"""Regression scenarios from the October audit; all device access is isolated."""
import asyncio
from contextlib import ExitStack
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, mock_open, patch

import test_integration
import active_app
import module_runtime
import tdp_backend as tdp
import tdp_updater
import vibration_backend as vibe


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


class HelperDownloadTests(unittest.TestCase):
    def helper(self):
        return tdp_updater.Updater(user_agent='test', log_prefix='test', plugin_dir='.', logger=Mock())

    def test_total_deadline_stops_a_stream_that_keeps_delivering_data(self):
        clock = [0]
        class SlowResponse(io.BytesIO):
            def read1(self, _size):
                clock[0] += 29
                return b'x'
            def read(self, _size):
                raise AssertionError('read1 must avoid a full-buffer blocking read')
        response, output = SlowResponse(), io.BytesIO()
        helper = self.helper()
        with patch.object(helper, 'open_url', return_value=response) as opened, \
                patch.object(tdp_updater.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(TimeoutError, 'total time limit'):
                helper.download_to(tdp.RYZENADJ_URL, output, timeout=30)
        opened.assert_called_once_with(tdp.RYZENADJ_URL, timeout=tdp_updater.READ_TIMEOUT_SECONDS)
        self.assertEqual(output.getvalue(), b'xxxx')
        self.assertTrue(response.closed)

    def test_cancelled_read_is_rejected_before_writing_the_returned_chunk(self):
        cancel = threading.Event()
        class Response(io.BytesIO):
            def read1(self, _size):
                cancel.set()
                return b'partial'
        output = io.BytesIO()
        helper = self.helper()
        with patch.object(helper, 'open_url', return_value=Response()):
            with self.assertRaisesRegex(InterruptedError, 'cancelled'):
                helper.download_to(tdp.RYZENADJ_URL, output, timeout=30, cancel=cancel.is_set)
        self.assertEqual(output.getvalue(), b'')

    def test_partial_archive_is_removed_without_replacing_existing_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            binary = Path(raw, 'ryzenadj')
            binary.write_bytes(b'previous verified helper')
            mkstemp = tempfile.mkstemp
            def temporary(**kwargs):
                kwargs.setdefault('dir', raw)
                return mkstemp(**kwargs)
            def stalled(_url, out, **_kwargs):
                out.write(b'partial archive')
                raise TimeoutError('total time limit')
            with patch.object(tdp, 'BIN_DIR', raw), patch.object(tdp, 'BIN_PATH', str(binary)), \
                    patch.object(tdp.tempfile, 'mkstemp', side_effect=temporary), \
                    patch.object(tdp.updater, 'download_to', side_effect=stalled):
                with self.assertRaises(TimeoutError):
                    tdp._download_ryzenadj()
            self.assertEqual(binary.read_bytes(), b'previous verified helper')
            self.assertEqual([path.name for path in Path(raw).iterdir()], ['ryzenadj'])

    def test_firmware_handoff_requires_balanced_readback(self):
        with tempfile.TemporaryDirectory() as raw:
            profile = Path(raw, 'platform_profile')
            profile.write_text('custom')
            with patch.object(tdp, '_profile_path', return_value=str(profile)), \
                    patch.object(tdp, '_wmi_caps', return_value={'supported': True}), \
                    patch.object(tdp, '_write_profile', return_value=True), \
                    patch.object(tdp, '_run_ryzenadj') as helper:
                self.assertFalse(tdp._restore_defaults_locked()['success'])
            helper.assert_not_called()


class HelperLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_retry_adopts_verified_helper_without_duplicate_downloads(self):
        entered, finish = threading.Event(), threading.Event()
        def ensure(**_kwargs):
            entered.set()
            if not finish.wait(2):
                raise TimeoutError('test did not release download')
            return True
        plugin = tdp.Plugin()
        with patch.object(tdp, '_wmi_only', return_value=False), \
                patch.object(tdp, '_ryzenadj_available', False), \
                patch.object(tdp, '_current_game_id', '111'), \
                patch.object(tdp, '_transition_key', ('111',)), \
                patch.object(tdp, '_ensure_ryzenadj', side_effect=ensure) as verified:
            task = asyncio.create_task(plugin.retry_extras())
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.005)
                self.assertTrue(entered.is_set())
                self.assertFalse((await plugin.retry_extras())['success'])
            finally:
                finish.set()
                result = await asyncio.wait_for(task, 1)
            self.assertTrue(result['success'])
            verified.assert_called_once()
            self.assertTrue(tdp._ryzenadj_available)
            self.assertIsNone(tdp._transition_key)
            self.assertFalse(plugin._extras_retry_busy)

    async def test_firmware_start_finishes_while_optional_download_is_still_running(self):
        entered, finish = threading.Event(), threading.Event()
        def ensure(cancel=None, download=True):
            if not download:
                return False
            entered.set()
            while not finish.wait(.01):
                if cancel():
                    raise InterruptedError('cancelled')
            return True
        plugin = tdp.Plugin()
        with ExitStack() as stack:
            for name, value in {'_ryzenadj_available': False, '_current_game_id': '', '_transition_key': None}.items():
                stack.enter_context(patch.object(tdp, name, value))
            for name, value in {'_wmi_only': False, '_wmi_caps': {'supported': True}, '_get_ac_online': False,
                    '_suspend_offset': 0, '_load_settings': {'enabled': False},
                    '_startup_context_target': ('', (16000, 25000, 30000), False),
                    '_reapply_saved_cpu_power_controls_locked': [], '_cancel_ac_settle': None}.items():
                stack.enter_context(patch.object(tdp, name, return_value=value))
            stack.enter_context(patch.object(tdp.updater, 'ssl_context'))
            stack.enter_context(patch.object(tdp, '_ensure_ryzenadj', side_effect=ensure))
            stack.enter_context(patch.object(plugin, '_enforce_loop', new=AsyncMock()))
            stack.enter_context(patch.object(plugin, '_info_loop', new=AsyncMock()))
            try:
                await asyncio.wait_for(plugin._main(), 1)
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                self.assertTrue(tdp.Plugin._ready)
                self.assertFalse(tdp._ryzenadj_available)
                await asyncio.wait_for(plugin._unload(), 1)
                await asyncio.wait_for(module_runtime.drain('tdp'), 1)
                self.assertFalse(tdp._ryzenadj_available, 'A closed module cannot adopt a late download')
            finally:
                finish.set()
                await plugin._unload()
                await module_runtime.drain('tdp')


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


if __name__ == '__main__':
    unittest.main()

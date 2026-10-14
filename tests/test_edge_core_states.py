# SPDX-License-Identifier: BSD-3-Clause
"""Combined foreground, fallback and partial-write states using real local files."""
import asyncio
import builtins
from contextlib import ExitStack
import copy
import io
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import test_integration
import module_runtime
from safe_settings import AtomicSettingsManager
import vibration_backend as vibe


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


if __name__ == '__main__':
    unittest.main()

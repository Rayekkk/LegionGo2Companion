# SPDX-License-Identifier: BSD-3-Clause
"""Power availability and release scenarios from the second quality audit."""
from contextlib import ExitStack
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_integration
import main
import module_control
import module_runtime
import tdp_backend as tdp


class PowerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await module_runtime.drain('tdp')

    async def test_release_uses_balanced_when_a_ppt_capability_is_missing(self):
        for operation in ('uninstall', 'module_disable'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as raw:
                profile = Path(raw, 'platform_profile')
                profile.write_text('custom')
                Path(raw, 'choices').write_text('balanced custom')
                firmware = Path(raw, 'firmware')
                for key, directory in tdp.WMI_ATTRS.items():
                    entry = firmware / directory
                    entry.mkdir(parents=True)
                    (entry / 'min_value').write_text('5')
                    if key != 'fppt':
                        (entry / 'max_value').write_text('35')
                plugin = tdp.Plugin()
                with patch.object(tdp, 'PLATFORM_PROFILE_GLOB', str(profile)), \
                        patch.object(tdp, 'WMI_ROOT', str(firmware)), \
                        patch.object(tdp, '_ryzenadj_available', False), \
                        patch.object(tdp, '_load_settings', return_value={'enabled': True}), \
                        patch.object(tdp, '_load_profiles', return_value={}), \
                        patch.object(tdp, '_run_ryzenadj') as helper:
                    self.assertEqual(tdp._wmi_caps(), {})
                    if operation == 'uninstall':
                        await plugin._uninstall()
                    else:
                        await module_control.withdraw('tdp', plugin)
                    self.assertEqual(profile.read_text(), 'balanced')
                    helper.assert_not_called()

    async def test_incomplete_caps_do_not_turn_unconfirmed_release_into_success(self):
        with tempfile.TemporaryDirectory() as raw:
            profile = Path(raw, 'platform_profile')
            profile.write_text('custom')
            with patch.object(tdp, '_profile_path', return_value=str(profile)), \
                    patch.object(tdp, '_wmi_caps', return_value={}), \
                    patch.object(tdp, '_write_profile', return_value=True), \
                    patch.object(tdp, '_load_settings', return_value={'enabled': True}), \
                    patch.object(tdp, '_load_profiles', return_value={}), \
                    patch.object(tdp, '_run_ryzenadj') as helper:
                with self.assertRaisesRegex(RuntimeError, 'Balanced firmware profile was not confirmed'):
                    await tdp.Plugin()._uninstall()
                self.assertEqual(profile.read_text(), 'custom')
                helper.assert_not_called()

    async def test_caps_distinguish_both_missing_paths_from_firmware_and_helper(self):
        plugin = tdp.Plugin()
        firmware = {key: {'min': 5, 'max': 35} for key in tdp.WMI_ATTRS}
        with patch.object(tdp, '_wmi_only', return_value=False), \
                patch.object(tdp, '_wmi_caps', return_value={}) as caps, \
                patch.object(tdp, '_profile_path', return_value='/mock/profile') as profile, \
                patch.object(tdp, '_ryzenadj_available', False), \
                patch.object(tdp.os.path, 'isfile', return_value=False):
            unavailable = await plugin.get_caps()
            self.assertFalse(unavailable['tdp_available'])
            self.assertTrue(unavailable['helper_retry_available'])
            self.assertFalse(unavailable['extras'])
            self.assertIn('unavailable', unavailable['tdp_error'])
            caps.return_value = firmware
            available = await plugin.get_caps()
            self.assertTrue(available['tdp_available'])
            self.assertFalse(available['extras'])
            self.assertEqual(available['tdp_error'], '')
            profile.return_value = None
            self.assertFalse((await plugin.get_caps())['tdp_available'])
        with patch.object(tdp, '_wmi_only', return_value=False), \
                patch.object(tdp, '_wmi_caps', return_value={}), \
                patch.object(tdp, '_ryzenadj_available', True), \
                patch.object(tdp.os.path, 'isfile', return_value=True):
            helper = await plugin.get_caps()
            self.assertTrue(helper['tdp_available'])
            self.assertTrue(helper['extras'])
            self.assertFalse(helper['helper_retry_available'])

    async def test_firmware_only_does_not_offer_an_unsupported_helper_retry(self):
        with patch.object(tdp, '_wmi_only', return_value=True), \
                patch.object(tdp, '_wmi_caps', return_value={}), \
                patch.object(tdp, '_ryzenadj_available', True), \
                patch.object(tdp, '_ensure_ryzenadj') as ensure:
            plugin = tdp.Plugin()
            caps = await plugin.get_caps()
            self.assertFalse(caps['tdp_available'])
            self.assertFalse(caps['extras'])
            self.assertFalse(caps['helper_retry_available'])
            self.assertFalse((await plugin.retry_extras())['success'])
            ensure.assert_not_called()

    async def test_failed_download_is_visible_then_verified_retry_updates_caps(self):
        plugin = tdp.Plugin()
        with patch.object(tdp, '_wmi_only', return_value=False), \
                patch.object(tdp, '_wmi_caps', return_value={}), \
                patch.object(tdp, '_ryzenadj_available', False), \
                patch.object(tdp, '_current_game_id', ''), \
                patch.object(tdp, '_transition_key', None), \
                patch.object(tdp.os.path, 'isfile', return_value=False) as present, \
                patch.object(tdp, '_ensure_ryzenadj', side_effect=TimeoutError('offline')) as ensure:
            self.assertFalse((await plugin.retry_extras())['success'])
            caps = await plugin.get_caps()
            self.assertFalse(caps['tdp_available'])
            self.assertIn('offline', caps['tdp_error'])
            ensure.side_effect = None
            ensure.return_value = True
            present.return_value = True
            self.assertTrue((await plugin.retry_extras())['success'])
            caps = await plugin.get_caps()
            self.assertTrue(caps['tdp_available'])
            self.assertEqual(caps['tdp_error'], '')
            self.assertTrue(caps['extras'])
            self.assertFalse(caps['helper_retry_available'])

    async def test_offline_start_keeps_cpu_module_ready_but_reports_tdp_unavailable(self):
        def ensure(cancel=None, download=True):
            if not download:
                return False
            raise TimeoutError('offline')
        plugin = tdp.Plugin()
        with ExitStack() as stack:
            for name, value in {'_wmi_caps': {}, '_wmi_only': False, '_profile_path': None,
                    '_get_ac_online': False, '_get_running_appid': '', '_suspend_offset': 0,
                    '_load_settings': {'enabled': True, 'spl': 16000, 'sppt': 25000,
                        'fppt': 30000, 'cpu_boost_enabled': None, 'epp': None},
                    '_load_profiles': {}, '_cancel_ac_settle': None}.items():
                stack.enter_context(patch.object(tdp, name, return_value=value))
            stack.enter_context(patch.object(tdp.Plugin, '_ready', False))
            stack.enter_context(patch.object(tdp.Plugin, '_setup_error', None))
            stack.enter_context(patch.object(tdp, '_ryzenadj_available', False))
            for name, value in {'_current_game_id': '', '_transition_key': None,
                    '_last_cpu_power_check': 0, '_current_ac_online': False,
                    '_last_suspend_offset': None}.items():
                stack.enter_context(patch.object(tdp, name, value))
            stack.enter_context(patch.object(tdp.updater, 'ssl_context'))
            stack.enter_context(patch.object(tdp, '_ensure_ryzenadj', side_effect=ensure))
            stack.enter_context(patch.object(plugin, '_enforce_loop', new=AsyncMock()))
            stack.enter_context(patch.object(plugin, '_info_loop', new=AsyncMock()))
            try:
                await plugin._main()
                await asyncio.gather(*plugin._tasks)
                self.assertTrue((await plugin.is_ready())['ready'])
                caps = await plugin.get_caps()
                self.assertFalse(caps['tdp_available'])
                self.assertIn('offline', caps['tdp_error'])
                self.assertFalse(tdp._apply_limits(16000, 25000, 30000)['success'])
            finally:
                await plugin._unload()

    async def test_root_forwards_reset_context_guards(self):
        plugin = main.Plugin()
        plugin._vibration.reset_to_default = AsyncMock(return_value={'success': True})
        await plugin.vibe_reset_to_default('111', '111')
        plugin._vibration.reset_to_default.assert_awaited_once_with('111', '111')


if __name__ == '__main__':
    unittest.main()

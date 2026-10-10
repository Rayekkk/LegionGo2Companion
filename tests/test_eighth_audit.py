# SPDX-License-Identifier: BSD-3-Clause
"""Live display withdrawal and pre-request download redirect boundaries."""
import unittest
import types
import stat
import tempfile
import io
from email.message import Message
from pathlib import Path
import urllib.request
import urllib.response
from unittest.mock import AsyncMock, Mock, patch

import test_integration
import display_backend as display
import module_control
import tdp_updater
import wifi_backend as wifi
import safe_settings
import conflict_guard


class DisplayWithdrawalTests(unittest.IsolatedAsyncioTestCase):
    def component(self, probe, session):
        return types.SimpleNamespace(
            cleanup_native_display=AsyncMock(return_value={}), _unload=AsyncMock(),
            native_display_active=lambda: False, _release=lambda: True,
            _edid_restore=lambda: True, _session_props=probe,
            _ownership_session_current=session)

    async def test_withdrawal_ignores_stale_internal_output_snapshot(self):
        reads = []
        def probe(names, *, fresh=False):
            reads.append(fresh)
            return {display.ATOM_IS_EXTERNAL: '1' if fresh else '0'}
        c = self.component(probe, lambda: True)
        with patch.object(display, '_write_atom_int', return_value=True) as write, \
                patch.object(display, '_uninstall_script', return_value='removed'):
            await module_control.withdraw('display', c)
        self.assertEqual(reads, [True])
        write.assert_called_once_with(display.ATOM_FORCE_HDR_SUPPORT, 0)

    async def test_new_session_during_probe_prevents_atom_writes(self):
        c = self.component(Mock(return_value={display.ATOM_IS_EXTERNAL: '0'}),
                           Mock(side_effect=[True, False]))
        with patch.object(display, '_write_atom_int', return_value=True) as write, \
                patch.object(display, '_uninstall_script', return_value='removed') as uninstall:
            await module_control.withdraw('display', c)
        write.assert_not_called()
        uninstall.assert_called_once()

    async def test_unknown_output_keeps_cleanup_pending(self):
        for props in ({}, {display.ATOM_IS_EXTERNAL: 'invalid'}):
            c = self.component(Mock(return_value=props), lambda: True)
            with self.subTest(props=props), \
                    patch.object(display, '_write_atom_int', return_value=True) as write, \
                    patch.object(display, '_uninstall_script') as uninstall:
                with self.assertRaisesRegex(RuntimeError, 'verify'):
                    await module_control.withdraw('display', c)
            write.assert_not_called()
            uninstall.assert_not_called()

    async def test_unbound_recovery_removes_script_without_changing_session_atoms(self):
        # Pending cleanup runs before _main and may have no bound compositor.
        c = self.component(Mock(), lambda: None)
        with patch.object(display, '_write_atom_int', return_value=True) as write, \
                patch.object(display, '_uninstall_script', return_value='removed') as uninstall:
            note = await module_control.withdraw('display', c)
        write.assert_not_called()
        c._session_props.assert_not_called()
        uninstall.assert_called_once()
        self.assertIn('Restart', note)

    async def test_confirmed_internal_session_releases_both_atoms(self):
        c = self.component(Mock(return_value={display.ATOM_IS_EXTERNAL: '0'}), lambda: True)
        with patch.object(display, '_write_atom_int', return_value=True) as write, \
                patch.object(display, '_uninstall_script', return_value='removed'):
            await module_control.withdraw('display', c)
        self.assertEqual([call.args for call in write.call_args_list], [
            (display.ATOM_FORCE_HDR_SUPPORT, 0), (display.ATOM_HDR_ENABLED, 0)])


class HelperRedirectTests(unittest.TestCase):
    def test_intermediate_foreign_redirect_is_never_opened(self):
        opened = []
        class OfflineHttps(urllib.request.HTTPSHandler):
            def https_open(self, request):
                opened.append(request.full_url)
                headers = Message()
                headers['Location'] = 'https://foreign.example/asset'
                response = urllib.response.addinfourl(io.BytesIO(b''), headers, request.full_url, 302)
                response.msg = 'Found'
                return response
        transport = OfflineHttps()
        real_build = urllib.request.build_opener
        helper = tdp_updater.Updater(user_agent='test', log_prefix='test', plugin_dir='.', logger=Mock())
        def build(*handlers):
            return real_build(transport, *handlers[1:])
        with patch.object(tdp_updater.urllib.request, 'build_opener', side_effect=build):
            with self.assertRaises(ValueError):
                helper.open_url('https://github.com/test/asset', 3)
        self.assertEqual(opened, ['https://github.com/test/asset'])

    def test_redirect_boundary_is_checked_before_followup_request(self):
        helper = tdp_updater.Updater(user_agent='test', log_prefix='test', plugin_dir='.', logger=Mock())
        opener = Mock()
        response = opener.open.return_value
        response.geturl.return_value = 'https://github.com/test/asset'
        with patch.object(tdp_updater.urllib.request, 'build_opener', return_value=opener) as build, \
                patch.object(tdp_updater.urllib.request, 'urlopen', return_value=response) as unsafe:
            self.assertIs(helper.open_url('https://github.com/test/asset', 3), response)
        unsafe.assert_not_called()
        handler = next(value for value in build.call_args.args
                       if isinstance(value, urllib.request.HTTPRedirectHandler))
        request = urllib.request.Request('https://github.com/test/asset')
        for url in ('http://github.com/test/asset', 'https://example.org/asset',
                    'https://user:password@github.com/asset', 'https://github.com:8443/asset'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, 'Found', {}, url)
        allowed = 'https://release-assets.githubusercontent.com/asset?token=fixture'
        self.assertEqual(handler.redirect_request(request, None, 302, 'Found', {}, allowed).full_url, allowed)

    def test_final_response_boundary_still_closes_invalid_endpoint(self):
        helper = tdp_updater.Updater(user_agent='test', log_prefix='test', plugin_dir='.', logger=Mock())
        opener = Mock()
        response = opener.open.return_value
        response.geturl.return_value = 'https://example.org/asset'
        with patch.object(tdp_updater.urllib.request, 'build_opener', return_value=opener), \
                patch.object(tdp_updater.urllib.request, 'urlopen', return_value=response), \
                self.assertRaises(ValueError):
            helper.open_url('https://github.com/test/asset', 3)
        response.close.assert_called_once()


class WifiConnectionIdentityTests(unittest.TestCase):
    OWN = 'da58df8b-cdb1-447d-80ab-19d11a634e7e'
    OTHER = '85194f4c-d496-4eec-bae0-d880b4cbcf26'

    def test_ui_connection_requires_completed_networkmanager_activation(self):
        plugin = wifi.Plugin()
        for state, expected in (('50 (connecting (configuring))', None),
                                ('70 (connecting (getting IP configuration))', None),
                                ('100 (connected)', self.OWN), ('100', self.OWN),
                                ('1000', None), ('', None)):
            with self.subTest(state=state), patch.object(plugin, '_run_cmd', return_value={
                    'success': True, 'stdout': state + '\n' + self.OWN}) as command:
                self.assertEqual(plugin._get_active_connection_uuid(
                    'wlan0', require_connected=True), expected)
                self.assertEqual(command.call_count, 1)
                self.assertIn('GENERAL.STATE,GENERAL.CON-UUID', command.call_args.args[0])
                self.assertEqual(command.call_args.kwargs['timeout'], 2)

    def responses(self, device_uuid):
        def run(argv, **kwargs):
            if argv[-2:] == ['device', 'show']:
                raise AssertionError('A UUID read must name its interface.')
            if 'GENERAL.CON-UUID' in argv:
                self.assertEqual(argv[-1], 'wlan0')
                return {'success': True, 'stdout': device_uuid + '\n'}
            if 'GENERAL.STATE,GENERAL.CON-UUID' in argv:
                self.assertEqual(argv[-1], 'wlan0')
                return {'success': True, 'stdout': f'100 (connected)\n{device_uuid}\n'}
            raise AssertionError(argv)
        return run

    def test_selected_interface_cannot_inherit_another_adapters_profile(self):
        plugin = wifi.Plugin()
        with patch.object(plugin, '_get_wifi_interface', return_value='wlan0'), \
                patch.object(plugin, '_run_cmd', side_effect=self.responses(self.OWN)):
            self.assertEqual(plugin._get_active_connection_uuid(), self.OWN)

    def test_disconnected_interface_cannot_borrow_another_active_profile(self):
        plugin = wifi.Plugin()
        for value in ('', '--', 'invalid', self.OWN + '\n' + self.OTHER):
            with self.subTest(value=value), \
                    patch.object(plugin, '_get_wifi_interface', return_value='wlan0'), \
                    patch.object(plugin, '_run_cmd', side_effect=self.responses(value)):
                self.assertIsNone(plugin._get_active_connection_uuid())

    def test_captured_interface_reuses_discovery_and_activity_belongs_to_that_adapter(self):
        plugin = wifi.Plugin()
        with patch.object(plugin, '_get_wifi_interface') as discovery, \
                patch.object(plugin, '_run_cmd', side_effect=self.responses(self.OWN)) as command:
            self.assertEqual(plugin._get_active_connection_uuid('wlan0'), self.OWN)
            discovery.assert_not_called()
            self.assertEqual(command.call_count, 1, 'A captured interface needs one device UUID query.')
            self.assertTrue(plugin._profile_is_active(self.OWN, 'wlan0'))
            self.assertFalse(plugin._profile_is_active(self.OTHER, 'wlan0'))
            self.assertFalse(plugin._profile_is_active('unknown', 'wlan0'))
            discovery.assert_not_called()


class NonblockingMetadataTests(unittest.TestCase):
    def test_settings_open_cannot_wait_for_a_fifo_replaced_after_lstat(self):
        with patch.object(safe_settings.os, 'O_NONBLOCK', 0x800, create=True), \
                patch.object(safe_settings.os, 'open', return_value=42) as opened:
            self.assertEqual(safe_settings._open_readonly_nofollow('fixture.json'), 42)
        self.assertTrue(opened.call_args.args[1] & 0x800,
                        'A later descriptor type check cannot help if open itself blocks on a replaced FIFO.')

    def test_manifest_descriptor_type_is_checked_before_reading(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'Companion').mkdir()
            sibling = root / 'Other'
            sibling.mkdir()
            (sibling / 'plugin.json').write_text('{"name":"Other"}', encoding='utf-8')
            stream = Mock()
            stream.fileno.return_value = 42
            handle = Mock()
            handle.__enter__ = Mock(return_value=stream)
            handle.__exit__ = Mock(return_value=False)
            with patch.object(conflict_guard.os, 'O_NONBLOCK', 0x800, create=True), \
                    patch.object(conflict_guard.os, 'open', return_value=42) as opened, \
                    patch.object(conflict_guard.os, 'fdopen', return_value=handle), \
                    patch.object(conflict_guard.os, 'close') as closed, \
                    patch.object(conflict_guard.os, 'fstat', return_value=types.SimpleNamespace(st_mode=stat.S_IFIFO)):
                with self.assertRaisesRegex(ValueError, 'regular file'):
                    conflict_guard.installed_conflicts(str(root / 'Companion'))
            self.assertTrue(opened.call_args.args[1] & 0x800)
            closed.assert_called_once_with(42)
            stream.read.assert_not_called()


class WifiLockFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_file_lock_reports_error_without_synchronous_settings_io(self):
        plugin = wifi.Plugin()
        with patch.object(plugin, '_acquire_band_policy_file_lock', side_effect=PermissionError('lock unavailable')), \
                patch.object(wifi, '_load_settings', side_effect=AssertionError('Settings I/O on the event loop')) as read, \
                patch.object(plugin, '_set_band_policy_impl') as mutate:
            result = await plugin.set_band_policy(wifi.BAND_POLICY_HIGH_ONLY)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'band_policy_lock_failed')
        self.assertIn('lock unavailable', result['detail'])
        read.assert_not_called()
        mutate.assert_not_called()


if __name__ == '__main__':
    unittest.main()

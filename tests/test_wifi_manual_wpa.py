# SPDX-License-Identifier: BSD-3-Clause
"""One-time WPA band selection must preserve normal profile and recovery."""
import json
import unittest
from unittest.mock import patch

import test_wifi_strict as fixtures
import wifi_backend as wifi

UUID = fixtures.UUID
SIX = 'aa:29:48:e1:7e:03'
FIVE = 'a8:29:48:e1:7e:01'


class ManualWpaTests(unittest.TestCase):
    setUp = fixtures.StrictWifiTests.setUp

    def configure(self):
        self.iwd.unlink(missing_ok=True)
        state = dict(wifi.DEFAULT_SETTINGS)
        self.settings.write_text(json.dumps(state), encoding='utf-8')
        self.fields = {'802-11-wireless.bssid': '', '802-11-wireless.band': ''}
        self.frequency = 5260
        self.bssid = FIVE
        for name, value in {'_get_current_backend': 'wpa_supplicant',
                            '_get_wifi_interface': 'wlan0',
                            '_get_enabled_high_band_frequencies': ([5260], [6295])}.items():
            self.stack.enter_context(patch.object(self.plugin, name, return_value=value))
        self.stack.enter_context(patch.object(self.plugin, '_nmcli_get', side_effect=
                                              lambda _uuid, field, **kw: (self.fields[field], {'success': True})))
        self.modify = self.stack.enter_context(patch.object(self.plugin, '_nmcli_modify', side_effect=self.write))
        self.stack.enter_context(patch.object(self.plugin, '_get_link_frequency', side_effect=lambda *_: self.frequency))
        self.stack.enter_context(patch.object(self.plugin, '_wait_for_profile_frequency', side_effect=lambda *_: self.frequency))
        self.stack.enter_context(patch.object(self.plugin, '_get_link_bssid', side_effect=lambda *_: self.bssid))
        self.reconnect = self.stack.enter_context(patch.object(self.plugin, '_reconnect_profile', side_effect=self.activate))
        self.scan = self.stack.enter_context(patch.object(self.plugin, '_scan_high_band_candidates_for_active_ssid', return_value={
            'scan_ok': True, 'candidates': [{'bssid': FIVE, 'frequency': 5260, 'signal_dbm': -20},
                                          {'bssid': SIX, 'frequency': 6295, 'signal_dbm': -40}]}))
        self.before = wifi._load_settings()

    def write(self, _uuid, field, value, **_kw):
        self.fields[field] = value
        return {'success': True}

    def activate(self, _uuid, *_args, **_kw):
        self.bssid = self.fields['802-11-wireless.bssid']
        self.frequency = 6295 if self.bssid == SIX else 5260
        return {'success': True}

    def test_six_selection_from_five_filters_stronger_five_and_clears_pin(self):
        self.configure()
        result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertTrue(result['success'], result)
        self.assertEqual(result['frequency'], 6295)
        self.assertTrue(result['temporary_bssid_cleared'])
        self.assertEqual(self.fields, {'802-11-wireless.bssid': '', '802-11-wireless.band': ''})
        self.assertEqual(wifi._load_settings(), self.before)
        self.assertFalse((self.journal_dir / 'band-policy-transaction.json').exists())
        self.reconnect.assert_called_once_with(UUID, True, cycle=True, bound_iface='wlan0')
        self.assertEqual(self.scan.call_args.kwargs['minimum_frequency'], 5925)

    def test_already_six_has_no_scan_or_network_cycle(self):
        self.configure(); self.frequency = 6295
        result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertTrue(result['success']); self.assertFalse(result['reconnected'])
        self.scan.assert_not_called(); self.modify.assert_not_called()

    def test_missing_six_does_not_connect_to_five_or_modify_profile(self):
        self.configure(); self.scan.return_value['candidates'] = [{'bssid': FIVE, 'frequency': 5260}]
        result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertTrue(result['success']); self.assertFalse(result['better_band_found'])
        self.modify.assert_not_called(); self.reconnect.assert_not_called()

    def test_connection_changed_during_scan_preserves_profile(self):
        self.configure()
        with patch.object(self.plugin, '_get_active_connection_uuid', side_effect=[UUID, 'external']):
            result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertEqual(result['error'], 'connection_changed')
        self.modify.assert_not_called()

    def test_external_band_or_bssid_is_not_replaced(self):
        for field, value in [('802-11-wireless.band', 'a'), ('802-11-wireless.bssid', FIVE)]:
            with self.subTest(field=field):
                self.configure(); self.fields[field] = value
                result = self.plugin._rescan_and_reconnect_sync('six')
                self.assertFalse(result['success']); self.modify.assert_not_called()

    def test_profile_changed_during_scan_preserves_external_selection(self):
        self.configure()
        def scan(*_args, **_kw):
            self.fields['802-11-wireless.bssid'] = FIVE
            return {'scan_ok': True, 'candidates': [{'bssid': SIX, 'frequency': 6295}]}
        self.scan.side_effect = scan
        result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertEqual(result['error'], 'profile_changed')
        self.modify.assert_not_called()

    def test_failed_activation_restores_pin_and_keeps_automatic_policy(self):
        self.configure(); self.reconnect.side_effect = None; self.reconnect.return_value = {'success': False}
        result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertFalse(result['success']); self.assertTrue(result['rolled_back'], result)
        self.assertEqual(self.fields['802-11-wireless.bssid'], '')
        self.assertEqual(wifi._load_settings()['band_policy'], wifi.BAND_POLICY_OFF)

    def test_invalid_target_is_rejected_without_settings_or_commands(self):
        with patch.object(wifi, '_load_settings') as read:
            for target in (None, False, 6, [], {}, 'five'):
                self.assertEqual(self.plugin._rescan_and_reconnect_sync(target)['error'], 'invalid_target_band')
            read.assert_not_called()

    def test_failed_rollback_preserves_independent_watchdog(self):
        self.configure(); self.reconnect.side_effect = None; self.reconnect.return_value = {'success': False}
        with patch.object(self.plugin, '_restore_band_policy_transaction',
                          return_value={'success': False, 'conflicts': ['temporary failure']}), \
             patch.object(self.plugin, '_cancel_band_policy_rollback') as cancel:
            result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertFalse(result['success']); cancel.assert_not_called()
        self.assertTrue((self.journal_dir / 'band-policy-transaction.json').exists())

    def test_external_pin_changed_while_connecting_is_not_cleared(self):
        self.configure()
        def activation(*_args, **_kw):
            self.bssid = SIX; self.frequency = 6295
            self.fields['802-11-wireless.bssid'] = FIVE
            return {'success': True}
        self.reconnect.side_effect = activation
        with patch.object(self.plugin, '_cancel_band_policy_rollback') as cancel:
            result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertFalse(result['success']); self.assertFalse(result['rolled_back'])
        self.assertEqual(self.fields['802-11-wireless.bssid'], FIVE)
        cancel.assert_not_called()

    def test_link_lost_while_clearing_pin_cannot_report_success(self):
        self.configure()
        def write(_uuid, field, value, **kw):
            result = self.write(_uuid, field, value, **kw)
            if not value:
                self.frequency = None
            return result
        self.modify.side_effect = write
        result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertFalse(result['success'])

    def test_scan_preserves_ssid_spaces_and_excludes_trimmed_lookalike(self):
        plugin = wifi.Plugin()
        commands = []
        def command(argv, **_kw):
            commands.append(argv)
            if argv[0] == '/usr/bin/nmcli':
                return {'success': True, 'stdout': UUID + '\n Network \nno'}
            return {'success': True, 'stdout':
                    'BSS ' + FIVE + '\n\tfreq: 5260\n\tsignal: -20 dBm\n\tSSID: Network\n'
                    'BSS ' + SIX + '\n\tfreq: 6295\n\tsignal: -40 dBm\n\tSSID:  Network \n'}
        with patch.object(plugin, '_run_cmd', side_effect=command):
            result = plugin._scan_high_band_candidates_for_active_ssid('wlan0', UUID)
        self.assertEqual(result['ssid'], ' Network ')
        self.assertEqual([c['bssid'] for c in result['candidates']], [SIX])
        self.assertEqual(commands[1][-1], ' Network ')

    def test_six_scan_continues_after_broad_scan_finds_only_five(self):
        plugin = wifi.Plugin()
        commands = []
        def command(argv, **_kw):
            commands.append(argv)
            if argv[0] == '/usr/bin/nmcli':
                return {'success': True, 'stdout': UUID + '\nNetwork\nno'}
            frequency, bssid = (6295, SIX) if 'freq' in argv else (5260, FIVE)
            return {'success': True, 'stdout':
                    f'BSS {bssid}\n\tfreq: {frequency}\n\tsignal: -30 dBm\n\tSSID: Network\n'}
        with patch.object(plugin, '_run_cmd', side_effect=command):
            result = plugin._scan_high_band_candidates_for_active_ssid(
                'wlan0', UUID, [5260], [6295], minimum_frequency=5925)
        self.assertTrue(result['scan_ok'])
        self.assertEqual([c['bssid'] for c in result['candidates']], [SIX])
        self.assertEqual(commands[2], ['/usr/bin/iw', 'dev', 'wlan0', 'scan',
                                      'freq', '6295', 'ssid', 'Network'])

    def test_default_high_scan_stops_after_first_five_candidate(self):
        plugin = wifi.Plugin()
        with patch.object(plugin, '_run_cmd', side_effect=[
            {'success': True, 'stdout': UUID + '\nNetwork\nno'},
            {'success': True, 'stdout':
             f'BSS {FIVE}\n\tfreq: 5260\n\tsignal: -30 dBm\n\tSSID: Network\n'},
        ]) as command:
            result = plugin._scan_high_band_candidates_for_active_ssid(
                'wlan0', UUID, [5260], [6295])
        self.assertEqual([c['bssid'] for c in result['candidates']], [FIVE])
        self.assertEqual(command.call_count, 2)


if __name__ == '__main__':
    unittest.main()

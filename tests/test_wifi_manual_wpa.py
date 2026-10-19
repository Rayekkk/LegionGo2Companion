# SPDX-License-Identifier: BSD-3-Clause
"""One-time WPA band selection must preserve normal profile and recovery."""
import itertools
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

    def test_watchdog_rollback_does_not_block_the_next_manual_reconnect(self):
        self.configure(); self.reconnect.side_effect = None; self.reconnect.return_value = {'success': False}
        with patch.object(self.plugin, '_profile_is_active', return_value=False):  # worker rollback fails too
            self.assertFalse(self.plugin._rescan_and_reconnect_sync('six')['success'])
        self.assertEqual(self.plugin._load_band_policy_journal()['phase'], 'rollback_failed')
        # The systemd watchdog fires once the network is reachable again.
        self.reconnect.side_effect = self.activate
        path = str(self.journal_dir / 'band-policy-transaction.json')
        plugin = self.plugin
        class SamePlugin(wifi.Plugin):  # keeps Plugin's static helpers real, unlike a Mock
            def __new__(cls):
                return plugin
        with patch.object(wifi, 'Plugin', SamePlugin):
            self.assertEqual(wifi._run_band_policy_rollback_cli(path), 0)
        self.assertEqual(self.plugin._load_band_policy_journal()['phase'], 'rolled_back')
        self.assertEqual(self.fields['802-11-wireless.bssid'], '')
        result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertTrue(result['success'], result)
        self.assertEqual(result['frequency'], 6295)
        self.assertIsNone(self.plugin._load_band_policy_journal())

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
                    'BSS ' + SIX + '\n\tfreq: 6295\n\tsignal: -40 dBm\n\tSSID: \\x20Network\\x20\n'}
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


class FakeNetworkManager:
    """nmcli semantics that matter here: `con down` of an inactive profile exits 10."""

    def __init__(self, unreachable_bssid):
        self.active = True
        self.fields = {'802-11-wireless.bssid': '', '802-11-wireless.band': ''}
        self.unreachable = unreachable_bssid
        self.log = []

    def run(self, argv, **_kw):
        self.log.append(argv)
        if argv[:3] == ['/usr/bin/nmcli', 'con', 'down']:
            if not self.active:
                return {'success': False, 'stdout': '', 'returncode': 10,
                        'stderr': "Error: '%s' is not an active connection." % argv[-1]}
            self.active = False
            return {'success': True, 'stdout': '', 'stderr': '', 'returncode': 0}
        if argv[:3] == ['/usr/bin/nmcli', 'con', 'up']:
            if self.fields['802-11-wireless.bssid'] == self.unreachable:
                return {'success': False, 'stdout': '', 'returncode': 4,
                        'stderr': 'Error: Connection activation failed: No network with SSID found.'}
            self.active = True
            return {'success': True, 'stdout': '', 'stderr': '', 'returncode': 0}
        if argv[:2] == ['/usr/bin/nmcli', '-g'] and argv[2] == 'GENERAL.STATE':
            return {'success': True, 'stdout': '100 (connected)' if self.active else '30 (disconnected)',
                    'stderr': '', 'returncode': 0}
        return {'success': True, 'stdout': 'active', 'stderr': '', 'returncode': 0}


class InactiveProfileRollbackTests(unittest.TestCase):
    setUp = fixtures.StrictWifiTests.setUp

    def test_failed_manual_reconnect_rollback_brings_the_inactive_profile_back(self):
        self.iwd.unlink(missing_ok=True)
        self.settings.write_text(json.dumps(dict(wifi.DEFAULT_SETTINGS)), encoding='utf-8')
        nm = FakeNetworkManager(unreachable_bssid=SIX)
        clock = itertools.count(0, 0.3)
        p = self.plugin
        for name, kw in {
            '_run_cmd': dict(side_effect=nm.run),
            '_get_current_backend': dict(return_value='wpa_supplicant'),
            '_get_wifi_interface': dict(return_value='wlan0'),
            '_get_enabled_high_band_frequencies': dict(return_value=([5260], [6295])),
            '_nmcli_get': dict(side_effect=lambda _u, f, **k: (nm.fields[f], {'success': True})),
            '_nmcli_modify': dict(side_effect=lambda _u, f, v, **k: (nm.fields.__setitem__(f, v), {'success': True})[1]),
            '_profile_is_active': dict(side_effect=lambda *_a: nm.active),
            '_get_active_connection_uuid': dict(side_effect=lambda *_a, **_k: UUID if nm.active else None),
            '_get_link_frequency': dict(side_effect=lambda *_a: 5260 if nm.active else None),
            '_get_link_bssid': dict(side_effect=lambda *_a: FIVE if nm.active else ''),
            '_scan_high_band_candidates_for_active_ssid': dict(return_value={
                'scan_ok': True, 'candidates': [{'bssid': SIX, 'frequency': 6295, 'signal_dbm': -40}]}),
        }.items():
            self.stack.enter_context(patch.object(p, name, **kw))
        self.stack.enter_context(patch.object(p, '_reconnect_profile', wifi.Plugin._reconnect_profile.__get__(p)))
        self.stack.enter_context(patch.object(wifi.time, 'sleep', lambda *_: None))
        self.stack.enter_context(patch.object(wifi.time, 'monotonic', lambda: next(clock)))

        result = p._rescan_and_reconnect_sync('six')

        self.assertTrue(result.get('rolled_back'), result)
        self.assertEqual(nm.fields['802-11-wireless.bssid'], '')
        self.assertTrue(nm.active)
        self.assertFalse((self.journal_dir / 'band-policy-transaction.json').exists())

    def test_profile_that_stays_active_after_failed_down_is_still_an_error(self):
        p = self.plugin
        down = {'success': False, 'stderr': 'timeout', 'returncode': 1}
        with patch.object(p, '_run_cmd', return_value=down), \
             patch.object(p, '_profile_is_active', return_value=True), \
             patch.object(p, '_get_current_backend', return_value='wpa_supplicant'):
            result = wifi.Plugin._reconnect_profile(p, UUID, True, cycle=True)
        self.assertFalse(result['success'])
        self.assertTrue(result['connection_active'])


class SixOnlySupportAndReleaseTests(unittest.TestCase):
    setUp = fixtures.StrictWifiTests.setUp
    change = fixtures.StrictWifiTests.change

    def test_unsupported_networkmanager_refuses_before_any_scan_or_profile_change(self):
        self.iwd.unlink(missing_ok=True)
        self.settings.write_text(json.dumps(dict(wifi.DEFAULT_SETTINGS)), encoding='utf-8')
        self.commands.side_effect = lambda *_a, **_k: {'success': True, 'stdout': 'nmcli tool, version 1.52.1'}
        with patch.object(self.plugin, '_get_current_backend', return_value='wpa_supplicant'), \
             patch.object(self.plugin, '_nmcli_modify') as modify:
            result = self.change(wifi.BAND_POLICY_SIX_ONLY)
        self.assertEqual(result['error'], 'preflight_failed')
        self.assertIn('1.58', result['message'])
        self.plugin._get_band_policy_capabilities_sync.assert_not_called()
        modify.assert_not_called()

    def test_support_follows_networkmanager_version_and_is_read_once(self):
        for text, expected in (('nmcli tool, version 1.52.1', False), ('nmcli tool, version 1.58.0', True)):
            plugin = wifi.Plugin()
            with patch.object(plugin, '_run_cmd', return_value={'success': True, 'stdout': text}) as run:
                self.assertIs(plugin._nm_supports_six_ghz_only(), expected)
                self.assertIs(plugin._nm_supports_six_ghz_only(), expected)
            self.assertEqual(run.call_count, 1)

    def six_only_state(self):
        self.iwd.unlink(missing_ok=True)
        state = dict(wifi.DEFAULT_SETTINGS)
        state.update(band_policy=wifi.BAND_POLICY_SIX_ONLY, band_policy_state={
            'schema': 1, 'mode': wifi.BAND_POLICY_SIX_ONLY, 'connection_uuid': UUID,
            'original': {'nm_band': '', 'iwd': self.baseline},
            'owns_nm_band': True, 'owns_iwd': False,
            'applied': {'nm_band': '6GHz', 'iwd_modifier_present': False, 'iwd_modifier_value': ''}})
        self.settings.write_text(json.dumps(state), encoding='utf-8')

    def turn_off(self, band, profiles):
        """`band` is the _nmcli_get result; `profiles` is the `nmcli con show` listing."""
        self.six_only_state()
        self.commands.side_effect = lambda *_a, **_k: {'success': True, 'stdout': profiles}
        with patch.object(self.plugin, '_get_current_backend', return_value='wpa_supplicant'), \
             patch.object(self.plugin, '_get_active_connection_uuid', return_value=None), \
             patch.object(self.plugin, '_nmcli_get', return_value=band), \
             patch.object(self.plugin, '_nmcli_modify') as modify:
            result = self.change(wifi.BAND_POLICY_OFF)
        return result, modify

    def test_off_after_profile_was_forgotten_drops_ownership(self):
        result, modify = self.turn_off(('', {'success': False, 'stderr': 'no such connection'}), 'other-uuid')
        self.assertTrue(result['success'], result)
        self.assertEqual(wifi._load_settings()['band_policy'], wifi.BAND_POLICY_OFF)
        modify.assert_not_called()

    def test_off_after_band_was_cleared_outside_drops_ownership(self):
        result, modify = self.turn_off(('', {'success': True}), UUID)
        self.assertTrue(result['success'], result)
        self.assertEqual(wifi._load_settings()['band_policy'], wifi.BAND_POLICY_OFF)
        modify.assert_not_called()

    def test_automatic_puts_the_original_back_over_a_band_changed_outside(self):
        band = ['a']                       # someone else set 5 GHz after our 6 GHz only
        self.six_only_state()
        self.commands.side_effect = lambda *_a, **_k: {'success': True, 'stdout': UUID}
        with patch.object(self.plugin, '_get_current_backend', return_value='wpa_supplicant'),              patch.object(self.plugin, '_get_active_connection_uuid', return_value=None),              patch.object(self.plugin, '_nmcli_get', side_effect=lambda *_a, **_k: (band[0], {'success': True})),              patch.object(self.plugin, '_nmcli_modify', side_effect=lambda _u, _k, v, **_kw: (
                 band.__setitem__(0, v), {'success': True})[1]) as modify:
            result = self.change(wifi.BAND_POLICY_OFF)
        self.assertTrue(result['success'], result)
        self.assertEqual(band[0], '')       # the recorded original: no band restriction
        self.assertIn('changed outside Companion was put back', result['message'])
        self.assertEqual(wifi._load_settings()['band_policy'], wifi.BAND_POLICY_OFF)

    def test_automatic_with_an_unrecognized_policy_and_nothing_owned_changes_nothing(self):
        state = dict(wifi.DEFAULT_SETTINGS, band_policy='from_a_newer_version', band_policy_state={})
        self.settings.write_text(json.dumps(state), encoding='utf-8')
        with patch.object(self.plugin, '_get_current_backend', return_value='wpa_supplicant'),              patch.object(self.plugin, '_nmcli_modify') as modify:
            result = self.change(wifi.BAND_POLICY_OFF)
        self.assertTrue(result['success'], result)
        self.assertEqual(wifi._load_settings()['band_policy'], wifi.BAND_POLICY_OFF)
        modify.assert_not_called()

    def test_off_still_refuses_an_unreadable_profile_that_exists(self):
        result, _ = self.turn_off(('', {'success': False}), UUID)
        self.assertEqual(result['error'], 'ownership_conflict')
        self.assertEqual(wifi._load_settings()['band_policy'], wifi.BAND_POLICY_SIX_ONLY)

    def test_off_still_refuses_incomplete_ownership_data(self):
        self.six_only_state()
        state = json.loads(self.settings.read_text(encoding='utf-8'))
        state['band_policy_state']['mode'] = wifi.BAND_POLICY_HIGH_ONLY
        self.settings.write_text(json.dumps(state), encoding='utf-8')
        with patch.object(self.plugin, '_get_current_backend', return_value='wpa_supplicant'), \
             patch.object(self.plugin, '_nmcli_modify') as modify:
            result = self.change(wifi.BAND_POLICY_OFF)
        self.assertEqual((result['error'], result['message']),
                         ('ownership_conflict', 'Band policy ownership data is incomplete.'))
        modify.assert_not_called()

    def test_status_checks_flag_a_band_restored_to_the_original_but_transitions_accept_it(self):
        # The NM band is back at the recorded original.
        self.stack.enter_context(patch.object(self.plugin, '_nmcli_get', return_value=('bg', {'success': True})))
        settings = {'band_policy': wifi.BAND_POLICY_SIX_ONLY, 'band_policy_state': {
            'mode': wifi.BAND_POLICY_SIX_ONLY, 'connection_uuid': UUID, 'owns_nm_band': True,
            'original': {'nm_band': 'bg'}, 'applied': {'nm_band': 'a'}}}
        self.assertIn('changed outside', self.plugin._band_policy_ownership_error(settings))
        self.assertEqual(self.plugin._band_policy_ownership_error(settings, allow_restored=True), '')


if __name__ == '__main__':
    unittest.main()

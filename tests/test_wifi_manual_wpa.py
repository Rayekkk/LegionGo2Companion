# SPDX-License-Identifier: BSD-3-Clause
"""One-time WPA band selection must preserve normal profile and recovery."""
import json
import unittest
from unittest.mock import patch

import test_wifi_strict as fixtures
import wifi_backend as wifi

UUID = fixtures.UUID
OTHER = 'ba58df8b-cdb1-447d-80ab-19d11a634e7e'
SIX = 'aa:29:48:e1:7e:03'
FIVE = 'a8:29:48:e1:7e:01'
NMCLI = '/usr/bin/nmcli'


class FakeNetworkManager:
    """nmcli/iw answers of one wlan0 adapter and its saved profile.
    `con down` of an inactive profile exits 10; `con up` joins the pinned BSSID (else the 5 GHz one)."""

    def __init__(self, unreachable_bssid=None):
        self.active, self.uuid = True, UUID
        self.bssid, self.frequency = FIVE, 5260
        self.fields = {'802-11-wireless.bssid': '', '802-11-wireless.band': ''}
        self.unreachable = unreachable_bssid
        self.on_modify = None

    def join(self):
        self.active = True
        self.bssid = self.fields['802-11-wireless.bssid'] or FIVE
        self.frequency = 6295 if self.bssid == SIX else 5260

    def run(self, argv, **_kw):
        if argv[-2:] == ['dev', 'status']:
            return fixtures.ok('wlan0:wifi\n')
        if argv[:3] == [NMCLI, 'con', 'down']:
            if not self.active:
                return {'success': False, 'stdout': '', 'returncode': 10,
                        'stderr': "Error: '%s' is not an active connection." % argv[-1]}
            self.active = False
            return fixtures.ok()
        if argv[:3] == [NMCLI, 'con', 'up']:
            if self.fields['802-11-wireless.bssid'] == self.unreachable:
                return {'success': False, 'stdout': '', 'returncode': 4,
                        'stderr': 'Error: Connection activation failed: No network with SSID found.'}
            self.join()
            return fixtures.ok()
        if argv[:3] == [NMCLI, 'con', 'mod']:
            self.fields[argv[5]] = argv[6]
            if self.on_modify:
                self.on_modify(argv[5], argv[6])
            return fixtures.ok()
        if argv[:2] == [NMCLI, '-g'] and argv[3:5] == ['con', 'show']:
            return fixtures.ok(self.fields.get(argv[2], ''))
        if argv[:2] == [NMCLI, '-g'] and argv[3:5] == ['device', 'show']:
            state, uuid = ('100 (connected)', self.uuid) if self.active else ('30 (disconnected)', '')
            return fixtures.ok({'GENERAL.STATE': state, 'GENERAL.CON-UUID': uuid,
                                'GENERAL.STATE,GENERAL.CON-UUID': f'{state}\n{uuid}'}[argv[2]])
        if argv[:2] == ['/usr/bin/iw', 'dev'] and argv[-1] == 'link':
            if not (self.active and self.frequency):
                return fixtures.ok('Not connected.\n')
            return fixtures.ok(f'Connected to {self.bssid} (on wlan0)\n\tfreq: {self.frequency}\n')
        if argv == ['/usr/bin/iw', 'list']:
            return fixtures.ok('\t\t\t* 5260 MHz [52] (20.0 dBm)\n\t\t\t* 6295 MHz [69] (20.0 dBm)\n')
        return fixtures.ok('active')


# The WPA flows below run the real NM/iw readers against FakeNetworkManager.
NM_READERS = ('_nmcli_get', '_get_link_frequency', '_get_active_connection_uuid', '_profile_is_active')


class ManualWpaTests(unittest.TestCase):
    setUp = fixtures.StrictWifiTests.setUp
    REAL = NM_READERS

    def configure(self):
        self.iwd.unlink(missing_ok=True)
        self.settings.write_text(json.dumps(dict(wifi.DEFAULT_SETTINGS)), encoding='utf-8')
        self.nm = FakeNetworkManager()
        self.commands.side_effect = self.nm.run
        self.plugin._get_current_backend.return_value = 'wpa_supplicant'
        fixtures.fake_clock(self.stack)
        self.reconnect = self.plugin._reconnect_profile
        self.reconnect.side_effect = self.activate
        self.scan = self.stack.enter_context(patch.object(self.plugin, '_scan_high_band_candidates_for_active_ssid', return_value={
            'scan_ok': True, 'candidates': [{'bssid': FIVE, 'frequency': 5260, 'signal_dbm': -20},
                                          {'bssid': SIX, 'frequency': 6295, 'signal_dbm': -40}]}))
        self.before = wifi._load_settings()

    def activate(self, *_args, **_kw):
        self.nm.join()
        return {'success': True}

    def fail_activation(self):
        self.reconnect.side_effect = None
        self.reconnect.return_value = {'success': False}

    def writes(self):
        return fixtures.profile_writes(self.commands)

    def test_six_selection_from_five_filters_stronger_five_and_clears_pin(self):
        self.configure()
        result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertTrue(result['success'], result)
        self.assertEqual(result['frequency'], 6295)
        self.assertTrue(result['temporary_bssid_cleared'])
        self.assertEqual(self.nm.fields, {'802-11-wireless.bssid': '', '802-11-wireless.band': ''})
        self.assertEqual(wifi._load_settings(), self.before)
        self.assertFalse((self.journal_dir / 'band-policy-transaction.json').exists())
        self.reconnect.assert_called_once()   # one network cycle

    def test_already_six_has_no_scan_or_network_cycle(self):
        self.configure(); self.nm.bssid, self.nm.frequency = SIX, 6295
        result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertTrue(result['success']); self.assertFalse(result['reconnected'])
        self.scan.assert_not_called(); self.assertEqual(self.writes(), [])

    def test_missing_six_does_not_connect_to_five_or_modify_profile(self):
        self.configure(); self.scan.return_value['candidates'] = [{'bssid': FIVE, 'frequency': 5260}]
        result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertTrue(result['success']); self.assertFalse(result['better_band_found'])
        self.assertEqual(self.writes(), []); self.reconnect.assert_not_called()

    def test_connection_or_profile_changed_during_scan_is_preserved(self):
        def other_network():
            self.nm.uuid = OTHER

        def external_pin():
            self.nm.fields['802-11-wireless.bssid'] = FIVE

        for change, error in ((other_network, 'connection_changed'), (external_pin, 'profile_changed')):
            with self.subTest(error=error):
                self.configure()
                def scan(*_args, **_kw):
                    change()
                    return {'scan_ok': True, 'candidates': [{'bssid': SIX, 'frequency': 6295}]}
                self.scan.side_effect = scan
                result = self.plugin._rescan_and_reconnect_sync('six')
                self.assertEqual(result['error'], error)
                self.assertEqual(self.writes(), [])

    def test_external_band_or_bssid_is_not_replaced(self):
        for field, value in [('802-11-wireless.band', 'a'), ('802-11-wireless.bssid', FIVE)]:
            with self.subTest(field=field):
                self.configure(); self.nm.fields[field] = value
                result = self.plugin._rescan_and_reconnect_sync('six')
                self.assertFalse(result['success']); self.assertEqual(self.writes(), [])

    def test_failed_activation_restores_pin_and_keeps_automatic_policy(self):
        self.configure(); self.fail_activation()
        result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertFalse(result['success']); self.assertTrue(result['rolled_back'], result)
        self.assertEqual(self.nm.fields['802-11-wireless.bssid'], '')
        self.assertEqual(wifi._load_settings()['band_policy'], wifi.BAND_POLICY_OFF)

    def test_invalid_target_is_rejected_without_settings_or_commands(self):
        before = self.settings.read_bytes()
        for target in (None, False, 6, [], {}, 'five'):
            self.assertEqual(self.plugin._rescan_and_reconnect_sync(target)['error'], 'invalid_target_band')
        self.commands.assert_not_called()
        self.assertEqual(self.settings.read_bytes(), before)

    def test_failed_rollback_preserves_independent_watchdog(self):
        self.configure(); self.fail_activation()
        with patch.object(self.plugin, '_restore_band_policy_transaction',
                          return_value={'success': False, 'conflicts': ['temporary failure']}):
            result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertFalse(result['success'])
        self.plugin._cancel_band_policy_rollback.assert_not_called()
        self.assertTrue((self.journal_dir / 'band-policy-transaction.json').exists())

    def test_watchdog_rollback_does_not_block_the_next_manual_reconnect(self):
        self.configure()
        def link_lost(*_args, **_kw):   # the link is gone, so the worker's own rollback fails too
            self.nm.active = False
            return {'success': False}
        self.reconnect.side_effect = link_lost
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
        self.assertEqual(self.nm.fields['802-11-wireless.bssid'], '')
        result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertTrue(result['success'], result)
        self.assertEqual(result['frequency'], 6295)
        self.assertIsNone(self.plugin._load_band_policy_journal())

    def test_external_pin_changed_while_connecting_is_not_cleared(self):
        self.configure()
        def activation(*_args, **_kw):
            self.nm.join()
            self.nm.fields['802-11-wireless.bssid'] = FIVE
            return {'success': True}
        self.reconnect.side_effect = activation
        result = self.plugin._rescan_and_reconnect_sync('six')
        self.assertFalse(result['success']); self.assertFalse(result['rolled_back'])
        self.assertEqual(self.nm.fields['802-11-wireless.bssid'], FIVE)
        self.plugin._cancel_band_policy_rollback.assert_not_called()

    def test_link_lost_while_clearing_pin_cannot_report_success(self):
        self.configure()
        def lose_link(field, value):
            if field == '802-11-wireless.bssid' and not value:
                self.nm.frequency = None
        self.nm.on_modify = lose_link
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


class InactiveProfileRollbackTests(unittest.TestCase):
    setUp = fixtures.StrictWifiTests.setUp
    REAL = NM_READERS + ('_reconnect_profile',)

    def test_failed_manual_reconnect_rollback_brings_the_inactive_profile_back(self):
        self.iwd.unlink(missing_ok=True)
        self.settings.write_text(json.dumps(dict(wifi.DEFAULT_SETTINGS)), encoding='utf-8')
        nm = FakeNetworkManager(unreachable_bssid=SIX)
        self.commands.side_effect = nm.run
        self.plugin._get_current_backend.return_value = 'wpa_supplicant'
        fixtures.fake_clock(self.stack)
        self.stack.enter_context(patch.object(self.plugin, '_scan_high_band_candidates_for_active_ssid', return_value={
            'scan_ok': True, 'candidates': [{'bssid': SIX, 'frequency': 6295, 'signal_dbm': -40}]}))

        result = self.plugin._rescan_and_reconnect_sync('six')

        self.assertTrue(result.get('rolled_back'), result)
        self.assertEqual(nm.fields['802-11-wireless.bssid'], '')
        self.assertTrue(nm.active)
        self.assertFalse((self.journal_dir / 'band-policy-transaction.json').exists())

    def test_profile_that_stays_active_after_failed_down_is_still_an_error(self):
        nm = FakeNetworkManager()
        down = {'success': False, 'stderr': 'timeout', 'returncode': 1}
        self.commands.side_effect = lambda argv, **kw: down if argv[:3] == [NMCLI, 'con', 'down'] else nm.run(argv)
        self.plugin._get_current_backend.return_value = 'wpa_supplicant'
        result = self.plugin._reconnect_profile(UUID, True, cycle=True)
        self.assertFalse(result['success'])
        self.assertTrue(result['connection_active'])


class SixOnlySupportAndReleaseTests(unittest.TestCase):
    setUp = fixtures.StrictWifiTests.setUp
    change = fixtures.StrictWifiTests.change

    def test_unsupported_networkmanager_refuses_before_any_scan_or_profile_change(self):
        self.iwd.unlink(missing_ok=True)
        self.settings.write_text(json.dumps(dict(wifi.DEFAULT_SETTINGS)), encoding='utf-8')
        self.commands.side_effect = lambda *_a, **_k: {'success': True, 'stdout': 'nmcli tool, version 1.52.1'}
        self.plugin._get_current_backend.return_value = 'wpa_supplicant'
        result = self.change(wifi.BAND_POLICY_SIX_ONLY)
        self.assertEqual(result['error'], 'preflight_failed')
        self.assertIn('1.58', result['message'])
        self.plugin._get_band_policy_capabilities_sync.assert_not_called()
        self.assertEqual(fixtures.profile_writes(self.commands), [])

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
        self.plugin._get_current_backend.return_value = 'wpa_supplicant'
        self.plugin._get_active_connection_uuid.return_value = None

    def turn_off(self, band, profiles):
        """`band` is the saved profile's band read; `profiles` is the `nmcli con show` listing."""
        self.six_only_state()
        self.commands.side_effect = lambda *_a, **_k: {'success': True, 'stdout': profiles}
        self.plugin._nmcli_get.return_value = band
        return self.change(wifi.BAND_POLICY_OFF)

    def test_off_after_profile_was_forgotten_or_band_cleared_outside_drops_ownership(self):
        for band, profiles in ((('', {'success': False, 'stderr': 'no such connection'}), 'other-uuid'),
                               (('', {'success': True}), UUID)):
            with self.subTest(profiles=profiles):
                self.commands.reset_mock()
                result = self.turn_off(band, profiles)
                self.assertTrue(result['success'], result)
                self.assertEqual(wifi._load_settings()['band_policy'], wifi.BAND_POLICY_OFF)
                self.assertEqual(fixtures.profile_writes(self.commands), [])

    def test_automatic_puts_the_original_back_over_a_band_changed_outside(self):
        band = ['a']                       # someone else set 5 GHz after our 6 GHz only
        self.six_only_state()

        def command(argv, **_kw):
            if argv[:3] == [NMCLI, 'con', 'mod']:
                band[0] = argv[6]
            return {'success': True, 'stdout': UUID}

        self.commands.side_effect = command
        self.plugin._nmcli_get.side_effect = lambda *_a, **_k: (band[0], {'success': True})
        result = self.change(wifi.BAND_POLICY_OFF)
        self.assertTrue(result['success'], result)
        self.assertEqual(band[0], '')       # the recorded original: no band restriction
        self.assertIn('changed outside Companion was put back', result['message'])
        self.assertEqual(wifi._load_settings()['band_policy'], wifi.BAND_POLICY_OFF)

    def test_automatic_with_an_unrecognized_policy_and_nothing_owned_changes_nothing(self):
        state = dict(wifi.DEFAULT_SETTINGS, band_policy='from_a_newer_version', band_policy_state={})
        self.settings.write_text(json.dumps(state), encoding='utf-8')
        self.plugin._get_current_backend.return_value = 'wpa_supplicant'
        result = self.change(wifi.BAND_POLICY_OFF)
        self.assertTrue(result['success'], result)
        self.assertEqual(wifi._load_settings()['band_policy'], wifi.BAND_POLICY_OFF)
        self.assertEqual(fixtures.profile_writes(self.commands), [])

    def test_off_still_refuses_an_unreadable_profile_that_exists(self):
        result = self.turn_off(('', {'success': False}), UUID)
        self.assertEqual(result['error'], 'ownership_conflict')
        self.assertEqual(wifi._load_settings()['band_policy'], wifi.BAND_POLICY_SIX_ONLY)

    def test_off_still_refuses_incomplete_ownership_data(self):
        self.six_only_state()
        state = json.loads(self.settings.read_text(encoding='utf-8'))
        state['band_policy_state']['mode'] = wifi.BAND_POLICY_HIGH_ONLY
        self.settings.write_text(json.dumps(state), encoding='utf-8')
        result = self.change(wifi.BAND_POLICY_OFF)
        self.assertEqual((result['error'], result['message']),
                         ('ownership_conflict', 'Band policy ownership data is incomplete.'))
        self.assertEqual(fixtures.profile_writes(self.commands), [])

    def test_status_checks_flag_a_band_restored_to_the_original_but_transitions_accept_it(self):
        self.plugin._nmcli_get.return_value = ('bg', {'success': True})   # back at the recorded original
        settings = {'band_policy': wifi.BAND_POLICY_SIX_ONLY, 'band_policy_state': {
            'mode': wifi.BAND_POLICY_SIX_ONLY, 'connection_uuid': UUID, 'owns_nm_band': True,
            'original': {'nm_band': 'bg'}, 'applied': {'nm_band': 'a'}}}
        self.assertIn('changed outside', self.plugin._band_policy_ownership_error(settings))
        self.assertEqual(self.plugin._band_policy_ownership_error(settings, allow_restored=True), '')


if __name__ == '__main__':
    unittest.main()

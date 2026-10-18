# SPDX-License-Identifier: BSD-3-Clause
"""External scan output contracts; no radio or NetworkManager is opened."""
import subprocess
import unittest
from unittest.mock import patch

import support  # Install isolated Decky/Linux doubles for standalone discovery.
import wifi_backend as wifi


UUID = '11111111-2222-3333-4444-555555555555'
FIVE, SIX = 'aa:bb:cc:dd:ee:01', 'aa:bb:cc:dd:ee:02'


def escaped(value):
    return value.replace('\\', '\\\\').replace(':', '\\:')


class WifiScanFormatTests(unittest.TestCase):
    def test_manual_nmcli_fallback_accepts_real_escaped_rows_and_six_filter(self):
        ssid = ' Network:Room\\ '
        commands = []
        def run(argv, **_kwargs):
            commands.append(argv)
            if argv[0] == '/usr/bin/iw':
                return subprocess.CompletedProcess(argv, 1, '', 'Operation not supported')
            self.assertNotIn('--separator', argv, 'nmcli has a fixed escaped colon separator')
            if 'con' in argv:
                output = UUID + '\n' + escaped(ssid) + '\nno\n'
            else:
                self.assertEqual(argv[argv.index('-f')+1], 'BSSID,SSID,FREQ,SIGNAL')
                output = (f'{escaped(FIVE)}:{escaped(ssid)}:5260 MHz:95\n'
                          f'{escaped(SIX)}:{escaped(ssid.strip())}:6295 MHz:99\n'
                          f'{escaped(SIX)}:{escaped(ssid)}:6295 MHz:40\n')
            return subprocess.CompletedProcess(argv, 0, output, '')
        with patch.object(wifi.subprocess, 'run', side_effect=run):
            result = wifi.Plugin()._scan_high_band_candidates_for_active_ssid(
                'wlan0', UUID, minimum_frequency=5925)
        self.assertTrue(result['scan_ok'])
        self.assertEqual(result['scan_source'], 'nmcli')
        self.assertEqual(result['ssid'], ssid)
        self.assertEqual(result['candidates'], [{'bssid': SIX, 'frequency': 6295, 'signal_dbm': 40}])
        self.assertEqual(commands[1][-1], ssid)

    def test_visible_direct_scan_preserves_spaces_and_rejects_trimmed_lookalike(self):
        # iw util.c print_ssid_escaped hex-escapes boundary spaces. A literal
        # backslash is escaped separately, so the lookalike stays distinct.
        for observed, expected in (('Network', False), (r'\x20Network\x20', True),
                                   (r'\x5cx20Network\x5cx20', False)):
            with self.subTest(observed=observed):
                commands = []
                def run(argv, **_kwargs):
                    commands.append(argv)
                    if argv[0] == '/usr/bin/iw':
                        output = f'BSS {FIVE}\n\tfreq: 5260\n\tSSID: {observed}\n'
                    elif 'con' in argv:
                        output = UUID + '\n Network \nno\n'
                    else:
                        output = ''
                    return subprocess.CompletedProcess(argv, 0, output, '')
                with patch.object(wifi.subprocess, 'run', side_effect=run):
                    result = wifi.Plugin()._get_visible_bands_for_active_ssid('wlan0', UUID)
                self.assertEqual(result['fresh_high_band'], expected)
                self.assertEqual(result['five_ghz'] > 0, expected)
                self.assertEqual(commands[1][-1], ' Network ')

    def test_visible_nmcli_fallback_preserves_escaped_name_and_outer_spaces(self):
        # A literal backslash immediately before the delimiter must not escape
        # it after nmcli has escaped that backslash itself.
        for ssid in (' Network ', 'Network:Room\\'):
            with self.subTest(ssid=ssid):
                def run(argv, **_kwargs):
                    if argv[0] == '/usr/bin/iw':
                        return subprocess.CompletedProcess(argv, 1, '', 'Operation not supported')
                    if 'con' in argv:
                        output = UUID + '\n' + escaped(ssid) + '\nno\n'
                    else:
                        self.assertEqual(argv[argv.index('-f')+1], 'FREQ,SSID,SIGNAL')
                        output = f'5260 MHz:{escaped(ssid)}:75\n'
                    return subprocess.CompletedProcess(argv, 0, output, '')
                with patch.object(wifi.subprocess, 'run', side_effect=run):
                    result = wifi.Plugin()._get_visible_bands_for_active_ssid('wlan0', UUID)
                self.assertTrue(result['fresh_high_band'])
                self.assertEqual(result['five_ghz'], 1)

    def test_iw_scan_hex_bytes_match_exact_utf8_ssid_without_double_decoding(self):
        # Raw text fixtures follow iw util.c print_ssid_escaped (LC_ALL=C),
        # not the plugin decoder or a production-derived encoder.
        for ssid, raw, foreign in (
                (' Network ', r'\x20Network\x20', 'Network'),
                ('Room\\A', r'Room\x5cA', 'RoomA'),
                (r'Room\x41', r'Room\x5cx41', 'RoomA'),
                ('Café', r'Caf\xc3\xa9', 'Cafe')):
            for manual in (True, False):
                with self.subTest(ssid=ssid, manual=manual):
                    def run(argv, **_kwargs):
                        if argv[0] == '/usr/bin/iw':
                            output = (f'BSS {FIVE}\n\tfreq: 5260\n\tsignal: -20 dBm\n\tSSID: {foreign}\n'
                                      f'BSS {SIX}\n\tfreq: 6295\n\tsignal: -40 dBm\n\tSSID: {raw}\n')
                        elif 'con' in argv:
                            output = UUID + '\n' + escaped(ssid) + '\nno\n'
                        else:
                            output = ''
                        return subprocess.CompletedProcess(argv, 0, output, '')
                    with patch.object(wifi.subprocess, 'run', side_effect=run):
                        plugin = wifi.Plugin()
                        result = (plugin._scan_high_band_candidates_for_active_ssid('wlan0', UUID)
                                  if manual else plugin._get_visible_bands_for_active_ssid('wlan0', UUID))
                    if manual:
                        self.assertEqual([candidate['bssid'] for candidate in result['candidates']], [SIX])
                    else:
                        self.assertEqual(result['five_ghz'], 0)
                        self.assertEqual(result['six_ghz'], 1)
                        self.assertTrue(result['fresh_high_band'])

    def test_unicode_line_separators_remain_part_of_ssid_in_nmcli_records(self):
        # NM 1.52.0 returns valid UTF-8 SSIDs unchanged (nm_utils_ssid_to_utf8)
        # and terse output escapes only colon/backslash (nmcli/utils.c).
        # These separators are data; ASCII LF alone separates output records.
        for separator in ('\u0085', '\u2028', '\u2029'):
            for manual in (True, False):
                with self.subTest(separator=repr(separator), manual=manual):
                    ssid = 'A' + separator + 'B:Room\\'
                    def run(argv, **_kwargs):
                        if argv[0] == '/usr/bin/iw':
                            return subprocess.CompletedProcess(argv, 1, '', 'Operation not supported')
                        if 'con' in argv:
                            output = UUID + '\n' + escaped(ssid) + '\nno\n'
                        elif manual:
                            output = (f'{escaped(FIVE)}:AB:5260 MHz:95\n'
                                      f'{escaped(SIX)}:{escaped(ssid)}:6295 MHz:75\n')
                        else:
                            output = f'5260 MHz:AB:95\n6295 MHz:{escaped(ssid)}:75\n'
                        return subprocess.CompletedProcess(argv, 0, output, '')
                    with patch.object(wifi.subprocess, 'run', side_effect=run):
                        plugin = wifi.Plugin()
                        result = (plugin._scan_high_band_candidates_for_active_ssid('wlan0', UUID)
                                  if manual else plugin._get_visible_bands_for_active_ssid('wlan0', UUID))
                    if manual:
                        self.assertEqual(result['ssid'], ssid)
                        self.assertEqual([candidate['bssid'] for candidate in result['candidates']], [SIX])
                    else:
                        self.assertEqual(result['five_ghz'], 0)
                        self.assertEqual(result['six_ghz'], 1)
                        self.assertTrue(result['fresh_high_band'])

    def test_iwd_profile_conflicts_compare_unicode_separator_ssids_exactly(self):
        other = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        for separator in ('\u0085', '\u2028', '\u2029'):
            for duplicate in (False, True):
                with self.subTest(separator=repr(separator), duplicate=duplicate):
                    ssid = 'A' + separator + 'B'
                    def run(argv, **_kwargs):
                        if 'UUID,TYPE' in argv:
                            output = f'{UUID}:802-11-wireless\n{other}:802-11-wireless\n'
                        else:
                            def record(uid, name):
                                return (f'connection.uuid:{uid}\nconnection.type:802-11-wireless\n'
                                        f'connection.autoconnect:yes\n802-11-wireless.ssid:{name}\n'
                                        '802-11-wireless.hidden:no\n802-11-wireless-security.key-mgmt:wpa-psk\n')
                            output = record(UUID, ssid) + '\n' + record(other, ssid if duplicate else 'AB')
                        return subprocess.CompletedProcess(argv, 0, output, '')
                    with patch.object(wifi.subprocess, 'run', side_effect=run):
                        conflict = wifi.Plugin()._get_iwd_profile_conflict(UUID)
                    self.assertEqual(bool(conflict), duplicate)
                    if duplicate:
                        self.assertIn('Multiple saved WiFi profiles', conflict)

    def test_profile_metadata_still_rejects_actual_ascii_cr_and_lf(self):
        for separator in ('\r', '\n'):
            with self.subTest(separator=repr(separator)):
                def run(argv, **_kwargs):
                    return subprocess.CompletedProcess(argv, 0, f'{UUID}\nA{separator}B\nno\n', '')
                with patch.object(wifi.subprocess, 'run', side_effect=run):
                    self.assertIsNone(wifi.Plugin()._get_profile_ssid(UUID))


if __name__ == '__main__':
    unittest.main()

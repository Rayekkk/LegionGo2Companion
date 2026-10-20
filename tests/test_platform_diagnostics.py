# SPDX-License-Identifier: BSD-3-Clause
"""Meaningful platform diagnostics use isolated files and fake external tools."""
import asyncio
import collections
import errno
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import support
import diagnostics
import display_backend as display
import fan_backend as fan
import wifi_backend as wifi
import test_display_edid as edid_fixture
import test_display_lifecycle as lifecycle_fixture
import test_wifi_manual_wpa as wpa_fixture


class PlatformDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.seen = patch.object(diagnostics, '_seen', collections.OrderedDict())
        self.sink = patch.object(diagnostics, '_sink', lambda text, _level: self.records.append(json.loads(text)))
        self.seen.start(); self.sink.start()
        self.addCleanup(self.seen.stop); self.addCleanup(self.sink.stop)

    def events(self, component, action):
        return [row for row in self.records if row['component'] == component and row['event'] == action]

    def fixture(self, cls):
        value = cls(); value.setUp()
        self.addCleanup(value.doCleanups)
        return value

    def test_fan_settings_failure_is_once_and_valid_save_records_recovery(self):
        with patch.object(fan, 'settings') as settings:
            settings.recovery_error = ''
            settings.getSetting.return_value = {'schema': 999}
            plugin = fan.Plugin()
            plugin._load(); plugin._load()
            self.assertFalse(plugin._settings_valid)
            self.assertEqual([row['context']['status'] for row in self.events('fan', 'settings')], ['unavailable'])
            plugin._save(fan.defaults())
        self.assertEqual([row['context']['status'] for row in self.events('fan', 'settings')], ['unavailable', 'ready'])

    def test_fan_monitor_fault_records_release_and_persist_failure_without_rearming(self):
        plugin = fan.Plugin()
        plugin._client = Mock()
        plugin._client.pump.side_effect = RuntimeError('Fixture sensor failed')
        plugin._client.snapshot.return_value = {'kind': 'telemetry', 'error': 'Fixture sensor failed', 'automatic': True}
        plugin._state['enabled'] = True
        async def offload(_component, method, *args): return method(*args)
        async def immediate(_delay): pass
        with patch.object(plugin, '_persist', new_callable=AsyncMock, side_effect=OSError('Fixture settings full')), \
                patch.object(fan.module_runtime, 'offload', side_effect=offload), patch.object(fan.asyncio, 'sleep', side_effect=immediate):
            asyncio.run(plugin._monitor())
        event = self.events('fan', 'control_fault')[0]
        self.assertIn('Fixture settings full', event['context']['reason'])
        self.assertFalse(event['context']['enabled'])
        self.assertEqual(self.events('fan', 'session_ended')[0]['context']['outcome'], 'fault')
        self.assertFalse(plugin._state['enabled'])
        plugin._client.close.assert_called_once()

    def test_unsafe_edid_and_recovery_log_transitions_without_poll_noise(self):
        case = self.fixture(edid_fixture.NativeDisplayEdidTests)
        with patch.object(display, '_published_edid_is_safe', return_value=False):
            for _ in range(7): display.Plugin._edid_pass()
        self.assertEqual(len(self.events('display', 'edid_recovery')), 1)
        self.assertEqual(self.events('display', 'edid_recovery')[0]['context']['status'], 'unsafe_path')
        display.Plugin._edid_pass()
        for _ in range(7): display.Plugin._edid_pass()
        self.assertEqual([row['context']['status'] for row in self.events('display', 'edid_recovery')], ['unsafe_path', 'applied'])
        self.assertEqual(case.edid.read_bytes(), case.expected)
        self.assertNotIn(str(case.edid), json.dumps(self.records))

    def test_failed_edid_intent_reports_stage_once_then_clears_on_recovery(self):
        case = self.fixture(edid_fixture.NativeDisplayEdidTests)
        with patch.object(case.store, 'replace', side_effect=PermissionError('Fixture settings are read only')):
            for _ in range(4): display.Plugin._edid_pass()
        self.assertEqual(case.edid.read_bytes(), case.original)
        events = self.events('display', 'edid_recovery_store')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['context']['stage'], 'identity')  # nothing is written before the backup's owner is saved
        self.assertEqual(events[0]['error']['type'], 'RuntimeError')
        display.Plugin._edid_pass()
        self.assertTrue(display.Plugin._edid_restore())
        self.assertIn('ready', [row['context']['status'] for row in self.events('display', 'edid_recovery_store')])

    def test_foreign_edid_restore_reports_once_and_preserves_contents(self):
        case = self.fixture(edid_fixture.NativeDisplayEdidTests)
        display.Plugin._edid_pass()
        case.edid.write_bytes(b'foreign connector bytes')
        for _ in range(7): self.assertTrue(display.Plugin._edid_restore())
        foreign = [row for row in self.events('display', 'edid_recovery') if row['context']['status'] == 'foreign']
        self.assertEqual(len(foreign), 1)
        self.assertEqual(case.edid.read_bytes(), b'foreign connector bytes')
        self.assertEqual(case.backup.read_bytes(), case.original)

    def test_repeated_brightness_release_failure_is_coalesced(self):
        self.fixture(lifecycle_fixture.NativeDisplayMigrationTests)
        with patch.object(display, '_write_nits', return_value=False):
            for _ in range(7): self.assertFalse(display.Plugin._release())
        self.assertEqual(len(self.events('display', 'brightness_restore_failed')), 1)

    def test_display_setup_keeps_private_failure_in_ui_and_logs_exception_metadata(self):
        self.fixture(lifecycle_fixture.DisplayResetTests)
        marker = 'unlabelled-private-setup-marker'
        with patch.object(display, '_install_script', side_effect=PermissionError(errno.EACCES, marker)), \
                patch.object(display, '_select_mode'), patch.object(display.decky, 'logger') as legacy:
            result = asyncio.run(display.Plugin._apply_mode_locked('pq'))
        self.assertIn(marker, result['setup_error'])
        event = self.events('display', 'script_install_failed')[0]
        self.assertEqual(event['error']['type'], 'PermissionError')
        self.assertEqual(event['error']['errno'], errno.EACCES)
        self.assertNotIn(marker, json.dumps(self.records))
        self.assertNotIn(marker, repr(legacy.mock_calls))

    def test_display_restart_logs_numeric_attempt_failures_without_subprocess_details(self):
        case = self.fixture(lifecycle_fixture.DisplayResetTests)
        marker = 'unlabelled-private-restart-marker'
        for exception in (False, True):
            with self.subTest(exception=exception):
                self.records.clear(); diagnostics._seen.clear()
                process = Mock(returncode=7, stderr=marker, stdout='')
                with patch.object(display, '_display_owner', return_value=None), \
                        patch.object(display, 'pwd', None), patch.object(display, '_system_env', return_value={}), \
                        patch.object(display.os.path, 'exists', return_value=True), \
                        patch.object(display.subprocess, 'run', return_value=process,
                                     side_effect=PermissionError(errno.EACCES, marker) if exception else None), \
                        patch.object(display.decky, 'logger') as legacy:
                    result = asyncio.run(case.plugin.restart_session())
                self.assertIn(marker, result['restart_error'])
                attempts = self.events('display', 'session_restart_attempt_failed')
                self.assertEqual(len(attempts), 2)
                if exception:
                    self.assertTrue(all(row['error']['errno'] == errno.EACCES for row in attempts))
                else:
                    self.assertTrue(all(row['context']['returncode'] == 7 for row in attempts))
                self.assertEqual(self.events('display', 'session_restart_returned')[0]['context']['outcome'], 'failed')
                self.assertNotIn(marker, json.dumps(self.records))
                self.assertNotIn(marker, repr(legacy.mock_calls))

    def test_edid_rollback_with_failed_metadata_clear_reports_actual_pending_recovery(self):
        case = self.fixture(edid_fixture.DisplayWriteRecoveryTests)
        store = display.Plugin._store_settings
        def commit(values, **kwargs):
            if values.get('edid_write_pending', 'other') is None:
                raise OSError(errno.ENOSPC, 'unlabelled-private-clear-marker')
            return store(values, **kwargs)
        with patch.object(display.Plugin, '_store_settings', side_effect=commit):
            case.interrupt_patch(rollback_fails=False)
        case.store.read()
        self.assertEqual(case.edid.read_bytes(), case.original)
        self.assertEqual(case.backup.read_bytes(), case.original)
        self.assertIsNotNone(case.store.getSetting('edid_write_pending'))
        event = self.events('display', 'edid_write_rolled_back')[0]
        self.assertTrue(event['context']['restored'])
        self.assertTrue(event['context']['pending'])
        self.assertNotIn('unlabelled-private-clear-marker', json.dumps(self.records))

    def test_native_handoff_failure_has_one_deferred_event_and_one_recovery(self):
        case = self.fixture(lifecycle_fixture.NativeDisplayMigrationTests)
        with patch.object(display.Plugin, '_native_store', side_effect=OSError('Fixture native journal is full')):
            for _ in range(4): case.refresh()
        self.assertEqual([row['context']['status'] for row in self.events('display', 'native_cleanup')], ['deferred'])
        case.refresh()
        self.assertEqual([row['context']['status'] for row in self.events('display', 'native_cleanup')], ['deferred', 'ready'])

    def test_scan_failure_is_safe_once_recovery_is_visible_and_cancel_is_quiet(self):
        process = Mock(returncode=2)
        process.communicate.return_value = ('fixture-private-stdout', 'SSID=fixture-private-name\nFORGED_LOG_LINE')
        process.poll.return_value = 2
        plugin = wifi.Plugin()
        with patch.object(wifi.subprocess, 'Popen', return_value=process):
            for _ in range(4): plugin._run_discovery_scan('wlan0', 'fixture-private-name', threading.Event())
            process.returncode = 0
            plugin._run_discovery_scan('wlan0', 'fixture-private-name', threading.Event())
            stop = threading.Event(); stop.set()
            plugin._run_discovery_scan('wlan0', 'fixture-private-name', stop)
        events = self.events('wifi', 'discovery_scan')
        self.assertEqual([row['context']['status'] for row in events], ['failed', 'ready'])
        self.assertEqual(events[0]['context']['returncode'], 2)
        self.assertNotIn('fixture-private', json.dumps(self.records))
        self.assertNotIn('FORGED_LOG_LINE', json.dumps(self.records))

    def test_manual_wifi_failure_and_rollback_have_safe_phase_events(self):
        case = self.fixture(wpa_fixture.ManualWpaTests); case.configure()
        with patch.object(case.plugin, '_nmcli_modify', return_value={
                'success': False, 'stderr': 'NM private SSID fixture-private-name\nFORGED_LOG_LINE'}), \
                patch.object(wifi.decky, 'logger') as legacy:
            result = case.plugin._rescan_and_reconnect_sync('six')
        self.assertFalse(result['success']); self.assertTrue(result['rolled_back'])
        self.assertEqual([row['context']['phase'] for row in self.events('wifi', 'transaction')],
                         ['pending', 'rolling_back', 'rolled_back'])
        self.assertEqual(self.events('wifi', 'manual_reconnect_failed')[0]['context']['stage'], 'apply')
        self.assertNotIn('fixture-private', json.dumps(self.records))
        self.assertNotIn('FORGED_LOG_LINE', json.dumps(self.records))
        legacy.error.assert_not_called()

    def test_band_recovery_reports_commit_without_claiming_previous_band_restored(self):
        for phase in ('committed', 'verified'):
            with self.subTest(phase=phase):
                case = self.fixture(wpa_fixture.ManualWpaTests); case.configure()
                with patch.object(case.plugin, '_remove_band_policy_journal'):
                    self.assertTrue(case.plugin._rescan_and_reconnect_sync('six')['success'])
                journal = case.plugin._load_band_policy_journal()
                journal['phase'] = phase
                case.plugin._save_band_policy_journal(journal)
                self.records.clear(); diagnostics._seen.clear()
                writes, reconnects = len(case.writes()), case.reconnect.call_count
                with patch.object(case.plugin, '_restore_band_policy_transaction',
                                  wraps=case.plugin._restore_band_policy_transaction) as restore:
                    self.assertTrue(case.plugin._recover_band_policy_journal()['success'])
                restore.assert_not_called()
                self.assertEqual(case.nm.frequency, 6295)
                self.assertEqual((len(case.writes()), case.reconnect.call_count), (writes, reconnects))
                context = self.events('wifi', 'band_recovery')[0]['context']
                self.assertEqual(context['outcome'], 'completed')
                self.assertFalse(context['restored'])

    def test_terminal_wifi_recovery_reports_restored_only_for_recorded_rollback(self):
        case = self.fixture(wpa_fixture.ManualWpaTests); case.configure()
        for phase in ('committed', 'rolled_back'):
            with self.subTest(phase=phase):
                journal = dict(schema=1, phase=phase, settings_file=str(case.settings))
                case.plugin._save_band_policy_journal(journal)
                self.records.clear(); diagnostics._seen.clear()
                self.assertTrue(case.plugin._recover_band_policy_journal()['success'])
                context = self.events('wifi', 'band_recovery')[0]['context']
                self.assertEqual(context['restored'], phase == 'rolled_back')
                self.assertEqual(context['outcome'], 'restored' if phase == 'rolled_back' else 'completed')
        self.assertEqual(case.writes(), []); case.reconnect.assert_not_called()

    def test_independent_wifi_watchdog_failure_records_pending_without_private_journal(self):
        case = self.fixture(wpa_fixture.ManualWpaTests); case.configure()
        journal = case.journal_dir / 'band-policy-transaction.json'
        case.journal_dir.mkdir(exist_ok=True)
        journal.write_text(json.dumps(dict(schema=1, phase='applying', settings_file=str(case.settings))))
        with patch.object(wifi.Plugin, '_acquire_band_policy_file_lock', return_value=Mock()), \
                patch.object(wifi.Plugin, '_release_band_policy_file_lock'), \
                patch.object(wifi.Plugin, '_restore_band_policy_transaction', return_value={'success':False,'conflicts':['SSID=fixture-private-name']}):
            self.assertEqual(wifi._run_band_policy_rollback_cli(str(journal)), 1)
        event = self.events('wifi', 'watchdog_recovery_finished')[0]
        self.assertEqual(event['level'], 'error'); self.assertTrue(event['context']['pending'])
        self.assertEqual(event['context']['outcome'], 'deferred')
        self.assertNotIn(str(case.settings), json.dumps(self.records))
        self.assertNotIn('fixture-private', json.dumps(self.records))


if __name__ == '__main__': unittest.main()

# SPDX-License-Identifier: BSD-3-Clause
"""The desktop diagnostic snapshot is bounded, useful and read-only."""
import datetime
import contextlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import diagnostics_reader as reader


def event(second=0, **fields):
    result = {'time': (datetime.datetime(2026, 10, 14, tzinfo=datetime.timezone.utc)
                      + datetime.timedelta(seconds=second)).isoformat(),
              'level': 'warning', 'component': 'fan', 'event': 'transport_failed', 'pid': 17,
              'session': 'a12b34', 'operation_id': 'op1234', 'context': {'errno': 104, 'stage': 'receive'}}
    result.update(fields)
    return result


def journal(record, identifier='legiongo2companion-fan', uid='0', **fields):
    return json.dumps({'SYSLOG_IDENTIFIER': identifier, '_UID': uid,
                       'MESSAGE': json.dumps(record), **fields}).encode() + b'\n'


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.directory = self.base / 'diagnostics'
        self.directory.mkdir(mode=0o700)
        self.query = patch.object(reader, '_query_journal', return_value=(b'', set(), False, True))
        self.query.start()
        self.addCleanup(self.query.stop)

    def write(self, records, leaf='events.jsonl', tail=b''):
        path = self.directory / leaf
        path.write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in records) + tail)
        path.chmod(0o600)
        return path

    def test_rotation_and_workers_are_one_newest_chronological_snapshot(self):
        self.write([event(0), event(2)], 'events.jsonl.2')
        self.write([event(1)], 'events.jsonl.1')
        self.write([event(4)])
        data = journal(event(3), 'legiongo2companion-wifi') + journal(event(5), 'legiongo2companion-firmware')
        with patch.object(reader, '_query_journal', return_value=(data, set(), False, True)):
            result = reader.read_events(self.base, limit=4)
        self.assertEqual([datetime.datetime.fromisoformat(row['time']).second for row in result['events']], [2, 3, 4, 5])
        self.assertEqual([row['source'] for row in result['events']], ['backend', 'wifi', 'backend', 'firmware'])
        self.assertTrue(result['truncated'])
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['available_sources'], ['backend', 'fan', 'firmware', 'wifi'])

    def test_raw_messages_paths_settings_and_unrecognized_fields_are_never_returned(self):
        row = event()
        row.update(message='private freeform text', password='private-password', settings={'secret': 'data'})
        row['context'].update(password='private-password', args=['secret'], reason='token=private-token',
                              scope='/home/deck/private', duration_ms=float('nan'))
        row['error'] = {'type': 'ConnectionResetError', 'errno': 104, 'message': 'private-error-text',
                        'frames': [{'file': 'fan_backend.py', 'function': 'receive', 'line': 105},
                                   {'file': '/home/private/user/code.py', 'function': 'read', 'line': 1}],
                        'causes': [{'type': 'OSError', 'errno': 5, 'message': 'private-cause'}]}
        self.write([row])
        result = reader.read_events(self.base)
        encoded = json.dumps(result)
        for private in ('private freeform', 'private-password', 'private-token', 'private-error', 'private-cause', '/home/'):
            self.assertNotIn(private, encoded)
        self.assertEqual(result['events'][0]['error']['frames'], [{'file': 'fan_backend.py', 'function': 'receive', 'line': 105}])
        self.assertEqual(result['events'][0]['error']['causes'], [{'type': 'OSError', 'errno': 5}])
        self.assertNotIn('duration_ms', result['events'][0]['context'])

    def test_untrusted_journal_fields_and_arbitrary_noncompanion_text_are_not_a_log_api(self):
        self.write([])
        data = (journal(event(1), 'other-plugin') + journal(event(2), uid='1000')
                + journal(event(3), identifier={'bad': 'type'})
                + json.dumps({'SYSLOG_IDENTIFIER': 'legiongo2companion-fan', '_UID': '0',
                              'MESSAGE': 'raw user-controlled traceback /home/private'}).encode() + b'\n'
                + journal(event(4), '_bad') + journal(event(5)))
        with patch.object(reader, '_query_journal', return_value=(data, set(), False, True)):
            result = reader.read_events(self.base)
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['events'][0]['source'], 'fan')
        self.assertIn('malformed_records', result['warnings'])
        self.assertNotIn('user-controlled', json.dumps(result))

    def test_partial_and_malformed_rows_do_not_hide_valid_older_records(self):
        self.write([event(1), event(2)], tail=b'{"time":"unfinished')
        with (self.directory / 'events.jsonl').open('ab') as stream:
            stream.write(b'\nnot-json\n' + b'x' * (reader.MAX_RECORD + 1) + b'\n')
            stream.write(json.dumps(event(3)).encode() + b'\n{"unfinished"')
        result = reader.read_events(self.base)
        self.assertEqual(len(result['events']), 3)
        self.assertEqual(result['warnings'], ['malformed_records', 'partial_records'])

    def test_missing_and_empty_sources_are_distinguished_without_creating_files(self):
        with patch.object(reader, '_query_journal', return_value=(b'', {'journal_unavailable'}, False, False)):
            missing = reader.read_events(None)
            self.write([])
            empty = reader.read_events(self.base)
        self.assertEqual(missing, {'events': [], 'available_sources': [],
                                  'warnings': ['backend_missing', 'journal_unavailable'], 'truncated': False})
        self.assertEqual(empty['available_sources'], ['backend'])
        self.assertEqual(empty['warnings'], ['journal_unavailable'])
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ['events.jsonl'])

    def test_unusual_json_shapes_are_skipped_without_breaking_snapshot(self):
        malformed = [None, [], {'time': 'bad'}, event(level=[]), event(time='2026-10-14T00:00:00'),
                     event(component=['bad']), event(time='2026-10-14T00:00:00+99:00')]
        self.write(malformed + [event(1)])
        result = reader.read_events(self.base)
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['warnings'], ['malformed_records'])

    def test_timestamps_with_offsets_are_normalized_before_ordering(self):
        self.write([event(time='2026-10-14T01:00:00+01:00'), event(time='2026-10-14T00:00:01Z')])
        result = reader.read_events(self.base)
        self.assertEqual([row['time'] for row in result['events']], ['2026-10-14T00:00:00.000+00:00', '2026-10-14T00:00:01.000+00:00'])

    def test_oversized_history_reads_only_its_tail_and_keeps_newest_rows(self):
        self.write([event(i) for i in range(100)])
        with patch.object(reader, 'MAX_BYTES', 1024):
            result = reader.read_events(self.base)
        self.assertTrue(result['truncated'])
        self.assertLess(len(result['events']), 10)
        self.assertEqual(result['events'][-1]['time'], reader.normalize_event(event(99), 'backend')['time'])

    def test_response_budget_caps_default_json_encoding_and_preserves_newest(self):
        records = []
        for index in range(1000):
            row = event(index)
            row['context'] = {key: '\u2603' * 10 for key in reader._CONTEXT}
            records.append(row)
        # A large valid record is limited to MAX_RECORD by the producer too.
        data = b''.join(journal(row) for row in records)
        with patch.object(reader, '_query_journal', return_value=(data, set(), False, True)):
            result = reader.read_events(self.base)
        self.assertLessEqual(len(json.dumps(result).encode()), reader.MAX_RESPONSE_BYTES)
        self.assertTrue(result['truncated'])
        self.assertTrue(result['events'])
        self.assertEqual(result['events'][-1]['time'], reader.normalize_event(event(999), 'fan')['time'])

    @unittest.skipUnless(os.name == 'posix', 'POSIX no-follow and ownership boundary')
    def test_symlinks_hardlinks_fifo_and_nonprivate_files_are_refused_without_reading(self):
        outside = self.base / 'outside'
        outside.write_bytes(json.dumps(event()).encode() + b'\n')
        outside.chmod(0o600)
        leaf = self.directory / 'events.jsonl'
        leaf.symlink_to(outside)
        self.assertEqual(reader.read_events(self.base)['events'], [])
        leaf.unlink()
        os.link(outside, leaf)
        self.assertEqual(reader.read_events(self.base)['events'], [])
        leaf.unlink()
        os.mkfifo(leaf, 0o600)
        before = time.monotonic()
        result = reader.read_events(self.base)
        self.assertLess(time.monotonic() - before, 1)
        self.assertIn('backend_unsafe', result['warnings'])
        leaf.unlink()
        self.write([event()]).chmod(0o644)
        self.assertEqual(reader.read_events(self.base)['events'], [])

    @unittest.skipUnless(os.name == 'posix', 'POSIX directory descriptor boundary')
    def test_symlinked_or_writable_log_directory_is_refused(self):
        self.write([event()])
        link = self.base / 'alias'
        link.symlink_to(self.directory, target_is_directory=True)
        other = self.base / 'other'
        other.mkdir(mode=0o700)
        (other / 'diagnostics').symlink_to(self.directory, target_is_directory=True)
        self.assertEqual(reader.read_events(other)['events'], [])
        self.directory.chmod(0o777)
        self.assertEqual(reader.read_events(self.base)['events'], [])

    @unittest.skipUnless(os.name == 'posix', 'Decky SDK log owner can differ from backend UID')
    def test_sdk_owned_private_logs_are_read_when_backend_uid_differs(self):
        self.write([event()])
        # Real files keep the SDK base's owner; only the simulated plugin UID
        # differs, as on Decky where base/diagnostics/files are owned by deck.
        with patch.object(reader.os, 'geteuid', return_value=os.getuid() + 100):
            result = reader.read_events(self.base)
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['warnings'], [])

    @unittest.skipUnless(os.name == 'posix', 'POSIX mixed owner history across backend reload')
    def test_current_backend_owned_history_also_works_in_sdk_owned_directory(self):
        self.write([event()])
        backend_uid = os.getuid() + 100
        real_fstat = os.fstat
        def owner(fd):
            info = real_fstat(fd)
            if stat.S_ISREG(info.st_mode):
                values = list(info)
                values[4] = backend_uid
                return os.stat_result(values)
            return info
        with patch.object(reader.os, 'geteuid', return_value=backend_uid), patch.object(reader.os, 'fstat', side_effect=owner):
            result = reader.read_events(self.base)
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['warnings'], [])

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership identity boundary')
    def test_a_third_owner_is_rejected_for_both_diagnostic_directory_and_file(self):
        self.write([event()])
        real_fstat = os.fstat
        child_inode = self.directory.stat().st_ino
        for change_directory in (True, False):
            def owner(fd):
                info = real_fstat(fd)
                if (change_directory and info.st_ino == child_inode
                        or not change_directory and stat.S_ISREG(info.st_mode)):
                    values = list(info)
                    values[4] = os.getuid() + 200
                    return os.stat_result(values)
                return info
            with self.subTest(directory=change_directory):
                with patch.object(reader.os, 'geteuid', return_value=os.getuid() + 100), patch.object(reader.os, 'fstat', side_effect=owner):
                    result = reader.read_events(self.base)
                self.assertEqual(result['events'], [])
                self.assertIn('backend_unsafe', result['warnings'])


@unittest.skipUnless(os.name == 'posix', 'Real bounded journal pipe transport is Linux-only')
class JournalProcessTests(unittest.TestCase):
    def run_child(self, script, **limits):
        actual = subprocess.Popen
        invoked = []
        def child(arguments, **kwargs):
            invoked.append((arguments, kwargs))
            return actual([sys.executable, '-c', script], **kwargs)
        with patch.object(reader.subprocess, 'Popen', side_effect=child):
            with _patches(limits):
                result = reader._query_journal()
        self.assertEqual(invoked[0][0], reader._JOURNAL_ARGV)
        self.assertEqual(invoked[0][0][0], '/usr/bin/journalctl')
        self.assertIn('_UID=0', invoked[0][0])
        self.assertEqual(invoked[0][1]['stdin'], subprocess.DEVNULL)
        self.assertNotIn('shell', invoked[0][1])
        return result

    def test_stdout_limit_is_enforced_during_read_not_after_communicate(self):
        data, warnings, truncated, available = self.run_child("import os; os.write(1,b'x'*1000000)", JOURNAL_BYTES=256)
        self.assertEqual(len(data), 256)
        self.assertEqual(warnings, {'journal_output_limit'})
        self.assertTrue(truncated)
        self.assertFalse(available)

    def test_stderr_limit_is_also_bounded_and_never_returned(self):
        data, warnings, truncated, available = self.run_child("import os; os.write(2,b'private error'*100000)", JOURNAL_STDERR_BYTES=32)
        self.assertEqual(data, b'')
        self.assertEqual(warnings, {'journal_output_limit'})
        self.assertTrue(truncated)
        self.assertFalse(available)

    def test_deadline_reaps_only_the_owned_readonly_child(self):
        before = time.monotonic()
        data, warnings, truncated, available = self.run_child('import time; time.sleep(30)', JOURNAL_SECONDS=.1)
        self.assertLess(time.monotonic() - before, 1.5)
        self.assertEqual(warnings, {'journal_timeout'})
        self.assertFalse(available)
        self.assertFalse(truncated)
        self.assertEqual(data, b'')

    def test_nonzero_exit_keeps_only_fixed_warning_not_stderr(self):
        data, warnings, truncated, available = self.run_child("import sys; sys.stderr.write('/home/user/password'); sys.exit(1)")
        self.assertEqual(warnings, {'journal_unavailable'})
        self.assertFalse(available)
        self.assertEqual(data, b'')

    def test_descriptor_allocation_failure_is_an_unavailable_source(self):
        with patch.object(reader.selectors, 'DefaultSelector', side_effect=OSError(24, 'private detail')):
            result = reader._query_journal()
        self.assertEqual(result, (b'', {'journal_unavailable'}, False, False))


def _patches(limits):
    stack = contextlib.ExitStack()
    for name, value in limits.items():
        stack.enter_context(patch.object(reader, name, value))
    return stack


if __name__ == '__main__':
    unittest.main()

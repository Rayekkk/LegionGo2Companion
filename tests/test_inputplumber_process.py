# SPDX-License-Identifier: BSD-3-Clause

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import inputplumber_process as module


class ProcessWatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.proc = Path(temporary.name)
        proc_patch = patch.object(module, "PROC_ROOT", self.proc)
        proc_patch.start()
        self.addCleanup(proc_patch.stop)
        self.watch = module.ProcessWatch()
        self.write_stat(101, starttime=12345)

    def write_stat(self, pid, *, state="S", starttime=12345,
                   comm="Input (Plumber) worker)"):
        process = self.proc / str(pid)
        process.mkdir(exist_ok=True)
        # Linux /proc/PID/stat: state is field 3, starttime is field 22.
        fields = [state] + ["0"] * 18 + [str(starttime)] + ["0"] * 30
        (process / "stat").write_text(
            f"{pid} ({comm}) " + " ".join(fields) + "\n", encoding="utf-8")

    def query(self, *, owner=":1.23", pid=101):
        def reply(args):
            if args[4] == "GetNameOwner":
                self.assertEqual(args, [
                    "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                    "org.freedesktop.DBus", "GetNameOwner", "s",
                    "org.shadowblip.InputPlumber",
                ])
                return [owner]
            self.assertEqual(args, [
                "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "GetConnectionUnixProcessID", "s", owner,
            ])
            return [pid]
        return Mock(side_effect=reply)

    def test_stat_parser_handles_spaces_and_parentheses_in_process_name(self):
        self.assertEqual(module._read_process(101), ("S", 12345))

    def test_missing_process_is_distinguished_from_unreadable_stat(self):
        self.assertEqual(module._read_process(999), ("missing", 0))
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.assertIsNone(module._read_process(101))

    def test_malformed_stat_is_unknown(self):
        stat = self.proc / "101" / "stat"
        for contents in ("", "101 (truncated) S 0", "101 missing-parentheses S 0",
                         "101 (name) S " + "0 " * 18 + "not-a-number"):
            with self.subTest(contents=contents):
                stat.write_text(contents, encoding="utf-8")
                self.assertIsNone(module._read_process(101))

    def test_capture_resolves_unique_owner_and_ticks_use_no_dbus(self):
        query = self.query()
        self.watch.capture(query)
        self.assertEqual(query.call_count, 2)
        for _ in range(100):
            self.assertFalse(self.watch.consume_exit())
        self.assertEqual(query.call_count, 2)

    def test_explicit_owner_avoids_name_lookup_and_healthy_recapture_is_cached(self):
        query = self.query()
        self.watch.capture(query, owner=":1.23")
        self.assertEqual(query.call_count, 1)
        self.watch.capture(query, owner=":1.23")
        self.watch.capture(query)
        self.assertFalse(self.watch.consume_exit())
        self.assertEqual(query.call_count, 1)

    def test_process_death_is_consumed_only_once(self):
        query = self.query()
        self.watch.capture(query)
        (self.proc / "101" / "stat").unlink()
        self.assertTrue(self.watch.consume_exit())
        self.assertFalse(self.watch.consume_exit())
        self.assertEqual(query.call_count, 2)

    def test_pid_reuse_and_zombie_are_consumed_only_once(self):
        for state, starttime in (("S", 98765), ("Z", 12345)):
            with self.subTest(state=state, starttime=starttime):
                self.write_stat(101)
                watch = module.ProcessWatch()
                query = self.query()
                watch.capture(query)
                self.write_stat(101, state=state, starttime=starttime)
                self.assertTrue(watch.consume_exit())
                self.assertFalse(watch.consume_exit())
                self.assertEqual(query.call_count, 2)

    def test_unreadable_or_malformed_tick_preserves_process_token(self):
        query = self.query()
        self.watch.capture(query)
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.assertFalse(self.watch.consume_exit())
        (self.proc / "101" / "stat").write_text("malformed", encoding="utf-8")
        self.assertFalse(self.watch.consume_exit())
        self.write_stat(101)
        self.assertFalse(self.watch.consume_exit())
        (self.proc / "101" / "stat").unlink()
        self.assertTrue(self.watch.consume_exit())
        self.assertEqual(query.call_count, 2)

    def test_clear_drops_token_without_reporting_process_exit(self):
        query = self.query()
        self.watch.capture(query)
        self.watch.clear()
        (self.proc / "101" / "stat").unlink()
        self.assertFalse(self.watch.consume_exit())
        self.write_stat(101, starttime=98765)
        self.watch.capture(query)
        self.assertEqual(query.call_count, 4)
        self.assertFalse(self.watch.consume_exit())

    def test_capture_can_track_new_process_after_consumed_exit(self):
        self.watch.capture(self.query())
        (self.proc / "101" / "stat").unlink()
        self.assertTrue(self.watch.consume_exit())
        self.write_stat(202, starttime=98765)
        query = self.query(owner=":1.24", pid=202)
        self.watch.capture(query, owner=":1.24")
        self.assertFalse(self.watch.consume_exit())
        self.write_stat(202, state="Z", starttime=98765)
        self.assertTrue(self.watch.consume_exit())
        self.assertFalse(self.watch.consume_exit())
        self.assertEqual(query.call_count, 1)

    def test_invalid_owner_never_reaches_pid_lookup(self):
        for owner in ("org.shadowblip.InputPlumber", ":1", ":1.2 extra", "", 123):
            with self.subTest(owner=owner):
                watch = module.ProcessWatch()
                query = Mock(return_value=[owner])
                watch.capture(query)
                self.assertEqual(query.call_count, 1)
                self.assertFalse(watch.consume_exit())
                query.reset_mock()
                watch.capture(query, owner=owner)
                query.assert_not_called()
                self.assertFalse(watch.consume_exit())

    def test_invalid_pid_is_rejected_without_reading_proc(self):
        for pid in (0, -1, True, 101.0, "101", None):
            with self.subTest(pid=pid):
                watch = module.ProcessWatch()
                with patch.object(module, "_read_process") as read:
                    watch.capture(self.query(pid=pid), owner=":1.23")
                    self.assertFalse(watch.consume_exit())
                    read.assert_not_called()

    def test_dbus_errors_and_invalid_response_shapes_are_best_effort(self):
        for response in (None, [], [":1.23", ":1.24"], {"data": [":1.23"]}):
            with self.subTest(response=response):
                watch = module.ProcessWatch()
                watch.capture(Mock(return_value=response))
                self.assertFalse(watch.consume_exit())
        for failed_lookup in ("owner", "pid"):
            with self.subTest(failed_lookup=failed_lookup):
                watch = module.ProcessWatch()
                query = Mock(side_effect=OSError("D-Bus unavailable"))
                watch.capture(query, **({"owner": ":1.23"} if failed_lookup == "pid" else {}))
                self.assertFalse(watch.consume_exit())

    def test_failed_new_owner_capture_drops_old_process_token(self):
        self.watch.capture(self.query())
        query = Mock(side_effect=OSError("D-Bus unavailable"))
        self.watch.capture(query, owner=":1.24")
        (self.proc / "101" / "stat").unlink()
        self.assertFalse(self.watch.consume_exit())
        self.write_stat(202, starttime=98765)
        self.watch.capture(self.query(owner=":1.24", pid=202), owner=":1.24")
        (self.proc / "202" / "stat").unlink()
        self.assertTrue(self.watch.consume_exit())

    def test_inflight_capture_cannot_restore_token_after_clear(self):
        def delayed_reply(_args):
            self.watch.clear()
            return [101]

        self.watch.capture(delayed_reply, owner=":1.23")
        (self.proc / "101" / "stat").unlink()
        self.assertFalse(self.watch.consume_exit())

    def test_inflight_capture_cannot_replace_newer_capture(self):
        self.write_stat(202, starttime=98765)
        query = self.query(owner=":1.24", pid=202)

        def delayed_reply(_args):
            self.watch.capture(query, owner=":1.24")
            return [101]

        self.watch.capture(delayed_reply, owner=":1.23")
        (self.proc / "101" / "stat").unlink()
        self.assertFalse(self.watch.consume_exit())
        (self.proc / "202" / "stat").unlink()
        self.assertTrue(self.watch.consume_exit())
        self.assertEqual(query.call_count, 1)

    def test_newer_capture_wins_when_older_lookup_finishes_first(self):
        self.write_stat(202, starttime=98765)
        old_entered, new_entered = threading.Event(), threading.Event()
        release_old, release_new = threading.Event(), threading.Event()
        failures = []

        def reply(pid, entered, release):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Timed out releasing process lookup")
            return [pid]

        old_query = Mock(side_effect=lambda _args: reply(101, old_entered, release_old))
        new_query = Mock(side_effect=lambda _args: reply(202, new_entered, release_new))

        def capture(query, owner):
            try:
                self.watch.capture(query, owner=owner)
            except BaseException as error:
                failures.append(error)

        older = threading.Thread(target=capture, args=(old_query, ":1.23"), daemon=True)
        newer = threading.Thread(target=capture, args=(new_query, ":1.24"), daemon=True)
        older.start()
        try:
            self.assertTrue(old_entered.wait(2), "Older lookup did not start")
            newer.start()
            self.assertTrue(new_entered.wait(2), "Newer lookup did not start")
            release_old.set()
            older.join(2)
            self.assertFalse(older.is_alive(), "Older lookup did not finish first")
            release_new.set()
            newer.join(2)
        finally:
            release_old.set()
            release_new.set()
            older.join(2)
            if newer.ident is not None:
                newer.join(2)
        self.assertFalse(newer.is_alive(), "Newer lookup did not finish")
        self.assertFalse(failures, repr(failures))
        self.assertEqual(old_query.call_count, 1)
        self.assertEqual(new_query.call_count, 1)
        (self.proc / "101" / "stat").unlink()
        self.assertFalse(self.watch.consume_exit(), "Older lookup replaced the newer token")
        (self.proc / "202" / "stat").unlink()
        self.assertTrue(self.watch.consume_exit(), "Newer process was not tracked")
        self.assertFalse(self.watch.consume_exit())

    def consume_during(self, operation):
        """Hold an old /proc read while another operation changes the token."""
        entered, release, updated = threading.Event(), threading.Event(), threading.Event()
        result, failures = [], []
        read_process = module._read_process

        def delayed_read(pid):
            if threading.current_thread() is reader:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Timed out releasing stale process read")
                return ("missing", 0)
            return read_process(pid)

        def consume():
            try:
                result.append(self.watch.consume_exit())
            except BaseException as error:
                failures.append(error)

        def update():
            try:
                operation()
            except BaseException as error:
                failures.append(error)
            finally:
                updated.set()

        reader = threading.Thread(target=consume, daemon=True)
        updater = threading.Thread(target=update, daemon=True)
        completed = False
        with patch.object(module, "_read_process", side_effect=delayed_read):
            reader.start()
            try:
                self.assertTrue(entered.wait(2), "Process tick did not read /proc")
                updater.start()
                completed = updated.wait(2)
            finally:
                release.set()
                reader.join(2)
                if updater.ident is not None:
                    updater.join(2)
        self.assertFalse(reader.is_alive(), "Process tick did not finish")
        self.assertFalse(updater.is_alive(), "Token update did not finish")
        self.assertFalse(failures, repr(failures))
        self.assertTrue(completed, "Token update was blocked by the stale /proc read")
        self.assertEqual(result, [False], "Stale tick reported an exit after token changed")

    def test_stale_tick_cannot_consume_new_capture(self):
        self.watch.capture(self.query())
        self.write_stat(202, starttime=98765)
        query = self.query(owner=":1.24", pid=202)
        self.consume_during(lambda: self.watch.capture(query, owner=":1.24"))
        self.assertFalse(self.watch.consume_exit())
        (self.proc / "202" / "stat").unlink()
        self.assertTrue(self.watch.consume_exit())
        self.assertFalse(self.watch.consume_exit())
        self.assertEqual(query.call_count, 1)

    def test_stale_tick_cannot_report_exit_after_clear(self):
        self.watch.capture(self.query())
        self.consume_during(self.watch.clear)
        self.assertFalse(self.watch.consume_exit())


if __name__ == "__main__":
    unittest.main()

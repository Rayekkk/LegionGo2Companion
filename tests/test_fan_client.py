# SPDX-License-Identifier: BSD-3-Clause
import errno
import json
import math
import pathlib
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import support
import fan_backend as fan


def terminal(**changes):
    return dict(dict(kind='closed', active=False, automatic=True, error='Original sensor failure',
                     session='session-client-test', override=0, automatic_reads=3), **changes)


class FanClientTests(unittest.TestCase):
    def client(self):
        value = fan.Client()
        value.state = dict(kind='telemetry', session='session-client-test', active=True, automatic=False, error='')
        value.socket = Mock()
        return value

    def receive(self, client):
        with patch.object(fan.select, 'select', return_value=([client.socket], [], [])):
            client._receive()

    @unittest.skipUnless(hasattr(socket, 'AF_UNIX') and hasattr(socket, 'SOCK_SEQPACKET'), 'UNIX SEQPACKET sockets')
    def test_start_is_ready_when_ready_and_telemetry_arrive_in_one_drain(self):
        parent, child = Mock(), Mock()
        child.fileno.return_value = 7
        status = dict(session='session-client-test', active=False, automatic=True, error='')
        parent.recv.side_effect = [json.dumps(dict(status, kind='ready')).encode(),
                                   json.dumps(dict(status, kind='telemetry')).encode(), BlockingIOError()]
        c = fan.Client()
        with patch.object(fan.socket, 'socketpair', return_value=(parent, child)), \
                patch.object(fan.subprocess, 'Popen', return_value=Mock(poll=Mock(return_value=None))), \
                patch.object(fan.select, 'select', return_value=([parent], [], [])), patch.object(fan, 'SELF_TEST_S', 0):
            c.start()
        self.assertEqual(c.snapshot()['kind'], 'telemetry')

    def test_reset_before_queued_terminal_preserves_reason_and_confirmed_auto(self):
        c = self.client()
        c.socket.recv.side_effect = [ConnectionResetError(errno.ECONNRESET, 'reset'), json.dumps(terminal()).encode()]
        self.receive(c)
        self.assertEqual(c.snapshot()['error'], 'Original sensor failure')
        self.assertTrue(c.snapshot()['automatic'])
        self.assertEqual(c.socket.recv.call_count, 2)

    def test_terminal_stops_drain_before_later_reset(self):
        c = self.client()
        c.socket.recv.side_effect = [json.dumps(terminal()).encode(), ConnectionResetError(errno.ECONNRESET, 'reset')]
        self.receive(c)
        self.assertEqual(c.socket.recv.call_count, 1)
        with self.assertRaisesRegex(RuntimeError, 'Original sensor failure'):
            c.pump()

    def test_repeated_reset_is_bounded_and_never_claims_automatic(self):
        c = self.client(); sock = c.socket
        # The drain batch size is an implementation choice. This tripwire only
        # prevents an unbounded retry loop from hanging the test process.
        deadline = time.monotonic() + 5
        def reset(*_args):
            if time.monotonic() >= deadline:
                raise AssertionError('Reset draining did not terminate')
            raise ConnectionResetError(errno.ECONNRESET, 'reset')
        sock.recv.side_effect = reset
        self.receive(c)
        self.assertGreater(sock.recv.call_count, 0)
        self.assertIsNone(c.socket)
        self.assertFalse(c.snapshot()['active']); self.assertFalse(c.snapshot()['automatic'])

    def test_missing_or_mismatched_terminal_proof_is_rejected(self):
        for packet in [[], {}, terminal(session='session-other'), terminal(override=False),
                       terminal(automatic_reads=1), terminal(active=True), terminal(error={})]:
            c = self.client(); c.socket.recv.return_value = json.dumps(packet).encode()
            with self.assertRaises(RuntimeError):
                self.receive(c)

    def test_disconnected_shutdown_still_waits_and_checks_exact_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            for code in (errno.ENOTCONN, errno.ECONNRESET, errno.EPIPE):
                c = self.client(); c.runtime = pathlib.Path(d); c.process = Mock()
                c.socket.shutdown.side_effect = OSError(code, 'disconnected')
                c.socket.recv.side_effect = BlockingIOError()
                receipt = dict(session=c.state['session'], monotonic=time.monotonic(), active=False,
                               automatic=True, reason='Real backup recovery', state=dict(override=0, automatic_reads=3))
                (c.runtime/'last.json').write_text(json.dumps(receipt))
                # Avoid a real four-second drain when the transport has no data.
                def eof(wait=0): c._disconnect()
                c._receive = eof
                c.close()
                c.process.wait.assert_called_once()
                timeout = c.process.wait.call_args.kwargs.get('timeout')
                self.assertIsInstance(timeout, (int, float))
                self.assertTrue(math.isfinite(timeout) and timeout > 0,
                                'Peer reaping must retain a bounded wait')
                self.assertTrue(c.snapshot()['automatic'])
                self.assertEqual(c.snapshot()['error'], 'Real backup recovery')

    def test_receipt_requires_current_session_fresh_timestamp_and_triple_readback(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.client(); c.runtime = pathlib.Path(d); c.last_intent_at = time.monotonic()
            good = dict(session=c.state['session'], monotonic=time.monotonic(), active=False,
                        automatic=True, reason='Recovered', state=dict(override=0, automatic_reads=3))
            for edits in [dict(session='session-other'), dict(monotonic=c.last_intent_at-1),
                          dict(monotonic=time.monotonic()+10), dict(active=True), dict(automatic=False),
                          dict(state=dict(override=256, automatic_reads=3)), dict(state=dict(override=0, automatic_reads=1))]:
                (c.runtime/'last.json').write_text(json.dumps(dict(good, **edits)))
                self.assertFalse(c._recover_receipt())
            (c.runtime/'last.json').write_text(json.dumps(good))
            self.assertTrue(c._recover_receipt()); self.assertFalse(c.snapshot()['active'])

    def test_unexpected_shutdown_error_is_retained_after_recovery_check(self):
        c = self.client(); c.process = Mock()
        c.socket.shutdown.side_effect = OSError(errno.EIO, 'I/O failed')
        def confirmed(wait=0): c.state = terminal(error='')
        c._receive = confirmed
        with self.assertRaisesRegex(RuntimeError, 'I/O failed'):
            c.close()
        c.process.wait.assert_called_once()

    def test_withdraw_requests_release_without_waiting_for_the_io_lock(self):
        c = self.client(); held = threading.Event(); release = threading.Event()
        def busy():
            with c.lock:
                held.set(); release.wait(5)
        worker = threading.Thread(target=busy); worker.start()
        try:
            self.assertTrue(held.wait(5))
            c.withdraw()
            c.socket.shutdown.assert_called_once_with(socket.SHUT_WR)
        finally:
            release.set(); worker.join(5)
        with self.assertRaisesRegex(RuntimeError, 'closing'):
            c.configure(True, [])

    def test_telemetry_poll_never_sends_a_lease(self):
        c = self.client(); c.socket.recv.side_effect = BlockingIOError()
        self.receive(c)
        with patch.object(fan.select, 'select', return_value=([c.socket], [], [])):
            self.assertTrue(c.pump()['active'])
        c.socket.send.assert_not_called()


@unittest.skipUnless(__import__('sys').platform == 'linux', 'Linux UNIX/SEQPACKET reset')
class LinuxSocketTests(unittest.TestCase):
    def test_real_linux_reset_delivers_original_terminal_reason(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            a.send(b'unread heartbeat'); b.send(json.dumps(terminal()).encode()); b.close()
            a.setblocking(False)
            c = fan.Client(); c.socket = a
            c.state.update(session='session-client-test')
            with self.assertRaisesRegex(RuntimeError, 'Original sensor failure'): c.pump()
            self.assertTrue(c.snapshot()['automatic'])
        finally:
            a.close(); b.close()

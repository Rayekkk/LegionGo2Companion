# SPDX-License-Identifier: BSD-3-Clause
"""Qualification channel fault injection; no USB library or device is opened."""
import threading
import unittest

from controller_firmware import FirmwareError
from controller_firmware_usb import UsbVersionChannel, _Descriptor
import ctypes


class FakeUsb:
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []
        self.quiet = False
        self.block = None
        self.output_result = (0, 64, bytes(64))
    def interrupt(self, handle, endpoint, data, timeout):
        self.calls.append((handle, endpoint, data, timeout))
        if self.block is not None:
            started, release = self.block
            self.block = None
            started.set()
            if not release.wait(2): raise RuntimeError('Own test thread was not released.')
        if endpoint == 3:
            return self.output_result
        if not self.quiet:
            self.quiet = True
            return -7, 0, bytes(64)
        return self.replies.pop(0) if self.replies else (-7, 0, bytes(64))


def reply(target=3, version=26031010):
    return bytes((4, 0, 2, 4, target)) + version.to_bytes(4, 'big') + bytes(55)


class VersionChannelTests(unittest.TestCase):
    def channel(self, api, valid=lambda: True, clock=lambda: 1.):
        return UsbVersionChannel(api, 42, valid, clock)

    def test_exact_query_and_matching_reply_discard_wrong_targets(self):
        api = FakeUsb([(0, 64, reply(4)), (0, 64, reply(3))])
        self.assertEqual(self.channel(api).query(3, deadline=2), reply(3))
        outputs = [call for call in api.calls if call[1] == 3]
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0][2], b'\x05\x00\x02\x04\x03' + bytes(59))
        self.assertTrue(all(1 <= call[3] <= 100 for call in api.calls))
        self.assertTrue(all(call[1] in (3, 0x83) for call in api.calls))

    def test_ota_targets_and_invalid_values_never_reach_transport(self):
        api = FakeUsb()
        for target in (True, 0, 5, 7, 8, 'left', b'\xc0', None):
            with self.assertRaises(FirmwareError): self.channel(api).query(target, deadline=2)
        self.assertEqual(api.calls, [])

    def test_partial_or_timeout_output_is_not_retried(self):
        for output in ((0, 63, bytes(64)), (-7, 64, bytes(64)), (-7, 32, bytes(64))):
            api = FakeUsb(); api.output_result = output
            channel = self.channel(api)
            with self.assertRaises(FirmwareError): channel.query(3, deadline=2)
            self.assertEqual(len([c for c in api.calls if c[1] == 3]), 1)
            calls = len(api.calls)
            with self.assertRaises(FirmwareError): channel.query(3, deadline=2)
            self.assertEqual(len(api.calls), calls)

    def test_partial_timeout_in_drain_is_not_quiet_or_output_permission(self):
        api = FakeUsb(); api.quiet = True; api.replies = [(-7, 32, reply())]
        with self.assertRaises(FirmwareError): self.channel(api).query(3, deadline=2)
        self.assertFalse(any(call[1] == 3 for call in api.calls))

    def test_stale_control_before_output_is_rejected(self):
        api = FakeUsb([(0, 64, reply())] * 40); api.quiet = True
        with self.assertRaises(FirmwareError): self.channel(api).query(3, deadline=2)
        self.assertEqual(len(api.calls), 1)
        self.assertFalse(any(call[1] == 3 for call in api.calls))

    def test_continuous_gamepad_input_does_not_require_endpoint_silence(self):
        ordinary = bytes((4, 0x3c, 0x74, 1, 0)) + bytes(59)
        api = FakeUsb([(0, 64, ordinary)] * 70 + [(0, 64, reply())])
        api.quiet = True
        self.assertEqual(self.channel(api).query(3, deadline=2), reply())
        self.assertEqual(len([c for c in api.calls if c[1] == 3]), 1)

    def test_ownership_check_expiring_deadline_prevents_io(self):
        now = [1.0]
        def valid():
            now[0] = 3.0
            return True
        api = FakeUsb()
        with self.assertRaises(FirmwareError):
            self.channel(api, valid, clock=lambda: now[0]).query(3, deadline=2)
        self.assertEqual(api.calls, [])

    def test_unrelated_replies_and_empty_waits_cannot_busy_loop_forever(self):
        class AdvancingUsb(FakeUsb):
            def __init__(self, replies):
                super().__init__(replies)
                self.now = 1.
            def interrupt(self, handle, endpoint, data, timeout):
                if self.now >= 2:
                    raise AssertionError('USB I/O started after the absolute request deadline')
                outcome = super().interrupt(handle, endpoint, data, timeout)
                self.now += timeout / 1000 if outcome[0] == -7 else .001
                return outcome
        for replies in ([], [(0, 64, reply(4))] * 520):
            api = AdvancingUsb(replies)
            channel = self.channel(api, clock=lambda: api.now)
            with self.assertRaises(FirmwareError): channel.query(3, deadline=2)
            # The request deadline and transport timeout, not the current loop
            # count, bound work. Empty waits and received frames consume time.
            self.assertGreater(len(api.calls), 0)
            self.assertEqual(sum(call[1] == 3 for call in api.calls), 1)
            self.assertTrue(all(1 <= call[3] <= 100 for call in api.calls))
            before = list(api.calls)
            with self.assertRaises(FirmwareError): channel.query(3, deadline=2)
            self.assertEqual(api.calls, before, 'uncertain output must not be retried')

    def test_lease_lost_during_drain_prevents_output(self):
        api = FakeUsb(); observations = iter((True, False))
        with self.assertRaises(FirmwareError): self.channel(api, lambda: next(observations, False)).query(3, deadline=2)
        self.assertFalse(any(call[1] == 3 for call in api.calls))

    def test_late_reply_or_backward_clock_cannot_be_accepted(self):
        for times in ((1, 1, 2), (1, .9)):
            api = FakeUsb([(0, 64, reply())]); clock = iter(times)
            with self.assertRaises(FirmwareError): self.channel(api, clock=lambda: next(clock, 2)).query(3, deadline=2)

    def test_expired_or_nonfinite_deadline_cannot_request_infinite_timeout(self):
        for deadline in (1, .9, True, float('nan'), float('inf'), '2', 1.0001):
            api = FakeUsb()
            with self.assertRaises(FirmwareError): self.channel(api).query(3, deadline=deadline)
            self.assertFalse(api.calls)

    def test_concurrent_query_cannot_interleave_or_invalidate_first_owner(self):
        api = FakeUsb([(0, 64, reply())]); started, release = threading.Event(), threading.Event()
        api.block = started, release
        channel = self.channel(api); outcomes = []
        def run():
            try: outcomes.append(channel.query(3, deadline=2))
            except BaseException as exc: outcomes.append(exc)
        thread = threading.Thread(target=run); thread.start()
        try:
            self.assertTrue(started.wait(1))
            with self.assertRaises(FirmwareError): channel.query(4, deadline=2)
        finally:
            release.set(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertEqual(outcomes, [reply()])

    def test_descriptor_abi_size_is_eighteen_bytes(self):
        self.assertEqual(ctypes.sizeof(_Descriptor), 18)


if __name__ == '__main__': unittest.main()

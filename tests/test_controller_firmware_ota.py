# SPDX-License-Identifier: BSD-3-Clause
"""Simulated raw USB; no hardware access or live flashing."""
from dataclasses import replace
import unittest

from controller_firmware import FirmwareError
from controller_firmware_ota import OtaTransport
from controller_firmware_protocol import Preflight
from controller_firmware_transfer import Command
from controller_firmware_worker import ControllerClaim
import test_controller_firmware_transfer as transfer_tests


class Api:
    def __init__(self):
        self.now=0.;self.calls=[];self.replies=[];self.output=(0,64,bytes(64))
    def interrupt(self,handle,endpoint,data,timeout):
        self.calls.append((endpoint,data,timeout))
        if endpoint==3:return self.output
        if self.replies:return (0,64,self.replies.pop(0))
        self.now+=timeout/1000
        return (-7,0,bytes(64))


class OtaTests(unittest.TestCase):
    def make(self):
        self.api=Api();self.owned=True;self.closed=[]
        self.claim=ControllerClaim('a'*64,'epoch',Preflight('Legion Go 8ASP2',2,'250101A',True,True,True,None,'epoch',0.))
        transport=OtaTransport('left',self.api,123,self.claim,lambda:self.owned,
            lambda *args:None,lambda:self.closed.append(True),clock=lambda:self.api.now)
        transport.acquire('left',1.)
        return transport
    def command(self,sequence=1):return Command(sequence,'start',bytes((5,0,0xc0,1,7))+bytes(59),True,.5)

    def test_output_is_exactly_once_and_retains_reply_arriving_during_output(self):
        transport=self.make();self.assertEqual(transport.write(self.command(),1.),64)
        ack=bytes((4,0,0xc0,1,7))+bytes(59)
        self.api.replies=[b'\x04\x3c'+bytes(62),ack]
        self.assertEqual(transport.read(.4),ack)
        self.assertEqual(len([x for x in self.api.calls if x[0]==3]),1)
        with self.assertRaises(FirmwareError):transport.write(self.command(),1.)
        transport.close();transport.close();self.assertEqual(self.closed,[True])

    def test_partial_or_timed_out_output_latches_failure_without_retry(self):
        for outcome in ((0,63,bytes(64)),(-7,64,bytes(64)),(-1,0,bytes(64))):
            transport=self.make();self.api.output=outcome
            with self.assertRaises(FirmwareError):transport.write(self.command(),1.)
            with self.assertRaises(FirmwareError):transport.write(self.command(),1.)
            self.assertEqual(len([x for x in self.api.calls if x[0]==3]),1)

    def test_stale_control_reply_blocks_first_output(self):
        transport=self.make();self.api.replies=[bytes((4,0,0xc0,1,7))+bytes(59)]
        with self.assertRaises(FirmwareError):transport.write(self.command(),1.)
        self.assertFalse(any(x[0]==3 for x in self.api.calls))

    def test_read_filters_continuous_input_but_rejects_foreign_update_reply(self):
        transport=self.make();transport.write(self.command(),1.)
        self.api.replies=[b'\x04\x3c'+bytes(62)]*70+[bytes((4,0,0xa0,3,7,1))+bytes(58)]
        self.assertEqual(transport.read(.4)[:4],b'\x04\x00\xa0\x03')
        self.api.replies=[bytes((4,0,0xc0,1,8))+bytes(59)]
        with self.assertRaises(FirmwareError):transport.read(.4)
        self.assertTrue(transport.failed)

    def test_empty_read_performs_bounded_wait_and_never_uses_infinite_timeout(self):
        transport=self.make();self.assertIsNone(transport.read(.2));self.assertGreaterEqual(self.api.now,.2)
        self.assertTrue(all(1<=x[2]<=100 for x in self.api.calls))

    def test_empty_in_timeout_scheduler_overrun_does_not_fail_whole_transaction(self):
        transport = self.make()
        original = self.api.interrupt
        def delayed(*args):
            result = original(*args)
            self.api.now += .003
            return result
        self.api.interrupt = delayed
        self.assertIsNone(transport.read(.1))
        self.assertFalse(transport.failed)

    def test_lost_or_expired_permission_prevents_output(self):
        transport=self.make();self.owned=False
        with self.assertRaises(FirmwareError):transport.write(self.command(),1.)
        self.assertEqual(self.api.calls,[])
        transport=self.make();self.api.now=2
        with self.assertRaises(FirmwareError):transport.write(self.command(),1.)
        self.assertEqual(self.api.calls,[])

    def test_closed_handle_and_wrong_target_cannot_send(self):
        transport=self.make();bad=replace(self.command(),report=bytes((5,0,0xc0,1,8))+bytes(59))
        with self.assertRaises(FirmwareError):transport.write(bad,1.)
        transport.close()
        with self.assertRaises(FirmwareError):transport.read(1.)
        self.assertEqual(self.api.calls,[])

    def test_vendor_dynamic_boundary_accepts_positive_signed_int32_not_guessed_alignment(self):
        helper=transfer_tests.TransferTests()
        for boundary in (1,31,33,64,1024,0x7fffffff):
            session=helper.make(policy=helper.policy(allowed_boundaries=None))
            helper.drive(session,boundary=boundary)
            self.assertEqual(session.snapshot()['phase'],'awaiting_completion')


if __name__=='__main__':unittest.main()

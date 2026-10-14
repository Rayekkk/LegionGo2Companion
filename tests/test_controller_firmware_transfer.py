# SPDX-License-Identifier: BSD-3-Clause
from dataclasses import replace
import os
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

import controller_firmware as fw
import controller_firmware_protocol as protocol
import controller_firmware_transfer as transfer

BINDING = 'a'*64


def reply(component, command, payload=b'', family=0xc0):
    target = protocol.OTA_TARGETS[component] if family != 2 else protocol.VERSION_QUERY_TARGETS[component]
    return bytes((4, 0, family, command, target)) + payload + bytes(59-len(payload))


class Clock:
    def __init__(self): self.value = 0.
    def step(self): self.value += .0001; return self.value


class TransferTests(unittest.TestCase):
    def policy(self, boundary=64, **changes):
        return replace(transfer.TransferPolicy(frozenset((boundary,)), .5, 10., 5., 60.), **changes)

    def make(self, component='left', data=b'X'*65, checkpoint=None, policy=None):
        # Synthetic bytes test scheduling only. Authentication has separate
        # original-BIN tests and is never mocked in the production core.
        with patch.object(transfer, 'validate_image', return_value=fw.ValidatedImage(fw.release_for(component), data)):
            return transfer.TransferSession(component, data, policy or self.policy(), checkpoint or (lambda value: None))

    def good(self):
        return protocol.Preflight('Legion Go 8ASP2', 2, '251204A', True, True, True, None, 'before', 0.)

    def observation(self, session, now):
        version = int(session.snapshot()['offered_version'], 16)
        return transfer.InstalledObservation(BINDING, 'Legion Go 8ASP2', 2, 'after', now,
            session.snapshot()['verification_token'], reply(session.snapshot()['component'], 4, struct.pack('>I', version), 2))

    def drive(self, session, clock=None, boundary=64, early=False, until=None):
        clock = clock or Clock(); commands=[]
        if session.snapshot()['phase'] == 'idle': session.start(self.good(), 'before', BINDING, clock.step())
        while session.snapshot()['phase'].startswith('ready_'):
            command = session.next_command(clock.step()); commands.append(command)
            if command.sequence == until: return commands, clock
            session.sent(command.sequence, 64, clock.step())
            if command.needs_reply:
                if command.kind == 'finish' and early:
                    session.receive(reply(session.snapshot()['component'], 3, b'\1', 0xa0), clock.step())
                payload = struct.pack('<I', boundary) if command.kind == 'boundary' else b'\0'
                session.receive(reply(session.snapshot()['component'], command.report[3], payload), clock.step())
        return commands, clock

    def complete(self, session, clock, early=False):
        if not early:
            self.assertEqual(session.snapshot()['phase'], 'awaiting_completion')
            self.assertEqual(session.snapshot()['sent_bytes'], session.snapshot()['total_bytes'])
            session.receive(reply(session.snapshot()['component'], 3, b'\1', 0xa0), clock.step())
        self.assertEqual(session.snapshot()['phase'], 'verifying_version')
        session.confirm_installed(self.observation(session, clock.step()), clock.step())
        self.assertEqual(session.snapshot()['phase'], 'succeeded')

    def test_all_components_no_ack_blocks_partial_final_and_exact_original_stream(self):
        for component in fw.RELEASES:
            original=bytes(range(65)); session=self.make(component, original)
            commands, clock=self.drive(session)
            self.assertEqual([c.report[3] for c in commands], [1,2,3,4,4,4,5])
            chunks=[c for c in commands if c.kind=='block']
            self.assertEqual([c.needs_reply for c in chunks], [False,True,True])
            self.assertEqual(b''.join(c.report[10:10+c.report[9]] for c in chunks), original)
            self.assertEqual([c.end_bytes for c in chunks], [32,64,65])
            self.complete(session, clock)

    def test_byte_counts_never_conflate_no_ack_write_with_acknowledged_data(self):
        session=self.make(); commands,clock=self.drive(session,until=4)
        command=commands[-1]; self.assertFalse(command.needs_reply)
        self.assertEqual(session.snapshot()['sent_bytes'],0)
        session.sent(command.sequence,64,clock.step())
        self.assertEqual(session.snapshot()['sent_bytes'],32)
        self.assertEqual(session.snapshot()['acknowledged_bytes'],0)
        next_command=session.next_command(clock.step()); session.sent(next_command.sequence,64,clock.step())
        self.assertEqual(session.snapshot()['sent_bytes'],64)
        self.assertEqual(session.snapshot()['acknowledged_bytes'],0)
        session.receive(reply('left',4),clock.step())
        self.assertEqual(session.snapshot()['acknowledged_bytes'],64)

    def test_boundary_schedules_multiple_sizes_and_always_waits_for_final_block(self):
        for boundary in (32,64,1024):
            for size in (1,32,33,64,65,96,1025):
                session=self.make(data=b'Q'*size,policy=self.policy(boundary))
                commands,_=self.drive(session,boundary=boundary)
                chunks=[c for c in commands if c.kind=='block']
                self.assertTrue(chunks[-1].needs_reply)
                self.assertEqual([c.needs_reply for c in chunks], [c.end_bytes%boundary==0 or c.end_bytes==size for c in chunks])
                self.assertEqual(session.snapshot()['sent_bytes'],size)
                self.assertEqual(session.snapshot()['acknowledged_bytes'],size)

    def test_policy_has_no_implicit_hardware_defaults(self):
        for changes in ({'allowed_boundaries':set((64,))},{'allowed_boundaries':frozenset()},
                {'allowed_boundaries':frozenset((True,))},{'allowed_boundaries':frozenset((0,))},
                {'write_timeout':float('nan')},{'write_timeout':10**400},{'completion_timeout':0},{'verification_timeout':True},
                {'transaction_timeout':3601}):
            with self.assertRaises(fw.FirmwareError):self.policy(**changes)
        original=b'X'*65
        with patch.object(transfer,'validate_image',return_value=fw.ValidatedImage(fw.release_for('left'),original)):
            control=transfer.TransferSession('left',original,self.policy(),lambda value:None)
            self.assertEqual(control.snapshot()['phase'],'idle')
            with self.assertRaises(fw.FirmwareError): transfer.TransferSession('left',original,None,lambda value:None)
            with self.assertRaises(fw.FirmwareError): transfer.TransferSession('left',original,self.policy(),None)

    def test_bad_preflight_or_binding_never_hands_out_start_packet(self):
        for changes in ({'model':'unknown'},{'left_attached':False},{'right_attached':False},
                {'ac':False,'battery_percent':29},{'installed_version':'260422A'},{'observed_at':-10},
                {'connection_epoch':'different'},{'observed_at':10**400},{'observed_at':-1}):
            session=self.make()
            with self.assertRaises(fw.FirmwareError): session.start(replace(self.good(),**changes),'before',BINDING,0.)
            self.assertEqual(session.snapshot()['phase'],'failed_before_write')
            self.assertFalse(session.snapshot()['write_attempted'])
        session=self.make()
        with self.assertRaises(fw.FirmwareError):session.start(self.good(),'before','',0.)
        self.assertEqual(session.snapshot()['sequence'],0)

    def test_missing_write_or_reply_at_every_output_is_uncertain_without_replay(self):
        for output in range(1,8):
            session=self.make();commands,clock=self.drive(session,until=output)
            command=commands[-1]
            with self.assertRaises(fw.FirmwareError):session.tick(clock.value+.501)
            self.assertEqual(session.snapshot()['phase'],'uncertain')
            self.assertEqual(session.snapshot()['sequence'],output)
            with self.assertRaises(fw.FirmwareError):session.next_command(clock.value+.6)
            self.assertEqual(session.snapshot()['sequence'],output)
            if command.needs_reply:
                session=self.make();commands,clock=self.drive(session,until=output)
                command=commands[-1];session.sent(command.sequence,64,clock.step())
                with self.assertRaises(fw.FirmwareError): session.tick(clock.value+command.reply_timeout+.001)
                self.assertEqual(session.snapshot()['phase'],'uncertain')

    def test_unknown_boundary_partial_write_wrong_sequence_and_wrong_ack_fail_closed(self):
        session=self.make()
        with self.assertRaises(fw.FirmwareError):self.drive(session,boundary=32)
        self.assertEqual(session.snapshot()['phase'],'uncertain')
        for sequence,count in ((1,63),(2,64),(True,64),(1,True)):
            session=self.make();_,clock=self.drive(session,until=1)
            with self.assertRaises(fw.FirmwareError):session.sent(sequence,count,clock.step())
            self.assertEqual(session.snapshot()['phase'],'uncertain')
        for response in (reply('right',1),reply('left',2),reply('left',1,b'\1'),b''):
            session=self.make();_,clock=self.drive(session,until=1);session.sent(1,64,clock.step())
            with self.assertRaises(fw.FirmwareError):session.receive(response,clock.step())
            self.assertEqual(session.snapshot()['phase'],'uncertain')

    def test_completion_before_finish_ack_is_buffered_but_ack_is_still_required(self):
        session=self.make();_,clock=self.drive(session,early=True)
        self.complete(session,clock,early=True)
        session=self.make();commands,clock=self.drive(session,until=7)
        session.sent(commands[-1].sequence,64,clock.step())
        session.receive(reply('left',3,b'\1',0xa0),clock.step())
        self.assertEqual(session.snapshot()['phase'],'awaiting_reply')
        with self.assertRaises(fw.FirmwareError):session.tick(clock.value+4.001)
        self.assertEqual(session.snapshot()['phase'],'uncertain')

    def test_all_bytes_or_completion_alone_are_not_success_and_invalid_completion_is_uncertain(self):
        for data in (reply('left',3,b'\0',0xa0),reply('left',3,b'\2',0xa0),reply('right',3,b'\1',0xa0),b''):
            session=self.make();_,clock=self.drive(session)
            with self.assertRaises(fw.FirmwareError):session.receive(data,clock.step())
            self.assertEqual(session.snapshot()['phase'],'uncertain')
        session=self.make();_,clock=self.drive(session)
        with self.assertRaises(fw.FirmwareError):session.tick(clock.value+10.001)
        session=self.make();_,clock=self.drive(session)
        session.receive(reply('left',3,b'\1',0xa0),clock.step())
        with self.assertRaises(fw.FirmwareError):session.tick(clock.value+5.001)
        self.assertEqual(session.snapshot()['phase'],'uncertain')

    def test_installed_confirmation_requires_fresh_request_bound_identity_and_exact_version(self):
        for changes in ({'device_binding':'b'*64},{'model':'Legion Go S'},{'hardware_generation':True},
                {'connection_epoch':''},{'sampled_at':-1},{'sampled_at':float('nan')},{'sampled_at':10**400},
                {'sampled_at':100.},{'verification_token':'old'},
                {'version_report':reply('left',4,struct.pack('>I',int('251204A',16)),2)},
                {'version_report':reply('right',4,struct.pack('>I',int('260310A',16)),2)}):
            session=self.make();_,clock=self.drive(session)
            session.receive(reply('left',3,b'\1',0xa0),clock.step())
            with self.assertRaises(fw.FirmwareError):session.confirm_installed(replace(self.observation(session,clock.step()),**changes),clock.step())
            self.assertEqual(session.snapshot()['phase'],'uncertain')
        session=self.make();_,clock=self.drive(session)
        with self.assertRaises(fw.FirmwareError):session.confirm_installed(self.observation(session,clock.step()),clock.step())

    def test_clock_invariants_overall_deadline_and_external_fault(self):
        for time in (True,float('nan'),float('inf'),-1,10**400):
            session=self.make()
            with self.assertRaises(fw.FirmwareError):session.start(self.good(),'before',BINDING,time)
            self.assertEqual(session.snapshot()['phase'],'failed_before_write')
        session=self.make();session.start(self.good(),'before',BINDING,1.)
        with self.assertRaises(fw.FirmwareError):session.tick(.5)
        self.assertEqual(session.snapshot()['phase'],'failed_before_write')
        session=self.make(policy=self.policy(transaction_timeout=.1));self.drive(session,until=1)
        with self.assertRaises(fw.FirmwareError):session.tick(.101)
        self.assertEqual(session.snapshot()['phase'],'uncertain')
        session=self.make();session.start(self.good(),'before',BINDING,0.)
        with self.assertRaises(fw.FirmwareError):session.abort()
        self.assertEqual(session.snapshot()['phase'],'failed_before_write')
        session=self.make();self.drive(session,until=4)
        with self.assertRaises(fw.FirmwareError):session.abort()
        self.assertEqual(session.snapshot()['phase'],'uncertain')
        session=self.make();self.drive(session,until=1)
        with self.assertRaises(fw.FirmwareError):session.tick(10**400)
        self.assertEqual(session.snapshot()['phase'],'uncertain')

    def test_durable_intent_precedes_first_packet_and_journal_failure_stops_progress(self):
        saved=[];session=self.make(checkpoint=lambda value:saved.append(value))
        session.start(self.good(),'before',BINDING,0.)
        command=session.next_command(.01)
        self.assertEqual(saved[-1]['phase'],'awaiting_send');self.assertTrue(saved[-1]['write_attempted'])
        self.assertEqual(saved[-1]['sequence'],command.sequence)
        saved[-1]['sent_bytes']=999
        self.assertEqual(session.snapshot()['sent_bytes'],0)
        for fail_at in (1,2,3,4,5):
            calls=[]
            def save(value):
                calls.append(value)
                if len(calls)>=fail_at: raise OSError('synthetic full disk')
            session=self.make(checkpoint=save)
            with self.assertRaises(fw.FirmwareError):self.drive(session)
            self.assertEqual(session.snapshot()['phase'],'failed_before_write' if fail_at<=2 else 'uncertain')
            self.assertTrue(session.snapshot()['journal_error'])

    def test_progress_checkpoint_rate_and_final_journal_failure(self):
        saved=[];session=self.make(data=b'X'*1025,checkpoint=lambda value:saved.append(value),policy=self.policy(32))
        commands,clock=self.drive(session,boundary=32)
        self.assertEqual(sum(c.kind=='block' for c in commands),33)
        self.assertLess(len(saved),12,'no synchronous disk save per 32-byte block')
        session.receive(reply('left',3,b'\1',0xa0),clock.step())
        def fail(value): raise OSError('synthetic final persistence error')
        session._checkpoint=fail
        with self.assertRaises(fw.FirmwareError):session.confirm_installed(self.observation(session,clock.step()),clock.step())
        self.assertEqual(session.snapshot()['phase'],'uncertain')

    def test_closed_session_is_never_restarted_or_replayed(self):
        session=self.make();_,clock=self.drive(session);self.complete(session,clock)
        for operation in (lambda:session.next_command(clock.step()),lambda:session.start(self.good(),'before',BINDING,clock.step()),
                lambda:session.receive(reply('left',1),clock.step()),lambda:session.abort()):
            with self.assertRaises(fw.FirmwareError):operation()
            self.assertEqual(session.snapshot()['phase'],'succeeded')

    def test_persistence_failure_at_each_critical_checkpoint_never_false_succeeds(self):
        baseline=[];session=self.make(checkpoint=lambda value:baseline.append(value))
        _,clock=self.drive(session);self.complete(session,clock)
        for fail_at in range(1,len(baseline)+1):
            calls=[]
            def checkpoint(value):
                calls.append(value)
                if len(calls)>=fail_at:raise OSError('injected persistence loss')
            session=self.make(checkpoint=checkpoint)
            with self.assertRaises(fw.FirmwareError):
                _,clock=self.drive(session);self.complete(session,clock)
            self.assertEqual(session.snapshot()['phase'],'failed_before_write' if fail_at<=2 else 'uncertain')
            self.assertTrue(session.snapshot()['journal_error'])

    def test_duplicates_and_out_of_phase_events_do_not_advance_or_extend_deadlines(self):
        session=self.make();_,clock=self.drive(session,until=7);session.sent(7,64,clock.step())
        session.receive(reply('left',3,b'\1',0xa0),clock.step())
        with self.assertRaises(fw.FirmwareError):session.receive(reply('left',3,b'\1',0xa0),clock.step())
        self.assertEqual(session.snapshot()['phase'],'uncertain')
        session=self.make();_,clock=self.drive(session,until=4);session.sent(4,64,clock.step())
        with self.assertRaises(fw.FirmwareError):session.receive(reply('left',4),clock.step())
        self.assertEqual(session.snapshot()['acknowledged_bytes'],0)
        session=self.make();_,clock=self.drive(session,until=1);session.sent(1,64,clock.step())
        with self.assertRaises(fw.FirmwareError):session.receive(reply('left',3,b'\1',0xa0),clock.step())
        self.assertEqual(session.snapshot()['phase'],'uncertain')


class OfficialImageSessionTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('COMPANION_OFFICIAL_FW_FIXTURES'), 'Original Lenovo BINs required')
    def test_authenticated_original_bytes_are_scheduled_unchanged_for_all_components(self):
        helper=TransferTests()
        root=Path(os.environ['COMPANION_OFFICIAL_FW_FIXTURES'])
        for component,name in (('left','left-v26.03.10.01.bin'),('right','right-v26.03.10.01.bin'),('receiver','rx-v26.04.22.01.bin')):
            original=(root/name).read_bytes()
            session=transfer.TransferSession(component,original,helper.policy(1024),lambda value:None)
            commands,clock=helper.drive(session,boundary=1024)
            sent=b''.join(c.report[10:10+c.report[9]] for c in commands if c.kind=='block')
            self.assertEqual(sent,original)
            helper.complete(session,clock)


if __name__ == '__main__': unittest.main()

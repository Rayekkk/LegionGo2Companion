# SPDX-License-Identifier: BSD-3-Clause
"""Qualification ordering/failure tests. Facts are fake, not USB proof."""
from dataclasses import replace
import copy
import unittest

from controller_firmware import FirmwareError
from controller_firmware_handoff import QualificationSession, QualificationObservation, STEPS, RECOVERY

BOOT='a106b34d-90a0-4db9-8c76-92b8b2c05cdb'


class HandoffTests(unittest.TestCase):
    def make(self, target=None, checkpoint=None):
        self.records=[]
        return QualificationSession('a'*64,BOOT,10.,20.,checkpoint or (lambda s:self.records.append(copy.deepcopy(s))),version_target=target)

    def action(self, session, now=10.1):return session.next_action(now,now+10,BOOT)

    def done(self, session, action, facts=None, now=10.1):
        if facts is None:
            step=next(s for s in STEPS if s[0]==action.kind)
            facts=set(step[2])
            if action.kind=='probe':facts.add('no_application_output' if action.target is None else 'single_version_reply')
        observation=QualificationObservation(session.snapshot()['job_id'],'a'*64,BOOT,action.sequence,frozenset(facts))
        session.complete(action,observation,now,now+10,BOOT)

    def reach(self, session, kind):
        while True:
            action=self.action(session)
            if action.kind==kind:return action
            self.done(session,action)

    def test_no_packet_profile_orders_restore_before_fence_release(self):
        session=self.make();seen=[]
        for _ in STEPS:
            action=self.action(session);seen.append(action.kind)
            self.assertEqual(action.interface,2)
            self.assertIsNone(action.target)
            self.assertEqual(self.records[-1]['pending'],action.kind)
            self.done(session,action)
        # Assert the safety dependencies independently of the production step
        # table. Deriving the expected order from STEPS hid unsafe reordering.
        self.assertEqual(len(seen),len(set(seen)), 'side effects cannot replay')
        self.assertEqual(seen[-1],'close', 'admission reopens only after cleanup')
        for earlier,later in (
                ('arm_rescue','pause_companion'),('pause_companion','publish_fence'),
                ('publish_fence','quiet_consumers'),('quiet_consumers','open_interface2'),
                ('open_interface2','detach_interface2'),('detach_interface2','claim_interface2'),
                ('claim_interface2','probe'),('probe','retire_probe'),
                ('retire_probe','restore_binding'),('restore_binding','restore_consumers'),
                ('restore_consumers','restore_companion'),('restore_companion','verify'),('verify','close')):
            self.assertLess(seen.index(earlier),seen.index(later),(earlier,later))
        self.assertIsNone(self.action(session))
        self.assertEqual(session.snapshot()['phase'],'closed')
        self.assertFalse(session.snapshot()['flash_available'])
        self.assertFalse(session.snapshot()['query_attempted'])

    def test_only_targets_one_through_four_are_immutable_and_once(self):
        for target in (1,2,3,4):
            session=self.make(target);action=self.reach(session,'probe')
            self.assertEqual(action.target,target)
            self.assertTrue(session.snapshot()['query_attempted'])
            with self.assertRaises(FirmwareError):self.action(session)
            session.fail('uncertain output')
            self.assertEqual(self.action(session).kind,'retire_probe')
            self.assertEqual(session.snapshot()['outcomes']['probe'],'uncertain')
        for target in (True,0,5,7,8,'left',b'\xc0'):
            with self.assertRaises(FirmwareError):self.make(target)

    def test_foreign_or_incomplete_observation_never_grants_usb_lease(self):
        for change in ('job','baseline','boot','sequence','missing','wrong_type'):
            session=self.make();action=self.reach(session,'claim_interface2')
            o=QualificationObservation(session.snapshot()['job_id'],'a'*64,BOOT,action.sequence,frozenset(STEPS[6][2]))
            if change=='job':o=replace(o,job_id='b'*32)
            if change=='baseline':o=replace(o,baseline_sha256='b'*64)
            if change=='boot':o=replace(o,boot_id='b'*36)
            if change=='sequence':o=replace(o,sequence=0)
            if change=='missing':o=replace(o,facts=o.facts-{'no_foreign_owner'})
            if change=='wrong_type':o=replace(o,facts=set(o.facts))
            with self.assertRaises(FirmwareError):session.complete(action,o,10.1,20.1,BOOT)
            self.assertFalse(session.lease_valid(10.1,20.1,BOOT))
            self.assertEqual(self.action(session).kind,'retire_probe')

    def test_equal_action_copy_or_completed_action_cannot_be_replayed(self):
        session=self.make();action=self.action(session)
        o=QualificationObservation(session.snapshot()['job_id'],'a'*64,BOOT,action.sequence,frozenset(STEPS[0][2]))
        with self.assertRaises(FirmwareError):session.complete(replace(action),o,10.1,20.1,BOOT)
        self.done(session,action)
        with self.assertRaises(FirmwareError):session.complete(action,o,10.1,20.1,BOOT)

    def test_failure_at_every_side_effect_has_only_recovery_next(self):
        for kind,_,_ in STEPS[:8]:
            session=self.make(3);action=self.reach(session,kind)
            session.fail('lost acknowledgement')
            self.assertEqual(session.snapshot()['uncertain_action'],kind)
            self.assertFalse(session.snapshot()['lease_valid'])
            cleanup=self.action(session)
            self.assertEqual(cleanup.kind,'retire_probe')
            for expected,_,_ in RECOVERY:
                if cleanup.kind!=expected:cleanup=self.action(session)
                self.assertEqual(cleanup.kind,expected);self.done(session,cleanup)
            self.assertEqual(session.snapshot()['phase'],'closed')
            self.assertFalse(session.snapshot()['flash_available'])

    def test_failed_cleanup_cannot_retry_detach_or_restore_indefinitely(self):
        session=self.make();self.reach(session,'detach_interface2');session.fail('detach result lost')
        self.done(session,self.action(session))
        action=self.action(session);self.assertEqual(action.kind,'restore_binding')
        session.fail('attach hung')
        self.assertEqual(session.snapshot()['phase'],'recovery_required')
        self.assertIsNone(self.action(session))
        self.assertNotEqual(session.snapshot()['outcomes']['fence'],'confirmed')

    def test_all_phases_use_original_absolute_deadline_without_heartbeat_extension(self):
        session=self.make();action=self.action(session)
        self.assertEqual(action.deadline,15)
        self.done(session,action,now=14.9)
        action=session.next_action(14.95,24.95,BOOT)
        self.assertEqual(action.deadline,30)
        with self.assertRaises(FirmwareError):session.complete(action,None,30,40,BOOT)
        cleanup=session.next_action(30.1,40.1,BOOT)
        self.assertEqual(cleanup.deadline,110)
        with self.assertRaises(FirmwareError):session.complete(cleanup,None,110,120,BOOT)
        self.assertEqual(session.snapshot()['phase'],'recovery_required')

    def test_suspend_wrong_boot_and_invalid_time_latch_recovery_without_new_action(self):
        for now,bt,boot in ((9,19,BOOT),(10.1,25,BOOT),(10.1,20.1,'b'*36),
                             (float('nan'),20,BOOT),(float('inf'),20,BOOT),(True,20,BOOT)):
            session=self.make()
            with self.assertRaises(FirmwareError):session.next_action(now,bt,boot)
            self.assertEqual(session.snapshot()['phase'],'recovery_required')
            self.assertFalse(session.snapshot()['lease_valid'])

    def test_expired_claim_lease_revokes_before_version_output(self):
        session=self.make(3);action=self.reach(session,'claim_interface2');self.done(session,action)
        self.assertTrue(session.lease_valid(34.9,44.9,BOOT))
        self.assertFalse(session.lease_valid(35,45,BOOT))
        self.assertFalse(session.snapshot()['query_attempted'])
        self.assertEqual(session.next_action(35.1,45.1,BOOT).kind,'retire_probe')

    def test_restore_binding_active_service_alone_is_not_consumer_confirmation(self):
        session=self.make();action=self.reach(session,'restore_consumers')
        with self.assertRaises(FirmwareError):self.done(session,action,{'service_active'})
        self.assertNotEqual(session.snapshot()['outcomes']['consumer_restore'],'confirmed')
        self.assertNotEqual(session.snapshot()['outcomes']['fence'],'confirmed')

    def test_failed_intent_checkpoint_never_returns_action_or_releases_fence(self):
        def failed(_):raise OSError('disk full')
        session=self.make(checkpoint=failed)
        with self.assertRaises(FirmwareError):self.action(session)
        self.assertTrue(session.snapshot()['journal_failed'])
        self.assertFalse(session.snapshot()['query_attempted'])
        self.assertNotEqual(session.snapshot()['outcomes']['fence'],'confirmed')

    def test_commit_failure_preserves_uncertainty_and_blocks_fence_removal(self):
        broken=[False]
        def checkpoint(s):
            if broken[0]:raise OSError('fsync failed')
        session=self.make(checkpoint=checkpoint)
        action=self.reach(session,'claim_interface2');broken[0]=True
        with self.assertRaises(FirmwareError):self.done(session,action)
        self.assertTrue(session.snapshot()['journal_failed'])
        self.assertEqual(session.snapshot()['outcomes']['evidence'],'uncertain')
        self.assertEqual(session.snapshot()['phase'],'restoring')
        broken[0]=False
        for kind,_,_ in RECOVERY[:-1]:
            action=self.action(session);self.assertEqual(action.kind,kind);self.done(session,action)
        with self.assertRaises(FirmwareError):self.action(session)
        self.assertEqual(session.snapshot()['phase'],'recovery_required')

    def test_bad_manifest_never_checkpoints(self):
        checkpoint=lambda _:self.fail('Invalid manifest reached journal')
        for digest,boot,start,bt in (('x'*64,BOOT,1,1),('a'*64,'oldboot',1,1),('a'*64,BOOT,-1,1),('a'*64,BOOT,1,float('nan'))):
            with self.assertRaises(FirmwareError):QualificationSession(digest,boot,start,bt,checkpoint)

    def test_late_claim_cannot_revive_after_terminal_clock_failure(self):
        session=self.make(3);action=self.reach(session,'claim_interface2')
        with self.assertRaises(FirmwareError):session.next_action(10.2,30.2,BOOT)
        with self.assertRaises(FirmwareError):self.done(session,action,now=10.3)
        self.assertEqual(session.snapshot()['uncertain_action'],'claim_interface2')
        self.assertFalse(session.lease_valid(10.4,20.4,BOOT))
        self.assertIsNone(session.next_action(10.5,20.5,BOOT))
        self.assertFalse(session.snapshot()['query_attempted'])

    def test_failed_final_commit_is_not_reported_as_closed(self):
        broken=[False]
        def checkpoint(_):
            if broken[0]:raise OSError('fsync failed')
        session=self.make(checkpoint=checkpoint);action=self.reach(session,'close');broken[0]=True
        with self.assertRaises(FirmwareError):self.done(session,action)
        self.assertEqual(session.snapshot()['phase'],'recovery_required')
        self.assertEqual(session.snapshot()['outcomes']['fence'],'uncertain')

    def test_normal_path_cleanup_failure_never_replays_retire_or_attach(self):
        for kind,_,_ in RECOVERY:
            session=self.make();action=self.reach(session,kind)
            session.fail('Cleanup result lost')
            self.assertEqual(session.snapshot()['phase'],'recovery_required')
            self.assertEqual(session.snapshot()['uncertain_action'],action.kind)
            self.assertIsNone(self.action(session))

    def test_cleanup_commit_failure_requires_independent_recovery(self):
        broken=[False]
        def checkpoint(_):
            if broken[0]:raise OSError('Lost cleanup commit')
        session=self.make(checkpoint=checkpoint);action=self.reach(session,'restore_binding');broken[0]=True
        with self.assertRaises(FirmwareError):self.done(session,action)
        self.assertEqual(session.snapshot()['phase'],'recovery_required')
        self.assertEqual(session.snapshot()['outcomes']['evidence'],'uncertain')
        self.assertIsNone(self.action(session))


if __name__=='__main__':unittest.main()

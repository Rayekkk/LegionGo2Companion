# SPDX-License-Identifier: BSD-3-Clause
from dataclasses import replace
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import controller_firmware as fw
import controller_firmware_transfer as transfer
import controller_firmware_worker as worker
from firmware_worker_fixture import Clock, Journal, Inhibitor, Transport


class WorkerTests(unittest.TestCase):
    def assert_resources_closed(self):
        self.assertCountEqual(self.events, ['transport', 'inhibitor', 'journal'])
        # Both guards stay held until the device handle has been retired.
        self.assertEqual(self.events[0], 'transport')

    def make(self, component='left', original=b'X'*65, accept=None, authenticated=False):
        self.clock=Clock(); self.events=[]
        self.transport=Transport(self.clock, self.events)
        self.inhibitor=Inhibitor(self.events); self.journal=Journal(self.events)
        policy=transfer.TransferPolicy(frozenset((64,)), .5, 5., 5., 60.)
        self.accept_calls=0
        def default_accept(): self.accept_calls+=1; return True
        instance=worker.FirmwareWorker(component, original, policy, self.transport,
            self.inhibitor, self.journal, accept or default_accept, self.clock)
        if not authenticated:
            # Synthetic scheduler model; production authentication is never mocked.
            self.patch=patch.object(transfer, 'validate_image',
                return_value=fw.ValidatedImage(fw.release_for(component), original))
            self.patch.start(); self.addCleanup(self.patch.stop)
        return instance

    def test_success_exact_stream_and_single_authorization_for_all_components(self):
        for component in fw.RELEASES:
            instance=self.make(component, bytes(range(65)))
            outcome=instance.run()
            self.assertEqual(outcome['snapshot']['phase'], 'succeeded')
            self.assertEqual(b''.join(c.report[10:10+c.report[9]] for c in self.transport.commands
                if c.kind=='block'), bytes(range(65)))
            self.assertEqual(self.accept_calls, 1)
            self.assert_resources_closed()
            self.assertTrue(outcome['cleanup_complete'])
            self.patch.stop()

    def test_ui_loss_after_one_shot_handoff_does_not_cancel(self):
        parent={'alive':True}
        instance=self.make(accept=lambda:parent['alive'])
        self.transport.first_write=lambda:parent.update(alive=False)
        self.assertEqual(instance.run()['snapshot']['phase'], 'succeeded')

    def test_preflight_must_remain_fresh_after_authorization(self):
        def delayed_accept(): self.clock.advance(3.); return True
        instance=self.make(accept=delayed_accept)
        result=instance.run()
        self.assertEqual(result['snapshot']['phase'],'failed_before_write')
        self.assertFalse(result['snapshot']['write_attempted'])
        self.assertEqual(self.transport.commands,[])

    def test_preflight_must_remain_fresh_after_durable_intent(self):
        instance=self.make()
        instance._policy=replace(instance._policy,write_timeout=5.)
        save=self.journal.save
        def slow(value):
            save(value)
            if value['write_attempted']: self.clock.advance(3.)
        self.journal.save=slow
        result=instance.run()
        self.assertEqual(result['snapshot']['phase'],'uncertain')
        self.assertEqual(self.transport.commands,[])

    def test_authorization_refusal_and_non_boolean_never_output(self):
        for value in (False, None, 1):
            instance=self.make(accept=lambda:value)
            outcome=instance.run()
            self.assertEqual(outcome['snapshot']['phase'], 'failed_before_write')
            self.assertEqual(self.transport.commands, [])
            self.patch.stop()

    def test_authentication_failure_never_acquires_device_or_inhibitor(self):
        instance=self.make(original=b'bad', authenticated=True)
        result=instance.run()
        self.assertIsNone(result['snapshot'])
        self.assertFalse(self.inhibitor.acquired)
        self.assertIsNone(self.transport.claim)

    def test_no_replay_after_success_or_failure(self):
        instance=self.make(); instance.run()
        with self.assertRaises(fw.FirmwareError): instance.run()
        instance=self.make(accept=lambda:False); instance.run()
        with self.assertRaises(fw.FirmwareError): instance.run()

    def test_missing_ack_is_bounded_uncertain_not_success(self):
        instance=self.make(); self.transport.drop=True
        result=instance.run()
        self.assertEqual(result['snapshot']['phase'], 'uncertain')
        self.assertLessEqual(self.clock(), .6)
        self.assertEqual(len(self.transport.commands), 1)

    def test_early_completion_retained_but_finish_ack_still_required(self):
        instance=self.make(); self.transport.early=True
        self.assertEqual(instance.run()['snapshot']['phase'], 'succeeded')

    def test_non_waiting_empty_transport_fails_instead_of_busy_looping(self):
        for method in ('read','observe'):
            for elapsed in (0.,.00001):
                instance=self.make(); calls=[]
                def early_empty(*args):
                    calls.append(1); self.clock.advance(elapsed)
                    if len(calls)>3: raise AssertionError('busy loop')
                    return None
                setattr(self.transport,method,early_empty)
                self.assertEqual(instance.run()['snapshot']['phase'],'uncertain')
                self.assertEqual(len(calls),1)
                self.patch.stop()

    def test_missing_post_observation_has_a_finite_deadline(self):
        instance=self.make()
        def wait(token,deadline): self.clock.value=deadline; return None
        self.transport.observe=wait
        self.assertEqual(instance.run()['snapshot']['phase'],'uncertain')
        self.assertLess(self.clock(),6.)

    def test_checkpoint_expiry_is_rechecked_before_transport_output(self):
        instance=self.make(); save=self.journal.save
        def slow(value):
            save(value)
            if value['write_attempted']: self.clock.advance(1.)
        self.journal.save=slow
        result=instance.run()
        self.assertEqual(result['snapshot']['phase'],'uncertain')
        self.assertEqual(self.transport.commands,[])

    def test_guard_loss_before_and_after_output(self):
        for owner in ('transport', 'inhibitor'):
            instance=self.make(); getattr(self, owner).alive=False
            self.assertEqual(instance.run()['snapshot']['phase'], 'failed_before_write')
            self.patch.stop()
            instance=self.make()
            self.transport.first_write=lambda:getattr(self, owner).__setattr__('alive', False)
            self.assertEqual(instance.run()['snapshot']['phase'], 'uncertain')
            self.assertEqual(len(self.transport.commands), 1)
            self.patch.stop()

    def test_operation_failures_do_not_leak_exception_text(self):
        for resource, method in (('inhibitor','acquire'), ('transport','acquire'),
                ('transport','write'), ('transport','read'), ('transport','observe')):
            instance=self.make()
            def fail(*args): raise RuntimeError('super secret details')
            setattr(getattr(self,resource),method,fail)
            result=instance.run()
            self.assertNotIn('secret', str(result))
            self.assertNotEqual(result['snapshot']['phase'], 'succeeded')
            self.assert_resources_closed()
            self.patch.stop()

    def test_partial_write_and_expired_write_are_uncertain(self):
        for count, delay in ((63,.001),(64,1.)):
            instance=self.make()
            def write(command, deadline): self.clock.advance(delay); return count
            self.transport.write=write
            self.assertEqual(instance.run()['snapshot']['phase'], 'uncertain')
            self.patch.stop()

    def test_acquisition_expiry_and_invalid_claim_prevent_output(self):
        for delay, invalid in ((6.,False),(0.,True)):
            instance=self.make(); acquire=self.transport.acquire
            def slow(component, deadline):
                claim=acquire(component, deadline); self.clock.advance(delay)
                return None if invalid else claim
            self.transport.acquire=slow
            self.assertEqual(instance.run()['snapshot']['phase'], 'failed_before_write')
            self.assertEqual(self.transport.commands, [])
            self.patch.stop()

    def test_invalid_and_reversed_clocks_fail_closed(self):
        for value in (float('nan'),float('inf'),10**400,True,-1.):
            instance=self.make(); instance._clock=lambda:value
            self.assertNotEqual(instance.run()['snapshot']['phase'], 'succeeded')
            self.patch.stop()
        instance=self.make(); self.transport.first_write=lambda:setattr(self.clock,'value',-1.)
        self.assertEqual(instance.run()['snapshot']['phase'],'uncertain')

    def test_wrong_or_stale_post_version_is_uncertain(self):
        for change in ({'device_binding':'b'*64},{'sampled_at':-1.}, {'verification_token':'old'}):
            instance=self.make(); observe=self.transport.observe
            self.transport.observe=lambda token,deadline:replace(observe(token,deadline), **change)
            self.assertEqual(instance.run()['snapshot']['phase'],'uncertain')
            self.patch.stop()

    def test_critical_journal_failure_never_false_succeeds(self):
        instance=self.make(); instance.run(); checkpoints=len(self.journal.records)
        self.patch.stop()
        for fail_at in range(1, checkpoints+1):
            instance=self.make(); self.journal.fail_at=fail_at
            result=instance.run()
            self.assertNotEqual(result['snapshot']['phase'],'succeeded')
            self.assertTrue(result['snapshot']['journal_error'])
            self.patch.stop()

    def test_close_failure_explicit_and_other_handles_still_closed(self):
        for name in ('transport','inhibitor','journal'):
            instance=self.make()
            def close(): self.events.append(name); raise OSError('secret')
            getattr(self,name).close=close
            outcome=instance.run()
            self.assertEqual(outcome['snapshot']['phase'],'succeeded')
            self.assertFalse(outcome['cleanup_complete'])
            self.assertEqual(outcome['cleanup_errors'], [name])
            self.assert_resources_closed()
            self.patch.stop()

    def test_disabled_entrypoint_never_creates_worker(self):
        with (patch.object(worker,'FirmwareWorker',side_effect=AssertionError('device path')),
                patch('controller_firmware_install.OTA_ENABLED',False),
                patch('builtins.print')):
            self.assertEqual(worker.main(),2)


class OfficialWorkerTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('COMPANION_OFFICIAL_FW_FIXTURES'), 'Original Lenovo BINs required')
    def test_actual_authenticated_images_with_simulated_transport(self):
        helper=WorkerTests(); root=Path(os.environ['COMPANION_OFFICIAL_FW_FIXTURES'])
        for component,name in (('left','left-v26.03.10.01.bin'),('right','right-v26.03.10.01.bin'),
                ('receiver','rx-v26.04.22.01.bin')):
            instance=helper.make(component,(root/name).read_bytes(),authenticated=True)
            self.assertEqual(instance.run()['snapshot']['phase'],'succeeded')


if __name__ == '__main__': unittest.main()

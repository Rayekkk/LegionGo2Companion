# SPDX-License-Identifier: BSD-3-Clause
"""Complete batch scheduler with simulated USB, never live OTA evidence."""
import unittest
from unittest.mock import patch

import controller_firmware as fw
import controller_firmware_worker as worker
import controller_firmware_transfer as transfer
from firmware_worker_fixture import Clock, Journal, Inhibitor, Transport


class PlanTests(unittest.TestCase):
    def run_plan(self, *, failed='', restored=True, broken_progress=False):
        clock=Clock();events=[];transports=[];admissions=[]
        inhibitor=Inhibitor(events)
        images=tuple(fw.ValidatedImage(r,b'X'*65) for r in fw.RELEASES.values())
        def transport(component):
            value=Transport(clock,events)
            if component==failed:value.drop=True
            transports.append((component,value));return value
        def restore():
            self.assertTrue(inhibitor.valid(),'plan inhibitor held through restoration')
            events.append('restore');return restored
        def authorize():admissions.append(True);return True
        def progress(phase,message):
            if broken_progress and phase=='restoring':raise OSError('status write failed')
        validate=lambda c,d,release=None:fw.ValidatedImage(release or fw.release_for(c),d)
        with patch.object(worker,'validate_image',side_effect=validate),patch.object(transfer,'validate_image',side_effect=validate):
            result=worker.run_update_plan(images,{c:'250101A' for c in fw.RELEASES},
                transfer.TransferPolicy(None,.5,5.,5.,60.),transport,lambda c:Journal(events),
                inhibitor,authorize,restore,progress,clock=clock)
        return result,transports,events,admissions

    def test_whole_plan_one_authorization_and_one_inhibitor_until_restore(self):
        result,transports,events,admissions=self.run_plan()
        self.assertEqual(result['phase'],'succeeded')
        self.assertEqual(result['completed'],['left','right','receiver'])
        self.assertEqual([c for c,t in transports],result['completed'])
        self.assertEqual(admissions,[True])
        self.assertEqual(events.count('inhibitor'),1)
        self.assertEqual(events[-2:],['restore','inhibitor'])
        for component,transport in transports:
            self.assertTrue(transport.commands)
            self.assertEqual(transport.commands[0].report[4],{'left':7,'right':8,'receiver':1}[component])

    def test_uncertain_second_component_stops_plan_and_preserves_completed_first(self):
        result,transports,events,admissions=self.run_plan(failed='right')
        self.assertEqual(result['phase'],'uncertain')
        self.assertEqual(result['completed'],['left'])
        self.assertEqual([c for c,t in transports],['left','right'])
        self.assertTrue(result['restored']);self.assertEqual(events.count('restore'),1)
        self.assertEqual(result['versions']['left'],fw.release_for('left').version)
        self.assertEqual(result['versions']['right'],'250101A')

    def test_restoration_failure_cannot_report_success(self):
        result,*_=self.run_plan(restored=False)
        self.assertEqual(result['phase'],'uncertain');self.assertFalse(result['cleanup_complete'])

    def test_failed_progress_write_does_not_skip_actual_input_restoration(self):
        result,_,events,_=self.run_plan(broken_progress=True)
        self.assertEqual(result['phase'],'uncertain')
        self.assertTrue(result['restored'])
        self.assertEqual(events[-2:],['restore','inhibitor'])

    def test_bad_image_rejects_whole_plan_before_any_acquisition(self):
        calls=[]
        with self.assertRaises(fw.FirmwareError):
            worker.run_update_plan((fw.ValidatedImage(fw.release_for('left'),b'invalid'),),
                {c:'250101A' for c in fw.RELEASES},None,lambda c:calls.append(c),None,None,None,None,None)
        self.assertEqual(calls,[])

    def test_duplicate_or_out_of_order_plan_is_not_admitted(self):
        left=fw.ValidatedImage(fw.release_for('left'),b'X'*65)
        right=fw.ValidatedImage(fw.release_for('right'),b'X'*65)
        acquired=[]
        def acquire(component):
            acquired.append(component)
            raise fw.FirmwareError('Test acquisition boundary')
        def run(images):
            return worker.run_update_plan(images,{c:'250101A' for c in fw.RELEASES},
                transfer.TransferPolicy(None,.5,5.,5.,60.),acquire,lambda c:None,
                Inhibitor([]),lambda:True,lambda:True,lambda *a:None)
        # Keep image authentication valid for this structure-only model. An
        # invalid image would also raise FirmwareError and hide a missing guard.
        validate=lambda c,d,release=None:fw.ValidatedImage(release or fw.release_for(c),d)
        with patch.object(worker,'validate_image',side_effect=validate):
            self.assertEqual(run((left,))['phase'],'failed_before_write')
            self.assertEqual(acquired,['left'],'valid control reaches acquisition')
            for images in ((left,left),(right,left),()):
                with self.subTest(components=[i.release.component for i in images]):
                    acquired.clear()
                    with self.assertRaises(fw.FirmwareError):run(images)
                    self.assertEqual(acquired,[])


if __name__=='__main__':unittest.main()

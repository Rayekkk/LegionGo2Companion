# SPDX-License-Identifier: BSD-3-Clause
"""Live-release path, authenticated old fixtures and simulated future OTA.

No synthetic test image is sent to hardware. An observed vendor-signed image is
not evidence that a future release or physical flash has been tested.
"""
from dataclasses import replace
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
import threading
import unittest
from unittest.mock import patch

import controller_firmware as fw
import controller_firmware_catalog as catalog
import controller_firmware_install as install
import controller_firmware_stage as stage
import controller_firmware_journal as journal
import controller_firmware_transfer as transfer
import controller_firmware_worker as worker
from test_controller_firmware_catalog import result
import test_controller_firmware_updates as update_tests
from test_controller_firmware_updates import observation
from test_controller_firmware_journal import state, BOOT
from firmware_worker_fixture import Clock, Transport, Inhibitor, Journal, reply
import struct

REPO = Path(__file__).resolve().parents[1]


def future_feed():
    feed = result()
    for c in fw.RELEASES:
        feed['candidates'][c] = {'version':'270101A', 'feed_version':'v27.01.01.01',
            'url':'https://s3.service.lenovo.com/legion/cms/package/future-'+c+'.bin'}
    return feed


class FutureWorkflowTests(unittest.TestCase):
    def test_isolated_download_entrypoint_needs_no_neighbouring_module(self):
        r=fw.candidate_release('left',future_feed()['candidates']['left'])
        class Response(io.BytesIO):
            status=200
            headers={}
            def geturl(self):return r.url
        class Output:
            buffer=io.BytesIO()
        output=Output()
        with patch.dict(sys.modules,{'controller_firmware_catalog':None}),patch.object(sys,'argv',
                [str(REPO/'controller_firmware.py'),'--download-data','left',r.version,r.url,'0']),\
                patch.object(sys,'stdout',output),patch.object(fw.urllib.request,'build_opener') as opener:
            opener.return_value.open.return_value=Response(b'X'*700)
            runpy.run_path(str(REPO/'controller_firmware.py'),run_name='__main__')
        self.assertEqual(output.buffer.getvalue(),b'X'*700)
        for url in (r.url,None,'https://foreign.invalid/x.bin',r.url+'?x=1',r.url+'/../x.bin'):
            self.assertEqual(fw.valid_image_url(url),catalog.valid_image_url(url))

    def test_new_release_names_versions_sizes_and_hashes_are_not_a_whitelist(self):
        feed = future_feed();events = []
        def validate(c, data, release=None):
            self.assertEqual(release.version, '270101A')
            self.assertEqual(release.size, 0)
            events.append(('validate', c))
            return fw.ValidatedImage(replace(release,size=len(data),sha256=hashlib.sha256(data).hexdigest()),data)
        def installer(images, before, progress):
            self.assertEqual(events, [('validate',c) for c in fw.RELEASES])
            self.assertEqual([i.release.version for i in images], ['270101A']*3)
            return {'phase':'succeeded','restored':True,'cleanup_complete':True,
                'cleanup_errors':[],'error':'','completed':list(fw.RELEASES),
                'versions':{c:'270101A' for c in fw.RELEASES}}
        with patch.object(fw,'download_image',return_value=b'future test image'),patch.object(fw,'validate_image',side_effect=validate):
            helper = update_tests.UpdateTests()
            try:
                service = helper.run_service(feed=feed,installer=installer)
                self.assertEqual(service.status()['job']['phase'],'succeeded')
                self.assertTrue(service.status()['catalog_live'])
                self.assertEqual(service.status()['installed_versions'],{c:'270101A' for c in fw.RELEASES})
            finally:helper.doCleanups()

    def test_future_plan_rejects_old_postflash_versions_and_invalid_second_image(self):
        good = {'phase':'succeeded','restored':True,'cleanup_complete':True,
            'cleanup_errors':[],'error':'','completed':list(fw.RELEASES),'versions':observation()['versions']}
        helper = update_tests.UpdateTests()
        try:
            with patch.object(fw,'download_image',return_value=b'fixture'),patch.object(fw,'validate_image',
                    side_effect=lambda c,d,release=None:fw.ValidatedImage(release,d)):
                service=helper.run_service(feed=future_feed(),installer=lambda *a:good)
            self.assertEqual(service.status()['job']['phase'],'uncertain')
            with self.assertRaises(fw.FirmwareError):service.check_updates()
            calls=[]
            def validate(c,d,release=None):
                if c=='right':raise fw.FirmwareError('Invalid vendor signature')
                return fw.ValidatedImage(release,d)
            with patch.object(fw,'download_image',return_value=b'fixture'),patch.object(fw,'validate_image',side_effect=validate):
                service=helper.run_service(feed=future_feed(),installer=lambda *a:calls.append(a))
            self.assertEqual(service.status()['job']['phase'],'failed');self.assertEqual(calls,[])
        finally:helper.doCleanups()

    def test_dynamic_private_plan_and_journal_carry_actual_identity(self):
        r=replace(fw.candidate_release('left',future_feed()['candidates']['left']),size=600000,sha256='a'*64)
        record=fw.release_record(r)
        plan={'components':['left'],'versions':observation()['versions'],'offers':{'left':record}}
        checked=install.validate_plan(json.dumps(plan).encode(),['left'])
        self.assertEqual(fw.release_from_record('left',checked['offers']['left']),r)
        value=state('succeeded')
        value.update(image_sha256=r.sha256,offered_version=r.version,total_bytes=r.size,
            sent_bytes=r.size,acknowledged_bytes=r.size)
        self.assertEqual(journal.recovered_record(journal.encode_record(value,BOOT))['snapshot'],value)
        for change in ({'url':'http://localhost/x.bin'},{'size':True},{'size':fw.MAX_IMAGE_BYTES+1},
                {'sha256':'bad'},{'version':'270230A'},{'path':'elsewhere'}):
            tampered=copy.deepcopy(plan);tampered['offers']['left'].update(change)
            with self.assertRaises(fw.FirmwareError):install.validate_plan(json.dumps(tampered).encode(),['left'])

    def test_dynamic_worker_and_transfer_use_frozen_future_version_not_reference(self):
        clock=Clock();events=[]
        r=replace(fw.candidate_release('left',future_feed()['candidates']['left']),size=65,sha256='a'*64)
        image=fw.ValidatedImage(r,b'X'*65)
        class FutureTransport(Transport):
            def observe(self,token,deadline):
                self.clock.advance()
                return transfer.InstalledObservation(self.claim.device_binding,'Legion Go 8ASP2',2,
                    'after',self.clock(),token,reply(self.component,4,struct.pack('>I',int(r.version,16)),2))
        def validate(c,d,release=None):return fw.ValidatedImage(release,d)
        with patch.object(worker,'validate_image',side_effect=validate),patch.object(transfer,'validate_image',side_effect=validate):
            result_=worker.run_update_plan((image,),observation()['versions'],
                transfer.TransferPolicy(None,.5,5.,5.,60.),lambda c:FutureTransport(clock,events),
                lambda c:Journal(events),Inhibitor(events),lambda:True,lambda:True,lambda *a:None,clock)
        self.assertEqual(result_['phase'],'succeeded')
        self.assertEqual(result_['versions']['left'],'270101A')
        self.assertEqual(result_['completed'],['left'])

    def test_dynamic_download_is_official_host_bounded_and_exact_when_frozen(self):
        r=fw.candidate_release('left',future_feed()['candidates']['left'])
        class Response(io.BytesIO):
            status=200
            headers={}
            def geturl(self):return r.url
        def download(response):
            with patch.object(fw.urllib.request,'build_opener') as opener:
                opener.return_value.open.return_value=response
                return fw._download_http(r,threading.Event())
        self.assertEqual(len(download(Response(b'x'*700))),700)
        for length in ('9999999','-1','0','1','700\n','garbage'):
            response=Response(b'x'*700);response.headers={'Content-Length':length}
            with self.assertRaises(fw.FirmwareError):download(response)
        with self.assertRaises(fw.FirmwareError):download(Response(b'x'*(fw.MAX_IMAGE_BYTES+1)))
        with patch.object(fw.subprocess,'Popen') as spawn:
            with self.assertRaises(fw.FirmwareError):fw.download_image(replace(r,filename='../evil.bin'),threading.Event())
            spawn.assert_not_called()


@unittest.skipUnless(os.environ.get('COMPANION_OFFICIAL_FW_FIXTURES'),'Original signed Lenovo images required')
class AuthenticatedDynamicTests(unittest.TestCase):
    def test_signed_bytes_accept_new_official_filename_without_reference_url_pin(self):
        path=Path(os.environ['COMPANION_OFFICIAL_FW_FIXTURES'])/'left-v26.03.10.01.bin'
        data=path.read_bytes();candidate=future_feed()['candidates']['left'].copy()
        candidate.update(version='260310A',feed_version='v26.03.10.01')
        image=fw.validate_image('left',data,release=fw.candidate_release('left',candidate))
        self.assertEqual(image.original,data)
        self.assertEqual(image.release.size,len(data))
        self.assertEqual(image.release.sha256,hashlib.sha256(data).hexdigest())
        self.assertNotEqual(image.release.url,fw.release_for('left').url)
        before=observation(True)
        frozen=install.freeze_plan((image,),before,REPO)
        manifest=stage.validate_bundle(frozen)
        plan=install.validate_plan(frozen.files['plan.json'],manifest['components'])
        r=fw.release_from_record('left',plan['offers']['left'])
        self.assertEqual(fw.validate_image('left',frozen.files['left.bin'],release=r),image)
        for change in (data[:-1],data+b'x',bytes([data[0]^1])+data[1:]):
            with self.assertRaises(fw.FirmwareError):fw.validate_image('left',change,release=r)
        with self.assertRaises(fw.FirmwareError):fw.validate_image('right',data,release=fw.candidate_release('right',candidate))


if __name__=='__main__':unittest.main()

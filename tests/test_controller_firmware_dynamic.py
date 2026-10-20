# SPDX-License-Identifier: BSD-3-Clause
"""Live-release path and authenticated old fixtures for future releases.

No synthetic test image is sent to hardware. An observed vendor-signed image is
not evidence that a future release or physical flash has been tested.
"""
from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
import runpy
import sys
import threading
from types import MappingProxyType
import unittest
from unittest.mock import patch

import controller_firmware as fw
import controller_firmware_catalog as catalog
from test_controller_firmware_catalog import result

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

    def test_known_image_under_another_catalog_label_is_refused(self):
        known={c:b'\0'*1000+r.footer+bytes([i])*fw.SIGNATURE_BYTES for i,(c,r) in enumerate(fw.RELEASES.items())}
        pinned=MappingProxyType({c:replace(r,sha256=hashlib.sha256(known[c]).hexdigest()) for c,r in fw.RELEASES.items()})
        fresh={c:data[:-1]+b'\x7f' for c,data in known.items()}  # bytes no pinned release has
        for images,phase in ((known,'failed'),(fresh,'downloaded')):
            job=fw.FirmwarePreparation(reader=lambda:dict.fromkeys(fw.RELEASES,'261001A'),model=lambda:'Legion Go 8ASP2')
            with self.subTest(phase=phase),patch.object(fw,'RELEASES',pinned),\
                    patch.object(fw,'_verify_signature'),patch.object(fw,'_check_structure'),\
                    patch.object(catalog,'check_catalog',return_value=future_feed()),\
                    patch.object(fw,'download_image',side_effect=lambda r,_:images[r.component]):
                job._run_check()
                self.assertEqual(job.status()['phase'],'available')
                job._run_download(job._check)
                status=job.status()
                self.assertEqual(status['phase'],phase,status.get('error'))
                if phase=='failed':
                    self.assertIn('different version',status['error'])
                    with self.assertRaises(fw.FirmwareError):job.admit_install(status.get('token',''))


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
        r=image.release
        self.assertEqual(fw.validate_image('left',data,release=r),image)
        for change in (data[:-1],data+b'x',bytes([data[0]^1])+data[1:]):
            with self.assertRaises(fw.FirmwareError):fw.validate_image('left',change,release=r)
        with self.assertRaises(fw.FirmwareError):fw.validate_image('right',data,release=fw.candidate_release('right',candidate))
        with self.assertRaises(fw.FirmwareError):  # pinned 260310A bytes behind a 270101A label
            fw.validate_image('left',data,release=fw.candidate_release('left',future_feed()['candidates']['left']))


if __name__=='__main__':unittest.main()

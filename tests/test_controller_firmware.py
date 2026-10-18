# SPDX-License-Identifier: BSD-3-Clause
import io
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch, Mock
import support
import controller_firmware as fw
import subprocess
import urllib.request
import controller_firmware
import controller_firmware_catalog


class PolicyTests(unittest.TestCase):
    def test_component_and_version_binding(self):
        for invalid in (None, {}, 1, '../left', 'Left', 'all'):
            with self.assertRaises(fw.FirmwareError):
                fw.release_for(invalid)
        self.assertTrue(fw.update_needed('251204A', '260310A'))
        self.assertFalse(fw.update_needed('260310a', '260310A'))
        self.assertFalse(fw.update_needed('260422A', '251208A'))
        for invalid in ('', '0', '0000000', '26.03.10.01', '260310G', True,
                '261332A', '260000A', '2000000', '260229A', '26A310A'):
            with self.assertRaises(fw.FirmwareError):
                fw.version_number(invalid)
        self.assertTrue(fw.update_needed('240228F', '2402290'))
        self.assertTrue(fw.update_needed('260310A', '260310B'))

    def test_invalid_images_cannot_reach_signature_or_structural_parsers(self):
        with patch.object(fw, '_verify_signature') as verify:
            for component in fw.RELEASES:
                release = fw.release_for(component)
                for data in (b'', bytearray(release.size), b'x' * (release.size-1), b'x' * release.size):
                    with self.assertRaises(fw.FirmwareError):
                        fw.validate_image(component, data)
            verify.assert_not_called()

    def test_redirect_is_never_followed(self):
        with self.assertRaises(fw.FirmwareError):
            fw._NoRedirect().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/')

    def test_download_rejects_bad_http_metadata_oversize_and_deadline(self):
        release = fw.release_for('left')
        class Response(io.BytesIO):
            status = 200
            headers = {}
            def geturl(self): return release.url
        def download(response, clock=None):
            with patch.object(fw.urllib.request, 'build_opener') as builder:
                builder.return_value.open.return_value = response
                if clock is None:
                    return fw._download_http(release, threading.Event())
                with patch.object(fw.time, 'monotonic', side_effect=clock):
                    return fw._download_http(release, threading.Event())
        for headers in ({'Content-Length': '1'}, {'Content-Encoding': 'gzip'}):
            response = Response(b'x'); response.headers = headers
            with self.assertRaises(fw.FirmwareError): download(response)
        response = Response(b'x'); response.status = 404
        with self.assertRaises(fw.FirmwareError): download(response)
        response = Response(b'x'); response.geturl = lambda: 'https://other.invalid/'
        with self.assertRaises(fw.FirmwareError): download(response)
        with self.assertRaises(fw.FirmwareError): download(Response(b'x'*(release.size+1)))
        with self.assertRaises(fw.FirmwareError): download(Response(b'x'), [0., 0., 36.])
        self.assertEqual(download(Response(b'original')), b'original')

    def test_download_parent_deadline_and_cancel_kill_only_own_helper(self):
        release = fw.release_for('left')
        worker = Mock(returncode=0)
        worker.poll.return_value = 0
        worker.communicate.return_value = (bytes(release.size), None)
        with patch.object(fw.subprocess, 'Popen', return_value=worker) as spawn:
            self.assertEqual(len(fw.download_image(release, threading.Event())), release.size)
            args, kwargs = spawn.call_args
            self.assertEqual(args[0][-5:], ['--download-data', 'left', release.version, release.url, str(release.size)])
            self.assertIn('-I', args[0])
            self.assertNotIn('shell', kwargs)
            worker.kill.assert_not_called()
        worker = Mock(returncode=None)
        worker.poll.return_value = None
        worker.communicate.side_effect = [subprocess.TimeoutExpired('helper', .2), (b'', None)]
        with patch.object(fw.subprocess, 'Popen', return_value=worker), patch.object(fw.time, 'monotonic', side_effect=[0., 0., 36.]):
            with self.assertRaises(fw.FirmwareError):
                fw.download_image(release, threading.Event())
        worker.kill.assert_called_once()
        cancelled = threading.Event(); cancelled.set()
        with patch.object(fw.subprocess, 'Popen') as spawn:
            with self.assertRaises(fw.FirmwareError): fw.download_image(release, cancelled)
            spawn.assert_not_called()

    def test_cancel_reaps_real_stalled_download_process(self):
        cancelled = threading.Event()
        # Only the helper's contents are replaced; the parent's pipe timeout, kill and reaping stay real.
        with support.stalled_child() as child:
            timer = threading.Timer(.3, cancelled.set)
            timer.start()
            try:
                with patch.object(fw.subprocess, 'Popen', return_value=child):
                    with self.assertRaises(fw.FirmwareError):
                        fw.download_image(fw.release_for('left'), cancelled)
                self.assertIsNotNone(child.poll())
            finally:
                timer.cancel()
                timer.join(2)

@unittest.skipUnless(os.environ.get('COMPANION_OFFICIAL_FW_FIXTURES'), 'Original Lenovo images required for offline crypto tests')
class OfficialImageTests(unittest.TestCase):
    def originals(self):
        root = Path(os.environ['COMPANION_OFFICIAL_FW_FIXTURES'])
        return {component: (root / name).read_bytes() for component, name in (
            ('left', 'left-v26.03.10.01.bin'), ('right', 'right-v26.03.10.01.bin'), ('receiver', 'rx-v26.04.22.01.bin'))}

    def test_three_original_signed_images(self):
        for component, data in self.originals().items():
            with self.subTest(component=component):
                validated = fw.validate_image(component, data)
                self.assertEqual(validated.original, data)
                self.assertEqual(validated.release.component, component)

    def test_corruption_and_cross_component_are_rejected(self):
        for component, data in self.originals().items():
            for position in (0, 32, 192, 4096, len(data)-389, len(data)-385, len(data)-384, len(data)-1):
                modified = bytearray(data); modified[position] ^= 1
                with self.subTest(component=component, position=position), self.assertRaises(fw.FirmwareError):
                    fw.validate_image(component, bytes(modified))
                # Exercise real OpenSSL too, bypassing only the earlier hash gate.
                with self.assertRaises(fw.FirmwareError):
                    fw._verify_signature(bytes(modified))
            for other in fw.RELEASES:
                if other != component:
                    with self.assertRaises(fw.FirmwareError):
                        fw.validate_image(other, data)
            for modified in (data[:-1], data+b'x'):
                with self.assertRaises(fw.FirmwareError):
                    fw.validate_image(component, modified)


class RedirectResourceTests(unittest.TestCase):
    def test_rejected_redirect_closes_the_original_response(self):
        url = 'https://github.com/test/asset'
        handlers = (
            (controller_firmware._NoRedirect(), controller_firmware.FirmwareError),
            (controller_firmware_catalog._NoRedirect(), controller_firmware_catalog.CatalogError),
        )
        for handler, exception in handlers:
            with self.subTest(handler=type(handler).__module__):
                response = io.BytesIO(b'redirect body')
                with self.assertRaises(exception):
                    handler.redirect_request(urllib.request.Request(url), response, 302,
                                             'Found', {}, 'https://foreign.example/asset')
                self.assertTrue(response.closed)


if __name__ == '__main__':
    unittest.main()

# SPDX-License-Identifier: BSD-3-Clause
import copy
import io
import json
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import controller_firmware as fw
import controller_firmware_catalog as catalog


def bootstrap():
    return {'code': 200, 'data': {'gameDomain': catalog.BASE, 'areaCode': 'PL',
        'extend': {'MachineAdaption': [{'MachineName': catalog.MODEL, 'MachineType': 'Legion Go 2'}]}}}


def entry(component='left'):
    _, terminal, package = next(row for row in catalog.TARGETS if row[0] == component)
    version = 'v26.04.22.01' if component == 'receiver' else 'v26.03.10.01'
    return {'code': 200, 'data': [{'terminalId': terminal, 'packageEname': package,
        'updateType': 2, 'versionCode': version, 'versionName': version,
        'packageUrl': fw.release_for(component).url}]}


def result():
    candidates = {component: catalog._candidate(entry(component), terminal, package)
        for component, terminal, package in catalog.TARGETS}
    return {'checked_at': '2026-10-13T12:00:00+00:00', 'model': catalog.MODEL,
        'region': 'PL', 'candidates': candidates}


class CatalogTests(unittest.TestCase):
    def test_official_version_domain_and_calendar(self):
        self.assertEqual(catalog.raw_version('v26.03.10.01'), '260310A')
        self.assertEqual(catalog.raw_version('v24.02.29.00'), '2402290')
        self.assertEqual(catalog.raw_version('v26.04.22.06'), '260422F')
        for value in (None, True, '260310A', 'v26.02.29.01', 'v26.03.10.07',
                'v26.03.10.1', 'v00.00.00.00', 'v２６.03.10.01', 'v26.03.10.01\n'):
            with self.subTest(value=value), self.assertRaises(catalog.CatalogError):
                catalog.raw_version(value)

    def test_component_identity_version_and_url_are_all_required(self):
        _, terminal, package = catalog.TARGETS[0]
        original = entry()
        mutations = [('terminalId', True), ('terminalId', 63), ('packageEname', 'other'),
            ('updateType', True), ('updateType', 1), ('versionCode', 'invalid'),
            ('packageUrl', 'https://s3.service.lenovo.com.evil/legion/cms/package/test.bin'),
            ('packageUrl', 'https://s3.service.lenovo.com/legion/cms/package/../test.bin'),
            ('packageUrl', fw.release_for('left').url + '?query=1')]
        for key, value in mutations:
            body = copy.deepcopy(original); body['data'][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(catalog.CatalogError):
                catalog._candidate(body, terminal, package)
        for body in ([], {'code': True, 'data': None}, {'code': 200, 'data': []},
                {'code': 200, 'data': original['data'] * 2}, {'code': 201, 'data': []}):
            with self.assertRaises(catalog.CatalogError): catalog._candidate(body, terminal, package)
        self.assertIsNone(catalog._candidate({'code': 201, 'data': None}, terminal, package))

    def test_display_version_label_cannot_override_or_reject_machine_version(self):
        _,terminal,package=catalog.TARGETS[0]
        for label in ('Controller firmware March release','v26.03.10.02','',None):
            body=entry();body['data'][0]['versionName']=label
            candidate=catalog._candidate(body,terminal,package)
            self.assertEqual(candidate['version'],'260310A')
            self.assertEqual(candidate['feed_version'],'v26.03.10.01')
            self.assertEqual(fw.candidate_release('left',candidate).version,'260310A')
        body=entry();body['data'][0]['versionCode']='invalid'
        with self.assertRaises(catalog.CatalogError):catalog._candidate(body,terminal,package)

    def test_bootstrap_never_selects_an_untrusted_host_or_model(self):
        for bad in ({'gameDomain': 'http://127.0.0.1'}, {'areaCode': '../'}, {'extend': {}},
                {'extend': {'MachineAdaption': [{'MachineName': 'Legion Go S', 'MachineType': 'Legion Go S'}]}}):
            body = bootstrap(); body['data'].update(bad)
            with patch.object(catalog, '_fetch', return_value=body) as fetch:
                with self.assertRaises(catalog.CatalogError): catalog._check_http()
                self.assertEqual(fetch.call_count, 1)
        with patch.object(catalog, '_fetch', side_effect=[bootstrap(), entry(), entry('right'), entry('receiver')]) as fetch:
            response = catalog._check_http()
            self.assertEqual(response['region'], 'PL')
            self.assertEqual(set(response['candidates']), set(fw.RELEASES))
            for call in fetch.call_args_list[1:]:
                self.assertEqual(call.args[1]['sn'], '')
                self.assertEqual(call.args[1]['packageVer'], 'v00.00.00.00')
            self.assertEqual(response['candidates']['left']['version'], '260310A')

    def test_json_rejects_duplicates_invalid_types_and_nonfinite(self):
        for data in (b'{"code":200,"code":201}', b'{"x":NaN}', b'\xff', b'not json'):
            with self.assertRaises(catalog.CatalogError): catalog._json(data)

    def test_http_metadata_size_and_redirect(self):
        url = catalog.BASE + '/api/getBaseUrls?source=1'
        class Response(io.BytesIO):
            status = 200
            headers = {}
            def geturl(self): return url
        def fetch(response):
            with patch.object(catalog.urllib.request, 'build_opener') as builder:
                builder.return_value.open.return_value = response
                return catalog._fetch('/api/getBaseUrls', {'source': '1'})
        self.assertEqual(fetch(Response(b'{"code":200}')), {'code': 200})
        for response in (Response(b'x' * (catalog.MAX_BODY+1)), Response(b'x')):
            if len(response.getvalue()) == 1: response.headers = {'Content-Encoding': 'gzip'}
            with self.assertRaises(catalog.CatalogError): fetch(response)
        bad = Response(b'{}'); bad.geturl = lambda: 'https://other.invalid/'
        with self.assertRaises(catalog.CatalogError): fetch(bad)
        bad = Response(b'{}'); bad.status = 404
        with self.assertRaises(catalog.CatalogError): fetch(bad)
        with self.assertRaises(catalog.CatalogError): catalog._NoRedirect().redirect_request(None)

    def test_parent_deadline_cancel_and_isolated_command(self):
        worker = Mock(returncode=0); worker.poll.return_value = 0
        worker.communicate.return_value = (json.dumps(result()).encode(), None)
        with patch.object(catalog.subprocess, 'Popen', return_value=worker) as spawn:
            self.assertEqual(catalog.check_catalog(threading.Event()), result())
            self.assertEqual(spawn.call_args.args[0][-1], '--catalog-data')
            self.assertIn('-I', spawn.call_args.args[0])
            self.assertNotIn('shell', spawn.call_args.kwargs)
        worker = Mock(returncode=None); worker.poll.return_value = None
        worker.communicate.side_effect = [subprocess.TimeoutExpired('helper', .2), (b'', None)]
        with patch.object(catalog.subprocess, 'Popen', return_value=worker), patch.object(
                catalog.time, 'monotonic', side_effect=[0., 0., 46.]):
            with self.assertRaises(catalog.CatalogError): catalog.check_catalog(threading.Event())
        worker.kill.assert_called_once()
        cancelled = threading.Event(); cancelled.set()
        with patch.object(catalog.subprocess, 'Popen') as spawn:
            with self.assertRaises(catalog.CatalogError): catalog.check_catalog(cancelled)
            spawn.assert_not_called()

    def test_close_reaps_real_stalled_network_helper(self):
        cancelled = threading.Event()
        with subprocess.Popen([sys.executable, '-I', '-c', 'import time;time.sleep(30)'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as child:
            timer = threading.Timer(.3, cancelled.set); timer.start()
            try:
                with patch.object(catalog.subprocess, 'Popen', return_value=child):
                    with self.assertRaises(catalog.CatalogError): catalog.check_catalog(cancelled)
                self.assertIsNotNone(child.poll())
            finally:
                timer.cancel(); timer.join(2)
                if child.poll() is None: child.kill(); child.wait(timeout=3)

    def test_job_exclusion_unknown_candidate_and_snapshot_isolation(self):
        started, finish = threading.Event(), threading.Event()
        new = result(); new['candidates']['left']['version'] = '260311A'
        new['candidates']['left']['url'] = 'https://s3.service.lenovo.com/legion/cms/package/new.bin'
        def fetch(cancelled): started.set(); self.assertTrue(finish.wait(2)); return new
        with patch.object(catalog, 'check_catalog', side_effect=fetch):
            service = fw.FirmwarePreparation()
            self.assertEqual(service.status()['catalog']['phase'], 'unchecked')
            service.check_catalog(); self.assertTrue(started.wait(2))
            for action in (lambda: service.prepare('left'), service.check_catalog):
                with self.assertRaises(fw.FirmwareError): action()
            finish.set(); service._thread.join(2)
            status = service.status()
            self.assertTrue(status['catalog_live'])
            self.assertFalse(status['catalog']['candidates']['left']['reviewed'])
            self.assertTrue(status['catalog']['candidates']['right']['reviewed'])
            self.assertNotIn('url', status['catalog']['candidates']['right'])
            self.assertEqual(status['components'][0]['version'], '260310A')
            self.assertIsNone(status['installed_versions']); self.assertFalse(status['flash_available'])
            status['catalog']['candidates']['right']['reviewed'] = False
            self.assertTrue(service.status()['catalog']['candidates']['right']['reviewed'])
            service.close()
            with self.assertRaises(fw.FirmwareError): service.check_catalog()

    def test_failed_refresh_marks_old_results_stale_and_does_not_discard_validated_image(self):
        with patch.object(catalog, 'check_catalog', side_effect=[result(), catalog.CatalogError('offline')]):
            service = fw.FirmwarePreparation()
            service._image = fw.ValidatedImage(fw.release_for('left'), b'fixture')
            service.check_catalog(); service._thread.join(2)
            service.check_catalog(); service._thread.join(2)
            status = service.status()
            self.assertFalse(status['catalog_live']); self.assertEqual(status['catalog']['phase'], 'failed')
            self.assertEqual(status['catalog']['checked_at'], result()['checked_at'])
            self.assertEqual(status['catalog']['error'], 'offline')
            self.assertIsNotNone(service._image); service.close()

    def test_prepare_blocks_catalog_and_close_prevents_late_catalog_publish(self):
        started, finish = threading.Event(), threading.Event()
        def fetch(cancelled): started.set(); self.assertTrue(finish.wait(2)); return result()
        with patch.object(catalog, 'check_catalog', side_effect=fetch):
            service = fw.FirmwarePreparation(); service.check_catalog(); self.assertTrue(started.wait(2))
            closer = threading.Thread(target=service.close); closer.start()
            self.assertTrue(service._cancelled.wait(2)); finish.set(); closer.join(2)
            self.assertFalse(closer.is_alive()); self.assertEqual(service.status()['catalog']['phase'], 'stopped')
            self.assertEqual(service.status()['catalog']['checked_at'], '')
        started.clear(); finish.clear()
        def download(release, cancelled): started.set(); self.assertTrue(finish.wait(2)); return b'fixture'
        with patch.object(fw, 'download_image', side_effect=download), patch.object(fw, 'validate_image',
                return_value=fw.ValidatedImage(fw.release_for('left'), b'fixture')):
            service = fw.FirmwarePreparation(); service.prepare('left'); self.assertTrue(started.wait(2))
            with self.assertRaises(fw.FirmwareError): service.check_catalog()
            finish.set(); service._thread.join(2); service.close()


if __name__ == '__main__': unittest.main()

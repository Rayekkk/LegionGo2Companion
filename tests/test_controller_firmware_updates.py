# SPDX-License-Identifier: BSD-3-Clause
"""Unified user action; fake installer tests are not live OTA evidence."""
import copy
import threading
import unittest
from unittest.mock import patch

import controller_firmware as fw
import controller_firmware_catalog as catalog
from test_controller_firmware_catalog import result, result as result_feed


def observation(old=False):
    return {'versions': {c: '250101A' if old else r.version for c, r in fw.RELEASES.items()},
            'attachment': 'same-device', 'source': 'fixture'}


class UpdateTests(unittest.TestCase):
    def test_verified_before_write_failure_is_retryable_but_cleanup_fault_is_not(self):
        clean={'phase':'failed_before_write','restored':True,'cleanup_complete':True,
            'cleanup_errors':[],'error':'Preflight rejected','completed':[],'versions':observation(True)['versions']}
        for fault in (False,True):
            result=dict(clean,cleanup_complete=not fault,cleanup_errors=['restoration'] if fault else [])
            with patch.object(fw,'download_image',return_value=b'image'),patch.object(fw,'validate_image',
                    side_effect=lambda c,d,release=None:fw.ValidatedImage(release,d)):
                service=self.run_service(reader=lambda:observation(True),installer=lambda *a:result)
                self.assertEqual(service.status()['job']['phase'],'uncertain' if fault else 'failed_before_write')
                if fault:
                    with self.assertRaises(fw.FirmwareError):service.check_updates()
                else:
                    with patch.object(catalog,'check_catalog',return_value=result_feed()):
                        service.check_updates();service._thread.join(3)
                    self.assertEqual(service.status()['job']['phase'],'failed_before_write')

    def test_close_does_not_wait_for_admitted_independent_worker(self):
        entered,released=threading.Event(),threading.Event()
        def install(*args):entered.set();self.assertTrue(released.wait(3));return {'phase':'uncertain'}
        service=fw.FirmwarePreparation(installed_reader=lambda:observation(True),installer=install)
        try:
            with patch.object(catalog,'check_catalog',return_value=result_feed()),\
                    patch.object(fw,'download_image',return_value=b'image'),patch.object(fw,'validate_image',
                    side_effect=lambda c,d,release=None:fw.ValidatedImage(release,d)):
                service.check_updates();self.assertTrue(entered.wait(2))
                service.close();self.assertTrue(service._thread.is_alive())
                with self.assertRaises(fw.FirmwareError):service.check_updates()
                released.set();service._thread.join(2)
            self.assertEqual(service.status()['job']['phase'],'uncertain')
        finally:released.set();service.close()
    def test_restart_observes_active_or_uncertain_job_before_any_new_check(self):
        class Installer:
            def __init__(self,phase):self.phase=phase
            def previous_status(self):return {'id':'old-job','phase':self.phase,'error':'','message':'Previous update'}
            def __call__(self,*args):raise AssertionError('second installer')
        for phase in ('installing','uncertain'):
            service=fw.FirmwarePreparation(installed_reader=lambda:(_ for _ in ()).throw(AssertionError('new check')),
                installer=Installer(phase))
            self.addCleanup(service.close)
            self.assertEqual(service.status()['job']['phase'],phase)
            if phase=='uncertain':
                with self.assertRaises(fw.FirmwareError):service.check_updates()
            else:self.assertEqual(service.check_updates()['job']['id'],'old-job')
            self.assertIsNone(service._thread)

    def test_late_progress_cannot_erase_uncertain_result_or_admit_retry(self):
        callbacks = []
        def install(images, before, progress):
            callbacks.append(progress)
            return {'phase': 'uncertain'}
        with patch.object(fw, 'download_image', return_value=b'image'), patch.object(fw, 'validate_image',
                side_effect=lambda c,d,release=None:fw.ValidatedImage(release or fw.release_for(c),d)):
            service = self.run_service(reader=lambda: observation(True), installer=install)
        callbacks[0]('verifying', 'Late message')
        self.assertEqual(service.status()['job']['phase'], 'uncertain')
        with self.assertRaises(fw.FirmwareError): service.check_updates()

    def run_service(self, reader=None, installer=None, feed=None):
        service = fw.FirmwarePreparation(installed_reader=reader or observation, installer=installer)
        with patch.object(catalog, 'check_catalog', return_value=feed or result()):
            service.check_updates()
            service._thread.join(3)
        self.assertFalse(service._thread.is_alive())
        self.addCleanup(service.close)
        return service

    def test_no_newer_component_never_downloads_or_opens_installer(self):
        with patch.object(fw, 'download_image') as download:
            service = self.run_service()
        self.assertEqual(service.status()['job']['phase'], 'up_to_date')
        self.assertEqual(service.status()['job']['pending'], [])
        download.assert_not_called()

    def test_missing_installer_cannot_turn_check_into_fake_installation(self):
        service = self.run_service(reader=lambda: observation(True))
        self.assertEqual(service.status()['job']['phase'], 'blocked')
        self.assertEqual(service.status()['job']['completed'], [])

    def test_one_plan_validates_all_images_then_installs_all_needed_components(self):
        events = []
        def download(release, cancelled):
            events.append(('download', release.component));return release.component.encode()
        def validate(component, data, release=None):
            events.append(('validate', component));return fw.ValidatedImage(release or fw.release_for(component), data)
        def install(images, before, progress):
            events.append(('install', tuple(i.release.component for i in images)))
            self.assertEqual(before, observation(True))
            progress('verifying', 'Verifying update.')
            return {'phase':'succeeded','restored':True,'cleanup_complete':True,'cleanup_errors':[],'error':'','completed':['left','right','receiver'],'versions':observation()['versions']}
        with patch.object(fw, 'download_image', side_effect=download), patch.object(fw, 'validate_image', side_effect=validate):
            service = self.run_service(reader=lambda: observation(True), installer=install)
        self.assertEqual(events[-1], ('install', ('left', 'right', 'receiver')))
        self.assertEqual(len(events), 7)
        self.assertEqual(service.status()['job']['phase'], 'succeeded')
        self.assertEqual(service.status()['job']['completed'], ['left', 'right', 'receiver'])

    def test_equal_or_newer_installed_components_are_skipped_not_downgraded(self):
        before=observation();before['versions']['left']='250101A';before['versions']['receiver']='260501A'
        def install(images, observed, progress):
            self.assertEqual([i.release.component for i in images], ['left'])
            after=copy.deepcopy(before['versions']);after['left']=fw.release_for('left').version
            return {'phase':'succeeded','restored':True,'cleanup_complete':True,'cleanup_errors':[],'error':'','completed':['left'],'versions':after}
        with patch.object(fw, 'download_image', return_value=b'image'), patch.object(fw, 'validate_image',
                side_effect=lambda c,d,release=None:fw.ValidatedImage(release or fw.release_for(c),d)):
            service=self.run_service(reader=lambda:copy.deepcopy(before),installer=install)
        self.assertEqual(service.status()['job']['completed'], ['left'])
        self.assertEqual(service.status()['installed_versions']['receiver'], '260501A')

    def test_foreign_offer_blocks_entire_plan_before_download_or_install(self):
        feed=result();feed['candidates']['right']['url']='https://foreign.invalid/new.bin'
        with patch.object(fw,'download_image') as download:
            service=self.run_service(reader=lambda:observation(True),feed=feed,installer=lambda *a:None)
        self.assertEqual(service.status()['job']['phase'],'failed');download.assert_not_called()

    def test_invalid_partial_feed_and_changed_connection_cannot_report_up_to_date(self):
        feed=result();feed['candidates']['left']=None
        self.assertEqual(self.run_service(feed=feed).status()['job']['phase'],'failed')
        iterator=iter([observation(),dict(observation(),attachment='other-device')])
        self.assertEqual(self.run_service(reader=lambda:next(iterator)).status()['job']['phase'],'failed')

    def test_failure_before_all_images_validated_never_installs(self):
        calls=[]
        with patch.object(fw,'download_image',return_value=b'image'),patch.object(fw,'validate_image',
                side_effect=fw.FirmwareError('Bad signature')):
            service=self.run_service(reader=lambda:observation(True),installer=lambda *a:calls.append(a))
        self.assertEqual(service.status()['job']['phase'],'failed');self.assertEqual(calls,[])

    def test_unverified_result_or_installer_exception_latches_no_retry(self):
        for outcome in ({'restored':False,'versions':observation()['versions']},
                        {'restored':True,'versions':observation(True)['versions']},RuntimeError('private failure')):
            def install(*args):
                if isinstance(outcome,Exception):raise outcome
                return outcome
            with patch.object(fw,'download_image',return_value=b'image'),patch.object(fw,'validate_image',
                    side_effect=lambda c,d,release=None:fw.ValidatedImage(release or fw.release_for(c),d)):
                service=self.run_service(reader=lambda:observation(True),installer=install)
            self.assertEqual(service.status()['job']['phase'],'uncertain')
            with self.assertRaises(fw.FirmwareError):service.check_updates()
            self.assertNotIn('private failure',service.status()['job']['error'])

    def test_progress_cannot_erase_admission_and_allow_retry(self):
        def install(images, before, progress):
            progress('failed', 'Failure')
            raise RuntimeError('Failure after admission')
        with patch.object(fw,'download_image',return_value=b'image'),patch.object(fw,'validate_image',
                side_effect=lambda c,d,release=None:fw.ValidatedImage(release or fw.release_for(c),d)):
            service=self.run_service(reader=lambda:observation(True),installer=install)
        self.assertEqual(service.status()['job']['phase'],'uncertain')
        with self.assertRaises(fw.FirmwareError):service.check_updates()

    def test_partial_failed_or_cleanup_error_cannot_become_success_from_versions(self):
        good={'phase':'succeeded','restored':True,'cleanup_complete':True,'cleanup_errors':[],'error':'',
              'completed':['left','right','receiver'],'versions':observation()['versions']}
        for changes in ({'phase':'uncertain'},{'completed':['left']},{'cleanup_errors':['transport']},
                        {'cleanup_complete':False},{'error':'failed'},{'versions':{'left':'260310A'}}):
            with patch.object(fw,'download_image',return_value=b'image'),patch.object(fw,'validate_image',
                    side_effect=lambda c,d,release=None:fw.ValidatedImage(release or fw.release_for(c),d)):
                service=self.run_service(reader=lambda:observation(True),installer=lambda *a:dict(good,**changes))
            self.assertEqual(service.status()['job']['phase'],'uncertain')

    def test_concurrent_frontends_share_job_and_status_is_not_mutable(self):
        entered,release=threading.Event(),threading.Event()
        def feed(cancelled):entered.set();self.assertTrue(release.wait(2));return result()
        service=fw.FirmwarePreparation(installed_reader=observation)
        try:
            with patch.object(catalog,'check_catalog',side_effect=feed) as fetch:
                first=service.check_updates();self.assertTrue(entered.wait(2))
                second=service.check_updates();self.assertEqual(first['job']['id'],second['job']['id'])
                second['job']['pending'].append('fake')
                self.assertEqual(service.status()['job']['pending'],[])
                release.set();service._thread.join(2);self.assertEqual(fetch.call_count,1)
        finally:release.set();service.close()


if __name__ == '__main__': unittest.main()

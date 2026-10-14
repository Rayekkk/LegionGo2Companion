# SPDX-License-Identifier: BSD-3-Clause
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import controller_firmware as fw
import controller_firmware_process as process

REPO=Path(__file__).resolve().parents[1]


class ProcessPlanTests(unittest.TestCase):
    def make_sources(self, root):
        for name in process.SOURCES: (root/name).write_bytes((REPO/name).read_bytes())

    def freeze(self, root, job='a'*32):
        with patch.object(process,'validate_image',return_value=fw.ValidatedImage(fw.release_for('left'),b'fixture')):
            return process.freeze_bundle(job,'left',b'fixture',root)

    def test_fixed_bounded_system_service_is_not_a_child_scope_or_shell(self):
        plan=process.service_plan('a'*32,60,'b'*64)
        self.assertEqual(plan.working_directory,process.RUNTIME_ROOT+'/'+'a'*32)
        self.assertEqual(plan.argv[0],'/usr/bin/systemd-run')
        self.assertIn('--service-type=exec',plan.argv)
        self.assertIn('--property=Restart=no',plan.argv)
        self.assertIn('--property=RuntimeMaxSec=60',plan.argv)
        self.assertIn('--property=KillMode=control-group',plan.argv)
        self.assertIn('--property=StandardOutput=null',plan.argv)
        self.assertIn('--property=StandardError=journal',plan.argv)
        self.assertNotIn('--scope',plan.argv)
        self.assertNotIn('--user',plan.argv)
        command=plan.argv[plan.argv.index('/usr/bin/python3'):]
        self.assertCountEqual(command[1:3],('-I','-B'))
        self.assertEqual(command[3:],('-c',process.BOOTSTRAP,plan.working_directory,'b'*64))
        self.assertFalse(any('plugin_loader' in item or 'Decky Plugins' in item for item in plan.argv))

    def test_invalid_job_and_runtime_cannot_inject_options_or_paths(self):
        for job in ('../abc','a'*31,'A'*32,'a'*32+';bad','--scope',None,1):
            with self.assertRaises(fw.FirmwareError): process.service_plan(job,60,'b'*64)
        for duration in (True,9,3661,1.5,None,'60'):
            with self.assertRaises(fw.FirmwareError): process.service_plan('a'*32,duration,'b'*64)

    def test_unverified_bootstrap_reports_fixed_fault_without_importing_untrusted_source(self):
        result=subprocess.run([sys.executable,'-I','-B','-c',process.BOOTSTRAP,
            'private-secret-marker','invalid-manifest-digest'],capture_output=True,timeout=5)
        self.assertEqual(result.returncode,2)
        self.assertFalse(json.loads(result.stdout)['flash_available'])
        self.assertEqual(json.loads(result.stderr)['event'],'staging_verification_failed')
        self.assertNotIn(b'private-secret-marker',result.stderr)
        self.assertNotIn(b'Traceback',result.stderr)

    def test_bundle_bytes_are_immutable_and_independent_of_removed_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'producer'; root.mkdir(); self.make_sources(root)
            bundle=self.freeze(root); manifest=json.loads(bundle.manifest)
            self.assertEqual(set(bundle.files),set(process.SOURCES)|{'image.bin'})
            for name,data in bundle.files.items():
                self.assertEqual(manifest['files'][name],{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
            with self.assertRaises(TypeError): bundle.files['image.bin']=b'changed'
            shutil.rmtree(root)
            self.assertEqual(bundle.files['image.bin'],b'fixture')
            self.assertTrue(bundle.files['controller_firmware_worker.py'])
            self.assertTrue(bundle.files['diagnostics.py'])

    def test_invalid_authentication_cannot_capture_sources(self):
        with patch.object(process.Path,'lstat',side_effect=AssertionError('capture before auth')):
            with self.assertRaises(fw.FirmwareError): process.freeze_bundle('a'*32,'left',b'bad',REPO)

    def test_missing_empty_oversized_or_directory_sources_rejected(self):
        for kind in ('missing','empty','oversized','directory'):
            with tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary); self.make_sources(root)
                target=root/'controller_firmware_worker.py'; target.unlink()
                if kind=='empty': target.touch()
                if kind=='oversized': target.write_bytes(b'X'*(process.MAX_SOURCE+1))
                if kind=='directory': target.mkdir()
                with self.assertRaises(fw.FirmwareError): self.freeze(root)

    def test_relative_source_root_rejected(self):
        with self.assertRaises(fw.FirmwareError): self.freeze(Path('.'))

    def test_worker_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); self.make_sources(root)
            target=root/'controller_firmware_worker.py'; target.unlink()
            try: target.symlink_to(REPO/'controller_firmware_worker.py')
            except OSError: self.skipTest('Creating symlinks unavailable on this host')
            with self.assertRaises(fw.FirmwareError): self.freeze(root)


class OfficialProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('COMPANION_OFFICIAL_FW_FIXTURES'), 'Original Lenovo BINs required')
    def test_worker_survives_own_broker_loss_and_producer_directory_removal(self):
        # This real subprocess experiment proves only this host's process
        # behavior. It does not establish SteamOS cgroup/inhibitor/HID behavior.
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve(); producer=root/'producer'; producer.mkdir()
            stage=root/'stage'; stage.mkdir()
            for name in process.SOURCES: shutil.copyfile(REPO/name,producer/name)
            original=(Path(os.environ['COMPANION_OFFICIAL_FW_FIXTURES'])/'left-v26.03.10.01.bin').read_bytes()
            bundle=process.freeze_bundle('b'*32,'left',original,producer)
            for name,data in bundle.files.items(): (stage/name).write_bytes(data)
            shutil.copyfile(REPO/'tests/firmware_worker_fixture.py',stage/'firmware_worker_fixture.py')
            marker=root/'started'; gate=root/'continue'; result=root/'result.json'
            broker=root/'broker.py'
            broker.write_text('import subprocess,sys,time\n'
                'subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)\n'
                'time.sleep(20)\n',encoding='utf-8')
            code=('import sys;sys.path.insert(0,sys.argv[1]);'
                'from firmware_worker_fixture import process_fixture;process_fixture(*sys.argv[1:])')
            arguments=[sys.executable,'-I','-B','-c',code,str(stage),str(marker),str(gate),str(result)]
            parent=subprocess.Popen([sys.executable,str(broker)]+arguments,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+10
                while not marker.exists() and time.monotonic()<deadline: time.sleep(.02)
                self.assertTrue(marker.exists(),'Authenticated test worker did not reach first output')
                parent.kill(); parent.wait(timeout=3)  # Only our own disposable broker.
                self.assertIsNotNone(parent.returncode)
                # Checked absolute path is inside this test's temporary root.
                self.assertEqual(producer.parent,root)
                shutil.rmtree(producer)
                gate.touch()
                deadline=time.monotonic()+10
                while not result.exists() and time.monotonic()<deadline: time.sleep(.02)
                self.assertTrue(result.exists(),'Finite child did not return a result')
                outcome=json.loads(result.read_text(encoding='utf-8'))
                self.assertEqual(outcome['snapshot']['phase'],'succeeded')
                self.assertEqual(outcome['snapshot']['total_bytes'],len(original))
                self.assertCountEqual(outcome['close_order'],['transport','inhibitor','journal'])
                self.assertEqual(outcome['close_order'][0],'transport')
                self.assertTrue(outcome['cleanup_complete'])
                self.assertTrue((stage/'controller_firmware_worker.py').exists())
            finally:
                gate.touch()  # The test worker also has its own eight-second gate deadline.
                if parent.poll() is None: parent.kill()
                parent.wait(timeout=3)


if __name__ == '__main__': unittest.main()

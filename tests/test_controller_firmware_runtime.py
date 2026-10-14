# SPDX-License-Identifier: BSD-3-Clause
import hashlib
import json
import os
import stat
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

import controller_firmware as fw
import controller_firmware_process as process
import controller_firmware_stage as stage
from controller_firmware_sleep import FirmwareInhibitor
from controller_firmware_transfer import TransferPolicy
from test_fan_sleep import Library, Function

REPO = Path(__file__).resolve().parents[1]


def policy(): return TransferPolicy(frozenset({1024}), 1, 5, 5, 60)


def bundle():
    with patch.object(process, 'validate_image', return_value=fw.ValidatedImage(fw.release_for('left'), b'fixture')):
        captured = process.freeze_bundle('a'*32, 'left', b'fixture', REPO)
    # Pure staging tests use a fake pinned release with this fixture's digest.
    manifest = json.loads(captured.manifest)
    manifest['image_sha256'] = hashlib.sha256(b'fixture').hexdigest()
    return process.FrozenBundle(captured.job_id, captured.files, json.dumps(manifest).encode('ascii'))


def fixture_validation():
    return patch.object(stage, 'validate_image', return_value=SimpleNamespace(
        release=SimpleNamespace(sha256=hashlib.sha256(b'fixture').hexdigest())))


class RuntimeTests(unittest.TestCase):
    def test_bundle_tampering_missing_extra_and_manifest_identity_rejected(self):
        original = bundle()
        with fixture_validation():
            self.assertEqual(stage.validate_bundle(original)['job_id'], original.job_id)
            for action in ('data', 'missing', 'extra', 'job', 'image', 'duplicate', 'schema'):
                files, manifest = dict(original.files), json.loads(original.manifest)
                if action == 'data': files['image.bin'] = b'changed'
                if action == 'missing': files.pop('controller_firmware_worker.py')
                if action == 'extra': files['evil.py'] = b'evil'
                if action == 'job': manifest['job_id'] = 'b'*32
                if action == 'image': manifest['image_sha256'] = 'b'*64
                if action == 'schema': manifest['schema'] = True
                raw = json.dumps(manifest).encode('ascii')
                if action == 'duplicate': raw = original.manifest[:-1] + b',"schema":1}'
                with self.assertRaises(fw.FirmwareError):
                    stage.validate_bundle(process.FrozenBundle(original.job_id, MappingProxyType(files), raw))

    def test_invalid_stage_identity_and_untrusted_digest_fail_before_launch(self):
        for digest in ('', 'x'*64, 'a'*63, None, 1, '--scope'):
            with self.assertRaises(fw.FirmwareError): process.service_plan('a'*32,60,digest)
        for job in (None, stage.StagedJob('a'*32,'/tmp/plugin/'+'a'*32,'b'*64)):
            with self.assertRaises(fw.FirmwareError): stage.verify_stage(job)

    def test_service_lifetime_contains_acquisition_and_transaction_margin(self):
        lifetime=process.service_runtime(policy())
        self.assertGreater(lifetime,policy().verification_timeout+policy().transaction_timeout)
        self.assertLessEqual(lifetime,3660)
        with self.assertRaises(fw.FirmwareError): process.service_runtime(None)
        with self.assertRaises(fw.FirmwareError):
            process.service_runtime(TransferPolicy(frozenset({1024}),1,5,3600,3600))

    def test_linux_required_without_mutation(self):
        frozen = bundle()
        with patch('controller_firmware_stage.os.name','nt'), patch.object(stage,'_publish') as publish:
            with self.assertRaises(fw.FirmwareError): stage.stage_bundle(frozen)
            publish.assert_not_called()
        with patch('controller_firmware_process.os.name','nt'), patch.object(process.subprocess,'run') as run:
            with self.assertRaises(fw.FirmwareError): process.launch_service(None,policy())
            with self.assertRaises(fw.FirmwareError): process.service_state('a'*32)
            run.assert_not_called()

    def test_single_launch_claim_no_retry_even_on_timeout_or_nonzero(self):
        job=stage.StagedJob('a'*32,stage.RUNTIME_ROOT+'/'+'a'*32,'b'*64)
        for outcome in (subprocess.CompletedProcess([],0),subprocess.CompletedProcess([],1),subprocess.TimeoutExpired('fixed',5)):
            with patch('controller_firmware_process.os.name','posix'), patch('controller_firmware_process.os.geteuid',return_value=0,create=True), \
                    patch.object(process,'verify_stage'), patch.object(process,'_private_directory',return_value=1), \
                    patch.object(process.os,'O_NOFOLLOW',0,create=True),patch.object(process.os,'O_CLOEXEC',0,create=True), \
                    patch.object(process.os,'open',return_value=2), patch.object(process.os,'write',side_effect=lambda fd,data:len(data)), \
                    patch.object(process.os,'fsync'), patch.object(process.os,'close'), \
                    patch.object(process.subprocess,'run',side_effect=outcome if isinstance(outcome,Exception) else None,
                        return_value=outcome) as run:
                result=process.launch_service(job,policy())
                self.assertIs(result['submitted'],True if isinstance(outcome,subprocess.CompletedProcess) and outcome.returncode==0 else None)
                self.assertFalse(result['flash_available']);self.assertTrue(result['stage_retained'])
                self.assertEqual(run.call_count,1)
                self.assertEqual(run.call_args.kwargs['timeout'],5)
        with patch('controller_firmware_process.os.name','posix'), patch('controller_firmware_process.os.geteuid',return_value=0,create=True), \
                patch.object(process,'verify_stage'),patch.object(process,'_private_directory',return_value=1), \
                patch.object(process.os,'O_NOFOLLOW',0,create=True),patch.object(process.os,'O_CLOEXEC',0,create=True), \
                patch.object(process.os,'open',side_effect=FileExistsError),patch.object(process.os,'close'), \
                patch.object(process.subprocess,'run') as run:
            with self.assertRaises(fw.FirmwareError): process.launch_service(job,policy())
            run.assert_not_called()

    def test_lost_verification_never_claims_or_submits(self):
        with patch('controller_firmware_process.os.name','posix'), patch('controller_firmware_process.os.geteuid',return_value=0,create=True), \
                patch.object(process,'verify_stage',side_effect=fw.FirmwareError('bad')),patch.object(process.os,'open') as opened, \
                patch.object(process.subprocess,'run') as run:
            with self.assertRaises(fw.FirmwareError): process.launch_service(stage.StagedJob('a'*32,stage.RUNTIME_ROOT+'/'+'a'*32,'b'*64),policy())
            opened.assert_not_called();run.assert_not_called()

    def test_loader_environment_is_removed_without_mutating_parent(self):
        with patch.dict(os.environ, {'PYTHONPATH':'evil','PYTHONSTARTUP':'evil','SYSTEMD_BUS_TIMEOUT':'evil',
                'LD_PRELOAD':'evil','DBUS_SYSTEM_BUS_ADDRESS':'evil','KEEP':'yes'}):
            result=process._environment()
            self.assertFalse(any(key.startswith(('PYTHON','SYSTEMD_','LD_')) for key in result))
            self.assertNotIn('DBUS_SYSTEM_BUS_ADDRESS',result)
            self.assertEqual(result['KEEP'],'yes');self.assertEqual(os.environ['PYTHONPATH'],'evil')

    def test_service_observation_checks_identity_and_retains_unknown(self):
        unit='legiongo2-firmware-'+'a'*32+'.service'
        output=('Id='+unit+'\nLoadState=loaded\nActiveState=active\nSubState=running\nInvocationID='+'b'*32+
            '\nControlGroup=/system.slice/'+unit+'\nResult=success\n').encode('ascii')
        for candidate,expected in ((output,'active'),(output.replace(b'Id=legiongo2',b'Id=alien'),'unknown'),
                (output+b'Id=duplicate\n','unknown'),(output.replace(b'/system.slice/',b'/other.slice/'),'unknown'),
                (b'X'*4097,'unknown')):
            with patch('controller_firmware_process.os.name','posix'),patch.object(process.subprocess,'run',
                    return_value=subprocess.CompletedProcess([],0,candidate)) as run:
                result=process.service_state('a'*32)
                self.assertEqual(result['state'],expected);self.assertTrue(result['stage_retained'])
                self.assertEqual(run.call_args.args[0][0],'/usr/bin/systemctl')

    def test_failed_temporary_unlink_still_closes_all_directory_descriptors(self):
        # Fault injection works on Windows; actual dirfd behavior remains POSIX.
        captured=bundle()
        lock_info=SimpleNamespace(st_mode=stat.S_IFREG|0o600,st_uid=0,st_nlink=1)
        with fixture_validation(),patch.object(stage,'_private_directory',return_value=100), \
                patch.dict('sys.modules',{'fcntl':SimpleNamespace(LOCK_EX=2,LOCK_NB=4,flock=lambda *a:None)}), \
                patch.object(stage.os,'O_NOFOLLOW',0,create=True),patch.object(stage.os,'O_CLOEXEC',0,create=True), \
                patch.object(stage.os,'O_DIRECTORY',0,create=True),patch.object(stage.os,'open',side_effect=[101,102,103]), \
                patch.object(stage.os,'fstat',return_value=lock_info),patch.object(stage.os,'stat',side_effect=FileNotFoundError), \
                patch.object(stage.os,'mkdir'),patch.object(stage.os,'write',return_value=0), \
                patch.object(stage.os,'unlink',side_effect=OSError('failure')),patch.object(stage.os,'rmdir',side_effect=OSError('nonempty')), \
                patch.object(stage.os,'close') as closed:
            with self.assertRaisesRegex(fw.FirmwareError,'remnants were retained'):
                stage._publish(captured,'/fixed',0)
            # These independent handles must each close once; their relative
            # close order is not the cleanup contract under test.
            self.assertCountEqual([call.args[0] for call in closed.call_args_list],[103,101,102,100])


@unittest.skipUnless(os.name=='posix','Anchored POSIX staging requires Linux/POSIX')
class PosixStageTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name).resolve()
        self.root.chmod(0o700);self.captured=bundle();self.owner=os.geteuid()
    def tearDown(self): self.temporary.cleanup()
    def publish(self):
        with fixture_validation(): return stage._publish(self.captured,str(self.root),self.owner)
    def verifier(self):
        namespace={'__name__':'_test'};exec(stage.BOOTSTRAP,namespace);return namespace['verify']
    def test_cleanup_allows_optional_bad_output_but_keeps_sources_and_baseline_strict(self):
        job=self.publish();folder=Path(job.directory);verify=self.verifier()
        optional=folder/'status.json';optional.write_bytes(b'bad');optional.chmod(0o644)
        with self.assertRaises(ValueError):verify(job.directory,job.manifest_sha256,self.owner)
        self.assertEqual(verify(job.directory,job.manifest_sha256,self.owner,cleanup=True)['job_id'],job.job_id)
        critical=folder/'recovery.json';critical.write_bytes(b'bad');critical.chmod(0o644)
        with self.assertRaises(ValueError):verify(job.directory,job.manifest_sha256,self.owner,cleanup=True)
        critical.unlink()
        source=folder/'controller_firmware.py';source.write_bytes(b'changed')
        with self.assertRaises(ValueError):verify(job.directory,job.manifest_sha256,self.owner,cleanup=True)
    def test_atomic_publish_exact_private_files_and_duplicate_rejection(self):
        job=self.publish();folder=Path(job.directory)
        self.assertEqual(set(p.name for p in folder.iterdir()),stage.SOURCES|{'image.bin','manifest.json'})
        self.assertEqual(folder.stat().st_mode&0o777,0o700)
        for file in folder.iterdir(): self.assertEqual(file.stat().st_mode&0o777,0o600)
        self.assertEqual(self.verifier()(job.directory,job.manifest_sha256,self.owner)['job_id'],job.job_id)
        with self.assertRaises(fw.FirmwareError): self.publish()
    def test_source_tamper_symlink_hardlink_permission_and_manifest_digest_rejected(self):
        for kind in ('tamper','symlink','hardlink','mode','digest'):
            with self.subTest(kind=kind):
                folder=self.root/kind;folder.mkdir(mode=0o700)
                with fixture_validation(): job=stage._publish(self.captured,str(folder),self.owner)
                target=Path(job.directory)/'controller_firmware_worker.py'
                if kind=='tamper': target.write_bytes(b'raise RuntimeError("evil")')
                if kind=='symlink': target.unlink();target.symlink_to(REPO/'controller_firmware_worker.py')
                if kind=='hardlink': os.link(target,folder/'extra')
                if kind=='mode': target.chmod(0o644)
                expected='c'*64 if kind=='digest' else job.manifest_sha256
                with self.assertRaises((ValueError,OSError)): self.verifier()(job.directory,expected,self.owner)
    def test_symlink_or_writable_parent_and_wrong_private_root_rejected(self):
        self.root.chmod(0o755)
        with self.assertRaises(fw.FirmwareError): self.publish()
        self.root.chmod(0o700)
        alias=self.root/'alias';alias.symlink_to(self.root,target_is_directory=True)
        with fixture_validation(),self.assertRaises(OSError): stage._publish(self.captured,str(alias),self.owner)
    def test_short_write_never_publishes_job_and_cleans_owned_temporary(self):
        with fixture_validation(),patch.object(stage.os,'write',return_value=0),self.assertRaises(fw.FirmwareError):
            stage._publish(self.captured,str(self.root),self.owner)
        self.assertFalse((self.root/self.captured.job_id).exists())
        self.assertFalse(any(p.name.startswith('.stage-') for p in self.root.iterdir()))
    def test_extra_importable_module_or_pycache_cannot_bypass_source_verification(self):
        job=self.publish();folder=Path(job.directory)
        for name in ('subprocess.py','__pycache__'):
            target=folder/name
            if name=='__pycache__':target.mkdir(mode=0o700)
            else:target.write_bytes(b'raise RuntimeError("unverified")');target.chmod(0o600)
            with self.assertRaises(ValueError):self.verifier()(job.directory,job.manifest_sha256,self.owner)
            if target.is_dir():target.rmdir()
            else:target.unlink()
        (folder/'launch.claim').write_bytes(b'fixed');(folder/'launch.claim').chmod(0o600)
        self.verifier()(job.directory,job.manifest_sha256,self.owner)


class InhibitLibrary(Library):
    def __init__(self,fd,preparing=False,failure=None):
        super().__init__(fd,preparing,failure)
        self.calls=[];self.timeouts=[];self.open=True;self.wrong_sender=False
        self.sd_bus_is_open=Function(lambda *a:int(self.open))
        self.sd_bus_set_method_call_timeout=Function(lambda bus,value:self.timeouts.append(value) or 0)
        self.sd_bus_message_is_signal=Function(lambda message,interface,name:int(name=={
            'owner-change':b'NameOwnerChanged','sleep':b'PrepareForSleep','shutdown':b'PrepareForShutdown'}.get(self.current)))
        self.sd_bus_message_get_sender=Function(lambda message:b':9.9' if self.wrong_sender else b'org.freedesktop.DBus' if self.current=='owner-change' else b':1.5')
    def call(self,*args):
        self.calls.append(args)
        return super().call(*args)
    def read(self,message,types,*args):
        if types==b'b':return self.set(args[0],True)
        return super().read(message,types,*args)


class InhibitorTests(unittest.TestCase):
    def setUp(self):self.r,self.w=os.pipe()
    def tearDown(self):os.close(self.r);os.close(self.w)
    def acquire(self,lib,clock=lambda:1.0):
        poll=SimpleNamespace(register=lambda *a:None,poll=lambda *a:[])
        with patch('controller_firmware_sleep.os.name','posix'),patch('controller_firmware_sleep.c.CDLL',return_value=lib), \
                patch('controller_firmware_sleep.select.poll',return_value=poll,create=True), \
                patch('controller_firmware_sleep.select.POLLERR',8,create=True),patch('controller_firmware_sleep.select.POLLHUP',16,create=True), \
                patch('controller_firmware_sleep.select.POLLNVAL',32,create=True):
            inhibitor=FirmwareInhibitor(clock);inhibitor.acquire(2.0)
        return inhibitor
    def valid(self,inhibitor,events=()):
        poll=SimpleNamespace(register=lambda *a:None,poll=lambda *a:list(events))
        with patch('controller_firmware_sleep.select.poll',return_value=poll,create=True), \
                patch('controller_firmware_sleep.select.POLLERR',8,create=True),patch('controller_firmware_sleep.select.POLLHUP',16,create=True), \
                patch('controller_firmware_sleep.select.POLLNVAL',32,create=True):return inhibitor.valid()
    def test_actual_descriptor_owned_noninheritable_block_both_unique_login_owner(self):
        lib=InhibitLibrary(self.w);inhibitor=self.acquire(lib)
        try:
            self.assertTrue(self.valid(inhibitor));self.assertNotEqual(inhibitor._fd,self.w)
            self.assertFalse(os.get_inheritable(inhibitor._fd))
            call=next(item for item in lib.calls if item[4]==b'Inhibit')
            self.assertEqual(call[1],b':1.5')
            self.assertEqual([arg.value for arg in call[8:]],
                [b'sleep:shutdown',b'LegionGo2Companion',b'Controller firmware transaction',b'block'])
            self.assertTrue(all(0<value<=1000000 for value in lib.timeouts))
            with self.assertRaises(fw.FirmwareError): inhibitor.acquire(3)
        finally:inhibitor.close()
        self.assertFalse(self.valid(inhibitor));self.assertTrue(lib.closed);os.fstat(self.w)
    def test_preparing_or_method_failure_releases_fd_and_bus(self):
        for preparing,failure in ((True,None),(False,b'Inhibit'),(False,b'Get'),(False,b'GetNameOwner')):
            lib=InhibitLibrary(self.w,preparing,failure)
            with self.assertRaises(fw.FirmwareError):self.acquire(lib)
            self.assertTrue(lib.closed)
    def test_disconnect_ownerchange_forced_sleep_shutdown_flood_and_fd_loss_invalidate(self):
        class Flood:
            """Persistent input, independent of the production drain batch size."""
            def __init__(self):self.deadline=time.monotonic()+5
            def __bool__(self):return True
            def pop(self,index):
                if time.monotonic()>=self.deadline:
                    raise AssertionError('Firmware event draining did not terminate')
                return 'unrelated'
        for events in (['disconnect'],['owner-change'],['sleep'],['shutdown'],Flood()):
            lib=InhibitLibrary(self.w);inhibitor=self.acquire(lib)
            try:lib.messages=events;self.assertFalse(self.valid(inhibitor))
            finally:inhibitor.close()
        lib=InhibitLibrary(self.w);inhibitor=self.acquire(lib)
        try:
            self.assertFalse(self.valid(inhibitor,[(inhibitor._fd,16)]))
            lib.open=False;self.assertFalse(self.valid(inhibitor))
        finally:inhibitor.close()
    def test_deadline_invalid_expired_and_backward_fail_before_ready(self):
        inhibitor=FirmwareInhibitor(lambda:2.0)
        with patch('controller_firmware_sleep.os.name','posix'),self.assertRaises(fw.FirmwareError):inhibitor.acquire(2.0)
        for deadline in (True,float('nan'),float('inf'),-1):
            with patch('controller_firmware_sleep.os.name','posix'),self.assertRaises(fw.FirmwareError):FirmwareInhibitor().acquire(deadline)
        times=iter([1.0,.5]);lib=InhibitLibrary(self.w)
        with self.assertRaises(fw.FirmwareError):self.acquire(lib,lambda:next(times))
        self.assertTrue(lib.closed)
    def test_environment_selected_bus_rejected_before_loading_library(self):
        with patch.dict(os.environ,{'DBUS_SYSTEM_BUS_ADDRESS':'unix:path=/tmp/fake'}), \
                patch('controller_firmware_sleep.os.name','posix'),patch('controller_firmware_sleep.c.CDLL') as loaded:
            with self.assertRaises(fw.FirmwareError):FirmwareInhibitor(lambda:1.0).acquire(2.0)
            loaded.assert_not_called()


if __name__=='__main__':unittest.main()

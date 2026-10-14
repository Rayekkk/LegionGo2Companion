# SPDX-License-Identifier: BSD-3-Clause
import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import controller_firmware as fw
import controller_firmware_journal as journal
import controller_firmware_transfer as transfer
import test_controller_firmware_transfer as transfer_tests

BINDING = transfer_tests.BINDING

BOOT = 'a1c2d3e4-1234-4321-abcd-0123456789ab'


def state(phase='idle'):
    value = journal._empty_snapshot()
    value['phase'] = phase
    if phase not in ('idle', 'ready_start', 'failed_before_write'):
        value.update(write_attempted=True, device_binding=BINDING, connection_epoch='connection-1', sequence=1)
    if phase in ('verifying_version', 'succeeded'):
        value.update(verification_token='b'*32, sent_bytes=value['total_bytes'], acknowledged_bytes=value['total_bytes'])
    return value


class JournalCodecTests(unittest.TestCase):
    def test_vendor_signed_positive_boundary_survives_real_journal_validation(self):
        value=state('succeeded');value['boundary']=0x7fffffff
        self.assertEqual(journal.recovered_record(journal.encode_record(value,BOOT))['snapshot']['boundary'],0x7fffffff)
        for bad in (0x80000000,0xffffffff):
            value['boundary']=bad
            with self.assertRaises(fw.FirmwareError):journal.encode_record(value,BOOT)
    def test_every_phase_recovers_as_display_only_with_no_resume(self):
        for phase in journal.PHASES:
            value=state(phase); before=copy.deepcopy(value)
            record=journal.recovered_record(journal.encode_record(value,BOOT))
            expected=phase if phase in transfer.TERMINAL else ('uncertain' if value['write_attempted'] else 'failed_before_write')
            self.assertEqual(record['snapshot']['phase'],expected)
            self.assertEqual(record['boot_id'],BOOT)
            self.assertEqual(value,before)
            self.assertEqual(set(record['snapshot']),journal.FIELDS)
            self.assertFalse(any(key in record for key in ('command','report','resume','retry')))

    def test_schema_identity_counter_and_completion_tampering_rejected(self):
        mutations=[('schema',True),('schema',2),('job_id','../job'),('component',[]),
            ('image_sha256','bad-digest'),('offered_version','not-a-version'),('total_bytes',1),
            ('phase',[]),('phase','flashing'),('write_attempted',1),('journal_error',0),
            ('sent_bytes',True),('sent_bytes',-1),('sent_bytes',999999),
            ('acknowledged_bytes',1),('sequence',1.5),('boundary',True),('boundary',0),
            ('device_binding',[]),('verification_token',[]),('error','\x00'),('error','X'*1025),
            ('connection_epoch','X'*257)]
        for key,new in mutations:
            value=state();value[key]=new
            with self.subTest(key=key,value=new),self.assertRaises(fw.FirmwareError):journal.encode_record(value,BOOT)
        for missing in journal.FIELDS:
            value=state();del value[missing]
            with self.assertRaises(fw.FirmwareError):journal.encode_record(value,BOOT)
        value=state();value['extra']=1
        with self.assertRaises(fw.FirmwareError):journal.encode_record(value,BOOT)
        for phase in ('idle','ready_start','failed_before_write'):
            value=state(phase);value['write_attempted']=True
            with self.assertRaises(fw.FirmwareError):journal.encode_record(value,BOOT)
        for key,new in (('write_attempted',False),('device_binding',''),('connection_epoch',''),
                ('sent_bytes',1),('acknowledged_bytes',1),('verification_token',''),('journal_error',True)):
            value=state('succeeded');value[key]=new
            with self.assertRaises(fw.FirmwareError):journal.encode_record(value,BOOT)

    def test_bounded_strict_json_and_boot_identity(self):
        valid=journal.encode_record(state(),BOOT)
        bad=[b'',b'X'*(journal.MAX_JOURNAL+1),b'\xff',b'[]',b'null',b'NaN',b'{',
            b'{"schema":1,"schema":1}',valid.replace(b'"schema": 1',b'"schema": NaN',1),
            valid.replace(b'"schema": 1',b'"schema": true',1),
            b'['*1001+b'0'+b']'*1001]
        for data in bad:
            with self.assertRaises(fw.FirmwareError):journal.recovered_record(data)
        for boot in ('',None,True,'secret','../boot',BOOT.upper()):
            with self.assertRaises(fw.FirmwareError):journal.encode_record(state(),boot)
        envelope=json.loads(valid);envelope['extra']=0
        with self.assertRaises(fw.FirmwareError):journal.recovered_record(json.dumps(envelope).encode())

    def test_windows_protected_filesystem_is_not_claimed_supported(self):
        with patch.object(journal.os,'name','nt'):
            with self.assertRaises(fw.FirmwareError):journal.FirmwareJournal('ignored',BOOT)


class OriginalSessionJournalTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('COMPANION_OFFICIAL_FW_FIXTURES'),'Original Lenovo BINs required')
    def test_real_image_core_checkpoints_encode_and_recover_without_replay(self):
        original=(Path(os.environ['COMPANION_OFFICIAL_FW_FIXTURES'])/'left-v26.03.10.01.bin').read_bytes()
        helper=transfer_tests.TransferTests(); records=[]
        session=transfer.TransferSession('left',original,helper.policy(1024),lambda value:records.append(journal.encode_record(value,BOOT)))
        commands,clock=helper.drive(session,boundary=1024)
        self.assertEqual(session.snapshot()['phase'],'awaiting_completion')
        self.assertEqual(journal.recovered_record(records[-1])['snapshot']['phase'],'uncertain')
        helper.complete(session,clock)
        self.assertEqual(journal.recovered_record(records[-1])['snapshot']['phase'],'succeeded')
        self.assertEqual(journal.recovered_record(records[0])['snapshot']['phase'],'failed_before_write')
        for data in records[1:-1]:
            self.assertEqual(journal.recovered_record(data)['snapshot']['phase'],'uncertain')
        self.assertEqual(len(commands),4+(len(original)+31)//32)


@unittest.skipUnless(os.name=='posix','Actual POSIX dirfd/flock/fsync security requires Linux; not emulated on Windows')
class PosixJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)/'private'
        self.directory.mkdir(mode=0o700)
    def tearDown(self):self.temp.cleanup()

    def test_private_atomic_record_and_cooperating_process_ownership(self):
        with journal.FirmwareJournal(self.directory,BOOT) as writer:
            self.assertIsNone(writer.load());writer.save(state('ready_block'))
            self.assertEqual(writer.load()['snapshot']['phase'],'uncertain')
            self.assertEqual(stat.S_IMODE((self.directory/'journal.json').stat().st_mode),0o600)
            script='import sys;from controller_firmware_journal import FirmwareJournal;FirmwareJournal(sys.argv[1],sys.argv[2])'
            process=subprocess.run([sys.executable,'-c',script,str(self.directory),BOOT],capture_output=True,timeout=5)
            self.assertNotEqual(process.returncode,0)
            self.assertIn(b'ownership could not be acquired',process.stderr)
        with journal.FirmwareJournal(self.directory,BOOT) as writer:
            writer.save(state('succeeded'));self.assertEqual(writer.load()['snapshot']['phase'],'succeeded')

    def test_existing_modes_symlinks_hardlinks_fifo_and_path_traversal_rejected(self):
        with self.assertRaises(fw.FirmwareError):journal.FirmwareJournal(self.directory.parent/'private'/'..'/'private',BOOT)
        self.directory.chmod(0o755)
        with self.assertRaises(fw.FirmwareError):journal.FirmwareJournal(self.directory,BOOT)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode),0o755);self.directory.chmod(0o700)
        alias=self.directory.parent/'alias';alias.symlink_to(self.directory,target_is_directory=True)
        with self.assertRaises(fw.FirmwareError):journal.FirmwareJournal(alias,BOOT)
        for kind in ('mode','symlink','hardlink','fifo'):
            lock=self.directory/'worker.lock';other=self.directory/'other'
            if kind=='fifo':os.mkfifo(lock,0o600)
            else:
                other.write_bytes(b'');other.chmod(0o600)
                if kind=='symlink':lock.symlink_to(other)
                elif kind=='hardlink':os.link(other,lock)
                else:lock.write_bytes(b'');lock.chmod(0o644)
            with self.assertRaises(fw.FirmwareError):journal.FirmwareJournal(self.directory,BOOT)
            lock.unlink()
            if other.exists():other.unlink()

    def test_unexpected_journal_objects_rejected_without_touching_target(self):
        with journal.FirmwareJournal(self.directory,BOOT) as writer:
            target=self.directory/'other';target.write_bytes(b'untouched');target.chmod(0o600)
            path=self.directory/'journal.json'
            for kind in ('symlink','hardlink','fifo','mode'):
                if kind=='symlink':path.symlink_to(target)
                elif kind=='hardlink':os.link(target,path)
                elif kind=='fifo':os.mkfifo(path,0o600)
                else:path.write_bytes(b'unsafe');path.chmod(0o644)
                with self.assertRaises((fw.FirmwareError,OSError)):writer.save(state())
                with self.assertRaises((fw.FirmwareError,OSError)):writer.load()
                self.assertEqual(target.read_bytes(),b'untouched');path.unlink()
            path.write_bytes(b'X'*(journal.MAX_JOURNAL+1));path.chmod(0o600)
            with self.assertRaises(fw.FirmwareError):writer.load()

    def test_failed_atomic_write_keeps_previous_record_and_cleans_temp(self):
        with journal.FirmwareJournal(self.directory,BOOT) as writer:
            writer.save(state());before=(self.directory/'journal.json').read_bytes()
            for call in ('write','fsync','replace'):
                with patch.object(journal.os,call,side_effect=OSError('injected failure')):
                    with self.assertRaises(OSError):writer.save(state('ready_block'))
                self.assertEqual((self.directory/'journal.json').read_bytes(),before)
                self.assertEqual(list(self.directory.glob('.journal-*')),[])
            actual_write=os.write
            with patch.object(journal.os,'write',side_effect=lambda fd,data:actual_write(fd,data[:3])):
                writer.save(state('succeeded'))
            self.assertEqual(writer.load()['snapshot']['phase'],'succeeded')

    def test_replaced_lock_and_closed_writer_are_rejected(self):
        writer=journal.FirmwareJournal(self.directory,BOOT)
        (self.directory/'worker.lock').unlink();(self.directory/'worker.lock').write_bytes(b'')
        (self.directory/'worker.lock').chmod(0o600)
        with self.assertRaises(fw.FirmwareError):writer.save(state())
        writer.close();writer.close()
        with self.assertRaises(fw.FirmwareError):writer.load()

    def test_process_exit_leaves_only_uncertain_display_record(self):
        script=('import os,sys;from controller_firmware_journal import FirmwareJournal;'
            'from test_controller_firmware_journal import state;'
            'writer=FirmwareJournal(sys.argv[1],sys.argv[2]);writer.save(state("ready_block"));os._exit(17)')
        environment=dict(os.environ,PYTHONPATH=str(Path(__file__).parent)+os.pathsep+str(Path(__file__).parents[1]))
        result=subprocess.run([sys.executable,'-c',script,str(self.directory),BOOT],env=environment,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,17,result.stderr)
        with journal.FirmwareJournal(self.directory,BOOT) as writer:
            self.assertEqual(writer.load()['snapshot']['phase'],'uncertain')

    def test_directory_fsync_failure_after_replace_is_reported_not_false_success(self):
        with journal.FirmwareJournal(self.directory,BOOT) as writer:
            actual=os.fsync
            def fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError('directory persistence not confirmed')
                return actual(fd)
            with patch.object(journal.os,'fsync',side_effect=fsync):
                with self.assertRaises(OSError):writer.save(state('ready_block'))
            self.assertEqual(writer.load()['snapshot']['phase'],'uncertain')
            self.assertEqual(list(self.directory.glob('.journal-*')),[])


if __name__=='__main__':unittest.main()

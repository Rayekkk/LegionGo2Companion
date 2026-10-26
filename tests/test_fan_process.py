# SPDX-License-Identifier: BSD-3-Clause
"""Real detached processes, IPC, SIGKILL and flock; model ports, never hardware.

Windows skips these POSIX cases. Run on Linux as well; mocks do not establish
physical fan response, which has separate device evidence.
"""
import errno
import json
import mmap
import os
import pathlib
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def model_worker(root, ipc_fd):
    import fan_guardian as guard
    import fcntl
    guard.RUNTIME = root
    guard.platform_check = lambda: {'bios_version': (root/'bios').read_text() if (root/'bios').exists() else 'RRCN16WW'}
    guard.FIRMWARE_FILE = root/'firmware'/'fan_firmware.json'
    if (root/'self-test-1s').exists():  # The real self-test with a 1 s deadline instead of 15 s.
        guard.SELF_TEST_S = 1
    real_validate_curve = guard.validate_curve
    def stalled_validate_curve(curve):  # A live guardian that stalls inside a configure, e.g. under memory pressure.
        if (root/'configure-stall').exists(): time.sleep(float((root/'configure-stall').read_text()))
        return real_validate_curve(curve)
    guard.validate_curve = stalled_validate_curve
    real_atomic = guard.atomic
    def atomic(path, data):  # A full /run tmpfs: the session records cannot be written.
        if (root/'enospc').exists() and pathlib.Path(path).name in ('reason.json', 'last.json'):
            raise OSError(errno.ENOSPC, 'No space left on device')
        return real_atomic(path, data)
    guard.atomic = atomic
    real_touch = pathlib.Path.touch
    def touch(path, *args, **kwargs):  # A /run whose inodes are exhausted: no new stop marker.
        if path.name == 'stop' and (root/'enospc-stop').exists():
            raise OSError(errno.ENOSPC, 'No space left on device')
        return real_touch(path, *args, **kwargs)
    pathlib.Path.touch = touch
    class FlakyEC(guard.EC):
        def release(self):  # A transient EC error for the first N releases.
            left = root/'release-fails'
            if left.exists() and int(left.read_text()) > 0:
                left.write_text(str(int(left.read_text())-1)); raise RuntimeError('Transient EC error')
            return super().release()
    guard.EC = FlakyEC
    from fan_sleep import Pause, Retry
    def check_owners():  # Hardware ownership has separate pure/live checks.
        if (root/'owners-slow').exists(): raise Retry('Raw-port ownership scan exceeded its time budget.')
    guard.check_owners = check_owners
    def ownership():
        fd=os.open(root/'owner.lock',os.O_CREAT|os.O_RDWR,0o600)
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException: os.close(fd); raise RuntimeError('Another Fan Control instance')
        return fd
    guard.secure_root=ownership
    model_sample={'temperature':45}
    class Model:
        def __init__(self):
            self.fd=os.open(root/'ports-model',os.O_RDWR)
            self.m=mmap.mmap(self.fd,65536)
            self.target_writes=0
        def read(self,port):
            if port==0x4E: return self.m[0]
            if self.m[0]==0x2E: return self.m[1]
            if self.m[1]==0x12 and self.m[2]*256+self.m[3]==0xC307:
                return model_sample['temperature']
            if self.m[1]==0x12 and self.m[2]*256+self.m[3]==0xC6C8 and (root/'misread-once').exists():
                try:(root/'misread-once').unlink();return self.m[0xC6C8]^0x55  # One disturbed port read.
                except FileNotFoundError:pass
            return {0x10:self.m[3],0x11:self.m[2]}.get(self.m[1],self.m[self.m[2]*256+self.m[3]])
        def write(self,port,value):
            if (root/'hang-io').exists() and os.getpid()==int((root/'worker-pid').read_text()):
                time.sleep(20)  # Backup must kill only this EC worker, then acquire flock.
            if port==0x4E: self.m[0]=value
            elif self.m[0]==0x2E: self.m[1]=value
            elif self.m[1]==0x10: self.m[3]=value
            elif self.m[1]==0x11: self.m[2]=value
            else:
                addr=self.m[2]*256+self.m[3]
                if addr in (0xC6C8,0xC6C9) and (root/'ignore-target').exists():
                    return  # A firmware that no longer takes the override.
                self.m[addr]=value
                if addr in (0xC6C8,0xC6C9):
                    override=self.m[0xC6C8]+self.m[0xC6C9]*256
                    # Instant synthetic response, explicitly not real fan physics.
                    effective=override if override>=1500 else 0
                    tach=int((root/'forced-tach').read_text()) if (root/'forced-tach').exists() else effective
                    self.m[0xC6C0:0xC6C2]=tach.to_bytes(2,'little')
                    self.m[0xC6C2:0xC6C4]=effective.to_bytes(2,'little')
                    for name,sig in (('kill-after-write',signal.SIGKILL),('term-after-write',signal.SIGTERM)):
                        if (root/name).exists() and os.getpid()==int((root/'worker-pid').read_text()):
                            self.target_writes+=1
                            if self.target_writes==int((root/name).read_text()):
                                os.kill(os.getpid(),sig)  # SIGKILL: no transaction finally.
        def close(self): self.m.close();os.close(self.fd)
    class Sensors:
        def __init__(self): pass
        def read(self):
            if (root/'invalid-sensor').exists(): raise RuntimeError('Invalid model sensor')
            if (root/'slow-sensor').exists(): raise Retry('Stale/slow APU temperature acquisition.')
            temp=int((root/'temperature').read_text()) if (root/'temperature').exists() else 100 if (root/'thermal').exists() else 45
            model_sample['temperature']=int((root/'ec-temperature').read_text()) if (root/'ec-temperature').exists() else temp
            gpu=int((root/'gpu-temperature').read_text()) if (root/'gpu-temperature').exists() else temp-1
            stamp=time.monotonic()
            self.last_sample=dict(cpu_temperature=temp,gpu_temperature=gpu,
                                  sensor_started_at=stamp,sensor_finished_at=stamp,acquisition_ms=0)
            return {'k10temp':temp,'amdgpu':gpu}
        def close(self): pass
    class Sleep:
        def __init__(self):
            if (root/'sleep-in-progress').exists():raise OSError(errno.EINPROGRESS,'Fan suspend protection is unavailable.')
            (root/('inhibit-'+str(os.getpid()))).touch()
        def check(self):
            if (root/'prepare-sleep').exists():raise Pause('Suspend requested before sleep')
        def close(self):
            # Assert release happened before the synthetic delay FD is closed.
            if self.maybe_active():raise RuntimeError('Inhibitor closed before EC automatic')
            (root/('inhibit-'+str(os.getpid()))).unlink(missing_ok=True)
        @staticmethod
        def maybe_active():return (root/'ports-model').read_bytes()[0xC6C8:0xC6CA]!=b'\0\0'
    import fan_policy
    from fan_policy import CurveControl,target_for,MIN_RPM
    production_grace=fan_policy.TACH_GRACE_S
    class TransportControl(CurveControl):
        """Bypass ramp only in explicit transport kill tests; never hardware."""
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            if (root/'bridge-transport').exists():self.target=MIN_RPM
        def step(self,curve,temperature,now):
            if (root/'bridge-transport').exists():self.target=target_for(curve,temperature);return self.target
            return super().step(curve,temperature,now)
        def is_stalled(self,rpm,now):  # The real stall check with a 1 s spin-up grace instead of 12 s.
            fan_policy.TACH_GRACE_S=1 if (root/'tach-grace-1s').exists() else production_grace
            return super().is_stalled(rpm,now)
    if (root/'stop-settle-1s').exists():  # The real stop check with a 1 s settle instead of 20 s.
        real_stop_timed_out=guard.stop_timed_out
        guard.stop_timed_out=lambda rpm,effective,elapsed:real_stop_timed_out(rpm,effective,elapsed+19)
    guard.Ports=Model;guard.Sensors=Sensors;guard.SleepSafety=Sleep;guard.CurveControl=TransportControl
    (root/'worker-pid').write_text(str(os.getpid()))
    ipc=socket.socket(fileno=ipc_fd)
    try: guard.serve(ipc)
    except Exception as exc:
        ipc.send(json.dumps(dict(kind='closed',active=False,automatic=False,error=str(exc),outcome=guard.outcome_of(exc))).encode())
    finally: ipc.close()


@unittest.skipUnless(sys.platform=='linux' and hasattr(os,'pidfd_open'), 'Linux process recovery test')
class FanProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='lg2-fan-model-')
        self.root=pathlib.Path(self.tmp.name)
        data=bytearray(65536);data[0:4]=bytes([0xAA,0x12,0xC6,0xC9])
        data[0xC300:0xC302]=bytes([1,16]);data[0xC307]=45
        (self.root/'ports-model').write_bytes(data)
        self.workers=[];self.sockets=[]
        state=self.start()
        self.assertEqual(state['kind'],'ready',state)
        self.initial=state

    def start(self,timeout=3):
        a,b=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        p=subprocess.Popen([sys.executable,'-u',__file__,'--worker',str(self.root),str(b.fileno())],
                           pass_fds=(b.fileno(),),start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        b.close();self.workers.append(p);self.sockets.append(a)
        self.p,self.ipc=p,a
        return self.receive_kind('ready',timeout,allow_closed=True)

    def receive_kind(self,kind,timeout,allow_closed=False):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if select.select([self.ipc],[],[],.1)[0]:
                try: raw=self.ipc.recv(16384)
                except ConnectionResetError:
                    raise AssertionError('IPC reset: '+(self.root/'last.json').read_text())
                if not raw: break
                state=json.loads(raw)
                if state['kind']==kind or allow_closed and state['kind']=='closed': return state
                if state['kind']=='closed':raise AssertionError('Controller closed: '+json.dumps(state))
        last=(self.root/'last.json').read_text() if (self.root/'last.json').exists() else ''
        raise AssertionError('Missing '+kind+': '+last)
    @staticmethod
    def until(condition,timeout,step=.05):
        """Poll a condition owned by another process (no event to wait on) up to `timeout` s."""
        end=time.monotonic()+timeout
        while not condition():
            if time.monotonic()>=end:return False
            time.sleep(step)
        return True

    def put(self,name,value):
        # Atomic like sysfs: write_text truncates first and the guardian may read an empty file.
        tmp=self.root/(name+'.tmp');tmp.write_text(value);os.replace(tmp,self.root/name)

    def configure(self,enabled=True,revision=1,curve=None):
        # A process/IPC fixture must not depend on the shipped Balanced template
        # continuing to request exactly 1800 RPM at the model's 45 degrees.
        # Template contents have their own policy tests.
        running_curve = [[0,1800],[20,1800],[35,1800],[50,1800],
                         [65,1800],[80,2000],[90,3000],[100,4800]]
        self.ipc.send(json.dumps(dict(op='configure',enabled=enabled,curve=curve if curve is not None else running_curve,revision=revision)).encode())
        return self.receive_kind('configured',2)

    def wait_release(self,timeout=5):
        import fcntl
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            try:
                value=json.loads((self.root/'last.json').read_text())
                # Observe selectors under the same lock as real transactions;
                # the backup may be performing its fresh confirmation reads.
                with (self.root/'ports-model').open('rb') as f:
                    fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    data=f.read()
                if self.p.poll() is not None and value.get('automatic') and value.get('reason') and data[0xC6C8:0xC6CA]==b'\0\0':
                    self.assertEqual(data[:4],bytes([0xAA,0x12,0xC6,0xC9]))
                    return value
            except (FileNotFoundError,json.JSONDecodeError,BlockingIOError): pass
            time.sleep(.05)
        raise AssertionError('No automatic release/recovered selectors')

    def tearDown(self):
        for sock in self.sockets: sock.close()
        for p in self.workers:
            try: p.wait(timeout=6)
            except subprocess.TimeoutExpired: p.kill();p.wait(timeout=3)
        # Backup may finish just after the worker's exit. Verify ownership is
        # actually free instead of deleting a live recovery process's files.
        import fcntl
        deadline=time.monotonic()+3
        with (self.root/'owner.lock').open('r+') as f:
            while True:
                try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>deadline: self.fail('Recovery ownership leaked')
                    time.sleep(.05)
        self.tmp.cleanup()

    def test_controller_eof_pauses_and_releases(self):
        st=self.configure();self.assertTrue(st['active']);self.assertEqual(st['target'],1800)
        self.assertEqual(st['override'],1800,'enable ACK must contain fresh target readback')
        self.ipc.close();self.assertEqual(self.wait_release()['outcome'],'pause')
        self.assertFalse((self.root/'last-fault.json').exists(),'a pause is not a fault')

    def test_silent_plugin_keeps_control_without_any_lease(self):
        # Desktop/Gaming switches can stall the plugin for seconds; the guardian runs the curve itself.
        # Silence past the backup's progress lease proves no plugin-side lease exists.
        from fan_policy import LEASE_S
        self.configure();end=time.monotonic()+LEASE_S+.5
        while time.monotonic()<end:
            if select.select([self.ipc],[],[],max(0,end-time.monotonic()))[0]:
                state=json.loads(self.ipc.recv(16384))
                self.assertNotEqual(state['kind'],'closed',state)
        state=self.receive_kind('telemetry',1.5)
        self.assertTrue(state['active']);self.assertEqual(state['override'],1800)
        self.assertIsNone(self.p.poll())

    def test_sigterm_mid_target_write_pauses_releases_and_restores_selectors(self):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        self.configure();(self.root/'term-after-write').write_text('1')
        self.ipc.send(json.dumps(dict(op='configure',enabled=True,curve=[[t,MAX_RPM] for t,_ in DEFAULT_CURVE],revision=2)).encode())
        st=self.wait_release();self.assertEqual(st['outcome'],'pause');self.assertIn('SIGTERM',st['reason'])
        self.assertEqual(self.p.wait(timeout=1),0)

    def test_sigterm_to_owner_and_backup_releases_and_leaves_no_process(self):
        st=self.configure();backup=st['backup_pid']
        os.kill(self.p.pid,signal.SIGTERM);os.kill(backup,signal.SIGTERM)
        self.assertEqual(self.wait_release()['outcome'],'pause')
        def gone():
            try:os.kill(backup,0)
            except ProcessLookupError:return True
            return False
        self.assertTrue(self.until(gone,3),'Backup survived SIGTERM')

    def test_sleep_pause_then_a_new_session_rearms(self):
        self.configure();(self.root/'prepare-sleep').touch()
        self.assertEqual(self.wait_release()['outcome'],'pause')
        (self.root/'prepare-sleep').unlink()
        self.assertEqual(self.start()['kind'],'ready')
        st=self.configure();self.assertTrue(st['active']);self.assertEqual(st['override'],1800)

    def test_startup_ownership_budget_is_retry_and_writes_nothing(self):
        self.ipc.close();self.wait_release();(self.root/'owners-slow').touch()
        st=self.start();self.assertEqual(st['kind'],'closed');self.assertEqual(st['outcome'],'retry')
        self.assertEqual((self.root/'ports-model').read_bytes()[0xC6C8:0xC6CA],b'\0\0')
        self.p.wait(timeout=3);self.assertFalse((self.root/st['session']).exists(),'no empty session dir is left behind')

    def test_start_during_sleep_preparation_is_pause_not_retry(self):
        # Seen on the device: re-arm attempts while logind is suspending must not spend the retry budget.
        self.ipc.close();self.wait_release();(self.root/'sleep-in-progress').touch()
        st=self.start();self.assertEqual(st['kind'],'closed');self.assertEqual(st['outcome'],'pause',st)
        self.assertEqual((self.root/'ports-model').read_bytes()[0xC6C8:0xC6CA],b'\0\0')

    def test_orphaned_override_is_released_at_session_start(self):
        self.ipc.close();self.wait_release()
        data=bytearray((self.root/'ports-model').read_bytes());data[0xC6C8:0xC6CA]=(1800).to_bytes(2,'little')
        (self.root/'ports-model').write_bytes(data)
        st=self.start();self.assertEqual(st['kind'],'ready',st)
        self.assertEqual(st['override'],0);self.assertTrue(st['automatic'])
        self.assertEqual((self.root/'ports-model').read_bytes()[0xC6C8:0xC6CA],b'\0\0')

    def set_ec(self,**values):
        data=bytearray((self.root/'ports-model').read_bytes())
        for addr,value in values.items():
            addr=int(addr[1:],16);raw=value.to_bytes(2 if addr==0xC6C8 else 1,'little');data[addr:addr+len(raw)]=raw
        (self.root/'ports-model').write_bytes(data)

    def new_bios(self):
        self.ipc.close();self.wait_release();self.put('bios','RRCN17WW')

    def override(self):
        return int.from_bytes((self.root/'ports-model').read_bytes()[0xC6C8:0xC6CA],'little')

    def test_new_bios_passes_the_self_test_once_then_only_the_read_only_checks_run(self):
        self.new_bios()
        st=self.start(6);self.assertEqual(st['kind'],'ready',st)
        self.assertEqual(st['override'],0);self.assertEqual(st['automatic_reads'],3)
        self.assertEqual(json.loads((self.root/'firmware'/'fan_firmware.json').read_text()),{'validated':[['RRCN17WW','1.16']]})
        self.ipc.close();self.wait_release()
        self.put('forced-tach','0')  # A second active check would now fail.
        self.assertEqual(self.start()['kind'],'ready')
        self.assertEqual(self.configure()['override'],1800)

    def refused_self_test(self):
        st=self.start(6);self.assertEqual(st['kind'],'closed',st);self.assertEqual(st['outcome'],'fault')
        self.assertEqual(st['error'],'This BIOS/EC version changed how the fan is controlled; Fan Control stays on automatic.')
        self.wait_release();self.assertFalse((self.root/'firmware'/'fan_firmware.json').exists())

    def test_new_bios_whose_tach_does_not_follow_is_released_and_refused(self):
        self.new_bios();(self.root/'self-test-1s').touch();self.put('forced-tach','800')
        self.refused_self_test()

    def test_new_bios_without_exact_override_readback_is_released_and_refused(self):
        self.new_bios();(self.root/'ignore-target').touch()
        self.refused_self_test()

    def test_new_ec_version_with_a_remembered_pair_skips_the_active_check(self):
        self.ipc.close();self.wait_release();self.set_ec(xC301=17)
        (self.root/'firmware').mkdir(mode=0o700);(self.root/'firmware'/'fan_firmware.json').write_text(json.dumps({'validated':[['RRCN16WW','1.17']]}))
        self.put('forced-tach','0')
        st=self.start();self.assertEqual(st['kind'],'ready',st);self.assertEqual(st['ec_version'],'1.17')

    def test_new_firmware_with_a_foreign_override_or_bad_reading_writes_nothing(self):
        for override,ec_temperature,text in ((1800,45,'Unknown fan override'),(3000,255,'Invalid EC temperature')):
            with self.subTest(text=text):
                if text=='Unknown fan override':self.new_bios()
                self.set_ec(xC6C8=override);self.put('ec-temperature',str(ec_temperature))
                st=self.start();self.assertEqual(st['kind'],'closed',st);self.assertIn(text,st['error'])
                self.assertEqual(st['outcome'],'fault');self.p.wait(timeout=3)
                self.assertEqual(self.override(),override,'an unproven interface gets no write')

    def test_ec_worker_sigkill_is_recovered_by_independent_backup(self):
        st=self.configure();self.assertNotEqual(st['backup_pid'],self.p.pid)
        backup_inhibitor=self.root/('inhibit-'+str(st['backup_pid']))
        # Each process holds its own sleep inhibitor: the owner's dies with the owner.
        self.assertTrue((self.root/('inhibit-'+str(self.p.pid))).exists());self.assertTrue(backup_inhibitor.exists())
        os.kill(self.p.pid,signal.SIGKILL);self.assertEqual(self.wait_release()['outcome'],'retry')
        self.assertTrue(self.until(lambda:not backup_inhibitor.exists(),2),'the backup closes its inhibitor only after the release')

    def released(self,timeout=5):
        return self.until(lambda:self.p.poll() is not None and self.override()==0,timeout)

    def test_unwritable_stop_marker_never_blocks_the_owners_own_release(self):
        self.configure();(self.root/'enospc-stop').touch()
        self.ipc.close();self.assertTrue(self.released(),'the owner is the only writer it has to stop')

    def test_unwritable_stop_marker_never_blocks_release_after_the_owner_died(self):
        st=self.configure();(self.root/'enospc-stop').touch()
        os.kill(self.p.pid,signal.SIGKILL);self.p.wait(timeout=2)
        self.until(lambda:self.override()==0,5)
        self.assertEqual(self.override(),0,'a backup that saw the owner die releases without the marker')

    def test_backup_retries_after_a_transient_ec_error_although_the_owner_is_gone(self):
        st=self.configure();(self.root/'release-fails').write_text('8')
        os.kill(self.p.pid,signal.SIGKILL);self.p.wait(timeout=2)
        self.until(lambda:self.override()==0,6)
        self.assertEqual(self.override(),0,'killing an already dead owner (ESRCH) must not end recovery')
        self.assertEqual((self.root/'release-fails').read_text(),'0')

    def test_full_runtime_dir_never_blocks_the_release(self):
        self.configure();(self.root/'enospc').touch()
        self.ipc.close();self.released()
        self.assertEqual(self.override(),0,'session records that cannot be written never keep the override')
        self.assertIsNotNone(self.p.poll())

    def test_stalled_transaction_owner_is_reaped_then_selectors_restored(self):
        self.configure();(self.root/'hang-io').touch()
        self.wait_release(timeout=7)
        self.assertEqual(self.p.wait(timeout=1),-signal.SIGKILL)

    def test_session_start_removes_dirs_left_by_killed_sessions(self):
        import fcntl
        st=self.configure();os.kill(st['backup_pid'],signal.SIGKILL);os.kill(self.p.pid,signal.SIGKILL);self.p.wait(timeout=2)
        stale=self.root/self.initial['session'];self.assertTrue(stale.is_dir())
        with (self.root/'owner.lock').open('r+') as f:  # The killed backup's exit frees ownership.
            end=time.monotonic()+3
            while True:
                try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>end:self.fail('Killed backup still owns recovery')
                    time.sleep(.05)
        st=self.start();self.assertEqual(st['kind'],'ready',st)
        self.assertFalse(stale.exists());self.assertTrue((self.root/st['session']).is_dir())

    def test_backup_death_causes_worker_to_release(self):
        st=self.configure();os.kill(st['backup_pid'],signal.SIGKILL)
        st=self.wait_release();self.assertIn('backup watchdog died',st['reason']);self.assertEqual(st['outcome'],'retry')

    def test_invalid_sensor_and_thermal_boundary_fail_closed(self):
        self.configure();(self.root/'invalid-sensor').touch();self.assertEqual(self.wait_release()['outcome'],'fault')

    def test_independent_ec_channel_does_not_reject_startup(self):
        self.ipc.close();self.wait_release()
        self.put('temperature','72')
        self.put('gpu-temperature','47')
        self.put('ec-temperature','40')
        ready=self.start()
        self.assertEqual(ready['kind'],'ready',ready)
        self.assertEqual(ready['ec_temperature'],40)
        state=self.configure()
        self.assertTrue(state['active']);self.assertFalse(state['automatic'])
        self.assertEqual(state['temperatures'],{'k10temp':72,'amdgpu':47})

    def test_independent_temperature_changes_keep_control_and_use_hotter_apu_channel(self):
        from fan_policy import DEFAULT_CURVE
        self.put('gpu-temperature','85')
        self.put('ec-temperature','45')
        state=self.configure(curve=DEFAULT_CURVE)
        self.assertTrue(state['active']);self.assertEqual(state['target'],4400)
        self.put('temperature','72')
        self.put('gpu-temperature','47')
        deadline=time.monotonic()+1.5
        while time.monotonic()<deadline:
            state=self.receive_kind('telemetry',1.5)
            if state.get('temperatures')=={'k10temp':72,'amdgpu':47}:break
        self.assertEqual(state['temperatures'],{'k10temp':72,'amdgpu':47})
        self.assertTrue(state['active']);self.assertEqual(state['ec_temperature'],45)
        self.assertFalse(state['automatic']);self.assertFalse((self.root/'last-fault.json').exists())

    def test_invalid_ec_value_preserves_actual_fault_sample_and_releases(self):
        self.configure()
        self.put('ec-temperature','255')
        self.wait_release()
        record=json.loads((self.root/'last-fault.json').read_text())
        self.assertEqual(record['outcome'],'fault');self.assertEqual(record['stage'],'ec_readback')
        self.assertEqual(record['sample']['ec_temperature'],255)
        self.assertEqual(record['sample']['cpu_temperature'],45)
        self.assertIn('sensor_started_at',record['sample'])

    def test_one_disturbed_override_read_is_read_again_and_keeps_control(self):
        self.configure();(self.root/'misread-once').touch()
        self.assertTrue(self.until(lambda:not (self.root/'misread-once').exists(),2))
        for _ in range(3):state=self.receive_kind('telemetry',1.5)  # The last one follows the second read.
        self.assertTrue(state['active']);self.assertEqual(state['override'],1800)
        self.assertIsNone(self.p.poll());self.assertFalse((self.root/'last-fault.json').exists())

    def poke(self,addr,data):
        """Another EC writer (firmware or a foreign tool), under the same lock as real transactions."""
        import fcntl
        with (self.root/'ports-model').open('r+b') as f:
            fcntl.flock(f,fcntl.LOCK_EX);f.seek(addr);f.write(data)

    def test_firmware_taking_the_fan_back_is_a_retry_and_a_fresh_session_rearms(self):
        self.configure();self.poke(0xC6C8,b'\0\0')
        st=self.wait_release();self.assertEqual(st['outcome'],'retry',st);self.assertIn('Firmware took the fan back',st['reason'])
        self.assertEqual(json.loads((self.root/'last-fault.json').read_text())['outcome'],'retry')
        self.assertEqual(self.start()['kind'],'ready')
        st=self.configure();self.assertTrue(st['active']);self.assertEqual(st['override'],1800)

    def test_foreign_override_write_is_a_fault_and_released(self):
        self.configure();self.poke(0xC6C8,(2400).to_bytes(2,'little'))
        st=self.wait_release();self.assertEqual(st['outcome'],'fault',st);self.assertIn('outside its owner',st['reason'])

    def test_100_hardmax_without_handover_or_heating_hardware(self):
        from fan_policy import DEFAULT_CURVE,MIN_RPM,LAST_MIN_RPM
        self.put('ec-temperature','45')
        self.configure(curve=[[t,MIN_RPM] for t,_ in DEFAULT_CURVE[:7]]+[[100,LAST_MIN_RPM]]);(self.root/'thermal').touch()
        end=time.monotonic()+1.5
        while time.monotonic()<end:
            state=self.receive_kind('telemetry',1)
            if state['target']==4800:
                self.assertTrue(state['active']);self.assertFalse(state['automatic']);self.assertEqual(state['override'],4800);return
        self.fail('No hard maximum')

    def test_duplicate_owner_cannot_arm_or_steal_override(self):
        active=self.configure();first=self.p
        st=self.start();self.assertEqual(st['kind'],'closed');self.assertIn('Another',st['error'])
        self.assertIsNone(first.poll())
        self.assertEqual(int.from_bytes((self.root/'ports-model').read_bytes()[0xC6C8:0xC6CA],'little'),
                         active['override'],'A second owner must not change either override byte')

    def test_explicit_stop_is_not_native_auto_and_disable_releases(self):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        curve=[[t,0 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]
        st=self.configure(curve=curve)
        self.assertEqual(st['target'],0);self.assertFalse(st['automatic'])
        self.assertEqual(st['override'],256,'stop ACK must not retain old native-zero telemetry')
        st=self.receive_kind('telemetry',1)
        self.assertEqual(st['override'],256);self.assertEqual(st['effective'],0)
        self.assertEqual(st['rpm'],0);self.assertTrue(st['stop_verified'])
        st=self.configure(False,2);self.assertTrue(st['automatic']);self.assertEqual(st['override'],0);self.assertFalse(st['stop_verified'])

    def test_repeated_saves_do_not_postpone_the_stop_check(self):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        self.ipc.close();self.wait_release()
        (self.root/'stop-settle-1s').touch();self.put('forced-tach','900')  # The rotor never stops.
        self.assertEqual(self.start()['kind'],'ready')
        stop=[[t,0 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]
        self.configure(curve=stop)
        for revision in range(2,10):  # Saves every 0.3 s for 2.4 s, well past the 1 s settle.
            time.sleep(.3)
            try:self.configure(curve=stop,revision=revision)
            except (AssertionError,OSError):break  # Closed by the stop check.
        else:self.fail('Repeated saves postponed the stop check')
        self.assertIn('Fan stop did not reach zero',self.wait_release()['reason'])

    def test_restart_at_minimum_never_commands_a_boost(self):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        stop=[[t,0 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]
        low=[[t,1500 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]
        self.configure(curve=stop)
        self.configure(curve=low,revision=2)
        targets=[];end=time.monotonic()+1.5
        while time.monotonic()<end:
            st=self.receive_kind('telemetry',1);targets.append(st['target'])
            self.assertIn(st['override'],(256,1500))
        self.assertEqual(targets[-1],1500)
        self.ipc.close();self.wait_release()

    def failed_start(self,tach):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        (self.root/'tach-grace-1s').touch();self.put('forced-tach',str(tach))
        st=self.configure(curve=[[t,1500 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)])
        self.assertEqual(st['rpm'],tach);self.assertEqual(st['override'],1500)
        end=time.monotonic()+15
        while self.p.poll() is None and time.monotonic()<end:
            if select.select([self.ipc],[],[],.1)[0]:
                try:raw=self.ipc.recv(16384)
                except ConnectionResetError:break
                if not raw:break
        result=self.wait_release()
        self.assertIn('stable running speed',result['reason']);self.assertEqual(result['outcome'],'fault')

    def test_no_start_releases_even_with_healthy_lease(self):self.failed_start(0)

    def test_underperforming_start_releases_even_with_healthy_lease(self):self.failed_start(800)

    def test_stopped_controller_sigkill_recovers_native_auto(self):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        self.configure(curve=[[t,0 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)])
        os.kill(self.p.pid,signal.SIGKILL);self.wait_release()

    def test_stopped_fan_restarts_on_warming_without_dwell(self):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        self.configure(curve=[[t,0 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]);self.put('temperature','99')
        end=time.monotonic()+1.5
        while time.monotonic()<end:
            st=self.receive_kind('telemetry',1)
            if st['target']>0:
                self.assertGreaterEqual(st['override'],1500);return
        self.fail('Warming stop did not restart')

    def test_stopped_frozen_worker_thermal_backup_releases(self):
        from fan_policy import DEFAULT_CURVE,LAST_MIN_RPM
        # The final point at its lowest speed must not weaken the backup's hard maximum.
        self.configure(curve=[[t,0 if i<7 else LAST_MIN_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]);self.receive_kind('telemetry',1)
        self.put('ec-temperature','45')
        os.kill(self.p.pid,signal.SIGSTOP);self.put('temperature','100')
        # Frozen worker cannot service heat; backup sets max before progress-lease recovery.
        self.assertTrue(self.until(lambda:self.override()==4800,1.5),'Independent backup did not impose hard maximum')
        self.wait_release();self.assertEqual(self.p.wait(timeout=1),-signal.SIGKILL)

    def test_prepare_sleep_releases_before_inhibitor_close(self):
        self.configure();(self.root/'prepare-sleep').touch();self.assertEqual(self.wait_release()['outcome'],'pause')
        self.until(lambda:not list(self.root.glob('inhibit-*')),1)
        self.assertEqual(list(self.root.glob('inhibit-*')),[])

    def crash_during_target_byte(self,position):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        self.configure();(self.root/'kill-after-write').write_text(str(position))
        self.ipc.send(json.dumps(dict(op='configure',enabled=True,curve=[[t,MAX_RPM] for t,_ in DEFAULT_CURVE],revision=2)).encode())
        self.wait_release();self.assertEqual(self.p.wait(timeout=1),-signal.SIGKILL)

    def test_first_target_byte_sigkill_backup_releases_and_restores_selectors(self):
        self.crash_during_target_byte(1)

    def test_second_target_byte_sigkill_backup_releases_and_restores_selectors(self):
        self.crash_during_target_byte(2)

    def bridge_crash(self,position):
        from fan_policy import DEFAULT_CURVE,MIN_RPM,MAX_RPM
        (self.root/'bridge-transport').touch()
        self.configure(curve=[[t,MIN_RPM if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)])
        (self.root/'kill-after-write').write_text(str(position))
        self.ipc.send(json.dumps(dict(op='configure',enabled=True,curve=[[t,MAX_RPM] for t,_ in DEFAULT_CURVE],revision=2)).encode())
        self.wait_release();self.assertEqual(self.p.wait(timeout=1),-signal.SIGKILL)

    def test_bridge_first_byte_sigkill_restores_original_selectors(self):self.bridge_crash(1)
    def test_bridge_second_byte_sigkill_restores_original_selectors(self):self.bridge_crash(2)
    def test_bridge_third_byte_sigkill_restores_original_selectors(self):self.bridge_crash(3)

    def test_stale_enable_after_disable_fails_closed(self):
        self.configure();self.configure(False,2)
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        self.ipc.send(json.dumps(dict(op='configure',enabled=True,curve=DEFAULT_CURVE,revision=1)).encode())
        self.wait_release()
        self.assertEqual((self.root/'ports-model').read_bytes()[0xC6C9],0)

    def real_client(self):
        # Standalone model invocation does not import Decky's real settings.
        if 'decky' not in sys.modules:
            import types
            sys.modules['decky']=types.SimpleNamespace(DECKY_PLUGIN_SETTINGS_DIR=str(self.root/'settings'))
        import fan_backend
        client=fan_backend.Client(runtime=self.root)
        client.process=self.p;client.socket=self.ipc;self.ipc.setblocking(False)
        client._accept(self.initial)
        return client

    def test_slow_settings_save_beside_real_monitor_keeps_control_then_unload_releases(self):
        import asyncio
        import copy
        import fan_policy
        from unittest.mock import patch
        client=self.real_client()
        import fan_backend
        plugin=fan_backend.Plugin();plugin._client=client;plugin._loaded=plugin._supported=True
        plugin._state=dict(fan_policy.defaults(),enabled=True)
        client.configure(True,fan_policy.DEFAULT_CURVE)
        plugin._applied=(True,json.dumps(fan_policy.DEFAULT_CURVE))
        def slow_save(value):
            time.sleep(fan_policy.LEASE_S+.5)  # Longer than the backup's progress lease.
            plugin._state=copy.deepcopy(value)
        async def run():
            plugin._task=asyncio.create_task(plugin._monitor())
            try:
                with patch.object(plugin,'_save',side_effect=slow_save):
                    result=await plugin.save_unit('percent',0)
                self.assertTrue(result['settings']['enabled'])
                self.assertTrue(result['telemetry']['active'])
                self.assertFalse(result['telemetry']['automatic'])
            finally:await plugin._unload()
        asyncio.run(run())
        self.assertTrue(client.snapshot()['automatic']);self.assertEqual(client.snapshot()['outcome'],'pause')
        self.assertEqual(self.p.wait(timeout=1),0)

    def ended(self,client):
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            try:client.pump()
            except RuntimeError:break
            time.sleep(.05)
        else:self.fail('Session did not end')
        client.close()
        return client.snapshot()

    def test_client_loss_of_worker_recovers_exact_session_as_retry(self):
        client=self.real_client()
        from fan_policy import DEFAULT_CURVE
        client.configure(True,DEFAULT_CURVE)
        try:
            os.kill(self.p.pid,signal.SIGKILL)
            state=self.ended(client)
            self.assertFalse(state['active']);self.assertTrue(state['automatic'])
            self.assertEqual(state['override'],0);self.assertEqual(state['automatic_reads'],3)
            self.assertIn('EC owner died',state['error']);self.assertEqual(state['outcome'],'retry')
            with self.assertRaises(RuntimeError):client.check()
            record=json.loads((self.root/'last-fault.json').read_text())
            self.assertEqual(record['session'],self.initial['session']);self.assertEqual(record['outcome'],'retry')
        finally:client.close()

    def test_configure_readback_timeout_of_a_stalled_live_guardian_is_a_retry(self):
        import asyncio
        from unittest.mock import patch
        import fan_policy
        client=self.real_client()
        import fan_backend
        client.configure(True,fan_policy.DEFAULT_CURVE)
        plugin=fan_backend.Plugin();plugin._client=client;plugin._loaded=plugin._supported=True
        plugin._state=dict(fan_policy.defaults(),enabled=True)
        # Past the 2 s readback deadline, inside the backup's 3 s lease: the guardian stays alive.
        self.put('configure-stall','2.5')
        with self.assertRaises(TimeoutError):client.configure(True,fan_policy.TEMPLATES['aggressive'])
        with patch.object(fan_backend,'release_orphan',side_effect=AssertionError('receipt proved automatic')):
            outcome,reason=asyncio.run(plugin._end_session(client.timeout))
        self.assertEqual((outcome,reason),('retry',str(client.timeout)))
        self.assertTrue(client.snapshot()['automatic']);self.assertIsNotNone(self.p.poll())
        self.assertEqual((self.root/'ports-model').read_bytes()[0xC6C8:0xC6CA],b'\0\0')

    def test_both_guardian_processes_killed_release_only_session_frees_override_then_off(self):
        import asyncio
        from unittest.mock import patch
        import fan_policy
        client=self.real_client()
        import fan_backend
        stop=[[t,0 if i<7 else fan_policy.MAX_RPM] for i,(t,_) in enumerate(fan_policy.DEFAULT_CURVE)]
        st=client.configure(True,stop)
        plugin=fan_backend.Plugin();plugin._client=client;plugin._loaded=plugin._supported=True
        plugin.BACKOFF_S,plugin.POLL_S=(.05,),.05  # The monitor's pacing is not under test, its decisions are.
        plugin._state=dict(fan_policy.defaults(),enabled=True,global_curve=stop);plugin._applied=(True,json.dumps(stop))
        os.kill(st['backup_pid'],signal.SIGKILL);os.kill(self.p.pid,signal.SIGKILL);self.p.wait(timeout=2)
        override=lambda:(self.root/'ports-model').read_bytes()[0xC6C8:0xC6CA]
        self.assertEqual(override(),(256).to_bytes(2,'little'),'nothing released the forced stop')
        model=fan_backend.Client
        def fresh_session():
            # The release-only session, started on the model ports instead of /usr/bin/python3.
            fresh=model(runtime=self.root);st=self.start();self.assertEqual(st['kind'],'ready',st)
            fresh.process=self.p;fresh.socket=self.ipc;self.ipc.setblocking(False);fresh._accept(st)
            fresh.start=lambda:None
            return fresh
        saved=[]
        async def run():
            with patch.object(plugin,'_save',side_effect=saved.append),patch.object(fan_backend,'Client',side_effect=fresh_session):
                await asyncio.wait_for(plugin._monitor(),20)
        asyncio.run(run())
        self.assertEqual(override(),b'\0\0')
        self.assertEqual(len(self.workers),2,'one release-only session, no re-arm')
        self.assertIn('restored by a release-only session',plugin._error)
        self.assertFalse(saved[-1]['enabled']);self.assertIsNone(plugin._client)

    def test_unload_releases_before_a_slow_settings_operation_drains(self):
        import asyncio
        import copy
        import threading
        from unittest.mock import patch
        import fan_policy
        client=self.real_client()
        import fan_backend
        plugin=fan_backend.Plugin();plugin._client=client;plugin._loaded=plugin._supported=True
        plugin._state=dict(fan_policy.defaults(),enabled=True)
        client.configure(True,fan_policy.DEFAULT_CURVE)
        entered=threading.Event();release=threading.Event()
        def save(value):
            entered.set();release.wait(7);plugin._state=copy.deepcopy(value)
        async def run():
            with patch.object(plugin,'_save',side_effect=save):
                request=asyncio.create_task(plugin.save_unit('percent',0))
                while not entered.is_set():await asyncio.sleep(.01)
                unload=asyncio.create_task(plugin._unload())
                try:
                    deadline=time.monotonic()+5
                    while self.p.poll() is None and time.monotonic()<deadline:await asyncio.sleep(.05)
                    self.assertIsNotNone(self.p.poll(),'withdrawal must not wait for settings I/O')
                    self.assertEqual((self.root/'ports-model').read_bytes()[0xC6C8:0xC6CA],b'\0\0')
                    self.assertFalse(unload.done())
                finally:
                    release.set();await request;await unload
        asyncio.run(run())
        self.assertTrue(client.snapshot()['automatic'])
        self.assertTrue(plugin._state['enabled'],'unload never writes the saved intent')

    def test_client_sensor_range_is_a_fault_and_sensor_timeout_is_a_retry(self):
        from fan_policy import DEFAULT_CURVE
        for marker,outcome,text in (('invalid-sensor','fault','Invalid model sensor'),('slow-sensor','retry','Stale/slow')):
            with self.subTest(outcome=outcome):
                if outcome=='retry':
                    self.initial=self.start();self.assertEqual(self.initial['kind'],'ready')
                client=self.real_client();client.configure(True,DEFAULT_CURVE)
                try:
                    (self.root/marker).touch()
                    state=self.ended(client);(self.root/marker).unlink()
                    self.assertFalse(state['active']);self.assertTrue(state['automatic'])
                    self.assertIn(text,state['error']);self.assertEqual(state['outcome'],outcome)
                    with self.assertRaises(RuntimeError):client.configure(True,DEFAULT_CURVE)
                    self.assertEqual((self.root/'ports-model').read_bytes()[0xC6C8:0xC6CA],b'\0\0')
                    record=json.loads((self.root/'last-fault.json').read_text())
                    self.assertIn(text,record['reason']);self.assertEqual(record['outcome'],outcome)
                finally:client.close()

    def test_fault_log_is_bounded_private_and_retains_first_cause(self):
        import fan_guardian
        from unittest.mock import patch
        with patch.object(fan_guardian,'RUNTIME',self.root):
            fan_guardian.record_fault('session-x','first sensor fault '+'x'*1000,'sensor','fault')
            fan_guardian.record_fault('session-x','later socket reset','ipc','retry')
        path=self.root/'last-fault.json';record=json.loads(path.read_text())
        self.assertTrue(record['reason'].startswith('first sensor fault'));self.assertEqual(len(record['reason']),512)
        self.assertEqual(record['stage'],'sensor');self.assertEqual(record['outcome'],'fault')
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertLess(path.stat().st_size,8192)

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--worker': model_worker(pathlib.Path(sys.argv[2]),int(sys.argv[3]))
    else: unittest.main()

# SPDX-License-Identifier: BSD-3-Clause
"""Real detached processes, IPC, SIGKILL and flock; model ports, never hardware.

Windows skips these POSIX cases. Run on Linux as well; mocks do not establish
physical fan response, which has separate device evidence.
"""
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
    guard.platform_check = lambda: {'model': True}
    guard.check_owners = lambda exclude=(): None  # Hardware ownership has separate pure/live checks.
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
            return {0x10:self.m[3],0x11:self.m[2]}.get(self.m[1],self.m[self.m[2]*256+self.m[3]])
        def write(self,port,value):
            if (root/'hang-io').exists() and os.getpid()==int((root/'worker-pid').read_text()):
                time.sleep(20)  # Backup must kill only this EC worker, then acquire flock.
            if port==0x4E: self.m[0]=value
            elif self.m[0]==0x2E: self.m[1]=value
            elif self.m[1]==0x10: self.m[3]=value
            elif self.m[1]==0x11: self.m[2]=value
            else:
                addr=self.m[2]*256+self.m[3];self.m[addr]=value
                if addr in (0xC6C8,0xC6C9):
                    override=self.m[0xC6C8]+self.m[0xC6C9]*256
                    # Instant synthetic response, explicitly not real fan physics.
                    effective=override if override>=1500 else 0
                    tach=int((root/'forced-tach').read_text()) if (root/'forced-tach').exists() else effective
                    self.m[0xC6C0:0xC6C2]=tach.to_bytes(2,'little')
                    self.m[0xC6C2:0xC6C4]=effective.to_bytes(2,'little')
                    if (root/'kill-after-write').exists() and os.getpid()==int((root/'worker-pid').read_text()):
                        self.target_writes+=1
                        if self.target_writes==int((root/'kill-after-write').read_text()):
                            os.kill(os.getpid(),signal.SIGKILL)  # No transaction finally.
        def close(self): self.m.close();os.close(self.fd)
    class Sensors:
        def __init__(self): pass
        def read(self):
            if (root/'invalid-sensor').exists(): raise RuntimeError('Invalid model sensor')
            temp=int((root/'temperature').read_text()) if (root/'temperature').exists() else 100 if (root/'thermal').exists() else 45
            model_sample['temperature']=temp
            return {'k10temp':temp,'amdgpu':temp-1}
        def close(self): pass
    class Sleep:
        def __init__(self):
            (root/('inhibit-'+str(os.getpid()))).touch()
        def check(self):
            if (root/'prepare-sleep').exists():raise RuntimeError('Suspend requested before sleep')
        def close(self):
            # Assert release happened before the synthetic delay FD is closed.
            if self.maybe_active():raise RuntimeError('Inhibitor closed before EC automatic')
            (root/('inhibit-'+str(os.getpid()))).unlink(missing_ok=True)
        @staticmethod
        def maybe_active():return (root/'ports-model').read_bytes()[0xC6C8:0xC6CA]!=b'\0\0'
    from fan_policy import CurveControl,target_for,MIN_RPM
    class TransportControl(CurveControl):
        """Bypass ramp only in explicit transport kill tests; never hardware."""
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            if (root/'bridge-transport').exists():self.target=MIN_RPM
        def step(self,curve,temperature,now):
            if (root/'bridge-transport').exists():self.target=target_for(curve,temperature);return self.target
            return super().step(curve,temperature,now)
    guard.Ports=Model;guard.Sensors=Sensors;guard.SleepSafety=Sleep;guard.CurveControl=TransportControl
    (root/'worker-pid').write_text(str(os.getpid()))
    ipc=socket.socket(fileno=ipc_fd)
    try: guard.serve(ipc)
    except Exception as exc:
        ipc.send(json.dumps(dict(kind='closed',active=False,automatic=False,error=str(exc))).encode())
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

    def start(self):
        a,b=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        p=subprocess.Popen([sys.executable,'-u',__file__,'--worker',str(self.root),str(b.fileno())],
                           pass_fds=(b.fileno(),),start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        b.close();self.workers.append(p);self.sockets.append(a)
        self.p,self.ipc=p,a
        return self.receive_kind('ready',3,allow_closed=True)

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

    def configure(self,enabled=True,revision=1,curve=None):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        self.ipc.send(json.dumps(dict(op='heartbeat')).encode())
        self.ipc.send(json.dumps(dict(op='configure',enabled=enabled,curve=curve if curve is not None else DEFAULT_CURVE,revision=revision)).encode())
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

    def test_controller_eof_releases_without_heartbeat_or_finally(self):
        st=self.configure();self.assertTrue(st['active']);self.assertEqual(st['target'],1800)
        self.assertEqual(st['override'],1800,'enable ACK must contain fresh target readback')
        self.ipc.close();self.wait_release()

    def test_plugin_lease_expiration_releases(self):
        self.configure();st=self.wait_release();self.assertIn('heartbeat',st['reason'])

    def test_ec_worker_sigkill_is_recovered_by_independent_backup(self):
        st=self.configure();self.assertNotEqual(st['backup_pid'],self.p.pid)
        os.kill(self.p.pid,signal.SIGKILL);self.wait_release()

    def test_stalled_transaction_owner_is_reaped_then_selectors_restored(self):
        self.configure();(self.root/'hang-io').touch()
        self.wait_release(timeout=7)
        self.assertEqual(self.p.wait(timeout=1),-signal.SIGKILL)

    def test_backup_death_causes_worker_to_release(self):
        st=self.configure();os.kill(st['backup_pid'],signal.SIGKILL)
        st=self.wait_release();self.assertIn('backup watchdog died',st['reason'])

    def test_invalid_sensor_and_thermal_boundary_fail_closed(self):
        self.configure();(self.root/'invalid-sensor').touch();self.wait_release()

    def test_100_hardmax_without_handover_or_heating_hardware(self):
        self.configure();(self.root/'thermal').touch()
        end=time.monotonic()+1.5
        while time.monotonic()<end:
            self.ipc.send(json.dumps(dict(op='heartbeat')).encode())
            state=self.receive_kind('telemetry',1)
            if state['target']==4800:
                self.assertTrue(state['active']);self.assertFalse(state['automatic']);self.assertEqual(state['override'],4800);return
        self.fail('No hard maximum')

    def test_duplicate_owner_cannot_arm_or_steal_override(self):
        self.configure();first=self.p
        st=self.start();self.assertEqual(st['kind'],'closed');self.assertIn('Another',st['error'])
        self.assertIsNone(first.poll())
        self.assertEqual((self.root/'ports-model').read_bytes()[0xC6C9],1800>>8)

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

    def test_restart_at_minimum_never_commands_a_boost(self):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        stop=[[t,0 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]
        low=[[t,1500 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]
        self.configure(curve=stop)
        self.configure(curve=low,revision=2)
        targets=[];end=time.monotonic()+1.5
        while time.monotonic()<end:
            self.ipc.send(json.dumps(dict(op='heartbeat')).encode())
            st=self.receive_kind('telemetry',1);targets.append(st['target'])
            self.assertIn(st['override'],(256,1500))
        self.assertEqual(targets[-1],1500)
        self.ipc.close();self.wait_release()

    def failed_start(self,tach):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        (self.root/'forced-tach').write_text(str(tach))
        st=self.configure(curve=[[t,1500 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)])
        self.assertEqual(st['rpm'],tach);self.assertEqual(st['override'],1500)
        end=time.monotonic()+15
        while self.p.poll() is None and time.monotonic()<end:
            try:self.ipc.send(json.dumps(dict(op='heartbeat')).encode())
            except (BrokenPipeError,ConnectionResetError):break
            if select.select([self.ipc],[],[],.1)[0]:
                try:raw=self.ipc.recv(16384)
                except ConnectionResetError:break
                if not raw:break
        result=self.wait_release()
        self.assertIn('stable running speed',result['reason'])

    def test_no_start_releases_even_with_healthy_lease(self):self.failed_start(0)

    def test_underperforming_start_releases_even_with_healthy_lease(self):self.failed_start(800)

    def test_stopped_controller_sigkill_recovers_native_auto(self):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        self.configure(curve=[[t,0 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)])
        os.kill(self.p.pid,signal.SIGKILL);self.wait_release()

    def test_stopped_fan_restarts_on_warming_without_dwell(self):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        self.configure(curve=[[t,0 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]);(self.root/'temperature').write_text('99')
        end=time.monotonic()+1.5
        while time.monotonic()<end:
            st=self.receive_kind('telemetry',1)
            if st['target']>0:
                self.assertGreaterEqual(st['override'],1500);return
        self.fail('Warming stop did not restart')

    def test_stopped_frozen_worker_thermal_backup_releases(self):
        from fan_policy import DEFAULT_CURVE,MAX_RPM
        self.configure(curve=[[t,0 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]);self.receive_kind('telemetry',1)
        os.kill(self.p.pid,signal.SIGSTOP);(self.root/'temperature').write_text('100')
        # Frozen worker cannot service heat; backup sets max before progress-lease recovery.
        end=time.monotonic()+1.5
        while time.monotonic()<end:
            data=(self.root/'ports-model').read_bytes()
            if int.from_bytes(data[0xC6C8:0xC6CA],'little')==4800:break
            time.sleep(.05)
        else:self.fail('Independent backup did not impose hard maximum')
        self.wait_release();self.assertEqual(self.p.wait(timeout=1),-signal.SIGKILL)

    def test_prepare_sleep_releases_before_inhibitor_close(self):
        self.configure();(self.root/'prepare-sleep').touch();self.wait_release()
        end=time.monotonic()+1
        while list(self.root.glob('inhibit-*')) and time.monotonic()<end:time.sleep(.05)
        self.assertEqual(list(self.root.glob('inhibit-*')),[])

    def test_frozen_owner_prepare_sleep_is_recovered_by_backup(self):
        self.configure();os.kill(self.p.pid,signal.SIGSTOP);(self.root/'prepare-sleep').touch()
        self.wait_release();self.assertEqual(self.p.wait(timeout=1),-signal.SIGKILL)

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


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--worker': model_worker(pathlib.Path(sys.argv[2]),int(sys.argv[3]))
    else: unittest.main()

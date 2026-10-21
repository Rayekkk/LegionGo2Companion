# SPDX-License-Identifier: BSD-3-Clause
import copy
import os
import unittest
from unittest.mock import patch
import fan_ec
from fan_ec import EC, platform_check, transition_bytes
from fan_policy import MIN_RPM, MAX_RPM, STOP_OVERRIDE, encode_target


class ModelPorts:
    """Register/selector model, with interruption at any individual IO operation."""
    def __init__(self):
        self.primary, self.secondary, self.hi, self.lo = 0xAA, 0x12, 0xC6, 0xC9
        self.mem = {0xC300:1,0xC301:16,0xC307:45}
        self.operations = 0; self.fail_at = None; self.writes = []

    def interrupt(self):
        self.operations += 1
        if self.operations == self.fail_at:
            raise OSError('Injected EC interruption')

    def data_port(self, port):
        # Only the 0x4E/0x4F pair exists; its data port means something only behind the EC selectors.
        if port not in (0x4E, 0x4F) or port == 0x4F and self.primary not in (0x2E, 0x2F):
            raise AssertionError(f'I/O port {port:#x} accessed with primary selector {self.primary:#x}')

    def read(self, port):
        self.interrupt(); self.data_port(port)
        if port == 0x4E: return self.primary
        if self.primary == 0x2E: return self.secondary
        return {0x10:self.lo,0x11:self.hi}.get(self.secondary, self.mem.get(self.hi<<8|self.lo,0))

    def write(self, port, value):
        self.interrupt(); self.data_port(port)
        if port == 0x4E: self.primary = value
        elif self.primary == 0x2E: self.secondary = value
        elif self.secondary == 0x10: self.lo = value
        elif self.secondary == 0x11: self.hi = value
        else:
            addr = self.hi<<8|self.lo
            self.mem[addr]=value; self.writes.append((addr,value))


class FanTransportTests(unittest.TestCase):
    def test_every_pair_has_bounded_byte_intermediates_and_stop_is_not_auto(self):
        targets=[STOP_OVERRIDE]+list(range(MIN_RPM,MAX_RPM+1,100))+[1501,1535,1536,3073,4607,4608,4799]
        for current in [0]+targets:
            for target in targets:
                value=current
                for addr,byte in transition_bytes(current,target):
                    value=(byte<<8|value&255) if addr==0xC6C9 else (value&0xff00|byte)
                    if current>=MIN_RPM and target>=MIN_RPM:
                        self.assertTrue(MIN_RPM<=value<=MAX_RPM,(current,target,value))
                    else:
                        self.assertTrue(0<value<=MAX_RPM,(current,target,value))
                self.assertEqual(value,target)
        self.assertEqual(encode_target(0),256)

    def test_hundred_rpm_transitions_read_back_and_restore_selectors(self):
        p=ModelPorts(); e=EC(p); e.snapshot()
        for rpm in list(range(MIN_RPM,MAX_RPM+1,100))+[1501,1502,1791,3073,4351,4799,0]+list(range(MAX_RPM,MIN_RPM-1,-100)):
            e.set_target(rpm,lambda:False)
            self.assertEqual(e.word(0xC6C8),encode_target(rpm))
        self.assertTrue(all(a in (0xC6C8,0xC6C9) for a,v in p.writes))
        e.release(); self.assertEqual(e.word(0xC6C8),0)
        e.restore(); self.assertEqual((p.primary,p.secondary,p.hi,p.lo),(0xAA,0x12,0xC6,0xC9))

    def test_interruption_at_every_io_position_recovers_original_selectors_and_releases(self):
        p=ModelPorts(); e=EC(p); baseline=copy.deepcopy(e.snapshot()); p.operations=0
        for old,rpm in [(0,4000),(1500,1600),(4700,4800),(4800,4700),(1500,4800),(4800,1500),(1500,0),(STOP_OVERRIDE,1500)]:
            p=ModelPorts();p.mem.update({0xC6C8:old&255,0xC6C9:old>>8});e=EC(p);e.baseline=baseline
            e.set_target(rpm,lambda:False);e.status();e.restore();count=p.operations
            for i in range(1,count+1):
                p=ModelPorts();p.mem.update({0xC6C8:old&255,0xC6C9:old>>8});e=EC(p);e.baseline=baseline;p.fail_at=i
                try: e.set_target(rpm,lambda:False); e.status(); e.restore()
                except OSError: pass
                p.fail_at=None;e.release();e.restore()
                self.assertEqual(e.word(0xC6C8),0,f'{old}->{rpm} operation {i}')
                e.restore(); self.assertEqual((p.primary,p.secondary,p.hi,p.lo),(0xAA,0x12,0xC6,0xC9),f'operation {i}')

    def test_no_late_write_or_arbitrary_ec_data(self):
        p=ModelPorts(); e=EC(p)
        for rpm in [100,1499,4801,5300,10000,True,1500.5]:
            with self.assertRaises(ValueError): e.set_target(rpm,lambda:False)
        with self.assertRaises(RuntimeError): e.set_target(4000,lambda:True)
        with self.assertRaises(ValueError): e._write_byte(0xC300,1)
        self.assertEqual(p.writes,[])
        # Withdraw at the data-write phase, rather than at an assumed Nth
        # lease check. Additional harmless preflight checks must stay valid.
        expired=False
        write=e._write_byte
        def withdraw_before_data(*args,**kwargs):
            nonlocal expired
            expired=True
            return write(*args,**kwargs)
        with patch.object(e,'_write_byte',side_effect=withdraw_before_data), \
                self.assertRaises(RuntimeError):
            e.set_target(4000,lambda:expired)
        self.assertTrue(expired,'The late-withdrawal fault must reach the data-write phase')
        self.assertEqual(p.writes,[])
        p.mem[0xC6C8]=1
        with self.assertRaises(RuntimeError): e.set_target(4000,lambda:False)

    def test_terminal_stop_between_both_bytes_prevents_second_write(self):
        p=ModelPorts();p.mem.update({0xC6C8:1500&255,0xC6C9:1500>>8});e=EC(p)
        with self.assertRaises(RuntimeError):
            e.set_target(1600,lambda:bool(p.writes))
        self.assertEqual(len(p.writes),1)
        intermediate = e.word(0xC6C8)
        self.assertTrue(MIN_RPM <= intermediate <= MAX_RPM,
                        'An interrupted byte sequence must leave a safe running request')
        self.assertNotEqual(intermediate,1600,'The withdrawn second write must not occur')
        e.release();self.assertEqual(e.word(0xC6C8),0)

    def test_firmware_automatic_is_reached_only_by_release(self):
        for current in (0, STOP_OVERRIDE, MIN_RPM, MAX_RPM):
            with self.assertRaises(RuntimeError): transition_bytes(current, 0)

    def test_release_from_a_running_speed_never_passes_through_a_stop(self):
        for old in (1728, 3000, MAX_RPM):  # 1728 = 0x06C0, the lowest high byte released low byte first.
            p=ModelPorts();p.mem.update({0xC6C8:old&255,0xC6C9:old>>8});e=EC(p)
            e.release()
            value=old; seen=[]
            for addr,byte in p.writes:
                value=(byte<<8|value&255) if addr==0xC6C9 else (value&0xff00|byte); seen.append(value)
            self.assertEqual(seen[-1],0)
            self.assertTrue(all(v==0 or MIN_RPM<=v<=MAX_RPM for v in seen),(old,seen))

    def test_snapshot_accepts_any_ec_selector_and_leaves_a_foreign_one_untouched(self):
        for secondary in (0x10,0x11,0x12):
            p=ModelPorts();p.secondary=secondary;e=EC(p)
            self.assertEqual(e.snapshot()['secondary'],secondary)
            self.assertEqual((p.primary,p.secondary,p.hi,p.lo),(0xAA,secondary,0xC6,0xC9))
        p=ModelPorts();p.secondary=0x07
        with self.assertRaisesRegex(RuntimeError,'Unknown initial EC selector'): EC(p).snapshot()
        self.assertEqual(p.primary,0xAA,'the firmware must get its own primary selector back')
        self.assertEqual(p.writes,[])

    @unittest.skipUnless(hasattr(os,'pread'),'POSIX positional I/O')
    def test_port_io_touches_exactly_one_port(self):
        import tempfile
        from fan_ec import Ports
        with tempfile.TemporaryFile() as dev:
            dev.write(bytes(range(0x50)))
            dev.flush()
            ports=object.__new__(Ports);ports.fd=dev.fileno()
            self.assertEqual(ports.read(0x4E),0x4E)
            ports.write(0x4F,0xA5)
            dev.seek(0);self.assertEqual(dev.read(),bytes(range(0x4F))+b'\xa5')
            self.assertEqual(ports.read(0x4F),0xA5)

    @unittest.skipUnless(hasattr(os,'pread'),'POSIX flock')
    def test_a_transaction_snapshots_then_restores_the_firmware_selectors(self):
        import tempfile
        with tempfile.TemporaryFile() as lock:
            p=ModelPorts();p.fd=lock.fileno();e=EC(p)
            with e.transaction(): e.set_target(2000,lambda:False)
            self.assertEqual((p.primary,p.secondary,p.hi,p.lo),(0xAA,0x12,0xC6,0xC9))
            self.assertEqual(p.mem[0xC6C9]<<8|p.mem[0xC6C8],2000)

    def test_platform_is_the_exact_model_with_any_bios_or_ec_version(self):
        dmi={'sys_vendor':'LENOVO','product_name':'83N0','product_version':'Legion Go 8ASP2',
             'bios_version':'RRCN16WW','ec_firmware_release':'1.16','board_name':'LNVNB161216'}
        with patch('fan_ec.platform.system',return_value='Linux'), patch('fan_ec.platform.machine',return_value='x86_64'), \
                patch('fan_ec.pathlib.Path.read_text',lambda p:dmi[p.name]):
            self.assertEqual(platform_check()['bios_version'],'RRCN16WW')
            dmi.update(bios_version='RRCN99WW',ec_firmware_release='2.03')  # A Lenovo update keeps the model.
            self.assertEqual(platform_check()['bios_version'],'RRCN99WW')
            for name in ('sys_vendor','product_name','product_version','board_name'):
                value=dmi[name]; dmi[name]=value+'-unknown'
                with self.assertRaises(RuntimeError): platform_check()
                dmi[name]=value
            dmi.update(board_name='LNVNB000000')
            with self.assertRaises(RuntimeError): platform_check()  # Only the 83N1 stands in its BIOS line.
        dmi.update(board_name='LNVNB161216')
        for system,machine in (('Linux','aarch64'),('Windows','x86_64')):
            with patch('fan_ec.platform.system',return_value=system), patch('fan_ec.platform.machine',return_value=machine), \
                    patch('fan_ec.pathlib.Path.read_text',lambda p:dmi[p.name]), self.assertRaises(RuntimeError):
                platform_check()

    def test_the_go_2_z2_is_admitted_as_beta_on_its_own_bios_line_only(self):
        dmi={'sys_vendor':'LENOVO','product_name':'83N1','product_version':'Legion Go 8AHP2',
             'bios_version':'RQCN17WW','board_name':'LNVNB000000'}  # Its board name is unknown: any.
        with patch('fan_ec.platform.system',return_value='Linux'), patch('fan_ec.platform.machine',return_value='x86_64'), \
                patch('fan_ec.pathlib.Path.read_text',lambda p:dmi[p.name]):
            identity=platform_check()
            self.assertEqual((identity['product_name'],identity['bios_version']),('83N1','RQCN17WW'))
            self.assertIn(identity['product_name'],fan_ec.BETA_MODELS)
            self.assertNotIn('83N0',fan_ec.BETA_MODELS)
            for name,value in (('product_version','Legion Go 8ASP2'),('bios_version','RRCN16WW'),
                               ('bios_version','XRQCN17WW'),('sys_vendor','OTHER'),('product_name','83E1')):
                with self.subTest(name=name,value=value):
                    original=dmi[name]; dmi[name]=value
                    with self.assertRaises(RuntimeError): platform_check()
                    dmi[name]=original

    def test_any_ec_version_reads_but_a_change_within_the_session_fails(self):
        p=ModelPorts();p.mem.update({0xC300:2,0xC301:3});e=EC(p)
        self.assertEqual(e.plausible()['ec_version'],'2.3')
        p.mem[0xC301]=4
        with self.assertRaisesRegex(RuntimeError,'EC version changed'): e.status()
        self.assertEqual(p.writes,[])

    def test_plausibility_rejects_unstable_or_implausible_readings_without_writes(self):
        for addr,values in ((0xC6C8,[0,0x08]),(0xC307,[45,60])):  # Override appears; temperature jumps 15 °C.
            with self.subTest(addr=hex(addr)):
                p=ModelPorts();e=EC(p);real=e.status;sequence=iter(values)
                def status():
                    p.mem[addr]=next(sequence);return real()
                with patch.object(e,'status',side_effect=status), self.assertRaisesRegex(RuntimeError,'unstable'):
                    e.plausible()
                self.assertEqual(p.writes,[])
        p=ModelPorts();p.mem.update({0xC6C0:0x10,0xC6C1:0x30});e=EC(p)  # 12304 RPM is not a fan reading.
        with self.assertRaisesRegex(RuntimeError,'Implausible'): e.plausible()
        self.assertEqual(p.writes,[])

    @unittest.skipUnless(hasattr(os,'O_NOFOLLOW'),'POSIX file checks')
    def test_remembered_firmware_is_read_only_from_our_own_regular_file(self):
        import json, pathlib, tempfile
        import fan_guardian
        with tempfile.TemporaryDirectory() as raw, patch.object(fan_guardian,'FIRMWARE_FILE',pathlib.Path(raw)/'fw'/'fan_firmware.json'):
            path=fan_guardian.FIRMWARE_FILE;known={('RRCN16WW','1.16')}
            self.assertEqual(fan_guardian.validated_firmware(),known)
            fan_guardian.remember_firmware(('RRCN17WW','1.17'))
            self.assertEqual(json.loads(path.read_text()),{'validated':[['RRCN17WW','1.17']]})
            self.assertEqual(path.stat().st_mode&0o777,0o600);self.assertEqual(path.parent.stat().st_mode&0o777,0o700)
            self.assertIn(('RRCN17WW','1.17'),fan_guardian.validated_firmware())
            for damaged in ('{"validated": [["RRCN17WW", 1]]}','[]','not json'):
                path.write_text(damaged);self.assertEqual(fan_guardian.validated_firmware(),known)
            other=path.parent/'other.json';other.write_text('{"validated": [["RRCN17WW", "1.17"]]}')
            path.unlink();path.symlink_to(other)
            self.assertEqual(fan_guardian.validated_firmware(),known,'a symlink is not our file')
            path.unlink();path.write_text('{"validated": [["RRCN17WW", "1.17"]]}')
            with patch.object(fan_guardian.os,'geteuid',return_value=os.geteuid()+1):
                self.assertEqual(fan_guardian.validated_firmware(),known,'a foreign owner (deck after a Decky chown) is not trusted')
                fan_guardian.remember_firmware(('RRCN18WW','1.18'))
            os.chmod(path.parent,0o770)
            self.assertEqual(fan_guardian.validated_firmware(),known,'a directory others can write is not trusted')
            fan_guardian.remember_firmware(('RRCN18WW','1.18'))
            self.assertNotIn('RRCN18WW',path.read_text())
            os.chmod(path.parent,0o700)
            self.assertIn(('RRCN17WW','1.17'),fan_guardian.validated_firmware())

    def test_remembered_pairs_live_outside_the_decky_settings_tree(self):
        # Decky re-chowns DECKY_PLUGIN_SETTINGS_DIR to deck on every load, which made the root-only check drop the pairs.
        import pathlib
        import fan_guardian
        self.assertEqual(fan_guardian.FIRMWARE_FILE,pathlib.Path('/var/lib/legiongo2companion-fan/fan_firmware.json'))

    def test_transient_ec_access_errors_are_retries_and_readback_mismatch_is_a_fault(self):
        from fan_guardian import outcome_of
        class Interfering(ModelPorts):
            def read(self,port):
                value=super().read(port)
                return value^1 if port==0x4E else value  # Another 0x4E/0x4F user moved the selector.
        with self.assertRaisesRegex(RuntimeError,'selector interference') as caught: EC(Interfering()).byte(0xC300)
        self.assertEqual(outcome_of(caught.exception),'retry')
        e=EC(ModelPorts());changing=iter(range(9))
        with patch.object(e,'byte',side_effect=lambda addr:next(changing)), self.assertRaisesRegex(RuntimeError,'Unstable EC word') as caught:
            e.word(0xC6C0)
        self.assertEqual(outcome_of(caught.exception),'retry')
        if hasattr(os,'pread'):
            import tempfile
            from fan_ec import Ports
            with tempfile.TemporaryFile() as empty:
                ports=object.__new__(Ports);ports.fd=empty.fileno()
                with self.assertRaisesRegex(RuntimeError,'Short EC port read') as caught: ports.read(0x4E)
            self.assertEqual(outcome_of(caught.exception),'retry')
        e=EC(ModelPorts())
        with patch.object(e,'byte',return_value=0), self.assertRaisesRegex(RuntimeError,'readback mismatch') as caught:
            e._write_byte(0xC6C8,7)
        self.assertEqual(outcome_of(caught.exception),'fault','a write readback mismatch is hardware evidence')

# SPDX-License-Identifier: BSD-3-Clause
import copy
import unittest
from unittest.mock import patch
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

    def read(self, port):
        self.interrupt()
        if port == 0x4E: return self.primary
        if self.primary == 0x2E: return self.secondary
        return {0x10:self.lo,0x11:self.hi}.get(self.secondary, self.mem.get(self.hi<<8|self.lo,0))

    def write(self, port, value):
        self.interrupt()
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
        for rpm in list(range(MIN_RPM,MAX_RPM+1,100))+[1501,1502,3073,4799,0]+list(range(MAX_RPM,MIN_RPM-1,-100)):
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
        checks=iter([False,True])
        with self.assertRaisesRegex(RuntimeError,'immediately before'):
            e.set_target(4000,lambda:next(checks))
        self.assertEqual(p.writes,[])
        p.mem[0xC6C8]=1
        with self.assertRaises(RuntimeError): e.set_target(4000,lambda:False)

    def test_terminal_stop_between_both_bytes_prevents_second_write(self):
        p=ModelPorts();p.mem.update({0xC6C8:1500&255,0xC6C9:1500>>8});e=EC(p)
        checks=iter([False,False,True])
        with self.assertRaisesRegex(RuntimeError,'immediately before'):
            e.set_target(1600,lambda:next(checks))
        self.assertEqual(len(p.writes),1)
        self.assertEqual(e.word(0xC6C8),1756)
        e.release();self.assertEqual(e.word(0xC6C8),0)

    def test_platform_is_exact_not_substring_or_bios_guess(self):
        expected={'sys_vendor':'LENOVO','product_name':'83N0','product_version':'Legion Go 8ASP2',
                  'bios_version':'RRCN16WW','ec_firmware_release':'1.16','board_name':'LNVNB161216'}
        with patch('fan_ec.platform.system',return_value='Linux'), patch('fan_ec.platform.machine',return_value='x86_64'), \
                patch('fan_ec.pathlib.Path.read_text',lambda p:expected[p.name]):
            self.assertEqual(platform_check(),expected)
            for name in expected:
                value=expected[name]; expected[name]=value+'-unknown'
                with self.assertRaises(RuntimeError): platform_check()
                expected[name]=value

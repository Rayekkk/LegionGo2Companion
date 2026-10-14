# SPDX-License-Identifier: BSD-3-Clause
from dataclasses import replace
import struct
import unittest
import controller_firmware_protocol as p


def response(family, command, target, payload=b''):
    return bytes((4, 0, family, command, target)) + payload + bytes(59-len(payload))


class ProtocolTests(unittest.TestCase):
    def test_vendor_byte_layout_and_partial_block_padding(self):
        data = bytes(range(33))
        start = p.start_report('left')
        self.assertEqual(start, bytes.fromhex('0500c0010701') + bytes(58))
        self.assertEqual(p.metadata_report('left', data), bytes.fromhex('0500c0020721000000020000005d30') + bytes(49))
        parts = list(p.blocks('left', data, 32))
        self.assertEqual(parts[0], (bytes.fromhex('0500c004070100000020')+data[:32]+bytes(22), True))
        self.assertEqual(parts[1], (bytes.fromhex('0500c00407020000000120')+bytes(53), True))
        self.assertEqual(p.finish_report('right')[:5], bytes.fromhex('0500c00508'))
        self.assertEqual(p.boundary_report('receiver')[:5], bytes.fromhex('0500c00301'))

    def test_ack_boundary_is_byte_count_not_block_count(self):
        parts = list(p.blocks('right', bytes(96), 64))
        self.assertEqual([wait for _, wait in parts], [False, True, True])
        self.assertEqual(p.parse_boundary(response(0xc0,3,8,struct.pack('<I',1024)), 'right'),1024)
        with self.assertRaises(p.FirmwareError):
            p.parse_boundary(response(0xc0,3,8,bytes(4)), 'right')

    def test_ack_errors_short_and_wrong_target_or_opcode_never_succeed(self):
        self.assertTrue(p.parse_ack(response(0xc0,4,7,b'\0'),4,'left'))
        for data in (b'', response(0xc0,4,7)[:-1], response(0xc0,5,7),response(0xc0,4,8),response(0xc0,4,7,b'\x01')):
            with self.assertRaises(p.FirmwareError):
                p.parse_ack(data,4,'left')

    def test_boundary_matches_legionspace_signed_positive_int32_gate(self):
        for value in (1,64,65535,0x7fffffff):
            self.assertEqual(p.parse_boundary(response(0xc0,3,7,struct.pack('<I',value)),'left'),value)
        for value in (0,0x80000000,0xffffffff):
            with self.assertRaises(p.FirmwareError):p.parse_boundary(response(0xc0,3,7,struct.pack('<I',value)),'left')
            with self.assertRaises(p.FirmwareError):list(p.blocks('left',b'X',value))
        self.assertTrue(p.parse_completion(response(0xa0,3,7,b'\x01'),'left'))
        for status in (0,2,255):
            with self.assertRaises(p.FirmwareError):
                p.parse_completion(response(0xa0,3,7,bytes([status])),'left')

    def test_versions_use_different_query_targets_and_big_endian(self):
        self.assertEqual(p.firmware_version_report('left')[:5],bytes.fromhex('0500020403'))
        self.assertEqual(p.firmware_version_report('right')[:5],bytes.fromhex('0500020404'))
        self.assertEqual(p.parse_firmware_version(response(2,4,3,bytes.fromhex('0260310a')),'left'),'260310A')
        with self.assertRaises(p.FirmwareError):
            p.parse_firmware_version(response(2,4,7,bytes.fromhex('0260310a')),'left')

    def test_invalid_frames_fail_without_io(self):
        for data in (b'', b'x'*33, bytearray(32)):
            with self.assertRaises(p.FirmwareError):p.block_report('left',1,data)
        for index in (0,-1,True,32769):
            with self.assertRaises(p.FirmwareError):p.block_report('left',index,b'a')
        for boundary in (0,-1,True,2**32):
            with self.assertRaises(p.FirmwareError):list(p.blocks('left',b'a',boundary))

    def test_malformed_decoder_arguments_fail_with_protocol_error(self):
        for component in ([], {}, None, True, 'all'):
            for decode, args in ((p.parse_ack, (4, component)),
                    (p.parse_boundary, (component,)),
                    (p.parse_firmware_version, (component,)),
                    (p.parse_completion, (component,))):
                with self.subTest(decoder=decode.__name__, component=component), self.assertRaises(p.FirmwareError):
                    decode(bytes(64), *args)
        for command in (True, 1.0, '1', None, [], 3):
            with self.subTest(command=command), self.assertRaises(p.FirmwareError):
                p.parse_ack(response(0xc0,1,7), command, 'left')

    def test_preflight_has_fresh_official_power_attachment_gates_and_no_downgrade(self):
        good=p.Preflight('Legion Go 8ASP2',2,'251204A',True,True,False,30,'epoch',10.)
        self.assertTrue(good.require('260310A','epoch',11.))
        self.assertTrue(replace(good,ac=True,battery_percent=None).require('260310A','epoch',11.))
        for change in ({'model':'Legion Go 8AHP2'},{'hardware_generation':255},{'left_attached':False},
            {'right_attached':False},{'battery_percent':29},{'battery_percent':101},{'ac':1},
            {'observed_at':7.},{'observed_at':12.},{'observed_at':float('nan')},{'observed_at':True},{'observed_at':10**400},
            {'connection_epoch':'other'},{'installed_version':'260422A'}):
            with self.subTest(change=change),self.assertRaises(p.FirmwareError):
                replace(good,**change).require('260310A','epoch',11.)
        for now in (10**400,-1,True,float('inf')):
            with self.assertRaises(p.FirmwareError):good.require('260310A','epoch',now)
        with self.assertRaises(p.FirmwareError):replace(good,observed_at=-1).require('260310A','epoch',0.)


if __name__=='__main__':unittest.main()

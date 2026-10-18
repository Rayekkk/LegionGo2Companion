# SPDX-License-Identifier: BSD-3-Clause
"""Controller firmware image gates run for real: signature, directory/payload structure, power.

Nothing here touches fwupd or a device. The signature path uses a throwaway RSA-3072 key
and the pinned-key constants are patched; the signing options mirror what the vendor image
must satisfy (PSS, salt 32, MGF1-SHA256).
"""
import base64
import binascii
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import support  # noqa: F401 - installs the fake decky module
import controller_firmware as fw

NAMES = (b'app_area_head', b'app.bin', b'cfg_tool.bin', b'VM', b'PRCT', b'BTIF')
APP = bytes(range(256)) * 2
CFG = bytes(reversed(range(200)))


def build_plain(*, names=NAMES, app=APP, cfg=CFG, app_address=None, cfg_address=None,
                app_size=None, corrupt_payload=False):
    """Decoded firmware body: 6 x 32-byte directory, app, cfg, 4 footer bytes."""
    app_address = 192 if app_address is None else app_address
    cfg_address = 192 + len(app) if cfg_address is None else cfg_address
    app_size = len(app) if app_size is None else app_size
    rows = [(0, 0, 0), (app_address, app_size, binascii.crc_hqx(app[:app_size], 0)),
            (cfg_address, len(cfg), binascii.crc_hqx(cfg, 0)), (0, 0, 0), (0, 0, 0), (0, 0, 0)]
    directory = b''
    for index, (name, (address, size, crc)) in enumerate(zip(names, rows)):
        tail = struct.pack('<HIIBBH16s', crc, address, size, 0, 0, index, name.ljust(16, b'\xff'))
        directory += struct.pack('<H', binascii.crc_hqx(tail, 0)) + tail
    body = bytearray(directory + app + cfg + bytes(4))
    if corrupt_payload:
        body[192 + 5] ^= 0x40  # after the checksums were computed
    return body


def encode(plain, footer=None):
    encoded = fw._decode_sfc(bytes(plain))  # the obfuscation is a symmetric XOR
    if footer is not None:
        encoded[-4:] = footer
    return bytes(encoded)


def structure_image(**options):
    return encode(build_plain(**options)) + bytes(fw.SIGNATURE_BYTES)


class DecodeTests(unittest.TestCase):
    def test_decode_is_a_symmetric_xor_keyed_by_32_byte_block_offset(self):
        self.assertEqual(fw._decode_sfc(bytes(2)), bytearray([0x0f, 0x3f]))
        data = os.urandom(100)
        self.assertEqual(bytes(fw._decode_sfc(bytes(fw._decode_sfc(data)))), data)
        # Every 32-byte block restarts with an offset-dependent key.
        self.assertNotEqual(fw._decode_sfc(bytes(64))[:32], fw._decode_sfc(bytes(64))[32:])


class StructureTests(unittest.TestCase):
    def assertRejected(self, image, message):
        with self.assertRaisesRegex(fw.FirmwareError, message):
            fw._check_structure(image)

    def test_well_formed_image_is_accepted(self):
        fw._check_structure(structure_image())

    def test_directory_checksum_covers_every_directory_byte(self):
        image = bytearray(structure_image())
        for position in (2, 31, 64 + 20, 191):  # data crc .. name of the third entry .. last entry
            bad = bytearray(image)
            bad[position] ^= 0x01
            with self.subTest(position=position):
                self.assertRejected(bytes(bad), 'directory checksum')

    def test_unexpected_names_or_order_are_an_unsupported_layout(self):
        swapped = (NAMES[0], NAMES[2], NAMES[1]) + NAMES[3:]
        for names in (swapped, NAMES[:5] + (b'XXXX',), (b'other',) + NAMES[1:]):
            with self.subTest(names=names):
                self.assertRejected(structure_image(names=names), 'directory layout')

    def test_payload_bounds_are_enforced(self):
        cases = {
            'zero size': dict(app_size=0),
            'inside the directory': dict(app_address=191),
            'beyond the image': dict(app_address=192 + len(APP) + len(CFG) + 1),
            'overlapping the app': dict(cfg_address=192 + len(APP) - 1),
            'cfg runs into the footer': dict(cfg_address=192 + len(APP) + 1),
        }
        for label, options in cases.items():
            with self.subTest(label):
                self.assertRejected(structure_image(**options), 'bounds')

    def test_payload_checksum_is_enforced(self):
        self.assertRejected(structure_image(corrupt_payload=True), 'payload checksum')

    def test_signature_bytes_are_not_part_of_the_structure(self):
        fw._check_structure(encode(build_plain()) + os.urandom(fw.SIGNATURE_BYTES))


def _openssl(*args, data=None):
    return subprocess.run([fw._openssl_executable(), *args], input=data, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def _probe_openssl():
    try:
        fw._openssl_executable()
    except fw.FirmwareError:
        return 'OpenSSL is unavailable on this platform'
    try:
        _openssl('version')
    except (OSError, subprocess.SubprocessError) as exc:
        return f'OpenSSL cannot be run: {exc}'
    return ''


class Key:
    def __init__(self, folder, name):
        self.path = Path(folder) / (name + '.pem')
        _openssl('genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', str(self.path))
        self.public = b'\n'.join(_openssl('pkey', '-in', str(self.path), '-pubout').splitlines()) + b'\n'
        der = base64.b64decode(b''.join(self.public.splitlines()[1:-1]))
        self.sha256 = hashlib.sha256(der).hexdigest()

    def sign(self, payload, *options):
        options = options or ('rsa_padding_mode:pss', 'rsa_pss_saltlen:32', 'rsa_mgf1_md:sha256')
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'payload.bin'
            source.write_bytes(payload)
            args = ['dgst', '-sha256', '-sign', str(self.path), '-out', str(Path(folder) / 'sig.bin')]
            for option in options:
                args += ['-sigopt', option]
            _openssl(*args, str(source))
            return (Path(folder) / 'sig.bin').read_bytes()


class PinnedKeyTests(unittest.TestCase):
    def test_pinned_key_matches_its_hash(self):
        der = base64.b64decode(b''.join(fw.PUBLIC_KEY.splitlines()[1:-1]), validate=True)
        self.assertEqual(hashlib.sha256(der).hexdigest(), fw.KEY_SHA256)

    def test_a_key_that_does_not_match_the_pinned_hash_is_never_used(self):
        with patch.object(fw, 'KEY_SHA256', '0' * 64), patch.object(fw.subprocess, 'run') as run:
            with self.assertRaisesRegex(fw.FirmwareError, 'trusted firmware key is invalid'):
                fw._verify_signature(bytes(1000))
        run.assert_not_called()


@unittest.skipIf(_probe_openssl(), _probe_openssl())
class SignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        cls.key, cls.other = Key(cls.folder.name, 'vendor'), Key(cls.folder.name, 'other')

    def pinned(self, key=None):
        key = key or self.key
        stack = patch.multiple(fw, PUBLIC_KEY=key.public, KEY_SHA256=key.sha256)
        stack.start()
        self.addCleanup(stack.stop)

    def signed(self, key=None, payload=None, *options):
        payload = os.urandom(5000) if payload is None else payload
        return payload + (key or self.key).sign(payload, *options)

    def test_valid_signature_is_accepted_and_every_edge_bit_flip_is_rejected(self):
        self.pinned()
        image = self.signed()
        fw._verify_signature(image)
        for position in (0, len(image) - fw.SIGNATURE_BYTES - 1, len(image) - fw.SIGNATURE_BYTES,
                         len(image) // 2, len(image) - 1):
            bad = bytearray(image)
            bad[position] ^= 0x01
            with self.subTest(position=position), self.assertRaisesRegex(fw.FirmwareError, 'signature is not valid'):
                fw._verify_signature(bytes(bad))

    def test_signature_from_another_key_or_with_other_padding_is_rejected(self):
        self.pinned()
        payload = os.urandom(3000)
        for label, image in {
            'wrong key': self.signed(self.other, payload),
            'pkcs1 v1.5': self.signed(self.key, payload, 'rsa_padding_mode:pkcs1'),
            'pss salt 20': self.signed(self.key, payload, 'rsa_padding_mode:pss', 'rsa_pss_saltlen:20',
                                       'rsa_mgf1_md:sha256'),
        }.items():
            with self.subTest(label), self.assertRaisesRegex(fw.FirmwareError, 'signature is not valid'):
                fw._verify_signature(image)

    def test_truncated_or_extended_image_is_rejected(self):
        self.pinned()
        image = self.signed()
        for label, bad in {'one byte short': image[:-1], 'signature cut in half': image[:-192],
                           'extra trailing byte': image + b'\0', 'signature only': image[-fw.SIGNATURE_BYTES:]}.items():
            with self.subTest(label), self.assertRaisesRegex(fw.FirmwareError, 'signature is not valid'):
                fw._verify_signature(bad)

    def test_the_real_pinned_vendor_key_rejects_an_image_signed_by_another_key(self):
        with self.assertRaisesRegex(fw.FirmwareError, 'signature is not valid'):
            fw._verify_signature(self.signed())

    def test_missing_openssl_fails_closed(self):
        self.pinned()
        with patch.object(fw, '_openssl_executable', side_effect=fw.FirmwareError('OpenSSL is unavailable.')):
            with self.assertRaisesRegex(fw.FirmwareError, 'OpenSSL is unavailable'):
                fw._verify_signature(self.signed())

    def release(self):
        return replace(fw.release_for('left'), size=0, sha256='')

    def image(self, key=None, **options):
        plain = build_plain(**options)
        return self.signed(key, encode(plain, self.release().footer))

    def test_validate_image_accepts_a_signed_well_formed_image_end_to_end(self):
        self.pinned()
        data = self.image()
        validated = fw.validate_image('left', data, self.release())
        self.assertEqual(validated.original, data)
        self.assertEqual((validated.release.size, validated.release.sha256),
                         (len(data), hashlib.sha256(data).hexdigest()))

    def test_image_size_bounds_are_enforced_on_both_ends(self):
        self.pinned()
        for label, length in {'below the minimum': fw.MIN_IMAGE_BYTES - 1, 'empty': 0, 'above the maximum': fw.MAX_IMAGE_BYTES + 1}.items():
            with self.subTest(label), self.assertRaisesRegex(fw.FirmwareError, 'size is invalid'):
                fw.validate_image('left', bytes(length), self.release())
        for length in (fw.MIN_IMAGE_BYTES - 1, fw.MAX_IMAGE_BYTES + 1):
            with self.subTest(announced=length), self.assertRaisesRegex(fw.FirmwareError, 'Invalid firmware download size'):
                fw.download_image(replace(fw.release_for('left'), size=length), threading.Event())

    def test_validate_image_rejects_each_broken_gate(self):
        self.pinned()
        good = self.image()
        footer_at = len(good) - fw.SIGNATURE_BYTES - 4
        wrong_footer = bytearray(good)
        wrong_footer[footer_at:footer_at + 4] = fw.release_for('right').footer
        cases = {
            'different component': (bytes(wrong_footer), 'different component'),
            'signed by another key': (self.image(self.other), 'signature is not valid'),
            'tampered after signing': (good[:200] + bytes([good[200] ^ 1]) + good[201:], 'signature is not valid'),
            # Correctly signed, but the directory is malformed: signature alone is not enough.
            'signed bad layout': (self.image(names=NAMES[:5] + (b'XXXX',)), 'directory layout'),
            'signed bad payload': (self.image(corrupt_payload=True), 'payload checksum'),
        }
        for label, (data, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(fw.FirmwareError, message):
                fw.validate_image('left', data, self.release())


class ConsolePowerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        real = fw.Path
        patcher = patch.object(fw, 'Path', lambda value, *rest: self.root if str(value).replace('\\', '/') ==
                               '/sys/class/power_supply' else real(value, *rest))
        patcher.start()
        self.addCleanup(patcher.stop)

    def supply(self, name, kind, **attributes):
        folder = self.root / name
        folder.mkdir()
        (folder / 'type').write_text(kind + '\n')
        for key, value in attributes.items():
            (folder / key).write_text(str(value) + '\n')

    def test_ac_online_passes_regardless_of_the_battery_level(self):
        self.supply('ACAD', 'Mains', online=1)
        self.supply('BAT0', 'Battery', scope='System', capacity=5)
        self.assertEqual(fw.console_power(), (True, 5))

    def test_usb_c_charger_counts_as_ac(self):
        self.supply('ucsi', 'USB_PD', online=1)
        self.assertEqual(fw.console_power(), (True, None))

    def test_battery_alone_needs_thirty_percent(self):
        self.supply('ACAD', 'Mains', online=0)
        self.supply('BAT0', 'Battery', scope='System', capacity=30)
        self.assertEqual(fw.console_power(), (False, 30))
        for percent in (29, 0, 101):
            (self.root / 'BAT0' / 'capacity').write_text(f'{percent}\n')
            with self.subTest(percent=percent), self.assertRaisesRegex(fw.FirmwareError, 'at least 30%'):
                fw.console_power()
        (self.root / 'BAT0' / 'capacity').write_text('100\n')
        self.assertEqual(fw.console_power(), (False, 100))

    def test_no_charger_and_no_battery_is_refused(self):
        with self.assertRaisesRegex(fw.FirmwareError, 'at least 30%'):
            fw.console_power()
        self.supply('ACAD', 'Mains', online=0)
        with self.assertRaisesRegex(fw.FirmwareError, 'at least 30%'):
            fw.console_power()

    def test_device_scope_supplies_are_ignored(self):
        self.supply('hid-left', 'Battery', scope='Device', capacity=95)
        self.supply('hid-charger', 'USB', scope='Device', online=1)
        self.supply('BAT0', 'Battery', scope='System', capacity=10)
        with self.assertRaisesRegex(fw.FirmwareError, 'at least 30%'):
            fw.console_power()

    def test_legion_go_2_layout_allows_battery_without_a_scope_file(self):
        # Read on the device: ACAD and BAT0 have no scope file, the ucsi supplies are scope=Device.
        self.supply('ACAD', 'Mains', online=0)
        self.supply('BAT0', 'Battery', present=1, capacity=97)
        self.supply('ucsi-source-psy-1', 'USB', scope='Device', online=0)
        self.supply('ucsi-source-psy-2', 'USB', scope='Device', online=1)
        self.assertEqual(fw.console_power(), (False, 97))
        (self.root / 'BAT0' / 'capacity').write_text('29\n')
        with self.assertRaisesRegex(fw.FirmwareError, 'at least 30%'):
            fw.console_power()

    def test_scopeless_battery_not_named_bat_is_not_trusted(self):
        self.supply('hid-pad-battery', 'Battery', capacity=90)
        with self.assertRaisesRegex(fw.FirmwareError, 'at least 30%'):
            fw.console_power()

    def test_two_system_batteries_cannot_be_told_apart(self):
        self.supply('BAT0', 'Battery', scope='System', capacity=80)
        self.supply('BAT1', 'Battery', scope='System', capacity=80)
        self.supply('ACAD', 'Mains', online=1)
        with self.assertRaisesRegex(fw.FirmwareError, 'battery could not be identified'):
            fw.console_power()

    def test_oversized_inventory_is_refused(self):
        for index in range(33):
            self.supply(f'dev{index}', 'Unknown')
        with self.assertRaisesRegex(fw.FirmwareError, 'inventory exceeds'):
            fw.console_power()


if __name__ == '__main__':
    unittest.main()

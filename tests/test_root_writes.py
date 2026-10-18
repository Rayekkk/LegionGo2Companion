# SPDX-License-Identifier: BSD-3-Clause
"""The real root write primitives, run unmocked against files standing in for sysfs and /dev/mem.

Gates (path containment, symlink and type checks, value allow-lists) stay in force; only
the locations they point at are redirected.
"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support  # noqa: F401 - installs the fake decky module
from support import power_led_dsdt
import battery_backend as battery
import controller_imu as imu
import rgb_backend

POSIX = os.name == 'posix'


def raw(path):
    """File bytes with Windows text-mode newlines normalised (os.open without O_BINARY)."""
    return Path(path).read_bytes().replace(b'\r\n', b'\n')


class TempTree(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()


class BatteryWriteModeTests(TempTree):
    def setUp(self):
        super().setUp()
        patcher = patch.object(battery, 'SYS_ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.attribute = self.root / 'class/power_supply/BAT0/charge_types'
        self.attribute.parent.mkdir(parents=True)
        self.attribute.write_bytes(b'')

    def test_each_allowed_mode_is_written_with_a_newline_and_nothing_else(self):
        for mode in sorted(battery.ALLOWED_MODES):
            self.attribute.write_bytes(b'')
            with self.subTest(mode=mode):
                battery._write_mode(self.attribute, mode)
                self.assertEqual(raw(self.attribute), mode.encode() + b'\n')

    def test_modes_outside_the_allow_list_never_touch_the_file(self):
        self.attribute.write_bytes(b'Standard\n')
        for mode in ('', 'standard', 'Trickle', 'Fast\nLong_Life', 'Fast ', None, 1, b'Fast'):
            with self.subTest(mode=mode), self.assertRaises(battery.BatteryError):
                battery._write_mode(self.attribute, mode)
        self.assertEqual(raw(self.attribute), b'Standard\n')

    def test_missing_or_directory_or_outside_paths_are_refused(self):
        outside = tempfile.NamedTemporaryFile(delete=False)
        outside.close()
        self.addCleanup(os.unlink, outside.name)
        for path in (self.root / 'class/power_supply/BAT0/absent', self.attribute.parent, Path(outside.name)):
            with self.subTest(path=str(path)), self.assertRaises((battery.BatteryError, OSError)):
                battery._write_mode(path, 'Fast')
        self.assertEqual(raw(outside.name), b'')

    @unittest.skipUnless(POSIX, 'symlinks need POSIX')
    def test_symlinked_attribute_is_refused_even_inside_sysfs(self):
        real = self.root / 'devices/real_charge_types'
        real.parent.mkdir()
        real.write_bytes(b'')
        link = self.root / 'class/power_supply/BAT0/linked'
        link.symlink_to(real)
        with self.assertRaises(battery.BatteryError):
            battery._write_mode(link, 'Fast')
        self.assertEqual(raw(real), b'')

    @unittest.skipUnless(POSIX, 'symlinks need POSIX')
    def test_link_leaving_sysfs_is_refused(self):
        target = tempfile.NamedTemporaryFile(delete=False)
        target.close()
        self.addCleanup(os.unlink, target.name)
        link = self.root / 'class/power_supply/BAT0/escape'
        link.symlink_to(target.name)
        with self.assertRaises(battery.BatteryError):
            battery._write_mode(link, 'Fast')
        self.assertEqual(raw(target.name), b'')

    def test_short_write_is_reported_not_swallowed(self):
        with patch.object(battery.os, 'write', return_value=2):
            with self.assertRaisesRegex(battery.BatteryError, 'only part'):
                battery._write_mode(self.attribute, 'Long_Life')


class ImuWriteTests(TempTree):
    def setUp(self):
        super().setUp()
        patcher = patch.object(imu, 'SYS_ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.device = self.root / 'devices/hid0002'
        for side in imu.SIDES:
            attribute = self.device / f'{side}_handle/imu_bypass_enabled'
            attribute.parent.mkdir(parents=True)
            attribute.write_bytes(b'')

    def attribute(self, side):
        return self.device / f'{side}_handle/imu_bypass_enabled'

    def test_bool_is_written_as_true_or_false_and_only_to_the_chosen_side(self):
        imu._write(self.device, 'left', True)
        self.assertEqual(raw(self.attribute('left')), b'true\n')
        self.assertEqual(raw(self.attribute('right')), b'')
        self.attribute('right').write_bytes(b'')
        self.attribute('left').write_bytes(b'')
        imu._write(self.device, 'right', False)
        self.assertEqual(raw(self.attribute('right')), b'false\n')
        self.assertEqual(raw(self.attribute('left')), b'')

    def test_written_value_reads_back_through_the_checked_reader(self):
        imu._write(self.device, 'left', True)
        self.assertEqual(imu._read_bool(self.device, 'left'), True)
        self.attribute('left').write_bytes(b'')
        imu._write(self.device, 'left', False)
        self.assertEqual(imu._read_bool(self.device, 'left'), False)

    def test_non_bool_values_and_unknown_sides_never_write(self):
        for enabled in (1, 0, 'true', None, 'false'):
            with self.subTest(enabled=enabled), self.assertRaises(imu.ImuError):
                imu._write(self.device, 'left', enabled)
        with self.assertRaises(imu.ImuError):
            imu._write(self.device, 'center', True)
        self.assertEqual(raw(self.attribute('left')), b'')

    def test_missing_attribute_is_an_error_and_creates_nothing(self):
        self.attribute('right').unlink()
        with self.assertRaises(OSError):
            imu._write(self.device, 'right', True)
        self.assertFalse(self.attribute('right').exists())

    @unittest.skipUnless(POSIX, 'symlinks need POSIX')
    def test_symlinked_attribute_is_refused(self):
        target = self.root / 'devices/other'
        target.write_bytes(b'')
        self.attribute('left').unlink()
        self.attribute('left').symlink_to(target)
        with self.assertRaises(imu.ImuError):
            imu._write(self.device, 'left', True)
        self.assertEqual(raw(target), b'')

    @unittest.skipUnless(POSIX, 'symlinks need POSIX')
    def test_attribute_reached_through_a_directory_link_leaving_the_device_is_refused(self):
        elsewhere = self.root / 'devices/elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'imu_bypass_enabled').write_bytes(b'')
        handle = self.device / 'left_handle'
        (handle / 'imu_bypass_enabled').unlink()
        handle.rmdir()
        handle.symlink_to(elsewhere)
        with self.assertRaises(imu.ImuError):
            imu._write(self.device, 'left', True)
        self.assertEqual(raw(elsewhere / 'imu_bypass_enabled'), b'')

    def test_short_write_is_reported(self):
        with patch.object(imu.os, 'write', return_value=1):
            with self.assertRaisesRegex(imu.ImuError, 'only part'):
                imu._write(self.device, 'left', True)


@unittest.skipUnless(POSIX, 'mmap of a device file needs POSIX')
class PowerLedMmapTests(TempTree):
    MARKER = 0xA4  # other bits set in both nibbles, bit 0 clear (a stray `| 1` shows), bit 6 clear
    BIT = rgb_backend.POWER_LED_MASK

    def setUp(self):
        super().setUp()
        self.page = rgb_backend.mmap.PAGESIZE
        self.address = 3 * self.page + 0x10
        self.devmem = self.root / 'mem'
        self.devmem.write_bytes(bytes([0x11]) * (5 * self.page))
        self.set_byte(self.MARKER | self.BIT)  # LED off (logic is inverted)
        self.dsdt = self.root / 'DSDT'
        self.dsdt.write_bytes(power_led_dsdt())
        identity = {'product_family': rgb_backend.EXPECTED_FAMILY, 'product_name': rgb_backend.EXPECTED_PRODUCT,
                    'board_name': rgb_backend.EXPECTED_BOARD, 'bios_version': 'RRCN16WW'}
        for name, value in {
            'POWER_LED_ADDRESS': self.address, 'DEV_MEM_PATH': str(self.devmem),
            'POWER_LOCK_PATH': str(self.root / 'power.lock'), 'DSDT_PATH': str(self.dsdt),
            '_power_capability_cache': None, '_power_capability_retry_at': 0.0,
        }.items():
            patcher = patch.object(rgb_backend, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(rgb_backend, '_read_identity', return_value=identity)
        patcher.start()
        self.addCleanup(patcher.stop)

    def byte(self, offset=None):
        with open(self.devmem, 'rb') as handle:
            handle.seek(self.address if offset is None else offset)
            return handle.read(1)[0]

    def set_byte(self, value):
        with open(self.devmem, 'r+b') as handle:
            handle.seek(self.address)
            handle.write(bytes([value]))

    def test_real_gate_accepts_the_fake_device_and_register_is_page_offset_addressed(self):
        self.assertEqual(rgb_backend._power_capability(), (True, ''))
        seen = []
        value = rgb_backend._map_power_register(False, lambda mapped, offset: seen.append(offset) or mapped[offset])
        self.assertEqual(seen, [0x10])
        self.assertEqual(value, self.MARKER | self.BIT)

    def test_read_only_mapping_cannot_write(self):
        def attempt(mapped, offset):
            mapped[offset] = 0
        with self.assertRaises((TypeError, ValueError, OSError)):
            rgb_backend._map_power_register(False, attempt)
        self.assertEqual(self.byte(), self.MARKER | self.BIT)

    def test_led_on_clears_only_bit_6_and_off_sets_it_back(self):
        before_file = self.devmem.read_bytes()
        self.assertFalse(rgb_backend._read_power_led())
        self.assertTrue(rgb_backend._write_power_led(True))
        self.assertEqual(self.byte(), self.MARKER & ~self.BIT)
        self.assertTrue(rgb_backend._read_power_led())
        changed = [i for i, (a, b) in enumerate(zip(before_file, self.devmem.read_bytes())) if a != b]
        self.assertEqual(changed, [self.address])
        self.assertTrue(rgb_backend._write_power_led(False))
        self.assertEqual(self.devmem.read_bytes(), before_file)

    def test_setting_the_state_it_already_has_leaves_every_bit_alone(self):
        self.set_byte(0xFF)
        self.assertTrue(rgb_backend._write_power_led(False))
        self.assertEqual(self.byte(), 0xFF)
        self.set_byte(0x00)
        self.assertTrue(rgb_backend._write_power_led(True))
        self.assertEqual(self.byte(), 0x00)

    def test_neighbouring_registers_are_never_written(self):
        before = self.devmem.read_bytes()
        rgb_backend._write_power_led(True)
        after = self.devmem.read_bytes()
        self.assertEqual(after[:self.address], before[:self.address])
        self.assertEqual(after[self.address + 1:], before[self.address + 1:])

    def led_on_through(self, on_get=None, on_set=None):
        """Turn the LED on through a proxy around the real mapping that may misbehave like a busy EC."""
        real = rgb_backend.mmap.mmap

        class Proxy:
            def __init__(self, mapped):
                self.mapped, self.reads, self.writes = mapped, 0, 0

            def __getitem__(self, index):
                self.reads += 1
                value = self.mapped[index]
                return on_get(self, value) if on_get else value

            def __setitem__(self, index, value):
                self.writes += 1
                self.mapped[index] = on_set(self, value) if on_set else value

            def close(self):
                self.mapped.close()

        with patch.object(rgb_backend.mmap, 'mmap', lambda *a, **k: Proxy(real(*a, **k))):
            return rgb_backend._write_power_led(True)

    def test_write_the_hardware_drops_is_reported_and_unrelated_bits_survive(self):
        # First write: our bit stays as it was while another bit (0) changes underneath us.
        dropped = self.led_on_through(on_set=lambda p, v: (v | self.BIT) ^ 0x01 if p.writes == 1 else v)
        self.assertFalse(dropped)
        self.assertEqual(self.byte() & self.BIT, self.BIT)
        self.assertEqual(self.byte() & ~self.BIT, (self.MARKER ^ 0x01) & ~self.BIT)

    def test_write_that_lands_after_a_failed_verify_is_rolled_back_to_the_original_bit(self):
        # Read 1 = before, read 2 = verify (stale), read 3 = the rollback's fresh read.
        late = self.led_on_through(on_get=lambda p, v: v ^ self.BIT if p.reads == 2 else v)
        self.assertFalse(late)
        self.assertEqual(self.byte(), self.MARKER | self.BIT)

    def test_device_file_too_short_for_the_register_fails_without_raising(self):
        self.devmem.write_bytes(bytes(self.address))  # one byte short of the register
        self.assertFalse(rgb_backend._write_power_led(True))
        self.assertIsNone(rgb_backend._read_power_led())
        self.assertEqual(len(self.devmem.read_bytes()), self.address)

    def test_non_bool_state_is_refused_before_any_mapping(self):
        with patch.object(rgb_backend, '_map_power_register') as mapped:
            for value in (1, 0, 'on', None):
                self.assertFalse(rgb_backend._write_power_led(value))
        mapped.assert_not_called()

    def test_audited_physical_address_maps_the_right_page_and_byte(self):
        address = 0xFEEC2300 + 0x10
        size = address - address % self.page + self.page
        try:
            with open(self.devmem, 'r+b') as handle:
                handle.truncate(size)  # sparse
        except OSError as exc:
            self.skipTest(f'cannot create a sparse {size}-byte file: {exc}')
        self.address = address
        with patch.object(rgb_backend, 'POWER_LED_ADDRESS', address):
            self.set_byte(self.MARKER | self.BIT)
            self.assertTrue(rgb_backend._write_power_led(True))
        self.assertEqual(self.byte(), self.MARKER & ~self.BIT)
        self.assertEqual(self.byte(address - 1), 0)
        self.assertEqual(self.byte(address + 1), 0)


if __name__ == '__main__':
    unittest.main()

# SPDX-License-Identifier: BSD-3-Clause
"""Independent EC implementation. Hardware protocol facts are attributed in SOURCE.md.

    ONLY the validated 83N0 / 8ASP2 RRCN16WW EC1.16 mapping is admitted.
    No WMI fan controls, MMIO writes or arbitrary register API.
"""
import contextlib
import os
import pathlib
import platform
import time
from fan_policy import MIN_RPM, MAX_RPM, STOP_OVERRIDE, encode_target


def transition_bytes(current, target):
    """Choose a two-byte path with bounded, non-stalling positive intermediates.

    Firmware may sample after either write. Positive-to-positive transitions
    stay within 1500..4800; stop transitions stay in the explicit stop branch.
    The endpoint is never firmware automatic, which is reserved for release.
    """
    allowed = lambda v: v == 0 or v == STOP_OVERRIDE or MIN_RPM <= v <= MAX_RPM
    if not allowed(current) or not allowed(target) or target == 0:
        raise RuntimeError('Foreign/partial fan target; release instead of overwriting.')
    hi, lo = target >> 8, target & 255
    candidates = [((0xC6C9, hi), (0xC6C8, lo)), ((0xC6C8, lo), (0xC6C9, hi))]
    # Boundary pairs (1500 <-> 4800) can make both direct orders unsafe.
    # An interior high byte first provides a third-write bridge. Validate
    # every prefix below instead of assuming a particular firmware sampling.
    candidates.extend(((0xC6C9, bridge), (0xC6C8, lo), (0xC6C9, hi))
                      for bridge in range(MIN_RPM//256+1, MAX_RPM//256))
    for plan in candidates:
        value = current
        steps = []
        for addr, byte in plan:
            next_value = (byte << 8 | value & 255) if addr == 0xC6C9 else (value & 0xff00 | byte)
            if next_value != value:
                steps.append((addr, byte))
            value = next_value
            if current >= MIN_RPM and target >= MIN_RPM:
                safe = MIN_RPM <= value <= MAX_RPM
            else:
                safe = 0 < value < 1500 or MIN_RPM <= value <= MAX_RPM
            if not safe:
                break
        else:
            return steps
    raise RuntimeError('No bounded fan byte transition.')

WHITELIST = frozenset((0xC300, 0xC301, 0xC307, 0xC683,
                      0xC6C0, 0xC6C1, 0xC6C2, 0xC6C3, 0xC6C8, 0xC6C9))


def platform_check(root='/sys/class/dmi/id'):
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('Fan Control requires the validated Legion Go 8ASP2 platform.')
    expected = {'sys_vendor': 'LENOVO', 'product_name': '83N0',
                'product_version': 'Legion Go 8ASP2', 'bios_version': 'RRCN16WW',
                'ec_firmware_release': '1.16', 'board_name': 'LNVNB161216'}
    for name, value in expected.items():
        if (pathlib.Path(root)/name).read_text().strip() != value:
            raise RuntimeError('This hardware/BIOS/EC variant has not been validated for Fan Control.')
    return expected


class Ports:
    def __init__(self):
        self.fd = os.open('/dev/port', os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)

    def read(self, port):
        b = os.pread(self.fd, 1, port)
        if len(b) != 1:
            raise OSError('Short EC port read.')
        return b[0]

    def write(self, port, value):
        if os.pwrite(self.fd, bytes([value]), port) != 1:
            raise OSError('Short EC port write.')

    def close(self):
        os.close(self.fd)


class EC:
    def __init__(self, ports):
        self.p = ports
        self.baseline = None

    def primary(self, value):
        self.p.write(0x4E, value)
        if self.p.read(0x4E) != value:
            raise RuntimeError('EC primary selector interference.')

    def secondary(self, value):
        if value not in (0x10, 0x11, 0x12):
            raise ValueError('Unknown EC selector.')
        self.primary(0x2E)
        self.p.write(0x4F, value)
        if self.p.read(0x4F) != value:
            raise RuntimeError('EC secondary selector interference.')

    def selector_read(self, value):
        self.secondary(value); self.primary(0x2F)
        return self.p.read(0x4F)

    def selector_write(self, register, value):
        if register not in (0x10, 0x11):
            raise ValueError('Not an address selector.')
        self.secondary(register); self.primary(0x2F); self.p.write(0x4F, value)
        if self.p.read(0x4F) != value:
            raise RuntimeError('EC address selector interference.')

    def address(self, addr):
        if addr not in WHITELIST:
            raise ValueError('EC address outside the fan whitelist.')
        self.selector_write(0x11, addr >> 8); self.selector_write(0x10, addr & 255)
        self.secondary(0x12); self.primary(0x2F)

    def byte(self, addr):
        self.address(addr)
        return self.p.read(0x4F)

    def word(self, addr):
        for _ in range(3):
            high = self.byte(addr+1); low = self.byte(addr)
            if high == self.byte(addr+1):
                return (high << 8) | low
        raise RuntimeError('Unstable EC word.')

    def _write_byte(self, addr, value, stopped=None):
        if type(value) is not int or not ((addr == 0xC6C9 and 0 <= value <= MAX_RPM//256)
                or (addr == 0xC6C8 and 0 <= value <= 255)):
            raise ValueError('Unsafe EC data write.')
        self.address(addr)
        if stopped is not None and stopped():
            raise RuntimeError('Fan lease expired immediately before its data write.')
        self.p.write(0x4F, value)
        if self.byte(addr) != value:
            raise RuntimeError('EC write readback mismatch.')

    def snapshot(self):
        primary = self.p.read(0x4E)
        self.primary(0x2E); secondary = self.p.read(0x4F)
        if secondary not in (0x10, 0x11, 0x12):
            self.primary(primary)
            raise RuntimeError('Unknown initial EC selector.')
        self.baseline = dict(primary=primary, secondary=secondary,
                             hi=self.selector_read(0x11), lo=self.selector_read(0x10))
        self.restore()
        return self.baseline

    def restore(self):
        b = self.baseline
        if b is None:
            return
        self.selector_write(0x11, b['hi']); self.selector_write(0x10, b['lo'])
        hi, lo = self.selector_read(0x11), self.selector_read(0x10)
        self.secondary(b['secondary']); secondary = self.p.read(0x4F)
        self.primary(b['primary'])
        if dict(primary=self.p.read(0x4E), secondary=secondary, hi=hi, lo=lo) != b:
            raise RuntimeError('EC selector restoration failed.')

    @contextlib.contextmanager
    def transaction(self, timeout=0.3):
        import fcntl
        deadline = time.monotonic()+timeout
        while True:
            try:
                fcntl.flock(self.p.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('EC transaction ownership timeout.')
                time.sleep(0.002)
        try:
            if self.baseline is None:
                self.snapshot()
            yield self
        finally:
            try:
                self.restore()
            finally:
                fcntl.flock(self.p.fd, fcntl.LOCK_UN)

    def status(self):
        if [self.byte(0xC300), self.byte(0xC301)] != [1, 16]:
            raise RuntimeError('EC version gate failed.')
        result = dict(rpm=self.word(0xC6C0), effective=self.word(0xC6C2),
                      override=self.word(0xC6C8), ec_temperature=self.byte(0xC307), power_mode=self.byte(0xC683))
        if any(result[k] > 10000 for k in ('rpm', 'effective', 'override')):
            raise RuntimeError('Implausible EC fan telemetry.')
        return result

    def set_target(self, rpm, stopped):
        target = encode_target(rpm)
        plan = transition_bytes(self.word(0xC6C8), target)
        if stopped():
            raise RuntimeError('Fan lease already withdrawn.')
        for addr, byte in plan:
            self._write_byte(addr, byte, stopped)
        if self.word(0xC6C8) != target:
            raise RuntimeError('Fan target readback failed.')

    def release(self):
        if self.word(0xC6C8) != 0:
            # Retain running speed where possible, including a foreign partial
            # word; high<6 takes the stop branch briefly before native zero.
            order = (0xC6C8, 0xC6C9) if self.byte(0xC6C9) >= 6 else (0xC6C9, 0xC6C8)
            for addr in order:
                self._write_byte(addr, 0)
        if self.word(0xC6C8) != 0:
            raise RuntimeError('Firmware automatic release not confirmed.')

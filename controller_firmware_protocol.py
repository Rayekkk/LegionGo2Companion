# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Pure codecs for Lenovo's native C0 OTA protocol. No device I/O.

Layout facts: static LegionSpace 1.4.4.31 LEGOKZHandle.dll analysis, round4.
fwupd's 53/11 UART path is different and must not supply C0 ACK semantics.
These functions do not establish Linux transport or duplicate-request safety.
"""
from __future__ import annotations
import binascii
from dataclasses import dataclass
import struct
from controller_firmware import FirmwareError, update_needed, version_number

REPORT_SIZE = 64
BLOCK_SIZE = 32
MAX_STREAM_SIZE = 1024 * 1024
MAX_ACK_BOUNDARY = 0x7fffffff  # LegionSpace tests a signed positive int32.
OTA_TARGETS = {'left': 7, 'right': 8, 'receiver': 1}
VERSION_QUERY_TARGETS = {'left': 3, 'right': 4, 'receiver': 1}


def _integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise FirmwareError('Invalid controller protocol value.')
    return value


def _frame(family, command, target, payload=b''):
    _integer(target, 1, 8)
    if not isinstance(payload, bytes) or len(payload) > 59:
        raise FirmwareError('Invalid controller report payload.')
    return bytes((5, 0, family, command, target)) + payload + bytes(59 - len(payload))


def _ota(command, component, payload=b''):
    if not isinstance(component, str) or component not in OTA_TARGETS:
        raise FirmwareError('Unsupported controller component.')
    return _frame(0xc0, command, OTA_TARGETS[component], payload)


def start_report(component):
    return _ota(1, component, b'\x01')


def metadata_report(component, original):
    if not isinstance(original, bytes) or not 1 <= len(original) <= MAX_STREAM_SIZE:
        raise FirmwareError('Invalid controller firmware stream size.')
    size = len(original)
    return _ota(2, component, struct.pack('<IIH', size, (size + 31) // 32,
        binascii.crc_hqx(original, 0)))


def boundary_report(component):
    return _ota(3, component)


def block_report(component, index, data):
    _integer(index, 1, MAX_STREAM_SIZE // BLOCK_SIZE)
    if not isinstance(data, bytes) or not 1 <= len(data) <= BLOCK_SIZE:
        raise FirmwareError('Invalid controller firmware block.')
    return _ota(4, component, struct.pack('<IB', index, len(data)) + data)


def finish_report(component):
    return _ota(5, component)


def firmware_version_report(component):
    if not isinstance(component, str) or component not in VERSION_QUERY_TARGETS:
        raise FirmwareError('Unsupported controller component.')
    return _frame(2, 4, VERSION_QUERY_TARGETS[component])


def hardware_generation_report(component):
    if not isinstance(component,str) or component not in VERSION_QUERY_TARGETS: raise FirmwareError('Unsupported controller component.')
    return _frame(2, 6, VERSION_QUERY_TARGETS[component])


def parse_hardware_generation(report, component):
    if not isinstance(component,str) or component not in VERSION_QUERY_TARGETS: raise FirmwareError('Unsupported controller component.')
    payload = _response(report, 2, 6, VERSION_QUERY_TARGETS[component])
    if payload[0] != 2: raise FirmwareError('This controller hardware generation is not supported.')
    return payload[0]


def _response(report, family, command, target):
    if not isinstance(report, bytes) or len(report) != REPORT_SIZE:
        raise FirmwareError('Controller returned an incomplete report.')
    if report[:5] != bytes((4, 0, family, command, target)):
        raise FirmwareError('Controller reply does not match the requested operation.')
    return report[5:]


def parse_ack(report, command, component):
    if type(command) is not int or command not in (1, 2, 4, 5) or not isinstance(component, str) or component not in OTA_TARGETS:
        raise FirmwareError('Unsupported controller acknowledgement.')
    payload = _response(report, 0xc0, command, OTA_TARGETS[component])
    if payload[0] != 0:
        raise FirmwareError('Controller rejected the firmware operation.')
    # It has no chunk index. Matching this report alone cannot prove freshness.
    return True


def parse_boundary(report, component):
    if not isinstance(component, str) or component not in OTA_TARGETS:
        raise FirmwareError('Unsupported controller component.')
    payload = _response(report, 0xc0, 3, OTA_TARGETS[component])
    boundary = struct.unpack_from('<I', payload)[0]
    if not 1 <= boundary <= MAX_ACK_BOUNDARY:
        raise FirmwareError('Controller returned an invalid acknowledgement boundary.')
    # Match the official host's signed JLE/CMOVG gate. No alignment is imposed.
    return boundary


def parse_firmware_version(report, component):
    if not isinstance(component, str) or component not in VERSION_QUERY_TARGETS:
        raise FirmwareError('Unsupported controller component.')
    payload = _response(report, 2, 4, VERSION_QUERY_TARGETS[component])
    value = struct.unpack_from('>I', payload)[0]
    version = f'{value:X}'
    version_number(version)
    return version


def parse_completion(report, component):
    if not isinstance(component, str) or component not in OTA_TARGETS:
        raise FirmwareError('Unsupported controller component.')
    payload = _response(report, 0xa0, 3, OTA_TARGETS[component])
    if payload[0] != 1:
        raise FirmwareError('Controller did not report successful completion.')
    # Completion event still needs re-enumeration and installed-version readback.
    return True


def blocks(component, original, boundary):
    """Yield report and ACK requirement, without writing or replaying anything."""
    _integer(boundary, 1, MAX_ACK_BOUNDARY)
    if not isinstance(original, bytes) or not 1 <= len(original) <= MAX_STREAM_SIZE:
        raise FirmwareError('Invalid controller firmware stream size.')
    for offset in range(0, len(original), BLOCK_SIZE):
        chunk = original[offset:offset+BLOCK_SIZE]
        end = offset + len(chunk)
        yield block_report(component, offset // BLOCK_SIZE + 1, chunk), end % boundary == 0 or end == len(original)


@dataclass(frozen=True)
class Preflight:
    model: str
    hardware_generation: int
    installed_version: str
    left_attached: bool
    right_attached: bool
    ac: bool
    battery_percent: int | None
    connection_epoch: str
    observed_at: float

    def require(self, offered, epoch, now):
        """Policy check on independently acquired data, not an identity probe."""
        if self.model != 'Legion Go 8ASP2' or type(self.hardware_generation) is not int or self.hardware_generation != 2:
            raise FirmwareError('This controller variant is not validated for firmware transfer.')
        if not isinstance(self.connection_epoch, str) or not self.connection_epoch or self.connection_epoch != epoch:
            raise FirmwareError('The controller connection changed.')
        if type(self.observed_at) not in (int, float) or type(now) not in (int, float):
            raise FirmwareError('Controller checks have no valid timestamp.')
        import math
        try:
            fresh = (math.isfinite(self.observed_at) and math.isfinite(now) and
                self.observed_at >= 0 and now >= 0 and 0 <= now - self.observed_at <= 2)
        except OverflowError:
            fresh = False
        if not fresh:
            raise FirmwareError('Controller checks must be refreshed before transfer.')
        if self.left_attached is not True or self.right_attached is not True:
            raise FirmwareError('Attach both controllers before updating firmware.')
        if type(self.ac) is not bool or (not self.ac and (type(self.battery_percent) is not int or not 30 <= self.battery_percent <= 100)):
            raise FirmwareError('Connect external power or charge the console to at least 30%.')
        if not update_needed(self.installed_version, offered):
            raise FirmwareError('The offered firmware is not newer than the installed version.')
        return True

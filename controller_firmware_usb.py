# SPDX-License-Identifier: BSD-3-Clause
"""Internal libusb inventory and version-only interrupt qualification.

No open, claim, detach, reset, alternate-setting or OTA operation is provided.
An interrupt channel requires an already owned handle from a future reviewed
handoff coordinator. Inventory never opens a device. This does not implement
the flashing adapter or establish cross-process exclusion or crash restoration.
ABI source: upstream libusb 1.0 device/synchronous-I/O documentation.
"""
import ctypes as C
import math
import threading
import time

from controller_firmware import FirmwareError


class _Descriptor(C.Structure):
    _fields_ = [('length', C.c_uint8), ('kind', C.c_uint8), ('usb', C.c_uint16),
        ('device_class', C.c_uint8), ('subclass', C.c_uint8), ('protocol', C.c_uint8),
        ('packet0', C.c_uint8), ('vendor', C.c_uint16), ('product', C.c_uint16),
        ('revision', C.c_uint16), ('manufacturer', C.c_uint8), ('product_string', C.c_uint8),
        ('serial', C.c_uint8), ('configurations', C.c_uint8)]


class Libusb:
    """Minimal Linux ABI; construction loads a library, not a USB context."""
    def __init__(self):
        self._lib = C.CDLL('libusb-1.0.so.0')
        signatures = {
            'libusb_init': (C.c_int, [C.POINTER(C.c_void_p)]),
            'libusb_exit': (None, [C.c_void_p]),
            'libusb_get_device_list': (C.c_ssize_t, [C.c_void_p, C.POINTER(C.POINTER(C.c_void_p))]),
            'libusb_free_device_list': (None, [C.POINTER(C.c_void_p), C.c_int]),
            'libusb_get_device_descriptor': (C.c_int, [C.c_void_p, C.POINTER(_Descriptor)]),
            'libusb_get_bus_number': (C.c_uint8, [C.c_void_p]),
            'libusb_get_device_address': (C.c_uint8, [C.c_void_p]),
            'libusb_get_port_numbers': (C.c_int, [C.c_void_p, C.POINTER(C.c_uint8), C.c_int]),
            'libusb_interrupt_transfer': (C.c_int, [C.c_void_p, C.c_ubyte, C.POINTER(C.c_ubyte),
                C.c_int, C.POINTER(C.c_int), C.c_uint]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self._lib, name)
            function.restype, function.argtypes = result, arguments

    def inventory(self):
        context, devices = C.c_void_p(), C.POINTER(C.c_void_p)()
        if self._lib.libusb_init(C.byref(context)) != 0:
            raise FirmwareError('USB inventory context could not be initialized.')
        try:
            count = self._lib.libusb_get_device_list(context, C.byref(devices))
            if count < 0:
                raise FirmwareError('USB inventory enumeration failed.')
            try:
                if count > 1024:
                    raise FirmwareError('USB inventory exceeded its bound.')
                result = []
                for index in range(count):
                    device, descriptor = devices[index], _Descriptor()
                    if self._lib.libusb_get_device_descriptor(device, C.byref(descriptor)) != 0:
                        raise FirmwareError('USB inventory descriptor read failed.')
                    if (descriptor.vendor, descriptor.product) != (0x17ef, 0x61eb):
                        continue
                    ports = (C.c_uint8 * 7)()
                    depth = self._lib.libusb_get_port_numbers(device, ports, 7)
                    if not 1 <= depth <= 7 or descriptor.length != 18 or descriptor.kind != 1:
                        raise FirmwareError('USB controller topology/descriptor is invalid.')
                    result.append({'bus': self._lib.libusb_get_bus_number(device),
                        'address': self._lib.libusb_get_device_address(device),
                        'ports': list(ports[:depth]), 'vendor': descriptor.vendor,
                        'product': descriptor.product, 'bcd_device': descriptor.revision,
                        'configurations': descriptor.configurations})
                # VID/PID/topology are observations, never support authorization.
                return result
            finally:
                self._lib.libusb_free_device_list(devices, 1)
        finally:
            self._lib.libusb_exit(context)

    def interrupt(self, handle, endpoint, data, timeout_ms):
        buffer = (C.c_ubyte * 64).from_buffer_copy(data)
        transferred = C.c_int()
        status = self._lib.libusb_interrupt_transfer(handle, endpoint, buffer, 64,
            C.byref(transferred), timeout_ms)
        return status, transferred.value, bytes(buffer)


class UsbVersionChannel:
    """Serial, bounded version reads on verified interface 02 OUT03/IN83.

    Caller supplies a previously qualified exclusive handle and a live lease
    check. The callback alone is not that qualification. This class cannot
    acquire the handle, rebind a driver or replace an independent guardian.
    Errors latch the channel unavailable; no uncertain output is retried.
    IN83 also carries ordinary gamepad input (04 3c). A bounded input drain
    and matching response establish request correlation, not a wire nonce or
    proof against every possible delayed duplicate.
    """
    def __init__(self, api, handle, lease_valid, clock=time.monotonic):
        if not handle or not callable(lease_valid) or not callable(clock):
            raise FirmwareError('An owned USB handle and live lease are required.')
        self._api, self._handle, self._valid, self._clock = api, handle, lease_valid, clock
        self._lock, self._failed, self._last_time = threading.Lock(), False, None

    def _remaining(self, deadline):
        now = self._clock()
        if (type(now) not in (int, float) or not math.isfinite(now) or
                self._last_time is not None and now < self._last_time):
            raise FirmwareError('USB qualification clock is invalid.')
        self._last_time = now
        if now >= deadline or self._valid() is not True:
            raise FirmwareError('USB qualification deadline or ownership was lost.')
        # A lease check may block. Compute the timeout after it returns so an
        # expensive ownership check cannot start I/O with an expired deadline.
        after = self._clock()
        if (type(after) not in (int, float) or not math.isfinite(after) or after < now):
            raise FirmwareError('USB qualification clock is invalid.')
        self._last_time = now = after
        # libusb timeout zero means infinite. Never use it or round past deadline.
        milliseconds = min(100, math.floor((deadline - now) * 1000))
        if milliseconds < 1:
            raise FirmwareError('USB qualification deadline expired.')
        return milliseconds

    def _transfer(self, endpoint, data, deadline, maximum_ms=100):
        timeout = min(maximum_ms, self._remaining(deadline))
        result = self._api.interrupt(self._handle, endpoint, data, timeout)
        self._remaining(deadline)
        if (not isinstance(result, tuple) or len(result) != 3 or type(result[0]) is not int
                or type(result[1]) is not int or not 0 <= result[1] <= 64
                or not isinstance(result[2], bytes) or len(result[2]) != 64):
            raise FirmwareError('USB transfer returned invalid byte counts.')
        return result

    def query(self, target, *, deadline, subcommand=4):
        if type(target) is not int or target not in (1, 2, 3, 4):
            raise FirmwareError('Only approved version-query targets are available.')
        if type(subcommand) is not int or subcommand not in (4,6):
            raise FirmwareError('Only version and hardware-generation queries are available.')
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise FirmwareError('USB qualification deadline is invalid.')
        if not self._lock.acquire(blocking=False):
            raise FirmwareError('USB version qualification is already in progress.')
        try:
            if self._failed:
                raise FirmwareError('USB version qualification previously failed.')
            self._remaining(deadline)
            # Ordinary input can be continuous. It is not an updater response
            # or proof of an empty FIFO. Reject any observed control response
            # before OUT, and never treat a partial timeout as an empty queue.
            for _ in range(32):
                status, size, report = self._transfer(0x83, bytes(64), deadline, 5)
                if status == -7 and size == 0:
                    break
                if status != 0 or size != 64:
                    raise FirmwareError('USB receive queue could not be qualified.')
                if report[:2] != bytes((4, 0x3c)):
                    raise FirmwareError('An unexpected control report preceded the version query.')
            packet = bytes((5, 0, 2, subcommand, target)) + bytes(59)
            status, size, _ = self._transfer(3, packet, deadline)
            if status != 0 or size != 64:
                raise FirmwareError('USB version output was incomplete or uncertain.')
            for _ in range(512):
                status, size, reply = self._transfer(0x83, bytes(64), deadline)
                if status == -7 and size == 0:
                    continue
                if status != 0 or size != 64:
                    raise FirmwareError('USB version reply was incomplete.')
                if reply[:5] == bytes((4, 0, 2, subcommand, target)):
                    return reply
            raise FirmwareError('USB version reply was not observed within the bound.')
        except BaseException:
            self._failed = True
            raise
        finally:
            self._lock.release()

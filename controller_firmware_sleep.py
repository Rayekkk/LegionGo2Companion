# SPDX-License-Identifier: BSD-3-Clause
"""Worker-owned login1 sleep/shutdown block inhibitor, independent of Decky.

No sleep/reboot request, no helper daemon and no dependency on either UI.
Administrator override, power loss and kernel failure remain outside this lock.
Real libsystemd/logind behavior still needs a SteamOS validation run.
"""
import ctypes as c
import math
import os
import select
import time

from controller_firmware import FirmwareError


class FirmwareInhibitor:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._bus, self._fd, self._owner = c.c_void_p(), None, None
        self._lib = None
        self._used = False
        self._deadline = None
        self._last_time = None
        self._lost = False

    def _remaining(self):
        now = self._clock()
        if (type(now) not in (int, float) or not math.isfinite(now) or now < 0 or
                (self._last_time is not None and now < self._last_time) or
                now >= self._deadline):
            raise FirmwareError('Firmware inhibition acquisition deadline expired.')
        self._last_time = now
        return min(1_000_000, max(1, int((self._deadline - now) * 1_000_000)))

    @staticmethod
    def _ok(result):
        if result < 0: raise FirmwareError('Firmware sleep/shutdown inhibition is unavailable.')
        return result

    def acquire(self, deadline):
        if self._used: raise FirmwareError('Firmware inhibition cannot be reacquired or replayed.')
        self._used = True
        if (os.name != 'posix' or type(deadline) not in (int, float) or
                not math.isfinite(deadline) or deadline < 0):
            raise FirmwareError('Firmware inhibition requires a bounded Linux system bus.')
        self._deadline = deadline
        try:
            self._remaining()
            if 'DBUS_SYSTEM_BUS_ADDRESS' in os.environ:
                raise FirmwareError('Firmware inhibition cannot use an environment-selected bus.')
            self._lib = c.CDLL('libsystemd.so.0')
            p, s, pp = c.c_void_p, c.c_char_p, c.POINTER(c.c_void_p)
            signatures = {
                'sd_bus_open_system': ([pp], c.c_int),
                'sd_bus_set_method_call_timeout': ([p, c.c_uint64], c.c_int),
                'sd_bus_call_method': ([p, s, s, s, s, p, pp, s], c.c_int),
                'sd_bus_message_read': ([p, s], c.c_int),
                'sd_bus_message_unref': ([p], p),
                'sd_bus_close_unref': ([p], p),
                'sd_bus_is_open': ([p], c.c_int),
                'sd_bus_process': ([p, pp], c.c_int),
                'sd_bus_message_is_signal': ([p, s, s], c.c_int),
                'sd_bus_message_get_sender': ([p], s),
            }
            for name, (args, result) in signatures.items():
                fn = getattr(self._lib, name); fn.argtypes = args; fn.restype = result
            self._ok(self._lib.sd_bus_open_system(c.byref(self._bus)))
            self._remaining()
            self._call(b'org.freedesktop.DBus', b'/org/freedesktop/DBus', b'org.freedesktop.DBus',
                b'AddMatch', b's', [b"type='signal',sender='org.freedesktop.DBus',path='/org/freedesktop/DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.freedesktop.login1'"])
            self._call(b'org.freedesktop.DBus', b'/org/freedesktop/DBus', b'org.freedesktop.DBus',
                b'GetNameOwner', b's', [b'org.freedesktop.login1'], self._keep_owner)
            # Bind all subsequent calls and signals to this unique owner. A
            # logind restart cannot silently issue a replacement inhibitor.
            for signal in (b'PrepareForSleep', b'PrepareForShutdown'):
                match = (b"type='signal',sender='" + self._owner + b"',path='/org/freedesktop/login1',interface='org.freedesktop.login1.Manager',member='" + signal + b"'")
                self._call(b'org.freedesktop.DBus', b'/org/freedesktop/DBus', b'org.freedesktop.DBus', b'AddMatch', b's', [match])
            self._call(self._owner, b'/org/freedesktop/login1', b'org.freedesktop.login1.Manager',
                b'Inhibit', b'ssss', [b'sleep:shutdown', b'LegionGo2Companion',
                    b'Controller firmware transaction', b'block'], self._keep_fd)
            for prop in (b'PreparingForSleep', b'PreparingForShutdown'):
                self._call(self._owner, b'/org/freedesktop/login1', b'org.freedesktop.DBus.Properties',
                    b'Get', b'ss', [b'org.freedesktop.login1.Manager', prop], self._not_preparing)
            self._remaining()
            if not self.valid(): raise FirmwareError('Firmware inhibition was lost during acquisition.')
        except BaseException:
            self.close()
            raise

    def _call(self, destination, path, interface, member, types, args, consume=None):
        reply = c.c_void_p()
        try:
            self._ok(self._lib.sd_bus_set_method_call_timeout(self._bus, self._remaining()))
            self._ok(self._lib.sd_bus_call_method(self._bus, destination, path, interface, member,
                None, c.byref(reply), types, *(c.c_char_p(x) for x in args)))
            self._remaining()
            if consume is not None: consume(reply)
        finally:
            if reply.value: self._lib.sd_bus_message_unref(reply)

    def _keep_owner(self, reply):
        owner = c.c_char_p()
        if (self._ok(self._lib.sd_bus_message_read(reply, b's', c.byref(owner))) != 1 or
                not owner.value or not owner.value.startswith(b':') or len(owner.value) > 255):
            raise FirmwareError('The login1 system bus owner is invalid.')
        # Unique-name syntax prevents D-Bus match expression injection.
        import re
        if not re.fullmatch(rb':[0-9]+\.[0-9]+', owner.value):
            raise FirmwareError('The login1 system bus owner is invalid.')
        self._owner = bytes(owner.value)

    def _keep_fd(self, reply):
        descriptor = c.c_int(-1)
        if self._ok(self._lib.sd_bus_message_read(reply, b'h', c.byref(descriptor))) != 1 or descriptor.value < 0:
            raise FirmwareError('No firmware inhibition descriptor was received.')
        self._fd = os.dup(descriptor.value)
        os.set_inheritable(self._fd, False)
        os.fstat(self._fd)

    def _not_preparing(self, reply):
        value = c.c_int()
        if (self._ok(self._lib.sd_bus_message_read(reply, b'v', c.c_char_p(b'b'), c.byref(value))) != 1 or value.value):
            raise FirmwareError('Sleep or shutdown is already preparing.')

    def valid(self):
        if self._lost: return False
        valid = self._check()
        if not valid: self._lost = True
        return valid

    def _check(self):
        if self._fd is None or not self._bus.value or self._lib is None: return False
        try:
            os.fstat(self._fd)
            poller = select.poll()
            poller.register(self._fd, select.POLLERR | select.POLLHUP | select.POLLNVAL)
            if poller.poll(0) or self._lib.sd_bus_is_open(self._bus) != 1: return False
            for _ in range(16):
                message = c.c_void_p()
                try:
                    result = self._lib.sd_bus_process(self._bus, c.byref(message))
                    if result < 0: return False
                    if result == 0: return True
                    if not message.value: continue
                    if self._lib.sd_bus_message_is_signal(message, b'org.freedesktop.DBus', b'NameOwnerChanged') > 0:
                        name, old, new = c.c_char_p(), c.c_char_p(), c.c_char_p()
                        if (self._lib.sd_bus_message_get_sender(message) != b'org.freedesktop.DBus' or
                                self._lib.sd_bus_message_read(message, b'sss', c.byref(name), c.byref(old), c.byref(new)) != 1 or
                                name.value == b'org.freedesktop.login1'):
                            return False
                    for signal in (b'PrepareForSleep', b'PrepareForShutdown'):
                        if self._lib.sd_bus_message_is_signal(message, b'org.freedesktop.login1.Manager', signal) > 0:
                            value = c.c_int()
                            if (self._lib.sd_bus_message_get_sender(message) != self._owner or
                                    self._lib.sd_bus_message_read(message, b'b', c.byref(value)) != 1 or value.value):
                                return False
                finally:
                    if message.value: self._lib.sd_bus_message_unref(message)
            return False  # Flooding never creates an unbounded worker poll.
        except (OSError, ValueError): return False

    def close(self):
        # Idempotent release; a cleanup exception must not retain the other FD.
        descriptor, self._fd = self._fd, None
        bus, self._bus = self._bus, c.c_void_p()
        try:
            if descriptor is not None: os.close(descriptor)
        finally:
            if bus.value and self._lib is not None: self._lib.sd_bus_close_unref(bus)

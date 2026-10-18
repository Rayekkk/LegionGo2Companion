# SPDX-License-Identifier: BSD-3-Clause
"""Narrow login1 delay-inhibitor binding through the system's libsystemd ABI.

No daemon, shell commands, pip dependency or sleep requests. Each independent
fan process owns its own connection and inhibitor until EC recovery finishes.
"""
import ctypes as c
import os


class Pause(RuntimeError):
    """Power or process lifecycle: release, keep the saved intent, re-arm later."""


class Retry(RuntimeError):
    """Transient software or timing failure: release, re-arm with a bounded budget."""


class SleepSafety:
    def __init__(self):
        self.bus = c.c_void_p()
        self.inhibitor = None
        self.login_owner = None
        self.lib = c.CDLL('libsystemd.so.0')
        p, s, pp = c.c_void_p, c.c_char_p, c.POINTER(c.c_void_p)
        signatures = {
            'sd_bus_open_system': ([pp], c.c_int),
            'sd_bus_set_method_call_timeout': ([p, c.c_uint64], c.c_int),
            'sd_bus_call_method': ([p, s, s, s, s, p, pp, s], c.c_int),
            'sd_bus_message_read': ([p, s], c.c_int),
            'sd_bus_message_unref': ([p], p),
            'sd_bus_close_unref': ([p], p),
            'sd_bus_process': ([p, pp], c.c_int),
            'sd_bus_message_is_signal': ([p, s, s], c.c_int),
            'sd_bus_message_get_sender': ([p], s),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.lib, name); fn.argtypes = args; fn.restype = result
        try:
            self._ok(self.lib.sd_bus_open_system(c.byref(self.bus)))
            self._ok(self.lib.sd_bus_set_method_call_timeout(self.bus, 1_000_000))
            match = b"type='signal',sender='org.freedesktop.login1',path='/org/freedesktop/login1',interface='org.freedesktop.login1.Manager',member='PrepareForSleep'"
            self._call(b'org.freedesktop.DBus', b'/org/freedesktop/DBus', b'org.freedesktop.DBus', b'AddMatch', b's', [match])
            owner_match = b"type='signal',sender='org.freedesktop.DBus',path='/org/freedesktop/DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.freedesktop.login1'"
            self._call(b'org.freedesktop.DBus', b'/org/freedesktop/DBus', b'org.freedesktop.DBus', b'AddMatch', b's', [owner_match])
            self._call(b'org.freedesktop.DBus', b'/org/freedesktop/DBus', b'org.freedesktop.DBus', b'GetNameOwner', b's', [b'org.freedesktop.login1'], self._owner)
            self._call(b'org.freedesktop.login1', b'/org/freedesktop/login1', b'org.freedesktop.login1.Manager',
                       b'Inhibit', b'ssss', [b'sleep', b'LegionGo2Companion', b'Release custom fan override before sleep', b'delay'], self._keep_fd)
            # A sleep already preparing before AddMatch must also fail closed.
            self._call(b'org.freedesktop.login1', b'/org/freedesktop/login1', b'org.freedesktop.DBus.Properties',
                       b'Get', b'ss', [b'org.freedesktop.login1.Manager', b'PreparingForSleep'], self._initial)
            self.check()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _ok(result):
        if result < 0:
            raise OSError(-result, 'Fan suspend protection is unavailable.')
        return result

    def _call(self, destination, path, interface, member, types, args, consume=None):
        reply = c.c_void_p()
        try:
            self._ok(self.lib.sd_bus_call_method(self.bus, destination, path, interface, member, None,
                                                c.byref(reply), types, *(c.c_char_p(x) for x in args)))
            if consume is not None:
                consume(reply)
        finally:
            if reply.value:
                self.lib.sd_bus_message_unref(reply)

    def _keep_fd(self, reply):
        fd = c.c_int(-1)
        if self._ok(self.lib.sd_bus_message_read(reply, b'h', c.byref(fd))) != 1 or fd.value < 0:
            raise RuntimeError('Missing fan sleep delay inhibitor.')
        self.inhibitor = os.dup(fd.value)  # Message owns the original FD.

    def _initial(self, reply):
        value = c.c_int()
        if self._ok(self.lib.sd_bus_message_read(reply, b'v', c.c_char_p(b'b'), c.byref(value))) != 1 or value.value:
            raise Pause('Sleep is preparing; fan control remains off.')

    def _owner(self, reply):
        owner = c.c_char_p()
        if self._ok(self.lib.sd_bus_message_read(reply, b's', c.byref(owner))) != 1 or not owner.value:
            raise RuntimeError('Missing login1 owner; fan control remains off.')
        self.login_owner = bytes(owner.value)

    def check(self):
        for _ in range(16):
            message = c.c_void_p()
            try:
                result = self._ok(self.lib.sd_bus_process(self.bus, c.byref(message)))
                if result == 0:
                    return
                if message.value and self.lib.sd_bus_message_is_signal(message, b'org.freedesktop.DBus', b'NameOwnerChanged') > 0:
                    name, old, new = c.c_char_p(), c.c_char_p(), c.c_char_p()
                    if self._ok(self.lib.sd_bus_message_read(message, b'sss', c.byref(name), c.byref(old), c.byref(new))) != 1 or name.value == b'org.freedesktop.login1':
                        raise Pause('login1 restarted; fan sleep protection must be renewed.')
                if message.value and self.lib.sd_bus_message_is_signal(message, b'org.freedesktop.login1.Manager', b'PrepareForSleep') > 0:
                    if self.lib.sd_bus_message_get_sender(message) != self.login_owner:
                        raise RuntimeError('Unexpected sleep signal owner; fan control withdrawn.')
                    value = c.c_int()
                    if self._ok(self.lib.sd_bus_message_read(message, b'b', c.byref(value))) != 1 or value.value:
                        raise Pause('Suspend requested; firmware automatic restored before sleep.')
            finally:
                if message.value:
                    self.lib.sd_bus_message_unref(message)
        raise RuntimeError('Fan sleep event queue exceeded its bounded budget.')

    def close(self):
        if self.inhibitor is not None:
            os.close(self.inhibitor); self.inhibitor = None
        if self.bus.value:
            self.lib.sd_bus_close_unref(self.bus); self.bus = c.c_void_p()

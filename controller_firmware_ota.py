# SPDX-License-Identifier: BSD-3-Clause
"""Bounded C0/A0 transport on an already exclusively owned libusb interface02.

This is the actual OTA I/O adapter, not the qualification ABI. Acquisition,
fresh hardware/power preflight and independent process/input restoration are
the worker coordinator's responsibilities. It is used only by an admitted
independent update plan, never by UI polling or plugin startup.
"""
import math
import threading
import time

from controller_firmware import FirmwareError
from controller_firmware_protocol import OTA_TARGETS
from controller_firmware_transfer import Command, InstalledObservation
from controller_firmware_worker import ControllerClaim


class OtaTransport:
    """One serial component transaction, on one owned handle, without retries.

    Coordinator supplies a raw Libusb API (NOT LinuxUsbOwnership's version-only
    qualification method), a verified claim, current ownership callback,
    fresh post-update observer and cleanup. It keeps this process independent
    of Decky and preserves the entire update plan until it exits.
    """
    def __init__(self, component, api, handle, claim, valid, observe, close,
                 clock=time.monotonic):
        if (component not in OTA_TARGETS or not handle or not isinstance(claim, ControllerClaim)
                or not all(callable(f) for f in (valid, observe, close, clock))):
            raise FirmwareError('An owned controller transport is required.')
        self.component, self.api, self.handle = component, api, handle
        self.claim, self._valid, self._observe, self._close, self.clock = claim, valid, observe, close, clock
        self.closed = self.failed = self.acquired = False
        self.sequence = 0
        self._last_time = None
        self._busy = threading.Lock()

    def _now(self):
        now = self.clock()
        if (type(now) not in (int, float) or not math.isfinite(now) or now < 0 or
                self._last_time is not None and now < self._last_time):
            raise FirmwareError('The controller transport clock is invalid.')
        self._last_time = now
        return now

    def valid(self, claim):
        return not self.closed and not self.failed and claim is self.claim and self._valid() is True

    def _timeout(self, deadline, cap=100):
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise FirmwareError('Controller I/O needs a finite deadline.')
        if not self.valid(self.claim):
            raise FirmwareError('Controller transport ownership was lost.')
        remaining = math.floor((deadline - self._now()) * 1000)
        if remaining < 1:
            raise FirmwareError('Controller I/O deadline expired.')
        return min(cap, remaining)

    def _transfer(self, endpoint, packet, deadline, cap=100, *, empty_timeout=False):
        result = self.api.interrupt(self.handle, endpoint, packet, self._timeout(deadline, cap))
        if (not isinstance(result, tuple) or len(result) != 3 or
                type(result[0]) is not int or type(result[1]) is not int or
                not 0 <= result[1] <= 64 or type(result[2]) is not bytes or len(result[2]) != 64):
            raise FirmwareError('Controller I/O returned an invalid report.')
        # A libusb empty-IN timeout may return slightly after the polling slice.
        # The engine enforces the actual ACK/transaction deadline afterwards.
        # Output, received bytes and partial/uncertain timeouts stay strict.
        now = self._now()
        if not self.valid(self.claim) or (now > deadline and not (
                empty_timeout and result[0] == -7 and result[1] == 0)):
            raise FirmwareError('Controller I/O deadline or ownership was lost.')
        return result

    def acquire(self, component, deadline):
        if self.acquired or component != self.component:
            raise FirmwareError('The controller transport cannot be reused.')
        self._timeout(deadline)
        self.acquired = True
        return self.claim

    def _enter(self):
        if not self._busy.acquire(blocking=False):
            raise FirmwareError('Controller I/O is already running.')
        if self.closed or self.failed or not self.acquired:
            self._busy.release()
            raise FirmwareError('Controller I/O is unavailable.')

    def write(self, command, deadline):
        self._enter()
        try:
            if (not isinstance(command, Command) or command.sequence != self.sequence + 1 or
                    len(command.report) != 64 or command.report[:3] != b'\x05\x00\xc0' or
                    command.report[3] not in (1, 2, 3, 4, 5) or
                    command.report[4] != OTA_TARGETS[self.component]):
                raise FirmwareError('The updater issued an invalid controller command.')
            if self.sequence == 0:
                # Reject pre-existing control replies; ordinary input need not
                # stop. No on-wire nonce exists in the vendor protocol.
                for _ in range(32):
                    rc, count, report = self._transfer(0x83, bytes(64), deadline, 5)
                    if rc == -7 and count == 0: break
                    if rc != 0 or count != 64 or report[:2] != b'\x04\x3c':
                        raise FirmwareError('An unexpected reply preceded the update.')
            self.sequence = command.sequence  # never retry uncertain output
            rc, count, _ = self._transfer(3, command.report, deadline)
            if rc != 0 or count != 64:
                raise FirmwareError('The controller update write was incomplete.')
            return count
        except BaseException:
            self.failed = True
            raise
        finally:
            self._busy.release()

    def read(self, deadline):
        self._enter()
        try:
            while self._now() < deadline:
                if deadline - self._now() < .001:
                    time.sleep(max(0, deadline - self._now()))
                    return None
                rc, count, report = self._transfer(0x83, bytes(64), deadline, empty_timeout=True)
                if rc == -7 and count == 0: continue
                if rc != 0 or count != 64:
                    raise FirmwareError('The controller update reply was incomplete.')
                if report[:2] == b'\x04\x3c': continue
                if (report[:2] != b'\x04\x00' or report[2] not in (0xc0, 0xa0) or
                        report[4] != OTA_TARGETS[self.component]):
                    raise FirmwareError('An unexpected controller update reply was received.')
                # TransferSession validates command/status/ACK boundaries and
                # retains early A0 completion while requiring the finish ACK.
                return report
            return None
        except BaseException:
            self.failed = True
            raise
        finally:
            self._busy.release()

    def observe(self, token, deadline):
        self._enter()
        try:
            self._timeout(deadline)
            observation = self._observe(self.component, token, deadline)
            if observation is not None and not isinstance(observation, InstalledObservation):
                raise FirmwareError('Fresh controller version observation is required.')
            if self._now() > deadline and observation is not None:
                raise FirmwareError('Controller version observation exceeded its deadline.')
            return observation
        except BaseException:
            self.failed = True
            raise
        finally:
            self._busy.release()

    def close(self):
        if self.closed: return
        self.closed = True
        self._close()

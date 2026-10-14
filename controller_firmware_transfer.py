# SPDX-License-Identifier: BSD-3-Clause
"""Nonblocking OTA transaction core. This module never opens a device.

The independently supervised Linux adapter enforces ownership, I/O deadlines,
reply attribution, identity and inhibitors. The core schedules authenticated
packets, journals intent and classifies outcomes; polling cannot start it.
"""
from dataclasses import dataclass
import math
import re
import uuid

from controller_firmware import FirmwareError, validate_image
import controller_firmware_protocol as protocol

TERMINAL = frozenset(('succeeded', 'failed_before_write', 'uncertain'))


def _seconds(value):
    if type(value) not in (int, float) or not .1 <= value <= 3600 or not math.isfinite(value):
        raise FirmwareError('Invalid firmware transaction timeout policy.')
    return float(value)


def _timestamp(value):
    if type(value) not in (int, float) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _binding(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise FirmwareError('The controller identity binding is not confirmed.')
    return value


@dataclass(frozen=True)
class TransferPolicy:
    # Supplied by a separately reviewed adapter, never from a UI selection.
    # Explicit None follows LegionSpace's negotiated positive signed int32 boundary.
    # A set remains useful for narrower qualified adapters and offline tests.
    allowed_boundaries: frozenset[int] | None
    write_timeout: float
    completion_timeout: float
    verification_timeout: float
    transaction_timeout: float

    def __post_init__(self):
        if self.allowed_boundaries is not None and (type(self.allowed_boundaries) is not frozenset or not self.allowed_boundaries or
                any(type(v) is not int or not 1 <= v <= protocol.MAX_STREAM_SIZE for v in self.allowed_boundaries)):
            raise FirmwareError('Acknowledgement boundaries need an explicit adapter policy.')
        for name in ('write_timeout', 'completion_timeout', 'verification_timeout', 'transaction_timeout'):
            _seconds(getattr(self, name))


@dataclass(frozen=True)
class Command:
    sequence: int
    kind: str
    report: bytes
    needs_reply: bool
    reply_timeout: float
    end_bytes: int = 0


@dataclass(frozen=True)
class InstalledObservation:
    """Adapter-acquired fresh identity and query reply, not UI/cached settings.

    The token is local request correlation, not an on-wire freshness field.
    Linux acquisition/freshness remains an unvalidated adapter responsibility.
    """
    device_binding: str
    model: str
    hardware_generation: int
    connection_epoch: str
    sampled_at: float
    verification_token: str
    version_report: bytes


class TransferSession:
    """One session only; no retries/resume or I/O hidden inside transitions.

    next_command journals possible activity before handing a packet out. The
    caller reports a successful exact-length write through sent(), then delivers
    only freshly attributed replies. Snapshot byte counts are not success.
    Checkpoints are critical-phase writes plus progress at most once per second.
    A single worker actor must call transitions serially. This is not a shared
    thread-safe RPC object or a substitute for an independently supervised worker.
    """
    def __init__(self, component, original, policy, checkpoint, *, release=None):
        if not isinstance(policy, TransferPolicy) or not callable(checkpoint):
            raise FirmwareError('Transfer policy and durable checkpoint are required.')
        # Revalidate even if a caller already had a ValidatedImage object.
        self._image = (validate_image(component, original) if release is None else
            validate_image(component, original, release=release))
        self._policy, self._checkpoint = policy, checkpoint
        self._job = uuid.uuid4().hex
        self._phase, self._error = 'idle', ''
        self._ever_issued = False
        self._pending = None
        self._sequence = self._sent_bytes = self._acknowledged_bytes = 0
        self._boundary = None
        self._binding = self._epoch = ''
        self._deadline = self._overall_deadline = None
        self._clock_value = None
        self._last_checkpoint = -math.inf
        self._completion_at = None
        self._verification_at = None
        self._completion_seen = False
        self._verification_token = ''
        self._journal_error = False

    def snapshot(self):
        release = self._image.release
        return {'schema': 1, 'job_id': self._job, 'component': release.component,
            'image_sha256': release.sha256, 'offered_version': release.version,
            'total_bytes': len(self._image.original), 'phase': self._phase,
            'write_attempted': self._ever_issued or self._pending is not None,
            'sent_bytes': self._sent_bytes, 'acknowledged_bytes': self._acknowledged_bytes,
            'sequence': self._sequence, 'boundary': self._boundary,
            'device_binding': self._binding, 'connection_epoch': self._epoch,
            'verification_token': self._verification_token,
            'error': self._error, 'journal_error': self._journal_error}

    def _save(self, now, critical=False):
        if critical or now - self._last_checkpoint >= 1:
            try:
                self._checkpoint(self.snapshot())
            except Exception:
                self._journal_error = True
                self._fail('The firmware journal could not be saved.')
            self._last_checkpoint = now

    def _fail(self, message):
        if self._phase not in TERMINAL:
            self._phase = 'uncertain' if self._ever_issued else 'failed_before_write'
            self._error, self._pending, self._deadline = message, None, None
            try:
                self._checkpoint(self.snapshot())
            except Exception:
                self._journal_error = True
        raise FirmwareError(message)

    def _check(self, now):
        if self._phase in TERMINAL:
            raise FirmwareError('This firmware session is closed. It cannot be replayed.')
        if (not _timestamp(now) or
                (self._clock_value is not None and now < self._clock_value)):
            self._fail('The firmware transaction clock is invalid.')
        self._clock_value = float(now)
        if ((self._overall_deadline is not None and now >= self._overall_deadline) or
                (self._deadline is not None and now >= self._deadline)):
            self._fail('The firmware transaction timed out. It will not be retried.')

    def start(self, preflight, epoch, device_binding, now):
        self._check(now)
        if self._phase != 'idle':
            self._fail('The firmware transaction was already started.')
        try:
            if not isinstance(preflight, protocol.Preflight):
                raise FirmwareError('Fresh controller preflight is required.')
            preflight.require(self._image.release.version, epoch, now)
            self._binding = _binding(device_binding)
        except FirmwareError as exc:
            self._fail(str(exc))
        self._epoch = epoch
        self._overall_deadline = now + self._policy.transaction_timeout
        self._phase = 'ready_start'
        self._save(now, True)

    def next_command(self, now):
        self._check(now)
        component, original = self._image.release.component, self._image.original
        kind = self._phase.removeprefix('ready_')
        if self._phase == 'ready_start':
            report, timeout, end, needs = protocol.start_report(component), .5, 0, True
        elif self._phase == 'ready_metadata':
            report, timeout, end, needs = protocol.metadata_report(component, original), 1., 0, True
        elif self._phase == 'ready_boundary':
            report, timeout, end, needs = protocol.boundary_report(component), .5, 0, True
        elif self._phase == 'ready_block':
            chunk = original[self._sent_bytes:self._sent_bytes+32]
            end = self._sent_bytes + len(chunk)
            report = protocol.block_report(component, self._sent_bytes // 32 + 1, chunk)
            timeout, needs = .5, end % self._boundary == 0 or end == len(original)
        elif self._phase == 'ready_finish':
            report, timeout, end, needs = protocol.finish_report(component), 4., 0, True
        else:
            self._fail('A firmware command was requested out of order.')
        self._sequence += 1
        self._pending = Command(self._sequence, kind, report, needs, timeout, end)
        self._phase, self._deadline = 'awaiting_send', now + self._policy.write_timeout
        # Do not return even the first start packet unless intent is durable.
        self._save(now, kind != 'block')
        self._ever_issued = True
        return self._pending

    def sent(self, sequence, bytes_written, now):
        self._check(now)
        if (self._phase != 'awaiting_send' or type(sequence) is not int or
                sequence != self._pending.sequence or type(bytes_written) is not int or bytes_written != 64):
            self._fail('The firmware report write was incomplete or out of order.')
        command = self._pending
        if command.kind == 'block':
            self._sent_bytes = command.end_bytes
        if command.needs_reply:
            self._phase, self._deadline = 'awaiting_reply', now + command.reply_timeout
        else:
            self._phase = 'ready_finish' if self._sent_bytes == len(self._image.original) else 'ready_block'
            self._pending, self._deadline = None, None
        self._save(now)

    def receive(self, report, now):
        self._check(now)
        component = self._image.release.component
        # A0 is delivered on a separate asynchronous path in the vendor host.
        # Buffer an early completion only after the finish write, while still
        # requiring its C0/05 ACK. Never infer success from a timeout counter.
        early_completion = (self._phase == 'awaiting_reply' and self._pending.kind == 'finish')
        if (self._phase == 'awaiting_completion' or early_completion) and isinstance(report, bytes) and report[:4] == b'\x04\x00\xa0\x03':
            try:
                protocol.parse_completion(report, component)
            except FirmwareError as exc:
                self._fail(str(exc))
            if self._completion_seen:
                self._fail('An ambiguous duplicate completion event was received.')
            self._completion_seen, self._completion_at = True, now
            if self._phase == 'awaiting_completion':
                self._begin_verification(now)
            return
        if self._phase != 'awaiting_reply':
            self._fail('An unexpected firmware reply was received.')
        command = self._pending
        try:
            if command.kind == 'boundary':
                self._boundary = protocol.parse_boundary(report, component)
                if self._policy.allowed_boundaries is not None and self._boundary not in self._policy.allowed_boundaries:
                    raise FirmwareError('The acknowledgement boundary is not validated by this adapter.')
            else:
                protocol.parse_ack(report, {'start': 1, 'metadata': 2, 'block': 4, 'finish': 5}[command.kind], component)
        except FirmwareError as exc:
            self._fail(str(exc))
        self._pending, self._deadline = None, None
        if command.kind == 'block':
            self._acknowledged_bytes = command.end_bytes
            self._phase = 'ready_finish' if self._sent_bytes == len(self._image.original) else 'ready_block'
        elif command.kind == 'finish':
            self._phase, self._deadline = 'awaiting_completion', now + self._policy.completion_timeout
            if self._completion_seen:
                self._begin_verification(now)
        else:
            self._phase = {'start': 'ready_metadata', 'metadata': 'ready_boundary', 'boundary': 'ready_block'}[command.kind]
        self._save(now, command.kind != 'block')

    def _begin_verification(self, now):
        self._phase, self._deadline = 'verifying_version', now + self._policy.verification_timeout
        self._verification_at = now
        self._verification_token = uuid.uuid4().hex
        self._save(now, True)

    def confirm_installed(self, observation, now):
        self._check(now)
        if self._phase != 'verifying_version' or not isinstance(observation, InstalledObservation):
            self._fail('A fresh post-update controller observation is required.')
        try:
            if (_binding(observation.device_binding) != self._binding or observation.model != 'Legion Go 8ASP2' or
                    type(observation.hardware_generation) is not int or observation.hardware_generation != 2 or
                    not isinstance(observation.connection_epoch, str) or not observation.connection_epoch or
                    observation.verification_token != self._verification_token):
                raise FirmwareError('Post-update controller identity or request correlation changed.')
            sampled = observation.sampled_at
            if (not _timestamp(sampled) or
                    not self._verification_at <= sampled <= now or now - sampled > 2):
                raise FirmwareError('Post-update controller version is not a fresh observation.')
            version = protocol.parse_firmware_version(observation.version_report, self._image.release.component)
            if version != self._image.release.version:
                raise FirmwareError('Installed firmware does not match the prepared image.')
        except FirmwareError as exc:
            self._fail(str(exc))
        self._phase, self._deadline = 'succeeded', None
        # A durable failure here must not leave an in-memory success claim.
        try:
            self._checkpoint(self.snapshot())
        except Exception:
            self._phase, self._error, self._journal_error = 'uncertain', 'Final firmware result could not be saved.', True
            raise FirmwareError(self._error)

    def tick(self, now):
        self._check(now)
        return self.snapshot()

    def deadline(self):
        """Earliest operation/transaction deadline for the serial I/O adapter.

        No blocking or clock read is hidden here. The worker must still tick
        after each bounded adapter call and cannot use this to extend a wait.
        """
        values = [value for value in (self._deadline, self._overall_deadline) if value is not None]
        return min(values) if values else None

    def abort(self):
        # The eventual worker uses this for faults, not frontend close/cancel.
        self._fail('Controller transport or supervision was lost.')

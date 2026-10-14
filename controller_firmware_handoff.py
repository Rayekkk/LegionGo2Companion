# SPDX-License-Identifier: BSD-3-Clause
"""Pure one-shot interface-02 qualification planner, not a hardware adapter.

It orders verified observations, durable intents and recovery. The trusted
adapter must obtain the facts from authenticated processes/kernel readback;
flags/hashes supplied to this planner are not those proofs. No CLI, RPC,
startup, USB library, consumer stop or firmware write calls this planner.
Actual USB ownership/restoration remains unqualified. Qualification timeouts
must never be reused to kill an updater after a firmware write has started.
"""
from dataclasses import dataclass
import math
import re
import uuid

from controller_firmware import FirmwareError

# Absolute offsets from the original monotonic start; no heartbeat/reset can
# extend them. Each accepted observation binds the same job and baseline.
STEPS = (
    ('arm_rescue', 5, ('rescue_ready', 'independent_cgroups', 'both_inhibitors')),
    ('pause_companion', 20, ('all_instances_paused', 'threads_drained', 'capture_joined')),
    ('publish_fence', 20, ('exact_fence_durable', 'instances_rechecked')),
    ('quiet_consumers', 20, ('controlled_consumers_quiet', 'no_respawn', 'no_foreign_owner')),
    ('open_interface2', 25, ('handle_identity', 'unchanged_attachment', 'unchanged_other_bindings')),
    ('detach_interface2', 25, ('driver_detached', 'unchanged_attachment', 'unchanged_other_bindings')),
    ('claim_interface2', 25, ('own_usbfs_claim', 'old_hid_gone', 'no_foreign_owner', 'unchanged_other_bindings')),
    ('probe', 25, ('probe_finished', 'lease_valid')),
    ('retire_probe', 27, ('probe_dead', 'probe_cgroup_empty', 'no_usb_handle')),
    ('restore_binding', 37, ('usbhid_restored', 'hid_driver_restored', 'configuration_readback', 'unchanged_other_bindings')),
    ('restore_consumers', 47, ('consumer_identity', 'sources_targets', 'profile_filters', 'no_foreign_owner')),
    ('restore_companion', 47, ('retained_intent', 'applied_readback', 'monitor_ready')),
    ('verify', 57, ('baseline_restored', 'no_helpers', 'evidence_complete')),
    ('close', 60, ('exact_fence_released', 'admission_reopened')),
)
RECOVERY = STEPS[8:]


@dataclass(frozen=True)
class QualificationAction:
    sequence: int
    kind: str
    deadline: float
    target: int | None = None
    interface: int = 2


@dataclass(frozen=True)
class QualificationObservation:
    job_id: str
    baseline_sha256: str
    boot_id: str
    sequence: int
    facts: frozenset[str]


class QualificationSession:
    """A serial actor; never treat this as a thread-safe or untrusted RPC API."""
    def __init__(self, baseline_sha256, boot_id, started_at, boottime_at,
                 checkpoint, *, version_target=None):
        if (not isinstance(baseline_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', baseline_sha256)
                or not isinstance(boot_id, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot_id)
                or not callable(checkpoint) or not self._time(started_at) or not self._time(boottime_at)
                or version_target is not None and (type(version_target) is not int or version_target not in (1, 2, 3, 4))):
            raise FirmwareError('Invalid fixed controller qualification manifest.')
        self._baseline, self._boot, self._checkpoint = baseline_sha256, boot_id, checkpoint
        self._job, self._target = uuid.uuid4().hex, version_target
        self._start, self._boottime, self._last = float(started_at), float(boottime_at), float(started_at)
        self._steps, self._index, self._sequence = STEPS, 0, 0
        self._pending, self._uncertain = None, None
        self._phase, self._error = 'preflight', ''
        self._journal_failed, self._lease, self._query_attempted = False, False, False
        self._cleanup_started=False
        self._outcomes = {key: 'not_observed' for key in ('probe', 'usb_restore', 'consumer_restore', 'companion_restore', 'evidence', 'fence')}

    @staticmethod
    def _time(value):
        if type(value) not in (int, float) or value < 0:return False
        try:return math.isfinite(value)
        except OverflowError:return False

    def snapshot(self):
        return {'schema':1, 'job_id':self._job, 'baseline_sha256':self._baseline,
                'boot_id':self._boot, 'phase':self._phase, 'error':self._error,
                'pending':self._pending.kind if self._pending else None,
                'uncertain_action':self._uncertain, 'lease_valid':self._lease,
                'query_attempted':self._query_attempted, 'target':self._target,
                'journal_failed':self._journal_failed, 'flash_available':False,
                'outcomes':dict(self._outcomes)}

    def _save(self):
        try:self._checkpoint(self.snapshot())
        except Exception:
            self._journal_failed=True
            self._lease=False
            raise FirmwareError('Qualification journal could not be confirmed.') from None

    def _clock(self, now, boottime, boot_id):
        if (not self._time(now) or not self._time(boottime) or now < self._last
                or boot_id!=self._boot or abs((boottime-self._boottime)-(now-self._start))>.5):
            self._lease=False
            self._uncertain=self._pending.kind if self._pending else self._uncertain
            self._pending=None
            self._phase='recovery_required'
            try:self._save()
            except FirmwareError:pass
            raise FirmwareError('Qualification clock/boot changed; independent recovery required.')
        self._last=float(now)

    def fail(self, message):
        """Retire pending intent, never replay or infer that it had no effect."""
        if self._phase in ('closed','recovery_required'):return
        self._uncertain=self._pending.kind if self._pending else self._uncertain
        if self._pending is not None and self._pending.kind=='probe':
            self._outcomes['probe']='uncertain'
        self._pending=None
        self._lease=False
        self._error=str(message)[:256]
        if self._phase=='restoring' or self._cleanup_started:self._phase='recovery_required'
        else:
            self._phase='restoring'
            self._steps,self._index=RECOVERY,0
        try:self._save()
        except FirmwareError:pass  # Cleanup is still needed; keep fence/evidence fault.

    def lease_valid(self, now, boottime, boot_id):
        try:self._clock(now,boottime,boot_id)
        except FirmwareError:return False
        if self._lease and now<self._start+25:return True
        if self._lease:self.fail('USB qualification lease expired.')
        return False

    def next_action(self, now, boottime, boot_id):
        self._clock(now,boottime,boot_id)
        if self._phase in ('closed','recovery_required'):return None
        if self._pending is not None:
            raise FirmwareError('An uncertain/in-flight action cannot be repeated.')
        kind,offset,_=self._steps[self._index]
        deadline=self._start+(100 if self._phase=='restoring' else offset)
        if now>=deadline:
            if self._phase=='restoring':self._phase='recovery_required'
            else:self.fail('Qualification phase deadline expired.')
            raise FirmwareError('Qualification phase deadline expired.')
        if kind=='probe' and not self._lease:
            self.fail('USB claim evidence is missing.')
            raise FirmwareError('USB claim evidence is missing.')
        if kind=='close' and self._journal_failed:
            self._phase='recovery_required'
            raise FirmwareError('Uncertain evidence cannot authorize removal of the fence.')
        self._sequence+=1
        self._pending=QualificationAction(self._sequence,kind,deadline,
            self._target if kind=='probe' else None)
        if kind in {step[0] for step in RECOVERY}:self._cleanup_started=True
        if kind=='probe' and self._target is not None:
            self._query_attempted=True
        try:self._save()  # Intent must be durable before the action is returned.
        except FirmwareError:
            self.fail('Qualification journal failed before action admission.')
            raise
        return self._pending

    def complete(self, action, observation, now, boottime, boot_id):
        self._clock(now,boottime,boot_id)
        if self._phase in ('closed','recovery_required'):
            raise FirmwareError('Terminal qualification cannot accept a late completion.')
        if action is None or action is not self._pending:
            raise FirmwareError('Qualification action owner/sequence does not match.')
        if now>=action.deadline:
            self.fail('Qualification action exceeded its deadline.')
            raise FirmwareError('Qualification action exceeded its deadline.')
        required=set(self._steps[self._index][2])
        if action.kind=='probe':
            required.add('no_application_output' if self._target is None else 'single_version_reply')
        if (type(observation) is not QualificationObservation or observation.job_id!=self._job
                or observation.baseline_sha256!=self._baseline or observation.boot_id!=self._boot
                or type(observation.sequence) is not int or observation.sequence!=action.sequence
                or type(observation.facts) is not frozenset or not required.issubset(observation.facts)
                or any(not isinstance(fact,str) or len(fact)>64 for fact in observation.facts)):
            self.fail('Qualification observation is incomplete or foreign.')
            raise FirmwareError('Qualification observation is incomplete or foreign.')
        if action.kind=='claim_interface2':self._lease=True
        if action.kind=='retire_probe':self._lease=False
        outcomes={'probe':'probe','restore_binding':'usb_restore','restore_consumers':'consumer_restore',
                  'restore_companion':'companion_restore','verify':'evidence','close':'fence'}
        if action.kind in outcomes:self._outcomes[outcomes[action.kind]]='confirmed'
        self._pending=None
        self._index+=1
        if action.kind=='close':self._phase='closed'
        elif self._phase!='restoring':self._phase=action.kind+'_confirmed'
        try:self._save()
        except FirmwareError:
            # A confirmed physical action with an unconfirmed commit is still
            # recoverable, but evidence never becomes complete by assumption.
            self._outcomes['evidence']='uncertain'
            if self._phase=='closed':
                self._phase='recovery_required'
                self._outcomes['fence']='uncertain'
            else:self.fail('Qualification result commit was uncertain.')
            raise

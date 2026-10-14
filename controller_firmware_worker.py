# SPDX-License-Identifier: BSD-3-Clause
"""Serial process-worker engine with injected Linux capabilities.

The production installer supplies the owned Linux C0 adapter and independent
systemd worker. Platform ownership, inhibition, bounded I/O and actual reply
attribution belong to that adapter; tests also inject simulated transports.
"""
from dataclasses import dataclass
import json
import time

from controller_firmware import FirmwareError, ValidatedImage, validate_image, version_number
from controller_firmware_transfer import TransferSession, TERMINAL, _timestamp
from diagnostics import event

WAIT_SLICE = .1


@dataclass(frozen=True)
class ControllerClaim:
    device_binding: str
    connection_epoch: str
    preflight: object


class FirmwareWorker:
    """Run one authenticated transaction with one journal/transport owner.

    Required transport methods:
      acquire(component, deadline) -> ControllerClaim with fresh preflight
      valid(claim) -> bool; write(command, deadline) -> exact byte count
      read(deadline) -> fresh attributed bytes or None after bounded wait
      observe(token, deadline) -> fresh InstalledObservation or None
      close() releases only this worker's resources, never device rollback.

    write must recheck exclusive ownership atomically with the output and retain
    replies arriving during output. read/observe must wait until their deadline
    rather than busy-polling. All operations must respect absolute monotonic
    deadlines; this Python engine cannot interrupt a stalled kernel call.

    Inhibitor acquire(deadline)/valid()/close() protect sleep/shutdown while the
    process owns the transaction. A boolean is not proof of a Linux inhibitor.
    Journal save()/close() must be private/durable/exclusive before first output.
    accept() is a one-shot authorization handoff, not a UI heartbeat. Once
    accepted, frontend or Decky disappearance cannot cancel an active transfer.
    """
    def __init__(self, component, original, policy, transport, inhibitor, journal,
                 accept, clock=time.monotonic, *, release=None):
        if not callable(accept) or not callable(clock):
            raise FirmwareError('Worker authorization and monotonic clock are required.')
        self._component, self._original, self._policy = component, original, policy
        self._transport, self._inhibitor, self._journal = transport, inhibitor, journal
        self._accept, self._clock = accept, clock
        self._session = None
        self._release = release
        self._used = False
        self._last_time = None

    def _now(self):
        value = self._clock()
        if not _timestamp(value) or (self._last_time is not None and value < self._last_time):
            raise FirmwareError('The firmware worker clock is invalid.')
        self._last_time = value
        return value

    def _guards(self, claim):
        if self._inhibitor.valid() is not True or self._transport.valid(claim) is not True:
            raise FirmwareError('Firmware worker ownership or inhibition was lost.')

    def run(self):
        if self._used:
            raise FirmwareError('This firmware worker cannot be restarted or replayed.')
        self._used = True
        error, cleanup_errors = '', []
        stage = 'authentication'
        event('firmware','component_worker_started',scope=self._component)
        try:
            # Authentication precedes all acquisition/authorization and output.
            self._session = TransferSession(self._component, self._original,
                self._policy, self._journal.save, release=self._release)
            acquisition_deadline = self._now() + self._policy.verification_timeout
            stage = 'inhibition'
            self._inhibitor.acquire(acquisition_deadline)
            if self._inhibitor.valid() is not True:
                raise FirmwareError('Firmware inhibition was not acquired.')
            stage = 'exclusive_acquisition'
            claim = self._transport.acquire(self._component, acquisition_deadline)
            if self._now() >= acquisition_deadline or not isinstance(claim, ControllerClaim):
                raise FirmwareError('Fresh controller ownership was not acquired in time.')
            self._guards(claim)
            self._session.start(claim.preflight, claim.connection_epoch,
                claim.device_binding, self._now())
            stage = 'authorization'
            if self._accept() is not True:
                raise FirmwareError('The firmware transaction was not authorized.')
            claim.preflight.require(self._session.snapshot()['offered_version'],
                claim.connection_epoch, self._now())
            stage = 'transfer'
            while self._session.snapshot()['phase'] not in TERMINAL:
                now = self._now()
                self._session.tick(now)
                self._guards(claim)
                phase = self._session.snapshot()['phase']
                if phase.startswith('ready_'):
                    command = self._session.next_command(self._now())
                    self._session.tick(self._now())
                    self._guards(claim)
                    if command.sequence == 1:
                        # Authorization and durable intent can take time. A
                        # still-owned lease does not make old power/attachment
                        # observations fresh enough to start device output.
                        claim.preflight.require(self._session.snapshot()['offered_version'],
                            claim.connection_epoch, self._now())
                    count = self._transport.write(command, self._session.deadline())
                    self._session.sent(command.sequence, count, self._now())
                elif phase in ('awaiting_reply', 'awaiting_completion'):
                    before = self._now()
                    until = min(self._session.deadline(), before + WAIT_SLICE)
                    response = self._transport.read(until)
                    now = self._now()
                    self._session.tick(now)
                    self._guards(claim)
                    if response is None and now < until:
                        raise FirmwareError('The transport did not perform a bounded wait.')
                    if response is not None:
                        self._session.receive(response, now)
                elif phase == 'verifying_version':
                    before = self._now()
                    until = min(self._session.deadline(), before + WAIT_SLICE)
                    observation = self._transport.observe(
                        self._session.snapshot()['verification_token'], until)
                    now = self._now()
                    self._session.tick(now)
                    self._guards(claim)
                    if observation is None and now < until:
                        raise FirmwareError('The observation did not perform a bounded wait.')
                    if observation is not None:
                        self._session.confirm_installed(observation, now)
                else:
                    raise FirmwareError('Unexpected firmware worker phase.')
        except BaseException as exc:
            event('firmware','component_worker_failed',level='warning',error=exc,scope=self._component,
                stage=stage,phase=self._session.snapshot()['phase'] if self._session is not None else 'uninitialized')
            # Fixed messages never copy raw exception text, device IDs or secrets
            # to the journal/result. SIGKILL/power loss cannot execute this path.
            error = 'The firmware worker could not complete the transaction.'
            if self._session is not None and self._session.snapshot()['phase'] not in TERMINAL:
                try:
                    self._session.abort()
                except BaseException as exc:
                    event('firmware','component_abort_evidence_failed',level='error',error=exc,scope=self._component)
        finally:
            # Keep journal and inhibitor until our device handles are closed.
            # Process exit closes OS handles; cleanup failure remains explicit.
            for name, resource in (('transport', self._transport),
                    ('inhibitor', self._inhibitor), ('journal', self._journal)):
                try:
                    resource.close()
                except BaseException as exc:
                    event('firmware','component_cleanup_failed',level='error',error=exc,scope=self._component,stage=name)
                    cleanup_errors.append(name)
        snapshot = self._session.snapshot() if self._session is not None else None
        if snapshot is not None and snapshot['phase'] != 'succeeded' and not error:
            error = 'The firmware transaction did not complete.'
        event('firmware','component_worker_finished',scope=self._component,
            phase=snapshot['phase'] if snapshot is not None else 'uninitialized',count=len(cleanup_errors),
            level='warning' if error or cleanup_errors else 'info')
        return {'snapshot': snapshot, 'error': error, 'cleanup_errors': cleanup_errors,
            'cleanup_complete': not cleanup_errors}


def run_update_plan(images, installed_versions, policy, transport_factory,
                    journal_factory, inhibitor, authorize, restore, progress,
                    clock=time.monotonic):
    """Run the complete selected plan in ONE independently launched process.

    The Linux coordinator keeps its global ownership/maintenance claim until
    this function restores input and returns. Sessions own only their transport
    and journal. One shared inhibitor stays held across all components; UI loss
    never cancels the admitted plan. Factories and restoration are private worker
    capabilities, never frontend arguments or loadable user-selected adapters.
    The installed build explicitly gates device transfer; startup never updates.
    """
    if (type(images) is not tuple or not images or len(images) > 3 or
            not all(isinstance(image, ValidatedImage) for image in images)):
        raise FirmwareError('A validated controller update plan is required.')
    order = ['left', 'right', 'receiver']
    components = [image.release.component for image in images]
    if components != [c for c in order if c in components] or len(set(components)) != len(components):
        raise FirmwareError('The controller update plan is ambiguous.')
    if not isinstance(installed_versions, dict) or set(installed_versions) != set(order):
        raise FirmwareError('Versions for the complete controller set are required.')
    versions = dict(installed_versions)
    for value in versions.values(): version_number(value)
    # Every image authenticates before ANY acquisition or authorisation.
    images = tuple(validate_image(image.release.component, image.original, release=image.release) for image in images)
    completed, errors, attempted = [], [], False
    cleanup_errors = []
    admitted = False
    restored = False
    outcome = 'failed_before_write'

    class PlanInhibitor:
        def acquire(self, deadline):
            if not self.valid(): inhibitor.acquire(deadline)
        def valid(self): return inhibitor.valid()
        def close(self): pass  # parent owns it until the whole plan is restored

    def accept():
        nonlocal admitted
        if not admitted:
            admitted = authorize() is True
        return admitted

    try:
        for image in images:
            component = image.release.component
            progress('installing', 'Updating controllers. Keep both controllers attached.')
            transport = transport_factory(component)
            try:
                journal = journal_factory(component)
            except BaseException:
                transport.close()
                raise
            worker = FirmwareWorker(component, image.original, policy, transport,
                PlanInhibitor(), journal, accept, clock=clock, release=image.release)
            result = worker.run()
            snapshot = result.get('snapshot') or {}
            attempted = attempted or snapshot.get('write_attempted') is True
            if (snapshot.get('phase') != 'succeeded' or result.get('error') or
                    result.get('cleanup_complete') is not True or result.get('cleanup_errors')):
                cleanup_errors.extend(result.get('cleanup_errors') or [])
                if result.get('cleanup_complete') is not True and not result.get('cleanup_errors'):
                    cleanup_errors.append('component resources')
                errors.append('The controller update did not complete.')
                outcome = 'uncertain' if attempted or completed else 'failed_before_write'
                break
            # FirmwareWorker requires a fresh query matching this version.
            versions[component] = image.release.version
            completed.append(component)
        else:
            outcome = 'succeeded'
    except BaseException as exc:
        event('firmware','plan_failed',level='warning',error=exc,count=len(completed))
        errors.append('The controller update worker failed.')
        # Factories may fail around acquisition. Conservatively retain an
        # uncertain outcome once any preceding component has been touched.
        outcome = 'uncertain' if attempted or completed or admitted else 'failed_before_write'
    finally:
        try:
            progress('restoring', 'Reconnecting controllers.')
        except BaseException as exc:
            event('firmware','plan_cleanup_failed',level='error',error=exc,stage='status_evidence')
            errors.append('The update status could not be saved.')
            cleanup_errors.append('status evidence')
        try:
            restored = restore() is True
            if not restored:
                errors.append('Controller input restoration was not confirmed.');cleanup_errors.append('restoration')
        except BaseException as exc:
            event('firmware','plan_cleanup_failed',level='error',error=exc,stage='restoration')
            errors.append('Controller input restoration failed.')
            cleanup_errors.append('restoration')
        try:
            inhibitor.close()
        except BaseException as exc:
            event('firmware','plan_cleanup_failed',level='error',error=exc,stage='inhibitor')
            errors.append('The update inhibitor could not be released.')
            cleanup_errors.append('inhibitor')
        if errors and outcome == 'succeeded': outcome = 'uncertain'
    return {'phase': outcome, 'restored': restored, 'versions': versions,
        'completed': completed, 'cleanup_complete': not cleanup_errors,
        'cleanup_errors': cleanup_errors, 'error': '' if not errors else 'The controller update could not be completed and verified.'}


def main():
    from pathlib import Path
    if Path(__file__).with_name('controller_firmware_install.py').is_file():
        from controller_firmware_install import OTA_ENABLED, run_staged
        if OTA_ENABLED:
            return run_staged(str(Path.cwd()))
    # Neither arbitrary Python adapter loading nor a user-selected fake transport
    # is exposed by the installed entry point. Device writes stay unavailable.
    print(json.dumps({'flash_available': False,
        'error': 'The Linux controller adapter has not been validated. No device was opened.'}))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())

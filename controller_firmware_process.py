# SPDX-License-Identifier: BSD-3-Clause
"""Frozen artifact and one-shot Linux transient-service handoff.

The explicit shared update action launches a frozen systemd worker. Startup
never submits an update; the private verified bundle survives plugin removal.
Service handoff alone is not proof of successful physical flashing.
"""
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
from types import MappingProxyType

from controller_firmware import FirmwareError, validate_image
from controller_firmware_stage import (RUNTIME_ROOT, SOURCES, MAX_SOURCE, BOOTSTRAP,
    StagedJob, _private_directory, verify_stage)
from controller_firmware_transfer import TransferPolicy
from system_process import system_env
from diagnostics import event



def _job(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise FirmwareError('Invalid firmware process job identity.')
    return value


@dataclass(frozen=True)
class FrozenBundle:
    # Values are immutable bytes and mappings, never paths back into a plugin
    # which can be removed. Producer trust/private staging must still be proved.
    job_id: str
    files: object
    manifest: bytes


def freeze_bundle(job_id, component, original, source_root, *, release=None):
    _job(job_id)
    image = (validate_image(component, original) if release is None else
        validate_image(component, original, release=release))
    root = Path(source_root)
    if not root.is_absolute() or not root.is_dir():
        raise FirmwareError('A trusted absolute worker source directory is required.')
    files = {}
    for name in sorted(SOURCES):
        path = root / name
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_SOURCE:
                raise FirmwareError('Worker source is not a bounded regular file.')
            with path.open('rb') as source:
                opened = os.fstat(source.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise FirmwareError('Worker source identity changed during capture.')
                data = source.read(MAX_SOURCE + 1)
            if not 1 <= len(data) <= MAX_SOURCE:
                raise FirmwareError('Worker source exceeds its size limit.')
        except OSError as exc:
            raise FirmwareError('Required worker source could not be captured.') from exc
        files[name] = data
    files['image.bin'] = image.original
    manifest = {'schema': 1, 'job_id': job_id, 'component': component,
        'image_sha256': image.release.sha256,
        'files': {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            for name, data in files.items()}}
    return FrozenBundle(job_id, MappingProxyType(files),
        (json.dumps(manifest, sort_keys=True) + '\n').encode('ascii'))


@dataclass(frozen=True)
class ServicePlan:
    unit: str
    working_directory: str
    argv: tuple[str, ...]


def service_plan(job_id, runtime_seconds, manifest_sha256, *, cleanup=False):
    """Build a non-shell plan, never execute it or claim unit readiness.

    Staged modules/image/manifest must remain private and intact until confirmed
    service exit, including plugin removal. Fixed system Python runs isolated
    from Decky's frozen runtime. The worker validates the frozen plan before
    acquiring the controller; service submission does not prove a write.
    """
    _job(job_id)
    if not isinstance(manifest_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', manifest_sha256):
        raise FirmwareError('A trusted worker manifest digest is required.')
    if type(runtime_seconds) is not int or not 10 <= runtime_seconds <= 3660:
        raise FirmwareError('Invalid firmware service runtime bound.')
    directory = RUNTIME_ROOT + '/' + job_id
    unit = 'legiongo2-firmware-' + job_id + '.service'
    argv = ('/usr/bin/systemd-run', '--quiet', '--collect', '--service-type=exec',
        '--unit=' + unit, '--property=KillMode=control-group', '--property=Restart=no',
        '--property=RuntimeMaxSec=' + str(runtime_seconds), '--property=TimeoutStopSec=5',
        '--property=UMask=0077', '--property=StandardInput=null',
        '--property=StandardOutput=null', '--property=StandardError=journal',
        '--property=WorkingDirectory=' + directory, '--property=NoNewPrivileges=yes',
        '--property=UnsetEnvironment=LD_LIBRARY_PATH LD_LIBRARY_PATH_ORIG LD_PRELOAD LD_AUDIT PYTHONPATH PYTHONHOME DBUS_SYSTEM_BUS_ADDRESS DBUS_SESSION_BUS_ADDRESS',
        *((('--property=TimeoutStopSec=60', '--property=ExecStopPost=/usr/bin/python3 -I -B '+directory+'/bootstrap.py '+directory+' '+manifest_sha256+' --cleanup') if cleanup else ())),
        '/usr/bin/python3', '-I', '-B', '-c', BOOTSTRAP, directory, manifest_sha256)
    return ServicePlan(unit, directory, argv)


def service_runtime(policy):
    if not isinstance(policy, TransferPolicy):
        raise FirmwareError('A reviewed worker timeout policy is required.')
    # Acquisition plus overall transfer, authentication/launch and cleanup margin.
    # This cap is not a device timeout. Oversized policies fail closed.
    result = math.ceil(policy.verification_timeout + policy.transaction_timeout + 30)
    if not 10 <= result <= 3660:
        raise FirmwareError('The firmware policy exceeds the bounded service lifetime.')
    return result


def _environment():
    env = system_env()
    for name in tuple(env):
        if name.startswith('PYTHON') or name.startswith('SYSTEMD_') or name in (
                'DBUS_SYSTEM_BUS_ADDRESS','DBUS_SESSION_BUS_ADDRESS'):
            del env[name]
    env['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin'
    return env


def launch_service(job, policy, *, components=1):
    """Submit the verified isolated worker once. Never equate submit to flash.

    A timeout/failure retains the stage and launch marker: it may have reached
    systemd. No automatic retry, stop, kill, cleanup or restart is provided.
    Decky and Desktop share the backend that submits this worker.
    """
    if os.name != 'posix' or os.geteuid() != 0:
        raise FirmwareError('Firmware services require the Linux system owner.')
    if not isinstance(job, StagedJob): raise FirmwareError('Invalid staged firmware job.')
    if type(components) is not int or not 1 <= components <= 3: raise FirmwareError('Invalid component count.')
    manifest = verify_stage(job)
    plan = service_plan(job.job_id, service_runtime(policy)*components, job.manifest_sha256,
        cleanup=manifest['schema']==2)
    directory = _private_directory(job.directory, 0)
    try:
        # EXCL is a durable one-shot claim even if the caller dies around submit.
        fd = os.open('launch.claim', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=directory)
        try:
            data = (plan.unit + '\n' + job.manifest_sha256 + '\n').encode('ascii')
            if os.write(fd, data) != len(data): raise OSError('short claim')
            os.fsync(fd)
        finally: os.close(fd)
        os.fsync(directory)
    except OSError as exc:
        raise FirmwareError('The firmware service launch was already claimed or could not be recorded.') from exc
    finally: os.close(directory)
    try:
        result = subprocess.run(plan.argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=_environment(), timeout=5, check=False)
        submitted = True if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired) as exc:
        event('firmware','service_submission_failed',level='warning',error=exc,worker_id=job.job_id,stage='submission')
        submitted = None
    return {'unit': plan.unit, 'submitted': submitted, 'flash_available': False,
        'stage_retained': True, 'error': '' if submitted else 'The service submission could not be confirmed. No retry was started.'}


def service_state(job_id):
    """Bounded display-only observation; absence is not proof of no prior writes."""
    _job(job_id)
    if os.name != 'posix': raise FirmwareError('Firmware service observation requires Linux.')
    unit = 'legiongo2-firmware-' + job_id + '.service'
    args = ('/usr/bin/systemctl', 'show', '--no-pager',
        '--property=Id,LoadState,ActiveState,SubState,InvocationID,ControlGroup,Result', unit)
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=_environment(), timeout=3, check=False)
        if result.returncode or len(result.stdout) > 4096: raise ValueError('query')
        values = {}
        for line in result.stdout.decode('ascii').splitlines():
            key, value = line.split('=', 1)
            if key in values: raise ValueError('duplicate')
            values[key] = value
        fields = {'Id','LoadState','ActiveState','SubState','InvocationID','ControlGroup','Result'}
        if set(values) != fields or values['Id'] != unit: raise ValueError('identity')
        invocation = values['InvocationID']
        if invocation and not re.fullmatch(r'[0-9a-f]{32}', invocation): raise ValueError('invocation')
        group = values['ControlGroup']
        if group and group != '/system.slice/' + unit: raise ValueError('cgroup')
        if values['ActiveState'] not in ('active','activating','deactivating','inactive','failed'):
            raise ValueError('state')
        if values['LoadState'] not in ('loaded','not-found','error','masked'):
            raise ValueError('load')
        if values['ActiveState'] in ('active','activating','deactivating') and (
                values['LoadState'] != 'loaded' or not invocation or not group):
            raise ValueError('active identity')
        return {'unit': unit, 'state': values['ActiveState'], 'invocation_id': invocation,
            'substate': values['SubState'], 'result': values['Result'], 'stage_retained': True}
    except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError, AttributeError) as exc:
        event('firmware','service_observation_failed',level='warning',error=exc,worker_id=job_id,repeat=60,stage='service_observation')
        return {'unit': unit, 'state': 'unknown', 'stage_retained': True}

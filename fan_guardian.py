# SPDX-License-Identifier: BSD-3-Clause
"""Detached EC owner and independent recovery process. No Decky/UI/network calls.

The plugin must renew its lease. The backup separately watches the EC owner's
progress, sensors and suspend clock; it can reap its stalled sibling with pidfd
before release. Both recovery paths require a functioning kernel and EC bus.
"""
import json
import math
import os
import pathlib
import select
import shutil
import signal
import socket
import stat
import time
import tempfile
from diagnostics import configure_worker, event as diagnostic_event
from fan_ec import EC, Ports, platform_check
from fan_sleep import SleepSafety
from fan_policy import CurveControl, LEASE_S, SAMPLE_S, HARD_MAX_C, MAX_RPM, encode_target, validate_curve, stop_timed_out

RUNTIME = pathlib.Path('/run/legiongo2companion-fan')


def atomic(path, value):
    fd, tmp = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def record_fault(runtime, session, reason, stage, state=None, **details):
    """One bounded, private diagnostic; never prevent hardware recovery.

    Healthy telemetry and later clean shutdowns do not erase this record. The
    session correlates a transport failure with its independent recovery.
    """
    if not hasattr(os, 'geteuid') or not isinstance(session, str) or not session.startswith('session-') or not reason:
        return
    try:
        runtime = pathlib.Path(runtime)
        info = runtime.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            return
        previous = {}
        path = runtime/'last-fault.json'
        if path.exists():
            if path.is_symlink() or path.stat().st_size > 8192:
                return
            try:
                previous = json.loads(path.read_text())
            except (ValueError, TypeError):
                pass
        first = not isinstance(previous, dict) or previous.get('session') != session
        if first:
            previous = dict(session=session, reason=str(reason)[:512], stage=str(stage)[:64],
                            monotonic=time.monotonic(), pid=os.getpid())
        if first and isinstance(details.get('sensor_sample'), dict):
            snapshot = clean_sensor_sample(details['sensor_sample'])
            previous['sensor_sample'] = snapshot
            # Existing log/export allowlists accept these fixed numeric fields
            # as a bounded summary; never log raw sysfs input or recovery samples.
            summary = ' '.join(k+'='+format(v, '.6g') for k, v in snapshot.items()
                               if type(v) in (int, float))
            diagnostic_event('fan', 'fault_sample', level='error', stage=stage,
                             code=snapshot.get('code', 'unknown'), worker_id=session,
                             reason=summary)
        sample = state or {}
        previous.update(recovery=dict(active=sample.get('active'), automatic=sample.get('automatic'),
                                      override=sample.get('override'), automatic_reads=sample.get('automatic_reads')))
        for key, value in details.items():
            if key in ('transport_error', 'exit_code', 'heartbeat_age'):
                previous[key] = value[:512] if isinstance(value, str) else value
        atomic(path, previous)
    except (OSError, ValueError, TypeError):
        pass  # Diagnostics, including a full disk, cannot delay release.


def clean_sensor_sample(sample):
    result = {}
    for key in ('cpu_temperature', 'gpu_temperature', 'ec_temperature', 'difference_c',
                'sensor_started_at', 'sensor_finished_at', 'ec_started_at',
                'ec_finished_at', 'fault_at', 'acquisition_ms'):
        value = sample.get(key)
        if type(value) in (int, float) and math.isfinite(value):
            result[key] = value
    if sample.get('sensor_name') in ('k10temp', 'amdgpu'):
        result['sensor_name'] = sample['sensor_name']
    code = sample.get('code', 'unknown')
    result['code'] = code if code in ('sensor_io', 'sensor_value_format', 'sensor_range',
        'sensor_acquisition_timeout', 'ec_io', 'ec_temperature_range') else 'other_fault'
    return result


def fault_sample(sensors, ec_sample):
    sample = dict(ec_sample)
    sample.update(getattr(sensors, 'last_sample', {}) if sensors is not None else {})
    values = [sample[k] for k in ('cpu_temperature', 'gpu_temperature') if k in sample]
    if values and 'ec_temperature' in sample:
        sample['difference_c'] = max(values)-sample['ec_temperature']
    sample['fault_at'] = time.monotonic()
    return sample


def read_ec_status(ec, sample):
    sample.clear()
    sample.update(ec_started_at=time.monotonic(), code='ec_io')
    try:
        result = ec.status()
        sample.update(ec_temperature=result['ec_temperature'])
        sample.pop('code')
        return result
    finally:
        sample['ec_finished_at'] = time.monotonic()


def check_ec_temperature(sample):
    # C307 has no proven identity equivalent to Tctl or GPU edge. A valid
    # independent channel may lag or differ; equality is not a freshness test.
    if not 10 <= sample['ec_temperature'] <= 105:
        sample['code'] = 'ec_temperature_range'
        raise RuntimeError('Invalid EC temperature reading.')


def secure_root():
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    info = RUNTIME.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError('Unsafe fan runtime directory.')
    fd = os.open(RUNTIME/'owner.lock', os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
        os.close(fd)
        raise RuntimeError('Unsafe fan ownership file.')
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise RuntimeError('Another Fan Control instance still owns EC recovery.')
    return fd


class Sensors:
    def __init__(self):
        self.fds = {}
        try:
            for p in pathlib.Path('/sys/class/hwmon').glob('hwmon*'):
                name = (p/'name').read_text().strip()
                if name in ('k10temp', 'amdgpu'):
                    if name in self.fds:
                        raise RuntimeError('Ambiguous APU temperature sensors.')
                    self.fds[name] = os.open(p/'temp1_input', os.O_RDONLY | os.O_CLOEXEC)
            if set(self.fds) != {'k10temp', 'amdgpu'}:
                raise RuntimeError('Both independent APU sensors are required.')
        except BaseException:
            self.close()
            raise

    def read(self):
        values = {}
        start = time.monotonic()
        self.last_sample = dict(sensor_started_at=start)
        try:
            for name, fd in self.fds.items():
                self.last_sample.update(sensor_name=name, code='sensor_io')
                os.lseek(fd, 0, os.SEEK_SET)
                raw = os.read(fd, 64)
                self.last_sample['code'] = 'sensor_value_format'
                t = int(raw)/1000
                self.last_sample['cpu_temperature' if name == 'k10temp' else 'gpu_temperature'] = t
                self.last_sample['code'] = 'sensor_range'
                if not 10 <= t <= 105:
                    raise RuntimeError('Invalid APU temperature reading.')
                values[name] = t
            self.last_sample.pop('code')
            self.last_sample.pop('sensor_name')
        finally:
            finished = time.monotonic()
            self.last_sample.update(sensor_finished_at=finished, acquisition_ms=(finished-start)*1000)
        if finished-start > SAMPLE_S:
            self.last_sample['code'] = 'sensor_acquisition_timeout'
            raise RuntimeError('Stale/slow APU temperature acquisition.')
        return values

    def close(self):
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()


def boot_gap(previous):
    # CLOCK_BOOTTIME includes suspend, CLOCK_MONOTONIC excludes it.
    current = time.clock_gettime(time.CLOCK_BOOTTIME)-time.monotonic()
    if previous is not None and abs(current-previous) > 0.75:
        raise RuntimeError('Suspend/resume detected; re-enable Fan Control after resume.')
    return current


def check_owners(exclude=()):
    modules = {line.split()[0] for line in pathlib.Path('/proc/modules').read_text().splitlines() if line.split()}
    if modules.intersection(('legion_laptop', 'gpd_fan')):
        raise RuntimeError('Conflicting fan driver is loaded.')
    for line in pathlib.Path('/proc/ioports').read_text().splitlines():
        prefix, _, name = line.strip().partition(':')
        # PCI bus aperture is a parent allocation, not a port-consuming driver.
        if name.strip().startswith('PCI Bus '):
            continue
        try:
            lo, hi = (int(x, 16) for x in prefix.strip().split('-'))
        except ValueError:
            continue
        if lo <= 0x4F and hi >= 0x4E:
            raise RuntimeError('EC host ports have another kernel owner.')
    started = time.monotonic()
    for p in pathlib.Path('/proc').glob('[0-9]*'):
        if int(p.name) in (os.getpid(), *exclude):
            continue
        try:
            for fd in (p/'fd').iterdir():
                try:
                    if os.readlink(fd) == '/dev/port':
                        raise RuntimeError('Another userspace process has raw EC host port access.')
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
        if time.monotonic()-started > 0.5:
            raise RuntimeError('Raw-port ownership scan exceeded its time budget.')


def independent_hard_max(ec, root, temperature):
    """Backup can enforce 100°C without a live RPC or worker scheduling.

    The same EC lock covers target ownership metadata and both writers. A
    marker lets the worker adopt a verified backup write before comparing its
    old target. An unknown override still faults rather than being overwritten.
    """
    if temperature < HARD_MAX_C or not (root/'armed').exists():
        return
    with ec.transaction():
        if (root/'stop').exists() or not (root/'armed').exists():
            return
        owned = json.loads((root/'owned-target.json').read_text())['rpm']
        if ec.word(0xC6C8) != encode_target(owned):
            raise RuntimeError('Fan ownership changed before independent hard maximum.')
        (root/'hard-max').touch(mode=0o600, exist_ok=True)
        ec.set_target(MAX_RPM, lambda: (root/'stop').exists())
        atomic(root/'owned-target.json', dict(rpm=MAX_RPM))


def adopt_hard_max(ec, root, state, control, now):
    if not (root/'hard-max').exists():
        return
    owned = json.loads((root/'owned-target.json').read_text())['rpm']
    if owned != MAX_RPM or ec.word(0xC6C8) != MAX_RPM:
        raise RuntimeError('Independent hard maximum readback changed.')
    state.update(target=MAX_RPM, target_changed_at=now)
    control.force_max(now)
    (root/'hard-max').unlink()


def stop_release(ec, root, reason):
    """Stop marker and zero are committed under the same lock as every target."""
    errors = []
    for _ in range(8):
        try:
            with ec.transaction():
                if not (root/'reason.json').exists():
                    atomic(root/'reason.json', dict(reason=reason))
                (root/'stop').touch(mode=0o600, exist_ok=True)
                if (root/'armed').exists():
                    ec.release()
                elif ec.word(0xC6C8):
                    raise RuntimeError('Foreign fan override; ownership was never armed.')
                result = ec.status()
            # Zero selects the native firmware branch on this exact EC image.
            # Require fresh observations as well; native effective RPM may
            # legitimately equal the preceding override and is not rejected.
            result = confirm_automatic(ec)
            first_reason = json.loads((root/'reason.json').read_text())['reason']
            atomic(RUNTIME/'last.json', dict(active=False, automatic=result['override'] == 0,
                                            reason=first_reason, state=result, session=root.name, monotonic=time.monotonic()))
            return result
        except Exception as exc:
            errors.append(str(exc)); time.sleep(0.1)
    raise RuntimeError('EC recovery is pending: ' + '; '.join(errors))


def confirm_automatic(ec):
    for _ in range(3):
        time.sleep(0.1)
        with ec.transaction():
            result = ec.status()
            if result['override'] != 0:
                raise RuntimeError('Fan override reappeared after withdrawal.')
    return dict(result, automatic_reads=3)


def backup(root, baseline, parent, ready_fd, inherited_port, ipc, owner_fd):
    os.setsid()
    ipc.close(); inherited_port.close()
    port, sensors, pidfd, sleep = None, None, None, None
    recovered = False
    reason = 'EC owner exited'
    stage = 'backup_startup'
    fault = False
    try:
        port = Ports(); ec = EC(port); ec.baseline = baseline
        sensors = Sensors()
        pidfd = os.pidfd_open(parent)
        sleep = SleepSafety()
        sensors.read()
        with ec.transaction():
            if ec.status()['override'] != 0:
                raise RuntimeError('Backup readiness requires firmware automatic.')
        os.write(ready_fd, b'1'); os.close(ready_fd); ready_fd = -1
        clock = boot_gap(None)
        next_temperature = next_owners = 0
        while True:
            now = time.monotonic()
            stage = 'backup_sleep'
            clock = boot_gap(clock)
            sleep.check()
            pulse = json.loads((root/'pulse.json').read_text())
            if now >= next_owners:
                stage = 'backup_ownership'
                check_owners((parent,))
                next_owners = now+1
            if now >= next_temperature:
                stage = 'backup_sensor'
                temperature = max(sensors.read().values())
                independent_hard_max(ec, root, temperature)
                next_temperature = now+0.5
            if (root/'stop').exists():
                reason = 'Control withdrawn'; break
            if now-pulse['monotonic'] >= LEASE_S:
                reason = 'EC owner progress lease expired'; stage = 'backup_progress'; fault = True; break
            if select.select([pidfd], [], [], 0.1)[0]:
                reason = 'EC owner died'; stage = 'backup_owner_exit'; fault = True; break
    except Exception as exc:
        reason = str(exc); fault = True
    finally:
        if fault:
            record_fault(RUNTIME, root.name, reason, stage, sensor_sample=fault_sample(sensors, {}))
        if ready_fd >= 0:
            os.close(ready_fd)
        if port is not None:
            # Stop own EC worker BEFORE attempting recovery of a transaction it
            # might still hold. pidfd identifies this exact process, not a PID reuse.
            if pidfd is not None and not (root/'stop').exists():
                try:
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                    select.select([pidfd], [], [], 0.5)
                except ProcessLookupError:
                    pass
            try:
                try:
                    restored = stop_release(ec, root, reason)
                except Exception:
                    if pidfd is not None:
                        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                        select.select([pidfd], [], [], 0.5)
                    restored = stop_release(ec, root, reason)
                recovered = True
                if fault:
                    record_fault(RUNTIME, root.name, reason, stage, dict(restored, active=False, automatic=True))
            except Exception as exc:
                atomic(RUNTIME/'last.json', dict(active=False, automatic=False,
                                                reason=str(exc), monotonic=time.monotonic()))
            port.close()
        if fault or not recovered:
            diagnostic_event('fan', 'worker_recovery', level='warning' if recovered else 'error',
                             stage=stage, source='backup', restored=recovered, worker_id=root.name)
        if sensors is not None:
            sensors.close()
        if sleep is not None:
            sleep.close()  # Delay inhibitor outlives the final automatic readback.
        if pidfd is not None:
            if recovered and select.select([pidfd], [], [], 0)[0]:
                shutil.rmtree(root, ignore_errors=True)
            os.close(pidfd)
        os.close(owner_fd)
        os._exit(0)


def serve(ipc):
    platform_check()
    owner = secure_root()
    root = pathlib.Path(tempfile.mkdtemp(prefix='session-', dir=RUNTIME))
    port, sensors, child, sleep = None, None, None, None
    state = dict(active=False, automatic=False, error='', target=0, writes=0, samples=0, stop_verified=False, session=root.name)
    reason = 'Controller disconnected'
    stage = 'startup'
    ec_sample = {}
    try:
        check_owners()
        port = Ports(); ec = EC(port)
        sensors = Sensors(); temperatures = sensors.read()
        with ec.transaction():
            initial = read_ec_status(ec, ec_sample)
            if initial['override'] != 0:
                raise RuntimeError('Another controller has an EC fan override; custom control remains off.')
            check_ec_temperature(ec_sample)
        atomic(root/'pulse.json', dict(monotonic=time.monotonic()))
        rfd, wfd = os.pipe()
        worker_pid = os.getpid()
        child = os.fork()
        if child == 0:
            os.close(rfd)
            sensors.close()
            backup(root, ec.baseline, worker_pid, wfd, port, ipc, owner)
        os.close(wfd)
        try:
            sleep = SleepSafety()  # Construct after fork: no inherited bus reuse.
            if not select.select([rfd], [], [], 1)[0] or os.read(rfd, 1) != b'1':
                raise RuntimeError('Independent backup watchdog did not become ready.')
        finally:
            os.close(rfd)
        ipc.setblocking(False)
        state.update(initial, automatic=True, guardian_pid=os.getpid(), backup_pid=child)
        ipc.send(json.dumps(dict(kind='ready', **state)).encode())
        diagnostic_event('fan', 'worker_ready', source='owner', worker_id=root.name)
        last_lease = time.monotonic(); next_sample = next_ec = last_lease
        clock = boot_gap(None); curve = None; control = None; revision = -1
        last_report, last_file, last_owner_scan = 0, 0, time.monotonic()
        while True:
            now = time.monotonic()
            stage = 'sleep'
            clock = boot_gap(clock)
            sleep.check()
            if now-last_owner_scan >= 1:
                stage = 'ownership'
                check_owners((child,))
                last_owner_scan = time.monotonic()
            if now-last_lease >= LEASE_S:
                stage = 'plugin_heartbeat'
                raise RuntimeError('Plugin heartbeat expired; firmware automatic restored.')
            if (root/'stop').exists():
                raise RuntimeError('Independent watchdog withdrew the lease.')
            if os.waitpid(child, os.WNOHANG)[0]:
                child = None
                raise RuntimeError('Independent backup watchdog died.')
            if select.select([ipc], [], [], min(0.1, max(0, next_sample-now)))[0]:
                stage = 'ipc'
                raw = ipc.recv(16384)
                if not raw:
                    break
                command = json.loads(raw)
                if command.get('op') == 'heartbeat':
                    last_lease = time.monotonic()
                elif command.get('op') == 'configure':
                    stage = 'configuration'
                    seq = command.get('revision')
                    if type(seq) is not int or seq <= revision or type(command.get('enabled')) is not bool:
                        raise RuntimeError('Stale/invalid fan configuration.')
                    new_curve = validate_curve(command['curve'])
                    if command['enabled']:
                        # Backup readiness, fresh sensors and target readback precede active status.
                        temperatures = sensors.read()
                        if control is None:
                            control = CurveControl(time.monotonic(), max(temperatures.values()), new_curve)
                        elif new_curve != curve:
                            control.retarget(max(temperatures.values()), time.monotonic())
                        with ec.transaction():
                            adopt_hard_max(ec, root, state, control, time.monotonic())
                            if max(temperatures.values()) >= HARD_MAX_C:
                                control.step(new_curve, max(temperatures.values()), time.monotonic())
                            expected = encode_target(state['target']) if state['active'] else 0
                            if ec.word(0xC6C8) != expected:
                                raise RuntimeError('Fan ownership changed before activation.')
                            (root/'armed').touch(mode=0o600, exist_ok=True)
                            if ec.word(0xC6C8) != encode_target(control.target):
                                ec.set_target(control.target, lambda: (root/'stop').exists() or time.monotonic()-last_lease >= LEASE_S)
                                state['writes'] += 1
                            activated = read_ec_status(ec, ec_sample)
                            atomic(root/'owned-target.json', dict(rpm=control.target))
                        curve = new_curve
                        state.update(activated, active=True, automatic=False, target=control.target,
                                     target_changed_at=time.monotonic(), temperatures=temperatures, sampled_at=time.monotonic(),
                                     stop_verified=control.target == 0 and activated['rpm'] == 0 and activated['effective'] == 0)
                    else:
                        with ec.transaction():
                            ec.release()
                            (root/'armed').unlink(missing_ok=True)
                            (root/'hard-max').unlink(missing_ok=True)
                        state.update(confirm_automatic(ec), active=False, automatic=True, target=0, stop_verified=False)
                        atomic(RUNTIME/'last.json', dict(active=False, automatic=True, reason='Custom disabled',
                                                        state=state, session=root.name, monotonic=time.monotonic()))
                        curve = control = None
                    revision = seq
                    state.update(revision=revision)
                    next_sample = next_ec = time.monotonic()
                    ipc.send(json.dumps(dict(kind='configured', **state)).encode())
                else:
                    raise RuntimeError('Unknown fan command.')
            now = time.monotonic()
            if now >= next_sample:
                stage = 'sensor'
                # Safety continues off-screen, independent of plugin RPC/React.
                temperatures = sensors.read()
                target = control.step(curve, max(temperatures.values()), now) if state['active'] else 0
                if now >= next_ec or target != state['target']:
                    stage = 'ec_readback'
                    with ec.transaction():
                        if state['active'] and (root/'hard-max').exists():
                            adopt_hard_max(ec, root, state, control, now)
                            target = MAX_RPM
                        current = read_ec_status(ec, ec_sample)
                        check_ec_temperature(ec_sample)
                        expected = encode_target(state['target']) if state['active'] else 0
                        if current['override'] != expected:
                            raise RuntimeError('Fan override changed outside its owner.')
                        if target != state['target']:
                            ec.set_target(target, lambda: (root/'stop').exists() or time.monotonic()-last_lease >= LEASE_S)
                            state['target'] = target; state['writes'] += 1; state['target_changed_at'] = now
                            current = read_ec_status(ec, ec_sample)
                        if state['active'] and control.is_stalled(current['rpm'], now):
                            stage = 'tach'
                            raise RuntimeError('Fan did not reach a stable running speed.')
                        if state['active'] and target == 0 and stop_timed_out(current['rpm'], current['effective'], now-state['target_changed_at']):
                            raise RuntimeError('Fan stop did not reach zero; firmware automatic restored.')
                        state['stop_verified'] = state['active'] and target == 0 and current['rpm'] == 0 and current['effective'] == 0
                        state.update(current, sampled_at=now)
                        if state['active']:
                            atomic(root/'owned-target.json', dict(rpm=state['target']))
                    next_ec = now+0.5
                state.update(temperatures=temperatures)
                state['samples'] += 1
                next_sample = now + (SAMPLE_S if state['active'] else 1.0)
                atomic(root/'pulse.json', dict(monotonic=time.monotonic(), active=state['active'], target=state['target']))
                if now-last_file >= 2:
                    atomic(RUNTIME/'last.json', dict(active=state['active'], automatic=state['automatic'],
                                                    reason='', state=state, session=root.name, monotonic=now))
                    last_file = now
                if now-last_report >= 0.5:
                    try:
                        ipc.send(json.dumps(dict(kind='telemetry', **state)).encode())
                        last_report = now
                    except BlockingIOError:
                        pass  # A backed-up frontend never blocks EC recovery.
    except Exception as exc:
        reason = str(exc); state['error'] = reason
        record_fault(RUNTIME, root.name, reason, stage, state,
                     sensor_sample=fault_sample(sensors, ec_sample), heartbeat_age=None if 'last_lease' not in locals() else max(0, time.monotonic()-last_lease))
    finally:
        if port is not None:
            try:
                state.update(stop_release(ec, root, reason), active=False, automatic=True, target=0, stop_verified=False)
            except Exception as exc:
                state.update(error=str(exc), active=False, automatic=False)
        if state['error']:
            record_fault(RUNTIME, root.name, reason, stage, state)
        if child is not None:
            # Backup observes stop within 100ms; bounded wait retains recovery ownership.
            deadline = time.monotonic()+2
            while time.monotonic() < deadline:
                if os.waitpid(child, os.WNOHANG)[0]:
                    child = None; break
                time.sleep(0.05)
        try:
            ipc.send(json.dumps(dict(kind='closed', **state)).encode())
        except (OSError, BlockingIOError):
            pass
        if sensors is not None:
            sensors.close()
        if port is not None:
            port.close()
        diagnostic_event('fan', 'worker_closed', level='warning' if state['error'] else 'info',
                         source='owner', stage=stage, worker_id=root.name,
                         restored=state.get('automatic') is True, pending=state.get('automatic') is not True)
        if sleep is not None:
            sleep.close()
        if child is None and state['automatic']:
            shutil.rmtree(root)
        os.close(owner)


if __name__ == '__main__':
    import sys
    configure_worker('fan')
    sock = socket.socket(fileno=int(sys.argv[1]))
    try:
        serve(sock)
    except Exception as exc:
        diagnostic_event('fan', 'worker_start_failed', level='error', error=exc, stage='bootstrap')
        sock.send(json.dumps(dict(kind='closed', active=False, automatic=False, error=str(exc))).encode())
    finally:
        sock.close()

# SPDX-License-Identifier: BSD-3-Clause
"""Detached EC owner and independent recovery process. No Decky/UI/network calls.

The owner runs the curve from its own sensors; plugin EOF releases. The backup
separately watches the owner's progress and temperature; it can reap its stalled
sibling with pidfd before release. Both recovery paths require a functioning
kernel and EC bus.
"""
import errno
import json
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
from fan_sleep import Pause, Retry, SleepSafety
from fan_policy import (CurveControl, LEASE_S, SAMPLE_S, HARD_MAX_C, MAX_RPM, SELF_TEST_S, SELF_TEST_RPM,
                        encode_target, validate_curve, stop_timed_out)

RUNTIME = pathlib.Path('/run/legiongo2companion-fan')
# Self-tested (BIOS, EC) pairs. Root-owned and outside Decky's settings tree, which Decky re-chowns to deck on every load.
FIRMWARE_FILE = pathlib.Path('/var/lib/legiongo2companion-fan/fan_firmware.json')
VALIDATED_FIRMWARE = frozenset({('RRCN16WW', '1.16')})  # Hardware-validated before the self-test existed.
CHANGED = 'This BIOS/EC version changed how the fan is controlled; Fan Control stays on automatic.'


def atomic(path, value):
    """Runtime state in /run (tmpfs), written every sample: no fsync on the EC control path."""
    fd, tmp = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def outcome_of(exc):
    """pause: power/process lifecycle; retry: transient software or timing; fault: anything else."""
    if isinstance(exc, (Pause, ConnectionError)):
        return 'pause'  # A dead or unloading plugin breaks the socket.
    return 'retry' if isinstance(exc, (Retry, TimeoutError)) else 'fault'


def record_fault(session, reason, stage, outcome, sensors=None, ec_sample=None):
    """First failure of a session in the private last-fault.json; never delays release."""
    try:
        path = RUNTIME/'last-fault.json'
        if (path.is_file() and not path.is_symlink() and path.stat().st_size < 8192
                and json.loads(path.read_text()).get('session') == session):
            return
        sample = dict(ec_sample or {}, **getattr(sensors, 'last_sample', {}))
        atomic(path, dict(session=session, reason=str(reason)[:512], stage=stage, outcome=outcome,
                          monotonic=time.monotonic(),
                          sample={k: v for k, v in sample.items() if type(v) in (int, float, str)}))
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # Diagnostics, including a full disk, cannot delay release.


def read_ec_status(ec, sample):
    sample.clear(); sample.update(ec.status())
    return dict(sample)


def confirm_override_change(ec, sample, expected):
    """Re-read a changed override once, writing nothing: one disturbed port read is not an owner change.

    Zero twice: the firmware took the fan back on its own (automatic is safe), so a fresh
    session may re-arm within the retry budget. Any other confirmed value is a foreign writer.
    """
    first = sample['override']
    time.sleep(0.05)
    current = read_ec_status(ec, sample)
    # In the reason, so session_ended, control_fault and last-fault.json carry it past a reboot.
    detail = f'(read {first} then {current["override"]}, expected {expected}, power mode {current["power_mode"]})'
    if first == current['override'] == 0:
        raise Retry(f'Firmware took the fan back {detail}.')
    if current['override'] != expected:
        raise RuntimeError(f'Fan override changed outside its owner {detail}.')
    diagnostic_event('fan', 'override_misread', level='warning', reason=detail)
    check_ec_temperature(sample)
    return current


def check_ec_temperature(sample):
    # C307 has no proven identity equivalent to Tctl or GPU edge. A valid
    # independent channel may lag or differ; equality is not a freshness test.
    if not 10 <= sample['ec_temperature'] <= 105:
        raise RuntimeError('Invalid EC temperature reading.')


def firmware_dir(create=False):
    """The remembered-pairs directory, only if it is ours: a directory, our uid, no group/other access."""
    directory = pathlib.Path(FIRMWARE_FILE).parent
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise OSError('Unsafe fan firmware directory.')
    return directory


def validated_firmware():
    pairs = set(VALIDATED_FIRMWARE)
    try:
        firmware_dir()
        fd = os.open(FIRMWARE_FILE, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            # Only our own (root) file counts: a forged entry would skip the active check.
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_size <= 8192:
                pairs.update(tuple(pair) for pair in json.loads(os.read(fd, 8192))['validated']
                             if isinstance(pair, list) and len(pair) == 2 and all(type(x) is str for x in pair))
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError, KeyError):
        pass  # Missing or damaged: the self-test runs again.
    return pairs


def remember_firmware(pair):
    from safe_settings import UnsafeSettingsPath, _atomic_write_bytes  # Only on a new BIOS/EC pair.
    try:
        firmware_dir(create=True)
        # Durable (fsynced) like settings: a power cut must not leave an empty file that repeats the self-test.
        _atomic_write_bytes(str(FIRMWARE_FILE), json.dumps(
            dict(validated=sorted(map(list, validated_firmware() - VALIDATED_FIRMWARE | {pair})))).encode())
    except (OSError, UnsafeSettingsPath) as exc:
        diagnostic_event('fan', 'firmware_record_failed', level='warning', error=exc)


def self_test(ec, root, ipc, sleep):
    """First session on a new (BIOS, EC) pair, with the backup and inhibitor live.

    A raised target must read back exactly and the tach must follow it; release
    must then read firmware automatic. It never lowers the firmware's speed.
    """
    stopped = lambda: (root/'stop').exists()
    try:
        with ec.transaction():
            target = SELF_TEST_RPM if ec.status()['rpm'] < SELF_TEST_RPM-800 else MAX_RPM
            (root/'armed').touch(mode=0o600, exist_ok=True)
            ec.set_target(target, stopped)
            atomic(root/'owned-target.json', dict(rpm=target))
        near = lambda value: abs(value-target) <= .15*target
        deadline, followed = time.monotonic()+SELF_TEST_S, 0
        while followed < 2:
            if time.monotonic() > deadline:
                raise RuntimeError('Fan tach did not follow the self-test target.')
            if select.select([ipc], [], [], 0.25)[0]:  # Nothing is sent before ready: this is EOF.
                raise Pause('Fan check cancelled; firmware automatic restored.')
            check_terminated(); sleep.check()
            atomic(root/'pulse.json', dict(monotonic=time.monotonic()))
            with ec.transaction():
                sample = ec.status()
            if sample['override'] != target:
                raise RuntimeError('Fan override changed during the self-test.')
            followed = followed+1 if near(sample['rpm']) and near(sample['effective']) else 0
        with ec.transaction():
            ec.release()
            (root/'armed').unlink()
        return confirm_automatic(ec)
    except (Pause, Retry, TimeoutError):
        raise
    except RuntimeError as exc:
        if stopped() or (root/'hard-max').exists():
            raise  # The backup acted; its own reason is the cause.
        diagnostic_event('fan', 'self_test_failed', level='warning', error=exc, worker_id=root.name)
        raise RuntimeError(CHANGED) from exc


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
        raise Retry('Another Fan Control instance still owns EC recovery.')
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
                raise Retry('Both independent APU sensors are required.')
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
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    raw = os.read(fd, 64)
                except OSError as exc:
                    raise Retry('APU temperature read failed.') from exc
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
            raise Retry('Stale/slow APU temperature acquisition.')
        return values

    def close(self):
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()


def boot_gap(previous):
    # CLOCK_BOOTTIME includes suspend, CLOCK_MONOTONIC excludes it.
    current = time.clock_gettime(time.CLOCK_BOOTTIME)-time.monotonic()
    if previous is not None and abs(current-previous) > 0.75:
        raise Pause('Suspend/resume detected; firmware automatic restored.')
    return current


CONFLICTING_DRIVERS = ('legion_laptop', 'gpd_fan')


def ec_port_claims(ioports):
    """Names of the /proc/ioports entries that claim the EC index ports 0x4E-0x4F."""
    claims = []
    for line in ioports.splitlines():
        prefix, _, name = line.strip().partition(':')
        # PCI bus aperture is a parent allocation, not a port-consuming driver.
        if name.strip().startswith('PCI Bus '):
            continue
        try:
            lo, hi = (int(x, 16) for x in prefix.strip().split('-'))
        except ValueError:
            continue
        if lo <= 0x4F and hi >= 0x4E:
            claims.append(name.strip())
    return claims


def check_owners():
    """Session start and every passive read; afterwards the override readback catches a foreign writer."""
    modules = {line.split()[0] for line in pathlib.Path('/proc/modules').read_text().splitlines() if line.split()}
    if modules.intersection(CONFLICTING_DRIVERS):
        raise RuntimeError('Conflicting fan driver is loaded.')
    if ec_port_claims(pathlib.Path('/proc/ioports').read_text()):
        raise RuntimeError('Another driver is using the fan controller.')
    started = time.monotonic()
    # Plain str paths: pathlib costs over twice as much per fd and this runs on every passive read.
    for pid in os.listdir('/proc'):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        fds = f'/proc/{pid}/fd/'
        try:
            for fd in os.listdir(fds):
                try:
                    if os.readlink(fds+fd) == '/dev/port':
                        raise RuntimeError('Another app is using the fan controller.')
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
        if time.monotonic()-started > 0.5:
            raise Retry('Raw-port ownership scan exceeded its time budget.')


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


def stop_release(ec, root, reason, outcome, sole_writer=False):
    """Stop marker and zero are committed under the same lock as every target.

    The first reason and outcome of a session win; both processes may release.
    Only the stop marker must be written first: it keeps a stale owner from
    re-arming. The reason and last.json records never delay or block the release.
    sole_writer: the caller is the owner itself, or the owner is proven dead,
    so nobody is left to re-arm and an unwritable marker cannot block the release.
    """
    errors = []
    for _ in range(8):
        try:
            with ec.transaction():
                try:
                    (root/'stop').touch(mode=0o600, exist_ok=True)
                except OSError:
                    if not sole_writer:
                        raise
                if (root/'armed').exists():
                    ec.release()
                elif ec.word(0xC6C8):
                    raise RuntimeError('Foreign fan override; ownership was never armed.')
                result = ec.status()
                try:
                    if not (root/'reason.json').exists():
                        atomic(root/'reason.json', dict(reason=reason, outcome=outcome))
                except OSError:
                    pass
            # Zero selects the native firmware branch on this exact EC image.
            # Require fresh observations as well; native effective RPM may
            # legitimately equal the preceding override and is not rejected.
            result = confirm_automatic(ec)
            try:
                first = json.loads((root/'reason.json').read_text())
            except (OSError, ValueError):
                first = dict(reason=reason, outcome=outcome)
            try:
                atomic(RUNTIME/'last.json', dict(active=False, automatic=result['override'] == 0, reason=first['reason'],
                                                outcome=first['outcome'], state=result, session=root.name, monotonic=time.monotonic()))
            except OSError:
                pass
            return dict(result, outcome=first['outcome'])
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


TERMINATED = False


def _terminate(*_):
    global TERMINATED
    TERMINATED = True  # Polled at loop safe points: an EC transaction is never interrupted.


def check_terminated():
    if TERMINATED:
        raise Pause('Stopped by SIGTERM; firmware automatic restored.')


def backup(root, baseline, parent, ready_fd, inherited_port, ipc, owner_fd):
    os.setsid()
    ipc.close(); inherited_port.close()
    port, sensors, pidfd, sleep = None, None, None, None
    recovered = False
    reason, outcome = 'EC owner exited', 'pause'
    stage = 'backup_startup'
    try:
        port = Ports(); ec = EC(port); ec.baseline = baseline
        sensors = Sensors()
        pidfd = os.pidfd_open(parent)
        try:
            sleep = SleepSafety()  # Its own bus and delay inhibitor: the owner's dies with the owner.
        except OSError as exc:
            if exc.errno == errno.EINPROGRESS:
                raise Pause('Sleep is preparing; fan control remains off.') from exc
            raise
        sensors.read()
        with ec.transaction():
            if ec.status()['override'] != 0:
                raise RuntimeError('Backup readiness requires firmware automatic.')
        os.write(ready_fd, b'1'); os.close(ready_fd); ready_fd = -1
        next_temperature = 0
        while True:
            now = time.monotonic()
            if TERMINATED:
                reason = 'Stopped by SIGTERM; firmware automatic restored.'; break
            sleep.check()
            pulse = json.loads((root/'pulse.json').read_text())
            if now >= next_temperature:
                stage = 'backup_sensor'
                temperature = max(sensors.read().values())
                independent_hard_max(ec, root, temperature)
                next_temperature = now+0.5
            if (root/'stop').exists():
                reason = 'Control withdrawn'; break
            if now-pulse['monotonic'] >= LEASE_S:
                reason, outcome, stage = 'EC owner progress lease expired', 'retry', 'backup_progress'; break
            # pidfd wakes at once on owner death; otherwise wake for the 0.5 s temperature check or lease expiry.
            if select.select([pidfd], [], [], min(0.5, max(0, pulse['monotonic']+LEASE_S-time.monotonic())))[0]:
                reason, outcome, stage = 'EC owner died', 'retry', 'backup_owner_exit'; break
    except Exception as exc:
        reason, outcome = str(exc), outcome_of(exc)
    finally:
        # After the owner's own release, the owner already recorded the first cause.
        failed = outcome != 'pause' and not (root/'stop').exists()
        if failed:
            record_fault(root.name, reason, stage, outcome, sensors)
        if ready_fd >= 0:
            os.close(ready_fd)
        if port is not None:
            # Stop own EC worker BEFORE attempting recovery of a transaction it
            # might still hold. pidfd identifies this exact process, not a PID reuse.
            if pidfd is not None and failed:
                try:
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                    select.select([pidfd], [], [], 0.5)
                except ProcessLookupError:
                    pass
            def owner_dead():
                return pidfd is not None and bool(select.select([pidfd], [], [], 0)[0])
            try:
                try:
                    stop_release(ec, root, reason, outcome, sole_writer=owner_dead())
                except Exception:
                    if pidfd is not None:
                        try:
                            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                            select.select([pidfd], [], [], 0.5)
                        except ProcessLookupError:
                            pass  # Already gone: exactly what the kill was for.
                    stop_release(ec, root, reason, outcome, sole_writer=owner_dead())
                recovered = True
            except Exception as exc:
                try:
                    atomic(RUNTIME/'last.json', dict(active=False, automatic=False, reason=str(exc),
                                                    outcome='fault', monotonic=time.monotonic()))
                except OSError:
                    pass  # os._exit below must still run: this is a forked child.
            port.close()
        if failed or not recovered:
            diagnostic_event('fan', 'worker_recovery', level='warning' if recovered else 'error',
                             stage=stage, source='backup', restored=recovered, worker_id=root.name,
                             outcome=outcome, reason=reason)
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
    signal.signal(signal.SIGTERM, _terminate)  # Inherited by the backup fork.
    identity = platform_check()
    owner = secure_root()
    try:
        pathlib.Path('/proc/self/oom_score_adj').write_text('-900')  # Also inherited by the backup.
    except OSError as exc:
        diagnostic_event('fan', 'oom_protection_failed', level='warning', error=exc)
    for stale in RUNTIME.glob('session-*'):  # Owner lock held: left by SIGKILLed sessions, no live user.
        shutil.rmtree(stale, ignore_errors=True)
    root = pathlib.Path(tempfile.mkdtemp(prefix='session-', dir=RUNTIME))
    port, sensors, child, sleep = None, None, None, None
    state = dict(active=False, automatic=False, error='', target=0, writes=0, samples=0, stop_verified=False, session=root.name)
    reason, outcome = 'Controller disconnected', 'pause'
    stage = 'startup'
    ec_sample = {}
    try:
        check_owners()
        port = Ports(); ec = EC(port)
        sensors = Sensors(); temperatures = sensors.read()
        known = validated_firmware()
        with ec.transaction():
            initial = ec.plausible(); ec_sample.update(initial)  # Read-only, before any write.
        pair = (identity['bios_version'], initial['ec_version'])
        if pair not in known:
            # An unproven interface gets no write, not even a release, until it looks sane.
            try:
                check_ec_temperature(ec_sample)
                if initial['override'] not in (0, SELF_TEST_RPM, MAX_RPM):  # Only a killed self-test's own target.
                    raise RuntimeError('Unknown fan override on this BIOS/EC version; Fan Control was not started.')
            except RuntimeError:
                port.close(); port = None  # Nothing was written, so there is no release to attempt.
                raise
        if initial['override'] != 0:
            with ec.transaction():
                ec.release()
        if initial['override'] != 0:
            # Owner lock and ownership scan found no live writer: an orphan from a
            # killed session or tool. Release it instead of blocking until power-off.
            diagnostic_event('fan', 'stale_override_released', level='warning', worker_id=root.name, count=initial['override'])
            initial = confirm_automatic(ec); ec_sample.update(initial)
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
            try:
                sleep = SleepSafety()  # Construct after fork: no inherited bus reuse.
            except OSError as exc:
                if exc.errno == errno.EINPROGRESS:  # logind refuses inhibitors while a sleep is already under way.
                    raise Pause('Sleep is preparing; fan control remains off.') from exc
                raise Retry(str(exc)) from exc
            if not select.select([rfd], [], [], 1)[0] or os.read(rfd, 1) != b'1':
                raise Retry('Independent backup watchdog did not become ready.')
        finally:
            os.close(rfd)
        if pair not in known:
            stage = 'self_test'
            initial = self_test(ec, root, ipc, sleep); ec_sample.update(initial)
            remember_firmware(pair)
            diagnostic_event('fan', 'firmware_validated', worker_id=root.name, version='/'.join(pair))
        ipc.setblocking(False)
        state.update(initial, automatic=True, guardian_pid=os.getpid(), backup_pid=child)
        ipc.send(json.dumps(dict(kind='ready', **state)).encode())
        diagnostic_event('fan', 'worker_ready', source='owner', worker_id=root.name)
        next_sample = next_ec = time.monotonic()
        clock = boot_gap(None); curve = None; control = None; revision = -1
        last_report, last_file, last_pulse = 0, 0, 0
        stopped = lambda: (root/'stop').exists()
        while True:
            now = time.monotonic()
            stage = 'sleep'
            check_terminated()
            clock = boot_gap(clock)
            sleep.check()
            if stopped():
                raise RuntimeError('Independent watchdog withdrew the lease.')
            if os.waitpid(child, os.WNOHANG)[0]:
                child = None
                raise Retry('Independent backup watchdog died.')
            if select.select([ipc], [], [], min(SAMPLE_S, max(0, next_sample-now)))[0]:
                stage = 'ipc'
                raw = ipc.recv(16384)
                if not raw:
                    break
                command = json.loads(raw)
                if command.get('op') == 'configure':
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
                                ec.set_target(control.target, stopped)
                                state['writes'] += 1
                            activated = read_ec_status(ec, ec_sample)
                            atomic(root/'owned-target.json', dict(rpm=control.target))
                        curve = new_curve
                        # Only a new target restarts the stop check; repeated saves must not postpone it.
                        changed_at = state['target_changed_at'] if state['active'] and control.target == state['target'] else time.monotonic()
                        state.update(activated, active=True, automatic=False, target=control.target,
                                     target_changed_at=changed_at, temperatures=temperatures, sampled_at=time.monotonic(),
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
                            current = confirm_override_change(ec, ec_sample, expected)
                        if target != state['target']:
                            ec.set_target(target, stopped)
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
                if now-last_pulse >= 1:  # About 3 progress proofs per backup lease (LEASE_S).
                    atomic(root/'pulse.json', dict(monotonic=time.monotonic(), active=state['active'], target=state['target']))
                    last_pulse = now
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
        reason, outcome = str(exc), outcome_of(exc)
        state['error'] = reason
    finally:
        state['outcome'] = outcome
        if port is not None:
            try:
                state.update(stop_release(ec, root, reason, outcome, sole_writer=True), active=False, automatic=True, target=0, stop_verified=False)
            except Exception as exc:
                state.update(error=str(exc), active=False, automatic=False, outcome='fault')
        if state['outcome'] != 'pause':
            record_fault(root.name, state['error'] or reason, stage, state['outcome'], sensors, ec_sample)
        if child is not None:
            # Backup observes stop within 0.5 s; bounded wait retains recovery ownership.
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
        diagnostic_event('fan', 'worker_closed', level='info' if state['outcome'] == 'pause' else 'warning',
                         source='owner', stage=stage, worker_id=root.name, outcome=state['outcome'], reason=reason,
                         restored=state.get('automatic') is True, pending=state.get('automatic') is not True)
        if sleep is not None:
            sleep.close()
        if child is None and (state['automatic'] or port is None):  # port None: start failed before the EC opened.
            shutil.rmtree(root)
        os.close(owner)


if __name__ == '__main__':
    import sys
    configure_worker('fan')
    sock = socket.socket(fileno=int(sys.argv[1]))
    try:
        serve(sock)
    except Exception as exc:
        diagnostic_event('fan', 'worker_start_failed', level='error', error=exc, stage='bootstrap', outcome=outcome_of(exc))
        sock.send(json.dumps(dict(kind='closed', active=False, automatic=False, error=str(exc), outcome=outcome_of(exc))).encode())
    finally:
        sock.close()

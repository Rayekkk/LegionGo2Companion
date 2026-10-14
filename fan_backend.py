# SPDX-License-Identifier: BSD-3-Clause
"""Decky fan RPCs and durable intent. Hardware safety lives in detached processes."""
import asyncio
import copy
import errno
import json
import os
import pathlib
import select
import socket
import subprocess
import threading
import time

import decky
from diagnostics import event as diagnostic_event, transition as diagnostic_transition
import module_runtime
from system_process import system_env
from safe_settings import SettingsManager
from fan_ec import platform_check
from fan_guardian import secure_root, check_owners, Sensors, record_fault
from fan_ec import Ports, EC
from fan_policy import defaults, validate_settings, migrate_settings, validate_curve, PRESETS, TEMPLATES, MIN_RPM, MAX_RPM, RPM_STEP, HARD_MAX_C

settings = SettingsManager('fan_settings', decky.DECKY_PLUGIN_SETTINGS_DIR)
READING_MAX_AGE_S = 2.5


def measured_readings(sample, source):
    """Only actual tach/sensors are display readings; closed targets are not."""
    try:
        import math
        age = time.monotonic()-sample['sampled_at']
        temperatures = sample['temperatures']
        rpm = sample['rpm']
        if (not 0 <= age <= READING_MAX_AGE_S or type(rpm) is not int or not 0 <= rpm <= 10000
                or set(temperatures) != {'k10temp', 'amdgpu'}
                or any(type(t) not in (int, float) or not math.isfinite(t) or not 10 <= t <= 105
                       for t in temperatures.values())):
            raise ValueError('Fan reading is stale or invalid.')
        return dict(valid=True, rpm=rpm, apu_temperature=max(temperatures.values()), temperatures=temperatures, age_seconds=age,
                    source=source, automatic=sample.get('override') == 0, error='')
    except (KeyError, TypeError, ValueError) as exc:
        return dict(valid=False, error=str(exc) or 'Fresh fan reading unavailable.')


def read_automatic():
    """One bounded passive read; no worker, inhibitor, lease or override write.

    Hold the guardian's ownership lock until the raw-port observer has closed.
    This also excludes another instance's worker/backup, including recovery.
    """
    owner, sensors, port = None, None, None
    started = time.monotonic()
    try:
        platform_check()
        owner = secure_root()
        check_owners()
        sensors = Sensors()
        temperatures = sensors.read()
        port = Ports()
        ec = EC(port)
        with ec.transaction():
            sample = ec.status()
        if time.monotonic()-started > 0.75:
            raise RuntimeError('Passive fan reading exceeded its time budget.')
        sample.update(temperatures=temperatures, sampled_at=started)
        result = measured_readings(sample, 'passive')
        result['ownership'] = dict(blocked=sample['override'] != 0,
                                   state='external_override' if sample['override'] else 'firmware_auto',
                                   override=sample['override'], checked_at=time.monotonic(),
                                   reason='An unowned EC override is active.' if sample['override'] else '')
        return result
    except Exception as exc:
        message = str(exc) or 'Fresh fan reading unavailable.'
        return dict(valid=False, error=message, ownership=dict(blocked=True, state='unavailable',
                     reason=message, checked_at=time.monotonic()))
    finally:
        if port is not None:
            port.close()
        if sensors is not None:
            sensors.close()
        if owner is not None:
            os.close(owner)


def companion_preset():
    """Observe Companion intent, including the active game/AC profile. No WMI writes."""
    import tdp_backend as tdp
    state = tdp._load_settings()
    if not state.get('enabled', True):
        return None
    app = tdp._current_game_id
    profile = tdp._load_profiles().get(app) if app else None
    if profile:
        key = 'ac_preset' if profile.get('ac_separate') and tdp._get_ac_online() else 'preset'
        return profile.get(key) if profile.get(key) in PRESETS else None
    return state.get('active_preset') if state.get('active_preset') in PRESETS else None


class Client:
    HEARTBEAT_S = 0.5
    LOST = 'Fan watchdog connection lost; recovery is being checked.'
    DISCONNECTED = frozenset((errno.ECONNRESET, errno.EPIPE, errno.ENOTCONN))

    def __init__(self, runtime=None):
        self.lock = threading.RLock()
        self.state_lock = threading.Lock()
        self.socket = None
        self.process = None
        self.state = dict(active=False, automatic=False, error='')
        self.sequence = 0
        self.last_intent_at = 0
        self.runtime = pathlib.Path(runtime) if runtime is not None else pathlib.Path('/run/legiongo2companion-fan')
        self._stop = threading.Event()
        self._heartbeat = None
        self._failure = ''
        self._transport_error = ''
        self._heartbeat_at = None

    def start(self):
        with self.lock:
            if self.process is not None or self._stop.is_set():
                raise RuntimeError('A fresh fan session is required.')
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            script = pathlib.Path(__file__).with_name('fan_guardian.py')
            env = system_env()
            for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONINSPECT'):
                env.pop(name, None)
            try:
                self.process = subprocess.Popen(['/usr/bin/python3', '-E', '-s', '-u', str(script), str(child.fileno())],
                    pass_fds=(child.fileno(),), start_new_session=True, close_fds=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
                self.socket = parent
                parent.setblocking(False)
                deadline = time.monotonic()+3
                while time.monotonic() < deadline:
                    self._receive(0.1)
                    if self.state.get('kind') == 'ready':
                        self._start_heartbeat()
                        return
                    if self.state.get('kind') == 'closed' or self.process.poll() is not None:
                        raise RuntimeError(self.state.get('error') or 'Fan watchdog did not start.')
                raise TimeoutError('Fan watchdog readiness timed out.')
            except BaseException:
                self._stop.set()
                parent.close(); self.socket = None
                if self.process is not None:
                    try:
                        self.process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        pass  # Independent backup retains ownership until release.
                raise
            finally:
                child.close()

    def _start_heartbeat(self):
        # One sender per Client/session, independent of asyncio's mutation lock,
        # slow persistence and the shared to_thread pool. It never restarts a peer.
        if self._heartbeat is not None or self._stop.is_set():
            raise RuntimeError('Fan heartbeat lifecycle is already closed.')
        self._heartbeat = threading.Thread(target=self._keep_alive, name='CompanionFanHeartbeat', daemon=True)
        self._heartbeat.start()

    def _keep_alive(self):
        while not self._stop.is_set():
            try:
                with self.lock:
                    if self._stop.is_set():
                        return
                    self.pump()
            except Exception as exc:
                if self._stop.is_set():
                    return  # An explicit close owns cleanup, never rearm it.
                with self.state_lock:
                    self._failure = self.state.get('error') or str(exc)
                try:
                    self.close()
                except Exception as recovery:
                    with self.state_lock:
                        primary = self.state.get('error') or self._failure
                        self.state['error'] = primary if str(recovery) == primary else primary+' '+str(recovery)
                with self.state_lock:
                    self._failure = self.state.get('error') or self._failure
                    self.state.update(active=False, error=self._failure)
                self._record_fault('heartbeat')
                return
            self._stop.wait(self.HEARTBEAT_S)

    def _record_fault(self, stage):
        state = self.snapshot()
        record_fault(self.runtime, state.get('session'), state.get('error') or self._failure,
                     stage, state, transport_error=self._transport_error,
                     exit_code=self.process.poll() if self.process is not None else None,
                     heartbeat_age=None if self._heartbeat_at is None else max(0, time.monotonic()-self._heartbeat_at))
        diagnostic_event('fan', 'watchdog_fault', level='error', stage=stage,
                         restored=state.get('automatic') is True,
                         returncode=self.process.poll() if self.process is not None else None)

    def _disconnect(self):
        if self.socket is not None:
            self.socket.close(); self.socket = None
        with self.state_lock:
            # Only a terminal packet or a correlated recovery receipt can prove
            # automatic. Earlier ready/telemetry cannot prove a later recovery.
            if self.state.get('kind') != 'closed':
                self.state.update(kind='disconnected', active=False, automatic=False,
                                  error=self.state.get('error') or self.LOST)

    def _accept(self, value):
        if (not isinstance(value, dict) or value.get('kind') not in ('ready', 'configured', 'telemetry', 'closed')
                or type(value.get('active')) is not bool or type(value.get('automatic')) is not bool
                or not isinstance(value.get('error'), str)):
            raise RuntimeError('Invalid fan watchdog status; recovery required.')
        session = value.get('session')
        expected = self.state.get('session')
        if expected and session != expected:
            raise RuntimeError('Fan watchdog session changed; recovery required.')
        if not expected and (not isinstance(session, str) or not session.startswith('session-')):
            # Startup rejection can precede the creation of a secure session.
            if value['kind'] != 'closed' or value['active'] or value['automatic'] or not value['error']:
                raise RuntimeError('Missing fan watchdog session; recovery required.')
        if value['kind'] == 'closed' and (value['active'] or value['automatic'] and
                (type(value.get('override')) is not int or value['override'] != 0
                 or type(value.get('automatic_reads')) is not int or value['automatic_reads'] != 3 or not session)):
            raise RuntimeError('Fan recovery readback was not confirmed.')
        with self.state_lock:
            self.state = value

    def _receive(self, wait=0):
        if self.socket is None:
            raise RuntimeError(self.state.get('error') or 'Fan watchdog is disconnected.')
        reset = False
        if select.select([self.socket], [], [], wait)[0]:
            for _ in range(16):
                try:
                    raw = self.socket.recv(16384)
                except ConnectionResetError as exc:
                    # Linux can surface its pending SO_ERROR before the queued
                    # terminal datagram. Bounded draining recovers that reason.
                    self._transport_error = str(exc); reset = True
                    continue
                except BlockingIOError:
                    break
                if not raw:
                    self._disconnect()
                    break
                self._accept(json.loads(raw))
                if self.state.get('kind') == 'closed':
                    return  # Do not obscure a terminal status with a later reset.
        if reset and self.state.get('kind') != 'closed':
            self._disconnect()

    def _send(self, value):
        if self.socket is None or self.state.get('kind') in ('closed', 'disconnected'):
            raise RuntimeError(self.state.get('error') or 'Fan watchdog stopped; firmware automatic recovery required.')
        data = json.dumps(value).encode()
        try:
            if self.socket.send(data) != len(data):
                raise RuntimeError('Incomplete fan watchdog command; recovery required.')
        except OSError as exc:
            if exc.errno not in self.DISCONNECTED:
                raise
            self._transport_error = str(exc)
            # A failed send may also hide the terminal status waiting for recv.
            self._receive()
            self._disconnect()
            raise RuntimeError(self.state.get('error') or 'Fan watchdog connection lost; recovery required.') from exc
        if value.get('op') == 'heartbeat':
            self._heartbeat_at = time.monotonic()

    def check(self):
        with self.state_lock:
            if self._failure or self.state.get('kind') in ('closed', 'disconnected'):
                raise RuntimeError(self.state.get('error') or self._failure or 'Fan watchdog stopped.')
            return copy.deepcopy(self.state)

    def pump(self):
        with self.lock:
            self.check()
            self._receive()
            self._send(dict(op='heartbeat'))
            return self.snapshot()

    def configure(self, enabled, curve):
        with self.lock:
            if self._stop.is_set():
                raise RuntimeError(self.state.get('error') or 'Fan watchdog is closing.')
            self.check()
            self._receive()
            self._send(dict(op='heartbeat'))
            self.sequence += 1
            self.last_intent_at = time.monotonic()
            self._send(dict(op='configure', enabled=enabled, curve=curve, revision=self.sequence))
            deadline = time.monotonic()+2
            while time.monotonic() < deadline:
                self._receive(0.1)
                self.check()
                if self.state.get('revision') == self.sequence:
                    if self.state.get('active') != enabled or (not enabled and not self.state.get('automatic')):
                        raise RuntimeError('Fan intent was not confirmed by EC readback.')
                    return self.snapshot()
            # Closing the channel requests independent release. The caller's
            # cleanup waits outside this I/O lock; no heartbeat join under it.
            self._disconnect()
            raise TimeoutError('Fan configuration readback timed out; control withdrawn.')

    def snapshot(self):
        with self.state_lock:
            return copy.deepcopy(self.state)

    def stop_heartbeat(self):
        # Nonblocking withdrawal before waiting for slow admitted settings work.
        self._stop.set()

    def _recover_receipt(self):
        try:
            outcome = json.loads((self.runtime/'last.json').read_text())
            recovered = outcome.get('state', {})
            now = time.monotonic()
            stamp = outcome.get('monotonic')
            if (self.state.get('session') and outcome.get('session') == self.state['session']
                    and type(stamp) in (int, float) and self.last_intent_at <= stamp <= now
                    and outcome.get('automatic') is True and outcome.get('active') is False
                    and type(recovered.get('override')) is int and recovered['override'] == 0
                    and type(recovered.get('automatic_reads')) is int and recovered['automatic_reads'] == 3):
                with self.state_lock:
                    reason = outcome.get('reason')
                    # The correlated guardian receipt outranks a generic lost
                    # channel error, never an unrelated session's diagnosis.
                    error = self.state.get('error', '')
                    if (not error or error == self.LOST) and isinstance(reason, str) and reason:
                        error = reason
                    self.state.update(recovered, kind='closed', active=False, automatic=True,
                                      target=0, stop_verified=False, error=error)
                return True
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return False

    def close(self):
        self.stop_heartbeat()  # A queued sender rechecks after acquiring lock.
        errors = []
        try:
            with self.lock:
                if self.socket is not None:
                    try:
                        try:
                            self.socket.shutdown(socket.SHUT_WR)
                        except OSError as exc:
                            if exc.errno not in self.DISCONNECTED:
                                errors.append(exc)
                            else:
                                self._transport_error = str(exc)
                        deadline = time.monotonic()+4
                        while self.socket is not None and time.monotonic() < deadline:
                            try:
                                self._receive(0.1)
                            except Exception as exc:
                                errors.append(exc)
                                break
                            if self.state.get('kind') == 'closed':
                                break
                    finally:
                        if self.socket is not None:
                            self.socket.close(); self.socket = None
                exited = True
                if self.process is not None:
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        exited = False
                if not self.state.get('automatic') or self.state.get('kind') != 'closed':
                    deadline = time.monotonic()+4
                    while time.monotonic() < deadline:
                        if self._recover_receipt():
                            break
                        time.sleep(0.05)
                if not exited:
                    raise RuntimeError('Independent EC recovery is still running.')
                if not self.state.get('automatic') or self.state.get('kind') != 'closed':
                    raise RuntimeError(self.state.get('error') or 'Firmware automatic readback is pending.')
                if errors:
                    raise RuntimeError('Fan communication cleanup failed: '+str(errors[0])) from errors[0]
        finally:
            thread = self._heartbeat
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1)
                if thread.is_alive():
                    raise RuntimeError('Fan heartbeat shutdown is pending.')

class Plugin:
    def __init__(self):
        self._client = None
        self._task = None
        self._mutation = asyncio.Lock()
        self._supported = False
        self._error = ''
        self._applied = None
        self._preset = None
        self._state = defaults()
        self._settings_valid = True
        self._loaded = False
        self._closing = False

    def _load(self):
        self._settings_valid = True
        try:
            if settings.recovery_error:
                raise ValueError(settings.recovery_error)
            raw = settings.getSetting('state', None)
            self._state = defaults() if raw is None else migrate_settings(raw)
            if raw is not None and raw.get('schema') in (1, 2, 3):
                self._save(self._state)
        except (ValueError, TypeError, OSError) as exc:
            self._state = defaults(); self._error = str(exc); self._settings_valid = False
            diagnostic_transition('fan', 'settings', 'unavailable', level='warning', error=exc,
                                  store='fan_settings', pending=True)
        self._loaded = True
        if self._settings_valid:
            diagnostic_transition('fan', 'settings', 'ready', store='fan_settings', pending=False)

    def _save(self, value):
        settings.setSetting('state', value); settings.commit()
        self._state = copy.deepcopy(value)
        self._settings_valid = True
        self._loaded = True
        diagnostic_transition('fan', 'settings', 'ready', store='fan_settings', pending=False)

    async def _persist(self, value):
        # Cancellation cannot release the settings lock while its write still
        # runs, or allow an old revision to overwrite a later disable/save.
        await module_runtime.complete(module_runtime.offload('fan', self._save, value))

    def _selected(self):
        # Without explicit assignments, neither TDP nor game/process probing is needed.
        observed = companion_preset() if self._state['assign_tdp_presets'] and self._state['presets'] else None
        preset = observed if observed in self._state['presets'] else None
        return self._state['presets'].get(preset, self._state['global_curve']), preset

    async def _apply(self):
        if self._closing:
            raise RuntimeError('Fan Control is shutting down.')
        if self._client is None:
            raise RuntimeError(self._error or 'Fan Control is unavailable.')
        curve, preset = await module_runtime.offload('fan', self._selected)
        intent = (self._state['enabled'], json.dumps(curve))
        if intent != self._applied:
            await module_runtime.offload('fan', self._client.configure, intent[0], curve)
            self._applied = intent
        self._preset = preset

    async def _monitor(self):
        while True:
            try:
                await asyncio.sleep(0.5)
                async with self._mutation:
                    self._client.check()
                    await self._apply()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error = str(exc)
                cleanup_errors = []
                async with self._mutation:
                    value = dict(self._state, enabled=False, revision=self._state['revision']+1)
                    try:
                        await self._persist(value)
                    except Exception as save_error:
                        self._state = value; cleanup_errors.append(str(save_error))
                    try:
                        await module_runtime.offload('fan', self._client.close)
                    except Exception as close_error:
                        cleanup_errors.append(str(close_error))
                    primary = self._client.snapshot().get('error') or str(exc)
                    self._error = ' '.join([primary, *cleanup_errors])
                    diagnostic_event('fan', 'control_fault', level='error', error=exc, stage='monitor',
                                     restored=self._client.snapshot().get('automatic') is True,
                                     count=len(cleanup_errors), enabled=False)
                return  # Faults are latched; explicit re-enable starts fresh watchdogs.

    async def _main(self):
        self._closing = False
        self._load()
        try:
            await module_runtime.offload('fan', platform_check)
            self._supported = True
            diagnostic_transition('fan', 'availability', 'supported', supported=True)
            if not self._settings_valid:
                return  # Preserve malformed/newer settings until an explicit user edit.
            if self._state['enabled']:
                # EOF cannot distinguish a clean unload from a killed Decky
                # controller. Durable intent is not a new acknowledgement.
                await self._persist(dict(self._state,
                    enabled=False, revision=self._state['revision']+1))
                self._error = 'Review and re-enable Fan Control after plugin startup.'
        except Exception as exc:
            self._error = str(exc)
            diagnostic_transition('fan', 'availability', 'unavailable', level='warning' if self._supported else 'info', error=exc,
                                  supported=self._supported, stage='startup')
            self._state['enabled'] = False
            if self._settings_valid:
                try:
                    await self._persist(dict(self._state, revision=self._state['revision']+1))
                except Exception as save_error:
                    self._error += ' ' + str(save_error)

    async def _start_client(self):
        if self._closing:
            raise RuntimeError('Fan Control is shutting down.')
        if self._client is not None:
            await module_runtime.offload('fan', self._client.close)
        if self._closing:
            raise RuntimeError('Fan Control is shutting down.')
        self._client = Client(); self._applied = None
        try:
            await module_runtime.offload('fan', self._client.start)
        except Exception as exc:
            diagnostic_event('fan', 'watchdog_start_failed', level='error', error=exc, stage='readiness',
                             returncode=getattr(getattr(self._client, 'process', None), 'returncode', None))
            raise
        diagnostic_event('fan', 'watchdog_ready', stage='readiness')
        # Never silently reactivate an interrupted session after a process fault.
        await self._apply()
        self._task = asyncio.create_task(self._monitor())

    def _withdraw_lease(self):
        # Whole-plugin shutdown can wait for unrelated work before async unload.
        self._closing = True
        if self._client is not None:
            self._client.stop_heartbeat()

    async def _unload(self):
        self._withdraw_lease()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await module_runtime.drain('fan')
        async with self._mutation:
            if self._client is not None:
                await module_runtime.offload('fan', self._client.close)

    async def get_state(self):
        if not self._loaded:
            async with self._mutation:
                if not self._loaded:
                    await module_runtime.offload('fan', self._load)
        telemetry = self._client.snapshot() if self._client else None
        if not self._supported:
            readings = dict(valid=False, error='Fan Control unavailable on this platform.')
        elif self._state['enabled']:
            readings = measured_readings(telemetry or {}, 'controller') if telemetry and telemetry.get('active') \
                else dict(valid=False, error='Waiting for fresh controller readings.')
        elif self._mutation.locked():
            # A disable/recovery can wait seconds. Keep the QAM read responsive.
            readings = dict(valid=False, error='Fan control is changing; waiting for fresh readings.')
        else:
            async with self._mutation:
                # complete retains the mutation lock through cancellation of an
                # RPC; enable must not race the underlying raw-port thread.
                readings = await module_runtime.complete(module_runtime.offload('fan', read_automatic))
                telemetry = self._client.snapshot() if self._client else None
        return dict(success=True, supported=self._supported, settings=copy.deepcopy(self._state), readings=readings,
                    ownership=(dict(blocked=False, state='companion', reason='') if readings.get('valid') and telemetry and telemetry.get('active')
                        else readings.get('ownership', dict(blocked=True, state='unavailable', reason=readings.get('error', 'Ownership has not been verified.')))),
                    needs_curve_review=not self._settings_valid,
                    error=self._error or (telemetry or {}).get('error', ''), active_curve=self._preset or 'global',
                    telemetry=telemetry,
                    limits=dict(min_rpm=MIN_RPM, max_rpm=MAX_RPM, step=RPM_STEP,
                                hard_max=HARD_MAX_C), presets=list(PRESETS), templates=copy.deepcopy(TEMPLATES))

    async def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Enabled must be a boolean.')
        async with self._mutation:
            if not self._loaded:
                await module_runtime.offload('fan', self._load)
            if enabled and self._closing:
                raise RuntimeError('Fan Control is shutting down.')
            if enabled and not self._supported:
                raise RuntimeError(self._error or 'Unsupported fan platform.')
            if enabled and not self._settings_valid:
                raise RuntimeError('Review and save a valid curve before enabling Fan Control.')
            if enabled and not (self._client and self._client.snapshot().get('active')):
                ownership = await module_runtime.complete(module_runtime.offload('fan', read_automatic))
                if ownership.get('ownership', {}).get('blocked', True):
                    raise RuntimeError(ownership.get('ownership', {}).get('reason') or 'Fresh exclusive fan ownership is required.')
            # Disable takes physical precedence even if saving settings fails.
            value = dict(self._state, enabled=enabled, revision=self._state['revision']+1)
            if not enabled:
                # Prevent the monitor from rearming old intent even if disk is full.
                self._state = copy.deepcopy(value); self._applied = None
            try:
                if not enabled and self._client is not None:
                    try:
                        if self._client.socket is not None:
                            await module_runtime.offload('fan', self._client.configure, False, value['global_curve'])
                        elif not self._client.snapshot().get('automatic'):
                            await module_runtime.offload('fan', self._client.close)
                    except Exception as communication:
                        # A disconnected peer must not abort explicit disable
                        # before independent release/readback and durable off.
                        await module_runtime.offload('fan', self._client.close)
                        self._error = self._client.snapshot().get('error') or str(communication)
                if self._settings_valid:
                    await self._persist(value)
                if enabled and (self._task is None or self._task.done()):
                    self._error = ''
                    await self._start_client()
                elif enabled and self._client is not None:
                    await self._apply()
                if enabled:
                    self._error = ''
                else:
                    # No background EC ownership, sensor polling or workers while off.
                    task, self._task = self._task, None
                    if task is not None:
                        task.cancel(); await asyncio.gather(task, return_exceptions=True)
                    if self._client is not None:
                        await module_runtime.offload('fan', self._client.close)
                    self._applied = None
            except Exception as exc:
                self._error = str(exc)
                if enabled:
                    self._state['enabled'] = False; self._applied = None
                    try:
                        await self._persist(dict(self._state, revision=self._state['revision']+1))
                    except Exception as save_error:
                        self._error += ' ' + str(save_error)
                    if self._client is not None:
                        await module_runtime.offload('fan', self._client.close)
                else:
                    # Disk failure cannot leave idle workers or a monitor that
                    # might keep retrying the saved pre-disable intent.
                    task, self._task = self._task, None
                    if task is not None:
                        task.cancel(); await asyncio.gather(task, return_exceptions=True)
                    if self._client is not None:
                        try:
                            await module_runtime.offload('fan', self._client.close)
                        except Exception as close_error:
                            self._error += ' ' + str(close_error)
                raise
        return await self.get_state()

    async def save_unit(self, unit, expected_revision):
        if unit not in ('rpm', 'percent'):
            raise ValueError('Unknown fan display unit.')
        async with self._mutation:
            if not self._loaded:
                await module_runtime.offload('fan', self._load)
            if not self._settings_valid or type(expected_revision) is not int or expected_revision != self._state['revision']:
                raise ValueError('Fan settings changed elsewhere. Reload before changing units.')
            value = dict(self._state, display_unit=unit, revision=self._state['revision']+1)
            await self._persist(value)
        return await self.get_state()

    async def save_curve(self, scope, curve, expected_revision):
        if scope != 'global' and scope not in PRESETS:
            raise ValueError('Unknown curve scope.')
        checked = None if curve is None and scope != 'global' else validate_curve(curve)
        async with self._mutation:
            if not self._loaded:
                await module_runtime.offload('fan', self._load)
            if type(expected_revision) is not int or expected_revision != self._state['revision']:
                raise ValueError('The curve changed elsewhere. Reload before saving.')
            value = copy.deepcopy(self._state)
            value['revision'] += 1
            if scope == 'global':
                value['global_curve'] = checked
            elif checked is None:
                value['presets'].pop(scope, None)
            else:
                value['presets'][scope] = checked
            reviewed = not self._settings_valid
            await self._persist(value)
            if reviewed and self._supported:
                self._error = ''
            if self._task is not None and not self._task.done():
                await self._apply()
        return await self.get_state()

    async def save_assignment_mode(self, enabled, expected_revision):
        if type(enabled) is not bool:
            raise ValueError('TDP curve assignments must be a boolean.')
        async with self._mutation:
            if not self._loaded:
                await module_runtime.offload('fan', self._load)
            if not self._settings_valid or type(expected_revision) is not int or expected_revision != self._state['revision']:
                raise ValueError('Fan settings changed elsewhere. Reload before changing assignments.')
            value = dict(self._state, assign_tdp_presets=enabled, revision=self._state['revision']+1)
            await self._persist(value)
            if self._task is not None and not self._task.done():
                try:
                    await self._apply()
                except Exception as exc:
                    # A failed curve switch must not leave the previous mode armed.
                    self._error = str(exc); self._applied = None
                    self._state = dict(self._state, enabled=False, revision=self._state['revision']+1)
                    try:
                        await self._persist(self._state)
                    finally:
                        task, self._task = self._task, None
                        task.cancel(); await asyncio.gather(task, return_exceptions=True)
                        await module_runtime.offload('fan', self._client.close)
                    raise
            else:
                self._preset = None
        return await self.get_state()

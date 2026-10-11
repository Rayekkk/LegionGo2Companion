# SPDX-License-Identifier: BSD-3-Clause
"""Decky fan RPCs and durable intent. Hardware safety lives in detached processes."""
import asyncio
import copy
import json
import os
import pathlib
import select
import socket
import subprocess
import threading
import time

import decky
import module_runtime
from safe_settings import SettingsManager
from fan_ec import platform_check
from fan_guardian import secure_root, check_owners, Sensors
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
    def __init__(self):
        self.lock = threading.RLock()
        self.state_lock = threading.Lock()
        self.socket = None
        self.process = None
        self.state = dict(active=False, automatic=False, error='')
        self.sequence = 0
        self.last_intent_at = 0

    def start(self):
        with self.lock:
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            script = pathlib.Path(__file__).with_name('fan_guardian.py')
            env = dict(os.environ)
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
                        return
                    if self.state.get('kind') == 'closed' or self.process.poll() is not None:
                        raise RuntimeError(self.state.get('error') or 'Fan watchdog did not start.')
                raise TimeoutError('Fan watchdog readiness timed out.')
            except BaseException:
                parent.close(); self.socket = None
                if self.process is not None:
                    try:
                        self.process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        pass  # Detached backup retains ownership until it releases EC.
                raise
            finally:
                child.close()

    def _receive(self, wait=0):
        if self.socket is None:
            raise RuntimeError('Fan watchdog is disconnected.')
        if select.select([self.socket], [], [], wait)[0]:
            for _ in range(16):
                try:
                    raw = self.socket.recv(16384)
                except BlockingIOError:
                    break
                if not raw:
                    self.socket.close(); self.socket = None
                    if not self.state.get('automatic'):
                        with self.state_lock:
                            self.state.update(active=False, error=self.state.get('error') or 'Fan watchdog connection lost; recovery pending.')
                    break
                value = json.loads(raw)
                with self.state_lock:
                    self.state = value

    def _send(self, value):
        if self.socket is None or self.state.get('kind') == 'closed':
            raise RuntimeError(self.state.get('error') or 'Fan watchdog is not healthy.')
        self.socket.send(json.dumps(value).encode())

    def pump(self):
        with self.lock:
            self._receive()
            self._send(dict(op='heartbeat'))
            return copy.deepcopy(self.state)

    def configure(self, enabled, curve):
        with self.lock:
            self._receive()
            self._send(dict(op='heartbeat'))
            self.sequence += 1
            self.last_intent_at = time.monotonic()
            self._send(dict(op='configure', enabled=enabled, curve=curve, revision=self.sequence))
            deadline = time.monotonic()+2
            while time.monotonic() < deadline:
                self._receive(0.1)
                if self.state.get('revision') == self.sequence:
                    if self.state.get('active') != enabled or (not enabled and not self.state.get('automatic')):
                        raise RuntimeError('Fan intent was not confirmed by EC readback.')
                    return copy.deepcopy(self.state)
                if self.socket is None or self.state.get('kind') == 'closed':
                    raise RuntimeError(self.state.get('error') or 'Fan watchdog withdrew control.')
            self.close()
            raise TimeoutError('Fan configuration readback timed out; control withdrawn.')

    def snapshot(self):
        # Recovery can hold the I/O lock for seconds. A QAM read must never
        # block Decky's event loop behind that wait or delay heartbeats.
        with self.state_lock:
            return copy.deepcopy(self.state)

    def close(self):
        with self.lock:
            if self.socket is not None:
                try:
                    # Shut down writes only, retaining final readback from the guardian.
                    self.socket.shutdown(socket.SHUT_WR)
                    deadline = time.monotonic()+4
                    while self.socket is not None and time.monotonic() < deadline:
                        self._receive(0.1)
                        if self.state.get('kind') == 'closed':
                            break
                finally:
                    if self.socket is not None:
                        self.socket.close(); self.socket = None
            if self.process is not None:
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    raise RuntimeError('Independent EC recovery is still running.')
            if not self.state.get('automatic'):
                # A SIGKILLed EC owner cannot acknowledge its backup's release
                # over IPC. Accept only that exact session's later triple readback.
                deadline = time.monotonic()+4
                while time.monotonic() < deadline:
                    try:
                        outcome = json.loads(pathlib.Path('/run/legiongo2companion-fan/last.json').read_text())
                        recovered = outcome.get('state', {})
                        if (self.state.get('session') and outcome.get('session') == self.state['session']
                                and outcome.get('monotonic', 0) >= self.last_intent_at
                                and outcome.get('automatic') is True and outcome.get('active') is False
                                and recovered.get('override') == 0 and recovered.get('automatic_reads') == 3):
                            with self.state_lock:
                                self.state.update(recovered, active=False, automatic=True, target=0, stop_verified=False)
                            break
                    except (OSError, ValueError, TypeError):
                        pass
                    time.sleep(0.05)
                if not self.state.get('automatic'):
                    raise RuntimeError(self.state.get('error') or 'Firmware automatic readback is pending.')


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
        self._loaded = True

    def _save(self, value):
        settings.setSetting('state', value); settings.commit()
        self._state = copy.deepcopy(value)
        self._settings_valid = True
        self._loaded = True

    def _selected(self):
        # Without explicit assignments, neither TDP nor game/process probing is needed.
        observed = companion_preset() if self._state['assign_tdp_presets'] and self._state['presets'] else None
        preset = observed if observed in self._state['presets'] else None
        return self._state['presets'].get(preset, self._state['global_curve']), preset

    async def _apply(self):
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
                    await module_runtime.offload('fan', self._client.pump)
                    await self._apply()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error = str(exc)
                async with self._mutation:
                    value = dict(self._state, enabled=False, revision=self._state['revision']+1)
                    try:
                        await module_runtime.offload('fan', self._save, value)
                    except Exception as save_error:
                        self._state = value; self._error += ' ' + str(save_error)
                    try:
                        await module_runtime.offload('fan', self._client.close)
                    except Exception as close_error:
                        self._error += ' ' + str(close_error)
                return  # Faults are latched; explicit re-enable starts fresh watchdogs.

    async def _main(self):
        self._load()
        try:
            await module_runtime.offload('fan', platform_check)
            self._supported = True
            if not self._settings_valid:
                return  # Preserve malformed/newer settings until an explicit user edit.
            if self._state['enabled']:
                # EOF cannot distinguish a clean unload from a killed Decky
                # controller. Durable intent is not a new acknowledgement.
                await module_runtime.offload('fan', self._save, dict(self._state,
                    enabled=False, revision=self._state['revision']+1))
                self._error = 'Review and re-enable Fan Control after plugin startup.'
        except Exception as exc:
            self._error = str(exc)
            self._state['enabled'] = False
            if self._settings_valid:
                try:
                    await module_runtime.offload('fan', self._save, dict(self._state, revision=self._state['revision']+1))
                except Exception as save_error:
                    self._error += ' ' + str(save_error)

    async def _start_client(self):
        if self._client is not None:
            await module_runtime.offload('fan', self._client.close)
        self._client = Client(); self._applied = None
        await module_runtime.offload('fan', self._client.start)
        # Never silently reactivate an interrupted session after a process fault.
        await self._apply()
        self._task = asyncio.create_task(self._monitor())

    async def _unload(self):
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
                    error=self._error, active_curve=self._preset or 'global',
                    telemetry=telemetry,
                    limits=dict(min_rpm=MIN_RPM, max_rpm=MAX_RPM, step=RPM_STEP,
                                hard_max=HARD_MAX_C), presets=list(PRESETS), templates=copy.deepcopy(TEMPLATES))

    async def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Enabled must be a boolean.')
        async with self._mutation:
            if not self._loaded:
                await module_runtime.offload('fan', self._load)
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
                if self._client is not None and self._client.socket is not None:
                    await module_runtime.offload('fan', self._client.configure, False, value['global_curve'])
                elif self._client is not None and not self._client.snapshot().get('automatic'):
                    await module_runtime.offload('fan', self._client.close)
            try:
                if self._settings_valid:
                    await module_runtime.offload('fan', self._save, value)
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
                        await module_runtime.offload('fan', self._save, dict(self._state, revision=self._state['revision']+1))
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
            await module_runtime.offload('fan', self._save, value)
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
            await module_runtime.offload('fan', self._save, value)
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
            await module_runtime.offload('fan', self._save, value)
            if self._task is not None and not self._task.done():
                try:
                    await self._apply()
                except Exception as exc:
                    # A failed curve switch must not leave the previous mode armed.
                    self._error = str(exc); self._applied = None
                    self._state = dict(self._state, enabled=False, revision=self._state['revision']+1)
                    try:
                        await module_runtime.offload('fan', self._save, self._state)
                    finally:
                        task, self._task = self._task, None
                        task.cancel(); await asyncio.gather(task, return_exceptions=True)
                        await module_runtime.offload('fan', self._client.close)
                    raise
            else:
                self._preset = None
        return await self.get_state()

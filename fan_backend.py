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
from system_process import PYTHON, system_env
from safe_settings import SettingsManager
from fan_ec import platform_check
from fan_guardian import secure_root, check_owners, Sensors, outcome_of
from fan_ec import Ports, EC
from fan_sleep import Pause, Retry
from fan_policy import defaults, validate_settings, validate_curve, PRESETS, TEMPLATES, MIN_RPM, LAST_MIN_RPM, MAX_RPM, RPM_STEP, HARD_MAX_C, SELF_TEST_S

settings = SettingsManager('fan_settings', decky.DECKY_PLUGIN_SETTINGS_DIR)
READING_MAX_AGE_S = 2.5
RUNTIME = pathlib.Path('/run/legiongo2companion-fan')


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
        # Owner lock and scan are clear, so a nonzero override is an orphan; session start releases it.
        result['ownership'] = dict(blocked=False,
                                   state='external_override' if sample['override'] else 'firmware_auto',
                                   override=sample['override'], checked_at=time.monotonic(), reason='')
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


def last_session():
    """Receipt of the latest guardian session in this boot; /run does not survive reboot."""
    try:
        value = json.loads((RUNTIME/'last.json').read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def companion_preset(cache=None):
    """Observe Companion intent, including the active game/AC profile. No WMI writes.

    With a caller-kept cache dict the TDP file is parsed again only after it is replaced.
    """
    import tdp_backend as tdp
    if not tdp.Plugin._ready:
        return None  # Starting, failed or withdrawn in Manage modules: firmware owns power.
    try:
        info = os.stat(tdp.settings.path)  # Before the read: a replace in between costs one extra read.
        key = (info.st_ino, info.st_mtime_ns, info.st_size)
    except OSError:
        key = None
    if cache is not None and key is not None and cache.get('key') == key:
        state, profiles = cache['value']
    else:
        state, profiles = tdp._load_settings(), tdp._load_profiles()
        if cache is not None:
            cache.update(key=key, value=(state, profiles))
    if not state.get('enabled', True):
        return None
    app = tdp._current_game_id
    profile = profiles.get(app) if app else None
    if profile:
        key = 'ac_preset' if profile.get('ac_separate') and tdp._get_ac_online() else 'preset'
        return profile.get(key) if profile.get(key) in PRESETS else None
    return state.get('active_preset') if state.get('active_preset') in PRESETS else None


class Client:
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
        self.timeout = None  # Our own readback timeout, which closed the channel.
        self.runtime = pathlib.Path(runtime) if runtime is not None else RUNTIME
        self._stop = threading.Event()

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
                self.process = subprocess.Popen([PYTHON, '-E', '-s', '-u', str(script), str(child.fileno())],
                    pass_fds=(child.fileno(),), start_new_session=True, close_fds=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
                self.socket = parent
                parent.setblocking(False)
                deadline = time.monotonic()+3+SELF_TEST_S  # A new BIOS/EC pair is self-tested before ready.
                while time.monotonic() < deadline:
                    self._receive(0.1)
                    # The guardian reports telemetry right after ready; one drain can
                    # deliver both, so telemetry of this session also means ready.
                    if self.state.get('kind') in ('ready', 'telemetry'):
                        return
                    if self.state.get('kind') == 'closed':
                        # The guardian classified its own startup failure.
                        failure = {'pause': Pause, 'retry': Retry}.get(self.state.get('outcome'), RuntimeError)
                        raise failure(self.state.get('error') or 'Fan watchdog did not start.')
                    if self.process.poll() is not None:
                        raise Retry('Fan watchdog exited before it was ready.')
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
                except ConnectionResetError:
                    # Linux can surface its pending SO_ERROR before the queued
                    # terminal datagram. Bounded draining recovers that reason.
                    reset = True
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
            # A failed send may also hide the terminal status waiting for recv.
            self._receive()
            self._disconnect()
            raise RuntimeError(self.state.get('error') or 'Fan watchdog connection lost; recovery required.') from exc

    def check(self):
        with self.state_lock:
            if self.state.get('kind') in ('closed', 'disconnected'):
                raise RuntimeError(self.state.get('error') or 'Fan watchdog stopped.')
            return copy.deepcopy(self.state)

    def pump(self):
        # Telemetry only: the guardian runs the curve without any plugin lease.
        with self.lock:
            self.check()
            self._receive()
            return self.check()

    def configure(self, enabled, curve):
        with self.lock:
            if self._stop.is_set():
                raise RuntimeError(self.state.get('error') or 'Fan watchdog is closing.')
            self.check()
            self._receive()
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
            # Closing the channel requests independent release.
            self.timeout = TimeoutError('Fan configuration readback timed out; control withdrawn.')
            self._disconnect()
            raise self.timeout

    def snapshot(self):
        with self.state_lock:
            return copy.deepcopy(self.state)

    def withdraw(self):
        """Release now, without the I/O lock: EOF makes the guardian restore firmware automatic."""
        self._stop.set()
        sock = self.socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass  # close() still reaps the peer and verifies the release.

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
                                      target=0, stop_verified=False, error=error, outcome=outcome.get('outcome'))
                return True
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return False

    def close(self):
        self._stop.set()
        errors = []
        with self.lock:
            drained = self.socket is not None
            if drained:
                try:
                    try:
                        self.socket.shutdown(socket.SHUT_WR)
                    except OSError as exc:
                        if exc.errno not in self.DISCONNECTED:
                            errors.append(exc)
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
                    # Our readback timeout closed the channel unpaced: allow the EOF release and backup wait (~3 s).
                    self.process.wait(timeout=1 if drained else 4)
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


def release_orphan():
    """Release-only guardian session: its start releases an orphaned override, its close proves automatic."""
    client = Client()
    client.start()
    client.close()


class Plugin:
    RETRY_LIMIT, RETRY_WINDOW_S = 3, 600
    BACKOFF_S, POLL_S = (1, 2, 5, 10, 30), 0.5

    def __init__(self):
        self._client = None
        self._task = None
        self._mutation = asyncio.Lock()
        self._supported = False
        self._error = ''
        self._applied = None
        self._preset = None
        self._tdp_seen = {}
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
            self._state = defaults() if raw is None else validate_settings(raw)
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

    def _revised(self, **changes):
        return dict(self._state, **changes, revision=self._state['revision'] + 1)

    async def _ensure_loaded(self):
        if not self._loaded:
            await module_runtime.offload('fan', self._load)

    async def _persist(self, value):
        # Cancellation cannot release the settings lock while its write still
        # runs, or allow an old revision to overwrite a later disable/save.
        await module_runtime.complete(module_runtime.offload('fan', self._save, value))

    def _selected(self):
        # Without explicit assignments, neither TDP nor game/process probing is needed.
        observed = companion_preset(self._tdp_seen) if self._state['assign_tdp_presets'] and self._state['presets'] else None
        preset = observed if observed in self._state['presets'] else None
        return self._state['presets'].get(preset, self._state['global_curve']), preset

    async def _apply(self):
        if self._closing:
            raise RuntimeError('Fan Control is shutting down.')
        if self._client is None:
            raise RuntimeError(self._error or 'Fan Control is unavailable.')
        # Only TDP assignments read files; the global curve is a dict lookup not worth a thread hop.
        assigned = self._state['assign_tdp_presets'] and self._state['presets']
        curve, preset = await module_runtime.offload('fan', self._selected) if assigned else self._selected()
        intent = (self._state['enabled'], json.dumps(curve))
        if intent != self._applied:
            await module_runtime.offload('fan', self._client.configure, intent[0], curve)
            self._applied = intent
        self._preset = preset

    async def _monitor(self, pending=False):
        """Keep the saved ON intent running: pauses re-arm, retries are bounded, faults latch OFF.

        Every (re)start is a fresh guardian with all of its start checks.
        """
        retries, attempt = [], 0
        while True:
            await asyncio.sleep(self.BACKOFF_S[min(attempt, len(self.BACKOFF_S)-1)] if pending else self.POLL_S)
            async with self._mutation:
                if self._closing or not self._state['enabled']:
                    return
                try:
                    if pending:
                        await self._start_client()
                        pending, attempt, self._error = False, 0, ''
                        diagnostic_event('fan', 'control_resumed', stage='rearm')
                    else:
                        await module_runtime.offload('fan', self._client.pump)
                        await self._apply()
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    outcome, reason = await self._end_session(exc)
                if self._closing:
                    return  # Unload never changes the saved intent.
                now = time.monotonic()
                if outcome == 'retry':
                    retries = [t for t in retries if now-t < self.RETRY_WINDOW_S] + [now]
                diagnostic_event('fan', 'session_ended', level='info' if outcome == 'pause' else 'warning',
                                 outcome=outcome, reason=reason, count=len(retries))
                if outcome == 'fault' or len(retries) > self.RETRY_LIMIT:
                    self._task = None
                    await self._save_off(reason if outcome == 'fault' else
                                         'Fan Control kept failing and was turned off: ' + reason)
                    return
                self._error = ('Fan Control paused; resuming automatically. ' if outcome == 'pause' else
                               'Fan controller restarted after an internal error. ') + reason
                if pending:
                    attempt += 1
                pending = True

    async def _close_client(self):
        """Close the session and prove firmware automatic.

        Without that proof (e.g. owner and backup both SIGKILLed or OOM-killed) the
        override may still be set, and the EC keeps it across warm reboots: one
        release-only session frees it. Returns a note naming the unconfirmed close
        after that release, else None. Caller holds _mutation.
        """
        try:
            await module_runtime.offload('fan', self._client.close)
            return None
        except Exception as exc:
            if self._closing:
                raise  # Unload stays fast; the next plugin start releases a leftover override.
            try:
                await module_runtime.offload('fan', release_orphan)
            except Exception as error:
                diagnostic_event('fan', 'orphan_release_failed', level='error', error=error, stage='session_end')
                raise RuntimeError(f'{exc} Leftover fan override release failed: {error}') from exc
            self._client = None  # Its override is released; nothing is left to close.
            return f'{exc} Firmware automatic restored by a release-only session.'

    async def _end_session(self, exc):
        """Prove firmware automatic, then classify how the session ended."""
        if self._client is None:
            return outcome_of(exc), str(exc)  # Failed start: the guardian never armed a target.
        state = self._client.snapshot()
        # The guardian sees our timeout's channel close as EOF (pause); it is a retry wherever it surfaced.
        exc = self._client.timeout or exc
        # A terminal packet or recovery receipt carries the guardian's class; local failures are ours.
        local = isinstance(exc, TimeoutError) or state.get('kind') not in ('closed', 'disconnected')
        reason = state.get('error') or str(exc)
        joined = lambda error: str(error) if str(error).startswith(reason) else f'{reason} {error}'
        try:
            unconfirmed = await self._close_client()
        except Exception as error:
            return 'fault', joined(error)
        if unconfirmed is not None:
            return 'fault', joined(unconfirmed)
        state = self._client.snapshot()
        if local:
            return outcome_of(exc), str(exc)
        return state.get('outcome') or 'fault', state.get('error') or reason

    async def _save_off(self, reason):
        # Firmware automatic is already proven; a disk failure still blocks any re-arm.
        value = self._revised(enabled=False)
        self._applied = None
        try:
            await self._persist(value)
        except Exception as save_error:
            self._state = value; reason = f'{reason} {save_error}'
        self._error = reason
        diagnostic_event('fan', 'control_fault', level='error', stage='monitor', enabled=False, reason=reason)

    async def _main(self):
        self._closing = False
        self._load()
        try:
            await module_runtime.offload('fan', platform_check)
            self._supported = True
            diagnostic_transition('fan', 'availability', 'supported', supported=True)
            # Malformed/newer settings stay OFF and untouched until an explicit user edit.
            if self._settings_valid and self._state['enabled']:
                previous = await module_runtime.offload('fan', last_session)
                if previous.get('outcome') == 'fault':
                    # Hardware evidence from this boot outranks the saved intent.
                    await self._save_off(previous.get('reason') or 'Fan Control stopped after a fault.')
                else:
                    self._error = 'Resuming Fan Control after startup.'
                    self._task = asyncio.create_task(self._monitor(pending=True))
                    return  # The resumed session start releases any leftover override.
            await self._release_leftover_override()
        except Exception as exc:
            self._error = str(exc)
            diagnostic_transition('fan', 'availability', 'unavailable', level='warning' if self._supported else 'info', error=exc,
                                  supported=self._supported, stage='startup')
            self._state['enabled'] = False
            if self._settings_valid:
                try:
                    await self._persist(self._revised())
                except Exception as save_error:
                    self._error += ' ' + str(save_error)

    async def _release_leftover_override(self):
        """Fan stays OFF: release once an override left by a killed session (EC keeps it across warm reboots)."""
        async with self._mutation:
            if self._closing or self._state['enabled']:
                return  # An explicit enable's own session start releases it.
            observed = await module_runtime.complete(module_runtime.offload('fan', read_automatic))
            # external_override = nonzero target with a free owner lock and no raw-port holder.
            if observed.get('ownership', {}).get('state') != 'external_override':
                return
            try:
                await module_runtime.offload('fan', release_orphan)
            except Exception as exc:
                self._error = f'Leftover fan override release failed: {exc}'
                diagnostic_event('fan', 'orphan_release_failed', level='error', error=exc, stage='startup')

    async def _start_client(self):
        """Fresh guardian with every start check; a failed start leaves no client behind."""
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
                             outcome=outcome_of(exc), returncode=getattr(getattr(self._client, 'process', None), 'returncode', None))
            self._client = None  # Start reaped its own peer; it never armed a target.
            raise
        diagnostic_event('fan', 'watchdog_ready', stage='readiness')
        await self._apply()

    def _live(self):
        return (self._task is not None and not self._task.done() and self._client is not None
                and self._client.snapshot().get('kind') in ('ready', 'configured', 'telemetry'))

    async def _shutdown(self):
        """Stop supervision, then prove firmware automatic. Caller holds _mutation."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
        self._applied = None
        if self._client is not None:
            await module_runtime.offload('fan', self._client.close)

    def _withdraw_lease(self):
        # Whole-plugin shutdown can wait for unrelated work before async unload; EOF releases now.
        self._closing = True
        if self._client is not None:
            self._client.withdraw()

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
                await self._ensure_loaded()
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
                    # A closed session's own error is history once Fan is OFF; a latched fault keeps self._error.
                    error=self._error or (telemetry.get('error', '') if telemetry and self._state['enabled'] else ''), active_curve=self._preset or 'global',
                    telemetry=telemetry,
                    limits=dict(min_rpm=MIN_RPM, last_min_rpm=LAST_MIN_RPM, max_rpm=MAX_RPM, step=RPM_STEP,
                                hard_max=HARD_MAX_C), presets=list(PRESETS), templates=copy.deepcopy(TEMPLATES))

    async def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Enabled must be a boolean.')
        async with self._mutation:
            await self._ensure_loaded()
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
            value = self._revised(enabled=enabled)
            if not enabled:
                if self._state['enabled']:
                    self._error = ''  # Resume/pause notes describe the ON intent; a fault latch is already OFF and keeps its reason.
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
                        try:
                            unconfirmed = await self._close_client()
                        except Exception:
                            # The OFF must survive the next Decky start even when its release failed.
                            if self._settings_valid:
                                await self._persist(value)
                            raise
                        self._error = unconfirmed or self._client.snapshot().get('error') or str(communication)
                if self._settings_valid:
                    await self._persist(value)
                if enabled and not self._live():
                    # Explicit enable replaces a pending re-arm and resets its retry budget.
                    self._error = ''
                    await self._shutdown()
                    await self._start_client()
                    self._task = asyncio.create_task(self._monitor())
                elif enabled:
                    await self._apply()
                if enabled:
                    self._error = ''
                else:
                    # No background EC ownership, sensor polling or workers while off.
                    await self._shutdown()
            except Exception as exc:
                self._error = str(exc)
                if enabled:
                    self._state['enabled'] = False; self._applied = None
                    if not self._closing:  # Unload never changes the saved intent.
                        try:
                            await self._persist(self._revised())
                        except Exception as save_error:
                            self._error += ' ' + str(save_error)
                    await self._shutdown()
                else:
                    # Disk failure cannot leave idle workers or a monitor that
                    # might keep retrying the saved pre-disable intent.
                    try:
                        await self._shutdown()
                    except Exception as close_error:
                        self._error += ' ' + str(close_error)
                raise
        return await self.get_state()

    async def save_unit(self, unit, expected_revision):
        if unit not in ('rpm', 'percent'):
            raise ValueError('Unknown fan display unit.')
        async with self._mutation:
            await self._ensure_loaded()
            if not self._settings_valid or type(expected_revision) is not int or expected_revision != self._state['revision']:
                raise ValueError('Fan settings changed elsewhere. Reload before changing units.')
            value = self._revised(display_unit=unit)
            await self._persist(value)
        return await self.get_state()

    async def save_curve(self, scope, curve, expected_revision):
        if scope != 'global' and scope not in PRESETS:
            raise ValueError('Unknown curve scope.')
        checked = None if curve is None and scope != 'global' else validate_curve(curve)
        async with self._mutation:
            await self._ensure_loaded()
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
            if self._live():
                await self._apply()
        return await self.get_state()

    async def save_assignment_mode(self, enabled, expected_revision):
        if type(enabled) is not bool:
            raise ValueError('TDP curve assignments must be a boolean.')
        async with self._mutation:
            await self._ensure_loaded()
            if not self._settings_valid or type(expected_revision) is not int or expected_revision != self._state['revision']:
                raise ValueError('Fan settings changed elsewhere. Reload before changing assignments.')
            value = self._revised(assign_tdp_presets=enabled)
            await self._persist(value)
            if self._live():
                try:
                    await self._apply()
                except Exception as exc:
                    # A failed curve switch must not leave the previous mode armed.
                    self._error = str(exc); self._applied = None
                    self._state = self._revised(enabled=False)
                    try:
                        await self._persist(self._state)
                    finally:
                        await self._shutdown()
                    raise
            else:
                self._preset = None
        return await self.get_state()

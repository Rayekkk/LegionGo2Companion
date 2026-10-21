# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Decky entry point: lifecycle ordering and one guarded RPC surface over the hardware backends."""

from __future__ import annotations

import asyncio
import json
import functools
import inspect
import os
import platform
import stat
import sys
import time
from pathlib import Path

import decky

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import battery_backend
import controller_backend
import display_backend
import fan_backend
import remap_backend
import rgb_backend
import tdp_backend
import vibration_backend
import wifi_backend
import module_runtime
import diagnostics
from companion_updates import CompanionUpdates
import device_report
from controller_firmware import FirmwarePreparation, INSTALL_SECONDS, updater_running, applies as firmware_applies
from safe_settings import SettingsManager
from module_control import withdraw, capture_intent, restore_intent
from desktop_bridge import DesktopBridge
from desktop_install import DesktopInstall
from inputplumber_process import LostControllerRecovery
from conflict_guard import installed_conflicts, CHECK_INTERVAL_S

MODULE_NAMES = ('tdp', 'fan', 'vibration', 'display', 'wifi', 'rgb', 'remap', 'battery', 'controller')
FIRMWARE_RPCS = frozenset({'firmware_get_status', 'firmware_check', 'firmware_download', 'firmware_install'})
UPDATE_RPCS = frozenset({'updates_check', 'updates_download'}) | FIRMWARE_RPCS
# Read-only and for any console: no module of its own, also during controller maintenance (it opens no
# controller), never while the conflict guard has paused Companion (another plugin may be calling the firmware).
REPORT_RPCS = frozenset({'save_device_report'})
# Admission exceptions (_guard_component_calls). By default a public RPC waits for startup, is refused while the
# conflict guard is blocked/failed or the plugin closes, and needs its module (_rpc_module) enabled, ready and
# not paused; module-less calls (modules_*, updates) also wait while controllers are paused and drain admitted RPCs.
_UNGUARDED_RPCS = frozenset({'get_version'})
# Paused or disabled modules cannot hide the update state; this snapshot never opens a controller.
_ALWAYS_ADMITTED_RPCS = frozenset({'firmware_get_status'})
_RPC_RESOURCE = {'battery_get_controller_levels': 'controller'}  # Pauses with the controllers, not the battery.
_PAUSE_EXEMPT_RPCS = FIRMWARE_RPCS | REPORT_RPCS  # The firmware handoff itself, and the read-only report.
_PAUSE_EXEMPT_MODULE_CHANGES = tuple(name for name in MODULE_NAMES if name not in module_runtime.CONTROLLER_MODULES)
# May download (Extras: RyzenAdj); the report runs up to 20 s and never waits for module calls to drain.
_BACKGROUND_RPCS = UPDATE_RPCS | REPORT_RPCS | {'retry_extras', 'set_extras_unlocked'}
_UPDATER_RPCS = UPDATE_RPCS - FIRMWARE_RPCS  # Need the companion updater started.
GUARD_FAILURE_LIMIT = 3  # Consecutive failed scans (~15 s of periodic checks) before modules are withdrawn.
UPDATER_WAIT_S = INSTALL_SECONDS  # A write that outlives its Companion is one fwupdtool run.
UPDATER_POLL_S = 2.0
SHUTDOWN_BUDGET_S = 4.0  # Of Decky's 5 s SIGTERM-to-SIGKILL window; _uninstall needs the rest.
# All nine modules start in ~1.2 s on the console. A start can legitimately take much longer
# (Wi-Fi rolling back an interrupted band-policy change: iwd restart up to 15 s, reconnect up
# to 33 s, nmcli edits 8 s each): past this limit the next module goes, the slow one keeps running.
MODULE_START_LIMIT_S = 15.0
# A stop gives a start still running past the limit this long to finish and be unloaded,
# next to the other modules' unload; it fits SHUTDOWN_BUDGET_S.
STARTING_STOP_WAIT_S = 2.0
# An update's band policy waits for WiFi to reconnect after uninstall restored the network files:
# about two minutes of retries, then the next start tries again.
BAND_RESTORE_RETRY_S = 20.0
BAND_RESTORE_ATTEMPTS = 6
_ADVISORY_RPCS = frozenset({'set_panel_active', 'set_active_app', 'vibe_set_active_app'})  # Besides reads.
# Preconditions the backend checks before changing anything; the reply already tells the user why.
_REFUSALS = frozenset({'unsupported_backend', 'unsupported_device', 'preflight_failed', 'no_wifi',
                       'backend_inactive', 'iwd_inactive', 'preference_disabled', 'managed_profile_not_active'})


def _refused_before_change(exc):
    """A restore the backend turned down before touching anything: safe to repeat once WiFi is back."""
    return isinstance(exc, module_runtime.NotConfirmed) and exc.code in _REFUSALS


def _logged_rpc(function, name):
    """Observe the existing boundary; never retry, cancel or change its result."""
    read = name.startswith(('get_', 'is_')) or '_get_' in name or name.endswith('_is_ready')
    quiet = read or name in _ADVISORY_RPCS
    component = _rpc_module(name) or ('firmware' if name.startswith('firmware_') else 'plugin')
    @functools.wraps(function)
    async def logged(self, *args, **kwargs):
        with diagnostics.operation(name=name):
            started = time.monotonic()
            def failed(**fields):
                duration = round((time.monotonic() - started) * 1000, 1)
                if quiet:
                    diagnostics.transition(component, 'rpc_health', 'failed', level='warning', operation=name, duration_ms=duration, **fields)
                else:
                    diagnostics.event(component, 'rpc_failed', level='warning', operation=name,
                                      outcome='not_confirmed', duration_ms=duration, **fields)
            def refused(reason):
                # Expected refusals are context, not problems; reads stay silent.
                if not quiet:
                    diagnostics.event(component, 'rpc_completed', operation=name, outcome='refused', reason=reason,
                                      duration_ms=round((time.monotonic() - started) * 1000, 1))
            if not quiet:
                diagnostics.event(component, 'rpc_started', operation=name)
            try:
                result = await function(self, *args, **kwargs)
            except asyncio.CancelledError:
                # The underlying admitted transaction may still be draining.
                diagnostics.event(component, 'rpc_waiter_cancelled',
                                  operation=name, outcome='refresh_required', repeat=60)
                raise
            except module_runtime.AdmissionClosed:
                refused('admission_closed')
                raise
            except Exception as exc:
                failed(error=exc)
                raise
            if isinstance(result, dict) and result.get('success') is False and result.get('error') in _REFUSALS:
                refused(result['error'])
            # restart_error stays in the shared display state; only the restart call itself owns it.
            elif isinstance(result, dict) and (result.get('success') is False
                                               or (name == 'display_restart_session' and bool(result.get('restart_error')))):
                code = result.get('returncode')
                failed(reason='backend_reported_failure', returncode=code if type(code) is int else None)
            elif quiet:
                diagnostics.transition(component, 'rpc_health', 'ok', operation=name, initial=False)
            else:
                diagnostics.event(component, 'rpc_completed', operation=name,
                    outcome='succeeded' if isinstance(result, dict) and result.get('success') is True else 'returned',
                    duration_ms=round((time.monotonic() - started) * 1000, 1))
            return result
    return logged

def _rpc_module(name):
    for prefix, module in (('fan_', 'fan'), ('vibe_', 'vibration'), ('display_', 'display'), ('wifi_', 'wifi'),
                           ('rgb_', 'rgb'), ('remap_', 'remap'), ('battery_', 'battery'), ('controller_', 'controller')):
        if name.startswith(prefix):
            return module
    return None if name.startswith('modules_') or name in UPDATE_RPCS or name in REPORT_RPCS else 'tdp'


def _plugin_version() -> str:
    try:
        with open(os.path.join(PLUGIN_DIR, "plugin.json"), encoding="utf-8") as handle:
            metadata = json.load(handle)
            value = metadata.get("version") if isinstance(metadata, dict) else None
        return str(value or "0.0.0")
    except (OSError, ValueError, TypeError):
        return "0.0.0"


def _session_account():
    """The console (Decky) user's pwd entry, never root; None when unknown."""
    try:
        import pwd
        username = getattr(decky, 'DECKY_USER', None) or os.environ.get('DECKY_USER')
        account = pwd.getpwnam(username) if username else None
    except (ImportError, KeyError):
        return None
    return account if account is not None and account.pw_uid > 0 else None


def _runtime_identity(dmi='/sys/class/dmi/id', os_release='/etc/os-release') -> dict:
    """What a bug report needs first; firmware strings come from DMI, never from the EC."""
    def read(path, limit=128):
        try:
            with open(path, encoding='utf-8', errors='replace') as handle:
                return handle.read(limit).strip()
        except OSError:
            return ''
    release = dict(line.partition('=')[::2] for line in read(os_release, 8192).splitlines())
    value = lambda text: text.strip().strip('"') or None
    return {'model': value(read(os.path.join(dmi, 'product_name'))),
            'bios': value(read(os.path.join(dmi, 'bios_version'))),
            'ec': value(read(os.path.join(dmi, 'ec_firmware_release'))),
            'steamos': value(release.get('VERSION_ID', '')),
            'steamos_build': value(release.get('BUILD_ID', '')),
            'decky': getattr(decky, 'DECKY_VERSION', None) or None}


def _installed_standalone_plugins() -> list[str]:
    return installed_conflicts(PLUGIN_DIR)


def _rotate_logs(keep: int = 10) -> None:
    """Decky creates a timestamped log per plugin load and never prunes them.
    Keep the newest `keep`; diagnostics have their own size-based rotation."""
    try:
        log_dir = getattr(decky, "DECKY_PLUGIN_LOG_DIR", None)
        if not log_dir or os.name != "posix":
            return
        # Decky chowns this directory to deck: never follow a symlink swapped in for it.
        try:
            fd = os.open(log_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except (FileNotFoundError, NotADirectoryError):
            return
        try:
            files = []
            for name in os.listdir(fd):
                if name.endswith(".log"):
                    try:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:  # Decky may prune or create logs meanwhile.
                        continue
                    if stat.S_ISREG(info.st_mode):
                        files.append((info.st_mtime, name))
            if len(files) <= keep:
                return
            files.sort(reverse=True)
            current_log = os.path.basename(getattr(decky, "DECKY_PLUGIN_LOG", None) or "")
            removed = 0
            for _mtime, name in files[keep:]:
                if name == current_log:  # Never delete the file we're currently writing to.
                    continue
                try:
                    os.unlink(name, dir_fd=fd)
                    removed += 1
                except OSError:
                    pass
        finally:
            os.close(fd)
        if removed:
            decky.logger.info(f"Rotated logs: removed {removed} old file(s), kept {keep} newest")
    except Exception as e:
        diagnostics.event('plugin', 'rotate_logs_failed', level='error', error=e, repeat=60)


def _end_decky_listener_at_eof():
    """Decky 3.2.x re-reads its RPC socket after the loader closes it on stop, and
    at EOF that loop never yields: the event loop starves, _unload stalls at its
    first await and Decky SIGKILLs the plugin. End that listener at EOF instead."""
    for cell in getattr(getattr(decky, 'emit', None), '__closure__', None) or ():
        try:
            socket = getattr(cell.cell_contents, '_socket', None)  # SandboxedPlugin._socket
        except ValueError:
            continue
        read = getattr(socket, '_read_single_line', None)
        if read is None:
            continue
        async def read_line(reader, read=read):
            line = await read(reader)
            if not line:  # The loader closed the connection; nothing more can arrive.
                raise asyncio.CancelledError
            return line
        socket._read_single_line = read_line
        return
    # A Decky update changed these internals: a stop may be SIGKILLed before hardware is restored.
    diagnostics.event('plugin', 'decky_listener_unpatched', level='warning')


def _guard_component_calls(cls):
    """Default-deny every public component RPC, including future additions."""
    for name, method in tuple(vars(cls).items()):
        if name.startswith("_") or name in _UNGUARDED_RPCS or not inspect.iscoroutinefunction(method):
            continue
        def wrap(fn):
            name = fn.__name__
            @functools.wraps(fn)
            async def guarded(self, *args, **kwargs):
                await self._ensure_guard()
                if name in _ALWAYS_ADMITTED_RPCS:
                    return await fn(self, *args, **kwargs)
                if self._startup is not None:
                    # A request must not reach modules before _main has started them.
                    await self._startup.wait()
                async with self._guard_lock:
                    if self._guard_blocked or self._guard_error or self._closing:
                        raise module_runtime.AdmissionClosed(self._guard_message())
                    module = _rpc_module(name)
                    resource = _RPC_RESOURCE.get(name, module)
                    if module_runtime.is_paused(resource) or (module is None and module_runtime.is_paused('controller')
                                                              and not _pause_exempt(name, args, kwargs)):
                        # Lifecycle/update admission must not tear down paused
                        # consumers or mutate module choices during a handoff.
                        raise module_runtime.ControllerPaused('Controller operations are paused for firmware maintenance.')
                    module_runtime.require_available(resource)
                    if module and not self._applies(module):
                        raise RuntimeError('This module does not apply to this console.')
                    if module and not self._module_enabled(module):
                        raise RuntimeError('This module is disabled. Enable it in Manage Modules.')
                    if module and self._still_starting(module):
                        raise RuntimeError('This module is still starting. Try again in a moment.')
                    if module and self._module_health.get(module, {}).get('ready') is False:
                        raise RuntimeError('This module could not start. Retry it in Manage Modules.')
                    if name in _BACKGROUND_RPCS:
                        # Register download work before releasing the admission
                        # gate, but never hold hardware controls behind networking.
                        if name in _UPDATER_RPCS:
                            self._ensure_updates()
                        task = asyncio.create_task(fn(self, *args, **kwargs))
                        self._update_jobs.add(task)
                        task.add_done_callback(self._update_jobs.discard)
                    elif module:
                        # Independent modules need not queue behind a slow WiFi
                        # probe. Lifecycle changes still drain every admitted RPC.
                        task = asyncio.create_task(self._run_rpc(resource, fn, args, kwargs))
                        self._rpc_jobs.add(task)
                        task.add_done_callback(self._rpc_jobs.discard)
                        if resource in module_runtime.CONTROLLER_MODULES:
                            self._controller_rpc_jobs.add(task)
                            task.add_done_callback(self._controller_rpc_jobs.discard)
                    else:
                        await module_runtime.complete(self._drain_rpc_jobs())
                        return await module_runtime.complete(fn(self, *args, **kwargs))
                return await module_runtime.complete(task)
            return _logged_rpc(guarded, name)
        setattr(cls, name, wrap(method))
    return cls


def _pause_exempt(name, args, kwargs):
    if name == 'modules_set_enabled':
        return (args[0] if args else kwargs.get('name')) in _PAUSE_EXEMPT_MODULE_CHANGES
    return name in _PAUSE_EXEMPT_RPCS


@_guard_component_calls
class Plugin:
    """Compose the audited Legion Go 2 controls into one Decky plugin."""

    def __init__(self):
        _end_decky_listener_at_eof()
        self._guard_lock = asyncio.Lock()
        self._rpc_locks = {name: asyncio.Lock() for name in MODULE_NAMES}
        # These calls share InputPlumber filters, profiles and HID diagnostics.
        self._rpc_locks['remap'] = self._rpc_locks['controller']
        self._rpc_jobs = set()
        self._controller_rpc_jobs = set()
        self._guard_scan = None
        self._guard_task = None
        self._updater_wait = None
        self._band_restore = None
        self._inputplumber = None  # Restarts an InputPlumber that lost the controller (Legion Go family).
        self._inputplumber_recovery = None
        self._device_report = None  # The running save_device_report; concurrent requests share it.
        self._report_collecting = None  # Its collection thread, which can outlive a timed-out request.
        self._guard_blocked = False
        self._guard_conflicts = []
        self._guard_error = ""
        self._guard_failures = 0
        self._guard_checked = False
        self._modules_started = False
        self._startup = None  # Event while _main is starting modules; RPCs wait for it.
        self._ever_started = False
        self._closing = False
        self._uninstalling = False  # Diagnostic sink lifetime only, never admission.
        self._updates = None
        self._firmware = FirmwarePreparation(pads=lambda: self._controller._controller_batteries(max_age=0))
        self._desktop = None
        self._desktop_install = None
        self._desktop_lock = asyncio.Lock()
        self._diagnostics_read = None
        self._desktop_panel_deadline = 0.0
        self._qam_panel_deadline = 0.0
        self._update_jobs = set()
        self._module_store = SettingsManager('module_settings', decky.DECKY_PLUGIN_SETTINGS_DIR)
        self._module_state = self._module_store.getSetting('modules', {})
        if self._module_store.recovery_error:
            # Unknown choices are not a first install. Never start modules or
            # guess a hardware rollback while their enabled state is lost.
            self._module_state = {name: {'enabled': False,
                'error': 'Saved module choices could not be recovered. Review the module before enabling it again.'}
                for name in MODULE_NAMES}
        if not isinstance(self._module_state, dict):
            self._module_state = {name: {'enabled': False, 'error': 'Invalid module settings. Review before enabling.'} for name in MODULE_NAMES}
        self._module_state = {name: value if isinstance(value, dict)
            and type(value.get('enabled')) is bool and type(value.get('pending', False)) is bool
            and isinstance(value.get('resume', {}), dict)
            else {'enabled': False, 'error': 'Invalid module settings. Review before enabling.'}
            for name, value in self._module_state.items() if name in MODULE_NAMES}
        self._module_health = {}
        self._applicable = {}  # Module name -> it applies to this console (_ensure_device); empty until read.
        self._device = {}
        self._starting = {}  # Module name -> its startup task; never cancelled mid firmware call.
        self._late_stops = {}  # Module name -> its unload queued behind a start a stop found still running.
        self._stop_deadline = 0.0  # End of the shutdown budget; _uninstall waits for late starts until then.
        self._shutdown_requested = asyncio.Event()  # wakes a startup wait so shutdown is not held for a module's time limit
        self._tdp = tdp_backend.Plugin()
        self._fan = fan_backend.Plugin()
        self._vibration = vibration_backend.Plugin()
        self._display = display_backend.Plugin()
        self._wifi = wifi_backend.Plugin()
        self._rgb = rgb_backend.Plugin()
        self._remap = remap_backend.Plugin()
        self._battery = battery_backend.Plugin()
        self._controller = controller_backend.Plugin()

    def _ensure_updates(self):
        if self._updates is not None:
            return
        account = _session_account()
        self._updates = CompanionUpdates(PLUGIN_DIR, *((account.pw_dir, account.pw_uid, account.pw_gid)
                                                       if account else ('', -1, -1)))

    async def _run_rpc(self, resource, function, args, kwargs):
        async with self._rpc_locks[resource]:
            # A queued call may have been admitted before a conflict or shutdown.
            if self._guard_blocked or self._guard_error or self._closing:
                raise module_runtime.AdmissionClosed(self._guard_message())
            module_runtime.require_available(resource)
            with module_runtime.controller_scope(resource):
                return await function(self, *args, **kwargs)

    async def _drain_rpc_jobs(self):
        while self._rpc_jobs:
            jobs = tuple(self._rpc_jobs)
            await asyncio.gather(*jobs, return_exceptions=True)
            # gather may complete immediately for finished tasks. Their queued
            # discard callbacks then cannot run until we yield; remove the
            # drained snapshot explicitly rather than spinning on those jobs.
            self._rpc_jobs.difference_update(job for job in jobs if job.done())

    async def _stop_component(self, name):
        try:
            await getattr(self, '_' + name)._unload()
        finally:
            # Monitor cancellation cannot cancel a thread's hardware operation.
            await module_runtime.drain(name)
        diagnostics.event(name, 'module_stopped')

    def _module_enabled(self, name):
        value = self._module_state.get(name, {})
        return isinstance(value, dict) and value.get('enabled', True) is True and not value.get('pending', False)

    def _save_modules(self):
        self._module_store.setSetting('modules', self._module_state)
        self._module_store.commit()

    def _applies(self, name):
        return self._applicable.get(name, True)

    def _detect_device(self):
        """Read-only, once per load: which modules apply to this console. One that does not never
        starts or touches hardware, is refused, and both interfaces hide it with its options."""
        applicable = {}
        for name in MODULE_NAMES:
            check = getattr(getattr(self, '_' + name), 'applicable', None)  # None: applies everywhere (WiFi).
            try:
                applicable[name] = bool(check()) if callable(check) else True
            except Exception as exc:
                diagnostics.event(name, 'applicability_unknown', level='warning', error=exc)
                applicable[name] = False
        try:
            console, model = module_runtime.legion_device()
            # Options inside modules that keep their own model gate (Fan's is the module itself).
            # controller_driver False on a Go 1 means its older controller firmware (no kernel driver).
            device = {'console': console, 'model': model or None, 'firmware': firmware_applies(),
                      'power_led': rgb_backend.power_led_model(), 'controller_driver': module_runtime.kernel_controller()}
        except Exception as exc:  # An unknown console must not stop the plugin from starting.
            diagnostics.event('plugin', 'device_unknown', level='warning', error=exc)
            device = {'console': None, 'model': None, 'firmware': False, 'power_led': False, 'controller_driver': False}
        return applicable, device

    async def _ensure_device(self):
        if not self._applicable:
            self._applicable, self._device = await asyncio.to_thread(self._detect_device)

    def _module_status(self):
        result = {name: {'enabled': self._module_enabled(name),
                       # None until the console is detected: both UIs wait rather than show every module.
                       'applicable': self._applies(name) if self._applicable else None,
                       'ready': self._module_health.get(name, {}).get('ready', True),
                       'pending': bool(self._module_state.get(name, {}).get('pending')),
                       # A startup error no longer applies once the module is turned off.
                       'error': (self._module_state.get(name, {}).get('error', '')
                                 or (self._module_health.get(name, {}).get('error', '')
                                     if self._module_enabled(name) else '')),
                       'note': self._module_state.get(name, {}).get('note', '')}
                for name in MODULE_NAMES}
        for name, entry in result.items():
            if self._still_starting(name):
                # Also once turned off meanwhile (enabled False): it is withdrawn when the start ends.
                entry.update(ready=False, starting=True, error='')
            if entry['enabled'] and module_runtime.is_paused(name):
                entry.update(ready=False, maintenance=True,
                    note='Controller operations are paused for firmware maintenance.')
        display = self._module_state.get('display', {})
        if display.get('session') is not None:
            current = display_backend._gamescope_start_time()
            if current is not None and not display_backend.Plugin._same_session(display['session'], current):
                result['display']['note'] = ''
        return result

    def _still_starting(self, name):
        start = self._starting.get(name)
        return start is not None and not start.done()

    async def _start_module(self, name):
        component = getattr(self, '_' + name)
        module_runtime.begin_start(name)
        try:
            if self._module_health.get(name, {}).get('ready') is False:
                # A failed startup may have partially created workers. Finish
                # their cleanup before retrying the saved user choice.
                await self._stop_component(name)
            self._module_health[name] = {'ready': False}
            with module_runtime.starting(name):  # Not its cleanup below: restoration is never refused.
                result = await component._main()
            if isinstance(result, dict) and result.get('success') is False:
                raise RuntimeError(result.get('error') or result.get('message') or 'Module startup failed.')
            self._module_health[name] = {'ready': True}
        except module_runtime.StartStopped:
            # A shutdown stopped it: not ready and no error; whoever stopped it unloads it.
            diagnostics.event(name, 'module_start_stopped')
        except Exception as exc:
            diagnostics.event(name, 'module_start_failed', level='error', error=exc)
            self._module_health[name] = {'ready': False, 'error': str(exc)}
            try:
                await self._stop_component(name)
            except Exception as stop_error:
                self._module_health[name]['error'] += f' Cleanup failed: {stop_error}'
            raise

    async def _publish_modules(self):
        try:
            await decky.emit('companion_guard', await asyncio.to_thread(self._guard_status))
        except Exception as exc:
            diagnostics.event('plugin', 'module_status_notification_failed', level='warning', error=exc, repeat=60)

    async def modules_restart_session(self):
        result = await self._display.restart_session()
        return {'success': not bool(result.get('restart_error')), 'error': result.get('restart_error', '')}

    async def modules_set_enabled(self, name, enabled):
        if name not in MODULE_NAMES or type(enabled) is not bool:
            raise ValueError('Invalid module or enabled value.')
        if not self._applies(name):
            raise RuntimeError('This module does not apply to this console.')
        if name == 'fan' and not enabled:
            # Fan safety takes precedence even if module-choice persistence fails.
            await self._fan.set_enabled(False)
        if enabled and self._still_starting(name):
            raise RuntimeError('This module is still starting. Try again when it has finished.')
        previous = dict(self._module_state)
        component = getattr(self, '_' + name)
        resume = self._module_state.get(name, {}).get('resume', {})
        if enabled:
            if self._module_state.get(name, {}).get('pending'):
                raise RuntimeError('Retry disabling this module to finish restoration first.')
            if self._module_enabled(name) and self._module_health.get(name, {}).get('ready', True):
                return await asyncio.to_thread(self._module_status)
            # Startup is gated until the durable intent is written; failure stays visible.
            self._module_state[name] = {'enabled': True, 'resume': resume}
            try:
                await module_runtime.complete(asyncio.to_thread(self._save_modules))
            except Exception:
                self._module_state = previous
                raise
            try:
                await self._start_module(name)
                await restore_intent(name, component, resume)
                self._module_state[name] = {'enabled': True}
                await module_runtime.complete(asyncio.to_thread(self._save_modules))
            except Exception as exc:
                self._module_state[name] = {'enabled': False, 'pending': True, 'resume': resume, 'error': str(exc)}
                try:
                    await module_runtime.complete(asyncio.to_thread(self._save_modules))
                finally:
                    try:
                        if self._module_health.get(name, {}).get('ready', True):
                            await self._stop_component(name)
                    finally:
                        await self._publish_modules()
                raise
        else:
            if not self._module_enabled(name) and not self._module_state.get(name, {}).get('pending'):
                return await asyncio.to_thread(self._module_status)
            # Persist the gate before cleanup. An interrupted withdrawal never starts
            # the module on next boot; it retries cleanup with the saved ownership.
            # An update still restoring keeps its choice; the live one is only the baseline.
            if not (self._module_state.get(name, {}).get('pending') or self._module_state.get(name, {}).get('update')):
                resume = await asyncio.to_thread(capture_intent, name)
            self._module_state[name] = {'enabled': False, 'pending': True, 'resume': resume}
            try:
                await module_runtime.complete(asyncio.to_thread(self._save_modules))
            except Exception:
                self._module_state = previous
                raise
            await self._publish_modules()
            if self._still_starting(name):
                # Never interrupt its hardware work: _finish_late_start withdraws it once the start ends.
                return await asyncio.to_thread(self._module_status)
            try:
                await self._withdraw_module(name, resume)
            except Exception as exc:
                self._module_state[name] = {'enabled': False, 'pending': True, 'resume': resume, 'error': str(exc)}
                await module_runtime.complete(asyncio.to_thread(self._save_modules))
        await self._publish_modules()
        return await asyncio.to_thread(self._module_status)

    async def _run_stage(self, stage: str, names=MODULE_NAMES) -> None:
        runnable = tuple(
            (name, component)
            for name, component in ((name, getattr(self, '_' + name)) for name in names)
            if (self._module_enabled(name) or (stage == '_unload' and self._module_state.get(name, {}).get('pending')))
            and (stage == '_unload' or not module_runtime.is_paused(name)) and self._applies(name)
            and callable(getattr(component, stage, None))
        )
        if stage == '_main':
            # One at a time: the Legion's AML aborts WMI/EC methods run concurrently
            # (AE_AML_NOT_OWNER on ECMT). Unload keeps gather for Decky's 5 s budget.
            # A start past the time limit is left running, never cancelled mid firmware
            # call: one hung module must not hold back the rest (Fan comes second).
            results = []
            for name, _ in runnable:
                if self._closing:
                    break  # Shutdown waits on the guard lock; skip modules not yet started.
                began = time.monotonic()
                start = self._starting[name] = asyncio.ensure_future(self._start_module(name))
                woken = asyncio.ensure_future(self._shutdown_requested.wait())
                try:
                    await asyncio.wait({start, woken}, timeout=MODULE_START_LIMIT_S, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    woken.cancel()
                if start.done():
                    results.append(start.exception() if not start.cancelled() else None)
                    continue
                if self._closing:
                    break  # Shutdown woke the wait; _stop_modules bounds the start still running.
                diagnostics.event(name, 'module_start_slow', level='warning', seconds=round(time.monotonic() - began, 1))
                module_runtime.background_task(self._finish_late_start(name, start, began))
                results.append(None)
        else:
            results = await asyncio.gather(*(getattr(component, stage)() for _, component in runnable),
                                           return_exceptions=True)
        for (name, _), result in zip(runnable, results):
            if isinstance(result, BaseException):
                diagnostics.event(name, 'module_stage_failed', level='error', stage=stage, error=result)

    async def _finish_late_start(self, name, start, began):
        await asyncio.wait({start})
        # A failure was already recorded in health and logged as module_start_failed,
        # a stopped start (not ready, no error) as module_start_stopped.
        if self._module_health.get(name, {}).get('ready'):
            diagnostics.event(name, 'module_started_late', seconds=round(time.monotonic() - began, 1))
        async with self._guard_lock:
            if self._closing or not self._modules_started:
                return  # The stop that came meanwhile unloads it (_stop_after_start).
            await self._recover_pending_module(name)  # Turned off while it was starting.
            await self._restore_resumed((name,))
        await self._publish_modules()

    def _stop_after_start(self, name):
        """Unload a module once its running start has finished, never alongside it."""
        start = self._starting[name]
        async def stop():
            await asyncio.wait({start})
            if not start.cancelled() and start.exception() is None:  # A failed start unloaded itself.
                await self._stop_component(name)
        task = self._late_stops[name] = module_runtime.background_task(stop())
        return task

    def _guard_message(self):
        if self._guard_error:
            return "Companion is paused: installed plugins could not be verified. " + self._guard_error
        if self._guard_conflicts:
            return ("All Companion modules are paused. Uninstall "
                    + ", ".join(self._guard_conflicts)
                    + " in Decky Settings, then restart Decky or the console.")
        return "Companion is paused. Restart Decky or the console to resume safely."

    def _guard_status(self):
        return {"version": _plugin_version(),
                "modules": self._module_status(),
                "device": dict(self._device),
                "standalone_plugins": list(self._guard_conflicts),
                "blocked": self._guard_blocked or bool(self._guard_error),
                # Only this latch survives a later clean scan; guard_error alone is transient.
                "latched": self._guard_blocked,
                "restart_required": self._guard_blocked and not self._guard_conflicts and not self._guard_error,
                "guard_error": self._guard_error,
                "message": self._guard_message() if self._guard_blocked or self._guard_error else ""}

    async def _ensure_guard(self):
        # Admission uses the last periodic scan; only the very first caller scans.
        if not self._guard_checked:
            await self._check_guard()

    async def _inspect_guard(self):
        # Concurrent callers share the pending scan only.
        if self._guard_scan is None or self._guard_scan.done():
            self._guard_scan = asyncio.create_task(self._inspect_guard_once())
        task = self._guard_scan
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._guard_scan is task:
                self._guard_scan = None

    async def _inspect_guard_once(self):
        # Only these fields can change during a conflict scan. Building the
        # full status also reads version metadata and probes the display session.
        before = (self._guard_blocked, tuple(self._guard_conflicts), self._guard_error)
        # Retry briefly so one transient error does not close admission. A failing
        # scan blocks admission while it fails; withdrawal waits for repeated failure.
        for attempt in range(3):
            try:
                self._guard_conflicts = await asyncio.to_thread(_installed_standalone_plugins)
                self._guard_error = ""
                break
            except (OSError, ValueError, TypeError) as exc:
                if attempt == 2:
                    self._guard_error = str(exc)
                    diagnostics.event('plugin', 'guard_inspection_failed', level='error', error=exc, repeat=60)
                else:
                    await asyncio.sleep(0.1)
        self._guard_checked = True
        self._guard_failures = self._guard_failures + 1 if self._guard_error else 0
        if self._guard_conflicts or self._guard_failures >= GUARD_FAILURE_LIMIT:
            # Latched until a fresh process starts. Reusing partially unloaded
            # module globals would risk duplicate workers or lost restoration state.
            self._guard_blocked = True
        if before != (self._guard_blocked, tuple(self._guard_conflicts), self._guard_error):
            closed = self._guard_blocked or bool(self._guard_error)
            diagnostics.transition('plugin', 'guard', 'blocked' if closed else 'clear', level='warning' if closed else 'info', count=len(self._guard_conflicts), reason='inspection_unavailable' if self._guard_error else 'conflict_scan')
            try:
                await decky.emit("companion_guard", await asyncio.to_thread(self._guard_status))
            except Exception as exc:
                diagnostics.event('plugin', 'guard_notification_failed', level='warning', error=exc, repeat=60)

    async def _check_guard(self):
        await self._inspect_guard()
        if self._guard_blocked and self._modules_started:
            async with self._guard_lock:
                if self._modules_started:
                    await self._stop_modules()
                    decky.logger.warning("[legiongo2companion] all module workers stopped by conflict guard")
        if self._guard_blocked:
            # After withdrawal: the firmware worker can take seconds to join.
            if self._updates is not None:
                await module_runtime.complete(asyncio.to_thread(self._updates.close))
            try:
                await module_runtime.complete(asyncio.to_thread(self._firmware.close))
            except Exception as exc:
                diagnostics.event('firmware', 'observer_cleanup_failed', level='warning', stage='guard_withdrawal', error=exc)

    async def _stop_modules(self):
        self._withdraw_fan_lease()
        async def stop():
            await self._drain_rpc_jobs()
            began = time.monotonic()
            starting = tuple(name for name in MODULE_NAMES if self._still_starting(name))
            for name in starting:
                module_runtime.request_stop(name)  # It skips its remaining steps and applies nothing more.
            late = [self._stop_after_start(name) for name in starting]
            names = tuple(name for name in MODULE_NAMES if name not in starting)
            await (self._run_stage("_unload", names) if starting else self._run_stage("_unload"))
            # Both normal shutdown and conflict withdrawal must finish threads
            # that outlive the cancellation of their async monitor.
            await asyncio.gather(*(module_runtime.drain(name) for name in names))
            # Within Decky's budget; past it the start's own stop check keeps it from applying more,
            # and its unload still follows if the process outlives it (conflict withdrawal).
            if late:
                pending = (await asyncio.wait(late, timeout=max(began + STARTING_STOP_WAIT_S - time.monotonic(), 0)))[1]
                if pending:
                    diagnostics.event('plugin', 'module_stop_timeout', level='warning',
                                      modules=','.join(name for name, task in zip(starting, late) if task in pending))
        try:
            await module_runtime.complete(stop())
        finally:
            self._modules_started = False

    async def _watch_conflicts(self):
        while True:
            await asyncio.sleep(CHECK_INTERVAL_S)
            await self._check_guard()

    async def _recover_pending_module(self, name):
        entry = self._module_state.get(name, {})
        if not entry.get('pending') or not self._applies(name):
            return
        resume = entry.get('resume', {})
        try:
            await self._withdraw_module(name, resume)
        except Exception as exc:
            self._module_state[name] = {'enabled': False, 'pending': True, 'resume': resume, 'error': str(exc)}
            diagnostics.event(name, 'module_recovery_failed', level='error', error=exc)
            try:
                await module_runtime.complete(asyncio.to_thread(self._save_modules))
            except Exception as save_error:
                diagnostics.event(name, 'module_recovery_persist_failed', level='error', error=save_error)

    async def _withdraw_module(self, name, resume):
        note = await withdraw(name, getattr(self, '_' + name))
        self._module_state[name] = {'enabled': False, 'note': note, 'resume': resume}
        if name == 'display':
            self._module_state[name]['session'] = await asyncio.to_thread(display_backend._gamescope_start_time)
        await module_runtime.complete(asyncio.to_thread(self._save_modules))

    async def _migration(self):
        await self._ensure_device()
        await self._check_guard()
        if self._guard_blocked or self._guard_error:
            return  # _main scans again before any module starts.
        async with self._guard_lock:
            await module_runtime.complete(self._drain_rpc_jobs())
            await self._run_stage("_migration")

    async def _main(self):
        startup = self._startup = asyncio.Event()
        try:
            await self._start(startup)
        finally:
            startup.set()

    async def _start(self, startup):
        await asyncio.to_thread(diagnostics.configure, diagnostics.LOG_DIR, decky.logger)
        await asyncio.to_thread(_rotate_logs)
        diagnostics.event('plugin', 'startup', version=_plugin_version(), source='backend')
        diagnostics.event('plugin', 'runtime', source='backend', kernel=platform.release(),
                          python=platform.python_version(), architecture=platform.machine(),
                          **await asyncio.to_thread(_runtime_identity))
        await self._ensure_device()
        decky.logger.info(f"[legiongo2companion] startup v{_plugin_version()}")
        if self._device.get('console') and self._inputplumber is None and not self._closing:
            # A system repair, independent of the modules; it never runs during controller maintenance.
            # Its settled controller events also let a module that applies only now start.
            self._inputplumber_recovery = LostControllerRecovery(
                lambda: self._closing or self._guard_blocked or bool(self._guard_error) or module_runtime.is_paused('controller'),
                updater_running, lambda: module_runtime.background_task(self._controller_hotplug()))
            self._inputplumber = module_runtime.background_task(self._inputplumber_recovery.run())
        if self._guard_task is None:
            self._guard_task = asyncio.create_task(self._watch_conflicts())
        await self._check_guard()
        if self._guard_blocked or self._guard_error:
            # Modules never start in this process, so a later clean scan must not reopen admission.
            self._guard_blocked = True
            startup.set()
            await self._start_desktop()
            return
        async with self._guard_lock:
            if self._modules_started or self._guard_blocked or self._closing:
                return
            await module_runtime.complete(self._drain_rpc_jobs())
            await self._inspect_guard()
            if self._guard_blocked or self._guard_error:
                self._guard_blocked = True
                return
            self._modules_started = self._ever_started = True
            if await asyncio.to_thread(updater_running):
                # A previous process's fwupdtool outlives it (KillMode=process), as does Steam's updater.
                module_runtime.pause_controller()
                diagnostics.event('firmware', 'startup_waits_for_updater', level='warning')
                self._updater_wait = module_runtime.background_task(self._start_controllers_after_updater())
            await self._start_modules(MODULE_NAMES)
            # One summary instead of a record per healthy module; failures logged their own cause.
            # A shutdown during startup skipped the remaining modules, so there is nothing to summarise.
            if not self._closing:
                diagnostics.event('plugin', 'startup_complete', source='backend',
                    ready=','.join(name for name in MODULE_NAMES if self._module_enabled(name) and self._applies(name)
                                   and self._module_health.get(name, {}).get('ready', True)) or None,
                    not_applicable=','.join(name for name in MODULE_NAMES if not self._applies(name)) or None,
                    failed=','.join(name for name in MODULE_NAMES
                                    if (self._module_health.get(name, {}).get('ready') is False
                                        and not self._still_starting(name))
                                    or self._module_state.get(name, {}).get('pending')) or None,
                    starting=','.join(name for name in MODULE_NAMES if self._still_starting(name)) or None)
        startup.set()
        await self._publish_modules()
        await self._check_guard()
        await self._start_desktop()

    async def _start_modules(self, names):
        for name in names:
            if not module_runtime.is_paused(name):
                await self._recover_pending_module(name)
        await self._run_stage("_main", names)
        if self._closing:
            return  # Modules skipped by a shutdown never started; nothing to restore into.
        await self._restore_resumed(names)

    async def _restore_resumed(self, names):
        for name in names:
            entry = self._module_state.get(name, {})
            if (self._module_enabled(name) and self._module_health.get(name, {}).get('ready', True)
                    and not module_runtime.is_paused(name) and self._applies(name)
                    and entry.get('resume')):
                try:
                    await restore_intent(name, getattr(self, '_' + name), entry['resume'])
                    self._module_state[name] = {'enabled': True}
                    await module_runtime.complete(asyncio.to_thread(self._save_modules))
                except Exception as exc:
                    if entry.get('update'):
                        # The module keeps running on Automatic. Only a refusal made before any change
                        # (WiFi still reconnecting) is retried; a failed change would drop the link again.
                        if _refused_before_change(exc):
                            diagnostics.event(name, 'band_policy_restore_deferred', level='warning', error=exc)
                            if self._band_restore is None or self._band_restore.done():
                                self._band_restore = module_runtime.background_task(self._retry_band_policy(entry))
                        else:
                            diagnostics.event(name, 'band_policy_restore_postponed', level='warning',
                                              attempts=1, error=exc)
                        continue
                    self._module_state[name] = {'enabled': False, 'pending': True,
                                                'resume': entry['resume'], 'error': str(exc)}
                    try:
                        await module_runtime.complete(asyncio.to_thread(self._save_modules))
                    except Exception as save_error:
                        diagnostics.event(name, 'module_recovery_persist_failed', level='error', error=save_error)
                    try:
                        await self._stop_component(name)
                    except Exception as stop_error:
                        diagnostics.event(name, 'module_recovery_stop_failed', level='error', error=stop_error)

    async def _retry_band_policy(self, entry):
        """Retry an update's band policy as a WiFi RPC: a module change or a new user choice ends it."""
        async def attempt(plugin):
            if plugin._module_state.get('wifi') is not entry:
                return False  # The user chose a band policy while this attempt waited.
            await restore_intent('wifi', plugin._wifi, entry['resume'])
            plugin._module_state['wifi'] = {'enabled': True}
            await module_runtime.complete(asyncio.to_thread(plugin._save_modules))
            return True
        error = None
        for count in range(1, BAND_RESTORE_ATTEMPTS + 1):
            await self._band_retry_pause()
            async with self._guard_lock:
                if (self._closing or self._guard_blocked or self._guard_error
                        or self._module_state.get('wifi') is not entry
                        or self._module_health.get('wifi', {}).get('ready') is False):
                    return  # Stopping, or the module was changed; a kept entry waits for the next start.
                job = asyncio.create_task(self._run_rpc('wifi', attempt, (), {}))
                self._rpc_jobs.add(job)
                job.add_done_callback(self._rpc_jobs.discard)
            try:
                if await module_runtime.complete(job):
                    diagnostics.event('wifi', 'band_policy_restored', attempts=count)
                return
            except Exception as exc:
                error = exc
                if not _refused_before_change(exc):
                    break
        diagnostics.event('wifi', 'band_policy_restore_postponed', level='warning',
                          attempts=count, error=error)

    async def _band_retry_pause(self):
        await asyncio.sleep(BAND_RESTORE_RETRY_S)

    async def _start_controllers_after_updater(self):
        deadline = time.monotonic() + UPDATER_WAIT_S
        while await asyncio.to_thread(updater_running):
            if time.monotonic() > deadline:
                # Stay paused: never write the controllers while an updater runs; a restart retries.
                diagnostics.event('firmware', 'startup_updater_timeout', level='error')
                return
            await asyncio.sleep(UPDATER_POLL_S)
        async with self._guard_lock:
            if self._closing or self._guard_blocked or not self._modules_started:
                return
            module_runtime.resume_controller()
            await module_runtime.complete(self._start_modules(
                tuple(name for name in MODULE_NAMES if name in module_runtime.CONTROLLER_MODULES)))
        await self._publish_modules()

    async def _controller_hotplug(self):
        """A controller came or its driver bound: a module that did not apply at load may now (a Go
        whose hid-lenovo-go bound after Decky started). It starts as at startup; none is ever stopped here."""
        if not self._applicable or all(self._applicable.values()):
            return
        applicable, device = await asyncio.to_thread(self._detect_device)
        async with self._guard_lock:
            added = tuple(name for name in MODULE_NAMES if applicable.get(name) and not self._applies(name))
            if not added or self._closing:
                return
            self._applicable.update(dict.fromkeys(added, True))
            self._device['controller_driver'] = self._device.get('controller_driver') or device['controller_driver']
            diagnostics.event('plugin', 'modules_now_applicable', modules=','.join(added))
            # Before startup reaches its modules, it starts these with the rest.
            if self._modules_started and not (self._guard_blocked or self._guard_error):
                await module_runtime.complete(self._start_modules(added))
        await self._publish_modules()

    async def _desktop_get_logs(self):
        # Read-only diagnostics remain available when hardware modules are
        # disabled or gated. Concurrent viewers share one bounded collection.
        if self._closing:
            raise RuntimeError('Companion is stopping. Reconnect after startup.')
        task = self._diagnostics_read
        if task is None or task.done():
            from diagnostics_reader import read_events
            task = module_runtime.background_task(asyncio.to_thread(read_events, diagnostics.LOG_DIR))
            self._diagnostics_read = task
        return await asyncio.shield(task)

    async def _desktop_context(self):
        await self._ensure_guard()
        # Read-only context. Never set frontend app hints from an application that
        # cannot observe Steam's authoritative foreground app.
        app_id = '' if self._guard_blocked or self._guard_error else await asyncio.to_thread(tdp_backend._get_running_appid)
        session = await asyncio.to_thread(display_backend._gamescope_start_time)
        return {'app_id': app_id, 'gamescope': session is not None}

    async def _start_desktop(self):
        async with self._desktop_lock:
            # Root publication in a worker must finish before unload can remove
            # the session, even when startup's caller is cancelled.
            await module_runtime.complete(self._start_desktop_locked())

    async def _start_desktop_locked(self):
        if self._desktop is not None or self._closing:
            return
        bridge = None
        try:
            assets = Path(PLUGIN_DIR) / 'dist' / 'desktop'
            methods = json.loads((assets / 'rpc-methods.json').read_text(encoding='utf-8'))
            if not isinstance(methods, list) or not all(isinstance(name, str) for name in methods):
                raise ValueError('Invalid Desktop RPC contract')
            bridge = DesktopBridge(self, methods, self._desktop_context, {
                'set_panel_active': self._desktop_panel_active,
                'display_restart_session': self._desktop_display_restart,
            }, read_logs=self._desktop_get_logs)
            await bridge.start()
            self._desktop = bridge
            installer = DesktopInstall.for_decky(Path(PLUGIN_DIR) / 'desktop', decky)
            if installer is not None:
                self._desktop_install = installer
                await asyncio.to_thread(installer.install, bridge.origin, bridge.token)
            decky.logger.info('[legiongo2companion] Desktop frontend ready')
        except Exception as exc:
            if bridge is not None:
                await bridge.close()
            self._desktop = None
            diagnostics.event('desktop', 'frontend_unavailable', level='warning', error=exc)

    async def _panel_active(self, deadline, active):
        if type(active) is not bool:
            raise ValueError('active must be a boolean')
        now = time.monotonic()
        setattr(self, deadline, now + 90 if active else 0)
        return await self._tdp.set_panel_active(now < max(self._desktop_panel_deadline, self._qam_panel_deadline))

    async def _desktop_panel_active(self, active):
        return await self._panel_active('_desktop_panel_deadline', active)

    async def _desktop_display_restart(self):
        if await asyncio.to_thread(display_backend._gamescope_start_time) is None:
            state = await self.display_get_state()
            return dict(state, restart_error='Switch to Gaming Mode to apply OLED session changes. Desktop session was not restarted.')
        return await self.display_restart_session()

    async def _unload(self):
        diagnostics.event('plugin', 'shutdown_started', source='backend')
        self._closing = True  # No new admissions; already admitted RPCs drain below.
        self._shutdown_requested.set()
        for name in tuple(self._starting):
            if self._still_starting(name):
                module_runtime.request_stop(name)  # before draining RPCs: a slow start applies nothing more
        self._withdraw_fan_lease()
        # Wake a helper download now; it is an update job, not a drained module RPC.
        helper_cancel = getattr(self._tdp, '_helper_cancel', None)  # Tests stub the component.
        if helper_cancel is not None:
            helper_cancel.set()
        deadline = self._stop_deadline = time.monotonic() + SHUTDOWN_BUDGET_S

        async def hardware():
            for name in ('_guard_task', '_updater_wait', '_band_restore', '_inputplumber'):
                task = getattr(self, name)
                setattr(self, name, None)
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass  # A failed monitor already logged its error; restoration below must still run.
            async with self._guard_lock:
                await self._drain_rpc_jobs()
                if self._modules_started:
                    await self._stop_modules()

        async def extras():
            if self._updates is not None:
                # Preserve the final publication barrier without blocking Decky's loop.
                await asyncio.to_thread(self._updates.close)
            async with self._desktop_lock:
                bridge, self._desktop = self._desktop, None
                if bridge is not None:
                    await bridge.close()
                if self._desktop_install is not None:
                    try:
                        await asyncio.to_thread(self._desktop_install.remove)
                    except Exception as exc:
                        diagnostics.event('desktop', 'session_cleanup_failed', level='warning', error=exc)
            try:
                await asyncio.to_thread(self._firmware.close)
            except Exception as exc:
                diagnostics.event('firmware', 'observer_cleanup_failed', level='warning', stage='shutdown', error=exc)
            if self._guard_scan is not None:
                await self._guard_scan
                self._guard_scan = None
            if self._update_jobs:
                await asyncio.gather(*tuple(self._update_jobs), return_exceptions=True)
            if self._diagnostics_read is not None:
                await asyncio.gather(self._diagnostics_read, return_exceptions=True)
                self._diagnostics_read = None

        async def cleanup():
            # Decky SIGKILLs 5 s after SIGTERM: hardware restoration goes first and
            # is never abandoned; the rest only gets what is left of the budget.
            try:
                await hardware()
            finally:
                work = asyncio.ensure_future(extras())
                try:
                    await asyncio.wait_for(asyncio.shield(work), max(deadline - time.monotonic(), 0.1))
                except asyncio.TimeoutError:
                    work.add_done_callback(lambda done: done.cancelled() or done.exception())
                    diagnostics.event('plugin', 'shutdown_extras_timeout', level='warning', source='backend')
        try:
            await module_runtime.complete(cleanup())
        except BaseException as exc:
            diagnostics.event('plugin', 'shutdown_waiter_cancelled' if isinstance(exc, asyncio.CancelledError) else 'shutdown_failed',
                              level='warning', error=exc, source='backend')
            raise
        decky.logger.info("[legiongo2companion] unloaded")
        diagnostics.event('plugin', 'shutdown_completed', source='backend')
        if not self._uninstalling:
            await asyncio.to_thread(diagnostics.close)

    def _withdraw_fan_lease(self):
        withdraw_lease = getattr(self._fan, '_withdraw_lease', None)
        if callable(withdraw_lease):
            withdraw_lease()

    async def _uninstall_after_start(self, names):
        """Uninstall a module whose start outlived the unload only after that start and its unload end."""
        async def uninstall(name):
            await asyncio.wait({self._late_stops[name]})
            await self._run_stage("_uninstall", (name,))
            await self._recover_pending_module(name)
        tasks = [module_runtime.background_task(uninstall(name)) for name in names]
        if tasks:
            pending = (await asyncio.wait(tasks, timeout=max(self._stop_deadline - time.monotonic(), 0)))[1]
            if pending:
                diagnostics.event('plugin', 'module_uninstall_timeout', level='warning',
                                  modules=','.join(name for name, task in zip(names, tasks) if task in pending))

    async def _keep_band_policy(self):
        """Decky also uninstalls before every update, and WiFi's uninstall puts the system's
        network files back. Keep its choice like a re-enable in progress: the next start restores it."""
        if not self._module_enabled('wifi') or self._module_state.get('wifi', {}).get('update'):
            return  # A turned-off module, or one still restoring an earlier update, already holds its choice.
        try:
            self._module_state['wifi'] = {'enabled': True, 'update': True,
                                          'resume': await asyncio.to_thread(capture_intent, 'wifi')}
            await asyncio.to_thread(self._save_modules)
        except Exception as exc:
            diagnostics.event('wifi', 'band_policy_not_kept', level='warning', error=exc)

    async def _uninstall(self):
        # A blocked instance that never owned hardware must not run restoration
        # hooks against the standalone plugin's live state.
        async def cleanup():
            if not self._closing:  # Decky already ran _unload(); a second pass would spend a fresh budget.
                await self._unload()
            if self._desktop_install is not None:
                try:
                    await asyncio.to_thread(self._desktop_install.remove, True)
                except Exception as exc:
                    diagnostics.event('desktop', 'shortcut_cleanup_failed', level='warning', error=exc)
            if self._ever_started and not (self._guard_blocked or self._guard_error):
                await self._keep_band_policy()
                # Start every independent restoration immediately. A pending
                # device/reconnect must not delay other modules before Decky's
                # process shutdown deadline.
                late = tuple(name for name, task in self._late_stops.items() if not task.done())
                names = tuple(name for name in MODULE_NAMES if name not in late)
                await asyncio.gather(
                    self._run_stage("_uninstall", names) if late else self._run_stage("_uninstall"),
                    *(self._recover_pending_module(name) for name in names),
                    self._uninstall_after_start(late),
                )
        self._uninstalling = True
        diagnostics.ensure_fallback()
        with diagnostics.source_context('backend'):
            diagnostics.event('plugin', 'uninstall_started')
            try:
                await module_runtime.complete(cleanup())
            except BaseException as exc:
                diagnostics.event('plugin', 'uninstall_not_confirmed', level='warning', error=exc)
                raise
            else:
                diagnostics.event('plugin', 'uninstall_completed')
            finally:
                self._uninstalling = False
                await asyncio.to_thread(diagnostics.close)
        decky.logger.info("[legiongo2companion] uninstalled")

    # Updates are explicit user actions, independent of hardware module toggles.

    async def updates_check(self):
        return await asyncio.to_thread(self._updates.check)

    async def updates_download(self, expected_version: str):
        return await asyncio.to_thread(self._updates.download, expected_version)

    async def save_device_report(self):
        """A read-only hardware and firmware report in the console user's Desktop folder (device_report)."""
        task = self._device_report
        if task is None or task.done():
            task = self._device_report = asyncio.create_task(self._save_device_report())
        return await asyncio.shield(task)

    async def _save_device_report(self):
        account = _session_account()
        if account is None:
            return {'success': False, 'error': 'The console user is unknown, so the report cannot be saved.'}
        collecting = self._report_collecting
        if collecting is None or collecting.done():  # Otherwise wait for the one still collecting.
            await self._ensure_device()
            try:
                caps = await asyncio.wait_for(self._tdp.get_caps(), 5.0)
            except Exception as exc:
                caps = type(exc).__name__
            recovery = self._inputplumber_recovery
            facts = {'version': _plugin_version(), 'device': dict(self._device),
                     'modules': await asyncio.to_thread(self._module_status),
                     'inputplumber_restarts': recovery.restarts if recovery else None, 'tdp_caps': caps,
                     'fan': {'supported': self._fan._supported, 'beta': self._fan._beta, 'error': self._fan._error,
                             'telemetry': self._fan._client.snapshot() if self._fan._client else None},
                     'log_dir': diagnostics.LOG_DIR, 'user': account.pw_name}
            collecting = self._report_collecting = asyncio.ensure_future(asyncio.to_thread(
                device_report.collect, facts, stopped=lambda: self._closing))
        try:
            text = await asyncio.wait_for(asyncio.shield(collecting), device_report.TOTAL_SECONDS + 5)
            path = await asyncio.to_thread(device_report.save, text, account.pw_dir, account.pw_uid, account.pw_gid)
        except asyncio.TimeoutError:
            return {'success': False, 'error': 'Collecting the report took too long. Try again.'}
        except (OSError, ValueError) as exc:
            diagnostics.event('plugin', 'device_report_failed', level='warning', error=exc)
            return {'success': False, 'error': str(exc) if isinstance(exc, ValueError)
                    else 'The report could not be saved in the Desktop folder.'}
        return {'success': True, 'path': path}

    async def firmware_get_status(self):
        return await asyncio.to_thread(self._firmware.status)

    async def firmware_check(self):
        return await asyncio.to_thread(self._firmware.check)

    async def firmware_download(self):
        return await asyncio.to_thread(self._firmware.download)

    async def firmware_install(self, token):
        await asyncio.to_thread(self._firmware.admit_install, token)
        # fwupd writes the same hidraw device: close controller writes and drain admitted ones first.
        module_runtime.pause_controller()
        try:
            await module_runtime.drain_controller()
            jobs = tuple(self._controller_rpc_jobs)
            if jobs:
                await asyncio.wait_for(asyncio.shield(asyncio.gather(*jobs, return_exceptions=True)), 5.0)
        except asyncio.TimeoutError as exc:
            module_runtime.resume_controller()
            raise RuntimeError('Controller settings are still being applied. Try again.') from exc
        except BaseException:
            module_runtime.resume_controller()
            raise
        loop = asyncio.get_running_loop()
        def done():
            try:
                loop.call_soon_threadsafe(self._firmware_finished)
            except RuntimeError:  # The backend loop already closed.
                module_runtime.resume_controller()
        return await asyncio.to_thread(self._firmware.install, token, done)

    def _firmware_finished(self):
        # Controllers can re-enumerate during an update; forget cached HID paths first.
        self._controller._status_cache = None
        vibration_backend._forget_device()
        module_runtime.resume_controller()

    # TDP / CPU -----------------------------------------------------------------

    async def is_ready(self):
        return await self._tdp.is_ready()

    async def get_version(self):
        await self._ensure_guard()
        return await asyncio.to_thread(self._guard_status)

    async def get_settings(self):
        return await self._tdp.get_settings()

    async def get_cpu_power_controls(self, app_id="", ac_profile=False):
        return await self._tdp.get_cpu_power_controls(app_id, ac_profile)

    async def set_cpu_boost(self, enabled, app_id="", ac_profile=False, expected_app_id=None, stored=False):
        return await self._tdp.set_cpu_boost(enabled, app_id, ac_profile, expected_app_id, stored)

    async def set_epp(self, value, app_id="", ac_profile=False, expected_app_id=None, stored=False):
        return await self._tdp.set_epp(value, app_id, ac_profile, expected_app_id, stored)

    async def get_power_source(self):
        return await self._tdp.get_power_source()

    async def retry_extras(self):
        return await self._tdp.retry_extras()

    async def get_extras_unlocked(self):
        return await self._tdp.get_extras_unlocked()

    async def set_extras_unlocked(self, enabled):
        return await self._tdp.set_extras_unlocked(enabled)

    async def set_advanced_tdp_control(self, enabled):
        return await self._tdp.set_advanced_tdp_control(enabled)

    async def set_separate_battery_presets(self, enabled):
        return await self._tdp.set_separate_battery_presets(enabled)

    async def get_game_profile(self, app_id):
        return await self._tdp.get_game_profile(app_id)

    async def list_game_profiles(self):
        return await self._tdp.list_game_profiles()

    async def set_game_ac_profile(
        self, app_id, spl, sppt, fppt, ac_separate, preset_name="", stored=False
    ):
        return await self._tdp.set_game_ac_profile(
            app_id, spl, sppt, fppt, ac_separate, preset_name, stored
        )

    async def delete_game_profile(self, app_id):
        return await self._tdp.delete_game_profile(app_id)

    async def set_game_profile_active(self, app_id, active):
        return await self._tdp.set_game_profile_active(app_id, active)

    async def set_plugin_enabled(self, enabled):
        return await self._tdp.set_plugin_enabled(enabled)

    async def get_caps(self):
        return await self._tdp.get_caps()

    async def set_panel_active(self, active):
        return await self._panel_active('_qam_panel_deadline', active)

    async def reapply(self):
        return await self._tdp.reapply()

    async def set_active_app(self, app_id):
        return await self._tdp.set_active_app(app_id)

    async def get_tdp_info(self):
        return await self._tdp.get_tdp_info()

    async def fan_get_state(self):
        return await self._fan.get_state()

    async def fan_set_enabled(self, enabled):
        return await self._fan.set_enabled(enabled)

    async def fan_save_curve(self, scope, curve, expected_revision):
        return await self._fan.save_curve(scope, curve, expected_revision)

    async def fan_save_unit(self, unit, expected_revision):
        return await self._fan.save_unit(unit, expected_revision)

    async def fan_save_assignment_mode(self, enabled, expected_revision):
        return await self._fan.save_assignment_mode(enabled, expected_revision)

    async def apply_tdp(
        self,
        spl,
        sppt,
        fppt,
        app_id="",
        preset_name="",
        expected_app_id=None,
        stored=False,
    ):
        return await self._tdp.apply_tdp(
            spl, sppt, fppt, app_id, preset_name, expected_app_id, stored
        )

    # Vibration -----------------------------------------------------------------

    async def vibe_is_ready(self):
        return await self._vibration.is_ready()

    async def vibe_get_capabilities(self):
        return await self._vibration.get_capabilities()

    async def vibe_get_settings(self):
        return await self._vibration.get_settings()

    async def vibe_set_active_app(self, app_id):
        return await self._vibration.set_active_app(app_id)

    async def vibe_set_intensity(self, level, expected_app_id=None, expected_profile_id=None):
        return await self._vibration.set_intensity(level, expected_app_id, expected_profile_id)

    async def vibe_set_rumble_mode(self, mode_idx, expected_app_id=None, expected_profile_id=None):
        return await self._vibration.set_rumble_mode(mode_idx, expected_app_id, expected_profile_id)

    async def vibe_set_touchpad_intensity(self, level, expected_app_id=None, expected_profile_id=None):
        return await self._vibration.set_touchpad_intensity(level, expected_app_id, expected_profile_id)

    async def vibe_set_touchpad_enabled(self, enabled, expected_app_id=None, expected_profile_id=None):
        return await self._vibration.set_touchpad_enabled(enabled, expected_app_id, expected_profile_id)

    async def vibe_reset_to_default(self, expected_app_id=None, expected_profile_id=None):
        return await self._vibration.reset_to_default(expected_app_id, expected_profile_id)

    async def vibe_reapply(self):
        return await self._vibration.reapply()

    async def vibe_set_profile_overwrite(self, app_id, enabled, name=""):
        return await self._vibration.set_profile_overwrite(app_id, enabled, name)

    async def vibe_get_driver_status(self):
        return await self._vibration.get_driver_status()

    async def vibe_test_vibration(self, duration_ms=500):
        return await self._vibration.test_vibration(duration_ms)

    # OLED display / brightness / EDID ------------------------------------------

    async def display_get_state(self):
        return await self._display.get_state()

    async def display_run_setup(self, mode="pq"):
        return await self._display.run_setup(mode)

    async def display_set_panel_mode(self, mode):
        return await self._display.set_panel_mode(mode)

    async def display_reset_settings(self):
        return await self._display.reset_settings()

    async def display_restart_session(self):
        return await self._display.restart_session()

    async def display_set_enabled(self, enabled):
        return await self._display.set_enabled(enabled)

    async def display_set_edid_fix(self, enabled):
        return await self._display.set_edid_fix(enabled)

    # WiFi 5/6 GHz preference --------------------------------------------------

    async def wifi_get_status(self):
        return await self._wifi.get_status()

    async def wifi_set_band_policy(self, mode, allow_unverified_scan=False):
        result = await self._wifi.set_band_policy(mode, allow_unverified_scan)
        if result.get('success') and self._module_state.get('wifi', {}).get('update'):
            # The user's new choice replaces the one an update is still restoring.
            self._module_state['wifi'] = {'enabled': True}
            await module_runtime.complete(asyncio.to_thread(self._save_modules))
        return result

    async def wifi_rescan_and_reconnect(self, target_band="high"):
        return await self._wifi.rescan_and_reconnect(target_band)

    # RGB rings / power-button light -------------------------------------------

    async def rgb_get_status(self):
        return await self._rgb.get_status()

    async def rgb_set_control_enabled(self, enabled):
        return await self._rgb.set_control_enabled(enabled)

    async def rgb_set_rings_enabled(self, enabled):
        return await self._rgb.set_rings_enabled(enabled)

    async def rgb_set_effect(self, effect):
        return await self._rgb.set_effect(effect)

    async def rgb_set_color(self, hue, saturation):
        return await self._rgb.set_color(hue, saturation)

    async def rgb_set_brightness(self, brightness):
        return await self._rgb.set_brightness(brightness)

    async def rgb_set_speed(self, speed):
        return await self._rgb.set_speed(speed)

    async def rgb_set_power_led(self, enabled):
        return await self._rgb.set_power_led(enabled)

    async def rgb_restore_original(self):
        return await self._rgb.restore_original()

    # Desktop / Page button remapping -----------------------------------------

    async def remap_get_status(self):
        return await self._remap.get_status()

    async def remap_set_enabled(self, enabled):
        return await self._remap.set_enabled(enabled)

    async def remap_set_action(self, button, action):
        return await self._remap.set_action(button, action)

    async def remap_restore_defaults(self):
        return await self._remap.restore_defaults()

    # Firmware battery protection --------------------------------------------

    async def battery_get_status(self):
        return await self._battery.get_status()

    async def battery_get_controller_levels(self):
        if not self._applies('controller'):  # Go S, a Go on the older controller firmware: no controller code runs.
            return {'available': False, 'left': None, 'right': None, 'connection_left': None, 'connection_right': None,
                    'reason': 'This console has no supported controllers.'}
        return await self._controller.get_battery_levels()

    async def battery_set_enabled(self, enabled):
        return await self._battery.set_enabled(enabled)

    async def battery_release_control(self):
        return await self._battery.release_control()

    # Gyro selection and bounded, passive controller diagnostics --------------

    async def controller_get_status(self):
        return await self._controller.get_status()

    async def controller_set_gyro_source(self, source):
        return await self._controller.set_gyro_source(source)

    async def controller_release_control(self):
        return await self._controller.release_control()

    async def controller_start_diagnostics(self):
        return await self._controller.start_diagnostics()

    async def controller_get_diagnostics(self, token):
        return await self._controller.get_diagnostics(token)

    async def controller_stop_diagnostics(self, token):
        return await self._controller.stop_diagnostics(token)

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Decky entry point for the Legion Go 2 all-in-one plugin.

The hardware implementations remain isolated in their original backends.  This
module owns lifecycle ordering and a single, collision-free RPC surface.
"""

from __future__ import annotations

import asyncio
import json
import functools
import inspect
import os
import sys

import decky

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import battery_backend
import controller_backend
import display_backend
import remap_backend
import rgb_backend
import tdp_backend
import vibration_backend
import wifi_backend
import module_runtime
from safe_settings import atomic_write_json, load_json_object, SettingsManager
from module_control import withdraw, capture_intent, restore_intent

MODULE_NAMES = ('tdp', 'vibration', 'display', 'wifi', 'rgb', 'remap', 'battery', 'controller')

def _rpc_module(name):
    for prefix, module in (('vibe_', 'vibration'), ('display_', 'display'), ('wifi_', 'wifi'),
                           ('rgb_', 'rgb'), ('remap_', 'remap'), ('battery_', 'battery'), ('controller_', 'controller')):
        if name.startswith(prefix):
            return module
    return None if name.startswith('modules_') else 'tdp'
from conflict_guard import installed_conflicts, CHECK_INTERVAL_S

LEGACY_SETTINGS = {
    "tdp_settings.json": ("LeGoTDP",),
    "vibration_settings.json": ("LeGo-Vibe-Control", "LeGo Vibe Control"),
    "display_settings.json": ("LeGo2BrightnessFix", "LeGo2 Brightness Fix"),
    "wifi_settings.json": ("WiFi Optimizer Go 2", "WifiOptimizerGo2"),
}



def _plugin_version() -> str:
    try:
        with open(os.path.join(PLUGIN_DIR, "plugin.json"), encoding="utf-8") as handle:
            value = json.load(handle).get("version")
        return str(value or "0.0.0")
    except (OSError, ValueError, TypeError):
        return "0.0.0"


def _installed_standalone_plugins() -> list[str]:
    return installed_conflicts(PLUGIN_DIR)


def _migrate_legacy_settings() -> None:
    """Import missing values from each standalone plugin's settings.

    The source remains untouched. Existing combined values always win, while
    missing top-level values are recovered from the standalone settings. This
    also repairs an interrupted first migration without rolling newer data back.
    """
    # Keep the final directory component unresolved. atomic_write_json()
    # deliberately rejects a plugin settings directory that was replaced with
    # a symlink; realpath() here would erase that security boundary before the
    # checked writer ever saw it.
    current_dir = os.path.abspath(decky.DECKY_PLUGIN_SETTINGS_DIR)
    settings_root = os.path.dirname(current_dir)
    os.makedirs(current_dir, exist_ok=True)

    for destination_name, legacy_dirs in LEGACY_SETTINGS.items():
        destination = os.path.join(current_dir, destination_name)
        source = next((candidate for candidate in (
            os.path.join(settings_root, directory, "settings.json")
            for directory in legacy_dirs
        ) if os.path.lexists(candidate)), None)
        if not source:
            continue
        try:
            legacy_payload = load_json_object(source)
            current_payload = load_json_object(destination, missing_ok=True) or {}
            payload = dict(legacy_payload)
            payload.update(current_payload)
            if payload == current_payload:
                continue
            atomic_write_json(destination, payload)
            decky.logger.info(
                f"[legiongo2companion] imported missing settings from {source}"
            )
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            decky.logger.warning(
                f"[legiongo2companion] could not import {source}: {exc}"
            )


def _reload_component_settings() -> None:
    """Refresh module-level SettingsManager instances after disk migration."""
    for module in (
        tdp_backend, vibration_backend, display_backend, wifi_backend, rgb_backend,
        remap_backend, battery_backend, controller_backend,
    ):
        manager = getattr(module, "settings", None)
        reader = getattr(manager, "read", None)
        if callable(reader):
            try:
                reader()
            except Exception as exc:
                decky.logger.error(
                    f"[legiongo2companion] settings reload failed: {exc}"
                )


def _guard_component_calls(cls):
    """Default-deny every public component RPC, including future additions."""
    for name, method in tuple(vars(cls).items()):
        if name.startswith("_") or name == "get_version" or not inspect.iscoroutinefunction(method):
            continue
        def wrap(fn):
            @functools.wraps(fn)
            async def guarded(self, *args, **kwargs):
                await self._check_guard()
                async with self._guard_lock:
                    if self._guard_blocked or self._closing:
                        raise RuntimeError(self._guard_message())
                    module = _rpc_module(fn.__name__)
                    if module and not self._module_enabled(module):
                        raise RuntimeError('This module is disabled. Enable it in Manage Modules.')
                    # A cancelled RPC must not release the gate while its backend
                    # operation is still running (notably the Wi-Fi worker).
                    return await module_runtime.complete(fn(self, *args, **kwargs))
            return guarded
        setattr(cls, name, wrap(method))
    return cls


@_guard_component_calls
class Plugin:
    """Compose the audited Legion Go 2 controls into one Decky plugin."""

    def __init__(self):
        self._guard_lock = asyncio.Lock()
        self._guard_task = None
        self._guard_blocked = False
        self._guard_conflicts = []
        self._guard_error = ""
        self._modules_started = False
        self._ever_started = False
        self._closing = False
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
        self._tdp = tdp_backend.Plugin()
        self._vibration = vibration_backend.Plugin()
        self._display = display_backend.Plugin()
        self._wifi = wifi_backend.Plugin()
        self._rgb = rgb_backend.Plugin()
        self._remap = remap_backend.Plugin()
        self._battery = battery_backend.Plugin()
        self._controller = controller_backend.Plugin()

    def _module_enabled(self, name):
        value = self._module_state.get(name, {})
        return isinstance(value, dict) and value.get('enabled', True) is True and not value.get('pending', False)

    def _save_modules(self):
        self._module_store.setSetting('modules', self._module_state)
        self._module_store.commit()

    def _module_status(self):
        result = {name: {'enabled': self._module_enabled(name),
                       'pending': bool(self._module_state.get(name, {}).get('pending')),
                       'error': self._module_state.get(name, {}).get('error', ''),
                       'note': self._module_state.get(name, {}).get('note', '')}
                for name in MODULE_NAMES}
        display = self._module_state.get('display', {})
        if display.get('session') is not None:
            current = display_backend._gamescope_start_time()
            if current is not None and current != display['session']:
                result['display']['note'] = ''
        return result

    async def _publish_modules(self):
        try:
            await decky.emit('companion_guard', self._guard_status())
        except Exception as exc:
            decky.logger.warning(f'Module status notification failed: {exc}')

    async def modules_get_status(self):
        return self._module_status()

    async def modules_restart_session(self):
        result = await self._display.restart_session()
        return {'success': not bool(result.get('restart_error')), 'error': result.get('restart_error', '')}

    async def modules_set_enabled(self, name, enabled):
        if name not in MODULE_NAMES or type(enabled) is not bool:
            raise ValueError('Invalid module or enabled value.')
        previous = dict(self._module_state)
        component = getattr(self, '_' + name)
        resume = self._module_state.get(name, {}).get('resume', {})
        if enabled:
            if self._module_state.get(name, {}).get('pending'):
                raise RuntimeError('Retry disabling this module to finish restoration first.')
            if self._module_enabled(name):
                return self._module_status()
            # Startup is gated until the durable intent is written; failure stays visible.
            self._module_state[name] = {'enabled': True, 'resume': resume}
            try:
                self._save_modules()
            except Exception:
                self._module_state = previous
                raise
            try:
                await component._main()
                await restore_intent(name, component, resume)
                self._module_state[name] = {'enabled': True}
                self._save_modules()
            except Exception as exc:
                self._module_state[name] = {'enabled': False, 'pending': True, 'resume': resume, 'error': str(exc)}
                try:
                    self._save_modules()
                finally:
                    try:
                        await component._unload()
                    finally:
                        await self._publish_modules()
                raise
        else:
            if not self._module_enabled(name) and not self._module_state.get(name, {}).get('pending'):
                return self._module_status()
            # Persist the gate before cleanup. An interrupted withdrawal never starts
            # the module on next boot; it retries cleanup with the saved ownership.
            if not self._module_state.get(name, {}).get('pending'):
                resume = await asyncio.to_thread(capture_intent, name)
            self._module_state[name] = {'enabled': False, 'pending': True, 'resume': resume}
            try:
                self._save_modules()
            except Exception:
                self._module_state = previous
                raise
            await self._publish_modules()
            try:
                note = await withdraw(name, component)
                self._module_state[name] = {'enabled': False, 'note': note, 'resume': resume}
                if name == 'display':
                    self._module_state[name]['session'] = display_backend._gamescope_start_time()
                self._save_modules()
            except Exception as exc:
                self._module_state[name] = {'enabled': False, 'pending': True, 'resume': resume, 'error': str(exc)}
                self._save_modules()
        await self._publish_modules()
        return self._module_status()

    async def _run_stage(self, stage: str) -> None:
        components = (
            ("tdp", self._tdp),
            ("vibration", self._vibration),
            ("display", self._display),
            ("wifi", self._wifi),
            ("rgb", self._rgb),
            ("remap", self._remap),
            ("battery", self._battery),
            ("controller", self._controller),
        )
        runnable = tuple(
            (name, component)
            for name, component in components
            if (self._module_enabled(name) or (stage == '_unload' and self._module_state.get(name, {}).get('pending')))
            and callable(getattr(component, stage, None))
        )
        results = await asyncio.gather(
            *(getattr(component, stage)() for _, component in runnable),
            return_exceptions=True,
        )
        for (name, _), result in zip(runnable, results):
            if isinstance(result, BaseException):
                decky.logger.error(
                    f"[legiongo2companion] {name} {stage} failed: {result}"
                )

    def _guard_message(self):
        if self._guard_error:
            return "Companion is paused: installed plugins could not be verified. " + self._guard_error
        if self._guard_conflicts:
            return ("All Companion modules are paused. Uninstall "
                    + ", ".join(self._guard_conflicts)
                    + " in Decky Settings, keep their settings, then restart Decky or the console.")
        return "Companion is paused. Restart Decky or the console to resume safely."

    def _guard_status(self):
        return {"version": _plugin_version(),
                "modules": self._module_status(),
                "standalone_plugins": list(self._guard_conflicts),
                "blocked": self._guard_blocked,
                "restart_required": self._guard_blocked and not self._guard_conflicts and not self._guard_error,
                "guard_error": self._guard_error,
                "message": self._guard_message() if self._guard_blocked else ""}

    async def _inspect_guard(self):
        before = self._guard_status()
        try:
            self._guard_conflicts = await asyncio.to_thread(_installed_standalone_plugins)
            self._guard_error = ""
        except (OSError, ValueError, TypeError) as exc:
            self._guard_error = str(exc)
        if self._guard_conflicts or self._guard_error:
            # Latched until a fresh process starts. Reusing partially unloaded
            # module globals would risk duplicate workers or lost restoration state.
            self._guard_blocked = True
        if before != self._guard_status():
            decky.logger.warning("[legiongo2companion] " + self._guard_message())
            try:
                await decky.emit("companion_guard", self._guard_status())
            except Exception as exc:
                decky.logger.warning(f"[legiongo2companion] guard event unavailable: {exc}")

    async def _check_guard(self):
        await self._inspect_guard()
        if self._guard_blocked and self._modules_started:
            async with self._guard_lock:
                if self._modules_started:
                    await self._stop_modules()
                    decky.logger.warning("[legiongo2companion] all module workers stopped by conflict guard")

    async def _stop_modules(self):
        async def stop():
            await self._run_stage("_unload")
            if self._guard_blocked:
                # Cancelling a monitor does not stop its thread. A live conflict
                # must finish those writes before advertising a paused plugin.
                await asyncio.gather(*(module_runtime.drain(name) for name in MODULE_NAMES))
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
        if not entry.get('pending'):
            return
        resume = entry.get('resume', {})
        try:
            note = await withdraw(name, getattr(self, '_' + name))
            self._module_state[name] = {'enabled': False, 'note': note, 'resume': resume}
            if name == 'display':
                self._module_state[name]['session'] = display_backend._gamescope_start_time()
            self._save_modules()
        except Exception as exc:
            self._module_state[name] = {'enabled': False, 'pending': True, 'resume': resume, 'error': str(exc)}
            decky.logger.error(f'Module {name} recovery failed: {exc}')
            try:
                self._save_modules()
            except Exception as save_error:
                decky.logger.error(f'Module {name} recovery could not be saved: {save_error}')

    async def _migration(self):
        await self._check_guard()
        if self._guard_blocked:
            return
        async with self._guard_lock:
            await asyncio.to_thread(_migrate_legacy_settings)
            await asyncio.to_thread(_reload_component_settings)
            await self._run_stage("_migration")

    async def _main(self):
        decky.logger.info(f"[legiongo2companion] startup v{_plugin_version()}")
        if self._guard_task is None:
            self._guard_task = asyncio.create_task(self._watch_conflicts())
        await self._check_guard()
        if self._guard_blocked:
            decky.logger.warning("[legiongo2companion] " + self._guard_message())
            return
        async with self._guard_lock:
            if self._modules_started or self._guard_blocked or self._closing:
                return
            await asyncio.to_thread(_migrate_legacy_settings)
            await asyncio.to_thread(_reload_component_settings)
            await self._inspect_guard()
            if self._guard_blocked:
                return
            self._modules_started = self._ever_started = True
            for name in MODULE_NAMES:
                await self._recover_pending_module(name)
            await self._run_stage("_main")
            for name in MODULE_NAMES:
                entry = self._module_state.get(name, {})
                if self._module_enabled(name) and entry.get('resume'):
                    try:
                        await restore_intent(name, getattr(self, '_' + name), entry['resume'])
                        self._module_state[name] = {'enabled': True}
                        self._save_modules()
                    except Exception as exc:
                        self._module_state[name] = {'enabled': False, 'pending': True,
                                                    'resume': entry['resume'], 'error': str(exc)}
                        try:
                            self._save_modules()
                        except Exception as save_error:
                            decky.logger.error(f'Module {name} recovery could not be saved: {save_error}')
                        try:
                            await getattr(self, '_' + name)._unload()
                        except Exception as stop_error:
                            decky.logger.error(f'Module {name} recovery could not stop: {stop_error}')
        await self._check_guard()

    async def _unload(self):
        self._closing = True
        task, self._guard_task = self._guard_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._guard_lock:
            if self._modules_started:
                await self._stop_modules()
        decky.logger.info("[legiongo2companion] unloaded")

    async def _uninstall(self):
        # A blocked instance that never owned hardware must not run restoration
        # hooks against the standalone plugin's live state.
        async def cleanup():
            await self._unload()
            if self._ever_started and not self._guard_blocked:
                # Start every independent restoration immediately. A pending
                # device/reconnect must not delay other modules before Decky's
                # process shutdown deadline.
                await asyncio.gather(
                    self._run_stage("_uninstall"),
                    *(self._recover_pending_module(name) for name in MODULE_NAMES),
                )
        await module_runtime.complete(cleanup())
        decky.logger.info("[legiongo2companion] uninstalled")

    # TDP / CPU -----------------------------------------------------------------

    async def is_ready(self):
        return await self._tdp.is_ready()

    async def get_version(self):
        await self._check_guard()
        return self._guard_status()

    async def get_settings(self):
        return await self._tdp.get_settings()

    async def get_cpu_power_controls(self):
        return await self._tdp.get_cpu_power_controls()

    async def set_cpu_boost(self, enabled):
        return await self._tdp.set_cpu_boost(enabled)

    async def set_epp(self, value):
        return await self._tdp.set_epp(value)

    async def get_power_source(self):
        return await self._tdp.get_power_source()

    async def retry_extras(self):
        return await self._tdp.retry_extras()

    async def get_extras_unlocked(self):
        return await self._tdp.get_extras_unlocked()

    async def set_extras_unlocked(self, enabled):
        return await self._tdp.set_extras_unlocked(enabled)

    async def get_game_profile(self, app_id):
        return await self._tdp.get_game_profile(app_id)

    async def set_game_ac_profile(
        self, app_id, spl, sppt, fppt, ac_separate, preset_name=""
    ):
        return await self._tdp.set_game_ac_profile(
            app_id, spl, sppt, fppt, ac_separate, preset_name
        )

    async def delete_game_profile(self, app_id):
        return await self._tdp.delete_game_profile(app_id)

    async def set_plugin_enabled(self, enabled):
        return await self._tdp.set_plugin_enabled(enabled)

    async def get_caps(self):
        return await self._tdp.get_caps()

    async def restore_defaults(self):
        return await self._tdp.restore_defaults()

    async def set_panel_active(self, active):
        return await self._tdp.set_panel_active(active)

    async def reapply(self):
        return await self._tdp.reapply()

    async def set_active_app(self, app_id):
        return await self._tdp.set_active_app(app_id)

    async def get_tdp_info(self):
        return await self._tdp.get_tdp_info()

    async def apply_tdp(
        self,
        spl,
        sppt,
        fppt,
        app_id="",
        preset_name="",
        expected_app_id=None,
    ):
        return await self._tdp.apply_tdp(
            spl, sppt, fppt, app_id, preset_name, expected_app_id
        )

    # Vibration -----------------------------------------------------------------

    async def vibe_is_ready(self):
        return await self._vibration.is_ready()

    async def vibe_get_version(self):
        return {"version": _plugin_version()}

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

    async def vibe_reset_to_default(self):
        return await self._vibration.reset_to_default()

    async def vibe_reapply(self):
        return await self._vibration.reapply()

    async def vibe_get_game_profiles(self):
        return await self._vibration.get_game_profiles()

    async def vibe_set_profile_overwrite(self, app_id, enabled, name=""):
        return await self._vibration.set_profile_overwrite(app_id, enabled, name)

    async def vibe_delete_game_profile(self, app_id):
        return await self._vibration.delete_game_profile(app_id)

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

    async def display_restart_session(self):
        return await self._display.restart_session()

    async def display_get_version(self):
        return {"version": _plugin_version()}

    async def display_set_enabled(self, enabled):
        return await self._display.set_enabled(enabled)

    async def display_set_edid_fix(self, enabled):
        return await self._display.set_edid_fix(enabled)

    # WiFi 5/6 GHz preference --------------------------------------------------

    async def wifi_get_status(self):
        return await self._wifi.get_status()

    async def wifi_set_band_preference(self, enabled):
        return await self._wifi.set_band_preference(enabled)

    async def wifi_rescan_and_reconnect(self):
        return await self._wifi.rescan_and_reconnect()

    async def wifi_reset_settings(self):
        return await self._wifi.reset_settings()

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

    async def rgb_reapply(self):
        return await self._rgb.reapply()

    # Desktop / Page button remapping -----------------------------------------

    async def remap_get_status(self):
        return await self._remap.get_status()

    async def remap_set_enabled(self, enabled):
        return await self._remap.set_enabled(enabled)

    async def remap_set_action(self, button, action):
        return await self._remap.set_action(button, action)

    async def remap_restore_defaults(self):
        return await self._remap.restore_defaults()

    async def remap_reapply(self):
        return await self._remap.reapply()

    # Firmware battery protection --------------------------------------------

    async def battery_get_status(self):
        return await self._battery.get_status()

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

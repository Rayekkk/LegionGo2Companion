# SPDX-License-Identifier: BSD-3-Clause
"""Explicit module withdrawal; never use uninstall hooks that erase settings."""
import asyncio
import os
import module_runtime

def capture_intent(name):
    """Keep preferences, never reuse a hardware ownership snapshot after re-enable."""
    if name == 'rgb':
        import rgb_backend as b
        s = b._load_state()
        return {k: s[k] for k in ('control_enabled', 'power_led_managed', 'power_led_enabled')}
    if name == 'battery':
        import battery_backend as b
        s = b.settings.getSetting('state', {})
        return {'managed': s.get('managed', False), 'enabled': s.get('requested_enabled')}
    if name == 'remap':
        import remap_backend as b
        return {'enabled': b._load_state()['enabled']}
    if name == 'controller':
        import controller_backend as b
        return {'source': b._state()['gyro_source']}
    if name == 'wifi':
        import wifi_backend as b
        s = b._load_settings()
        return {'enabled': s.get('band_preference_enabled') is True,
                'power_save_disabled': s.get('power_save_disabled') is True}
    if name == 'display':
        import display_backend as b
        return {'panel_mode': b.settings.getSetting('panel_mode', None)}
    return {}

async def restore_intent(name, component, intent):
    check = module_runtime.require_success
    if name == 'rgb':
        if intent.get('control_enabled') is True:
            check(await component.set_control_enabled(True))
        if intent.get('power_led_managed') is True and type(intent.get('power_led_enabled')) is bool:
            check(await component.set_power_led(intent['power_led_enabled']))
    elif name == 'battery' and intent.get('managed') is True and type(intent.get('enabled')) is bool:
        check(await component.set_enabled(intent['enabled']))
    elif name == 'remap' and intent.get('enabled') is True:
        check(await component.set_enabled(True))
    elif name == 'controller' and intent.get('source') in ('left', 'right', 'combined'):
        await component.set_gyro_source(intent['source'])
    elif name == 'wifi':
        if intent.get('enabled') is True:
            check(await component.set_band_preference(True))
        if intent.get('power_save_disabled') is True:
            check(await component.set_power_save(True))
    elif name == 'display' and intent.get('panel_mode') in ('gamma22', 'pq', 'hybrid'):
        result = await component.set_panel_mode(intent['panel_mode'])
        if result.get('setup_error') or not result.get('setup_done'):
            raise RuntimeError(result.get('setup_error') or result.get('setup_note') or 'Display script installation failed.')

async def withdraw(name, component):
    await component._unload()
    await module_runtime.drain(name)
    check = module_runtime.require_success
    note = ''
    if name == 'tdp':
        import tdp_backend as backend
        def restore():
            with backend._mutation_lock:
                state = backend._load_settings()
                if state.get('enabled'):
                    check(backend._restore_defaults_locked())
                # Older versions did not retain original CPU policy values.
                # Explicit withdrawal returns managed controls to standard policy.
                if state.get('cpu_boost_enabled') is not None:
                    check(backend._apply_cpu_boost_hardware(True))
                if state.get('epp') is not None:
                    check(backend._apply_epp_hardware('balance_performance'))
        await asyncio.to_thread(restore)
    elif name == 'vibration':
        import vibration_backend as backend
        if not await backend._offload(backend._apply_settings, dict(backend.DEFAULT_PROFILE), None, True):
            raise RuntimeError('The controller did not confirm default vibration settings.')
    elif name == 'rgb':
        check(await component.restore_original())
    elif name == 'remap':
        check(await component.set_enabled(False))
    elif name == 'controller':
        await asyncio.to_thread(component._release, persist=True)
    elif name == 'battery':
        check(await component.release_control())
    elif name == 'wifi':
        import wifi_backend as backend
        check(await component.set_band_policy(backend.BAND_POLICY_OFF))
        check(await component.set_power_save(False))
        check(await asyncio.to_thread(component._restore_legacy_bssid_lock))
        check(await asyncio.to_thread(component._remove_dispatcher))
        try:
            os.remove(backend.ENFORCED_FILE)
        except FileNotFoundError:
            pass
    elif name == 'display':
        import display_backend as backend
        await component._unload(uninstalling=True)
        await module_runtime.drain(name)
        # Lifecycle unload logs failures; explicit disabling must surface them.
        for restore in (component._release, component._edid_restore):
            if not await asyncio.to_thread(restore):
                raise RuntimeError('Display restoration could not be confirmed. Retry cleanup in Gaming Mode.')
        props = await asyncio.to_thread(component._session_props, [backend.ATOM_IS_EXTERNAL])
        if not await asyncio.to_thread(backend._write_atom_int, backend.ATOM_FORCE_HDR_SUPPORT, 0):
            raise RuntimeError('Could not release display HDR support. Retry cleanup in Gaming Mode.')
        if backend._as_int(props.get(backend.ATOM_IS_EXTERNAL)) == 0:
            if not await asyncio.to_thread(backend._write_atom_int, backend.ATOM_HDR_ENABLED, 0):
                raise RuntimeError('Could not restore the internal display mode. Retry cleanup in Gaming Mode.')
        note = await asyncio.to_thread(backend._uninstall_script)
        if note.startswith('could not'):
            raise RuntimeError(note)
        note = 'Restart Gaming Mode to fully unload the display script already loaded by gamescope.'
    else:
        raise ValueError('Unknown module.')
    return note

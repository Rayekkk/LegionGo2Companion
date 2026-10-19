# SPDX-License-Identifier: BSD-3-Clause
"""Explicit module withdrawal; never use uninstall hooks that erase settings."""
import asyncio
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
        return {'band_policy': s.get('band_policy', b.BAND_POLICY_OFF)}
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
        import wifi_backend as backend
        policy = intent.get('band_policy')
        if isinstance(policy, str) and policy in backend.BAND_POLICIES - {backend.BAND_POLICY_OFF}:
            check(await component.set_band_policy(policy))
    elif name == 'display' and intent.get('panel_mode') in ('gamma22', 'pq', 'hybrid'):
        result = await component.set_panel_mode(intent['panel_mode'])
        # Native retirement only disables the legacy display controls. Keep
        # the module running so its independent EDID correction can continue.
        if result.get('native_support_detected') or result.get('native_cleanup_pending'):
            return
        if result.get('setup_error') or not result.get('setup_done'):
            raise RuntimeError(result.get('setup_error') or result.get('setup_note') or 'Display script installation failed.')

async def withdraw(name, component):
    # Recovery of a pending withdrawal runs before component._main(). Probe
    # first so a system update cannot route that recovery through legacy writes.
    if name == 'display':
        await component.cleanup_native_display()
    try:
        # Display ends in its uninstall state; a plain unload first would restore
        # the Hybrid PQ baseline only to switch back to gamma 2.2 (two modesets).
        await (component._unload(uninstalling=True) if name == 'display' else component._unload())
    finally:
        await module_runtime.drain(name)
    check = module_runtime.require_success
    note = ''
    if name == 'tdp':
        import tdp_backend as backend
        def restore():
            with backend._mutation_lock:
                check(backend._release_controls_locked())
        await asyncio.to_thread(restore)
    elif name == 'fan':
        # Pending withdrawal is retried before _main has loaded durable curves.
        # Refresh them before clearing intent; never resume a custom override.
        await module_runtime.offload('fan', component._load)
        check(await component.set_enabled(False))
    elif name == 'vibration':
        import vibration_backend as backend
        if not await backend._offload(backend._restore_baseline):
            raise RuntimeError('The controller did not confirm the restored vibration settings.')
    elif name == 'rgb':
        check(await component.restore_original())
    elif name == 'remap':
        check(await component.set_enabled(False))
    elif name == 'controller':
        await asyncio.to_thread(component._release, persist=True)
    elif name == 'battery':
        check(await component.release_control())
    elif name == 'wifi':
        for result in (await component._release_all()).values():
            check(result)
    elif name == 'display':
        import display_backend as backend
        native_active = getattr(component, 'native_display_active', lambda: False)

        async def finish_native():
            result = await component.cleanup_native_display()
            if result.get('native_cleanup_pending') or result.get('native_cleanup_error'):
                raise RuntimeError(result.get('native_cleanup_error') or
                                   'Native display cleanup is still pending. Retry cleanup in Gaming Mode.')
            if not await asyncio.to_thread(component._edid_restore):
                raise RuntimeError('Display restoration could not be confirmed. Retry cleanup in Gaming Mode.')
            return ('Restart Gaming Mode to unload the previous display script.'
                    if result.get('native_restart_pending') else '')

        if native_active():
            return await finish_native()

        def finish_legacy():
            # A concurrent state read can detect a package update while unload
            # awaits. Serialize the final ownership check with every mutation.
            with backend.Plugin._runtime_lock:
                if native_active():
                    return None
                # Lifecycle unload logs failures; explicit disabling surfaces them.
                for restore in (component._release, component._edid_restore):
                    if not restore():
                        raise RuntimeError('Display restoration could not be confirmed. Retry cleanup in Gaming Mode.')
                # A stopped property watcher can still hold the pre-docking
                # snapshot. Only the bound, live compositor may receive these
                # final writes; pending recovery can run before _main binds one.
                if component._ownership_session_current() is True:
                    props = component._session_props([backend.ATOM_IS_EXTERNAL], fresh=True)
                    if component._ownership_session_current() is True:
                        external = backend._as_int(props.get(backend.ATOM_IS_EXTERNAL))
                        if external not in (0, 1):
                            raise RuntimeError('Could not verify the active display. Retry cleanup in Gaming Mode.')
                        if not backend._write_atom_int(backend.ATOM_FORCE_HDR_SUPPORT, 0):
                            raise RuntimeError('Could not release display HDR support. Retry cleanup in Gaming Mode.')
                        if external == 0:
                            if not backend._write_atom_int(backend.ATOM_HDR_ENABLED, 0):
                                raise RuntimeError('Could not restore the internal display mode. Retry cleanup in Gaming Mode.')
                result = backend._uninstall_script()
                if result.startswith('could not'):
                    raise RuntimeError(result)
                return 'Restart Gaming Mode to fully unload the display script already loaded by gamescope.'

        note = await asyncio.to_thread(finish_legacy)
        if note is None:
            return await finish_native()
    else:
        raise ValueError('Unknown module.')
    return note

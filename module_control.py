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
        return {'source': b._state()['gyro_source'], 'sleep': b._sleep_minutes()}
    if name == 'wifi':
        import wifi_backend as b
        s = b._load_settings()
        return {'band_policy': s.get('band_policy', b.BAND_POLICY_OFF)}
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
    elif name == 'controller':
        import controller_backend as backend
        if intent.get('source') in ('left', 'right', 'combined'):
            await component.set_gyro_source(intent['source'])
        if type(intent.get('sleep')) is int and intent['sleep'] in backend.SLEEP_MINUTES:
            try:
                await component.set_sleep_time(intent['sleep'])
            except backend.SleepPending:
                pass  # saved: the module retries once the pads respond
    elif name == 'wifi':
        import wifi_backend as backend
        policy = intent.get('band_policy')
        if isinstance(policy, str) and policy in backend.BAND_POLICIES - {backend.BAND_POLICY_OFF}:
            check(await component.set_band_policy(policy))

async def withdraw(name, component):
    try:
        await component._unload()
    finally:
        await module_runtime.drain(name)
    check = module_runtime.require_success
    if name == 'tdp':
        import tdp_backend as backend
        def restore():
            with backend._mutation_lock:
                check(backend._answer(backend._release_controls_locked()))
        await asyncio.to_thread(restore)
    elif name == 'fan':
        # Pending withdrawal is retried before _main has loaded durable curves.
        # Refresh them before clearing intent; never resume a custom override.
        await module_runtime.offload('fan', component._load)
        await component.set_enabled(False)  # its failures raise; the state it answers carries no verdict
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
        await asyncio.to_thread(component._release_sleep)
    elif name == 'battery':
        check(await component.release_control())
    elif name == 'wifi':
        for result in (await component._release_all()).values():
            check(result)
    elif name == 'display':
        # Unload only logs a failed restore; explicit disabling surfaces it.
        if not await asyncio.to_thread(component._edid_restore):
            raise RuntimeError('Display restoration could not be confirmed. Retry cleanup in Gaming Mode.')
    else:
        raise ValueError('Unknown module.')

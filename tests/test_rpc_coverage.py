# SPDX-License-Identifier: BSD-3-Clause
"""Public RPC methods driven through the Plugin class against fake sysfs / D-Bus layers.

One behaviour each: bad input is refused without touching hardware, good input reaches the
files the real kernel interface would expose, and a failing step leaves the previous state.
"""
import asyncio
from contextlib import ExitStack
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support  # noqa: F401 - installs the fake decky module
import controller_backend
import remap_backend
import rgb_backend
import vibration_backend as vibe
from safe_settings import AtomicSettingsManager
import test_tdp_core as tdp_core  # module import only: its TestCase classes must not be re-collected here


def run(coroutine):
    return asyncio.run(coroutine)


class MemorySettings:
    def __init__(self, state=None, default=None):
        self.path = '/nonexistent/settings.json'
        self.recovery_error = ''
        self.data = {'state': copy.deepcopy(state or default)}

    def read(self): pass
    def commit(self): pass
    def getSetting(self, key, default=None): return copy.deepcopy(self.data.get(key, default))
    def setSetting(self, key, value): self.data[key] = copy.deepcopy(value)


# -- RGB ---------------------------------------------------------------------------------

LED = {
    'enabled': 'false', 'enabled_index': 'true false', 'profile': '3', 'profile_range': '1-3',
    'mode': 'custom', 'mode_index': 'dynamic custom', 'effect': 'monocolor',
    'effect_index': 'monocolor breathe chroma rainbow', 'brightness': '20', 'max_brightness': '100',
    'multi_intensity': '0 100 100', 'multi_index': 'red green blue', 'multi_max_intensity': '100 100 100',
    'speed': '50', 'speed_range': '0-100',
}
LED_OFF_BIT = 0x40  # power LED logic is inverted: bit 6 set means off


class Register:
    def __init__(self, value):
        self.value = value

    def __getitem__(self, _index): return self.value

    def __setitem__(self, _index, value): self.value = value


class RgbRpcTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.led = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in LED.items():
            (self.led / name).write_text(value + '\n', encoding='ascii')
        self.memory = MemorySettings(default=rgb_backend.DEFAULT_STATE)
        self.register = Register(0xA5 | LED_OFF_BIT)
        for target, value in {
            'settings': self.memory, '_last_error': '', '_power_capability_cache': None,
        }.items():
            stack.enter_context(patch.object(rgb_backend, target, value))
        # Gate predicates and the /dev/mem boundary are faked; every write below is the real code.
        stack.enter_context(patch.object(rgb_backend, '_rgb_capability', return_value=(str(self.led), '')))
        stack.enter_context(patch.object(rgb_backend, '_power_capability', return_value=(True, '')))
        stack.enter_context(patch.object(rgb_backend, '_with_power_lock', side_effect=lambda operation: operation()))
        stack.enter_context(patch.object(rgb_backend, '_map_power_register',
                                         side_effect=lambda _write, operation: operation(self.register, 0)))
        self.plugin = rgb_backend.Plugin()

    def attr(self, name):
        return (self.led / name).read_text(encoding='ascii').strip()

    def saved(self, key):
        return self.memory.data['state'][key]

    def control(self, rings=True):
        self.assertTrue(run(self.plugin.set_control_enabled(True))['success'])
        if rings:
            self.assertTrue(run(self.plugin.set_rings_enabled(True))['success'])

    def test_every_setter_is_refused_until_rgb_control_is_enabled(self):
        calls = (self.plugin.set_rings_enabled(True), self.plugin.set_effect('breathe'),
                 self.plugin.set_color(120, 50), self.plugin.set_brightness(80), self.plugin.set_speed(100))
        for call in calls:
            result = run(call)
            self.assertFalse(result['success'])
            self.assertEqual(result['error'], 'Enable RGB control first.')
        self.assertEqual({name: self.attr(name) for name in LED}, {name: value for name, value in LED.items()})

    def test_invalid_input_is_rejected_without_a_write(self):
        self.control()
        before = {name: self.attr(name) for name in LED}
        state = copy.deepcopy(self.memory.data['state'])
        bad = (self.plugin.set_rings_enabled(1), self.plugin.set_rings_enabled('true'),
               self.plugin.set_effect('disco'), self.plugin.set_effect(None), self.plugin.set_effect(3),
               self.plugin.set_color(360, 10), self.plugin.set_color(-1, 10), self.plugin.set_color(10, 101),
               self.plugin.set_color(10, -1), self.plugin.set_color(True, 10), self.plugin.set_color(10.0, 10),
               self.plugin.set_color('10', 10), self.plugin.set_brightness(101), self.plugin.set_brightness(-1),
               self.plugin.set_brightness(50.0), self.plugin.set_brightness(True), self.plugin.set_speed(101),
               self.plugin.set_speed(-1), self.plugin.set_speed('5'))
        for call in bad:
            self.assertFalse(run(call)['success'])
        self.assertEqual({name: self.attr(name) for name in LED}, before)
        self.assertEqual(self.memory.data['state'], state)

    def test_rings_effect_color_brightness_and_speed_reach_the_led_and_are_saved(self):
        self.control(rings=False)
        self.assertEqual(self.attr('enabled'), 'false')
        result = run(self.plugin.set_rings_enabled(True))
        self.assertTrue(result['success'], result)
        self.assertEqual(self.attr('enabled'), 'true')
        self.assertTrue(result['status']['settings']['rings_enabled'])
        self.assertTrue(run(self.plugin.set_effect('Breathe'))['success'])  # case-insensitive
        self.assertEqual((self.attr('effect'), self.saved('effect')), ('breathe', 'breathe'))
        self.assertTrue(run(self.plugin.set_color(120, 50))['success'])
        self.assertEqual(self.attr('multi_intensity'), '50 100 50')
        self.assertEqual((self.saved('hue'), self.saved('saturation')), (120, 50))
        self.assertTrue(run(self.plugin.set_brightness(80))['success'])
        self.assertEqual((self.attr('brightness'), self.saved('brightness')), ('80', 80))
        result = run(self.plugin.set_speed(100))
        self.assertTrue(result['success'])
        self.assertEqual((self.attr('speed'), self.saved('speed')), ('100', 100))
        self.assertEqual({key: result['status']['settings'][key] for key in ('effect', 'hue', 'saturation', 'brightness', 'speed')},
                         {'effect': 'breathe', 'hue': 120, 'saturation': 50, 'brightness': 80, 'speed': 100})
        self.assertFalse(result['status']['recovery_required'])
        self.assertTrue(run(self.plugin.set_rings_enabled(False))['success'])
        self.assertEqual(self.attr('enabled'), 'false')

    def test_unconfirmed_hardware_write_rolls_the_led_and_the_saved_state_back(self):
        self.control()
        real = rgb_backend._write_attr
        refused = []

        def final_enable_not_confirmed(path, attr, value, **options):
            if attr == 'enabled' and not refused:  # brightness has already landed by then
                refused.append(value)
                return False
            return real(path, attr, value, **options)

        with patch.object(rgb_backend, '_write_attr', side_effect=final_enable_not_confirmed):
            result = run(self.plugin.set_brightness(80))
        self.assertEqual(refused, ['true'])
        self.assertFalse(result['success'])
        self.assertIn('did not confirm', result['error'])
        self.assertEqual((self.attr('brightness'), self.saved('brightness')), ('20', 20))
        self.assertIsNone(self.saved('pending'))
        self.assertFalse(result['status']['recovery_required'])

    def test_power_led_is_changed_through_the_register_and_remembers_the_original(self):
        for invalid in (1, 'on', None):
            self.assertFalse(run(self.plugin.set_power_led(invalid))['success'])
        self.assertEqual(self.register.value, 0xA5 | LED_OFF_BIT)
        result = run(self.plugin.set_power_led(True))
        self.assertTrue(result['success'], result)
        self.assertEqual(self.register.value, 0xA5)  # only bit 6 moved
        self.assertTrue(result['status']['power_led']['current'])
        self.assertTrue(result['status']['power_led']['managed'])
        self.assertIs(self.saved('power_led_original_enabled'), False)
        self.assertTrue(run(self.plugin.set_power_led(False))['success'])
        self.assertEqual(self.register.value, 0xA5 | LED_OFF_BIT)
        self.assertIs(self.saved('power_led_original_enabled'), False)  # first-seen value is kept

    def test_unconfirmed_power_led_change_is_rolled_back(self):
        class Stuck(Register):
            def __setitem__(self, _index, _value): pass  # the EC ignores the write

        self.register = Stuck(0xA5 | LED_OFF_BIT)
        with patch.object(rgb_backend, '_write_power_led', wraps=rgb_backend._write_power_led) as write:
            result = run(self.plugin.set_power_led(True))
        self.assertFalse(result['success'])
        self.assertEqual([call.args for call in write.call_args_list], [(True,), (False,)])  # apply, then rollback
        self.assertEqual(self.register.value, 0xA5 | LED_OFF_BIT)
        self.assertFalse(self.saved('power_led_managed'))
        self.assertFalse(result['status']['recovery_required'])

    def test_restore_original_puts_back_the_led_and_the_power_button_and_releases_control(self):
        self.control()
        self.assertTrue(run(self.plugin.set_effect('rainbow'))['success'])
        self.assertTrue(run(self.plugin.set_color(240, 80))['success'])
        self.assertTrue(run(self.plugin.set_brightness(90))['success'])
        self.assertTrue(run(self.plugin.set_speed(10))['success'])
        self.assertTrue(run(self.plugin.set_power_led(True))['success'])
        self.assertEqual(self.register.value, 0xA5)
        result = run(self.plugin.restore_original())
        self.assertTrue(result['success'], result)
        for name in ('enabled', 'effect', 'brightness', 'multi_intensity', 'speed', 'profile', 'mode'):
            self.assertEqual(self.attr(name), LED[name], name)
        self.assertEqual(self.register.value, 0xA5 | LED_OFF_BIT)
        self.assertFalse(self.saved('control_enabled'))
        self.assertFalse(self.saved('power_led_managed'))
        self.assertIsNone(self.saved('original_rgb'))
        self.assertFalse(result['status']['recovery_required'])

    def test_restore_original_without_anything_owned_changes_nothing(self):
        before = {name: self.attr(name) for name in LED}
        result = run(self.plugin.restore_original())
        self.assertTrue(result['success'])
        self.assertEqual({name: self.attr(name) for name in LED}, before)
        self.assertEqual(self.register.value, 0xA5 | LED_OFF_BIT)


# -- Vibration ----------------------------------------------------------------------------

FIRMWARE_DEFAULTS = {'rumble_intensity': 'medium', 'left_handle/rumble_mode': 'fps',
                     'right_handle/rumble_mode': 'fps', 'touchpad/vibration_intensity': 'medium',
                     'touchpad/vibration_enabled': 'true'}


class VibrationRpcTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.device = self.root / 'hid'
        self.settings_dir = self.root / 'settings'
        self.write_hardware(FIRMWARE_DEFAULTS)
        self.store = AtomicSettingsManager('vibration_settings', str(self.settings_dir))
        self.store.replace({'schema_version': 2, 'baseline': {}, 'game_profiles': {
            '0': {'overwrite': False, 'settings': dict(vibe.DEFAULT_PROFILE)}}})
        for name, value in {'settings': self.store, '_device_path': str(self.device), '_attr_cache': {},
                            '_capabilities_cache': {}, '_last_applied_values': None}.items():
            stack.enter_context(patch.object(vibe, name, value))
        stack.enter_context(patch.object(vibe, '_discover', return_value=(str(self.device), 'fixture')))
        stack.enter_context(patch.object(vibe, '_running_app_id', return_value='0'))
        self.plugin = vibe.Plugin()

    def write_hardware(self, values):
        for name, value in values.items():
            path = self.device / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding='ascii')

    def hardware(self):
        return {name: (self.device / name).read_text(encoding='ascii').strip() for name in FIRMWARE_DEFAULTS}

    def profile(self):
        return AtomicSettingsManager('vibration_settings', str(self.settings_dir)).getSetting('game_profiles')['0']['settings']

    def test_rumble_mode_sets_both_handles_and_the_saved_profile(self):
        result = run(self.plugin.set_rumble_mode(2))
        self.assertTrue(result['success'], result)
        self.assertEqual({k: v for k, v in self.hardware().items() if 'rumble_mode' in k},
                         {'left_handle/rumble_mode': 'standard', 'right_handle/rumble_mode': 'standard'})
        self.assertEqual(self.profile()['mode'], 2)
        self.assertEqual(result['settings']['mode'], 2)
        self.assertEqual(self.hardware()['rumble_intensity'], 'medium')  # other attributes keep their values

    def test_rumble_mode_index_is_clamped_to_what_the_driver_offers(self):
        self.assertTrue(run(self.plugin.set_rumble_mode(99))['success'])
        self.assertEqual(self.hardware()['left_handle/rumble_mode'], 'rpg')
        self.assertTrue(run(self.plugin.set_rumble_mode(-4))['success'])
        self.assertEqual(self.hardware()['right_handle/rumble_mode'], 'fps')

    def test_rumble_mode_uses_the_modes_the_driver_advertises(self):
        (self.device / 'left_handle/rumble_mode_index').write_text('alpha beta gamma\n')
        self.assertTrue(run(self.plugin.set_rumble_mode(1))['success'])
        self.assertEqual(self.hardware()['left_handle/rumble_mode'], 'beta')

    def test_touchpad_intensity_levels_and_clamping(self):
        for level, name in ((0, 'off'), (1, 'low'), (3, 'high'), (9, 'high'), (-2, 'off')):
            with self.subTest(level=level):
                result = run(self.plugin.set_touchpad_intensity(level))
                self.assertTrue(result['success'], result)
                self.assertEqual(self.hardware()['touchpad/vibration_intensity'], name)
        self.assertEqual(self.profile()['touchpadIntensity'], 0)

    def test_non_integers_are_rejected_without_a_write_or_a_save(self):
        before = (self.hardware(), self.profile())
        for call in (self.plugin.set_rumble_mode('2'), self.plugin.set_rumble_mode(2.0), self.plugin.set_rumble_mode(True),
                     self.plugin.set_rumble_mode(None), self.plugin.set_touchpad_intensity('1'),
                     self.plugin.set_touchpad_intensity(1.5), self.plugin.set_touchpad_intensity(False)):
            result = run(call)
            self.assertFalse(result['success'])
            self.assertIn('integer', result['error'])
        self.assertEqual((self.hardware(), self.profile()), before)

    def test_stale_profile_expectation_is_refused_without_a_write(self):
        before = (self.hardware(), self.profile())
        for call in (self.plugin.set_rumble_mode(3, '555', '555'), self.plugin.set_touchpad_intensity(3, '555', '555')):
            result = run(call)
            self.assertFalse(result['success'])
            self.assertIn('Active profile changed', result['error'])
        self.assertEqual((self.hardware(), self.profile()), before)

    def test_failed_hardware_write_is_reported_but_the_choice_is_kept(self):
        with patch.object(vibe, '_write_attr', return_value=False):
            result = run(self.plugin.set_rumble_mode(4))
        self.assertFalse(result['success'])
        self.assertEqual(self.profile()['mode'], 4)  # saved intent survives for the next apply
        self.assertEqual(self.hardware(), FIRMWARE_DEFAULTS)

    def test_reapply_forces_the_saved_profile_back_after_the_controller_reset(self):
        self.store.replace({'schema_version': 2, 'baseline': {}, 'game_profiles': {'0': {
            'overwrite': False, 'settings': {'level': 3, 'mode': 1, 'touchpadIntensity': 0, 'touchpadEnabled': False}}}})
        self.assertTrue(run(self.plugin.reapply())['success'])
        saved = {'rumble_intensity': 'high', 'left_handle/rumble_mode': 'racing', 'right_handle/rumble_mode': 'racing',
                 'touchpad/vibration_intensity': 'off', 'touchpad/vibration_enabled': 'false'}
        self.assertEqual(self.hardware(), saved)
        self.write_hardware(FIRMWARE_DEFAULTS)  # resume: firmware is back at its defaults
        with patch.object(vibe, '_write_attr', wraps=vibe._write_attr) as write:
            result = run(self.plugin.reapply())
        self.assertTrue(result['success'], result)
        self.assertEqual(self.hardware(), saved)
        self.assertEqual(result['settings']['level'], 3)
        # The driver's read-back can be stale after a reset, so nothing may be skipped as "already set".
        self.assertEqual(len(write.call_args_list), 5)
        self.assertTrue(all(call.args[3] is True for call in write.call_args_list))

    def test_reapply_reports_failure_when_the_controller_never_returns(self):
        with patch.object(vibe, '_discover', return_value=(None, None)), \
                patch.object(vibe, '_REAPPLY_WAIT_S', 0.0):
            result = run(self.plugin.reapply())
        self.assertFalse(result['success'])
        self.assertEqual(self.hardware(), FIRMWARE_DEFAULTS)


# -- Controller gyro source ---------------------------------------------------------------

PHYSICAL = {'path': '/dev/hidraw1', 'identity': '/sys/devices/usb/1-1:1.2',
            'sysfs': '/sys/devices/usb/1-1:1.2/0003:17EF:61EB.0002', 'pid': '61eb'}
SOURCE = 'hidraw://hidraw1'
ROUTING = {  # which IMU events InputPlumber must filter for each selection
    'combined': ['Accelerometer:Left', 'Accelerometer:Right', 'Gyroscope:Left', 'Gyroscope:Right'],
    'left': ['Accelerometer:Center', 'Accelerometer:Right', 'Gyroscope:Center', 'Gyroscope:Right'],
    'right': ['Accelerometer:Center', 'Accelerometer:Left', 'Gyroscope:Center', 'Gyroscope:Left'],
}
DEVICE = {'path': '/org/shadowblip/InputPlumber/CompositeDevice0', 'source': SOURCE,
          'identity': PHYSICAL['identity'], 'generation': PHYSICAL['sysfs']}


class GyroSourceRpcTests(unittest.TestCase):
    def setUp(self):
        self.store = MemorySettings(default={})
        self.store.data = {}
        self.current = {SOURCE: list(ROUTING['combined']) + ['Mouse:Wheel'], 'evdev://event7': ['Keyboard:KeyA']}
        self.imu = {'left': False, 'right': False}
        self.busctl_calls = []
        self.fail_busctl = False
        self.plugin = controller_backend.Plugin()
        module = controller_backend
        for item in (patch.object(module, 'settings', self.store),
                     patch.object(module, '_hid_inventory', return_value=(PHYSICAL, None)),
                     patch.object(module, '_discover', side_effect=self.discover),
                     patch.object(module, '_filters', side_effect=lambda _path: copy.deepcopy(self.current)),
                     patch.object(module, '_run_busctl', side_effect=self.busctl),
                     patch.object(module.controller_imu, 'read_status',
                                  side_effect=lambda: {'available': True, 'actual': copy.deepcopy(self.imu)}),
                     patch.object(module.controller_imu, 'apply', side_effect=self.apply_imu),
                     patch.object(self.plugin, '_ensure_watch')):
            item.start()
            self.addCleanup(item.stop)

    def apply_imu(self, expected, desired):
        self.assertEqual(expected, self.imu)
        self.imu = copy.deepcopy(desired)

    def discover(self, physical, *, require_exclusive=True):
        return {**DEVICE, 'source': 'hidraw://' + physical['path'].rsplit('/', 1)[-1],
                'generation': physical['sysfs'], 'filters': copy.deepcopy(self.current)}

    def busctl(self, args):
        if self.fail_busctl:
            raise controller_backend.ControllerError('InputPlumber refused the change')
        entries, index, result = int(args[6]), 7, {}
        for _ in range(entries):
            source, count = args[index], int(args[index + 1])
            result[source] = args[index + 2:index + 2 + count]
            index += 2 + count
        self.busctl_calls.append(result)
        self.current = result
        return ''

    def saved_source(self):
        return controller_backend._state()['gyro_source']

    def test_unknown_sources_are_rejected_before_any_hardware_access(self):
        for source in ('', 'both', 'LEFT', 'left ', None, 1, ['left']):
            with self.subTest(source=source), self.assertRaises(ValueError):
                run(self.plugin.set_gyro_source(source))
        self.assertEqual(self.busctl_calls, [])
        self.assertEqual(self.imu, {'left': False, 'right': False})
        self.assertEqual(self.saved_source(), 'system')
        self.plugin._ensure_watch.assert_not_called()

    def test_selecting_a_stick_routes_that_sensor_and_system_gives_everything_back(self):
        baseline = copy.deepcopy(self.current)
        result = run(self.plugin.set_gyro_source('left'))
        self.assertEqual((result['gyro_source'], result['applied_source'], result['controlled']), ('left', 'left', True))
        self.assertEqual(self.imu, {'left': True, 'right': False})
        self.assertEqual(controller_backend._imu_values(self.current, SOURCE), ROUTING['left'])
        self.assertIn('Mouse:Wheel', self.current[SOURCE])        # unrelated filters are preserved
        self.assertEqual(self.current['evdev://event7'], ['Keyboard:KeyA'])
        self.assertEqual(self.saved_source(), 'left')
        self.plugin._ensure_watch.assert_called_once_with()
        result = run(self.plugin.set_gyro_source('system'))
        self.assertEqual((result['gyro_source'], result['controlled']), ('system', False))
        self.assertEqual(self.imu, {'left': False, 'right': False})
        self.assertEqual(self.current, baseline)
        self.assertEqual(self.saved_source(), 'system')

    def test_switching_between_sticks_keeps_the_original_baseline(self):
        baseline = copy.deepcopy(self.current)
        run(self.plugin.set_gyro_source('right'))
        self.assertEqual(self.imu, {'left': False, 'right': True})
        run(self.plugin.set_gyro_source('combined'))
        self.assertEqual(self.imu, {'left': True, 'right': True})
        run(self.plugin.set_gyro_source('system'))
        self.assertEqual(self.current, baseline)

    def test_inputplumber_failure_is_raised_recorded_and_leaves_no_ownership(self):
        self.fail_busctl = True
        with self.assertRaises(controller_backend.ControllerError):
            run(self.plugin.set_gyro_source('left'))
        self.assertIn('refused', self.plugin._error)
        self.assertEqual(self.imu, {'left': False, 'right': False})
        self.assertEqual(self.saved_source(), 'system')
        self.assertNotIn('Gyroscope:Center', self.current[SOURCE])


# -- Remap --------------------------------------------------------------------------------

BASE_PROFILE = """version: 1
kind: DeviceProfile
name: Custom User Profile
description: keep this
mapping:
- name: LeftTop
  source_event:
    gamepad:
      button: LeftTop
  target_events:
  - gamepad:
      button: LeftPaddle1
- name: Keyboard
  source_event:
    gamepad:
      button: Keyboard
  target_events:
  - gamepad:
      button: Guide
  - gamepad:
      button: North
- name: QuickAccess2
  source_event:
    gamepad:
      button: QuickAccess2
  target_events:
  - gamepad:
      button: Screenshot
"""


class RemapRpcTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.memory = MemorySettings(default=remap_backend.DEFAULT_STATE)
        self.hardware = BASE_PROFILE
        self.loads = []
        self.fail_load = False
        for target, value in {'settings': self.memory, '_service_watch': remap_backend.ProcessWatch(),
                              '_last_error': ''}.items():
            stack.enter_context(patch.object(remap_backend, target, value))
        stack.enter_context(patch.object(remap_backend._service_watch, 'capture'))
        stack.enter_context(patch.object(remap_backend, '_find_device', return_value=('/org/test', 'Lenovo Legion Go 2')))
        stack.enter_context(patch.object(remap_backend, '_get_profile', side_effect=lambda _path: self.hardware))
        stack.enter_context(patch.object(remap_backend, '_load_profile', side_effect=self.load))
        stack.enter_context(patch.object(remap_backend, '_inputplumber_version', return_value='0.70.0'))
        self.plugin = remap_backend.Plugin()

    def load(self, _path, profile):
        if self.fail_load:
            raise remap_backend.RemapError('D-Bus temporarily unavailable')
        self.loads.append(profile)
        self.hardware = profile
        return profile

    def state(self):
        return self.memory.data['state']

    def blocks(self, profile):
        return remap_backend._mapping_blocks(profile)[1]

    def test_invalid_enabled_value_is_refused_and_nothing_is_loaded(self):
        for value in ('yes', 1, None, [True]):
            result = run(self.plugin.set_enabled(value))
            self.assertFalse(result['success'])
            self.assertIn('invalid remapper setting', result['error'])
        self.assertEqual(self.loads, [])
        self.assertFalse(self.state()['enabled'])
        self.assertEqual(self.hardware, BASE_PROFILE)

    def test_enable_captures_the_live_profile_applies_the_mappings_and_disable_restores_it(self):
        result = run(self.plugin.set_enabled(True))
        self.assertTrue(result['success'], result)
        self.assertTrue(result['status']['enabled'])
        self.assertTrue(result['status']['active'])
        self.assertEqual(self.state()['baseline_profile'], BASE_PROFILE)
        self.assertEqual(remap_backend._profile_name(self.hardware), remap_backend.PROFILE_NAME)
        self.assertTrue(remap_backend._profile_matches(self.hardware, remap_backend._sanitize_state(self.state())))
        # The unrelated mapping of the user's own profile is still there.
        self.assertIn('button: LeftPaddle1', self.hardware)
        result = run(self.plugin.set_enabled(False))
        self.assertTrue(result['success'], result)
        self.assertFalse(self.state()['enabled'])
        self.assertIsNone(self.state()['baseline_profile'])
        self.assertEqual(remap_backend._profile_name(self.hardware), 'Custom User Profile')
        self.assertEqual(self.blocks(self.hardware), self.blocks(BASE_PROFILE))

    def test_restore_defaults_returns_both_buttons_to_the_original_mappings(self):
        self.assertTrue(run(self.plugin.set_enabled(True))['success'])
        self.assertTrue(run(self.plugin.set_action('desktop', 'f1'))['success'])
        self.assertTrue(run(self.plugin.set_action('page', 'f2'))['success'])
        self.assertIn('keyboard: KeyF1', self.hardware)
        self.assertIn('keyboard: KeyF2', self.hardware)
        result = run(self.plugin.restore_defaults())
        self.assertTrue(result['success'], result)
        self.assertEqual((self.state()['desktop_action'], self.state()['page_action']), ('default', 'default'))
        self.assertNotIn('keyboard: KeyF1', self.hardware)
        self.assertNotIn('keyboard: KeyF2', self.hardware)
        self.assertIn('button: Guide', self.hardware)
        self.assertIn('button: Screenshot', self.hardware)
        self.assertTrue(result['status']['active'])
        self.assertTrue(remap_backend._profile_matches(self.hardware, remap_backend._sanitize_state(self.state())))

    def test_restore_defaults_while_disabled_only_resets_the_saved_choices(self):
        self.memory.data['state'] = remap_backend._sanitize_state({'desktop_action': 'f3', 'page_action': 'escape'})
        result = run(self.plugin.restore_defaults())
        self.assertTrue(result['success'], result)
        self.assertEqual((self.state()['desktop_action'], self.state()['page_action']), ('default', 'default'))
        self.assertFalse(self.state()['enabled'])
        self.assertEqual(self.loads, [])
        self.assertEqual(self.hardware, BASE_PROFILE)

    def test_restore_defaults_failure_is_reported_with_the_current_status(self):
        self.assertTrue(run(self.plugin.set_enabled(True))['success'])
        self.assertTrue(run(self.plugin.set_action('desktop', 'f1'))['success'])
        self.fail_load = True
        result = run(self.plugin.restore_defaults())
        self.assertFalse(result['success'])
        self.assertIn('D-Bus temporarily unavailable', result['error'])
        self.assertTrue(result['status']['enabled'])
        self.assertIn('keyboard: KeyF1', self.hardware)  # the live mapping was not half-changed


# -- TDP ----------------------------------------------------------------------------------

class TdpRpcTests(unittest.TestCase):
    setUp = tdp_core.TdpCoreTests.setUp
    seed = tdp_core.TdpCoreTests.seed
    ppt = tdp_core.TdpCoreTests.ppt
    ppts = tdp_core.TdpCoreTests.ppts
    set_ppts = tdp_core.TdpCoreTests.set_ppts
    rpc = tdp_core.TdpCoreTests.rpc

    def saved(self):
        return AtomicSettingsManager('tdp_settings', str(self.root / 'settings'))

    def test_invalid_app_ids_are_refused(self):
        for app_id in ('', None, '../x', 'a b', 'x' * 65, 'a/b', {}):
            with self.subTest(app_id=app_id):
                result = self.rpc('get_game_profile', app_id)
                self.assertEqual((result['exists'], result['profile'], result['error']), (False, {}, 'invalid app id'))

    def test_unknown_game_has_no_profile_and_no_error(self):
        self.assertEqual(self.rpc('get_game_profile', '999'),
                         {'exists': False, 'active': False, 'profile': {}, 'ac_separate': False, 'ac_profile': {}})

    def test_saved_game_profile_is_returned_clamped_with_its_separate_ac_values(self):
        self.seed(profiles={
            '123': {'spl': 18000, 'sppt': 22000, 'fppt': 30000, 'preset': 'Quiet',
                    'ac_separate': True, 'ac_spl': 25000, 'ac_sppt': 28000, 'ac_fppt': 32000, 'ac_preset': 'Plugged'},
            '456': {'spl': 1000, 'sppt': 900, 'fppt': 800},
            '789': {'spl': 20000, 'sppt': 21000, 'fppt': 22000, 'ac_separate': 'yes'},
        })
        result = self.rpc('get_game_profile', '123')
        self.assertTrue(result['exists'])
        self.assertEqual({k: result['profile'][k] for k in ('spl', 'sppt', 'fppt', 'preset')},
                         {'spl': 18000, 'sppt': 22000, 'fppt': 30000, 'preset': 'Quiet'})
        self.assertTrue(result['ac_separate'])
        self.assertEqual({k: result['ac_profile'][k] for k in ('spl', 'sppt', 'fppt', 'ac_preset')},
                         {'spl': 25000, 'sppt': 28000, 'fppt': 32000, 'ac_preset': 'Plugged'})
        # Out-of-range values are clamped on read; ordering spl <= sppt <= fppt holds.
        low = self.rpc('get_game_profile', '456')['profile']
        self.assertLessEqual(low['spl'], low['sppt'])
        self.assertLessEqual(low['sppt'], low['fppt'])
        self.assertGreaterEqual(low['spl'], 5000)
        # A non-bool flag is not trusted; without AC values the AC profile mirrors the battery one.
        odd = self.rpc('get_game_profile', '789')
        self.assertFalse(odd['ac_separate'])
        self.assertEqual({k: odd['ac_profile'][k] for k in ('spl', 'sppt', 'fppt')}, {'spl': 20000, 'sppt': 21000, 'fppt': 22000})
        self.assertEqual(self.rpc('get_game_profile', 123)['exists'], True)  # numeric Steam ids are accepted

    def test_plugin_enabled_rejects_non_bool(self):
        before = self.ppts()
        for value in (1, 0, 'true', None):
            result = self.rpc('set_plugin_enabled', value)
            self.assertFalse(result['success'])
            self.assertIn('boolean', result['stderr'])
        self.assertEqual(self.ppts(), before)
        self.assertEqual(self.profile_writes, [])

    def test_disable_hands_the_firmware_back_and_enable_applies_the_saved_limits_again(self):
        self.assertTrue(self.rpc('apply_tdp', 18000, 22000, 30000)['success'])
        self.assertEqual((self.profile.read_text(), self.ppts()), ('custom', (18, 22, 30)))
        result = self.rpc('set_plugin_enabled', False)
        self.assertTrue(result['success'], result)
        self.assertEqual(self.profile.read_text(), 'balanced')
        self.assertFalse(self.saved().getSetting('settings')['enabled'])
        result = self.rpc('set_plugin_enabled', True)
        self.assertTrue(result['success'], result)
        self.assertEqual(self.profile.read_text(), 'custom')
        state = self.saved().getSetting('settings')
        self.assertTrue(state['enabled'])
        self.assertEqual(self.ppts(), (state['active_spl'] // 1000, state['active_sppt'] // 1000, state['active_fppt'] // 1000))

    def test_enable_uses_the_running_games_profile(self):
        self.seed(profiles={'123': {'spl': 12000, 'sppt': 14000, 'fppt': 16000}}, enabled=False)
        self.app = '123'
        self.assertTrue(self.rpc('set_plugin_enabled', True)['success'])
        self.assertEqual(self.ppts(), (12, 14, 16))

    def test_failed_restore_keeps_the_plugin_enabled(self):
        self.assertTrue(self.rpc('apply_tdp', 18000, 22000, 30000)['success'])
        with patch.object(tdp_core.tdp, '_read_profile', return_value='custom'):
            result = self.rpc('set_plugin_enabled', False)
        self.assertFalse(result['success'])
        self.assertTrue(self.saved().getSetting('settings')['enabled'])


if __name__ == '__main__':
    unittest.main()

# SPDX-License-Identifier: BSD-3-Clause
"""Third-audit regressions, using temporary settings and simulated hardware."""
import asyncio
import copy
from unittest.mock import patch
import unittest

import test_second_hardware_audit as fixtures
from test_remap_backend import BASE_PROFILE, MemorySettings
import display_backend as display
import remap_backend as remap
import vibration_backend as vibe


class DisplaySessionTests(unittest.TestCase):
    def setUp(self):
        fixtures.DisplayBaselineTests.setUp(self)
        self.stack.enter_context(patch.object(display.Plugin, '_task', None))
        self.stack.enter_context(patch.object(display.Plugin, '_prop_task', None))

    def test_disable_after_compositor_restart_does_not_restore_old_brightness(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        with patch.object(display, '_gamescope_start_time', return_value=200.0), \
                patch.object(display.Plugin, '_refresh_native_support'), \
                patch.object(display, '_write_nits') as writes:
            result = asyncio.run(display.Plugin().set_enabled(False))
        writes.assert_not_called()
        self.assertFalse(result['active'])
        self.assertEqual(result['reason'], 'turned off')
        self.assertIsNone(display.Plugin._baseline)

    def test_unload_after_restart_does_not_restore_old_hdr_atoms(self):
        self.state.update(active_mode='hybrid')
        display.Plugin._hdr_baseline = False
        with patch.object(display, '_gamescope_start_time', return_value=200.0), \
                patch.object(display.Plugin, '_read_settings'), \
                patch.object(display.Plugin, '_refresh_native_support'), \
                patch.object(display.Plugin, '_edid_restore'), \
                patch.object(display, '_write_atom_int') as writes, \
                patch.object(display, '_write_nits') as nits:
            asyncio.run(display.Plugin()._unload())
        writes.assert_not_called()
        nits.assert_not_called()
        self.assertIsNone(display.Plugin._hdr_baseline)

    def test_unknown_session_defers_release_and_same_session_can_retry(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        display.Plugin._hdr_baseline = False
        self.props[display.ATOM_SDR_NITS] = display._float_raw(120.0)
        with patch.object(display, '_gamescope_start_time', return_value=None), \
                patch.object(display, '_write_nits') as nits, \
                patch.object(display, '_write_atom_int') as atoms:
            self.assertFalse(display.Plugin._release())
            self.assertFalse(display.Plugin._hybrid_release())
        nits.assert_not_called()
        atoms.assert_not_called()
        self.assertEqual(display.Plugin._baseline, 250.0)
        self.assertIs(display.Plugin._hdr_baseline, False)
        with patch.object(display, '_write_nits', return_value=True) as nits:
            self.assertTrue(display.Plugin._release())
        nits.assert_called_once_with(250.0)

    def test_stale_gate_cannot_forward_brightness_to_a_new_compositor(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        with patch.object(display, '_gamescope_start_time', return_value=200.0), \
                patch.object(display, '_write_nits') as writes:
            self.assertIsNone(display.Plugin._forward_nits(180.0))
        writes.assert_not_called()
        self.assertFalse(self.state['active'])

    def test_stale_hybrid_pass_cannot_write_before_the_new_session_is_adopted(self):
        self.state.update(active_mode='hybrid', panel_mode='hybrid')
        display.Plugin._hdr_baseline = False
        with patch.object(display, '_gamescope_start_time', return_value=200.0), \
                patch.object(display, '_write_atom_int') as writes:
            display.Plugin._hybrid_pass()
        writes.assert_not_called()
        self.assertIsNone(display.Plugin._hdr_baseline)

    def test_new_session_recovers_forwarding_and_restores_its_own_fresh_baseline(self):
        display.Plugin._baseline = 250.0
        display.Plugin._last_written = 120.0
        self.props[display.ATOM_SDR_NITS] = display._float_raw(700.0)
        def write(value):
            self.props[display.ATOM_SDR_NITS] = display._float_raw(value)
            return True
        with patch.object(display, '_gamescope_start_time', return_value=200.0), \
                patch.object(display, '_write_nits', side_effect=write) as writes:
            self.assertIsNone(display.Plugin._forward_nits(180.0))
            display.Plugin._sync_loaded_script()
            self.assertTrue(display.Plugin._refresh_gate())
            self.state['active'] = True  # The worker admits the fresh gate.
            self.assertTrue(display.Plugin._forward_nits(180.0))
            self.assertTrue(display.Plugin._release())
        self.assertEqual([call.args[0] for call in writes.call_args_list], [180.0, 700.0])
        self.assertEqual(self.store.getSetting('brightness_baseline_session'), 200.0)
        self.assertEqual(self.store.getSetting('brightness_baseline'), 700.0)


class VibrationCapacityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.VibrationProfileContextTests.asyncSetUp
    asyncTearDown = fixtures.VibrationProfileContextTests.asyncTearDown
    persisted = fixtures.VibrationProfileContextTests.persisted

    def fill(self, count):
        self.store.replace({'game_profiles': {str(i): {
            'overwrite': i != 0, 'settings': dict(vibe.DEFAULT_PROFILE), 'name': f'Game {i}'
        } for i in range(count)}})

    async def test_capacity_rejects_creation_before_commit_or_hardware_write(self):
        self.fill(vibe.MAX_GAME_PROFILES)
        before = self.persisted()
        with patch.object(vibe, '_running_app_id', return_value='9999'), \
                patch.object(vibe, '_apply_settings') as hardware:
            result = await self.plugin.set_profile_overwrite('9999', True)
        self.assertFalse(result['success'])
        self.assertIn('limit', result['error'])
        self.assertEqual(result['profile_id'], '0')
        self.assertEqual(self.persisted(), before)
        hardware.assert_not_called()

    async def test_existing_profiles_can_be_edited_and_deleted_at_capacity(self):
        self.fill(vibe.MAX_GAME_PROFILES)
        with patch.object(vibe, '_running_app_id', return_value='111'), \
                patch.object(vibe, '_apply_settings', return_value=True):
            self.assertTrue((await self.plugin.set_intensity(3, '111', '111'))['success'])
            self.assertTrue((await self.plugin.delete_game_profile('222'))['success'])
            self.assertTrue((await self.plugin.set_profile_overwrite('9999', True))['success'])
        saved = self.persisted()
        self.assertEqual(len(saved), vibe.MAX_GAME_PROFILES)
        self.assertEqual(saved['111']['settings']['level'], 3)
        self.assertIn('9999', saved)

    async def test_legacy_over_capacity_store_survives_unrelated_edit(self):
        self.fill(vibe.MAX_GAME_PROFILES + 1)
        before = self.persisted()
        with patch.object(vibe, '_running_app_id', return_value='0'), \
                patch.object(vibe, '_apply_settings', return_value=True):
            self.assertTrue((await self.plugin.set_intensity(3, '0', '0'))['success'])
        after = self.persisted()
        self.assertEqual(set(after), set(before))
        self.assertEqual(after[str(vibe.MAX_GAME_PROFILES)], before[str(vibe.MAX_GAME_PROFILES)])

    async def test_profile_toggle_reports_committed_scope_when_hardware_apply_fails(self):
        with patch.object(vibe, '_running_app_id', return_value='111'), \
                patch.object(vibe, '_apply_settings', return_value=False):
            result = await self.plugin.set_profile_overwrite('111', False)
        self.assertFalse(result['success'])
        self.assertEqual((result['app_id'], result['profile_id'], result['overwrite']), ('111', '0', False))
        self.assertFalse(self.persisted()['111']['overwrite'])

    async def test_toggle_of_inactive_profile_returns_actual_running_scope(self):
        with patch.object(vibe, 'scan_proc_for_appid', side_effect=['222', '111']) as scan, \
                patch.object(vibe, '_apply_settings', return_value=True):
            result = await self.plugin.set_profile_overwrite('111', False)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual((result['app_id'], result['profile_id'], result['overwrite']), ('222', '222', True))

    async def test_new_profile_is_seeded_from_the_single_admitted_running_snapshot(self):
        with patch.object(vibe, 'scan_proc_for_appid', side_effect=['222', '111']) as scan, \
                patch.object(vibe, '_apply_settings', return_value=True):
            result = await self.plugin.set_profile_overwrite('333', True)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(self.persisted()['333']['settings'], self.profiles['222']['settings'])
        self.assertEqual(result['app_id'], '222')


class RemapScalarTests(unittest.TestCase):
    def run_cycle(self, baseline, append=False):
        state = remap._sanitize_state({'enabled': False, 'desktop_action': 'f1', 'page_action': 'f2'})
        store = MemorySettings(state)
        current = [baseline]
        def load(_path, profile):
            current[0] = profile
            return profile
        with patch.object(remap, 'settings', store), \
                patch.object(remap, '_find_device', return_value=('/fixture', 'Legion Go 2')), \
                patch.object(remap, '_get_profile', side_effect=lambda _: current[0]), \
                patch.object(remap, '_load_profile', side_effect=load):
            result = remap._mutate_sync({'enabled': True})
            self.assertTrue(result['success'], result)
            self.assertEqual(remap._profile_name(current[0]), remap.PROFILE_NAME)
            self.assertNotIn('\n  User\n', current[0])
            self.assertEqual(store.data['state']['baseline_profile'], baseline)
            if append:
                current[0] += '- name: External LeftTop\n  source_event:\n    gamepad:\n      button: LeftTop\n  target_events:\n  - keyboard: KeyF12\n'
            remap._repair_sync()
            self.assertEqual(store.data['state']['baseline_profile'], baseline)
            result = remap._mutate_sync({'enabled': False})
            self.assertTrue(result['success'], result)
        return current[0]

    def test_complete_multiline_name_is_replaced_and_original_scalar_restored(self):
        for name in ('name: |\n  User\n  Profile\n', 'name: >-\n  User\n  Profile\n',
                     'name: "User:\\nProfile"\n', 'name: "User\n  Profile"\n'):
            with self.subTest(name=name):
                baseline = BASE_PROFILE.replace('name: Custom User Profile\n', name).replace(
                    'description: keep this', 'description: |\n  Keep this\n  description')
                restored = self.run_cycle(baseline)
                self.assertEqual(restored, baseline)

    def test_restore_separates_unterminated_baseline_from_external_mapping(self):
        baseline = BASE_PROFILE.replace('  - gamepad:\n      button: Guide\n  - gamepad:\n      button: North\n',
                                        '  - keyboard: KeyF7\n').replace(
                                        '  - gamepad:\n      button: Screenshot\n', '  - keyboard: KeyF8\n').rstrip('\n')
        restored = self.run_cycle(baseline, append=True)
        self.assertIn('keyboard: KeyF8\n- name: External LeftTop', restored)
        self.assertIn('keyboard: KeyF7\n', restored)
        self.assertIn('keyboard: KeyF12\n', restored)
        self.assertNotIn('keyboard: KeyF1\n', restored)
        self.assertEqual(len(remap._mapping_blocks(restored)[1]), 4)

# SPDX-License-Identifier: BSD-3-Clause
"""Diagnostic outcomes remain truthful, private and quiet on repeated faults."""
import asyncio
import builtins
from collections import OrderedDict
from contextlib import ExitStack
import copy
import errno
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_integration
import diagnostics
import battery_backend as battery
import controller_backend as controller
import remap_backend as remap
import rgb_backend as rgb
import safe_settings
import tdp_backend as tdp
import vibration_backend as vibe


async def call_sync(function, *args, **kwargs):
    return function(*args, **kwargs)


class CoreLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.records = []
        self.stack.enter_context(patch.object(diagnostics, '_sink',
            lambda text, level: self.records.append(json.loads(text))))
        self.stack.enter_context(patch.object(diagnostics, '_seen', OrderedDict()))

    def events(self, component, action):
        return [row for row in self.records if row['component'] == component and row['event'] == action]

    async def test_vibration_uninstall_does_not_claim_restore_after_false_readback(self):
        with patch.object(vibe, '_offload', AsyncMock(side_effect=[False, True])):
            await vibe.Plugin()._uninstall()
            await vibe.Plugin()._uninstall()
        rows = self.events('vibration', 'restore')
        self.assertEqual([row['context']['restored'] for row in rows], [False, True])
        self.assertEqual([row['level'] for row in rows], ['warning', 'info'])

    async def test_rgb_uninstall_reports_incomplete_pending_recovery(self):
        with patch.object(rgb, '_offload', side_effect=call_sync), patch.object(rgb, '_recover_pending', return_value=False):
            await rgb.Plugin()._uninstall()
        row, = self.events('rgb', 'restore')
        self.assertFalse(row['context']['restored'])
        self.assertEqual(row['context']['outcome'], 'unconfirmed')

    async def test_rgb_write_retains_errno_and_attribute_without_raw_path(self):
        with patch.object(rgb.os, 'open', side_effect=PermissionError(errno.EACCES, 'private detail', '/home/private/secret-file')):
            self.assertFalse(rgb._write_attr('/home/private/secret-file', 'brightness', '11', force=True))
        row, = self.events('rgb', 'attribute_write')
        self.assertEqual(row['error']['errno'], errno.EACCES)
        self.assertEqual(row['context']['attribute'], 'brightness')
        self.assertNotIn('/home/private', json.dumps(self.records))
        self.assertNotIn('private detail', json.dumps(self.records))

    async def test_rgb_short_write_has_an_origin_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / 'brightness').write_text('10\n')
            with patch.object(rgb.os, 'write', return_value=1):
                self.assertFalse(rgb._write_attr(str(path), 'brightness', '11', force=True))
        row, = self.events('rgb', 'attribute_write')
        self.assertEqual(row['context']['reason'], 'short_write')
        self.assertEqual(row['context']['bytes'], 1)

    async def test_rgb_background_failure_is_coalesced_and_recovery_is_visible(self):
        state = dict(copy.deepcopy(rgb.DEFAULT_STATE), control_enabled=True)
        with patch.object(rgb, '_load_state', return_value=state), patch.object(rgb, '_recover_pending', return_value=True), patch.object(rgb, '_rgb_capability', return_value=(None, 'Controller unavailable')):
            self.assertFalse(rgb._reconcile())
            self.assertFalse(rgb._reconcile())
            state['control_enabled'] = False
            self.assertTrue(rgb._reconcile())
        rows = self.events('rgb', 'reconcile')
        self.assertEqual([row['context']['status'] for row in rows], ['ring_unavailable', 'confirmed'])

    async def test_controller_startup_watch_repetition_and_recovery_are_one_state_history(self):
        plugin = controller.Plugin()
        with patch.object(controller, '_offload', side_effect=call_sync), patch.object(controller, '_state', return_value={'gyro_source':'left'}), patch.object(plugin, '_select', side_effect=OSError(errno.EIO, 'initial failure')), patch.object(plugin, '_ensure_watch'):
            await plugin._main()
        clock = iter((0, 61, 122, 183))
        async def sleep(_seconds):
            sleep.calls += 1
            if sleep.calls > 3:
                raise asyncio.CancelledError()
        sleep.calls = 0
        with patch.object(controller, 'time', types.SimpleNamespace(monotonic=lambda:next(clock))), patch.object(controller, '_suspend_offset', return_value=0), patch.object(controller.asyncio, 'sleep', side_effect=sleep), patch.object(plugin, '_watch_check', AsyncMock(side_effect=[OSError(errno.EIO, 'retry one'), OSError(errno.EIO, 'retry two'), None])):
            with self.assertRaises(asyncio.CancelledError):
                await plugin._watch()
        rows = self.events('controller', 'managed_profile')
        self.assertEqual([row['context']['status'] for row in rows], ['deferred', 'True'])
        self.assertEqual([row['level'] for row in rows], ['warning', 'info'])

    async def test_controller_diagnostics_records_endpoints_and_finite_summary_without_token(self):
        plugin = controller.Plugin()
        capture = {'active':True, 'reason':'', 'token':'private-diagnostics-token',
                   'physical':{'reports':0}, 'virtual':{'reports':0}}
        plugin._worker((None, None), capture, plugin._stop)
        self.assertEqual(len(self.events('controller', 'diagnostics_stream')), 2)
        row, = self.events('controller', 'diagnostics_complete')
        self.assertEqual(row['context']['reason'], 'unavailable')
        self.assertEqual(row['context']['count'], 0)
        self.assertNotIn(capture['token'], json.dumps(self.records))

    async def test_remap_repeated_failure_and_later_recovery_stay_bounded(self):
        state = {'enabled':True}
        with patch.object(remap, '_load_state', return_value=state), patch.object(remap, '_apply_state', side_effect=[RuntimeError('first'), RuntimeError('second'), RuntimeError('third'), (state, False)]), patch.object(remap._service_watch, 'capture'):
            self.assertEqual([remap._repair_sync() for _ in range(4)], [False, False, False, True])
        rows = self.events('remap', 'managed_profile')
        self.assertEqual([row['context']['status'] for row in rows], ['False', 'True'])

    async def test_backup_failures_identify_each_store_and_recovery_without_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            stores = [safe_settings.AtomicSettingsManager(name, temporary) for name in ('battery_settings', 'rgb_settings')]
            backup_paths = {store.backup_path for store in stores}
            write = safe_settings._atomic_write_bytes
            def storage(path, payload):
                if path in backup_paths:
                    raise OSError(errno.ENOSPC, 'private backup failure')
                return write(path, payload)
            with patch.object(safe_settings, '_atomic_write_bytes', side_effect=storage):
                for store in stores:
                    store.replace({'private_setting':'private-payload-marker'})
                    store.replace({'private_setting':'private-payload-marker'})
            stores[0].replace({'private_setting':'private-payload-marker'})
        rows = self.events('settings', 'backup_refresh')
        self.assertEqual([(row['context']['store'], row['context']['status']) for row in rows],
                         [('battery_settings','False'), ('rgb_settings','False'), ('battery_settings','True')])
        self.assertNotIn('private-payload-marker', json.dumps(self.records))

    async def test_primary_commit_failure_is_identified_without_changing_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = safe_settings.AtomicSettingsManager('controller_settings', temporary)
            store.replace({'enabled':False})
            with patch.object(safe_settings, '_atomic_write_bytes', side_effect=OSError(errno.ENOSPC, 'full')):
                with self.assertRaises(OSError):
                    store.replace({'enabled':True})
            self.assertFalse(store.getSetting('enabled'))
        row, = self.events('settings', 'primary_commit_failed')
        self.assertEqual(row['context']['store'], 'controller_settings')
        self.assertEqual(row['error']['errno'], errno.ENOSPC)

    async def test_tdp_write_failures_keep_independent_attribute_origins(self):
        with patch.object(tdp, '_wmi_path', return_value='/offline-fixture'), patch('builtins.open', side_effect=PermissionError(errno.EACCES, 'denied')):
            tdp._write_ppt(15, 18, 25)
            self.assertFalse(tdp._write_profile('/offline-fixture', 'custom'))
        rows = self.events('tdp', 'attribute_write')
        self.assertEqual(len(rows), 4)
        self.assertEqual({row['context']['attribute'] for row in rows}, set(tdp.WMI_ATTRS.values()) | {'platform_profile'})

    async def test_power_led_access_failure_has_checked_stage_without_hardware_access(self):
        with patch.object(rgb, '_power_capability', return_value=(True, '')), patch.object(rgb, '_with_power_lock', side_effect=TimeoutError('checked lock busy')):
            self.assertIsNone(rgb._read_power_led())
            self.assertFalse(rgb._write_power_led(True))
        rows = self.events('rgb', 'power_led_access')
        self.assertEqual([row['context']['stage'] for row in rows], ['read', 'write'])
        self.assertTrue(all(row['error']['type'] == 'TimeoutError' for row in rows))

    async def test_tdp_hardware_rollback_exception_is_not_discarded(self):
        state = {'enabled':True, 'spl':15000, 'sppt':18000, 'fppt':25000}
        with patch.object(tdp, '_load_settings', return_value=state), patch.object(tdp, '_effective_cpu_state', side_effect=copy.deepcopy), patch.object(tdp, '_read_cpu_baseline', return_value=None), patch.object(tdp, '_apply_limits_with_saved_cpu_power', side_effect=OSError(errno.EIO, 'rollback failed')):
            with tdp._tdp_user_transaction() as restore_failed:
                result = restore_failed({'success':False, 'stderr':'apply failed'})
        self.assertIn('rollback failed', result['stderr'])
        row, = self.events('tdp', 'rollback')
        self.assertEqual(row['error']['errno'], errno.EIO)
        self.assertFalse(row['context']['restored'])

    async def test_background_helper_returned_failure_and_retry_have_finite_outcomes(self):
        plugin = tdp.Plugin()
        with patch.object(tdp, '_offload', side_effect=call_sync), patch.object(tdp, '_wmi_only', return_value=False), patch.object(tdp, '_ensure_ryzenadj', side_effect=[TimeoutError('private helper URL/token'), True]), patch.object(tdp, '_ryzenadj_available', False), patch.object(tdp, '_current_game_id', ''), patch.object(tdp, '_transition_key', None):
            self.assertFalse((await plugin.retry_extras())['success'])
            self.assertTrue((await plugin.retry_extras())['success'])
        rows = self.events('tdp', 'helper_ready')
        self.assertEqual([row['context']['status'] for row in rows], ['False', 'True'])
        self.assertEqual(rows[0]['error']['type'], 'TimeoutError')
        self.assertNotIn('private helper', json.dumps(self.records))

    async def test_controller_and_remap_lifecycle_faults_do_not_log_raw_dependency_text(self):
        marker='ConfidentialValue_Z7'
        logger=Mock();plugin=controller.Plugin()
        with patch.object(controller.decky,'logger',logger),patch.object(plugin,'_stop_capture'), \
                patch.object(plugin._service_watch,'clear'),patch.object(controller,'_offload',AsyncMock(side_effect=OSError(errno.EIO,marker))):
            await plugin._unload()
        with patch.object(remap.decky,'logger',logger),patch.object(remap._service_watch,'clear'), \
                patch.object(remap.module_runtime,'offload',AsyncMock(side_effect=OSError(errno.EACCES,marker))):
            await remap.Plugin()._uninstall()
        rows=self.events('controller','restore_failed')+self.events('remap','restore_failed')
        self.assertEqual(len(rows),2);self.assertTrue(all(row['context']['pending'] for row in rows))
        self.assertEqual([row['error']['errno'] for row in rows],[errno.EIO,errno.EACCES])
        self.assertNotIn(marker,json.dumps(self.records));self.assertNotIn(marker,str(logger.mock_calls))

    async def test_cpu_status_dependency_failure_remains_generic_and_private(self):
        marker='ConfidentialValue_Z7';logger=Mock()
        with patch.object(tdp.decky,'logger',logger),patch.object(tdp,'_offload',side_effect=call_sync), \
                patch.object(tdp,'_cpu_scoped_status',side_effect=OSError(errno.EIO,marker)):
            result=await tdp.Plugin().get_cpu_power_controls()
        self.assertFalse(result['success']);self.assertEqual(result['error'],'CPU power controls unavailable')
        row,=self.events('tdp','cpu_status_failed');self.assertEqual(row['error']['errno'],errno.EIO)
        self.assertNotIn(marker,json.dumps(self.records));self.assertNotIn(marker,str(logger.mock_calls))

    async def test_controller_battery_read_fault_is_private_and_expected_absence_stays_quiet(self):
        marker='ConfidentialValue_Z7';plugin=controller.Plugin()
        with patch.object(controller,'_hid_inventory',return_value=({'fixture':True},None)), \
                patch.object(controller,'_open_reader',side_effect=PermissionError(errno.EACCES,marker)) as reader:
            result=plugin._controller_batteries()
            self.assertEqual(plugin._controller_batteries(),result)
        reader.assert_called_once()
        self.assertFalse(result['available']);self.assertIn(marker,result['reason'])
        row,=self.events('controller','battery_read_failed')
        self.assertEqual(row['error']['errno'],errno.EACCES)
        self.assertTrue(row['error']['frames']);self.assertNotIn(marker,json.dumps(self.records))
        with patch.object(controller,'_hid_inventory',return_value=(None,None)), \
                patch.object(controller,'_open_reader') as reader:
            self.assertFalse(controller.Plugin()._controller_batteries()['available'])
        reader.assert_not_called()
        with patch.object(controller,'_hid_inventory',return_value=({'fixture':True},None)), \
                patch.object(controller,'_open_reader',return_value=17), \
                patch.object(controller.select,'select',return_value=([],[],[])),patch.object(controller.os,'close') as close:
            self.assertFalse(controller.Plugin()._controller_batteries()['available'])
        close.assert_called_once_with(17)
        self.assertEqual(len(self.events('controller','battery_read_failed')),1)

    async def test_battery_returned_restore_error_is_a_fixed_outcome_not_a_log_payload(self):
        marker='ConfidentialValue_Z7';plugin=battery.Plugin();logger=Mock()
        with patch.object(battery.decky,'logger',logger),patch.object(plugin,'_unload',AsyncMock()), \
                patch.object(plugin,'release_control',AsyncMock(return_value={'success':False,'error':marker})):
            self.assertIsNone(await plugin._uninstall())
        row,=self.events('battery','restore_failed')
        self.assertFalse(row['context']['restored']);self.assertEqual(row['context']['reason'],'operation_unconfirmed')
        self.assertNotIn(marker,json.dumps(self.records));self.assertNotIn(marker,str(logger.mock_calls))

    async def test_returned_tdp_error_stays_in_result_without_raw_diagnostic_or_extra_apply(self):
        marker='ConfidentialValue_Z7';result={'success':False,'stderr':marker,'stdout':marker,'returncode':4};logger=Mock()
        with patch.object(tdp.decky,'logger',logger),patch.object(tdp,'_load_settings',return_value={'enabled':True}), \
                patch.object(tdp,'_clamp_for_settings',return_value=(15000,18000,25000)), \
                patch.object(tdp,'_apply_limits_with_saved_cpu_power',return_value=result) as apply,patch.object(tdp,'_save_active') as save:
            self.assertIs(tdp._apply_and_record(15000,18000,25000,'fixture'),result)
        apply.assert_called_once();save.assert_not_called()
        row,=self.events('tdp','limits_apply_failed');self.assertEqual(row['context']['returncode'],4)
        self.assertNotIn(marker,json.dumps(self.records));self.assertNotIn(marker,str(logger.mock_calls))

    async def test_enforcement_loop_logs_liveness_recovery_without_claiming_hardware_apply(self):
        plugin=tdp.Plugin();calls=0
        async def pause(_seconds):
            nonlocal calls
            if calls==3:raise asyncio.CancelledError()
            calls+=1
        with patch.object(tdp.asyncio,'sleep',side_effect=pause), \
                patch.object(tdp,'_offload',AsyncMock(side_effect=[OSError(errno.EIO,'ConfidentialValue_Z7'),{},{}])):
            with self.assertRaises(asyncio.CancelledError):await plugin._enforce_loop()
        rows=self.events('tdp','enforcement_loop')
        self.assertEqual([row['context']['status'] for row in rows],['unavailable','running'])
        self.assertEqual(rows[-1]['context']['outcome'],'cycle_complete')
        self.assertNotIn('ConfidentialValue_Z7',json.dumps(self.records))


if __name__ == '__main__':
    unittest.main()

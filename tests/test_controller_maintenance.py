# SPDX-License-Identifier: BSD-3-Clause
"""Real thread/cancellation races; no USB or firmware output."""
import asyncio
import copy
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_integration
import main
import module_runtime as runtime
import controller_backend
from dataclasses import replace
from controller_firmware import FirmwareError
from controller_firmware_usb_linux import UsbObservation, HID_REPORT


class ControllerMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_workers = runtime._workers
        self.previous_pause = runtime._controller_pause
        self.previous_generation = runtime._controller_generation
        runtime._workers = {}
        runtime._controller_pause = None
        self.thread_releases = []
        self.folder = tempfile.TemporaryDirectory()
        self.settings = patch.object(main.decky, 'DECKY_PLUGIN_SETTINGS_DIR', self.folder.name)
        self.settings.start()
        self.plugin = main.Plugin()
        self.plugin._check_guard = AsyncMock()
        self.plugin._controller._stop_capture = Mock()

    async def asyncTearDown(self):
        for event in self.thread_releases:
            event.set()
        for name in tuple(runtime._workers):
            await runtime.drain(name)
        runtime._workers = self.previous_workers
        runtime._controller_pause = self.previous_pause
        runtime._controller_generation = self.previous_generation
        self.settings.stop()
        self.folder.cleanup()

    async def eventually(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait(), 2)

    async def held_thread(self):
        started, release = threading.Event(), threading.Event()
        self.thread_releases.append(release)
        def work():
            started.set()
            if not release.wait(3):
                raise RuntimeError('Test failed to release its own thread.')
            return 'completed'
        task = asyncio.create_task(controller_backend._offload(work))
        await self.eventually(started.is_set)
        return task, release

    def usb_baseline(self):
        return UsbObservation('a1c2d3e4-1234-4321-abcd-0123456789ab',
            '/sys/devices/usb3/3-1',(1,2),3,2,(1,),bytes(18),(),
            '/sys/bus/usb/drivers/usbhid',HID_REPORT)

    async def test_failed_post_drain_observation_recovers_only_own_original_unclaimed_pause(self):
        baseline=self.usb_baseline()
        with patch('controller_firmware_usb_linux.observe_command_interface',
                side_effect=[baseline,FirmwareError('Temporary disconnect')]):
            with self.assertRaises(FirmwareError):await self.plugin._updater_pause()
        hold=self.plugin._controller_hold
        self.assertIsNotNone(hold);self.assertTrue(hold.quiescent)
        self.assertIs(self.plugin._controller_usb_baseline,baseline)
        for observed in (replace(baseline,ports=(2,)),replace(baseline,driver=None),
                         replace(baseline,hid_report_sha256='foreign')):
            with patch('controller_maintenance.maintenance_present',return_value=False), \
                    patch('controller_firmware_usb_linux.observe_command_interface',return_value=observed):
                with self.assertRaises(RuntimeError):await self.plugin._updater_resume(None)
            self.assertIs(self.plugin._controller_hold,hold)
            self.assertTrue(runtime.is_paused('controller'))
        with patch('controller_maintenance.maintenance_present',return_value=True):
            with self.assertRaises(RuntimeError):await self.plugin._updater_resume(None)
        with patch('controller_maintenance.maintenance_present',return_value=False), \
                patch('controller_firmware_usb_linux.observe_command_interface',return_value=baseline):
            await self.plugin._updater_resume(None)
        self.assertIsNone(self.plugin._controller_hold)
        self.assertIsNone(self.plugin._controller_usb_baseline)
        self.assertFalse(runtime.is_paused('controller'))
        with self.assertRaises(RuntimeError):await self.plugin._updater_resume(None)

    async def test_unclaimed_recovery_cannot_resume_a_timed_out_drain_until_worker_finishes(self):
        baseline=self.usb_baseline()
        caller,release=await self.held_thread()
        original=runtime.drain_controller
        async def short_drain(hold):await original(hold,timeout=.01)
        with patch('controller_firmware_usb_linux.observe_command_interface',return_value=baseline), \
                patch.object(runtime,'drain_controller',side_effect=short_drain), \
                patch('controller_maintenance.maintenance_present',return_value=False):
            with self.assertRaises(asyncio.TimeoutError):await self.plugin._updater_pause()
            hold=self.plugin._controller_hold
            with self.assertRaises(asyncio.TimeoutError):await self.plugin._updater_resume(None)
            self.assertIs(self.plugin._controller_hold,hold)
            self.assertFalse(hold.quiescent)
            self.assertTrue(runtime.is_paused('controller'))
            release.set();await caller
            await self.plugin._updater_resume(None)
        self.assertIsNone(self.plugin._controller_hold)
        self.assertFalse(runtime.is_paused('controller'))

    async def test_closes_all_affected_admission_but_keeps_fan_and_tdp_available(self):
        hold = runtime.pause_controller()
        effect = Mock()
        # These existing modules reach the controller or its InputPlumber
        # composite. Reading the production set here hid unsafe omissions.
        for name in {'controller', 'remap', 'rgb', 'vibration'} | runtime.CONTROLLER_MODULES:
            with self.assertRaises(runtime.ControllerPaused):
                await runtime.offload(name, effect)
        effect.assert_not_called()
        self.assertEqual(await runtime.offload('fan', lambda: 'auto'), 'auto')
        self.assertEqual(await runtime.offload('tdp', lambda: 'unchanged'), 'unchanged')
        await runtime.drain_controller(hold)
        runtime.confirm_controller_quiescent(hold)
        runtime.resume_controller(hold, restored=True)
        await runtime.offload('rgb', effect)
        effect.assert_called_once()

    async def test_private_restore_keeps_ordinary_tasks_and_status_paused(self):
        hold = runtime.pause_controller()
        runtime.confirm_controller_quiescent(hold)
        arrived, run = asyncio.Event(), asyncio.Event()
        async def ordinary():
            arrived.set()
            await run.wait()
            return await runtime.offload('controller', lambda: 'unrelated')
        caller = asyncio.create_task(ordinary())
        await arrived.wait()
        with patch.object(runtime, 'maintenance_present', return_value=True):
            with runtime.controller_restoration(hold, usb_restored=True):
                self.assertTrue(runtime.is_paused('controller'))
                for module in runtime.CONTROLLER_MODULES:
                    self.assertEqual(await runtime.offload(module, lambda: 'readback'), 'readback')
                with self.assertRaises(runtime.ControllerPaused): await self.plugin.rgb_get_status()
                run.set()
                with self.assertRaises(runtime.ControllerPaused): await caller
                with self.assertRaises(runtime.ControllerPaused): runtime.resume_controller(hold, restored=True)
        self.assertTrue(runtime.is_paused('controller'))
        with self.assertRaises(runtime.ControllerPaused): await runtime.offload('controller', lambda: None)
        runtime.resume_controller(hold, restored=True)

    async def test_restore_rejects_wrong_owner_unconfirmed_usb_and_nested_entry(self):
        hold = runtime.pause_controller()
        for owner, proof in ((hold, True), (object(), True), (hold, 1), (hold, False)):
            with self.assertRaises(runtime.ControllerPaused):
                with runtime.controller_restoration(owner, usb_restored=proof): self.fail('Entered before confirmation')
        runtime.confirm_controller_quiescent(hold)
        with runtime.controller_restoration(hold, usb_restored=True):
            with self.assertRaises(runtime.ControllerPaused):
                with runtime.controller_restoration(hold, usb_restored=True): self.fail('Nested owner')

    async def test_restore_failure_revokes_escaped_continuations_and_monitors(self):
        hold = runtime.pause_controller()
        runtime.confirm_controller_quiescent(hold)
        run, monitor_run = asyncio.Event(), asyncio.Event()
        async def late(event):
            await event.wait()
            return await runtime.offload('rgb', lambda: 'late write')
        with self.assertRaisesRegex(RuntimeError, 'readback mismatch'):
            with runtime.controller_restoration(hold, usb_restored=True):
                escaped = asyncio.create_task(late(run))
                monitor = runtime.background_task(late(monitor_run))
                # Even while the restoration lane is active, a newly created
                # lifecycle monitor must not inherit privileged admission.
                monitor_run.set()
                with self.assertRaises(runtime.ControllerPaused): await monitor
                raise RuntimeError('readback mismatch')
        # An old privileged continuation cannot borrow a subsequent lane held
        # by the same pause owner.
        with runtime.controller_restoration(hold, usb_restored=True):
            run.set()
            with self.assertRaises(runtime.ControllerPaused): await escaped
        self.assertFalse(hold.restoring)
        self.assertTrue(runtime.is_paused('rgb'))

    async def test_cancelled_restore_thread_remains_visible_until_drained(self):
        hold = runtime.pause_controller()
        runtime.confirm_controller_quiescent(hold)
        with runtime.controller_restoration(hold, usb_restored=True):
            caller, release = await self.held_thread()
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError): await caller
        with self.assertRaises(runtime.ControllerPaused): runtime.resume_controller(hold, restored=True)
        with self.assertRaises(runtime.ControllerPaused):
            with runtime.controller_restoration(hold, usb_restored=True): self.fail('Restored with live worker')
        release.set()
        await runtime.drain_controller(hold)
        runtime.resume_controller(hold, restored=True)

    async def test_expired_restore_cannot_write_after_resume_or_adopt_a_new_lane(self):
        hold = runtime.pause_controller()
        runtime.confirm_controller_quiescent(hold)
        go = asyncio.Event()
        effect = Mock()
        async def escaped():
            await go.wait()
            for operation in (lambda: runtime.require_available('rgb'),
                              lambda: runtime.controller_restoration(hold, usb_restored=True).__enter__()):
                with self.assertRaises(runtime.ControllerPaused): operation()
            with self.assertRaises(runtime.ControllerPaused):
                await runtime.offload('rgb', effect)
        with runtime.controller_restoration(hold, usb_restored=True):
            stale = asyncio.create_task(escaped())
        runtime.resume_controller(hold, restored=True)
        go.set()
        await stale
        effect.assert_not_called()
        # Also reject an old ordinary operation before replacing its generation.
        token = runtime._operation_generation.set(runtime._controller_generation - 1)
        try:
            new_hold = runtime.pause_controller()
            runtime.confirm_controller_quiescent(new_hold)
            with self.assertRaises(runtime.ControllerPaused):
                with runtime.controller_restoration(new_hold, usb_restored=True): self.fail('Adopted generation')
        finally: runtime._operation_generation.reset(token)
        runtime.resume_controller(new_hold, restored=True)

    async def test_cancelled_caller_does_not_hide_actual_controller_thread(self):
        caller, release = await self.held_thread()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertEqual(len(runtime._workers['controller']), 1)
        begin = asyncio.create_task(self.plugin._begin_controller_maintenance())
        await self.eventually(lambda: runtime.is_paused('controller'))
        await asyncio.sleep(.01)
        self.assertFalse(begin.done())
        self.plugin._controller._stop_capture.assert_not_called()
        release.set()
        hold = await begin
        self.plugin._controller._stop_capture.assert_called_once()
        self.assertTrue(hold.quiescent)
        self.assertFalse(runtime._workers['controller'])
        self.plugin._finish_controller_maintenance(hold, restored=True)

    async def test_timeout_keeps_thread_registered_and_admission_closed(self):
        caller, release = await self.held_thread()
        hold = runtime.pause_controller()
        with self.assertRaises(asyncio.TimeoutError):
            await runtime.drain_controller(hold, timeout=.01)
        self.assertEqual(len(runtime._workers['controller']), 1)
        self.assertFalse(hold.quiescent)
        self.assertTrue(runtime.is_paused('rgb'))
        with self.assertRaises(runtime.ControllerPaused):
            runtime.resume_controller(hold, restored=True)
        release.set()
        await caller
        await runtime.drain_controller(hold)
        runtime.confirm_controller_quiescent(hold)
        runtime.resume_controller(hold, restored=True)

    async def test_repeated_cancellation_waits_for_threads_and_keeps_hold_latched(self):
        caller, release = await self.held_thread()
        begin = asyncio.create_task(self.plugin._begin_controller_maintenance())
        await self.eventually(lambda: runtime.is_paused('controller'))
        for _ in range(3):
            begin.cancel()
            await asyncio.sleep(.005)
        self.assertFalse(begin.done())
        self.assertEqual(len(runtime._workers['controller']), 1)
        release.set()
        await caller
        with self.assertRaises(asyncio.CancelledError):
            await begin
        self.assertTrue(runtime.is_paused('controller'))
        self.assertFalse(self.plugin._controller_hold.quiescent)
        self.plugin._controller._stop_capture.assert_not_called()

    async def test_failed_observer_shutdown_cannot_confirm_quiescence(self):
        self.plugin._controller._stop_capture.side_effect = RuntimeError('Observer still alive')
        with self.assertRaisesRegex(RuntimeError, 'Observer still alive'):
            await self.plugin._begin_controller_maintenance()
        hold = self.plugin._controller_hold
        self.assertFalse(hold.quiescent)
        with self.assertRaises(runtime.ControllerPaused):
            self.plugin._finish_controller_maintenance(hold, restored=True)
        self.assertTrue(runtime.is_paused('controller'))

    async def test_owner_and_explicit_restoration_are_required_to_resume(self):
        hold = await self.plugin._begin_controller_maintenance()
        with self.assertRaises(runtime.ControllerPaused):
            runtime.pause_controller()
        with self.assertRaises(runtime.ControllerPaused):
            self.plugin._finish_controller_maintenance(object(), restored=True)
        for proof in (False, None, 1, 'true'):
            with self.assertRaises(runtime.ControllerPaused):
                self.plugin._finish_controller_maintenance(hold, restored=proof)
        self.plugin._finish_controller_maintenance(hold, restored=True)
        with self.assertRaises(runtime.ControllerPaused):
            runtime.resume_controller(hold, restored=True)

    async def test_call_queued_before_pause_cannot_touch_hardware_after_lock_release(self):
        await self.plugin._rpc_locks['controller'].acquire()
        effect = AsyncMock(return_value={'success': True})
        queued = asyncio.create_task(self.plugin._run_rpc('controller', effect, (), {}))
        await asyncio.sleep(0)
        hold = runtime.pause_controller()
        self.plugin._rpc_locks['controller'].release()
        with self.assertRaises(runtime.ControllerPaused):
            await queued
        effect.assert_not_awaited()
        runtime.confirm_controller_quiescent(hold)
        runtime.resume_controller(hold, restored=True)

    async def test_controller_battery_rpc_and_affected_module_changes_are_blocked(self):
        hold = runtime.pause_controller()
        self.plugin._controller.get_battery_levels = AsyncMock()
        self.plugin._rgb.set_effect = AsyncMock()
        before = copy.deepcopy(self.plugin._module_state)
        for call in (lambda: self.plugin.battery_get_controller_levels(),
                     lambda: self.plugin.rgb_set_effect('solid'),
                     lambda: self.plugin.modules_set_enabled('rgb', False)):
            with self.assertRaises(runtime.ControllerPaused):
                await call()
        self.plugin._controller.get_battery_levels.assert_not_awaited()
        self.plugin._rgb.set_effect.assert_not_awaited()
        self.assertEqual(before, self.plugin._module_state)
        runtime.confirm_controller_quiescent(hold)
        runtime.resume_controller(hold, restored=True)

    async def test_disabled_module_choice_is_not_changed_or_reported_as_maintenance(self):
        self.plugin._module_state['rgb'] = {'enabled': False}
        before = copy.deepcopy(self.plugin._module_state)
        hold = await self.plugin._begin_controller_maintenance()
        status = await self.plugin.modules_get_status()
        self.assertFalse(status['controller']['ready'])
        self.assertTrue(status['controller']['enabled'])
        self.assertTrue(status['controller']['maintenance'])
        self.assertTrue(status['fan']['ready'])
        self.assertNotIn('maintenance', status['rgb'])
        self.assertFalse(status['rgb']['enabled'])
        self.assertEqual(before, self.plugin._module_state)
        self.plugin._finish_controller_maintenance(hold, restored=True)
        self.assertTrue(self.plugin._module_status()['controller']['ready'])

    async def test_fan_safety_disable_remains_available_during_controller_hold(self):
        hold = await self.plugin._begin_controller_maintenance()
        self.plugin._fan.set_enabled = AsyncMock()
        with patch.object(main, 'capture_intent', return_value={}), \
                patch.object(main, 'withdraw', new=AsyncMock(return_value='')) as cleanup:
            result = await self.plugin.modules_set_enabled('fan', False)
        self.plugin._fan.set_enabled.assert_awaited_once_with(False)
        cleanup.assert_awaited_once_with('fan', self.plugin._fan)
        self.assertFalse(result['fan']['enabled'])
        self.assertTrue(result['controller']['maintenance'])
        self.plugin._finish_controller_maintenance(hold, restored=True)

    async def test_pause_closes_before_observer_join_and_joins_own_capture(self):
        self.plugin._controller._stop_capture = controller_backend.Plugin._stop_capture.__get__(self.plugin._controller)
        stop = self.plugin._controller._stop = threading.Event()
        ended = threading.Event()
        def observe():
            stop.wait(2)
            ended.set()
        thread = self.plugin._controller._thread = threading.Thread(target=observe)
        thread.start()
        hold = await self.plugin._begin_controller_maintenance()
        self.assertTrue(ended.is_set())
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.plugin._controller._thread)
        self.assertTrue(hold.quiescent)
        self.plugin._finish_controller_maintenance(hold, restored=True)

    async def test_background_hotplug_does_not_change_vibration_cache_when_paused(self):
        import vibration_backend as vibration
        hold = runtime.pause_controller()
        with patch.object(vibration, '_device_path', '/original'), patch.object(vibration, '_offload', new=AsyncMock()) as effect:
            with self.assertRaises(runtime.ControllerPaused):
                await vibration._handle_device_added('/replacement', 'bind')
            self.assertEqual(vibration._device_path, '/original')
            effect.assert_not_awaited()
        runtime.confirm_controller_quiescent(hold)
        runtime.resume_controller(hold, restored=True)

    async def test_ready_requires_affected_rpc_retry_continuation_to_finish(self):
        import vibration_backend as vibration
        calls = []
        def apply(*args, **kwargs):
            calls.append('apply')
            return False
        with patch.object(vibration, '_forget_device', Mock()), \
                patch.object(vibration, '_active_values', return_value={}), \
                patch.object(vibration, '_apply_settings', apply), \
                patch.object(vibration, '_REAPPLY_STEP_S', .05):
            job = asyncio.create_task(self.plugin.vibe_reapply())
            await self.eventually(lambda: len(calls) == 1 and not runtime._workers.get('vibration'))
            hold = await self.plugin._begin_controller_maintenance()
            self.assertTrue(hold.quiescent)
            self.assertFalse(self.plugin._controller_rpc_jobs)
            self.assertTrue(job.done())
            with self.assertRaises(runtime.ControllerPaused):
                await job
            self.plugin._finish_controller_maintenance(hold, restored=True)
            self.assertEqual(calls, ['apply'])

    async def test_old_background_retry_cannot_adopt_restored_generation(self):
        import vibration_backend as vibration
        calls = []
        def apply(*args, **kwargs):
            calls.append('apply')
            return False
        with patch.object(vibration, '_forget_device', Mock()), \
                patch.object(vibration, '_active_values', return_value={}), \
                patch.object(vibration, '_apply_settings', apply), \
                patch.object(vibration, '_REAPPLY_STEP_S', .1):
            job = asyncio.create_task(self.plugin._vibration.reapply())
            await self.eventually(lambda: len(calls) == 1 and not runtime._workers.get('vibration'))
            hold = await self.plugin._begin_controller_maintenance()
            self.plugin._finish_controller_maintenance(hold, restored=True)
            with self.assertRaises(runtime.ControllerPaused):
                await job
            self.assertEqual(calls, ['apply'])
            # A new operation uses the restored generation.
            self.assertEqual(await runtime.offload('vibration', lambda: 'fresh'), 'fresh')

    async def test_invalid_drain_timeout_cannot_reopen_gate(self):
        hold = runtime.pause_controller()
        for timeout in (True, 0, -1, 31, float('nan'), float('inf'), '5'):
            with self.assertRaises(ValueError):
                await runtime.drain_controller(hold, timeout=timeout)
        self.assertTrue(runtime.is_paused('controller'))

    async def test_watch_created_by_public_rpc_reconciles_in_new_generation(self):
        original_sleep = asyncio.sleep
        calls = []
        async def ticks(seconds):
            await original_sleep(.01 if seconds == 5 else seconds)
        with patch.object(controller_backend, 'CHECK_SECONDS', 0), \
                patch.object(controller_backend, '_state', return_value={'gyro_source': 'left'}), \
                patch.object(controller_backend, '_suspend_offset', return_value=0), \
                patch.object(asyncio, 'sleep', ticks):
            self.plugin._controller._select = Mock(side_effect=lambda *a, **k: calls.append('select'))
            self.plugin._controller._status = Mock(return_value={})
            self.plugin._controller._service_watch.consume_exit = Mock(return_value=False)
            try:
                await self.plugin.controller_set_gyro_source('left')
                await self.eventually(lambda: len(calls) >= 2)
                hold = await self.plugin._begin_controller_maintenance()
                before = len(calls)
                await original_sleep(.03)
                self.assertEqual(len(calls), before)
                self.plugin._finish_controller_maintenance(hold, restored=True)
                await self.eventually(lambda: len(calls) > before)
                self.assertFalse(self.plugin._controller._watch_task.done())
            finally:
                task = self.plugin._controller._watch_task
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_persistent_fence_blocks_fresh_backend_startup_without_changing_choices(self):
        for name in main.MODULE_NAMES:
            setattr(self.plugin, '_' + name, types.SimpleNamespace(_main=AsyncMock(),
                _migration=AsyncMock(), _unload=AsyncMock()))
        self.plugin._module_state['rgb'] = {'enabled': True, 'resume': {'unchanged': True}}
        self.plugin._module_state['controller'] = {'enabled': False, 'pending': True, 'resume': {}}
        before = copy.deepcopy(self.plugin._module_state)
        self.plugin._start_desktop = AsyncMock()
        self.plugin._inspect_guard = AsyncMock()
        self.plugin._recover_pending_module = AsyncMock()
        with patch.object(runtime, 'maintenance_present', return_value=True), \
                patch.object(main, '_migrate_legacy_settings'), \
                patch.object(main, '_reload_component_settings'):
            await self.plugin._main()
            for name in runtime.CONTROLLER_MODULES:
                getattr(self.plugin, '_' + name)._main.assert_not_awaited()
            self.plugin._fan._main.assert_awaited_once()
            self.assertTrue(self.plugin._module_status()['rgb']['maintenance'])
            recovered = [call.args[0] for call in self.plugin._recover_pending_module.await_args_list]
            self.assertTrue(set(recovered).isdisjoint(runtime.CONTROLLER_MODULES))
            self.assertEqual(before, self.plugin._module_state)
            with self.assertRaises(runtime.ControllerPaused): runtime.pause_controller()
        status = self.plugin._module_status()
        self.assertFalse(status['rgb']['ready'])
        self.assertNotIn('maintenance', status['rgb'])
        with self.assertRaisesRegex(RuntimeError, 'could not start'):
            await self.plugin.rgb_get_status()
        # Explicit retry starts a deferred module without running restoration
        # for a backend that never started or owned hardware in this process.
        with patch.object(main, 'restore_intent', new=AsyncMock()):
            await self.plugin.modules_set_enabled('rgb', True)
        self.plugin._rgb._main.assert_awaited_once()
        self.plugin._rgb._unload.assert_not_awaited()
        self.assertTrue(self.plugin._module_status()['rgb']['ready'])


if __name__ == '__main__':
    unittest.main()

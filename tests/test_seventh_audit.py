# SPDX-License-Identifier: BSD-3-Clause
"""Lifecycle recovery must read current ownership without blocking asyncio."""
import asyncio
import threading
import struct
import copy
from contextlib import ExitStack, contextmanager
import unittest
from unittest.mock import AsyncMock, patch

import test_integration
import remap_backend as remap
import module_runtime
import vibration_backend as vibe
from test_remap_backend import BASE_PROFILE


class RemapRestorationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await module_runtime.drain('remap')

    async def test_lifecycle_settings_read_keeps_event_loop_responsive(self):
        loop = asyncio.get_running_loop()
        for operation in ('_unload', '_uninstall'):
            observed = []
            def read():
                heartbeat = threading.Event()
                loop.call_soon_threadsafe(heartbeat.set)
                observed.append(heartbeat.wait(.5))
                return dict(remap.DEFAULT_STATE)
            with self.subTest(operation=operation), \
                    patch.object(remap, '_watch_task', None), \
                    patch.object(remap, '_load_state', side_effect=read), \
                    patch.object(remap, '_restore_if_owned_locked', return_value=True):
                await getattr(remap.Plugin(), operation)()
            self.assertEqual(observed, [True], 'Settings recovery or its lock must not block the event loop.')

    async def test_restoration_uses_ownership_committed_by_last_worker(self):
        for operation in ('_unload', '_uninstall'):
            with self.subTest(operation=operation):
                entered, release = threading.Event(), threading.Event()
                saved = {'desktop_action': 'f1'}
                restored = []
                def last_worker():
                    with remap._operation_lock:
                        entered.set()
                        if not release.wait(3):
                            raise RuntimeError('test worker timeout')
                        saved['desktop_action'] = 'f2'
                with patch.object(remap, '_watch_task', None), \
                        patch.object(remap, '_load_state', side_effect=lambda: dict(saved)), \
                        patch.object(remap, '_restore_if_owned_locked', side_effect=lambda state: restored.append(state)):
                    worker = asyncio.create_task(asyncio.to_thread(last_worker))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    cleanup = asyncio.create_task(getattr(remap.Plugin(), operation)())
                    try:
                        done, _ = await asyncio.wait([cleanup], timeout=.05)
                        self.assertFalse(done)
                    finally:
                        release.set()
                        await asyncio.gather(worker, cleanup)
                self.assertEqual(restored, [{'desktop_action': 'f2'}], 'Restoration must use ownership read after the last operation completes.')

    async def test_unload_restores_mapping_applied_by_finishing_worker(self):
        entered, release = threading.Event(), threading.Event()
        saved = remap._sanitize_state({'enabled': True, 'baseline_profile': BASE_PROFILE,
                                      'desktop_action': 'f1', 'page_action': 'default'})
        live = {'profile': remap._build_profile(BASE_PROFILE, saved)}
        def last_worker():
            with remap._operation_lock:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('test worker timeout')
                saved['desktop_action'] = 'f2'
                live['profile'] = remap._build_profile(BASE_PROFILE, saved)
        def restore(_device, profile):
            live['profile'] = profile
            return profile
        with patch.object(remap, '_watch_task', None), \
                patch.object(remap, '_load_state', side_effect=lambda: copy.deepcopy(saved)), \
                patch.object(remap, '_find_device', return_value=('/org/test', 'Legion Go 2')), \
                patch.object(remap, '_get_profile', side_effect=lambda _device: live['profile']), \
                patch.object(remap, '_load_profile', side_effect=restore):
            worker = asyncio.create_task(asyncio.to_thread(last_worker))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            cleanup = asyncio.create_task(remap.Plugin()._unload())
            try:
                done, _ = await asyncio.wait([cleanup], timeout=.05)
                self.assertFalse(done)
            finally:
                release.set()
                await asyncio.gather(worker, cleanup)
        self.assertEqual(live['profile'], BASE_PROFILE)


class VibrationEndpointTests(unittest.TestCase):
    def test_valid_cache_skips_scan_and_capability_probe(self):
        node = '/dev/input/event9'
        with patch.object(vibe, '_ff_device_cache', {'node': node}), \
                patch.object(vibe.os.path, 'exists', return_value=True), \
                patch.object(vibe, '_get_device_path', return_value='/sys/hid/0003:17EF:61EB.0001'), \
                patch.object(vibe, '_node_ids', return_value=('17ef', '61eb')), \
                patch.object(vibe, '_event_nodes') as scan, \
                patch.object(vibe, '_has_rumble') as probe:
            self.assertEqual(vibe._find_ff_device(), node)
            scan.assert_not_called()
            probe.assert_not_called()

    def test_unknown_hid_identity_never_falls_back_to_another_pad(self):
        with patch.object(vibe, '_ff_device_cache', {'node': None}), \
                patch.object(vibe, '_get_device_path', return_value=None), \
                patch.object(vibe, '_event_nodes', return_value=['/dev/input/event9']), \
                patch.object(vibe, '_has_rumble', return_value=True):
            self.assertIsNone(vibe._find_ff_device())

    def test_cached_event_number_reused_by_foreign_pad_is_rejected(self):
        nodes = ['/dev/input/event4', '/dev/input/event9']
        ids = {nodes[0]: ('045e', '02ea'), nodes[1]: ('17ef', '61eb')}
        with patch.object(vibe, '_ff_device_cache', {'node': nodes[0]}), \
                patch.object(vibe.os.path, 'exists', return_value=True), \
                patch.object(vibe, '_get_device_path', return_value='/sys/hid/0003:17EF:61EB.0001'), \
                patch.object(vibe, '_event_nodes', return_value=nodes), \
                patch.object(vibe, '_node_ids', side_effect=ids.get), \
                patch.object(vibe, '_has_rumble', return_value=True):
            self.assertEqual(vibe._find_ff_device(), nodes[1])

    def test_probe_opens_only_matching_controller_endpoints(self):
        nodes = ['/dev/input/event4', '/dev/input/event9']
        ids = {nodes[0]: ('045e', '02ea'), nodes[1]: ('17ef', '61eb')}
        with patch.object(vibe, '_ff_device_cache', {'node': None}), \
                patch.object(vibe, '_get_device_path', return_value='/sys/hid/0003:17EF:61EB.0001'), \
                patch.object(vibe, '_event_nodes', return_value=nodes), \
                patch.object(vibe, '_node_ids', side_effect=ids.get), \
                patch.object(vibe, '_has_rumble', return_value=True) as probe:
            self.assertEqual(vibe._find_ff_device(), nodes[1])
            probe.assert_called_once_with(nodes[1])


class VibrationDescriptorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # These cases mock os.open/close for a force-feedback descriptor. The
        # independent POSIX maintenance check must not consume that fake FD.
        fence = patch.object(module_runtime, 'maintenance_present', return_value=False)
        fence.start()
        self.addCleanup(fence.stop)

    async def asyncTearDown(self):
        await module_runtime.drain('vibration')

    @contextmanager
    def playable(self, *, pause=None, start_error=False, id_error=False):
        def ioctl(_fd, request, payload):
            if request == vibe._EVIOCGID:
                if id_error:
                    raise OSError('descriptor disappeared')
                payload[:] = struct.pack('HHHH', 3, 0x17ef, 0x61eb, 1)
            elif request == vibe._EVIOCSFF:
                struct.pack_into('<h', payload, 2, 1)
            return 0
        def output(_fd, payload):
            if start_error and struct.unpack('<qqHHi', payload)[-1] == 1:
                raise OSError('effect start failed')
            return len(payload)
        with ExitStack() as stack:
            for name, value in {'_ff_busy': False, '_ff_device_cache': {'node': '/dev/input/event9'}}.items():
                stack.enter_context(patch.object(vibe, name, value))
            stack.enter_context(patch.object(vibe, '_active_values', return_value={vibe.PKEY_LEVEL: 2}))
            stack.enter_context(patch.object(vibe, '_find_ff_device', return_value='/dev/input/event9'))
            stack.enter_context(patch.object(vibe, '_get_device_path', return_value='/sys/hid/0003:17EF:61EB.0001'))
            stack.enter_context(patch.object(vibe.os, 'open', return_value=43))
            close = stack.enter_context(patch.object(vibe.os, 'close'))
            writes = stack.enter_context(patch.object(vibe.os, 'write', side_effect=output))
            calls = stack.enter_context(patch.object(vibe.fcntl, 'ioctl', side_effect=ioctl, create=True))
            stack.enter_context(patch.object(vibe.time, 'sleep', side_effect=pause))
            yield close, writes, calls

    async def test_matching_descriptor_plays_and_cleans_effect_off_event_loop(self):
        loop = asyncio.get_running_loop()
        observed = []
        def pause(_seconds):
            heartbeat = threading.Event()
            loop.call_soon_threadsafe(heartbeat.set)
            observed.append(heartbeat.wait(.5))
        with self.playable(pause=pause) as (close, writes, calls):
            result = await vibe.Plugin().test_vibration(100)
            self.assertTrue(result['success'], result)
            self.assertEqual(observed, [True])
            self.assertEqual([struct.unpack('<qqHHi', call.args[1])[-1] for call in writes.call_args_list], [1, 0])
            self.assertEqual([call.args[1] for call in calls.call_args_list], [vibe._EVIOCGID, vibe._EVIOCSFF, vibe._EVIOCRMFF])
            close.assert_called_once_with(43)
            self.assertFalse(vibe._ff_busy)

    async def test_failed_start_still_stops_erases_and_closes_effect(self):
        with self.playable(start_error=True) as (close, writes, calls):
            result = await vibe.Plugin().test_vibration(100)
            self.assertFalse(result['success'])
            self.assertEqual(writes.call_count, 2)
            self.assertEqual(calls.call_args_list[-1].args, (43, vibe._EVIOCRMFF, 1))
            close.assert_called_once_with(43)

    async def test_identity_read_failure_closes_without_hardware_output(self):
        with self.playable(id_error=True) as (close, writes, calls):
            result = await vibe.Plugin().test_vibration(100)
            self.assertFalse(result['success'])
            writes.assert_not_called()
            self.assertEqual(calls.call_count, 1)
            close.assert_called_once_with(43)

    async def test_repeated_cancellation_waits_for_effect_cleanup_and_keeps_busy_guard(self):
        entered, release = threading.Event(), threading.Event()
        def pause(_seconds):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test effect timeout')
        with self.playable(pause=pause) as (close, writes, calls):
            task = asyncio.create_task(vibe.Plugin().test_vibration(100))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertTrue(vibe._ff_busy)
                self.assertFalse((await vibe.Plugin().test_vibration(100))['success'])
                close.assert_not_called()
            finally:
                release.set()
                result = await asyncio.gather(task, return_exceptions=True)
            self.assertIsInstance(result[0], asyncio.CancelledError)
            self.assertFalse(vibe._ff_busy)
            self.assertEqual(writes.call_count, 2)
            self.assertEqual(calls.call_args_list[-1].args, (43, vibe._EVIOCRMFF, 1))
            close.assert_called_once_with(43)

    async def test_test_rechecks_open_descriptor_before_upload_or_output(self):
        def ioctl(_fd, request, payload):
            if request == 0x80084502:  # EVIOCGID, struct input_id from Linux UAPI.
                payload[:] = struct.pack('HHHH', 3, 0x045e, 0x02ea, 1)
            elif request == vibe._EVIOCSFF:
                struct.pack_into('<h', payload, 2, 1)
            return 0
        with patch.object(vibe, '_ff_busy', False), \
                patch.object(vibe, '_ff_device_cache', {'node': '/dev/input/event9'}), \
                patch.object(vibe, '_active_values', return_value={vibe.PKEY_LEVEL: 2}), \
                patch.object(vibe, '_find_ff_device', return_value='/dev/input/event9'), \
                patch.object(vibe, '_get_device_path', return_value='/sys/hid/0003:17EF:61EB.0001'), \
                patch.object(vibe.os, 'open', return_value=43), \
                patch.object(vibe.os, 'close') as close, \
                patch.object(vibe.os, 'write', return_value=24) as output, \
                patch.object(vibe.fcntl, 'ioctl', side_effect=ioctl, create=True) as calls, \
                patch.object(vibe.asyncio, 'sleep', new=AsyncMock()), \
                patch.object(vibe.time, 'sleep'):
            result = await vibe.Plugin().test_vibration(100)
        self.assertFalse(result['success'], 'A reused descriptor must be rejected before its device receives output.')
        output.assert_not_called()
        self.assertNotIn(vibe._EVIOCSFF, [call.args[1] for call in calls.call_args_list])
        close.assert_called_once_with(43)

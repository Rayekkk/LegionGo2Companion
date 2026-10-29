# SPDX-License-Identifier: BSD-3-Clause
"""Remap ownership, restoration and boot settling against an in-memory InputPlumber."""
import asyncio
import copy
import subprocess
import threading
import types
import unittest
from unittest.mock import AsyncMock, patch

import support
import remap_backend as remap
from test_remap_backend import BASE_PROFILE, MemorySettings
import module_runtime


class RemapRecoveryTests(unittest.TestCase):
    def test_remap_unchanged_profile_is_not_reloaded(self):
        state = remap._sanitize_state({"enabled": True, "baseline_profile": BASE_PROFILE})
        current = remap._build_profile(BASE_PROFILE, state)
        with patch.object(remap, "settings", MemorySettings(state)), \
             patch.object(remap, "_find_device", return_value=("device", "Go2")), \
             patch.object(remap, "_get_profile", return_value=current), \
             patch.object(remap._service_watch, "capture"), \
             patch.object(remap, "_load_profile", return_value=current) as load:
            remap._repair_sync()
        load.assert_not_called()

    def test_remap_preserves_unrelated_edit_under_same_profile_name(self):
        state = remap._sanitize_state({"enabled": True, "baseline_profile": BASE_PROFILE})
        original = remap._build_profile(BASE_PROFILE, state)
        externally_edited = original.replace("LeftPaddle1", "RightPaddle1")
        self.assertTrue(remap._profile_matches(externally_edited, state))
        with patch.object(remap, "settings", MemorySettings(state)), \
             patch.object(remap, "_find_device", return_value=("device", "Go2")), \
             patch.object(remap, "_get_profile", return_value=externally_edited), \
             patch.object(remap._service_watch, "capture"), \
             patch.object(remap, "_load_profile", side_effect=lambda p, data: data) as load:
            remap._repair_sync()
        load.assert_not_called()

    def test_remap_malformed_action_uses_default(self):
        for invalid in ([], {}, None, 3, False):
            self.assertEqual(remap._sanitize_state({"desktop_action": invalid})["desktop_action"],
                             remap.DEFAULT_ACTIONS["desktop"])

    def test_remap_repair_merges_live_unmanaged_mappings(self):
        state = remap._sanitize_state({"enabled": True, "baseline_profile": BASE_PROFILE})
        current = remap._build_profile(BASE_PROFILE, state).replace("LeftPaddle1", "RightPaddle1")
        current = current.replace("button: Screenshot", "button: South")
        with patch.object(remap, "_find_device", return_value=("device", "Go2")), \
             patch.object(remap, "_get_profile", return_value=current), \
             patch.object(remap, "_load_profile", side_effect=lambda p, text: text) as load:
            remap._apply_state(state, adopt_external=False)
        self.assertIn("RightPaddle1", load.call_args.args[1])
        self.assertTrue(remap._profile_matches(load.call_args.args[1], state))

    def test_remap_settles_at_boot_then_returns_to_slow_checks(self):
        now = [0.0];delays=[]
        async def sleep(delay):
            delays.append(delay)
            if not 0 < delay <= 10:raise asyncio.CancelledError
            now[0] += delay
            if now[0] > 300:raise asyncio.CancelledError
        observed = []
        async def offload(fn):
            observed.append(now[0])
        with patch.object(remap.asyncio, "sleep", side_effect=sleep), \
             patch.object(remap, "time", types.SimpleNamespace(monotonic=lambda: now[0])), \
             patch.object(remap, "_resume_detected", return_value=False), \
             patch.object(remap._service_watch, "consume_exit", return_value=False), \
             patch.object(remap.asyncio, "to_thread", side_effect=offload):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(remap._watch_loop())
        self.assertTrue(delays)
        self.assertTrue(all(0 < delay <= 10 for delay in delays),delays)
        boot = [at for at in observed if at < 30]
        stable = [at for at in observed if at >= 45]
        self.assertGreaterEqual(len(boot),2);self.assertLessEqual(len(boot),7)
        self.assertLessEqual(boot[0],10)
        self.assertGreaterEqual(len(stable),3)
        self.assertTrue(all(45 <= b-a <= 75 for a,b in zip(stable,stable[1:])),observed)

    def test_remap_rpc_rejects_unhashable_arguments(self):
        plugin = remap.Plugin()
        with patch.object(plugin, "get_status", new_callable=AsyncMock, return_value={}):
            for button, action in (([], "default"), ("desktop", {})):
                self.assertFalse(asyncio.run(plugin.set_action(button,action))["ok"])


class RemapScalarTests(unittest.TestCase):
    def run_cycle(self, baseline, append=False):
        state = remap._sanitize_state({'enabled': False, 'desktop_action': 'f1', 'page_action': 'f2'})
        store = MemorySettings(state)
        current = [baseline]
        def load(_path, profile):
            current[0] = profile
            return profile
        watcher = remap.ProcessWatch()
        with patch.object(remap, 'settings', store), \
                patch.object(remap, '_service_watch', watcher), \
                patch.object(watcher, 'capture'), \
                patch.object(remap, '_inputplumber_version', return_value='fixture'), \
                patch.object(remap, '_find_device', return_value=('/fixture', 'Legion Go 2')), \
                patch.object(remap, '_get_profile', side_effect=lambda _: current[0]), \
                patch.object(remap, '_load_profile', side_effect=load):
            result = remap._mutate_sync({'enabled': True})
            self.assertTrue(result['enabled'], result)
            self.assertEqual(remap._profile_name(current[0]), remap.PROFILE_NAME)
            self.assertNotIn('\n  User\n', current[0])
            self.assertEqual(store.data['state']['baseline_profile'], baseline)
            if append:
                current[0] += '- name: External LeftTop\n  source_event:\n    gamepad:\n      button: LeftTop\n  target_events:\n  - keyboard: KeyF12\n'
            remap._repair_sync()
            self.assertEqual(store.data['state']['baseline_profile'], baseline)
            result = remap._mutate_sync({'enabled': False})
            self.assertFalse(result['enabled'], result)
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


NOT_RUNNING = remap.RemapError(
    "Failed to introspect object / of service org.shadowblip.InputPlumber: The name is not activatable")
STALLED = remap.RemapError("InputPlumber did not respond: Command '['/usr/bin/busctl']' timed out after 5 seconds")


class FakeInputPlumber:
    """In-memory InputPlumber; `normalize` re-serializes a loaded profile equivalently."""

    def __init__(self, normalize=lambda text: text):
        self.profile = BASE_PROFILE
        self.normalize = normalize
        self.down = None  # the busctl error every call fails with, or None

    def find(self):
        if self.down:
            raise self.down
        return "/org/test", "Lenovo Legion Go 2"

    def load(self, _path, text):
        self.profile = self.normalize(text)
        return self.profile

    def run(self, scenario):
        with patch.object(remap, "settings", MemorySettings()),              patch.object(remap, "_find_device", side_effect=self.find),              patch.object(remap, "_get_profile", side_effect=lambda _path: self.profile),              patch.object(remap, "_load_profile", side_effect=self.load),              patch.object(remap._service_watch, "capture"),              patch.object(remap._service_watch, "clear"),              patch.object(remap, "_inputplumber_version", return_value="test"):
            return scenario()


class RemapDisableTests(unittest.TestCase):
    def test_disable_while_inputplumber_unavailable_is_not_reverted_by_watchdog(self):
        ip = FakeInputPlumber()

        def scenario():
            remap._mutate_sync({"enabled": True, "desktop_action": "insert"})
            ip.down = NOT_RUNNING
            remap._mutate_sync({"enabled": False})
            state = remap._load_state()
            # InputPlumber returns with its stock profile; the watchdog must leave it alone.
            ip.down = None
            ip.profile = BASE_PROFILE
            self.assertTrue(remap._repair_sync())
            return state
        state = ip.run(scenario)
        self.assertFalse(state["enabled"])
        self.assertIsNone(state["baseline_profile"])
        self.assertEqual(ip.profile, BASE_PROFILE)

    def test_disable_is_refused_while_inputplumber_only_stalls(self):
        # A timeout does not prove InputPlumber is gone; our mappings may still be live.
        ip = FakeInputPlumber()

        def scenario():
            remap._mutate_sync({"enabled": True, "desktop_action": "insert"})
            ip.down = STALLED
            with self.assertRaisesRegex(remap.RemapError, "did not respond"):
                remap._mutate_sync({"enabled": False})
            return remap._load_state()
        state = ip.run(scenario)
        self.assertTrue(state["enabled"])
        self.assertIsNotNone(state["baseline_profile"])

    def test_only_proof_of_absence_makes_the_controller_unreachable(self):
        tree = "/\n/org/shadowblip/InputPlumber/CompositeDevice0\n"
        cases = [(NOT_RUNNING, None, False), (STALLED, None, True),
                 (None, remap.RemapError("Failed to get property Name on interface "
                                         "org.shadowblip.Input.CompositeDevice: Unknown object "
                                         "'/org/shadowblip/InputPlumber/CompositeDevice0'"), False),
                 (None, STALLED, True)]
        for tree_error, property_error, reachable in cases:
            with self.subTest(tree=tree_error, property=property_error), \
                    patch.object(remap.module_runtime, "is_legion_go_2", return_value=True), \
                    patch.object(remap, "_run_busctl", side_effect=tree_error, return_value=tree), \
                    patch.object(remap, "_get_property", side_effect=property_error):
                self.assertIs(remap._device_reachable(), reachable)

    def test_disable_is_still_refused_when_a_reachable_restore_fails(self):
        ip = FakeInputPlumber()

        def scenario():
            remap._mutate_sync({"enabled": True})
            with patch.object(remap, "_restore_confirmed", return_value=False):
                with self.assertRaises(remap.RemapError):
                    remap._mutate_sync({"enabled": False})
            return remap._load_state()["enabled"]
        self.assertTrue(ip.run(scenario))

    def test_disable_accepts_an_equivalent_reserialized_restore(self):
        ip = FakeInputPlumber(normalize=lambda text: text.replace(
            "- name: Keyboard\n", "- name: 'Keyboard'\n"))

        def scenario():
            remap._mutate_sync({"enabled": True, "desktop_action": "insert"})
            remap._mutate_sync({"enabled": False})
            return remap._load_state()["enabled"]
        self.assertFalse(ip.run(scenario))
        self.assertEqual(remap._profile_name(ip.profile), "Custom User Profile")

    def test_restore_check_rejects_a_changed_mapping(self):
        changed = BASE_PROFILE.replace("LeftPaddle1", "RightPaddle1")
        self.assertTrue(remap._restore_confirmed(BASE_PROFILE, BASE_PROFILE))
        self.assertFalse(remap._restore_confirmed(changed, BASE_PROFILE))
        self.assertFalse(remap._restore_confirmed(
            BASE_PROFILE.replace("Custom User Profile", "Other"), BASE_PROFILE))


class RemapRestorationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await module_runtime.drain('remap')

    async def test_unload_restore_lost_to_system_shutdown_is_not_a_warning(self):
        import collections, json, diagnostics
        for stopping, expected in ((True, [('restore', 'info')]),
                                   (False, [('worker_failed', 'warning'), ('restore_failed', 'warning')])):
            records = []
            with self.subTest(stopping=stopping), \
                    patch.object(diagnostics, '_sink', lambda text, level: records.append(json.loads(text))), \
                    patch.object(diagnostics, '_seen', collections.OrderedDict()), \
                    patch.object(remap, '_watch_task', None), \
                    patch.object(remap, '_load_state', return_value=dict(remap.DEFAULT_STATE)), \
                    patch.object(remap, '_restore_if_owned_locked',
                                 side_effect=remap.RemapError('Remote peer disconnected')), \
                    patch.object(remap, '_system_stopping', return_value=stopping):
                await remap.Plugin()._unload()
                await module_runtime.drain('remap')
            self.assertEqual([(row['event'], row['level']) for row in records if row['component'] == 'remap'], expected)

    def test_system_stopping_reads_systemd_state(self):
        for output, expected in (('stopping\n', True), ('running\n', False), ('degraded\n', False)):
            with patch.object(remap.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, output)):
                self.assertIs(remap._system_stopping(), expected)
        with patch.object(remap.subprocess, 'run', side_effect=OSError):
            self.assertFalse(remap._system_stopping())

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


if __name__ == '__main__':
    unittest.main()

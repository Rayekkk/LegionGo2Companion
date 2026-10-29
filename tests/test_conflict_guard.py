import asyncio
import inspect
import json
import os
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import stat

import support  # Install the Decky/settings stubs before importing main.
import main
import module_runtime
from conflict_guard import installed_conflicts
import test_modules as fixtures
import fan_backend
import conflict_guard


def decoder_nesting_failure(manifest):
    # Python versions have different decoder nesting limits. Inject the actual
    # failure at the decoder boundary while leaving other JSON reads intact.
    original_loads = json.loads

    def decode(text, *args, **kwargs):
        if text == manifest:
            raise RecursionError("JSON decoder nesting limit exceeded")
        return original_loads(text, *args, **kwargs)

    return patch("conflict_guard.json.loads", side_effect=decode)


class DetectionTests(unittest.TestCase):
    def test_only_three_public_plugins_block_including_renamed_installs(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); current = root / "LegionGo2Companion"; current.mkdir()
            for name in ("HueSync", "WifiOptimizerGo2", "WiFi Optimizer Go 2"):
                directory = root / name; directory.mkdir()
                (directory / "plugin.json").write_text(json.dumps({"name": name}))
            self.assertEqual(installed_conflicts(current), [])
            (root / "LeGoTDP").mkdir()
            renamed = root / "renamed-plugin"; renamed.mkdir()
            (renamed / "plugin.json").write_text(json.dumps({"name": "LeGo Vibe Control"}))
            (root / "LeGo2 Brightness Fix").mkdir()
            self.assertEqual(installed_conflicts(current),
                             ["LeGo Vibe Control", "LeGo2 Brightness Fix", "LeGoTDP"])

    def test_unchanged_sibling_manifests_are_not_reopened_on_every_scan(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); current = root / "LegionGo2Companion"; current.mkdir()
            other = root / "renamed-plugin"; other.mkdir()
            manifest = other / "plugin.json"; manifest.write_text(json.dumps({"name": "OtherPlugin"}))
            with patch("conflict_guard._manifest_name", wraps=conflict_guard._manifest_name) as reads:
                self.assertEqual(installed_conflicts(current), [])
                self.assertEqual(installed_conflicts(current), [])
                self.assertEqual(reads.call_count, 1)
                manifest.write_text(json.dumps({"name": "LeGo Vibe Control"}))
                self.assertEqual(installed_conflicts(current), ["LeGo Vibe Control"])
                self.assertEqual(reads.call_count, 2)

    def test_settings_siblings_are_not_installed_plugins(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); current = root / "plugins" / "LegionGo2Companion"
            current.mkdir(parents=True)
            (root / "settings" / "LeGoTDP").mkdir(parents=True)
            self.assertEqual(installed_conflicts(current), [])

    def test_linked_companion_scans_installed_siblings_not_source_checkout(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            current = root / "plugins" / "LegionGo2Companion"
            current.mkdir(parents=True)
            (current.parent / "LeGoTDP").mkdir()
            checkout = root / "sources" / "LegionGo2Companion"
            checkout.mkdir(parents=True)
            original_realpath = os.path.realpath
            def resolve_link(path):
                return str(checkout) if str(path) == str(current) else original_realpath(path)
            # Windows CI may not permit creating symlinks. Emulate only their
            # resolution; the installed and source directories remain real.
            with patch("conflict_guard.os.path.realpath", side_effect=resolve_link):
                self.assertEqual(installed_conflicts(current), ["LeGoTDP"])

    def test_incomplete_unrelated_manifest_is_diagnostic_not_global_conflict(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); current = root / "LegionGo2Companion"; current.mkdir()
            other = root / "renamed"; other.mkdir()
            (other / "plugin.json").write_text('{"name":')
            with patch('conflict_guard.event',create=True) as log:
                self.assertEqual(installed_conflicts(current),[])
            log.assert_called_once()
            # An unrelated failure must not hide a confirmed renamed predecessor.
            known=root/'renamed-predecessor';known.mkdir()
            (known/'plugin.json').write_text(json.dumps({'name':'LeGoTDP'}))
            self.assertEqual(installed_conflicts(current),['LeGoTDP'])

    def test_deep_unrelated_manifest_does_not_certify_a_hardware_conflict(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); current = root / "LegionGo2Companion"; current.mkdir()
            other = root / "unrelated-plugin"; other.mkdir()
            text = '{"name":"OtherPlugin","metadata":' + '[' * 2000 + '0' + ']' * 2000 + '}'
            (other / "plugin.json").write_text(text)
            with decoder_nesting_failure(text), patch('conflict_guard.event',create=True) as log:
                self.assertEqual(installed_conflicts(current),[])
            log.assert_called_once()

    def test_bad_manifest_does_not_stop_scan_before_a_canonical_conflict(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);current=root/'Companion';current.mkdir()
            unrelated=root/'unrelated';unrelated.mkdir()
            (unrelated/'plugin.json').write_text('not JSON')
            (root/'LeGoTDP').mkdir()
            self.assertEqual(installed_conflicts(current),['LeGoTDP'])

    def test_permission_error_of_unrelated_manifest_is_local_and_logged(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);current=root/'Companion';current.mkdir()
            unrelated=root/'unrelated';unrelated.mkdir()
            (unrelated/'plugin.json').write_text('{}')
            real_open=os.open
            def read(path,*args,**kwargs):
                if Path(path)==unrelated/'plugin.json':raise PermissionError('temporary access denied')
                return real_open(path,*args,**kwargs)
            with patch('conflict_guard.os.open',side_effect=read),patch('conflict_guard.event',create=True) as log:
                self.assertEqual(installed_conflicts(current),[])
            log.assert_called_once()


class GateTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        plugin = support.LegacyPlugin()
        self.components = []
        for name in ("tdp", "vibration", "display", "wifi", "rgb", "remap", "battery", "controller"):
            component = types.SimpleNamespace(_main=AsyncMock(), _migration=AsyncMock(),
                                              _unload=AsyncMock(), _uninstall=AsyncMock())
            setattr(plugin, "_" + name, component); self.components.append(component)
        return plugin

    async def test_deep_unrelated_manifest_keeps_healthy_modules_and_rpcs_running(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); current = root / "LegionGo2Companion"; current.mkdir()
            other = root / "unrelated-plugin"; other.mkdir()
            manifest = other / "plugin.json"
            manifest.write_text(json.dumps({"name": "OtherPlugin"}))
            plugin = self.make_plugin()
            with patch.object(main, "_installed_standalone_plugins",
                              side_effect=lambda: installed_conflicts(current)):
                try:
                    await plugin._main()
                    text = '{"name":"OtherPlugin","metadata":' + '[' * 2000 + '0' + ']' * 2000 + '}'
                    manifest.write_text(text)
                    with decoder_nesting_failure(text):
                        await plugin._check_guard()
                    self.assertFalse(plugin._guard_blocked)
                    self.assertEqual(plugin._guard_error,"")
                    self.assertTrue(plugin._modules_started)
                    for component in self.components:
                        component._unload.assert_not_awaited()
                    plugin._rgb.set_brightness=AsyncMock(return_value={"ok":True})
                    with decoder_nesting_failure(text):
                        self.assertEqual(await plugin.rgb_set_brightness(20),{"ok":True})
                    plugin._rgb.set_brightness.assert_awaited_once_with(20)
                    manifest.write_text(json.dumps({"name": "OtherPlugin"}))
                    status = await plugin.get_version()
                    self.assertFalse(status["blocked"])
                    self.assertFalse(status["restart_required"])
                finally:
                    await plugin._unload()

    async def test_guard_changes_publish_fresh_status_once_and_keep_restart_latch(self):
        plugin = self.make_plugin()
        with patch.object(main, '_installed_standalone_plugins',
                          side_effect=[[], ['LeGoTDP'], ['LeGoTDP'], []]) as scan, \
             patch.object(main, '_plugin_version', return_value='1.0.4') as version, \
             patch.object(main.decky, 'emit', new=AsyncMock()) as emit:
            for _ in range(4):
                await plugin._inspect_guard()
        self.assertEqual(scan.call_count, 4)
        self.assertEqual(version.call_count, 2)
        self.assertEqual(emit.await_count, 2)
        blocked, cleared = [call.args[1] for call in emit.await_args_list]
        self.assertEqual(blocked['standalone_plugins'], ['LeGoTDP'])
        self.assertTrue(blocked['blocked'])
        self.assertFalse(blocked['restart_required'])
        self.assertEqual(cleared['standalone_plugins'], [])
        self.assertTrue(cleared['blocked'])
        self.assertTrue(cleared['restart_required'])
        self.assertIn('modules', cleared)

    async def test_blocked_start_migration_unload_and_uninstall_never_enter_modules(self):
        plugin = self.make_plugin()
        with patch.object(main, "_installed_standalone_plugins", return_value=["LeGoTDP"]):
            await plugin._migration(); await plugin._main()
            status = await plugin.get_version()
            self.assertTrue(status["blocked"])
            await plugin._unload(); await plugin._uninstall()
        for c in self.components:
            for stage in ("_main", "_migration", "_unload", "_uninstall"):
                getattr(c, stage).assert_not_awaited()

    async def test_every_control_rpc_is_rejected_but_firmware_outcome_remains_visible(self):
        plugin = self.make_plugin(); checked = []
        plugin._firmware = Mock(status=Mock(return_value={'job': {'phase': 'installing'}}))
        with patch.object(main, "_installed_standalone_plugins", return_value=["LeGo Vibe Control"]):
            for name, fn in vars(main.Plugin).items():
                if name.startswith("_") or name == "get_version" or not inspect.iscoroutinefunction(fn):
                    continue
                required = [p for p in inspect.signature(fn).parameters.values()
                            if p.name != "self" and p.default is inspect.Parameter.empty]
                if name == 'firmware_get_status':
                    self.assertEqual(await getattr(plugin, name)(), {'job': {'phase': 'installing'}})
                    continue
                with self.assertRaisesRegex(RuntimeError, "All Companion modules are paused"):
                    await getattr(plugin, name)(*[None for _ in required])
                checked.append(name)
        self.assertGreater(len(checked), 50)
        plugin._firmware.status.assert_called_once()
        plugin._firmware.check.assert_not_called()

    async def test_live_conflict_stops_all_modules_and_latches_until_process_restart(self):
        plugin = self.make_plugin(); conflicts = []
        with patch.object(main, "_installed_standalone_plugins", side_effect=lambda: list(conflicts)):
            await plugin._main()
            for c in self.components: c._main.assert_awaited_once()
            conflicts.append("LeGo2 Brightness Fix")
            await plugin._check_guard()
            for c in self.components: c._unload.assert_awaited_once()
            conflicts.clear()
            await plugin._check_guard()  # The next periodic scan.
            status = await plugin.get_version()
            self.assertTrue(status["blocked"]); self.assertTrue(status["restart_required"]); self.assertTrue(status["latched"])
            await plugin._main()
            for c in self.components: c._main.assert_awaited_once()
            await plugin._unload(); await plugin._uninstall()
            for c in self.components:
                c._unload.assert_awaited_once(); c._uninstall.assert_not_awaited()

    async def test_conflict_withdrawal_does_not_wait_for_the_firmware_worker(self):
        plugin = self.make_plugin(); release = threading.Event(); unloaded = asyncio.Event()
        plugin._rgb._unload = AsyncMock(side_effect=unloaded.set)
        plugin._firmware = Mock(close=Mock(side_effect=lambda: release.wait(5)))
        plugin._modules_started = plugin._guard_checked = True
        with patch.object(main, "_installed_standalone_plugins", return_value=["LeGoTDP"]),              patch.object(main.decky, "emit", new=AsyncMock()):
            task = asyncio.create_task(plugin._check_guard())
            try:
                await asyncio.wait_for(unloaded.wait(), 1)
                self.assertFalse(task.done())
            finally:
                release.set(); await task
        plugin._firmware.close.assert_called()

    async def test_rpc_before_main_takes_the_lock_waits_for_module_startup(self):
        plugin = self.make_plugin(); plugin._module_state = {"rgb": {"enabled": False}}
        with patch.object(main, "_installed_standalone_plugins", return_value=[]),              patch.object(main, "restore_intent", AsyncMock()), patch.object(plugin, "_start_desktop", AsyncMock()):
            startup = asyncio.create_task(plugin._main())
            await plugin.modules_set_enabled("rgb", True)  # The first frontend RPC.
            await startup
        plugin._rgb._main.assert_awaited_once()
        await plugin._unload()

    async def test_rpc_sent_before_main_is_called_waits_for_module_startup(self):
        plugin = self.make_plugin(); plugin._module_state = {"rgb": {"enabled": False}}
        plugin._startup.clear()  # as Decky creates the plugin: _main has not run yet
        with patch.object(main, "_installed_standalone_plugins", return_value=[]),                 patch.object(main, "restore_intent", AsyncMock()), patch.object(plugin, "_start_desktop", AsyncMock()):
            request = asyncio.create_task(plugin.modules_set_enabled("rgb", True))
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertFalse(request.done())
            plugin._rgb._main.assert_not_awaited()
            await plugin._main()
            await request
        plugin._rgb._main.assert_awaited_once()
        await plugin._unload()

    async def test_normal_lifecycle_still_runs_all_modules(self):
        plugin = self.make_plugin()
        with patch.object(main, "_installed_standalone_plugins", return_value=[]):
            await plugin._migration(); await plugin._main(); await plugin._unload(); await plugin._uninstall()
        for c in self.components:
            for stage in ("_migration", "_main", "_unload", "_uninstall"):
                getattr(c, stage).assert_awaited_once()

    async def test_failing_scan_blocks_rpcs_only_while_it_fails(self):
        plugin = self.make_plugin()
        plugin._modules_started = True
        plugin._rgb.set_brightness = AsyncMock(return_value={"ok": True})
        with patch.object(main, "_installed_standalone_plugins", side_effect=PermissionError("denied")) as scan:
            status = await plugin.get_version()
            self.assertTrue(status["blocked"]); self.assertIn("denied", status["guard_error"])
            self.assertFalse(status["latched"])  # The panel must resume after a clean scan.
            with self.assertRaisesRegex(RuntimeError, "could not be verified"):
                await plugin.rgb_set_brightness(20)
            self.assertEqual(scan.call_count, 3)  # One retried scan; RPCs reuse its result.
        with patch.object(main, "_installed_standalone_plugins", return_value=[]):
            await plugin._check_guard()
        self.assertEqual(await plugin.rgb_set_brightness(20), {"ok": True})
        status = await plugin.get_version()
        self.assertFalse(status["blocked"]); self.assertFalse(status["latched"])
        self.assertTrue(plugin._modules_started)
        for c in self.components: c._unload.assert_not_awaited()

    async def test_scan_failure_at_startup_keeps_unstarted_modules_closed(self):
        plugin = self.make_plugin()
        with patch.object(main, "_installed_standalone_plugins", side_effect=PermissionError("denied")):
            await plugin._main()
        with patch.object(main, "_installed_standalone_plugins", return_value=[]):
            await plugin._check_guard()
            status = await plugin.get_version()
        self.assertTrue(status["blocked"]); self.assertTrue(status["restart_required"]); self.assertTrue(status["latched"])
        for c in self.components: c._main.assert_not_awaited()
        await plugin._unload()

    async def test_admission_uses_the_last_scan_instead_of_scanning_each_rpc(self):
        plugin = self.make_plugin()
        plugin._rgb.set_brightness = AsyncMock(return_value={"ok": True})
        with patch.object(main, "_installed_standalone_plugins", return_value=[]) as scan:
            for _ in range(5):
                await plugin.rgb_set_brightness(20)
            await plugin.get_version()
        scan.assert_called_once()

    async def test_monitor_catches_installation_without_frontend(self):
        plugin = self.make_plugin(); conflicts = []
        with patch.object(main, "_installed_standalone_plugins", side_effect=lambda: list(conflicts)), \
             patch.object(main, "CHECK_INTERVAL_S", 0.01):
            await plugin._main(); conflicts.append("LeGoTDP")
            for _ in range(100):
                if not plugin._modules_started: break
                await asyncio.sleep(0.01)
            self.assertFalse(plugin._modules_started)
            await plugin._unload()
        for c in self.components: c._unload.assert_awaited_once()

    async def test_inflight_rpc_finishes_before_modules_stop(self):
        plugin = self.make_plugin(); conflicts = []; entered = asyncio.Event(); finish = asyncio.Event(); order = []
        async def operation(*args):
            entered.set(); await finish.wait(); order.append("rpc finished"); return {"ok": True}
        async def stop(): order.append("modules stopped")
        plugin._wifi.rescan_and_reconnect = operation
        plugin._wifi._unload = AsyncMock(side_effect=stop)
        plugin._modules_started = True
        with patch.object(main, "_installed_standalone_plugins", side_effect=lambda: list(conflicts)):
            rpc = asyncio.create_task(plugin.wifi_rescan_and_reconnect()); await entered.wait()
            conflicts.append("LeGoTDP"); checking = asyncio.create_task(plugin._check_guard())
            for _ in range(100):
                if plugin._guard_blocked: break
                await asyncio.sleep(0.001)
            self.assertTrue(plugin._guard_blocked); self.assertEqual(order, [])
            finish.set(); await rpc; await checking
            self.assertEqual(order, ["rpc finished", "modules stopped"])

    async def test_drains_terminate_on_already_finished_jobs(self):
        class BoundedRegistry(set):
            reads = 0
            def __iter__(self):
                self.reads += 1
                if self.reads > 3:
                    raise AssertionError("drain spun on an already completed snapshot")
                return super().__iter__()
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        # gather completes immediately for finished tasks (Python 3.14), so discard callbacks have not run yet.
        plugin = self.make_plugin()
        plugin._rpc_jobs = BoundedRegistry({done})
        with patch.object(main.asyncio, "gather", new=AsyncMock(return_value=[None])):
            await plugin._drain_rpc_jobs()
        self.assertFalse(plugin._rpc_jobs)
        registry = BoundedRegistry({done})
        with patch.dict(module_runtime._workers, {"wifi": registry}),              patch.object(module_runtime.asyncio, "gather", new=AsyncMock(return_value=[None])):
            await module_runtime.drain("wifi")
        self.assertFalse(registry)

    async def test_conflict_waits_for_cancelled_background_hardware_worker(self):
        plugin = self.make_plugin()
        plugin._modules_started = True
        entered, finish = threading.Event(), threading.Event()
        unloaded = asyncio.Event()
        original_stage = plugin._run_stage
        async def stage(name):
            await original_stage(name)
            unloaded.set()
        plugin._run_stage = stage
        def worker(): entered.set(); finish.wait(3)
        background = asyncio.create_task(module_runtime.offload('vibration', worker))
        while not entered.is_set(): await asyncio.sleep(.001)
        background.cancel()
        with self.assertRaises(asyncio.CancelledError): await background
        checking = None
        try:
            with patch.object(main, '_installed_standalone_plugins', return_value=['LeGoTDP']):
                checking = asyncio.create_task(plugin._check_guard())
                await asyncio.wait_for(unloaded.wait(), 2)
                for _ in range(20): await asyncio.sleep(0)
                self.assertFalse(checking.done(), 'The guard cannot finish while old hardware work is still running')
                finish.set()
                await checking
                self.assertFalse(plugin._modules_started)
        finally:
            finish.set()
            if checking is not None: await checking


class ConflictAdmissionTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ModuleGateTests.setUp
    make_plugin = fixtures.ModuleGateTests.make_plugin
    asyncTearDown = fixtures.ModuleGateTests.asyncTearDown

    async def test_concurrent_rpc_admission_shares_one_inflight_conflict_scan(self):
        entered, release = threading.Event(), threading.Event()
        def scan():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test scan timeout')
            return []
        with patch.object(main, '_installed_standalone_plugins', side_effect=scan) as scanning:
            tasks = [asyncio.create_task(getattr(self.plugin, name)()) for name in
                     ('rgb_get_status', 'wifi_get_status', 'battery_get_status')]
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                # Give all callers a chance to enter the same held scan.
                for _ in range(20):
                    await asyncio.sleep(0)
            finally:
                release.set()
                await asyncio.gather(*tasks)
        self.assertEqual(scanning.call_count, 1)

    async def test_cancelled_scan_waiter_does_not_cache_the_completed_scan(self):
        entered, release = threading.Event(), threading.Event()
        def scan():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test scan timeout')
            return []
        with patch.object(main, '_installed_standalone_plugins', side_effect=scan):
            caller = asyncio.create_task(self.plugin._inspect_guard())
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            release.set()
            await self.plugin._guard_scan
        with patch.object(main, '_installed_standalone_plugins', return_value=['LeGoTDP']) as scanning:
            await self.plugin._inspect_guard()
        scanning.assert_called_once()
        self.assertTrue(self.plugin._guard_blocked)


class ConflictLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_conflict_withdraws_fan_before_an_admitted_rpc_finishes(self):
        plugin = support.LegacyPlugin()
        client = fan_backend.Client()
        plugin._fan._client = client
        plugin._run_stage = AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()
        request = asyncio.create_task(release.wait())
        plugin._rpc_jobs.add(request)
        drain = plugin._drain_rpc_jobs
        async def observed_drain():
            entered.set()
            await drain()
        with patch.object(plugin, '_drain_rpc_jobs', side_effect=observed_drain):
            task = asyncio.create_task(plugin._stop_modules())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertTrue(client._stop.is_set())
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

    async def test_transient_incomplete_plugin_manifest_does_not_unload_healthy_modules(self):
        plugin = support.LegacyPlugin()
        plugin._modules_started = True
        plugin._stop_modules = AsyncMock()
        with patch.object(main, '_installed_standalone_plugins', side_effect=[ValueError('Incomplete plugin manifest'), []]) as scan:
            await plugin._check_guard()
        self.assertEqual(scan.call_count, 2)
        self.assertFalse(plugin._guard_blocked)
        self.assertEqual(plugin._guard_error, '')
        plugin._stop_modules.assert_not_awaited()

    async def test_persistent_scan_failure_still_withdraws_unverifiable_ownership(self):
        plugin = support.LegacyPlugin()
        plugin._modules_started = True
        plugin._stop_modules = AsyncMock()
        with patch.object(main, '_installed_standalone_plugins', side_effect=PermissionError('denied')):
            for _ in range(main.GUARD_FAILURE_LIMIT - 1):
                await plugin._check_guard()
            self.assertFalse(plugin._guard_blocked)
            plugin._stop_modules.assert_not_awaited()
            await plugin._check_guard()
        self.assertTrue(plugin._guard_blocked)
        plugin._stop_modules.assert_awaited_once()


class ManifestMetadataTests(unittest.TestCase):
    def test_manifest_descriptor_type_is_checked_before_reading(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'Companion').mkdir()
            sibling = root / 'Other'
            sibling.mkdir()
            (sibling / 'plugin.json').write_text('{"name":"Other"}', encoding='utf-8')
            stream = Mock()
            stream.fileno.return_value = 42
            handle = Mock()
            handle.__enter__ = Mock(return_value=stream)
            handle.__exit__ = Mock(return_value=False)
            with patch.object(conflict_guard.os, 'O_NONBLOCK', 0x800, create=True), \
                    patch.object(conflict_guard.os, 'open', return_value=42) as opened, \
                    patch.object(conflict_guard.os, 'fdopen', return_value=handle), \
                    patch.object(conflict_guard.os, 'close') as closed, \
                    patch.object(conflict_guard.os, 'fstat', return_value=types.SimpleNamespace(st_mode=stat.S_IFIFO)):
                with patch.object(conflict_guard,'event') as warning:
                    self.assertEqual(conflict_guard.installed_conflicts(str(root / 'Companion')),[])
                warning.assert_called_once()
            self.assertTrue(opened.call_args.args[1] & 0x800)
            closed.assert_called_once_with(42)
            stream.read.assert_not_called()

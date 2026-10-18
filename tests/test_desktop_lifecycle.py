# SPDX-License-Identifier: BSD-3-Clause
import asyncio
import copy
import io
import json
import tempfile
import threading
import time
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import support
import main
import fan_backend
from fan_policy import DEFAULT_CURVE
from safe_settings import SettingsManager
from desktop_bridge import DesktopBridge


class DesktopLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def desktop_dns_shutdown(self, method):
        import bounded_http
        import tdp_backend
        import tdp_updater
        from companion_updates import CompanionUpdates
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / 'package.json').write_text('{"version":"2.1.0"}')
            marker = directory / 'dns-entered'
            source = support.dns_stall_source(marker)
            plugin = self.plugin()
            plugin._tdp = tdp_backend.Plugin()
            plugin._updates = CompanionUpdates(raw, raw, 1000, 1000)
            bridge = DesktopBridge(plugin, [method])
            plugin._desktop = bridge
            await bridge.start()
            downloader = tdp_updater.Updater(user_agent='test')
            def ensure_helper(cancel=None):
                downloader.download_to('https://github.com/test/helper', io.BytesIO(), 3, cancel=cancel)
                return True
            children = []
            launch = support.stalled_child_launcher(source, children)
            reader, writer = await asyncio.open_connection('127.0.0.1', bridge.port)
            body = json.dumps({'method': method, 'args': []}).encode()
            headers = (f'POST /rpc HTTP/1.1\r\nHost: 127.0.0.1:{bridge.port}\r\n'
                f'Origin: {bridge.origin}\r\nAuthorization: Bearer {bridge.token}\r\n'
                f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode()
            shutdown = None
            with patch.object(bounded_http.subprocess, 'Popen', side_effect=launch), \
                    patch.object(tdp_backend, '_ensure_ryzenadj', side_effect=ensure_helper):
                try:
                    writer.write(headers + body)
                    await writer.drain()
                    deadline = time.monotonic() + 3
                    while not marker.exists() and time.monotonic() < deadline:
                        await asyncio.sleep(.01)
                    self.assertTrue(marker.exists(), 'The real Desktop RPC did not enter controlled urllib DNS')
                    self.assertTrue(bridge.operations)
                    shutdown = asyncio.create_task(plugin._unload())
                    deadline = time.monotonic() + 1
                    while not shutdown.done() and time.monotonic() < deadline:
                        await asyncio.sleep(.01)
                    self.assertTrue(shutdown.done(), 'Shutdown must cancel network work before draining Desktop RPC operations')
                    await shutdown
                    self.assertFalse(bridge.operations)
                    self.assertFalse(plugin._update_jobs)
                    self.assertTrue(plugin._updates._closed.is_set())
                    self.assertTrue(plugin._tdp._helper_cancel.is_set())
                    self.assertIsNotNone(children[0].poll())
                finally:
                    # A failing old implementation must not leave the controlled
                    # resolver, shutdown task or loopback socket behind.
                    plugin._updates.close()
                    plugin._tdp._helper_cancel.set()
                    if shutdown is not None:
                        await shutdown
                    await bridge.close()
                    writer.close()
                    await writer.wait_closed()
                    await reader.read()
                    support.reap(children)

    async def test_unload_cancels_update_dns_before_draining_real_desktop_rpc(self):
        await self.desktop_dns_shutdown('updates_check')

    async def test_unload_cancels_extras_dns_before_draining_real_desktop_rpc(self):
        await self.desktop_dns_shutdown('retry_extras')

    async def test_unload_restores_hardware_before_extras_and_bounds_a_hanging_one(self):
        plugin = self.plugin()
        order = []
        plugin._modules_started = True
        plugin._stop_modules = AsyncMock(side_effect=lambda: order.append('modules'))
        plugin._updates = Mock(close=Mock(side_effect=lambda: order.append('updates')))
        plugin._firmware = Mock(close=Mock(side_effect=lambda: order.append('firmware')))
        never = asyncio.Event()
        async def hanging_close():
            order.append('bridge')
            await never.wait()
        plugin._desktop = SimpleNamespace(close=hanging_close)
        with patch.object(main, 'SHUTDOWN_BUDGET_S', .2):
            started = time.monotonic()
            await asyncio.wait_for(plugin._unload(), 2)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(order, ['modules', 'updates', 'bridge'])
        plugin._tdp._helper_cancel.set.assert_called_once()
        never.set()

    async def test_uninstall_after_unload_does_not_spend_a_second_shutdown_budget(self):
        plugin = self.plugin()
        plugin._ever_started = True
        never = asyncio.Event()
        async def hanging_close():
            await never.wait()
        plugin._desktop = SimpleNamespace(close=hanging_close)
        stamps = {}
        async def stage(name):
            stamps[name] = time.monotonic()
        plugin._run_stage = stage
        with patch.object(main, 'SHUTDOWN_BUDGET_S', .5):
            started = time.monotonic()
            await plugin._unload()
            await plugin._uninstall()
        self.assertLess(stamps['_uninstall'] - started, .9)
        never.set()

    async def test_decky_listener_ends_at_eof_instead_of_starving_unload(self):
        class Socket:  # Decky 3.2.10 UnixSocket's listener, with a bounded spin.
            active = True
            async def _read_single_line(self, reader):
                try:
                    return (await reader.readuntil()).decode()
                except asyncio.IncompleteReadError as err:
                    return err.partial.decode()
            async def listen(self, reader, handled):
                for _ in range(1000):
                    line = await self._read_single_line(reader)
                    handled.append(line)
                return 'spun'
        socket = Socket()
        def closure(sandboxed):
            async def emit(event, *args):
                await sandboxed._socket.write(event)
            return emit
        reader, handled = asyncio.StreamReader(), []
        with patch.object(main.decky, 'emit', closure(SimpleNamespace(_socket=socket))):
            self.plugin()
        reader.feed_data(b'{"id": 1}\n')
        reader.feed_eof()
        listener = asyncio.ensure_future(socket.listen(reader, handled))
        unload = asyncio.ensure_future(asyncio.sleep(0, 'unloaded'))  # Needs the loop to run again.
        await asyncio.wait([listener, unload])
        self.assertTrue(listener.cancelled())
        self.assertEqual(handled, ['{"id": 1}\n'])
        self.assertEqual(unload.result(), 'unloaded')

    async def test_firmware_calls_never_block_other_backend_tasks(self):
        loop = asyncio.get_running_loop()
        observed = []
        def slow():
            # The loop can only run this callback if the call is not executing on it.
            beat = threading.Event()
            loop.call_soon_threadsafe(beat.set)
            observed.append(beat.wait(2))
            return {'phase':'installing','message':'Installing','error':''}
        plugin=self.plugin();plugin._firmware=Mock(status=Mock(side_effect=slow),check=Mock(side_effect=slow))
        for method in (plugin.firmware_get_status,plugin.firmware_check):
            result=await method()
            self.assertEqual(result['phase'],'installing')
        self.assertEqual(observed,[True,True],'firmware result observation blocked Decky loop')

    def plugin(self):
        plugin = main.Plugin()
        plugin._check_guard = AsyncMock()
        plugin._tdp = Mock(set_panel_active=AsyncMock(), get_settings=AsyncMock(return_value={'spl': 15000}))
        plugin._display = Mock(get_state=AsyncMock(return_value={'enabled': True}), restart_session=AsyncMock())
        return plugin

    async def test_decky_and_desktop_share_one_real_fan_state_and_revision(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        plugin = self.plugin()
        with patch.object(fan_backend, 'settings', SettingsManager('fan_settings', temporary.name)), \
                patch.object(fan_backend, 'read_automatic', return_value={'valid': True, 'automatic': True,
                    'rpm': 1800, 'apu_temperature': 45, 'ownership': {'blocked': False}}):
            plugin._fan = fan_backend.Plugin(); plugin._fan._supported = True; plugin._fan._load()
            bridge = DesktopBridge(plugin, ['fan_get_state', 'fan_save_curve', 'fan_save_unit'])
            before = await plugin.fan_get_state()  # Exactly the public Decky RPC.
            curve = copy.deepcopy(DEFAULT_CURVE); curve[6][0] = 94
            desktop = await bridge.dispatch('fan_save_curve', ['global', curve, before['settings']['revision']])
            decky = await plugin.fan_get_state()
            self.assertEqual(decky['settings'], desktop['settings'])
            self.assertEqual(decky['settings']['global_curve'], curve)
            updated = await plugin.fan_save_unit('percent', decky['settings']['revision'])
            self.assertEqual((await bridge.dispatch('fan_get_state', []))['settings'], updated['settings'])
            with self.assertRaises(ValueError):
                await bridge.dispatch('fan_save_curve', ['global', curve, before['settings']['revision']])
            fresh = fan_backend.Plugin(); fresh._load()
            self.assertEqual(fresh._state, updated['settings'])
            self.assertFalse(fresh._state['enabled'])
            await plugin._fan._unload()

    async def test_panel_leases_from_two_frontends_do_not_clear_each_other(self):
        plugin = self.plugin()
        await plugin._desktop_panel_active(True)
        await plugin.set_panel_active(False)  # QAM closes while Desktop is visible.
        plugin._tdp.set_panel_active.assert_awaited_with(True)
        plugin._desktop_panel_deadline = 0
        await plugin.set_panel_active(False)
        plugin._tdp.set_panel_active.assert_awaited_with(False)
        await plugin.set_panel_active(True)
        await plugin._desktop_panel_active(False)  # Desktop closes, QAM stays visible.
        plugin._tdp.set_panel_active.assert_awaited_with(True)
        await plugin.set_panel_active(False)
        plugin._tdp.set_panel_active.assert_awaited_with(False)
        with patch.object(main, 'time', SimpleNamespace(monotonic=lambda: 100)):
            await plugin._desktop_panel_active(True)
        with patch.object(main, 'time', SimpleNamespace(monotonic=lambda: 191)):
            await plugin.set_panel_active(False)
        plugin._tdp.set_panel_active.assert_awaited_with(False)
        with self.assertRaises(ValueError):
            await plugin._desktop_panel_active('yes')

    async def test_no_gamescope_never_restarts_desktop_session(self):
        plugin = self.plugin()
        with patch.object(main.display_backend, '_gamescope_start_time', return_value=None):
            result = await plugin._desktop_display_restart()
            self.assertIn('Gaming Mode', result['restart_error'])
        plugin._display.restart_session.assert_not_awaited()

    async def test_gamescope_restart_retains_existing_rpc_guards(self):
        plugin = self.plugin()
        plugin._closing = True
        with patch.object(main.display_backend, '_gamescope_start_time', return_value=123):
            with self.assertRaises(RuntimeError):
                await plugin._desktop_display_restart()
        plugin._display.restart_session.assert_not_awaited()

    async def test_start_installs_once_and_unload_removes_session_only(self):
        plugin = self.plugin()
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        assets = Path(temporary.name) / 'dist/desktop'; assets.mkdir(parents=True)
        (assets / 'rpc-methods.json').write_text('["get_settings"]')
        installer = Mock()
        with patch.object(main, 'PLUGIN_DIR', temporary.name), patch.object(main.DesktopInstall, 'for_decky', return_value=installer):
            await plugin._start_desktop()
            first = plugin._desktop
            await plugin._start_desktop()
            self.assertIs(first, plugin._desktop)
            installer.install.assert_called_once_with(first.origin, first.token)
            self.assertEqual(await first.dispatch('get_settings', []), {'spl': 15000})
            await plugin._unload()
            installer.remove.assert_called_once_with()
            self.assertIsNone(plugin._desktop)

    async def test_firmware_observer_failure_does_not_skip_other_module_cleanup(self):
        plugin=self.plugin()
        plugin._firmware=Mock(close=Mock(side_effect=RuntimeError('observer timeout')))
        plugin._modules_started=True
        plugin._stop_modules=AsyncMock()
        await plugin._unload()
        plugin._firmware.close.assert_called_once()
        plugin._stop_modules.assert_awaited_once()

    async def test_paused_modules_cannot_hide_firmware_outcome_or_enable_an_update(self):
        plugin=self.plugin();plugin._check_guard=AsyncMock();plugin._guard_blocked=True
        plugin._firmware=Mock(status=Mock(return_value={'phase':'installing'}))
        self.assertEqual((await plugin.firmware_get_status())['phase'],'installing')
        for method in (plugin.firmware_check,plugin.firmware_download,lambda:plugin.firmware_install('token')):
            with self.assertRaises(RuntimeError):await method()
        plugin._firmware.check.assert_not_called();plugin._firmware.admit_install.assert_not_called()

    async def test_failed_publication_closes_bridge_without_stopping_modules(self):
        plugin = self.plugin()
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        assets = Path(temporary.name) / 'dist/desktop'; assets.mkdir(parents=True)
        (assets / 'rpc-methods.json').write_text('["get_settings"]')
        installer = Mock(); installer.install.side_effect = OSError('unsafe home')
        with patch.object(main, 'PLUGIN_DIR', temporary.name), patch.object(main.DesktopInstall, 'for_decky', return_value=installer):
            await plugin._start_desktop()
        self.assertIsNone(plugin._desktop)
        self.assertEqual(await plugin.get_settings(), {'spl': 15000})
        await plugin._unload()

    async def test_cancelled_startup_publication_is_drained_before_unload(self):
        plugin = self.plugin()
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        assets = Path(temporary.name) / 'dist/desktop'; assets.mkdir(parents=True)
        (assets / 'rpc-methods.json').write_text('["get_settings"]')
        started, release = threading.Event(), threading.Event()
        order = []
        installer = Mock()
        def install(*_args):
            started.set()
            if not release.wait(2):
                raise RuntimeError('test publication timed out')
            order.append('install')
        installer.install.side_effect = install
        installer.remove.side_effect = lambda: order.append('remove')
        with patch.object(main, 'PLUGIN_DIR', temporary.name), patch.object(main.DesktopInstall, 'for_decky', return_value=installer):
            startup = asyncio.create_task(plugin._start_desktop())
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            startup.cancel()
            unload = asyncio.create_task(plugin._unload())
            await asyncio.sleep(.02)
            self.assertFalse(unload.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await startup
            await unload
        self.assertEqual(order, ['install', 'remove'])
        self.assertIsNone(plugin._desktop)

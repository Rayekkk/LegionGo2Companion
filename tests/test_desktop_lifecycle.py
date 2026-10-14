# SPDX-License-Identifier: BSD-3-Clause
import asyncio
import copy
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_integration
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
            source = ("import runpy,socket,sys,time; from pathlib import Path\n"
                "def resolver(*args,**kwargs):\n"
                f" Path({str(marker)!r}).write_text('entered')\n"
                " time.sleep(30)\n"
                " raise OSError('controlled DNS failure')\n"
                "socket.getaddrinfo=resolver\n"
                f"sys.argv=[{str(Path(bounded_http.__file__).resolve())!r},'--fetch']\n"
                f"runpy.run_path({str(Path(bounded_http.__file__).resolve())!r},run_name='__main__')\n")
            plugin = self.plugin()
            plugin._tdp = tdp_backend.Plugin()
            plugin._updates = CompanionUpdates(raw, raw, 1000, 1000)
            bridge = DesktopBridge(plugin, directory, [method])
            plugin._desktop = bridge
            await bridge.start()
            downloader = tdp_updater.Updater(user_agent='test', log_prefix='test', plugin_dir=raw, logger=Mock())
            def ensure_helper(cancel=None):
                downloader.download_to('https://github.com/test/helper', io.BytesIO(), 3, cancel=cancel)
                return True
            children = []
            original = subprocess.Popen
            def launch(_args, **kwargs):
                child = original([sys.executable, '-I', '-c', source], **kwargs)
                children.append(child)
                return child
            reader, writer = await asyncio.open_connection('127.0.0.1', bridge.port)
            body = json.dumps({'method': method, 'args': []}).encode()
            headers = (f'POST /rpc HTTP/1.1\r\nHost: 127.0.0.1:{bridge.port}\r\n'
                f'Origin: {bridge.origin}\r\nAuthorization: Bearer {bridge.token}\r\n'
                f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode()
            shutdown = None
            with patch.object(bounded_http.subprocess, 'Popen', side_effect=launch), \
                    patch.object(tdp_backend, '_wmi_only', return_value=False), \
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
                    for child in children:
                        if child.poll() is None:
                            child.kill()
                        child.wait(timeout=3)

    async def test_unload_cancels_update_dns_before_draining_real_desktop_rpc(self):
        await self.desktop_dns_shutdown('updates_check')

    async def test_unload_cancels_extras_dns_before_draining_real_desktop_rpc(self):
        await self.desktop_dns_shutdown('retry_extras')

    async def test_unload_drains_lifecycle_components_without_optional_network_helper(self):
        plugin = self.plugin()
        plugin._module_state = {}
        plugin._modules_started = True
        plugin._firmware = Mock()
        for name in main.MODULE_NAMES:
            setattr(plugin, '_' + name, SimpleNamespace(_unload=AsyncMock()))
        await plugin._unload()
        for name in main.MODULE_NAMES:
            getattr(plugin, '_' + name)._unload.assert_awaited_once()
        self.assertFalse(plugin._modules_started)

    async def test_firmware_history_status_never_blocks_other_backend_tasks(self):
        from controller_firmware import FirmwarePreparation
        class Installer:
            def previous_status(self):
                time.sleep(.08)  # Bounded systemctl/history I/O after restart.
                return {'id':'existing','phase':'installing','message':'Updating','error':''}
        plugin=self.plugin();plugin._firmware=FirmwarePreparation(installer=Installer())
        for method in (plugin.firmware_get_status,plugin.firmware_check_updates):
            ticks=[]
            async def heartbeat():
                deadline=time.monotonic()+.06
                while time.monotonic()<deadline:
                    ticks.append(time.monotonic());await asyncio.sleep(.005)
            _,result=await asyncio.gather(heartbeat(),method())
            self.assertGreaterEqual(len(ticks),3,'firmware result observation blocked Decky loop')
            self.assertEqual(result['job']['phase'],'installing')

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
            bridge = DesktopBridge(plugin, Path(temporary.name), ['fan_get_state', 'fan_save_curve', 'fan_save_unit'])
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
            result = await plugin._desktop_modules_restart()
            self.assertFalse(result['success'])
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
        plugin._firmware=Mock(status=Mock(return_value={'job':{'phase':'installing'}}))
        self.assertEqual((await plugin.firmware_get_status())['job']['phase'],'installing')
        with self.assertRaises(RuntimeError):await plugin.firmware_check_updates()
        plugin._firmware.check_updates.assert_not_called()

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

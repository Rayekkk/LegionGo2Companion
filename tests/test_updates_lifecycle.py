# SPDX-License-Identifier: BSD-3-Clause
"""Network waits must never own the hardware RPC gate or outlive shutdown."""
import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_integration
import main


class UpdateLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        plugin = main.Plugin()
        plugin._check_guard = AsyncMock()
        plugin._updates = Mock()
        plugin._updates.check.return_value = {'success': True, 'no_release': True}
        return plugin

    async def test_checks_work_with_tdp_disabled_and_do_not_initialize_hardware(self):
        plugin = self.make_plugin()
        plugin._module_state['tdp'] = {'enabled': False}
        self.assertTrue((await plugin.updates_check())['success'])
        plugin._updates.check.assert_called_once_with()
        self.assertFalse(plugin._modules_started)

    async def test_exact_selected_version_reaches_backend_and_unknown_rpcs_stay_gated(self):
        plugin = self.make_plugin()
        plugin._updates.download.return_value = {'success': True, 'version': '0.7.0'}
        await plugin.updates_download('0.7.0')
        plugin._updates.download.assert_called_once_with('0.7.0')
        self.assertEqual(main._rpc_module('updates_future_hardware_write'), 'tdp')

    async def test_blocked_or_closing_never_starts_network_work(self):
        for field in ('_guard_blocked', '_closing'):
            plugin = self.make_plugin()
            setattr(plugin, field, True)
            with self.assertRaises(RuntimeError):
                await plugin.updates_check()
            with self.assertRaises(RuntimeError):
                await plugin.updates_download('0.7.0')
            plugin._updates.check.assert_not_called()
            plugin._updates.download.assert_not_called()

    async def test_download_does_not_hold_hardware_rpc_gate(self):
        plugin = self.make_plugin()
        started, release = threading.Event(), threading.Event()
        def download(_version):
            started.set()
            if not release.wait(3):
                raise RuntimeError('test worker timed out')
            return {'success': True}
        plugin._updates.download.side_effect = download
        plugin._tdp = Mock(get_settings=AsyncMock(return_value={'spl': 25000}))
        task = asyncio.create_task(plugin.updates_download('0.7.0'))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            value = await asyncio.wait_for(plugin.get_settings(), .5)
            self.assertEqual(value, {'spl': 25000})
        finally:
            release.set()
            await task

    async def test_cancelled_rpc_is_drained_and_close_signals_worker(self):
        plugin = self.make_plugin()
        started, release, stopped = threading.Event(), threading.Event(), threading.Event()
        def download(_version):
            started.set()
            if not release.wait(3):
                raise RuntimeError('test worker timed out')
            stopped.set()
            return {'success': False, 'error': 'closed'}
        plugin._updates.download.side_effect = download
        plugin._updates.close.side_effect = release.set
        task = asyncio.create_task(plugin.updates_download('0.7.0'))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertTrue(plugin._update_jobs)
            await asyncio.wait_for(plugin._unload(), 1)
            self.assertTrue(stopped.is_set())
            self.assertFalse(plugin._update_jobs)
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_conflict_signals_network_stop_without_waiting_for_download(self):
        plugin = self.make_plugin()
        with patch.object(main, '_installed_standalone_plugins', return_value=['LeGoTDP']):
            await plugin._inspect_guard()
        self.assertTrue(plugin._guard_blocked)
        plugin._updates.close.assert_called_once()

    async def test_repeated_unload_cancellation_at_hardware_gate_still_drains_download(self):
        plugin = self.make_plugin()
        started, release = threading.Event(), threading.Event()
        def download(_version):
            started.set()
            if not release.wait(3):
                raise RuntimeError('test worker timed out')
            return {'success': False}
        plugin._updates.download.side_effect = download
        task = asyncio.create_task(plugin.updates_download('0.7.0'))
        unload = None
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            await plugin._guard_lock.acquire()
            unload = asyncio.create_task(plugin._unload())
            for _ in range(5):
                await asyncio.sleep(0)
            for _ in range(2):
                unload.cancel()
                await asyncio.sleep(0)
                self.assertFalse(unload.done())
            plugin._guard_lock.release()
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertFalse(unload.done(), 'Shutdown waits for actual network worker exit')
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(unload, 1)
            await task
            self.assertFalse(plugin._update_jobs)
        finally:
            if plugin._guard_lock.locked():
                plugin._guard_lock.release()
            release.set()
            await asyncio.gather(task, *([unload] if unload else []), return_exceptions=True)

    async def test_decky_account_is_used_instead_of_root_or_first_login(self):
        plugin = main.Plugin()
        account = Mock(pw_dir='/home/player', pw_uid=1001, pw_gid=1001)
        with patch.object(test_integration.decky, 'DECKY_USER', 'player', create=True), \
             patch('pwd.getpwnam', return_value=account) as lookup, \
             patch.object(main, 'CompanionUpdates') as updater:
            plugin._ensure_updates()
            plugin._ensure_updates()
            lookup.assert_called_once_with('player')
            updater.assert_called_once_with(main.PLUGIN_DIR, '/home/player', 1001, 1001)


if __name__ == '__main__':
    unittest.main()

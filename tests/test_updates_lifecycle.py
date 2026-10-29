# SPDX-License-Identifier: BSD-3-Clause
"""Network waits must never own the hardware RPC gate or outlive shutdown."""
import asyncio
import inspect
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import support
import main
import test_modules as fixtures
import dk_update


class UpdateLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        plugin = support.LegacyPlugin()
        plugin._check_guard = AsyncMock()
        plugin._updates = Mock()
        plugin._updates_home = True
        plugin._updates.check.return_value = {'ok': True, 'no_release': True}
        return plugin

    async def test_checks_work_with_tdp_disabled_and_do_not_initialize_hardware(self):
        plugin = self.make_plugin()
        plugin._module_state['tdp'] = {'enabled': False}
        self.assertTrue((await plugin.updates_check())['ok'])
        plugin._updates.check.assert_called_once_with(True)  # a click asks GitHub again (DeckyKit dk_update)
        self.assertFalse(plugin._modules_started)

    async def test_exact_selected_version_reaches_backend_and_unknown_rpcs_stay_gated(self):
        plugin = self.make_plugin()
        plugin._updates.download.return_value = {'ok': True, 'version': '0.7.0'}
        await plugin.updates_download('0.7.0')
        plugin._updates.download.assert_called_once_with('0.7.0')
        with self.assertRaises(KeyError):  # a new RPC without a module is caught when main.py is imported
            main._rpc_module('updates_future_hardware_write')
        public = [name for name, fn in vars(main.Plugin).items()
                  if not name.startswith('_') and name not in main._UNGUARDED_RPCS and inspect.iscoroutinefunction(fn)]
        self.assertEqual({name for name in public if main._rpc_module(name) == 'tdp'}, main.TDP_RPCS)

    async def test_answers_are_d2_with_dk_update_fields_as_the_state(self):
        plugin = self.make_plugin()
        newer = {'ok': True, 'current_version': '0.6.2', 'latest_version': '0.7.0', 'update_available': True,
                 'download_available': False, 'message': 'The release has no single plugin ZIP yet.'}
        plugin._updates.check.return_value = newer
        self.assertEqual(await plugin.updates_check(), {'ok': True, 'state': {k: v for k, v in newer.items() if k != 'ok'}})
        self.assertTrue(newer['ok'], "dk_update's cached view is not changed")
        plugin._updates.check.return_value = {'ok': False, 'current_version': '0.6.2', 'update_available': False,
                                              'download_available': False, 'error': 'GitHub rate limit reached.'}
        self.assertEqual(await plugin.updates_check(), {'ok': False, 'error': 'GitHub rate limit reached.'})
        plugin._updates.download.return_value = {'ok': True, 'path': '/home/deck/Downloads/a.zip', 'version': '0.7.0',
                                                 'sha256': 'a' * 64}
        self.assertEqual((await plugin.updates_download('0.7.0'))['state']['path'], '/home/deck/Downloads/a.zip')
        plugin._updates_home = None
        self.assertEqual(await plugin.updates_download('0.7.0'), {
            'ok': False, 'error': 'The desktop user is unavailable; the file cannot be saved safely.'})

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
            return {'ok': True}
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
            return {'ok': False, 'error': 'closed'}
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
            await main.Plugin._check_guard(plugin)  # make_plugin stubs the instance method
        self.assertTrue(plugin._guard_blocked)
        plugin._updates.close.assert_called_once()

    async def test_repeated_unload_cancellation_at_hardware_gate_still_drains_download(self):
        plugin = self.make_plugin()
        started, release = threading.Event(), threading.Event()
        def download(_version):
            started.set()
            if not release.wait(3):
                raise RuntimeError('test worker timed out')
            return {'ok': False}
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
        plugin = support.LegacyPlugin()
        account = Mock(pw_dir='/home/player', pw_uid=1001, pw_gid=1001)
        with patch.object(support.decky, 'DECKY_USER', 'player', create=True), \
             patch('pwd.getpwnam', return_value=account) as lookup, \
             patch.object(main.dk_update, 'Updates') as updater:
            plugin._ensure_updates()
            plugin._ensure_updates()
            lookup.assert_called_once_with('player')
            updater.assert_called_once()
            self.assertEqual((updater.call_args.kwargs['home'], updater.call_args.kwargs['uid']), ('/home/player', 1001))
            self.assertEqual(updater.call_args.kwargs['flags'], ['root'])


class UpdateCloseResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ModuleGateTests.setUp
    make_plugin = fixtures.ModuleGateTests.make_plugin
    asyncTearDown = fixtures.ModuleGateTests.asyncTearDown

    def slow_close(self, observations):
        updater = dk_update.Updates(repo='Rayekkk/LegionGo2Companion', folder='LegionGo2Companion',
                                    plugin_name='Legion Go 2 Companion', current='2.4.0', home=self.folder.name)
        self.plugin._updates = updater
        loop = asyncio.get_running_loop()
        close = updater.close

        def waiting_close():
            heartbeat = threading.Event()
            loop.call_soon_threadsafe(heartbeat.set)
            observations.append(heartbeat.wait(.5))
            close()

        return updater, waiting_close

    async def test_unload_publication_barrier_does_not_block_the_event_loop(self):
        observations = []
        updater, closing = self.slow_close(observations)
        self.plugin._modules_started = True
        with patch.object(updater, 'close', side_effect=closing):
            await self.plugin._unload()
        self.assertEqual(observations, [True], 'Waiting for archive publication must leave the event loop free.')
        self.assertTrue(updater._closed.is_set())
        for name in main.MODULE_NAMES:
            getattr(self.plugin, '_' + name)._unload.assert_awaited_once()

    async def test_conflict_publication_barrier_does_not_block_the_event_loop(self):
        observations = []
        updater, closing = self.slow_close(observations)
        with patch.object(updater, 'close', side_effect=closing), \
                patch.object(main, '_installed_standalone_plugins', return_value=['LeGoTDP']):
            await self.plugin._check_guard()
        self.assertEqual(observations, [True], 'A conflict must not synchronously wait for the archive rename.')
        self.assertTrue(self.plugin._guard_blocked)
        self.assertTrue(updater._closed.is_set())

    async def test_repeated_unload_cancellation_finishes_publication_and_module_cleanup(self):
        updater = dk_update.Updates(repo='Rayekkk/LegionGo2Companion', folder='LegionGo2Companion',
                                    plugin_name='Legion Go 2 Companion', current='2.4.0', home=self.folder.name)
        self.plugin._updates = updater
        self.plugin._modules_started = True
        entered, release = threading.Event(), threading.Event()
        close = updater.close

        def waiting_close():
            entered.set()
            if not release.wait(2):
                raise TimeoutError('Test did not release the publication barrier.')
            close()

        with patch.object(updater, 'close', side_effect=waiting_close):
            task = asyncio.create_task(self.plugin._unload())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done(), 'Cancellation cannot abandon archive publication or hardware cleanup.')
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(updater._closed.is_set())
        self.assertFalse(self.plugin._modules_started)
        for name in main.MODULE_NAMES:
            getattr(self.plugin, '_' + name)._unload.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()

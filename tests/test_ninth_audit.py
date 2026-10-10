# SPDX-License-Identifier: BSD-3-Clause
"""Lifecycle publication barriers must leave the main event loop responsive."""
import asyncio
import threading
import unittest
from unittest.mock import patch

import test_modules as fixtures
import main
from companion_updates import CompanionUpdates


class UpdateCloseResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ModuleGateTests.setUp
    make_plugin = fixtures.ModuleGateTests.make_plugin
    asyncTearDown = fixtures.ModuleGateTests.asyncTearDown

    def slow_close(self, observations):
        updater = CompanionUpdates(self.folder.name, '', -1, -1)
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
            await self.plugin._inspect_guard_once()
        self.assertEqual(observations, [True], 'A conflict must not synchronously wait for the archive rename.')
        self.assertTrue(self.plugin._guard_blocked)
        self.assertTrue(updater._closed.is_set())

    async def test_repeated_unload_cancellation_finishes_publication_and_module_cleanup(self):
        updater = CompanionUpdates(self.folder.name, '', -1, -1)
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

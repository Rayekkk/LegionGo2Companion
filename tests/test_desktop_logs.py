# SPDX-License-Identifier: BSD-3-Clause
import asyncio
import threading
import unittest
from unittest.mock import patch

import support
import main


class DesktopLogCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_collection_is_single_flight_and_survives_waiter_cancel(self):
        plugin=main.Plugin()
        entered,release=threading.Event(),threading.Event()
        calls=[]
        def collect(directory):
            calls.append(directory);entered.set();release.wait(3)
            return {'events':[], 'warnings':[], 'truncated':False}
        with patch('diagnostics_reader.read_events',side_effect=collect):
            first=asyncio.create_task(plugin._desktop_get_logs())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                second=asyncio.create_task(plugin._desktop_get_logs())
                await asyncio.sleep(0)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):await first
                self.assertFalse(plugin._diagnostics_read.done())
                release.set()
                self.assertEqual((await second)['events'],[])
                self.assertEqual(calls,[main.diagnostics.LOG_DIR])
            finally:
                release.set()
                await asyncio.gather(first,return_exceptions=True)
                if plugin._diagnostics_read:await asyncio.gather(plugin._diagnostics_read,return_exceptions=True)

    async def test_logs_do_not_depend_on_hardware_gates_and_stop_admission_on_shutdown(self):
        plugin=main.Plugin();plugin._guard_blocked=True
        with patch('diagnostics_reader.read_events',return_value={'events':[], 'warnings':[], 'truncated':False}) as read:
            self.assertEqual((await plugin._desktop_get_logs())['events'],[])
            read.assert_called_once()
            plugin._closing=True
            with self.assertRaises(RuntimeError):await plugin._desktop_get_logs()
            self.assertEqual(read.call_count,1)

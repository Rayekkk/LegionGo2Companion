# SPDX-License-Identifier: BSD-3-Clause
"""Controller monitoring must not stall the event loop."""
import asyncio
import threading
import types
import unittest
from unittest.mock import AsyncMock, patch

import support
import controller_backend as controller


class ControllerMonitorResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_settings_read_allows_event_loop_heartbeat(self):
        plugin = controller.Plugin()
        loop = asyncio.get_running_loop()
        observations = []
        def slow_state():
            heartbeat = threading.Event()
            loop.call_soon_threadsafe(heartbeat.set)
            observations.append(heartbeat.wait(.5))
            return {'gyro_source': 'system'}
        # End immediately after one scheduled reconciliation tick; no hardware
        # discovery or actual settings writes are involved in this test.
        with patch.object(controller, '_state', side_effect=slow_state), \
                patch.object(controller, '_suspend_offset', return_value=0), \
                patch.object(controller, 'time', types.SimpleNamespace(monotonic=iter([0, 61]).__next__)), \
                patch.object(controller.asyncio, 'sleep', new=AsyncMock(side_effect=[None, asyncio.CancelledError()])):
            with self.assertRaises(asyncio.CancelledError):
                await plugin._watch()
        self.assertEqual(observations, [True], 'Settings recovery and its file lock must not block the event loop.')


if __name__ == '__main__':
    unittest.main()

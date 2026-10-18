# SPDX-License-Identifier: BSD-3-Clause
"""Blocking hardware I/O offloaded through module_runtime must not stall the event loop."""
import asyncio
import threading
import unittest

import support  # Install isolated Decky stubs before backend imports.
import module_runtime


class OffloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await module_runtime.drain('battery')

    async def test_blocking_work_lets_the_event_loop_run(self):
        loop = asyncio.get_running_loop()
        def blocked():
            beat = threading.Event()
            loop.call_soon_threadsafe(beat.set)
            return beat.wait(.5), 'done'
        self.assertEqual(await module_runtime.offload('battery', blocked), (True, 'done'))


if __name__ == '__main__':
    unittest.main()

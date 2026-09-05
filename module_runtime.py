# SPDX-License-Identifier: BSD-3-Clause
"""Drain hardware work before withdrawing a module, including cancelled workers."""
import asyncio

_workers = {}

async def complete(operation):
    """Keep a hardware transaction alive through repeated caller cancellation."""
    task = asyncio.ensure_future(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                # A task that cancelled itself is not a recoverable caller cancel.
                result = task.result()
                break
    if cancelled:
        raise asyncio.CancelledError
    return result

async def offload(module, function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    pending = _workers.setdefault(module, set())
    pending.add(task)
    def completed(worker):
        pending.discard(worker)
        if not worker.cancelled():
            worker.exception()  # Also retrieve failures after the caller was cancelled.
    task.add_done_callback(completed)
    # Task cancellation cannot cancel the underlying worker or hide it from drain.
    return await asyncio.shield(task)

async def drain(module):
    while _workers.get(module):
        await asyncio.gather(*tuple(_workers[module]), return_exceptions=True)

def require_success(result):
    if not isinstance(result, dict) or result.get('success') is not True:
        detail = result if isinstance(result, dict) else {}
        raise RuntimeError(detail.get('error') or detail.get('message')
                           or detail.get('stderr') or 'Hardware restoration was not confirmed.')

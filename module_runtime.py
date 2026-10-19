# SPDX-License-Identifier: BSD-3-Clause
"""Drain hardware work before withdrawing a module, including cancelled workers."""
import asyncio
import contextlib
import contextvars
import functools
import os
import threading
import time
from diagnostics import event

DMI_ROOT = "/sys/class/dmi/id"

_workers = {}
# The Legion's AML aborts EC/WMI methods that run concurrently ("cannot release
# Mutex [ECMT]"). Hold only around one firmware-backed sysfs write, innermost,
# from worker threads only: never take another lock or block on I/O beyond it.
firmware_lock = threading.Lock()
CONTROLLER_MODULES = frozenset({'controller', 'remap', 'rgb', 'vibration'})
_controller_paused = False
_controller_generation = 0
_operation_generation = contextvars.ContextVar('controller_operation_generation', default=None)


class AdmissionClosed(RuntimeError):
    """A refused call: the conflict guard, shutdown or maintenance closed admission and already reports why."""


class ControllerPaused(AdmissionClosed):
    """Maintenance admission is closed without changing saved module choices."""


def is_paused(module):
    return module in CONTROLLER_MODULES and _controller_paused


_stop_requested = set()


def request_stop(module):
    """A stop came while the module was still starting: its start must apply nothing more."""
    _stop_requested.add(module)


def stop_requested(module):
    return module in _stop_requested


def begin_start(module):
    _stop_requested.discard(module)


def require_available(module):
    generation = _operation_generation.get()
    if is_paused(module) or (module in CONTROLLER_MODULES and generation is not None
                            and generation != _controller_generation):
        raise ControllerPaused('Controller operations are paused for firmware maintenance.')


@contextlib.contextmanager
def controller_scope(module):
    """An old multi-step operation cannot adopt the post-maintenance generation."""
    require_available(module)
    token = None
    if module in CONTROLLER_MODULES and _operation_generation.get() is None:
        token = _operation_generation.set(_controller_generation)
    try:
        yield
    finally:
        if token is not None:
            _operation_generation.reset(token)


def controller_operation(module):
    def decorate(function):
        @functools.wraps(function)
        async def operation(*args, **kwargs):
            with controller_scope(module):
                return await function(*args, **kwargs)
        return operation
    return decorate


def background_task(operation):
    """Start a long-lived monitor outside the caller's operation generation; never use it to revive a stale retry."""
    context = contextvars.copy_context()
    context.run(_operation_generation.set, None)
    task = context.run(asyncio.create_task, operation)
    def completed(worker):
        if not worker.cancelled():
            error = worker.exception()
            if error is not None:
                event('runtime', 'background_task_failed', level='error', error=error, repeat=60)
    task.add_done_callback(completed)
    return task


def pause_controller():
    """Close Companion's own controller admission while fwupd writes; the caller then runs drain_controller()."""
    global _controller_paused, _controller_generation
    if _controller_paused:
        raise ControllerPaused('Controller maintenance is already in progress.')
    _controller_paused = True
    _controller_generation += 1


async def drain_controller():
    work = asyncio.ensure_future(asyncio.gather(*(drain(module) for module in CONTROLLER_MODULES)))
    def finished(task):
        if not task.cancelled():
            task.exception()
    work.add_done_callback(finished)
    # A timed-out/cancelled caller cannot cancel a still-running hardware thread.
    await complete(asyncio.wait_for(asyncio.shield(work), 5.0))


def resume_controller():
    global _controller_paused
    _controller_paused = False

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
    # Admission and registration contain no await: a pause cannot slip between
    # them on the backend loop. Queued/running threads remain visible to drain.
    require_available(module)
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    pending = _workers.setdefault(module, set())
    pending.add(task)
    def completed(worker):
        pending.discard(worker)
        if not worker.cancelled():
            error = worker.exception()  # Also after the caller was cancelled.
            if error is not None:
                event(module, 'worker_failed', level='warning', error=error, repeat=60)
    task.add_done_callback(completed)
    # Task cancellation cannot cancel the underlying worker or hide it from drain.
    return await asyncio.shield(task)

async def drain(module):
    while _workers.get(module):
        # Cancelling gather directly would cancel the Task around to_thread,
        # remove it from the registry and leave its actual thread running.
        workers = tuple(_workers[module])
        await complete(asyncio.gather(*workers, return_exceptions=True))
        _workers[module].difference_update(worker for worker in workers if worker.done())

def is_legion_go_2(dmi_root=DMI_ROOT):
    """LENOVO 83N0/83N1, the identity the Legion Go 2 modules share.

    Fan, RGB and controller firmware keep their own stricter gates.
    """
    def read(name):
        try:
            with open(os.path.join(dmi_root, name), encoding="ascii") as handle:
                return handle.read(128).strip()
        except (OSError, UnicodeError):
            return ""
    return read("sys_vendor") == "LENOVO" and read("product_name") in ("83N0", "83N1")


def suspend_offset():
    """CLOCK_BOOTTIME - CLOCK_MONOTONIC: grows by the time spent suspended."""
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is None or not hasattr(time, "clock_gettime"):
        return None
    try:
        return time.clock_gettime(clock) - time.monotonic()
    except (OSError, ValueError):
        return None


class ResumeWatch:
    """Calling it reports whether at least a second of suspend passed since the
    last reading; an unreadable clock (None) keeps that baseline. Battery (None
    drops the baseline) and controller/WiFi (None as 0, strictly > 1 s) differ."""
    last = None

    def mark(self):
        self.last = suspend_offset()

    def __call__(self):
        current = suspend_offset()
        if current is None:
            return False
        previous, self.last = self.last, current
        return previous is not None and current - previous >= 1.0


def require_success(result):
    if not isinstance(result, dict) or result.get('success') is not True:
        detail = result if isinstance(result, dict) else {}
        raise RuntimeError(detail.get('error') or detail.get('message')
                           or detail.get('stderr') or 'Hardware restoration was not confirmed.')

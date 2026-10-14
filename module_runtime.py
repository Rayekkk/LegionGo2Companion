# SPDX-License-Identifier: BSD-3-Clause
"""Drain hardware work before withdrawing a module, including cancelled workers."""
import asyncio
import math
import contextlib
import contextvars
import functools
from diagnostics import event
from controller_maintenance import maintenance_present

_workers = {}
CONTROLLER_MODULES = frozenset({'controller', 'remap', 'rgb', 'vibration'})
_controller_pause = None
_controller_generation = 0
_operation_generation = contextvars.ContextVar('controller_operation_generation', default=None)
_restoration_owner = contextvars.ContextVar('controller_restoration_owner', default=None)


class ControllerPaused(RuntimeError):
    """Maintenance admission is closed without changing saved module choices."""


class _ControllerPause:
    def __init__(self):
        self.quiescent = False
        self.restoring = False
        self.restoration_token = None


def is_paused(module):
    return module in CONTROLLER_MODULES and (_controller_pause is not None or maintenance_present())


def require_available(module):
    generation = _operation_generation.get()
    owner = _restoration_owner.get()
    restoring = (owner is not None and _controller_pause is not None and
                 owner is _controller_pause.restoration_token and _controller_pause.restoring)
    if (module in CONTROLLER_MODULES and owner is not None and not restoring) or (is_paused(module) and not restoring) or (module in CONTROLLER_MODULES and generation is not None
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
    """Create a lifecycle monitor, not a continuation of the initiating RPC.

    Each monitored transaction still enters its own scope. Never use this to
    revive a stale transaction/retry: clearing an operation generation is only
    appropriate at the boundary creating a new long-lived monitor.
    """
    context = contextvars.copy_context()
    context.run(_operation_generation.set, None)
    context.run(_restoration_owner.set, None)
    task = context.run(asyncio.create_task, operation)
    def completed(worker):
        if not worker.cancelled():
            error = worker.exception()
            if error is not None:
                event('runtime', 'background_task_failed', level='error', error=error, repeat=60)
    task.add_done_callback(completed)
    return task


def pause_controller():
    """Close admission synchronously, before waiting for already admitted work.

    This is only a process-local gate. It does not exclude InputPlumber, another
    plugin or a restarted Decky process and cannot authorize a USB handoff.
    """
    global _controller_pause, _controller_generation
    if _controller_pause is not None or maintenance_present():
        raise ControllerPaused('Controller maintenance is already in progress.')
    _controller_pause = _ControllerPause()
    _controller_generation += 1
    return _controller_pause


def _require_pause(pause):
    if pause is None or pause is not _controller_pause:
        raise ControllerPaused('The controller maintenance owner does not match.')


async def drain_controller(pause, *, timeout=5.0):
    _require_pause(pause)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError('Invalid controller drain deadline.')
    work = asyncio.ensure_future(asyncio.gather(*(drain(module) for module in CONTROLLER_MODULES)))
    def finished(task):
        if not task.cancelled():
            task.exception()
    work.add_done_callback(finished)
    # A timed-out/cancelled caller cannot cancel a still-running hardware thread
    # or reopen admission. The registry remains available to a later recovery.
    await complete(asyncio.wait_for(asyncio.shield(work), timeout))
    _require_pause(pause)


def confirm_controller_quiescent(pause):
    """Caller must also have joined its separate read-only capture thread."""
    _require_pause(pause)
    if pause.restoring or any(_workers.get(module) for module in CONTROLLER_MODULES):
        raise ControllerPaused('Controller workers are still stopping.')
    pause.quiescent = True


@contextlib.contextmanager
def controller_restoration(pause, *, usb_restored):
    """Private coordinator lane while ordinary admission remains closed.

    Only the current quiescent owner may enter after separately proving USB
    restoration. The boolean is its attestation, not a hardware proof supplied
    by this function. It permits existing backend transactions to restore their
    retained intent and read it back, not arbitrary frontend/controller work.
    No RPC/startup hook calls this lane. Persistent monitors do not inherit it;
    escaped continuations lose admission as soon as the owner exits the scope.
    Any failure leaves the ordinary maintenance gate closed.
    """
    # An inherited restoration capability is never a coordinator capability.
    # Check before replacing its generation, including after a previous resume.
    generation = _operation_generation.get()
    if (_restoration_owner.get() is not None or
            generation is not None and generation != _controller_generation):
        raise ControllerPaused('An expired operation cannot enter controller restoration.')
    _require_pause(pause)
    if (usb_restored is not True or not pause.quiescent or pause.restoring or
            any(_workers.get(module) for module in CONTROLLER_MODULES)):
        raise ControllerPaused('USB restoration and quiescent ownership are required.')
    pause.restoring = True
    pause.restoration_token = object()
    owner_token = _restoration_owner.set(pause.restoration_token)
    generation_token = _operation_generation.set(_controller_generation)
    try:
        yield
    finally:
        pause.restoring = False
        pause.restoration_token = None
        _operation_generation.reset(generation_token)
        _restoration_owner.reset(owner_token)


def resume_controller(pause, *, restored):
    """No timeout/cancellation may implicitly reopen maintenance admission."""
    global _controller_pause
    _require_pause(pause)
    if restored is not True or not pause.quiescent or pause.restoring:
        raise ControllerPaused('Controller restoration has not been confirmed.')
    if any(_workers.get(module) for module in CONTROLLER_MODULES):
        raise ControllerPaused('Controller workers are still stopping.')
    _controller_pause = None

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

def require_success(result):
    if not isinstance(result, dict) or result.get('success') is not True:
        detail = result if isinstance(result, dict) else {}
        raise RuntimeError(detail.get('error') or detail.get('message')
                           or detail.get('stderr') or 'Hardware restoration was not confirmed.')

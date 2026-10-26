# SPDX-License-Identifier: BSD-3-Clause
"""Drain hardware work before withdrawing a module, including cancelled workers."""
import asyncio
import contextlib
import contextvars
import functools
import os
import re
import stat
import threading
import time
from diagnostics import event

DMI_ROOT = "/sys/class/dmi/id"

_workers = {}
_starting = contextvars.ContextVar('starting_module', default=None)


class StartStopped(RuntimeError):
    """A stop came while this module was starting; its start makes no further firmware call."""


class _FirmwareLock:
    """A start whose stop was requested finishes the firmware call in flight but
    begins no other: with the EC busy each call can take seconds, and Decky
    SIGKILLs a stop still waiting on the start's thread after 5 s."""

    def __init__(self):
        self._lock = threading.Lock()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        self._lock.acquire()
        if _starting.get() in _stop_requested:
            self._lock.release()
            raise StartStopped('Stopped while starting; no further firmware call was made.')

    def __exit__(self, *exc_info):
        self._lock.release()


# The Legion's AML aborts EC/WMI methods that run concurrently ("cannot release
# Mutex [ECMT]"). Hold only around one firmware-backed sysfs write, innermost,
# from worker threads only: never take another lock or block on I/O beyond it.
firmware_lock = _FirmwareLock()
# The controller MCU answers one configuration command at a time and the Go S driver
# matches no reply to its request. RGB's sysfs transactions (each access is a command)
# and Vibration's raw Go S frames hold this lock so their commands never interleave.
controller_config_lock = threading.RLock()
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


@contextlib.contextmanager
def starting(module):
    """Mark a module's start; its threads and tasks inherit the mark (firmware_lock)."""
    token = _starting.set(module)
    try:
        yield
    finally:
        _starting.reset(token)


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
    """Start a long-lived monitor outside the caller's operation generation and outside a start's
    stop mark (firmware_lock); never use it to revive a stale retry."""
    context = contextvars.copy_context()
    context.run(_operation_generation.set, None)
    context.run(_starting.set, None)
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

def read_sysfs_text(path, limit=512, *, error=OSError, subject=None):
    """Read a small ASCII sysfs attribute without following a final symlink.

    Anything that is not a regular file, or is longer than `limit`, raises
    `error`; a missing file stays the OSError from open, bad bytes a UnicodeError.
    """
    subject = subject or f"sysfs attribute {path}"
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise error(f"{subject} changed during access.")
        raw = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise error(f"{subject} returned an oversized value.")
    return raw.decode("ascii", errors="strict").strip()


CONTROLLER_READ_GAP_S = 0.15


def settled_read(read, reads=4):
    """The value two reads in a row agree on, or None after `reads` reads.

    Every hid-lenovo-go attribute read sends a command to the pad, and the driver can hand back
    the previous command's reply (one completion for all), so a single read can be stale."""
    last = read()
    for _ in range(reads - 1):
        time.sleep(CONTROLLER_READ_GAP_S)
        value = read()
        if value == last:
            return value
        last = value
    return None


# Machine type (DMI product_name) -> console: go1 Legion Go, gos Legion Go S, go2 Legion Go 2.
LEGION_GO_MODELS = {"83E1": "go1", "83L3": "gos", "83N6": "gos", "83N0": "go2", "83N1": "go2"}
# Left out on purpose (owner's decision), so product_version does not place them either.
EXCLUDED_MODELS = frozenset({"83Q2", "83Q3"})
HID_DEVICES = "/sys/bus/hid/devices"


def legion_device(dmi_root=None):
    """(console, machine type) of a Lenovo Legion Go; console is None outside the family.

    A machine type not in the table is placed by product_version, never as a Go 2:
    the Go 2 modules stay with the two types they were tested on. Fan, the power
    light and controller firmware keep their own stricter gates.
    """
    def read(name):
        try:
            with open(os.path.join(dmi_root or DMI_ROOT, name), encoding="ascii") as handle:
                return handle.read(128).strip()
        except (OSError, UnicodeError):
            return ""
    if read("sys_vendor") != "LENOVO":
        return None, ""
    model = read("product_name")
    console = LEGION_GO_MODELS.get(model)
    if console is None and model not in EXCLUDED_MODELS:
        version = read("product_version")
        console = "gos" if version.startswith("Legion Go S ") else "go1" if version == "Legion Go 8APU1" else None
    return console, model


def is_legion_go_2(dmi_root=None):
    """LENOVO 83N0/83N1, the identity the Legion Go 2 modules share."""
    return legion_device(dmi_root)[0] == "go2"


def kernel_controller(hid_root=HID_DEVICES):
    """A controller served by hid-lenovo-go: the Go 2's, or a Go 1's on the 2025 controller firmware
    (61EB-61EE). The Go 1's older firmware (6182-6185) has no kernel driver for vibration, lights or gyro."""
    try:
        names = os.listdir(hid_root)
    except OSError:
        return False
    return any(re.fullmatch(r"0003:17EF:61E[B-E]\.[0-9A-F]+", name, re.IGNORECASE)
               and os.path.basename(os.path.realpath(os.path.join(hid_root, name, "driver"))) == "hid-lenovo-go"
               for name in names)


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
    last reading; an unreadable clock (None) keeps that baseline."""
    last = None

    def mark(self):
        self.last = suspend_offset()

    def __call__(self):
        current = suspend_offset()
        if current is None:
            return False
        previous, self.last = self.last, current
        return previous is not None and current - previous >= 1.0


class NotConfirmed(RuntimeError):
    """A backend reply that did not confirm success; ``code`` is its ``error`` field, if any."""
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def require_success(result):
    if not isinstance(result, dict) or result.get('success') is not True:
        detail = result if isinstance(result, dict) else {}
        raise NotConfirmed(detail.get('error') or detail.get('message')
                           or detail.get('stderr') or 'Hardware restoration was not confirmed.',
                           detail.get('error'))

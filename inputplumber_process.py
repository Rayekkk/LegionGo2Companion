# SPDX-License-Identifier: BSD-3-Clause
"""InputPlumber D-Bus access (busctl) shared by remap and gyro, plus cheap process-exit hints.

D-Bus/device ownership still authorizes every repair; ProcessWatch only speeds it up.
LostControllerRecovery restarts an InputPlumber that dropped the controller.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable, Any

import module_runtime
from diagnostics import event
from system_process import run


BUSCTL = "/usr/bin/busctl"
PROC_ROOT = Path("/proc")
SERVICE = "org.shadowblip.InputPlumber"
INTERFACE = "org.shadowblip.Input.CompositeDevice"
DEVICE_PATH_RE = re.compile(r"^/org/shadowblip/InputPlumber/CompositeDevice[0-9]+$")
_OWNER = re.compile(r"^:[0-9]+\.[0-9]+$")


class InputPlumberError(RuntimeError):
    pass


def run_busctl(args: list[str], *, timeout: float = 5.0) -> str:
    if not os.path.isfile(BUSCTL):
        raise InputPlumberError("InputPlumber control interface is unavailable.")
    try:
        result = run([BUSCTL, *args], timeout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InputPlumberError(f"InputPlumber did not respond: {exc}") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise InputPlumberError(message[:500] or "InputPlumber rejected the request.")
    return result.stdout


def json_value(args: list[str]) -> Any:
    raw = run_busctl(["--json=short", *args])
    try:
        payload = json.loads(raw)
        return payload["data"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InputPlumberError("InputPlumber returned an invalid D-Bus response.") from exc


def _read_process(pid: int) -> tuple[str, int] | None:
    try:
        with (PROC_ROOT / str(pid) / "stat").open(encoding="ascii") as handle:
            raw = handle.read(4097)
        if len(raw) > 4096 or not raw.startswith(f"{pid} ("):
            return None
        # comm can contain spaces and parentheses; split after its final ')'.
        fields = raw[raw.rindex(")") + 1:].split()
        if len(fields) < 20 or fields[0] not in {"R", "S", "D", "Z", "T", "t", "X", "x", "K", "W", "P", "I"}:
            return None
        started = int(fields[19])  # field 22; fields[0] is process state (3).
        return (fields[0], started) if started >= 0 else None
    except FileNotFoundError:
        return ("missing", 0) if PROC_ROOT.is_dir() else None
    except (OSError, UnicodeError, ValueError):
        return None


class ProcessWatch:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revision = 0
        self._token: tuple[str, int, int] | None = None

    def clear(self) -> None:
        with self._lock:
            self._revision += 1
            self._token = None

    def capture(self, query: Callable[[list[str]], Any], owner: str | None = None) -> None:
        """Call only after a successful operation, never from a status query.

        Missing metadata degrades to the existing slow watchdog. An in-flight
        capture cannot revive a cleared token or supersede a newer capture.
        """
        with self._lock:
            if self._token and (owner is None or owner == self._token[0]):
                return
            # Reserve this capture before I/O: neither an older capture nor a
            # tick checking its old token may supersede the new observation.
            self._revision += 1
            revision = self._revision
            self._token = None
        token = None
        try:
            if not PROC_ROOT.is_dir():
                return
            prefix = ["call", "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus"]
            if owner is None:
                result = query([*prefix, "GetNameOwner", "s", SERVICE])
                owner = result[0] if isinstance(result, list) and len(result) == 1 else None
            if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
                return
            result = query([*prefix, "GetConnectionUnixProcessID", "s", owner])
            if (isinstance(result, list) and len(result) == 1
                    and type(result[0]) is int and 0 < result[0] <= 0xffffffff):
                pid = result[0]
                process = _read_process(pid)
                if process is not None and process[0] not in {"missing", "Z", "X", "x"}:
                    token = (owner, pid, process[1])
        except Exception:
            # A successful hardware operation must not fail on optional metadata.
            pass
        with self._lock:
            if self._revision == revision:
                self._token = token

    def consume_exit(self) -> bool:
        """Return one hint per dead process, with no subprocess or D-Bus calls."""
        with self._lock:
            token, revision = self._token, self._revision
        if token is None:
            return False
        process = _read_process(token[1])
        if process is None or (process[0] not in {"missing", "Z", "X", "x"} and process[1] == token[2]):
            return False
        with self._lock:
            if self._revision != revision or self._token != token:
                return False
            self._revision += 1
            self._token = None
            return True


# InputPlumber 0.78 (SteamOS 3.8) can lose the controller for good when it re-enumerates twice in a
# row (an MCU reset 61EB -> 61ED -> 61EB, or a wake): the second add fails with EBUSY, the
# CompositeDevice stops and the controller stays hidden until the service restarts. Fixed upstream
# in 0.79.1. Remap and gyro already recover from a restart through ProcessWatch.
SYSTEMCTL = "/usr/bin/systemctl"
UNIT = "inputplumber.service"
USER_OFF = frozenset({"disabled", "masked", "masked-runtime"})
CONTROLLER_HID = re.compile(r"0003:17EF:61E[B-E]\.[0-9A-F]+", re.IGNORECASE)
SETTLE_S = 12.0  # after the last controller event, wake or start: InputPlumber's own time to recover
CONFIRM_S = 5.0  # the lost state must still hold this much later
RESUME_POLL_S = 30.0  # a clock read only (ResumeWatch); the controller's own udev events usually come first
RESTART_SPACING_S = 600.0
RESTARTS_PER_RUN = 3
HELD_RETRY_S = 60.0  # a check held by maintenance, fwupd or an updater is tried again this often
HELD_RETRIES = 30  # per trigger: half an hour, then the next controller event or wake re-arms it
FWUPD = "org.freedesktop.fwupd"
# FwupdStatus values that mean a device is being touched (2.0.x enum). An idle fwupd 2.0.18 keeps
# reporting a stale earlier value (measured: 3, DECOMPRESSING), so only these hold the restart.
FWUPD_DEVICE_WORK = frozenset({"4", "5", "6", "9", "10", "11", "12", "14"})  # RESTART WRITE VERIFY READ ERASE AUTH BUSY USER


def lost_controller() -> dict | None:
    """The unit state when a hid-lenovo-go controller is present and InputPlumber, enabled and
    running, holds no CompositeDevice; None when anything differs or cannot be read."""
    if not module_runtime.kernel_controller():
        return None
    try:
        result = run([SYSTEMCTL, "show", "--property=LoadState,ActiveState,UnitFileState", UNIT], 3.0,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                     encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    state = dict(line.partition("=")[::2] for line in result.stdout.splitlines())
    # Stopped, disabled or masked by the user (or by another controller daemon): never touch it.
    if (result.returncode != 0 or state.get("LoadState") != "loaded" or state.get("ActiveState") != "active"
            or state.get("UnitFileState", "") in USER_OFF):
        return None
    try:
        tree = run_busctl(["--auto-start=no", "tree", SERVICE, "--list", "--no-pager"], timeout=3.0)
    except InputPlumberError:
        return None  # An unanswered query proves nothing.
    if any(DEVICE_PATH_RE.fullmatch(line.strip()) for line in tree.splitlines()):
        return None
    return {"status": state.get("UnitFileState") or None}  # the unit-file state, for the log


def fwupd_busy() -> bool:
    """True while the fwupd daemon (Discover, fwupdmgr, a Steam update) works on a device, or when
    that cannot be ruled out: an unreadable Status is not idle."""
    bus = ["--auto-start=no", "--system"]
    try:
        fields = run_busctl([*bus, "get-property", FWUPD, "/", FWUPD, "Status"], timeout=2.0).split()
        return not (len(fields) == 2 and fields[0] == "u" and fields[1].isdigit()) or fields[1] in FWUPD_DEVICE_WORK
    except InputPlumberError:
        pass
    try:  # No Status: fine only when fwupd is not on the bus at all (it is D-Bus activated).
        owned = run_busctl([*bus, "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                            "org.freedesktop.DBus", "NameHasOwner", "s", FWUPD], timeout=2.0).split()
    except InputPlumberError:
        return True
    return owned != ["b", "false"]


class LostControllerRecovery:
    """Event-driven: one check SETTLE_S after start, a wake or the controller's last udev event;
    a restart needs the lost state twice, CONFIRM_S apart, and stays within the limits. A check
    held by maintenance is repeated every HELD_RETRY_S, at most HELD_RETRIES times."""

    def __init__(self, blocked: Callable[[], bool], updating: Callable[[], bool]) -> None:
        self._blocked = blocked  # On the loop: closing, conflict guard, controller maintenance.
        self._updating = updating  # In a thread: a firmware updater process (fwupd itself is checked here).
        self._wake = asyncio.Event()
        self._restarts: list[float] = []
        self._verify = False  # A restart was requested and InputPlumber has not shown the controller since.

    @property
    def restarts(self) -> int:
        """Restarts requested since this Companion start (the device report shows it)."""
        return len(self._restarts)

    def _drain(self, monitor) -> None:
        try:
            for device in iter(lambda: monitor.poll(0), None):
                if CONTROLLER_HID.fullmatch(device.sys_name):
                    self._wake.set()
        except Exception:
            self._wake.set()  # A lost or unreadable event may have been the controller's.

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        monitor = _hid_monitor(loop, self._drain)
        resume = module_runtime.ResumeWatch()
        resume.mark()
        trigger, due = "start", loop.time() + SETTLE_S
        held = 0
        try:
            while True:
                wait = RESUME_POLL_S if due is None else min(max(due - loop.time(), 0), RESUME_POLL_S)
                try:
                    await asyncio.wait_for(self._wake.wait(), wait)
                    self._wake.clear()
                    trigger, due = "controller", loop.time() + SETTLE_S  # every event restarts the window
                    held = 0
                    continue
                except asyncio.TimeoutError:
                    pass
                if resume():
                    trigger, due = "resume", loop.time() + SETTLE_S
                    held = 0
                elif due is not None and loop.time() >= due:
                    due = None
                    try:
                        result = await self._check(trigger)
                        if result == "restarted":
                            trigger, due = "verify", loop.time() + SETTLE_S
                        if result != "held":
                            held = 0
                        elif held < HELD_RETRIES:
                            held += 1
                            due = loop.time() + HELD_RETRY_S
                        else:
                            event("inputplumber", "recovery_held_too_long", level="warning", reason=trigger)
                    except Exception as exc:
                        event("inputplumber", "recovery_check_failed", level="warning", error=exc, repeat=600)
        finally:
            if monitor is not None:
                loop.remove_reader(monitor.fileno())
                monitor = None  # The last reference: libudev unrefs the monitor and closes its socket.

    async def _check(self, trigger: str) -> str | None:
        """"restarted" after a restart was requested, "held" when maintenance prevented the check."""
        if self._blocked():
            return "held"
        state = await asyncio.to_thread(lost_controller)
        if state is not None:
            await asyncio.sleep(CONFIRM_S)
            if self._blocked():
                return "held"
            state = await asyncio.to_thread(lost_controller)
        if state is None:
            if self._verify:
                event("inputplumber", "controller_recovered_after_restart")
            self._verify = False
            return None
        if self._verify:
            # A restart that brought no CompositeDevice back will not on the next try either.
            event("inputplumber", "restart_did_not_recover_controller", level="warning", repeat=600, **state)
            return None
        if (await asyncio.to_thread(self._updating) or await asyncio.to_thread(fwupd_busy)
                or self._blocked()):
            return "held"
        now = asyncio.get_running_loop().time()
        if len(self._restarts) >= RESTARTS_PER_RUN or (self._restarts and now - self._restarts[-1] < RESTART_SPACING_S):
            event("inputplumber", "restart_limited", level="warning", repeat=600, reason=trigger,
                  count=len(self._restarts), **state)
            return None
        self._restarts.append(now)
        self._verify = True
        event("inputplumber", "restart_for_lost_controller", level="warning", reason=trigger,
              count=len(self._restarts), **state)
        # try-restart: a service stopped meanwhile stays stopped. --no-block: no wait on the job.
        result = await asyncio.to_thread(run, [SYSTEMCTL, "--no-block", "try-restart", UNIT], 5.0,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            event("inputplumber", "restart_failed", level="warning", returncode=result.returncode)
        return "restarted"


def _hid_monitor(loop, drain):
    """A netlink socket the kernel filters to HID events, read by the event loop (no thread).
    Without it the start and every wake still trigger a check."""
    try:
        import pyudev
        monitor = pyudev.Monitor.from_netlink(pyudev.Context())
        monitor.filter_by("hid")
        monitor.start()
        loop.add_reader(monitor.fileno(), drain, monitor)
        return monitor
    except Exception as exc:
        event("inputplumber", "hotplug_watch_unavailable", level="warning", error=exc)
        return None

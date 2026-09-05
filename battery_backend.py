# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Battery conservation through the existing Linux power-supply interface.

Only the kernel's advertised Standard/Fast/Long_Life modes are used. No EC,
WMI, charge-behaviour commands, probing writes, or percentage emulation.
"""

from __future__ import annotations

import asyncio
import copy
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any

import decky
from safe_settings import SettingsManager


SYS_ROOT = Path("/sys")
CHECK_INTERVAL_S = 60.0
RESUME_CHECK_S = 5.0
ALLOWED_MODES = frozenset({"Standard", "Fast", "Long_Life"})
DEFAULT_STATE = {
    "schema_version": 1, "managed": False, "requested_enabled": None,
    "baseline": None, "normal_mode": None, "last_applied": None,
}
settings = SettingsManager("battery_settings", decky.DECKY_PLUGIN_SETTINGS_DIR)


class BatteryError(RuntimeError):
    pass


def _checked_path(path: Path) -> Path:
    root = SYS_ROOT.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise BatteryError("Battery interface resolves outside sysfs.")
    # Class/device parent links are part of sysfs; an attribute itself must be
    # a real regular kernel attribute, never an additional link.
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BatteryError("Battery interface is not a regular sysfs attribute.")
    return resolved


def _read_attr(path: Path, limit: int = 512) -> str:
    resolved = _checked_path(path)
    fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BatteryError("Battery interface changed during access.")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise BatteryError("Battery interface returned an oversized value.")
    return data.decode("ascii", errors="strict").strip()


def _optional_attr(path: Path) -> str | None:
    try:
        return _read_attr(path)
    except FileNotFoundError:
        return None


def _parse_modes(value: str) -> tuple[list[str], str]:
    tokens = value.split()
    if not tokens or any(not re.fullmatch(r"\[?[A-Za-z][A-Za-z_]*\]?", t)
                         for t in tokens):
        raise BatteryError("The battery returned malformed charging modes.")
    selected = [t[1:-1] for t in tokens if t.startswith("[") and t.endswith("]")]
    if len(selected) != 1 or any((t.startswith("[")) != (t.endswith("]"))
                                 for t in tokens):
        raise BatteryError("The battery did not report one active charging mode.")
    options = [t.strip("[]") for t in tokens]
    if len(options) != len(set(options)) or not {"Standard", "Long_Life"}.issubset(options):
        raise BatteryError("The battery does not advertise the required protection modes.")
    if selected[0] not in ALLOWED_MODES:
        raise BatteryError("The current charging mode cannot be safely preserved.")
    return options, selected[0]


def _detect() -> dict[str, Any]:
    identity = SYS_ROOT / "class/dmi/id"
    vendor = _read_attr(identity / "sys_vendor", 128)
    product = _read_attr(identity / "product_name", 128)
    if vendor != "LENOVO" or product not in {"83N0", "83N1"}:
        raise BatteryError("Battery protection is supported only on Lenovo Legion Go 2.")
    batteries = []
    for candidate in (SYS_ROOT / "class/power_supply").iterdir():
        if not candidate.resolve(strict=True).is_relative_to(SYS_ROOT.resolve(strict=True)):
            raise BatteryError("A power-supply device resolves outside sysfs.")
        if not candidate.is_dir() or _read_attr(candidate / "type") != "Battery":
            continue
        scope = _optional_attr(candidate / "scope")
        # ACPI system batteries often omit scope. BAT* avoids mistaking a
        # controller's HID battery for the console battery in that case.
        if scope != "System" and (scope is not None or not re.fullmatch(r"BAT\d+", candidate.name)):
            continue
        present = _optional_attr(candidate / "present")
        if present == "0":
            continue
        if present not in {None, "1"}:
            raise BatteryError("The system battery reported an invalid presence state.")
        batteries.append(candidate)
    if len(batteries) != 1:
        raise BatteryError("No system battery was found." if not batteries else
                           "More than one system battery was found; no battery was selected.")
    battery = batteries[0]
    attribute = battery / "charge_types"
    options, current = _parse_modes(_read_attr(attribute))
    if not _checked_path(attribute).stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise BatteryError("The kernel exposes battery charging modes as read-only.")
    return {"path": attribute, "battery": battery, "options": options, "current": current}


def _write_mode(path: Path, mode: str) -> None:
    if mode not in ALLOWED_MODES:
        raise BatteryError("Unsupported battery mode.")
    resolved = _checked_path(path)
    fd = os.open(resolved, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BatteryError("Battery interface changed during access.")
        payload = (mode + "\n").encode("ascii")
        if os.write(fd, payload) != len(payload):
            raise BatteryError("The kernel accepted only part of the battery setting.")
    finally:
        os.close(fd)


def _suspend_offset() -> float | None:
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is None or not hasattr(time, "clock_gettime"):
        return None
    try:
        return time.clock_gettime(clock) - time.monotonic()
    except (OSError, ValueError):
        return None


class Plugin:
    def __init__(self):
        self._lock = threading.RLock()
        self._task: asyncio.Task | None = None
        self._running = False
        self._error = ""

    @staticmethod
    def _validated_state(raw) -> dict[str, Any]:
        if not isinstance(raw, dict) or type(raw.get("managed", False)) is not bool:
            raise BatteryError("Saved battery settings are invalid; no setting was applied.")
        if not raw.get("managed", False):
            return copy.deepcopy(DEFAULT_STATE)
        if (type(raw.get("schema_version")) is not int or raw.get("schema_version") != 1
                or type(raw.get("requested_enabled")) is not bool
                or raw.get("baseline") not in ALLOWED_MODES
                or raw.get("normal_mode") not in {"Standard", "Fast"}
                or raw.get("last_applied") not in ALLOWED_MODES):
            raise BatteryError("Saved battery settings lack a valid restoration state.")
        return {key: raw[key] for key in DEFAULT_STATE}

    def _load(self) -> dict[str, Any]:
        if getattr(settings, "recovery_error", ""):
            # Allow a verified file/backup restored elsewhere to recover the
            # module; healthy reads remain memory-only.
            settings.read()
        if getattr(settings, "recovery_error", ""):
            raise BatteryError("Saved battery ownership could not be recovered. " + settings.recovery_error)
        return self._validated_state(settings.getSetting("state", DEFAULT_STATE))

    def _save(self, state: dict[str, Any]) -> None:
        payload = self._snapshot()
        payload.pop("pending", None)
        payload["state"] = copy.deepcopy(state)
        settings.replace(payload)

    def _snapshot(self) -> dict[str, Any]:
        # Keep unrelated keys during rollback and avoid retaining rejected
        # in-memory values even if a replacement SettingsManager raises early.
        return copy.deepcopy(settings.settings)

    def _recover_pending(self) -> None:
        """Roll an interrupted hardware transaction back before new writes.

        The journal is prepared durably before changing charge_types. A valid
        primary state without a journal means the operation was committed.
        Status reads deliberately do not call this method.
        """
        pending = settings.getSetting("pending", None)
        if pending is None:
            return
        if (not isinstance(pending, dict) or type(pending.get("version")) is not int
                or pending.get("version") != 1
                or not isinstance(pending.get("before_mode"), str)
                or pending["before_mode"] not in ALLOWED_MODES
                or not isinstance(pending.get("target"), str)
                or pending["target"] not in ALLOWED_MODES
                or not isinstance(pending.get("previous"), dict)
                or "pending" in pending["previous"]):
            raise BatteryError("The interrupted battery change has an invalid recovery record; no hardware was changed.")
        self._validated_state(pending["previous"].get("state", DEFAULT_STATE))
        device = _detect()
        before, target = pending["before_mode"], pending["target"]
        if device["current"] == target and target != before:
            if before not in device["options"]:
                raise BatteryError("The original charging mode is unavailable; recovery remains pending.")
            _write_mode(device["path"], before)
            _, actual = _parse_modes(_read_attr(device["path"]))
            if actual != before:
                raise BatteryError("The battery did not confirm recovery of its original mode.")
        # A third mode was selected elsewhere: retain that hardware state.
        # The previously committed Companion preference remains the preference.
        previous = copy.deepcopy(pending["previous"])
        try:
            settings.replace(previous)
        except Exception as exc:
            # Keep the record actionable in memory even if the failing commit
            # has already replaced its primary file on disk.
            retained = copy.deepcopy(previous)
            retained["pending"] = pending
            settings.settings = retained
            raise BatteryError(f"Battery recovery could not be saved: {exc}") from exc
        self._error = ""

    def _status(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {
                "success": True, "supported": False, "managed": False,
                "enabled": None, "requested_enabled": None,
                "charging_status": None, "capacity": None, "reason": "",
                "error": self._error, "backend": "charge_types", "options": [],
                "current_mode": None, "baseline": None, "recovery_pending": False,
            }
            try:
                state = self._load()
                out.update({key: state[key] for key in ("managed", "requested_enabled", "baseline")})
                if settings.getSetting("pending", None) is not None:
                    out.update(success=False, recovery_pending=True,
                               error=self._error or "An interrupted battery change needs recovery before further changes.")
            except Exception as exc:
                out.update(success=False, error=str(exc), reason="Saved settings could not be read.")
                return out
            try:
                device = _detect()
                out.update(supported=True, enabled=device["current"] == "Long_Life",
                           current_mode=device["current"], options=device["options"])
                capacity = _optional_attr(device["battery"] / "capacity")
                if capacity is not None and capacity.isdecimal() and 0 <= int(capacity) <= 100:
                    out["capacity"] = int(capacity)
                out["charging_status"] = _optional_attr(device["battery"] / "status")
            except Exception as exc:
                out["reason"] = str(exc)
            if self._error:
                out["success"] = False
            return out

    def _transact(self, device: dict[str, Any], target: str | None,
                  state: dict[str, Any]) -> None:
        snapshot = self._snapshot()
        if snapshot.get("pending") is not None:
            raise BatteryError("An interrupted battery change must be recovered first.")
        before = device["current"]
        attempted = False
        journal = copy.deepcopy(snapshot)
        journal["pending"] = {
            "version": 1, "before_mode": before, "target": target,
            "previous": copy.deepcopy(snapshot),
        }
        try:
            if target is not None and target not in device["options"]:
                raise BatteryError("The original charging mode is no longer supported by the kernel.")
            if target is not None and target != before:
                # Store the exact original mode (including Fast) before its
                # first possible loss during a process or power failure.
                settings.replace(journal)
                attempted = True
                _write_mode(device["path"], target)
                _, actual = _parse_modes(_read_attr(device["path"]))
                if actual != target:
                    raise BatteryError("The battery did not confirm the requested charging mode.")
            # Remove the recovery record in the same atomic commit as the new
            # setting. A crash sees either the prepared journal or this commit.
            self._save(state)
        except Exception as exc:
            failures = []
            hardware_restored = True
            if attempted:
                try:
                    # Avoid replacing a third mode chosen externally during
                    # this operation. If readback itself is broken, attempt
                    # the exact pre-operation mode and verify restoration.
                    try:
                        _, actual = _parse_modes(_read_attr(device["path"]))
                    except Exception:
                        actual = None
                    if actual not in {None, target, before}:
                        raise BatteryError("Charging mode changed externally; rollback did not overwrite it.")
                    if actual != before:
                        _write_mode(device["path"], before)
                    _, restored = _parse_modes(_read_attr(device["path"]))
                    if restored != before:
                        raise BatteryError("The original charging mode could not be confirmed.")
                except Exception as rollback:
                    hardware_restored = False
                    failures.append(f"Hardware rollback failed: {rollback}")
            rollback_snapshot = snapshot if hardware_restored else journal
            try:
                # A failed filesystem fsync can occur after an atomic rename;
                # explicitly restore the durable snapshot as well as memory.
                settings.replace(rollback_snapshot)
            except Exception as rollback:
                settings.settings = copy.deepcopy(rollback_snapshot)
                failures.append(f"Settings rollback failed: {rollback}")
            suffix = " " + " ".join(failures) if failures else " Previous settings were restored."
            raise BatteryError(f"Battery setting failed: {exc}.{suffix}") from exc

    def _set(self, enabled: bool) -> dict[str, Any]:
        with self._lock:
            if type(enabled) is not bool:
                raise BatteryError("Battery protection must be enabled or disabled with a boolean value.")
            self._recover_pending()
            state = self._load()
            device = _detect()
            if not state["managed"]:
                state.update(managed=True, baseline=device["current"],
                             normal_mode=device["current"] if device["current"] in {"Fast", "Standard"} else "Standard")
            target = "Long_Life" if enabled else state["normal_mode"]
            state.update(requested_enabled=enabled, last_applied=target)
            self._transact(device, target, state)
            self._error = ""
            return self._status()

    def _release(self) -> dict[str, Any]:
        with self._lock:
            self._recover_pending()
            state = self._load()
            if not state["managed"]:
                self._error = ""
                return self._status()
            device = _detect()
            target = state["baseline"] if device["current"] == state["last_applied"] else None
            self._transact(device, target, copy.deepcopy(DEFAULT_STATE))
            self._error = ""
            return self._status()

    def _repair(self) -> None:
        with self._lock:
            if not self._running:
                return
            try:
                self._recover_pending()
                state = self._load()
                if not state["managed"]:
                    self._error = ""
                    return
                device = _detect()
                target = "Long_Life" if state["requested_enabled"] else state["normal_mode"]
                if device["current"] != target:
                    state["last_applied"] = target
                    self._transact(device, target, state)
                self._error = ""
            except Exception as exc:
                message = str(exc)
                if message != self._error:
                    decky.logger.warning(f"[companion-battery] Saved setting could not be applied: {message}")
                self._error = message

    async def _thread(self, fn, *args):
        # Cancellation cannot abandon an in-flight sysfs/settings transaction.
        task = asyncio.create_task(asyncio.to_thread(fn, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _watch(self):
        last_check = time.monotonic()
        interval = RESUME_CHECK_S if self._error else CHECK_INTERVAL_S
        retry_delay = RESUME_CHECK_S
        offset = _suspend_offset()
        while True:
            await asyncio.sleep(RESUME_CHECK_S)
            now = time.monotonic()
            current_offset = _suspend_offset()
            resumed = (offset is not None and current_offset is not None
                       and current_offset - offset >= 1.0)
            offset = current_offset
            if resumed or now - last_check >= interval:
                if resumed:
                    retry_delay = RESUME_CHECK_S
                await self._thread(self._repair)
                last_check = now
                # Firmware attributes can be unavailable during the first
                # wake tick. Retry promptly, then back off if it stays absent;
                # a healthy battery keeps the normal low-frequency check.
                if self._error:
                    interval = retry_delay
                    retry_delay = min(CHECK_INTERVAL_S, retry_delay * 2)
                else:
                    interval = CHECK_INTERVAL_S
                    retry_delay = RESUME_CHECK_S

    async def _main(self):
        await self._thread(self._load)
        with self._lock:
            self._running = True
        await self._thread(self._repair)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._watch())

    async def _unload(self):
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # The monitor and any previously started transaction have completed.
        with self._lock:
            self._running = False

    async def _uninstall(self):
        await self._unload()
        result = await self.release_control()
        if not result["success"]:
            decky.logger.warning(f"[companion-battery] Uninstall restore failed: {result['error']}")

    async def get_status(self):
        result = await self._thread(self._status)
        if not result["success"]:
            return {**result, "status": copy.deepcopy(result)}
        return result

    async def set_enabled(self, enabled: bool):
        try:
            status = await self._thread(self._set, enabled)
            return {"success": True, "status": status}
        except Exception as exc:
            self._error = str(exc)
            return {"success": False, "error": str(exc), "status": await self.get_status()}

    async def release_control(self):
        try:
            status = await self._thread(self._release)
            return {"success": True, "status": status}
        except Exception as exc:
            self._error = str(exc)
            return {"success": False, "error": str(exc), "status": await self.get_status()}

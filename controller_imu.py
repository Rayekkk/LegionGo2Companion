# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Checked Go 2 IMU bypass access for the controller's common transaction.

This module has no settings, worker, or startup writes. The caller must prepare
its durable journal (including baseline/expected/desired) before apply(), then
commit it only after the corresponding InputPlumber filter change succeeds.
"""

from __future__ import annotations

import os
import re
import stat
import threading
from pathlib import Path
from typing import Any

SYS_ROOT = Path("/sys")
HID_DEVICES = Path("/sys/bus/hid/devices")
SIDES = ("left", "right")
_lock = threading.RLock()


class ImuError(RuntimeError):
    pass


def _regular(path: Path, within: Path | None = None) -> Path:
    resolved = path.resolve(strict=True)
    boundary = (within or SYS_ROOT).resolve(strict=True)
    if not resolved.is_relative_to(boundary):
        raise ImuError("The IMU attribute resolves outside its checked sysfs device.")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ImuError("The IMU attribute is not a regular sysfs file.")
    return resolved


def _read(path: Path, within: Path | None = None) -> str:
    resolved = _regular(path, within)
    fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ImuError("The IMU attribute changed during access.")
        raw = os.read(fd, 513)
    finally:
        os.close(fd)
    if len(raw) > 512:
        raise ImuError("The IMU attribute returned an oversized value.")
    return raw.decode("ascii", errors="strict").strip()


def _driver_name(device: Path) -> str:
    driver = (device / "driver").resolve(strict=True)
    if not driver.is_relative_to(SYS_ROOT.resolve(strict=True)):
        raise ImuError("The controller driver resolves outside sysfs.")
    return driver.name


def _attribute(device: Path, side: str) -> Path:
    if side not in SIDES:
        raise ImuError("Unsupported controller side.")
    # imu_enabled is intentionally excluded: this kernel has a right-side
    # alias bug in that attribute. Only the validated bypass setting is used.
    return device / f"{side}_handle" / "imu_bypass_enabled"


def _read_bool(device: Path, side: str) -> bool:
    value = _read(_attribute(device, side), device)
    if value not in {"true", "false"}:
        raise ImuError(f"The {side} controller returned an invalid IMU bypass state.")
    return value == "true"


def _values(device: Path) -> dict[str, bool]:
    return {side: _read_bool(device, side) for side in SIDES}


def _discover() -> Path:
    dmi = SYS_ROOT / "class/dmi/id"
    if (_read(dmi / "sys_vendor") != "LENOVO"
            or _read(dmi / "product_name") not in {"83N0", "83N1"}):
        raise ImuError("IMU control is available only on Lenovo Legion Go 2.")
    matches = []
    for entry in HID_DEVICES.iterdir():
        if not re.fullmatch(r"0003:17EF:61E[B-E]\.[0-9A-F]+", entry.name, re.IGNORECASE):
            continue
        device = entry.resolve(strict=True)
        if not device.is_relative_to((SYS_ROOT / "devices").resolve(strict=True)):
            raise ImuError("The controller resolves outside physical sysfs devices.")
        if (_driver_name(device) != "hid-lenovo-go"
                or _read(device.parent / "bInterfaceNumber") != "02"):
            continue
        if _read(device / "hardware_generation", device) != "2":
            continue
        matches.append(device)
    if len(matches) != 1:
        raise ImuError("A compatible Go 2 IMU interface was not found." if not matches else
                       "More than one Go 2 IMU interface was found; none was selected.")
    device = matches[0]
    for side in SIDES:
        attribute = _attribute(device, side)
        options = _read(attribute.with_name("imu_bypass_enabled_index"), device).split()
        if len(options) != 2 or set(options) != {"true", "false"}:
            raise ImuError(f"The {side} controller does not advertise the expected bypass values.")
        info = _regular(attribute, device).stat()
        if not info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise ImuError(f"The {side} controller IMU bypass is read-only.")
        _read_bool(device, side)
    return device


def _checked_values(value: Any) -> dict[str, bool]:
    if (not isinstance(value, dict) or set(value) != set(SIDES)
            or any(type(value[side]) is not bool for side in SIDES)):
        raise ImuError("IMU bypass values must contain exactly left/right boolean states.")
    return dict(value)


def _write(device: Path, side: str, enabled: bool) -> None:
    if type(enabled) is not bool:
        raise ImuError("IMU bypass state must be a boolean.")
    path = _regular(_attribute(device, side), device)
    fd = os.open(path, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ImuError("The IMU attribute changed during access.")
        payload = b"true\n" if enabled else b"false\n"
        if os.write(fd, payload) != len(payload):
            raise ImuError("The kernel accepted only part of the IMU setting.")
    finally:
        os.close(fd)


def read_status() -> dict[str, Any]:
    with _lock:
        try:
            device = _discover()
            return {"available": True, "actual": _values(device),
                    "identity": str(device), "reason": ""}
        except Exception as exc:
            return {"available": False, "actual": None, "identity": None, "reason": str(exc)}


def desired_for_source(source: str, baseline: dict, actual: dict,
                       last_applied: dict | None = None) -> dict[str, bool]:
    """Enable selected sensors, restore only still-owned unselected values."""
    if not isinstance(source, str) or source not in {"system", "combined", "left", "right"}:
        raise ImuError("Unsupported gyro source.")
    baseline = _checked_values(baseline)
    result = _checked_values(actual)
    owned = _checked_values(last_applied) if last_applied is not None else None
    for side in SIDES:
        if source == "combined" or source == side:
            result[side] = True
        elif owned is not None and result[side] == owned[side]:
            result[side] = baseline[side]
    return result


def apply(expected: dict, desired: dict) -> None:
    """Compare, change and verify both sides, rolling back our writes on error.

    Only literal boolean settings are accepted. No path from the caller or
    from persisted settings is used to open a file. The same checked device is
    retained throughout the operation, so hotplug cannot redirect a rollback.
    """
    expected, desired = _checked_values(expected), _checked_values(desired)
    with _lock:
        device = _discover()
        if _values(device) != expected:
            raise ImuError("IMU bypass changed before the operation; no setting was written.")
        progress = dict(expected)
        attempted = []
        try:
            for side in SIDES:
                if progress[side] == desired[side]:
                    continue
                if _values(device) != progress:
                    raise ImuError("IMU bypass changed externally during the operation.")
                attempted.append(side)
                _write(device, side, desired[side])
                if _read_bool(device, side) != desired[side]:
                    raise ImuError(f"The {side} controller did not confirm its IMU bypass setting.")
                progress[side] = desired[side]
            if _values(device) != desired:
                raise ImuError("The controllers did not confirm the complete IMU bypass setting.")
        except Exception as exc:
            failures = []
            for side in reversed(attempted):
                try:
                    # Each attribute is binary. Only an attribute that this
                    # operation attempted to change is eligible for rollback.
                    if _read_bool(device, side) != expected[side]:
                        _write(device, side, expected[side])
                    if _read_bool(device, side) != expected[side]:
                        raise ImuError("Original value was not confirmed.")
                except Exception as rollback:
                    failures.append(f"{side}: {rollback}")
            detail = " Rollback failed: " + "; ".join(failures) if failures else " Previous IMU values were restored."
            raise ImuError(f"IMU bypass update failed: {exc}.{detail}") from exc

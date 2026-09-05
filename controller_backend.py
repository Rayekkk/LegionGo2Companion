# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Legion Go 2 controller diagnostics and reversible InputPlumber IMU selection.

The packet decoders below are independent implementations of protocol layouts
documented by InputPlumber, not copies of its GPL driver implementation:
https://github.com/ShadowBlip/InputPlumber/blob/v0.78.0/src/drivers/lego/hid_report.rs
https://github.com/ShadowBlip/InputPlumber/blob/v0.78.0/src/drivers/steam_deck/hid_report.rs

Diagnostics use separate read-only, nonblocking hidraw descriptors. They send
no HID commands, ioctls or grabs. Receiving a virtual report proves only that
the virtual controller produced data, not that Steam or a game consumed it.
Only the separately verified IMU bypass controls may be changed by an explicit
source selection. Calibration and imu_enabled attributes are never written.
"""

from __future__ import annotations

import asyncio
import copy
import math
import os
import re
import secrets
import select
import stat
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import decky
from safe_settings import SettingsManager
from remap_backend import SERVICE, INTERFACE, DEVICE_PATH_RE, _json_value, _run_busctl
from inputplumber_process import ProcessWatch
import controller_imu


settings = SettingsManager("controller_settings", decky.DECKY_PLUGIN_SETTINGS_DIR)
HID_CLASS = Path("/sys/class/hidraw")
IIO_CLASS = Path("/sys/bus/iio/devices")
DMI_ROOT = Path("/sys/class/dmi/id")
GO2_PIDS = {0x61EB, 0x61EC, 0x61ED, 0x61EE}
SOURCES = {"system", "combined", "left", "right"}
IMU_KEYS = frozenset(f"{kind}:{side}" for kind in ("Gyroscope", "Accelerometer")
                     for side in ("Left", "Right", "Center"))
LEASE_SECONDS = 3.0
CAPTURE_SECONDS = 30.0
CHECK_SECONDS = 60.0
RECOVERY_RETRY_SECONDS = 5.0


class ControllerError(RuntimeError):
    pass


def _text(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return handle.read(4096).strip()
    except (OSError, UnicodeError):
        return None


def _number(path: Path) -> float | None:
    try:
        value = float(_text(path) or "")
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def _is_go2() -> bool:
    return (_text(DMI_ROOT / "sys_vendor") == "LENOVO"
            and _text(DMI_ROOT / "product_name") in {"83N0", "83N1"})


def _hid_inventory() -> tuple[dict | None, dict | None]:
    if not _is_go2():
        return None, None
    physical, virtual = [], []
    for entry in list(HID_CLASS.glob("hidraw*"))[:128]:
        if not re.fullmatch(r"hidraw[0-9]+", entry.name):
            continue
        device = (entry / "device").resolve()
        if not str(device).startswith("/sys/devices/"):
            continue
        values = dict(line.split("=", 1) for line in (_text(device / "uevent") or "").splitlines()
                      if "=" in line)
        match = re.fullmatch(r"0003:([0-9A-Fa-f]{8}):([0-9A-Fa-f]{8})", values.get("HID_ID", ""))
        if not match:
            continue
        vid, pid = (int(part, 16) for part in match.groups())
        driver = (device / "driver").resolve().name
        devnum = _text(entry / "dev")
        if not devnum or not re.fullmatch(r"[0-9]+:[0-9]+", devnum):
            continue
        record = {"path": f"/dev/{entry.name}", "sysfs": str(device), "pid": f"{pid:04x}",
                  "driver": driver, "devnum": devnum, "identity": str(device.parent)}
        if (vid == 0x17EF and pid in GO2_PIDS and driver == "hid-lenovo-go"
                and _text(device.parent / "bInterfaceNumber") == "02"):
            physical.append(record)
        if (vid == 0x28DE and pid == 0x12FB and values.get("HID_NAME") == "Legion Go 2 Controller"
                and str(device).startswith("/sys/devices/virtual/misc/uhid/")
                and driver in {"hid-steam", "hid-generic"}):
            virtual.append(record)
    # Ambiguity is not resolved by choosing whichever device was enumerated first.
    return (physical[0] if len(physical) == 1 else None,
            virtual[0] if len(virtual) == 1 else None)


def _iio_inventory() -> list[dict]:
    result = []
    if not _is_go2():
        return result
    for path in list(IIO_CLASS.glob("iio:device*"))[:32]:
        name = _text(path / "name")
        if name not in {"gyro_3d", "accel_3d"}:
            continue
        prefix = "in_anglvel" if name == "gyro_3d" else "in_accel"
        frequency = _number(path / f"{prefix}_sampling_frequency")
        if frequency is None:
            frequency = _number(path / "sampling_frequency")
        result.append({"name": name, "path": str(path), "frequency": frequency,
                       "scale": _number(path / f"{prefix}_scale"),
                       "raw": {axis: _number(path / f"{prefix}_{axis}_raw") for axis in "xyz"}})
    return result


def _axes(packet: bytes, offsets: tuple[int, int, int], endian: str) -> dict:
    return {axis: int.from_bytes(packet[offset:offset + 2], endian, signed=True)
            for axis, offset in zip("xyz", offsets)}


def parse_physical_report(packet: bytes) -> dict | None:
    """Decode only known Go2 input frames; configuration replies are not input.

The device sends 64 bytes with declared size 60. hidapi clients requesting 60
bytes receive the same report truncated to 60, which is also accepted here.
Touch contact follows InputPlumber's x/y nonzero convention; the raw values
remain available for diagnosing edge-coordinate ambiguity.
"""
    if len(packet) not in (60, 64) or packet[:3] != b"\x04\x3c\x74":
        return None
    x, y = (int.from_bytes(packet[n:n + 2], "big") for n in (26, 28))
    touching = x != 0 and y != 0
    touchpad = {"x": x / 1024 if touching else None, "y": y / 1024 if touching else None,
                "is_touching": touching, "raw_x": x, "raw_y": y} if max(x, y) <= 1024 else None
    connection = {1: "connecting", 2: "attached", 3: "detached"}
    return {"touchpad": touchpad,
            "gyro_left": _axes(packet, (41, 43, 45), "big"),
            "gyro_right": _axes(packet, (56, 54, 58), "big"),
            "accel_left": _axes(packet, (35, 37, 39), "big"),
            "accel_right": _axes(packet, (50, 48, 52), "big"),
            "imu_timestamp_left": packet[34], "imu_timestamp_right": packet[47],
            "battery_left": packet[5] if packet[5] <= 100 else None,
            "battery_right": packet[7] if packet[7] <= 100 else None,
            "connection_left": connection.get(packet[12]),
            "connection_right": connection.get(packet[13]),
            "mode": {0: "xinput", 1: "dinput", 2: "fps"}.get(packet[9])}


def parse_virtual_report(packet: bytes) -> dict | None:
    if len(packet) != 64 or packet[:4] != b"\x01\x00\x09\x40":
        return None
    touching = bool(packet[10] & 0x10)
    x, y = (int.from_bytes(packet[n:n + 2], "little", signed=True) for n in (20, 22))
    return {"gyro": _axes(packet, (30, 32, 34), "little"),
            "accel": _axes(packet, (24, 26, 28), "little"),
            "frame": int.from_bytes(packet[4:8], "little"),
            "touchpad": {"x": (max(-32767, x) + 32767) / 65534 if touching else None,
                         "y": (32767 - max(-32767, y)) / 65534 if touching else None,
                         "is_touching": touching, "raw_x": x, "raw_y": y}}


def _open_reader(record: dict) -> int:
    if not _is_go2() or not re.fullmatch(r"/dev/hidraw[0-9]+", record.get("path", "")):
        raise ControllerError("The controller identity could not be verified.")
    # Revalidate immediately before open, including the kernel device number.
    candidates = _hid_inventory()
    if record not in candidates:
        raise ControllerError("The controller changed before the test started.")
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(record["path"], flags)
    try:
        info = os.fstat(fd)
        major, minor = (int(value) for value in record["devnum"].split(":"))
        if not stat.S_ISCHR(info.st_mode) or info.st_rdev != os.makedev(major, minor):
            raise ControllerError("The controller device node changed.")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _filter_map(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict) or len(raw) > 128:
        raise ControllerError("InputPlumber returned an invalid filter map.")
    result = {}
    for source, values in raw.items():
        if (not isinstance(source, str) or len(source) > 512 or "\x00" in source
                or not isinstance(values, list) or len(values) > 2048
                or any(not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value
                       for value in values)):
            raise ControllerError("InputPlumber returned invalid filter entries.")
        result[source] = sorted(set(values))
    return result


def _filters(path: str) -> dict[str, list[str]]:
    return _filter_map(_json_value(["get-property", SERVICE, path, INTERFACE, "FilteredEvents"]))


def _imu_values(filters: dict, source: str) -> list[str]:
    return sorted(IMU_KEYS.intersection(filters.get(source, [])))


def _desired_filters(source: str) -> list[str]:
    side = {"combined": "Center", "left": "Left", "right": "Right"}[source]
    return sorted(key for key in IMU_KEYS if key.rsplit(":", 1)[1] != side)


def _discover(physical: dict, *, require_exclusive: bool = True) -> dict:
    source = "hidraw://" + Path(physical["path"]).name
    tree = _run_busctl(["tree", SERVICE, "--list", "--no-pager"])
    found = []
    for path in tree.splitlines():
        path = path.strip()
        if not DEVICE_PATH_RE.fullmatch(path):
            continue
        raw = _json_value(["call", SERVICE, path, "org.freedesktop.DBus.Properties", "GetAll", "s", INTERFACE])
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
            raise ControllerError("InputPlumber returned invalid controller properties.")
        props = {key: value.get("data") for key, value in raw[0].items() if isinstance(value, dict)}
        if props.get("Name") != "Lenovo Legion Go 2":
            continue
        filterable = _filter_map(props.get("FilterableEvents"))
        if not IMU_KEYS.issubset(filterable.get(source, [])):
            continue
        filters = _filter_map(props.get("FilteredEvents"))
        # Do not blend a second unfiltered IMU into the chosen controller source.
        for other, caps in filterable.items():
            if require_exclusive and other != source and any(cap.startswith(("Gyroscope:", "Accelerometer:"))
                                       and cap not in filters.get(other, []) for cap in caps):
                raise ControllerError("Another motion sensor is active in this controller; automatic selection is unavailable.")
        found.append({"path": path, "source": source, "filters": filters,
                      "identity": physical["identity"], "generation": physical["sysfs"]})
    if len(found) != 1:
        raise ControllerError("A single supported InputPlumber controller could not be identified.")
    owner = _json_value(["call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                         "org.freedesktop.DBus", "GetNameOwner", "s", SERVICE])
    if not isinstance(owner, list) or len(owner) != 1 or not isinstance(owner[0], str):
        raise ControllerError("InputPlumber service identity could not be verified.")
    found[0]["generation"] += "|" + owner[0]
    found[0]["service_owner"] = owner[0]
    return found[0]


def _write_owned(device: dict, expected: list[str], desired: list[str]) -> None:
    """Read/merge/write the COMPLETE map; InputPlumber clears omitted sources."""
    current = _filters(device["path"])
    source = device["source"]
    if _imu_values(current, source) != sorted(expected):
        raise ControllerError("Motion settings were changed by another application.")
    if sorted(expected) == sorted(desired):
        return
    merged = copy.deepcopy(current)
    merged[source] = sorted((set(current.get(source, [])) - IMU_KEYS) | set(desired))
    args = ["set-property", SERVICE, device["path"], INTERFACE, "FilteredEvents", "a{sas}", str(len(merged))]
    for key, values in sorted(merged.items()):
        args.extend([key, str(len(values)), *values])
    _run_busctl(args)
    # This setter enqueues a command; a successful D-Bus reply is not readback.
    for attempt in range(6):
        if _imu_values(_filters(device["path"]), source) == sorted(desired):
            return
        if attempt < 5:
            time.sleep(0.05)
    raise ControllerError("InputPlumber did not confirm the motion settings.")


def _valid_owner(ownership: Any) -> bool:
    return (isinstance(ownership, dict) and isinstance(ownership.get("identity"), str)
             and bool(ownership["identity"]) and len(ownership["identity"]) <= 4096
             and type(ownership.get("pending")) is bool
             and type(ownership.get("release_pending", False)) is bool
             and all(isinstance(ownership.get(key), list)
                     and all(isinstance(value, str) and value in IMU_KEYS for value in ownership[key])
                     for key in ("baseline", "applied", "previous"))
             and all(isinstance(ownership.get(key), dict) and set(ownership[key]) == {"left", "right"}
                     and all(type(value) is bool for value in ownership[key].values())
                     for key in ("imu_baseline", "imu_applied", "imu_previous")))


def _state() -> dict:
    if getattr(settings, "recovery_error", ""):
        settings.read()
    if getattr(settings, "recovery_error", ""):
        raise ControllerError("Saved controller ownership could not be recovered. " + settings.recovery_error)
    raw = settings.getSetting("state", {})
    if not isinstance(raw, dict):
        raw = {}
    source = raw.get("gyro_source", "system")
    ownership = raw.get("ownership")
    valid = _valid_owner(ownership)
    if valid and ownership.get("release_pending"):
        resume = ownership.get("resume_owner")
        valid = (_valid_owner(resume) and not resume.get("release_pending")
                 and resume["identity"] == ownership["identity"])
    if not valid:
        ownership = None
    return {"gyro_source": source if isinstance(source, str) and source in SOURCES
            and (source == "system" or ownership) else "system",
            "ownership": copy.deepcopy(ownership)}


def _save(state: dict) -> None:
    previous = copy.deepcopy(settings.getSetting("state", {}))
    try:
        settings.setSetting("state", state)
        settings.commit()
    except Exception:
        settings.setSetting("state", previous)
        raise


def _imu_status() -> dict:
    status = controller_imu.read_status()
    if not status.get("available") or not isinstance(status.get("actual"), dict):
        raise ControllerError(status.get("reason") or "Controller motion controls are unavailable.")
    return status


def _capture_time() -> float:
    # Include suspend time: reopening the UI after sleep cannot revive a lease.
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


def _suspend_offset() -> float:
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
    except (AttributeError, OSError):
        return 0.0


class Plugin:
    def __init__(self) -> None:
        self._operation = threading.RLock()
        self._capture_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._capture: dict | None = None
        self._watch_task: asyncio.Task | None = None
        self._closed = False
        self._error = ""
        self._conflict = False
        self._generation: str | None = None
        self._service_watch = ProcessWatch()
        self._status_cache: tuple[float, dict] | None = None

    def _status(self, fresh: bool = False) -> dict:
        with self._operation:
            now = time.monotonic()
            if not fresh and self._status_cache and now - self._status_cache[0] < 5:
                result = copy.deepcopy(self._status_cache[1])
            else:
                state = _state()
                physical, virtual = _hid_inventory()
                reason = "" if physical else "The supported Legion Go 2 controller is unavailable."
                applied = None
                available = False
                controlled = False
                imu = controller_imu.read_status() if physical else {"available": False, "actual": None}
                if physical:
                    try:
                        device = _discover(physical)
                        active = _imu_values(device["filters"], device["source"])
                        applied = next((source for source in SOURCES - {"system"}
                                        if active == _desired_filters(source)), None)
                        owner = state["ownership"]
                        controlled = bool(owner and owner["identity"] == device["identity"]
                                          and active == owner["applied"] and imu.get("actual") == owner["imu_applied"]
                                          and not owner.get("pending")
                                          and state["gyro_source"] != "system")
                        available = imu.get("available") is True
                        if not available:
                            reason = imu.get("reason") or "Controller motion controls are unavailable."
                    except Exception as exc:
                        reason = str(exc)
                result = {"available": available, "reason": reason, "gyro_source": state["gyro_source"],
                          "applied_source": applied, "controlled": controlled, "conflict": self._conflict,
                          "error": self._error, "physical": physical, "virtual": virtual,
                          "iio": _iio_inventory(), "imu": imu,
                          "recovery_pending": bool(state["ownership"] and state["ownership"].get("pending"))}
                self._status_cache = (now, copy.deepcopy(result))
            with self._capture_lock:
                result["diagnostics_active"] = bool(self._capture and self._capture["active"])
            return result

    async def get_status(self) -> dict:
        return await asyncio.to_thread(self._status)

    def _select(self, source: str, *, reconcile: bool = False) -> None:
        with self._operation:
            if self._closed:
                raise ControllerError("Controller support is stopped.")
            old = _state()
            if reconcile:
                # A user transaction may have completed while this monitor
                # request waited for the lock. Always use the current intent.
                source = old["gyro_source"]
            if source == "system":
                self._release(persist=True)
                return
            physical, _ = _hid_inventory()
            if physical is None:
                raise ControllerError("The supported Legion Go 2 controller is unavailable.")
            release_pending = bool(old["ownership"] and old["ownership"].get("release_pending"))
            device = _discover(physical, require_exclusive=not release_pending)
            if release_pending:
                old = self._recover_release(old, device)
                # Recovery may restore our previous ownership even if another
                # sensor appeared, but taking control still requires one IMU.
                device = _discover(physical)
            imu_actual = _imu_status()["actual"]
            current = _imu_values(device["filters"], device["source"])
            owner = old["ownership"]
            desired = _desired_filters(source)
            if owner and owner["identity"] != device["identity"]:
                raise ControllerError("The saved motion settings belong to another controller connection.")
            if owner and current not in (owner["applied"], owner["previous"] if owner.get("pending") else owner["applied"]):
                restarted = self._generation != device["generation"]
                if not (reconcile and restarted and current == owner["baseline"]):
                    self._conflict = True
                    raise ControllerError("Motion settings changed outside Companion. Choose System before taking control again.")
            pending_imu = bool(owner and owner.get("pending") and all(
                imu_actual[side] in (owner["imu_previous"][side], owner["imu_applied"][side])
                for side in ("left", "right")))
            if owner and imu_actual != owner["imu_applied"] and not pending_imu:
                restarted = self._generation != device["generation"]
                if not (reconcile and restarted and imu_actual == owner["imu_baseline"]):
                    self._conflict = True
                    raise ControllerError("Controller motion controls changed outside Companion. Choose System before taking control again.")
            baseline = owner["baseline"] if owner else current
            imu_baseline = owner["imu_baseline"] if owner else imu_actual
            imu_desired = controller_imu.desired_for_source(source, imu_baseline, imu_actual,
                                                          owner["imu_applied"] if owner else None)
            journal = {"gyro_source": source, "ownership": {
                "identity": device["identity"], "baseline": baseline, "applied": desired,
                "previous": current, "pending": True, "imu_baseline": imu_baseline,
                "imu_applied": imu_desired, "imu_previous": imu_actual}}
            if (owner and current == desired and imu_actual == imu_desired
                    and old["gyro_source"] == source and not owner.get("pending")):
                self._generation = device["generation"]
                self._service_watch.capture(_json_value, device.get("service_owner", ""))
                self._conflict = False
                self._error = ""
                return
            # Write restoration information before touching hardware, including
            # the pending transition so recovery can handle either side of it.
            _save(journal)
            try:
                controller_imu.apply(imu_actual, imu_desired)
                _write_owned(device, current, desired)
                completed = copy.deepcopy(journal)
                completed["ownership"].update(previous=desired, imu_previous=imu_desired, pending=False)
                _save(completed)
            except Exception as exc:
                failures = []
                try:
                    actual = _imu_values(_filters(device["path"]), device["source"])
                    if actual == desired:
                        _write_owned(device, desired, current)
                    elif actual != current:
                        raise ControllerError("An external change prevents rollback.")
                except Exception as rollback:
                    failures.append(str(rollback))
                try:
                    imu_now = _imu_status()["actual"]
                    if imu_now == imu_desired:
                        controller_imu.apply(imu_desired, imu_actual)
                    elif imu_now != imu_actual:
                        raise ControllerError("An external motion-control change prevents rollback.")
                except Exception as rollback:
                    failures.append(str(rollback))
                if not failures:
                    try:
                        _save(old)
                    except Exception as rollback:
                        failures.append(str(rollback))
                if failures:
                    self._error = f"{exc} Recovery is pending: {'; '.join(failures)}"
                    raise ControllerError(self._error) from exc
                raise
            self._generation = device["generation"]
            self._service_watch.capture(_json_value, device.get("service_owner", ""))
            self._conflict = False
            self._error = ""
            self._status_cache = None

    def _recover_release(self, state: dict, device: dict) -> dict:
        """Recover a process interrupted while restoring the previous owner.

        The original ownership is retained separately: filters that were
        already external when release started must not become ours merely
        because the release itself was interrupted.
        """
        owner = state["ownership"]
        if owner["identity"] != device["identity"]:
            raise ControllerError("The controller required for motion recovery is unavailable.")
        current = _imu_values(_filters(device["path"]), device["source"])
        if current == owner["applied"]:
            _write_owned(device, current, owner["previous"])
        # If neither endpoint matches, leave that external filter change alone.
        imu_now = _imu_status()["actual"]
        previous = controller_imu.desired_for_source("system", owner["imu_previous"], imu_now,
                                                     owner["imu_applied"])
        controller_imu.apply(imu_now, previous)
        recovered = {"gyro_source": state["gyro_source"], "ownership": copy.deepcopy(owner["resume_owner"])}
        _save(recovered)
        return recovered

    def _release(self, *, persist: bool) -> None:
        with self._operation:
            old = _state()
            owner = old["ownership"]
            if owner is None:
                self._service_watch.clear()
                self._conflict = False
                self._error = ""
                self._status_cache = None
                return
            physical, _ = _hid_inventory()
            if physical is None:
                raise ControllerError("The controller is unavailable; restoration remains pending.")
            # Another application's new sensor must not prevent relinquishing
            # our own filters and bypass values. Its entries remain untouched.
            device = _discover(physical, require_exclusive=False)
            if owner.get("release_pending"):
                old = self._recover_release(old, device)
                owner = old["ownership"]
                device["filters"] = _filters(device["path"])
            if owner["identity"] != device["identity"]:
                raise ControllerError("The saved controller connection is unavailable; restoration remains pending.")
            current = _imu_values(device["filters"], device["source"])
            imu_current = _imu_status()["actual"]
            imu_expected = owner["imu_previous"] if owner.get("pending") and imu_current == owner["imu_previous"] else owner["imu_applied"]
            imu_restored = controller_imu.desired_for_source("system", owner["imu_baseline"], imu_current, imu_expected)
            owned = current == owner["applied"] or (owner.get("pending") and current == owner["previous"])
            changed = owned and current != owner["baseline"]
            release_owner = copy.deepcopy(owner)
            release_owner.update(applied=owner["baseline"] if changed else current, previous=current,
                                 imu_applied=imu_restored, imu_previous=imu_current,
                                 pending=True, release_pending=True, resume_owner=copy.deepcopy(owner))
            _save({"gyro_source": old["gyro_source"], "ownership": release_owner})
            try:
                if changed:
                    _write_owned(device, current, owner["baseline"])
                controller_imu.apply(imu_current, imu_restored)
                if persist:
                    _save({"gyro_source": "system", "ownership": None})
                else:
                    _save(old)
            except Exception as exc:
                failures = []
                try:
                    imu_now = _imu_status()["actual"]
                    if imu_now == imu_restored:
                        controller_imu.apply(imu_restored, imu_current)
                    elif imu_now != imu_current:
                        raise ControllerError("Motion controls changed during restoration.")
                except Exception as rollback:
                    failures.append(str(rollback))
                try:
                    if changed and _imu_values(_filters(device["path"]), device["source"]) == owner["baseline"]:
                        _write_owned(device, owner["baseline"], current)
                except Exception as rollback:
                    failures.append(str(rollback))
                if not failures:
                    try:
                        _save(old)
                    except Exception as rollback:
                        failures.append(str(rollback))
                if failures:
                    raise ControllerError(f"{exc} Restoration remains pending: {'; '.join(failures)}") from exc
                raise
            # Release is successful even when externally modified fields were
            # preserved. Do not leave a sticky conflict that prevents re-entry.
            self._service_watch.clear()
            self._conflict = False
            self._error = ""
            self._status_cache = None

    async def set_gyro_source(self, source: str) -> dict:
        if not isinstance(source, str) or source not in SOURCES:
            raise ValueError("Unknown motion source.")
        try:
            await asyncio.to_thread(self._select, source)
        except Exception as exc:
            self._error = str(exc)
            self._status_cache = None
            raise
        self._ensure_watch()
        return await asyncio.to_thread(self._status, True)

    async def release_control(self) -> dict:
        return await self.set_gyro_source("system")

    def _snapshot(self, token: str | None = None, *, renew: bool = False) -> dict:
        with self._capture_lock:
            capture = self._capture
            if (capture is None or not isinstance(token, str) or len(token) > 128
                    or not secrets.compare_digest(token, capture["token"])):
                raise ValueError("This controller test is no longer available.")
            now = _capture_time()
            if capture["active"] and (now >= capture["deadline"] or now >= capture["lease"]):
                capture["active"] = False
                capture["reason"] = "completed" if now >= capture["deadline"] else "lease_expired"
                self._stop.set()
            if renew and capture["active"]:
                capture["lease"] = now + LEASE_SECONDS
            elapsed = max(0.0, min(now, capture.get("ended", now)) - capture["started"])
            result = {"token": token, "active": capture["active"], "reason": capture["reason"],
                      "elapsed_s": round(elapsed, 3),
                      "remaining_s": round(max(0.0, capture["deadline"] - now), 3) if capture["active"] else 0.0}
            for kind in ("physical", "virtual"):
                stream = capture[kind]
                recent = stream.setdefault("times", deque(maxlen=4096))
                while recent and recent[0] < now - 1:
                    recent.popleft()
                duration = max(0.001, min(1.0, now - capture["started"]))
                result[kind] = {"received": stream["reports"] > 0, "reports": stream["reports"],
                                "rate_hz": round(len(recent) / duration, 1) if stream["reports"] else None,
                                "age_ms": round((now - stream["last"]) * 1000) if stream["last"] is not None else None,
                                "sample": copy.deepcopy(stream["sample"]), "error": stream["error"],
                                "invalid_reports": stream["invalid_reports"]}
            return result

    def _worker(self, records: tuple[dict | None, dict | None], capture: dict, stop: threading.Event) -> None:
        readers = {}
        try:
            for kind, record in zip(("physical", "virtual"), records):
                if not record:
                    capture[kind]["error"] = "The controller report endpoint is unavailable."
                    continue
                try:
                    readers[_open_reader(record)] = kind
                except Exception as exc:
                    capture[kind]["error"] = str(exc)
            while readers and not stop.is_set():
                now = _capture_time()
                with self._capture_lock:
                    if now >= capture["deadline"] or now >= capture["lease"]:
                        capture["reason"] = "completed" if now >= capture["deadline"] else "lease_expired"
                        break
                ready, _, _ = select.select(list(readers), [], [], 0.1)
                for fd in ready:
                    kind = readers[fd]
                    try:
                        packet = os.read(fd, 256)
                        if not packet:
                            raise OSError("The controller disconnected.")
                    except BlockingIOError:
                        continue
                    except OSError as exc:
                        with self._capture_lock:
                            capture[kind]["error"] = str(exc)
                        os.close(fd)
                        del readers[fd]
                        continue
                    sample = parse_physical_report(packet) if kind == "physical" else parse_virtual_report(packet)
                    with self._capture_lock:
                        stream = capture[kind]
                        if sample is None:
                            stream["invalid_reports"] += 1
                        else:
                            stream["sample"] = sample
                            stream["reports"] += 1
                            stream["last"] = _capture_time()
                            stream.setdefault("times", deque(maxlen=4096)).append(stream["last"])
            if not readers and not capture["reason"]:
                capture["reason"] = "unavailable"
        except Exception as exc:
            with self._capture_lock:
                capture["reason"] = str(exc)
        finally:
            for fd in readers:
                os.close(fd)
            with self._capture_lock:
                capture["active"] = False
                capture["ended"] = _capture_time()
                capture["reason"] = capture["reason"] or "stopped"

    def _stop_capture(self) -> None:
        with self._operation:
            self._stop_capture_locked()

    def _stop_capture_locked(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)
            if thread.is_alive():
                raise ControllerError("The previous controller test is still stopping.")
        self._thread = None

    def _start_capture(self) -> dict:
        with self._operation:
            if self._closed:
                raise ControllerError("Controller support is stopped.")
            self._stop_capture()
            records = _hid_inventory()
            if records[0] is None:
                raise ControllerError("The supported Legion Go 2 controller is unavailable.")
            now = _capture_time()
            stream = {"reports": 0, "invalid_reports": 0, "sample": None, "last": None, "error": ""}
            capture = {"token": secrets.token_urlsafe(24), "active": True, "reason": "",
                       "started": now, "deadline": now + CAPTURE_SECONDS, "lease": now + LEASE_SECONDS,
                       "physical": copy.deepcopy(stream), "virtual": copy.deepcopy(stream)}
            self._stop = threading.Event()
            with self._capture_lock:
                self._capture = capture
            self._thread = threading.Thread(target=self._worker, args=(records, capture, self._stop),
                                            name="companion-controller-test", daemon=True)
            self._thread.start()
            return self._snapshot(capture["token"])

    async def start_diagnostics(self) -> dict:
        return await asyncio.to_thread(self._start_capture)

    async def get_diagnostics(self, token: str) -> dict:
        return self._snapshot(token, renew=True)

    async def stop_diagnostics(self, token: str) -> dict:
        def stop_token() -> dict:
            with self._operation:
                self._snapshot(token)
                self._stop_capture_locked()
                return self._snapshot(token)
        return await asyncio.to_thread(stop_token)

    def _ensure_watch(self) -> None:
        if not self._closed and (self._watch_task is None or self._watch_task.done()):
            self._watch_task = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        now, offset = time.monotonic(), _suspend_offset()
        # A first startup/resume probe can precede HID/InputPlumber discovery.
        # Retry after 5/10/20/40 seconds, then at the normal minute interval.
        # Healthy devices and externally changed ownership stay at one minute.
        retry_delay = RECOVERY_RETRY_SECONDS
        next_check = now + (retry_delay if self._error and not self._conflict else CHECK_SECONDS)
        while not self._closed:
            await asyncio.sleep(5)
            now, new_offset = time.monotonic(), _suspend_offset()
            resumed = new_offset - offset > 1
            offset = new_offset
            service_exited = self._service_watch.consume_exit()
            if service_exited:
                # Only advance the schedule. _select still verifies the actual
                # HID/D-Bus generation and exact ownership before changing it.
                retry_delay = RECOVERY_RETRY_SECONDS
            if resumed:
                await asyncio.to_thread(self._stop_capture)
                self._generation = None
                retry_delay = RECOVERY_RETRY_SECONDS
            if not resumed and not service_exited and now < next_check:
                continue
            next_check = now + CHECK_SECONDS
            try:
                source = _state()["gyro_source"]
                if source == "system":
                    retry_delay = RECOVERY_RETRY_SECONDS
                    continue
                await asyncio.to_thread(self._select, source, reconcile=True)
                retry_delay = RECOVERY_RETRY_SECONDS
            except Exception as exc:
                self._error = str(exc)
                if not self._conflict:
                    next_check = now + retry_delay
                    retry_delay = min(CHECK_SECONDS, retry_delay * 2)
            self._status_cache = None

    async def _main(self) -> None:
        try:
            source = (await asyncio.to_thread(_state))["gyro_source"]
        except Exception as exc:
            self._error = str(exc)
            raise
        self._closed = False
        self._generation = None
        self._service_watch.clear()
        self._error = ""
        self._status_cache = None
        try:
            if source != "system":
                await asyncio.to_thread(self._select, source, reconcile=True)
        except Exception as exc:
            self._error = str(exc)
        self._ensure_watch()

    async def _unload(self) -> None:
        self._closed = True
        task, self._watch_task = self._watch_task, None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self._stop_capture)
        try:
            await asyncio.to_thread(self._release, persist=False)
        except Exception as exc:
            self._error = str(exc)
            decky.logger.warning(f"Controller restoration remains pending: {exc}")
        finally:
            self._service_watch.clear()

    async def _uninstall(self) -> None:
        await self._unload()
        try:
            await asyncio.to_thread(self._release, persist=True)
        except Exception as exc:
            decky.logger.warning(f"Controller restoration remains pending: {exc}")

    async def _migration(self) -> None:
        pass

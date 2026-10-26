# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Legion Go controller diagnostics and reversible InputPlumber IMU selection.

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
import functools
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
from diagnostics import event, transition
from safe_settings import SettingsManager
from inputplumber_process import (
    DEVICE_PATH_RE, INTERFACE, SERVICE, ProcessWatch,
    json_value as _json_value, run_busctl as _run_busctl,
)
import controller_imu
import module_runtime


_offload = functools.partial(module_runtime.offload, 'controller')


settings = SettingsManager("controller_settings", decky.DECKY_PLUGIN_SETTINGS_DIR)
HID_CLASS = Path("/sys/class/hidraw")
DMI_ROOT = Path("/sys/class/dmi/id")
GO2_PIDS = {0x61EB, 0x61EC, 0x61ED, 0x61EE}  # Also a Go 1 on the 2025 controller firmware.
# InputPlumber's name for the controller. It already flips the Go 1's yaw sign by DMI (83E1),
# and Companion only chooses which IMU it forwards, so no axis is changed here.
INPUTPLUMBER_NAMES = {"go1": "Lenovo Legion Go", "go2": "Lenovo Legion Go 2"}
# InputPlumber's virtual Steam Deck controller (28DE) for each: product id and HID_NAME
# (target/steam_deck_uhid.rs and drivers/steam_deck/mod.rs, v0.62.0 to v0.82.0).
VIRTUAL_CONTROLLERS = {"go1": (0x12FE, "Legion Go Controller"), "go2": (0x12FB, "Legion Go 2 Controller")}
SOURCES = {"system", "combined", "left", "right"}
IMU_KEYS = frozenset(f"{kind}:{side}" for kind in ("Gyroscope", "Accelerometer")
                     for side in ("Left", "Right", "Center"))
LEASE_SECONDS = 3.0
CAPTURE_SECONDS = 30.0
CHECK_SECONDS = 60.0
RECOVERY_RETRY_SECONDS = 5.0
BATTERY_SAMPLE_TIMEOUT_SECONDS = 0.25
BATTERY_CACHE_SECONDS = 15.0
# Minutes, the choices Legion Space offers for the detached controllers; 0 turns the timer off.
SLEEP_MINUTES = (0, 5, 10, 15, 30, 45, 60)
# The sleep time is checked this often, besides start, a wake and a controller event: a pad that wakes
# sends no event, and a write to a sleeping pad is lost although the driver reports success. A failing
# check is retried after 5 s, doubling up to this. Reads this rare do not keep a pad awake (measured).
SLEEP_CHECK_SECONDS = 120.0


class ControllerError(RuntimeError):
    pass


class SleepPending(ControllerError):
    """The sleep time is saved, but the pads have not confirmed it yet; the module keeps retrying."""


def _text(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return handle.read(4096).strip()
    except (OSError, UnicodeError):
        return None


def _console() -> str | None:
    """go1 or go2, the consoles whose controllers this module serves; otherwise None."""
    console = module_runtime.legion_device(DMI_ROOT)[0]
    return console if console in INPUTPLUMBER_NAMES else None


def _supported() -> bool:
    console = _console()
    return console == "go2" or (console == "go1" and module_runtime.kernel_controller())


def _hid_inventory() -> tuple[dict | None, dict | None]:
    console = _console()
    if not console:
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
        if (vid == 0x28DE and (pid, values.get("HID_NAME")) == VIRTUAL_CONTROLLERS[console]
                and str(device).startswith("/sys/devices/virtual/misc/uhid/")
                and driver in {"hid-steam", "hid-generic"}):
            virtual.append(record)
    # Ambiguity is not resolved by choosing whichever device was enumerated first.
    return (physical[0] if len(physical) == 1 else None,
            virtual[0] if len(virtual) == 1 else None)


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
    if not _console() or not re.fullmatch(r"/dev/hidraw[0-9]+", record.get("path", "")):
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
        if props.get("Name") != INPUTPLUMBER_NAMES.get(_console()):
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


def _save(state: dict | None, key: str = "state") -> None:
    previous = copy.deepcopy(settings.getSetting(key, {}))
    try:
        settings.setSetting(key, state)
        settings.commit()
    except Exception:
        settings.setSetting(key, previous)
        raise


def _sleep_minutes() -> int | None:
    """The user's sleep time; None until the user picks one."""
    minutes = settings.getSetting("sleep_minutes", None)
    return minutes if type(minutes) is int and minutes in SLEEP_MINUTES else None


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


class Plugin:
    @staticmethod
    def applicable() -> bool:
        """The Go 2, and a Go 1 on the controller firmware hid-lenovo-go serves (same reports and IMU switch)."""
        return _supported()

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
        self._battery_cache: tuple[float, dict] | None = None
        self._battery_lock = threading.Lock()
        self._sleep_error = ""
        self._sleep_retry = RECOVERY_RETRY_SECONDS
        self._sleep_due = 0.0  # monotonic time of the next sleep-time check

    @staticmethod
    def _battery_result(sample: dict | None = None, reason: str = "") -> dict:
        if sample is None:
            return {"available": False, "left": None, "right": None,
                    "connection_left": None, "connection_right": None,
                    "reason": reason or "Controller battery levels are unavailable."}
        left, right = sample.get("battery_left"), sample.get("battery_right")
        return {"available": left is not None or right is not None,
                "left": left, "right": right,
                "connection_left": sample.get("connection_left"),
                "connection_right": sample.get("connection_right"),
                "reason": "" if left is not None or right is not None
                else "The controller report did not include battery levels."}

    def _controller_batteries(self, max_age: float = BATTERY_CACHE_SECONDS) -> dict:
        """Read one verified native report without keeping the HID device open.

        max_age=0 forces a fresh report (the firmware gate must not trust a cached attach state)."""
        with self._battery_lock:
            now = _capture_time()
            if self._battery_cache and now - self._battery_cache[0] < max_age:
                return copy.deepcopy(self._battery_cache[1])

            # Reuse a recent diagnostics sample when one already exists instead
            # of opening a second descriptor for the same physical controller.
            with self._capture_lock:
                stream = self._capture.get("physical") if self._capture else None
                sample = copy.deepcopy(stream.get("sample")) if stream else None
                last = stream.get("last") if stream else None
            if sample is not None and isinstance(last, (int, float)) and now - last < max_age:
                result = self._battery_result(sample)
                self._battery_cache = (now, copy.deepcopy(result))
                return result

            physical, _virtual = _hid_inventory()
            if physical is None:
                result = self._battery_result(reason="The supported Legion Go controller is unavailable.")
                self._battery_cache = (now, copy.deepcopy(result))
                return result

            fd = None
            try:
                fd = _open_reader(physical)
                deadline = time.monotonic() + BATTERY_SAMPLE_TIMEOUT_SECONDS
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        result = self._battery_result(reason="No controller battery report was received.")
                        break
                    ready, _, _ = select.select([fd], [], [], remaining)
                    if not ready:
                        result = self._battery_result(reason="No controller battery report was received.")
                        break
                    try:
                        packet = os.read(fd, 256)
                    except BlockingIOError:
                        continue
                    if not packet:
                        result = self._battery_result(reason="The controller disconnected during the battery read.")
                        break
                    sample = parse_physical_report(packet)
                    if sample is not None:
                        result = self._battery_result(sample)
                        break
            except Exception as exc:
                event('controller', 'battery_read_failed', level='warning', error=exc, repeat=60)
                result = self._battery_result(reason=str(exc))
            finally:
                if fd is not None:
                    os.close(fd)

            self._battery_cache = (now, copy.deepcopy(result))
            return result

    async def get_battery_levels(self) -> dict:
        return await _offload(self._controller_batteries)

    def _status(self, fresh: bool = False) -> dict:
        with self._operation:
            now = time.monotonic()
            if not fresh and self._status_cache and now - self._status_cache[0] < 5:
                result = copy.deepcopy(self._status_cache[1])
            else:
                state = _state()
                physical, virtual = _hid_inventory()
                reason = "" if physical else "The supported Legion Go controller is unavailable."
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
                          "imu": imu,
                          "recovery_pending": bool(state["ownership"] and state["ownership"].get("pending")),
                          "sleep": self._sleep_status()}
                self._status_cache = (now, copy.deepcopy(result))
            with self._capture_lock:
                result["diagnostics_active"] = bool(self._capture and self._capture["active"])
            return result

    async def get_status(self) -> dict:
        return await _offload(self._status)

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
                raise ControllerError("The supported Legion Go controller is unavailable.")
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
            await _offload(self._select, source)
        except Exception as exc:
            self._error = str(exc)
            self._status_cache = None
            raise
        self._ensure_watch()
        return await _offload(self._status, True)

    async def release_control(self) -> dict:
        return await self.set_gyro_source("system")

    def _sleep_status(self) -> dict:
        # The pads' own value is not shown: the driver can report a stale cached one.
        return {"available": controller_imu.sleep_supported(), "minutes": _sleep_minutes(), "error": self._sleep_error}

    def _apply_sleep(self) -> bool:
        """Write the chosen sleep time to a pad that reports another one; nothing until the user picks one."""
        with self._operation:
            minutes = _sleep_minutes()
            if minutes is None:
                ok, error = True, ""
            else:
                try:
                    ok = controller_imu.apply_sleep({"left": minutes, "right": minutes})
                    error = "" if ok else "The controllers have not confirmed the sleep time yet. Companion tries again when they respond."
                except Exception as exc:
                    ok, error = False, str(exc)
                transition("controller", "sleep_time", ok, level="info" if ok else "warning",
                           outcome="confirmed" if ok else "deferred", initial=False)
            if error != self._sleep_error:
                self._status_cache = None
            self._sleep_error = error
            return ok

    def _set_sleep(self, minutes: int) -> None:
        with self._operation:
            if self._closed:
                raise ControllerError("Controller support is stopped.")
            if not controller_imu.sleep_supported():
                raise ControllerError("These controllers do not offer a sleep timer.")
            _save(minutes, "sleep_minutes")
            self._status_cache = None
            if not self._apply_sleep():
                self._sleep_retry = RECOVERY_RETRY_SECONDS
                self._sleep_due = time.monotonic() + RECOVERY_RETRY_SECONDS
                raise SleepPending(self._sleep_error)

    async def set_sleep_time(self, minutes) -> dict:
        if type(minutes) is not int or minutes not in SLEEP_MINUTES:
            raise ValueError("Unsupported controller sleep time.")
        await _offload(self._set_sleep, minutes)
        return await _offload(self._status, True)

    def _release_sleep(self) -> None:
        """Turning the module off only stops managing: the pads keep their time, nothing is written.

        A value read back to restore could be the driver's stale cache (0, Never, on a factory 83N0)."""
        with self._operation:
            if settings.getSetting("sleep_minutes", None) is not None:
                _save(None, "sleep_minutes")
            self._sleep_error = ""
            self._status_cache = None

    def controller_event(self) -> None:
        """The controllers' udev events settled (a pad reconnected): check the sleep time on the next tick."""
        self._sleep_retry = RECOVERY_RETRY_SECONDS
        self._sleep_due = 0.0

    @module_runtime.controller_operation('controller')
    async def _sleep_pass(self) -> bool:
        return await _offload(self._apply_sleep)

    async def _sleep_check(self, now: float, resumed: bool) -> None:
        """Only a pad that differs is written, at most once per check. A failing check backs off from 5 s to
        SLEEP_CHECK_SECONDS, so a sleeping pad is not flooded; a confirmed one is checked again after that."""
        if resumed:
            self._sleep_retry = RECOVERY_RETRY_SECONDS
        try:
            ok = await self._sleep_pass()
        except module_runtime.ControllerPaused:
            self._sleep_due = now + RECOVERY_RETRY_SECONDS
            return
        except Exception:
            ok = False  # e.g. maintenance began: the gyro watch must keep running
        if ok:
            self._sleep_retry = RECOVERY_RETRY_SECONDS
            self._sleep_due = now + SLEEP_CHECK_SECONDS
        else:
            self._sleep_due = now + self._sleep_retry
            self._sleep_retry = min(SLEEP_CHECK_SECONDS, self._sleep_retry * 2)

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
                recent = stream["times"]
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
                    event("controller", "diagnostics_stream", level="warning", source=kind,
                          reason="endpoint_unavailable", repeat=60)
                    continue
                try:
                    readers[_open_reader(record)] = kind
                except Exception as exc:
                    capture[kind]["error"] = str(exc)
                    event("controller", "diagnostics_stream", level="warning", error=exc,
                          source=kind, stage="open", repeat=60)
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
                        event("controller", "diagnostics_stream", level="warning", error=exc,
                              source=kind, stage="read", repeat=60)
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
                            stream["times"].append(stream["last"])
            if not readers and not capture["reason"]:
                capture["reason"] = "unavailable"
        except Exception as exc:
            with self._capture_lock:
                capture["reason"] = str(exc)
            event("controller", "diagnostics_worker", level="warning", error=exc,
                  outcome="failed", repeat=60)
        finally:
            for fd in readers:
                os.close(fd)
            with self._capture_lock:
                capture["active"] = False
                capture["ended"] = _capture_time()
                capture["reason"] = capture["reason"] or "stopped"
                reason = capture["reason"] if capture["reason"] in {
                    "completed", "lease_expired", "stopped", "unavailable"} else "failed"
                count = sum(capture[kind]["reports"] for kind in ("physical", "virtual"))
            event("controller", "diagnostics_complete", reason=reason, count=count)

    def _stop_capture(self) -> None:
        with self._operation:
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
                raise ControllerError("The supported Legion Go controller is unavailable.")
            now = _capture_time()
            stream = {"reports": 0, "invalid_reports": 0, "sample": None, "last": None, "error": "",
                      "times": deque(maxlen=4096)}
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
        return await _offload(self._start_capture)

    async def get_diagnostics(self, token: str) -> dict:
        return self._snapshot(token, renew=True)

    async def stop_diagnostics(self, token: str) -> dict:
        def stop_token() -> dict:
            with self._operation:
                self._snapshot(token)
                self._stop_capture()
                return self._snapshot(token)
        return await _offload(stop_token)

    def _ensure_watch(self) -> None:
        if not self._closed and (self._watch_task is None or self._watch_task.done()):
            self._watch_task = module_runtime.background_task(self._watch())

    @module_runtime.controller_operation('controller')
    async def _watch_check(self, resumed):
        if resumed:
            await _offload(self._stop_capture)
            self._generation = None
        source = (await _offload(_state))["gyro_source"]
        if source != "system":
            await _offload(self._select, source, reconcile=True)

    async def _watch(self) -> None:
        now = time.monotonic()
        resume = module_runtime.ResumeWatch()
        resume.mark()
        # A first startup/resume probe can precede HID/InputPlumber discovery.
        # Retry after 5/10/20/40 seconds, then at the normal minute interval.
        # Healthy devices and externally changed ownership stay at one minute.
        retry_delay = RECOVERY_RETRY_SECONDS
        next_check = now + (retry_delay if self._error and not self._conflict else CHECK_SECONDS)
        self._sleep_due = now  # startup, an update or re-enabling: one check on the first tick
        while not self._closed:
            await asyncio.sleep(5)
            if module_runtime.is_paused('controller'):
                continue
            now, resumed = time.monotonic(), resume()
            if resumed or now >= self._sleep_due:
                await self._sleep_check(now, resumed)
            service_exited = self._service_watch.consume_exit()
            if service_exited:
                # Only advance the schedule. _select still verifies the actual
                # HID/D-Bus generation and exact ownership before changing it.
                retry_delay = RECOVERY_RETRY_SECONDS
            if not resumed and not service_exited and now < next_check:
                continue
            next_check = now + CHECK_SECONDS
            try:
                await self._watch_check(resumed)
                transition("controller", "managed_profile", True, outcome="confirmed", initial=False)
                retry_delay = RECOVERY_RETRY_SECONDS
            except module_runtime.ControllerPaused:
                continue
            except Exception as exc:
                self._error = str(exc)
                transition("controller", "managed_profile", "conflict" if self._conflict else "deferred",
                           level="warning", error=exc,
                           outcome="deferred", pending=not self._conflict)
                if not self._conflict:
                    next_check = now + retry_delay
                    retry_delay = min(CHECK_SECONDS, retry_delay * 2)
            self._status_cache = None

    async def _main(self) -> None:
        try:
            source = (await _offload(_state))["gyro_source"]
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
                await _offload(self._select, source, reconcile=True)
                transition("controller", "managed_profile", True, outcome="confirmed", initial=False)
        except Exception as exc:
            self._error = str(exc)
            transition("controller", "managed_profile", "conflict" if self._conflict else "deferred",
                       level="warning", error=exc,
                       outcome="deferred", pending=not self._conflict)
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
        # Closing our own read-only observers is permitted during maintenance;
        # this path never restores hardware or user intent.
        await module_runtime.complete(asyncio.to_thread(self._stop_capture))
        try:
            await _offload(self._release, persist=False)
        except Exception as exc:
            self._error = str(exc)
            event('controller','restore_failed',level='warning',error=exc,stage='unload',pending=True,restored=False)
        finally:
            self._service_watch.clear()

    async def _uninstall(self) -> None:
        # Decky also uninstalls before every update. Unload already restored the
        # controller and keeps the choice with its ownership, which _main reapplies.
        # The pads keep their sleep time: only turning the module off gives the old one back.
        await self._unload()

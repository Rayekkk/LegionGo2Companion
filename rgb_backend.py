# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Low-overhead lighting control for the Lenovo Legion Go 2.

Joystick-ring control uses the kernel's hid-lenovo-go LED-class ABI.  The
power-button LED uses one audited MMIO bit from the machine's own DSDT and is
disabled unless every identity and firmware check matches the tested device.
"""

from __future__ import annotations

import asyncio
import colorsys
import copy
import fcntl
import hashlib
import mmap
import os
import stat
import threading
import time
from typing import Any, Callable

import decky
import module_runtime

from safe_settings import SettingsManager


RGB_LED_PATH = "/sys/class/leds/go:rgb:joystick_rings"
RGB_HID_ID = "17EF:61EB"
RGB_DRIVER = "hid-lenovo-go"
RGB_PROFILE = 3
RGB_EFFECTS = ("monocolor", "breathe", "chroma", "rainbow")
RGB_EFFECT_LABELS = {
    "monocolor": "Solid",
    "breathe": "Breathing",
    "chroma": "Color cycle",
    "rainbow": "Rainbow",
}

EXPECTED_FAMILY = "Legion Go 8ASP2"
EXPECTED_PRODUCT = "83N0"
EXPECTED_BOARD = "LNVNB161216"
AUDITED_BIOS = "RRCN16WW"
AUDITED_DSDT_SHA256 = {
    "1b4231432de76a52e016eb239d38162c78d59e8d0afdb2084c0c86ed84f00f6b",
}

# RRCN16WW DSDT: OperationRegion ERAM at 0xFEEC2300, LPBL at offset
# 0x10 bit 6.  Logic is inverted: zero is on and one is off.  This deliberately
# does not use HueSync's older 0xFE0B0300/0x52 mapping, which does not match the
# DSDT read from the user's Go 2.
POWER_MMIO_BASE = 0xFEEC2300
POWER_MMIO_OFFSET = 0x10
POWER_LED_BIT = 6
POWER_LED_ADDRESS = POWER_MMIO_BASE + POWER_MMIO_OFFSET
POWER_LED_MASK = 1 << POWER_LED_BIT
POWER_LOCK_PATH = "/run/lock/legiongo2companion-power-led.lock"
DSDT_PATH = "/sys/firmware/acpi/tables/DSDT"
DEV_MEM_PATH = "/dev/mem"

SCHEMA_VERSION = 1
DRIFT_INTERVAL_S = 60.0
STARTUP_SETTLE_S = 30.0
RESUME_CHECK_S = 5.0
REAPPLY_WAIT_S = 12.0
REAPPLY_STEP_S = 0.5

DEFAULT_STATE: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "configured": False,
    "control_enabled": False,
    "rings_enabled": False,
    "effect": "monocolor",
    "hue": 186,
    "saturation": 99,
    "brightness": 50,
    "speed": 50,
    "original_rgb": None,
    "power_led_managed": False,
    "power_led_enabled": True,
    "power_led_original_enabled": None,
    "pending": None,
}

settings = SettingsManager(
    name="rgb_settings",
    settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR,
)

_settings_lock = threading.RLock()
_apply_lock = threading.RLock()
_drift_task: asyncio.Task | None = None
_last_suspend_offset: float | None = None
_last_error = ""
_power_capability_cache: tuple[bool, str] | None = None


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    if type(value) is bool:
        return default
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _strict_bool(value: Any, default: bool) -> bool:
    return value if type(value) is bool else default


def _read_small(path: str, limit: int = 512) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"not a regular sysfs attribute: {path}")
        raw = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise OSError(f"attribute is unexpectedly large: {path}")
    return raw.decode("ascii", errors="strict").strip()


def _read_identity() -> dict[str, str]:
    values: dict[str, str] = {}
    for key in ("product_family", "product_name", "board_name", "bios_version"):
        try:
            values[key] = _read_small(f"/sys/class/dmi/id/{key}", 128)
        except (OSError, UnicodeError):
            values[key] = ""
    return values


def _rgb_capability() -> tuple[str | None, str]:
    identity = _read_identity()
    if (
        identity["product_family"] != EXPECTED_FAMILY
        or identity["product_name"] != EXPECTED_PRODUCT
    ):
        return None, "RGB control is limited to the Lenovo Legion Go 2 (83N0)."

    if not os.path.lexists(RGB_LED_PATH):
        return None, "The hid-lenovo-go joystick-ring interface is not available."
    real = os.path.realpath(RGB_LED_PATH)
    if not real.startswith("/sys/devices/") or f":{RGB_HID_ID}." not in real.upper():
        return None, "The joystick-ring interface does not belong to the expected controller."
    hid_root = real.split("/leds/", 1)[0]
    driver = os.path.basename(os.path.realpath(os.path.join(hid_root, "driver")))
    if driver != RGB_DRIVER:
        return None, f"The controller is bound to {driver or 'an unknown driver'}, not {RGB_DRIVER}."

    required_rw = (
        "enabled", "profile", "mode", "effect", "brightness",
        "multi_intensity", "speed",
    )
    required_ro = (
        "enabled_index", "profile_range", "mode_index", "effect_index",
        "max_brightness", "multi_max_intensity", "speed_range",
    )
    for attr in required_rw + required_ro:
        path = os.path.join(real, attr)
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            return None, f"The joystick-ring interface is incomplete ({attr} missing)."
        if not stat.S_ISREG(info.st_mode):
            return None, f"Unexpected joystick-ring attribute type: {attr}."
    for attr in required_rw:
        if not os.access(os.path.join(real, attr), os.W_OK):
            return None, f"The joystick-ring attribute {attr} is not writable."

    try:
        enabled = set(_read_small(os.path.join(real, "enabled_index")).split())
        profiles = _read_small(os.path.join(real, "profile_range"))
        modes = set(_read_small(os.path.join(real, "mode_index")).split())
        effects = set(_read_small(os.path.join(real, "effect_index")).split())
        speed_range = _read_small(os.path.join(real, "speed_range"))
        maxima = [int(v) for v in _read_small(
            os.path.join(real, "multi_max_intensity")
        ).split()]
        max_brightness = int(_read_small(os.path.join(real, "max_brightness")))
    except (OSError, UnicodeError, ValueError):
        return None, "The joystick-ring capability data could not be read."

    if (
        not {"true", "false"}.issubset(enabled)
        or profiles != "1-3"
        or "custom" not in modes
        or not set(RGB_EFFECTS).issubset(effects)
        or speed_range != "0-100"
        or len(maxima) != 3
        or any(value <= 0 or value > 255 for value in maxima)
        or max_brightness <= 0
        or max_brightness > 255
    ):
        return None, "The joystick-ring driver ABI is not the audited Go 2 layout."
    return real, ""


def _normalise_attr(attr: str, value: str) -> str:
    value = " ".join(value.strip().split())
    if attr in ("brightness", "profile", "speed"):
        return str(int(value))
    return value.lower()


def _write_attr(path: str, attr: str, value: str, *, force: bool = False) -> bool:
    target = os.path.join(path, attr)
    expected = _normalise_attr(attr, value)
    try:
        if not force:
            current = _normalise_attr(attr, _read_small(target))
            if current == expected:
                return True
        flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags)
        try:
            payload = (value.strip() + "\n").encode("ascii")
            if os.write(fd, payload) != len(payload):
                return False
        finally:
            os.close(fd)
        return _normalise_attr(attr, _read_small(target)) == expected
    except (OSError, UnicodeError, ValueError):
        return False


def _parse_triplet(value: str) -> tuple[int, int, int]:
    parts = tuple(int(item) for item in value.split())
    if len(parts) != 3:
        raise ValueError("expected three RGB channels")
    return parts


def _read_rgb_snapshot(path: str | None = None) -> dict[str, Any] | None:
    if path is None:
        path, _ = _rgb_capability()
    if path is None:
        return None
    try:
        maxima = _parse_triplet(_read_small(os.path.join(path, "multi_max_intensity")))
        rgb = _parse_triplet(_read_small(os.path.join(path, "multi_intensity")))
        brightness_max = int(_read_small(os.path.join(path, "max_brightness")))
        brightness = int(_read_small(os.path.join(path, "brightness")))
        speed = int(_read_small(os.path.join(path, "speed")))
        enabled_text = _read_small(os.path.join(path, "enabled")).lower()
        mode = _read_small(os.path.join(path, "mode")).lower()
        effect = _read_small(os.path.join(path, "effect")).lower()
        try:
            profile = int(_read_small(os.path.join(path, "profile")))
        except (OSError, ValueError):
            # The firmware may report an invalid built-in profile before the
            # first user profile is selected.  It is safe to preserve every
            # other field and omit only that unrepresentable value.
            profile = None
    except (OSError, UnicodeError, ValueError):
        return None
    if (
        enabled_text not in ("true", "false")
        or mode not in ("dynamic", "custom")
        or effect not in RGB_EFFECTS
        or len(maxima) != 3
        or any(maximum <= 0 or channel < 0 or channel > maximum
               for channel, maximum in zip(rgb, maxima))
        or brightness < 0 or brightness > brightness_max
        or speed < 0 or speed > 100
        or (profile is not None and profile not in (1, 2, 3))
    ):
        return None
    return {
        "profile": profile,
        "mode": mode,
        "effect": effect,
        "brightness": brightness,
        "brightness_max": brightness_max,
        "rgb": list(rgb),
        "rgb_max": list(maxima),
        "speed": speed,
        "enabled": enabled_text == "true",
    }


def _sanitize_snapshot(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        profile = raw.get("profile")
        if profile is not None:
            profile = int(profile)
            if profile not in (1, 2, 3):
                return None
        mode = str(raw["mode"]).lower()
        effect = str(raw["effect"]).lower()
        rgb = [int(value) for value in raw["rgb"]]
        rgb_max = [int(value) for value in raw["rgb_max"]]
        brightness = int(raw["brightness"])
        brightness_max = int(raw["brightness_max"])
        speed = int(raw["speed"])
        enabled = raw["enabled"]
    except (KeyError, TypeError, ValueError):
        return None
    if (
        type(enabled) is not bool
        or mode not in ("dynamic", "custom")
        or effect not in RGB_EFFECTS
        or len(rgb) != 3 or len(rgb_max) != 3
        or any(maximum <= 0 or value < 0 or value > maximum
               for value, maximum in zip(rgb, rgb_max))
        or brightness_max <= 0
        or brightness < 0 or brightness > brightness_max
        or speed < 0 or speed > 100
    ):
        return None
    return {
        "profile": profile,
        "mode": mode,
        "effect": effect,
        "brightness": brightness,
        "brightness_max": brightness_max,
        "rgb": rgb,
        "rgb_max": rgb_max,
        "speed": speed,
        "enabled": enabled,
    }


def _sanitize_state(raw: Any, *, include_pending: bool = True) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    effect = str(raw.get("effect", DEFAULT_STATE["effect"])).lower()
    if effect not in RGB_EFFECTS:
        effect = DEFAULT_STATE["effect"]
    original_power = raw.get("power_led_original_enabled")
    if type(original_power) is not bool:
        original_power = None
    result = {
        "schema_version": SCHEMA_VERSION,
        "configured": _strict_bool(raw.get("configured"), False),
        "control_enabled": _strict_bool(raw.get("control_enabled"), False),
        "rings_enabled": _strict_bool(raw.get("rings_enabled"), False),
        "effect": effect,
        "hue": _bounded_int(raw.get("hue"), DEFAULT_STATE["hue"], 0, 359),
        "saturation": _bounded_int(
            raw.get("saturation"), DEFAULT_STATE["saturation"], 0, 100
        ),
        "brightness": _bounded_int(
            raw.get("brightness"), DEFAULT_STATE["brightness"], 0, 100
        ),
        "speed": _bounded_int(raw.get("speed"), DEFAULT_STATE["speed"], 0, 100),
        "original_rgb": _sanitize_snapshot(raw.get("original_rgb")),
        "power_led_managed": _strict_bool(raw.get("power_led_managed"), False),
        "power_led_enabled": _strict_bool(raw.get("power_led_enabled"), True),
        "power_led_original_enabled": original_power,
        "pending": None,
    }
    if result["control_enabled"] and result["original_rgb"] is None:
        # An ownership claim without recovery data is unsafe.  Do not write the
        # rings until the user explicitly enables control again.
        result["control_enabled"] = False
    if result["power_led_managed"] and original_power is None:
        result["power_led_managed"] = False

    if include_pending and isinstance(raw.get("pending"), dict):
        pending = raw["pending"]
        previous = _sanitize_state(pending.get("previous"), include_pending=False)
        before_rgb = _sanitize_snapshot(pending.get("before_rgb"))
        before_power = pending.get("before_power")
        if type(before_power) is not bool:
            before_power = None
        if before_rgb is not None or before_power is not None:
            result["pending"] = {
                "previous": previous,
                "before_rgb": before_rgb,
                "before_power": before_power,
            }
    return result


def _load_state() -> dict[str, Any]:
    with _settings_lock:
        settings.read()
        return _sanitize_state(copy.deepcopy(settings.getSetting("state", {})))


def _save_state(state: dict[str, Any]) -> None:
    clean = _sanitize_state(state)
    with _settings_lock:
        settings.setSetting("state", clean)
        settings.commit()


def _ensure_settings_file() -> None:
    if not os.path.exists(settings.path):
        _save_state(dict(DEFAULT_STATE))


def _seed_from_snapshot(state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    red, green, blue = (
        channel / maximum
        for channel, maximum in zip(snapshot["rgb"], snapshot["rgb_max"])
    )
    hue, saturation, _value = colorsys.rgb_to_hsv(red, green, blue)
    state["hue"] = min(359, round(hue * 360))
    state["saturation"] = round(saturation * 100)
    state["brightness"] = round(
        snapshot["brightness"] * 100 / snapshot["brightness_max"]
    )
    state["speed"] = snapshot["speed"]
    state["effect"] = snapshot["effect"]
    state["rings_enabled"] = snapshot["enabled"]
    state["configured"] = True


def _target_for_state(state: dict[str, Any], path: str) -> dict[str, Any]:
    maxima = _parse_triplet(_read_small(os.path.join(path, "multi_max_intensity")))
    brightness_max = int(_read_small(os.path.join(path, "max_brightness")))
    hue = state["hue"] / 360.0
    saturation = state["saturation"] / 100.0
    red, green, blue = colorsys.hsv_to_rgb(hue, saturation, 1.0)
    rgb = [round(channel * maximum) for channel, maximum in zip(
        (red, green, blue), maxima
    )]
    return {
        "profile": RGB_PROFILE,
        "mode": "custom",
        "effect": state["effect"],
        "brightness": round(brightness_max * state["brightness"] / 100),
        "brightness_max": brightness_max,
        "rgb": rgb,
        "rgb_max": list(maxima),
        "speed": state["speed"],
        "enabled": state["rings_enabled"],
    }


def _apply_rgb_snapshot(path: str, snapshot: dict[str, Any], *, force: bool = False) -> bool:
    snapshot = _sanitize_snapshot(snapshot)
    if snapshot is None:
        return False
    current = _read_rgb_snapshot(path)
    if snapshot["profile"] is None and current is not None and all(
        current.get(key) == snapshot.get(key)
        for key in ("mode", "effect", "brightness", "rgb", "speed", "enabled")
    ):
        # Some firmware reports no selected built-in profile while the rings are
        # disabled.  If nothing observable changed, keep that state untouched.
        return True
    results: list[bool] = []
    if not snapshot["enabled"]:
        # Darken first so restoration cannot flash an intermediate effect.
        results.append(_write_attr(path, "enabled", "false", force=force))
    profile = snapshot["profile"] if snapshot["profile"] is not None else RGB_PROFILE
    results.append(_write_attr(path, "profile", str(profile), force=force))
    results.extend((
        _write_attr(path, "speed", str(snapshot["speed"]), force=force),
        _write_attr(path, "multi_intensity", " ".join(
            str(value) for value in snapshot["rgb"]
        ), force=force),
        _write_attr(path, "brightness", str(snapshot["brightness"]), force=force),
        _write_attr(path, "effect", snapshot["effect"], force=force),
        _write_attr(path, "mode", snapshot["mode"], force=force),
        _write_attr(
            path, "enabled", "true" if snapshot["enabled"] else "false", force=force
        ),
    ))
    return all(results)


def _apply_rgb_target(state: dict[str, Any], *, force: bool = False) -> bool:
    path, _ = _rgb_capability()
    if path is None:
        return False
    if not state["rings_enabled"]:
        return _write_attr(path, "enabled", "false", force=force)
    return _apply_rgb_snapshot(path, _target_for_state(state, path), force=force)


def _rgb_matches(state: dict[str, Any], current: dict[str, Any], path: str) -> bool:
    if not state["rings_enabled"]:
        return current["enabled"] is False
    target = _target_for_state(state, path)
    return all(current.get(key) == target.get(key) for key in (
        "profile", "mode", "effect", "brightness", "rgb", "speed", "enabled"
    ))


def _power_capability() -> tuple[bool, str]:
    global _power_capability_cache
    if _power_capability_cache is not None:
        return _power_capability_cache
    identity = _read_identity()
    expected = {
        "product_family": EXPECTED_FAMILY,
        "product_name": EXPECTED_PRODUCT,
        "board_name": EXPECTED_BOARD,
        "bios_version": AUDITED_BIOS,
    }
    for key, value in expected.items():
        if identity.get(key) != value:
            _power_capability_cache = (
                False,
                "Power-button control is disabled because this hardware/BIOS "
                "does not match the audited Go 2 configuration.",
            )
            return _power_capability_cache
    try:
        digest = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(DSDT_PATH, flags)
        try:
            while True:
                block = os.read(fd, 65536)
                if not block:
                    break
                digest.update(block)
        finally:
            os.close(fd)
    except OSError:
        _power_capability_cache = (False, "The BIOS DSDT could not be verified.")
        return _power_capability_cache
    if digest.hexdigest().lower() not in AUDITED_DSDT_SHA256:
        _power_capability_cache = (
            False,
            "Power-button control needs a new audit after this BIOS/DSDT change.",
        )
        return _power_capability_cache
    if not os.path.exists(DEV_MEM_PATH) or not os.access(
        DEV_MEM_PATH, os.R_OK | os.W_OK
    ):
        _power_capability_cache = (False, "The verified power-button interface is unavailable.")
        return _power_capability_cache
    _power_capability_cache = (True, "")
    return _power_capability_cache


def _with_power_lock(operation: Callable[[], Any]) -> Any:
    lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    lock_fd = os.open(POWER_LOCK_PATH, lock_flags, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return operation()
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _map_power_register(write: bool, operation: Callable[[mmap.mmap, int], Any]) -> Any:
    page_size = mmap.PAGESIZE
    map_base = POWER_LED_ADDRESS - (POWER_LED_ADDRESS % page_size)
    page_offset = POWER_LED_ADDRESS - map_base
    flags = os.O_RDWR if write else os.O_RDONLY
    flags |= os.O_SYNC | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(DEV_MEM_PATH, flags)
    try:
        protection = mmap.PROT_READ | (mmap.PROT_WRITE if write else 0)
        mapped = mmap.mmap(
            fd,
            page_offset + 1,
            flags=mmap.MAP_SHARED,
            prot=protection,
            offset=map_base,
        )
        try:
            return operation(mapped, page_offset)
        finally:
            mapped.close()
    finally:
        os.close(fd)


def _read_power_led() -> bool | None:
    supported, _ = _power_capability()
    if not supported:
        return None
    try:
        value = _with_power_lock(
            lambda: _map_power_register(False, lambda mapped, offset: mapped[offset])
        )
        return (value & POWER_LED_MASK) == 0
    except (OSError, ValueError):
        return None


def _write_power_led(enabled: bool) -> bool:
    supported, _ = _power_capability()
    if not supported or type(enabled) is not bool:
        return False

    def _write() -> bool:
        def _mapped(mapped: mmap.mmap, offset: int) -> bool:
            before = mapped[offset]
            target = before & ~POWER_LED_MASK if enabled else before | POWER_LED_MASK
            if target != before:
                mapped[offset] = target
            verify = mapped[offset]
            if (verify & POWER_LED_MASK) == (target & POWER_LED_MASK):
                return True
            # Restore only our bit, preserving any unrelated concurrent changes.
            current = mapped[offset]
            rollback = (
                current | POWER_LED_MASK
                if before & POWER_LED_MASK
                else current & ~POWER_LED_MASK
            )
            mapped[offset] = rollback
            return False

        return _map_power_register(True, _mapped)

    try:
        return bool(_with_power_lock(_write))
    except (OSError, ValueError):
        return False


def _restore_transaction(pending: dict[str, Any]) -> bool:
    before_rgb = pending.get("before_rgb")
    before_power = pending.get("before_power")
    ok = True
    if before_rgb is not None:
        path, _ = _rgb_capability()
        ok = path is not None and _apply_rgb_snapshot(path, before_rgb, force=True) and ok
    if before_power is not None:
        ok = _write_power_led(before_power) and ok
    if ok:
        _save_state(pending["previous"])
    return ok


def _recover_pending() -> bool:
    state = _load_state()
    pending = state.get("pending")
    if not pending:
        return True
    decky.logger.warning("[lego-rgb] recovering an interrupted lighting transaction")
    return _restore_transaction(pending)


def _transaction(
    current_state: dict[str, Any],
    candidate: dict[str, Any],
    *,
    before_rgb: dict[str, Any] | None = None,
    before_power: bool | None = None,
    apply: Callable[[], bool],
) -> tuple[bool, str]:
    previous = _sanitize_state(current_state, include_pending=False)
    candidate = _sanitize_state(candidate, include_pending=False)
    staged = copy.deepcopy(previous)
    staged["pending"] = {
        "previous": previous,
        "before_rgb": before_rgb,
        "before_power": before_power,
    }
    try:
        _save_state(staged)
    except Exception as exc:
        return False, f"Could not create the recovery record: {exc}"

    try:
        applied = apply()
    except Exception as exc:
        applied = False
        detail = str(exc)
    else:
        detail = ""

    if applied:
        try:
            _save_state(candidate)
            return True, ""
        except Exception as exc:
            detail = f"Could not save the new setting: {exc}"
    elif not detail:
        detail = "The hardware did not confirm the complete change."

    pending = staged["pending"]
    try:
        rolled_back = _restore_transaction(pending)
    except Exception:
        rolled_back = False
    if not rolled_back:
        return False, detail + " Automatic recovery still needs to finish."
    return False, detail


def _status() -> dict[str, Any]:
    state = _load_state()
    path, rgb_reason = _rgb_capability()
    rgb_current = _read_rgb_snapshot(path) if path else None
    power_supported, power_reason = _power_capability()
    power_current = _read_power_led() if power_supported else None
    rgb_drift = False
    if state["control_enabled"] and path and rgb_current:
        try:
            rgb_drift = not _rgb_matches(state, rgb_current, path)
        except (OSError, ValueError):
            rgb_drift = True
    power_drift = (
        state["power_led_managed"]
        and power_current is not None
        and power_current != state["power_led_enabled"]
    )
    return {
        "success": True,
        "settings": {key: copy.deepcopy(state[key]) for key in (
            "configured", "control_enabled", "rings_enabled", "effect", "hue",
            "saturation", "brightness", "speed", "power_led_managed",
            "power_led_enabled",
        )},
        "rgb": {
            "supported": path is not None,
            "reason": rgb_reason,
            "current": rgb_current,
            "effects": [
                {"id": effect, "label": RGB_EFFECT_LABELS[effect]}
                for effect in RGB_EFFECTS
            ],
            "drift": rgb_drift,
        },
        "power_led": {
            "supported": power_supported,
            "reason": power_reason,
            "current": power_current,
            "managed": state["power_led_managed"],
            "drift": power_drift,
            "bios": AUDITED_BIOS if power_supported else "",
        },
        "recovery_required": state.get("pending") is not None,
        "error": _last_error,
    }


def _reconcile(*, force: bool = False) -> bool:
    global _last_error
    with _apply_lock:
        if not _recover_pending():
            _last_error = "An interrupted lighting change still requires recovery."
            return False
        state = _load_state()
        ok = True
        if state["control_enabled"]:
            path, reason = _rgb_capability()
            current = _read_rgb_snapshot(path) if path else None
            if path is None or current is None:
                ok = False
                _last_error = reason or "The joystick-ring state could not be read."
            else:
                try:
                    drifted = not _rgb_matches(state, current, path)
                except (OSError, ValueError):
                    drifted = True
                if force or drifted:
                    ok = _apply_rgb_target(state, force=force) and ok
        if state["power_led_managed"]:
            current_power = _read_power_led()
            if current_power is None:
                ok = False
                _last_error = _power_capability()[1] or "Power-button state is unavailable."
            elif force or current_power != state["power_led_enabled"]:
                ok = _write_power_led(state["power_led_enabled"]) and ok
        if ok:
            _last_error = ""
        return ok


def _suspend_offset() -> float | None:
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is None or not hasattr(time, "clock_gettime"):
        return None
    try:
        return time.clock_gettime(clock) - time.monotonic()
    except (OSError, ValueError):
        return None


def _resume_detected() -> bool:
    global _last_suspend_offset
    current = _suspend_offset()
    if current is None:
        return False
    previous = _last_suspend_offset
    _last_suspend_offset = current
    return previous is not None and current - previous >= 1.0


async def _offload(function: Callable, *args, **kwargs):
    return await module_runtime.offload('rgb', function, *args, **kwargs)


class Plugin:
    _setup_error = ""

    async def _drift_loop(self) -> None:
        last_check = time.monotonic()
        while True:
            await asyncio.sleep(RESUME_CHECK_S)
            try:
                if _resume_detected():
                    decky.logger.info("[lego-rgb] backend detected resume from suspend")
                    await self.reapply()
                    last_check = time.monotonic()
                    continue
                now = time.monotonic()
                if now < getattr(self, "_settle_until", 0.0) or now - last_check >= DRIFT_INTERVAL_S:
                    await _offload(_reconcile)
                    last_check = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                decky.logger.warning(f"[lego-rgb] drift verification failed: {exc}")

    async def _main(self) -> None:
        global _drift_task, _last_suspend_offset
        try:
            await _offload(_ensure_settings_file)
            _last_suspend_offset = _suspend_offset()
            # The controller may appear or reset after Decky starts. Keep a
            # bounded settling window even if the very first read was successful.
            self._settle_until = time.monotonic() + STARTUP_SETTLE_S
            if not await _offload(_reconcile):
                decky.logger.warning("[lego-rgb] startup state not yet confirmed; retrying during controller initialization")
            _drift_task = asyncio.create_task(self._drift_loop())
            decky.logger.info("[lego-rgb] lighting module ready")
        except Exception as exc:
            Plugin._setup_error = str(exc)
            decky.logger.error(f"[lego-rgb] setup failed: {exc}")

    async def _unload(self) -> None:
        global _drift_task
        if _drift_task:
            _drift_task.cancel()
            await asyncio.wait((_drift_task,), timeout=1.0)
        _drift_task = None
        decky.logger.info("[lego-rgb] unloaded")

    async def _uninstall(self) -> None:
        def _restore() -> None:
            with _apply_lock:
                if not _recover_pending():
                    return
                state = _load_state()
                ok = True
                original = state.get("original_rgb")
                if state["control_enabled"] and original:
                    path, _ = _rgb_capability()
                    ok = path is not None and _apply_rgb_snapshot(
                        path, original, force=True
                    ) and ok
                original_power = state.get("power_led_original_enabled")
                if state["power_led_managed"] and original_power is not None:
                    ok = _write_power_led(original_power) and ok
                if ok:
                    restored = dict(DEFAULT_STATE)
                    restored.update({
                        "configured": state["configured"],
                        "rings_enabled": state["rings_enabled"],
                        "effect": state["effect"],
                        "hue": state["hue"],
                        "saturation": state["saturation"],
                        "brightness": state["brightness"],
                        "speed": state["speed"],
                    })
                    _save_state(restored)
        await _offload(_restore)
        decky.logger.info("[lego-rgb] uninstall restoration finished")

    async def get_status(self) -> dict[str, Any]:
        return await _offload(_status)

    async def set_control_enabled(self, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            return {"success": False, "error": "enabled must be a boolean"}

        def _change() -> tuple[bool, str]:
            with _apply_lock:
                if not _recover_pending():
                    return False, "Automatic recovery must finish first."
                state = _load_state()
                if state["control_enabled"] == enabled:
                    return True, ""
                path, reason = _rgb_capability()
                before = _read_rgb_snapshot(path) if path else None
                if path is None or before is None:
                    return False, reason or "The joystick-ring state could not be read."
                candidate = copy.deepcopy(state)
                if enabled:
                    if not candidate["configured"]:
                        _seed_from_snapshot(candidate, before)
                    candidate["original_rgb"] = before
                    candidate["control_enabled"] = True
                    operation = lambda: _apply_rgb_target(candidate, force=True)
                else:
                    original = candidate.get("original_rgb")
                    if original is None:
                        return False, "The original joystick-ring state is unavailable."
                    candidate["control_enabled"] = False
                    candidate["original_rgb"] = None
                    operation = lambda: _apply_rgb_snapshot(path, original, force=True)
                return _transaction(
                    state, candidate, before_rgb=before, apply=operation
                )

        success, error = await _offload(_change)
        return {"success": success, "error": error, "status": await self.get_status()}

    async def _set_rgb_field(self, field: str, value: Any) -> dict[str, Any]:
        def _change() -> tuple[bool, str]:
            with _apply_lock:
                if not _recover_pending():
                    return False, "Automatic recovery must finish first."
                state = _load_state()
                if not state["control_enabled"]:
                    return False, "Enable RGB control first."
                path, reason = _rgb_capability()
                before = _read_rgb_snapshot(path) if path else None
                if path is None or before is None:
                    return False, reason or "The joystick-ring state could not be read."
                candidate = copy.deepcopy(state)
                candidate[field] = value
                candidate = _sanitize_state(candidate, include_pending=False)
                return _transaction(
                    state,
                    candidate,
                    before_rgb=before,
                    apply=lambda: _apply_rgb_target(candidate),
                )

        success, error = await _offload(_change)
        return {"success": success, "error": error, "status": await self.get_status()}

    async def set_rings_enabled(self, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            return {"success": False, "error": "enabled must be a boolean"}
        return await self._set_rgb_field("rings_enabled", enabled)

    async def set_effect(self, effect: str) -> dict[str, Any]:
        if not isinstance(effect, str) or effect.lower() not in RGB_EFFECTS:
            return {"success": False, "error": "unsupported lighting effect"}
        return await self._set_rgb_field("effect", effect.lower())

    async def set_color(self, hue: int, saturation: int) -> dict[str, Any]:
        if type(hue) is not int or type(saturation) is not int:
            return {"success": False, "error": "color values must be integers"}
        if not 0 <= hue <= 359 or not 0 <= saturation <= 100:
            return {"success": False, "error": "color value is outside the supported range"}

        def _change() -> tuple[bool, str]:
            with _apply_lock:
                if not _recover_pending():
                    return False, "Automatic recovery must finish first."
                state = _load_state()
                if not state["control_enabled"]:
                    return False, "Enable RGB control first."
                path, reason = _rgb_capability()
                before = _read_rgb_snapshot(path) if path else None
                if path is None or before is None:
                    return False, reason or "The joystick-ring state could not be read."
                candidate = copy.deepcopy(state)
                candidate["hue"] = hue
                candidate["saturation"] = saturation
                return _transaction(
                    state,
                    candidate,
                    before_rgb=before,
                    apply=lambda: _apply_rgb_target(candidate),
                )

        success, error = await _offload(_change)
        return {"success": success, "error": error, "status": await self.get_status()}

    async def set_brightness(self, brightness: int) -> dict[str, Any]:
        if type(brightness) is not int or not 0 <= brightness <= 100:
            return {"success": False, "error": "brightness must be from 0 to 100"}
        return await self._set_rgb_field("brightness", brightness)

    async def set_speed(self, speed: int) -> dict[str, Any]:
        if type(speed) is not int or not 0 <= speed <= 100:
            return {"success": False, "error": "speed must be from 0 to 100"}
        return await self._set_rgb_field("speed", speed)

    async def set_power_led(self, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            return {"success": False, "error": "enabled must be a boolean"}

        def _change() -> tuple[bool, str]:
            with _apply_lock:
                if not _recover_pending():
                    return False, "Automatic recovery must finish first."
                supported, reason = _power_capability()
                before = _read_power_led() if supported else None
                if before is None:
                    return False, reason or "The power-button state could not be read."
                state = _load_state()
                candidate = copy.deepcopy(state)
                if not candidate["power_led_managed"]:
                    candidate["power_led_original_enabled"] = before
                candidate["power_led_managed"] = True
                candidate["power_led_enabled"] = enabled
                return _transaction(
                    state,
                    candidate,
                    before_power=before,
                    apply=lambda: _write_power_led(enabled),
                )

        success, error = await _offload(_change)
        return {"success": success, "error": error, "status": await self.get_status()}

    async def restore_original(self) -> dict[str, Any]:
        def _restore() -> tuple[bool, str]:
            with _apply_lock:
                if not _recover_pending():
                    return False, "Automatic recovery must finish first."
                state = _load_state()
                before_rgb = None
                before_power = None
                operations: list[Callable[[], bool]] = []
                candidate = copy.deepcopy(state)
                if state["control_enabled"]:
                    path, reason = _rgb_capability()
                    before_rgb = _read_rgb_snapshot(path) if path else None
                    original = state.get("original_rgb")
                    if path is None or before_rgb is None or original is None:
                        return False, reason or "The joystick-ring state could not be restored."
                    operations.append(
                        lambda path=path, original=original: _apply_rgb_snapshot(
                            path, original, force=True
                        )
                    )
                    candidate["control_enabled"] = False
                    candidate["original_rgb"] = None
                if state["power_led_managed"]:
                    before_power = _read_power_led()
                    original_power = state.get("power_led_original_enabled")
                    if before_power is None or original_power is None:
                        return False, "The power-button state could not be restored."
                    operations.append(
                        lambda original_power=original_power: _write_power_led(original_power)
                    )
                    candidate["power_led_managed"] = False
                    candidate["power_led_original_enabled"] = None
                    candidate["power_led_enabled"] = original_power
                if not operations:
                    return True, ""

                def _apply_all() -> bool:
                    outcomes = [operation() for operation in operations]
                    return all(outcomes)

                return _transaction(
                    state,
                    candidate,
                    before_rgb=before_rgb,
                    before_power=before_power,
                    apply=_apply_all,
                )

        success, error = await _offload(_restore)
        return {"success": success, "error": error, "status": await self.get_status()}

    async def reapply(self) -> dict[str, Any]:
        self._settle_until = time.monotonic() + STARTUP_SETTLE_S
        deadline = time.monotonic() + REAPPLY_WAIT_S
        while True:
            state = await _offload(_load_state)
            needs_rgb = state["control_enabled"]
            success = await _offload(_reconcile, force=True)
            if success or not needs_rgb or time.monotonic() >= deadline:
                return {"success": success, "status": await self.get_status()}
            await asyncio.sleep(REAPPLY_STEP_S)

# SPDX-License-Identifier: BSD-3-Clause AND MIT
# Copyright (c) 2026 Rayekkk
# Portions copyright (c) 2026 piyush-tyagi-13 and M4ttiA, MIT - see LICENSE.MIT
# https://github.com/Rayekkk/LeGo-Vibe-Control

import decky
import module_runtime
from diagnostics import event, transition
from active_app import resolve_running_app, scan_proc_for_appid
import copy
import os
import re
import sys
import glob
import time
import asyncio
import struct
import fcntl
import threading
from safe_settings import CorruptSettings, SettingsManager

# ── Optional pyudev - graceful fallback to glob if unavailable ─────────────────

# Ensure bundled libs (pyudev/) are importable regardless of how Decky
# sets up sys.path before loading this module.
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

try:
    import pyudev as _pyudev
    _udev_ctx = _pyudev.Context()
    _PYUDEV = True
except Exception as _e:
    decky.logger.warning(f"[lego-vibe] pyudev {'import' if isinstance(_e, ImportError) else 'initialization'} failed")
    _pyudev = None
    _udev_ctx = None
    _PYUDEV = False

# ── Constants ──────────────────────────────────────────────────────────────────


# Fallback value lists, used only when the driver does not expose the
# matching <attr>_index file. The driver on kernel 6.18 does expose them.
LEVEL_NAMES  = ["off", "low", "medium", "high"]
RUMBLE_MODES = ["fps", "racing", "standard", "spg", "rpg"]

DEFAULT_APP = "0"
APP_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_GAME_PROFILES = 512
MAX_NAME_LENGTH = 128

# How long reapply() keeps looking for the controller after a resume. The
# notification fires the moment the system wakes, several seconds before USB has
# finished re-enumerating.
_REAPPLY_WAIT_S = 12.0
_REAPPLY_STEP_S = 0.5

# Profile field names. These match the shape the frontend has always
# persisted, so existing settings.json files migrate without translation.
PKEY_LEVEL  = "level"
PKEY_MODE   = "mode"
PKEY_TP_INT = "touchpadIntensity"
PKEY_TP_EN  = "touchpadEnabled"

DEFAULT_PROFILE = {
    PKEY_LEVEL:  2,     # medium
    PKEY_MODE:   0,     # fps
    PKEY_TP_INT: 2,     # medium
    PKEY_TP_EN:  True,
}

SETTINGS_KEY_GAME_PROFILES = "game_profiles"
SETTINGS_KEY_SCHEMA        = "schema_version"
# Driver values found before the first write: {"pending": True} until captured,
# {} when unknown (an existing install), else {attribute: value}.
SETTINGS_KEY_BASELINE      = "baseline"
_PROFILE_ATTRS = ('rumble_intensity', 'left_handle/rumble_mode', 'right_handle/rumble_mode',
                  'touchpad/vibration_intensity', 'touchpad/vibration_enabled')
CURRENT_SCHEMA             = 2

settings = SettingsManager(
    name="vibration_settings",
    settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR,
)

# Profiles are read from the hotplug executor thread while RPC handlers write
# them from the event loop. Re-entrant because the write paths load first.
_settings_lock = threading.RLock()

# Sysfs attribute unique to the hid-lenovo-go driver
_SIGNATURE_ATTR = "rumble_intensity"

# Module-level device path (sysfs dir that owns rumble_intensity)
_device_path: str | None = None
_discovery_method: str | None = None
_monitor_task: asyncio.Task | None = None
_drift_task: asyncio.Task | None = None
_last_suspend_offset: float | None = None
_DRIFT_INTERVAL_S = 30.0
_RESUME_CHECK_S = 5.0

# App id the frontend last reported as running. Drives profile resolution
# for hotplug and resume re-applies, which have no frontend round trip.
_active_app_id: str = DEFAULT_APP
_active_app_ts: float | None = None
_READBACK_ATTEMPTS = 3
_READBACK_DELAY_S = 0.05

# Serialises sysfs writes between RPC calls, hotplug and resume.
_apply_lock = threading.Lock()

# Guards against stacking effects, e.g. the test button being tapped twice.
# Two concurrent FF effects on one device combine into something that
# resembles neither pattern.
_ff_busy = False

# Force-feedback ioctl numbers (Linux x86-64, sizeof(ff_effect) == 48)
_EVIOCGBIT_FF = 0x80204535
_EVIOCGID     = 0x80084502  # _IOR('E', 0x02, struct input_id), Linux UAPI.
_EVIOCSFF     = 0x40304580
_EVIOCRMFF    = 0x40044581
_EV_FF        = 0x15
_FF_RUMBLE    = 0x50


# ── Device discovery ───────────────────────────────────────────────────────────

def _discover() -> tuple[str | None, str | None]:
    """
    Return (sysfs_dir, method) where sysfs_dir contains rumble_intensity.
    Tries pyudev enumeration first; falls back to a driver-agnostic
    glob over /sys/bus/hid/drivers/*/ (no driver name hardcoded).
    """
    if _PYUDEV:
        found_any = False
        for dev in _udev_ctx.list_devices(subsystem='hid'):
            found_any = True
            candidate = os.path.join(dev.sys_path, _SIGNATURE_ATTR)
            if os.path.exists(candidate):
                return dev.sys_path, "pyudev"
        event("vibration", "device_discovery", level="warning", source="pyudev",
              reason="compatible_device_missing" if found_any else "no_hid_devices", repeat=60)

    # Glob fallback - driver-name-agnostic, searches all HID drivers.
    # Path structure: /sys/bus/hid/drivers/<driver>/<device_id>/rumble_intensity
    patterns = [
        (f"/sys/bus/hid/drivers/*/*/{_SIGNATURE_ATTR}", "glob-hid"),
        (f"/sys/module/*/drivers/hid:*/{_SIGNATURE_ATTR}", "glob-module"),
    ]
    for pattern, method in patterns:
        for match in glob.glob(pattern):
            path = os.path.dirname(match)
            return path, method

    event("vibration", "device_discovery", level="warning", source="all",
          reason="compatible_device_missing", repeat=60)
    return None, None


def _get_device_path() -> str | None:
    global _device_path, _discovery_method
    if _device_path is not None:
        if os.path.exists(os.path.join(_device_path, _SIGNATURE_ATTR)):
            return _device_path
        _forget_device()
    _device_path, _discovery_method = _discover()
    return _device_path


def _forget_device() -> None:
    """Drop the cached device path and everything derived from it."""
    global _device_path, _discovery_method, _last_applied_values
    if _device_path is not None:
        _invalidate_cache(_device_path)
        _capabilities_cache.pop(_device_path, None)
    _device_path = None
    _discovery_method = None
    _last_applied_values = None
    _ff_device_cache["node"] = None


def _device_ids(sys_path: str) -> tuple[str, str] | None:
    """
    Extract (vendor, product) as lowercase hex from a HID sysfs dir name
    like '0003:17EF:61EB.0013'.
    """
    m = re.match(r'^[0-9A-Fa-f]+:([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})',
                 os.path.basename(sys_path))
    return (m.group(1).lower(), m.group(2).lower()) if m else None


# ── Driver capabilities (valid enum values, read from <attr>_index) ────────────

_capabilities_cache: dict[str, dict[str, list[str]]] = {}


def _read_enum(sys_path: str, rel_path: str, fallback: list[str]) -> list[str]:
    """
    The driver publishes the legal values of every enum attribute in a
    sibling '<attr>_index' file, space separated. Prefer that over a
    hardcoded list so new driver values do not need a plugin release.
    """
    try:
        with open(os.path.join(sys_path, rel_path + '_index')) as f:
            values = f.read().split()
        if values:
            return values
    except OSError:
        pass
    return list(fallback)


def _capabilities(sys_path: str) -> dict[str, list[str]]:
    caps = _capabilities_cache.get(sys_path)
    if caps is None:
        caps = {
            "intensity":    _read_enum(sys_path, 'rumble_intensity', LEVEL_NAMES),
            "mode":         _read_enum(sys_path, 'left_handle/rumble_mode', RUMBLE_MODES),
            "tp_intensity": _read_enum(sys_path, 'touchpad/vibration_intensity', LEVEL_NAMES),
        }
        _capabilities_cache[sys_path] = caps
    return caps


def _enum_at(values: list[str], index: int) -> str:
    return values[max(0, min(len(values) - 1, int(index)))]


# ── Sysfs writes ───────────────────────────────────────────────────────────────

# In-memory cache of the last value we successfully wrote to each attribute.
# The hid-lenovo-go driver does not reliably reflect written values on
# subsequent reads - rumble_intensity returns the value from *before* the
# last write - so we track state ourselves rather than reading back.
# Anything that may have reset the hardware (hotplug, resume from suspend)
# must invalidate this, otherwise writes get skipped as no-ops.
_attr_cache: dict[tuple[str, str], str] = {}
# The complete profile last confirmed by every driver attribute. Unlike a
# frontend heartbeat flag, this also follows background fallback/hotplug applies.
_last_applied_values: dict | None = None


def _invalidate_cache(sys_path: str | None = None) -> None:
    if sys_path is None:
        _attr_cache.clear()
    else:
        for key in [k for k in _attr_cache if k[0] == sys_path]:
            del _attr_cache[key]


def _write_attr(sys_path: str, rel_path: str, value: str, force: bool = False) -> bool:
    key = (sys_path, rel_path)
    if not force and _attr_cache.get(key) == value:
        try:
            with open(os.path.join(sys_path, rel_path), encoding="ascii") as handle:
                if handle.read().strip() == value:
                    return True
        except (OSError, UnicodeError):
            # Fall through to the normal write path.  The device may have been
            # rebound under the same sysfs path since the cache was populated.
            pass
    path = os.path.join(sys_path, rel_path)
    try:
        with open(path, 'w') as f:
            f.write(value + '\n')
        for attempt in range(_READBACK_ATTEMPTS):
            with open(path, encoding="ascii") as source:
                if source.read().strip() == value:
                    break
            if attempt + 1 == _READBACK_ATTEMPTS:
                raise OSError("The controller did not confirm the requested value")
            time.sleep(_READBACK_DELAY_S)
        _attr_cache[key] = value
        return True
    except (OSError, UnicodeError) as exc:
        _attr_cache.pop(key, None)
        event("vibration", "attribute_write", level="error", error=exc,
              attribute=rel_path, outcome="failed", repeat=60)
        return False


def _capture_baseline(sys_path: str) -> None:
    """Persist the driver's values once, before the plugin first writes them."""
    with _settings_lock:
        if settings.getSetting(SETTINGS_KEY_BASELINE, None) != {"pending": True}:
            return
        values = {}
        try:
            for attr in _PROFILE_ATTRS:
                with open(os.path.join(sys_path, attr), encoding="ascii") as handle:
                    values[attr] = handle.read(64).strip()
        except (OSError, UnicodeError):
            values = {}  # unknown; never retried after our own write
        settings.setSetting(SETTINGS_KEY_BASELINE, values)
        settings.commit()


def _restore_baseline() -> bool:
    """Release: the captured driver values, else DEFAULT_PROFILE."""
    global _last_applied_values
    with _settings_lock:
        _read_valid_settings()
        device = _get_device_path()
        if device is not None:
            _capture_baseline(device)  # a pending baseline must not fall through to DEFAULT_PROFILE
        baseline = settings.getSetting(SETTINGS_KEY_BASELINE, None)
    if isinstance(baseline, dict) and set(baseline) == set(_PROFILE_ATTRS):
        with _apply_lock:
            p = _get_device_path()
            caps = _capabilities(p) if p is not None else {}
            allowed = dict(zip(_PROFILE_ATTRS, (caps.get("intensity"), caps.get("mode"), caps.get("mode"),
                                                caps.get("tp_intensity"), ["true", "false"])))
            if p is not None and all(baseline[a] in (allowed[a] or ()) for a in allowed):
                _last_applied_values = None
                return all([_write_attr(p, attr, baseline[attr], True) for attr in _PROFILE_ATTRS])
    return _apply_settings(dict(DEFAULT_PROFILE), None, True)


async def _offload(fn, *args):
    """Run blocking sysfs/settings work off the loop so RPCs and hotplug keep draining."""
    def run():
        with _settings_lock:
            return fn(*args)
    return await module_runtime.offload('vibration', run)


def _apply_settings(values: dict, sys_path: str | None = None,
                    force: bool = False) -> bool:
    """Write a full profile to the hardware. Every attribute is attempted."""
    global _last_applied_values
    # Explicit withdrawal/uninstall can reach this with driver defaults rather
    # than a loaded profile. They must also preserve unrecoverable user state.
    with _settings_lock:
        _read_valid_settings()
        target = sys_path or _get_device_path()
        if target is not None:
            _capture_baseline(target)  # before _apply_lock: lock order is settings, then apply
    with _apply_lock:
        p = sys_path or _get_device_path()
        if p is None:
            _last_applied_values = None
            return False
        caps = _capabilities(p)
        mode = _enum_at(caps["mode"], values[PKEY_MODE])
        results = [
            _write_attr(p, 'rumble_intensity',
                        _enum_at(caps["intensity"], values[PKEY_LEVEL]), force),
            _write_attr(p, 'left_handle/rumble_mode',  mode, force),
            _write_attr(p, 'right_handle/rumble_mode', mode, force),
            _write_attr(p, 'touchpad/vibration_intensity',
                        _enum_at(caps["tp_intensity"], values[PKEY_TP_INT]), force),
            _write_attr(p, 'touchpad/vibration_enabled',
                        "true" if values[PKEY_TP_EN] else "false", force),
        ]
        success = all(results)
        _last_applied_values = copy.deepcopy(values) if success else None
        return success


# ── Profile store - the single source of truth for settings ────────────────────

def _coerce_int(value, default: int, low: int, high: int | None) -> int:
    """Clamp to a sane range, falling back to the default on junk. A
    hand-edited or truncated settings.json must never crash an apply."""
    try:
        result = max(low, int(value))
    except (TypeError, ValueError):
        return default
    return result if high is None else min(high, result)


def _coerce_bool(value, default: bool) -> bool:
    if type(value) is bool:
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    return default


def _normalise_app_id(value, *, allow_default: bool = True) -> str | None:
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or APP_ID_RE.fullmatch(value) is None:
        return None
    if not allow_default and value == DEFAULT_APP:
        return None
    return value


def _coerce_profile(raw: dict | None) -> dict:
    """Fill in missing fields and sanitise types."""
    raw = raw if isinstance(raw, dict) else {}
    return {
        PKEY_LEVEL:  _coerce_int(raw.get(PKEY_LEVEL), DEFAULT_PROFILE[PKEY_LEVEL], 0, 3),
        # No upper bound: the mode count comes from the driver, and
        # _enum_at() clamps against the list it actually reports.
        PKEY_MODE:   _coerce_int(raw.get(PKEY_MODE), DEFAULT_PROFILE[PKEY_MODE], 0, None),
        PKEY_TP_INT: _coerce_int(raw.get(PKEY_TP_INT), DEFAULT_PROFILE[PKEY_TP_INT], 0, 3),
        PKEY_TP_EN:  _coerce_bool(
            raw.get(PKEY_TP_EN, DEFAULT_PROFILE[PKEY_TP_EN]),
            DEFAULT_PROFILE[PKEY_TP_EN],
        ),
    }


def _read_valid_settings() -> None:
    settings.read()
    error = getattr(settings, "recovery_error", "")
    if error:
        raise CorruptSettings("Vibration settings recovery failed; saved controller controls were left untouched. " + error)


def _load_profiles() -> dict:
    """A private copy of the profile store: getSetting returns a live reference callers would mutate."""
    with _settings_lock:
        _read_valid_settings()
        raw_profiles = settings.getSetting(SETTINGS_KEY_GAME_PROFILES, {}) or {}
        raw_profiles = copy.deepcopy(raw_profiles) if isinstance(raw_profiles, dict) else {}
    profiles: dict[str, dict] = {}
    for raw_app_id, raw_entry in raw_profiles.items():
        app_id = _normalise_app_id(raw_app_id)
        if app_id is None or not isinstance(raw_entry, dict):
            continue
        entry = {
            "overwrite": (
                False if app_id == DEFAULT_APP else
                _coerce_bool(raw_entry.get("overwrite"), False)
            ),
            "settings": _coerce_profile(raw_entry.get("settings")),
        }
        if isinstance(raw_entry.get("name"), str):
            entry["name"] = raw_entry["name"][:MAX_NAME_LENGTH]
        profiles[app_id] = entry
        # Keep older over-capacity stores intact. Limit creation, never truncate
        # saved profiles during a read followed by an unrelated edit.
    if DEFAULT_APP not in profiles:
        profiles[DEFAULT_APP] = {"overwrite": False, "settings": dict(DEFAULT_PROFILE)}
    return profiles


def _save_profiles(profiles: dict) -> None:
    with _settings_lock:
        _read_valid_settings()
        settings.setSetting(SETTINGS_KEY_GAME_PROFILES, profiles)
        settings.commit()


def _running_app_id() -> str:
    return resolve_running_app(_active_app_id, _active_app_ts, time.monotonic(),
                               scan_proc_for_appid) or DEFAULT_APP


def _resolve_app_id(profiles: dict, app_id: str | None = None) -> str:
    """A game's profile only wins while it is both running and flagged."""
    app_id = _running_app_id() if app_id is None else app_id
    if app_id != DEFAULT_APP and profiles.get(app_id, {}).get("overwrite"):
        return app_id
    return DEFAULT_APP


def _active_values(profiles: dict | None = None, app_id: str | None = None) -> dict:
    profiles = _load_profiles() if profiles is None else profiles
    entry = profiles.get(_resolve_app_id(profiles, app_id), {})
    return _coerce_profile(entry.get("settings"))


def _migrate() -> None:
    """Create the default profile '0' and stamp the store schema, once."""
    with _settings_lock:
        _read_valid_settings()
        # A hand-edited or truncated store can hold anything here, and this runs
        # from _migration() - where a raise kills the plugin before its RPC
        # socket exists.
        try:
            schema = int(settings.getSetting(SETTINGS_KEY_SCHEMA, 1))
        except (TypeError, ValueError):
            schema = 1
        if settings.getSetting(SETTINGS_KEY_BASELINE, None) is None:
            # Only an empty store proves the controller still holds its own values.
            fresh = not settings.settings
            settings.setSetting(SETTINGS_KEY_BASELINE, {"pending": True} if fresh else {})
            settings.commit()
        if schema >= CURRENT_SCHEMA:
            return
        profiles = settings.getSetting(SETTINGS_KEY_GAME_PROFILES, {}) or {}
        if not isinstance(profiles, dict):
            profiles = {}
        if DEFAULT_APP not in profiles:
            profiles[DEFAULT_APP] = {"overwrite": False,
                                     "settings": _coerce_profile(None)}
            event("vibration", "settings_migrated", scope="global", version=str(CURRENT_SCHEMA))
        settings.setSetting(SETTINGS_KEY_GAME_PROFILES, profiles)
        settings.setSetting(SETTINGS_KEY_SCHEMA, CURRENT_SCHEMA)
        settings.commit()


def _profile_context() -> tuple[dict, dict]:
    """Resolve a game and its profile once for a complete read or mutation."""
    profiles = _load_profiles()
    running = _running_app_id()
    app_id = _resolve_app_id(profiles, running)
    return profiles, {
        "settings":   _active_values(profiles, running),
        "app_id":     running,
        "profile_id": app_id,
        "overwrite":  app_id != DEFAULT_APP,
    }


def _settings_payload() -> dict:
    """What get_settings() reports: the resolved profile plus which one it is."""
    return _profile_context()[1]


def _device_status() -> dict:
    p = _get_device_path()
    ids = _device_ids(p) if p else None
    return {
        "found":  p is not None,
        "paths":  [p] if p else [],
        "method": _discovery_method or "",
        "ids":    f"{ids[0]}:{ids[1]}" if ids else "",
    }


async def _emit_device_status() -> None:
    """Push only the driver status: a settings payload would race the panel's pending edits."""
    try:
        await decky.emit("device", await _offload(_device_status))
    except Exception as exc:
        event('vibration','device_status_notification_failed',level='warning',error=exc,repeat=60)


_suspend_offset = module_runtime.suspend_offset


def _resume_detected() -> bool:
    global _last_suspend_offset
    _last_suspend_offset, resumed = module_runtime.resumed(_last_suspend_offset, _suspend_offset())
    return resumed


def _update_active(field: str, value, *, profiles: dict | None = None,
                   profile_id: str | None = None) -> dict:
    """Write one field into whichever profile is currently in effect."""
    with _settings_lock:
        profiles = _load_profiles() if profiles is None else profiles
        app_id = _resolve_app_id(profiles) if profile_id is None else profile_id
        entry = profiles.setdefault(app_id, {"overwrite": app_id != DEFAULT_APP,
                                             "settings": dict(DEFAULT_PROFILE)})
        entry["settings"] = _coerce_profile(entry.get("settings"))
        entry["settings"][field] = value
        _save_profiles(profiles)
        return entry["settings"]


# ── Force feedback ─────────────────────────────────────────────────────────────

_ff_device_cache: dict[str, str | None] = {"node": None}


def _event_nodes() -> list[str]:
    # Numeric sort: a lexicographic sort puts event16 before event2, which is
    # how the Steam virtual pad used to win over the real controller.
    nodes = glob.glob('/dev/input/event*')
    return sorted(nodes, key=lambda n: int(re.sub(r'\D', '', os.path.basename(n)) or 0))


def _node_ids(node: str) -> tuple[str, str] | None:
    base = os.path.basename(node)
    try:
        with open(f'/sys/class/input/{base}/device/id/vendor') as f:
            vendor = f.read().strip().lower()
        with open(f'/sys/class/input/{base}/device/id/product') as f:
            product = f.read().strip().lower()
        return vendor, product
    except OSError:
        return None


def _has_rumble(node: str) -> bool:
    try:
        with open(node, 'rb') as fh:
            bits = bytearray(32)
            fcntl.ioctl(fh.fileno(), _EVIOCGBIT_FF, bits)
            return bool(bits[_FF_RUMBLE // 8] & (1 << (_FF_RUMBLE % 8)))
    except OSError:
        return False  # Not every input node answers the force-feedback ioctl.


def _find_ff_device() -> str | None:
    """Find rumble only on an endpoint matching the discovered HID identity."""
    sys_path = _get_device_path()
    want = _device_ids(sys_path) if sys_path else None
    if want is None:
        _ff_device_cache["node"] = None
        return None

    cached = _ff_device_cache.get("node")
    if cached and os.path.exists(cached) and _node_ids(cached) == want:
        return cached
    _ff_device_cache["node"] = None
    for node in _event_nodes():
        # Most input endpoints are keyboards or unrelated pads. Reject their
        # IDs before opening them for the more expensive capability ioctl.
        if _node_ids(node) == want and _has_rumble(node):
            event("vibration", "ff_device_discovered", source="evdev", supported=True)
            _ff_device_cache["node"] = node
            return node
    return None


def _play_rumble(node: str, duration: int, magnitude: int) -> None:
    """Verify the actual descriptor, then own its effect through final cleanup."""
    sys_path = _get_device_path()
    want = _device_ids(sys_path) if sys_path else None
    if want is None:
        raise RuntimeError("The Legion controller is no longer available.")
    fd = os.open(node, os.O_RDWR | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        # event numbers can be reused between discovery and open. EVIOCGID
        # describes this fd, unlike a later lookup of the path in sysfs.
        identity = bytearray(8)
        fcntl.ioctl(fd, _EVIOCGID, identity)
        _bus, vendor, product, _version = struct.unpack('HHHH', identity)
        if (f'{vendor:04x}', f'{product:04x}') != want:
            raise RuntimeError("The rumble endpoint changed; reopen Vibration and try again.")
        effect_buf = bytearray(struct.pack(
            '<HhHHHHHxxHH28x', _FF_RUMBLE, -1, 0, 0, 0,
            duration, 0, magnitude, magnitude,
        ))
        fcntl.ioctl(fd, _EVIOCSFF, effect_buf)
        effect_id = struct.unpack_from('<h', effect_buf, 2)[0]
        if effect_id < 0:
            raise RuntimeError(f"Driver rejected FF effect (id={effect_id})")

        def event(value):
            now = time.time()
            return struct.pack('<qqHHi', int(now), int((now % 1) * 1e6) % 1_000_000,
                               _EV_FF, effect_id, value)
        try:
            os.write(fd, event(1))
            time.sleep(duration / 1000.0)
        finally:
            try:
                os.write(fd, event(0))
            finally:
                # The kernel takes the effect id by value, despite _IOW(int).
                fcntl.ioctl(fd, _EVIOCRMFF, effect_id)
    finally:
        os.close(fd)


# ── Hotplug monitor (only when pyudev is available) ────────────────────────────

# 'add' fires before the driver has probed, so rumble_intensity usually does
# not exist yet. 'bind' is the event that means the driver is attached.
_HOTPLUG_ACTIONS = ("add", "bind", "change")


async def _monitor_hotplug() -> None:
    if not _PYUDEV:
        decky.logger.info("[lego-vibe] pyudev unavailable, hotplug monitor disabled")
        return

    monitor = _pyudev.Monitor.from_netlink(_udev_ctx)
    monitor.filter_by(subsystem='hid')
    monitor.start()

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    # Read the netlink socket via the event loop instead of parking a thread
    # pool worker in a blocking poll() for the lifetime of the plugin.
    # start() put the fd in non-blocking mode, so poll(0) drains and returns.
    def _on_readable() -> None:
        for device in iter(lambda: monitor.poll(0), None):
            queue.put_nowait((device.action, device.sys_path))

    loop.add_reader(monitor.fileno(), _on_readable)
    try:
        while True:
            action, sys_path = await queue.get()
            if module_runtime.is_paused('vibration'):
                continue
            if action in _HOTPLUG_ACTIONS:
                try:
                    await _handle_device_added(sys_path, action)
                except module_runtime.ControllerPaused:
                    continue
                except Exception as exc:  # One failed event (e.g. a settings read) must not end hotplug handling.
                    event('vibration', 'hotplug_event_failed', level='warning', error=exc, repeat=60)
            elif action in ('remove', 'unbind') and sys_path == _device_path:
                # Events can arrive out of order: an unbind/rebind cycle
                # delivers the removal after the device is already back. Trust
                # sysfs over the event ordering, or we would drop a device
                # that is present and stop applying settings to it.
                if os.path.exists(os.path.join(sys_path, _SIGNATURE_ATTR)):
                    decky.logger.info(
                        f"[lego-vibe] ignoring stale '{action}' - device is still present"
                    )
                else:
                    decky.logger.info("[lego-vibe] device disconnected")
                    _forget_device()
                    await _emit_device_status()
    except asyncio.CancelledError:
        pass
    finally:
        try:
            loop.remove_reader(monitor.fileno())
        except Exception:
            pass


@module_runtime.controller_operation('vibration')
async def _handle_device_added(sys_path: str, action: str) -> None:
    global _device_path, _discovery_method
    if module_runtime.is_paused('vibration'):
        return
    attribute = os.path.join(sys_path, _SIGNATURE_ATTR)
    if not os.path.exists(attribute):
        # Only 'add' and 'bind' can race the driver's probe. A 'change' on a
        # device that has no rumble_intensity is simply not our device, and
        # waiting on it would stall every other queued event for a second -
        # a single `udevadm trigger` fans out to every HID device on the bus.
        if action not in ('add', 'bind'):
            return
        for _ in range(10):
            await asyncio.sleep(0.1)
            if os.path.exists(attribute):
                break
        else:
            return

    # A 'change' on the device we already own is not skipped: it still means
    # the firmware may have reset, so we fall through and force a re-apply.
    try:
        module_runtime.require_available('vibration')
    except module_runtime.ControllerPaused:
        return
    event("vibration", "hotplug", source=action, supported=True)
    _device_path = sys_path
    _discovery_method = f"udev-{action}"
    _ff_device_cache["node"] = None

    def _apply() -> bool:
        _invalidate_cache(sys_path)
        _capabilities_cache.pop(sys_path, None)
        return _apply_settings(_active_values(), sys_path=sys_path, force=True)

    try:
        ok = await _offload(_apply)
    except module_runtime.ControllerPaused:
        # A pause can arrive during the preceding discovery wait. Keep the
        # hotplug consumer alive; the paused gate forbids this reapply.
        return
    transition("vibration", "profile_apply", ok, source=action, initial=False,
               level="info" if ok else "warning", outcome="confirmed" if ok else "deferred")
    await _emit_device_status()


# ── Plugin class ───────────────────────────────────────────────────────────────

class Plugin:

    # Surfaced through is_ready() so a failed start shows up in the panel
    # instead of leaving the user with sliders that silently do nothing.
    _setup_error: str | None = None

    async def _drift_loop(self):
        last_check = time.monotonic()
        while True:
            await asyncio.sleep(_RESUME_CHECK_S)
            if module_runtime.is_paused('vibration'):
                continue
            try:
                if _resume_detected():
                    decky.logger.info(
                        "[lego-vibe] backend detected resume from suspend")
                    await self.reapply()
                    last_check = time.monotonic()
                    continue
                now = time.monotonic()
                if now - last_check < _DRIFT_INTERVAL_S:
                    continue

                def _verify() -> bool:
                    # _write_attr reads hardware before accepting a cache hit, so
                    # this is a read-only pass unless something actually drifted.
                    return _apply_settings(_active_values())

                ok = await _offload(_verify)
                transition("vibration", "profile_apply", ok, stage="periodic", initial=False,
                           level="info" if ok else "warning", outcome="confirmed" if ok else "deferred")
                last_check = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                event("vibration", "drift_verification", level="warning", error=exc,
                      outcome="failed", repeat=60)

    async def _migration(self):
        """Prepare the store before _main() reads it.

        Nothing may escape: a raise here exits the plugin before its RPC socket
        exists, so the failure is reported through is_ready() instead.
        """
        self._migration_error = None
        try:
            await _offload(_migrate)
        except Exception as exc:
            Plugin._setup_error = f"settings migration failed: {exc}"
            self._migration_error = Plugin._setup_error
            event('vibration','migration_failed',level='error',error=exc)

    async def _main(self):
        Plugin._setup_error = getattr(self, '_migration_error', None)
        global _monitor_task, _drift_task, _last_suspend_offset
        try:
            def _start() -> bool:
                global _last_suspend_offset
                _last_suspend_offset = _suspend_offset()
                values = _active_values()
                return _apply_settings(values, force=True)
            confirmed = await _offload(_start)
            transition("vibration", "profile_apply", bool(confirmed), initial=False,
                       level="info" if confirmed else "warning", stage="startup",
                       outcome="confirmed" if confirmed else "deferred")
            _monitor_task = module_runtime.background_task(_monitor_hotplug())
            _drift_task = module_runtime.background_task(self._drift_loop())
            # Preserve migration failures, while a successful retry clears a
            # transient startup failure from the previous attempt.
        except Exception as exc:
            Plugin._setup_error = str(exc)
            event('vibration','startup_failed',level='error',error=exc)
            raise

    async def _unload(self):
        global _active_app_ts
        _active_app_ts = None
        global _monitor_task, _drift_task
        tasks = [task for task in (_monitor_task, _drift_task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            # Bounded: a worker-thread apply ignores cancel, and the loader's 5 s SIGKILL would skip _uninstall().
            await asyncio.wait(tasks, timeout=1.0)
        _monitor_task = None
        _drift_task = None
        decky.logger.info("[lego-vibe] unloaded")

    async def _uninstall(self):
        """Put the controller back on the values found before the first write.

        _apply_lock serialises this with any hotplug apply still running in a worker.
        """
        restored = await _offload(_restore_baseline)
        event("vibration", "restore", level="info" if restored else "warning",
              stage="uninstall", restored=bool(restored),
              outcome="confirmed" if restored else "unconfirmed")

    # ---- RPC surface ------------------------------------------------ #

    async def is_ready(self) -> dict:
        return {"ready": Plugin._setup_error is None, "error": Plugin._setup_error or ""}

    async def get_capabilities(self) -> dict:
        def _do() -> dict:
            p = _get_device_path()
            return _capabilities(p) if p else {
                "intensity": LEVEL_NAMES, "mode": RUMBLE_MODES, "tp_intensity": LEVEL_NAMES,
            }
        return await _offload(_do)

    async def get_settings(self) -> dict:
        """Resolved settings for whichever profile is currently in effect."""
        return await _offload(_settings_payload)

    async def set_active_app(self, app_id: str) -> dict:
        """
        Frontend reports the running game. Applying here rather than in the
        frontend keeps hotplug and resume able to pick the right profile.
        """
        global _active_app_id
        app_id = _normalise_app_id(app_id if app_id not in (None, "") else DEFAULT_APP)
        if app_id is None:
            return {"success": False, "changed": False,
                    "error": "invalid app id", **(await _offload(_settings_payload))}
        def _do() -> dict:
            global _active_app_id, _active_app_ts
            changed = app_id != _active_app_id or app_id != _running_app_id()
            _active_app_id = app_id
            _active_app_ts = time.monotonic()
            _profiles, payload = _profile_context()
            values = payload["settings"]
            if (not changed and getattr(self, "_active_applied", False)
                    and _last_applied_values == values):
                return {"success": True, "changed": False, **payload}
            ok = _apply_settings(values)
            self._active_applied = ok
            return {"success": ok, "changed": changed, **payload}
        return await _offload(_do)

    async def _set_field(self, field: str, value, expected_app_id=None, expected_profile_id=None) -> dict:
        def _do() -> dict:
            profiles, payload = _profile_context()
            if ((expected_app_id is not None and expected_app_id != payload["app_id"])
                    or (expected_profile_id is not None and expected_profile_id != payload["profile_id"])):
                return {"success": False, "error": "Active profile changed; reload and try again", **payload}
            values = _update_active(field, value, profiles=profiles, profile_id=payload["profile_id"])
            return {"success": _apply_settings(values), **payload, "settings": values}
        return await _offload(_do)

    async def set_intensity(self, level: int, expected_app_id=None, expected_profile_id=None) -> dict:
        if type(level) is not int:
            return {"success": False, "error": "level must be an integer"}
        return await self._set_field(PKEY_LEVEL, max(0, min(3, int(level))), expected_app_id, expected_profile_id)

    async def set_rumble_mode(self, mode_idx: int, expected_app_id=None, expected_profile_id=None) -> dict:
        """Set the vibration pattern; the firmware plays its own demo, so none is added here."""
        if type(mode_idx) is not int:
            return {"success": False, "error": "mode must be an integer"}
        return await self._set_field(PKEY_MODE, max(0, min(63, mode_idx)), expected_app_id, expected_profile_id)

    async def set_touchpad_intensity(self, level: int, expected_app_id=None, expected_profile_id=None) -> dict:
        if type(level) is not int:
            return {"success": False, "error": "level must be an integer"}
        return await self._set_field(PKEY_TP_INT, max(0, min(3, int(level))), expected_app_id, expected_profile_id)

    async def set_touchpad_enabled(self, enabled: bool, expected_app_id=None, expected_profile_id=None) -> dict:
        if type(enabled) is not bool:
            return {"success": False, "error": "enabled must be a boolean"}
        return await self._set_field(PKEY_TP_EN, enabled, expected_app_id, expected_profile_id)

    async def reset_to_default(self, expected_app_id=None, expected_profile_id=None) -> dict:
        def _do() -> dict:
            profiles, payload = _profile_context()
            if ((expected_app_id is not None and expected_app_id != payload["app_id"])
                    or (expected_profile_id is not None and expected_profile_id != payload["profile_id"])):
                return {"success": False, "error": "Active profile changed; reload and try again", **payload}
            app_id = payload["profile_id"]
            entry = profiles.setdefault(app_id, {"overwrite": app_id != DEFAULT_APP,
                                                 "settings": {}})
            entry["settings"] = dict(DEFAULT_PROFILE)
            _save_profiles(profiles)
            ok = _apply_settings(entry["settings"], force=True)
            return {"success": ok, **payload, "settings": entry["settings"]}
        return await _offload(_do)

    @module_runtime.controller_operation('vibration')
    async def reapply(self) -> dict:
        """Force every attribute back after resume, when the controller returns at firmware defaults.

        Retried: the resume notification arrives seconds before USB re-enumerates.
        """
        deadline = time.monotonic() + _REAPPLY_WAIT_S

        def _do() -> dict:
            _forget_device()
            values = _active_values()
            return {"success": _apply_settings(values, force=True), "settings": values}

        while True:
            result = await _offload(_do)
            if result["success"] or time.monotonic() >= deadline:
                transition("vibration", "profile_apply", result["success"], initial=False,
                           level="info" if result["success"] else "warning",
                           stage="resume", outcome="confirmed" if result["success"] else "deferred")
                return result
            await asyncio.sleep(_REAPPLY_STEP_S)

    # ---- Per-game profiles ------------------------------------------ #

    async def set_profile_overwrite(self, app_id: str, enabled: bool,
                                    name: str = "") -> dict:
        """Turn a per-game profile on or off and apply the result."""
        app_id = _normalise_app_id(app_id, allow_default=False)
        if app_id is None or type(enabled) is not bool:
            return {"success": False, "error": "invalid profile request"}
        name = name[:MAX_NAME_LENGTH] if isinstance(name, str) else ""

        def _do() -> dict:
            profiles, context = _profile_context()
            entry = profiles.get(app_id)
            if entry is None:
                if len(profiles) >= MAX_GAME_PROFILES:
                    return {**context, "success": False,
                            "error": "The vibration profile limit has been reached."}
                # Seed a new per-game profile from whatever is active right now.
                entry = {"overwrite": enabled, "settings": _active_values(profiles, context["app_id"])}
                profiles[app_id] = entry
            else:
                entry["overwrite"] = enabled
                entry["settings"] = _coerce_profile(entry.get("settings"))
            # Remember the title so the profile list is readable when the game
            # is not running and Steam cannot resolve the id for us.
            if name:
                entry["name"] = name
            _save_profiles(profiles)
            running = context["app_id"]
            profile_id = _resolve_app_id(profiles, running)
            values = _active_values(profiles, running)
            ok = _apply_settings(values)
            return {"success": ok, "settings": values, "app_id": running,
                    "profile_id": profile_id, "overwrite": profile_id != DEFAULT_APP}
        return await _offload(_do)

    # ---- Driver status ---------------------------------------------- #

    async def get_driver_status(self) -> dict:
        return await _offload(_device_status)

    # ---- Test ------------------------------------------------------- #

    async def test_vibration(self, duration_ms: int = 500) -> dict:
        global _ff_busy
        if _ff_busy:
            # Dropping the request beats queueing a pile of buzzes behind a
            # slider the user is still dragging.
            return {"success": False, "error": "A vibration is already playing"}
        # Claimed before the first await: everything below yields to the event
        # loop, so checking here and setting the flag later would let two
        # concurrent taps both get through and stack their effects.
        _ff_busy = True
        try:
            values = await _offload(_active_values)
            level = values[PKEY_LEVEL]
            if level <= 0:
                return {"success": False,
                        "error": "Intensity is Off - raise it to feel the test"}

            intensity_pct = [0, 33, 66, 100][max(0, min(3, level))]
            duration = max(100, min(2000, int(duration_ms)))
            magnitude = int(0xFFFF * intensity_pct / 100)

            # Filter by HID identity before probing matching endpoints. The
            # final worker also verifies the actual opened descriptor.
            ff_path = await _offload(_find_ff_device)
            if ff_path is None:
                decky.logger.warning("[lego-vibe] test_vibration: no FF device found")
                return {"success": False, "error": "No matching Legion controller rumble input was found. Reconnect the controller and retry."}

            await module_runtime.complete(_offload(_play_rumble, ff_path, duration, magnitude))
            event("vibration", "test_complete", duration_ms=duration, outcome="confirmed")
            return {"success": True}
        except Exception as exc:
            _ff_device_cache["node"] = None
            event('vibration', 'test_failed', level='warning', error=exc)
            return {"success": False, "error": str(exc)}
        finally:
            _ff_busy = False

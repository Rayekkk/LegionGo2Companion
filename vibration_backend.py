# SPDX-License-Identifier: BSD-3-Clause AND MIT
# Copyright (c) 2026 Rayekkk
# Portions copyright (c) 2026 piyush-tyagi-13 and M4ttiA, MIT - see LICENSE.MIT
# https://github.com/Rayekkk/LeGo-Vibe-Control

import decky
import module_runtime
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

# Not `updater`: the loader aliases its own decky_loader.updater to that bare
# name before we are imported, and sys.modules wins over sys.path. Keep this
# backend-specific helper name for version/TLS compatibility.
from vibration_updater import Updater  # noqa: E402 - needs the sys.path line above

try:
    import pyudev as _pyudev
    _udev_ctx = _pyudev.Context()
    _PYUDEV = True
except ImportError as _e:
    decky.logger.warning(f"[lego-vibe] pyudev import failed: {_e}")
    _pyudev = None
    _udev_ctx = None
    _PYUDEV = False
except Exception as _e:
    decky.logger.warning(f"[lego-vibe] pyudev init failed (libudev?): {_e}")
    _pyudev = None
    _udev_ctx = None
    _PYUDEV = False

# ── Constants ──────────────────────────────────────────────────────────────────


# Retained local version and TLS compatibility helper; no standalone updates.
updater = Updater(
    user_agent="lego-vibe-plugin",
    log_prefix="[lego-vibe]",
    plugin_dir=PLUGIN_DIR,
    logger=decky.logger,
)

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
CURRENT_SCHEMA             = 2

# Pre-schema-2 keys. Read once during migration, never written again.
_LEGACY_KEYS = {
    PKEY_LEVEL:  "intensity_level",
    PKEY_MODE:   "rumble_mode",
    PKEY_TP_INT: "touchpad_intensity",
    PKEY_TP_EN:  "touchpad_enabled",
}

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

# Serialises sysfs writes between RPC calls, hotplug and resume.
_apply_lock = threading.Lock()

# Guards against stacking effects, e.g. the test button being tapped twice.
# Two concurrent FF effects on one device combine into something that
# resembles neither pattern.
_ff_busy = False

# Force-feedback ioctl numbers (Linux x86-64, sizeof(ff_effect) == 48)
_EVIOCGBIT_FF = 0x80204535
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
                decky.logger.info(f"[lego-vibe] found via pyudev: {dev.sys_path}")
                return dev.sys_path, "pyudev"
        if not found_any:
            decky.logger.warning("[lego-vibe] pyudev returned no HID devices at all")
        else:
            decky.logger.warning("[lego-vibe] pyudev: no HID device has rumble_intensity")

    # Glob fallback - driver-name-agnostic, searches all HID drivers.
    # Path structure: /sys/bus/hid/drivers/<driver>/<device_id>/rumble_intensity
    patterns = [
        (f"/sys/bus/hid/drivers/*/*/{_SIGNATURE_ATTR}", "glob-hid"),
        (f"/sys/module/*/drivers/hid:*/{_SIGNATURE_ATTR}", "glob-module"),
    ]
    for pattern, method in patterns:
        for match in glob.glob(pattern):
            path = os.path.dirname(match)
            decky.logger.info(f"[lego-vibe] found via {method} ({pattern}): {path}")
            return path, method

    decky.logger.warning("[lego-vibe] device not found (pyudev and glob both failed)")
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
    global _device_path, _discovery_method
    if _device_path is not None:
        _invalidate_cache(_device_path)
        _capabilities_cache.pop(_device_path, None)
    _device_path = None
    _discovery_method = None
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
        decky.logger.info(f"[lego-vibe] capabilities: {caps}")
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
        except OSError:
            # Fall through to the normal write path.  The device may have been
            # rebound under the same sysfs path since the cache was populated.
            pass
    path = os.path.join(sys_path, rel_path)
    try:
        with open(path, 'w') as f:
            f.write(value + '\n')
        _attr_cache[key] = value
        decky.logger.info(f"[lego-vibe] {rel_path} = '{value}'")
        return True
    except OSError as exc:
        _attr_cache.pop(key, None)
        decky.logger.error(f"[lego-vibe] write {path}: {exc}")
        return False


async def _offload(fn, *args):
    """Run blocking work off the event loop.

    Sysfs writes, settings I/O and waiting on _apply_lock all block. Decky gives
    each plugin its own process and loop, so blocking here does not stall other
    plugins - it stalls this one: every RPC the panel sends queues behind it, and
    the hotplug monitor stops draining its netlink queue until it returns.
    _apply_lock alone can be held by that worker for as long as five sysfs
    writes take.
    """
    def run():
        with _settings_lock:
            return fn(*args)
    return await module_runtime.offload('vibration', run)


def _apply_settings(values: dict, sys_path: str | None = None,
                    force: bool = False) -> bool:
    """Write a full profile to the hardware. Every attribute is attempted."""
    # Explicit withdrawal/uninstall can reach this with driver defaults rather
    # than a loaded profile. They must also preserve unrecoverable user state.
    with _settings_lock:
        _read_valid_settings()
    with _apply_lock:
        p = sys_path or _get_device_path()
        if p is None:
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
        return all(results)


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
    """A private copy of the profile store. Callers coerce and mutate what they
    get back, and getSetting hands out a live reference into the manager's own
    dict - so without the copy those edits would land in the store uncommitted,
    and a later read() would silently drop them again."""
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
        if len(profiles) >= MAX_GAME_PROFILES:
            break
    if DEFAULT_APP not in profiles:
        profiles[DEFAULT_APP] = {"overwrite": False, "settings": dict(DEFAULT_PROFILE)}
    return profiles


def _save_profiles(profiles: dict) -> None:
    with _settings_lock:
        _read_valid_settings()
        settings.setSetting(SETTINGS_KEY_GAME_PROFILES, profiles)
        settings.commit()


def _resolve_app_id(profiles: dict, app_id: str | None = None) -> str:
    """A game's profile only wins while it is both running and flagged."""
    app_id = _active_app_id if app_id is None else app_id
    if app_id != DEFAULT_APP and profiles.get(app_id, {}).get("overwrite"):
        return app_id
    return DEFAULT_APP


def _active_values(profiles: dict | None = None) -> dict:
    profiles = _load_profiles() if profiles is None else profiles
    entry = profiles.get(_resolve_app_id(profiles), {})
    return _coerce_profile(entry.get("settings"))


def _migrate() -> None:
    """Fold the old flat settings keys into profile '0' exactly once."""
    with _settings_lock:
        _read_valid_settings()
        # A hand-edited or truncated store can hold anything here, and this runs
        # from _migration() - where a raise kills the plugin before its RPC
        # socket exists. Same guard as LeGoTDP's copy.
        try:
            schema = int(settings.getSetting(SETTINGS_KEY_SCHEMA, 1))
        except (TypeError, ValueError):
            schema = 1
        if schema >= CURRENT_SCHEMA:
            return
        profiles = settings.getSetting(SETTINGS_KEY_GAME_PROFILES, {}) or {}
        if not isinstance(profiles, dict):
            profiles = {}
        if DEFAULT_APP not in profiles:
            legacy = {
                field: settings.getSetting(old_key, DEFAULT_PROFILE[field])
                for field, old_key in _LEGACY_KEYS.items()
            }
            profiles[DEFAULT_APP] = {"overwrite": False,
                                     "settings": _coerce_profile(legacy)}
            decky.logger.info(
                f"[lego-vibe] migrated legacy settings into the global profile: {legacy}")
        settings.setSetting(SETTINGS_KEY_GAME_PROFILES, profiles)
        settings.setSetting(SETTINGS_KEY_SCHEMA, CURRENT_SCHEMA)
        settings.commit()


def _settings_payload() -> dict:
    """What get_settings() reports: the resolved profile plus which one it is."""
    profiles = _load_profiles()
    app_id = _resolve_app_id(profiles)
    return {
        "settings":   _active_values(profiles),
        "app_id":     _active_app_id,
        "profile_id": app_id,
        "overwrite":  app_id != DEFAULT_APP,
    }


def _device_status() -> dict:
    p = _get_device_path()
    ids = _device_ids(p) if p else None
    decky.logger.debug(
        f"[lego-vibe] driver status -> path={p!r} "
        f"pyudev={_PYUDEV} method={_discovery_method!r}"
    )
    return {
        "found":  p is not None,
        "paths":  [p] if p else [],
        "method": _discovery_method or "",
        "ids":    f"{ids[0]}:{ids[1]}" if ids else "",
    }


async def _emit_device_status() -> None:
    """Push the driver status to the panel.

    A controller is plugged, unplugged or re-bound with the Quick Access Menu
    shut far more often than with it open. Before this the panel kept showing
    whatever it happened to see the last time it was opened, so the status dot
    could sit on red with the device working perfectly.

    Only the status goes out. Hotplug re-applies the stored profile without
    changing it, so a settings payload would be telling the panel what it
    already knows - and adopting one bumps the panel's edit counter, which can
    make it discard the reply to an edit the user made in the meantime and
    leave a slider showing a value the hardware no longer has.
    """
    try:
        await decky.emit("device", await _offload(_device_status))
    except Exception as exc:
        decky.logger.warning(f"[lego-vibe] could not emit device status: {exc}")


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


def _update_active(field: str, value) -> dict:
    """Write one field into whichever profile is currently in effect."""
    with _settings_lock:
        profiles = _load_profiles()
        app_id = _resolve_app_id(profiles)
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
    except OSError as exc:
        decky.logger.debug(f"[lego-vibe] FF probe {node}: {exc}")
        return False


def _find_ff_device() -> str | None:
    """
    Return the evdev node of the Legion controller, matched by the same
    vendor:product as the discovered HID device. Falls back to any
    rumble-capable node so the test button still does something on
    hardware we failed to identify.
    """
    cached = _ff_device_cache.get("node")
    if cached and os.path.exists(cached):
        return cached

    sys_path = _get_device_path()
    want = _device_ids(sys_path) if sys_path else None

    fallback = None
    for node in _event_nodes():
        if not _has_rumble(node):
            continue
        if want and _node_ids(node) == want:
            decky.logger.info(f"[lego-vibe] FF device: {node} (matched {want[0]}:{want[1]})")
            _ff_device_cache["node"] = node
            return node
        if fallback is None:
            fallback = node

    if fallback:
        decky.logger.warning(f"[lego-vibe] FF device: {fallback} (no VID:PID match, using first rumble-capable node)")
        _ff_device_cache["node"] = fallback
    return fallback


# ── Hotplug monitor (only when pyudev is available) ────────────────────────────

# 'add' fires before the driver has probed, so rumble_intensity usually does
# not exist yet. 'bind' is the event that means the driver is attached.
_HOTPLUG_ACTIONS = ("add", "bind", "change")


async def _monitor_hotplug() -> None:
    if not _PYUDEV:
        decky.logger.info("[lego-vibe] pyudev unavailable, hotplug monitor disabled")
        return
    global _device_path, _discovery_method

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
            if action in _HOTPLUG_ACTIONS:
                await _handle_device_added(sys_path, action)
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


async def _handle_device_added(sys_path: str, action: str) -> None:
    global _device_path, _discovery_method
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
    decky.logger.info(f"[lego-vibe] device available via '{action}': {sys_path}")
    _device_path = sys_path
    _discovery_method = f"udev-{action}"
    _ff_device_cache["node"] = None

    def _apply() -> bool:
        _invalidate_cache(sys_path)
        _capabilities_cache.pop(sys_path, None)
        return _apply_settings(_active_values(), sys_path=sys_path, force=True)

    ok = await _offload(_apply)
    decky.logger.info(f"[lego-vibe] re-applied profile after '{action}': success={ok}")
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
                if not ok:
                    decky.logger.warning(
                        "[lego-vibe] periodic driver-state verification failed")
                last_check = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                decky.logger.warning(
                    f"[lego-vibe] drift verification failed: {exc}")

    async def _migration(self):
        """Fold the pre-1.5.0 flat keys into profile '0', before anything reads
        the store.

        This is the loader's own hook for the job: it runs to completion before
        _main() is even scheduled, so no profile read can race the migration.

        decky.migrate_settings() does not fit here - it relocates files as they
        are, whereas this rewrites keys inside a file that is already in the
        right place.

        Nothing may escape. The loader runs this with run_until_complete inside
        a bare except that logs and sys.exit(0)s, and it never gets as far as
        creating the RPC socket - so a raise here would leave the panel retrying
        an is_ready() that has nobody to answer it, spinning on "Initializing"
        forever. Recording the failure instead tells the user their old settings
        did not come across, before they start rebuilding them on top.
        """
        try:
            await _offload(_migrate)
        except Exception as exc:
            Plugin._setup_error = f"settings migration failed: {exc}"
            decky.logger.error(f"[lego-vibe] migration failed: {exc}")

    async def _main(self):
        global _monitor_task, _drift_task, _last_suspend_offset
        decky.logger.info(
            f"[lego-vibe] startup  v{updater.plugin_version()}  pyudev={_PYUDEV}")
        try:
            def _start() -> None:
                global _last_suspend_offset
                _last_suspend_offset = _suspend_offset()
                values = _active_values()
                decky.logger.info(f"[lego-vibe] applying global profile: {values}")
                _apply_settings(values, force=True)
                # Resolve the trust store now so the log shows up front whether
                # update checks will be able to verify certificates.
                updater.ssl_context()
            await _offload(_start)
            _monitor_task = asyncio.create_task(_monitor_hotplug())
            _drift_task = asyncio.create_task(self._drift_loop())
            # Deliberately not cleared on success: _migration() runs first and
            # may have recorded a failure here, and that is precisely what the
            # panel needs to show. The attribute already starts out None.
        except Exception as exc:
            Plugin._setup_error = str(exc)
            decky.logger.error(f"[lego-vibe] setup failed: {exc}")

    async def _unload(self):
        global _monitor_task, _drift_task
        tasks = [task for task in (_monitor_task, _drift_task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            # Bounded on purpose. A hotplug apply runs in a worker thread, and
            # cancelling the task does not stop it - awaiting unbounded can
            # outlast the five seconds the loader waits before sending SIGKILL,
            # and a SIGKILL means _uninstall() never runs. That is how LeGoTDP
            # left the platform profile pinned after an uninstall.
            await asyncio.wait(tasks, timeout=1.0)
        _monitor_task = None
        _drift_task = None
        decky.logger.info("[lego-vibe] unloaded")

    async def _uninstall(self):
        """Put the controller back on the driver defaults.

        The sysfs values live in the driver, not in the plugin, so uninstalling
        while intensity is 'off' would otherwise leave a silent controller and
        nothing installed to turn it back on.

        Runs after _unload(), which cancels the hotplug monitor - though not any
        apply already running in a worker thread. _apply_settings serialises on
        _apply_lock, so the two cannot interleave and leave the hardware holding
        half of each profile.
        """
        await _offload(_apply_settings, dict(DEFAULT_PROFILE), None, True)
        decky.logger.info("[lego-vibe] uninstalled, restored default vibration settings")

    # ---- RPC surface ------------------------------------------------ #

    async def is_ready(self) -> dict:
        return {"ready": Plugin._setup_error is None, "error": Plugin._setup_error or ""}

    async def get_version(self) -> dict:
        return {"version": updater.plugin_version()}

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
            global _active_app_id
            changed = app_id != _active_app_id
            if not changed and getattr(self, "_active_applied", False):
                return {"success": True, "changed": False, **_settings_payload()}
            _active_app_id = app_id
            values = _active_values()
            decky.logger.info(f"[lego-vibe] active app -> {app_id}, applying {values}")
            ok = _apply_settings(values)
            self._active_applied = ok
            return {"success": ok, "changed": changed, **_settings_payload()}
        return await _offload(_do)

    async def _set_field(self, field: str, value, expected_app_id=None, expected_profile_id=None) -> dict:
        def _do() -> dict:
            payload = _settings_payload()
            if ((expected_app_id is not None and expected_app_id != payload["app_id"])
                    or (expected_profile_id is not None and expected_profile_id != payload["profile_id"])):
                return {"success": False, "error": "Active profile changed; reload and try again", **payload}
            values = _update_active(field, value)
            return {"success": _apply_settings(values), "settings": values}
        return await _offload(_do)

    async def set_intensity(self, level: int, expected_app_id=None, expected_profile_id=None) -> dict:
        if type(level) is not int:
            return {"success": False, "error": "level must be an integer"}
        return await self._set_field(PKEY_LEVEL, max(0, min(3, int(level))), expected_app_id, expected_profile_id)

    async def set_rumble_mode(self, mode_idx: int, expected_app_id=None, expected_profile_id=None) -> dict:
        """
        Set the vibration pattern. The controller firmware demonstrates the
        new mode by itself, so the plugin deliberately does not play one -
        doing so put a second, unsynchronised rumble on top of the firmware's
        and made the same mode feel different every time.
        """
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

    async def reset_to_default(self) -> dict:
        def _do() -> dict:
            profiles = _load_profiles()
            app_id = _resolve_app_id(profiles)
            entry = profiles.setdefault(app_id, {"overwrite": app_id != DEFAULT_APP,
                                                 "settings": {}})
            entry["settings"] = dict(DEFAULT_PROFILE)
            _save_profiles(profiles)
            ok = _apply_settings(entry["settings"], force=True)
            return {"success": ok, "settings": entry["settings"]}
        return await _offload(_do)

    async def reapply(self) -> dict:
        """
        Force every attribute back onto the hardware. Used after resume from
        suspend, where the controller comes back at its firmware defaults but
        the write cache still believes our values are in place.

        Driven from the frontend, off Steam's own resume notification: Decky has
        no backend resume hook, the loader only ever invokes _migration, _main,
        _unload and _uninstall.

        Retried, because that notification arrives before USB has finished
        coming back. Measured on the device: resume at 20:04:19, the controller
        re-appeared at 20:04:23 under a new sysfs path. A single attempt at t+0
        found no device and reported a failure that was really "not back yet" -
        and if the controller had not re-enumerated at all, the hotplug monitor
        would have had nothing to catch either.
        """
        deadline = time.monotonic() + _REAPPLY_WAIT_S

        def _do() -> dict:
            _forget_device()
            values = _active_values()
            return {"success": _apply_settings(values, force=True), "settings": values}

        while True:
            result = await _offload(_do)
            if result["success"] or time.monotonic() >= deadline:
                decky.logger.info(
                    f"[lego-vibe] reapply: success={result['success']} "
                    f"values={result['settings']}")
                return result
            await asyncio.sleep(_REAPPLY_STEP_S)

    # ---- Per-game profiles ------------------------------------------ #

    async def get_game_profiles(self) -> dict:
        return await _offload(_load_profiles)

    async def set_profile_overwrite(self, app_id: str, enabled: bool,
                                    name: str = "") -> dict:
        """Turn a per-game profile on or off and apply the result."""
        app_id = _normalise_app_id(app_id, allow_default=False)
        if app_id is None or type(enabled) is not bool:
            return {"success": False, "error": "invalid profile request"}
        name = name[:MAX_NAME_LENGTH] if isinstance(name, str) else ""

        def _do() -> dict:
            profiles = _load_profiles()
            entry = profiles.get(app_id)
            if entry is None:
                # Seed a new per-game profile from whatever is active right now.
                entry = {"overwrite": enabled, "settings": _active_values(profiles)}
                profiles[app_id] = entry
            else:
                entry["overwrite"] = enabled
                entry["settings"] = _coerce_profile(entry.get("settings"))
            # Remember the title so the profile list is readable when the game
            # is not running and Steam cannot resolve the id for us.
            if name:
                entry["name"] = name
            _save_profiles(profiles)
            values = _active_values(profiles)
            ok = _apply_settings(values)
            decky.logger.info(
                f"[lego-vibe] per-game profile {app_id} overwrite={enabled}, applied {values}")
            return {"success": ok, "settings": values, "overwrite": enabled}
        return await _offload(_do)

    async def delete_game_profile(self, app_id: str) -> dict:
        app_id = _normalise_app_id(app_id, allow_default=False)
        if app_id is None:
            return {"success": False, "error": "invalid app id"}

        def _do() -> dict:
            profiles = _load_profiles()
            if profiles.pop(app_id, None) is None:
                return {"success": False, "error": "No such profile"}
            _save_profiles(profiles)
            values = _active_values(profiles)
            ok = _apply_settings(values)
            decky.logger.info(f"[lego-vibe] deleted profile {app_id}")
            return {"success": ok, "settings": values}
        return await _offload(_do)

    async def set_game_profiles(self, profiles: dict) -> dict:
        """Bulk replace. Retained for compatibility with older frontends."""
        if not isinstance(profiles, dict):
            return {"success": False, "error": "profiles must be an object"}
        # Round-trip through the same sanitizer as ordinary reads before this
        # compatibility path is allowed to replace the complete store.
        normalised: dict[str, dict] = {}
        for raw_app_id, raw_entry in profiles.items():
            app_id = _normalise_app_id(raw_app_id)
            if app_id is None or not isinstance(raw_entry, dict):
                continue
            normalised[app_id] = {
                "overwrite": False if app_id == DEFAULT_APP else
                    _coerce_bool(raw_entry.get("overwrite"), False),
                "settings": _coerce_profile(raw_entry.get("settings")),
            }
            if isinstance(raw_entry.get("name"), str):
                normalised[app_id]["name"] = raw_entry["name"][:MAX_NAME_LENGTH]
            if len(normalised) >= MAX_GAME_PROFILES:
                break
        if DEFAULT_APP not in normalised:
            normalised[DEFAULT_APP] = {
                "overwrite": False, "settings": dict(DEFAULT_PROFILE)}
        await _offload(_save_profiles, normalised)
        return {"success": True}

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

            # Probing every /dev/input/event* with an ioctl is the slow part; the
            # effect upload and the two writes below are microseconds.
            ff_path = await _offload(_find_ff_device)
            if ff_path is None:
                decky.logger.warning("[lego-vibe] test_vibration: no FF device found")
                return {"success": False, "error": "No rumble-capable input device found"}

            fd = os.open(ff_path, os.O_RDWR)
            try:
                effect_buf = bytearray(struct.pack(
                    '<HhHHHHHxxHH28x',
                    _FF_RUMBLE, -1, 0,
                    0, 0,
                    duration, 0,
                    magnitude, magnitude,
                ))
                fcntl.ioctl(fd, _EVIOCSFF, effect_buf)
                effect_id = struct.unpack_from('<h', effect_buf, 2)[0]
                if effect_id < 0:
                    return {"success": False, "error": f"Driver rejected FF effect (id={effect_id})"}

                def _input_event(ev_type: int, code: int, value: int) -> bytes:
                    t = time.time()
                    return struct.pack('<qqHHi', int(t), int((t % 1) * 1e6) % 1_000_000,
                                       ev_type, code, value)

                os.write(fd, _input_event(_EV_FF, effect_id, 1))
                await asyncio.sleep(duration / 1000.0)
                os.write(fd, _input_event(_EV_FF, effect_id, 0))
                # EVIOCRMFF is declared _IOW(..., int) but the kernel reads the
                # effect id straight out of the argument value
                # (input_ff_erase(dev, (int)(unsigned long) p, file)). Passing a
                # packed buffer makes it erase whatever id the pointer happens
                # to look like, which always failed with EINVAL and leaked the
                # effect slot. Pass the id by value.
                fcntl.ioctl(fd, _EVIOCRMFF, effect_id)

                decky.logger.info(
                    f"[lego-vibe] test_vibration: level={level} ({intensity_pct}%) "
                    f"mag={magnitude:#06x} duration={duration}ms via {ff_path}"
                )
                return {"success": True}
            finally:
                os.close(fd)
        except Exception as exc:
            _ff_device_cache["node"] = None
            decky.logger.error(f"[lego-vibe] test_vibration failed: {exc}")
            return {"success": False, "error": str(exc)}
        finally:
            _ff_busy = False

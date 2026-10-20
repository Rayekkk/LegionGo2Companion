# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/LeGoTDP

import decky
from diagnostics import event, transition
from system_process import system_env
import module_runtime
from active_app import normalise_app_id, resolve_running_app, scan_proc_for_appid
import asyncio
import copy
import functools
import glob
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import threading
from safe_settings import CorruptSettings, SettingsManager

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

# Not `updater`: the loader aliases its own decky_loader.updater to that bare
# name before we are imported, and sys.modules wins over sys.path.
from tdp_updater import Updater  # noqa: E402 - needs the sys.path line above

BIN_DIR       = os.path.join(PLUGIN_DIR, "bin")
BIN_PATH      = os.path.join(BIN_DIR, "ryzenadj")
RYZENADJ_URL  = (
    "https://github.com/FlyGoat/RyzenAdj/releases/download/v0.19.0/"
    "ryzenadj-manylinux_2_28-x86_64.tar.gz"
)
RYZENADJ_SHA256 = "d04547f111c6af3e40d3f210468adb884561618ddade0b640d90e50c88d03444"
RYZENADJ_BINARY_SHA256 = "18a61170efec95d2366355b9dd5c75a961a9e8008d42e3471f4f414a6faec471"

# The pinned RyzenAdj download uses TLS verification and a host allowlist.
updater = Updater(user_agent="LeGoTDP")

# Reported by get_caps() when the firmware does not answer. The frontend carries
# the same numbers as FALLBACK_STD for the moments before get_caps() returns.
FALLBACK_STD_W = {"spl": 35, "sppt": 37, "fppt": 45}

# Absolute floor/ceiling for any single limit, in milliwatts. Applied on load, which
# also migrates profiles saved back when the Extras ceiling was 60 W.
HARD_MIN_MW = 5000
HARD_MAX_MW = 50000

# Preset ladder in watts.
PRESETS_DEFAULT = {
    "silent":      {"spl": 8,  "sppt": 15, "fppt": 20},
    "balanced":    {"spl": 16, "sppt": 25, "fppt": 30},
    "performance": {"spl": 20, "sppt": 32, "fppt": 35},
    "max":         {"spl": 35, "sppt": 37, "fppt": 45},
}

# Lenovo firmware attributes. Writing these goes through the EC instead of poking the
# SMU directly, so the firmware stops fighting us and the values survive suspend.
WMI_ROOT  = "/sys/class/firmware-attributes/lenovo-wmi-other-0/attributes"
WMI_ATTRS = {"spl": "ppt_pl1_spl", "sppt": "ppt_pl2_sppt", "fppt": "ppt_pl3_fppt"}
PLATFORM_PROFILE_GLOB = "/sys/class/platform-profile/*/profile"

# Package energy counter, used instead of spawning `ryzenadj --info` every 2 s.
RAPL_GLOB = "/sys/class/powercap/intel-rapl:*"

# Linux CPUFreq controls. Keeping the root configurable lets the root-free
# suite exercise the exact discovery and transactional write paths against a
# temporary sysfs-shaped directory tree.
CPU_SYS_ROOT = "/sys/devices/system/cpu"
EPP_MIN = 0
EPP_MAX = 255
_EPP_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]{0,2})$")
_EPP_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_EPP_NAMED_VALUES = {
    "performance": 0,
    "balance_performance": 128,
    "balance_power": 191,
    "power": 255,
}

_ryzenadj_lock = threading.Lock()
# Serialises a complete logical mutation: hardware apply, persistent settings
# and the target defended by the background loop. _apply_lock alone is too
# narrow because it is released before callers record what was applied.
_mutation_lock = threading.RLock()
# Serialises every hardware apply. The ryzenadj path has its own lock, but the WMI
# path (profile bounce + three ppt writes) is not atomic, so concurrent applies from
# the enforce loop and a user action could interleave and corrupt each other.
_apply_lock = threading.Lock()

# Runtime capability, not merely device capability. RyzenAdj is downloaded only
# when the user asks for Extras, and the Extras range is not advertised until
# the executable actually exists and passed its integrity check.
_ryzenadj_available: bool = False

# Cache of last successful --info parse - keeps UI responsive when lock is held
_info_cache: dict = {}
_info_cache_ts: float = 0.0
_info_cache_lock = threading.Lock()

_ROW_RE = re.compile(r"\|\s*(.+?)\s*\|\s*([\d.]+)\s*\|")

_current_game_id: str = ""
_current_ac_online: bool = False
_last_cpu_power_check: float = 0.0
CPU_POWER_DRIFT_CHECK_S = 60.0
_resume_detected = module_runtime.ResumeWatch()
MAX_GAME_PROFILES = 512
MAX_LABEL_LENGTH = 96

# The panel re-announces itself every 30 s; the TTL makes that a lease, so a frontend
# gone without cleanup (Steam UI restart) stops the info polling. _frontend_appid likewise.
_panel_active: bool = False
_panel_active_ts: float = 0.0
_PANEL_ACTIVE_TTL_S = 90.0

# The frontend detects the running game via Steam's Router, which is authoritative;
# the /proc/*/environ scan misses games sandboxed by pressure-vessel/gamescope. When
# the panel is open the frontend pushes the appid here; we trust it while it is fresh
# and fall back to the proc scan once it goes stale (panel closed).
_frontend_appid: str = ""
_frontend_appid_ts: float = 0.0
_FRONTEND_APPID_TTL = 12.0


# ── Device identity ────────────────────────────────────────────────────────────

# Read once: DMI does not change while the machine is running. Nothing is
# written to power controls on any other model.
_legion_go_2 = functools.cache(module_runtime.is_legion_go_2)
UNSUPPORTED_MODEL = "TDP control is available only on the Lenovo Legion Go 2."


def _ok(**extra) -> dict:
    return {"success": True, "stdout": "", "stderr": "", "returncode": 0, **extra}


def _fail(stderr: str, **extra) -> dict:
    return {"success": False, "stdout": "", "stderr": stderr, "returncode": -1, **extra}


def _defaults() -> dict:
    """Where a fresh install starts, in milliwatts."""
    balanced = PRESETS_DEFAULT["balanced"]
    return {"spl":  balanced["spl"]  * 1000,
            "sppt": balanced["sppt"] * 1000,
            "fppt": balanced["fppt"] * 1000,
            "enabled": True,
            "cpu_boost_enabled": None,
            "epp": None}


def _standard_ceilings_mw() -> tuple[int, int, int]:
    """Firmware ceilings in milliwatts, with the documented safe fallback."""
    caps = _wmi_caps()
    if caps:
        return tuple(caps[k]["max"] * 1000 for k in ("spl", "sppt", "fppt"))
    return tuple(FALLBACK_STD_W[k] * 1000 for k in ("spl", "sppt", "fppt"))


def _allowed_ceilings_mw(state: dict) -> tuple[int, int, int]:
    """Ceilings the current persisted mode is allowed to apply."""
    if state.get("extras_unlocked", False) and _ryzenadj_available:
        # The SMU keeps sustained power (STAPM) at the firmware's SPL ceiling; only
        # the boost limits go higher (Legion Go 2: STAPM stays 35 W, slow/fast take 50).
        return _standard_ceilings_mw()[0], HARD_MAX_MW, HARD_MAX_MW
    return _standard_ceilings_mw()


# Plugging the charger in makes the firmware apply its own profile after ours, so the
# values are re-asserted over the following seconds (on unplug too; it costs nothing).
AC_SETTLE_DELAYS_S = (0.5, 1.5, 3.0, 6.0)

# What the last charger transition asked for; the ladder re-asserts this, not active_*,
# because a failed apply records nothing and failure is when the ladder is needed.
_ac_target: tuple = ()
# Generation that armed the pending ladder; any newer action bumps it, voiding the old one.
_ac_generation: int = 0
# When the charger last changed; a game switch inside the ladder's span restarts it.
_ac_changed_at: float = float("-inf")


# ── AC power detection ─────────────────────────────────────────────────────────

def _read_sysfs(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _read_profile(path: str) -> str:
    """platform_profile is read through a WMI method, so it shares the firmware lock."""
    with module_runtime.firmware_lock:
        return _read_sysfs(path)


def _get_ac_online() -> bool:
    """True when an external charger is present.

    Only Mains-type supplies count. The Legion Go 2 also exposes USB-C PD source
    PSYs (ucsi-source-psy-*, type=USB, scope=Device) whose `online` flag tracks the
    port's PD role, not whether the device is being powered - ORing those in made an
    unplug flicker straight back to "charging". ACAD (Mains) is the real signal, and
    BAT0 status is unreliable here because battery conservation mode reports
    "Not charging" even while on AC.
    """
    mains_seen = False
    for path in glob.glob("/sys/class/power_supply/*"):
        if _read_sysfs(os.path.join(path, "type")) != "Mains":
            continue
        mains_seen = True
        with module_runtime.firmware_lock:  # reading online runs the ACPI _PSR method
            online = _read_sysfs(os.path.join(path, "online"))
        if online == "1":
            return True
    if mains_seen:
        return False
    # No Mains supply exposed at all - fall back to battery status.
    with module_runtime.firmware_lock:  # reading status runs the ACPI _BST method
        status = _read_sysfs("/sys/class/power_supply/BAT0/status")
    return status not in ("", "Discharging", "Unknown")


def _panel_is_active() -> bool:
    """True while the panel's lease is unexpired. See _PANEL_ACTIVE_TTL_S."""
    return _panel_active and time.monotonic() - _panel_active_ts < _PANEL_ACTIVE_TTL_S


def _pick_profile_values(p: dict, ac_online: bool) -> tuple[int, int, int]:
    if ac_online and p.get("ac_separate") and p.get("ac_spl") is not None:
        return (
            p["ac_spl"],
            p.get("ac_sppt", p.get("sppt", _defaults()["sppt"])),
            p.get("ac_fppt", p.get("fppt", _defaults()["fppt"])),
        )
    return (
        p.get("spl",  _defaults()["spl"]),
        p.get("sppt", _defaults()["sppt"]),
        p.get("fppt", _defaults()["fppt"]),
    )


def _safe_label(value) -> str:
    return value[:MAX_LABEL_LENGTH] if isinstance(value, str) else ""


# ── Persistence ────────────────────────────────────────────────────────────────

# Decky's settings directory, not the plugin directory, which every reinstall wipes.
settings = SettingsManager(
    name="tdp_settings",
    settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR,
)

SETTINGS_KEY_SETTINGS      = "settings"
SETTINGS_KEY_GAME_PROFILES = "game_profiles"
# Profiles the user turned off. A separate key, so no game_profiles lookup can apply them.
SETTINGS_KEY_INACTIVE_GAME_PROFILES = "inactive_game_profiles"
SETTINGS_FIELD_CPU_BOOST_ENABLED = "cpu_boost_enabled"
SETTINGS_FIELD_EPP = "epp"
# What release puts back: {"pending": True[, "after_boot": id]} until captured,
# then the system's own platform profile / CPU Boost / EPP (None = unknown).
SETTINGS_KEY_BASELINE = "baseline"
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

# The enforce loop reads settings from an executor thread while RPC handlers
# write them from the event loop. Re-entrant because the write paths load first.
_settings_lock = threading.RLock()


# Settings I/O, sysfs and the apply locks block; keep them off the plugin's loop.
_offload = functools.partial(module_runtime.offload, 'tdp')


def _read_valid_settings() -> None:
    settings.read()
    error = getattr(settings, "recovery_error", "")
    if error:
        raise CorruptSettings("TDP settings recovery failed; saved power controls were left untouched. " + error)


def _read_key(key: str, default: dict) -> dict:
    """A private copy of one key. Callers clamp and mutate what they get back,
    and getSetting hands out a live reference into the manager's own dict - so
    without the copy those edits would land in the store uncommitted, and a
    later read() would silently drop them again."""
    with _settings_lock:
        _read_valid_settings()
        value = settings.getSetting(key, None)
        return copy.deepcopy(value) if isinstance(value, dict) else dict(default)


def _write_keys(values: dict[str, dict]) -> None:
    with _settings_lock:
        # Refresh first so a caller cannot commit a stale in-memory copy of an
        # unrelated key. All values in this transaction reach one JSON commit.
        _read_valid_settings()
        for key, value in values.items():
            settings.setSetting(key, value)
        settings.commit()


def _write_key(key: str, value: dict) -> None:
    _write_keys({key: value})


def _clamp_triplet(spl, sppt, fppt,
                   ceilings: tuple[int, int, int] | None = None) -> tuple[int, int, int]:
    """Enforce 5 W <= spl <= sppt <= fppt <= 50 W (milliwatts).

    SPPT/FPPT are offsets above SPL in the UI, so they can never sit below it.
    """
    try:
        spl, sppt, fppt = int(spl), int(sppt), int(fppt)
    except (TypeError, ValueError):
        return _defaults()["spl"], _defaults()["sppt"], _defaults()["fppt"]
    spl_max, sppt_max, fppt_max = ceilings or (HARD_MAX_MW, HARD_MAX_MW, HARD_MAX_MW)
    spl  = max(HARD_MIN_MW, min(spl,  spl_max))
    fppt = max(spl,         min(fppt, fppt_max))
    sppt = max(spl,         min(sppt, min(sppt_max, fppt)))
    return spl, sppt, fppt


def _load_ceilings_mw() -> tuple[int, int, int]:
    """Bounds for saved values: no mode runs SPL above the firmware ceiling, so a
    higher SPL saved by 2.2.0 or earlier reads as what actually runs."""
    # Loads are frequent and every firmware read is a WMI call, so this takes the SPL max
    # the last firmware read reported (35 W until one has); applying clamps to the live one.
    return _firmware_spl_max_mw or FALLBACK_STD_W["spl"] * 1000, HARD_MAX_MW, HARD_MAX_MW


def _clamp_for_settings(state: dict, spl, sppt, fppt) -> tuple[int, int, int]:
    """Clamp a target to the range currently unlocked and actually available."""
    return _clamp_triplet(spl, sppt, fppt, _allowed_ceilings_mw(state))


def _is_custom_preset(spl, sppt, fppt, preset: str) -> bool:
    if preset == "custom":
        return True
    try:
        values = int(spl), int(sppt), int(fppt)
    except (TypeError, ValueError):
        values = ()
    return not any(
        values == tuple(entry[key] * 1000 for key in ("spl", "sppt", "fppt"))
        for entry in PRESETS_DEFAULT.values())


def _target_for_preset(state: dict, spl, sppt, fppt, preset: str) -> tuple[int, int, int]:
    """In simplified Custom mode, derive burst limits from the single TDP dial."""
    if state.get("advanced_tdp_control", True) or not _is_custom_preset(
            spl, sppt, fppt, preset):
        return _clamp_for_settings(state, spl, sppt, fppt)
    ceilings = _allowed_ceilings_mw(state)
    if not state.get("extras_unlocked", False):
        ceilings = tuple(min(limit, FALLBACK_STD_W[key] * 1000)
                         for limit, key in zip(ceilings, ("spl", "sppt", "fppt")))
    try:
        requested_spl = int(spl)
    except (TypeError, ValueError):
        requested_spl = _defaults()["spl"]
    spl = max(HARD_MIN_MW, min(requested_spl, ceilings[0]))
    return _clamp_triplet(spl, spl + 10000, spl + 15000, ceilings)


def _profile_target(state: dict, profile: dict, ac_online: bool) -> tuple[int, int, int]:
    preset_key = "ac_preset" if ac_online and profile.get("ac_separate") else "preset"
    return _target_for_preset(state, *_pick_profile_values(profile, ac_online),
                              profile.get(preset_key, ""))


def _canonical_epp_syntax(value) -> str | None:
    """Validate persisted/RPC syntax before checking live capabilities."""
    if not isinstance(value, str) or value == "custom":
        return None
    if _EPP_DECIMAL_RE.fullmatch(value):
        number = int(value, 10)
        return value if EPP_MIN <= number <= EPP_MAX else None
    return value if _EPP_NAME_RE.fullmatch(value) else None


def _load_settings() -> dict:
    s = _read_key(SETTINGS_KEY_SETTINGS, _defaults())
    s["enabled"] = (
        s.get("enabled") if type(s.get("enabled")) is bool else True)
    s["extras_unlocked"] = (
        s.get("extras_unlocked")
        if type(s.get("extras_unlocked")) is bool else False)
    s["advanced_tdp_control"] = (
        s.get("advanced_tdp_control")
        if type(s.get("advanced_tdp_control")) is bool else True)
    boost = s.get(SETTINGS_FIELD_CPU_BOOST_ENABLED)
    s[SETTINGS_FIELD_CPU_BOOST_ENABLED] = (
        boost if type(boost) is bool else None)
    s[SETTINGS_FIELD_EPP] = _canonical_epp_syntax(
        s.get(SETTINGS_FIELD_EPP))
    ceilings = _load_ceilings_mw()
    s["spl"], s["sppt"], s["fppt"] = _clamp_triplet(
        s.get("spl",  _defaults()["spl"]),
        s.get("sppt", _defaults()["sppt"]),
        s.get("fppt", _defaults()["fppt"]),
        ceilings,
    )
    if any(k in s for k in ("active_spl", "active_sppt", "active_fppt")):
        s["active_spl"], s["active_sppt"], s["active_fppt"] = _clamp_triplet(
            s.get("active_spl",  s["spl"]),
            s.get("active_sppt", s["sppt"]),
            s.get("active_fppt", s["fppt"]),
            ceilings,
        )
    s["active_preset"] = _safe_label(s.get("active_preset"))
    return s


def _save_settings(s: dict) -> None:
    _write_key(SETTINGS_KEY_SETTINGS, s)


# ── Per-game profiles ──────────────────────────────────────────────────────────

def _load_profiles() -> dict:
    return _normalised_profiles(_read_key(SETTINGS_KEY_GAME_PROFILES, {}))


def _load_inactive_profiles() -> dict:
    return _normalised_profiles(_read_key(SETTINGS_KEY_INACTIVE_GAME_PROFILES, {}))


def _normalised_profiles(raw_profiles: dict) -> dict:
    profiles: dict[str, dict] = {}
    ceilings = _load_ceilings_mw()
    for raw_app_id, raw_profile in raw_profiles.items():
        app_id = normalise_app_id(raw_app_id)
        if app_id is None or not isinstance(raw_profile, dict):
            continue
        p = dict(raw_profile)
        p["ac_separate"] = (
            p.get("ac_separate") if type(p.get("ac_separate")) is bool else False)
        p["preset"] = _safe_label(p.get("preset"))
        p["ac_preset"] = _safe_label(p.get("ac_preset"))
        for prefix in ("", "ac_"):
            boost_key, epp_key = prefix + SETTINGS_FIELD_CPU_BOOST_ENABLED, prefix + SETTINGS_FIELD_EPP
            if boost_key in p:
                boost = p[boost_key]
                p[boost_key] = boost if type(boost) is bool else None
            if epp_key in p:
                p[epp_key] = _canonical_epp_syntax(p[epp_key])
        if p.get("spl") is not None:
            p["spl"], p["sppt"], p["fppt"] = _clamp_triplet(
                p["spl"], p.get("sppt", p["spl"]), p.get("fppt", p["spl"]), ceilings)
        if p.get("ac_spl") is not None:
            p["ac_spl"], p["ac_sppt"], p["ac_fppt"] = _clamp_triplet(
                p["ac_spl"], p.get("ac_sppt", p["ac_spl"]), p.get("ac_fppt", p["ac_spl"]),
                ceilings)
        profiles[app_id] = p
    # MAX_GAME_PROFILES limits only new profiles: a store already above it keeps every valid one.
    return profiles


def _effective_cpu_values(
    state: dict, profile: dict | None = None, ac_profile: bool = False
) -> dict:
    """Resolve legacy omissions without altering existing saved profiles.

    _load_settings() and _normalised_profiles() already normalised both fields.
    """
    values = {field: state.get(field)
              for field in (SETTINGS_FIELD_CPU_BOOST_ENABLED, SETTINGS_FIELD_EPP)}
    if profile is not None:
        for prefix in (("", "ac_") if ac_profile else ("",)):
            boost = profile.get(prefix + SETTINGS_FIELD_CPU_BOOST_ENABLED)
            if type(boost) is bool:
                values[SETTINGS_FIELD_CPU_BOOST_ENABLED] = boost
            epp = profile.get(prefix + SETTINGS_FIELD_EPP)
            if epp is not None:
                values[SETTINGS_FIELD_EPP] = epp
    return values


def _effective_cpu_state(
    state: dict, profiles: dict | None = None, app_id: str | None = None,
    ac_online: bool | None = None
) -> dict:
    """A transient snapshot; never persist the resolved game values globally."""
    if state.get("_cpu_power_scope_resolved"):
        return state
    profile = None
    if state.get("enabled", True):
        app_id = _get_running_appid() if app_id is None else app_id
        if app_id:
            profiles = _load_profiles() if profiles is None else profiles
            profile = profiles.get(app_id)
    if ac_online is None:
        ac_online = _get_ac_online() if profile and profile.get("ac_separate") else False
    ac_profile = bool(profile and profile.get("ac_separate") and ac_online)
    return {**state, **_effective_cpu_values(state, profile, ac_profile),
            "_cpu_power_scope_resolved": True}


def _prepare_cpu_profile(
    state: dict, profiles: dict, app_id: str, *, required_field: str | None = None,
    snapshot_cpu: bool = False
) -> dict:
    """Freeze a new game's CPU values before a firmware profile can reset them.

    An unmanaged global control needs a readable baseline before the first game
    override, otherwise leaving that game could not restore the user's state.
    Optional controls that cannot be read do not prevent ordinary TDP profiles.
    """
    existing = profiles.get(app_id)
    creating = existing is None
    if creating and len(profiles) + len(_load_inactive_profiles()) >= MAX_GAME_PROFILES:
        raise ValueError("maximum number of game profiles reached")
    for field in (SETTINGS_FIELD_CPU_BOOST_ENABLED, SETTINGS_FIELD_EPP):
        if state.get(field) is not None or (
                not creating and not snapshot_cpu and field != required_field):
            continue
        # A malformed/manual profile may override an unmanaged global value.
        # Its current hardware is not evidence of the original global baseline.
        overrides = existing and any(
            _effective_cpu_values({}, existing, ac)[field] is not None
            for ac in (False, True))
        baseline = None if overrides else _read_cpu_baseline(field)
        if baseline is not None:
            state[field] = baseline
        elif field == required_field:
            raise ValueError("cannot safely read the global CPU baseline; game setting was not saved")
    if creating:
        existing = dict(zip(("spl", "sppt", "fppt"), _global_triplet(state)))
        existing["preset"] = _safe_label(state.get("active_preset"))
        existing.update(_effective_cpu_values(state))
        profiles[app_id] = existing
    return existing


def _read_cpu_baseline(field: str):
    try:
        capture = (_capture_cpu_boost() if field == SETTINGS_FIELD_CPU_BOOST_ENABLED
                   else _capture_epp())
        value = capture.get("enabled" if field == SETTINGS_FIELD_CPU_BOOST_ENABLED else "value")
        valid = (type(value) is bool if field == SETTINGS_FIELD_CPU_BOOST_ENABLED
                 else _canonical_epp_syntax(value) is not None)
        return value if capture.get("can_set") and valid else None
    except Exception:
        # Missing optional CPU controls must not disable normal TDP support.
        return None


def _save_active(s: dict, spl: int, sppt: int, fppt: int) -> None:
    with _mutation_lock:
        s["active_spl"]  = spl
        s["active_sppt"] = sppt
        s["active_fppt"] = fppt
        _save_settings(s)


def _migrate() -> None:
    """Mark the baseline as pending, once."""
    with _settings_lock:
        _read_valid_settings()
        if settings.getSetting(SETTINGS_KEY_BASELINE, None) is None:
            # A fresh store means nothing was written yet. Otherwise the hardware
            # may hold our own values until the next boot resets the CPU controls.
            fresh = (all(settings.getSetting(key, None) is None
                         for key in (SETTINGS_KEY_SETTINGS, SETTINGS_KEY_GAME_PROFILES)))
            settings.setSetting(SETTINGS_KEY_BASELINE, {"pending": True} if fresh else
                                {"pending": True, "after_boot": _read_sysfs(BOOT_ID_PATH)})
            settings.commit()


def _capture_baseline() -> None:
    """Persist the system's power controls once, before the first write to any."""
    with _settings_lock:
        _read_valid_settings()
        marker = settings.getSetting(SETTINGS_KEY_BASELINE, None)
        if not isinstance(marker, dict) or marker.get("pending") is not True:
            return
        if "after_boot" in marker and (
                not marker["after_boot"] or _read_sysfs(BOOT_ID_PATH) in ("", marker["after_boot"])):
            return
        path = _profile_path()
        profile = _read_profile(path) if path else ""
        settings.setSetting(SETTINGS_KEY_BASELINE, {
            # 'custom' is the plugin's own mode, never a baseline.
            "platform_profile": profile if profile and profile != "custom" else None,
            SETTINGS_FIELD_CPU_BOOST_ENABLED: _read_cpu_baseline(SETTINGS_FIELD_CPU_BOOST_ENABLED),
            SETTINGS_FIELD_EPP: _read_cpu_baseline(SETTINGS_FIELD_EPP),
        })
        settings.commit()


def _baseline() -> dict:
    """The captured values, or {} when none were captured (today's defaults apply)."""
    value = _read_key(SETTINGS_KEY_BASELINE, {})
    return {} if value.get("pending") else value


# ── ryzenadj binary ────────────────────────────────────────────────────────────

def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_ryzenadj(cancel=None) -> None:
    decky.logger.info(f"[legotdp] Downloading ryzenadj from {RYZENADJ_URL}")
    os.makedirs(BIN_DIR, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz")
    os.close(tmp_fd)
    bin_tmp = ""
    try:
        with open(tmp_path, "wb") as out:
            updater.download_to(RYZENADJ_URL, out, timeout=30, cancel=cancel)

        actual = _sha256_file(tmp_path)
        if actual != RYZENADJ_SHA256:
            raise RuntimeError(
                f"ryzenadj archive checksum mismatch: got {actual}, "
                f"expected {RYZENADJ_SHA256}")

        with tarfile.open(tmp_path, "r:gz") as tar:
            member = next(
                (m for m in tar.getmembers()
                 if os.path.basename(m.name) == "ryzenadj" and m.isfile()),
                None,
            )
            if member is None:
                raise RuntimeError("ryzenadj binary not found inside tarball")
            source = tar.extractfile(member)
            if source is None:
                raise RuntimeError("cannot read ryzenadj binary from tarball")
            bin_fd, bin_tmp = tempfile.mkstemp(prefix=".ryzenadj-", dir=BIN_DIR)
            with os.fdopen(bin_fd, "wb") as out, source:
                for chunk in iter(lambda: source.read(64 * 1024), b""):
                    out.write(chunk)
        os.chmod(bin_tmp, 0o755)
        binary_digest = _sha256_file(bin_tmp)
        if binary_digest != RYZENADJ_BINARY_SHA256:
            raise RuntimeError(
                f"ryzenadj binary checksum mismatch: got {binary_digest}, "
                f"expected {RYZENADJ_BINARY_SHA256}")
        if cancel is not None and cancel():
            raise InterruptedError("RyzenAdj download was cancelled")
        os.replace(bin_tmp, BIN_PATH)
        bin_tmp = ""
        event('tdp','helper_installed',source='ryzenadj',outcome='verified')
    finally:
        for path in (tmp_path, bin_tmp):
            if not path:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass


def _ensure_ryzenadj(cancel=None, download=True) -> bool:
    if cancel is not None and cancel():
        raise InterruptedError("RyzenAdj download was cancelled")
    if not os.path.isfile(BIN_PATH) or _sha256_file(BIN_PATH) != RYZENADJ_BINARY_SHA256:
        if not download:
            return False
        if os.path.isfile(BIN_PATH):
            decky.logger.warning(
                "[legotdp] existing ryzenadj failed integrity verification; replacing it")
            os.unlink(BIN_PATH)
        _download_ryzenadj(cancel=cancel)
    mode = os.stat(BIN_PATH).st_mode
    if not (mode & stat.S_IXUSR):
        os.chmod(BIN_PATH, mode | 0o111)
    return True


# ── ryzenadj helpers ───────────────────────────────────────────────────────────

def _run_ryzenadj(args: list, timeout: float = 5.0) -> tuple[int, str, str]:
    """Run ryzenadj, return (returncode, stdout, stderr).
    Uses Popen so kill() after timeout never calls communicate() and blocks."""
    if not _ryzenadj_available or not os.path.isfile(BIN_PATH):
        return -1, "", "verified ryzenadj is unavailable"
    proc = subprocess.Popen([BIN_PATH] + args,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=system_env(LC_ALL="C"))
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            decky.logger.warning("[legotdp] ryzenadj process could not be killed")
        event('tdp','helper_timeout',level='warning',source='ryzenadj',stage='execution',count=len(args),repeat=60)
        return -1, "", "timeout"


def _parse_ryzenadj_output(text: str) -> dict:
    values: dict = {}
    for line in text.splitlines():
        m = _ROW_RE.search(line)
        if not m:
            continue
        name  = m.group(1).strip().upper()
        value = float(m.group(2))
        if "STAPM" in name and "LIMIT" in name:
            values["spl_limit"] = value
        elif "STAPM" in name and "VALUE" in name:
            values["spl_value"] = value
        elif "FAST" in name and "LIMIT" in name:
            values["fppt_limit"] = value
        elif "FAST" in name and "VALUE" in name:
            values["fppt_value"] = value
        elif "SLOW" in name and "LIMIT" in name:
            values["sppt_limit"] = value
        elif "SLOW" in name and "VALUE" in name:
            values["sppt_value"] = value
        elif "PPT" in name and "LIMIT" in name and "APU" not in name and "sppt_limit" not in values:
            values["sppt_limit"] = value
        elif "PPT" in name and "VALUE" in name and "APU" not in name and "sppt_value" not in values:
            values["sppt_value"] = value
    return values


def _apply_ryzenadj(spl_mw: int, sppt_mw: int, fppt_mw: int) -> dict:
    if not _ryzenadj_lock.acquire(timeout=4.0):
        return _fail("ryzenadj busy")
    try:
        rc, out, err = _run_ryzenadj([
            f"--stapm-limit={spl_mw}",
            f"--slow-limit={sppt_mw}",
            f"--fast-limit={fppt_mw}",
        ])
        decky.logger.info(f"[legotdp] ryzenadj apply {spl_mw//1000}W/{sppt_mw//1000}W/{fppt_mw//1000}W -> rc={rc}")
        return {"success": rc == 0, "stdout": out, "stderr": err, "returncode": rc}
    finally:
        _ryzenadj_lock.release()


# ── Lenovo WMI firmware attributes ─────────────────────────────────────────────

def _wmi_path(key: str, leaf: str) -> str:
    return os.path.join(WMI_ROOT, WMI_ATTRS[key], leaf)


def _wmi_read(key: str, leaf: str) -> int | None:
    try:
        # Reading a firmware attribute also runs a WMI method, so it shares the write lock.
        with module_runtime.firmware_lock, open(_wmi_path(key, leaf)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


# SPL max from the last firmware read that looked sane; the ceiling for loaded values.
_firmware_spl_max_mw: int | None = None


def _wmi_caps() -> dict:
    """Firmware-reported {min,max} in watts per parameter, or {} when unavailable."""
    caps = {}
    for key in WMI_ATTRS:
        lo, hi = _wmi_read(key, "min_value"), _wmi_read(key, "max_value")
        if lo is None or hi is None:
            return {}
        caps[key] = {"min": lo, "max": hi}
    global _firmware_spl_max_mw
    if HARD_MIN_MW <= caps["spl"]["max"] * 1000 <= HARD_MAX_MW:
        _firmware_spl_max_mw = caps["spl"]["max"] * 1000
    return caps


def _profile_choices(path: str) -> list[str]:
    try:
        with open(os.path.join(os.path.dirname(path), "choices")) as f:
            return f.read().split()
    except OSError:
        return []


def _profile_path() -> str | None:
    """The platform-profile node whose choices include 'custom' (the tunable one)."""
    return next((path for path in glob.glob(PLATFORM_PROFILE_GLOB)
                 if "custom" in _profile_choices(path)), None)


def _write_profile(path: str, value: str) -> bool:
    try:
        with module_runtime.firmware_lock, open(path, "w") as f:
            f.write(value)
        return True
    except OSError as exc:
        event("tdp", "attribute_write", level="warning", error=exc,
              attribute="platform_profile", outcome="failed", repeat=60)
        return False


def _write_ppt(spl_w: int, sppt_w: int, fppt_w: int, retried: bool = False) -> None:
    for key, val in (("spl", spl_w), ("sppt", sppt_w), ("fppt", fppt_w)):
        try:
            with module_runtime.firmware_lock, open(_wmi_path(key, "current_value"), "w") as f:
                f.write(str(val))
        except OSError as exc:
            # A refused in-place write is normal: the profile bounce below writes again.
            event("tdp", "attribute_write", level="info" if retried else "warning", error=exc,
                  attribute=WMI_ATTRS[key], outcome="retried" if retried else "failed", repeat=60)


def _ppt_matches(spl_w: int, sppt_w: int, fppt_w: int) -> bool:
    return all(_wmi_read(k, "current_value") == v
               for k, v in (("spl", spl_w), ("sppt", sppt_w), ("fppt", fppt_w)))


# Verified on the Legion Go 2: the firmware only latches ppt_* writes when the
# platform profile transitions *into* 'custom'. Writing while already in custom is
# silently dropped, and entering custom resets the values to firmware defaults - so
# the reliable recipe is bounce-through-another-profile, then write.
def _apply_wmi(spl_w: int, sppt_w: int, fppt_w: int) -> dict:
    path = _profile_path()
    if path is None:
        return _fail("no custom platform profile")

    # Fast path: if we can latch a write in place, skip the visible profile bounce.
    if _read_profile(path) == "custom":
        _write_ppt(spl_w, sppt_w, fppt_w, retried=True)
        if _ppt_matches(spl_w, sppt_w, fppt_w):
            decky.logger.info(f"[legotdp] wmi apply {spl_w}W/{sppt_w}W/{fppt_w}W")
            return _ok()

    # Force a real transition into custom, then write. Bounce via a low profile so the
    # momentary blip is downward, never a spike.
    choices = _profile_choices(path)
    bounce = (next((c for c in ("low-power", "balanced", "performance") if c in choices),
                   next((c for c in choices if c != "custom"), "custom"))
              if choices else "low-power")
    _write_profile(path, bounce)
    if not _write_profile(path, "custom"):
        return _fail("cannot select custom profile")
    _write_ppt(spl_w, sppt_w, fppt_w)

    if not _ppt_matches(spl_w, sppt_w, fppt_w):
        mismatch = "; ".join(
            f"{WMI_ATTRS[k]}={_wmi_read(k, 'current_value')} want {v}"
            for k, v in (("spl", spl_w), ("sppt", sppt_w), ("fppt", fppt_w))
            if _wmi_read(k, "current_value") != v)
        return _fail(mismatch)
    decky.logger.info(f"[legotdp] wmi apply {spl_w}W/{sppt_w}W/{fppt_w}W (via bounce)")
    return _ok()


# ── RAPL package power ─────────────────────────────────────────────────────────

# None = never probed, "" = not found; a miss is retried because powercap can register late.
_rapl_dir: str | None = None
_rapl_probed_at: float = 0.0
_RAPL_RESCAN_S = 60.0
_rapl_last: tuple = ()


def _find_rapl_package() -> str | None:
    global _rapl_dir, _rapl_probed_at
    if _rapl_dir:
        return _rapl_dir
    now = time.monotonic()
    if _rapl_dir is not None and now - _rapl_probed_at < _RAPL_RESCAN_S:
        return None

    _rapl_probed_at = now
    _rapl_dir = ""
    for d in sorted(glob.glob(RAPL_GLOB)):
        try:
            with open(os.path.join(d, "name")) as f:
                if f.read().strip().startswith("package"):
                    _rapl_dir = d
                    break
        except OSError:
            continue
    return _rapl_dir or None


def _rapl_watts() -> float | None:
    """Average package draw since the previous call, in watts."""
    global _rapl_last
    d = _find_rapl_package()
    if not d:
        return None
    try:
        with open(os.path.join(d, "energy_uj")) as f:
            energy = int(f.read().strip())
    except (OSError, ValueError):
        return None
    now = time.monotonic()
    prev, _rapl_last = _rapl_last, (energy, now)
    if not prev:
        return None
    delta_e, delta_t = energy - prev[0], now - prev[1]
    if delta_t <= 0:
        return None
    if delta_e < 0:  # counter wrapped
        try:
            with open(os.path.join(d, "max_energy_range_uj")) as f:
                delta_e += int(f.read().strip())
        except (OSError, ValueError):
            return None
    return delta_e / delta_t / 1_000_000


# ── Apply dispatcher ───────────────────────────────────────────────────────────

_last_source: str = ""

# The triplet the hardware was last successfully asked for, in milliwatts.
_applied_mw: tuple = ()

# When that happened, so the enforce pass can tell whether the info cache it is
# about to read predates the change it is checking.
_applied_at: float = 0.0


def _apply_limits(spl_mw: int, sppt_mw: int, fppt_mw: int) -> dict:
    """The firmware path inside the range it accepts; verified ryzenadj only
    beyond it (the Extras range) or when the firmware interface is absent."""
    global _last_source, _applied_mw, _applied_at
    if not _legion_go_2():
        return _fail(UNSUPPORTED_MODEL)
    _capture_baseline()
    spl_mw, sppt_mw, fppt_mw = _clamp_triplet(spl_mw, sppt_mw, fppt_mw)
    triple_w = (("spl", spl_mw // 1000), ("sppt", sppt_mw // 1000), ("fppt", fppt_mw // 1000))
    if not _apply_lock.acquire(timeout=8.0):
        return _fail("apply busy")
    try:
        caps = _wmi_caps()
        if caps and all(caps[k]["min"] <= v <= caps[k]["max"] for k, v in triple_w):
            result = _apply_wmi(*(v for _, v in triple_w))
            if result["success"]:
                _last_source = "wmi"
                _applied_mw, _applied_at = (spl_mw, sppt_mw, fppt_mw), time.monotonic()
                _invalidate_limits_cache()
            # No SMU fallback inside the firmware range: a refusal is reported as is.
            return result
        result = _apply_ryzenadj(spl_mw, sppt_mw, fppt_mw)
        if result["success"]:
            _last_source = "ryzenadj"
            _applied_mw, _applied_at = (spl_mw, sppt_mw, fppt_mw), time.monotonic()
            _invalidate_limits_cache()
        return result
    finally:
        _apply_lock.release()


# The WMI attributes record the driver's own writes, not the hardware, so an external
# change leaves them stale (measured: 25/30/35 still shown after a drop to 15 W).
# `ryzenadj --info` reads live values, so it cross-checks - sparingly, as it spawns a process.
_WMI_VERIFY_EVERY_S = 30.0
# None = never: time.monotonic() starts near 0 at boot, so 0.0 would skip the first check.
_wmi_verified_at: float | None = None
_wmi_live_limits: tuple = ()


def _wmi_limits_overridden(want_w: tuple) -> bool | None:
    """True when a live read disagrees with what the firmware claims is set.

    Only SPPT and FPPT are compared: the firmware's own SPL reading is the
    thing under suspicion here, and ryzenadj's STAPM limit never exceeds it.
    """
    # None means no fresh cross-check, not proof the hardware reached target.
    global _wmi_verified_at, _wmi_live_limits
    if not _ryzenadj_available or not os.path.isfile(BIN_PATH):
        return None
    now = time.monotonic()
    if _wmi_verified_at is not None and now - _wmi_verified_at < _WMI_VERIFY_EVERY_S:
        return None
    _wmi_verified_at = now

    if not _ryzenadj_lock.acquire(timeout=2.0):
        return None
    try:
        rc, out, _ = _run_ryzenadj(["--info"], timeout=3.0)
    finally:
        _ryzenadj_lock.release()
    if rc != 0:
        return None

    live = _parse_ryzenadj_output(out)
    pairs = [(live.get("sppt_limit"), want_w[1]), (live.get("fppt_limit"), want_w[2])]
    if any(v is None for v, _ in pairs):
        return None
    _wmi_live_limits = tuple(v for v, _ in pairs)
    return any(abs(v - w) > DRIFT_TOLERANCE_WMI_W for v, w in pairs)


def _wmi_profile_lost() -> bool:
    """True when the last apply was via WMI but the platform profile is no longer
    'custom', so the ppt_* attributes still read the old values yet no longer bind.
    Something external (Steam, amd_pmf, gamezone) knocked us off custom."""
    if _last_source != "wmi":
        return False
    path = _profile_path()
    return path is not None and _read_profile(path) != "custom"


# On the ryzenadj path a limits read spawns a process and the enforce loop asks every
# 5 s, so a recent answer is served; _apply_limits drops it so our own change shows.
_LIMITS_CACHE_TTL_S = 15.0
_limits_cache: dict = {}
_limits_cache_ts: float = 0.0
_limits_cache_lock = threading.Lock()


def _invalidate_limits_cache() -> None:
    global _limits_cache, _limits_cache_ts
    with _limits_cache_lock:
        _limits_cache, _limits_cache_ts = {}, 0.0


def _read_limits() -> dict:
    """Current limits in watts, read from whichever layer last applied them.

    The two layers do not observe each other: after a ryzenadj write the WMI
    attributes still report the firmware's own stale bookkeeping, so reading the
    wrong one would misreport the active limits.
    """
    global _limits_cache, _limits_cache_ts
    if _last_source == "wmi":
        vals = {f"{k}_limit": _wmi_read(k, "current_value") for k in WMI_ATTRS}
        if all(v is not None for v in vals.values()):
            return {k: float(v) for k, v in vals.items()}

    with _limits_cache_lock:
        if _limits_cache and time.monotonic() - _limits_cache_ts < _LIMITS_CACHE_TTL_S:
            return dict(_limits_cache)

    if not _ryzenadj_lock.acquire(timeout=4.0):
        return {}
    try:
        rc, out, _ = _run_ryzenadj(["--info"], timeout=3.0)
    finally:
        _ryzenadj_lock.release()

    parsed = _parse_ryzenadj_output(out) if rc == 0 else {}
    if parsed:
        with _limits_cache_lock:
            _limits_cache, _limits_cache_ts = dict(parsed), time.monotonic()
    return parsed


# ── Info cache refresh ─────────────────────────────────────────────────────────

def _refresh_info_cache() -> None:
    global _info_cache_ts
    values = _read_limits()
    watts  = _rapl_watts()
    with _info_cache_lock:
        _info_cache_ts = time.monotonic()
        # A failed/absent sensor read is not a new sample of the previous value.
        _info_cache.clear()
        if values:
            _info_cache.update(values)
        if watts is not None:
            _info_cache["package_draw"] = round(watts, 1)
        _info_cache["source"] = _last_source or "wmi"


# ── Game detection ─────────────────────────────────────────────────────────────

def _get_running_appid() -> str:
    """Current Steam game appid, or ''.

    Prefer the frontend's Router-based value while it is fresh - the /proc scan below
    misses games running inside pressure-vessel/gamescope. Falls back to the scan when
    the frontend has gone quiet (panel closed)."""
    return resolve_running_app(_frontend_appid, _frontend_appid_ts,
                               time.monotonic(), _scan_proc_for_appid, _FRONTEND_APPID_TTL)


def _scan_proc_for_appid() -> str:
    # The Steam "reaper" wrapper (reaper SteamLaunch AppId=NNNN -- ...) runs outside
    # the game's pressure-vessel/gamescope sandbox, so its cmdline is the most reliable
    # background signal. Fall back to SteamAppId in the environ.
    return scan_proc_for_appid()


# ── TDP enforce ────────────────────────────────────────────────────────────────

def _global_triplet(s: dict) -> tuple[int, int, int]:
    return _target_for_preset(s, s["spl"], s["sppt"], s["fppt"], s.get("active_preset", ""))


def _active_triplet(s: dict) -> tuple[int, int, int]:
    """The last applied target, clamped for the current Extras state."""
    return _clamp_for_settings(
        s, *(s.get("active_" + key, s[key]) for key in ("spl", "sppt", "fppt")))


def _startup_context_target(s: dict) -> tuple[str, tuple[int, int, int], bool]:
    """Resolve cold-start limits from current reality, never stale active_* data."""
    app_id = _get_running_appid()
    profile = _load_profiles().get(app_id) if app_id else None
    target = (
        _profile_target(s, profile, _get_ac_online())
        if profile is not None else _global_triplet(s)
    )
    return app_id, target, profile is not None


def _arm_ac_settle(target: tuple) -> int:
    global _ac_target, _ac_generation
    _ac_generation += 1
    _ac_target = tuple(target)
    return _ac_generation


def _cancel_ac_settle() -> int:
    return _arm_ac_settle(())


def _apply_and_record(spl: int, sppt: int, fppt: int, why: str, reason: str) -> dict:
    """Apply a triplet and remember it as the target the enforce pass defends."""
    with _mutation_lock:
        state = _load_settings()
        if not state.get("enabled", True):
            return _fail("plugin disabled")
        target = _clamp_for_settings(state, spl, sppt, fppt)
        result = _apply_limits_with_saved_cpu_power(state, *target, reason=reason)
        if result["success"]:
            _save_active(state, *target)
            decky.logger.info(
                f"[legotdp] Applied {why}: "
                f"{target[0] // 1000}/{target[1] // 1000}/{target[2] // 1000} W")
        else:
            event('tdp','limits_apply_failed',level='warning',returncode=result.get('returncode'),reason='hardware_unconfirmed',repeat=60)
        return result


def _target_matches(target: tuple) -> bool:
    """True when the readable limits agree with a milliwatt target."""
    values = _read_limits()
    current = tuple(values.get(f"{key}_limit") for key in ("spl", "sppt", "fppt"))
    if any(value is None for value in current):
        return False
    tolerance = (DRIFT_TOLERANCE_WMI_W if _last_source == "wmi"
                 else DRIFT_TOLERANCE_RYZENADJ_W)
    wanted = tuple(value / 1000 for value in target)
    return all(abs(actual - expected) <= tolerance
               for actual, expected in zip(current, wanted))


def _reapply_current_target(expected_generation: int | None = None) -> bool:
    """Re-assert whatever the enforce pass is currently defending.

    Returns True once the hardware already agrees, so the caller can stop early
    rather than keep writing at something that has settled.
    """
    global _ac_target
    with _mutation_lock:
        if expected_generation is not None and expected_generation != _ac_generation:
            return True
        s = _load_settings()
        if not s.get("enabled", True):
            _cancel_ac_settle()
            return True
        target = _ac_target or _active_triplet(s)
        if _target_matches(target):
            _best_effort_reapply_saved_cpu_power_locked(s)
            _ac_target = ()
            return True
        _apply_and_record(*target, "TDP after a charger transition", "ac")
        if _target_matches(target):
            _best_effort_reapply_saved_cpu_power_locked(s)
            _ac_target = ()
            return True
        return False


_transition_key = None
_transition_failures = 0
_transition_retry_at = 0.0


def _transition_apply(target, why, key, reason):
    global _transition_key, _transition_failures, _transition_retry_at
    if key != _transition_key:
        _transition_key, _transition_failures, _transition_retry_at = key, 0, 0.0
    if time.monotonic() < _transition_retry_at:
        return {"success": False}
    try:
        result = _apply_and_record(*target, why, reason)
    except Exception:
        _transition_failures += 1
        _transition_retry_at = time.monotonic() + (60 if _transition_failures >= 3 else 5)
        raise
    if result.get("success"):
        _transition_failures, _transition_retry_at = 0, 0.0
    else:
        _transition_failures += 1
        # Three early wake retries, then a low-cost retry once a minute.
        _transition_retry_at = time.monotonic() + (60 if _transition_failures >= 3 else 5)
    return result


def _check_and_enforce_locked() -> dict:
    """One enforce pass.

    Returns the events the caller should emit. This runs in an executor thread,
    which cannot await decky.emit itself, so the async loop above does the
    emitting - that is what lets the panel stop polling for the charger state.
    """
    global _current_game_id, _current_ac_online, _last_cpu_power_check, _transition_key, _ac_changed_at

    s = _load_settings()
    now = time.monotonic()
    resumed = _resume_detected()
    if resumed:
        _transition_key = None
        _invalidate_limits_cache()
        # Force the normal game/global transition branch below even if the same
        # title is still running after wake.
        _current_game_id = "\x00"
        decky.logger.info("[legotdp] backend detected resume from suspend")
    if resumed or now - _last_cpu_power_check >= CPU_POWER_DRIFT_CHECK_S:
        _best_effort_reapply_saved_cpu_power_locked(s)
        _last_cpu_power_check = now
    if not s.get("enabled", True):
        # Track the fresh frontend report (no /proc scan) so an unchanged
        # set_active_app heartbeat does not run another pass.
        _current_game_id = resolve_running_app(_frontend_appid, _frontend_appid_ts, now,
                                               lambda: _current_game_id, _FRONTEND_APPID_TTL)
        return {}

    appid    = _get_running_appid()
    ac_now   = _get_ac_online()
    ac_changed = ac_now != _current_ac_online
    _current_ac_online = ac_now
    events = {"power_source": {"ac": ac_now}} if ac_changed else {}
    if ac_changed:
        _ac_changed_at = now
    resettle = now - _ac_changed_at < sum(AC_SETTLE_DELAYS_S)

    game_changed = appid != _current_game_id

    if game_changed or ac_changed:
        prev = _current_game_id if game_changed else appid
        profile = _load_profiles().get(appid) if appid else None
        if profile is not None:
            trigger = "AC state change" if ac_changed else "game launch"
            target = _profile_target(s, profile, ac_now)
            if resettle:
                events["_resettle_generation"] = _arm_ac_settle(target)
            else:
                _cancel_ac_settle()
            result = _transition_apply(
                target, f"game profile on {trigger} (ac={ac_now})",
                (appid, ac_now, target), "ac" if ac_changed and not game_changed else "game")
            _current_game_id = appid if result.get("success") else "\x00"
            return events

        # Nothing per-game applies, so the global settings are what should be
        # running. Skipping this would leave the enforce pass below defending a
        # stale active_* triplet left over from whatever ran last.
        if appid:
            why = "global TDP, running game has no profile"
        elif prev:
            why = "global TDP, game exited"
        else:
            why = f"global TDP on AC change (ac={ac_now})"
        target = _global_triplet(s)
        if resettle:
            events["_resettle_generation"] = _arm_ac_settle(target)
        else:
            _cancel_ac_settle()
        result = _transition_apply(target, why, (appid, ac_now, target),
                                   "ac" if ac_changed and not game_changed else "global")
        _current_game_id = appid if result.get("success") else "\x00"
        return events

    _enforce_target(_active_triplet(s))
    return events


def _check_and_enforce() -> dict:
    global _current_ac_online
    with _mutation_lock:
        ac_before = _current_ac_online
        try:
            return _check_and_enforce_locked()
        except Exception:
            # A transition that raised is not applied: keep the charger edge (and
            # its panel event) so the next pass retries it.
            _current_ac_online = ac_before
            raise


# WMI reads back exactly what we wrote. ryzenadj reports STAPM LIMIT for SPL, which drifts
# watts below the set point under load; the wide band still catches a post-resume reset.
DRIFT_TOLERANCE_WMI_W      = 1.0
DRIFT_TOLERANCE_RYZENADJ_W = 6.0
DRIFT_MAX_ATTEMPTS = 3

_drift_target:   tuple = ()
_drift_settled:  tuple = ()
_drift_attempts: int   = 0
_drift_context: tuple = (False, ())


def _enforce_target(want: tuple) -> None:
    """Re-apply `want` when the hardware has drifted off it.

    Some targets are unreachable (the SMU caps slow-limit near 50 W), so after a few
    failed attempts the settled value is accepted until the hardware moves off it.
    """
    global _drift_target, _drift_settled, _drift_attempts, _drift_context

    if want != _drift_target:
        _drift_target, _drift_settled, _drift_attempts = want, (), 0
        _drift_context = (False, ())

    # Reuse the panel's cache only if filled after the last apply, else it shows a false drift.
    with _info_cache_lock:
        parsed = dict(_info_cache) if _panel_is_active() and _info_cache_ts > _applied_at else {}
    if not parsed:
        parsed = _read_limits()
    cur = tuple(parsed.get(f"{k}_limit") for k in ("spl", "sppt", "fppt"))
    if any(v is None for v in cur):
        return

    want_w    = tuple(v / 1000 for v in want)
    tolerance = DRIFT_TOLERANCE_WMI_W if _last_source == "wmi" else DRIFT_TOLERANCE_RYZENADJ_W
    # WMI attributes keep the last value after leaving 'custom' or an external change,
    # so a matching `cur` proves nothing then: re-apply on either signal.
    profile_lost = _wmi_profile_lost()
    overridden = _wmi_limits_overridden(want_w) if _last_source == "wmi" else False
    # No fresh live read (None) keeps the last confirmed one, or the next identical read looks like a new cause.
    live = _wmi_live_limits if overridden is True else _drift_context[1] if overridden is None else ()
    context = (profile_lost, live)
    if _drift_settled:
        same_limits = all(abs(c - r) <= tolerance for c, r in zip(cur, _drift_settled))
        same_live = (len(live) == len(_drift_context[1]) and
                     all(abs(a-b) <= tolerance for a, b in zip(live, _drift_context[1])))
        same_cause = profile_lost == _drift_context[0] and same_live
        if same_limits and same_cause:
            return
        # A genuinely different observed state gets a fresh recovery budget.
        _drift_settled, _drift_attempts = (), 0
    if not profile_lost and overridden is not True \
            and all(abs(c - r) <= tolerance for c, r in zip(cur, want_w)):
        # A confirmed target refills the budget (it counts consecutive failures), but
        # unchecked WMI facade values cannot erase a known SMU disagreement.
        if _last_source != "wmi" or overridden is False or not _drift_context[1]:
            _drift_attempts = 0
            _drift_context = (False, ())
        return

    if profile_lost:
        decky.logger.info("[legotdp] platform profile left 'custom', re-asserting limits")

    if overridden:
        decky.logger.info(
            "[legotdp] a live read disagrees with the firmware attributes, "
            "something moved the limits behind us - re-asserting")
    _drift_context = context

    if _drift_attempts >= DRIFT_MAX_ATTEMPTS:
        _drift_settled = cur
        decky.logger.warning(
            f"[legotdp] target {want_w} unreachable after {_drift_attempts} attempts, "
            f"accepting {cur} and standing down")
        return

    _drift_attempts += 1
    decky.logger.info(
        f"[legotdp] TDP drift {cur} -> {want_w}, re-applying (attempt {_drift_attempts})")
    result = _apply_limits_with_saved_cpu_power(_load_settings(), *want, reason="drift")
    if not result["success"]:
        # A single miss is expected while Steam resets TDP during a session restart; the next pass retries.
        event('tdp','drift_apply_failed',level='warning' if _drift_attempts > 1 else 'info',
              returncode=result.get('returncode'),reason='hardware_unconfirmed',count=_drift_attempts,repeat=60)


def _restore_defaults_locked() -> dict:
    """Hand control back to firmware. Caller must hold _mutation_lock."""
    global _last_source, _applied_mw, _applied_at
    global _drift_target, _drift_settled, _drift_attempts, _drift_context
    if not _legion_go_2():
        return _ok()  # never written
    _capture_baseline()
    saved = _baseline().get("platform_profile")
    if not _apply_lock.acquire(timeout=8.0):
        return _fail("apply busy")
    try:
        _drift_target, _drift_settled, _drift_attempts = (), (), 0
        _drift_context = (False, ())
        path = _profile_path()
        if path:
            target = (saved if isinstance(saved, str) and saved != "custom"
                      and saved in _profile_choices(path) else "balanced")
            if not _write_profile(path, target) or _read_profile(path) != target:
                return _fail(f"{target.capitalize()} firmware profile was not confirmed")
            _last_source, _applied_mw, _applied_at = "", (), 0.0
            _invalidate_limits_cache()
            decky.logger.info(f"[legotdp] restore_defaults: platform profile -> {target}")
            return _ok()
        if not _ryzenadj_lock.acquire(timeout=4.0):
            return _fail("ryzenadj busy")
        try:
            rc, out, err = _run_ryzenadj(["--max-performance"], timeout=5.0)
            if rc == 0:
                _last_source, _applied_mw, _applied_at = "", (), 0.0
                _invalidate_limits_cache()
            decky.logger.info(f"[legotdp] restore_defaults rc={rc}")
            return {"success": rc == 0, "stdout": out, "stderr": err, "returncode": rc}
        finally:
            _ryzenadj_lock.release()
    finally:
        _apply_lock.release()


def _release_controls_locked() -> dict:
    """Return every managed power control to standard policy without erasing intent."""
    if not _legion_go_2():
        return {"success": True, "error": ""}  # nothing was ever written here
    state = _load_settings()
    profiles = _load_profiles()
    _capture_baseline()  # a pending baseline reads as {} and would be overwritten by the hard defaults
    baseline = _baseline()
    boost = baseline.get(SETTINGS_FIELD_CPU_BOOST_ENABLED)
    epp = _canonical_epp_syntax(baseline.get(SETTINGS_FIELD_EPP))
    errors = []
    operations = []
    if state.get("enabled", True):
        operations.append(("TDP", _restore_defaults_locked, ()))
    # A game or its AC profile may have managed a control with no global choice.
    choices = [state, *profiles.values()]
    if any(choice.get(key) is not None for choice in choices
           for key in ("cpu_boost_enabled", "ac_cpu_boost_enabled")):
        operations.append(("CPU Boost", _apply_cpu_boost_hardware,
                           (boost if type(boost) is bool else True,)))
    if any(choice.get(key) is not None for choice in choices for key in ("epp", "ac_epp")):
        operations.append(("EPP", _apply_epp_hardware, (epp or "balance_performance",)))
    for name, operation, args in operations:
        try:
            result = operation(*args)
            if result.get("success") is not True:
                errors.append(f"{name}: {result.get('error') or result.get('stderr') or 'restoration was not confirmed'}")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return {"success": not errors, "error": "; ".join(errors)}


def _clamp_profile(profile: dict, ceilings: tuple[int, int, int]) -> None:
    if profile.get("spl") is not None:
        before = (profile["spl"], profile.get("sppt", profile["spl"]),
                  profile.get("fppt", profile["spl"]))
        after = _clamp_triplet(
            profile["spl"], profile.get("sppt", profile["spl"]),
            profile.get("fppt", profile["spl"]), ceilings)
        profile["spl"], profile["sppt"], profile["fppt"] = after
        if after != before:
            profile["preset"] = "custom"
    if profile.get("ac_spl") is not None:
        before = (profile["ac_spl"], profile.get("ac_sppt", profile["ac_spl"]),
                  profile.get("ac_fppt", profile["ac_spl"]))
        after = _clamp_triplet(
            profile["ac_spl"], profile.get("ac_sppt", profile["ac_spl"]),
            profile.get("ac_fppt", profile["ac_spl"]), ceilings)
        profile["ac_spl"], profile["ac_sppt"], profile["ac_fppt"] = after
        if after != before:
            profile["ac_preset"] = "custom"


def _lock_extras_state(state: dict, profiles: dict) -> tuple:
    """Clamp every persisted target to firmware limits and return the active one."""
    ceilings = _standard_ceilings_mw()
    global_before = (state["spl"], state["sppt"], state["fppt"])
    state["spl"], state["sppt"], state["fppt"] = _clamp_triplet(*global_before, ceilings)
    if (state["spl"], state["sppt"], state["fppt"]) != global_before:
        state["active_preset"] = "custom"
    if any(key in state for key in ("active_spl", "active_sppt", "active_fppt")):
        active = _clamp_triplet(
            state.get("active_spl", state["spl"]),
            state.get("active_sppt", state["sppt"]),
            state.get("active_fppt", state["fppt"]), ceilings)
    else:
        active = (state["spl"], state["sppt"], state["fppt"])
    state["active_spl"], state["active_sppt"], state["active_fppt"] = active
    for profile in profiles.values():
        if isinstance(profile, dict):
            _clamp_profile(profile, ceilings)
    return active


# ── CPU Boost / Energy Performance Preference ────────────────────────────────

def _read_cpu_power_text(path: str) -> str:
    with open(path, encoding="ascii") as handle:
        return handle.read().strip()


def _write_cpu_power_text(path: str, value: str) -> None:
    with open(path, "w", encoding="ascii") as handle:
        handle.write(value)


def _cpufreq_dir_index(path: str) -> int:
    leaf = os.path.basename(os.path.normpath(path))
    if leaf == "cpufreq":
        leaf = os.path.basename(os.path.dirname(os.path.normpath(path)))
    match = re.fullmatch(r"(?:policy|cpu)([0-9]+)", leaf)
    return int(match.group(1)) if match else (1 << 31)


def _dedupe_control_dirs(paths: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for path in sorted(paths, key=lambda item: (_cpufreq_dir_index(item), item)):
        identity = os.path.normcase(os.path.realpath(path))
        if identity in seen:
            continue
        seen.add(identity)
        result.append(path)
    return result


def _cpufreq_control_dirs() -> list[str]:
    """Return one directory per CPUFreq policy in numeric policy order."""
    policies = [
        path for path in glob.glob(os.path.join(
            CPU_SYS_ROOT, "cpufreq", "policy*"))
        if re.fullmatch(r"policy[0-9]+", os.path.basename(path))
        and os.path.isdir(path)
    ]
    if policies:
        return _dedupe_control_dirs(policies)
    cpu_dirs = [
        path for path in glob.glob(os.path.join(
            CPU_SYS_ROOT, "cpu*", "cpufreq"))
        if re.fullmatch(
            r"cpu[0-9]+", os.path.basename(os.path.dirname(path)))
        and os.path.isdir(path)
    ]
    return _dedupe_control_dirs(cpu_dirs)


def _boost_targets() -> tuple[list[str], bool, str]:
    """Select one Boost ABI in kernel-preferred fallback order."""
    if not _legion_go_2():
        return [], False, UNSUPPORTED_MODEL
    generic = os.path.join(CPU_SYS_ROOT, "cpufreq", "boost")
    if os.path.exists(generic):
        return [generic], True, ""
    # Valve/HHD kernels can expose AMD's CPB fallback. Revisions differ between
    # enabled/disabled and 1/0, so the writer preserves the read dialect.
    amd_global = os.path.join(CPU_SYS_ROOT, "amd_pstate", "cpb_boost")
    if os.path.exists(amd_global):
        return [amd_global], True, ""
    dirs = _cpufreq_control_dirs()
    if not dirs:
        return [], False, ""
    paths = [os.path.join(path, "boost") for path in dirs]
    present = [os.path.exists(path) for path in paths]
    if not any(present):
        return [], False, ""
    if not all(present):
        return [], True, "CPU Boost is missing from one or more CPU policies"
    return paths, True, ""


def _parse_boost_token(value: str) -> bool | None:
    if value in ("1", "enabled"):
        return True
    if value in ("0", "disabled"):
        return False
    return None


def _boost_write_token(original: str, enabled: bool) -> str:
    if original in ("enabled", "disabled"):
        return "enabled" if enabled else "disabled"
    return "1" if enabled else "0"


def _capture_cpu_boost() -> dict:
    targets, supported, discovery_error = _boost_targets()
    result = {
        "supported": supported, "available": False, "can_set": False,
        "enabled": None, "error": discovery_error, "targets": targets,
        "originals": {},
    }
    if not targets:
        return result
    originals: dict[str, str] = {}
    states: list[bool] = []
    try:
        for path in targets:
            raw = _read_cpu_power_text(path)
            state = _parse_boost_token(raw)
            if state is None:
                raise ValueError(f"unexpected CPU Boost value {raw!r}")
            originals[path] = raw
            states.append(state)
    except (OSError, ValueError) as exc:
        result["error"] = f"CPU Boost state unavailable: {exc}"
        return result
    result.update(available=True, can_set=True, originals=originals)
    if all(state == states[0] for state in states):
        result["enabled"] = states[0]
    else:
        result["error"] = "CPU Boost state is inconsistent across policies"
    return result


def _epp_targets() -> tuple[list[str], bool, str]:
    if not _legion_go_2():
        return [], False, UNSUPPORTED_MODEL
    dirs = _cpufreq_control_dirs()
    if not dirs:
        return [], False, ""
    paths = [os.path.join(
        path, "energy_performance_preference") for path in dirs]
    present = [os.path.exists(path) for path in paths]
    if not any(present):
        return [], False, ""
    if not all(present):
        return [], True, "EPP is missing from one or more CPU policies"
    return paths, True, ""


def _preference_tokens(path: str) -> list[str]:
    sibling = os.path.join(
        os.path.dirname(path), "energy_performance_available_preferences")
    global_path = os.path.join(
        CPU_SYS_ROOT, "cpufreq", "energy_performance_available_preferences")
    source = sibling if os.path.exists(sibling) else global_path
    if not os.path.exists(source):
        raise FileNotFoundError("energy_performance_available_preferences")
    raw = _read_cpu_power_text(source)
    if not raw:
        raise ValueError("empty EPP preference list")
    result: list[str] = []
    seen: set[str] = set()
    for token in raw.split():
        # available_preferences is a list of named profiles plus the special
        # custom marker. A decimal here would be exposed in profiles but still
        # be rejected by set_epp without custom, violating the RPC contract.
        if token != "custom" and _EPP_NAME_RE.fullmatch(token) is None:
            raise ValueError(f"invalid EPP preference {token!r}")
        if token not in seen:
            seen.add(token)
            result.append(token)
    return result


def _epp_numeric_value(value: str) -> int | None:
    if _EPP_DECIMAL_RE.fullmatch(value):
        number = int(value, 10)
        return number if EPP_MIN <= number <= EPP_MAX else None
    return _EPP_NAMED_VALUES.get(value)


def _capture_epp() -> dict:
    targets, supported, discovery_error = _epp_targets()
    result = {
        "supported": supported, "available": False, "can_set": False,
        "value": None, "profiles": [], "numeric_supported": False,
        "numeric_value": None, "error": discovery_error, "targets": targets,
        "originals": {},
    }
    if not targets:
        return result
    originals: dict[str, str] = {}
    preference_lists: list[list[str]] = []
    try:
        # Snapshot every policy and capability before the first write.
        for path in targets:
            current = _canonical_epp_syntax(_read_cpu_power_text(path))
            if current is None:
                raise ValueError("unexpected EPP value")
            originals[path] = current
            preference_lists.append(_preference_tokens(path))
    except (OSError, ValueError) as exc:
        result["error"] = f"EPP state unavailable: {exc}"
        return result
    common = set(preference_lists[0])
    for preferences in preference_lists[1:]:
        common.intersection_update(preferences)
    ordered_common = [
        item for item in preference_lists[0] if item in common]
    numeric_supported = "custom" in common
    # custom is only a capability marker for numeric 0..255.
    profiles = [item for item in ordered_common if item != "custom"]
    if not profiles and not numeric_supported:
        result["error"] = (
            "no EPP preference is supported by every CPU policy")
        return result
    values = list(originals.values())
    value = values[0] if all(item == values[0] for item in values) else None
    numeric_values = [_epp_numeric_value(item) for item in values]
    numeric_value: int | None = None
    if all(item is not None and item == numeric_values[0]
           for item in numeric_values):
        numeric_value = numeric_values[0]
        if value is None:
            # Mixed named/raw representations are readable numerically; write
            # ownership below deliberately remains token-exact.
            value = str(numeric_value)
    result.update(
        available=True, can_set=True, originals=originals,
        profiles=profiles, numeric_supported=numeric_supported,
        value=value, numeric_value=numeric_value,
    )
    if value is None:
        result["error"] = "EPP state is inconsistent across CPU policies"
    return result


def _sysfs_short_name(path: str) -> str:
    try:
        relative = os.path.relpath(path, CPU_SYS_ROOT)
    except ValueError:
        return os.path.basename(path)
    return relative if relative != os.pardir and not relative.startswith(
        os.pardir + os.sep) else os.path.basename(path)


def _rollback_cpu_power_write(
    originals: dict[str, str],
    requested: dict[str, str],
    changed: list[str],
    equivalent,
) -> tuple[list[str], list[str]]:
    """Best-effort rollback without overwriting a third-party new value.
    Returns (external, failures): only `failures` may leave our own value behind."""
    external: list[str] = []
    failures: list[str] = []
    for path in reversed(changed):
        try:
            current = _read_cpu_power_text(path)
            if equivalent(current, originals[path]):
                continue
            if not equivalent(current, requested[path]):
                # An unresolved "default" alias may read back as our own concrete value.
                (failures if requested[path] == "default" else external).append(
                    f"{_sysfs_short_name(path)} changed externally")
                continue
            _write_cpu_power_text(path, originals[path])
            restored = _read_cpu_power_text(path)
            if not equivalent(restored, originals[path]):
                failures.append(
                    f"{_sysfs_short_name(path)} rollback readback mismatch")
        except (OSError, ValueError) as exc:
            failures.append(
                f"{_sysfs_short_name(path)} rollback failed: {exc}")
    return external, failures


def _transactional_cpu_power_write(
    originals: dict[str, str],
    requested: dict[str, str],
    equivalent,
    label: str,
    resolve_readback=None,
) -> dict:
    changed: list[str] = []
    try:
        for path, wanted in requested.items():
            if equivalent(originals[path], wanted):
                continue
            # close(2) can fail after a sysfs write took effect, so record the
            # target before opening it and include it in a possible rollback.
            changed.append(path)
            _write_cpu_power_text(path, wanted)
            actual = _read_cpu_power_text(path)
            if resolve_readback is not None:
                # Keep the concrete value returned by the kernel for final
                # verification and rollback ownership, not an input alias.
                wanted = resolve_readback(wanted, actual)
                requested[path] = wanted
            if not equivalent(actual, wanted):
                raise OSError(
                    f"{_sysfs_short_name(path)} readback mismatch")
        # A final pass catches an external writer racing an earlier policy.
        for path, wanted in requested.items():
            actual = _read_cpu_power_text(path)
            if not equivalent(actual, wanted):
                raise OSError(
                    f"{_sysfs_short_name(path)} final readback mismatch")
    except (OSError, ValueError, KeyError) as exc:
        external, failures = _rollback_cpu_power_write(originals, requested, changed, equivalent)
        rollback = external + failures
        suffix = (f"; rollback incomplete: {'; '.join(rollback)}" if rollback
                  else "; previous state restored")
        # Only our own value left behind is unrestored; a third party's is not ours to undo.
        return {"success": False, "error": f"{label} write failed: {exc}{suffix}",
                "restored": not failures}
    return {"success": True, "error": ""}


def _boost_equivalent(left: str, right: str) -> bool:
    a, b = _parse_boost_token(left), _parse_boost_token(right)
    if a is None or b is None:
        raise ValueError("invalid CPU Boost readback")
    return a == b


def _epp_equivalent(left: str, right: str) -> bool:
    a, b = _canonical_epp_syntax(left), _canonical_epp_syntax(right)
    if a is None or b is None:
        raise ValueError("invalid EPP readback")
    # Representation is ownership. Named power and raw 255 are not equivalent
    # for rollback: a third party may have written the named token after us.
    return a == b


def _apply_cpu_boost_hardware(enabled: bool) -> dict:
    _capture_baseline()
    capture = _capture_cpu_boost()
    if not capture["can_set"]:
        return {
            "success": False,
            "error": capture["error"] or "CPU Boost is unsupported",
        }
    requested = {
        path: _boost_write_token(capture["originals"][path], enabled)
        for path in capture["targets"]
    }
    return _transactional_cpu_power_write(
        capture["originals"], requested, _boost_equivalent, "CPU Boost")


def _validated_epp_request(capture: dict, value) -> str | None:
    canonical = _canonical_epp_syntax(value)
    if canonical is None:
        return None
    if _EPP_DECIMAL_RE.fullmatch(canonical):
        return canonical if capture["numeric_supported"] else None
    return canonical if canonical in capture["profiles"] else None


def _compatible_saved_epp(capture: dict, value) -> str | None:
    canonical = _validated_epp_request(capture, value)
    if canonical is not None:
        return canonical
    canonical = _canonical_epp_syntax(value)
    if canonical is None or not _EPP_DECIMAL_RE.fullmatch(canonical):
        return None
    # Older amd-pstate accepts names only. Quantize the applied value, never
    # the saved intent, so switching back to a custom-capable kernel is lossless.
    choices = [name for name in capture["profiles"] if name in _EPP_NAMED_VALUES]
    return min(choices, key=lambda name: abs(_EPP_NAMED_VALUES[name] - int(canonical))) if choices else None


def _apply_epp_hardware(value: str) -> dict:
    _capture_baseline()
    capture = _capture_epp()
    if not capture["can_set"]:
        return {
            "success": False,
            "error": capture["error"] or "EPP is unsupported",
        }
    canonical = _compatible_saved_epp(capture, value)
    if canonical is None:
        return {"success": False, "error": "invalid or unsupported EPP value"}
    requested = {path: canonical for path in capture["targets"]}
    def resolve_readback(wanted: str, actual: str) -> str:
        if wanted != "default":
            return wanted
        # amd-pstate resolves "default" to the boot EPP and reports its name
        # (or a raw value on custom-capable kernels). Only this documented
        # alias may adopt the immediate, capability-validated readback.
        resolved = _validated_epp_request(capture, actual)
        if resolved is None:
            raise ValueError("invalid default EPP readback")
        return resolved
    return _transactional_cpu_power_write(
        capture["originals"], requested, _epp_equivalent, "EPP", resolve_readback)


def _cpu_power_controls_status(
    *, operation_success: bool | None = None, operation_error: str = ""
) -> dict:
    boost = _capture_cpu_boost()
    epp = _capture_epp()
    boost_public = {
        "available": boost["available"],
        "enabled": boost["enabled"],
        "error": boost["error"],
    }
    epp_public = {
        "available": epp["available"],
        "value": epp["value"],
        "profiles": epp["profiles"],
        "numeric_supported": epp["numeric_supported"],
        "min": EPP_MIN,
        "max": EPP_MAX,
        "numeric_value": epp["numeric_value"],
        "error": epp["error"],
    }
    read_errors = [item for item in (boost["error"], epp["error"]) if item]
    if operation_success is None:
        operation_success = not read_errors
        operation_error = "; ".join(read_errors)
    return {
        "success": operation_success,
        "available": boost["available"] or epp["available"],
        "cpu_boost": boost_public,
        "epp": epp_public,
        "error": operation_error,
    }


def _cpu_power_controls_error_status(
    error: str, app_id: str = "", ac_profile: bool = False
) -> dict:
    """Preserve the RPC shape even if an unexpected dependency raises."""
    return {
        "success": False,
        "available": False,
        "cpu_boost": {"available": False, "enabled": None, "error": error},
        "epp": {
            "available": False,
            "value": None,
            "profiles": [],
            "numeric_supported": False,
            "min": EPP_MIN,
            "max": EPP_MAX,
            "numeric_value": None,
            "error": error,
        },
        "error": error,
        "profile": {"app_id": app_id, "ac_profile": ac_profile, "active": False,
                    "cpu_boost_enabled": None, "epp": None},
    }


def _stored_edit_refusal(app_id: str, current_app_id: str) -> str | None:
    """A saved profile is edited without hardware writes, so never the running game's."""
    if not app_id:
        return "invalid profile request"
    if current_app_id == app_id:
        return "game is running; change its settings as the running game"
    if app_id not in _load_profiles():
        return "this game has no profile in use"
    return None


def _scope_refusal(app_id: str, current_app_id: str, expected_app_id: str | None,
                   stored: bool) -> str | None:
    """Why an edit for app_id ("" = Global) no longer fits the foreground game."""
    if expected_app_id is not None and current_app_id != expected_app_id:
        return "foreground game changed"
    if stored:
        return _stored_edit_refusal(app_id, current_app_id)
    if app_id and current_app_id != app_id:
        return "game is no longer active"
    return None


def _cpu_editor_profile(state: dict, profiles: dict, app_id: str,
                        ac_profile: bool) -> dict:
    selected = profiles.get(app_id) if app_id else None
    current_app = _get_running_appid()
    ac_online = _get_ac_online()
    enabled = state.get("enabled", True)
    if app_id:
        separate = bool(selected and selected.get("ac_separate"))
        active = enabled and current_app == app_id and (
            (ac_profile and separate and ac_online)
            or (not ac_profile and not (separate and ac_online)))
    else:
        active = not enabled or current_app not in profiles
    return {"app_id": app_id, "ac_profile": ac_profile, "active": active,
            **_effective_cpu_values(state, selected, ac_profile)}


def _cpu_scoped_status(app_id: str = "", ac_profile: bool = False, **operation) -> dict:
    result = _cpu_power_controls_status(**operation)
    result["profile"] = _cpu_editor_profile(
        _load_settings(), _load_profiles(), app_id, ac_profile)
    saved = result["profile"]["epp"]
    effective = _compatible_saved_epp(result["epp"], saved)
    if saved is not None and effective is not None and effective != saved:
        result["epp"]["compatibility_note"] = (
            f"This kernel supports EPP presets only. Saved EPP {saved} uses "
            f"{effective.replace('_', ' ')} when this profile is active. "
            "The exact saved value is retained for newer kernels.")
    return result


def _best_effort_reapply_saved_cpu_power_locked(state: dict) -> list[str]:
    """Startup/resume/drift restore of the effective choice; errors are returned, never raised.

    Caller holds _mutation_lock.
    """
    errors: list[str] = []
    try:
        state = _effective_cpu_state(state)
        boost = state.get(SETTINGS_FIELD_CPU_BOOST_ENABLED)
        if type(boost) is bool:
            result = _apply_cpu_boost_hardware(boost)
            if not result["success"]:
                errors.append(result["error"])
        epp = state.get(SETTINGS_FIELD_EPP)
        if isinstance(epp, str):
            result = _apply_epp_hardware(epp)
            if not result["success"]:
                errors.append(result["error"])
    except Exception as exc:
        errors = [f"CPU power restore failed: {exc}"]
        event('tdp','cpu_power_restore_failed',level='warning',error=exc,stage='saved_cpu_controls',repeat=60)
    else:
        if errors:
            event('tdp','cpu_power_restore_failed',level='warning',stage='saved_cpu_controls',count=len(errors),reason='hardware_unconfirmed',repeat=60)
    return errors


def _apply_limits_with_saved_cpu_power(
    state: dict, spl: int, sppt: int, fppt: int, reason: str | None = None
) -> dict:
    """Apply Layer 1 first, then restore persistent CPU controls.

    Selecting a platform profile can reset amd-pstate EPP. Keeping this wrapper
    at every managed TDP transition makes Boost/EPP the final write without
    turning their optional capability failure into a TDP failure.
    Caller holds _mutation_lock.
    """
    result = _apply_limits(spl, sppt, fppt)
    if result["success"]:
        # Without a reason, the RPC that applied it is the event's operation.
        event('tdp', 'limits_applied', reason=reason, source=_last_source,
              **dict(zip(("spl", "sppt", "fppt"), (value // 1000 for value in _applied_mw))))
    # A failed Layer 1 call may already have selected platform_profile before a
    # later firmware/RyzenAdj error. CPU controls are independent, so restore
    # them even on that partial failure.
    errors = _best_effort_reapply_saved_cpu_power_locked(state)
    result["cpu_power_errors"] = errors
    return result


def _apply_active_target(state: dict, cpu_state: dict, target: tuple) -> dict:
    """Apply a target and, once it holds, record it as active. Caller holds _mutation_lock."""
    result = _apply_limits_with_saved_cpu_power(cpu_state, *target)
    if result["success"]:
        state["active_spl"], state["active_sppt"], state["active_fppt"] = target
        _cancel_ac_settle()
    return result


# ── Plugin class ───────────────────────────────────────────────────────────────

from contextlib import contextmanager


@contextmanager
def _tdp_user_transaction():
    """Restore the prior target after a failed hardware apply or durable commit.

    The yielded callback accepts an RPC result so expected hardware refusals can
    keep their original error shape while sharing the exception rollback path.
    """
    with _mutation_lock:
        before = copy.deepcopy(_load_settings())
        before_cpu = _effective_cpu_state(before)
        for field in (SETTINGS_FIELD_CPU_BOOST_ENABLED, SETTINGS_FIELD_EPP):
            if before_cpu.get(field) is None:
                before_cpu[field] = _read_cpu_baseline(field)
        def restore_previous() -> bool:
            try:
                if before.get("enabled", True):
                    rollback = _apply_limits_with_saved_cpu_power(
                        before_cpu, *_active_triplet(before), reason="rollback")
                else:
                    rollback = _restore_defaults_locked()
                    rollback["cpu_power_errors"] = _best_effort_reapply_saved_cpu_power_locked(before_cpu)
                return (rollback.get("success", False)
                        and not rollback.get("cpu_power_errors"))
            except Exception as exc:
                event("tdp", "rollback", level="error", error=exc,
                      restored=False, stage="hardware", repeat=60)
                return False

        def failure_detail(restored: bool) -> str:
            return ("previous target restored" if restored
                    else "hardware rollback failed; check limits")

        def restore_failed(result: dict) -> dict:
            if not result.get("success", False):
                detail = failure_detail(restore_previous())
                result["stderr"] = f"{result.get('stderr') or 'TDP apply failed'}; {detail}"
            return result

        try:
            yield restore_failed
        except Exception as exc:
            detail = failure_detail(restore_previous())
            raise RuntimeError(f"TDP operation failed: {exc}; {detail}") from exc


def _set_cpu_control_locked(field: str, value, selected: str, ac_profile: bool,
                            expected: str | None, stored: bool) -> tuple[bool, str]:
    """Save, then apply, one CPU Boost/EPP choice; return (success, error).

    Caller holds _mutation_lock and has validated the scope arguments.
    """
    current = _get_running_appid()
    refusal = _scope_refusal(selected, current, expected, stored)
    if refusal:
        return False, refusal
    state, profiles = _load_settings(), _load_profiles()
    if selected and not state.get("enabled", True):
        return False, "plugin disabled"
    if ac_profile and not profiles.get(selected, {}).get("ac_separate"):
        return False, "separate AC profile is disabled"
    if field == SETTINGS_FIELD_CPU_BOOST_ENABLED:
        if type(value) is not bool:
            return False, "invalid CPU Boost value"
        preflight, canonical = _capture_cpu_boost(), value
        apply = _apply_cpu_boost_hardware
    else:
        preflight = _capture_epp()
        canonical = _validated_epp_request(preflight, value)
        apply = _apply_epp_hardware
        if canonical is None:
            return False, "invalid or unsupported EPP value"
    if not preflight["can_set"]:
        return False, preflight["error"] or "CPU control is unsupported"
    if selected:
        profile = _prepare_cpu_profile(
            state, profiles, selected, required_field=field)
        # Legacy AC profiles inherited battery CPU values. Keep
        # their pre-edit value when the battery branch changes.
        if (not ac_profile and profile.get("ac_separate")
                and profile.get("ac_" + field) is None):
            profile["ac_" + field] = _effective_cpu_values(
                state, profile, True)[field]
        profile[("ac_" if ac_profile else "") + field] = canonical
    else:
        state[field] = canonical
    active = not stored and _cpu_editor_profile(state, profiles, selected, ac_profile)["active"]
    values = {SETTINGS_KEY_SETTINGS: state}
    previous = {SETTINGS_KEY_SETTINGS: _read_key(SETTINGS_KEY_SETTINGS, _defaults())}
    if selected:
        values[SETTINGS_KEY_GAME_PROFILES] = profiles
        previous[SETTINGS_KEY_GAME_PROFILES] = _read_key(SETTINGS_KEY_GAME_PROFILES, {})
    try:
        # Saved before the write: the choice is what makes release restore
        # the baseline, so a crash mid-write cannot orphan a changed control.
        _write_keys(values)
    except Exception as exc:
        return False, f"settings persist failed: {exc}"
    if active:
        transaction = apply(canonical)
        if not transaction["success"]:
            # An incomplete rollback keeps the choice, and with it the release.
            if transaction.get("restored", True):
                try:
                    _write_keys(previous)
                except Exception as exc:
                    return False, f"{transaction['error']}; settings revert failed: {exc}"
            return False, transaction["error"]
    event("tdp", "cpu_profile_saved", attribute=field,
          scope=("game_ac" if ac_profile else "game") if selected else "global",
          outcome="applied" if active else "saved")
    return True, ""


def _save_stored_tdp_locked(s: dict, app_id: str, spl: int, sppt: int, fppt: int,
                            preset_name: str) -> dict:
    """Edit a saved, not running, game profile without touching hardware."""
    profiles = _load_profiles()
    profile = profiles[app_id]
    requested = _target_for_preset(s, spl, sppt, fppt, preset_name or profile.get("preset", ""))
    profile.update({"spl": requested[0], "sppt": requested[1], "fppt": requested[2]})
    if preset_name:
        profile["preset"] = preset_name
    _write_keys({SETTINGS_KEY_GAME_PROFILES: profiles})
    event("tdp", "profile_saved", scope="game", outcome="saved")
    return _ok(applied=False, saved_only=True, stored=True)


def _save_inactive_global_tdp_locked(s: dict, spl: int, sppt: int, fppt: int,
                                     preset_name: str) -> dict:
    """Save Global while a game profile owns the limits.

    The desktop editor can select Global while a game owns the active limits.
    Save that future policy without replacing the game's defended target or
    its AC ladder.
    """
    requested = _target_for_preset(
        s, spl, sppt, fppt,
        preset_name or s.get("active_preset", ""))
    s["spl"], s["sppt"], s["fppt"] = requested
    # active_preset is the historical global preset key;
    # per-game labels live in their own profile entries.
    s["active_preset"] = preset_name or s.get("active_preset", "")
    try:
        _save_settings(s)
    except Exception as exc:
        return _fail(f"Could not save global TDP settings: {exc}",
                     applied=False, saved_only=False)
    return _ok(applied=False, saved_only=True, message=(
        "Global TDP settings saved. They will apply when no game profile is active."))


def _apply_tdp_locked(s: dict, app_id: str, spl: int, sppt: int, fppt: int,
                      preset_name: str) -> dict:
    """Apply and save Global or the running game's limits ("" = Global).

    Caller holds _mutation_lock across validation, hardware and persistence.
    """
    with _tdp_user_transaction() as restore_failed:
        profiles: dict = {}
        existing: dict = {}
        if app_id:
            profiles = _load_profiles()
            existing = _prepare_cpu_profile(s, profiles, app_id)
        effective_preset = preset_name or (
            existing.get("preset", "") if app_id else s.get("active_preset", ""))
        requested = _target_for_preset(
            s, spl, sppt, fppt, effective_preset)
        want = requested

        if app_id:
            # On AC with a separate AC profile, the sliders describe the
            # battery values but the hardware should run the AC ones.
            if (_get_ac_online() and existing.get("ac_separate")
                    and existing.get("ac_spl") is not None):
                want = _target_for_preset(
                    s,
                    existing["ac_spl"],
                    existing.get("ac_sppt", existing.get("sppt", _defaults()["sppt"])),
                    existing.get("ac_fppt", existing.get("fppt", _defaults()["fppt"])),
                    existing.get("ac_preset", ""),
                )

        cpu_state = (
            _effective_cpu_state(s, profiles, app_id) if app_id
            else _effective_cpu_state(s))
        result = _apply_limits_with_saved_cpu_power(cpu_state, *want)
        if not result["success"]:
            return restore_failed(result)

        if app_id:
            existing.update({"spl": requested[0], "sppt": requested[1],
                             "fppt": requested[2]})
            if preset_name:
                existing["preset"] = preset_name
            profiles[app_id] = existing
            event("tdp", "profile_saved", scope="game", outcome="confirmed")
        else:
            s["spl"], s["sppt"], s["fppt"] = requested
            s["active_preset"] = effective_preset
        s["active_spl"], s["active_sppt"], s["active_fppt"] = want
        values = {SETTINGS_KEY_SETTINGS: s}
        if app_id:
            values[SETTINGS_KEY_GAME_PROFILES] = profiles
        _write_keys(values)
        _cancel_ac_settle()
        result.update(applied=True, saved_only=False)
        return restore_failed(result)


class Plugin:
    def __init__(self):
        self._tasks = []
        self._helper_cancel = threading.Event()
        # Extras taps and helper retries run in tap order, so the last choice wins.
        self._extras_lock = asyncio.Lock()
        self._helper_error = ""

    _ready: bool = False
    # Surfaced through is_ready() so a failed start shows up in the panel
    # instead of leaving the user with sliders that silently do nothing.
    _setup_error: str | None = None
    _tasks: list = []

    async def is_ready(self) -> dict:
        return {"ready": self._ready, "error": self._setup_error or ""}

    async def get_settings(self) -> dict:
        return await _offload(_load_settings)

    async def get_cpu_power_controls(self, app_id: str = "", ac_profile: bool = False) -> dict:
        def _do() -> dict:
            with _mutation_lock:
                try:
                    selected = normalise_app_id(app_id, allow_empty=True)
                    if selected is None or type(ac_profile) is not bool or (ac_profile and not selected):
                        return _cpu_scoped_status(
                            operation_success=False, operation_error="invalid CPU profile context")
                    return _cpu_scoped_status(selected, ac_profile)
                except Exception as exc:
                    event('tdp','cpu_status_failed',level='warning',error=exc,repeat=60)
                    return _cpu_power_controls_error_status(
                        "CPU power controls unavailable", selected or "",
                        ac_profile if type(ac_profile) is bool else False)
        return await _offload(_do)

    async def _set_cpu_control(self, field: str, value, app_id: str,
                               ac_profile: bool, expected_app_id: str | None, stored: bool = False) -> dict:
        def _do() -> dict:
            with _mutation_lock:
                selected = normalise_app_id(app_id, allow_empty=True)
                expected = normalise_app_id(expected_app_id, allow_empty=True) if expected_app_id is not None else None
                def status(success: bool, error: str = "") -> dict:
                    return _cpu_scoped_status(
                        selected or "", ac_profile if type(ac_profile) is bool else False,
                        operation_success=success, operation_error=error)
                try:
                    if (selected is None or type(ac_profile) is not bool or type(stored) is not bool
                            or (ac_profile and not selected) or (stored and not selected)
                            or (expected_app_id is not None and expected is None)):
                        return status(False, "invalid CPU profile context")
                    return status(*_set_cpu_control_locked(
                        field, value, selected, ac_profile, expected, stored))
                except Exception as exc:
                    event('tdp','cpu_profile_failed',level='warning',error=exc,attribute=field)
                    try:
                        return status(False, str(exc))
                    except Exception:
                        return _cpu_power_controls_error_status(
                            "CPU power operation failed", selected or "",
                            ac_profile if type(ac_profile) is bool else False)
        return await _offload(_do)

    async def set_cpu_boost(self, enabled: bool, app_id: str = "", ac_profile: bool = False,
                            expected_app_id: str | None = None, stored: bool = False) -> dict:
        return await self._set_cpu_control(
            SETTINGS_FIELD_CPU_BOOST_ENABLED, enabled, app_id, ac_profile, expected_app_id, stored)

    async def set_epp(self, value: str, app_id: str = "", ac_profile: bool = False,
                      expected_app_id: str | None = None, stored: bool = False) -> dict:
        return await self._set_cpu_control(
            SETTINGS_FIELD_EPP, value, app_id, ac_profile, expected_app_id, stored)

    async def get_power_source(self) -> dict:
        return {"ac": await _offload(_get_ac_online)}

    async def retry_extras(self) -> dict:
        """Download and verify RyzenAdj. Only ever started by the user (Extras)."""
        async with self._extras_lock:
            return await self._retry_extras()

    async def _retry_extras(self) -> dict:
        if not _legion_go_2():
            return {"success": False, "error": UNSUPPORTED_MODEL}
        if _ryzenadj_available:
            return {"success": True}  # queued behind the retry that verified it
        cancellation = self._helper_cancel
        def retry():
            global _ryzenadj_available, _current_game_id, _transition_key
            try:
                if not _ensure_ryzenadj(cancel=cancellation.is_set):
                    raise RuntimeError("Verified RyzenAdj is unavailable")
                with _mutation_lock:
                    if cancellation.is_set():
                        return {"success": False, "error": "RyzenAdj download was cancelled"}
                    _ryzenadj_available = True
                    self._helper_error = ""
                    _current_game_id = "\x00"
                    _transition_key = None
                transition("tdp", "helper_ready", True, outcome="verified")
                return {"success": True}
            except Exception as exc:
                if not cancellation.is_set():
                    self._helper_error = str(exc)
                    transition("tdp", "helper_ready", False, level="warning", error=exc,
                               outcome="deferred", retryable=True)
                else:
                    event("tdp", "helper_job", outcome="cancelled", reason="module_shutdown")
                return {"success": False, "error": str(exc)}
        return await _offload(retry)

    async def get_extras_unlocked(self) -> bool:
        s = await _offload(_load_settings)
        return s.get("extras_unlocked", False)

    async def set_advanced_tdp_control(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            return {"success": False, "stderr": "enabled must be a boolean"}
        def _do() -> dict:
            with _tdp_user_transaction() as restore_failed:
                state = _load_settings()
                state["advanced_tdp_control"] = enabled
                if state.get("enabled", True):
                    app_id = _get_running_appid()
                    profiles = _load_profiles()
                    profile = profiles.get(app_id) if app_id else None
                    ac_online = _get_ac_online() if profile else False
                    preset = (profile.get("ac_preset" if ac_online and profile.get("ac_separate")
                                          else "preset", "") if profile
                              else state.get("active_preset", ""))
                    raw = (_pick_profile_values(profile, ac_online) if profile
                           else (state["spl"], state["sppt"], state["fppt"]))
                    if _is_custom_preset(*raw, preset):
                        target = (_profile_target(state, profile, ac_online) if profile
                                  else _global_triplet(state))
                        result = _apply_active_target(
                            state, _effective_cpu_state(state, profiles, app_id, ac_online), target)
                        if not result["success"]:
                            return restore_failed(result)
                _save_settings(state)
                return {"success": True}
        return await _offload(_do)

    async def set_extras_unlocked(self, enabled: bool) -> dict:
        async with self._extras_lock:
            return await self._set_extras_unlocked(enabled)

    async def _set_extras_unlocked(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            return _fail("enabled must be a boolean")
        if enabled and not _ryzenadj_available:
            helper = await self._retry_extras()
            if not helper["success"]:
                return _fail(f"Extras needs the verified RyzenAdj helper: {helper['error']}")
        def _do():
            with _tdp_user_transaction() as restore_failed:
                if enabled and not _ryzenadj_available:
                    return _fail("Extras is unavailable because ryzenadj is not ready")
                s = _load_settings()
                profiles = _load_profiles()
                if enabled:
                    s["extras_unlocked"] = True
                    _save_settings(s)
                    return _ok()

                _lock_extras_state(s, profiles)
                s["extras_unlocked"] = False
                app_id = _get_running_appid()
                profile = profiles.get(app_id) if app_id else None
                active = (_profile_target(s, profile, _get_ac_online()) if profile
                          else _global_triplet(s))
                if s.get("enabled", True):
                    result = _apply_limits_with_saved_cpu_power(
                        _effective_cpu_state(s, profiles, app_id), *active)
                    if not result["success"]:
                        return restore_failed(result)
                s["active_spl"], s["active_sppt"], s["active_fppt"] = active
                _write_keys({SETTINGS_KEY_SETTINGS: s,
                             SETTINGS_KEY_GAME_PROFILES: profiles})
                _cancel_ac_settle()
                return _ok()
        result = await _offload(_do)
        decky.logger.info(f"[legotdp] extras_unlocked={enabled} success={result['success']}")
        return result

    async def get_game_profile(self, app_id: str) -> dict:
        app_id = normalise_app_id(app_id)
        if app_id is None:
            return {"exists": False, "profile": {}, "ac_separate": False,
                    "ac_profile": {}, "error": "invalid app id"}
        def _do() -> dict:
            p = _load_profiles().get(app_id)
            active = p is not None
            if p is None:
                p = _load_inactive_profiles().get(app_id)
            if p is None:
                return {"exists": False, "active": False, "profile": {}, "ac_separate": False, "ac_profile": {}}
            spl  = p.get("spl",  _defaults()["spl"])
            sppt = p.get("sppt", _defaults()["sppt"])
            fppt = p.get("fppt", _defaults()["fppt"])
            state = _load_settings()
            return {
                "exists":      True,
                "active":      active,
                "profile":     {"spl": spl, "sppt": sppt, "fppt": fppt,
                                "preset": p.get("preset", ""),
                                **_effective_cpu_values(state, p)},
                "ac_separate": p.get("ac_separate", False),
                "ac_profile":  {"spl": p.get("ac_spl", spl), "sppt": p.get("ac_sppt", sppt),
                                "fppt": p.get("ac_fppt", fppt),
                                "ac_preset": p.get("ac_preset", ""),
                                **_effective_cpu_values(state, p, True)},
            }
        return await _offload(_do)

    async def list_game_profiles(self) -> dict:
        """Every saved game profile, for browsing; a profile kept under Global is inactive."""
        def _do() -> dict:
            rows = {}
            for active, profiles in ((False, _load_inactive_profiles()), (True, _load_profiles())):
                for app_id, p in profiles.items():
                    spl = p.get("spl", _defaults()["spl"])
                    rows[app_id] = {"app_id": app_id, "active": active,
                                    "ac_separate": p.get("ac_separate", False),
                                    "preset": p.get("preset", ""), "spl": spl,
                                    "ac_preset": p.get("ac_preset", ""), "ac_spl": p.get("ac_spl", spl)}
            return {"profiles": list(rows.values())}
        return await _offload(_do)

    async def set_game_ac_profile(self, app_id: str, spl: int, sppt: int, fppt: int, ac_separate: bool, preset_name: str = "",
                                  stored: bool = False) -> dict:
        app_id = normalise_app_id(app_id)
        if (app_id is None or type(ac_separate) is not bool or type(stored) is not bool
                or any(type(value) is not int for value in (spl, sppt, fppt))):
            return _fail("invalid profile request")
        preset_name = _safe_label(preset_name)
        def _do() -> dict:
            with _tdp_user_transaction() as restore_failed:
                current = _get_running_appid()
                refusal = _scope_refusal(app_id, current, None, stored)
                if refusal:
                    return _fail(refusal)
                state = _load_settings()
                if not state.get("enabled", True):
                    return _fail("plugin disabled")
                profiles = _load_profiles()
                p = _prepare_cpu_profile(
                    state, profiles, app_id,
                    snapshot_cpu=ac_separate and not profiles.get(app_id, {}).get("ac_separate"))
                ac = _target_for_preset(
                    state, spl, sppt, fppt, preset_name or p.get("ac_preset", ""))
                if ac_separate and not p.get("ac_separate"):
                    cpu = _effective_cpu_values(state, p)
                    for field, value in cpu.items():
                        if p.get("ac_" + field) is None:
                            p["ac_" + field] = value
                p.update({"ac_separate": ac_separate,
                          "ac_spl": ac[0], "ac_sppt": ac[1], "ac_fppt": ac[2]})
                if preset_name:
                    p["ac_preset"] = preset_name
                profiles[app_id] = p

                want = None
                if not stored and _get_ac_online():
                    if ac_separate:
                        want = ac
                    elif all(p.get(k) is not None for k in ("spl", "sppt", "fppt")):
                        want = _target_for_preset(
                            state, p["spl"], p["sppt"], p["fppt"], p.get("preset", ""))
                if want is not None:
                    result = _apply_active_target(
                        state, _effective_cpu_state(state, profiles, app_id), want)
                    if not result["success"]:
                        return restore_failed(result)

                _write_keys({SETTINGS_KEY_SETTINGS: state,
                             SETTINGS_KEY_GAME_PROFILES: profiles})
                event("tdp", "ac_profile_saved", scope="game_ac", enabled=ac_separate)
                return _ok()
        return await _offload(_do)

    async def delete_game_profile(self, app_id: str) -> dict:
        app_id = normalise_app_id(app_id)
        if app_id is None:
            return _fail("invalid app id")
        def _do() -> dict:
            with _tdp_user_transaction() as restore_failed:
                profiles, inactive = _load_profiles(), _load_inactive_profiles()
                profiles.pop(app_id, None)
                inactive.pop(app_id, None)
                state = _load_settings()
                if state.get("enabled", True) and _get_running_appid() == app_id:
                    target = _global_triplet(state)
                    result = _apply_active_target(
                        state, _effective_cpu_state(state, profiles, app_id), target)
                    if not result["success"]:
                        return restore_failed(result)
                _write_keys({SETTINGS_KEY_SETTINGS: state,
                             SETTINGS_KEY_GAME_PROFILES: profiles,
                             SETTINGS_KEY_INACTIVE_GAME_PROFILES: inactive})
                return _ok()
        result = await _offload(_do)
        event("tdp", "profile_deleted", scope="game",
              level="info" if result["success"] else "warning",
              outcome="confirmed" if result["success"] else "failed")
        return result

    async def set_game_profile_active(self, app_id: str, active: bool) -> dict:
        """Use a game's own profile, or keep it unused so the game follows Global.

        Turning a profile off moves it, unchanged, to the inactive key; turning it
        on moves it back, or starts one from the global settings.
        """
        app_id = normalise_app_id(app_id)
        if app_id is None or type(active) is not bool:
            return _fail("invalid profile request")
        def _do() -> dict:
            with _tdp_user_transaction() as restore_failed:
                state = _load_settings()
                profiles, inactive = _load_profiles(), _load_inactive_profiles()
                if not active:
                    if app_id in profiles:
                        inactive[app_id] = profiles.pop(app_id)
                elif app_id in inactive:
                    profile = inactive.pop(app_id)
                    if app_id not in profiles:
                        # 50 W may have been turned off while this profile was unused.
                        if not state.get("extras_unlocked", False):
                            _clamp_profile(profile, _standard_ceilings_mw())
                        profiles[app_id] = profile
                else:
                    _prepare_cpu_profile(state, profiles, app_id)
                if state.get("enabled", True) and _get_running_appid() == app_id:
                    profile = profiles.get(app_id)
                    target = (_profile_target(state, profile, _get_ac_online())
                              if profile is not None else _global_triplet(state))
                    result = _apply_active_target(
                        state, _effective_cpu_state(state, profiles, app_id), target)
                    if not result["success"]:
                        return restore_failed(result)
                _write_keys({SETTINGS_KEY_SETTINGS: state,
                             SETTINGS_KEY_GAME_PROFILES: profiles,
                             SETTINGS_KEY_INACTIVE_GAME_PROFILES: inactive})
                return _ok()
        result = await _offload(_do)
        event("tdp", "profile_active" if active else "profile_inactive", scope="game",
              level="info" if result["success"] else "warning",
              outcome="confirmed" if result["success"] else "failed")
        return result

    async def set_plugin_enabled(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            return _fail("enabled must be a boolean")
        def _do() -> dict:
            with _tdp_user_transaction() as restore_failed:
                state = _load_settings()
                if not enabled:
                    result = _restore_defaults_locked()
                    if not result["success"]:
                        return restore_failed(result)
                    state["enabled"] = False
                    result["cpu_power_errors"] = _best_effort_reapply_saved_cpu_power_locked(state)
                    _save_settings(state)
                    _cancel_ac_settle()
                    return restore_failed(result)

                state["enabled"] = True
                _, target, _ = _startup_context_target(state)
                result = _apply_limits_with_saved_cpu_power(state, *target)
                if not result["success"]:
                    return restore_failed(result)
                state["active_spl"], state["active_sppt"], state["active_fppt"] = target
                _save_settings(state)
                _cancel_ac_settle()
                return restore_failed(result)
        result = await _offload(_do)
        decky.logger.info(f"[legotdp] Plugin enabled={enabled} success={result['success']}")
        return result

    async def get_caps(self) -> dict:
        """Slider ceilings in watts. `std` is what the firmware accepts over WMI;
        `max` is the Extras range, which falls through to ryzenadj."""
        def _do() -> dict:
            supported = _legion_go_2()
            caps = _wmi_caps() if supported else {}
            std = {k: caps[k]["max"] for k in WMI_ATTRS} if caps else dict(FALLBACK_STD_W)
            firmware_available = bool(caps) and _profile_path() is not None
            helper_available = (supported and _ryzenadj_available
                                and os.path.isfile(BIN_PATH))
            available = firmware_available or helper_available
            error = ""
            if not supported:
                error = UNSUPPORTED_MODEL
            elif not available:
                error = "Firmware TDP controls are unavailable and verified RyzenAdj is unavailable"
                if self._helper_error:
                    error += f": {self._helper_error}"
            return {
                "min": min(caps[k]["min"] for k in WMI_ATTRS) if caps else HARD_MIN_MW // 1000,
                "std": std,
                # Until the helper is verified the two ranges are the same.
                "max": dict(std) if not helper_available
                       else dict(std, sppt=HARD_MAX_MW // 1000, fppt=HARD_MAX_MW // 1000),
                "wmi": bool(caps),
                "extras": helper_available,
                "tdp_available": available,
                "tdp_error": error,
                # Enabling Extras (or this retry) downloads the verified helper.
                "helper_retry_available": supported and not helper_available,
                "presets": PRESETS_DEFAULT,
            }
        return await _offload(_do)

    async def set_panel_active(self, active: bool) -> None:
        """Renew (or drop) the panel's lease on the info loop."""
        global _panel_active, _panel_active_ts
        if type(active) is not bool:
            raise ValueError("active must be a boolean")
        _panel_active = active
        _panel_active_ts = time.monotonic() if active else 0.0

    async def reapply(self) -> dict:
        """Force the saved limits back; the frontend calls it on resume (no backend hook)."""
        def _do() -> dict:
            with _mutation_lock:
                s = _load_settings()
                if not s.get("enabled", True):
                    return {"success": True, "skipped": True,
                            "cpu_power_errors": _best_effort_reapply_saved_cpu_power_locked(s)}
                # Whatever was cached describes the pre-suspend hardware.
                _invalidate_limits_cache()
                _, target, _ = _startup_context_target(s)
                result = _apply_limits_with_saved_cpu_power(s, *target)
                if result["success"]:
                    _save_active(s, *target)
                    _cancel_ac_settle()
                decky.logger.info(
                    f"[legotdp] reapply after resume: success={result['success']}")
                return {"success": result["success"], "skipped": False,
                        "stderr": result.get("stderr", ""),
                        "cpu_power_errors": result.get(
                            "cpu_power_errors", [])}
        return await _offload(_do)

    async def set_active_app(self, app_id: str) -> None:
        """Frontend reports the authoritative running-game appid (or '' for none)."""
        global _frontend_appid, _frontend_appid_ts
        normalised = normalise_app_id(app_id, allow_empty=True)
        if normalised is None:
            raise ValueError("invalid app id")
        _frontend_appid = normalised
        _frontend_appid_ts = time.monotonic()
        # A switch is applied now rather than on the next five-second tick. The
        # pass re-checks the game under _mutation_lock, so a racing tick is a no-op.
        if self._ready and _get_running_appid() != _current_game_id:
            await self._enforce_pass()

    async def get_tdp_info(self) -> dict:
        if not self._ready:
            return {"success": False, "values": {}, "error": "not ready"}
        with _info_cache_lock:
            age = time.monotonic() - _info_cache_ts if _info_cache_ts > 0 else None
            return {"success": True,
                    "values": dict(_info_cache) if age is not None and 0 <= age <= 6 else {},
                    "sample_age_seconds": age}

    async def apply_tdp(self, spl: int, sppt: int, fppt: int, app_id: str = "",
                        preset_name: str = "",
                        expected_app_id: str | None = None, stored: bool = False) -> dict:
        if not self._ready:
            return _fail("not ready")
        app_id = normalise_app_id(app_id, allow_empty=True)
        expected_was_supplied = expected_app_id is not None
        expected_app_id = (
            normalise_app_id(expected_app_id, allow_empty=True) if expected_was_supplied else None)
        if (app_id is None or (expected_was_supplied and expected_app_id is None)
                or type(stored) is not bool or (stored and not app_id)
                or any(type(value) is not int for value in (spl, sppt, fppt))):
            return _fail("invalid TDP request")
        preset_name = _safe_label(preset_name)

        def _do() -> dict:
            with _mutation_lock:
                s = _load_settings()
                if not s.get("enabled", True):
                    return _fail("plugin disabled")
                current_app_id = _get_running_appid()
                refusal = _scope_refusal(app_id, current_app_id, expected_app_id, stored)
                if refusal:
                    return _fail(refusal)
                if stored:
                    return _save_stored_tdp_locked(s, app_id, spl, sppt, fppt, preset_name)
                if (not app_id and current_app_id
                        and current_app_id in _load_profiles()):
                    return _save_inactive_global_tdp_locked(s, spl, sppt, fppt, preset_name)
                return _apply_tdp_locked(s, app_id, spl, sppt, fppt, preset_name)
        return await _offload(_do)

    async def _push_info(self) -> bool:
        """One refresh, pushed to the panel. True when something went out."""
        if not _panel_is_active():
            return False
        await _offload(_refresh_info_cache)
        with _info_cache_lock:
            values = dict(_info_cache)
        await decky.emit("tdp_info", {"success": True, "values": values})
        return True

    async def _info_loop(self):
        while True:
            await asyncio.sleep(2)
            try:
                published = await self._push_info()
                if published:transition('tdp','info_stream','available',outcome='published',initial=False)
            except Exception as e:
                transition('tdp','info_stream','unavailable',level='warning',error=e,outcome='unavailable')

    async def _resettle_after_ac(self, generation: int):
        """Re-assert the target after a charger transition (see AC_SETTLE_DELAYS_S)."""
        for delay in AC_SETTLE_DELAYS_S:
            await asyncio.sleep(delay)
            try:
                settled = await _offload(_reapply_current_target, generation)
            except Exception as e:
                event('tdp','ac_settle_failed',level='warning',error=e,repeat=60)
                return
            if settled:
                return

    async def _enforce_pass(self):
        try:
            events = await _offload(_check_and_enforce)
            generation = events.pop("_resettle_generation", None)
            if generation is not None:
                # Deliberately not awaited: the ladder runs for several
                # seconds and the enforce loop has its own schedule to keep.
                task = asyncio.create_task(self._resettle_after_ac(generation))
                self._tasks.append(task)
                task.add_done_callback(
                    lambda done: self._tasks.remove(done) if done in self._tasks else None)
            for event, payload in events.items():
                await decky.emit(event, payload)
            transition('tdp','enforcement_loop','running',outcome='cycle_complete',initial=False)
        except Exception as e:
            transition('tdp','enforcement_loop','unavailable',level='warning',error=e,outcome='unavailable')

    async def _enforce_loop(self):
        while True:
            await asyncio.sleep(5)
            await self._enforce_pass()

    async def _migration(self):
        """Prepare the store before anything reads it; runs before _main().

        Nothing may escape: a raise here stops the loader before the RPC socket
        exists, leaving the panel on "Initializing"; is_ready() reports it instead.
        """
        try:
            await _offload(_migrate)
        except Exception as e:
            Plugin._setup_error = f"settings migration failed: {e}"
            event('tdp','migration_failed',level='error',error=e)

    async def _main(self):
        self._helper_cancel = threading.Event()
        self._helper_error = ""
        try:
            global _current_ac_online, _ryzenadj_available
            Plugin._ready = False
            Plugin._setup_error = None
            if not await _offload(_legion_go_2):
                # Reported through is_ready(); no loop starts, nothing is written.
                Plugin._setup_error = UNSUPPORTED_MODEL
                event('tdp', 'unsupported_model', level='warning')
                return
            # Resolve the trust store now so the log states up front whether downloads
            # and update checks will be able to verify certificates.
            await _offload(updater.ssl_context)
            # Seed this so the first enforce pass does not report a phantom AC change.
            _current_ac_online = await _offload(_get_ac_online)
            # Establish the suspend-clock baseline before the workers start, so
            # even an early sleep/wake cycle is detected by their first pass.
            _resume_detected.mark()
            wmi = await _offload(_wmi_caps)
            try:
                # Verify a helper left by an earlier Extras opt-in; never download here.
                _ryzenadj_available = bool(await _offload(lambda: _ensure_ryzenadj(download=False)))
            except Exception as e:
                _ryzenadj_available = False
                self._helper_error = str(e)
                transition('tdp','helper_ready',False,level='warning',error=e,outcome='deferred',retryable=True)
            def _apply_saved() -> dict:
                global _current_game_id, _last_cpu_power_check
                with _mutation_lock:
                    if module_runtime.stop_requested("tdp"):
                        return None  # A stop came during the slower steps above: apply nothing.
                    s = _load_settings()
                    # Runtime ceilings already account for unavailable RyzenAdj.
                    # Preserve user profiles so a later online start can restore them.
                    app_id, target, using_profile = _startup_context_target(s)
                    _current_game_id = "\x00"
                    if not s.get("enabled", True):
                        cpu_power_errors = _best_effort_reapply_saved_cpu_power_locked(s)
                        _last_cpu_power_check = time.monotonic()
                        return {"success": True, "skipped": True,
                                "cpu_power_errors": cpu_power_errors}
                    result = _apply_limits_with_saved_cpu_power(s, *target, reason="startup")
                    _last_cpu_power_check = time.monotonic()
                    if result["success"]:
                        _save_active(s, *target)
                        _current_game_id = app_id
                        decky.logger.info(
                            "[legotdp] startup target: "
                            + ("game profile" if using_profile
                               else "global profile"))
                    return result
            startup_apply = await _offload(_apply_saved)
            if startup_apply is None:
                return
            if not startup_apply.get("success", False):
                event('tdp','startup_limits_unconfirmed',level='warning',reason='hardware_unconfirmed',returncode=startup_apply.get('returncode'))
            if startup_apply.get('cpu_power_errors'):
                event('tdp','startup_cpu_unconfirmed',level='warning',reason='hardware_unconfirmed',count=len(startup_apply['cpu_power_errors']))

            Plugin._ready = True
            # Keep references - a bare create_task() may be garbage-collected mid-run.
            self._tasks = [
                asyncio.create_task(self._enforce_loop()),
                asyncio.create_task(self._info_loop()),
            ]
            decky.logger.info(
                f"[legotdp] ready (wmi={'yes' if wmi else 'no'}, "
                f"ryzenadj={'yes' if _ryzenadj_available else 'no'})")
        except module_runtime.StartStopped:
            return  # Like the stop check in _apply_saved: the stop ends the start, not an error.
        except Exception as e:
            Plugin._setup_error = str(e)
            event('tdp','startup_failed',level='error',error=e)
            raise

    async def _unload(self):
        self._helper_cancel.set()
        Plugin._ready = False
        def _invalidate_ac_ladder() -> None:
            with _mutation_lock:
                _cancel_ac_settle()
        await _offload(_invalidate_ac_ladder)
        for t in self._tasks:
            t.cancel()
        # Bounded: a cancelled executor task keeps its thread, and the loader SIGKILLs
        # after 5 s, which would skip _uninstall() and leave the profile on 'custom'.
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=1.0)
        self._tasks = []
        decky.logger.info("[legotdp] unloaded")

    async def _uninstall(self):
        """Hand the hardware back, else the firmware stays on the last 'custom' triplet.

        Settings are left on disk on purpose so a reinstall still finds them.
        """
        def _do() -> None:
            # An in-flight enforce pass may still hold this lock; wait so it cannot re-assert.
            with _mutation_lock:
                module_runtime.require_success(_release_controls_locked())
        await _offload(_do)
        decky.logger.info("[legotdp] uninstalled")

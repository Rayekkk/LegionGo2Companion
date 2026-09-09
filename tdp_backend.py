# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/LeGoTDP

import decky
from system_process import system_env
import module_runtime
import asyncio
import copy
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
# name before we are imported, and sys.modules wins over sys.path. Keep this
# backend-specific helper name for version/TLS compatibility.
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
# Standalone plugin update paths are intentionally not part of Companion.
updater = Updater(
    user_agent="LeGoTDP",
    log_prefix="[legotdp]",
    plugin_dir=PLUGIN_DIR,
    logger=decky.logger,
)

# Reported by get_caps() when the firmware does not answer. The frontend carries
# the same numbers as FALLBACK_STD for the moments before get_caps() returns.
FALLBACK_STD_W = {"spl": 35, "sppt": 37, "fppt": 45}

# Absolute floor/ceiling for any single limit, in milliwatts. Applied on load, which
# also migrates profiles saved back when the Extras ceiling was 60 W.
HARD_MIN_MW = 5000
HARD_MAX_MW = 50000

# Devices driven through the firmware alone. The plugin downloads ryzenadj
# itself, and on these it simply does not: the firmware range is the whole
# range that is wanted here, so there is nothing for a second tool to add. The
# sliders stop at what the firmware reports rather than at the Extras ceiling.
#
# Matched on DMI product_family, which names the family rather than the SKU -
# a Legion Go S reports "Legion Go S 8APU1" whatever the model number on the
# box. Anything that does not match is left on the path it has always taken,
# so a device that is not listed here behaves exactly as it did before.
WMI_ONLY_FAMILIES = ("legion go s",)

# Preset ladders in watts. They are spaced against what each machine's firmware
# actually accepts, so the top of the ladder is the top of the hardware rather
# than a number carried over from a different device. Served to the panel so
# there is one place to change them.
PRESETS_DEFAULT = {
    "minimum":     {"spl": 5,  "sppt": 5,  "fppt": 10},
    "silent":      {"spl": 8,  "sppt": 10, "fppt": 15},
    "balanced":    {"spl": 15, "sppt": 18, "fppt": 25},
    "performance": {"spl": 25, "sppt": 28, "fppt": 35},
    "max":         {"spl": 35, "sppt": 37, "fppt": 45},
}

# Legion Go S: 40 / 43 / 53 W is exactly what its firmware reports as the
# ceiling, so Max asks for all of it.
PRESETS_LEGION_GO_S = {
    "minimum":     {"spl": 5,  "sppt": 8,  "fppt": 10},
    "silent":      {"spl": 8,  "sppt": 10, "fppt": 15},
    "balanced":    {"spl": 18, "sppt": 20, "fppt": 25},
    "performance": {"spl": 33, "sppt": 33, "fppt": 35},
    "max":         {"spl": 40, "sppt": 43, "fppt": 53},
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

# Runtime capability, not merely device capability. A Go 2 still has a working
# WMI range when the download fails, but it must not advertise the Extras range
# until the executable actually exists and passed its integrity check.
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
_last_suspend_offset: float | None = None
APP_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_GAME_PROFILES = 512
MAX_LABEL_LENGTH = 96

# The panel says when it is on screen, and re-says it every 30 s while it stays
# there. The timestamp is what makes that a lease rather than a latch: a
# frontend that goes away without running its cleanup - a Steam UI restart, say
# - used to leave the info loop reading RAPL every two seconds, and spawning
# `ryzenadj --info` every fifteen with Extras on, for the rest of the session.
# _frontend_appid below is leased the same way for the same reason.
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

_wmi_only_cache: bool | None = None


def _dmi(field: str) -> str:
    try:
        with open(f"/sys/class/dmi/id/{field}") as f:
            return f.read().strip()
    except OSError:
        return ""


def _wmi_only() -> bool:
    """True on hardware this plugin drives through the firmware alone.

    Read once: DMI does not change while the machine is running.
    """
    global _wmi_only_cache
    if _wmi_only_cache is None:
        # Each field is matched on its own. Joining them first would let a name
        # straddle the seam - "Legion Go" followed by a version starting with
        # "S" would read as "legion go s" and take a Go 2 down the wrong path.
        fields = [_dmi(f).lower()
                  for f in ("product_family", "product_version", "product_name")]
        _wmi_only_cache = any(
            name in field for field in fields for name in WMI_ONLY_FAMILIES)
        if _wmi_only_cache:
            decky.logger.info(
                f"[legotdp] {_dmi('product_family') or _dmi('product_name')}: "
                "firmware interface only, Extras range unavailable")
    return _wmi_only_cache


def _presets() -> dict:
    """The preset ladder for the hardware in front of us."""
    return PRESETS_LEGION_GO_S if _wmi_only() else PRESETS_DEFAULT


def _defaults() -> dict:
    """Where a fresh install starts, in milliwatts.

    Read from the ladder rather than written out again, because Balanced is not
    the same everywhere - 15 / 18 / 25 W on a Go 2, 18 / 20 / 25 W on a Go S.
    Hard-coding one of them meant a fresh install on the other machine opened on
    numbers belonging to a different device, and disagreed with the preset the
    panel was highlighting at the same moment.
    """
    balanced = _presets()["balanced"]
    return {"spl":  balanced["spl"]  * 1000,
            "sppt": balanced["sppt"] * 1000,
            "fppt": balanced["fppt"] * 1000,
            "enabled": True,
            "cpu_boost_enabled": None,
            "epp": None}


def _ceilings_mw() -> tuple[int, int, int]:
    """(spl, sppt, fppt) ceilings in milliwatts.

    One per parameter, because the firmware does not use the same limit for all
    three - a Legion Go S reports 40 / 43 / 53 W. Everywhere else this stays the
    Extras ceiling, which ryzenadj reaches.

    A firmware that will not answer keeps the old bound rather than collapsing
    the sliders: better to leave them where they were than to silently cap
    somebody at the minimum.
    """
    if not _wmi_only():
        return HARD_MAX_MW, HARD_MAX_MW, HARD_MAX_MW
    caps = _wmi_caps()
    if not caps:
        return HARD_MAX_MW, HARD_MAX_MW, HARD_MAX_MW
    return tuple(caps[k]["max"] * 1000 for k in ("spl", "sppt", "fppt"))


def _standard_ceilings_mw() -> tuple[int, int, int]:
    """Firmware ceilings in milliwatts, with the documented safe fallback."""
    caps = _wmi_caps()
    if caps:
        return tuple(caps[k]["max"] * 1000 for k in ("spl", "sppt", "fppt"))
    return tuple(FALLBACK_STD_W[k] * 1000 for k in ("spl", "sppt", "fppt"))


def _allowed_ceilings_mw(state: dict) -> tuple[int, int, int]:
    """Ceilings the current persisted mode is allowed to apply."""
    if _wmi_only():
        return _ceilings_mw()
    if state.get("extras_unlocked", False) and _ryzenadj_available:
        return HARD_MAX_MW, HARD_MAX_MW, HARD_MAX_MW
    return _standard_ceilings_mw()


# Plugging the charger in makes the firmware apply a profile of its own, and it
# lands after ours: measured on a Legion Go S, an apply at the moment of the
# transition wrote 40/43/53 and the attributes read back 10/15/20 - the
# low-power defaults - a fraction of a second later. One write at the instant
# the state changes is simply too early, so the values go back several times
# over the seconds that follow, until one of them is the last word.
#
# Unplugging does not need this; it is included anyway because it costs one
# comparison and the firmware is free to grow the same behaviour there.
AC_SETTLE_DELAYS_S = (0.5, 1.5, 3.0, 6.0)

# What the last charger transition asked for. The ladder re-asserts this rather
# than the recorded active_* triplet, because a failed apply records nothing -
# and a failure is precisely when the ladder is needed. Reading active_* there
# would put back whatever ran before the transition, which with a per-game AC
# profile is the wrong half of it.
_ac_target: tuple = ()
# Every delayed charger ladder carries the generation that armed it. Any newer
# user action, game change or charger transition increments this and makes the
# old ladder a no-op before it reaches hardware.
_ac_generation: int = 0


# ── AC power detection ─────────────────────────────────────────────────────────

def _read_sysfs(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


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
        if _read_sysfs(os.path.join(path, "online")) == "1":
            return True
    if mains_seen:
        return False
    # No Mains supply exposed at all - fall back to battery status.
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


def _normalise_app_id(value, *, allow_empty: bool = True) -> str | None:
    if value in (None, "") and allow_empty:
        return ""
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or APP_ID_RE.fullmatch(value) is None:
        return None
    return value


def _safe_label(value) -> str:
    return value[:MAX_LABEL_LENGTH] if isinstance(value, str) else ""


# ── Persistence ────────────────────────────────────────────────────────────────

# Settings live in Decky's settings directory, not in the plugin directory. The
# plugin directory is wiped by every reinstall, and this plugin's own updater
# tells the user to uninstall before installing the new zip - which used to take
# the global settings and every per-game profile with it.
settings = SettingsManager(
    name="tdp_settings",
    settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR,
)

SETTINGS_KEY_SETTINGS      = "settings"
SETTINGS_KEY_GAME_PROFILES = "game_profiles"
SETTINGS_KEY_SCHEMA        = "schema_version"
CURRENT_SCHEMA             = 2
SETTINGS_FIELD_CPU_BOOST_ENABLED = "cpu_boost_enabled"
SETTINGS_FIELD_EPP = "epp"

# Pre-schema-2 locations, inside the plugin directory. Read once by _migrate()
# and never written again.
LEGACY_SETTINGS_FILE = os.path.join(PLUGIN_DIR, "settings.json")
LEGACY_PROFILES_FILE = os.path.join(PLUGIN_DIR, "profiles.json")

# The enforce loop reads settings from an executor thread while RPC handlers
# write them from the event loop. Re-entrant because the write paths load first.
_settings_lock = threading.RLock()


async def _offload(fn, *args):
    """Run blocking work off the event loop.

    Settings I/O, sysfs reads and waiting on _settings_lock or _apply_lock all
    block. Decky gives each plugin its own process and loop, so blocking here
    does not stall other plugins - it stalls this one: every RPC the panel sends
    queues behind it, and neither the enforce loop nor the info loop ticks until
    it returns. _apply_lock alone can be held for a profile bounce plus three
    firmware writes.
    """
    return await module_runtime.offload('tdp', fn, *args)


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
    spl_max, sppt_max, fppt_max = ceilings or _ceilings_mw()
    spl  = max(HARD_MIN_MW, min(spl,  spl_max))
    fppt = max(spl,         min(fppt, fppt_max))
    sppt = max(spl,         min(sppt, min(sppt_max, fppt)))
    return spl, sppt, fppt


def _clamp_for_settings(state: dict, spl, sppt, fppt) -> tuple[int, int, int]:
    """Clamp a target to the range currently unlocked and actually available."""
    return _clamp_triplet(spl, sppt, fppt, _allowed_ceilings_mw(state))


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
    boost = s.get(SETTINGS_FIELD_CPU_BOOST_ENABLED)
    s[SETTINGS_FIELD_CPU_BOOST_ENABLED] = (
        boost if type(boost) is bool else None)
    s[SETTINGS_FIELD_EPP] = _canonical_epp_syntax(
        s.get(SETTINGS_FIELD_EPP))
    s["spl"], s["sppt"], s["fppt"] = _clamp_triplet(
        s.get("spl",  _defaults()["spl"]),
        s.get("sppt", _defaults()["sppt"]),
        s.get("fppt", _defaults()["fppt"]),
    )
    if any(k in s for k in ("active_spl", "active_sppt", "active_fppt")):
        s["active_spl"], s["active_sppt"], s["active_fppt"] = _clamp_triplet(
            s.get("active_spl",  s["spl"]),
            s.get("active_sppt", s["sppt"]),
            s.get("active_fppt", s["fppt"]),
        )
    s["active_preset"] = _safe_label(s.get("active_preset"))
    return s


def _save_settings(s: dict) -> None:
    _write_key(SETTINGS_KEY_SETTINGS, s)


# ── Per-game profiles ──────────────────────────────────────────────────────────

def _load_profiles() -> dict:
    raw_profiles = _read_key(SETTINGS_KEY_GAME_PROFILES, {})
    profiles: dict[str, dict] = {}
    for raw_app_id, raw_profile in raw_profiles.items():
        app_id = _normalise_app_id(raw_app_id, allow_empty=False)
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
                p["spl"], p.get("sppt", p["spl"]), p.get("fppt", p["spl"]))
        if p.get("ac_spl") is not None:
            p["ac_spl"], p["ac_sppt"], p["ac_fppt"] = _clamp_triplet(
                p["ac_spl"], p.get("ac_sppt", p["ac_spl"]), p.get("ac_fppt", p["ac_spl"]))
        profiles[app_id] = p
        if len(profiles) >= MAX_GAME_PROFILES:
            break
    return profiles


def _save_profiles(profiles: dict) -> None:
    _write_key(SETTINGS_KEY_GAME_PROFILES, profiles)


def _effective_cpu_values(
    state: dict, profile: dict | None = None, ac_profile: bool = False
) -> dict:
    """Resolve legacy omissions without altering existing saved profiles."""
    values = {
        SETTINGS_FIELD_CPU_BOOST_ENABLED: (
            state.get(SETTINGS_FIELD_CPU_BOOST_ENABLED)
            if type(state.get(SETTINGS_FIELD_CPU_BOOST_ENABLED)) is bool else None),
        SETTINGS_FIELD_EPP: _canonical_epp_syntax(state.get(SETTINGS_FIELD_EPP)),
    }
    if profile is not None:
        for prefix in (("", "ac_") if ac_profile else ("",)):
            boost = profile.get(prefix + SETTINGS_FIELD_CPU_BOOST_ENABLED)
            if type(boost) is bool:
                values[SETTINGS_FIELD_CPU_BOOST_ENABLED] = boost
            epp = _canonical_epp_syntax(profile.get(prefix + SETTINGS_FIELD_EPP))
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
    if creating and len(profiles) >= MAX_GAME_PROFILES:
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


def _read_legacy(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _migrate() -> None:
    """Fold the pre-1.5.0 files in the plugin directory into Decky's store.

    Runs exactly once. The old files are left on disk untouched: they disappear
    with the next reinstall anyway, and leaving them means a downgrade still
    finds its settings.
    """
    with _settings_lock:
        _read_valid_settings()
        try:
            schema = int(settings.getSetting(SETTINGS_KEY_SCHEMA, 1))
        except (TypeError, ValueError):
            schema = 1
        if schema >= CURRENT_SCHEMA:
            return

        legacy_settings = _read_legacy(LEGACY_SETTINGS_FILE)
        legacy_profiles = _read_legacy(LEGACY_PROFILES_FILE)

        if legacy_settings and settings.getSetting(SETTINGS_KEY_SETTINGS, None) is None:
            settings.setSetting(SETTINGS_KEY_SETTINGS, legacy_settings)
            decky.logger.info(
                f"[legotdp] migrated {LEGACY_SETTINGS_FILE} into the Decky settings store")
        if legacy_profiles and settings.getSetting(SETTINGS_KEY_GAME_PROFILES, None) is None:
            settings.setSetting(SETTINGS_KEY_GAME_PROFILES, legacy_profiles)
            decky.logger.info(
                f"[legotdp] migrated {len(legacy_profiles)} per-game profile(s) "
                f"from {LEGACY_PROFILES_FILE}")

        settings.setSetting(SETTINGS_KEY_SCHEMA, CURRENT_SCHEMA)
        settings.commit()


# ── ryzenadj binary ────────────────────────────────────────────────────────────

def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_ryzenadj() -> None:
    decky.logger.info(f"[legotdp] Downloading ryzenadj from {RYZENADJ_URL}")
    os.makedirs(BIN_DIR, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz")
    os.close(tmp_fd)
    bin_tmp = ""
    try:
        with open(tmp_path, "wb") as out:
            updater.download_to(RYZENADJ_URL, out, timeout=30)

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
        os.replace(bin_tmp, BIN_PATH)
        bin_tmp = ""
        decky.logger.info(f"[legotdp] ryzenadj installed at {BIN_PATH}")
    finally:
        for path in (tmp_path, bin_tmp):
            if not path:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass


def _ensure_ryzenadj() -> None:
    if not os.path.isfile(BIN_PATH) or _sha256_file(BIN_PATH) != RYZENADJ_BINARY_SHA256:
        if os.path.isfile(BIN_PATH):
            decky.logger.warning(
                "[legotdp] existing ryzenadj failed integrity verification; replacing it")
            os.unlink(BIN_PATH)
        _download_ryzenadj()
    mode = os.stat(BIN_PATH).st_mode
    if not (mode & stat.S_IXUSR):
        os.chmod(BIN_PATH, mode | 0o111)


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
        decky.logger.warning(f"[legotdp] ryzenadj timed out: {args}")
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
        return {"success": False, "stdout": "", "stderr": "ryzenadj busy", "returncode": -1}
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
        with open(_wmi_path(key, leaf)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _wmi_caps() -> dict:
    """Firmware-reported {min,max} in watts per parameter, or {} when unavailable."""
    caps = {}
    for key in WMI_ATTRS:
        lo, hi = _wmi_read(key, "min_value"), _wmi_read(key, "max_value")
        if lo is None or hi is None:
            return {}
        caps[key] = {"min": lo, "max": hi}
    return caps


def _profile_path() -> str | None:
    """The platform-profile node whose choices include 'custom' (the tunable one)."""
    for path in glob.glob(PLATFORM_PROFILE_GLOB):
        try:
            with open(os.path.join(os.path.dirname(path), "choices")) as f:
                if "custom" in f.read().split():
                    return path
        except OSError:
            continue
    return None


def _read_profile(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_profile(path: str, value: str) -> bool:
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except OSError:
        return False


def _write_ppt(spl_w: int, sppt_w: int, fppt_w: int) -> None:
    for key, val in (("spl", spl_w), ("sppt", sppt_w), ("fppt", fppt_w)):
        try:
            with open(_wmi_path(key, "current_value"), "w") as f:
                f.write(str(val))
        except OSError:
            pass


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
        return {"success": False, "stdout": "", "stderr": "no custom platform profile",
                "returncode": -1}

    # Fast path: if we can latch a write in place, skip the visible profile bounce.
    if _read_profile(path) == "custom":
        _write_ppt(spl_w, sppt_w, fppt_w)
        if _ppt_matches(spl_w, sppt_w, fppt_w):
            decky.logger.info(f"[legotdp] wmi apply {spl_w}W/{sppt_w}W/{fppt_w}W")
            return {"success": True, "stdout": "", "stderr": "", "returncode": 0}

    # Force a real transition into custom, then write. Bounce via a low profile so the
    # momentary blip is downward, never a spike.
    bounce = "low-power"
    try:
        with open(os.path.join(os.path.dirname(path), "choices")) as f:
            choices = f.read().split()
        bounce = next((c for c in ("low-power", "balanced", "performance") if c in choices),
                      next((c for c in choices if c != "custom"), "custom"))
    except OSError:
        pass
    _write_profile(path, bounce)
    if not _write_profile(path, "custom"):
        return {"success": False, "stdout": "", "stderr": "cannot select custom profile",
                "returncode": -1}
    _write_ppt(spl_w, sppt_w, fppt_w)

    if not _ppt_matches(spl_w, sppt_w, fppt_w):
        mismatch = "; ".join(
            f"{WMI_ATTRS[k]}={_wmi_read(k, 'current_value')} want {v}"
            for k, v in (("spl", spl_w), ("sppt", sppt_w), ("fppt", fppt_w))
            if _wmi_read(k, "current_value") != v)
        return {"success": False, "stdout": "", "stderr": mismatch, "returncode": -1}
    decky.logger.info(f"[legotdp] wmi apply {spl_w}W/{sppt_w}W/{fppt_w}W (via bounce)")
    return {"success": True, "stdout": "", "stderr": "", "returncode": 0}


# ── RAPL package power ─────────────────────────────────────────────────────────

# None = never probed, "" = probed and not found. A miss is retried: powercap can
# register after the plugin starts, and remembering the failure forever left the
# package draw reading blank until the plugin was reloaded.
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
# Needed because one of the three is not readable back on the ryzenadj path -
# see _adopt_unreadable_spl().
_applied_mw: tuple = ()

# When that happened, so the enforce pass can tell whether the info cache it is
# about to read predates the change it is checking.
_applied_at: float = 0.0


def _apply_limits(spl_mw: int, sppt_mw: int, fppt_mw: int) -> dict:
    """Prefer the firmware path; fall back to ryzenadj only when the request exceeds
    what the firmware accepts (the Extras range)."""
    global _last_source, _applied_mw, _applied_at
    spl_mw, sppt_mw, fppt_mw = _clamp_triplet(spl_mw, sppt_mw, fppt_mw)
    triple_w = (("spl", spl_mw // 1000), ("sppt", sppt_mw // 1000), ("fppt", fppt_mw // 1000))
    if not _apply_lock.acquire(timeout=8.0):
        return {"success": False, "stdout": "", "stderr": "apply busy", "returncode": -1}
    try:
        caps = _wmi_caps()
        if caps and all(caps[k]["min"] <= v <= caps[k]["max"] for k, v in triple_w):
            result = _apply_wmi(*(v for _, v in triple_w))
            if result["success"]:
                _last_source = "wmi"
                _applied_mw, _applied_at = (spl_mw, sppt_mw, fppt_mw), time.monotonic()
                _invalidate_limits_cache()
                return result
            if _wmi_only():
                # Say what actually happened. ryzenadj is deliberately not
                # installed here, so falling through to it would only turn a
                # firmware refusal into a confusing "ryzenadj not found".
                return result
            decky.logger.warning(
                f"[legotdp] WMI apply failed ({result['stderr']}), falling back to ryzenadj")
        if _wmi_only():
            return {"success": False, "stdout": "",
                    "stderr": "requested limits are outside what the firmware accepts",
                    "returncode": -1}
        result = _apply_ryzenadj(spl_mw, sppt_mw, fppt_mw)
        if result["success"]:
            _last_source = "ryzenadj"
            _applied_mw, _applied_at = (spl_mw, sppt_mw, fppt_mw), time.monotonic()
            _invalidate_limits_cache()
        return result
    finally:
        _apply_lock.release()


# The WMI attributes are the driver's record of its own writes, not a reading of
# the hardware, so anything that moves the limits without going through that
# interface leaves them reporting stale values and the enforce pass idle.
# Measured on the device: with the plugin holding 25/30/35 through WMI, an
# external drop to 15 W left the attributes still reporting 25/30/35, the panel
# showing 25/30/35, and zero re-apply attempts.
#
# `ryzenadj --info` is a live read and does see WMI-applied slow and fast
# correctly, so it can serve as a cross-check. Sparingly: it spawns a process,
# which is the cost the limits cache exists to avoid in the first place.
_WMI_VERIFY_EVERY_S = 30.0
# None, not 0.0. time.monotonic() counts from boot, so on a machine that has
# only just started 0.0 is a timestamp a few seconds in the past rather than
# "never" - which skipped the first cross-check for the first half minute of
# uptime. Invisible on a dev box or a console that has been on for hours; CI
# runs on a freshly booted runner and caught it immediately.
_wmi_verified_at: float | None = None


def _wmi_limits_overridden(want_w: tuple) -> bool:
    """True when a live read disagrees with what the firmware claims is set.

    Only SPPT and FPPT are compared. SPL cannot be read back through ryzenadj
    on this hardware (see _adopt_unreadable_spl), and the firmware's own
    reading of it is the thing under suspicion here.
    """
    global _wmi_verified_at
    if not _ryzenadj_available or not os.path.isfile(BIN_PATH):
        return False
    now = time.monotonic()
    if _wmi_verified_at is not None and now - _wmi_verified_at < _WMI_VERIFY_EVERY_S:
        return False
    _wmi_verified_at = now

    if not _ryzenadj_lock.acquire(timeout=2.0):
        return False
    try:
        rc, out, _ = _run_ryzenadj(["--info"], timeout=3.0)
    finally:
        _ryzenadj_lock.release()
    if rc != 0:
        return False

    live = _parse_ryzenadj_output(out)
    pairs = [(live.get("sppt_limit"), want_w[1]), (live.get("fppt_limit"), want_w[2])]
    if any(v is None for v, _ in pairs):
        return False
    return any(abs(v - w) > DRIFT_TOLERANCE_WMI_W for v, w in pairs)


def _wmi_profile_lost() -> bool:
    """True when the last apply was via WMI but the platform profile is no longer
    'custom', so the ppt_* attributes still read the old values yet no longer bind.
    Something external (Steam, amd_pmf, gamezone) knocked us off custom."""
    if _last_source != "wmi":
        return False
    path = _profile_path()
    return path is not None and _read_profile(path) != "custom"


# Reading limits over WMI is three sysfs reads. On the ryzenadj path it spawns a
# process, and the enforce loop asks every five seconds whether or not anyone has
# the panel open - so with Extras enabled that was a `ryzenadj --info` every five
# seconds forever, including mid-game. Serve a recent answer instead. The window
# is well inside the ryzenadj drift tolerance (6 W), which only exists to catch a
# post-resume reset, and _apply_limits drops the cache so a change we made is
# never hidden behind it.
_LIMITS_CACHE_TTL_S = 15.0
_limits_cache: dict = {}
_limits_cache_ts: float = 0.0
_limits_cache_lock = threading.Lock()


def _invalidate_limits_cache() -> None:
    global _limits_cache, _limits_cache_ts
    with _limits_cache_lock:
        _limits_cache, _limits_cache_ts = {}, 0.0


def _adopt_unreadable_spl(parsed: dict) -> dict:
    """Replace the STAPM read-back, which does not report what we asked for.

    Measured on a Legion Go 2 (Strix Point, ryzenadj 0.19.0) by sampling for a
    minute after each change with the plugin stopped:

        set stapm=15 slow=18 fast=25  ->  STAPM settles on 25.0, held for 60 s
        set stapm=40 slow=45 fast=47  ->  STAPM settles on ~46.6, wobbling
        set stapm=50 slow=50 fast=50  ->  STAPM settles on 50.0

    STAPM LIMIT follows the fast limit, never the value handed to
    --stapm-limit, and the SMU moves it by a few hundred milliwatts while it
    manages the budget. Sampled a second after a change it is somewhere in
    transit between the old value and the new one - which is where readings
    like 34.880 and 49.746 come from, and why matching it against fppt exactly
    does not work.

    Taking the row at face value put the FPPT number in the panel's SPL row,
    and handed the enforce loop a target it could never reach: three wasted
    ryzenadj re-applies on every change before it gave up and stood down.

    SPL is therefore not observable on this layer, so report what was applied.
    Slow and fast are honoured exactly, so drift is still caught through them -
    including a post-resume reset, which moves all three. The WMI path does not
    come through here; there all three are real registers and read back exact.
    """
    if _applied_mw and "spl_limit" in parsed:
        parsed["spl_limit"] = _applied_mw[0] / 1000
    return parsed


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

    parsed = _adopt_unreadable_spl(_parse_ryzenadj_output(out)) if rc == 0 else {}
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
        if values:
            _info_cache.clear()
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
    if time.monotonic() - _frontend_appid_ts < _FRONTEND_APPID_TTL:
        return _frontend_appid
    return _scan_proc_for_appid()


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


def _scan_proc_for_appid() -> str:
    # The Steam "reaper" wrapper (reaper SteamLaunch AppId=NNNN -- ...) runs outside
    # the game's pressure-vessel/gamescope sandbox, so its cmdline is the most reliable
    # background signal. Fall back to SteamAppId in the environ.
    for path in glob.glob("/proc/*/cmdline"):
        try:
            with open(path, "rb") as f:
                for arg in f.read().split(b"\x00"):
                    if arg.startswith(b"AppId="):
                        appid = arg[len(b"AppId="):].decode(errors="replace")
                        if appid and appid != "0":
                            return appid
        except OSError:
            continue
    for path in glob.glob("/proc/*/environ"):
        try:
            with open(path, "rb") as f:
                for entry in f.read().split(b"\x00"):
                    if entry.startswith(b"SteamAppId="):
                        appid = entry[len(b"SteamAppId="):].decode(errors="replace")
                        if appid and appid != "0":
                            return appid
        except OSError:
            continue
    return ""


# ── TDP enforce ────────────────────────────────────────────────────────────────

def _global_triplet(s: dict) -> tuple[int, int, int]:
    return _clamp_for_settings(
        s,
        s.get("spl",  _defaults()["spl"]),
        s.get("sppt", _defaults()["sppt"]),
        s.get("fppt", _defaults()["fppt"]),
    )


def _startup_context_target(s: dict) -> tuple[str, tuple[int, int, int], bool]:
    """Resolve cold-start limits from current reality, never stale active_* data."""
    app_id = _get_running_appid()
    profile = _load_profiles().get(app_id) if app_id else None
    target = (
        _clamp_for_settings(s, *_pick_profile_values(profile, _get_ac_online()))
        if profile is not None else _global_triplet(s)
    )
    return app_id, target, profile is not None


def _cancel_ac_settle() -> int:
    global _ac_target, _ac_generation
    _ac_generation += 1
    _ac_target = ()
    return _ac_generation


def _arm_ac_settle(target: tuple) -> int:
    global _ac_target, _ac_generation
    _ac_generation += 1
    _ac_target = tuple(target)
    return _ac_generation


def _apply_and_record(spl: int, sppt: int, fppt: int, why: str) -> dict:
    """Apply a triplet and remember it as the target the enforce pass defends."""
    with _mutation_lock:
        state = _load_settings()
        if not state.get("enabled", True):
            return {"success": False, "stdout": "", "stderr": "plugin disabled",
                    "returncode": -1}
        target = _clamp_for_settings(state, spl, sppt, fppt)
        result = _apply_limits_with_saved_cpu_power(state, *target)
        if result["success"]:
            _save_active(state, *target)
            decky.logger.info(
                f"[legotdp] Applied {why}: "
                f"{target[0] // 1000}/{target[1] // 1000}/{target[2] // 1000} W")
        else:
            decky.logger.warning(
                f"[legotdp] Failed to apply {why}: "
                f"rc={result['returncode']} err={result['stderr']}")
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
        target = _ac_target or _clamp_for_settings(
            s,
            s.get("active_spl",  s.get("spl",  _defaults()["spl"])),
            s.get("active_sppt", s.get("sppt", _defaults()["sppt"])),
            s.get("active_fppt", s.get("fppt", _defaults()["fppt"])),
        )
        if _target_matches(target):
            _best_effort_reapply_saved_cpu_power_locked(
                s, "after charger transition settled")
            _ac_target = ()
            return True
        _apply_and_record(*target, "TDP after a charger transition")
        if _target_matches(target):
            _best_effort_reapply_saved_cpu_power_locked(
                s, "after charger transition re-apply")
            _ac_target = ()
            return True
        return False


_transition_key = None
_transition_failures = 0
_transition_retry_at = 0.0


def _transition_apply(target, why, key):
    global _transition_key, _transition_failures, _transition_retry_at
    if key != _transition_key:
        _transition_key, _transition_failures, _transition_retry_at = key, 0, 0.0
    if time.monotonic() < _transition_retry_at:
        return {"success": False}
    try:
        result = _apply_and_record(*target, why)
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
    global _current_game_id, _current_ac_online, _last_cpu_power_check, _transition_key

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
        _best_effort_reapply_saved_cpu_power_locked(
            s, "CPU control drift check")
        _last_cpu_power_check = now
    if not s.get("enabled", True):
        return {}

    appid    = _get_running_appid()
    ac_now   = _get_ac_online()
    ac_changed = ac_now != _current_ac_online
    _current_ac_online = ac_now
    events = {"power_source": {"ac": ac_now}} if ac_changed else {}

    game_changed = appid != _current_game_id

    if game_changed or ac_changed:
        prev = _current_game_id if game_changed else appid
        profile = _load_profiles().get(appid) if appid else None
        if profile is not None:
            trigger = "AC state change" if ac_changed else "game launch"
            target = _clamp_for_settings(s, *_pick_profile_values(profile, ac_now))
            if ac_changed:
                events["_resettle_generation"] = _arm_ac_settle(target)
            else:
                _cancel_ac_settle()
            result = _transition_apply(
                target, f"game profile for app={appid} on {trigger} (ac={ac_now})",
                (appid, ac_now, target))
            _current_game_id = appid if result.get("success") else "\x00"
            return events

        # Nothing per-game applies, so the global settings are what should be
        # running. Skipping this would leave the enforce pass below defending a
        # stale active_* triplet left over from whatever ran last.
        if appid:
            why = f"global TDP, app={appid} has no profile"
        elif prev:
            why = "global TDP, game exited"
        else:
            why = f"global TDP on AC change (ac={ac_now})"
        target = _global_triplet(s)
        if ac_changed:
            events["_resettle_generation"] = _arm_ac_settle(target)
        else:
            _cancel_ac_settle()
        result = _transition_apply(target, why, (appid, ac_now, target))
        _current_game_id = appid if result.get("success") else "\x00"
        return events

    _enforce_target(_clamp_for_settings(
        s,
        s.get("active_spl",  s.get("spl",  _defaults()["spl"])),
        s.get("active_sppt", s.get("sppt", _defaults()["sppt"])),
        s.get("active_fppt", s.get("fppt", _defaults()["fppt"])),
    ))
    return events


def _check_and_enforce() -> dict:
    with _mutation_lock:
        return _check_and_enforce_locked()


# WMI reads back the exact value we wrote, so a tight tolerance is right there. The
# ryzenadj path reports STAPM LIMIT for SPL, which the firmware manages dynamically
# (it drifts several watts below the set point under load), so comparing it tightly
# made the loop re-apply forever. A wide band there still catches a real reset - after
# resume the SMU drops to firmware defaults, which is a double-digit gap.
DRIFT_TOLERANCE_WMI_W      = 1.0
DRIFT_TOLERANCE_RYZENADJ_W = 6.0
DRIFT_MAX_ATTEMPTS = 3

_drift_target:   tuple = ()
_drift_settled:  tuple = ()
_drift_attempts: int   = 0


def _enforce_target(want: tuple) -> None:
    """Re-apply `want` when the hardware has drifted off it.

    Some targets are simply unreachable - the SMU silently caps slow-limit around
    50 W, for instance - and chasing those forever re-ran ryzenadj every 5 s and
    flooded the log. After a few failed attempts we accept whatever the hardware
    settled on, and only act again if it moves away from that.
    """
    global _drift_target, _drift_settled, _drift_attempts

    if want != _drift_target:
        _drift_target, _drift_settled, _drift_attempts = want, (), 0

    # Only reuse the panel's cache if it was filled after the last apply. It is
    # refreshed on its own two-second cadence, so a pass running right after a
    # change would otherwise compare the new target against a snapshot taken
    # before it - which reported a drift that had not happened and spent a
    # redundant apply correcting it. Visible in the journal as a "TDP drift"
    # line one second after every slider move.
    with _info_cache_lock:
        parsed = dict(_info_cache) if _panel_is_active() and _info_cache_ts > _applied_at else {}
    if not parsed:
        parsed = _read_limits()
    cur = tuple(parsed.get(f"{k}_limit") for k in ("spl", "sppt", "fppt"))
    if any(v is None for v in cur):
        return

    want_w    = tuple(v / 1000 for v in want)
    reference = _drift_settled or want_w
    tolerance = DRIFT_TOLERANCE_WMI_W if _last_source == "wmi" else DRIFT_TOLERANCE_RYZENADJ_W
    # The WMI attributes keep reporting the last value even after the profile leaves
    # 'custom', so a matching read is not proof the limit is actually enforced - force
    # a re-apply (which re-selects custom) when we detect that. For the same reason
    # a matching read is no proof nobody else moved the limits, hence the live
    # cross-check; both make `cur` unreliable rather than merely stale.
    profile_lost = _wmi_profile_lost()
    overridden = _last_source == "wmi" and _wmi_limits_overridden(want_w)
    if not (profile_lost or overridden) \
            and all(abs(c - r) <= tolerance for c, r in zip(cur, reference)):
        # The retry budget is for consecutive failures to reach a target, not
        # the lifetime count of unrelated drifts. A confirmed real target starts
        # the next recovery from a full budget.
        if not _drift_settled:
            _drift_attempts = 0
        return

    if profile_lost:
        decky.logger.info("[legotdp] platform profile left 'custom', re-asserting limits")
        _drift_settled, _drift_attempts = (), 0

    if overridden:
        decky.logger.info(
            "[legotdp] a live read disagrees with the firmware attributes, "
            "something moved the limits behind us - re-asserting")
        _drift_settled, _drift_attempts = (), 0

    if _drift_settled:
        # Moved off the value we had accepted, so something external changed it.
        # Give the real target another go.
        _drift_settled, _drift_attempts = (), 0

    if _drift_attempts >= DRIFT_MAX_ATTEMPTS:
        _drift_settled = cur
        decky.logger.warning(
            f"[legotdp] target {want_w} unreachable after {_drift_attempts} attempts, "
            f"accepting {cur} and standing down")
        return

    _drift_attempts += 1
    decky.logger.info(
        f"[legotdp] TDP drift {cur} -> {want_w}, re-applying (attempt {_drift_attempts})")
    result = _apply_limits_with_saved_cpu_power(_load_settings(), *want)
    if not result["success"]:
        decky.logger.warning(
            f"[legotdp] drift re-apply failed rc={result['returncode']} err={result['stderr']}")


def _restore_defaults_locked() -> dict:
    """Hand control back to firmware. Caller must hold _mutation_lock."""
    global _last_source, _applied_mw, _applied_at
    global _drift_target, _drift_settled, _drift_attempts
    if not _apply_lock.acquire(timeout=8.0):
        return {"success": False, "stdout": "", "stderr": "apply busy", "returncode": -1}
    try:
        _drift_target, _drift_settled, _drift_attempts = (), (), 0
        path = _profile_path()
        if _wmi_caps() and path and _write_profile(path, "balanced"):
            _last_source, _applied_mw, _applied_at = "", (), 0.0
            _invalidate_limits_cache()
            decky.logger.info("[legotdp] restore_defaults: platform profile -> balanced")
            return {"success": True, "stdout": "", "stderr": "", "returncode": 0}
        if not _ryzenadj_lock.acquire(timeout=4.0):
            return {"success": False, "stdout": "", "stderr": "ryzenadj busy", "returncode": -1}
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
    global_before = (state.get("spl", _defaults()["spl"]),
                     state.get("sppt", _defaults()["sppt"]),
                     state.get("fppt", _defaults()["fppt"]))
    state["spl"], state["sppt"], state["fppt"] = _clamp_triplet(
        state.get("spl", _defaults()["spl"]),
        state.get("sppt", _defaults()["sppt"]),
        state.get("fppt", _defaults()["fppt"]), ceilings)
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
) -> list[str]:
    """Best-effort rollback without overwriting a third-party new value."""
    failures: list[str] = []
    for path in reversed(changed):
        try:
            current = _read_cpu_power_text(path)
            if equivalent(current, originals[path]):
                continue
            if not equivalent(current, requested[path]):
                failures.append(
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
    return failures


def _transactional_cpu_power_write(
    originals: dict[str, str],
    requested: dict[str, str],
    equivalent,
    label: str,
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
        rollback = _rollback_cpu_power_write(
            originals, requested, changed, equivalent)
        suffix = (f"; rollback incomplete: {'; '.join(rollback)}" if rollback
                  else "; previous state restored")
        return {
            "success": False,
            "error": f"{label} write failed: {exc}{suffix}",
            "originals": originals,
            "requested": requested,
            "changed": changed,
        }
    return {
        "success": True,
        "error": "",
        "originals": originals,
        "requested": requested,
        "changed": changed,
    }


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
    return _transactional_cpu_power_write(
        capture["originals"], requested, _epp_equivalent, "EPP")


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


def _reapply_saved_cpu_power_controls_locked(state: dict) -> list[str]:
    """Best-effort startup/resume restore. Caller holds _mutation_lock."""
    state = _effective_cpu_state(state)
    errors: list[str] = []
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
    return errors


def _best_effort_reapply_saved_cpu_power_locked(
    state: dict, context: str
) -> list[str]:
    try:
        errors = _reapply_saved_cpu_power_controls_locked(state)
    except Exception as exc:
        errors = [f"CPU power restore failed: {exc}"]
    for error in errors:
        decky.logger.warning(f"[legotdp] {context}: {error}")
    return errors


def _apply_limits_with_saved_cpu_power(
    state: dict, spl: int, sppt: int, fppt: int
) -> dict:
    """Apply Layer 1 first, then restore persistent CPU controls.

    Selecting a platform profile can reset amd-pstate EPP. Keeping this wrapper
    at every managed TDP transition makes Boost/EPP the final write without
    turning their optional capability failure into a TDP failure.
    Caller holds _mutation_lock.
    """
    result = _apply_limits(spl, sppt, fppt)
    # A failed Layer 1 call may already have selected platform_profile before a
    # later firmware/RyzenAdj error. CPU controls are independent, so restore
    # them even on that partial failure.
    errors = _best_effort_reapply_saved_cpu_power_locked(
        state, "saved CPU control did not apply after Layer 1")
    result["cpu_power_errors"] = errors
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
                    old = _clamp_for_settings(before, *(
                        before.get("active_" + key, before.get(key, _defaults()[key]))
                        for key in ("spl", "sppt", "fppt")))
                    rollback = _apply_limits_with_saved_cpu_power(before_cpu, *old)
                else:
                    rollback = _restore_defaults_locked()
                    rollback["cpu_power_errors"] = _best_effort_reapply_saved_cpu_power_locked(
                        before_cpu, "after failed TDP operation")
                return (rollback.get("success", False)
                        and not rollback.get("cpu_power_errors"))
            except Exception:
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


class Plugin:
    _ready: bool = False
    # Surfaced through is_ready() so a failed start shows up in the panel
    # instead of leaving the user with sliders that silently do nothing.
    _setup_error: str | None = None
    _tasks: list = []

    async def is_ready(self) -> dict:
        return {"ready": self._ready, "error": self._setup_error or ""}

    async def get_version(self) -> dict:
        return {"version": updater.plugin_version()}

    async def get_settings(self) -> dict:
        return await _offload(_load_settings)

    @staticmethod
    def _cpu_power_persistence_failure(
        exc: Exception, transaction: dict, equivalent
    ) -> str:
        rollback = _rollback_cpu_power_write(
            transaction["originals"],
            transaction["requested"],
            transaction["changed"],
            equivalent,
        )
        suffix = (f"; rollback incomplete: {'; '.join(rollback)}" if rollback
                  else "; previous state restored")
        return f"settings persist failed: {exc}{suffix}"

    async def get_cpu_power_controls(self, app_id: str = "", ac_profile: bool = False) -> dict:
        def _do() -> dict:
            with _mutation_lock:
                try:
                    selected = _normalise_app_id(app_id)
                    if selected is None or type(ac_profile) is not bool or (ac_profile and not selected):
                        return _cpu_scoped_status(
                            operation_success=False, operation_error="invalid CPU profile context")
                    return _cpu_scoped_status(selected, ac_profile)
                except Exception as exc:
                    decky.logger.warning(f"[legotdp] CPU power status failed: {exc}")
                    return _cpu_power_controls_error_status(
                        "CPU power controls unavailable", selected or "",
                        ac_profile if type(ac_profile) is bool else False)
        return await _offload(_do)

    async def _set_cpu_control(self, field: str, value, app_id: str,
                               ac_profile: bool, expected_app_id: str | None) -> dict:
        def _do() -> dict:
            with _mutation_lock:
                selected = _normalise_app_id(app_id)
                expected = _normalise_app_id(expected_app_id) if expected_app_id is not None else None
                def status(success: bool, error: str = "") -> dict:
                    return _cpu_scoped_status(
                        selected or "", ac_profile if type(ac_profile) is bool else False,
                        operation_success=success, operation_error=error)
                try:
                    if (selected is None or type(ac_profile) is not bool
                            or (ac_profile and not selected)
                            or (expected_app_id is not None and expected is None)):
                        return status(False, "invalid CPU profile context")
                    current = _get_running_appid()
                    if expected is not None and current != expected:
                        return status(False, "foreground game changed")
                    if selected and current != selected:
                        return status(False, "game is no longer active")
                    state, profiles = _load_settings(), _load_profiles()
                    if selected and not state.get("enabled", True):
                        return status(False, "plugin disabled")
                    if ac_profile and not profiles.get(selected, {}).get("ac_separate"):
                        return status(False, "separate AC profile is disabled")
                    if field == SETTINGS_FIELD_CPU_BOOST_ENABLED:
                        if type(value) is not bool:
                            return status(False, "invalid CPU Boost value")
                        preflight, canonical = _capture_cpu_boost(), value
                        apply, equivalent = _apply_cpu_boost_hardware, _boost_equivalent
                    else:
                        preflight = _capture_epp()
                        canonical = _validated_epp_request(preflight, value)
                        apply, equivalent = _apply_epp_hardware, _epp_equivalent
                        if canonical is None:
                            return status(False, "invalid or unsupported EPP value")
                    if not preflight["can_set"]:
                        return status(False, preflight["error"] or "CPU control is unsupported")
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
                    active = _cpu_editor_profile(state, profiles, selected, ac_profile)["active"]
                    transaction = None
                    if active:
                        transaction = apply(canonical)
                        if not transaction["success"]:
                            return status(False, transaction["error"])
                    try:
                        values = {SETTINGS_KEY_SETTINGS: state}
                        if selected:
                            values[SETTINGS_KEY_GAME_PROFILES] = profiles
                        _write_keys(values)
                    except Exception as exc:
                        error = (self._cpu_power_persistence_failure(exc, transaction, equivalent)
                                 if transaction is not None else f"settings persist failed: {exc}")
                        return status(False, error)
                    decky.logger.info(
                        f"[legotdp] {field}={canonical} app={selected or 'global'} ac={ac_profile} active={active}")
                    return status(True)
                except Exception as exc:
                    decky.logger.warning(f"[legotdp] CPU profile RPC failed: {exc}")
                    try:
                        return status(False, str(exc))
                    except Exception:
                        return _cpu_power_controls_error_status(
                            "CPU power operation failed", selected or "",
                            ac_profile if type(ac_profile) is bool else False)
        return await _offload(_do)

    async def set_cpu_boost(self, enabled: bool, app_id: str = "", ac_profile: bool = False,
                            expected_app_id: str | None = None) -> dict:
        return await self._set_cpu_control(
            SETTINGS_FIELD_CPU_BOOST_ENABLED, enabled, app_id, ac_profile, expected_app_id)

    async def set_epp(self, value: str, app_id: str = "", ac_profile: bool = False,
                      expected_app_id: str | None = None) -> dict:
        return await self._set_cpu_control(
            SETTINGS_FIELD_EPP, value, app_id, ac_profile, expected_app_id)

    async def get_power_source(self) -> dict:
        return {"ac": await _offload(_get_ac_online)}

    async def retry_extras(self) -> dict:
        if _wmi_only():
            return {"success": False, "error": "Extras is unavailable on this device"}
        if getattr(self, "_extras_retry_busy", False):
            return {"success": False, "error": "A retry is already running"}
        self._extras_retry_busy = True
        def retry():
            global _ryzenadj_available, _current_game_id, _transition_key
            try:
                _ensure_ryzenadj()
                with _mutation_lock:
                    _ryzenadj_available = True
                    _current_game_id = "\x00"
                    _transition_key = None
                return {"success": True}
            except Exception as exc:
                return {"success": False, "error": str(exc)}
            finally:
                self._extras_retry_busy = False
        return await _offload(retry)

    async def get_extras_unlocked(self) -> bool:
        s = await _offload(_load_settings)
        return s.get("extras_unlocked", False)

    async def set_extras_unlocked(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            return {"success": False, "stdout": "",
                    "stderr": "enabled must be a boolean", "returncode": -1}
        def _do():
            with _tdp_user_transaction() as restore_failed:
                if enabled and (_wmi_only() or not _ryzenadj_available):
                    return {"success": False, "stdout": "",
                            "stderr": "Extras is unavailable because ryzenadj is not ready",
                            "returncode": -1}
                s = _load_settings()
                profiles = _load_profiles()
                if enabled:
                    s["extras_unlocked"] = True
                    _save_settings(s)
                    return {"success": True, "stdout": "", "stderr": "", "returncode": 0}

                active = _lock_extras_state(s, profiles)
                if s.get("enabled", True):
                    result = _apply_limits_with_saved_cpu_power(s, *active)
                    if not result["success"]:
                        return restore_failed(result)
                s["extras_unlocked"] = False
                _write_keys({SETTINGS_KEY_SETTINGS: s,
                             SETTINGS_KEY_GAME_PROFILES: profiles})
                _cancel_ac_settle()
                return {"success": True, "stdout": "", "stderr": "", "returncode": 0}
        result = await _offload(_do)
        decky.logger.info(f"[legotdp] extras_unlocked={enabled} success={result['success']}")
        return result

    async def get_game_profile(self, app_id: str) -> dict:
        app_id = _normalise_app_id(app_id, allow_empty=False)
        if app_id is None:
            return {"exists": False, "profile": {}, "ac_separate": False,
                    "ac_profile": {}, "error": "invalid app id"}
        def _do() -> dict:
            p = _load_profiles().get(app_id)
            if p is None:
                return {"exists": False, "profile": {}, "ac_separate": False, "ac_profile": {}}
            spl  = p.get("spl",  _defaults()["spl"])
            sppt = p.get("sppt", _defaults()["sppt"])
            fppt = p.get("fppt", _defaults()["fppt"])
            state = _load_settings()
            return {
                "exists":      True,
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

    async def set_game_ac_profile(self, app_id: str, spl: int, sppt: int, fppt: int, ac_separate: bool, preset_name: str = "") -> dict:
        app_id = _normalise_app_id(app_id, allow_empty=False)
        if (app_id is None or type(ac_separate) is not bool
                or any(type(value) is not int for value in (spl, sppt, fppt))):
            return {"success": False, "stderr": "invalid profile request",
                    "stdout": "", "returncode": -1}
        preset_name = _safe_label(preset_name)
        def _do() -> dict:
            with _tdp_user_transaction() as restore_failed:
                if not app_id or _get_running_appid() != app_id:
                    return {"success": False, "stderr": "game is no longer active",
                            "stdout": "", "returncode": -1}
                state = _load_settings()
                if not state.get("enabled", True):
                    return {"success": False, "stderr": "plugin disabled",
                            "stdout": "", "returncode": -1}
                ac = _clamp_for_settings(state, spl, sppt, fppt)
                profiles = _load_profiles()
                p = _prepare_cpu_profile(
                    state, profiles, app_id,
                    snapshot_cpu=ac_separate and not profiles.get(app_id, {}).get("ac_separate"))
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
                if _get_ac_online():
                    if ac_separate:
                        want = ac
                    elif all(p.get(k) is not None for k in ("spl", "sppt", "fppt")):
                        want = _clamp_for_settings(state, p["spl"], p["sppt"], p["fppt"])
                if want is not None:
                    result = _apply_limits_with_saved_cpu_power(
                        _effective_cpu_state(state, profiles, app_id), *want)
                    if not result["success"]:
                        return restore_failed(result)
                    state["active_spl"], state["active_sppt"], state["active_fppt"] = want
                    _cancel_ac_settle()

                _write_keys({SETTINGS_KEY_SETTINGS: state,
                             SETTINGS_KEY_GAME_PROFILES: profiles})
                decky.logger.info(
                    f"[legotdp] Saved AC profile: app={app_id} separate={ac_separate}")
                return {"success": True, "stderr": "", "stdout": "", "returncode": 0}
        return await _offload(_do)

    async def delete_game_profile(self, app_id: str) -> dict:
        app_id = _normalise_app_id(app_id, allow_empty=False)
        if app_id is None:
            return {"success": False, "stderr": "invalid app id",
                    "stdout": "", "returncode": -1}
        def _do() -> dict:
            with _tdp_user_transaction() as restore_failed:
                profiles = _load_profiles()
                profiles.pop(app_id, None)
                state = _load_settings()
                if state.get("enabled", True) and _get_running_appid() == app_id:
                    target = _global_triplet(state)
                    result = _apply_limits_with_saved_cpu_power(
                        _effective_cpu_state(state, profiles, app_id), *target)
                    if not result["success"]:
                        return restore_failed(result)
                    state["active_spl"], state["active_sppt"], state["active_fppt"] = target
                    _cancel_ac_settle()
                _write_keys({SETTINGS_KEY_SETTINGS: state,
                             SETTINGS_KEY_GAME_PROFILES: profiles})
                return {"success": True, "stdout": "", "stderr": "", "returncode": 0}
        result = await _offload(_do)
        decky.logger.info(
            f"[legotdp] Deleted game profile: app={app_id} success={result['success']}")
        return result

    async def set_plugin_enabled(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            return {"success": False, "stderr": "enabled must be a boolean",
                    "stdout": "", "returncode": -1}
        def _do() -> dict:
            with _tdp_user_transaction() as restore_failed:
                state = _load_settings()
                if not enabled:
                    result = _restore_defaults_locked()
                    if not result["success"]:
                        return restore_failed(result)
                    state["enabled"] = False
                    try:
                        result["cpu_power_errors"] = (
                            _reapply_saved_cpu_power_controls_locked(state))
                    except Exception as exc:
                        result["cpu_power_errors"] = [
                            f"CPU power restore failed: {exc}"]
                    state["enabled"] = False
                    _save_settings(state)
                    _cancel_ac_settle()
                    return restore_failed(result)

                state["enabled"] = True
                _, target, _ = _startup_context_target(state)
                result = _apply_limits_with_saved_cpu_power(state, *target)
                if not result["success"]:
                    return restore_failed(result)
                state["enabled"] = True
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
            caps = _wmi_caps()
            std = {k: caps[k]["max"] for k in WMI_ATTRS} if caps else dict(FALLBACK_STD_W)
            return {
                "min": min(caps[k]["min"] for k in WMI_ATTRS) if caps else HARD_MIN_MW // 1000,
                "std": std,
                # ryzenadj is not installed on these, so the two ranges are
                # the same and the frontend hides the Extras switch rather than
                # offering a range nothing here would apply.
                "max": dict(std) if _wmi_only() or not _ryzenadj_available
                       else {k: HARD_MAX_MW // 1000 for k in WMI_ATTRS},
                "wmi": bool(caps),
                "extras": not _wmi_only() and _ryzenadj_available,
                "presets": _presets(),
            }
        return await _offload(_do)

    async def restore_defaults(self) -> dict:
        def _do() -> dict:
            with _tdp_user_transaction() as restore_failed:
                result = _restore_defaults_locked()
                if result["success"]:
                    state = _load_settings()
                    try:
                        result["cpu_power_errors"] = (
                            _reapply_saved_cpu_power_controls_locked(state))
                    except Exception as exc:
                        result["cpu_power_errors"] = [
                            f"CPU power restore failed: {exc}"]
                    _cancel_ac_settle()
                return restore_failed(result)
        return await _offload(_do)

    async def set_panel_active(self, active: bool) -> None:
        """Renew (or drop) the panel's lease on the info loop."""
        global _panel_active, _panel_active_ts
        if type(active) is not bool:
            raise ValueError("active must be a boolean")
        _panel_active = active
        _panel_active_ts = time.monotonic() if active else 0.0

    async def reapply(self) -> dict:
        """Force the saved limits back onto the hardware.

        Called by the frontend on resume from suspend, where the SMU comes back
        at firmware defaults. Decky has no backend resume hook - the loader only
        ever invokes _migration, _main, _unload and _uninstall - so Steam's own
        notification is the only signal there is, and without it the enforce
        loop takes up to five seconds to notice.
        """
        def _do() -> dict:
            with _mutation_lock:
                s = _load_settings()
                if not s.get("enabled", True):
                    try:
                        cpu_power_errors = (
                            _reapply_saved_cpu_power_controls_locked(s))
                    except Exception as exc:
                        cpu_power_errors = [
                            f"CPU power restore failed: {exc}"]
                    return {"success": True, "skipped": True,
                            "cpu_power_errors": cpu_power_errors}
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
        normalised = _normalise_app_id(app_id)
        if normalised is None:
            raise ValueError("invalid app id")
        _frontend_appid = normalised
        _frontend_appid_ts = time.monotonic()

    async def get_tdp_info(self) -> dict:
        if not self._ready:
            return {"success": False, "values": {}, "error": "not ready"}
        with _info_cache_lock:
            return {"success": True, "values": dict(_info_cache)}

    async def apply_tdp(self, spl: int, sppt: int, fppt: int, app_id: str = "",
                        preset_name: str = "",
                        expected_app_id: str | None = None) -> dict:
        if not self._ready:
            return {"success": False, "stderr": "not ready", "stdout": "", "returncode": -1}
        app_id = _normalise_app_id(app_id)
        expected_was_supplied = expected_app_id is not None
        expected_app_id = (
            _normalise_app_id(expected_app_id) if expected_was_supplied else None)
        if (app_id is None or (expected_was_supplied and expected_app_id is None)
                or any(type(value) is not int for value in (spl, sppt, fppt))):
            return {"success": False, "stderr": "invalid TDP request",
                    "stdout": "", "returncode": -1}
        preset_name = _safe_label(preset_name)

        def _do() -> dict:
            with _tdp_user_transaction() as restore_failed:
                s = _load_settings()
                if not s.get("enabled", True):
                    return {"success": False, "stderr": "plugin disabled",
                            "stdout": "", "returncode": -1}
                current_app_id = _get_running_appid()
                if expected_app_id is not None and current_app_id != expected_app_id:
                    return {"success": False, "stderr": "foreground game changed",
                            "stdout": "", "returncode": -1}
                if app_id and current_app_id != app_id:
                    return {"success": False, "stderr": "game is no longer active",
                            "stdout": "", "returncode": -1}

                requested = _clamp_for_settings(s, spl, sppt, fppt)
                profiles: dict = {}
                existing: dict = {}
                want = requested

                if app_id:
                    profiles = _load_profiles()
                    existing = _prepare_cpu_profile(s, profiles, app_id)
                    # On AC with a separate AC profile, the sliders describe the
                    # battery values but the hardware should run the AC ones.
                    if (_get_ac_online() and existing.get("ac_separate")
                            and existing.get("ac_spl") is not None):
                        want = _clamp_for_settings(
                            s,
                            existing["ac_spl"],
                            existing.get("ac_sppt", existing.get("sppt", _defaults()["sppt"])),
                            existing.get("ac_fppt", existing.get("fppt", _defaults()["fppt"])),
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
                    decky.logger.info(f"[legotdp] Saved game profile: app={app_id}")
                else:
                    s["spl"], s["sppt"], s["fppt"] = requested
                    s["active_preset"] = preset_name
                s["active_spl"], s["active_sppt"], s["active_fppt"] = want
                values = {SETTINGS_KEY_SETTINGS: s}
                if app_id:
                    values[SETTINGS_KEY_GAME_PROFILES] = profiles
                _write_keys(values)
                _cancel_ac_settle()
                return restore_failed(result)
        return await _offload(_do)

    async def _push_info(self) -> bool:
        """One refresh, pushed to the panel. True when something went out.

        Split out of _info_loop so the emit itself can be tested - the loop
        around it never terminates, so there is no way to await one iteration.
        """
        if not _panel_is_active():
            return False
        await _offload(_refresh_info_cache)
        with _info_cache_lock:
            values = dict(_info_cache)
        # Pushed, not polled. The panel used to ask for this over RPC every two
        # seconds - a round trip per tick to fetch numbers the backend had just
        # refreshed on this very schedule.
        await decky.emit("tdp_info", {"success": True, "values": values})
        return True

    async def _info_loop(self):
        while True:
            await asyncio.sleep(2)
            try:
                await self._push_info()
            except Exception as e:
                decky.logger.warning(f"[legotdp] info loop error: {e}")

    async def _resettle_after_ac(self, generation: int):
        """Put the limits back over the seconds after a charger transition.

        The firmware writes its own profile on plug-in and wins the race against
        a single apply, so the target is re-asserted until it stops being
        overwritten. Each pass is skipped once the hardware already agrees, so
        this costs nothing when the first write did stick.
        """
        for delay in AC_SETTLE_DELAYS_S:
            await asyncio.sleep(delay)
            try:
                settled = await _offload(_reapply_current_target, generation)
            except Exception as e:
                decky.logger.warning(f"[legotdp] AC re-settle failed: {e}")
                return
            if settled:
                return

    async def _enforce_loop(self):
        while True:
            await asyncio.sleep(5)
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
            except Exception as e:
                decky.logger.warning(f"[legotdp] enforce iteration failed: {e}")

    async def _migration(self):
        """Fold the pre-1.5.0 files into Decky's store, before anything reads it.

        This is the loader's own hook for the job: it runs to completion before
        _main() is even scheduled, so no settings read can race the migration.

        decky.migrate_settings() deliberately goes unused. It relocates a file
        under its own basename and rm -rf's the source, so the legacy
        PLUGIN_DIR/settings.json would land straight on top of the
        SettingsManager store - identical filename - and replace the whole
        keyed object with a flat pre-1.5.0 dict. That helper moves files; this
        migration has to reshape them.

        Nothing may escape. The loader runs this with run_until_complete inside
        a bare except that logs and sys.exit(0)s, and it never gets as far as
        creating the RPC socket - so a raise here would leave the panel retrying
        an is_ready() that has nobody to answer it, spinning on "Initializing"
        forever. Recording the failure instead tells the user their old settings
        did not come across, before they start rebuilding them on top.
        """
        try:
            await _offload(_migrate)
        except Exception as e:
            Plugin._setup_error = f"settings migration failed: {e}"
            decky.logger.error(f"[legotdp] migration failed: {e}")

    async def _main(self):
        decky.logger.info(f"[legotdp] startup  v{updater.plugin_version()}")
        try:
            global _current_ac_online, _ryzenadj_available, _last_suspend_offset
            Plugin._ready = False
            Plugin._setup_error = None
            # Resolve the trust store now so the log states up front whether downloads
            # and update checks will be able to verify certificates.
            await _offload(updater.ssl_context)
            # Seed this so the first enforce pass does not report a phantom AC change.
            _current_ac_online = await _offload(_get_ac_online)
            # Establish the suspend-clock baseline before the workers start, so
            # even an early sleep/wake cycle is detected by their first pass.
            _last_suspend_offset = _suspend_offset()
            wmi = await _offload(_wmi_caps)
            try:
                if _wmi_only():
                    _ryzenadj_available = False
                    decky.logger.info(
                        "[legotdp] firmware-only device, skipping the ryzenadj download")
                else:
                    await _offload(_ensure_ryzenadj)
                    _ryzenadj_available = True
            except Exception as e:
                _ryzenadj_available = False
                # Only fatal when there is no firmware path to fall back on.
                if not wmi:
                    raise
                decky.logger.warning(
                    f"[legotdp] ryzenadj unavailable ({e}); Extras range disabled")
            def _apply_saved() -> dict:
                global _current_game_id, _last_cpu_power_check
                with _mutation_lock:
                    s = _load_settings()
                    # Runtime ceilings already account for unavailable RyzenAdj.
                    # Preserve user profiles so a later online start can restore them.
                    app_id, target, using_profile = _startup_context_target(s)
                    _current_game_id = "\x00"
                    if not s.get("enabled", True):
                        try:
                            cpu_power_errors = (
                                _reapply_saved_cpu_power_controls_locked(s))
                            _last_cpu_power_check = time.monotonic()
                        except Exception as exc:
                            cpu_power_errors = [
                                f"CPU power restore failed: {exc}"]
                        return {"success": True, "skipped": True,
                                "cpu_power_errors": cpu_power_errors}
                    result = _apply_limits_with_saved_cpu_power(s, *target)
                    _last_cpu_power_check = time.monotonic()
                    if result["success"]:
                        _save_active(s, *target)
                        _current_game_id = app_id
                        decky.logger.info(
                            "[legotdp] startup target: "
                            + (f"profile app={app_id}" if using_profile
                               else "global profile"))
                    return result
            startup_apply = await _offload(_apply_saved)
            if not startup_apply.get("success", False):
                decky.logger.warning(
                    f"[legotdp] saved TDP did not apply at startup: "
                    f"{startup_apply.get('stderr', 'unknown error')}")
            for error in startup_apply.get("cpu_power_errors", []):
                decky.logger.warning(
                    f"[legotdp] saved CPU control did not apply at startup: {error}")

            Plugin._ready = True
            # Keep references - a bare create_task() may be garbage-collected mid-run.
            self._tasks = [
                asyncio.create_task(self._enforce_loop()),
                asyncio.create_task(self._info_loop()),
            ]
            decky.logger.info(
                f"[legotdp] ready (wmi={'yes' if wmi else 'no'}, "
                f"ryzenadj={'yes' if _ryzenadj_available else 'no'})")
        except Exception as e:
            Plugin._setup_error = str(e)
            decky.logger.error(f"[legotdp] setup failed: {e}")

    async def _unload(self):
        Plugin._ready = False
        def _invalidate_ac_ladder() -> None:
            with _mutation_lock:
                _cancel_ac_settle()
        await _offload(_invalidate_ac_ladder)
        for t in self._tasks:
            t.cancel()
        # Bounded on purpose. Cancelling a task parked in run_in_executor does
        # not stop the worker thread, so awaiting it takes as long as whatever
        # blocking call it sits inside - a `ryzenadj --info` plus its lock wait
        # is five seconds on its own. The loader gives a plugin exactly five
        # seconds to stop before it sends SIGKILL, and a SIGKILL means
        # _uninstall() never runs at all. Observed on the device: the platform
        # profile was left pinned to 'custom' with the plugin already gone.
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=1.0)
        self._tasks = []
        decky.logger.info("[legotdp] unloaded")

    async def _uninstall(self):
        """Hand the hardware back before the plugin directory disappears.

        The firmware keeps whatever ppt_* triplet was last latched into the
        'custom' profile, so without this an uninstall leaves the machine pinned
        to the plugin's final TDP with nothing left installed to change it.

        Nothing is cleaned off disk here: the loader removes the whole plugin
        directory straight afterwards, and DECKY_PLUGIN_SETTINGS_DIR - where the
        settings and per-game profiles live - is deliberately left alone, so a
        reinstall still finds them.
        """
        def _do() -> None:
            # _unload() cancelled the enforce loop, but cancelling a task parked
            # in run_in_executor does not stop the worker thread, so a pass may
            # still be in flight holding this lock. Take it, or that pass could
            # re-assert the limits after we have handed the profile back.
            with _mutation_lock:
                _load_settings()
                if not _apply_lock.acquire(timeout=8.0):
                    decky.logger.warning(
                        "[legotdp] uninstall: apply busy, leaving the profile as it is")
                    return
                try:
                    path = _profile_path()
                    if path and _write_profile(path, "balanced"):
                        decky.logger.info("[legotdp] uninstall: platform profile -> balanced")
                finally:
                    _apply_lock.release()
        await _offload(_do)
        decky.logger.info("[legotdp] uninstalled")

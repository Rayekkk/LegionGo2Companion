# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/LeGo2BrightnessFix

"""Legacy display support and independent EDID correction under gamescope.

Recognised native Legion Go 2 support retires only the Lua/brightness/Hybrid
workaround. The description below documents that workaround on older systems;
EDID correction remains independent on both older and native configurations.

Both come from the same afternoon of measuring a Legion Go 2 (Samsung
AMS881KB01-0), and both are gaps rather than misbehaviour: something upstream
simply does not write where the display is listening.

1. The brightness slider (see Plugin._refresh_gate)

The panel is driven over eDP AUX luminance control, so /sys/class/backlight/
amdgpu_bl0 is expressed in millinits and tops out at 471000, i.e. 471 nits of
full-field white. While gamescope drives the panel in PQ, the panel ignores that
control completely. Steam still writes the slider there, so the slider moves and
nothing happens.

Steam does have a working path, but only for HDR content: with an HDR app on
screen it drives GAMESCOPE_HDR_INPUT_GAIN and GAMESCOPE_SDR_INPUT_GAIN instead
of the backlight (that is the two-tone slider the Quick Access Menu shows in
HDR). With no HDR content it falls back to the backlight alone and there is
nothing downstream that listens. So the gap is PQ output with no HDR content,
and this plugin forwards the requested level to
GAMESCOPE_SDR_ON_HDR_CONTENT_BRIGHTNESS, which is expressed in the same unit.

2. The EDID games are given (see Plugin._edid_pass)

gamescope writes a copy of the panel EDID and publishes its path in
GAMESCOPE_DISPLAY_EDID_PATH; Proton loads that into the Wine registry, and DXVK
parses it to fill DXGI_OUTPUT_DESC1. DXVK vendors libdisplay-info pinned at a
2023 commit whose parse_ext() ignores errno, so when _di_displayid_parse()
refuses a DisplayID 2.0 block with ENOTSUP the whole EDID is discarded rather
than that one block. DXVK then substitutes NormalizeDisplayMetadata()'s
placeholders: 1499 nits peak, 799 full-frame, and generic P3 primaries.

Measured on the device: this panel reports 1107.128 / 475.683 nits and its own
primaries, and games were seeing 1499 / 799 / P3. Removing the DisplayID
extension from gamescope's copy makes the rest parse and games read the real
values. DXVK never reads DisplayID at all - only base-block chromaticity and the
CTA HDR static metadata - so dropping it cannot cost those games anything.

The proper fix belongs in DXVK. This is a stopgap for as long as that is stale.
"""

import asyncio
import glob
import hashlib
import math
import os
from system_process import system_env as _system_env
import select
import signal
import stat
import struct
import subprocess
import sys
import threading
import time

try:
    import pwd
except ImportError:  # pragma: no cover - Windows test host
    pwd = None

import decky
import module_runtime
import display_native
from safe_settings import SettingsManager, CorruptSettings

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

LOG = "[lego2brightnessfix]"


# gamescope publishes these on the root window of its Xwayland display.
ATOM_SDR_NITS = "GAMESCOPE_SDR_ON_HDR_CONTENT_BRIGHTNESS"
ATOM_HDR_OUT = "GAMESCOPE_HDR_OUTPUT_FEEDBACK"
ATOM_HDR_APP = "GAMESCOPE_COLOR_APP_WANTS_HDR_FEEDBACK"
ATOM_EDID_PATH = "GAMESCOPE_DISPLAY_EDID_PATH"
ATOM_IS_EXTERNAL = "GAMESCOPE_DISPLAY_IS_EXTERNAL"

# Hybrid mode drives these two. The first is the only input that decides whether
# the output runs PQ or gamma 2.2, and gamescope acts on it live. The second is
# what makes the whole thing possible: the Vulkan layer reads
# GAMESCOPE_HDR_OUTPUT_FEEDBACK exactly once, when the game creates its surface,
# and hides every HDR format if it reads zero - so a game started while the panel
# sits in gamma 2.2 would never ask for HDR and the switch would never fire.
# gamescope publishes that feedback as (output HDR on OR this debug flag), and
# the flag does not touch the output itself, so holding it at 1 lets games see
# HDR while the panel stays in gamma 2.2 until one of them actually uses it.
ATOM_HDR_ENABLED = "GAMESCOPE_DISPLAY_HDR_ENABLED"
ATOM_FORCE_HDR_SUPPORT = "GAMESCOPE_DEBUG_FORCE_HDR_SUPPORT"

WATCHED_ATOMS = (
    ATOM_SDR_NITS,
    ATOM_HDR_OUT,
    ATOM_HDR_APP,
    ATOM_EDID_PATH,
    ATOM_IS_EXTERNAL,
    ATOM_HDR_ENABLED,
    ATOM_FORCE_HDR_SUPPORT,
)
_prop_cache: dict[str, str] = {}
_prop_cache_display: str | None = None
_prop_cache_ready = False
_prop_cache_lock = threading.Lock()


def _strict_bool(value, default: bool) -> bool:
    return value if type(value) is bool else default

# gamescope listens on the root window of every Xwayland server it runs, so a
# write to any of them takes effect. What cannot be assumed is the numbering:
# how many servers there are is a command line option and which numbers they get
# depends on what was already taken, so a desktop session holding :0 pushes them
# along. The server is found by looking for gamescope's own atoms - xprop exits 0
# whether or not the atom it was asked for exists, so its status says nothing.
DISPLAY = ":0"          # replaced at startup by _pick_display()
X11_SOCKET_DIR = "/tmp/.X11-unix"
DISPLAY_FALLBACK = (":0", ":1")

# The backlight class calls sysfs_notify() on `actual_brightness` every time the
# level is set, including from a sysfs write, so the kernel can wake us exactly
# when the slider moves and we never have to poll. Measured on the device: the
# wake-ups track the drag at ~50 ms, which is simply the rate Steam writes at.
#
# `actual_brightness` is only the doorbell. It lags the requested value and was
# observed reporting the minimum mid-transition, so the value itself always comes
# from `brightness`.
NOTIFY_ATTR = "actual_brightness"

# Used only where the notification is unavailable, so the plugin still works.
# Kernel notifications are the normal path.  On an unusual system without
# inotify support, 4 reads/s still feels immediate while avoiding a permanent
# 20 wakeups/s battery tax.
POLL_FALLBACK_S = 0.25

# Reading the gate atoms costs an xprop process, measured at 2.1 ms - the single
# largest thing this plugin does. The gate only moves when a game starts or
# stops, and being a second or two late there costs nothing: Steam does not take
# over its own gain atoms instantly either. The EDID check rides along on it.
GATE_INTERVAL_S = 2.0

# Hybrid mode polls faster, because this interval is exactly how long HDR
# content stays on a gamma 2.2 output before the panel follows it into PQ. One
# xprop costs about 2 ms, so twice a second is not worth economising on.
HYBRID_INTERVAL_S = 0.5
HARDWARE_RETRY_S = 10.0

# Enter PQ as soon as HDR appears, but do not chase brief SDR frames emitted
# while a game is rebuilding its swapchain or moving between loading screens.
# Both timers ride on the existing Hybrid poll; they add no wake-ups or xprop
# calls of their own. The minimum hold protects the physical PQ modeset from an
# immediate reversal, while the SDR debounce requires a continuously observed
# absence of HDR before the panel goes back to gamma 2.2.
HYBRID_PQ_MIN_HOLD_S = 3.0
HYBRID_SDR_DEBOUNCE_S = 2.0

# Every write to GAMESCOPE_DISPLAY_HDR_ENABLED is a modeset that blanks the
# panel for a moment. If something else ever held the opposite value, correcting
# it on every pass would turn the screen into a strobe rather than fix anything,
# so after a few corrections that did not stick we stand off and say so.
HDR_FIGHT_LIMIT = 4
HDR_BACKOFF_S = 60.0

# Panels known to ignore the backlight while in PQ, by EDID manufacturer and
# product code. Keeping this a list rather than "any internal panel" is the whole
# safety story for the brightness half: on a panel that does honour the
# backlight, forwarding the level a second time would dim the image twice.
KNOWN_PANELS = {
    ("SDC", 0x4301): "Lenovo Legion Go 2 (Samsung AMS881KB01-0)",
}

# gamescope's own default for the atom, used if we somehow cannot read the value
# that was there before we touched it.
FALLBACK_BASELINE_NITS = 203.0

# gamescope reads display scripts from $XDG_CONFIG_HOME/gamescope/scripts at
# startup, and only at startup. Without one of ours the panel runs stock: usable,
# but with no known refresh timings and no say over the EOTF, which is the single
# thing every mode below turns on. So the script is a precondition rather than an
# extra, and the plugin stays gated behind installing the variant the chosen mode
# needs.
SCRIPT_NAME = "lenovo.legiongo2.oled.lua"
SCRIPT_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "gamescope", "scripts",
)
INSTALLED_SCRIPT = os.path.join(SCRIPT_DIR, SCRIPT_NAME)

# Three ways to run this panel, none of them free:
#
#   pq       absolute encoding, the panel's full 1100 nit peak, but it ignores
#            the hardware brightness control, so dimming can only scale the
#            content down and the near-black floor stays put.
#   gamma22  the hardware control works again, black stays black at any level,
#            but HDR is tone mapped by gamescope against the ~471 nit ceiling
#            of the eDP AUX luminance control.
#   hybrid   gamma 2.2 by default, PQ only while a game is presenting HDR.
#
# Hybrid deliberately installs the PQ script. GetNativeColorimetry() only ever
# selects PQ output when IsHDR10() holds, and that needs eotf = pq in the display
# definition - a gamma22 script would pin the output to gamma 2.2 forever and no
# atom could move it. So the script says PQ and the runtime atom decides.
MODE_PQ = "pq"
MODE_G22 = "gamma22"
MODE_HYBRID = "hybrid"
MODES = (MODE_PQ, MODE_G22, MODE_HYBRID)

BUNDLED_SCRIPTS = {
    MODE_PQ: os.path.join(PLUGIN_DIR, "gamescope", "lenovo.legiongo2.oled.pq.lua"),
    MODE_G22: os.path.join(PLUGIN_DIR, "gamescope",
                           "lenovo.legiongo2.oled.gamma22.lua"),
}
MODE_SCRIPT = {MODE_PQ: MODE_PQ, MODE_G22: MODE_G22, MODE_HYBRID: MODE_PQ}

# 1.0.0 and 1.0.1 shipped the same PQ script under SCRIPT_NAME. It must remain
# recognisable after an update: treating it as third-party would overwrite the
# real .backup that the old release created for the user's previous script.
LEGACY_SCRIPT_SHA256 = {
    "eea882283c9d3b13456aba526750393ad80b4cba96593741dbd3c393e982a13a": MODE_PQ,
}

# Rebound by _select_mode(). The script helpers read this rather than taking a
# mode, so that the choice is made in exactly one place.
BUNDLED_SCRIPT = BUNDLED_SCRIPTS[MODE_PQ]


def _normalise_mode(mode) -> str:
    return mode if mode in MODES else MODE_PQ


def _select_mode(mode) -> str:
    """Point the script helpers at the variant this mode needs."""
    global BUNDLED_SCRIPT
    mode = _normalise_mode(mode)
    BUNDLED_SCRIPT = BUNDLED_SCRIPTS[MODE_SCRIPT[mode]]
    return mode


# The published EDID and our patch both outlive a plugin reload, but the bytes
# we would put back do not. Kept on disk so uninstalling still restores the file
# rather than leaving it trimmed with nothing maintaining it.
EDID_BACKUP = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "edid-original.bin")

EDID_BLOCK = 128
EXT_TAG_CTA = 0x02
EXT_TAG_DISPLAYID = 0x70

# panel_mode stays None until the user picks one, which is what puts the setup
# screen up. There is no safe default here: each mode trades something the user
# may care about, so guessing on their behalf would be the wrong call.
DEFAULT_SETTINGS = {
    "enabled": True,
    "edid_fix": True,
    "panel_mode": None,
    "active_mode": None,
    "brightness_baseline": None,
    "brightness_baseline_session": None,
    "hdr_baseline": None,
    "hdr_baseline_session": None,
    "reset_in_progress": False,
    "reset_session": None,
    "native_retired": False,
    "native_cleanup": None,
    "native_restart_session": None,
    "native_cleanup_note": "",
}

settings = SettingsManager(
    name="display_settings",
    settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR,
)
_settings_lock = threading.RLock()


def _offload(fn, *args):
    """Run a blocking call off the event loop."""
    return module_runtime.offload('display', fn, *args)


# â”€â”€ EDID parsing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _read_internal_edid() -> bytes:
    """Raw EDID of the internal panel, or b'' when there is none to read."""
    for path in sorted(glob.glob("/sys/class/drm/card*-eDP-*/edid")):
        try:
            with open(path, "rb") as f:
                data = f.read()
            if len(data) >= EDID_BLOCK:
                return data
        except OSError:
            continue
    return b""


def _decode_panel_id(edid: bytes):
    """(manufacturer, product code) from the EDID base block, or None."""
    if len(edid) < EDID_BLOCK or edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    packed = (edid[8] << 8) | edid[9]
    letters = "".join(
        chr(((packed >> shift) & 0x1F) + ord("A") - 1) for shift in (10, 5, 0)
    )
    product = edid[10] | (edid[11] << 8)
    return letters, product


def _identify_panel():
    """(matched, description). `matched` gates the brightness half."""
    ident = _decode_panel_id(_read_internal_edid())
    if ident is None:
        return False, "no internal panel EDID"
    name = KNOWN_PANELS.get(ident)
    if name:
        return True, name
    return False, f"unrecognised panel {ident[0]} 0x{ident[1]:04X}"


def _blocks(edid: bytes):
    """Yield (index, 128-byte block) for a well-formed EDID."""
    for i in range(len(edid) // EDID_BLOCK):
        yield i, edid[i * EDID_BLOCK:(i + 1) * EDID_BLOCK]


def _cta_max_luminance(edid: bytes):
    """Peak luminance in nits from the CTA HDR static metadata block, or None.

    CTA-861: luminance = 50 * 2**(code/32). A zero code means "undefined", which
    is exactly the case DXVK papers over with its 1499 nit placeholder.
    """
    for _, block in _blocks(edid):
        if not block or block[0] != EXT_TAG_CTA:
            continue
        dtd_start = block[2]
        if not 4 <= dtd_start <= EDID_BLOCK:
            continue
        j = 4
        while j < dtd_start:
            header = block[j]
            tag, size = header >> 5, header & 0x1F
            if size == 0:
                break
            # tag 7 is "extended"; extended tag 6 is HDR static metadata.
            if tag == 7 and size >= 4 and block[j + 1] == 6:
                code = block[j + 4]
                return 50.0 * (2.0 ** (code / 32.0)) if code else None
            j += 1 + size
    return None


def _has_displayid(edid: bytes) -> bool:
    return any(
        block and block[0] == EXT_TAG_DISPLAYID
        for i, block in _blocks(edid) if i > 0
    )


def _strip_displayid(edid: bytes):
    """EDID with every DisplayID extension removed, or None if there is none.

    Rewrites the extension count and the base block checksum so the result is
    still a valid EDID - a mismatched count is exactly the kind of thing a strict
    parser rejects, which would defeat the point.
    """
    if len(edid) < EDID_BLOCK * 2 or len(edid) % EDID_BLOCK:
        return None
    kept = [edid[:EDID_BLOCK]]
    dropped = 0
    for i, block in _blocks(edid):
        if i == 0:
            continue
        if block and block[0] == EXT_TAG_DISPLAYID:
            dropped += 1
            continue
        kept.append(block)
    if not dropped:
        return None

    out = bytearray(b"".join(kept))
    out[0x7E] = len(kept) - 1
    out[0x7F] = (256 - (sum(out[:0x7F]) & 0xFF)) & 0xFF
    return bytes(out)


# â”€â”€ Backlight â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _find_backlight() -> str:
    """Directory of the internal panel's backlight, preferring the amdgpu one."""
    candidates = sorted(glob.glob("/sys/class/backlight/*"))
    for path in candidates:
        if os.path.basename(path).startswith("amdgpu_bl"):
            return path
    return candidates[0] if candidates else ""


def _read_int(path: str):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _open_notify(bl_dir: str):
    """File descriptor armed for the kernel's change notification, or None.

    sysfs only reports a change relative to a completed read, so the attribute
    has to be read once up front to arm it.
    """
    path = os.path.join(bl_dir, NOTIFY_ATTR)
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY)
        os.read(fd, 64)
        return fd
    except OSError:
        if fd is not None:
            os.close(fd)
        return None


def _wait_for_change(fd, timeout_s: float) -> None:
    """Block until the backlight changes or `timeout_s` elapses.

    Falls back to a short sleep where the notification is not available, which
    turns the caller back into a poll loop without needing a second code path.
    """
    timeout_s = max(0.0, timeout_s)
    if fd is None:
        time.sleep(min(timeout_s, POLL_FALLBACK_S))
        return
    poller = select.poll()
    poller.register(fd, select.POLLPRI | select.POLLERR)
    try:
        if poller.poll(timeout_s * 1000.0):
            # Re-arm: the notification only fires once per completed read.
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                os.read(fd, 64)
            except OSError:
                pass
    finally:
        poller.unregister(fd)


# â”€â”€ gamescope atoms â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# Steam exports LD_LIBRARY_PATH pointing at its own bundled libraries and the
# plugin inherits it, so a system binary spawned from here loads Steam's copies
# of libreadline/libc and dies. Observed exactly once, as
# `bash: undefined symbol: rl_trim_arg_from_keyseq` from the restart button.
# Everything this plugin spawns is a system tool, so the loader variables go.


def _display_owner(display=None):
    try:
        number = (display or DISPLAY).rsplit(":", 1)[1].split(".", 1)[0]
        if not number.isdigit():
            return None
        return os.stat(os.path.join(X11_SOCKET_DIR, "X" + number)).st_uid
    except (AttributeError, IndexError, OSError):
        return None


def _xprop_env(display=None) -> dict:
    env = _system_env(DISPLAY=display or DISPLAY)
    owner = _display_owner(display)
    if pwd is not None and owner is not None:
        try:
            account = pwd.getpwuid(owner)
            env["HOME"] = account.pw_dir
            env["USER"] = account.pw_name
            env["LOGNAME"] = account.pw_name
            env["XDG_RUNTIME_DIR"] = f"/run/user/{owner}"
            env.pop("XAUTHORITY", None)
        except KeyError:
            pass
    return env


def _xprop_command(arguments, display=None):
    binary = "/usr/bin/xprop" if os.path.exists("/usr/bin/xprop") else "xprop"
    command = [binary] + list(arguments)
    get_euid = getattr(os, "geteuid", None)
    owner = _display_owner(display)
    if (get_euid is not None and get_euid() == 0 and owner not in (None, 0)
            and pwd is not None and os.path.exists("/usr/bin/runuser")):
        try:
            username = pwd.getpwuid(owner).pw_name
        except KeyError:
            return command
        return ["/usr/bin/runuser", "-u", username, "--"] + command
    return command


def _parse_prop_line(line: str) -> tuple[str, str | None] | None:
    for name in WATCHED_ATOMS:
        if line.startswith(name + "(") and "= " in line:
            return name, line.split("= ", 1)[1].strip()
        if line.startswith(name + ":") and "not found" in line:
            return name, None
    return None


def _set_prop_cache(values: dict, display: str, *, ready: bool = True) -> None:
    global _prop_cache_display, _prop_cache_ready
    with _prop_cache_lock:
        _prop_cache.clear()
        _prop_cache.update(values)
        _prop_cache_display = display
        _prop_cache_ready = ready


def _update_prop_cache(line: str, display: str) -> None:
    global _prop_cache_display, _prop_cache_ready
    parsed = _parse_prop_line(line)
    if parsed is None:
        return
    name, value = parsed
    with _prop_cache_lock:
        if _prop_cache_display != display:
            _prop_cache.clear()
            _prop_cache_display = display
        if value is None:
            _prop_cache.pop(name, None)
        else:
            _prop_cache[name] = value
        _prop_cache_ready = True


def _invalidate_prop_cache(display: str | None = None) -> None:
    global _prop_cache_display, _prop_cache_ready
    with _prop_cache_lock:
        if display is not None and _prop_cache_display != display:
            return
        _prop_cache.clear()
        _prop_cache_display = None
        _prop_cache_ready = False


def _cached_props(names) -> dict | None:
    with _prop_cache_lock:
        if not _prop_cache_ready or _prop_cache_display != DISPLAY:
            return None
        return {name: _prop_cache[name] for name in names if name in _prop_cache}


def _read_props_uncached(names, display=None) -> dict:
    """Raw text of each atom on the gamescope root window, in one xprop call.

    Everything this plugin needs comes from the same read, so the string atom
    rides along with the numeric ones rather than costing a second process.
    """
    try:
        out = subprocess.run(
            _xprop_command(["-root"] + list(names), display),
            capture_output=True, text=True, timeout=3, env=_xprop_env(display),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    values = {}
    for line in out.splitlines():
        for name in names:
            if line.startswith(name + "(") and "= " in line:
                values[name] = line.split("= ", 1)[1].strip()
    return values


def _read_props(names) -> dict:
    cached = _cached_props(names)
    return cached if cached is not None else _read_props_uncached(names)


async def _terminate_prop_process(process) -> None:
    if process is None or process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows is only a unit-test host
            process.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
        return
    except asyncio.TimeoutError:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        pass


async def _watch_properties() -> None:
    """Keep one xprop process alive and cache Gamescope property changes."""
    while True:
        process = None
        display = None
        try:
            if not await _offload(_pick_display):
                _invalidate_prop_cache()
                await asyncio.sleep(GATE_INTERVAL_S)
                continue
            display = DISPLAY
            initial = await _offload(_read_props_uncached, WATCHED_ATOMS, display)
            _set_prop_cache(initial, display, ready=bool(initial))
            command = _xprop_command(["-spy", "-root"] + list(WATCHED_ATOMS), display)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_xprop_env(display),
                start_new_session=os.name == "posix",
            )
            decky.logger.info(f"{LOG} watching gamescope properties on {display}")
            if process.stdout is None:
                raise RuntimeError("xprop watcher has no stdout")
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                _update_prop_cache(raw.decode("utf-8", errors="replace").strip(), display)
            decky.logger.info(f"{LOG} gamescope property watcher disconnected")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            decky.logger.warning(f"{LOG} gamescope property watcher: {exc}")
        finally:
            await _terminate_prop_process(process)
            if display is not None:
                _invalidate_prop_cache(display)
        await asyncio.sleep(0.5)


def _as_int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _as_path(raw):
    """The quoted value of a UTF8_STRING property, or None."""
    if not raw or '"' not in raw:
        return None
    return raw.split('"')[1] or None


def _probe_gamescope(display: str) -> tuple:
    try:
        out = subprocess.run(
            _xprop_command(["-root"], display),
            capture_output=True, text=True, timeout=5,
            env=_xprop_env(display),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0, False
    lines = out.splitlines()
    count = sum(1 for line in lines if line.startswith("GAMESCOPE"))
    has_control_root = any(
        line.startswith(ATOM_IS_EXTERNAL + "(") for line in lines)
    return count, has_control_root


def _display_candidates():
    """Every X server on the box, read off its socket rather than guessed."""
    try:
        names = sorted(name for name in os.listdir(X11_SOCKET_DIR)
                       if name.startswith("X") and name[1:].isdigit())
    except OSError:
        return DISPLAY_FALLBACK
    return tuple(f":{name[1:]}" for name in names) or DISPLAY_FALLBACK


def _pick_display() -> bool:
    """Settle on an X server gamescope is actually behind."""
    global DISPLAY
    best, best_score = None, (False, 0)
    for candidate in _display_candidates():
        count, control_root = _probe_gamescope(candidate)
        score = (control_root, count)
        if score > best_score:
            best, best_score = candidate, score
    if best is None:
        return False
    if best != DISPLAY:
        decky.logger.info(f"{LOG} talking to gamescope on {best} "
                          f"({best_score[1]} atoms"
                          f"{', control root' if best_score[0] else ''})")
    if best != DISPLAY:
        _invalidate_prop_cache()
    DISPLAY = best
    return True


def _as_float(raw):
    """gamescope stores floats bit-cast into a CARDINAL."""
    raw = _as_int(raw)
    if raw is None:
        return None
    try:
        return struct.unpack("f", struct.pack("I", raw & 0xFFFFFFFF))[0]
    except (OverflowError, struct.error):
        return None


def _float_raw(value: float) -> int:
    """A float the way gamescope stores it: bit-cast into a CARDINAL."""
    return struct.unpack("I", struct.pack("f", float(value)))[0]


def _write_atom_int(name: str, value: int) -> bool:
    """Set a CARDINAL atom on the root window gamescope is listening on."""
    try:
        subprocess.run(
            _xprop_command(
                ["-root", "-f", name, "32c", "-set", name, str(int(value))]
            ),
            capture_output=True, timeout=3, env=_xprop_env(), check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _write_nits(nits: float) -> bool:
    return _write_atom_int(ATOM_SDR_NITS, _float_raw(nits))


def _millinits_to_nits(raw: int) -> float:
    """Map the kernel's eDP AUX millinits to gamescope's nits."""
    return raw / 1000.0


_script_bytes_cache = {}


def _read_script_bytes(path: str):
    try:
        info = os.stat(path)
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except OSError:
        _script_bytes_cache.pop(path, None)
        return None
    cached = _script_bytes_cache.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]
    data = _read_bytes(path)
    _script_bytes_cache[path] = (signature, data)
    return data


def _read_bytes(path: str):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _script_variant(data):
    """Return the EOTF variant we shipped, ignoring checkout line endings."""
    if data is None:
        return None
    normalised = data.replace(b"\r\n", b"\n")
    for mode, path in BUNDLED_SCRIPTS.items():
        bundled = _read_script_bytes(path)
        if bundled is not None and (
                data == bundled
                or normalised == bundled.replace(b"\r\n", b"\n")):
            return mode
    digest = hashlib.sha256(data).hexdigest()
    variant = LEGACY_SCRIPT_SHA256.get(digest)
    if variant is None and b"\r\n" in data:
        # Release zips can be built from a Windows checkout or Linux CI. The Lua
        # is identical either way, but an exact hash is not. Normalising only
        # line endings keeps the legacy allowlist exact without treating a
        # substantively edited script as ours.
        digest = hashlib.sha256(normalised).hexdigest()
        variant = LEGACY_SCRIPT_SHA256.get(digest)
    return variant


def _is_our_script(data) -> bool:
    """True when these bytes are one of the variants we ship.

    Asked of whatever is already installed, so that swapping modes is not
    mistaken for displacing somebody else's script. Getting this wrong once
    meant the backup of a hand-written script was overwritten with our own
    previous variant, and there was then nothing left to restore.
    """
    if data is None:
        return False
    # BUNDLED_SCRIPT first: it is whichever variant the current mode selected,
    # and the one place that is rebound rather than read from the table.
    if data == _read_script_bytes(BUNDLED_SCRIPT):
        return True
    return _script_variant(data) is not None


def _loaded_script_variant(started=None):
    """Variant the current gamescope could have loaded, or None if unknowable.

    A file written after gamescope started cannot describe the running session.
    The Plugin class remembers the last known answer across later mode changes,
    so switching pq -> gamma22 -> pq does not ask for a meaningless restart.
    """
    started = _gamescope_start_time() if started is None else started
    if started is None:
        return None
    try:
        if os.path.getmtime(INSTALLED_SCRIPT) > started:
            return None
    except OSError:
        return None
    return _script_variant(_read_script_bytes(INSTALLED_SCRIPT))


def _script_status() -> tuple:
    """(installed, note). `installed` means our exact script is already in place."""
    ours = _read_script_bytes(BUNDLED_SCRIPT)
    if ours is None:
        return False, "the bundled display script is missing from the plugin"
    theirs = _read_script_bytes(INSTALLED_SCRIPT)
    if theirs is None:
        return False, "no display script installed for this panel"
    if theirs == ours:
        return True, "display script installed"
    return False, "a different display script is installed and will be replaced"


def _install_script() -> tuple:
    """Put our script in place, backing up anything already there. (ok, note)."""
    ours = _read_script_bytes(BUNDLED_SCRIPT)
    if ours is None:
        return False, "the bundled display script is missing from the plugin"
    try:
        os.makedirs(SCRIPT_DIR, exist_ok=True)
    except OSError as e:
        return False, f"could not create {SCRIPT_DIR}: {e}"

    existing = _read_script_bytes(INSTALLED_SCRIPT)
    if existing == ours:
        # Nothing to write. Rewriting identical bytes would still move the file's
        # mtime, and _restart_needed() reads exactly that - which is how switching
        # between pq and hybrid, two modes that share a script, used to demand a
        # Game Mode restart that changed nothing.
        return True, "installed"

    if existing is not None and not _is_our_script(existing):
        # Keep whatever was there. Someone who had hand-tuned a script should be
        # able to get it back without hunting for it. Our own previous variant is
        # explicitly not worth keeping: backing that up on every mode change
        # would bury the script the user actually wants back.
        backup = INSTALLED_SCRIPT + ".backup"
        if not _write_file_atomically(backup, existing):
            return False, "could not back up the existing display script"

    if not _write_file_atomically(INSTALLED_SCRIPT, ours):
        return False, f"could not write {INSTALLED_SCRIPT}"
    return True, "installed"


_gamescope_pid = None
_gamescope_scan_after = 0.0


def _gamescope_start_time():
    """Identify the live session, scanning /proc only after losing its PID."""
    global _gamescope_pid, _gamescope_scan_after
    def started(pid):
        with open(f"/proc/{pid}/comm") as handle:
            if handle.read().strip() != "gamescope-wl":
                return None
        return os.stat(f"/proc/{pid}").st_mtime
    if _gamescope_pid is not None:
        try:
            value = started(_gamescope_pid)
            if value is not None:
                return value
        except OSError:
            pass
        _gamescope_pid = None
        _gamescope_scan_after = 0.0
    now = time.monotonic()
    if now < _gamescope_scan_after:
        return None
    _gamescope_scan_after = now + 2.0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            value = started(entry)
            if value is not None:
                _gamescope_pid = entry
                return value
        except OSError:
            continue
    return None


_UNKNOWN_VARIANT = object()


def _restart_needed(loaded_variant=_UNKNOWN_VARIANT) -> bool:
    """True when the running gamescope did not load the selected script.

    File mtime is enough on a cold read. During this plugin process we can do
    better: remembering the loaded variant means pq -> gamma22 -> pq does not
    request a restart when the session has been running pq the whole time.
    """
    started = _gamescope_start_time()
    if started is None:
        # Cannot tell, so do not nag.
        return False
    if loaded_variant is _UNKNOWN_VARIANT:
        loaded_variant = _loaded_script_variant(started)
    wanted = _script_variant(_read_script_bytes(BUNDLED_SCRIPT))
    if wanted is not None:
        return loaded_variant != wanted
    # Test seams and development builds may point BUNDLED_SCRIPT at a file that
    # is not in the production variant table. Fall back to the original mtime
    # rule rather than silently declaring it loaded.
    try:
        return started < os.path.getmtime(INSTALLED_SCRIPT)
    except OSError:
        return False


def _uninstall_script() -> str:
    """Undo what setup did, so removing the plugin is not a downgrade.

    Leaving our script behind would strand the panel in PQ with nothing left to
    restore brightness control - worse than before the plugin was installed. If
    setup displaced someone else's script, that one goes back.
    """
    backup = INSTALLED_SCRIPT + ".backup"
    current = _read_script_bytes(INSTALLED_SCRIPT)

    if current is not None and not _is_our_script(current):
        # Someone replaced it after we installed; leave their choice alone.
        # Any of our variants counts as ours, not just the current mode's, or
        # uninstalling after a mode change would refuse to clean up.
        return "left a third-party display script in place"

    saved = _read_bytes(backup)
    if saved is not None:
        if _write_file_atomically(INSTALLED_SCRIPT, saved):
            try:
                os.unlink(backup)
            except OSError:
                pass
            return "restored the display script that was there before"
        return "could not restore the previous display script"

    if current is not None:
        try:
            os.unlink(INSTALLED_SCRIPT)
            return "removed the display script"
        except OSError:
            return "could not remove the display script"
    return "no display script to remove"


def _restart_session() -> tuple:
    """Restart Game Mode so gamescope picks the script up. (ok, note).

    Absolute paths on purpose: the plugin is spawned by the loader, not by a
    login shell, and cannot be assumed to have a useful PATH. Failing silently
    here once already looked to the user like a dead button.
    """
    env = _system_env()
    owner = _gamescope_owner_uid()
    account = None
    if pwd is not None and owner is not None:
        try:
            account = pwd.getpwuid(owner)
        except KeyError:
            pass
    if account is None and pwd is not None:
        try:
            candidate = pwd.getpwnam("deck")
            if candidate is not None and os.path.exists(
                    f"/run/user/{candidate.pw_uid}/bus"):
                account = candidate
                owner = candidate.pw_uid
        except (KeyError, AttributeError):
            pass

    runtime = f"/run/user/{owner}" if owner is not None else None
    if runtime:
        env["XDG_RUNTIME_DIR"] = runtime
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    if account is not None:
        env["HOME"] = account.pw_dir
        env["USER"] = account.pw_name
        env["LOGNAME"] = account.pw_name

    def session_command(argv):
        get_euid = getattr(os, "geteuid", None)
        if (account is not None and get_euid is not None and get_euid() == 0
                and account.pw_uid != 0
                and os.path.exists("/usr/bin/runuser")):
            return [
                "/usr/bin/runuser", "-u", account.pw_name, "--",
                "/usr/bin/env",
                f"HOME={account.pw_dir}",
                f"USER={account.pw_name}",
                f"LOGNAME={account.pw_name}",
                f"XDG_RUNTIME_DIR={runtime}",
                f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
                *argv,
            ]
        return argv

    attempts = (
        (["/usr/bin/systemctl", "--user", "restart", "gamescope-session.target"],
         "systemctl"),
        # Valve's own session switcher, in case the unit layout ever changes.
        (["/usr/bin/steamos-session-select", "gamescope"], "steamos-session-select"),
    )

    failures = []
    for argv, name in attempts:
        if not os.path.exists(argv[0]):
            failures.append(f"{name} not found")
            continue
        try:
            r = subprocess.run(session_command(argv), capture_output=True, text=True,
                               timeout=20, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            failures.append(f"{name}: {e}")
            continue
        if r.returncode == 0:
            return True, f"restarting via {name}"
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:]
        failures.append(f"{name}: {tail[0] if tail else 'exited %d' % r.returncode}")

    return False, "; ".join(failures) or "no restart method available"


def _write_file_atomically(path: str, data: bytes) -> bool:
    """Durably replace a regular file without following staging/target links."""
    from safe_settings import _atomic_write_bytes, UnsafeSettingsPath
    try:
        # Gamescope runs unprivileged and must be able to read installed Lua/EDID.
        _atomic_write_bytes(path, data, mode=0o644)
        return True
    except (OSError, UnsafeSettingsPath):
        return False


def _restore_file_snapshot(path: str, existed: bool, data) -> bool:
    """Best-effort rollback of one file captured before a transaction."""
    if existed:
        return data is not None and _write_file_atomically(path, data)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def _gamescope_owner_uid():
    """Owner of the X socket for the gamescope display, when POSIX exposes it."""
    try:
        number = DISPLAY.rsplit(":", 1)[1].split(".", 1)[0]
        if not number.isdigit():
            return None
        return os.stat(os.path.join(X11_SOCKET_DIR, "X" + number)).st_uid
    except (AttributeError, IndexError, OSError):
        return None


def _published_edid_is_safe(path: str, opened_stat=None) -> bool:
    """Only modify a regular EDID file owned by the gamescope session user.

    The path comes from an X property. Decky commonly runs plugins as root, so a
    forged property must not turn this narrowly scoped workaround into an
    arbitrary privileged file write. On SteamOS gamescope creates this file as
    the desktop user; a root-owned target is never its published EDID.
    """
    if not path or not os.path.isabs(path):
        return False
    try:
        info = opened_stat if opened_stat is not None else os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False

    get_euid = getattr(os, "geteuid", None)
    owner = getattr(info, "st_uid", None)
    if get_euid is None or owner is None:
        return True
    gamescope_owner = _gamescope_owner_uid()
    if gamescope_owner is not None:
        return owner == gamescope_owner
    euid = get_euid()
    return owner != 0 if euid == 0 else owner == euid


def _rewrite_file_if_unchanged(path: str, expected: bytes,
                               replacement: bytes) -> str:
    """Rewrite the verified inode without ever replacing a newer connector.

    gamescope publishes connector changes with an atomic rename. Keeping the
    verified file descriptor open means a rename after our check only makes us
    update the now-unlinked old inode; the new display's path stays untouched.
    Returns ``written``, ``changed`` or ``failed``.
    """
    try:
        with open(path, "r+b", buffering=0) as f:
            if f.read() != expected:
                return "changed"

            opened = os.fstat(f.fileno())
            if not _published_edid_is_safe(path, opened):
                return "failed"
            published = os.stat(path)
            if ((opened.st_dev, opened.st_ino)
                    != (published.st_dev, published.st_ino)):
                return "changed"

            try:
                f.seek(0)
                view = memoryview(replacement)
                while view:
                    written = f.write(view)
                    if not written:
                        raise OSError("short write")
                    view = view[written:]
                f.truncate()
                f.flush()
                os.fsync(f.fileno())
            except OSError:
                # A failed in-place update may have written a prefix. Restore the
                # verified bytes on the same inode before admitting failure.
                try:
                    f.seek(0)
                    f.write(expected)
                    f.truncate()
                    f.flush()
                    os.fsync(f.fileno())
                except OSError:
                    pass
                return "failed"

            # A connector can change while the old descriptor is being written.
            # That is safe, but report it as changed rather than claiming the new
            # published path is already patched.
            published = os.stat(path)
            if ((opened.st_dev, opened.st_ino)
                    != (published.st_dev, published.st_ino)):
                return "changed"
            return "written"
    except OSError:
        return "failed"


class Plugin:
    # Everything the panel reports back to the UI.
    _state = {
        # Setup gate - nothing else is shown until a mode is chosen and its
        # display script is in place
        "panel_mode": None,       # None until the user picks one
        "active_mode": None,      # what the running gamescope session is using
        "setup_done": False,
        "setup_note": "checking",
        "setup_error": "",
        "settings_error": "",
        "restart_pending": False,
        "restart_error": "",
        "reset_in_progress": False,
        "reset_restart_pending": False,
        "reset_error": "",
        "reset_note": "",
        "native_support_detected": False,
        "native_support_reason": "",
        "native_cleanup_pending": False,
        "native_cleanup_error": "",
        "native_restart_pending": False,
        # Hybrid half - idle in the other two modes
        "hybrid_reason": "",
        "hdr_now": False,         # is the panel in PQ right now
        "gamescope_reachable": False,
        "panel_ok": False,
        "panel_desc": "",
        "backlight": "",
        "enabled": True,
        "active": False,          # currently forwarding
        "reason": "starting up",
        "nits": 0.0,              # what we last wrote
        "max_nits": 0.0,
        # EDID half
        "edid_fix": True,
        "edid_patched": False,
        "edid_reason": "starting up",
        "edid_game_nits": 0.0,    # peak luminance games will read
    }
    _baseline = None              # atom value before we first touched it
    _last_written = None
    _edid_original = None         # untouched bytes, for putting back
    _edid_path = None
    _task = None
    _prop_task = None
    _hdr_fights = 0               # corrections in a row that did not stick
    _hdr_backoff_until = 0.0
    _hdr_baseline = None          # HDR mode as we first found it, for putting back
    _session_note = ""
    _loaded_script_variant = None
    _gamescope_started_at = None
    _session_retry_after = 0.0
    _hybrid_suspended_external = False
    _hybrid_pq_hold_until = 0.0
    _hybrid_sdr_since = None
    _edid_recheck_until = 0.0
    _runtime_lock = threading.RLock()
    _edid_lock = threading.RLock()
    _mode_lock = asyncio.Lock()
    _native_probe_after = 0.0
    _native_probe_session = None
    _native_detection = {}

    @staticmethod
    def native_display_active() -> bool:
        """Whether legacy display writes are fenced; EDID remains independent."""
        return bool(Plugin._state.get("native_support_detected")
                    or Plugin._state.get("native_cleanup_pending")
                    or Plugin._state.get("native_restart_pending")
                    or settings.getSetting("native_retired", False) is True
                    or settings.getSetting("native_cleanup", None) is not None)

    @staticmethod
    def _native_store(values: dict) -> None:
        # Unlike advisory runtime metadata, the handoff journal must be durable
        # before any owned file or atom can be changed.
        with _settings_lock:
            settings.read()
            Plugin._require_settings()
            payload = dict(settings.settings)
            payload.update(values)
            settings.replace(payload)

    @staticmethod
    def _native_remove_scripts() -> list[str]:
        notes = []
        for path, label in ((INSTALLED_SCRIPT, "display script"),
                            (INSTALLED_SCRIPT + ".backup", "display-script backup")):
            if not os.path.lexists(path):
                continue
            if os.path.islink(path) or not stat.S_ISREG(os.lstat(path).st_mode):
                notes.append(f"Preserved an unowned {label}.")
                continue
            data = _read_bytes(path)
            if data is None:
                raise RuntimeError(f"Could not read the {label} safely.")
            if not _is_our_script(data):
                note = f"Preserved a third-party {label}; it was not restored or replaced."
                if path == INSTALLED_SCRIPT:
                    note += " It may override the system display profile."
                notes.append(note)
                continue
            # Do not delete a file replaced after its ownership was checked.
            if _read_bytes(path) != data:
                raise RuntimeError(f"The {label} changed during native display cleanup.")
            os.unlink(path)
        return notes

    @staticmethod
    def _native_write_atom(name: str, value: int, session) -> None:
        if not Plugin._same_session(session, _gamescope_start_time()):
            raise RuntimeError("Gaming Mode changed during native cleanup; recovery will retry.")
        if (not _write_atom_int(name, value)
                or _as_int(_read_props_uncached([name]).get(name)) != value):
            raise RuntimeError(f"Gamescope did not confirm restoration of {name}.")

    @staticmethod
    def _native_cleanup_locked(journal: dict, session) -> None:
        if (not isinstance(journal, dict) or journal.get("version") != 1
                or not isinstance(journal.get("brightness"), dict)
                or not isinstance(journal.get("hdr"), dict)):
            raise RuntimeError("The native display cleanup journal is invalid; recovery was preserved.")
        # The restart marker is persisted before deleting Lua: a Decky reload
        # cannot infer what the current gamescope loaded from a now-missing file.
        notes = Plugin._native_remove_scripts()
        if notes:
            Plugin._state["native_support_reason"] += " " + " ".join(notes)
        brightness, hdr = journal["brightness"], journal["hdr"]
        owns_runtime = bool(brightness.get("owned") or hdr.get("owned"))
        original_session = journal.get("session")
        if owns_runtime and session is None:
            raise RuntimeError("Gaming Mode session is unknown; native cleanup will retry without changing display atoms.")
        if owns_runtime and original_session is None:
            # A session first observed after an unknown one is not proof that
            # gamescope restarted. Fence this session until a verified change.
            Plugin._native_store({"native_cleanup": {**journal, "session": session,
                                                       "wait_for_new_session": True},
                                  "native_restart_session": {"session": session}})
            raise RuntimeError("Previous display ownership is unknown. Restart Gaming Mode to finish native cleanup.")
        if owns_runtime and Plugin._same_session(original_session, session):
            if journal.get("wait_for_new_session"):
                raise RuntimeError("Previous display ownership is unknown. Restart Gaming Mode to finish native cleanup.")
            names = [ATOM_IS_EXTERNAL, ATOM_SDR_NITS, ATOM_HDR_ENABLED, ATOM_FORCE_HDR_SUPPORT]
            Plugin._session_props(names)
            # The property watcher can lag a concurrent Steam update. Ownership
            # must be compared with live values immediately before restoration.
            props = _read_props_uncached(names)
            external = _as_int(props.get(ATOM_IS_EXTERNAL))
            if external not in (0, 1):
                raise RuntimeError("The active display is unknown; native cleanup will retry without changing its mode.")
            if external == 0 and brightness.get("owned"):
                target = brightness.get("baseline")
                if type(target) not in (int, float) or not math.isfinite(target) or target <= 0:
                    raise RuntimeError("The previous brightness baseline is unavailable. Restart Gaming Mode to finish native cleanup.")
                live = _as_float(_as_int(props.get(ATOM_SDR_NITS)))
                expected = brightness.get("expected")
                if live is None:
                    raise RuntimeError("The current brightness value could not be read; native cleanup will retry.")
                if abs(live - target) > 0.01:
                    if expected is None:
                        raise RuntimeError("Brightness ownership cannot be confirmed after reload. Restart Gaming Mode to finish native cleanup.")
                    if abs(live - expected) <= 0.01:
                        Plugin._native_write_atom(ATOM_SDR_NITS, _float_raw(target), session)
                    # An external writer replaced our last value: leave it alone.
            if hdr.get("owned"):
                if hdr.get("force_owned"):
                    forced = _as_int(props.get(ATOM_FORCE_HDR_SUPPORT))
                    if forced is None:
                        raise RuntimeError("HDR support ownership could not be read; native cleanup will retry.")
                    if forced == 1:
                        Plugin._native_write_atom(ATOM_FORCE_HDR_SUPPORT, 0, session)
                        Plugin._edid_recheck_until = time.monotonic() + GATE_INTERVAL_S
                if external == 0:
                    target = hdr.get("baseline")
                    if type(target) is not bool:
                        raise RuntimeError("The previous HDR baseline is unavailable. Restart Gaming Mode to finish native cleanup.")
                    live = _as_int(props.get(ATOM_HDR_ENABLED))
                    if live is None:
                        raise RuntimeError("The current HDR mode could not be read; native cleanup will retry.")
                    expected = hdr.get("expected")
                    if bool(live) != target:
                        if expected is None:
                            raise RuntimeError("HDR ownership cannot be confirmed after reload. Restart Gaming Mode to finish native cleanup.")
                        if live == expected:
                            Plugin._native_write_atom(ATOM_HDR_ENABLED, int(target), session)
        # A different gamescope owns fresh atoms. Never apply old baselines to it.
        Plugin._native_store({"native_cleanup": None, "native_retired": True,
                              "native_cleanup_note": " ".join(notes),
                              "active_mode": None, "brightness_baseline": None,
                              "brightness_baseline_session": None, "hdr_baseline": None,
                              "hdr_baseline_session": None, "reset_in_progress": False,
                              "reset_session": None})
        Plugin._baseline = None
        Plugin._hdr_baseline = None
        Plugin._last_written = None
        Plugin._state.update(active=False, active_mode=None, setup_done=False,
                             restart_pending=False, native_cleanup_pending=False,
                             native_cleanup_error="", reset_in_progress=False,
                             reset_restart_pending=False, reset_error="")
        Plugin._reset_hybrid_timing()
        Plugin._hdr_mode_settled()

    @staticmethod
    def _refresh_native_support(force=False) -> None:
        with Plugin._runtime_lock:
            try:
                Plugin._require_settings()
                session = _gamescope_start_time()
                now = time.monotonic()
                if (force or now >= Plugin._native_probe_after
                        or Plugin._native_probe_session != session):
                    Plugin._native_detection = display_native.detect_native_display_support()
                    Plugin._native_probe_after = now + 30.0
                    Plugin._native_probe_session = session
                    decky.logger.debug(f"{LOG} native display probe: {Plugin._native_detection.get('reason', '')}")
                detection = Plugin._native_detection
                journal = settings.getSetting("native_cleanup", None)
                retired = settings.getSetting("native_retired", False) is True
                supported = detection.get("supported") is True
                if detection.get("status") == "absent" and journal is None:
                    marker = settings.getSetting("native_restart_session", None)
                    legacy_session = (session is not None and isinstance(marker, dict)
                                      and marker.get("session") is not None
                                      and not Plugin._same_session(marker["session"], session))
                    if retired and not legacy_session:
                        # Replacing the installed package does not replace the
                        # executable already running in this Gaming Mode session.
                        if marker is None:
                            Plugin._native_store({"native_restart_session": {"session": session}})
                        elif isinstance(marker, dict) and marker.get("session") is None and session is not None:
                            Plugin._native_store({"native_restart_session": {"session": session}})
                        Plugin._state.update(native_support_detected=True, native_cleanup_pending=False,
                                             native_cleanup_error="", native_restart_pending=True,
                                             native_support_reason="Restart Gaming Mode after changing the installed gamescope version before enabling legacy display controls.")
                        return
                    if retired or settings.getSetting("native_restart_session", None) is not None:
                        Plugin._native_store({"native_retired": False, "native_restart_session": None})
                    Plugin._state.update(native_support_detected=False, native_cleanup_pending=False,
                                         native_cleanup_error="", native_restart_pending=False,
                                         native_support_reason="")
                    return
                active = supported or retired or journal is not None
                reason = ("Gamescope includes the native Legion Go 2 display profile. "
                          "Companion's display fix is no longer needed." if supported else
                          "Native support was detected earlier. It cannot currently be verified, "
                          "so the retired display fix remains disabled." if active else "")
                Plugin._state.update(native_support_detected=active,
                                     native_support_reason=(reason + " "
                                         + settings.getSetting("native_cleanup_note", "")).strip(),
                                     native_cleanup_pending=journal is not None)
                if not active:
                    return
                Plugin._state["active"] = False
                Plugin._state["setup_done"] = False
                Plugin._state["reason"] = "Native gamescope display support; Companion brightness and Hybrid fixes are retired."
                if journal is None and not retired:
                    # Recover baselines only from the session that captured them.
                    previous_mode = (Plugin._state.get("active_mode")
                                     or settings.getSetting("active_mode", None))
                    previous_session = Plugin._gamescope_started_at
                    Plugin._sync_loaded_script()
                    brightness = Plugin._baseline
                    if type(brightness) not in (int, float) or not math.isfinite(brightness) or brightness <= 0:
                        brightness = None
                    hdr = Plugin._hdr_baseline if type(Plugin._hdr_baseline) is bool else None
                    own_script = _is_our_script(_read_script_bytes(INSTALLED_SCRIPT))
                    pq_ownership = (previous_mode == MODE_PQ
                                    and (own_script or Plugin._loaded_script_variant == MODE_PQ))
                    # Unknown legacy runtime state cannot justify writing a
                    # default, but its ownership still requires a session change.
                    possible_brightness = previous_mode == MODE_PQ and own_script
                    journal = {"version": 1, "session": session,
                               "brightness": {"owned": brightness is not None or Plugin._last_written is not None or possible_brightness,
                                              "baseline": brightness, "expected": Plugin._last_written},
                               "hdr": {"owned": hdr is not None or pq_ownership,
                                       "force_owned": hdr is not None, "baseline": hdr,
                                       "expected": (int(Plugin._state.get("hdr_now", False))
                                                    if Plugin._same_session(previous_session, session) else None)}}
                    # Presence proves installation, not that this session loaded
                    # the system profile. Require one session change on the first
                    # handover, without depending on gamescope binary internals.
                    Plugin._state["native_cleanup_pending"] = True
                    Plugin._native_store({"native_cleanup": journal,
                                          "native_restart_session": {"session": session}})
                if journal is not None:
                    Plugin._native_cleanup_locked(journal, session)
                marker = settings.getSetting("native_restart_session", None)
                pending = isinstance(marker, dict)
                if pending and session is not None:
                    previous = marker.get("session")
                    if previous is None:
                        Plugin._native_store({"native_restart_session": {"session": session}})
                    elif not Plugin._same_session(previous, session) and supported:
                        Plugin._native_store({"native_restart_session": None})
                        pending = False
                Plugin._state["native_restart_pending"] = pending
                Plugin._state["native_cleanup_error"] = ""
                Plugin._state["active_mode"] = None
                Plugin._state["active"] = False
                Plugin._state["setup_done"] = False
            except Exception as error:
                # Native ownership withdrawal must never stop the independent
                # EDID pass or permit legacy writers to resume after a failure.
                if Plugin.native_display_active() or settings.getSetting("native_cleanup", None) is not None:
                    Plugin._state["native_cleanup_pending"] = True
                    Plugin._state["native_cleanup_error"] = str(error)
                    Plugin._state["native_restart_pending"] = settings.getSetting("native_restart_session", None) is not None
                else:
                    Plugin._state["native_support_reason"] = str(error)

    async def cleanup_native_display(self) -> dict:
        async with Plugin._mode_lock:
            await module_runtime.complete(_offload(Plugin._refresh_native_support, True))
        return dict(Plugin._state)

    @staticmethod
    def _require_settings() -> None:
        error = getattr(settings, "recovery_error", "")
        if error:
            message = f"Display settings could not be recovered. Restore a valid settings copy and restart Decky. {error}"
            Plugin._state["settings_error"] = message
            Plugin._state["setup_error"] = message
            raise CorruptSettings(message)

    @staticmethod
    def _read_settings() -> None:
        with _settings_lock:
            settings.read()
            Plugin._require_settings()

    @staticmethod
    def _store_settings(values: dict) -> None:
        """Persist one logical runtime transaction in a single commit."""
        try:
            with _settings_lock:
                settings.read()
                Plugin._require_settings()
                for name, value in values.items():
                    settings.setSetting(name, value)
                settings.commit()
        except CorruptSettings:
            raise
        except Exception as e:
            decky.logger.warning(
                f"{LOG} could not save {', '.join(values.keys())}: {e}")

    @staticmethod
    def _store_setting(name, value) -> None:
        Plugin._store_settings({name: value})

    @staticmethod
    def _set_active_mode(mode) -> None:
        mode = mode if mode in MODES else None
        Plugin._state["active_mode"] = mode
        Plugin._store_setting("active_mode", mode)

    @staticmethod
    def _same_session(saved, current) -> bool:
        try:
            return abs(float(saved) - float(current)) < 0.01
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _sync_loaded_script() -> None:
        """Notice a new gamescope and bind runtime state to what it loaded."""
        started = _gamescope_start_time()
        if started is None or Plugin._same_session(
                Plugin._gamescope_started_at, started):
            return

        Plugin._gamescope_started_at = started
        Plugin._loaded_script_variant = _loaded_script_variant(started)
        # Atoms belong to the gamescope process. Never carry ownership of a
        # value written to the previous process into this fresh session.
        Plugin._last_written = None
        Plugin._state["active"] = False
        Plugin._state["restart_error"] = ""
        Plugin._hybrid_suspended_external = False
        Plugin._reset_hybrid_timing()
        Plugin._hdr_mode_settled()

        # Both baselines belong to one X/gamescope session. Recover them across a
        # Decky reload, but never carry them into a new session whose atoms reset.
        brightness_session = settings.getSetting(
            "brightness_baseline_session",
            DEFAULT_SETTINGS["brightness_baseline_session"])
        if Plugin._same_session(brightness_session, started):
            saved = settings.getSetting(
                "brightness_baseline", DEFAULT_SETTINGS["brightness_baseline"])
            try:
                Plugin._baseline = float(saved) if saved is not None else None
            except (TypeError, ValueError):
                Plugin._baseline = None
        else:
            Plugin._baseline = None

        hdr_session = settings.getSetting(
            "hdr_baseline_session", DEFAULT_SETTINGS["hdr_baseline_session"])
        if Plugin._same_session(hdr_session, started):
            saved = settings.getSetting(
                "hdr_baseline", DEFAULT_SETTINGS["hdr_baseline"])
            Plugin._hdr_baseline = (
                saved if type(saved) is bool else None)
        else:
            Plugin._hdr_baseline = None

        wanted = Plugin._state.get("panel_mode")
        stored_active = Plugin._state.get("active_mode")
        loaded = Plugin._loaded_script_variant
        if wanted in MODES and loaded == MODE_SCRIPT[wanted]:
            active = wanted
        elif stored_active in MODES and loaded == MODE_SCRIPT[stored_active]:
            active = stored_active
        elif loaded == MODE_PQ:
            # The 1.0.x script is PQ and had no explicit mode setting.
            active = MODE_PQ
        elif loaded == MODE_G22:
            active = MODE_G22
        else:
            active = stored_active if stored_active in MODES else None
        if Plugin.native_display_active():
            Plugin._state["active_mode"] = None
        else:
            Plugin._set_active_mode(active)

    @staticmethod
    def _activate_mode(mode) -> None:
        """Apply a choice whose script is already loaded by this session."""
        with Plugin._runtime_lock:
            if Plugin.native_display_active():
                return
            previous = Plugin._state.get("active_mode")
            if previous == mode:
                return
            Plugin._reset_hybrid_timing()
            Plugin._hdr_mode_settled()
            if previous == MODE_HYBRID:
                Plugin._hybrid_release(next_mode=mode)
            if mode == MODE_HYBRID:
                # This is a new ownership period. Capture what is there now and
                # keep it across Decky reloads, not across separate entries.
                Plugin._hdr_baseline = None
                Plugin._store_settings({
                    "hdr_baseline": None,
                    "hdr_baseline_session": None,
                })
                Plugin._hybrid_suspended_external = False
            Plugin._set_active_mode(mode)

    # â”€â”€ exported to the frontend â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def get_state(self) -> dict:
        try:
            Plugin._require_settings()
        except CorruptSettings:
            pass
        await _offload(Plugin._refresh_native_support)
        return dict(Plugin._state)

    async def run_setup(self, mode: str = MODE_PQ) -> dict:
        """First-time setup: remember the chosen mode and install its script."""
        return await Plugin._apply_mode(mode)

    async def set_panel_mode(self, mode: str) -> dict:
        """Change the mode after setup. Same path, so the two cannot diverge."""
        return await Plugin._apply_mode(mode)

    async def reset_settings(self) -> dict:
        await _offload(Plugin._read_settings)
        async with Plugin._mode_lock:
            await _offload(Plugin._refresh_native_support, True)
            if Plugin.native_display_active():
                return dict(Plugin._state)
            return await module_runtime.complete(_offload(Plugin._reset_settings_locked))

    @staticmethod
    def _reset_settings_locked() -> dict:
        """Withdraw display ownership before discarding its recovery snapshots."""
        with Plugin._runtime_lock, Plugin._edid_lock:
            if Plugin.native_display_active():
                return dict(Plugin._state)
            Plugin._state["reset_error"] = ""
            try:
                Plugin._sync_loaded_script()
                session = _gamescope_start_time()
                with _settings_lock:
                    settings.read()
                    Plugin._require_settings()
                    previous_session = settings.getSetting("reset_session", None)
                    needs_restart = (Plugin._loaded_script_variant in (MODE_PQ, MODE_G22)
                                     or (Plugin._same_session(previous_session, session)
                                         if previous_session is not None else
                                         Plugin._state.get("panel_mode") in MODES))
                    reset_session = session if needs_restart else None
                    settings.setSetting("reset_in_progress", True)
                    settings.setSetting("reset_session", reset_session)
                    settings.commit()
                # The durable marker also suppresses all hardware passes after
                # a Decky reload or an interrupted reset.
                Plugin._state["reset_in_progress"] = True
                Plugin._state["setup_done"] = False
                if session is None:
                    raise RuntimeError("Could not identify the Gaming Mode session. Retry reset in Gaming Mode.")
                props = Plugin._session_props([ATOM_IS_EXTERNAL])
                external = _as_int(props.get(ATOM_IS_EXTERNAL))
                if external not in (0, 1):
                    raise RuntimeError("Could not verify the active display. Retry reset in Gaming Mode.")
                if external == 0:
                    if Plugin._baseline is not None:
                        if not _write_nits(Plugin._baseline):
                            raise RuntimeError("Could not restore the previous brightness setting.")
                    elif not Plugin._release_locked():
                        raise RuntimeError("Could not release brightness control.")
                # An external display owns its brightness and output mode.
                Plugin._last_written = None
                Plugin._state["active"] = False
                if not _write_atom_int(ATOM_FORCE_HDR_SUPPORT, 0):
                    raise RuntimeError("Could not release HDR support control.")
                if external == 0:
                    hdr = Plugin._hdr_baseline if Plugin._hdr_baseline is not None else False
                    if not _write_atom_int(ATOM_HDR_ENABLED, int(hdr)):
                        raise RuntimeError("Could not restore the previous HDR setting.")
                    Plugin._state["hdr_now"] = bool(hdr)
                if not Plugin._edid_restore_locked():
                    raise RuntimeError("Could not restore the original display EDID.")
                note = _uninstall_script()
                if note.startswith("could not"):
                    raise RuntimeError(note)
                # A disappeared/replaced connector does not need restoring,
                # but its old recovery bytes must not survive a full reset.
                try:
                    os.unlink(EDID_BACKUP)
                except FileNotFoundError:
                    pass
                with _settings_lock:
                    settings.replace({**DEFAULT_SETTINGS, "reset_session": reset_session})
                Plugin._baseline = None
                Plugin._hdr_baseline = None
                Plugin._edid_original = None
                Plugin._edid_recheck_until = 0.0
                Plugin._hybrid_suspended_external = False
                Plugin._reset_hybrid_timing()
                Plugin._hdr_mode_settled()
                Plugin._state.update(panel_mode=None, active_mode=None, setup_done=False,
                    setup_note="", setup_error="", restart_pending=False, restart_error="",
                    enabled=DEFAULT_SETTINGS["enabled"], edid_fix=DEFAULT_SETTINGS["edid_fix"],
                    active=False, edid_patched=False, edid_game_nits=0.0,
                    reason="waiting for display setup", hybrid_reason="",
                    edid_reason="waiting for display setup", reset_in_progress=False,
                    reset_restart_pending=reset_session is not None,
                    reset_note=note)
                decky.logger.info(f"{LOG} reset: {note}")
            except Exception as error:
                # Preserve the journal/baselines and keep the module passive.
                # A retry can finish even if the script has already been removed.
                with _settings_lock:
                    settings.read()
                    pending = settings.getSetting("reset_in_progress", False) is True
                Plugin._state["reset_in_progress"] = pending
                Plugin._state["reset_error"] = str(error)
                if pending:
                    Plugin._state["setup_done"] = False
                decky.logger.warning(f"{LOG} reset incomplete: {error}")
            return dict(Plugin._state)

    @staticmethod
    async def _apply_mode(mode: str) -> dict:
        await _offload(Plugin._read_settings)
        # RPC calls can overlap even though the frontend normally disables its
        # buttons. BUNDLED_SCRIPT is process-global, so the whole transaction and
        # its derived runtime state must be serialised, not only the file write.
        async with Plugin._mode_lock:
            operation = asyncio.create_task(Plugin._apply_mode_locked(mode))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                # run_in_executor cannot stop a file operation already in
                # progress. Keep the lock until the transaction and rollback
                # finish, then propagate cancellation to the RPC caller.
                await operation
                raise

    @staticmethod
    async def _apply_mode_locked(mode: str) -> dict:
        await _offload(Plugin._refresh_native_support, True)
        if Plugin.native_display_active():
            return dict(Plugin._state)
        if Plugin._state.get("reset_in_progress") or Plugin._state.get("reset_restart_pending"):
            Plugin._state["setup_error"] = "Finish the display reset and restart Gaming Mode before choosing a new mode."
            return dict(Plugin._state)
        previous = Plugin._state["panel_mode"]
        mode = _normalise_mode(mode)

        def _install_then_save_locked():
            if Plugin.native_display_active():
                return False, "Native gamescope display support is active."
            backup = INSTALLED_SCRIPT + ".backup"
            installed_existed = os.path.exists(INSTALLED_SCRIPT)
            backup_existed = os.path.exists(backup)
            installed_before = _read_script_bytes(INSTALLED_SCRIPT)
            backup_before = _read_bytes(backup)
            if ((installed_existed and installed_before is None)
                    or (backup_existed and backup_before is None)):
                return False, "could not read the existing display script safely"

            def _rollback_files() -> bool:
                installed_ok = _restore_file_snapshot(
                    INSTALLED_SCRIPT, installed_existed, installed_before)
                backup_ok = _restore_file_snapshot(
                    backup, backup_existed, backup_before)
                return installed_ok and backup_ok

            _select_mode(mode)
            try:
                ok, note = _install_script()
            except Exception as e:
                rollback_ok = _rollback_files()
                _select_mode(previous if previous in MODES else MODE_PQ)
                rollback = ("previous script restored" if rollback_ok else
                            "script rollback failed")
                return False, f"display script installation failed: {e}; {rollback}"
            if not ok:
                rollback_ok = _rollback_files()
                _select_mode(previous if previous in MODES else MODE_PQ)
                if not rollback_ok:
                    note += "; script rollback failed"
                return False, note
            try:
                with _settings_lock:
                    settings.setSetting("panel_mode", mode)
                    settings.commit()
            except Exception as e:
                rollback_ok = _rollback_files()
                _select_mode(previous if previous in MODES else MODE_PQ)
                try:
                    with _settings_lock:
                        settings.setSetting("panel_mode", previous)
                        settings.commit()
                except Exception:
                    pass
                rollback = ("previous script restored" if rollback_ok else
                            "script rollback failed")
                return False, f"could not save the display mode: {e}; {rollback}"
            return True, note

        def _install_then_save():
            with Plugin._runtime_lock:
                return _install_then_save_locked()

        Plugin._state["setup_error"] = ""
        ok, note = await _offload(_install_then_save)
        if not ok:
            if Plugin.native_display_active():
                return dict(Plugin._state)
            Plugin._state["setup_error"] = note
            Plugin._state["setup_note"] = note
            decky.logger.error(f"{LOG} setup failed: {note}")
            return dict(Plugin._state)

        Plugin._state["panel_mode"] = mode
        Plugin._state["setup_note"] = ""
        Plugin._state["restart_error"] = ""
        await _offload(Plugin._refresh_setup)

        # A mode is live only when the running session has the variant it needs.
        # Until then the old active mode keeps doing its job behind the restart
        # screen instead of pretending the new script was loaded already.
        if (Plugin._gamescope_started_at is not None
                and Plugin._loaded_script_variant == MODE_SCRIPT[mode]):
            await _offload(Plugin._activate_mode, mode)

        decky.logger.info(
            f"{LOG} selected mode {previous or 'unset'} -> {mode}, "
            f"active {Plugin._state.get('active_mode') or 'unknown'}, "
            f"script {os.path.basename(BUNDLED_SCRIPT)}")

        if Plugin._state.get("active_mode") == MODE_HYBRID:
            await _offload(Plugin._hybrid_pass)
        return dict(Plugin._state)

    async def restart_session(self) -> dict:
        decky.logger.info(f"{LOG} restarting Game Mode at the user's request")
        Plugin._state["restart_error"] = ""
        ok, note = await _offload(_restart_session)
        if ok:
            decky.logger.info(f"{LOG} {note}")
        else:
            # The session going down would take this process with it, so if we
            # are still here to report, it genuinely did not work.
            Plugin._state["restart_error"] = note
            decky.logger.error(f"{LOG} restart failed: {note}")
        return dict(Plugin._state)

    async def set_enabled(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        await _offload(Plugin._refresh_native_support, True)
        if Plugin.native_display_active():
            return dict(Plugin._state)
        def _do():
            with Plugin._runtime_lock, _settings_lock:
                if Plugin.native_display_active():
                    return False
                settings.read()
                Plugin._require_settings()
                settings.setSetting("enabled", bool(enabled))
                settings.commit()
                return True
        if not await _offload(_do):
            return dict(Plugin._state)
        Plugin._state["enabled"] = bool(enabled)
        if not enabled:
            restored = await _offload(Plugin._release)
            Plugin._state["reason"] = (
                "turned off" if restored else
                "turned off, but could not restore the previous gamescope value")
        return dict(Plugin._state)

    async def set_edid_fix(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        def _do():
            with _settings_lock:
                settings.read()
                Plugin._require_settings()
                settings.setSetting("edid_fix", bool(enabled))
                settings.commit()
        await _offload(_do)
        Plugin._state["edid_fix"] = bool(enabled)
        if not enabled:
            await _offload(Plugin._edid_restore)
        else:
            await _offload(Plugin._edid_pass)
        return dict(Plugin._state)

    # â”€â”€ brightness half â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @staticmethod
    def _release() -> bool:
        with Plugin._runtime_lock:
            return Plugin._release_locked()

    @staticmethod
    def _release_locked() -> bool:
        """Hand the atom back to whatever it was before we started forwarding."""
        if Plugin.native_display_active():
            return True
        if not Plugin._state["active"] and Plugin._last_written is None:
            return True
        target = (Plugin._baseline if Plugin._baseline is not None
                  else FALLBACK_BASELINE_NITS)
        if not _write_nits(target):
            decky.logger.error(
                f"{LOG} could not release brightness ownership; "
                f"restoring {target:.2f} nits failed")
            return False
        Plugin._last_written = None
        Plugin._state["active"] = False
        decky.logger.info(f"{LOG} released, restored {target:.2f} nits")
        return True

    @staticmethod
    def _remember_brightness_baseline(value: float) -> None:
        Plugin._baseline = value
        Plugin._store_settings({
            "brightness_baseline": value,
            "brightness_baseline_session": Plugin._gamescope_started_at,
        })
        decky.logger.info(f"{LOG} baseline {value:.2f} nits")

    @staticmethod
    def _forward_nits(nits: float):
        """Write only while the gate that authorised this value is still open."""
        with Plugin._runtime_lock:
            if Plugin.native_display_active():
                Plugin._state["active"] = False
                return None
            if Plugin._state.get("reset_in_progress") or (
                    Plugin._state.get("panel_mode") is None and Plugin._last_written is None):
                Plugin._state["active"] = False
                return None
            if (not Plugin._state["active"]
                    or Plugin._state.get("active_mode") != MODE_PQ
                    or not Plugin._state.get("setup_done")
                    or not Plugin._state.get("enabled")
                    or not Plugin._state.get("panel_ok")):
                # The loop may hold a stale gate result while a toggle or mode
                # change is being processed. Hand back any prior value, but never
                # let that stale result authorise a new write.
                Plugin._release_locked()
                return None
            ok = _write_nits(nits)
            if ok:
                Plugin._last_written = nits
            return ok

    @staticmethod
    def _refresh_setup() -> None:
        """Re-check the display script; someone may have replaced it behind us."""
        with Plugin._runtime_lock:
            Plugin._refresh_setup_locked()

    @staticmethod
    def _refresh_setup_locked() -> None:
        Plugin._refresh_native_support()
        if Plugin.native_display_active():
            Plugin._state["setup_done"] = False
            Plugin._state["restart_pending"] = False
            return
        Plugin._sync_loaded_script()
        if Plugin._state.get("reset_in_progress"):
            Plugin._state["setup_done"] = False
            Plugin._state["restart_pending"] = False
            return
        if Plugin._state["panel_mode"] is None:
            # Nothing is installed on the user's behalf before they have said
            # which trade-off they want.
            Plugin._state["setup_done"] = False
            Plugin._state["restart_pending"] = False
            Plugin._state["setup_note"] = ""
            reset_session = settings.getSetting("reset_session", None)
            current = _gamescope_start_time()
            Plugin._state["reset_restart_pending"] = (
                reset_session is not None and (current is None or Plugin._same_session(reset_session, current)))
            if reset_session is not None and current is not None and not Plugin._same_session(reset_session, current):
                Plugin._store_setting("reset_session", None)
            Plugin._state["active_mode"] = None
            return
        installed, note = _script_status()
        Plugin._state["setup_done"] = installed
        pending = installed and _restart_needed(Plugin._loaded_script_variant)
        Plugin._state["restart_pending"] = pending
        if pending:
            Plugin._state["setup_note"] = "installed, restart Game Mode to apply"
        else:
            Plugin._state["setup_note"] = note

    @staticmethod
    def _session_props(names) -> dict:
        """Read atoms, re-find a session that moved, and learn the EDID path.

        Every caller needs the same two things before anything else: a session
        that answers, and wherever gamescope is currently publishing the EDID.
        Both live here rather than inside the brightness gate because that gate
        returns early in two of the three modes - which used to leave the EDID
        half with no path at all, and it is the one part of this plugin that has
        work to do no matter which mode the panel runs in.
        """
        wanted = list(dict.fromkeys(list(names) + [ATOM_EDID_PATH]))
        props = _read_props(wanted)
        needs_control_root = ATOM_IS_EXTERNAL in wanted
        complete = props and (
            not needs_control_root or ATOM_IS_EXTERNAL in props)
        if not complete:
            Plugin._state["gamescope_reachable"] = False
            # A non-empty reply is not enough: feedback and EDID_PATH are copied
            # to every Xwayland, while the safety/control atoms live only on the
            # gamescope root context. Re-probe a few times during startup, but do
            # not launch a scan twice a second forever while gamescope is down.
            now = time.monotonic()
            if now < Plugin._session_retry_after:
                Plugin._session_note = (
                    "gamescope control root not ready" if props
                    else "gamescope not reachable")
                return {}
            Plugin._session_retry_after = now + GATE_INTERVAL_S
            if not _pick_display():
                Plugin._session_note = "gamescope not reachable"
                return {}
            # Drop a potentially stale/incomplete watcher snapshot and use the
            # normal read path.  Keeping the retry behind _read_props() also
            # preserves the single interception point used by diagnostics and
            # tests.
            _invalidate_prop_cache()
            props = _read_props(wanted)
            complete = props and (
                not needs_control_root or ATOM_IS_EXTERNAL in props)
            if not complete:
                Plugin._session_note = \
                    "gamescope is up but its control root is not ready"
                return {}
        Plugin._session_note = ""
        Plugin._state["gamescope_reachable"] = True
        Plugin._edid_path = (_as_path(props.get(ATOM_EDID_PATH))
                             or Plugin._edid_path)
        return props

    @staticmethod
    def _refresh_gate() -> bool:
        with Plugin._runtime_lock:
            return Plugin._refresh_gate_locked()

    @staticmethod
    def _refresh_gate_locked() -> bool:
        """Decide whether we should be forwarding right now. Costs one xprop."""
        if Plugin._state.get("reset_in_progress"):
            return False
        if Plugin.native_display_active():
            Plugin._state["active"] = False
            return False
        if not Plugin._state["setup_done"]:
            # The panel shows only the setup button in this state, so quietly
            # doing the work anyway would leave no way to turn it off.
            Plugin._release()
            Plugin._state["reason"] = "waiting for the display script"
            return False

        # Ahead of the mode gates on purpose: this is what teaches the EDID half
        # where to look, and that half runs in all three modes.
        props = Plugin._session_props(
            [ATOM_HDR_OUT, ATOM_HDR_APP, ATOM_SDR_NITS, ATOM_IS_EXTERNAL,
             ATOM_HDR_ENABLED, ATOM_FORCE_HDR_SUPPORT])

        mode = Plugin._state.get("active_mode")
        if mode not in MODES:
            Plugin._release_locked()
            Plugin._state["reason"] = "waiting for gamescope to load a display mode"
            return False
        if (mode != MODE_HYBRID
                and _as_int(props.get(ATOM_FORCE_HDR_SUPPORT))):
            if _write_atom_int(ATOM_FORCE_HDR_SUPPORT, 0):
                Plugin._edid_recheck_until = time.monotonic() + GATE_INTERVAL_S
            else:
                Plugin._state["reason"] = "could not clear stale hybrid HDR support"
                return False
        if mode == MODE_G22:
            # The panel honours its hardware brightness control in gamma 2.2, so
            # there is nothing to forward and adding to it would dim twice.
            Plugin._release()
            Plugin._state["reason"] = \
                "gamma 2.2 mode - the system slider drives the panel directly"
            return False
        if mode == MODE_HYBRID:
            # Same story while it sits in gamma 2.2, and once it flips to PQ
            # there is HDR content up and Steam owns the gain atoms.
            Plugin._release()
            Plugin._state["reason"] = \
                "hybrid mode - the panel switches itself, nothing to forward"
            return False

        if not Plugin._state["panel_ok"]:
            # Forwarding the level on a panel that does honour its backlight
            # would dim the image twice.
            Plugin._release()
            Plugin._state["reason"] = "display not on the affected list"
            return False
        if not Plugin._state["enabled"]:
            Plugin._release()
            Plugin._state["reason"] = "turned off"
            return False

        if not props:
            Plugin._state["reason"] = Plugin._session_note or "gamescope not reachable"
            return False

        atoms = {name: _as_int(props.get(name))
                  for name in (ATOM_HDR_OUT, ATOM_HDR_APP, ATOM_SDR_NITS,
                               ATOM_IS_EXTERNAL, ATOM_HDR_ENABLED,
                               ATOM_FORCE_HDR_SUPPORT)}

        if atoms.get(ATOM_IS_EXTERNAL):
            # The backlight we read belongs to the built-in panel; the atom
            # applies to whatever gamescope is driving. Forwarding one to the
            # other would set an external display's brightness from a slider
            # that has nothing to do with it.
            live = _as_float(atoms.get(ATOM_SDR_NITS))
            if (Plugin._last_written is not None and live is not None
                    and abs(live - Plugin._last_written) > 0.01):
                # Connector setup or another owner has already replaced our
                # internal-panel value. Restoring the internal baseline now
                # would itself overwrite the external display's choice.
                Plugin._last_written = None
                Plugin._state["active"] = False
                decky.logger.info(
                    f"{LOG} external display replaced the brightness atom; "
                    "leaving its value alone")
            else:
                Plugin._release_locked()
            Plugin._state["reason"] = "external display, not ours to drive"
            return False

        # Persist the pre-existing value per gamescope session. A Decky reload
        # recovers it instead of mistaking our last write for the baseline, while
        # a fresh session captures Steam's current value.
        if Plugin._baseline is None:
            current = _as_float(atoms.get(ATOM_SDR_NITS))
            if current and current > 0:
                Plugin._remember_brightness_baseline(current)

        # gamescope drops what we wrote when it re-initialises, which it does
        # across a suspend and a session restart. The loop only writes when the
        # backlight moves, so without this the level stays wrong until the user
        # touches the slider - and the value needed to notice it is already here.
        live = _as_float(atoms.get(ATOM_SDR_NITS))
        if (Plugin._last_written is not None and live is not None
                and abs(live - Plugin._last_written) > 0.01):
            decky.logger.info(
                f"{LOG} atom reads {live:.2f} nits, we last wrote "
                f"{Plugin._last_written:.2f} - putting it back")
            Plugin._last_written = None

        # A killed hybrid can leave forced feedback behind. Clear it first or
        # HDR_OUTPUT_FEEDBACK would say one even with a gamma 2.2 output.
        if atoms.get(ATOM_FORCE_HDR_SUPPORT, 0):
            Plugin._release_locked()
            if _write_atom_int(ATOM_FORCE_HDR_SUPPORT, 0):
                Plugin._edid_recheck_until = time.monotonic() + GATE_INTERVAL_S
                Plugin._state["reason"] = "clearing stale hybrid HDR support"
            else:
                Plugin._state["reason"] = "could not clear stale hybrid HDR support"
            return False

        # The request atom is only our own last write; actual output feedback is
        # the closed loop. Retry even when HDR_ENABLED already reads one, because
        # that exact mismatch used to become a permanent "slider already works"
        # blind spot.
        if not atoms.get(ATOM_HDR_OUT, 0):
            Plugin._release_locked()
            if Plugin._standing_off():
                Plugin._state["reason"] = \
                    "something else keeps taking the panel out of PQ"
            elif Plugin._correct_hdr_mode(True):
                Plugin._state["reason"] = "putting the panel back into PQ"
            else:
                Plugin._state["reason"] = "panel is not in PQ"
            return False
        Plugin._hdr_mode_settled()

        if atoms.get(ATOM_HDR_APP, 0):
            # Steam drives the gain atoms while HDR content is up. Adding to that
            # would dim twice.
            Plugin._release()
            Plugin._state["reason"] = "HDR content on screen, Steam handles it"
            return False

        Plugin._state["reason"] = "forwarding slider to gamescope"
        return True

    # â”€â”€ display mode â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @staticmethod
    def _correct_hdr_mode(target: bool) -> bool:
        """Move the panel into `target`, giving up if something keeps undoing it.

        Shared by hybrid, which switches on content, and pq, which just holds the
        panel where the user put it. Both write the same atom, and both would
        strobe the screen if they argued with another writer forever.
        """
        if Plugin.native_display_active():
            return False
        now = time.monotonic()
        if now < Plugin._hdr_backoff_until:
            return False

        Plugin._hdr_fights += 1
        if Plugin._hdr_fights > HDR_FIGHT_LIMIT:
            Plugin._hdr_backoff_until = now + HDR_BACKOFF_S
            Plugin._hdr_fights = 0
            decky.logger.warning(
                f"{LOG} the display mode keeps being changed back - standing "
                f"off for {HDR_BACKOFF_S:.0f}s rather than blinking the panel")
            return False

        return _write_atom_int(ATOM_HDR_ENABLED, 1 if target else 0)

    @staticmethod
    def _hdr_mode_settled() -> None:
        """The panel is where we want it, so forget any argument we were having."""
        Plugin._hdr_fights = 0
        Plugin._hdr_backoff_until = 0.0

    @staticmethod
    def _standing_off() -> bool:
        return time.monotonic() < Plugin._hdr_backoff_until

    # â”€â”€ hybrid half â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @staticmethod
    def _reset_hybrid_timing() -> None:
        """Forget transition timers that cannot cross an ownership boundary."""
        Plugin._hybrid_pq_hold_until = 0.0
        Plugin._hybrid_sdr_since = None

    @staticmethod
    def _cancel_hybrid_sdr_candidate(log=False) -> None:
        """A false HDR signal is continuous only while every pass confirms it."""
        if Plugin._hybrid_sdr_since is not None and log:
            decky.logger.info(
                f"{LOG} hybrid: HDR returned, cancelled the gamma 2.2 switch")
        Plugin._hybrid_sdr_since = None

    @staticmethod
    def _hybrid_pass() -> None:
        with Plugin._runtime_lock:
            Plugin._hybrid_pass_locked()

    @staticmethod
    def _remember_hdr_baseline(value: bool) -> None:
        Plugin._hdr_baseline = bool(value)
        Plugin._store_settings({
            "hdr_baseline": bool(value),
            "hdr_baseline_session": Plugin._gamescope_started_at,
        })

    @staticmethod
    def _hybrid_pass_locked() -> None:
        """Move the panel between gamma 2.2 and PQ from what the game asks for."""
        if Plugin.native_display_active():
            return
        if Plugin._state.get("reset_in_progress") or Plugin._state.get("active_mode") != MODE_HYBRID:
            return
        if not Plugin._state["setup_done"]:
            Plugin._reset_hybrid_timing()
            Plugin._state["hybrid_reason"] = "waiting for the display script"
            return

        props = Plugin._session_props(
            [ATOM_HDR_APP, ATOM_HDR_ENABLED, ATOM_FORCE_HDR_SUPPORT,
             ATOM_IS_EXTERNAL, ATOM_HDR_OUT])
        if not props:
            # Time without feedback cannot count as continuously observed SDR.
            # Keep the PQ hold deadline, but make the debounce start over once
            # gamescope is reachable again.
            Plugin._cancel_hybrid_sdr_candidate()
            Plugin._state["hybrid_reason"] = \
                Plugin._session_note or "gamescope not reachable"
            return

        if _as_int(props.get(ATOM_IS_EXTERNAL)):
            if not Plugin._hybrid_suspended_external:
                Plugin._reset_hybrid_timing()
                dropped = _write_atom_int(ATOM_FORCE_HDR_SUPPORT, 0)
                if dropped:
                    Plugin._edid_recheck_until = \
                        time.monotonic() + GATE_INTERVAL_S
                if dropped:
                    Plugin._hybrid_suspended_external = True
                    output = _as_int(props.get(ATOM_HDR_OUT))
                    if output is not None:
                        Plugin._state["hdr_now"] = bool(output)
                    Plugin._hdr_mode_settled()
                    decky.logger.info(
                        f"{LOG} hybrid: suspended for external display")
                else:
                    Plugin._state["hybrid_reason"] = \
                        "could not hand HDR control to the external display"
                    return
            Plugin._state["hybrid_reason"] = "external display, not ours to drive"
            return

        returned_without_baseline = (
            Plugin._hybrid_suspended_external
            and Plugin._hdr_baseline is None)
        if Plugin._hybrid_suspended_external:
            Plugin._hybrid_suspended_external = False
            Plugin._reset_hybrid_timing()
            Plugin._hdr_mode_settled()

        forced = bool(_as_int(props.get(ATOM_FORCE_HDR_SUPPORT)))

        # This atom is input, not feedback, and gamescope does not create it.
        # Capture it before our first internal-panel write. An external display
        # has a different baseline and must never supply this value. If Hybrid
        # started while docked, use the internal panel's unforced output feedback
        # on return rather than carrying the monitor's request atom across.
        if Plugin._hdr_baseline is None:
            found = _as_int(props.get(ATOM_HDR_ENABLED))
            if returned_without_baseline:
                found = _as_int(props.get(ATOM_HDR_OUT)) or 0
            elif found is None:
                found = 0 if forced else (_as_int(props.get(ATOM_HDR_OUT)) or 0)
            Plugin._remember_hdr_baseline(bool(found))

        # Re-asserted rather than set once: gamescope drops it on restart, and
        # without it the Vulkan layer hides every HDR format from a game started
        # while the panel is in gamma 2.2 - so nothing would ever ask for HDR and
        # the switch below could never fire.
        if not _as_int(props.get(ATOM_FORCE_HDR_SUPPORT)):
            if _write_atom_int(ATOM_FORCE_HDR_SUPPORT, 1):
                Plugin._edid_recheck_until = time.monotonic() + GATE_INTERVAL_S
                decky.logger.info(f"{LOG} hybrid: HDR advertised to games")
            else:
                Plugin._state["hybrid_reason"] = "could not advertise HDR to games"
                return

        wants_hdr = bool(_as_int(props.get(ATOM_HDR_APP)))
        enabled = _as_int(props.get(ATOM_HDR_ENABLED))
        enabled_unknown = enabled is None and forced
        if enabled is None and not forced:
            # gamescope does not publish this input until somebody writes it.
            # Before we force HDR advertisement, output feedback is the only
            # observation of the actual starting mode.
            enabled = _as_int(props.get(ATOM_HDR_OUT)) or 0
        # Once force is present, output feedback is intentionally synthetic. An
        # absent input atom is therefore unknown, not gamma 2.2: explicitly write
        # the requested mode so an interrupted earlier hand-off cannot strand PQ.
        is_hdr = (bool(_as_int(props.get(ATOM_HDR_OUT))) if enabled_unknown
                  else bool(enabled))
        Plugin._state["hdr_now"] = is_hdr

        now = time.monotonic()
        if wants_hdr:
            Plugin._cancel_hybrid_sdr_candidate(log=True)
            if not enabled_unknown and is_hdr:
                # Hybrid may be enabled while an HDR game is already on screen.
                # Give that existing PQ state the same settling protection as a
                # modeset initiated below, without extending it every pass.
                if Plugin._hybrid_pq_hold_until <= 0.0:
                    Plugin._hybrid_pq_hold_until = \
                        now + HYBRID_PQ_MIN_HOLD_S
                Plugin._hdr_mode_settled()
                Plugin._state["hybrid_reason"] = \
                    "HDR content on screen, panel in PQ"
                return
            target = True
        elif not enabled_unknown and not is_hdr:
            Plugin._reset_hybrid_timing()
            Plugin._hdr_mode_settled()
            Plugin._state["hybrid_reason"] = \
                "no HDR content, panel in gamma 2.2"
            return
        else:
            # Going into PQ is urgent because HDR is already being presented.
            # Coming out is deliberately asymmetric: transient SDR commits are
            # common while games replace loading screens and swapchains, and a
            # second physical modeset during the first one can strand a game on
            # black. Require continuous SDR and honour the minimum PQ residency.
            if Plugin._hybrid_sdr_since is None:
                Plugin._hybrid_sdr_since = now
                decky.logger.info(
                    f"{LOG} hybrid: SDR observed, waiting before gamma 2.2")
            ready_at = max(
                Plugin._hybrid_sdr_since + HYBRID_SDR_DEBOUNCE_S,
                Plugin._hybrid_pq_hold_until,
            )
            if now < ready_at:
                Plugin._hdr_mode_settled()
                Plugin._state["hybrid_reason"] = (
                    "no HDR content, waiting for a stable switch to gamma 2.2")
                return
            target = False

        if Plugin._correct_hdr_mode(target):
            Plugin._state["hdr_now"] = target
            if target:
                Plugin._hybrid_pq_hold_until = \
                    now + HYBRID_PQ_MIN_HOLD_S
            else:
                Plugin._reset_hybrid_timing()
            Plugin._state["hybrid_reason"] = (
                "HDR content on screen, panel in PQ" if target
                else "no HDR content, panel in gamma 2.2")
            decky.logger.info(
                f"{LOG} hybrid: panel -> {'PQ' if target else 'gamma 2.2'}")
        elif Plugin._standing_off():
            Plugin._state["hybrid_reason"] = (
                "something else keeps changing the display mode, standing off")
        else:
            Plugin._state["hybrid_reason"] = "could not switch the panel"

    @staticmethod
    def _hybrid_release(next_mode=None) -> bool:
        with Plugin._runtime_lock:
            return Plugin._hybrid_release_locked(next_mode)

    @staticmethod
    def _hybrid_release_locked(next_mode=None) -> bool:
        """Stop forcing support and leave the output useful for what follows."""
        if Plugin.native_display_active():
            return True
        Plugin._reset_hybrid_timing()
        props = Plugin._session_props([ATOM_IS_EXTERNAL])
        external = _as_int(props.get(ATOM_IS_EXTERNAL))

        dropped = _write_atom_int(ATOM_FORCE_HDR_SUPPORT, 0)
        if dropped:
            Plugin._edid_recheck_until = time.monotonic() + GATE_INTERVAL_S

        if external != 0:
            # We can safely stop advertising synthetic HDR support, but without
            # the marker we must not write the global output-mode atom. A known
            # external display is equally hands-off: Steam or that monitor's own
            # policy decides whether it should run HDR.
            if external == 1 and dropped:
                Plugin._hdr_baseline = None
                Plugin._store_settings({
                    "hdr_baseline": None,
                    "hdr_baseline_session": None,
                })
                Plugin._hybrid_suspended_external = False
                Plugin._state["hybrid_reason"] = ""
                Plugin._hdr_mode_settled()
                decky.logger.info(
                    f"{LOG} hybrid: released control to the external display")
                return True
            Plugin._state["hybrid_reason"] = (
                "could not clear HDR support for the external display"
                if external == 1 else
                "could not verify which display should receive HDR control")
            decky.logger.error(f"{LOG} hybrid: display hand-back deferred")
            return False

        if next_mode == MODE_PQ:
            # Do not restore a false baseline only for the PQ gate to turn it on
            # again two seconds later, producing two visible modesets.
            target = True
        elif next_mode == MODE_G22:
            target = False
        elif Plugin._hdr_baseline is not None:
            target = Plugin._hdr_baseline
        else:
            # Nothing was captured or written in this ownership period. Clearing
            # our support flag is sufficient and makes repeated unloads idempotent.
            if dropped:
                Plugin._hybrid_suspended_external = False
                Plugin._state["hybrid_reason"] = ""
                Plugin._hdr_mode_settled()
                return True
            Plugin._state["hybrid_reason"] = "could not hand HDR control back"
            return False

        restored = _write_atom_int(ATOM_HDR_ENABLED, 1 if target else 0)

        if restored:
            Plugin._state["hdr_now"] = target
        if dropped and restored:
            # Only forget the hand-back target after both writes succeeded. If
            # Xwayland disappeared halfway through unload, a Decky reload in
            # the same gamescope session still has enough information to retry.
            Plugin._hdr_baseline = None
            Plugin._store_settings({
                "hdr_baseline": None,
                "hdr_baseline_session": None,
            })
            Plugin._hdr_mode_settled()
            Plugin._hybrid_suspended_external = False
            Plugin._state["hybrid_reason"] = ""
            decky.logger.info(
                f"{LOG} hybrid: released, panel back in "
                f"{'PQ' if target else 'gamma 2.2'}")
            return True
        else:
            # Worth saying out loud: the panel is now in whatever state the
            # failed write left it, and nothing is left running to correct it.
            decky.logger.error(
                f"{LOG} hybrid: could not hand the panel back "
                f"(flag={'ok' if dropped else 'failed'}, "
                f"mode={'ok' if restored else 'failed'})")
            Plugin._state["hybrid_reason"] = "could not hand HDR control back"
            return False

    # â”€â”€ EDID half â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @staticmethod
    def _edid_restore() -> bool:
        with Plugin._edid_lock:
            return Plugin._edid_restore_locked()

    @staticmethod
    def _edid_restore_locked() -> bool:
        """Put back the EDID exactly as gamescope wrote it.

        The file we would restore has to match the one on disk: after a dock the
        published EDID belongs to a different display, and writing this panel's
        bytes over it would be worse than leaving it trimmed.
        """
        original = Plugin._edid_original
        path = Plugin._edid_path
        if not original or not path:
            Plugin._state["edid_patched"] = False
            Plugin._state["edid_reason"] = "turned off"
            return True
        if not _published_edid_is_safe(path):
            Plugin._state["edid_reason"] = \
                "refusing an unsafe EDID path; original not restored"
            decky.logger.error(f"{LOG} refusing unsafe EDID path {path!r}")
            return False

        current = _read_bytes(path)
        patched = _strip_displayid(original)
        if current is None:
            decky.logger.info(f"{LOG} EDID path disappeared, not recreating it")
            Plugin._state["edid_patched"] = False
            Plugin._state["edid_reason"] = "turned off, EDID path disappeared"
            return True
        if current == original:
            result = "written"
        elif current == patched:
            result = _rewrite_file_if_unchanged(path, current, original)
        else:
            decky.logger.info(
                f"{LOG} EDID on disk is not the one we patched, leaving it")
            Plugin._state["edid_patched"] = False
            Plugin._state["edid_reason"] = "turned off, display EDID changed"
            return True

        if result == "written":
            decky.logger.info(f"{LOG} EDID restored")
            try:
                os.unlink(EDID_BACKUP)
            except OSError:
                pass
            Plugin._edid_original = None
            Plugin._state["edid_patched"] = False
            Plugin._state["edid_reason"] = "turned off"
            return True
        if result == "changed":
            Plugin._state["edid_patched"] = False
            Plugin._state["edid_reason"] = "EDID changed while it was being restored"
            return True

        Plugin._state["edid_patched"] = True
        Plugin._state["edid_reason"] = "could not restore the original EDID; retrying"
        decky.logger.error(f"{LOG} could not restore the original EDID")
        return False

    @staticmethod
    def _edid_pass() -> None:
        with Plugin._edid_lock:
            Plugin._edid_pass_locked()

    @staticmethod
    def _edid_pass_locked() -> None:
        """Keep gamescope's published EDID parseable by DXVK.

        Runs on the gate cadence rather than once at startup: gamescope rewrites
        this file whenever the connector changes, so a dock or an undock would
        otherwise silently undo the fix.
        """
        if Plugin._state.get("reset_in_progress") and not Plugin.native_display_active():
            return
        if not Plugin._state["edid_fix"]:
            Plugin._edid_restore_locked()
            return

        # Native panel support and Lua setup never determine whether games need
        # the independent EDID workaround, including after connector changes.
        Plugin._session_props([])

        path = Plugin._edid_path
        if not path:
            Plugin._state["edid_reason"] = "gamescope publishes no EDID path"
            return
        Plugin._edid_path = path
        if not _published_edid_is_safe(path):
            Plugin._state["edid_reason"] = "refusing an unsafe EDID path"
            decky.logger.error(f"{LOG} refusing unsafe EDID path {path!r}")
            return

        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            Plugin._state["edid_reason"] = "EDID file unreadable"
            return

        if not _has_displayid(data):
            # Either we already fixed it - possibly before a reload, which is why
            # the original is kept on disk - or this display never had the block.
            ours = (_strip_displayid(Plugin._edid_original)
                    if Plugin._edid_original else None)
            Plugin._state["edid_patched"] = data == ours
            nits = _cta_max_luminance(data)
            Plugin._state["edid_game_nits"] = round(nits, 1) if nits else 0.0
            Plugin._state["edid_reason"] = (
                "applied, games read the panel's real values"
                if data == ours else
                "nothing to fix on this display"
            )
            return

        # There is a DisplayID block. Only act if the CTA block still carries the
        # luminance games actually need - stripping is safe for DXVK either way,
        # but leaving a display with no usable metadata at all would be worse.
        nits = _cta_max_luminance(data)
        if not nits:
            Plugin._state["edid_reason"] = (
                "CTA block carries no luminance, leaving the EDID alone")
            return

        patched = _strip_displayid(data)
        if not patched:
            Plugin._state["edid_reason"] = "EDID has an unexpected layout"
            return

        # gamescope can rewrite the shared path for another connector between our
        # read and replace. Fail closed instead of putting this panel's EDID over
        # the new display and then mistaking the resulting trimmed file for it.
        if _read_bytes(path) != data:
            Plugin._state["edid_reason"] = "EDID changed while it was being checked"
            return

        # Always, not just the first time: connector changes replace this file,
        # so the bytes we would restore must describe the display just parsed.
        Plugin._edid_original = data
        if not _write_file_atomically(EDID_BACKUP, data):
            Plugin._state["edid_reason"] = "could not save the original EDID"
            Plugin._edid_original = None
            return

        result = _rewrite_file_if_unchanged(path, data, patched)
        if result == "written":
            Plugin._state["edid_patched"] = True
            Plugin._state["edid_game_nits"] = round(nits, 1)
            Plugin._state["edid_reason"] = "applied, games read the panel's real values"
            decky.logger.info(
                f"{LOG} EDID: dropped DisplayID block, "
                f"{len(data)} -> {len(patched)} bytes, games now see {nits:.1f} nits"
            )
        elif result == "changed":
            Plugin._state["edid_patched"] = False
            Plugin._state["edid_reason"] = "EDID changed while it was being written"
        else:
            Plugin._state["edid_patched"] = False
            Plugin._state["edid_reason"] = "could not write the EDID file"

    # â”€â”€ loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _loop(self, bl_dir: str):
        bl_path = os.path.join(bl_dir, "brightness") if bl_dir else ""
        notify = (await _offload(_open_notify, bl_dir)
                  if bl_dir and not Plugin.native_display_active() else None)
        if notify is not None:
            decky.logger.info(f"{LOG} waiting on kernel change notifications")
        elif not Plugin.native_display_active():
            decky.logger.info(
                f"{LOG} no notification support, polling every {POLL_FALLBACK_S}s")
        gate_ok = False
        gate_ts = 0.0
        hybrid_ts = 0.0
        hardware_retry_at = 0.0
        try:
            while True:
                try:
                    now = time.monotonic()
                    if now - gate_ts >= GATE_INTERVAL_S:
                        await _offload(Plugin._refresh_setup)
                        native = Plugin.native_display_active()
                        if native and notify is not None:
                            os.close(notify)
                            notify = None
                        # DRM/backlight nodes can appear after Decky starts, or
                        # move after a driver rebind. Retry discovery only while
                        # hardware is missing; a healthy panel needs no scans.
                        missing = not bl_dir or not os.path.exists(bl_path)
                        if (not native and now >= hardware_retry_at
                                and (missing or not Plugin._state["panel_ok"])):
                            hardware_retry_at = now + HARDWARE_RETRY_S
                            matched, desc = await _offload(_identify_panel)
                            Plugin._state["panel_ok"] = matched
                            Plugin._state["panel_desc"] = desc
                            if missing:
                                if notify is not None:
                                    os.close(notify)
                                    notify = None
                                bl_dir = await _offload(_find_backlight)
                                bl_path = os.path.join(bl_dir, "brightness") if bl_dir else ""
                                notify = await _offload(_open_notify, bl_dir) if bl_dir else None
                                Plugin._state["backlight"] = os.path.basename(bl_dir) if bl_dir else ""
                                maximum = _read_int(os.path.join(bl_dir, "max_brightness")) if bl_dir else None
                                Plugin._state["max_nits"] = round(_millinits_to_nits(maximum), 2) if maximum else 0.0
                                Plugin._last_written = None
                        gate_ok = await _offload(Plugin._refresh_gate)
                        await _offload(Plugin._edid_pass)
                        gate_ts = now

                    # Hybrid runs on its own, faster clock: this one decides how
                    # long HDR content is shown on a gamma 2.2 output before the
                    # panel catches up, and the gate cadence would be visible.
                    if (Plugin._state.get("active_mode") == MODE_HYBRID
                            and now - hybrid_ts >= HYBRID_INTERVAL_S):
                        await _offload(Plugin._hybrid_pass)
                        hybrid_ts = now

                    # Changing the forced-support flag makes gamescope regenerate
                    # its EDID file asynchronously. Re-apply for a short window so
                    # a game cannot keep the 1499-nit fallback from that rewrite.
                    if Plugin._edid_recheck_until:
                        await _offload(Plugin._edid_pass)
                        if time.monotonic() >= Plugin._edid_recheck_until:
                            Plugin._edid_recheck_until = 0.0

                    if gate_ok and not Plugin.native_display_active():
                        # Cheap enough to do on the event loop; offloading it
                        # would cost more in executor round trips than the read.
                        raw = _read_int(bl_path)
                        if raw is None:
                            Plugin._state["active"] = False
                            Plugin._state["reason"] = "backlight unreadable"
                        else:
                            # The backlight node is in millinits, exactly the unit
                            # the atom wants. No curve of our own: Steam already
                            # spaced the steps perceptually.
                            nits = _millinits_to_nits(raw)
                            Plugin._state["active"] = True
                            Plugin._state["nits"] = round(nits, 2)
                            if (Plugin._last_written is None
                                    or abs(Plugin._last_written - nits) > 0.01):
                                wrote = await _offload(Plugin._forward_nits, nits)
                                if wrote is False:
                                    Plugin._state["reason"] = \
                                        "could not write to gamescope"

                    # Sleep until either the backlight moves or the gate is due.
                    remaining = GATE_INTERVAL_S - (time.monotonic() - gate_ts)
                    if Plugin._state.get("active_mode") == MODE_HYBRID:
                        remaining = min(
                            remaining,
                            HYBRID_INTERVAL_S - (time.monotonic() - hybrid_ts))
                    if bl_dir and not Plugin.native_display_active():
                        await _offload(_wait_for_change, notify, remaining)
                    else:
                        await asyncio.sleep(max(0.0, remaining))
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # never let one bad pass kill the loop
                    decky.logger.error(f"{LOG} loop error: {e}")
                    await asyncio.sleep(0.5)
        finally:
            if notify is not None:
                os.close(notify)

    async def _main(self):
        decky.logger.info(f"{LOG} startup")
        try:
            await _offload(Plugin._read_settings)
        except Exception as error:
            message = f"Display settings are unavailable; the module was not started. {error}"
            Plugin._state["settings_error"] = message
            Plugin._state["setup_error"] = message
            decky.logger.error(f"{LOG} {message}")
            return
        Plugin._state["settings_error"] = ""
        Plugin._native_probe_after = 0.0
        Plugin._state["reset_in_progress"] = settings.getSetting("reset_in_progress", False) is True
        Plugin._state["reset_error"] = ""
        Plugin._state["enabled"] = _strict_bool(
            settings.getSetting("enabled", DEFAULT_SETTINGS["enabled"]),
            DEFAULT_SETTINGS["enabled"],
        )
        Plugin._state["edid_fix"] = _strict_bool(
            settings.getSetting("edid_fix", DEFAULT_SETTINGS["edid_fix"]),
            DEFAULT_SETTINGS["edid_fix"],
        )

        stored = settings.getSetting("panel_mode", DEFAULT_SETTINGS["panel_mode"])
        if stored in MODES:
            Plugin._state["panel_mode"] = _select_mode(stored)
            decky.logger.info(f"{LOG} panel mode {stored}")
        else:
            # No mode yet, so the panel shows the chooser and nothing is
            # installed until the user has decided.
            Plugin._state["panel_mode"] = None
            decky.logger.info(f"{LOG} no panel mode chosen yet")

        active = settings.getSetting("active_mode", DEFAULT_SETTINGS["active_mode"])
        Plugin._state["active_mode"] = active if active in MODES else None

        # Anything we wrote last time is still out there; the state that knew
        # about it is not. Pick the session up first so the gate has somewhere
        # to read from, and take back the EDID bytes we would have to restore.
        if not await _offload(_pick_display):
            decky.logger.info(f"{LOG} gamescope not up yet, the loop will retry")
        saved = await _offload(_read_bytes, EDID_BACKUP)
        if saved:
            Plugin._edid_original = saved
            decky.logger.info(
                f"{LOG} recovered the original EDID from a previous run")

        await _offload(Plugin._refresh_setup)

        if Plugin._state["reset_in_progress"]:
            await self.reset_settings()

        # As early as the gate allows, and before the slower startup work. The
        # Vulkan layer reads GAMESCOPE_HDR_OUTPUT_FEEDBACK once, when a game
        # creates its surface, so a game that starts before the forced-support
        # flag is back loses HDR for its whole run. This cannot close the window
        # completely - the plugin can start before gamescope does - but it takes
        # everything below out of it.
        if Plugin._state.get("active_mode") == MODE_HYBRID:
            await _offload(Plugin._hybrid_pass)

        matched, desc = await _offload(_identify_panel)
        Plugin._state["panel_ok"] = matched
        Plugin._state["panel_desc"] = desc

        # The EDID half is not tied to the panel allowlist: the DXVK bug fires on
        # any display carrying a DisplayID 2.0 block, and dropping a block DXVK
        # never reads cannot cost anything.
        await _offload(Plugin._session_props, [ATOM_IS_EXTERNAL])
        await _offload(Plugin._edid_pass)

        bl_dir = (await _offload(_find_backlight)
                  if not Plugin.native_display_active() else "")
        Plugin._state["backlight"] = os.path.basename(bl_dir) if bl_dir else ""
        max_raw = _read_int(os.path.join(bl_dir, "max_brightness")) if bl_dir else None
        Plugin._state["max_nits"] = (
            round(_millinits_to_nits(max_raw), 2) if max_raw else 0.0)

        if Plugin.native_display_active():
            decky.logger.info(f"{LOG} native display support; maintaining the independent EDID fix")
        elif not matched:
            Plugin._state["reason"] = f"display not on the affected list: {desc}"
            decky.logger.info(f"{LOG} brightness half idle - {desc}")
        elif not bl_dir:
            Plugin._state["reason"] = "no backlight device found"
            decky.logger.warning(f"{LOG} brightness half idle - no backlight device")
        else:
            decky.logger.info(
                f"{LOG} watching {Plugin._state['backlight']} "
                f"(max {Plugin._state['max_nits']} nits) on {desc}"
            )

        # The loop runs either way: the EDID half is not device specific, and the
        # setup gate needs re-checking so the panel updates itself after Setup.
        Plugin._prop_task = asyncio.create_task(_watch_properties())
        Plugin._task = asyncio.create_task(self._loop(bl_dir))

    async def _unload(self, uninstalling=False):
        # Stop both workers even when settings cannot be read. A failed unload
        # must not leave an active display loop behind a disabled module.
        tasks = {task for task in (Plugin._task, Plugin._prop_task) if task is not None}
        for task in tasks:
            task.cancel()
        Plugin._task = None
        Plugin._prop_task = None
        if tasks:
            await asyncio.wait(tasks, timeout=2.0)
        # Module recovery and uninstall can call us before _main. Detect native
        # ownership before any of the old release/default-atom paths can run.
        await _offload(Plugin._read_settings)
        await _offload(Plugin._refresh_native_support, True)
        # Put both things back before we go. The EDID would otherwise stay
        # trimmed with nothing left to maintain it, and the atom would keep
        # whatever level we last forwarded.
        try:
            await _offload(Plugin._release)
        except Exception:
            pass
        # Hybrid holds the panel in gamma 2.2 between HDR games. Leaving it there
        # with nothing left to switch it back would cost the user their HDR.
        if Plugin._state.get("active_mode") == MODE_HYBRID:
            try:
                await _offload(Plugin._hybrid_release)
            except Exception:
                pass
        if uninstalling:
            # Removing the script only affects the next gamescope process. Put
            # the current internal session into gamma 2.2 now, so uninstalling
            # cannot strand the user in PQ with a dead hardware slider.
            try:
                def release_legacy_atoms():
                    with Plugin._runtime_lock:
                        if Plugin.native_display_active():
                            return
                        props = Plugin._session_props([ATOM_IS_EXTERNAL])
                        _write_atom_int(ATOM_FORCE_HDR_SUPPORT, 0)
                        if _as_int(props.get(ATOM_IS_EXTERNAL)) == 0:
                            _write_atom_int(ATOM_HDR_ENABLED, 0)
                await _offload(release_legacy_atoms)
            except Exception:
                pass
        try:
            await _offload(Plugin._edid_restore)
        except Exception:
            pass
        decky.logger.info(f"{LOG} unloaded")

    async def _uninstall(self):
        await self._unload(uninstalling=True)
        try:
            def uninstall_legacy_script():
                with Plugin._runtime_lock:
                    if Plugin.native_display_active():
                        return None
                    return _uninstall_script()
            note = await _offload(uninstall_legacy_script)
            if note is None:
                await self.cleanup_native_display()
                return
            decky.logger.info(f"{LOG} uninstall: {note}")
        except Exception as e:
            decky.logger.error(f"{LOG} uninstall: {e}")

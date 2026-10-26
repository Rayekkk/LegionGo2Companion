# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/LeGo2BrightnessFix

"""HDR brightness fix for games under gamescope.

DXVK's vendored libdisplay-info discards the whole EDID when it refuses a
DisplayID 2.0 block, so games see placeholder metadata (1499/799 nits, P3)
instead of the panel's 1107/476 nits. Dropping the DisplayID extension from
gamescope's published copy fixes that; DXVK never reads DisplayID. A stopgap
until DXVK is fixed (Plugin._edid_pass).

SteamOS 3.9 drives the Legion Go 2 panel itself (gamescope's native profile).
Companion 1.0 to 2.3.0 shipped its own display script for older SteamOS; every
start removes what is left of it (_remove_old_scripts, _drop_retired_settings).
"""

import asyncio
import copy
from diagnostics import event as diagnostic_event, transition as diagnostic_transition
import errno
import functools
import hashlib
import os
import posixpath
from system_process import PYTHON, run, system_env as _system_env
import signal
import stat
import subprocess
import threading
import time

try:
    import pwd
except ImportError:  # pragma: no cover - Windows test host
    pwd = None

import decky
import module_runtime
import display_native
import safe_settings
from safe_settings import SettingsManager, CorruptSettings

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

LOG = "[lego2brightnessfix]"


# gamescope publishes these on the root window of its Xwayland display.
ATOM_EDID_PATH = "GAMESCOPE_DISPLAY_EDID_PATH"
ATOM_IS_EXTERNAL = "GAMESCOPE_DISPLAY_IS_EXTERNAL"  # only on the gamescope control root

WATCHED_ATOMS = (ATOM_EDID_PATH,)
_prop_cache: dict[str, str] = {}
_prop_cache_display: str | None = None
_prop_cache_ready = False
_prop_cache_lock = threading.Lock()

# Xwayland numbering is not fixed (a desktop session may hold :0), so the server
# is found by gamescope's own atoms; xprop exits 0 even for a missing atom.
DISPLAY = ":0"          # replaced at startup by _pick_display()
X11_SOCKET_DIR = "/tmp/.X11-unix"
DISPLAY_FALLBACK = (":0", ":1")

# gamescope rewrites the published EDID on every connector change (dock, undock),
# so the check repeats. The path comes from the watcher's cache, not a spawn.
GATE_INTERVAL_S = 2.0
WATCHER_RECOVERY_S = 30.0  # Longer than a Gamescope restart.
# Desktop Mode has no gamescope atoms: while no gamescope-wl runs, X probes back
# off (doubling) to this; a returning gamescope-wl is probed at once.
ABSENT_PROBE_MAX_S = 32.0
CLEANUP_RETRY_MAX_S = 300.0  # a failure that lasts (e.g. a symlinked ~/.config/gamescope) is retried this rarely

# Companion 1.0-2.3.0 installed one of these under SCRIPT_NAME in gamescope's
# user script dir (2.1.0 and earlier also, unseen, under /root). SHA-256 of the
# LF bytes of the PQ and gamma 2.2 scripts; nothing else there is ours to remove.
SCRIPT_NAME = "lenovo.legiongo2.oled.lua"
OLD_SCRIPT_SHA256 = frozenset({
    "eea882283c9d3b13456aba526750393ad80b4cba96593741dbd3c393e982a13a",  # pq
    "7c6781dfb699c042f654ab876a6e37bd224a0edb3f27f48d3a09b1c71ca29e04",  # gamma22
})
# As root, _bind_script_dir() rebinds these paths to gamescope's user and file
# changes go through display_script_io.py as that user.
LEGACY_SCRIPT_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "gamescope", "scripts",
)
SCRIPT_DIR = LEGACY_SCRIPT_DIR
INSTALLED_SCRIPT = os.path.join(SCRIPT_DIR, SCRIPT_NAME)
SCRIPT_USER = None  # (config dir, uid, gid) that script changes run as
SCRIPT_IO = os.path.join(PLUGIN_DIR, "display_script_io.py")
_script_binding = None  # what SCRIPT_DIR was resolved from

# The original bytes must outlive a reload, so turning the fix off can still
# restore them. Root reads them back, so they live outside the settings folder
# Decky hands to the user; 2.3.0 and earlier kept them in OLD_EDID_BACKUP.
EDID_BACKUP = "/var/lib/legiongo2companion/edid-original.bin"
OLD_EDID_BACKUP = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "edid-original.bin")

EDID_BLOCK = 128
EDID_HEADER = b"\x00\xff\xff\xff\xff\xff\xff\x00"
EXT_TAG_CTA = 0x02
EXT_TAG_DISPLAYID = 0x70

# Every other key in display_settings.json belonged to the retired display script.
DEFAULT_SETTINGS = {
    "edid_fix": True,
    "edid_write_pending": None,
    "edid_patched_identity": None,
}

settings = SettingsManager(
    name="display_settings",
    settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR,
)
_settings_lock = threading.RLock()


_offload = functools.partial(module_runtime.offload, 'display')


# -- EDID parsing --

def _valid_edid(data) -> bool:
    return bool(data) and len(data) % EDID_BLOCK == 0 and data[:8] == EDID_HEADER


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
            if size == 0 or j + size >= EDID_BLOCK:
                break  # malformed block: never read past the 128-byte extension
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


# -- gamescope atoms --

def _display_owner(display=None):
    try:
        number = (display or DISPLAY).rsplit(":", 1)[1].split(".", 1)[0]
        if not number.isdigit():
            return None
        return os.stat(os.path.join(X11_SOCKET_DIR, "X" + number)).st_uid
    except (AttributeError, IndexError, OSError):
        return None


def _account(uid):
    """The passwd entry for `uid`, or None when there is none."""
    if pwd is None or uid is None:
        return None
    try:
        return pwd.getpwuid(uid)
    except KeyError:
        return None


def _xprop_env(display=None) -> dict:
    env = _system_env(DISPLAY=display or DISPLAY)
    owner = _display_owner(display)
    account = _account(owner)
    if account is not None:
        env.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name,
                   XDG_RUNTIME_DIR=f"/run/user/{owner}")
        env.pop("XAUTHORITY", None)
    return env


def _xprop_command(arguments):
    binary = "/usr/bin/xprop" if os.path.exists("/usr/bin/xprop") else "xprop"
    return [binary] + list(arguments)


def _is_root() -> bool:
    get_euid = getattr(os, "geteuid", None)  # absent on the Windows test host
    return get_euid is not None and get_euid() == 0


def _run_xprop(args, display, timeout):
    return run(_xprop_command(args), timeout, env=_xprop_env(display), stdin=None,
               capture_output=True, text=True, **_xprop_user(display))


def _xprop_user(display=None) -> dict:
    """Spawn kwargs that drop root to the display owner (no PAM session, unlike runuser)."""
    owner = _display_owner(display)
    account = _account(owner) if _is_root() and owner != 0 else None
    return {} if account is None else {"user": owner, "group": account.pw_gid, "extra_groups": []}


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
    global _prop_cache_ready
    parsed = _parse_prop_line(line)
    if parsed is None:
        return
    name, value = parsed
    with _prop_cache_lock:
        if _prop_cache_display != display:
            return  # Cleared under a live spy: only a fresh seed may mark it complete.
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
    """Raw text of each atom on the gamescope root window, in one xprop call."""
    try:
        result = _run_xprop(["-root"] + list(names), display, 3)
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:  # partial output proves neither ownership nor absence
        return {}
    values = {}
    for line in result.stdout.splitlines():
        for name in names:
            if line.startswith(name + "(") and "= " in line:
                values[name] = line.split("= ", 1)[1].strip()
    return values


def _read_props(names, *, fresh=False) -> dict:
    cached = None if fresh else _cached_props(names)
    return cached if cached is not None else _read_props_uncached(names)


async def _terminate_prop_process(process) -> None:
    """Stop the xprop watcher's process group: SIGTERM, then SIGKILL after a second."""
    if process is None or process.returncode is not None:
        return
    for signame, fallback in (("SIGTERM", process.terminate), ("SIGKILL", process.kill)):
        try:
            if os.name == "posix":
                os.killpg(process.pid, getattr(signal, signame))
            else:  # Windows is only a unit-test host
                fallback()
        except OSError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
            return
        except asyncio.TimeoutError:
            pass


async def _watch_properties() -> None:
    """Keep one xprop process alive and cache Gamescope property changes."""
    lost_at = None  # Gamescope restarts drop the watcher; only an outage that outlasts one is a problem.
    probe_after, delay = 0.0, GATE_INTERVAL_S
    while True:
        process = None
        display = None
        try:
            if time.monotonic() < probe_after and await _offload(_gamescope_start_time) is None:
                await asyncio.sleep(GATE_INTERVAL_S)
                continue
            if not await _offload(_pick_display):
                lost_at = None  # No Gamescope (Desktop mode) - nothing to watch.
                _invalidate_prop_cache()
                probe_after, delay = time.monotonic() + delay, min(delay * 2, ABSENT_PROBE_MAX_S)
                await asyncio.sleep(GATE_INTERVAL_S)
                continue
            delay = GATE_INTERVAL_S
            display = DISPLAY
            initial = await _offload(_read_props_uncached, WATCHED_ATOMS, display)
            _set_prop_cache(initial, display, ready=bool(initial))
            command = _xprop_command(["-spy", "-root"] + list(WATCHED_ATOMS))
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_xprop_env(display),
                start_new_session=os.name == "posix",
                **_xprop_user(display),
            )
            if process.stdout is None:
                raise RuntimeError("xprop watcher has no stdout")
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                lost_at = None
                _update_prop_cache(raw.decode("utf-8", errors="replace").strip(), display)
            lost_at = time.monotonic() if lost_at is None else lost_at
            if time.monotonic() - lost_at >= WATCHER_RECOVERY_S:
                diagnostic_event('display', 'property_watcher_disconnected', level='warning', repeat=60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            lost_at = time.monotonic() if lost_at is None else lost_at
            if time.monotonic() - lost_at >= WATCHER_RECOVERY_S:
                diagnostic_event('display', 'property_watcher_failed', level='warning', error=exc, repeat=60)
        finally:
            await _terminate_prop_process(process)
            if display is not None:
                _invalidate_prop_cache(display)
        await asyncio.sleep(0.5)


def _as_path(raw):
    """The quoted value of a UTF8_STRING property, or None."""
    if not raw or '"' not in raw:
        return None
    return raw.split('"')[1] or None


def _probe_gamescope(display: str) -> tuple:
    try:
        out = _run_xprop(["-root"], display, 5).stdout
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
        _invalidate_prop_cache()
    DISPLAY = best
    return True


# -- files --

# An EDID is at most 256 blocks of 128 bytes; the old Lua scripts are a few KiB.
MAX_FILE_BYTES = 32 * 1024


def _open_regular(path: str, writable: bool = False):
    """Unbuffered handle on a bounded regular file, never via a final symlink.

    These paths come from X properties, sysfs and user-owned directories, and
    this process is root. O_NONBLOCK stops a FIFO swapped in after a check from
    hanging the open; the type is checked on the descriptor before any read.
    """
    flags = os.O_RDWR if writable else os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC", "O_BINARY"):
        flags |= getattr(os, name, 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise OSError(errno.EINVAL, "not a bounded regular file", path)
        return os.fdopen(fd, "r+b" if writable else "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise


def _read_capped(handle) -> bytes:
    """Read to EOF, refusing anything that grows past MAX_FILE_BYTES."""
    chunks, size = [], 0
    while chunk := handle.read(MAX_FILE_BYTES + 1 - size):
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_FILE_BYTES:
            raise OSError(errno.EFBIG, "file exceeds the read limit")
    return b"".join(chunks)


def _read_bytes(path: str):
    try:
        with _open_regular(path) as f:
            # Anything in the user's script dir that they do not own got there
            # through a link or from someone else; it is not theirs, nor ours.
            if (SCRIPT_USER is not None and os.path.dirname(path) == SCRIPT_DIR
                    and os.fstat(f.fileno()).st_uid != SCRIPT_USER[1]):
                return None
            return _read_capped(f)
    except OSError:
        return None


def _write_file_atomically(path: str, data: bytes) -> bool:
    """Durably replace a regular file without following staging/target links."""
    try:
        # Gamescope runs unprivileged and must be able to read a restored script.
        safe_settings._atomic_write_bytes(path, data, mode=0o644)
        return True
    except (OSError, safe_settings.UnsafeSettingsPath):
        return False


# -- the original EDID, kept by root --

def _private_dir(create: bool = False) -> str:
    """EDID_BACKUP's directory, only while it is ours alone: a real directory, our uid, no group/other access."""
    directory = os.path.dirname(EDID_BACKUP)
    if create:
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
    info = os.lstat(directory)
    get_euid = getattr(os, "geteuid", None)  # absent on the Windows test host
    if not stat.S_ISDIR(info.st_mode) or (
            get_euid is not None and (info.st_uid != get_euid() or info.st_mode & 0o077)):
        raise OSError(errno.EPERM, "unsafe EDID backup directory", directory)
    return directory


def _read_edid_backup(path: str, *, owned: bool):
    """A saved original EDID, or None. `owned` also requires the file to be ours."""
    try:
        with _open_regular(path) as handle:
            get_euid = getattr(os, "geteuid", None)
            if owned and get_euid is not None and os.fstat(handle.fileno()).st_uid != get_euid():
                return None
            data = _read_capped(handle)
    except OSError:
        return None
    return data if _valid_edid(data) else None


def _save_edid_backup(data: bytes) -> bool:
    try:
        _private_dir(create=True)
        safe_settings._atomic_write_bytes(EDID_BACKUP, data)  # 0600
        return True
    except (OSError, safe_settings.UnsafeSettingsPath):
        return False


def _load_edid_backup():
    """The saved original EDID; a copy left in the settings folder by 2.3.0 or earlier moves to EDID_BACKUP."""
    try:
        _private_dir()
        data = _read_edid_backup(EDID_BACKUP, owned=True)
    except OSError:
        data = None
    old = _read_edid_backup(OLD_EDID_BACKUP, owned=False)
    if old is None:
        return data
    if data is None:
        if not _save_edid_backup(old):
            return old  # The old copy stays until it is safely moved.
        data = old
        decky.logger.info(f"{LOG} moved the original EDID out of the settings folder")
    try:
        # Through a settings folder symlinked to ours, the old path is the private copy itself.
        if not os.path.samestat(os.lstat(OLD_EDID_BACKUP), os.stat(EDID_BACKUP)):
            os.unlink(OLD_EDID_BACKUP)
    except OSError:
        pass
    return data


# -- gamescope session --

_gamescope_pid = None
_gamescope_scan_after = 0.0
_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


# Session identities start above any epoch time (2.1 saved epoch stamps).
_IDENTITY_BASE = 1e11


def _boot_offset():
    """A per-boot constant, so process start ticks differ between boots."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as handle:
            return _IDENTITY_BASE + int(handle.read().strip().replace("-", "")[:6], 16) * 1000.0
    except (OSError, ValueError):
        return _IDENTITY_BASE


def _gamescope_start_time():
    """Identify the live session, scanning /proc only after losing its PID."""
    global _gamescope_pid, _gamescope_scan_after
    def started(pid):
        with open(f"/proc/{pid}/comm") as handle:
            if handle.read().strip() != "gamescope-wl":
                return None
        # starttime (field 22) is fixed at fork; the /proc/<pid> inode mtime is
        # only the moment something first looked and can change with the dcache.
        with open(f"/proc/{pid}/stat") as handle:
            try:
                ticks = int(handle.read().rpartition(")")[2].split()[19])
            except (ValueError, IndexError):
                return None
        return _boot_offset() + ticks / _CLK_TCK
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


# -- old display script cleanup --

def _gamescope_config_dir(pid):
    """(config dir, uid, gid) that gamescope `pid` reads its scripts under."""
    uid = os.stat(f"/proc/{pid}").st_uid
    account = pwd.getpwuid(uid)
    # gamescope's environment is always POSIX; posixpath keeps that true on any test host.
    base = posixpath.join(account.pw_dir, ".config")
    with open(f"/proc/{pid}/environ", "rb") as handle:
        environ = handle.read(256 * 1024)
    for entry in environ.split(b"\0"):
        if entry.startswith(b"XDG_CONFIG_HOME="):
            value = os.fsdecode(entry[len(b"XDG_CONFIG_HOME="):])
            # The XDG spec ignores a relative value, and so does gamescope.
            base = posixpath.normpath(value) if posixpath.isabs(value) else base
    return base, uid, account.pw_gid


def _bind_script_dir() -> bool:
    """Point the script paths at gamescope's user. False while root cannot tell.

    An unprivileged plugin already is that user, so only root rebinds.
    """
    global SCRIPT_DIR, INSTALLED_SCRIPT, SCRIPT_USER, _script_binding
    if not _is_root():
        return True
    _gamescope_start_time()
    pid = _gamescope_pid
    try:
        if pid is not None:
            key = ("gamescope", pid)
            if key == _script_binding:
                return True
            base, uid, gid = _gamescope_config_dir(pid)
        else:
            # Desktop Mode has no gamescope, but its X display names the user.
            uid = _display_owner()
            if uid is None:
                return _script_binding is not None
            key = ("display", uid)
            if key == _script_binding:
                return True
            account = pwd.getpwuid(uid)
            base, gid = os.path.join(account.pw_dir, ".config"), account.pw_gid
    except (OSError, KeyError):
        return _script_binding is not None
    SCRIPT_DIR = os.path.join(base, "gamescope", "scripts")
    INSTALLED_SCRIPT = os.path.join(SCRIPT_DIR, SCRIPT_NAME)
    SCRIPT_USER = (base, uid, gid) if uid != 0 else None
    _script_binding = key
    return True


def _run_script_io(operation: str, path: str, data: bytes = b"") -> int:
    """Run display_script_io.py as SCRIPT_USER; its exit code, 1 on failure."""
    base, uid, gid = SCRIPT_USER
    if os.path.dirname(path) != SCRIPT_DIR:
        return 1
    try:
        result = run([PYTHON, "-I", SCRIPT_IO, operation, base, os.path.basename(path)], 10,
                     env=_system_env(), stdin=None, input=data, capture_output=True, cwd="/",
                     user=uid, group=gid, extra_groups=[])
    except (OSError, subprocess.SubprocessError) as exc:
        diagnostic_event('display', 'script_io_failed', level='error', error=exc,
                         operation=operation, repeat=60)
        return 1
    if result.returncode not in (0, 2):
        diagnostic_event('display', 'script_io_failed', level='error', operation=operation,
                         returncode=result.returncode, repeat=60)
    return result.returncode


def _script_write(path: str, data: bytes) -> bool:
    """Atomically write a file in SCRIPT_DIR, as the user who owns that dir."""
    if SCRIPT_USER is None:
        return _write_file_atomically(path, data)
    return _run_script_io("write", path, data) == 0


def _script_unlink(path: str) -> None:
    """os.unlink() for a file in SCRIPT_DIR, as the user who owns that dir."""
    if SCRIPT_USER is None:
        os.unlink(path)
        return
    code = _run_script_io("unlink", path)
    if code == 2:
        raise FileNotFoundError(errno.ENOENT, "no such file", path)
    if code:
        raise OSError(errno.EIO, "could not remove the file", path)


def _is_our_script(data) -> bool:
    """True only for a script Companion shipped; release zips may carry CRLF line endings."""
    return data is not None and any(hashlib.sha256(variant).hexdigest() in OLD_SCRIPT_SHA256
                                    for variant in (data, data.replace(b"\r\n", b"\n")))


def _unlink_if_unchanged(path: str, data: bytes) -> None:
    """Remove our file, but not one replaced since it was checked."""
    if _read_bytes(path) != data:
        raise OSError(errno.EAGAIN, "the file changed while it was being removed", path)
    try:
        _script_unlink(path)
    except FileNotFoundError:
        pass


def _remove_legacy_scripts() -> None:
    """Drop our own copies from the /root dir 2.1.0 and earlier installed to."""
    if SCRIPT_DIR == LEGACY_SCRIPT_DIR:
        return
    for leaf in (SCRIPT_NAME, SCRIPT_NAME + ".backup"):
        path = os.path.join(LEGACY_SCRIPT_DIR, leaf)
        if _is_our_script(_read_bytes(path)):
            try:
                os.unlink(path)
            except OSError:
                pass


def _remove_old_scripts(restore_backup: bool):
    """Remove the display script 2.3.0 and earlier installed. What was done, or None to retry.

    `restore_backup` (no native profile, SteamOS 3.8) puts back the user's own
    script that Setup kept as .backup; with the native profile it would override
    SteamOS, so it stays where it is.
    """
    if not _bind_script_dir():
        return None
    _remove_legacy_scripts()
    backup = INSTALLED_SCRIPT + ".backup"
    current = _read_bytes(INSTALLED_SCRIPT)
    if current is None and os.path.lexists(INSTALLED_SCRIPT):
        return "kept an unreadable display script"
    if current is not None and not _is_our_script(current):
        return "kept a third-party display script"
    saved = _read_bytes(backup)
    if _is_our_script(saved):
        _unlink_if_unchanged(backup, saved)
        saved = None
    if restore_backup and saved is not None:
        if not _script_write(INSTALLED_SCRIPT, saved):
            raise OSError(errno.EIO, "could not restore the previous display script", INSTALLED_SCRIPT)
        _unlink_if_unchanged(backup, saved)
        return "restored the previous display script"
    if current is not None:
        _unlink_if_unchanged(INSTALLED_SCRIPT, current)
        return "removed the old display script"
    return "nothing to remove"


# -- EDID ownership and recovery --

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
    gamescope_owner = _display_owner()
    if gamescope_owner is not None:
        return owner == gamescope_owner
    euid = get_euid()
    return owner != 0 if euid == 0 else owner == euid


def _edid_owned_identity():
    """(device, inode) of the file our last patch wrote, or None when unknown (e.g. patched by 2.1.0)."""
    owned = settings.getSetting("edid_patched_identity", None)
    if isinstance(owned, dict) and type(owned.get("device")) is int and type(owned.get("inode")) is int:
        return owned["device"], owned["inode"]
    return None


def _edid_restore_action(current: bytes, original: bytes, identity, pending, owned) -> str:
    """What a restore may do with the published EDID it just read.

    "changed" and "written" are final results, "rewrite" puts the original back,
    "not_ours" and "foreign" leave the file alone, "unverified" keeps recovery pending.
    """
    if pending and identity != (pending["device"], pending["inode"]):
        # A new connector owns a different inode, even if its bytes happen
        # to equal the interrupted partial write. Never restore over it.
        return "changed"
    if current == original:
        return "written"
    if current == _strip_displayid(original):
        # Our trimmed bytes in a file we never wrote: a new file is not ours to restore.
        return "not_ours" if owned not in (None, identity) else "rewrite"
    if pending:
        if pending.get("partial_sha256") != hashlib.sha256(current).hexdigest():
            return "unverified"
        return "rewrite"
    return "foreign"


def _forget_edid_baseline() -> bool:
    """Drop the backup, the remembered original and its file identity; False keeps them for a retry."""
    try:
        os.unlink(EDID_BACKUP)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    Plugin._edid_original = None
    Plugin._store_settings({"edid_patched_identity": None})
    return True


def _clear_edid_write_pending() -> bool:
    if settings.getSetting("edid_write_pending", None) is None:
        return True
    try:
        Plugin._store_settings({"edid_write_pending": None}, required=True)
        diagnostic_transition('display', 'edid_recovery_store', 'ready', store='display_settings', pending=False)
        return True
    except Exception as exc:
        diagnostic_transition('display', 'edid_recovery_store', 'deferred', level='warning',
                              error=exc, store='display_settings', stage='clear', pending=True)
        return False  # Keep the durable recovery intent until a later retry.


def _overwrite_in_place(handle, data: bytes) -> None:
    """Replace the whole content of an open file on the same inode, durably."""
    handle.seek(0)
    view = memoryview(data)
    while view:
        written = handle.write(view)
        if not written:
            raise OSError("short write")
        view = view[written:]
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


def _rewrite_file_if_unchanged(path: str, expected: bytes,
                               replacement: bytes, expected_identity=None) -> str:
    """Rewrite the verified inode without ever replacing a newer connector.

    gamescope publishes connector changes with an atomic rename. Keeping the
    verified file descriptor open means a rename after our check only makes us
    update the now-unlinked old inode; the new display's path stays untouched.
    Returns ``written``, ``changed`` or ``failed``.
    """
    try:
        with _open_regular(path, writable=True) as f:
            opened = os.fstat(f.fileno())
            if not _published_edid_is_safe(path, opened):
                return "failed"
            if _read_capped(f) != expected:
                return "changed"
            if expected_identity is not None and (opened.st_dev, opened.st_ino) != expected_identity:
                return "changed"
            def still_published():
                published = os.stat(path)
                return (opened.st_dev, opened.st_ino) == (published.st_dev, published.st_ino)

            if not still_published():
                return "changed"

            previous_pending = copy.deepcopy(settings.getSetting("edid_write_pending", None))
            pending = dict(schema=1, path=os.path.abspath(path), device=opened.st_dev,
                           inode=opened.st_ino, partial_sha256=None,
                           original_sha256=hashlib.sha256(Plugin._edid_original or expected).hexdigest())
            # A failed write can also prevent rollback. Publish recovery intent
            # before touching data so a reload cannot mistake our partial write
            # for a new connector and discard the only original bytes.
            try:
                Plugin._store_settings({"edid_write_pending": pending}, required=True)
            except Exception as exc:
                diagnostic_transition('display', 'edid_recovery_store', 'deferred', level='warning',
                                      error=exc, stage='intent', store='display_settings', pending=True)
                return "failed"
            try:
                _overwrite_in_place(f, replacement)
            except OSError as exc:
                # A failed in-place update may have written a prefix. Restore the
                # verified bytes on the same inode before admitting failure.
                try:
                    _overwrite_in_place(f, expected)
                    f.seek(0)
                    if _read_capped(f) == expected:
                        # Rollback returns to the entry state, which can itself
                        # be an owned partial write under recovery. Preserve its
                        # proof instead of declaring that original bytes won.
                        try:
                            Plugin._store_settings({"edid_write_pending": previous_pending}, required=True)
                        except Exception as proof_error:
                            diagnostic_transition('display', 'edid_recovery_store', 'deferred', level='warning',
                                                  error=proof_error, stage='rollback_proof', store='display_settings', pending=True)
                        diagnostic_event('display', 'edid_write_rolled_back', level='warning', error=exc, repeat=60,
                                         restored=expected == Plugin._edid_original,
                                         pending=settings.getSetting('edid_write_pending', None) is not None)
                        return "failed"
                except OSError:
                    pass
                # Capture partial contents through the same verified descriptor.
                # If this commit also fails (e.g. full settings disk), the older
                # intent without a digest remains, requiring deferred recovery.
                try:
                    f.seek(0)
                    pending["partial_sha256"] = hashlib.sha256(_read_capped(f)).hexdigest()
                    Plugin._store_settings({"edid_write_pending": pending}, required=True)
                    diagnostic_transition('display', 'edid_recovery', 'partial_recorded', level='warning', pending=True)
                except Exception as proof_error:
                    diagnostic_transition('display', 'edid_recovery', 'unverified_partial', level='error',
                                          error=proof_error, stage='partial_proof', pending=True)
                return "failed"

            # A connector can change while the old descriptor is being written.
            # That is safe, but report it as changed rather than claiming the new
            # published path is already patched.
            current = still_published()
            _clear_edid_write_pending()
            return "written" if current else "changed"
    except OSError as exc:
        diagnostic_transition('display', 'edid_recovery', 'io_failed', level='warning', error=exc, stage='rewrite', pending=True)
        return "failed"


class Plugin:
    # Companion's display support is for the Go 2's OLED; SteamOS 3.9 has its profile.
    applicable = staticmethod(module_runtime.is_legion_go_2)

    # Everything the page reports back to the UI.
    _state = {
        "settings_error": "",
        "steamos_update_needed": False,  # no native panel profile: SteamOS older than 3.9
        "edid_fix": True,
        "edid_patched": False,
        "edid_reason": "starting up",
        "edid_game_nits": 0.0,    # peak luminance games will read
    }
    _edid_original = None         # untouched bytes, for putting back
    _edid_path = None
    _task = None
    _prop_task = None
    _cleanup_done = False         # the old display script was dealt with this start
    _cleanup_retry_after = 0.0
    _cleanup_retry_delay = GATE_INTERVAL_S  # grows while removing it keeps failing
    _session_retry_after = 0.0
    _session_retry_delay = GATE_INTERVAL_S  # grows only while gamescope-wl is gone
    # Re-entrant on purpose: methods that take it also call each other while holding it.
    _edid_lock = threading.RLock()

    @staticmethod
    def _require_settings() -> None:
        error = getattr(settings, "recovery_error", "")
        if error:
            message = f"Display settings could not be recovered. Restore a valid settings copy and restart Decky. {error}"
            Plugin._state["settings_error"] = message
            raise CorruptSettings(message)

    @staticmethod
    def _read_settings() -> None:
        with _settings_lock:
            settings.read()
            Plugin._require_settings()

    @staticmethod
    def _store_settings(values: dict, *, required: bool = False) -> None:
        """Persist one transaction; recovery records must succeed before writes."""
        try:
            with _settings_lock:
                settings.read()
                Plugin._require_settings()
                payload = dict(settings.settings)
                payload.update(values)
                settings.replace(payload)
        except CorruptSettings:
            raise
        except Exception as e:
            if required:
                raise RuntimeError(f"Could not save display recovery state: {e}") from e
            diagnostic_event('display', 'store_settings_failed', level='warning', error=e, repeat=60)

    @staticmethod
    def _drop_retired_settings() -> None:
        """Forget what 2.3.0 and earlier saved for the display script, brightness and Hybrid."""
        with _settings_lock:
            settings.read()
            Plugin._require_settings()
            kept = {key: value for key, value in settings.settings.items() if key in DEFAULT_SETTINGS}
            if kept != settings.settings:
                settings.replace(kept)

    @staticmethod
    def _refresh_native() -> str:
        """Probe the system panel profile; without it SteamOS needs an update."""
        detection = display_native.detect_native_display_support()
        status = detection.get("status", "inconclusive")
        diagnostic_transition('display', 'native_support', status, supported=status == "supported",
                              initial=False, reason=detection.get('reason', 'unknown'))
        Plugin._state["steamos_update_needed"] = status == "absent"
        return status

    @staticmethod
    def _cleanup_old_display_fix() -> bool:
        """Remove the display script of 2.3.0 and earlier; True once done for this start."""
        if time.monotonic() < Plugin._cleanup_retry_after:
            return False
        status = Plugin._refresh_native()
        if status not in ("supported", "absent"):
            return False  # Whether to restore the user's .backup depends on the profile: ask again.
        try:
            outcome = _remove_old_scripts(restore_backup=status == "absent")
        except Exception as exc:
            diagnostic_event('display', 'old_script_cleanup_failed', level='warning', error=exc, repeat=60)
            # Each attempt starts a helper process as gamescope's user.
            Plugin._cleanup_retry_after = time.monotonic() + Plugin._cleanup_retry_delay
            Plugin._cleanup_retry_delay = min(Plugin._cleanup_retry_delay * 2, CLEANUP_RETRY_MAX_S)
            return False
        if outcome is None:
            return False  # gamescope's user is not known yet
        if outcome != "nothing to remove":
            decky.logger.info(f"{LOG} {outcome}")
            diagnostic_event('display', 'old_script_cleanup', outcome=outcome)
        return True

    @staticmethod
    def _session_props() -> dict:
        """Read the published EDID path, re-finding a session that moved."""
        wanted = [ATOM_EDID_PATH]
        if (time.monotonic() < Plugin._session_retry_after and _cached_props(wanted) is None
                and _gamescope_start_time() is None):
            return {}  # Desktop Mode: no xprop until the back-off runs out.
        props = _read_props(wanted)
        if not props:
            # Re-probe a few times during startup, but do not launch a scan every
            # pass forever while gamescope is down.
            now = time.monotonic()
            if now < Plugin._session_retry_after:
                return {}
            Plugin._session_retry_after = now + GATE_INTERVAL_S
            if not _pick_display():
                if _gamescope_start_time() is None:
                    Plugin._session_retry_after = now + Plugin._session_retry_delay
                    Plugin._session_retry_delay = min(Plugin._session_retry_delay * 2, ABSENT_PROBE_MAX_S)
                return {}
            # Retry live, but keep the watcher's snapshot: a running xprop -spy
            # only reports later changes, so clearing it leaves a partial cache.
            props = _read_props(wanted, fresh=True)
            if not props:
                return {}
        Plugin._session_retry_delay = GATE_INTERVAL_S
        Plugin._edid_path = (_as_path(props.get(ATOM_EDID_PATH))
                             or Plugin._edid_path)
        return props

    # -- exported to the frontend --

    async def get_state(self) -> dict:
        try:
            Plugin._require_settings()
        except CorruptSettings:
            pass
        await _offload(Plugin._refresh_native)
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

    # -- EDID --

    @staticmethod
    def _edid_restore() -> bool:
        """Put back the EDID exactly as gamescope wrote it.

        The file we would restore has to match the one on disk: after a dock the
        published EDID belongs to a different display, and writing this panel's
        bytes over it would be worse than leaving it trimmed.
        """
        with Plugin._edid_lock:
            original = Plugin._edid_original
            path = Plugin._edid_path
            pending = settings.getSetting("edid_write_pending", None)
            if (isinstance(pending, dict) and pending.get("schema") == 1 and isinstance(pending.get("path"), str)
                    and os.path.isabs(pending["path"]) and pending["path"] != os.path.abspath(path or "")):
                # gamescope published a new path. Only proof that the interrupted write's own
                # file is gone ends that recovery; an existing or unreadable old file keeps it.
                try:
                    os.lstat(pending["path"])
                except FileNotFoundError:
                    diagnostic_transition('display', 'edid_recovery', 'target_gone', pending=False)
                    # Equal bytes do not prove a new file is ours, so drop that baseline too. If the
                    # backup cannot be removed, the recovery record stays and a restart cannot reload it.
                    if not (_forget_edid_baseline() and _clear_edid_write_pending()):
                        return False
                    pending, original = None, None
                except OSError:
                    pass
            if pending is not None:
                if (not isinstance(pending, dict) or pending.get("schema") != 1
                        or pending.get("path") != os.path.abspath(path or "")
                        or type(pending.get("device")) is not int or type(pending.get("inode")) is not int
                        or not original or pending.get("original_sha256") != hashlib.sha256(original).hexdigest()):
                    Plugin._state["edid_reason"] = "EDID write recovery is pending; original bytes were preserved"
                    diagnostic_transition('display', 'edid_recovery', 'invalid_proof', level='warning', pending=True)
                    return False
            if not original or not path:
                Plugin._state["edid_patched"] = False
                Plugin._state["edid_reason"] = "turned off"
                return True
            try:
                os.lstat(path)
            except FileNotFoundError:
                # A compositor can remove its published copy between sessions.
                # There is no inode left to restore and none should be recreated.
                Plugin._state["edid_patched"] = False
                Plugin._state["edid_reason"] = "turned off, EDID path disappeared"
                return _clear_edid_write_pending()
            except OSError:
                pass  # Access errors still go through the fail-closed path below.
            if not _published_edid_is_safe(path):
                Plugin._state["edid_reason"] = \
                    "refusing an unsafe EDID path; original not restored"
                diagnostic_transition('display', 'edid_recovery', 'unsafe_path', level='warning', pending=True)
                return False

            try:
                with _open_regular(path) as handle:
                    current_info = os.fstat(handle.fileno())
                    if not _published_edid_is_safe(path, current_info):
                        return False
                    current = _read_capped(handle)
            except FileNotFoundError:
                decky.logger.info(f"{LOG} EDID path disappeared, not recreating it")
                Plugin._state["edid_patched"] = False
                Plugin._state["edid_reason"] = "turned off, EDID path disappeared"
                return _clear_edid_write_pending()
            except OSError as exc:
                Plugin._state["edid_reason"] = "could not read the published EDID; restoration will retry"
                diagnostic_transition('display', 'edid_recovery', 'read_failed', level='warning', error=exc, pending=True)
                return False
            identity = (current_info.st_dev, current_info.st_ino)
            action = _edid_restore_action(current, original, identity, pending, _edid_owned_identity())
            if action == "not_ours" and not _forget_edid_baseline():
                return False
            if action in ("not_ours", "foreign"):
                diagnostic_transition('display', 'edid_recovery', 'foreign', pending=False)
                Plugin._state["edid_patched"] = False
                Plugin._state["edid_reason"] = "turned off, display EDID changed"
                return True
            if action == "unverified":
                Plugin._state["edid_reason"] = "Partial EDID ownership is unverified; original bytes were preserved"
                diagnostic_transition('display', 'edid_recovery', 'ownership_unverified', level='warning', pending=True)
                return False
            result = (_rewrite_file_if_unchanged(path, current, original, expected_identity=identity)
                      if action == "rewrite" else action)

            if result == "written":
                if not _clear_edid_write_pending():
                    Plugin._state["edid_reason"] = "EDID restored; recovery metadata cleanup will retry"
                    return False
                decky.logger.info(f"{LOG} EDID restored")
                try:
                    os.unlink(EDID_BACKUP)
                except OSError:
                    pass
                Plugin._edid_original = None
                Plugin._store_settings({"edid_patched_identity": None})
                Plugin._state["edid_patched"] = False
                Plugin._state["edid_reason"] = "turned off"
                diagnostic_transition('display', 'edid_recovery', 'restored', restored=True, pending=False)
                return True
            if result == "changed":
                if not _clear_edid_write_pending():
                    return False
                Plugin._state["edid_patched"] = False
                Plugin._state["edid_reason"] = "EDID changed while it was being restored"
                return True

            Plugin._state["edid_patched"] = True
            Plugin._state["edid_reason"] = "could not restore the original EDID; retrying"
            diagnostic_transition('display', 'edid_recovery', 'restore_failed', level='warning', pending=True)
            return False

    @staticmethod
    def _edid_pass() -> None:
        """Keep gamescope's published EDID parseable by DXVK.

        Runs on the gate cadence rather than once at startup: gamescope rewrites
        this file whenever the connector changes, so a dock or an undock would
        otherwise silently undo the fix.
        """
        with Plugin._edid_lock:
            if module_runtime.stop_requested("display"):
                return
            if not Plugin._state["edid_fix"]:
                Plugin._edid_restore()
                return

            Plugin._session_props()

            if settings.getSetting("edid_write_pending", None) is not None:
                if not Plugin._edid_restore():
                    return  # Never replace the sole backup of an interrupted write.

            path = Plugin._edid_path
            if not path:
                Plugin._state["edid_reason"] = "gamescope publishes no EDID path"
                return
            if not _published_edid_is_safe(path):
                Plugin._state["edid_reason"] = "refusing an unsafe EDID path"
                diagnostic_transition('display', 'edid_recovery', 'unsafe_path', level='warning', pending=True)
                return

            try:
                with _open_regular(path) as f:
                    info = os.fstat(f.fileno())
                    if not _published_edid_is_safe(path, info):
                        Plugin._state["edid_reason"] = "refusing an unsafe EDID path"
                        return
                    data = _read_capped(f)
                identity = (info.st_dev, info.st_ino)
            except OSError as exc:
                Plugin._state["edid_reason"] = "EDID file unreadable"
                diagnostic_transition('display', 'edid_recovery', 'read_failed', level='warning', error=exc, pending=True)
                return

            if not _has_displayid(data):
                # Either we already fixed it - possibly before a reload, which is why
                # the original is kept on disk - or this display never had the block.
                ours = (_strip_displayid(Plugin._edid_original)
                        if Plugin._edid_original else None)
                if data == ours and _edid_owned_identity() not in (None, identity):
                    # Equal bytes in a file we never wrote: that baseline belongs to a file that is gone.
                    ours = None if _forget_edid_baseline() else ours
                Plugin._state["edid_patched"] = data == ours
                nits = _cta_max_luminance(data)
                Plugin._state["edid_game_nits"] = round(nits, 1) if nits else 0.0
                Plugin._state["edid_reason"] = (
                    "applied, games read the panel's real values"
                    if data == ours else
                    "nothing to fix on this display"
                )
                diagnostic_transition('display', 'edid_recovery', 'applied' if data == ours else 'unnecessary', pending=False)
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
            if not _save_edid_backup(data):
                Plugin._state["edid_reason"] = "could not save the original EDID"
                diagnostic_transition('display', 'edid_recovery', 'backup_failed', level='warning', stage='backup', pending=True)
                Plugin._edid_original = None
                return
            # The backup belongs to this inode from now on, also if the write below is interrupted.
            # The rewrite is in place and pinned to it, so a replaced file never inherits the backup.
            try:
                Plugin._store_settings({"edid_patched_identity": {"device": identity[0], "inode": identity[1]}}, required=True)
            except RuntimeError as exc:
                Plugin._state["edid_reason"] = "could not save the original EDID"
                diagnostic_transition('display', 'edid_recovery_store', 'deferred', level='warning', error=exc,
                                      store='display_settings', stage='identity', pending=True)
                return

            result = _rewrite_file_if_unchanged(path, data, patched, expected_identity=identity)
            if result == "written":
                Plugin._state["edid_patched"] = True
                Plugin._state["edid_game_nits"] = round(nits, 1)
                Plugin._state["edid_reason"] = "applied, games read the panel's real values"
                decky.logger.info(
                    f"{LOG} EDID: dropped DisplayID block, "
                    f"{len(data)} -> {len(patched)} bytes, games now see {nits:.1f} nits"
                )
                diagnostic_transition('display', 'edid_recovery', 'applied', pending=False)
            elif result == "changed":
                Plugin._state["edid_patched"] = False
                Plugin._state["edid_reason"] = "EDID changed while it was being written"
            else:
                Plugin._state["edid_patched"] = False
                Plugin._state["edid_reason"] = "could not write the EDID file"

    # -- loop --

    async def _loop(self):
        while True:
            try:
                if not Plugin._cleanup_done:
                    Plugin._cleanup_done = await _offload(Plugin._cleanup_old_display_fix)
                await _offload(Plugin._edid_pass)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let one bad pass kill the loop
                diagnostic_event('display', 'loop_failed', level='error', error=e, repeat=60)
            await asyncio.sleep(GATE_INTERVAL_S)

    async def _main(self):
        decky.logger.info(f"{LOG} startup")
        try:
            await _offload(Plugin._read_settings)
        except Exception as error:
            Plugin._state["settings_error"] = f"Display settings are unavailable; the module was not started. {error}"
            diagnostic_event('display', 'settings_unavailable', level='error', error=error)
            raise
        Plugin._state["settings_error"] = ""
        value = settings.getSetting("edid_fix", DEFAULT_SETTINGS["edid_fix"])
        Plugin._state["edid_fix"] = value if type(value) is bool else DEFAULT_SETTINGS["edid_fix"]
        try:
            await _offload(Plugin._drop_retired_settings)
        except Exception as exc:
            diagnostic_event('display', 'store_settings_failed', level='warning', error=exc, repeat=60)

        # Take back the EDID bytes we would have to restore.
        Plugin._edid_original = await _offload(_load_edid_backup)
        if Plugin._edid_original:
            decky.logger.info(f"{LOG} recovered the original EDID from a previous run")
        if not await _offload(_pick_display):
            decky.logger.info(f"{LOG} gamescope not up yet, the loop will retry")

        def check_stop():
            """A stop that came during a slow step (see main.MODULE_START_LIMIT_S): apply nothing more."""
            if module_runtime.stop_requested("display"):
                raise module_runtime.StartStopped("Display stopped while starting; nothing more was applied.")
        check_stop()
        Plugin._cleanup_retry_after, Plugin._cleanup_retry_delay = 0.0, GATE_INTERVAL_S
        Plugin._cleanup_done = await _offload(Plugin._cleanup_old_display_fix)
        check_stop()
        await _offload(Plugin._edid_pass)
        check_stop()
        Plugin._prop_task = asyncio.create_task(_watch_properties())
        Plugin._task = asyncio.create_task(self._loop())

    async def _unload(self):
        # Stop both workers even when settings cannot be read. A failed unload
        # must not leave an active display loop behind a disabled module.
        tasks = {task for task in (Plugin._task, Plugin._prop_task) if task is not None}
        for task in tasks:
            task.cancel()
        Plugin._task = None
        Plugin._prop_task = None
        if tasks:
            await asyncio.wait(tasks, timeout=2.0)
        await _offload(Plugin._read_settings)
        # The EDID would otherwise stay trimmed with nothing left to maintain it.
        try:
            await _offload(Plugin._edid_restore)
        except Exception as exc:
            diagnostic_event('display', 'withdrawal_failed', level='warning', error=exc, stage='edid', pending=True)
        decky.logger.info(f"{LOG} unloaded")

    async def _uninstall(self):
        await self._unload()

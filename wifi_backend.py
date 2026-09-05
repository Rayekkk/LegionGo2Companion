"""Audited WiFi Optimizer Go 2 module for Legion Go 2 Companion.

Runs as root inside the plugin_loader process. All public async methods on
the Plugin class are callable from the React frontend via Decky's IPC. State
is persisted to settings.json under DECKY_PLUGIN_SETTINGS_DIR.  A minimal
NetworkManager dispatcher may reapply an explicitly selected per-profile
power-save state; band policy is persisted by NetworkManager/iwd and guarded
by an independent rollback journal.
"""

import os
import json
import time
import asyncio
import subprocess
import re
import sys
import math
import copy
import secrets
import stat
import threading
from contextlib import asynccontextmanager

from safe_settings import (
    AtomicSettingsManager,
    CorruptSettings,
    UnsafeSettingsPath,
    atomic_write_json,
    load_json_object,
)

try:
    import fcntl
    if not all(hasattr(fcntl, name) for name in ("flock", "LOCK_EX", "LOCK_UN")):
        fcntl = None
except ImportError:  # pragma: no cover - available on the Linux target
    fcntl = None

try:
    import decky
except ImportError:
    # Local fallback when decky isn't importable (e.g., running outside
    # plugin_loader for static analysis or ad-hoc testing). All runtime
    # paths on a Deck have the real module.
    class decky:  # type: ignore
        DECKY_PLUGIN_SETTINGS_DIR = "/tmp/wifi-optimizer"
        DECKY_PLUGIN_DIR = "/tmp/wifi-optimizer"
        DECKY_PLUGIN_VERSION = "0.0.0"
        class logger:
            @staticmethod
            def info(msg): print(f"[INFO] {msg}")
            @staticmethod
            def error(msg): print(f"[ERROR] {msg}")

DISPATCHER_PATH = "/etc/NetworkManager/dispatcher.d/99-wifi-optimizer-go2"
WIFI_BACKEND_CONF = "/etc/NetworkManager/conf.d/99-valve-wifi-backend.conf"
NM_DEFAULT_CONF = "/usr/lib/NetworkManager/conf.d/10-steamos-defaults.conf"
BAZZITE_IWD_CONF = "/etc/NetworkManager/conf.d/iwd.conf"
IWD_MAIN_CONF = "/etc/iwd/main.conf"

BAND_POLICY_OFF = "off"
BAND_POLICY_SIX_ONLY = "six_ghz_only"
BAND_POLICY_HIGH_ONLY = "five_six_no_24"
# Non-zero keeps 2.4 GHz available as a fallback, while making any usable
# 5/6 GHz BSS overwhelmingly preferable in iwd's ranking calculation.
BAND_PREFERENCE_2_4_MODIFIER = "0.01"
BAND_POLICIES = {
    BAND_POLICY_OFF,
    BAND_POLICY_SIX_ONLY,
    BAND_POLICY_HIGH_ONLY,
}
# Long enough for iwd restart (15s), NetworkManager activation (25s), and
# verification (24s), with margin for slower handheld boots.
BAND_POLICY_ROLLBACK_SECONDS = 180
MANUAL_RECONNECT_ROLLBACK_SECONDS = 60
BAND_POLICY_ROLLBACK_UNIT_PREFIX = "wifi-optimizer-go2-band-rollback"

DRIVER_PROFILES = {
    "rtw88": {
        "chip_label": "WiFi 5 (RTL8822CE)",
        "supports_6ghz": False,
    },
    "ath11k_pci": {
        "chip_label": "WiFi 6E (QCA206X)",
        "supports_6ghz": True,
    },
    "mt7921e": {
        "chip_label": "WiFi 6E (MT7922)",
        "supports_6ghz": True,
    },
    "iwlwifi": {
        "chip_label": "Intel WiFi",
        "supports_6ghz": True,
    },
}

DMI_DEVICES = {
    "Jupiter": {"family": "deck_lcd", "label": "Steam Deck LCD"},
    "Galileo": {"family": "deck_oled", "label": "Steam Deck OLED"},
    "83E1": {"family": "legion_go", "label": "Legion Go"},
    "83L3": {"family": "legion_go_s", "label": "Legion Go S"},
    "83N6": {"family": "legion_go_s", "label": "Legion Go S"},
    "83Q2": {"family": "legion_go_s", "label": "Legion Go S"},
    "83Q3": {"family": "legion_go_s", "label": "Legion Go S"},
    "83N0": {"family": "legion_go_2", "label": "Legion Go 2"},
    "83N1": {"family": "legion_go_2", "label": "Legion Go 2"},
}

DMI_SUBSTRING_DEVICES = [
    ("ROG Xbox Ally X RC73X", {"family": "rog_xbox_ally_x", "label": "ROG Xbox Ally X"}),
    ("ROG Xbox Ally RC73Y", {"family": "rog_xbox_ally", "label": "ROG Xbox Ally"}),
    ("ROG Ally X RC72LA", {"family": "rog_ally_x", "label": "ROG Ally X"}),
    ("ROG Ally RC71L", {"family": "rog_ally", "label": "ROG Ally"}),
]

try:
    SETTINGS_FILE = os.path.join(
        decky.DECKY_PLUGIN_SETTINGS_DIR, "wifi_settings.json"
    )
    ENFORCED_FILE = os.path.join(
        decky.DECKY_PLUGIN_SETTINGS_DIR, "wifi_last_enforced"
    )
except Exception:
    SETTINGS_FILE = "/tmp/legiongo2companion/wifi_settings.json"
    ENFORCED_FILE = "/tmp/legiongo2companion/wifi_last_enforced"

BAND_POLICY_RUNTIME_DIR = "/run/wifi-optimizer-go2"
BAND_POLICY_STATE_DIR = "/var/lib/legiongo2companion-wifi"
BAND_POLICY_JOURNAL_FILE = os.path.join(
    BAND_POLICY_STATE_DIR, "band-policy-transaction.json"
)
POWER_SAVE_JOURNAL_FILE = os.path.join(
    BAND_POLICY_STATE_DIR, "power-save-transaction.json"
)
BAND_POLICY_LOCK_FILE = os.path.join(BAND_POLICY_RUNTIME_DIR, "band-policy.lock")
DECKY_PLUGIN_DIR = getattr(decky, "DECKY_PLUGIN_DIR", os.path.dirname(__file__))
HEALTH_MARKER_FILE = os.path.join(DECKY_PLUGIN_DIR, ".wifi-backend-ready")

DEFAULT_SETTINGS = {
    "model": "unknown",
    "driver": "unknown",
    "device_family": "unknown",
    "device_label": "Unknown Device",
    "chip_label": "unknown",
    "supports_6ghz": False,
    "power_save_disabled": False,
    "power_save_connection_uuid": "",
    "power_save_previous": "",
    "power_save_runtime_previous": "",
    "power_save_legacy_detected": False,
    "auto_fix_on_wake": False,
    "bssid_lock_enabled": False,
    "bssid_lock_value": "",
    "bssid_lock_connection_uuid": "",
    "band_policy": BAND_POLICY_OFF,
    "band_policy_state": {},
    "band_policy_legacy_detected": False,
    "band_policy_legacy_band": "",
    "band_policy_legacy_connection_uuid": "",
    "band_preference": "5_6",
    "band_preference_enabled": False,
    "last_connection_uuid": "",
    "distro_id": "unknown",
    "distro_name": "Unknown",
    "last_applied": 0,
}


_SETTINGS_MANAGER = None
_SETTINGS_MANAGER_PATH = ""
_SETTINGS_MANAGER_LOCK = threading.Lock()


def _get_settings_manager() -> AtomicSettingsManager:
    """Return a store bound to the current settings path.

    Keeping this path-aware is useful for both Decky's runtime directory and
    the isolated paths used by the recovery tests.
    """
    global _SETTINGS_MANAGER, _SETTINGS_MANAGER_PATH
    path = os.path.abspath(SETTINGS_FILE)
    with _SETTINGS_MANAGER_LOCK:
        if _SETTINGS_MANAGER is None or _SETTINGS_MANAGER_PATH != path:
            filename = os.path.basename(path)
            name, extension = os.path.splitext(filename)
            if extension.lower() != ".json" or not name:
                raise UnsafeSettingsPath(
                    "settings file must have a simple .json filename"
                )
            _SETTINGS_MANAGER = AtomicSettingsManager(
                name, os.path.dirname(path)
            )
            _SETTINGS_MANAGER_PATH = path
        return _SETTINGS_MANAGER


def _load_settings() -> dict:
    manager = _get_settings_manager()
    manager.read()
    if manager.recovery_error:
        raise CorruptSettings(
            "WiFi settings recovery failed; the original network ownership "
            "cannot be determined. Existing network settings were left untouched. "
            + manager.recovery_error
        )
    original = copy.deepcopy(manager.settings)
    data = copy.deepcopy(original)

    # v0.11.x represented the 5 GHz lock as two unrelated fields.  Do not
    # silently map that to the new 5/6 GHz policy: doing so would expand a
    # per-profile lock into a system-wide iwd rule.  Instead remember the
    # legacy ownership and require one explicit `off` operation before a new
    # policy can be selected.
    if "band_policy" not in data and data.get("band_preference_enabled"):
        data["band_policy"] = BAND_POLICY_OFF
        data["band_policy_legacy_detected"] = True
        data["band_policy_legacy_band"] = str(
            data.get("band_preference") or "a"
        )
        data["band_policy_legacy_connection_uuid"] = str(
            data.get("last_connection_uuid")
            or data.get("bssid_lock_connection_uuid")
            or ""
        )
    # The public control is a single preference switch. Keep its boolean
    # representation synchronized with the internal transactional state.
    data["band_preference"] = "5_6"
    data["band_preference_enabled"] = (
        data.get("band_policy") == BAND_POLICY_HIGH_ONLY
        and not data.get("band_policy_legacy_detected")
    )
    # Older releases enabled power-save suppression globally and did not
    # record either the owning profile or its previous value.  There is no
    # safe value to infer here, so preserve an explicit recovery marker and
    # refuse to mutate anything until the upstream plugin is reset/uninstalled.
    if data.get("power_save_disabled") and (
        not data.get("power_save_connection_uuid")
        or str(data.get("power_save_previous") or "") == ""
    ):
        data["power_save_legacy_detected"] = True

    merged = {**DEFAULT_SETTINGS, **data}
    normalized = {k: v for k, v in merged.items() if k in DEFAULT_SETTINGS}
    # Persist migrations and remove stale fields once, rather than rewriting
    # settings on every status poll.
    if original and normalized != original:
        manager.replace(normalized)
    return normalized


def _save_settings(data: dict):
    _get_settings_manager().replace(data)


def _save_settings_with_timestamp(data: dict):
    """Save settings and update last_applied timestamp in one write."""
    data["last_applied"] = int(time.time())
    _save_settings(data)


class Plugin:
    """Root plugin instance. Decky exposes every async method here as a
    callable from the frontend. Synchronous helpers prefixed with `_` are
    for internal use only."""

    # ---- Helpers ----

    def _run_cmd(self, cmd: list[str], timeout: int = 5, clean_env: bool = False) -> dict:
        """Run a subprocess and return a result dict.

        clean_env strips LD_LIBRARY_PATH so children use system libraries
        instead of Decky's PyInstaller-bundled ones. Required for curl
        (OpenSSL mismatch) and bash (readline symbol mismatch); without it,
        those binaries fail with cryptic symbol-lookup errors.
        """
        try:
            env = None
            if clean_env:
                env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, env=env
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "stdout": "",
                "stderr": "Command timed out",
                "returncode": -1,
            }
        except FileNotFoundError:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Command not found: {cmd[0]}",
                "returncode": -1,
            }
        except Exception as e:
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "returncode": -1,
            }

    # ---- Band policy helpers -------------------------------------------------

    def _get_network_mutation_lock(self) -> asyncio.Lock:
        """Serialize profile, iwd, and their shared settings ownership state."""
        lock = getattr(self, "_network_mutation_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._network_mutation_lock = lock
        return lock

    def _get_band_policy_lock(self) -> asyncio.Lock:
        """Return the process-local lock serializing all band mutations."""
        return self._get_network_mutation_lock()

    def _get_band_policy_file_gate(self) -> asyncio.Lock:
        """Allow only one local caller to wait for the cross-process flock."""
        lock = getattr(self, "_band_policy_file_gate", None)
        if lock is None:
            lock = asyncio.Lock()
            self._band_policy_file_gate = lock
        return lock

    @asynccontextmanager
    async def _network_lock_context(self, already_held: bool = False):
        if already_held:
            yield
            return
        async with self._get_network_mutation_lock():
            yield

    async def _worker_complete(self, fn, *args, cancelled_cleanup=None):
        worker = asyncio.create_task(asyncio.to_thread(fn, *args))
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
        try:
            result = worker.result()
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError
            raise
        if cancelled:
            if cancelled_cleanup is not None:
                cancelled_cleanup(result)
            raise asyncio.CancelledError
        return result

    def _acquire_band_policy_file_lock(self):
        """Fence the worker, startup recovery, and systemd watchdog."""
        os.makedirs(BAND_POLICY_RUNTIME_DIR, mode=0o700, exist_ok=True)
        os.chmod(BAND_POLICY_RUNTIME_DIR, 0o700)
        handle = open(BAND_POLICY_LOCK_FILE, "a+", encoding="utf-8")
        os.chmod(BAND_POLICY_LOCK_FILE, 0o600)
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    @staticmethod
    def _release_band_policy_file_lock(handle):
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _atomic_write_text(self, path: str, text: str, mode: int = 0o644):
        """Atomically replace a regular file without following path symlinks."""
        encoded = text.encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError("refusing to write a file larger than 1 MiB")

        directory = os.path.abspath(os.path.dirname(path))
        leaf = os.path.basename(path)
        if not leaf or leaf in (".", ".."):
            raise ValueError("target must have a safe filename")
        if not os.path.lexists(directory):
            os.makedirs(directory, mode=0o755, exist_ok=True)
        directory_stat = os.lstat(directory)
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(
            directory_stat.st_mode
        ):
            raise UnsafeSettingsPath(
                f"target directory is not a real directory: {directory}"
            )

        temporary = f".{leaf}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

        if os.name != "posix":
            target = os.path.join(directory, leaf)
            temporary_path = os.path.join(directory, temporary)
            try:
                try:
                    old_stat = os.lstat(target)
                except FileNotFoundError:
                    old_stat = None
                if old_stat is not None:
                    if stat.S_ISLNK(old_stat.st_mode) or not stat.S_ISREG(
                        old_stat.st_mode
                    ):
                        raise UnsafeSettingsPath(
                            f"target path is not a regular file: {target}"
                        )
                    mode = old_stat.st_mode & 0o777
                fd = os.open(temporary_path, flags, mode)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, target)
            finally:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass
            return

        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        dir_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        dir_fd = os.open(directory, dir_flags)
        tmp_fd = -1
        try:
            try:
                old_stat = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                old_stat = None
            if old_stat is not None:
                if not stat.S_ISREG(old_stat.st_mode):
                    raise UnsafeSettingsPath(
                        f"target path is not a regular file: {path}"
                    )
                mode = old_stat.st_mode & 0o777

            tmp_fd = os.open(temporary, flags, mode, dir_fd=dir_fd)
            os.fchmod(tmp_fd, mode)
            if old_stat is not None and hasattr(os, "fchown"):
                try:
                    os.fchown(tmp_fd, old_stat.st_uid, old_stat.st_gid)
                except PermissionError:
                    pass
            with os.fdopen(tmp_fd, "wb") as handle:
                tmp_fd = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
        finally:
            if tmp_fd >= 0:
                os.close(tmp_fd)
            try:
                os.unlink(temporary, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            os.close(dir_fd)

    def _atomic_write_json(self, path: str, data: dict):
        self._atomic_write_text(path, json.dumps(data, indent=2) + "\n", 0o600)

    @staticmethod
    def _read_bounded_regular_text(path: str, limit: int = 1024 * 1024) -> str:
        """Read a small regular file without following its final symlink."""
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise UnsafeSettingsPath(f"path is not a regular file: {path}")
        if info.st_size > limit:
            raise ValueError(f"file exceeds the {limit}-byte safety limit")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > limit:
                raise UnsafeSettingsPath(f"opened path is unsafe: {path}")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                return handle.read(limit + 1)
        finally:
            if fd >= 0:
                os.close(fd)

    def _nmcli_get(self, uuid: str, key: str, timeout: int = 4) -> tuple[str, dict]:
        result = self._run_cmd(
            [
                "/usr/bin/nmcli",
                "-g",
                key,
                "con",
                "show",
                "uuid",
                uuid,
            ],
            timeout=timeout,
        )
        value = result.get("stdout", "").strip()
        if value == "--":
            value = ""
        return value, result

    @staticmethod
    def _parse_version(value: str) -> tuple[int, ...]:
        match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", value or "")
        if not match:
            return ()
        return tuple(int(part or 0) for part in match.groups())

    @staticmethod
    def _nmcli_unescape(value: str) -> str:
        return re.sub(r"\\(.)", r"\1", value)

    @staticmethod
    def _normalize_bssid(value: str | None) -> str:
        # `nmcli -g` escapes the colons in a MAC address by default
        # (AA\:BB\:...), while `iw` and our journal use plain colons.
        return Plugin._nmcli_unescape(str(value or "")).strip().lower()

    def _get_iwd_rank_modifier_snapshot(self) -> dict:
        """Read only the iwd key owned by this feature.

        The complete file is deliberately not stored or restored: another
        service or the user may update unrelated iwd settings while a policy
        is active.
        """
        if os.path.islink(IWD_MAIN_CONF):
            return {
                "file_exists": True,
                "empty_file_content": None,
                "rank_section_present": False,
                "rank_section_count": 0,
                "modifier_present": False,
                "modifier_count": 0,
                "modifier_value": "",
                "ambiguous": True,
                "unsafe_symlink": True,
                "five_modifier_present": False,
                "five_modifier_count": 0,
                "five_modifier_value": "",
                "six_modifier_present": False,
                "six_modifier_count": 0,
                "six_modifier_value": "",
            }
        try:
            text = self._read_bounded_regular_text(IWD_MAIN_CONF)
            exists = True
        except FileNotFoundError:
            text = ""
            exists = False
        except (OSError, UnicodeError, ValueError, UnsafeSettingsPath):
            return {
                "file_exists": True,
                "empty_file_content": None,
                "rank_section_present": False,
                "rank_section_count": 0,
                "modifier_present": False,
                "modifier_count": 0,
                "modifier_value": "",
                "ambiguous": True,
                "unsafe_symlink": True,
                "five_modifier_present": False,
                "five_modifier_count": 0,
                "five_modifier_value": "",
                "six_modifier_present": False,
                "six_modifier_count": 0,
                "six_modifier_value": "",
            }

        in_rank = False
        rank_count = 0
        modifier_count = 0
        modifier_value = ""
        five_modifier_count = 0
        five_modifier_value = ""
        six_modifier_count = 0
        six_modifier_value = ""
        section_re = re.compile(r"^\s*\[([^]]+)]\s*(?:[#;].*)?$")
        key_24_re = re.compile(
            r"^\s*BandModifier2_4GHz\s*=\s*([^#;]*?)\s*(?:[#;].*)?$",
            re.IGNORECASE,
        )
        key_5_re = re.compile(
            r"^\s*BandModifier5GHz\s*=\s*([^#;]*?)\s*(?:[#;].*)?$",
            re.IGNORECASE,
        )
        key_6_re = re.compile(
            r"^\s*BandModifier6GHz\s*=\s*([^#;]*?)\s*(?:[#;].*)?$",
            re.IGNORECASE,
        )
        for raw_line in text.splitlines():
            section_match = section_re.match(raw_line)
            if section_match:
                in_rank = section_match.group(1).strip().lower() == "rank"
                if in_rank:
                    rank_count += 1
                continue
            if in_rank:
                key_24_match = key_24_re.match(raw_line)
                if key_24_match:
                    modifier_count += 1
                    if modifier_count == 1:
                        modifier_value = key_24_match.group(1).strip()
                key_5_match = key_5_re.match(raw_line)
                if key_5_match:
                    five_modifier_count += 1
                    if five_modifier_count == 1:
                        five_modifier_value = key_5_match.group(1).strip()
                key_6_match = key_6_re.match(raw_line)
                if key_6_match:
                    six_modifier_count += 1
                    if six_modifier_count == 1:
                        six_modifier_value = key_6_match.group(1).strip()

        return {
            "file_exists": exists,
            # Retain only an otherwise empty file's bytes.  This is enough to
            # distinguish "absent" from "present but empty" without snapshotting
            # unrelated iwd configuration.
            "empty_file_content": text if exists and not text.strip() else None,
            "rank_section_present": rank_count > 0,
            "rank_section_count": rank_count,
            "modifier_present": modifier_count > 0,
            "modifier_count": modifier_count,
            "modifier_value": modifier_value,
            "ambiguous": rank_count > 1 or modifier_count > 1,
            # Read-only safety input: this feature never writes or owns the
            # 5 GHz modifier, but iwd 3.12 refuses a Rank configuration that
            # disables both 2.4 and 5 GHz.
            "five_modifier_present": five_modifier_count > 0,
            "five_modifier_count": five_modifier_count,
            "five_modifier_value": five_modifier_value,
            "six_modifier_present": six_modifier_count > 0,
            "six_modifier_count": six_modifier_count,
            "six_modifier_value": six_modifier_value,
        }

    @staticmethod
    def _iwd_five_ghz_modifier_error(snapshot: dict) -> str:
        count = int(snapshot.get("five_modifier_count") or 0)
        if count > 1:
            return "The iwd config contains duplicate BandModifier5GHz keys."
        if not snapshot.get("five_modifier_present"):
            return ""
        raw_value = str(snapshot.get("five_modifier_value") or "").strip()
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return "The iwd BandModifier5GHz value is not numeric."
        if not math.isfinite(value) or value <= 0:
            return (
                "BandModifier5GHz must remain above zero before high-band "
                "preference can be enabled."
            )
        return ""

    @staticmethod
    def _iwd_six_ghz_modifier_error(snapshot: dict) -> str:
        count = int(snapshot.get("six_modifier_count") or 0)
        if count > 1:
            return "The iwd config contains duplicate BandModifier6GHz keys."
        if not snapshot.get("six_modifier_present"):
            return ""
        raw_value = str(snapshot.get("six_modifier_value") or "").strip()
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return "The iwd BandModifier6GHz value is not numeric."
        if not math.isfinite(value) or value <= 0:
            return (
                "BandModifier6GHz must remain above zero before high-band "
                "preference can be enabled."
            )
        return ""

    def _set_iwd_rank_modifier(
        self, value: str | None, cleanup_created_rank: bool = False
    ):
        """Set/remove `[Rank] BandModifier2_4GHz` without rewriting INI data."""
        if os.path.islink(IWD_MAIN_CONF):
            raise ValueError("Refusing to modify a symlinked iwd main.conf")
        try:
            original = self._read_bounded_regular_text(IWD_MAIN_CONF)
            file_existed = True
        except FileNotFoundError:
            original = ""
            file_existed = False

        current_snapshot = self._get_iwd_rank_modifier_snapshot()
        if current_snapshot.get("ambiguous"):
            raise ValueError(
                "Duplicate [Rank] sections or BandModifier2_4GHz keys in iwd config"
            )

        newline = "\r\n" if "\r\n" in original else "\n"
        lines = original.splitlines(keepends=True)
        section_re = re.compile(r"^\s*\[([^]]+)]\s*(?:[#;].*)?(?:\r?\n)?$")
        key_re = re.compile(
            r"^(\s*)BandModifier2_4GHz\s*=.*?(\r?\n)?$", re.IGNORECASE
        )

        rank_start = None
        rank_end = len(lines)
        key_index = None
        for index, raw_line in enumerate(lines):
            section_match = section_re.match(raw_line)
            if section_match:
                if rank_start is not None:
                    rank_end = index
                    break
                if section_match.group(1).strip().lower() == "rank":
                    rank_start = index
                continue
            if rank_start is not None and key_index is None and key_re.match(raw_line):
                key_index = index

        if key_index is not None:
            if value is None:
                del lines[key_index]
                if key_index < rank_end:
                    rank_end -= 1
            else:
                match = key_re.match(lines[key_index])
                indent = match.group(1) if match else ""
                line_ending = match.group(2) if match and match.group(2) else newline
                lines[key_index] = f"{indent}BandModifier2_4GHz={value}{line_ending}"
        elif value is not None:
            if rank_start is None:
                if lines and not lines[-1].endswith(("\n", "\r")):
                    lines[-1] += newline
                if lines and lines[-1].strip():
                    lines.append(newline)
                lines.extend(
                    [f"[Rank]{newline}", f"BandModifier2_4GHz={value}{newline}"]
                )
                rank_start = len(lines) - 2
                rank_end = len(lines)
            else:
                insert_at = rank_end
                lines.insert(insert_at, f"BandModifier2_4GHz={value}{newline}")

        if value is None and cleanup_created_rank and rank_start is not None:
            # Remove a section header created by us only if it contains no
            # non-comment settings.  User comments are retained.
            rank_end = len(lines)
            for index in range(rank_start + 1, len(lines)):
                if section_re.match(lines[index]):
                    rank_end = index
                    break
            meaningful = [
                line
                for line in lines[rank_start + 1 : rank_end]
                if line.strip() and not line.lstrip().startswith(("#", ";"))
            ]
            comments = [
                line
                for line in lines[rank_start + 1 : rank_end]
                if line.strip() and line.lstrip().startswith(("#", ";"))
            ]
            if not meaningful and not comments:
                del lines[rank_start:rank_end]

        updated = "".join(lines)
        if updated == original:
            return
        if not file_existed and not updated.strip():
            return
        if not updated.strip():
            try:
                os.remove(IWD_MAIN_CONF)
            except FileNotFoundError:
                pass
            return
        self._atomic_write_text(IWD_MAIN_CONF, updated)

    def _restore_iwd_rank_modifier(self, snapshot: dict):
        before_present = bool(snapshot.get("modifier_present"))
        before_value = str(snapshot.get("modifier_value") or "")
        self._set_iwd_rank_modifier(
            before_value if before_present else None,
            cleanup_created_rank=not bool(snapshot.get("rank_section_present")),
        )
        if snapshot.get("file_exists"):
            empty_content = snapshot.get("empty_file_content")
            if empty_content is not None and not os.path.exists(IWD_MAIN_CONF):
                self._atomic_write_text(IWD_MAIN_CONF, str(empty_content))
        else:
            current = self._get_iwd_rank_modifier_snapshot()
            if not current.get("modifier_present"):
                try:
                    remaining = self._read_bounded_regular_text(IWD_MAIN_CONF)
                    if not remaining.strip():
                        os.remove(IWD_MAIN_CONF)
                except FileNotFoundError:
                    pass

    def _get_link_frequency(self, iface: str | None = None) -> int | None:
        iface = iface or self._get_wifi_interface()
        if not iface:
            return None
        result = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"], timeout=3)
        for line in result.get("stdout", "").splitlines():
            match = re.match(r"\s*freq:\s*(\d+)", line)
            if match:
                return int(match.group(1))
        return None

    def _get_link_bssid(self, iface: str | None = None) -> str:
        iface = iface or self._get_wifi_interface()
        if not iface:
            return ""
        result = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"], timeout=3)
        match = re.search(
            r"^Connected to\s+([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b",
            result.get("stdout", ""),
            re.IGNORECASE | re.MULTILINE,
        )
        return match.group(1).lower() if match else ""

    def _get_enabled_high_band_frequencies(self) -> tuple[list[int], list[int]]:
        result = self._run_cmd(["/usr/bin/iw", "list"], timeout=6)
        frequencies: set[int] = set()
        for line in result.get("stdout", "").splitlines():
            if "disabled" in line.lower():
                continue
            match = re.search(r"\*\s*(\d+(?:\.\d+)?)\s+MHz", line)
            if match:
                frequencies.add(int(float(match.group(1))))
        return (
            sorted(freq for freq in frequencies if 4900 <= freq < 5925),
            sorted(freq for freq in frequencies if 5925 <= freq < 7125),
        )

    def _scan_high_band_candidates_for_active_ssid(
        self,
        iface: str | None,
        uuid: str | None,
        five_ghz_frequencies: list[int] | None = None,
        six_ghz_frequencies: list[int] | None = None,
    ) -> dict:
        """Return fresh, exact BSS candidates without changing the connection.

        The manual action needs a BSSID, not merely a band count.  Stop after
        the first successful scan that finds a high-band BSS so a normal case
        performs one scan instead of the previous broad + per-band + DFS loop.
        """
        result = {
            "scan_ok": False,
            "scan_source": "",
            "ssid": "",
            "candidates": [],
        }
        if not iface or not uuid:
            return result
        ssid, ssid_result = self._nmcli_get(uuid, "802-11-wireless.ssid")
        if not ssid_result.get("success") or not ssid:
            return result
        ssid = self._nmcli_unescape(ssid)
        result["ssid"] = ssid
        candidates: dict[str, dict] = {}
        sources: list[str] = []

        def add_candidate(bssid: str, frequency, signal=None):
            if not re.fullmatch(
                r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", bssid or "", re.IGNORECASE
            ):
                return
            try:
                freq = int(float(str(frequency).strip()))
            except (TypeError, ValueError):
                return
            if not 4900 <= freq < 7125:
                return
            try:
                signal_dbm = float(str(signal).strip())
            except (TypeError, ValueError):
                signal_dbm = -999.0
            normalized = bssid.lower()
            candidate = {
                "bssid": normalized,
                "frequency": freq,
                "signal_dbm": signal_dbm,
            }
            previous = candidates.get(normalized)
            if previous is None or signal_dbm > previous["signal_dbm"]:
                candidates[normalized] = candidate

        def add_iw_scan(command: list[str], source: str) -> bool:
            scan = self._run_cmd(command, timeout=10)
            if not scan.get("success"):
                return False
            result["scan_ok"] = True
            sources.append(source)
            bssid = ""
            frequency = None
            signal = None
            for raw_line in scan.get("stdout", "").splitlines():
                line = raw_line.lstrip()
                bss_match = re.match(
                    r"BSS\s+([0-9a-f]{2}(?::[0-9a-f]{2}){5})(?:\(|\s|$)",
                    line,
                    re.IGNORECASE,
                )
                if bss_match:
                    bssid = bss_match.group(1)
                    frequency = None
                    signal = None
                elif line.startswith("freq:"):
                    frequency = line.split(":", 1)[1].strip()
                elif line.startswith("signal:"):
                    signal_match = re.match(
                        r"signal:\s*(-?\d+(?:\.\d+)?)", line, re.IGNORECASE
                    )
                    signal = signal_match.group(1) if signal_match else None
                elif line.startswith("SSID:"):
                    visible_ssid = line.split(":", 1)[1].strip()
                    if visible_ssid == ssid:
                        add_candidate(bssid, frequency, signal)
            return True

        broad_ok = add_iw_scan(
            ["/usr/bin/iw", "dev", iface, "scan", "ssid", ssid],
            "iw-directed",
        )
        if not candidates:
            high_frequencies = sorted(
                set(five_ghz_frequencies or []) | set(six_ghz_frequencies or [])
            )
            if high_frequencies:
                add_iw_scan(
                    [
                        "/usr/bin/iw",
                        "dev",
                        iface,
                        "scan",
                        "freq",
                        *[str(freq) for freq in high_frequencies],
                        "ssid",
                        ssid,
                    ],
                    "iw-directed-high-band",
                )

        # Portable fallback.  Do not ask NetworkManager for another active
        # scan if nl80211 already completed one; merge only its current view.
        if not candidates:
            nm_scan = self._run_cmd(
                [
                    "/usr/bin/nmcli",
                    "-t",
                    "--escape",
                    "yes",
                    "--separator",
                    "\t",
                    "-f",
                    "SSID,BSSID,FREQ,SIGNAL",
                    "dev",
                    "wifi",
                    "list",
                    "ifname",
                    iface,
                    "--rescan",
                    "no" if broad_ok else "yes",
                ],
                timeout=5 if broad_ok else 10,
            )
            if nm_scan.get("success"):
                result["scan_ok"] = True
                sources.append("nmcli")
                for line in nm_scan.get("stdout", "").splitlines():
                    fields = line.split("\t")
                    if len(fields) != 4:
                        continue
                    raw_ssid, bssid, frequency, signal = fields
                    if self._nmcli_unescape(raw_ssid) == ssid:
                        add_candidate(bssid, frequency, signal)

        result["candidates"] = sorted(
            candidates.values(),
            key=lambda item: (
                item["signal_dbm"],
                1 if item["frequency"] >= 5925 else 0,
            ),
            reverse=True,
        )
        result["scan_source"] = "+".join(sources)
        return result

    def _get_visible_bands_for_active_ssid(
        self,
        iface: str | None,
        uuid: str | None,
        rescan: bool = True,
        five_ghz_frequencies: list[int] | None = None,
        six_ghz_frequencies: list[int] | None = None,
        allow_recent_cache: bool = True,
    ) -> dict:
        bands = {
            "two_ghz": 0,
            "five_ghz": 0,
            "six_ghz": 0,
            "scan_ok": False,
            "scan_source": "",
            "fresh_high_band": False,
        }
        if not iface or not uuid:
            return bands
        ssid, ssid_result = self._nmcli_get(uuid, "802-11-wireless.ssid")
        if not ssid_result.get("success") or not ssid:
            return bands
        ssid = self._nmcli_unescape(ssid)

        # A recheck in the UI is immediately followed by a separate backend
        # preflight when the user applies a mode. Wireless scans are snapshots
        # and an AP can be omitted from either one, especially on DFS/6 GHz.
        # Keep only a short, connection-scoped copy of a successful high-band
        # observation so the apply call can use the result it just presented.
        cache_ttl_seconds = 60.0
        now = time.monotonic()
        cached = getattr(self, "_visible_band_cache", None)
        cached_bands = None
        if (
            isinstance(cached, dict)
            and cached.get("iface") == iface
            and cached.get("uuid") == uuid
            and cached.get("ssid") == ssid
            and 0 <= now - float(cached.get("checked_at", 0)) <= cache_ttl_seconds
        ):
            cached_bands = cached.get("bands")

        sources = []

        def add_frequency(
            raw_frequency: str | float | int | None, *, fresh: bool = False
        ):
            frequency_match = re.fullmatch(
                r"\s*(\d+(?:\.\d+)?)\s*(?:MHz)?\s*",
                str(raw_frequency or ""),
                re.IGNORECASE,
            )
            if not frequency_match:
                return
            freq = float(frequency_match.group(1))
            if 2400 <= freq < 2500:
                bands["two_ghz"] += 1
            elif 4900 <= freq < 5925:
                bands["five_ghz"] += 1
                if fresh:
                    bands["fresh_high_band"] = True
            elif 5925 <= freq < 7125:
                bands["six_ghz"] += 1
                if fresh:
                    bands["fresh_high_band"] = True

        # NetworkManager's iwd backend may expose one aggregate AP object per
        # SSID. On the Go 2 this can show only the currently associated 2.4 GHz
        # BSS. A passive nl80211 scan was also observed to omit the router's
        # DFS 5 GHz BSS at 5320 MHz. A directed scan for the already-connected
        # SSID exposes its separate 2.4, 5 and 6 GHz BSS records without fixing
        # a BSSID or changing the saved connection profile.
        def add_direct_scan(command: list[str], source: str) -> bool:
            direct_result = self._run_cmd(command, timeout=15)
            if not direct_result.get("success"):
                return False
            bands["scan_ok"] = True
            if source not in sources:
                sources.append(source)
            current_frequency = None
            for raw_line in direct_result.get("stdout", "").splitlines():
                line = raw_line.lstrip()
                if line.startswith("BSS "):
                    current_frequency = None
                elif line.startswith("freq:"):
                    current_frequency = line.split(":", 1)[1].strip()
                elif line.startswith("SSID:"):
                    visible_ssid = line.split(":", 1)[1].strip()
                    if visible_ssid == ssid:
                        add_frequency(current_frequency, fresh=True)
            return True

        direct_scan_ok = False
        if rescan:
            direct_scan_ok = add_direct_scan(
                ["/usr/bin/iw", "dev", iface, "scan", "ssid", ssid],
                "iw-directed",
            )
            # DFS 5 GHz records can be absent from a successful all-band scan.
            # A second scan restricted to the radio's enabled 5 GHz channels
            # reliably exposed the missing BSS on the tested Go 2. Do the same
            # for 6 GHz only when that band was absent from the first result.
            for frequencies_for_band, band_key, source in (
                (five_ghz_frequencies, "five_ghz", "iw-directed-5ghz"),
                (six_ghz_frequencies, "six_ghz", "iw-directed-6ghz"),
            ):
                if bands[band_key] == 0 and frequencies_for_band:
                    direct_scan_ok = (
                        add_direct_scan(
                            [
                                "/usr/bin/iw",
                                "dev",
                                iface,
                                "scan",
                                "freq",
                                *[str(freq) for freq in frequencies_for_band],
                                "ssid",
                                ssid,
                            ],
                            source,
                        )
                        or direct_scan_ok
                    )

            # A many-channel request can still dwell too briefly on DFS. Scan
            # each enabled DFS frequency individually until the SSID is found.
            # On the live router this stops at 5320 MHz (channel 64), normally
            # adding under two seconds rather than another broad scan.
            if bands["five_ghz"] == 0 and five_ghz_frequencies:
                for freq in five_ghz_frequencies:
                    if not 5260 <= freq <= 5720:
                        continue
                    direct_scan_ok = (
                        add_direct_scan(
                            [
                                "/usr/bin/iw",
                                "dev",
                                iface,
                                "scan",
                                "freq",
                                str(freq),
                                "ssid",
                                ssid,
                            ],
                            "iw-directed-5ghz-dfs",
                        )
                        or direct_scan_ok
                    )
                    if bands["five_ghz"] > 0:
                        break

        # Keep nmcli as a portable fallback and merge its cached results. Do
        # not trigger a second active scan when nl80211 already completed one.
        nm_result = self._run_cmd(
            [
                "/usr/bin/nmcli",
                "-t",
                "--escape",
                "yes",
                "-f",
                "SSID,FREQ",
                "dev",
                "wifi",
                "list",
                "ifname",
                iface,
                "--rescan",
                "yes" if rescan and not direct_scan_ok else "no",
            ],
            timeout=12 if rescan and not direct_scan_ok else 4,
        )
        if nm_result.get("success"):
            bands["scan_ok"] = True
            sources.append("nmcli")
            nmcli_result_is_fresh = bool(rescan and not direct_scan_ok)
            for line in nm_result.get("stdout", "").splitlines():
                if ":" not in line:
                    continue
                raw_ssid, raw_freq = line.rsplit(":", 1)
                if self._nmcli_unescape(raw_ssid) == ssid:
                    add_frequency(raw_freq, fresh=nmcli_result_is_fresh)

        fresh_high_band = bands["five_ghz"] > 0 or bands["six_ghz"] > 0
        if bands["scan_ok"] and fresh_high_band:
            self._visible_band_cache = {
                "iface": iface,
                "uuid": uuid,
                "ssid": ssid,
                "checked_at": now,
                "bands": {
                    "two_ghz": bands["two_ghz"],
                    "five_ghz": bands["five_ghz"],
                    "six_ghz": bands["six_ghz"],
                },
            }
        elif allow_recent_cache and isinstance(cached_bands, dict):
            for key in ("two_ghz", "five_ghz", "six_ghz"):
                bands[key] = max(bands[key], int(cached_bands.get(key, 0)))
            sources.append("recent-cache")
        bands["scan_source"] = "+".join(sources)
        return bands

    def _get_band_policy_capabilities_sync(self, rescan: bool = True) -> dict:
        iface = self._get_wifi_interface()
        uuid = self._get_active_connection_uuid()
        backend = self._get_current_backend() or ""
        _, device_family, _ = self._detect_device_family()
        driver = self._detect_wifi_driver()
        target_supported = device_family == "legion_go_2" and driver == "mt7921e"

        nm_version_result = self._run_cmd(
            ["/usr/bin/nmcli", "--version"], timeout=3
        )
        nm_version_text = nm_version_result.get("stdout", "")
        nm_version = self._parse_version(nm_version_text)
        nm_supports_6ghz = bool(nm_version and nm_version >= (1, 58, 0))

        iwd_version_result = self._run_cmd(
            ["/usr/lib/iwd/iwd", "--version"], timeout=3
        )
        iwd_version_text = iwd_version_result.get("stdout", "").strip()
        iwd_service = self._run_cmd(
            ["/usr/bin/systemctl", "is-active", "iwd"], timeout=3
        )
        iwd_active = iwd_service.get("stdout", "").strip() == "active"
        iwd_available = iwd_version_result.get("success", False) or os.path.isfile(
            "/usr/lib/systemd/system/iwd.service"
        )

        iw_list = self._run_cmd(["/usr/bin/iw", "list"], timeout=6)
        frequencies = []
        for line in iw_list.get("stdout", "").splitlines():
            if "disabled" in line.lower():
                continue
            match = re.search(r"\*\s*(\d+(?:\.\d+)?)\s+MHz", line)
            if match:
                frequencies.append(float(match.group(1)))
        has_5ghz = any(4900 <= freq < 5925 for freq in frequencies)
        has_6ghz = any(5925 <= freq < 7125 for freq in frequencies)

        reg_result = self._run_cmd(["/usr/bin/iw", "reg", "get"], timeout=3)
        reg_match = re.search(
            r"^\s*country\s+([A-Z0-9]{2}):", reg_result.get("stdout", ""), re.MULTILINE
        )
        regdom = reg_match.group(1) if reg_match else ""

        visible = self._get_visible_bands_for_active_ssid(
            iface,
            uuid,
            rescan and target_supported,
            five_ghz_frequencies=sorted(
                {int(freq) for freq in frequencies if 4900 <= freq < 5925}
            ),
            six_ghz_frequencies=sorted(
                {int(freq) for freq in frequencies if 5925 <= freq < 7125}
            ),
        )
        six_visible = visible["six_ghz"] > 0
        high_visible = visible["five_ghz"] > 0 or six_visible

        band = ""
        channel = ""
        bssid = ""
        if uuid:
            band, _ = self._nmcli_get(uuid, "802-11-wireless.band")
            channel, _ = self._nmcli_get(uuid, "802-11-wireless.channel")
            bssid, _ = self._nmcli_get(uuid, "802-11-wireless.bssid")

        settings = _load_settings()
        current_policy = settings.get("band_policy", BAND_POLICY_OFF)
        policy_state = settings.get("band_policy_state") or {}
        applied_state = policy_state.get("applied") or {}
        legacy = bool(settings.get("band_policy_legacy_detected"))
        iwd_snapshot = self._get_iwd_rank_modifier_snapshot()
        five_modifier_error = self._iwd_five_ghz_modifier_error(iwd_snapshot)
        six_modifier_error = self._iwd_six_ghz_modifier_error(iwd_snapshot)
        external_band_while_off = (
            current_policy == BAND_POLICY_OFF and bool(band) and not legacy
        )
        external_iwd_while_off = (
            current_policy == BAND_POLICY_OFF
            and iwd_snapshot.get("modifier_present")
        )
        external_band_during_high = (
            current_policy == BAND_POLICY_HIGH_ONLY
            and bool(band)
        )
        expected_six_iwd = {
            "modifier_present": applied_state.get("iwd_modifier_present", False),
            "modifier_value": applied_state.get("iwd_modifier_value", ""),
        }
        external_iwd_during_six = (
            current_policy == BAND_POLICY_SIX_ONLY
            and not self._iwd_snapshot_matches(iwd_snapshot, expected_six_iwd)
        )
        fixed_channel = channel not in ("", "0")
        fixed_bssid = bool(bssid)
        watchdog_available = os.path.isfile("/usr/bin/systemd-run") and os.path.isfile(
            "/usr/bin/python3"
        )

        common_reason = ""
        if not iface or not uuid:
            common_reason = "Connect to WiFi first."
        elif not target_supported:
            common_reason = (
                "Band changes are enabled only on a Legion Go 2 with the "
                "MediaTek MT7922 (mt7921e) driver."
            )
        elif legacy:
            common_reason = "Disable the migrated legacy band preference first."
        elif fixed_bssid:
            common_reason = "Clear the fixed BSSID before changing band policy."
        elif fixed_channel:
            common_reason = "Clear the fixed WiFi channel before changing band policy."
        elif iwd_snapshot.get("unsafe_symlink"):
            common_reason = "Refusing a symlinked /etc/iwd/main.conf."
        elif iwd_snapshot.get("ambiguous"):
            common_reason = (
                "The iwd config contains duplicate [Rank] sections or band keys."
            )
        elif not watchdog_available:
            common_reason = "The automatic rollback watchdog is unavailable."

        reason_six = common_reason
        if not reason_six and backend != "iwd":
            reason_six = "The Go 2 band policy requires the iwd WiFi backend."
        elif not reason_six and not iwd_active:
            reason_six = "The iwd service is not active."
        elif not reason_six and not nm_supports_6ghz:
            reason_six = "NetworkManager 1.58 or newer is required for 6 GHz only."
        elif not reason_six and not has_6ghz:
            reason_six = "No usable 6 GHz channels are exposed by the radio and regdomain."
        elif not reason_six and six_modifier_error:
            reason_six = six_modifier_error
        elif not reason_six and (external_band_while_off or external_band_during_high):
            reason_six = "The active profile already has an external band setting."
        elif not reason_six and external_iwd_while_off:
            reason_six = "An external iwd band policy is already configured."
        elif not reason_six and not six_visible:
            reason_six = "No 6 GHz BSS for the active SSID is currently visible."

        reason_high = common_reason
        if not reason_high and backend != "iwd":
            reason_high = "The 5/6 GHz policy requires the iwd WiFi backend."
        elif not reason_high and not iwd_available:
            reason_high = "iwd is not installed."
        elif not reason_high and not iwd_active:
            reason_high = "The iwd service is not active."
        elif not reason_high and not (has_5ghz or has_6ghz):
            reason_high = "No usable 5 or 6 GHz channels are exposed by the radio."
        elif not reason_high and five_modifier_error:
            reason_high = five_modifier_error
        elif not reason_high and six_modifier_error:
            reason_high = six_modifier_error
        elif not reason_high and external_band_while_off:
            reason_high = "The active profile already has an external band setting."
        elif not reason_high and (
            external_iwd_while_off or external_iwd_during_six
        ):
            reason_high = "An external iwd band policy is already configured."
        elif not reason_high and not high_visible:
            reason_high = "No 5 or 6 GHz BSS for the active SSID is currently visible."

        return {
            "success": True,
            "six_ghz_only_available": not bool(reason_six),
            "five_six_no_24_available": not bool(reason_high),
            "two_ghz_bss_visible": visible["two_ghz"] > 0,
            "five_ghz_bss_visible": visible["five_ghz"] > 0,
            "six_ghz_bss_visible": six_visible,
            "high_band_bss_visible": high_visible,
            "visible_band_scan_source": visible.get("scan_source", ""),
            "nm_supports_6ghz": nm_supports_6ghz,
            "iwd_available": iwd_available,
            "iwd_active": iwd_active,
            "has_5ghz": has_5ghz,
            "has_6ghz": has_6ghz,
            "reason_six_ghz_only": reason_six,
            "reason_five_six_no_24": reason_high,
            "current_backend": backend,
            "nm_version": ".".join(str(part) for part in nm_version),
            "iwd_version": iwd_version_text,
            "regdom": regdom,
            "connected": bool(iface and uuid),
            "rollback_watchdog_available": watchdog_available,
            "target_supported": target_supported,
            "device_family": device_family,
            "driver": driver,
        }

    def _load_band_policy_journal(self, path: str | None = None) -> dict | None:
        target = path or BAND_POLICY_JOURNAL_FILE
        try:
            return load_json_object(target)
        except FileNotFoundError:
            # Upgrade a transaction left by 0.4.1 without losing its rollback
            # data. The shared /run flock also fences an old watchdog process.
            if os.path.dirname(target) == BAND_POLICY_STATE_DIR:
                legacy = os.path.join(BAND_POLICY_RUNTIME_DIR, os.path.basename(target))
                journal = self._load_band_policy_journal(legacy)
                if journal is not None:
                    self._save_band_policy_journal(journal, target)
                    self._remove_band_policy_journal(legacy)
                return journal
            return None
        except (CorruptSettings, UnsafeSettingsPath, OSError) as exc:
            # A present but unreadable recovery journal is materially
            # different from no journal. Return a fail-closed marker so every
            # mutating path remains locked until the file is inspected.
            decky.logger.error(f"Unsafe recovery journal {target}: {exc}")
            return {
                "schema": 0,
                "phase": "unreadable",
                "rollback_conflicts": [str(exc)],
            }

    def _save_band_policy_journal(self, journal: dict, path: str | None = None):
        target = path or BAND_POLICY_JOURNAL_FILE
        directory = os.path.dirname(target)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise UnsafeSettingsPath("Recovery directory is not a real directory")
        if os.name == "posix" and info.st_uid != os.geteuid():
            raise UnsafeSettingsPath("Recovery directory has an unexpected owner")
        os.chmod(directory, 0o700)
        self._atomic_write_json(target, journal)

    def _remove_band_policy_journal(self, path: str | None = None):
        try:
            target = path or BAND_POLICY_JOURNAL_FILE
            os.remove(target)
            if os.name == "posix":
                fd = os.open(os.path.dirname(target), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except FileNotFoundError:
            pass

    def _get_network_recovery_issues(self) -> list[str]:
        """Describe only durable recovery failures, without mutating state."""
        issues = []
        for label, journal in (
            ("band policy", self._load_band_policy_journal()),
            ("power save", self._load_power_save_journal()),
        ):
            if not journal:
                continue
            phase = str(journal.get("phase") or "")
            if phase == "rollback_failed":
                conflicts = journal.get("rollback_conflicts") or []
                detail = "; ".join(str(item) for item in conflicts)
                issues.append(
                    f"{label} rollback failed" + (f": {detail}" if detail else "")
                )
            elif phase not in (
                "pending",
                "applying",
                "rolling_back",
                "verified",
                "committed",
                "rolled_back",
            ):
                issues.append(f"{label} journal has unknown phase: {phase or 'missing'}")
        return issues

    def _write_band_settings_fields(self, path: str, fields: dict):
        """Merge only the supplied owned fields, preserving concurrent settings."""
        if os.path.abspath(path) == os.path.abspath(SETTINGS_FILE):
            data = _load_settings()
        else:
            data = load_json_object(path, missing_ok=True) or {}
        data.update(fields)
        data["last_applied"] = int(time.time())
        if os.path.abspath(path) == os.path.abspath(SETTINGS_FILE):
            _save_settings(data)
        else:
            atomic_write_json(path, data)

    @staticmethod
    def _band_settings_fields(settings: dict) -> dict:
        return {
            "band_policy": settings.get("band_policy", BAND_POLICY_OFF),
            "band_policy_state": settings.get("band_policy_state", {}),
            "band_policy_legacy_detected": bool(
                settings.get("band_policy_legacy_detected")
            ),
            "band_policy_legacy_band": settings.get("band_policy_legacy_band", ""),
            "band_policy_legacy_connection_uuid": settings.get(
                "band_policy_legacy_connection_uuid", ""
            ),
            "band_preference": settings.get("band_preference", "5_6"),
            "band_preference_enabled": bool(
                settings.get("band_preference_enabled")
            ),
        }

    def _schedule_band_policy_rollback(self, journal: dict) -> dict:
        unit = (
            f"{BAND_POLICY_ROLLBACK_UNIT_PREFIX}-{os.getpid()}-"
            f"{int(time.time() * 1000)}"
        )
        journal["rollback_unit"] = unit
        self._save_band_policy_journal(journal)
        main_path = os.path.join(DECKY_PLUGIN_DIR, "wifi_backend.py")
        try:
            rollback_seconds = int(
                journal.get("rollback_delay_seconds", BAND_POLICY_ROLLBACK_SECONDS)
            )
        except (TypeError, ValueError):
            rollback_seconds = BAND_POLICY_ROLLBACK_SECONDS
        rollback_seconds = min(
            BAND_POLICY_ROLLBACK_SECONDS, max(30, rollback_seconds)
        )
        result = self._run_cmd(
            [
                "/usr/bin/systemd-run",
                "--quiet",
                "--collect",
                f"--unit={unit}",
                f"--on-active={rollback_seconds}s",
                "--timer-property=AccuracySec=1s",
                "/usr/bin/python3",
                main_path,
                "--rollback-band-policy",
                BAND_POLICY_JOURNAL_FILE,
            ],
            timeout=8,
            clean_env=True,
        )
        return result

    def _cancel_band_policy_rollback(self, journal: dict):
        unit = str(journal.get("rollback_unit") or "")
        if not unit:
            return
        for suffix in ("timer", "service"):
            self._run_cmd(
                ["/usr/bin/systemctl", "stop", f"{unit}.{suffix}"], timeout=4
            )
        self._run_cmd(
            [
                "/usr/bin/systemctl",
                "reset-failed",
                f"{unit}.service",
            ],
            timeout=4,
        )

    def _profile_is_active(self, uuid: str) -> bool:
        return bool(uuid and self._get_active_connection_uuid() == uuid)

    def _reconnect_profile(
        self, uuid: str, was_active: bool = True, cycle: bool = True
    ) -> dict:
        if not uuid or not was_active:
            return {"success": True, "reconnected": False}

        # Restarting iwd already cycles the device. NetworkManager briefly has
        # no WiFi device at that point and an immediate `con up` can be matched
        # against loopback, producing "No suitable device". Give normal
        # autoconnect a short chance first, then wait for the real WiFi device
        # to become usable and bind any explicit activation to that interface.
        if cycle:
            down_result = self._run_cmd(
                ["/usr/bin/nmcli", "con", "down", "uuid", uuid], timeout=8
            )
            if not down_result.get("success"):
                return {
                    **down_result,
                    "reconnected": False,
                    "connection_active": self._profile_is_active(uuid),
                }

        started = time.monotonic()
        deadline = started + 15.0
        manual_activation_after = started if cycle else started + 3.0
        saw_inactive = not cycle
        last_result = None

        while time.monotonic() < deadline:
            if self._profile_is_active(uuid):
                if not cycle or saw_inactive:
                    return {
                        "success": True,
                        "stdout": "",
                        "stderr": "",
                        "returncode": 0,
                        "reconnected": True,
                        "autoconnected": cycle,
                    }
                time.sleep(0.25)
                continue
            else:
                saw_inactive = True

            if time.monotonic() < manual_activation_after:
                time.sleep(0.25)
                continue

            iface = self._get_wifi_interface()
            if iface:
                state_result = self._run_cmd(
                    [
                        "/usr/bin/nmcli",
                        "-g",
                        "GENERAL.STATE",
                        "device",
                        "show",
                        iface,
                    ],
                    timeout=3,
                )
                state_match = re.match(
                    r"\s*(\d+)", str(state_result.get("stdout") or "")
                )
                state = int(state_match.group(1)) if state_match else None
                # 30 is disconnected and 100 is connected. Intermediate
                # states mean an automatic activation is already underway, so
                # let it finish instead of racing a second activation.
                if state_result.get("success") and state in (30, 100):
                    last_result = self._run_cmd(
                        [
                            "/usr/bin/nmcli",
                            "con",
                            "up",
                            "uuid",
                            uuid,
                            "ifname",
                            iface,
                        ],
                        timeout=25,
                    )
                    if last_result.get("success"):
                        last_result["reconnected"] = True
                        return last_result

            time.sleep(0.25)

        if last_result is None:
            last_result = {
                "success": False,
                "stdout": "",
                "stderr": "WiFi device did not become ready before timeout.",
                "returncode": 1,
            }
        last_result["reconnected"] = self._profile_is_active(uuid)
        return last_result

    def _wait_for_profile_frequency(
        self, uuid: str, timeout: float = 8.0
    ) -> int | None:
        deadline = time.monotonic() + timeout
        last_frequency = None
        while time.monotonic() < deadline:
            if self._profile_is_active(uuid):
                iface = self._get_wifi_interface()
                last_frequency = self._get_link_frequency(iface)
                if last_frequency is not None:
                    return last_frequency
            time.sleep(0.25)
        return last_frequency

    def _rescan_and_reconnect_sync(self) -> dict:
        """Temporarily select one freshly seen 5/6 GHz BSS, then unpin it."""
        if self._load_band_policy_journal() or self._load_power_save_journal():
            return {
                "success": False,
                "error": "network_recovery_required",
                "message": "A previous network operation still needs recovery.",
                "recovery_state_preserved": True,
            }

        settings = _load_settings()
        if settings.get("band_policy") != BAND_POLICY_HIGH_ONLY:
            return {
                "success": False,
                "error": "preference_disabled",
                "message": "Enable Prefer 5/6 GHz before using manual reconnect.",
            }
        ownership_error = self._band_policy_ownership_error(settings)
        if ownership_error:
            return {
                "success": False,
                "error": "ownership_conflict",
                "message": ownership_error,
                "recovery_state_preserved": True,
            }

        _, device_family, _ = self._detect_device_family()
        driver = self._detect_wifi_driver()
        if device_family != "legion_go_2" or driver != "mt7921e":
            return {
                "success": False,
                "error": "unsupported_device",
                "message": (
                    "Manual high-band reconnect is enabled only on a "
                    "Legion Go 2 with the MediaTek MT7922 driver."
                ),
            }
        if self._get_current_backend() != "iwd":
            return {
                "success": False,
                "error": "unsupported_backend",
                "message": "Manual high-band reconnect requires the iwd backend.",
            }
        iwd_service = self._run_cmd(
            ["/usr/bin/systemctl", "is-active", "iwd"], timeout=3
        )
        if iwd_service.get("stdout", "").strip() != "active":
            return {
                "success": False,
                "error": "iwd_inactive",
                "message": "The iwd service is not active.",
            }

        iface = self._get_wifi_interface()
        uuid = self._get_active_connection_uuid()
        if not iface or not uuid:
            return {
                "success": False,
                "error": "no_wifi",
                "message": "Connect to WiFi first, then retry.",
            }
        frequency_before = self._get_link_frequency(iface)
        if frequency_before is not None and 4900 <= frequency_before < 7125:
            return {
                "success": True,
                "reconnected": False,
                "better_band_found": True,
                "frequency_before": frequency_before,
                "frequency": frequency_before,
                "message": "WiFi is already connected on 5/6 GHz.",
            }

        bssid_before, bssid_result = self._nmcli_get(
            uuid, "802-11-wireless.bssid"
        )
        if not bssid_result.get("success"):
            return {
                "success": False,
                "error": "profile_read_failed",
                "message": "The saved WiFi profile could not be read.",
                "detail": bssid_result.get("stderr", ""),
            }
        if bssid_before:
            return {
                "success": False,
                "error": "external_bssid_configuration",
                "message": (
                    "The WiFi profile already has a fixed BSSID. Companion did "
                    "not replace that setting."
                ),
            }

        five_frequencies, six_frequencies = (
            self._get_enabled_high_band_frequencies()
        )
        scan = self._scan_high_band_candidates_for_active_ssid(
            iface,
            uuid,
            five_ghz_frequencies=five_frequencies,
            six_ghz_frequencies=six_frequencies,
        )
        if not scan.get("scan_ok"):
            return {
                "success": False,
                "error": "scan_failed",
                "message": "The WiFi scan did not complete; the connection was not changed.",
                "reconnected": False,
                "frequency_before": frequency_before,
            }
        candidates = scan.get("candidates") or []
        if not candidates:
            return {
                "success": True,
                "reconnected": False,
                "better_band_found": False,
                "frequency_before": frequency_before,
                "frequency": frequency_before,
                "message": "No 5/6 GHz BSS for the current network was found.",
            }
        selected = candidates[0]
        selected_bssid = self._normalize_bssid(selected.get("bssid"))
        selected_frequency = int(selected.get("frequency") or 0)
        settings_before = self._band_settings_fields(settings)
        operation_id = f"manual-{os.getpid()}-{int(time.time() * 1000)}"
        journal = {
            "schema": 1,
            "operation_id": operation_id,
            "operation": "manual_high_band_reconnect",
            "phase": "pending",
            "created_at": int(time.time()),
            "rollback_delay_seconds": MANUAL_RECONNECT_ROLLBACK_SECONDS,
            "settings_file": SETTINGS_FILE,
            "settings_before": settings_before,
            "settings_after": settings_before,
            "mode_before": BAND_POLICY_HIGH_ONLY,
            "mode_target": BAND_POLICY_HIGH_ONLY,
            "profile_before": {
                "uuid": uuid,
                "band": "",
                "bssid": bssid_before,
                "was_active": True,
            },
            "active_uuid_before": uuid,
            "iwd_before": self._get_iwd_rank_modifier_snapshot(),
            "rollback_reconnect_required": True,
            "rollback_reconnect_uuid": uuid,
            "mutations": {
                "nm_bssid": {
                    "planned": True,
                    "expected": selected_bssid,
                    "applied": False,
                }
            },
        }
        self._save_band_policy_journal(journal)
        watchdog = self._schedule_band_policy_rollback(journal)
        if not watchdog.get("success"):
            self._remove_band_policy_journal()
            return {
                "success": False,
                "error": "rollback_watchdog_failed",
                "message": "Automatic rollback could not be armed; no changes were made.",
                "detail": watchdog.get("stderr", ""),
            }

        try:
            journal["phase"] = "applying"
            self._save_band_policy_journal(journal)
            modified = self._nmcli_modify(
                uuid, "802-11-wireless.bssid", selected_bssid, timeout=8
            )
            if not modified.get("success"):
                raise RuntimeError(
                    modified.get("stderr")
                    or "NetworkManager rejected the temporary access-point selection"
                )
            observed_bssid, observed_result = self._nmcli_get(
                uuid, "802-11-wireless.bssid"
            )
            if (
                not observed_result.get("success")
                or self._normalize_bssid(observed_bssid) != selected_bssid
            ):
                raise RuntimeError(
                    "The temporary access-point selection could not be verified"
                )
            journal["mutations"]["nm_bssid"]["applied"] = True
            self._save_band_policy_journal(journal)

            reconnect = self._reconnect_profile(uuid, True, cycle=True)
            if not reconnect.get("success"):
                raise RuntimeError(
                    reconnect.get("stderr")
                    or "WiFi could not connect to the selected 5/6 GHz access point"
                )
            frequency = self._wait_for_profile_frequency(uuid)
            connected_bssid = self._get_link_bssid(iface)
            if not (
                frequency is not None
                and 4900 <= frequency < 7125
                and connected_bssid == selected_bssid
            ):
                raise RuntimeError(
                    "The selected 5/6 GHz access point did not become the active link"
                )

            # The BSSID is needed only to make this one choice deterministic.
            # Clear it while the verified link stays up, preserving roaming and
            # the 2.4 GHz fallback for future connectivity.
            cleared = self._nmcli_modify(
                uuid, "802-11-wireless.bssid", bssid_before, timeout=8
            )
            if not cleared.get("success"):
                raise RuntimeError(
                    cleared.get("stderr")
                    or "The temporary access-point selection could not be cleared"
                )
            final_bssid, final_result = self._nmcli_get(
                uuid, "802-11-wireless.bssid"
            )
            if (
                not final_result.get("success")
                or self._normalize_bssid(final_bssid)
                != self._normalize_bssid(bssid_before)
            ):
                raise RuntimeError(
                    "The saved WiFi profile still contains the temporary BSSID"
                )

            journal["rollback_reconnect_completed"] = True
            journal["phase"] = "verified"
            journal["verified_frequency"] = frequency
            self._save_band_policy_journal(journal)
            journal["phase"] = "committed"
            self._save_band_policy_journal(journal)
            self._cancel_band_policy_rollback(journal)
            self._remove_band_policy_journal()
            return {
                "success": True,
                "reconnected": True,
                "better_band_found": True,
                "attempts": 1,
                "frequency_before": frequency_before,
                "frequency": frequency,
                "selected_frequency": selected_frequency,
                "temporary_bssid_cleared": True,
                "message": "Reconnected on 5/6 GHz; the temporary AP selection was cleared.",
            }
        except Exception as exc:
            decky.logger.error(f"manual high-band reconnect failed: {exc}")
            rollback = self._restore_band_policy_transaction(journal, str(exc))
            self._cancel_band_policy_rollback(journal)
            if rollback.get("success"):
                self._remove_band_policy_journal()
            return {
                "success": False,
                "error": "manual_reconnect_failed",
                "message": str(exc),
                "detail": (
                    "The original WiFi profile was restored."
                    if rollback.get("success")
                    else "Automatic recovery requires attention: "
                    + "; ".join(rollback.get("conflicts", []))
                ),
                "rolled_back": rollback.get("rolled_back", False),
                "rollback_conflicts": rollback.get("conflicts", []),
                "connection_recovered": self._profile_is_active(uuid),
                "frequency_before": frequency_before,
                "frequency": self._get_link_frequency(),
                "attempts": 1,
            }

    async def rescan_and_reconnect(self) -> dict:
        """Public bounded manual recovery action for the high-band preference."""
        async with self._get_band_policy_file_gate():
            try:
                file_lock = await self._worker_complete(
                    self._acquire_band_policy_file_lock,
                    cancelled_cleanup=self._release_band_policy_file_lock
                )
            except Exception as exc:
                return {
                    "success": False,
                    "error": "band_policy_lock_failed",
                    "message": "The network-operation lock is unavailable.",
                    "detail": str(exc),
                }
            try:
                async with self._get_network_mutation_lock():
                    return await self._worker_complete(
                        self._rescan_and_reconnect_sync
                    )
            except Exception as exc:
                decky.logger.error(f"rescan_and_reconnect error: {exc}")
                return self._unexpected_response(exc)
            finally:
                self._release_band_policy_file_lock(file_lock)

    @staticmethod
    def _iwd_snapshot_matches(left: dict, right: dict) -> bool:
        return (
            not bool(left.get("ambiguous"))
            and not bool(right.get("ambiguous"))
            and bool(left.get("modifier_present"))
            == bool(right.get("modifier_present"))
            and str(left.get("modifier_value") or "")
            == str(right.get("modifier_value") or "")
        )

    def _restore_band_policy_transaction(
        self,
        journal: dict,
        reason: str,
        journal_path: str | None = None,
    ) -> dict:
        """Rollback only mutations whose expected values are still present."""
        path = journal_path or BAND_POLICY_JOURNAL_FILE
        journal["phase"] = "rolling_back"
        journal["rollback_reason"] = reason
        self._save_band_policy_journal(journal, path)
        conflicts = []
        mutations = journal.get("mutations", {})

        iwd_mutation = mutations.get("iwd", {})
        if iwd_mutation.get("planned"):
            current = self._get_iwd_rank_modifier_snapshot()
            expected = {
                "modifier_present": iwd_mutation.get("expected_present", False),
                "modifier_value": iwd_mutation.get("expected_value", ""),
            }
            before = journal.get("iwd_before", {})
            if self._iwd_snapshot_matches(current, expected):
                try:
                    # Persist the required service work before changing the
                    # file. If power is lost after the file restore, recovery
                    # still knows that iwd must be restarted and reconnected.
                    journal["rollback_iwd_restart_required"] = True
                    active_uuid = str(journal.get("active_uuid_before") or "")
                    if active_uuid:
                        journal["rollback_reconnect_required"] = True
                        journal["rollback_reconnect_uuid"] = active_uuid
                    self._save_band_policy_journal(journal, path)
                    self._restore_iwd_rank_modifier(before)
                    restored_iwd = self._get_iwd_rank_modifier_snapshot()
                    if self._iwd_snapshot_matches(restored_iwd, before):
                        journal["rollback_iwd_file_restored"] = True
                        self._save_band_policy_journal(journal, path)
                    else:
                        conflicts.append("iwd restore could not be verified")
                except Exception as e:
                    conflicts.append(f"iwd restore failed: {e}")
            elif not self._iwd_snapshot_matches(current, before):
                conflicts.append("iwd setting changed outside WiFi Optimizer")

        nm_mutation = mutations.get("nm_band", {})
        profile = journal.get("profile_before", {})
        uuid = str(profile.get("uuid") or "")
        if nm_mutation.get("planned") and uuid:
            current_band, current_result = self._nmcli_get(
                uuid, "802-11-wireless.band"
            )
            expected_band = str(nm_mutation.get("expected") or "")
            before_band = str(profile.get("band") or "")
            if current_result.get("success") and current_band == expected_band:
                if profile.get("was_active"):
                    journal["rollback_reconnect_required"] = True
                    journal["rollback_reconnect_uuid"] = uuid
                    self._save_band_policy_journal(journal, path)
                result = self._nmcli_modify(
                    uuid, "802-11-wireless.band", before_band, timeout=8
                )
                if result.get("success"):
                    restored_band, restored_result = self._nmcli_get(
                        uuid, "802-11-wireless.band"
                    )
                    if restored_result.get("success") and restored_band == before_band:
                        journal["rollback_nm_restored"] = True
                        self._save_band_policy_journal(journal, path)
                    else:
                        conflicts.append(
                            "NetworkManager band restore could not be verified"
                        )
                else:
                    conflicts.append(
                        f"NetworkManager restore failed: {result.get('stderr', '')}"
                    )
            elif current_result.get("success") and current_band != before_band:
                conflicts.append("profile band changed outside WiFi Optimizer")
            elif not current_result.get("success"):
                conflicts.append("could not read the saved WiFi profile")

        bssid_mutation = mutations.get("nm_bssid", {})
        if bssid_mutation.get("planned") and uuid:
            current_bssid, current_result = self._nmcli_get(
                uuid, "802-11-wireless.bssid"
            )
            current_bssid = self._normalize_bssid(current_bssid)
            expected_bssid = self._normalize_bssid(bssid_mutation.get("expected"))
            before_bssid = self._normalize_bssid(profile.get("bssid"))
            if current_result.get("success") and current_bssid == expected_bssid:
                restored = self._nmcli_modify(
                    uuid, "802-11-wireless.bssid", before_bssid, timeout=8
                )
                if restored.get("success"):
                    observed_bssid, observed_result = self._nmcli_get(
                        uuid, "802-11-wireless.bssid"
                    )
                    if (
                        observed_result.get("success")
                        and self._normalize_bssid(observed_bssid) == before_bssid
                    ):
                        journal["rollback_nm_bssid_restored"] = True
                        self._save_band_policy_journal(journal, path)
                    else:
                        conflicts.append(
                            "NetworkManager BSSID restore could not be verified"
                        )
                else:
                    conflicts.append(
                        f"NetworkManager BSSID restore failed: {restored.get('stderr', '')}"
                    )
            elif current_result.get("success") and current_bssid == before_bssid:
                journal["rollback_nm_bssid_restored"] = True
                self._save_band_policy_journal(journal, path)
            elif current_result.get("success"):
                conflicts.append("profile BSSID changed outside WiFi Optimizer")
            else:
                conflicts.append("could not read the saved WiFi profile BSSID")

        if journal.get("rollback_iwd_restart_required") and not journal.get(
            "rollback_iwd_restart_completed"
        ):
            restart = self._run_cmd(
                ["/usr/bin/systemctl", "restart", "iwd"], timeout=15
            )
            if not restart.get("success"):
                conflicts.append(f"iwd restart failed: {restart.get('stderr', '')}")
            else:
                journal["rollback_iwd_restart_completed"] = True
                self._save_band_policy_journal(journal, path)

        reconnect_uuid = str(journal.get("rollback_reconnect_uuid") or "")
        if (
            journal.get("rollback_reconnect_required")
            and not journal.get("rollback_reconnect_completed")
            and reconnect_uuid
        ):
            if self._profile_is_active(reconnect_uuid):
                journal["rollback_reconnect_completed"] = True
                self._save_band_policy_journal(journal, path)
            else:
                reconnect = self._reconnect_profile(
                    reconnect_uuid,
                    True,
                    cycle=not bool(journal.get("rollback_iwd_restart_required")),
                )
                if not reconnect.get("success"):
                    conflicts.append(
                        f"WiFi reconnect failed: {reconnect.get('stderr', '')}"
                    )
                else:
                    journal["rollback_reconnect_completed"] = True
                    self._save_band_policy_journal(journal, path)

        if not conflicts:
            settings_path = str(journal.get("settings_file") or SETTINGS_FILE)
            self._write_band_settings_fields(
                settings_path, journal.get("settings_before", {})
            )
            journal["phase"] = "rolled_back"
        else:
            journal["phase"] = "rollback_failed"
            journal["rollback_conflicts"] = conflicts
        journal["rolled_back_at"] = int(time.time())
        self._save_band_policy_journal(journal, path)
        return {
            "success": not conflicts,
            "rolled_back": not conflicts,
            "conflicts": conflicts,
        }

    def _recover_band_policy_journal(self) -> dict:
        """Complete/rollback an operation interrupted by a plugin restart."""
        journal = self._load_band_policy_journal()
        if not journal:
            return {"success": True, "recovered": False}
        phase = journal.get("phase")
        if phase in ("pending", "applying", "rolling_back"):
            result = self._restore_band_policy_transaction(
                journal, "interrupted operation"
            )
            if not result.get("success"):
                decky.logger.error(
                    "Band policy recovery needs manual intervention: "
                    + "; ".join(result.get("conflicts", []))
                )
                return {
                    "success": False,
                    "error": "band_policy_recovery_required",
                    "conflicts": result.get("conflicts", []),
                }
        elif phase == "verified":
            self._write_band_settings_fields(
                str(journal.get("settings_file") or SETTINGS_FILE),
                journal.get("settings_after", {}),
            )
            journal["phase"] = "committed"
            self._save_band_policy_journal(journal)
        elif phase == "rollback_failed":
            mutations = journal.get("mutations")
            retryable = (
                journal.get("schema") == 1
                and isinstance(journal.get("settings_before"), dict)
                and bool(journal.get("settings_before"))
                and isinstance(mutations, dict)
                and any(
                    isinstance(mutation, dict) and mutation.get("planned")
                    for mutation in mutations.values()
                )
            )
            if not retryable:
                decky.logger.error(
                    "Previous band policy rollback is incomplete; preserving journal"
                )
                return {
                    "success": False,
                    "error": "band_policy_recovery_required",
                    "conflicts": journal.get("rollback_conflicts", []),
                }
            result = self._restore_band_policy_transaction(
                journal, "retrying incomplete rollback"
            )
            if not result.get("success"):
                decky.logger.error(
                    "Band policy rollback retry needs manual intervention: "
                    + "; ".join(result.get("conflicts", []))
                )
                return {
                    "success": False,
                    "error": "band_policy_recovery_required",
                    "conflicts": result.get("conflicts", []),
                }
        elif phase not in ("committed", "rolled_back"):
            decky.logger.error(
                f"Unknown band policy journal phase '{phase}'; preserving journal"
            )
            return {
                "success": False,
                "error": "band_policy_recovery_required",
                "conflicts": [f"unknown journal phase: {phase}"],
            }

        self._cancel_band_policy_rollback(journal)
        self._remove_band_policy_journal()
        return {"success": True, "recovered": True}

    def _band_policy_ownership_error(self, settings: dict) -> str:
        mode = settings.get("band_policy", BAND_POLICY_OFF)
        if mode == BAND_POLICY_OFF:
            return ""
        state = settings.get("band_policy_state") or {}
        if state.get("mode") != mode:
            return "Band policy ownership data is incomplete."
        uuid = str(state.get("connection_uuid") or "")
        applied = state.get("applied") or {}
        if state.get("owns_nm_band"):
            current_band, result = self._nmcli_get(uuid, "802-11-wireless.band")
            if not result.get("success"):
                return "The managed WiFi profile cannot be read."
            if current_band != str(applied.get("nm_band") or ""):
                return "The profile band was changed outside WiFi Optimizer."
        if state.get("owns_iwd"):
            current_iwd = self._get_iwd_rank_modifier_snapshot()
            expected_iwd = {
                "modifier_present": applied.get("iwd_modifier_present", False),
                "modifier_value": applied.get("iwd_modifier_value", ""),
            }
            if not self._iwd_snapshot_matches(current_iwd, expected_iwd):
                return "The iwd band setting was changed outside WiFi Optimizer."
        return ""

    def _band_policy_transition_error(self, settings: dict, target: str) -> str:
        """Protect non-owned fields that the requested transition would replace."""
        mode = settings.get("band_policy", BAND_POLICY_OFF)
        state = settings.get("band_policy_state") or {}
        applied = state.get("applied") or {}
        if mode == BAND_POLICY_SIX_ONLY and target == BAND_POLICY_HIGH_ONLY:
            current_iwd = self._get_iwd_rank_modifier_snapshot()
            observed_iwd = {
                "modifier_present": applied.get("iwd_modifier_present", False),
                "modifier_value": applied.get("iwd_modifier_value", ""),
            }
            if not self._iwd_snapshot_matches(current_iwd, observed_iwd):
                return (
                    "The iwd band setting changed after 6 GHz only was enabled. "
                    "Disable the current policy before replacing it."
                )
        return ""

    async def _verify_band_policy(
        self, mode: str, uuid: str, timeout: int = 24
    ) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        last_frequency = None
        while asyncio.get_running_loop().time() < deadline:
            if self._profile_is_active(uuid):
                last_frequency = self._get_link_frequency()
                if mode == BAND_POLICY_SIX_ONLY and last_frequency is not None:
                    if 5925 <= last_frequency < 7125:
                        return {"success": True, "frequency": last_frequency}
                elif mode == BAND_POLICY_HIGH_ONLY:
                    # This mode is a ranking preference, not a band lock. A
                    # 2.4 GHz link is valid when no usable higher-band BSS is
                    # available; the iwd config itself is verified separately.
                    return {"success": True, "frequency": last_frequency}
                elif mode == BAND_POLICY_OFF:
                    return {"success": True, "frequency": last_frequency}
            await asyncio.sleep(1)
        return {
            "success": False,
            "frequency": last_frequency,
            "message": "WiFi did not reconnect on the requested band before timeout.",
        }

    def _get_wifi_interface(self) -> str | None:
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "DEVICE,TYPE", "dev", "status"]
        )
        if not result["success"]:
            return None
        for line in result["stdout"].split("\n"):
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "wifi":
                return parts[0]
        return None

    def _get_active_connection_uuid(self) -> str | None:
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "UUID,TYPE", "con", "show", "--active"]
        )
        if not result["success"]:
            return None
        for line in result["stdout"].split("\n"):
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "802-11-wireless":
                return parts[0]
        return None

    def _get_current_backend(self) -> str | None:
        """Return 'iwd', 'wpa_supplicant', or None if unknown.

        Checks distribution-owned NetworkManager configuration, then falls
        back to the active systemd service. The Go 2 fork never writes or
        switches the backend.
        """
        for path in (
            BAZZITE_IWD_CONF,
            WIFI_BACKEND_CONF,
            NM_DEFAULT_CONF,
        ):
            try:
                with open(path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or line.startswith(";"):
                            continue
                        if line.startswith("wifi.backend"):
                            _, _, val = line.partition("=")
                            val = val.strip()
                            if val in ("iwd", "wpa_supplicant"):
                                return val
            except FileNotFoundError:
                continue
            except Exception:
                continue
        # No config found — check which service is running
        result = self._run_cmd(["/usr/bin/systemctl", "is-active", "iwd"], timeout=3)
        if result.get("stdout", "").strip() == "active":
            return "iwd"
        result = self._run_cmd(
            ["/usr/bin/systemctl", "is-active", "wpa_supplicant"], timeout=3
        )
        if result.get("stdout", "").strip() == "active":
            return "wpa_supplicant"
        return None

    def _restore_legacy_bssid_lock(self, settings: dict | None = None) -> dict:
        """Restore only a legacy BSSID value still demonstrably owned by us."""
        settings = settings or _load_settings()
        if not settings.get("bssid_lock_enabled"):
            return {"success": True, "restored": False}
        uuid = str(settings.get("bssid_lock_connection_uuid") or "")
        expected = str(settings.get("bssid_lock_value") or "").lower()
        if not uuid or not expected:
            return {
                "success": False,
                "error": "ownership_missing",
                "message": "Legacy BSSID ownership data is incomplete.",
            }
        current, result = self._nmcli_get(uuid, "802-11-wireless.bssid")
        current = self._nmcli_unescape(current).lower()
        if not result.get("success"):
            return {
                "success": False,
                "error": "profile_read_failed",
                "message": "The legacy BSSID profile cannot be read.",
            }
        if current != expected:
            return {
                "success": False,
                "error": "ownership_conflict",
                "message": "The profile BSSID changed outside WiFi Optimizer.",
            }
        modified = self._nmcli_modify(uuid, "802-11-wireless.bssid", "")
        if not modified.get("success"):
            return {
                "success": False,
                "error": "nmcli_failed",
                "message": "The legacy BSSID lock could not be restored.",
                "detail": modified.get("stderr", ""),
            }
        settings["bssid_lock_enabled"] = False
        settings["bssid_lock_value"] = ""
        settings["bssid_lock_connection_uuid"] = ""
        _save_settings_with_timestamp(settings)
        if self._profile_is_active(uuid):
            self._reconnect_profile(uuid, True)
        return {"success": True, "restored": True}

    def _install_dispatcher(self) -> dict:
        try:
            template_path = os.path.join(
                DECKY_PLUGIN_DIR, "defaults", "dispatcher.sh.tmpl"
            )
            with open(template_path, "r") as f:
                script = f.read()
            script = script.replace("__SETTINGS_PATH__", SETTINGS_FILE)
            script = script.replace("__PLUGIN_DIR__", DECKY_PLUGIN_DIR)
            self._atomic_write_text(DISPATCHER_PATH, script, 0o755)
            os.chmod(DISPATCHER_PATH, 0o755)
            decky.logger.info("Dispatcher script installed")
            return {"success": True}
        except Exception as e:
            decky.logger.error(f"Failed to install dispatcher: {e}")
            return {"success": False, "error": "write_failed", "message": str(e)}

    def _remove_dispatcher(self) -> dict:
        try:
            os.remove(DISPATCHER_PATH)
            decky.logger.info("Dispatcher script removed")
            return {"success": True}
        except FileNotFoundError:
            return {"success": True}
        except Exception as e:
            decky.logger.error(f"Failed to remove dispatcher: {e}")
            return {"success": False, "error": "write_failed", "message": str(e)}

    def _rotate_logs(self, keep: int = 10):
        """Prune old log files on plugin startup. Decky does not rotate plugin
        logs automatically; each plugin load creates a new timestamped file in
        DECKY_PLUGIN_LOG_DIR, so without pruning they accumulate forever.
        Keep the newest `keep` files (typical size ~2-3 KB each, so bounded at
        roughly 30 KB total).
        """
        try:
            log_dir = getattr(decky, "DECKY_PLUGIN_LOG_DIR", None)
            if not log_dir or not os.path.isdir(log_dir):
                return
            files = [
                os.path.join(log_dir, f)
                for f in os.listdir(log_dir)
                if f.endswith(".log")
            ]
            if len(files) <= keep:
                return
            files.sort(key=os.path.getmtime, reverse=True)
            current_log = getattr(decky, "DECKY_PLUGIN_LOG", None)
            removed = 0
            for path in files[keep:]:
                # Paranoia: never delete the file we're currently writing to.
                if current_log and os.path.realpath(path) == os.path.realpath(current_log):
                    continue
                try:
                    os.remove(path)
                    removed += 1
                except Exception:
                    pass
            if removed:
                decky.logger.info(f"Rotated logs: removed {removed} old file(s), kept {keep} newest")
        except Exception as e:
            decky.logger.error(f"Log rotation error: {e}")

    # ---- Lifecycle ----

    async def _main(self):
        try:
            try:
                os.remove(HEALTH_MARKER_FILE)
            except FileNotFoundError:
                pass
            decky.logger.info("WiFi Optimizer Go 2 starting")
            self._rotate_logs()
            band_lock = await self._worker_complete(
                self._acquire_band_policy_file_lock,
                cancelled_cleanup=self._release_band_policy_file_lock
            )
            try:
                def recover():
                    return self._recover_power_save_journal(), self._recover_band_policy_journal()
                power_recovery, band_recovery = await self._worker_complete(recover)
            finally:
                self._release_band_policy_file_lock(band_lock)
            failed_recovery = [
                result
                for result in (power_recovery, band_recovery)
                if not result.get("success")
            ]
            if failed_recovery:
                decky.logger.error(
                    "WiFi Optimizer Go 2 is not ready: unresolved network "
                    "recovery state remains on disk"
                )
                # Deliberately do not create HEALTH_MARKER_FILE. The installer
                # must roll back instead of accepting a backend that cannot
                # safely determine ownership of the current network state.
                return
            info = await self.get_device_info()
            settings = _load_settings()
            settings["model"] = info.get("model", "unknown")
            settings["driver"] = info.get("driver", "unknown")
            settings["device_family"] = info.get("device_family", "unknown")
            settings["device_label"] = info.get("device_label", "Unknown Device")
            settings["chip_label"] = info.get("chip_label", "unknown")
            settings["supports_6ghz"] = info.get("supports_6ghz", False)
            distro = self._detect_distro()
            settings["distro_id"] = distro["id"]
            settings["distro_name"] = distro["name"]
            _save_settings(settings)

            if settings.get("auto_fix_on_wake", False):
                self._install_dispatcher()

            # Reapply only the explicitly selected per-profile power setting.
            iface = self._get_wifi_interface()
            if settings.get("power_save_legacy_detected"):
                decky.logger.error(
                    "Legacy global power-save state has no ownership snapshot; "
                    "startup left it untouched. Reset/uninstall the upstream "
                    "plugin before using this setting."
                )
            elif iface and settings.get("power_save_disabled"):
                try:
                    await self.set_power_save(True)
                except Exception as e:
                    decky.logger.error(f"Startup power save failed: {e}")

            # Read-only sanity check. The fork never switches NetworkManager's
            # WiFi backend; it merely refuses restricted modes unless iwd is live.
            conf_backend = self._get_current_backend()
            if conf_backend:
                active = self._run_cmd(
                    ["/usr/bin/systemctl", "is-active", conf_backend], timeout=3
                )
                state = (active.get("stdout") or "").strip()
                if state and state != "active":
                    decky.logger.error(
                        f"Backend inconsistency: config selects '{conf_backend}' "
                        f"but systemd reports '{state}'."
                    )

            decky.logger.info(
                f"WiFi Optimizer Go 2 ready: device={info.get('device_label')}, "
                f"family={info.get('device_family')}, driver={info.get('driver')}, "
                f"chip={info.get('chip_label')}, distro={distro['id']}"
            )
            self._atomic_write_json(
                HEALTH_MARKER_FILE,
                {
                    "plugin": "wifi-optimizer-go2",
                    "pid": os.getpid(),
                    "ready_at": int(time.time()),
                },
            )
        except Exception as e:
            decky.logger.error(f"WiFi Optimizer Go 2 _main error: {e}")

    async def _unload(self):
        try:
            try:
                os.remove(HEALTH_MARKER_FILE)
            except FileNotFoundError:
                pass
            decky.logger.info("WiFi Optimizer Go 2 unloading")
        except Exception as e:
            decky.logger.error(f"_unload error: {e}")

    async def _uninstall(self):
        try:
            decky.logger.info("WiFi Optimizer uninstalling")
            band_cleanup = await self.set_band_policy(BAND_POLICY_OFF)
            power_cleanup = await self.set_power_save(False)
            bssid_cleanup = self._restore_legacy_bssid_lock()
            preserve_recovery = not (
                band_cleanup.get("success")
                and power_cleanup.get("success")
                and bssid_cleanup.get("success")
            )
            if not band_cleanup.get("success"):
                decky.logger.error(
                    "Band policy could not be restored during uninstall; "
                    "preserving settings/journal for manual recovery: "
                    + band_cleanup.get("message", "unknown error")
                )
            if not power_cleanup.get("success"):
                decky.logger.error(
                    "Power-save policy could not be restored during uninstall; "
                    "preserving settings for manual recovery: "
                    + power_cleanup.get("message", "unknown error")
                )
            if not bssid_cleanup.get("success"):
                decky.logger.error(
                    "Legacy BSSID lock could not be restored during uninstall; "
                    "preserving settings for manual recovery: "
                    + bssid_cleanup.get("message", "unknown error")
                )
            self._remove_dispatcher()
            cleanup_paths = [ENFORCED_FILE]
            if not preserve_recovery:
                cleanup_paths.extend(
                    [
                        SETTINGS_FILE,
                        SETTINGS_FILE + ".bak",
                        BAND_POLICY_JOURNAL_FILE,
                        POWER_SAVE_JOURNAL_FILE,
                    ]
                )
            for path in cleanup_paths:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
        except Exception as e:
            decky.logger.error(f"_uninstall error: {e}")

    async def _migration(self):
        pass

    # ---- Hardware detection ----

    def _detect_device_family(self) -> tuple[str, str, str]:
        """Read DMI product_name and return (raw_product, family_id, display_label)."""
        try:
            with open("/sys/devices/virtual/dmi/id/product_name", "r") as f:
                product = f.read().strip()
        except Exception:
            return ("unknown", "unknown", "Unknown Device")

        if product in DMI_DEVICES:
            info = DMI_DEVICES[product]
            return (product, info["family"], info["label"])

        for prefix, info in DMI_SUBSTRING_DEVICES:
            if product.startswith(prefix):
                return (product, info["family"], info["label"])

        return (product, "unknown", "Unknown Device")

    def _detect_wifi_driver(self) -> str:
        """Detect the kernel driver of the active WiFi interface via sysfs.
        Normalizes sub-module names (e.g. rtw88_pci) to the canonical
        DRIVER_PROFILES key (rtw88)."""
        iface = self._get_wifi_interface()
        if not iface:
            return "unknown"
        try:
            driver_path = os.path.realpath(f"/sys/class/net/{iface}/device/driver/module")
            module = os.path.basename(driver_path)
            if module in DRIVER_PROFILES:
                return module
            for key in DRIVER_PROFILES:
                if module.startswith(key):
                    return key
            return module
        except Exception:
            return "unknown"

    def _detect_distro(self) -> dict:
        """Detect OS from /etc/os-release. Returns {id, name}."""
        info = {"id": "unknown", "name": "Unknown"}
        try:
            with open("/etc/os-release", "r") as f:
                for line in f:
                    if line.startswith("ID="):
                        info["id"] = line.split("=", 1)[1].strip().strip('"')
                    elif line.startswith("PRETTY_NAME="):
                        info["name"] = line.split("=", 1)[1].strip().strip('"')
        except Exception:
            pass
        return info

    async def get_device_info(self) -> dict:
        try:
            product, device_family, device_label = self._detect_device_family()
            driver = self._detect_wifi_driver()
            profile = DRIVER_PROFILES.get(driver, {})

            chip_label = profile.get("chip_label", "unknown")
            supports_6ghz = profile.get("supports_6ghz", False)

            model = "unknown"
            if device_family == "deck_lcd":
                model = "lcd"
            elif device_family == "deck_oled":
                model = "oled"

            return {
                "success": True,
                "model": model,
                "driver": driver,
                "device_family": device_family,
                "device_label": device_label,
                "chip_label": chip_label,
                "supports_6ghz": supports_6ghz,
            }
        except Exception as e:
            decky.logger.error(f"get_device_info error: {e}")
            return {
                "success": True,
                "model": "unknown",
                "driver": "unknown",
                "device_family": "unknown",
                "device_label": "Unknown Device",
                "chip_label": "unknown",
                "supports_6ghz": False,
            }

    def _get_support_tier(self) -> int:
        """Return 1 (full), 2 (partial), or 3 (generic) based on detection.
        Tier 1: recognized device + recognized driver.
        Tier 2: unknown device + recognized driver.
        Tier 3: unknown device + unknown driver."""
        settings = _load_settings()
        driver = settings.get("driver", "unknown")
        device_family = settings.get("device_family", "unknown")
        if driver in DRIVER_PROFILES and device_family != "unknown":
            return 1
        if driver in DRIVER_PROFILES:
            return 2
        return 3

    def _unexpected_response(self, e: Exception) -> dict:
        """Standard error dict for the catch-all exception handler in every
        setter. Callers log the error separately with the setter name."""
        return {"success": False, "error": "unexpected", "message": str(e)}

    @staticmethod
    def _deprecated_response(feature: str) -> dict:
        return {
            "success": False,
            "error": "deprecated",
            "message": (
                f"{feature} is not available in the Go 2 compatibility build."
            ),
        }

    def _nmcli_modify(self, uuid: str, key: str, value: str, timeout: int = 5) -> dict:
        """Run `nmcli con mod uuid <uuid> <key> <value>`. Returns the
        _run_cmd dict so callers can handle success/failure themselves."""
        return self._run_cmd(
            ["/usr/bin/nmcli", "con", "mod", "uuid", uuid, key, value],
            timeout=timeout,
        )

    # ---- Diagnostics ----

    async def get_diagnostic_info(self) -> dict:
        """Collect system info for remote debugging. Sanitized (no passwords)."""
        try:
            info = await self.get_device_info()
            iface = self._get_wifi_interface() or "none"
            iw_dev = self._run_cmd(["/usr/bin/iw", "dev"], timeout=3)
            iw_reg = self._run_cmd(["/usr/bin/iw", "reg", "get"], timeout=3)
            uname = self._run_cmd(["/usr/bin/uname", "-r"], timeout=3)
            os_release = ""
            try:
                with open("/etc/os-release", "r") as f:
                    os_release = f.read()
            except Exception:
                pass
            distro = self._detect_distro()
            return {
                "success": True,
                "device_info": info,
                "wifi_interface": iface,
                "iw_dev": iw_dev.get("stdout", ""),
                "iw_reg": iw_reg.get("stdout", ""),
                "kernel": uname.get("stdout", "").strip(),
                "os_release": os_release,
                "distro_id": distro["id"],
                "distro_name": distro["name"],
                "support_tier": self._get_support_tier(),
            }
        except Exception as e:
            decky.logger.error(f"get_diagnostic_info error: {e}")
            return {"success": False, "error": str(e)}

    async def save_diagnostic_info(self) -> dict:
        """Write diagnostics to a file in the settings directory as a
        fallback when clipboard is unavailable."""
        try:
            info = await self.get_diagnostic_info()
            diag_path = os.path.join(
                os.path.dirname(SETTINGS_FILE), "diagnostics.json"
            )
            self._atomic_write_json(diag_path, info)
            return {"success": True, "path": diag_path}
        except Exception as e:
            decky.logger.error(f"save_diagnostic_info error: {e}")
            return {"success": False, "error": str(e)}

    # ---- Status ----

    async def get_status(self) -> dict:
        # NetworkManager/iw are blocking system tools. Keep them away from
        # Decky's shared asyncio loop so a slow status read cannot stall other
        # Companion modules.
        return await asyncio.to_thread(self._get_status_sync)

    def _get_status_sync(self) -> dict:
        # Short read-only timeouts plus a 10-second UI interval keep background
        # work bounded while the panel is open.
        T = 2

        try:
            settings = _load_settings()
            iface = self._get_wifi_interface()
            uuid = self._get_active_connection_uuid()
            connected = iface is not None and uuid is not None
            support_tier = self._get_support_tier()

            status = {
                "success": True,
                "connected": connected,
                "support_tier": support_tier,
                "version": getattr(decky, "DECKY_PLUGIN_VERSION", "0.0.0"),
                "settings": settings,
                "live": {},
                "drift": {},
            }

            recovery_issues = self._get_network_recovery_issues()
            if recovery_issues:
                status["live"]["recovery_required"] = True
                status["live"]["recovery_errors"] = recovery_issues
                status["drift"]["recovery"] = True

            # Backend info is read-only and system-wide.
            status["live"]["wifi_backend"] = self._get_current_backend() or ""
            status["live"]["band_policy"] = settings.get(
                "band_policy", BAND_POLICY_OFF
            )

            if not connected:
                status["live"]["dispatcher_installed"] = os.path.isfile(
                    DISPATCHER_PATH
                )
                return status

            # Link info
            link_result = self._run_cmd(
                ["/usr/bin/iw", "dev", iface, "link"], timeout=T
            )
            link_out = link_result.get("stdout", "")
            for line in link_out.split("\n"):
                line = line.strip()
                if line.startswith("signal:"):
                    status["live"]["signal_dbm"] = line.split(":", 1)[1].strip()
                elif "tx bitrate:" in line:
                    status["live"]["tx_bitrate"] = line.split("tx bitrate:", 1)[
                        1
                    ].strip()
                elif line.startswith("freq:"):
                    status["live"]["frequency"] = line.split(":", 1)[1].strip()
                elif "Connected to" in line:
                    parts = line.split()
                    if len(parts) >= 3:
                        status["live"]["connected_bssid"] = parts[2]

            # Channel info - parse to "36 (80 MHz)" format
            info_result = self._run_cmd(
                ["/usr/bin/iw", "dev", iface, "info"], timeout=T
            )
            for line in info_result.get("stdout", "").split("\n"):
                line = line.strip()
                if line.startswith("channel"):
                    # Raw: "channel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz"
                    parts = line.split(",")
                    chan_num = ""
                    width = ""
                    if parts:
                        tokens = parts[0].split()
                        if len(tokens) >= 2:
                            chan_num = tokens[1]
                    for part in parts:
                        part = part.strip()
                        if part.startswith("width:"):
                            width = part.split(":", 1)[1].strip()
                    if chan_num and width:
                        status["live"]["channel"] = f"{chan_num} ({width})"
                    elif chan_num:
                        status["live"]["channel"] = chan_num
                    else:
                        status["live"]["channel"] = line

            # Band policy. Status is deliberately read-only: drift is reported
            # and never repaired from this code path.
            ownership_error = self._band_policy_ownership_error(settings)
            if ownership_error:
                status["drift"]["band_policy"] = True
                status["live"]["band_policy_error"] = ownership_error
            if settings.get("band_policy_legacy_detected"):
                status["drift"]["legacy_band_preference"] = True

            return status
        except Exception as e:
            decky.logger.error(f"get_status error: {e}")
            return self._unexpected_response(e)

    # ---- Power-save transaction helpers ------------------------------------

    def _get_power_save_lock(self) -> asyncio.Lock:
        return self._get_network_mutation_lock()

    @staticmethod
    def _parse_nm_power_save(value: str) -> str | None:
        normalized = (value or "").strip().lower()
        names = {
            "default": "0",
            "ignore": "1",
            "disable": "2",
            "enable": "3",
        }
        if normalized in ("0", "1", "2", "3"):
            return normalized
        for name, digit in names.items():
            if normalized in (name, f"{name} ({digit})", f"{digit} ({name})"):
                return digit
        return None

    def _get_runtime_power_save(self, iface: str) -> tuple[str | None, dict]:
        result = self._run_cmd(
            ["/usr/bin/iw", "dev", iface, "get", "power_save"], timeout=4
        )
        match = re.search(
            r"power\s+save:\s*(on|off)", result.get("stdout", ""), re.IGNORECASE
        )
        return (match.group(1).lower() if match else None), result

    @staticmethod
    def _power_settings_fields(settings: dict) -> dict:
        return {
            "power_save_disabled": bool(settings.get("power_save_disabled")),
            "power_save_connection_uuid": str(
                settings.get("power_save_connection_uuid") or ""
            ),
            "power_save_previous": str(settings.get("power_save_previous") or ""),
            "power_save_runtime_previous": str(
                settings.get("power_save_runtime_previous") or ""
            ),
            "power_save_legacy_detected": bool(
                settings.get("power_save_legacy_detected")
            ),
        }

    def _load_power_save_journal(self) -> dict | None:
        return self._load_band_policy_journal(POWER_SAVE_JOURNAL_FILE)

    def _save_power_save_journal(self, journal: dict):
        self._save_band_policy_journal(journal, POWER_SAVE_JOURNAL_FILE)

    def _remove_power_save_journal(self):
        self._remove_band_policy_journal(POWER_SAVE_JOURNAL_FILE)

    def _restore_power_save_transaction(self, journal: dict, reason: str) -> dict:
        journal["phase"] = "rolling_back"
        journal["rollback_reason"] = reason
        self._save_power_save_journal(journal)
        conflicts = []
        mutations = journal.get("mutations") or {}
        profile = journal.get("profile_before") or {}
        uuid = str(profile.get("uuid") or "")

        nm_mutation = mutations.get("nm_power_save") or {}
        if nm_mutation.get("planned") and uuid:
            current_raw, current_result = self._nmcli_get(
                uuid, "802-11-wireless.powersave"
            )
            current = self._parse_nm_power_save(current_raw)
            expected = str(nm_mutation.get("expected") or "")
            before = str(profile.get("nm_power_save") or "")
            if not current_result.get("success") or current is None:
                conflicts.append("could not parse the managed profile power-save state")
            elif current == expected:
                restored = self._nmcli_modify(
                    uuid, "802-11-wireless.powersave", before, timeout=8
                )
                if not restored.get("success"):
                    conflicts.append(
                        "NetworkManager power-save restore failed: "
                        + restored.get("stderr", "")
                    )
                else:
                    verified_raw, verified_result = self._nmcli_get(
                        uuid, "802-11-wireless.powersave"
                    )
                    verified = self._parse_nm_power_save(verified_raw)
                    if not verified_result.get("success") or verified != before:
                        conflicts.append(
                            "NetworkManager power-save restore could not be verified"
                        )
            elif current != before:
                conflicts.append("profile power-save changed outside WiFi Optimizer")

        runtime_mutation = mutations.get("runtime_power_save") or {}
        runtime = journal.get("runtime_before") or {}
        iface = str(runtime.get("iface") or "")
        if runtime_mutation.get("planned") and iface:
            current, current_result = self._get_runtime_power_save(iface)
            expected = str(runtime_mutation.get("expected") or "")
            before = str(runtime.get("power_save") or "")
            if not current_result.get("success") or current not in ("on", "off"):
                conflicts.append("could not parse the runtime power-save state")
            elif current == expected:
                restored = self._run_cmd(
                    [
                        "/usr/bin/iw",
                        "dev",
                        iface,
                        "set",
                        "power_save",
                        before,
                    ],
                    timeout=4,
                )
                if not restored.get("success"):
                    conflicts.append(
                        "runtime power-save restore failed: "
                        + restored.get("stderr", "")
                    )
                else:
                    verified, verified_result = self._get_runtime_power_save(iface)
                    if (
                        not verified_result.get("success")
                        or verified != before
                    ):
                        conflicts.append(
                            "runtime power-save restore could not be verified"
                        )
            elif current != before:
                conflicts.append("runtime power-save changed outside WiFi Optimizer")

        if not conflicts:
            self._write_band_settings_fields(
                str(journal.get("settings_file") or SETTINGS_FILE),
                journal.get("settings_before") or {},
            )
            journal["phase"] = "rolled_back"
        else:
            journal["phase"] = "rollback_failed"
            journal["rollback_conflicts"] = conflicts
        journal["rolled_back_at"] = int(time.time())
        self._save_power_save_journal(journal)
        return {
            "success": not conflicts,
            "rolled_back": not conflicts,
            "conflicts": conflicts,
        }

    def _recover_power_save_journal(self) -> dict:
        journal = self._load_power_save_journal()
        if not journal:
            return {"success": True, "recovered": False}
        phase = journal.get("phase")
        if phase in ("pending", "applying", "rolling_back"):
            result = self._restore_power_save_transaction(
                journal, "interrupted power-save operation"
            )
            if not result.get("success"):
                decky.logger.error(
                    "Power-save recovery needs manual intervention: "
                    + "; ".join(result.get("conflicts", []))
                )
                return {
                    "success": False,
                    "error": "power_save_recovery_required",
                    "conflicts": result.get("conflicts", []),
                }
        elif phase == "rollback_failed":
            decky.logger.error(
                "Previous power-save rollback failed; preserving journal"
            )
            return {
                "success": False,
                "error": "power_save_recovery_required",
                "conflicts": journal.get("rollback_conflicts", []),
            }
        elif phase not in ("committed", "rolled_back"):
            decky.logger.error(
                f"Unknown power-save journal phase '{phase}'; preserving journal"
            )
            return {
                "success": False,
                "error": "power_save_recovery_required",
                "conflicts": [f"unknown journal phase: {phase}"],
            }
        self._remove_power_save_journal()
        return {"success": True, "recovered": True}

    # ---- Optimization setters ----

    async def set_power_save(self, disabled: bool) -> dict:
        if not isinstance(disabled, bool):
            return {
                "success": False,
                "error": "invalid_power_save_state",
                "message": "Power-save state must be boolean.",
            }

        async with self._get_power_save_lock():
            if self._load_band_policy_journal():
                return {
                    "success": False,
                    "error": "network_recovery_required",
                    "message": (
                        "A band-policy transaction still needs recovery; "
                        "power save was not changed."
                    ),
                    "recovery_state_preserved": True,
                }
            existing_journal = self._load_power_save_journal()
            if existing_journal:
                self._recover_power_save_journal()
                remaining_journal = self._load_power_save_journal()
                if remaining_journal:
                    return {
                        "success": False,
                        "error": "power_save_recovery_required",
                        "message": (
                            "A previous power-save rollback needs manual recovery."
                        ),
                        "rollback_conflicts": remaining_journal.get(
                            "rollback_conflicts", []
                        ),
                        "recovery_state_preserved": True,
                    }

            settings = _load_settings()
            if settings.get("power_save_legacy_detected"):
                return {
                    "success": False,
                    "error": "legacy_power_save_state",
                    "message": (
                        "The previous plugin enabled a global power-save override "
                        "without recording the original per-profile value. No "
                        "safe value can be inferred; reset or uninstall the "
                        "upstream plugin first."
                    ),
                    "recovery_state_preserved": True,
                }
            if not disabled and not settings.get("power_save_disabled"):
                return {"success": True, "power_save_off": False, "rolled_back": False}

            iface = self._get_wifi_interface()
            active_uuid = self._get_active_connection_uuid()
            managed_uuid = str(settings.get("power_save_connection_uuid") or "")
            already_managed = bool(settings.get("power_save_disabled"))

            if disabled:
                if not iface or not active_uuid:
                    return {
                        "success": False,
                        "error": "no_wifi",
                        "message": "Connect to WiFi before changing power save.",
                    }
                if already_managed and managed_uuid != active_uuid:
                    return {
                        "success": False,
                        "error": "managed_profile_not_active",
                        "message": (
                            "Restore power save on the previously managed profile "
                            "before selecting another network."
                        ),
                    }
                profile_uuid = active_uuid
            else:
                if not managed_uuid:
                    return {
                        "success": False,
                        "error": "power_save_recovery_state_missing",
                        "message": "The managed power-save profile is missing.",
                    }
                profile_uuid = managed_uuid

            nm_raw, nm_result = self._nmcli_get(
                profile_uuid, "802-11-wireless.powersave"
            )
            nm_before = self._parse_nm_power_save(nm_raw)
            if not nm_result.get("success") or nm_before is None:
                return {
                    "success": False,
                    "error": "profile_state_unparseable",
                    "message": (
                        "The profile power-save value is not a supported "
                        "NetworkManager enum; no changes were made."
                    ),
                    "detail": nm_result.get("stderr", ""),
                }

            # Ownership is profile-scoped.  Check it before reading or writing
            # the volatile interface state so a reapply cannot overwrite an
            # external profile change (and does not require a runtime probe to
            # report that conflict).
            if already_managed and nm_before != "2":
                return {
                    "success": False,
                    "error": "ownership_conflict",
                    "message": (
                        "The profile power-save policy changed outside WiFi "
                        "Optimizer; it was not overwritten."
                    ),
                }

            runtime_active = bool(iface and active_uuid == profile_uuid)
            runtime_before = ""
            if runtime_active:
                runtime_before, runtime_result = self._get_runtime_power_save(iface)
                if not runtime_result.get("success") or runtime_before not in (
                    "on",
                    "off",
                ):
                    return {
                        "success": False,
                        "error": "runtime_state_unparseable",
                        "message": (
                            "The current runtime power-save state could not be "
                            "read; no changes were made."
                        ),
                        "detail": runtime_result.get("stderr", ""),
                    }

            settings_after = dict(settings)
            if disabled:
                if already_managed:
                    nm_target = "2"
                    nm_original = self._parse_nm_power_save(
                        str(settings.get("power_save_previous") or "")
                    )
                    if nm_original is None:
                        return {
                            "success": False,
                            "error": "power_save_recovery_state_invalid",
                            "message": "The saved prior profile value is invalid.",
                        }
                    runtime_original = str(
                        settings.get("power_save_runtime_previous") or ""
                    )
                    if runtime_original not in ("on", "off"):
                        # Legacy state did not record runtime. Preserve the
                        # exact current state instead of guessing.
                        runtime_original = runtime_before
                else:
                    nm_target = "2"
                    nm_original = nm_before
                    runtime_original = runtime_before
                runtime_target = "off"
                settings_after.update(
                    {
                        "power_save_disabled": True,
                        "power_save_connection_uuid": profile_uuid,
                        "power_save_previous": nm_original,
                        "power_save_runtime_previous": runtime_original,
                        "power_save_legacy_detected": False,
                    }
                )
            else:
                nm_target = self._parse_nm_power_save(
                    str(settings.get("power_save_previous") or "")
                )
                if nm_target is None:
                    return {
                        "success": False,
                        "error": "power_save_recovery_state_invalid",
                        "message": "The saved prior profile value is invalid.",
                    }
                runtime_target = str(
                    settings.get("power_save_runtime_previous") or ""
                )
                if runtime_active and runtime_target not in ("on", "off"):
                    # Legacy state: retain the observed state exactly.
                    runtime_target = runtime_before
                settings_after.update(
                    {
                        "power_save_disabled": False,
                        "power_save_connection_uuid": "",
                        "power_save_previous": "",
                        "power_save_runtime_previous": "",
                        "power_save_legacy_detected": False,
                    }
                )

            journal = {
                "schema": 1,
                "phase": "pending",
                "created_at": int(time.time()),
                "settings_file": SETTINGS_FILE,
                "settings_before": self._power_settings_fields(settings),
                "profile_before": {
                    "uuid": profile_uuid,
                    "nm_power_save": nm_before,
                },
                "runtime_before": {
                    "iface": iface if runtime_active else "",
                    "power_save": runtime_before if runtime_active else "",
                },
                "mutations": {},
            }
            try:
                self._save_power_save_journal(journal)
                journal["phase"] = "applying"
                self._save_power_save_journal(journal)

                if nm_before != nm_target:
                    journal["mutations"]["nm_power_save"] = {
                        "planned": True,
                        "expected": nm_target,
                    }
                    self._save_power_save_journal(journal)
                    modified = self._nmcli_modify(
                        profile_uuid,
                        "802-11-wireless.powersave",
                        nm_target,
                        timeout=8,
                    )
                    if not modified.get("success"):
                        raise RuntimeError(
                            modified.get("stderr")
                            or "NetworkManager rejected the power-save value"
                        )
                    verified_raw, verified_result = self._nmcli_get(
                        profile_uuid, "802-11-wireless.powersave"
                    )
                    verified_nm = self._parse_nm_power_save(verified_raw)
                    if (
                        not verified_result.get("success")
                        or verified_nm != nm_target
                    ):
                        raise RuntimeError(
                            "NetworkManager power-save verification did not "
                            "match the request"
                        )

                if runtime_active and runtime_before != runtime_target:
                    journal["mutations"]["runtime_power_save"] = {
                        "planned": True,
                        "expected": runtime_target,
                    }
                    self._save_power_save_journal(journal)
                    changed = self._run_cmd(
                        [
                            "/usr/bin/iw",
                            "dev",
                            iface,
                            "set",
                            "power_save",
                            runtime_target,
                        ],
                        timeout=4,
                    )
                    if not changed.get("success"):
                        raise RuntimeError(
                            changed.get("stderr") or "iw rejected the power-save state"
                        )
                    verified_runtime, verified_result = self._get_runtime_power_save(
                        iface
                    )
                    if (
                        not verified_result.get("success")
                        or verified_runtime != runtime_target
                    ):
                        raise RuntimeError(
                            "Runtime power-save verification did not match the request"
                        )

                journal["settings_after"] = self._power_settings_fields(
                    settings_after
                )
                self._write_band_settings_fields(
                    SETTINGS_FILE, journal["settings_after"]
                )
                journal["phase"] = "committed"
                self._save_power_save_journal(journal)
                self._remove_power_save_journal()
                return {
                    "success": True,
                    "power_save_off": disabled,
                    "rolled_back": False,
                }
            except Exception as e:
                decky.logger.error(f"set_power_save apply error: {e}")
                try:
                    rollback = self._restore_power_save_transaction(journal, str(e))
                except Exception as rollback_error:
                    decky.logger.error(
                        f"power-save rollback infrastructure failed: {rollback_error}"
                    )
                    return {
                        "success": False,
                        "error": "power_save_apply_failed",
                        "message": str(e),
                        "rolled_back": False,
                        "rollback_conflicts": [str(rollback_error)],
                        "recovery_state_preserved": os.path.isfile(
                            POWER_SAVE_JOURNAL_FILE
                        ),
                    }
                if rollback.get("success"):
                    self._remove_power_save_journal()
                return {
                    "success": False,
                    "error": "power_save_apply_failed",
                    "message": str(e),
                    "rolled_back": rollback.get("rolled_back", False),
                    "rollback_conflicts": rollback.get("conflicts", []),
                    "recovery_state_preserved": not rollback.get("success"),
                }

    async def set_auto_fix(self, enabled: bool) -> dict:
        try:
            async with self._get_network_mutation_lock():
                if self._load_band_policy_journal() or self._load_power_save_journal():
                    return {
                        "success": False,
                        "error": "network_recovery_required",
                        "message": (
                            "A previous network transaction still needs "
                            "recovery; the dispatcher was not changed."
                        ),
                        "recovery_state_preserved": True,
                    }
                if enabled:
                    dispatcher_result = self._install_dispatcher()
                else:
                    dispatcher_result = self._remove_dispatcher()

                if not dispatcher_result.get("success"):
                    return dispatcher_result

                self._write_band_settings_fields(
                    SETTINGS_FILE, {"auto_fix_on_wake": bool(enabled)}
                )
                return {
                    "success": True,
                    "dispatcher_installed": os.path.isfile(DISPATCHER_PATH),
                }
        except Exception as e:
            decky.logger.error(f"set_auto_fix error: {e}")
            return {"success": False, "error": "write_failed", "message": str(e)}

    async def set_bssid_lock(self, enabled: bool) -> dict:
        return self._deprecated_response("BSSID lock")

    async def get_band_policy_capabilities(self) -> dict:
        """Return live, non-secret preflight facts used by the policy UI."""
        try:
            return await asyncio.to_thread(
                self._get_band_policy_capabilities_sync, True
            )
        except Exception as e:
            decky.logger.error(f"get_band_policy_capabilities error: {e}")
            return {
                "success": False,
                "six_ghz_only_available": False,
                "five_six_no_24_available": False,
                "two_ghz_bss_visible": False,
                "five_ghz_bss_visible": False,
                "six_ghz_bss_visible": False,
                "nm_supports_6ghz": False,
                "iwd_available": False,
                "has_5ghz": False,
                "has_6ghz": False,
                "reason_six_ghz_only": str(e),
                "reason_five_six_no_24": str(e),
            }

    async def set_band_policy(self, mode: str) -> dict:
        """Run one band transaction while fenced from the rollback service."""
        async with self._get_band_policy_file_gate():
            try:
                file_lock = await self._worker_complete(
                    self._acquire_band_policy_file_lock,
                    cancelled_cleanup=self._release_band_policy_file_lock
                )
            except Exception as e:
                decky.logger.error(f"band policy lock error: {e}")
                return {
                    "success": False,
                    "error": "band_policy_lock_failed",
                    "message": "The cross-process rollback lock is unavailable.",
                    "detail": str(e),
                    "band_policy": _load_settings().get(
                        "band_policy", BAND_POLICY_OFF
                    ),
                    "rolled_back": False,
                }
            try:
                async with self._get_network_mutation_lock():
                    return await self._worker_complete(
                        lambda: asyncio.run(
                            self._set_band_policy_impl(mode, lock_already_held=True)
                        )
                    )
            finally:
                # LOCK_UN and close are local, non-blocking operations. Doing
                # this directly also guarantees progress if executor threads
                # are busy with unrelated work.
                self._release_band_policy_file_lock(file_lock)

    async def _set_band_policy_impl(
        self, mode: str, lock_already_held: bool = False
    ) -> dict:
        """Atomically select one Go 2 band policy, or restore the prior state.

        Every mutating operation is protected by both a process-local lock and
        a systemd rollback timer.  The timer reads the on-disk journal and
        restores only values that still equal the values written by us.
        """
        if mode not in BAND_POLICIES:
            return {
                "success": False,
                "error": "invalid_band_policy",
                "message": (
                    "Band policy must be off, six_ghz_only, or "
                    "five_six_no_24."
                ),
                "band_policy": BAND_POLICY_OFF,
                "rolled_back": False,
            }

        async with self._network_lock_context(lock_already_held):
            if self._load_power_save_journal():
                return {
                    "success": False,
                    "error": "network_recovery_required",
                    "message": (
                        "A power-save transaction still needs recovery; "
                        "the band policy was not changed."
                    ),
                    "band_policy": _load_settings().get(
                        "band_policy", BAND_POLICY_OFF
                    ),
                    "rolled_back": False,
                    "recovery_state_preserved": True,
                }
            existing_journal = self._load_band_policy_journal()
            if existing_journal:
                recovery = self._recover_band_policy_journal()
                if not recovery.get("success") or self._load_band_policy_journal():
                    remaining = self._load_band_policy_journal() or existing_journal
                    return {
                        "success": False,
                        "error": "band_policy_recovery_required",
                        "message": (
                            "A previous band-policy rollback needs manual "
                            "recovery; no new network changes were made."
                        ),
                        "band_policy": _load_settings().get(
                            "band_policy", BAND_POLICY_OFF
                        ),
                        "rolled_back": False,
                        "rollback_conflicts": remaining.get(
                            "rollback_conflicts", recovery.get("conflicts", [])
                        ),
                        "recovery_state_preserved": True,
                    }
            settings = _load_settings()
            current_mode = settings.get("band_policy", BAND_POLICY_OFF)
            legacy = bool(settings.get("band_policy_legacy_detected"))

            if current_mode not in BAND_POLICIES:
                return {
                    "success": False,
                    "error": "invalid_saved_band_policy",
                    "message": "Saved band policy is invalid. Reset it before continuing.",
                    "band_policy": current_mode,
                    "rolled_back": False,
                }

            preference_needs_upgrade = (
                current_mode == BAND_POLICY_HIGH_ONLY
                and mode == BAND_POLICY_HIGH_ONLY
                and str(
                    ((settings.get("band_policy_state") or {}).get("applied") or {}).get(
                        "iwd_modifier_value"
                    )
                    or ""
                )
                != BAND_PREFERENCE_2_4_MODIFIER
            )
            if current_mode == mode and not legacy and not preference_needs_upgrade:
                ownership_error = self._band_policy_ownership_error(settings)
                if ownership_error:
                    return {
                        "success": False,
                        "error": "ownership_conflict",
                        "message": ownership_error,
                        "band_policy": current_mode,
                        "rolled_back": False,
                    }
                return {
                    "success": True,
                    "band_policy": current_mode,
                    "reconnected": False,
                    "rolled_back": False,
                    "message": "Band policy is already active.",
                }

            if legacy and mode != BAND_POLICY_OFF:
                return {
                    "success": False,
                    "error": "legacy_band_preference_detected",
                    "message": (
                        "Disable the migrated legacy 5 GHz preference before "
                        "selecting a new band policy."
                    ),
                    "band_policy": current_mode,
                    "rolled_back": False,
                }

            ownership_error = self._band_policy_ownership_error(settings)
            if ownership_error:
                return {
                    "success": False,
                    "error": "ownership_conflict",
                    "message": ownership_error,
                    "band_policy": current_mode,
                    "rolled_back": False,
                }

            transition_error = self._band_policy_transition_error(settings, mode)
            if transition_error:
                return {
                    "success": False,
                    "error": "transition_conflict",
                    "message": transition_error,
                    "band_policy": current_mode,
                    "rolled_back": False,
                }

            if mode == BAND_POLICY_SIX_ONLY:
                capabilities = await asyncio.to_thread(
                    self._get_band_policy_capabilities_sync, True
                )
                if not capabilities.get("six_ghz_only_available"):
                    return {
                        "success": False,
                        "error": "preflight_failed",
                        "message": capabilities.get("reason_six_ghz_only")
                        or "Band policy preflight failed.",
                        "band_policy": current_mode,
                        "rolled_back": False,
                        "capabilities": capabilities,
                    }
            elif mode == BAND_POLICY_HIGH_ONLY:
                _, device_family, _ = self._detect_device_family()
                driver = self._detect_wifi_driver()
                if device_family != "legion_go_2" or driver != "mt7921e":
                    return {
                        "success": False,
                        "error": "unsupported_device",
                        "message": (
                            "High-band preference is enabled only on a "
                            "Legion Go 2 with the MediaTek MT7922 driver."
                        ),
                        "band_policy": current_mode,
                        "rolled_back": False,
                    }
                if self._get_current_backend() != "iwd":
                    return {
                        "success": False,
                        "error": "unsupported_backend",
                        "message": "High-band preference requires the iwd WiFi backend.",
                        "band_policy": current_mode,
                        "rolled_back": False,
                    }
                iwd_service = self._run_cmd(
                    ["/usr/bin/systemctl", "is-active", "iwd"], timeout=3
                )
                if iwd_service.get("stdout", "").strip() != "active":
                    return {
                        "success": False,
                        "error": "iwd_inactive",
                        "message": "The iwd service is not active.",
                        "band_policy": current_mode,
                        "rolled_back": False,
                    }

            active_uuid = self._get_active_connection_uuid()
            state = settings.get("band_policy_state") or {}
            profileless_iwd_transition = (
                not active_uuid
                and not legacy
                and mode in (BAND_POLICY_OFF, BAND_POLICY_HIGH_ONLY)
                and current_mode in (BAND_POLICY_OFF, BAND_POLICY_HIGH_ONLY)
            )
            if mode != BAND_POLICY_OFF:
                profile_uuid = active_uuid
                if current_mode == BAND_POLICY_SIX_ONLY:
                    managed_uuid = str(state.get("connection_uuid") or "")
                    if managed_uuid and managed_uuid != active_uuid:
                        return {
                            "success": False,
                            "error": "managed_profile_not_active",
                            "message": (
                                "Disable 6 GHz only on its managed profile before "
                                "activating another policy."
                            ),
                            "band_policy": current_mode,
                            "rolled_back": False,
                        }
            elif legacy:
                profile_uuid = str(
                    settings.get("band_policy_legacy_connection_uuid")
                    or active_uuid
                    or ""
                )
            elif current_mode == BAND_POLICY_SIX_ONLY:
                profile_uuid = str(state.get("connection_uuid") or "")
            elif current_mode == BAND_POLICY_HIGH_ONLY:
                # The 5/6 GHz policy owns only the global iwd Rank key.  Its
                # original profile may have been forgotten since activation;
                # removing that global key must not depend on the old UUID.
                profile_uuid = str(active_uuid or "")
            else:
                profile_uuid = str(active_uuid or state.get("connection_uuid") or "")

            if not profile_uuid and not profileless_iwd_transition:
                if mode == BAND_POLICY_OFF and current_mode == BAND_POLICY_OFF:
                    return {
                        "success": True,
                        "band_policy": BAND_POLICY_OFF,
                        "reconnected": False,
                        "rolled_back": False,
                    }
                return {
                    "success": False,
                    "error": "no_wifi",
                    "message": "No managed WiFi profile was found.",
                    "band_policy": current_mode,
                    "rolled_back": False,
                }

            if profileless_iwd_transition:
                # No NetworkManager field is owned or changed in this path.
                band_before = ""
            else:
                band_before, band_result = self._nmcli_get(
                    profile_uuid, "802-11-wireless.band"
                )
                if not band_result.get("success"):
                    return {
                        "success": False,
                        "error": "profile_read_failed",
                        "message": "The WiFi profile could not be read.",
                        "detail": band_result.get("stderr", ""),
                        "band_policy": current_mode,
                        "rolled_back": False,
                    }

            if legacy:
                expected_legacy = str(settings.get("band_policy_legacy_band") or "a")
                if band_before != expected_legacy:
                    return {
                        "success": False,
                        "error": "ownership_conflict",
                        "message": (
                            "The migrated profile band no longer matches the value "
                            "written by the legacy plugin."
                        ),
                        "band_policy": current_mode,
                        "rolled_back": False,
                    }

            iwd_before = self._get_iwd_rank_modifier_snapshot()
            if mode == BAND_POLICY_HIGH_ONLY:
                modifier_error = self._iwd_five_ghz_modifier_error(iwd_before)
                if not modifier_error:
                    modifier_error = self._iwd_six_ghz_modifier_error(iwd_before)
                if modifier_error:
                    return {
                        "success": False,
                        "error": "preflight_failed",
                        "message": modifier_error,
                        "band_policy": current_mode,
                        "rolled_back": False,
                    }
            profile_was_active = bool(profile_uuid and profile_uuid == active_uuid)

            # First calculate the neutral/original values after removing the
            # currently owned policy.  This is what makes transitions mutually
            # exclusive rather than stacking two independent toggles.
            neutral_band = band_before
            neutral_iwd = iwd_before
            if legacy:
                neutral_band = ""
            elif current_mode == BAND_POLICY_SIX_ONLY:
                neutral_band = str((state.get("original") or {}).get("nm_band") or "")
            elif current_mode == BAND_POLICY_HIGH_ONLY:
                neutral_iwd = (state.get("original") or {}).get("iwd") or {}

            final_band = neutral_band
            final_iwd = neutral_iwd
            if mode == BAND_POLICY_SIX_ONLY:
                final_band = "6GHz"
            elif mode == BAND_POLICY_HIGH_ONLY:
                final_iwd = {
                    "file_exists": True,
                    "rank_section_present": True,
                    "modifier_present": True,
                    "modifier_value": BAND_PREFERENCE_2_4_MODIFIER,
                }

            if mode in (BAND_POLICY_SIX_ONLY, BAND_POLICY_HIGH_ONLY) and neutral_band:
                return {
                    "success": False,
                    "error": "external_band_configuration",
                    "message": (
                        "The active profile has a band setting not owned by this "
                        "plugin. Clear it before enabling the high-band preference."
                    ),
                    "band_policy": current_mode,
                    "rolled_back": False,
                }

            settings_before = self._band_settings_fields(settings)
            operation_id = f"{os.getpid()}-{int(time.time() * 1000)}"
            journal = {
                "schema": 1,
                "operation_id": operation_id,
                "phase": "pending",
                "created_at": int(time.time()),
                "settings_file": SETTINGS_FILE,
                "settings_before": settings_before,
                "mode_before": current_mode,
                "mode_target": mode,
                "profile_before": {
                    "uuid": profile_uuid,
                    "band": band_before,
                    "was_active": profile_was_active,
                },
                "active_uuid_before": str(active_uuid or ""),
                "iwd_before": iwd_before,
                "mutations": {},
            }
            self._save_band_policy_journal(journal)
            watchdog = self._schedule_band_policy_rollback(journal)
            if not watchdog.get("success"):
                self._remove_band_policy_journal()
                return {
                    "success": False,
                    "error": "rollback_watchdog_failed",
                    "message": "Automatic rollback could not be armed; no changes were made.",
                    "detail": watchdog.get("stderr", ""),
                    "band_policy": current_mode,
                    "rolled_back": False,
                }

            watchdog_owns_transaction = False
            try:
                journal["phase"] = "applying"
                self._save_band_policy_journal(journal)
                iwd_changed = False
                nm_changed = False

                def apply_nm_band(value: str):
                    nonlocal band_before, nm_changed
                    if band_before == value:
                        return
                    journal["mutations"]["nm_band"] = {
                        "planned": True,
                        "expected": value,
                        "applied": False,
                    }
                    self._save_band_policy_journal(journal)
                    result = self._nmcli_modify(
                        profile_uuid, "802-11-wireless.band", value, timeout=8
                    )
                    if not result.get("success"):
                        raise RuntimeError(
                            result.get("stderr") or "NetworkManager rejected the band setting"
                        )
                    observed_band, observed_result = self._nmcli_get(
                        profile_uuid, "802-11-wireless.band"
                    )
                    if not observed_result.get("success") or observed_band != value:
                        raise RuntimeError(
                            "NetworkManager band verification did not match the request"
                        )
                    journal["mutations"]["nm_band"]["applied"] = True
                    self._save_band_policy_journal(journal)
                    band_before = value
                    nm_changed = True

                def apply_iwd(snapshot: dict):
                    nonlocal iwd_before, iwd_changed
                    if self._iwd_snapshot_matches(iwd_before, snapshot):
                        return
                    expected_present = bool(snapshot.get("modifier_present"))
                    expected_value = str(snapshot.get("modifier_value") or "")
                    journal["mutations"]["iwd"] = {
                        "planned": True,
                        "expected_present": expected_present,
                        "expected_value": expected_value,
                        "applied": False,
                    }
                    self._save_band_policy_journal(journal)
                    if expected_present:
                        self._set_iwd_rank_modifier(expected_value)
                    else:
                        self._restore_iwd_rank_modifier(snapshot)
                    journal["mutations"]["iwd"]["applied"] = True
                    self._save_band_policy_journal(journal)
                    iwd_before = self._get_iwd_rank_modifier_snapshot()
                    if not self._iwd_snapshot_matches(iwd_before, snapshot):
                        raise RuntimeError(
                            "iwd band-policy verification did not match the request"
                        )
                    iwd_changed = True

                # Remove the old owned policy before applying the new one.
                if current_mode == BAND_POLICY_HIGH_ONLY:
                    apply_iwd(neutral_iwd)
                if current_mode == BAND_POLICY_SIX_ONLY or legacy:
                    apply_nm_band(neutral_band)

                if mode == BAND_POLICY_SIX_ONLY:
                    apply_nm_band(final_band)
                elif mode == BAND_POLICY_HIGH_ONLY:
                    apply_iwd(final_iwd)

                if iwd_changed:
                    restart = self._run_cmd(
                        ["/usr/bin/systemctl", "restart", "iwd"], timeout=15
                    )
                    if not restart.get("success"):
                        raise RuntimeError(
                            restart.get("stderr") or "iwd restart failed"
                        )

                reconnect_uuid = ""
                if iwd_changed and active_uuid:
                    reconnect_uuid = active_uuid
                elif nm_changed and profile_was_active:
                    reconnect_uuid = profile_uuid
                reconnected = bool(reconnect_uuid)
                if reconnect_uuid:
                    reconnect = self._reconnect_profile(
                        reconnect_uuid, True, cycle=not iwd_changed
                    )
                    if not reconnect.get("success"):
                        raise RuntimeError(
                            reconnect.get("stderr") or "WiFi reconnect failed"
                        )

                if mode == BAND_POLICY_HIGH_ONLY:
                    verification = {
                        "success": True,
                        "frequency": self._get_link_frequency(),
                    }
                elif mode != BAND_POLICY_OFF:
                    verification = await self._verify_band_policy(
                        mode, profile_uuid
                    )
                    if not verification.get("success"):
                        raise RuntimeError(
                            verification.get("message") or "Band verification failed"
                        )
                elif reconnect_uuid:
                    verification = await self._verify_band_policy(
                        BAND_POLICY_OFF, reconnect_uuid
                    )
                    if not verification.get("success"):
                        raise RuntimeError(
                            verification.get("message") or "WiFi reconnect failed"
                        )
                else:
                    verification = {"success": True, "frequency": None}

                on_disk_journal = self._load_band_policy_journal()
                if (
                    not on_disk_journal
                    or on_disk_journal.get("operation_id") != operation_id
                    or on_disk_journal.get("phase") != "applying"
                ):
                    watchdog_owns_transaction = True
                    raise RuntimeError(
                        "The rollback watchdog took ownership of the transaction."
                    )

                if mode == BAND_POLICY_OFF:
                    next_state = {}
                else:
                    next_state = {
                        "schema": 1,
                        "mode": mode,
                        "connection_uuid": profile_uuid,
                        "original": {
                            "nm_band": neutral_band,
                            "iwd": neutral_iwd,
                        },
                        "owns_nm_band": mode == BAND_POLICY_SIX_ONLY,
                        "owns_iwd": mode == BAND_POLICY_HIGH_ONLY,
                        "applied": {
                            "nm_band": final_band,
                            "iwd_modifier_present": bool(
                                final_iwd.get("modifier_present")
                            ),
                            "iwd_modifier_value": str(
                                final_iwd.get("modifier_value") or ""
                            ),
                        },
                        "activated_at": int(time.time()),
                    }
                settings_after = {
                    "band_policy": mode,
                    "band_policy_state": next_state,
                    "band_policy_legacy_detected": False,
                    "band_policy_legacy_band": "",
                    "band_policy_legacy_connection_uuid": "",
                    "band_preference": "5_6",
                    "band_preference_enabled": mode == BAND_POLICY_HIGH_ONLY,
                }
                journal["settings_after"] = settings_after
                journal["phase"] = "verified"
                journal["verified_frequency"] = verification.get("frequency")
                self._save_band_policy_journal(journal)
                self._write_band_settings_fields(SETTINGS_FILE, settings_after)
                journal["phase"] = "committed"
                self._save_band_policy_journal(journal)
                self._cancel_band_policy_rollback(journal)
                self._remove_band_policy_journal()
                return {
                    "success": True,
                    "band_policy": mode,
                    "reconnected": reconnected,
                    "rolled_back": False,
                    "frequency": verification.get("frequency"),
                    "message": "Band policy applied and verified.",
                }
            except Exception as e:
                decky.logger.error(f"set_band_policy apply error: {e}")
                if watchdog_owns_transaction:
                    # The systemd service may be between restoring the iwd
                    # file and restarting/reconnecting. Never replay a stale
                    # in-memory journal or stop that service mid-recovery.
                    latest = self._load_band_policy_journal() or {}
                    latest_phase = str(latest.get("phase") or "")
                    return {
                        "success": False,
                        "error": "rollback_watchdog_owned",
                        "message": str(e),
                        "band_policy": current_mode,
                        "rolled_back": latest_phase == "rolled_back",
                        "rollback_conflicts": latest.get(
                            "rollback_conflicts", []
                        ),
                        "recovery_state_preserved": bool(latest),
                    }
                rollback = self._restore_band_policy_transaction(
                    journal, str(e)
                )
                self._cancel_band_policy_rollback(journal)
                if rollback.get("success"):
                    self._remove_band_policy_journal()
                return {
                    "success": False,
                    "error": "band_policy_apply_failed",
                    "message": str(e),
                    "band_policy": current_mode,
                    "rolled_back": rollback.get("rolled_back", False),
                    "rollback_conflicts": rollback.get("conflicts", []),
                }

    async def set_band_preference(self, enabled: bool, band: str = "a") -> dict:
        """Prefer 5/6 GHz while retaining 2.4 GHz as a fallback."""
        if type(enabled) is not bool:
            return {
                "success": False,
                "error": "invalid_band_preference_state",
                "message": "Band preference must be a boolean.",
            }
        return await self.set_band_policy(
            BAND_POLICY_HIGH_ONLY if enabled else BAND_POLICY_OFF
        )

    async def set_dns(
        self, enabled: bool, provider: str = "cloudflare", custom_servers: str = ""
    ) -> dict:
        return self._deprecated_response("Custom DNS")

    async def set_ipv6(self, disabled: bool) -> dict:
        return self._deprecated_response("IPv6 override")

    async def set_buffer_tuning(self, enabled: bool) -> dict:
        return self._deprecated_response("Global buffer tuning")

    async def set_cake(self, enabled: bool) -> dict:
        """Enable or disable CAKE QoS (unlimited mode: FQ + AQM + ack-filter, no bandwidth shaper)."""
        return self._deprecated_response("CAKE override")

    async def optimize_safe(self) -> dict:
        """Removed: the legacy bundle mixed unrelated system mutations."""
        return self._deprecated_response("One-click Optimize Safe")

    async def reapply_volatile(self) -> dict:
        """Reapply volatile (non-reconnecting) settings. Safe to call mid-stream."""
        try:
            settings = _load_settings()
            applied = 0
            total = 0

            if settings.get("power_save_disabled"):
                total += 1
                r = await self.set_power_save(True)
                if r.get("success"):
                    applied += 1

            if total > 0:
                decky.logger.info(f"reapply_volatile: {applied}/{total} applied")

            return {"success": True, "applied": applied, "total": total}
        except Exception as e:
            decky.logger.error(f"reapply_volatile error: {e}")
            return self._unexpected_response(e)

    async def reapply_all(self) -> dict:
        """Force reapply all enabled optimizations."""
        try:

            settings = _load_settings()
            results = {}
            applied = 0
            total = 0
            if settings.get("auto_fix_on_wake"):
                total += 1
                r = await self.set_auto_fix(True)
                results["auto_fix"] = r
                if r.get("success"):
                    applied += 1

            # Per-profile power save is the only remaining volatile tuning.
            if settings.get("power_save_disabled"):
                total += 1
                r = await self.set_power_save(True)
                results["power_save"] = r
                if r.get("success"):
                    applied += 1

            if total == 0:
                return {
                    "success": True,
                    "total": 0,
                    "applied": 0,
                    "results": {},
                    "message": "No optimizations enabled",
                }

            return {
                "success": True,
                "total": total,
                "applied": applied,
                "results": results,
            }
        except Exception as e:
            decky.logger.error(f"reapply_all error: {e}")
            return self._unexpected_response(e)

    async def reset_settings(self) -> dict:
        """Delete settings and revert to defaults."""
        try:
            band_cleanup = await self.set_band_policy(BAND_POLICY_OFF)
            if not band_cleanup.get("success"):
                return {
                    "success": False,
                    "error": "band_policy_cleanup_failed",
                    "message": (
                        "Settings were not reset because the managed band policy "
                        "could not be restored safely: "
                        + band_cleanup.get("message", "unknown error")
                    ),
                    "rolled_back": band_cleanup.get("rolled_back", False),
                }
            power_cleanup = await self.set_power_save(False)
            if not power_cleanup.get("success"):
                return {
                    "success": False,
                    "error": "power_save_cleanup_failed",
                    "message": (
                        "Settings were not reset because the managed power-save "
                        "policy could not be restored safely: "
                        + power_cleanup.get("message", "unknown error")
                    ),
                }
            bssid_cleanup = self._restore_legacy_bssid_lock()
            if not bssid_cleanup.get("success"):
                return {
                    "success": False,
                    "error": "bssid_cleanup_failed",
                    "message": (
                        "Settings were not reset because the legacy BSSID lock "
                        "could not be restored safely: "
                        + bssid_cleanup.get("message", "unknown error")
                    ),
                }
            self._remove_dispatcher()
            try:
                os.remove(SETTINGS_FILE)
            except FileNotFoundError:
                pass
            try:
                os.remove(ENFORCED_FILE)
            except FileNotFoundError:
                pass
            try:
                os.remove(BAND_POLICY_JOURNAL_FILE)
            except FileNotFoundError:
                pass
            try:
                os.remove(POWER_SAVE_JOURNAL_FILE)
            except FileNotFoundError:
                pass

            # Repopulate model/driver so the plugin doesn't show as "UNKNOWN /
            # Unsupported device" until the next plugin reload. Mirrors the
            # hardware detection _main does on startup.
            info = await self.get_device_info()
            fresh = dict(DEFAULT_SETTINGS)
            fresh["model"] = info.get("model", "unknown")
            fresh["driver"] = info.get("driver", "unknown")
            fresh["device_family"] = info.get("device_family", "unknown")
            fresh["device_label"] = info.get("device_label", "Unknown Device")
            fresh["chip_label"] = info.get("chip_label", "unknown")
            fresh["supports_6ghz"] = info.get("supports_6ghz", False)
            distro = self._detect_distro()
            fresh["distro_id"] = distro["id"]
            fresh["distro_name"] = distro["name"]
            _save_settings(fresh)

            decky.logger.info("Settings reset to defaults")
            return {"success": True, "message": "Settings reset to defaults"}
        except Exception as e:
            decky.logger.error(f"reset_settings error: {e}")
            return self._unexpected_response(e)

    # ---- Updates ----

    async def set_update_channel(self, channel: str) -> dict:
        """Removed until the fork has a verified release pipeline."""
        return self._deprecated_response("In-plugin updater")

    async def check_for_update(self) -> dict:
        """Removed until the fork has a verified release pipeline."""
        return self._deprecated_response("In-plugin updater")

    async def apply_update(self) -> dict:
        """Removed until the fork has a verified release pipeline."""
        return self._deprecated_response("In-plugin updater")

    # ---- WiFi backend switch (iwd / wpa_supplicant) ----

    async def start_backend_switch(self, backend: str) -> dict:
        """Removed because Go 2 band policy requires the existing iwd backend."""
        return {
            "accepted": False,
            "success": False,
            "reason": "deprecated",
            "message": "WiFi backend switching is not available in the Go 2 build.",
        }

    async def get_backend_switch_status(self) -> dict:
        return {
            "success": False,
            "in_progress": False,
            "phase": "deprecated",
            "target": None,
            "started_at": 0,
            "result": self._deprecated_response("WiFi backend switching"),
        }


def _run_band_policy_rollback_cli(journal_path: str) -> int:
    """Entry point used only by the root-owned transient systemd watchdog."""
    path = os.path.realpath(os.path.abspath(journal_path))
    if os.path.basename(path) != "band-policy-transaction.json":
        return 2
    try:
        journal_stat = os.stat(path)
        parent_stat = os.stat(os.path.dirname(path))
        if os.name == "posix":
            if journal_stat.st_mode & 0o022 or parent_stat.st_mode & 0o022:
                return 2
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                if journal_stat.st_uid != 0 or parent_stat.st_uid != 0:
                    return 2
    except OSError:
        return 2
    plugin = Plugin()
    try:
        file_lock = plugin._acquire_band_policy_file_lock()
    except Exception:
        return 1
    try:
        journal = plugin._load_band_policy_journal(path)
        # The worker may have committed and removed the journal while this
        # timer service was waiting for the lock.
        if not journal:
            return 0
        if journal.get("schema") != 1:
            return 2
        settings_path = os.path.realpath(
            os.path.abspath(str(journal.get("settings_file") or ""))
        )
        if os.path.basename(settings_path) != os.path.basename(SETTINGS_FILE):
            return 2
        # A verified/committed transaction must remain committed even if
        # stopping the timer raced with its activation.
        if journal.get("phase") not in ("pending", "applying", "rolling_back"):
            return 0
        result = plugin._restore_band_policy_transaction(
            journal, "automatic watchdog timeout", journal_path=path
        )
        return 0 if result.get("success") else 1
    finally:
        plugin._release_band_policy_file_lock(file_lock)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--rollback-band-policy":
        raise SystemExit(_run_band_policy_rollback_cli(sys.argv[2]))
    raise SystemExit(2)

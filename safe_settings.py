# SPDX-License-Identifier: BSD-3-Clause
"""Decky's SettingsManager API for this root plugin, on DeckyKit's dk_fs.

dk_fs does the file work: atomic private writes with fsync, symlinked files and folders and anything but a regular
file refused, a .last-good copy, damaged bytes kept as .corrupt-<time>, "lost" when no good copy remains (an empty
file too: this plugin alone writes its stores). This layer keeps what the backends rely on: setSetting() stays in
memory until commit(), a failed commit puts the last saved state back (a refused hardware change can never reach
the disk with the next commit), replace() is all-or-nothing, an unsafe path blocks every later access, the
exceptions the backends catch (UnsafeSettingsPath, CorruptSettings), and the single-file helpers for journals and
system files other modules write.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import threading
from typing import Any

import dk_fs
from diagnostics import event, shown, transition

MAX_SETTINGS_BYTES = 1024 * 1024
LEGACY_BACKUP = ".bak"  # the copy 2.4.0 and older kept: becomes .last-good once
open_directory = dk_fs.open_directory


class UnsafeSettingsPath(RuntimeError):
    """The configured settings path is not a regular file/directory."""


class CorruptSettings(RuntimeError):
    """A settings file exists but does not contain a JSON object."""


def _log(level: str, action: str, *, store: str, error=None) -> None:
    event("settings", action, level=level, store=store, error=error, repeat=60)


def _validate_leaf(name: str) -> str:
    if (not isinstance(name, str) or not name or name in (".", "..")
            or os.path.basename(name) != name or "\x00" in name):
        raise ValueError("settings name must be one safe path component")
    return name


def _validate_directory(directory: str) -> str:
    directory = os.path.abspath(os.fspath(directory))
    if os.path.lexists(directory):
        if stat.S_ISLNK(os.lstat(directory).st_mode):
            raise UnsafeSettingsPath(f"settings directory {os.path.basename(directory)} is a symbolic link")
    else:
        # Resolve only parent components. /home may legitimately map to a persistent volume, but the
        # plugin-specific final component may not be replaced with an attacker-controlled link.
        directory = os.path.join(os.path.realpath(os.path.dirname(directory)), os.path.basename(directory))
    os.makedirs(directory, mode=0o755, exist_ok=True)
    info = os.lstat(directory)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeSettingsPath(f"settings directory {os.path.basename(directory)} is not a real directory")
    return directory


def load_json_object(path: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
    """A bounded regular JSON object, never through a symlink; it never writes (journals, files of others)."""
    try:
        return dk_fs.parse_json(dk_fs.read_bytes(path, MAX_SETTINGS_BYTES, no_links=True), dk_fs.require_object)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except dk_fs.UnsafePath as exc:
        raise UnsafeSettingsPath(str(exc)) from exc
    except dk_fs.Damaged as exc:
        raise CorruptSettings(f"invalid JSON settings: {exc}") from exc


def _atomic_write_bytes(path: str, payload: bytes, mode: int = 0o600, *,
                        keep_existing: bool = False, strict_sync: bool = False) -> None:
    """Atomically replace a regular file.

    `keep_existing` keeps the mode and owner of the file being replaced (system files such as /etc/iwd/main.conf);
    `strict_sync` makes a failed directory fsync an error instead of a warning (recovery journals).
    """
    _validate_directory(os.path.dirname(path))
    _validate_leaf(os.path.basename(path))
    if keep_existing and os.path.lexists(path):
        mode = None
    try:
        confirmed = dk_fs.write_atomic(path, payload, mode, no_links=True, strict_sync=strict_sync)
    except dk_fs.UnsafePath as exc:
        raise UnsafeSettingsPath(str(exc)) from exc
    if not confirmed:
        # The new file is visible and its bytes were fsynced: raising here would roll hardware back against it.
        _log("warning", "directory_durability_unconfirmed", store=os.path.basename(path))


def _encode_json_object(payload: dict[str, Any]) -> bytes:
    if not isinstance(payload, dict):
        raise TypeError("settings root must be an object")
    encoded = (json.dumps(payload, indent=4, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_SETTINGS_BYTES:
        raise ValueError(f"settings exceed {MAX_SETTINGS_BYTES} bytes")
    try:
        dk_fs.parse_json(encoded, dk_fs.require_object)  # never write what the reader would refuse
    except dk_fs.Damaged as exc:
        raise ValueError(f"settings cannot be stored: {shown(exc)}") from exc
    return encoded


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    _atomic_write_bytes(path, _encode_json_object(payload))


class AtomicSettingsManager:
    """Small SettingsManager-compatible store with atomic explicit commits."""

    def __init__(self, name: str, settings_directory: str) -> None:
        leaf = _validate_leaf(name) + ".json"
        self._store_name = name
        self.settings: dict[str, Any] = {}
        self._committed_settings: dict[str, Any] = {}
        self._storage_hardened = False
        self._lock = threading.RLock()
        self._blocked_reason = ""
        # Missing settings are a first install; corrupt settings with no usable recovery copy are lost user
        # intent. Consumers such as module gating must be able to distinguish those cases before applying defaults.
        self._recovery_error = ""
        try:
            self.directory = _validate_directory(settings_directory)
            self.path = os.path.join(self.directory, leaf)
            self.backup_path = self.path + dk_fs.LAST_GOOD
            self.read()
        except UnsafeSettingsPath as exc:
            # Keep the module importable so the other companion modules can run,
            # but fail every later access closed instead of touching the path.
            self.directory = os.path.abspath(os.fspath(settings_directory))
            self.path = os.path.join(self.directory, leaf)
            self.backup_path = self.path + dk_fs.LAST_GOOD
            self._blocked_reason = str(exc)
            _log("error", "unsafe_storage", store=self._store_name, error=exc)

    def _ensure_safe(self) -> None:
        if self._blocked_reason:
            raise UnsafeSettingsPath(self._blocked_reason)

    @property
    def recovery_error(self) -> str:
        """Why all saved copies were lost; empty for new or recovered storage."""
        # An immutable string published under the writer's lock: admission and status checks read it without
        # waiting for a commit's fsync, which would stall Decky's event loop while storage is slow.
        return self._recovery_error

    def _adopt_legacy_backup(self) -> None:
        """2.4.0 and older kept <file>.bak: it becomes the .last-good copy, so a damaged file right after the update
        still has its recovery copy (and a damaged .bak still tells lost intent from a first install); after a
        downgrade and an update again a newer, good .bak replaces the copy."""
        legacy = self.path + LEGACY_BACKUP
        try:
            info = os.lstat(legacy)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeSettingsPath(f"{os.path.basename(legacy)} is not a regular file")
        try:
            if os.lstat(self.backup_path).st_mtime >= info.st_mtime:
                return
            load_json_object(legacy)  # a damaged old copy never replaces an existing one
        except FileNotFoundError:
            pass
        except CorruptSettings:
            return
        os.replace(legacy, self.backup_path)

    def read(self) -> None:
        with self._lock:
            self._ensure_safe()
            try:
                self._adopt_legacy_backup()
                # The store is made private once per process, not on every read (backends read on most RPCs).
                loaded = dk_fs.load_json(self.path, {}, MAX_SETTINGS_BYTES, validate=dk_fs.require_object,
                                         no_links=True, mode=None if self._storage_hardened else 0o600,
                                         empty_is_missing=False)
            except dk_fs.UnsafePath as exc:
                raise UnsafeSettingsPath(str(exc)) from exc
            except OSError as exc:
                # A read-only or failing filesystem must not make the module disappear: the next read retries.
                _log("warning", "storage_read_failed", store=self._store_name, error=exc)
                raise
            if loaded.status == "recovered":
                _log("warning", "backup_restored", store=self._store_name, error=loaded.detail)
            elif loaded.status == "lost":
                _log("warning", "primary_corrupt", store=self._store_name, error=loaded.detail)
            self._storage_hardened = True
            if loaded.status == "lost":
                self._recovery_error = f"No usable settings copy remains ({loaded.detail})."
            elif loaded.status != "missing":
                # A lost store whose file was then deleted stays lost in this process: only a good file or a commit
                # (the user's explicit repair) clears it, never a missing file read as a first install.
                self._recovery_error = ""
            self.settings = loaded.value
            self._committed_settings = copy.deepcopy(loaded.value)

    def getSetting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._ensure_safe()
            return self.settings.get(key, default)

    def setSetting(self, key: str, value: Any) -> None:
        with self._lock:
            self._ensure_safe()
            self.settings[key] = copy.deepcopy(value)

    def commit(self) -> None:
        with self._lock:
            self._ensure_safe()
            snapshot = copy.deepcopy(self.settings)
            try:
                if not isinstance(snapshot, dict):
                    raise TypeError("settings root must be an object")
                refreshed = dk_fs.write_json(self.path, snapshot, 0o600, limit=MAX_SETTINGS_BYTES,
                                             validate=dk_fs.require_object, no_links=True, folder_mode=0o755)
            except Exception as exc:
                # A failed user operation must not remain visible through getSetting() when the durable file still
                # holds the previous transaction. This also makes a later unrelated commit unable to smuggle the
                # rejected values onto disk.
                self.settings = copy.deepcopy(self._committed_settings)
                _log("error", "primary_commit_failed", store=self._store_name, error=exc)
                if isinstance(exc, dk_fs.UnsafePath):
                    raise UnsafeSettingsPath(str(exc)) from exc
                raise
            # The snapshot already owns a deep copy, separate from live settings.
            self._committed_settings = snapshot
            self._recovery_error = ""
            # The primary transaction is durable either way: a failed copy is never a failed user operation.
            transition("settings", "backup_refresh", refreshed, level="info" if refreshed else "warning",
                       store=self._store_name, outcome="confirmed" if refreshed else "failed", initial=False)

    def replace(self, payload: dict[str, Any]) -> None:
        """Atomically replace the complete object in one durable commit."""
        if not isinstance(payload, dict):
            raise TypeError("settings root must be an object")
        with self._lock:
            self._ensure_safe()
            previous = copy.deepcopy(self.settings)
            self.settings = copy.deepcopy(payload)
            try:
                self.commit()
            except Exception:
                self.settings = previous
                raise


# Drop-in name for the imported backends.
SettingsManager = AtomicSettingsManager

# SPDX-License-Identifier: BSD-3-Clause
"""Crash-safe settings storage for root Decky plugins.

Decky's SettingsManager writes directly to the destination file and commits on
every setSetting() call.  This compatible replacement deliberately keeps
setSetting() memory-only and commits the complete object with an atomic rename.
It also refuses non-regular files so a user-writable settings directory cannot
turn a root plugin write into a symlink-following overwrite elsewhere.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import stat
import threading
import time
from typing import Any


MAX_SETTINGS_BYTES = 1024 * 1024
MAX_SETTINGS_DEPTH = 64


class UnsafeSettingsPath(RuntimeError):
    """The configured settings path is not a regular file/directory."""


class CorruptSettings(RuntimeError):
    """A settings file exists but does not contain a JSON object."""


def _log(level: str, message: str) -> None:
    try:
        import decky

        logger = getattr(decky, "logger", None)
        writer = getattr(logger, level, None)
        if callable(writer):
            writer(f"[legiongo2companion-settings] {message}")
    except Exception:
        pass


def _validate_leaf(name: str) -> str:
    if (not isinstance(name, str) or not name or name in (".", "..")
            or os.path.basename(name) != name or "\x00" in name):
        raise ValueError("settings name must be one safe path component")
    return name


def _validate_directory(directory: str) -> str:
    directory = os.path.abspath(os.fspath(directory))
    if os.path.lexists(directory):
        initial = os.lstat(directory)
        if stat.S_ISLNK(initial.st_mode):
            raise UnsafeSettingsPath(
                f"settings directory is a symbolic link: {directory}")
    else:
        # Resolve only parent components.  /home may legitimately map to a
        # persistent volume, but the plugin-specific final component may not be
        # replaced with an attacker-controlled link.
        directory = os.path.join(
            os.path.realpath(os.path.dirname(directory)),
            os.path.basename(directory),
        )
    os.makedirs(directory, mode=0o755, exist_ok=True)
    info = os.lstat(directory)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeSettingsPath(f"settings directory is not a real directory: {directory}")
    return directory


def _open_readonly_nofollow(path: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError:
        # O_NOFOLLOW is the authoritative Linux check.  lstat below supplies a
        # portable pre-check on hosts that do not expose it.
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise UnsafeSettingsPath(f"settings path is a symbolic link: {path}")
        raise


def _harden_regular_file(path: str) -> None:
    """Make an existing settings file private without following a symlink."""
    if os.name != "posix":
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise UnsafeSettingsPath(
                f"settings path is not a regular file: {path}")
        return
    fd = _open_readonly_nofollow(path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeSettingsPath(
                f"settings path is not a regular file: {path}")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _validate_json_depth(payload: dict[str, Any]) -> None:
    # JSON's decoder can accept structures deep enough to exhaust deepcopy()
    # later. Bound container nesting without recursively walking untrusted data.
    containers = [iter((payload,))]
    while containers:
        try:
            value = next(containers[-1])
        except StopIteration:
            containers.pop()
            continue
        if isinstance(value, dict):
            children = value.values()
        elif isinstance(value, list):
            children = value
        else:
            continue
        if len(containers) > MAX_SETTINGS_DEPTH:
            raise CorruptSettings(f"settings nesting exceeds {MAX_SETTINGS_DEPTH} containers")
        containers.append(iter(children))


def load_json_object(path: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
    """Read a bounded regular JSON object without following a final symlink."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UnsafeSettingsPath(f"settings path is not a regular file: {path}")
    if info.st_size > MAX_SETTINGS_BYTES:
        raise CorruptSettings(f"settings file exceeds {MAX_SETTINGS_BYTES} bytes")

    fd = _open_readonly_nofollow(path)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise UnsafeSettingsPath(f"opened settings path is not regular: {path}")
        if opened.st_size > MAX_SETTINGS_BYTES:
            raise CorruptSettings(f"settings file exceeds {MAX_SETTINGS_BYTES} bytes")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            encoded = handle.read(MAX_SETTINGS_BYTES + 1)
            if len(encoded) > MAX_SETTINGS_BYTES:
                raise CorruptSettings(f"settings file exceeds {MAX_SETTINGS_BYTES} bytes")
            payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise CorruptSettings(f"invalid JSON settings: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(payload, dict):
        raise CorruptSettings("settings root is not an object")
    _validate_json_depth(payload)
    return payload


def _atomic_write_bytes(path: str, payload: bytes, mode: int = 0o600) -> None:
    directory = _validate_directory(os.path.dirname(path))
    leaf = _validate_leaf(os.path.basename(path))
    temporary = f".{leaf}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    if os.name != "posix":
        # Windows is only a development/test host for this plugin.  It does not
        # implement the dir_fd variants below, but replace within the same real
        # directory still gives the crash-safety property the tests exercise.
        temporary_path = os.path.join(directory, temporary)
        try:
            try:
                existing = os.lstat(path)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise UnsafeSettingsPath(
                    f"settings target is not a regular file: {path}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(temporary_path, flags, mode)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
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
            existing = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise UnsafeSettingsPath(f"settings target is not a regular file: {path}")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        tmp_fd = os.open(temporary, flags, mode, dir_fd=dir_fd)
        os.fchmod(tmp_fd, mode)
        owner = os.fstat(dir_fd)
        if hasattr(os, "fchown"):
            try:
                os.fchown(tmp_fd, owner.st_uid, owner.st_gid)
            except PermissionError:
                # Non-root unit tests cannot chown, while the root Decky process
                # can and does preserve the settings-directory owner.
                pass
        with os.fdopen(tmp_fd, "wb") as handle:
            tmp_fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary,
            leaf,
            src_dir_fd=dir_fd,
            dst_dir_fd=dir_fd,
        )
        try:
            os.fsync(dir_fd)
        except OSError as exc:
            # The new primary is already visible and its bytes were fsynced.
            # Raising here would falsely restore the old in-memory settings
            # and invite callers to roll hardware back against the new file.
            _log("warning", f"settings replaced but directory durability could not be confirmed: {exc}")
    finally:
        if tmp_fd >= 0:
            os.close(tmp_fd)
        try:
            os.unlink(temporary, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        os.close(dir_fd)


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise TypeError("settings root must be an object")
    _validate_json_depth(payload)
    encoded = (json.dumps(payload, indent=4, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_SETTINGS_BYTES:
        raise ValueError(f"settings exceed {MAX_SETTINGS_BYTES} bytes")
    _atomic_write_bytes(path, encoded)


class AtomicSettingsManager:
    """Small SettingsManager-compatible store with atomic explicit commits."""

    def __init__(self, name: str, settings_directory: str) -> None:
        leaf = _validate_leaf(name) + ".json"
        self.directory = _validate_directory(settings_directory)
        self.path = os.path.join(self.directory, leaf)
        self.backup_path = self.path + ".bak"
        self.settings: dict[str, Any] = {}
        self._committed_settings: dict[str, Any] = {}
        self._storage_hardened = False
        self._lock = threading.RLock()
        self._blocked_reason = ""
        # Missing settings are a first install; corrupt settings with no usable
        # recovery copy are lost user intent. Consumers such as module gating
        # must be able to distinguish those cases before applying defaults.
        self._recovery_error = ""
        try:
            self.read()
        except UnsafeSettingsPath as exc:
            # Keep the module importable so the other companion modules can run,
            # but fail every later access closed instead of touching the path.
            self._blocked_reason = str(exc)
            _log("error", self._blocked_reason)

    def _ensure_safe(self) -> None:
        if self._blocked_reason:
            raise UnsafeSettingsPath(self._blocked_reason)

    @property
    def recovery_error(self) -> str:
        """Why all saved copies were lost; empty for new or recovered storage."""
        with self._lock:
            return self._recovery_error

    def _quarantine_corrupt_primary(self) -> None:
        try:
            info = os.lstat(self.path)
            if not stat.S_ISREG(info.st_mode):
                raise UnsafeSettingsPath(
                    f"settings path is not a regular file: {self.path}")
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            destination = f"{self.path}.corrupt-{stamp}-{secrets.token_hex(3)}"
            os.replace(self.path, destination)
            _log("warning", f"quarantined corrupt settings as {destination}")
        except FileNotFoundError:
            pass

    def read(self) -> None:
        with self._lock:
            self._ensure_safe()
            corrupt_copies = []
            try:
                payload = load_json_object(self.path, missing_ok=True)
                if payload is not None:
                    self.settings = payload
                    self._committed_settings = copy.deepcopy(payload)
                    self._recovery_error = ""
                    if not self._storage_hardened:
                        try:
                            _harden_regular_file(self.path)
                            if os.path.lexists(self.backup_path):
                                _harden_regular_file(self.backup_path)
                            else:
                                # A valid file inherited from the standalone
                                # plugin may never be committed during startup.
                                # Give it recovery immediately, not only after a
                                # user changes a setting.
                                atomic_write_json(self.backup_path, payload)
                            self._storage_hardened = True
                        except UnsafeSettingsPath:
                            raise
                        except OSError as exc:
                            # The primary data was read successfully. A read-only
                            # or full filesystem should not make the module
                            # disappear; retry later and report on the next write.
                            _log("warning", f"could not harden settings storage: {exc}")
                    return
            except UnsafeSettingsPath:
                raise
            except CorruptSettings as exc:
                corrupt_copies.append(f"primary: {exc}")
                _log("warning", f"primary settings are corrupt: {exc}")

            try:
                backup = load_json_object(self.backup_path, missing_ok=True)
            except UnsafeSettingsPath:
                raise
            except CorruptSettings as exc:
                corrupt_copies.append(f"backup: {exc}")
                _log("warning", f"backup settings are corrupt: {exc}")
                backup = None

            if backup is None and corrupt_copies:
                self._recovery_error = "No usable settings copy remains (" + "; ".join(corrupt_copies) + ")."
            # Retain an unrecoverable primary so a later process still sees
            # lost intent, rather than interpreting quarantine as first install.
            if backup is not None and os.path.lexists(self.path):
                self._quarantine_corrupt_primary()
            self.settings = backup if backup is not None else {}
            if backup is not None:
                atomic_write_json(self.path, self.settings)
                self._recovery_error = ""
                _log("warning", "restored settings from the last good backup")
            self._committed_settings = copy.deepcopy(self.settings)

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
                atomic_write_json(self.path, snapshot)
            except Exception:
                # A failed user operation must not remain visible through
                # getSetting() when the durable file still holds the previous
                # transaction.  This also makes a later unrelated commit unable
                # to smuggle the rejected values onto disk.
                self.settings = copy.deepcopy(self._committed_settings)
                raise
            self._committed_settings = copy.deepcopy(snapshot)
            self._recovery_error = ""
            try:
                atomic_write_json(self.backup_path, snapshot)
                self._storage_hardened = True
            except Exception as exc:
                # The primary transaction is already durable.  Reporting this as
                # a failed user operation would invite a hardware rollback even
                # though the requested value was committed successfully.
                _log("warning", f"could not refresh settings backup: {exc}")

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


# Drop-in name for the three imported backends.
SettingsManager = AtomicSettingsManager

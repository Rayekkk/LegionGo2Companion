# SPDX-License-Identifier: BSD-3-Clause
"""Cheap process-exit hints; D-Bus/device ownership still authorizes every repair."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Callable, Any


PROC_ROOT = Path("/proc")
SERVICE = "org.shadowblip.InputPlumber"
_OWNER = re.compile(r"^:[0-9]+\.[0-9]+$")


def _read_process(pid: int) -> tuple[str, int] | None:
    try:
        with (PROC_ROOT / str(pid) / "stat").open(encoding="ascii") as handle:
            raw = handle.read(4097)
        if len(raw) > 4096 or not raw.startswith(f"{pid} ("):
            return None
        # comm can contain spaces and parentheses; split after its final ')'.
        fields = raw[raw.rindex(")") + 1:].split()
        if len(fields) < 20 or fields[0] not in {"R", "S", "D", "Z", "T", "t", "X", "x", "K", "W", "P", "I"}:
            return None
        started = int(fields[19])  # field 22; fields[0] is process state (3).
        return (fields[0], started) if started >= 0 else None
    except FileNotFoundError:
        return ("missing", 0) if PROC_ROOT.is_dir() else None
    except (OSError, UnicodeError, ValueError):
        return None


class ProcessWatch:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revision = 0
        self._token: tuple[str, int, int] | None = None

    def clear(self) -> None:
        with self._lock:
            self._revision += 1
            self._token = None

    def capture(self, query: Callable[[list[str]], Any], owner: str | None = None) -> None:
        """Call only after a successful operation, never from a status query.

        Missing metadata degrades to the existing slow watchdog. An in-flight
        capture cannot revive a cleared token or supersede a newer capture.
        """
        with self._lock:
            if self._token and (owner is None or owner == self._token[0]):
                return
            # Reserve this capture before I/O: neither an older capture nor a
            # tick checking its old token may supersede the new observation.
            self._revision += 1
            revision = self._revision
            self._token = None
        token = None
        try:
            if not PROC_ROOT.is_dir():
                return
            prefix = ["call", "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus"]
            if owner is None:
                result = query([*prefix, "GetNameOwner", "s", SERVICE])
                owner = result[0] if isinstance(result, list) and len(result) == 1 else None
            if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
                return
            result = query([*prefix, "GetConnectionUnixProcessID", "s", owner])
            if (isinstance(result, list) and len(result) == 1
                    and type(result[0]) is int and 0 < result[0] <= 0xffffffff):
                pid = result[0]
                process = _read_process(pid)
                if process is not None and process[0] not in {"missing", "Z", "X", "x"}:
                    token = (owner, pid, process[1])
        except Exception:
            # A successful hardware operation must not fail on optional metadata.
            pass
        with self._lock:
            if self._revision == revision:
                self._token = token

    def consume_exit(self) -> bool:
        """Return one hint per dead process, with no subprocess or D-Bus calls."""
        with self._lock:
            token, revision = self._token, self._revision
        if token is None:
            return False
        process = _read_process(token[1])
        if process is None or (process[0] not in {"missing", "Z", "X", "x"} and process[1] == token[2]):
            return False
        with self._lock:
            if self._revision != revision or self._token != token:
                return False
            self._revision += 1
            self._token = None
            return True

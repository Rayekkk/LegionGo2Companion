# SPDX-License-Identifier: BSD-3-Clause
"""Shared, expiring frontend game reports with an independent Steam process fallback."""
import glob
import re

APP_LEASE_SECONDS = 12.0
DEFAULT_APP = "0"
APP_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def normalise_app_id(value, *, allow_empty=False, allow_default=True):
    """A Steam app id (or a short custom name) as a string, else None. `allow_empty` turns None/"" into ""."""
    if allow_empty and value in (None, ""):
        return ""
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or APP_ID_RE.fullmatch(value) is None:
        return None
    if not allow_default and value == DEFAULT_APP:
        return None
    return value


def resolve_running_app(app_id, reported_at, now, fallback, ttl=APP_LEASE_SECONDS):
    if reported_at is not None and 0 <= now - reported_at < ttl:
        return app_id if app_id != "0" else ""
    return fallback()


def scan_proc_for_appid():
    # Steam's reaper is visible outside pressure-vessel. Prefer its launch id
    # over the less specific environment signal used as a fallback.
    for pattern, key in (("/proc/*/cmdline", b"AppId="),
                         ("/proc/*/environ", b"SteamAppId=")):
        for path in glob.glob(pattern):
            try:
                with open(path, "rb") as source:
                    for item in source.read().split(b"\x00"):
                        if item.startswith(key):
                            value = item[len(key):].decode(errors="replace")
                            if value.isascii() and value.isdecimal() and value != "0":
                                return value
            except OSError:
                continue
    return ""

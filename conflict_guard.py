# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Read-only detection of the three public standalone Companion predecessors."""

import json
import os
import stat
from diagnostics import event


PLUGIN_NAMES = {
    "legotdp": "LeGoTDP",
    "legovibecontrol": "LeGo Vibe Control",
    "lego2brightnessfix": "LeGo2 Brightness Fix",
}
CHECK_INTERVAL_S = 5.0


def _identity(value):
    return "".join(c for c in value.casefold() if c.isalnum()) if isinstance(value, str) else ""


def _manifest_name(entry):
    try:
        fd = os.open(os.path.join(entry.path, "plugin.json"),
                     os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("A plugin manifest is not a regular file.")
        with os.fdopen(fd, encoding="utf-8-sig") as source:
            fd = None
            text = source.read(65537)
    finally:
        if fd is not None:
            os.close(fd)
    if len(text) > 65536:
        raise ValueError("A plugin manifest is too large to verify safely.")
    try:
        manifest = json.loads(text)
    except RecursionError as exc:
        raise ValueError("A plugin manifest is nested too deeply to verify safely.") from exc
    if not isinstance(manifest, dict):
        raise ValueError("A plugin manifest is not a JSON object.")
    return PLUGIN_NAMES.get(_identity(manifest.get("name")), "")


def installed_conflicts(plugin_dir):
    """Find the three known predecessors; unrelated metadata is not ownership.

    Canonical directories count even during an incomplete installation. Valid
    manifests also identify renamed copies. One malformed sibling is reported
    locally and cannot shut down unrelated hardware modules. Failure to inspect
    the plugin directory itself still propagates to the admission gate.
    """
    installed_path = os.path.abspath(plugin_dir)
    current = os.path.realpath(installed_path)
    result = set()
    with os.scandir(os.path.dirname(installed_path)) as entries:
        for entry in entries:
            try:
                if os.path.realpath(entry.path) == current:
                    continue
                name = PLUGIN_NAMES.get(_identity(entry.name))
                if name and (entry.is_symlink() or entry.is_dir()):
                    result.add(name)
                    continue
                if not entry.is_dir():
                    continue
                name = _manifest_name(entry)
            except (OSError, ValueError, TypeError) as exc:
                event("plugin", "sibling_manifest_unreadable", level="warning", error=exc,
                      scope=entry.name, outcome="skipped", repeat=60)
                continue
            if name:
                result.add(name)
    return sorted(result)

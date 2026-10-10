# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Read-only detection of the three public standalone Companion predecessors."""

import json
import os
import stat


PLUGIN_NAMES = {
    "legotdp": "LeGoTDP",
    "legovibecontrol": "LeGo Vibe Control",
    "lego2brightnessfix": "LeGo2 Brightness Fix",
}
CHECK_INTERVAL_S = 5.0


def _identity(value):
    return "".join(c for c in value.casefold() if c.isalnum()) if isinstance(value, str) else ""


def installed_conflicts(plugin_dir):
    """Inspect siblings, including renamed/symlinked installs, never settings dirs.

    A directory with the canonical name already counts during an incomplete install.
    Unknown directories without a manifest do not. Unreadable/malformed manifests
    fail closed: their identity cannot safely be ruled out while Decky is installing.
    """
    installed_path = os.path.abspath(plugin_dir)
    current = os.path.realpath(installed_path)
    result = set()
    # A development install may link Companion to an external checkout. Its
    # installed siblings are still in Decky's plugin directory, not next to
    # the resolved checkout. Resolve only identities when excluding ourselves.
    with os.scandir(os.path.dirname(installed_path)) as entries:
        for entry in entries:
            if os.path.realpath(entry.path) == current:
                continue
            name = PLUGIN_NAMES.get(_identity(entry.name))
            if name and (entry.is_dir() or entry.is_symlink()):
                result.add(name)
                continue
            if not entry.is_dir():
                continue
            try:
                fd = os.open(os.path.join(entry.path, "plugin.json"),
                             os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
            except FileNotFoundError:
                continue
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("A plugin manifest is not a regular file.")
                with os.fdopen(fd, encoding="utf-8-sig") as f:
                    fd = None  # The file object owns this descriptor now.
                    text = f.read(65537)
            finally:
                if fd is not None:
                    os.close(fd)
            if len(text) > 65536:
                raise ValueError("A plugin manifest is too large to verify safely.")
            try:
                manifest = json.loads(text)
            except RecursionError as exc:
                # A bounded manifest can still exceed the decoder's nesting
                # limit. Report an unverifiable identity to the admission gate
                # instead of terminating its periodic watcher.
                raise ValueError("A plugin manifest is nested too deeply to verify safely.") from exc
            if not isinstance(manifest, dict):
                raise ValueError("A plugin manifest is not a JSON object.")
            name = PLUGIN_NAMES.get(_identity(manifest.get("name")))
            if name:
                result.add(name)
    return sorted(result)

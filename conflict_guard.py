# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Read-only detection of the three public standalone Companion predecessors."""

import json
import os


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
    current = os.path.realpath(plugin_dir)
    result = set()
    with os.scandir(os.path.dirname(current)) as entries:
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
                with open(os.path.join(entry.path, "plugin.json"), encoding="utf-8-sig") as f:
                    text = f.read(65537)
            except FileNotFoundError:
                continue
            if len(text) > 65536:
                raise ValueError("A plugin manifest is too large to verify safely.")
            manifest = json.loads(text)
            if not isinstance(manifest, dict):
                raise ValueError("A plugin manifest is not a JSON object.")
            name = PLUGIN_NAMES.get(_identity(manifest.get("name")))
            if name:
                result.add(name)
    return sorted(result)

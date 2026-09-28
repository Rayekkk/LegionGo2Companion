# SPDX-License-Identifier: BSD-3-Clause
"""Detect native Legion Go 2 support by the installed system profile alone.

The profile may change as gamescope improves panel support. Its contents and
compositor binaries are deliberately not inspected. Session restart handling
belongs to display_backend, since file presence cannot prove a running
compositor has loaded it.
"""
import os
import stat


SYSTEM_SCRIPT = "/usr/share/gamescope/scripts/00-gamescope/displays/lenovo.legiongo2.oled.lua"


def detect_native_display_support() -> dict:
    """Check file presence without reading or executing Lua or gamescope."""
    try:
        info = os.stat(SYSTEM_SCRIPT)
        if stat.S_ISREG(info.st_mode):
            state, reason = "supported", "system_profile_present"
        else:
            state, reason = "inconclusive", "system_profile_not_file"
    except FileNotFoundError:
        state, reason = "absent", "system_profile_missing"
    except OSError:
        # An inaccessible path does not establish that support was removed.
        state, reason = "inconclusive", "system_profile_inaccessible"
    return {
        "status": state,
        "supported": state == "supported",
        "reason": reason,
        "script_path": SYSTEM_SCRIPT,
    }

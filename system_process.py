# SPDX-License-Identifier: BSD-3-Clause
"""Environment for external Linux programs launched by frozen Decky Python."""

import os


def system_env(**extra) -> dict:
    # System binaries must not link Decky's bundled OpenSSL/readline libraries.
    # Keep the parent's environment untouched, including its own Python runtime.
    blocked = {"LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG", "LD_PRELOAD", "LD_AUDIT"}
    env = {key: value for key, value in os.environ.items() if key not in blocked}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env.update(extra)
    return env

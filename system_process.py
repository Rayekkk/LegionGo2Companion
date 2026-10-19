# SPDX-License-Identifier: BSD-3-Clause
"""Environment for external Linux programs launched by frozen Decky Python."""

import os
import subprocess
import sys
import time

PYTHON = sys.executable if os.name == "nt" else "/usr/bin/python3"
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def system_env(**extra) -> dict:
    # System binaries must not link Decky's bundled OpenSSL/readline libraries.
    # Keep the parent's environment untouched, including its own Python runtime.
    blocked = {"LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG", "LD_PRELOAD", "LD_AUDIT"}
    env = {key: value for key, value in os.environ.items() if key not in blocked}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env.update(extra)
    return env


def run(argv, timeout, *, env=None, stdin=subprocess.DEVNULL, check=False, **kwargs):
    """The one way to run a short external program: subprocess.run, which kills the child and
    re-raises TimeoutExpired at `timeout`. Default env: system libraries with LC_ALL=C."""
    return subprocess.run(argv, stdin=stdin, timeout=timeout, check=check,
                          env=system_env(LC_ALL="C") if env is None else env, **kwargs)


def run_helper(args, cancelled, seconds: float, stopped: Exception):
    """(returncode, stdout) of `python -I args`, raising `stopped` on cancel or the wall deadline."""
    deadline = time.monotonic() + seconds
    worker = subprocess.Popen([PYTHON, "-I", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env=system_env(), **NO_WINDOW)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if cancelled.is_set() or remaining <= 0:
                raise stopped
            try:
                data, _ = worker.communicate(timeout=min(.2, remaining))
            except subprocess.TimeoutExpired:
                continue
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise stopped
            return worker.returncode, data
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=3)

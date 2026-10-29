# SPDX-License-Identifier: BSD-3-Clause
# Shared from DeckyKit (shared/backend/dk_process.py): change it there, then `kit sync`.

"""The one way a plugin backend runs another program.

Sources: LegionGo2Companion/system_process.py (system_env, run, run_helper; Ayaneo3Companion has an older copy),
DeckyEQ/deckyeq/worker.py (parent-death signal for long-lived children), DeckyLS (UTF-8 locale and decoding),
DeckyVibranceHDR vibrance_host.py/vibrance_lutfile.py (the host's native programs when Decky runs emulated, no full
fork of a large emulated process, the helper's error output).

Guarantees:
- Children get the system's libraries: LD_LIBRARY_PATH(_ORIG), LD_PRELOAD and LD_AUDIT of Decky's frozen bundle
  are removed (they make system programs load the wrong OpenSSL or readline); everything else is passed on, and a
  PATH exists even when Decky's has none. The plugin's own environment is never changed.
- run(): always a timeout (the child is killed and TimeoutExpired raised), stdin from /dev/null, never a shell
  (argv list only), LC_ALL=C.UTF-8 (English messages, UTF-8 names intact), output as str with undecodable bytes
  replaced (text=False or bytes input keep bytes, for callers moving from system_process.run), no console window
  on Windows (a caller's own creationflags are kept, not a duplicate-argument TypeError). Everything else is subprocess.run's (Ayaneo3Companion relies on it): a non-zero exit is returned,
  check=True raises CalledProcessError, a given env is used as it is, input/cwd/user/group reach the child.
- run_helper(): `python -I` helper with a wall-clock deadline and a cancel flag; on either the child is killed and
  reaped, so no thread of Decky's executor waits on it (DNS lookups, slow scripts). The stop is reported at most
  REAP_SECONDS after it: a killed child the system has not let go of by then (a loaded machine) is reaped by a
  background thread, never a TimeoutExpired in place of the stop or the result. It answers (returncode, stdout,
  stderr), the streams as bytes when piped. On POSIX it starts through posix_spawn (close_fds=False: Python's own
  descriptors are not inheritable), never a full fork of Decky's process, which is slow when that process is
  emulated.
- Emulated Decky (an x86-64 Decky under FEX on an ARM handheld, e.g. Odin 3): emulated() tells, and host_path()
  turns a system program's path into the host's own native binary (/proc/self/root/...), past the emulator's view
  of /. Helpers and spawn() then run the host's native Python; a normal machine is not affected.
- spawn(): a long-lived child that gets SIGTERM when the thread that spawned it ends (PR_SET_PDEATHSIG, set before
  exec): call it from the event loop's thread, never from asyncio.to_thread, and nothing outlives Decky. If that
  thread is already gone, the child does not start at all. The program is resolved to an absolute path with the
  child's PATH before exec (execve, no PATH lookup in the wrapper). spawn_argv() gives the same wrapped argv to a
  caller that starts the child itself (asyncio.create_subprocess_exec, from the event loop's thread: DeckyEQ reads
  the output of its PipeWire filter and `pactl subscribe` with asyncio).
- session_env() (DeckyEQ runtime.py): for a user plugin talking to the user's session (PipeWire, PulseAudio,
  D-Bus), system_env() plus HOME of the running user and XDG_RUNTIME_DIR=/run/user/<uid>, set only when that folder
  is this user's own; a session that is not up yet leaves it unset, so the caller can build the env again later.

Deliberately not here: shell pipelines, sudo (the plugin already runs with the rights it has), killing by name, an
asyncio run() (DeckyEQ keeps its own: bounded output, kill and drain on cancel), restoring LD_LIBRARY_PATH_ORIG
(the user's own library path is not a system program's either), a bounded wait after killing a timed-out child
(subprocess.run waits for it, so plugins' tests can patch subprocess.run; a program that can hang in the kernel,
like Ayaneo3Companion's ryzenadj, keeps its own Popen with kill and a 2 s reap), detached children that must
outlive the plugin (Ayaneo3Companion's fan guardian: start_new_session, its own Popen).
"""

import functools
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

PYTHON = sys.executable if os.name == "nt" else "/usr/bin/python3"  # Decky's frozen Python cannot run scripts
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
_BLOCKED = {"LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG", "LD_PRELOAD", "LD_AUDIT"}
PR_SET_PDEATHSIG = 1  # <linux/prctl.h>
HOST_ROOT = "/proc/self/root"  # the host's own /, past an emulator's (FEX's) view of it
_ELF_MACHINE = slice(18, 20)  # e_machine in an ELF header (little-endian)
REAP_SECONDS = 3  # run_helper's wait for a killed child; with the 0.2 s poll well inside Decky's 5 s unload


@functools.cache
def _machines():
    """(host python3, running interpreter) ELF machine types; OSError when either cannot be read (not cached)."""
    found = []
    for path in (HOST_ROOT + "/usr/bin/python3", sys.executable):
        with open(path, "rb") as f:
            header = f.read(20)
        if header[:4] != b"\x7fELF" or len(header) < 20:
            raise OSError(f"{os.path.basename(path)} is not an ELF program")
        found.append(int.from_bytes(header[_ELF_MACHINE], "little"))
    return tuple(found)


def emulated():
    """True when this process runs under an emulator for another CPU than the host's (Decky under FEX on ARM)."""
    try:
        host, running = _machines()
    except OSError:
        return False
    return host != running


def host_path(path):
    """The host's own copy of an absolute system program: under emulation the emulator's / holds copies for the
    emulated CPU (slow, or missing), so the native one is reached through /proc/self/root."""
    return HOST_ROOT + path if emulated() and path.startswith("/") else path  # a Linux path, also on a Windows dev box


def system_env(**extra):
    """os.environ without the loader variables of Decky's bundle, plus extra."""
    env = {key: value for key, value in os.environ.items() if key not in _BLOCKED}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env.update(extra)
    return env


def session_env(**extra):
    """system_env() with the running user's HOME and, when it is theirs, XDG_RUNTIME_DIR (POSIX), plus extra."""
    env = system_env()
    if hasattr(os, "geteuid"):
        import pwd
        uid = os.geteuid()
        env["HOME"] = pwd.getpwuid(uid).pw_dir
        runtime = f"/run/user/{uid}"
        try:
            info = os.stat(runtime)
            if stat.S_ISDIR(info.st_mode) and info.st_uid == uid:
                env["XDG_RUNTIME_DIR"] = runtime
        except OSError:
            pass  # the user's session is not up yet: no runtime folder to give
    env.update(extra)
    return env


def run(argv, timeout, *, env=None, **kwargs):
    """subprocess.run(argv) with a timeout, no stdin and system libraries. Default env: LC_ALL=C.UTF-8. Output is
    str, decoded as UTF-8 with undecodable bytes replaced."""
    if isinstance(argv, str) or kwargs.get("shell"):
        raise ValueError("dk_process.run takes an argv list and never a shell")
    if "input" not in kwargs:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    if kwargs.get("text") is not False and not isinstance(kwargs.get("input"), (bytes, bytearray, memoryview)):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return subprocess.run(argv, timeout=timeout, env=system_env(LC_ALL="C.UTF-8") if env is None else env,
                          **{**NO_WINDOW, **kwargs})


def run_helper(args, seconds, cancelled=None, *, stopped=None, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
               stderr=subprocess.DEVNULL):
    """(returncode, stdout, stderr) of `python -I -B *args`, the streams as bytes when piped, else None. Raises `stopped` (default TimeoutError, or
    InterruptedError when cancelled() is true) once the wall deadline passes or cancelled() turns true; the child
    is then killed and reaped."""
    if type(seconds) not in (int, float) or not 0 < seconds <= 3600:
        raise ValueError("seconds must be in (0, 3600]")
    if cancelled is not None and cancelled():
        raise stopped or InterruptedError("cancelled")  # never start a process nobody waits for
    deadline = time.monotonic() + seconds
    worker = subprocess.Popen([host_path(PYTHON), "-I", "-B", *args], stdin=stdin, stdout=stdout, stderr=stderr,
                              env=system_env(), close_fds=os.name == "nt", **NO_WINDOW)
    try:
        while True:
            if cancelled is not None and cancelled():
                raise stopped or InterruptedError("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise stopped or TimeoutError(f"the helper took longer than {seconds} s")
            try:
                data, errors = worker.communicate(timeout=min(.2, remaining))
            except subprocess.TimeoutExpired:
                continue
            if (cancelled is not None and cancelled()) or time.monotonic() >= deadline:
                continue  # finished just too late: report the stop, not a result nobody waits for
            return worker.returncode, data, errors
    finally:
        if worker.poll() is None:
            worker.kill()
        try:
            worker.communicate(timeout=REAP_SECONDS)
        except subprocess.TimeoutExpired:  # killed, not yet let go of (a loaded machine): reap it later, answer now
            threading.Thread(target=worker.communicate, name="dk_process reap", daemon=True).start()


def spawn_argv(argv, env):
    """The argv that runs argv so that it dies with the plugin (Linux; elsewhere argv with the program resolved),
    for a caller that starts it itself from the event loop's thread. The program is resolved with env's PATH."""
    program = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0], path=env.get("PATH", os.defpath))
    if not program:
        raise FileNotFoundError(f"{argv[0]} is not on the PATH")
    if not sys.platform.startswith("linux"):
        return [program, *argv[1:]]
    return [host_path(PYTHON), "-I", "-B", os.path.abspath(__file__), "--pdeathsig", str(os.getpid()),
            program, *argv[1:]]


def spawn(argv, **popen):
    """Popen for a long-lived child that dies with the plugin (Linux; elsewhere a plain Popen)."""
    env = popen.pop("env") if "env" in popen else system_env()
    return subprocess.Popen(spawn_argv(argv, env), env=env, **{**NO_WINDOW, **popen})


def _pdeathsig_exec(parent, argv):
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot tie the process to the plugin's lifetime")
    if not os.path.isabs(argv[0]):
        raise OSError(f"{argv[0]} must be an absolute path")
    if os.getppid() != parent:
        return 0  # the plugin died before the signal was armed: do not start an orphan
    os.execve(argv[0], argv, os.environ.copy())


if __name__ == "__main__" and sys.argv[1:2] == ["--pdeathsig"] and len(sys.argv) > 3:
    raise SystemExit(_pdeathsig_exec(int(sys.argv[2]), sys.argv[3:]))

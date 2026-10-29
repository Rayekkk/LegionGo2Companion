# SPDX-License-Identifier: BSD-3-Clause
# Shared from DeckyKit (shared/tests/test_dk_process.py): change it there, then `kit sync`.
# One test (or more) per guarantee in dk_process.py's docstring.
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import _harness  # noqa: F401
import dk_process

SHOW_ENV = "import os; print(' '.join(sorted(os.environ)))"
LINUX = sys.platform.startswith("linux")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except (FileNotFoundError, ProcessLookupError):  # the process exited between kill(0) and the read
        return False


class ProcessTest(unittest.TestCase):
    def setUp(self):
        dk_process.PYTHON = sys.executable  # the helpers run under the test's interpreter
        self.addCleanup(setattr, dk_process, "PYTHON", "/usr/bin/python3" if os.name != "nt" else sys.executable)

    def test_children_get_system_libraries_and_everything_else(self):
        env = {**os.environ, "LD_LIBRARY_PATH": "/decky/lib", "LD_LIBRARY_PATH_ORIG": "x", "LD_PRELOAD": "x.so",
               "LD_AUDIT": "a.so", "DK_KEPT": "1"}
        env.pop("PATH", None)
        with mock.patch.dict(os.environ, env, clear=True):
            names = dk_process.system_env()
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/decky/lib", "the plugin's own environment stays")
        for blocked in ("LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG", "LD_PRELOAD", "LD_AUDIT"):
            self.assertNotIn(blocked, names)
        self.assertEqual(names["DK_KEPT"], "1")
        self.assertIn("/usr/bin", names["PATH"])

    def test_run_uses_that_environment_and_a_utf8_locale(self):
        with mock.patch.dict(os.environ, {"LD_PRELOAD": "x.so"}):
            result = dk_process.run([sys.executable, "-c", SHOW_ENV + "; print(os.environ['LC_ALL'])"], timeout=30,
                                    capture_output=True)
        lines = result.stdout.splitlines()
        self.assertNotIn("LD_PRELOAD", lines[0].split())
        self.assertEqual(lines[1], "C.UTF-8")

    def test_a_hanging_program_is_stopped(self):
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            dk_process.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
        self.assertLess(time.monotonic() - start, 10)

    def test_stdin_is_empty_and_there_is_never_a_shell(self):
        result = dk_process.run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout=30,
                                capture_output=True)
        self.assertEqual(result.stdout.strip(), "''")
        with self.assertRaises(ValueError):
            dk_process.run("echo hi", timeout=5)
        with self.assertRaises(ValueError):
            dk_process.run(["echo", "hi"], timeout=5, shell=True)

    def test_bytes_stay_bytes_when_asked_for(self):
        echo = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read() or b'none')"]
        self.assertEqual(dk_process.run(echo, timeout=30, capture_output=True, text=False).stdout, b"none")
        self.assertEqual(dk_process.run(echo, timeout=30, capture_output=True, input=b"\xff").stdout, b"\xff")

    def test_exit_codes_check_env_and_other_arguments_are_subprocess_runs(self):
        fail = [sys.executable, "-c", "import sys; sys.exit(3)"]
        self.assertEqual(dk_process.run(fail, timeout=30).returncode, 3, "a non-zero exit is returned, not raised")
        with self.assertRaises(subprocess.CalledProcessError):
            dk_process.run(fail, timeout=30, check=True)
        env = {**dk_process.system_env(), "DK_ONLY": "1"}
        shown = dk_process.run([sys.executable, "-c", SHOW_ENV], timeout=30, env=env, capture_output=True)
        self.assertEqual(shown.stdout.split(), sorted(env), "a given env is used exactly, without LC_ALL added")
        folder = tempfile.mkdtemp()
        echo = [sys.executable, "-c", "import os, sys; sys.stdout.buffer.write(os.getcwd().encode() + sys.stdin.buffer.read())"]
        result = dk_process.run(echo, timeout=30, capture_output=True, input=b"|in", stdin=None, cwd=folder)
        self.assertEqual(result.stdout, os.path.realpath(folder).encode() + b"|in")
        with mock.patch.object(dk_process.subprocess, "run") as run:
            dk_process.run(["x"], timeout=1, user="deck", group="deck", extra_groups=[], input=b"x", stdin=None)
        self.assertEqual({k: run.call_args.kwargs[k] for k in ("user", "group", "extra_groups", "input", "stdin")},
                         {"user": "deck", "group": "deck", "extra_groups": [], "input": b"x", "stdin": None})

    def test_output_is_text_and_undecodable_bytes_do_not_raise(self):
        result = dk_process.run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok \\xc3\\xa9 \\xff')"],
                                timeout=30, capture_output=True)
        self.assertEqual(result.stdout, "ok é �")

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_no_console_window_on_windows(self):
        with mock.patch.object(dk_process.subprocess, "run") as run:
            dk_process.run(["x"], timeout=1)
        self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_a_callers_own_creationflags_are_kept(self):
        with mock.patch.object(dk_process.subprocess, "run") as run, \
                mock.patch.object(dk_process.subprocess, "Popen") as popen:
            dk_process.run(["x"], timeout=1, creationflags=0)
            dk_process.spawn([sys.executable], creationflags=0)
        self.assertEqual(run.call_args.kwargs["creationflags"], 0)
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0)

    def test_helpers_default_to_the_system_python(self):
        fresh = {"__name__": "dk_process_fresh"}
        with open(dk_process.__file__, encoding="utf-8") as f:
            exec(compile(f.read(), dk_process.__file__, "exec"), fresh)  # PYTHON as imported, before setUp's change
        self.assertEqual(fresh["PYTHON"], sys.executable if os.name == "nt" else "/usr/bin/python3")

    def test_helper_deadline_and_cancel_kill_the_child(self):
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            dk_process.run_helper(["-c", "import time; time.sleep(30)"], 0.5)
        self.assertLess(time.monotonic() - start, 10)
        with self.assertRaises(InterruptedError):
            dk_process.run_helper(["-c", "import time; time.sleep(30)"], 30, lambda: True)
        stop = RuntimeError("stopped")
        with self.assertRaises(RuntimeError) as caught:
            dk_process.run_helper(["-c", "import time; time.sleep(30)"], 0.3, stopped=stop)
        self.assertIs(caught.exception, stop)
        self.assertEqual(dk_process.run_helper(["-c", "print('hi')"], 30), (0, b"hi" + os.linesep.encode(), None))
        failed = dk_process.run_helper(["-c", "import sys; sys.exit('broken')"], 30, stderr=subprocess.PIPE)
        self.assertEqual((failed[0], failed[2].strip()), (1, b"broken"), "the helper's error output is kept")

    def test_a_helper_that_finishes_after_the_stop_is_still_stopped_and_reaped(self):
        flips = iter([False, False, True])  # before start, first wait, then the stop comes in
        started = []
        real = subprocess.Popen
        with mock.patch.object(dk_process.subprocess, "Popen", side_effect=lambda *a, **k: started.append(
                real(*a, **k)) or started[-1]):
            with self.assertRaises(InterruptedError):
                dk_process.run_helper(["-c", "print('done')"], 30, lambda: next(flips, True))
        deadline = time.monotonic() + 30  # a loaded machine may leave the reap to run_helper's background thread
        while started[0].returncode is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(started[0].returncode, "reaped, no zombie")

    def test_a_killed_helper_slow_to_go_does_not_hide_the_stop(self):
        reaped = threading.Event()

        def communicate(timeout=None):
            if timeout is None:  # the background reap
                reaped.set()
                return b"", None
            raise subprocess.TimeoutExpired("helper", timeout)
        with mock.patch.object(dk_process.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = None
            popen.return_value.communicate.side_effect = communicate
            with self.assertRaises(TimeoutError):
                dk_process.run_helper(["-c", "pass"], 0.3)
            with self.assertRaises(InterruptedError):
                dk_process.run_helper(["-c", "pass"], 30, iter([False, True]).__next__)
        self.assertEqual(popen.return_value.kill.call_count, 2)
        self.assertTrue(reaped.wait(5), "reaped in the background, not left a zombie")
        self.assertLessEqual(dk_process.REAP_SECONDS, 3, "Decky kills a plugin whose unload takes over 5 s")

    def test_a_cancelled_helper_never_starts(self):
        with mock.patch.object(dk_process.subprocess, "Popen") as popen, self.assertRaises(InterruptedError):
            dk_process.run_helper(["-c", "pass"], 30, lambda: True)
        popen.assert_not_called()

    def test_helpers_get_system_libraries_too(self):
        with mock.patch.dict(os.environ, {"LD_PRELOAD": "x.so"}):
            _, out, _ = dk_process.run_helper(["-c", SHOW_ENV], 30)
        self.assertNotIn(b"LD_PRELOAD", out)

    @unittest.skipUnless(os.name == "posix", "posix_spawn")
    def test_a_helper_does_not_fork_the_whole_plugin(self):
        with mock.patch.object(dk_process.subprocess, "Popen", wraps=subprocess.Popen) as popen:
            dk_process.run_helper(["-c", "pass"], 30)
        self.assertIs(popen.call_args.kwargs["close_fds"], False, "close_fds=True rules out posix_spawn")
        self.assertTrue(os.path.isabs(popen.call_args.args[0][0]), "posix_spawn needs a path, not a PATH lookup")

    def test_emulated_decky_runs_the_hosts_native_programs(self):
        def machines(host, running):
            return mock.patch.object(dk_process, "_machines", return_value=(host, running))
        with machines(183, 62):  # aarch64 host, x86-64 Decky: FEX
            self.assertTrue(dk_process.emulated())
            self.assertEqual(dk_process.host_path("/usr/bin/xprop"), "/proc/self/root/usr/bin/xprop")
            self.assertEqual(dk_process.host_path("xprop"), "xprop", "only absolute paths are mapped")
            with mock.patch.object(dk_process.subprocess, "Popen") as popen:
                popen.return_value.communicate.return_value = (b"", None)
                popen.return_value.returncode = 0
                dk_process.PYTHON = "/usr/bin/python3"
                dk_process.run_helper(["-c", "pass"], 30)
                self.assertEqual(popen.call_args.args[0][0], "/proc/self/root/usr/bin/python3")
                if LINUX:  # elsewhere spawn has no wrapper
                    dk_process.spawn(["/usr/bin/true"], env={})
                    self.assertEqual(popen.call_args.args[0][0], "/proc/self/root/usr/bin/python3")
        with machines(62, 62):
            self.assertFalse(dk_process.emulated())
            self.assertEqual(dk_process.host_path("/usr/bin/xprop"), "/usr/bin/xprop")
        with mock.patch.object(dk_process, "_machines", side_effect=OSError("no /proc")):
            self.assertFalse(dk_process.emulated(), "unreadable means a normal machine")
        if sys.platform.startswith("linux") and os.path.exists("/usr/bin/python3"):
            self.assertFalse(dk_process.emulated(), "the test machine itself is not emulated")

    def test_spawn_resolves_the_program_and_keeps_a_given_environment(self):
        with mock.patch.object(dk_process.subprocess, "Popen") as popen:
            dk_process.spawn([os.path.basename(sys.executable), "-V"], env={"PATH": os.path.dirname(sys.executable)})
        argv, env = popen.call_args.args[0], popen.call_args.kwargs["env"]
        self.assertTrue(any(os.path.isabs(a) and os.path.basename(a) == os.path.basename(sys.executable)
                            for a in argv))
        self.assertEqual(env, {"PATH": os.path.dirname(sys.executable)}, "env={} or a given env is never replaced")
        with self.assertRaises(FileNotFoundError):
            dk_process.spawn(["no-such-program-dk"], env={"PATH": tempfile.gettempdir()})

    @unittest.skipUnless(LINUX, "PR_SET_PDEATHSIG is Linux")
    def test_a_spawned_child_dies_with_the_plugin(self):
        plugin = (f"import sys; sys.path.insert(0, {os.path.dirname(dk_process.__file__)!r}); import dk_process; "
                  f"dk_process.PYTHON = sys.executable; "
                  "p = dk_process.spawn(['sleep', '60']); print(p.pid, flush=True); import time; time.sleep(1)")
        out = subprocess.run([sys.executable, "-c", plugin], capture_output=True, text=True, timeout=30)
        child = int(out.stdout.split()[0])
        deadline = time.monotonic() + 5
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(child), "sleep outlived the plugin")

    @unittest.skipUnless(LINUX, "PR_SET_PDEATHSIG is Linux")
    def test_a_child_started_with_asyncio_from_spawn_argv_dies_with_the_plugin(self):
        plugin = (f"import asyncio, sys; sys.path.insert(0, {os.path.dirname(dk_process.__file__)!r}); "
                  "import dk_process; dk_process.PYTHON = sys.executable\n"
                  "async def main():\n"
                  "    env = dk_process.system_env()\n"
                  "    p = await asyncio.create_subprocess_exec(*dk_process.spawn_argv(['sleep', '60'], env), env=env)\n"
                  "    print(p.pid, flush=True); await asyncio.sleep(1)\n"
                  "asyncio.run(main())")
        out = subprocess.run([sys.executable, "-c", plugin], capture_output=True, text=True, timeout=30)
        child = int(out.stdout.split()[0])
        deadline = time.monotonic() + 5
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(child), "sleep outlived the plugin")
        self.assertEqual(dk_process.spawn_argv(["/bin/true", "x"], {})[-2:], ["/bin/true", "x"])

    @unittest.skipUnless(LINUX, "pwd and /run/user are Linux")
    def test_session_env_gives_home_and_only_the_users_own_runtime_folder(self):
        import pwd
        import stat
        uid, real_stat = os.geteuid(), os.stat
        for owner, used in ((uid, True), (uid + 1, False)):
            def fake(path, *args, **kwargs):
                if path == f"/run/user/{uid}":
                    return os.stat_result((stat.S_IFDIR | 0o700, 0, 0, 0, owner, 0, 0, 0, 0, 0))
                return real_stat(path, *args, **kwargs)
            with self.subTest(owner=owner), mock.patch.dict(os.environ, {"LD_PRELOAD": "x.so"}, clear=True), \
                    mock.patch.object(dk_process.os, "stat", side_effect=fake):
                env = dk_process.session_env(LC_ALL="C.UTF-8")
                self.assertEqual(env.get("XDG_RUNTIME_DIR") == f"/run/user/{uid}", used)
                self.assertEqual(env["HOME"], pwd.getpwuid(uid).pw_dir)
                self.assertEqual(env["LC_ALL"], "C.UTF-8")
                self.assertNotIn("LD_PRELOAD", env, "system_env() rules still apply")

    @unittest.skipUnless(LINUX, "PR_SET_PDEATHSIG is Linux")
    def test_a_child_whose_plugin_is_already_gone_does_not_start(self):
        # The wrapper is told its plugin has another pid than its real parent: it must exit instead of exec'ing.
        out = subprocess.run([sys.executable, "-I", dk_process.__file__, "--pdeathsig", "1", sys.executable, "-c",
                              "print('started')"], capture_output=True, text=True, timeout=30)
        self.assertEqual((out.returncode, out.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()

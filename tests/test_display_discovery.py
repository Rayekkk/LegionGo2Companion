"""Cold-start/rebind recovery without a console or a real event loop timer."""
import asyncio
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from safe_settings import AtomicSettingsManager, CorruptSettings

import support  # Decky/Linux test doubles before importing the backend.
import display_backend as display


class DisplayDiscoveryTests(unittest.TestCase):
    def test_loop_recovers_nodes_that_appear_after_start_without_steady_scans(self):
        with tempfile.TemporaryDirectory() as raw:
            node = Path(raw) / "amdgpu_bl1"
            node.mkdir()
            (node / "brightness").write_text("200000")
            (node / "max_brightness").write_text("471000")
            now = [2.0]
            scans, writes = [], []
            state = dict(display.Plugin._state, panel_ok=False,
                         panel_desc="no internal panel EDID", backlight="",
                         active_mode=display.MODE_PQ)

            async def offload(fn, *args):
                return fn(*args)

            def advance(seconds):
                now[0] += max(0.01, seconds)
                if now[0] >= 34:
                    raise asyncio.CancelledError()

            async def sleep(seconds):
                advance(seconds)

            def discover():
                scans.append(now[0])
                return str(node) if now[0] >= 12 else ""

            with patch.object(display.Plugin, "_state", state), \
                 patch.object(display.Plugin, "_last_written", None), \
                 patch.object(display, "_offload", offload), \
                 patch.object(display.time, "monotonic", side_effect=lambda: now[0]), \
                 patch.object(display.asyncio, "sleep", sleep), \
                 patch.object(display, "_identify_panel", side_effect=lambda: (now[0] >= 12, "panel")), \
                 patch.object(display, "_find_backlight", side_effect=discover), \
                 patch.object(display, "_open_notify", return_value=None), \
                 patch.object(display, "_wait_for_change", side_effect=lambda fd, timeout: advance(min(timeout, .25))), \
                 patch.object(display.Plugin, "_refresh_setup"), \
                 patch.object(display.Plugin, "_refresh_gate", side_effect=lambda: state["panel_ok"]), \
                 patch.object(display.Plugin, "_edid_pass"), \
                 patch.object(display.Plugin, "_forward_nits", side_effect=lambda value: writes.append(value)):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(display.Plugin()._loop(""))
            self.assertTrue(scans)
            self.assertLessEqual(scans[0], 4, "initial missing hardware should be probed promptly")
            recovered = [at for at in scans if at >= 12]
            self.assertTrue(recovered, "late panel discovery must remain retryable")
            self.assertLessEqual(recovered[0], 22, "available hardware must be found within a bounded retry")
            self.assertLessEqual(len(scans), 5, "missing hardware should not trigger continuous scans")
            self.assertFalse(any(at > recovered[0] for at in scans),
                             "a healthy panel must leave the discovery retry loop")
            self.assertTrue(writes, "brightness must recover without restarting Decky")
            self.assertEqual(state["backlight"], "amdgpu_bl1")
            self.assertEqual(state["max_nits"], 471.0)

    def test_desktop_mode_backs_off_xprop_and_probes_at_once_when_gamescope_returns(self):
        now, spawns, gamescope = [0.0], [], [None]

        def run(argv, **_kw):  # KWin's Xwayland: an X server without GAMESCOPE atoms
            spawns.append(argv)
            return subprocess.CompletedProcess(argv, 0, "_NET_SUPPORTED(ATOM) = x\n", "")

        async def offload(fn, *args):
            return fn(*args)

        async def sleep(seconds):
            now[0] += seconds
            if now[0] >= 600:
                raise asyncio.CancelledError()

        with patch.object(display.Plugin, "_state", dict(display.Plugin._state)), \
             patch.object(display.Plugin, "_session_retry_after", 0.0), \
             patch.object(display.Plugin, "_session_retry_delay", display.GATE_INTERVAL_S), \
             patch.object(display, "_display_candidates", return_value=(":0",)), \
             patch.object(display, "_gamescope_start_time", side_effect=lambda: gamescope[0]), \
             patch.object(display, "_xprop_command", side_effect=lambda args, *_: ["xprop", *args]), \
             patch.object(display, "_xprop_env", return_value={}), \
             patch.object(display, "_xprop_user", return_value={}), \
             patch.object(display.subprocess, "run", side_effect=run), \
             patch.object(display.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(display.asyncio, "sleep", new=sleep), \
             patch.object(display, "_offload", new=offload):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(display._watch_properties())
            for tick in range(300):  # the loop's gate and EDID pass, every 2 s for 10 minutes
                now[0] = tick * display.GATE_INTERVAL_S
                display.Plugin._session_props([display.ATOM_IS_EXTERNAL])
                display.Plugin._session_props([])
            self.assertLess(len(spawns), 100)  # was ~1200: two setuid forks a second
            spawns.clear()
            gamescope[0] = display._IDENTITY_BASE  # Gaming Mode is back
            display.Plugin._session_props([display.ATOM_IS_EXTERNAL])
            self.assertTrue(spawns)

    def test_session_identity_is_the_process_start_not_the_proc_inode_mtime(self):
        def stat_line(ticks):
            # comm may hold spaces and parentheses; starttime is field 22.
            return f"42 (gamescope (wl) S 1 42 42 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {ticks} 0 0"
        files = {"/proc/42/comm": "gamescope-wl\n", "/proc/42/stat": stat_line(1000),
                 "/proc/sys/kernel/random/boot_id": "0a1b2c3d-1111-2222-3333-444455556666\n"}
        def fake_open(path, *args, **kwargs):
            if path not in files:
                raise FileNotFoundError(path)
            return io.StringIO(files[path])
        def no_mtime(path, *args, **kwargs):
            raise AssertionError(f"identity must not use stat({path}) mtime")
        with patch.object(display, "_gamescope_pid", None), patch.object(display, "_gamescope_scan_after", 0.0),              patch.object(display, "_CLK_TCK", 100), patch.object(display, "open", fake_open, create=True),              patch.object(display.os, "listdir", return_value=["1", "42"]), patch.object(display.os, "stat", no_mtime):
            first = display._gamescope_start_time()
            self.assertEqual(display._gamescope_start_time(), first)
            files["/proc/sys/kernel/random/boot_id"] = "ffffffff-1111-2222-3333-444455556666\n"
            other_boot = display._gamescope_start_time()
            files["/proc/42/stat"] = stat_line(1500)
            restarted = display._gamescope_start_time()
        self.assertFalse(display.Plugin._same_session(first, other_boot))
        self.assertFalse(display.Plugin._same_session(other_boot, restarted))
        self.assertAlmostEqual(restarted - other_boot, 5.0)

    def _enter(self, context):  # TestCase.enterContext needs Python 3.11
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def _fake_session_proc(self, boot_id, ticks=605, uptime=1000.0):
        """gamescope-wl started `ticks`/100 s after a boot `uptime` seconds ago."""
        files = {"/proc/2021/comm": "gamescope-wl\n",
                 "/proc/2021/stat": f"2021 (gamescope-wl) S 1 2021 2021 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {ticks} 0 0",
                 "/proc/sys/kernel/random/boot_id": f"{boot_id}-1111-2222-3333-444455556666\n",
                 "/proc/uptime": f"{uptime} 0.0\n"}
        real_open = open
        def fake_open(path, *args, **kwargs):
            return io.StringIO(files[path]) if path in files else real_open(path, *args, **kwargs)
        for target, value in (("_gamescope_pid", 2021), ("_CLK_TCK", 100), ("open", fake_open)):
            self._enter(patch.object(display, target, value, create=True))
        return files

    def test_script_mtime_is_compared_with_the_session_wall_clock_start(self):
        # Live console values: boot_id prefix 5e474f, gamescope-wl started at 605 ticks.
        root = Path(self._enter(tempfile.TemporaryDirectory()))
        script = root / "lenovo.legiongo2.oled.lua"
        script.write_bytes(Path(display.BUNDLED_SCRIPTS[display.MODE_PQ]).read_bytes())
        self._enter(patch.object(display, "INSTALLED_SCRIPT", str(script)))
        files = self._fake_session_proc("5e474f00")
        self.assertIsNone(display._loaded_script_variant(),
                          "a script written after the session started cannot be the loaded one")
        old = display.time.time() - 5000  # before this boot
        os.utime(script, (old, old))
        self.assertEqual(display._loaded_script_variant(), display.MODE_PQ)
        # A boot-id prefix that is small as a number must not make old files look newer.
        files["/proc/sys/kernel/random/boot_id"] = "00012300-1111-2222-3333-444455556666\n"
        self.assertEqual(display._loaded_script_variant(), display.MODE_PQ)

    def test_restart_fallback_compares_the_script_with_the_session_wall_clock_start(self):
        root = Path(self._enter(tempfile.TemporaryDirectory()))
        script, bundled = root / "installed.lua", root / "dev-build.lua"
        script.write_bytes(b"-- not in the production variant table")
        bundled.write_bytes(script.read_bytes())
        self._enter(patch.object(display, "INSTALLED_SCRIPT", str(script)))
        self._enter(patch.object(display, "BUNDLED_SCRIPT", str(bundled)))
        self._fake_session_proc("5e474f00")
        self.assertTrue(display._restart_needed(), "a script written after the session needs a restart")
        old = display.time.time() - 5000
        os.utime(script, (old, old))
        self.assertFalse(display._restart_needed())

    def test_session_stamps_saved_by_2_1_still_match_the_running_session(self):
        # 2.1 saved the epoch mtime of /proc/<pid>; the session below began ~994 s ago.
        self._fake_session_proc("5e474f00")
        current = display._gamescope_start_time()
        now = display.time.time()
        self.assertTrue(display.Plugin._same_session(now - 900, current))
        self.assertFalse(display.Plugin._same_session(now - 2000, current), "a stamp from an earlier session")
        self.assertTrue(display.Plugin._same_session(current, current))
        self.assertFalse(display.Plugin._same_session(current - 1.0, current))

    def test_failed_notification_read_closes_the_descriptor(self):
        with patch.object(display.os, "open", return_value=73), \
             patch.object(display.os, "read", side_effect=OSError("device disappeared")), \
             patch.object(display.os, "close") as close:
            self.assertIsNone(display._open_notify("/test/backlight"))
        close.assert_called_once_with(73)

    def test_lost_settings_block_startup_mutations_and_default_commits(self):
        with tempfile.TemporaryDirectory() as raw:
            primary = Path(raw) / "display_settings.json"
            backup = Path(str(primary) + ".bak")
            primary.write_text("{broken primary")
            backup.write_text("{broken backup")
            manager = AtomicSettingsManager("display_settings", raw)
            before = (primary.read_bytes(), backup.read_bytes())
            self.assertTrue(manager.recovery_error)
            state = dict(display.Plugin._state)
            plugin = display.Plugin()
            with patch.object(display, "settings", manager), \
                 patch.object(display.Plugin, "_state", state), \
                 patch.object(display.Plugin, "_task", None), \
                 patch.object(display.Plugin, "_prop_task", None), \
                 patch.object(display, "_pick_display") as discover, \
                 patch.object(display.Plugin, "_refresh_setup") as setup, \
                 patch.object(display, "_write_atom_int") as write_atom, \
                 patch.object(display, "_write_nits") as write_nits, \
                 patch.object(display, "_install_script") as install, \
                 patch.object(display.Plugin, "_edid_pass") as edid:
                with self.assertRaises(CorruptSettings):
                    asyncio.run(plugin._main())
                self.assertIsNone(plugin._task)
                self.assertIsNone(plugin._prop_task)
                public = asyncio.run(plugin.get_state())
                self.assertIn("could not be recovered", public["settings_error"])
                with self.assertRaises(CorruptSettings):
                    display.Plugin._store_settings({"brightness_baseline": 100.0})
                for operation in (lambda: plugin.set_enabled(True),
                                  lambda: plugin.set_edid_fix(True),
                                  lambda: plugin.set_panel_mode("pq"),
                                  lambda: plugin._unload(uninstalling=True)):
                    with self.assertRaises(CorruptSettings):
                        asyncio.run(operation())
                for operation in (discover, setup, write_atom, write_nits, install, edid):
                    operation.assert_not_called()
            self.assertEqual((primary.read_bytes(), backup.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()

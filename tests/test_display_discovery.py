"""Cold-start/rebind recovery without a console or a real event loop timer."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from safe_settings import AtomicSettingsManager, CorruptSettings

import test_integration  # Decky/Linux test doubles before importing the backend.
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
            self.assertEqual(scans, [2.0, 12.0])
            self.assertTrue(writes, "brightness must recover without restarting Decky")
            self.assertEqual(state["backlight"], "amdgpu_bl1")
            self.assertEqual(state["max_nits"], 471.0)

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
                 patch.object(display.updater, "ssl_context"), \
                 patch.object(display, "_pick_display") as discover, \
                 patch.object(display.Plugin, "_refresh_setup") as setup, \
                 patch.object(display, "_write_atom_int") as write_atom, \
                 patch.object(display, "_write_nits") as write_nits, \
                 patch.object(display, "_install_script") as install, \
                 patch.object(display.Plugin, "_edid_pass") as edid:
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

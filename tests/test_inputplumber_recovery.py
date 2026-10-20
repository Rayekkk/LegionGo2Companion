# SPDX-License-Identifier: BSD-3-Clause
"""An InputPlumber that lost the controller is restarted once, and only then."""
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import support  # Stub decky before main.
import inputplumber_process as module
import main
import module_runtime

REAL_HID_MONITOR = module._hid_monitor
RESTART = ["/usr/bin/systemctl", "--no-block", "try-restart", "inputplumber.service"]


class Console:
    """systemctl and busctl as seen on a Legion Go 2 with InputPlumber."""

    def __init__(self):
        self.controller = True
        self.state = {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"}
        self.show_returncode = 0
        self.composite = [False]  # One value per busctl tree; the last one stays.
        self.heals_on_restart = True
        self.fail_next = None
        self.fwupd = None  # None: not on the bus; else the Status the daemon reports ("u 1" idle); "?": unreadable.
        self.calls = []

    def restarts(self):
        return [argv for argv in self.calls if "try-restart" in argv]

    def run(self, argv, timeout, **kwargs):
        self.calls.append(argv)
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        if argv[1] == "show":
            text = "".join(f"{key}={value}\n" for key, value in self.state.items())
            return subprocess.CompletedProcess(argv, self.show_returncode, text)
        if "get-property" in argv:
            if self.fwupd in (None, "?"):
                return subprocess.CompletedProcess(argv, 1, "", "Failed to get property Status")
            return subprocess.CompletedProcess(argv, 0, self.fwupd + "\n", "")
        if "NameHasOwner" in argv:
            if self.fwupd == "?":
                return subprocess.CompletedProcess(argv, 1, "", "Connection refused")
            return subprocess.CompletedProcess(argv, 0, f"b {'false' if self.fwupd is None else 'true'}\n", "")
        if "tree" in argv:
            present = self.composite.pop(0) if len(self.composite) > 1 else self.composite[0]
            text = "/\n/org/shadowblip/InputPlumber\n/org/shadowblip/InputPlumber/Manager\n"
            if present:
                text += "/org/shadowblip/InputPlumber/CompositeDevice0\n"
            return subprocess.CompletedProcess(argv, 0, text, "")
        if argv == RESTART:
            if self.heals_on_restart:
                self.composite = [True]
            return subprocess.CompletedProcess(argv, 0)
        raise AssertionError(f"unexpected command {argv}")


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.console = Console()
        self.blocked = False
        self.updating = False
        self.events = []
        self.suspended = 0.0
        stack.enter_context(patch.object(module, "SETTLE_S", 0.02))
        stack.enter_context(patch.object(module, "CONFIRM_S", 0.02))
        stack.enter_context(patch.object(module, "RESUME_POLL_S", 0.02))
        stack.enter_context(patch.object(module, "BUSCTL", sys.executable))
        stack.enter_context(patch.object(module, "run", side_effect=lambda *a, **k: self.console.run(*a, **k)))
        stack.enter_context(patch.object(module, "_hid_monitor", return_value=None))
        stack.enter_context(patch.object(module, "event", side_effect=lambda c, a, **f: self.events.append((a, f))))
        stack.enter_context(patch.object(module_runtime, "kernel_controller", side_effect=lambda: self.console.controller))
        stack.enter_context(patch.object(module_runtime, "suspend_offset", side_effect=lambda: self.suspended))
        self.checks = stack.enter_context(patch.object(module, "lost_controller", side_effect=module.lost_controller))
        self.recovery = module.LostControllerRecovery(lambda: self.blocked, lambda: self.updating)

    async def asyncSetUp(self):
        self.task = asyncio.create_task(self.recovery.run())

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def until(self, condition):
        deadline = time.monotonic() + 3
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out")
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.06)  # Anything that would follow (a confirmation, a restart) has had its chance.

    def names(self):
        return [name for name, _ in self.events]

    async def test_a_loss_that_holds_restarts_inputplumber_exactly_once(self):
        await self.until(lambda: "controller_recovered_after_restart" in self.names())
        self.assertEqual(self.console.restarts(), [RESTART])
        self.assertIn(("restart_for_lost_controller", {"level": "warning", "reason": "start", "count": 1,
                                                       "status": "enabled"}), self.events)
        self.recovery._wake.set()
        await self.until(lambda: self.checks.call_count >= 4)
        self.assertEqual(self.console.restarts(), [RESTART])

    async def test_steamos_enabled_runtime_unit_is_restarted(self):
        self.console.state["UnitFileState"] = "enabled-runtime"  # What SteamOS actually reports.
        await self.until(lambda: "controller_recovered_after_restart" in self.names())
        self.assertEqual(self.console.restarts(), [RESTART])

    async def test_fwupd_working_on_a_device_holds_the_restart_and_idle_or_absent_does_not(self):
        self.task.cancel()
        # "u 3" is what an idle fwupd 2.0.18 keeps reporting on the console, so it must not hold the restart.
        for status, restarted in (("u 5", False), ("u 6", False), ("u 12", False), ("u 4", False), ("u 10", False),
                                  ("u 14", False), ("garbage", False), ("?", False),
                                  ("u 1", True), ("u 0", True), ("u 3", True), ("u 2", True), (None, True)):
            with self.subTest(status=status):
                self.console = Console()
                self.console.fwupd = status
                task = asyncio.create_task(module.LostControllerRecovery(lambda: False, lambda: False).run())
                try:
                    await asyncio.sleep(0.2)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(self.console.restarts(), [RESTART] if restarted else [])

    async def test_a_loss_inputplumber_repairs_itself_is_left_alone(self):
        self.console.composite = [False, True]
        await self.until(lambda: self.checks.call_count >= 2)
        self.assertEqual(self.console.restarts(), [])

    async def test_a_stopped_disabled_masked_or_missing_inputplumber_is_never_restarted(self):
        self.task.cancel()
        for change in ({"ActiveState": "inactive"}, {"ActiveState": "activating"}, {"ActiveState": "failed"},
                       {"UnitFileState": "disabled"}, {"UnitFileState": "masked"},
                       {"LoadState": "not-found", "ActiveState": "inactive", "UnitFileState": ""}, None):
            with self.subTest(change=change):
                self.console = Console()
                if change is None:
                    self.console.show_returncode = 1
                else:
                    self.console.state.update(change)
                self.checks.reset_mock()
                task = asyncio.create_task(module.LostControllerRecovery(lambda: False, lambda: False).run())
                try:
                    await self.until(lambda: self.checks.call_count >= 1)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(self.console.restarts(), [])
                self.assertFalse(any("tree" in argv for argv in self.console.calls))  # Not even asked over D-Bus.

    async def test_without_the_controller_nothing_is_run(self):
        self.console.controller = False
        await self.until(lambda: self.checks.call_count >= 1)
        self.assertEqual(self.console.calls, [])

    async def test_maintenance_updater_or_shutdown_prevents_a_restart(self):
        self.task.cancel()
        for name in ("blocked", "updating", "blocked_during_confirmation"):
            with self.subTest(name):
                self.console = Console()
                flags = {"blocked": [False], "updating": False}
                if name == "blocked":
                    flags["blocked"] = [True]
                elif name == "updating":
                    flags["updating"] = True
                else:
                    flags["blocked"] = [False, True]  # Firmware install or Decky's stop began meanwhile.
                blocked = lambda: flags["blocked"].pop(0) if len(flags["blocked"]) > 1 else flags["blocked"][0]
                self.checks.reset_mock()
                task = asyncio.create_task(module.LostControllerRecovery(blocked, lambda: flags["updating"]).run())
                try:
                    await asyncio.sleep(0.2)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(self.console.restarts(), [])
                if name == "blocked":
                    self.assertEqual(self.checks.call_count, 0)

    async def test_a_restart_that_does_not_help_is_not_repeated(self):
        self.console.heals_on_restart = False
        await self.until(lambda: "restart_did_not_recover_controller" in self.names())
        for _ in range(3):
            self.recovery._wake.set()
            await asyncio.sleep(0.08)
        self.assertEqual(self.console.restarts(), [RESTART])

    async def test_restarts_are_spaced_and_capped_per_run(self):
        await self.until(lambda: "controller_recovered_after_restart" in self.names())
        self.console.composite = [False]  # Lost again soon after.
        self.recovery._wake.set()
        await self.until(lambda: "restart_limited" in self.names())
        self.assertEqual(len(self.console.restarts()), 1)
        with patch.object(module, "RESTART_SPACING_S", 0):
            for recovered in range(2, module.RESTARTS_PER_RUN + 1):
                self.console.composite = [False]
                self.recovery._wake.set()
                await self.until(lambda: self.names().count("controller_recovered_after_restart") == recovered)
            self.console.composite = [False]
            self.recovery._wake.set()
            await self.until(lambda: self.names().count("restart_limited") == 2)
        self.assertEqual(len(self.console.restarts()), module.RESTARTS_PER_RUN)

    async def test_controller_udev_events_rearm_the_check_and_others_do_not(self):
        class Monitor:
            def __init__(self, names):
                self.names = list(names)

            def poll(self, timeout):
                if not self.names:
                    return None
                name = self.names.pop(0)
                if isinstance(name, Exception):
                    raise name
                return types.SimpleNamespace(sys_name=name)
        recovery = module.LostControllerRecovery(lambda: False, lambda: False)
        recovery._drain(Monitor(["0003:046D:C52B.0001", "0003:17EF:6182.0002"]))
        self.assertFalse(recovery._wake.is_set())
        recovery._drain(Monitor(["0003:046D:C52B.0001", "0003:17EF:61ED.0007"]))
        self.assertTrue(recovery._wake.is_set())
        recovery._wake.clear()
        recovery._drain(Monitor([OSError(105, "No buffer space available")]))
        self.assertTrue(recovery._wake.is_set())  # A lost event may have been the controller.

    async def test_a_loop_that_cannot_read_the_monitor_only_costs_the_hotplug_watch(self):
        monitor = types.SimpleNamespace(filter_by=lambda name: None, start=lambda: None, fileno=lambda: 99)
        fake = types.ModuleType("pyudev")
        fake.Context = lambda: None
        fake.Monitor = types.SimpleNamespace(from_netlink=lambda context: monitor)

        def refuse(*args):
            raise NotImplementedError

        with patch.dict(sys.modules, {"pyudev": fake}):
            self.assertIsNone(REAL_HID_MONITOR(types.SimpleNamespace(add_reader=refuse), None))
        self.assertIn("hotplug_watch_unavailable", self.names())

    @unittest.skipIf(os.name == "nt", "The Windows loop has no add_reader.")
    async def test_the_monitor_is_unregistered_when_the_watch_ends(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        monitor = types.SimpleNamespace(fileno=left.fileno, poll=lambda timeout: None)

        def attach(loop, drain):
            loop.add_reader(monitor.fileno(), drain, monitor)
            return monitor
        with patch.object(module, "_hid_monitor", side_effect=attach):
            task = asyncio.create_task(module.LostControllerRecovery(lambda: False, lambda: False).run())
            await asyncio.sleep(0.05)
            right.send(b"x")  # Level-triggered: _drain runs (and finds nothing) while the reader is registered.
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(asyncio.get_running_loop().remove_reader(left.fileno()))  # Already gone.

    async def test_a_wake_rechecks(self):
        self.console.composite = [True]
        await self.until(lambda: self.checks.call_count >= 1)
        self.console.composite = [False]
        self.suspended += 30
        await self.until(lambda: self.console.restarts())
        self.assertEqual([fields["reason"] for name, fields in self.events if name == "restart_for_lost_controller"],
                         ["resume"])

    async def test_a_failing_check_is_logged_and_watching_continues(self):
        self.console.fail_next = RuntimeError("busctl crashed")
        await self.until(lambda: "recovery_check_failed" in self.names())
        self.assertFalse(self.task.done())
        self.recovery._wake.set()
        await self.until(lambda: self.console.restarts())
        self.assertEqual(self.console.restarts(), [RESTART])

    async def test_cancel_ends_the_watch_at_once_even_mid_confirmation(self):
        with patch.object(module, "CONFIRM_S", 30):
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = asyncio.create_task(self.recovery.run())
            await self.until(lambda: self.checks.call_count >= 1)
            began = time.monotonic()
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertEqual(self.console.restarts(), [])


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        stack.enter_context(patch.object(main.decky, "DECKY_PLUGIN_SETTINGS_DIR", folder.name))
        stack.enter_context(patch.object(main, "_installed_standalone_plugins", return_value=[]))
        stack.enter_context(patch.object(main, "capture_intent", return_value={}))
        self.addCleanup(module_runtime._stop_requested.clear)
        self.started = []

        class Watch:
            def __init__(watch, blocked, updating):
                watch.blocked = blocked
                self.started.append(watch)

            async def run(watch):
                await asyncio.Event().wait()
        stack.enter_context(patch.object(main, "LostControllerRecovery", Watch))

    def plugin(self, console):
        plugin = main.Plugin()
        for name in main.MODULE_NAMES:
            setattr(plugin, "_" + name, types.SimpleNamespace(_main=AsyncMock(), _unload=AsyncMock(),
                                                              _uninstall=AsyncMock()))
        plugin._start_desktop = AsyncMock()
        plugin._detect_device = lambda: ({name: True for name in main.MODULE_NAMES}, {"console": console})
        return plugin

    async def test_only_a_legion_go_watches_and_stopping_ends_the_watch_within_budget(self):
        other = self.plugin(None)
        await other._main()
        self.assertIsNone(other._inputplumber)
        await other._unload()

        plugin = self.plugin("go2")
        await plugin._main()
        task = plugin._inputplumber
        self.assertEqual(len(self.started), 1)
        self.assertFalse(self.started[0].blocked())
        module_runtime.pause_controller()  # Controller firmware install.
        try:
            self.assertTrue(self.started[0].blocked())
        finally:
            module_runtime.resume_controller()
        began = time.monotonic()
        await plugin._unload()
        self.assertLess(time.monotonic() - began, main.SHUTDOWN_BUDGET_S)
        self.assertTrue(task.cancelled())
        self.assertTrue(self.started[0].blocked())  # Closing.


if __name__ == "__main__":
    unittest.main()

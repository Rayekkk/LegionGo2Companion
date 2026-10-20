# SPDX-License-Identifier: BSD-3-Clause
"""Which modules each Legion Go console gets, and that a module which does not apply never runs."""
import asyncio
import functools
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import support  # Install isolated Decky stubs before backend imports.
import battery_backend
import controller_backend
import controller_firmware
import fan_ec
import main
import module_runtime
import rgb_backend
import wifi_backend

GO1 = dict(product_name="83E1", product_version="Legion Go 8APU1", product_family="Legion Go 8APU1")
GO_S = dict(product_name="83L3", product_version="Legion Go S 8ARP1", product_family="Legion Go S 8ARP1")
GO2 = dict(product_name="83N0", product_version="Legion Go 8ASP2", product_family="Legion Go 8ASP2",
           board_name="LNVNB161216")
GO2_Z2 = dict(product_name="83N1", product_version="Legion Go 8AHP2", product_family="Legion Go 8AHP2",
              bios_version="RQCN17WW")
FOREIGN = dict(product_name="82XA", product_version="Yoga Slim 7", product_family="Yoga Slim 7")
# console -> (DMI, controller on the kernel driver, joystick-ring LED class, kernel charge modes)
CONSOLES = {
    "Go 1, 2025 controller firmware": (GO1, True, "go", True),
    "Go 1, older controller firmware": (GO1, False, None, True),
    "Go S": (GO_S, False, "go_s", True),
    "Go S without charge modes": (GO_S, False, "go_s", False),
    "Go 2 83N0": (GO2, True, "go", True),
    "Go 2 83N1": (GO2_Z2, True, "go", True),
    "Lenovo outside the family": (FOREIGN, False, None, True),
}
EVERYTHING = {"fan", "vibration", "display", "wifi", "rgb", "remap", "battery", "controller"}
EXPECTED = {  # TDP reports its own applicability (tdp_backend).
    "Go 1, 2025 controller firmware": {"vibration", "wifi", "rgb", "battery", "controller"},
    "Go 1, older controller firmware": {"wifi", "battery"},
    "Go S": {"wifi", "rgb", "battery"},
    "Go S without charge modes": {"wifi", "rgb"},
    "Go 2 83N0": EVERYTHING,
    "Go 2 83N1": EVERYTHING,
    "Lenovo outside the family": {"wifi"},
}


class IdentityTests(unittest.TestCase):
    def dmi(self, **values):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for name, value in values.items():
            (Path(folder.name) / name).write_text(value + "\n", encoding="ascii")
        return folder.name

    def test_machine_type_names_the_console(self):
        for model, console in (("83E1", "go1"), ("83L3", "gos"), ("83N6", "gos"), ("83Q2", "gos"),
                               ("83Q3", "gos"), ("83N0", "go2"), ("83N1", "go2")):
            with self.subTest(model=model):
                root = self.dmi(sys_vendor="LENOVO", product_name=model)
                self.assertEqual(module_runtime.legion_device(root), (console, model))
                self.assertEqual(module_runtime.is_legion_go_2(root), console == "go2")

    def test_an_unknown_machine_type_is_placed_by_product_version_but_never_as_a_go_2(self):
        for version, console in (("Legion Go S 8XXX1", "gos"), ("Legion Go 8APU1", "go1"),
                                 ("Legion Go 8ASP2", None), ("Yoga Slim 7", None)):
            with self.subTest(version=version):
                root = self.dmi(sys_vendor="LENOVO", product_name="83ZZ", product_version=version)
                self.assertEqual(module_runtime.legion_device(root), (console, "83ZZ"))

    def test_another_vendor_or_unreadable_dmi_is_no_console(self):
        self.assertEqual(module_runtime.legion_device(self.dmi(sys_vendor="OTHER", product_name="83N0")), (None, ""))
        self.assertEqual(module_runtime.legion_device(self.dmi()), (None, ""))

    def test_kernel_controller_needs_the_new_pids_on_hid_lenovo_go(self):
        drivers = {"0003:17EF:61EB.0001": "hid-lenovo-go", "0003:17EF:6182.0002": "xpad",
                   "0003:17EF:61EC.0003": "hid-generic"}
        def realpath(path):
            name = Path(path).parent.name
            return "/sys/bus/hid/drivers/" + drivers.get(name, "")
        for present, expected in ((["0003:17EF:61EB.0001"], True), (["0003:17EF:6182.0002"], False),
                                  (["0003:17EF:61EC.0003"], False), ([], False)):
            with self.subTest(present=present), patch.object(module_runtime.os, "listdir", return_value=present), \
                    patch.object(module_runtime.os.path, "realpath", side_effect=realpath):
                self.assertIs(module_runtime.kernel_controller(), expected)
        with patch.object(module_runtime.os, "listdir", side_effect=FileNotFoundError):
            self.assertFalse(module_runtime.kernel_controller())


class ApplicabilityTests(unittest.TestCase):
    """The real per-module rules (read-only), each console faked at the sysfs it reads."""

    def detect(self, dmi, new_firmware, led, charge):
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            identity = root / "class/dmi/id"
            identity.mkdir(parents=True)
            for name, value in dict(dict(sys_vendor="LENOVO", board_name="LNVNB000000", bios_version="X"),
                                    **dmi).items():
                (identity / name).write_text(value + "\n", encoding="ascii")
            leds = {name: root / name for name in ("go", "go_s")}
            if led:
                leds[led].write_text("")
            def detect():
                if not charge:
                    raise battery_backend.BatteryError("No charge modes.")
                return {}
            for target, values in (
                (module_runtime, dict(DMI_ROOT=str(identity), kernel_controller=lambda: new_firmware)),
                (battery_backend, dict(SYS_ROOT=root, _detect=detect)),
                (controller_backend, dict(DMI_ROOT=identity)),
                (rgb_backend, dict(RGB_LED_PATH=str(leds["go"]), GO_S_RGB_LED_PATH=str(leds["go_s"]))),
                (fan_ec.platform, dict(system=lambda: "Linux", machine=lambda: "x86_64")),
                (main, dict(firmware_applies=functools.partial(controller_firmware.applies, root))),
            ):
                for name, value in values.items():
                    stack.enter_context(patch.object(target, name, value))
            return main.Plugin()._detect_device()

    def test_each_console_gets_only_the_modules_that_apply(self):
        for console, setup in CONSOLES.items():
            with self.subTest(console=console):
                applicable, device = self.detect(*setup)
                self.assertEqual({name for name, value in applicable.items() if value} - {"tdp"},
                                 EXPECTED[console])
                # Fan, the power light and controller firmware keep their own Go 2 model gates.
                go2 = console.startswith("Go 2")
                self.assertEqual((applicable["fan"], device["firmware"], device["power_led"]), (go2,) * 3)

    def test_fan_stays_visible_on_a_go_2_whose_dmi_fails_the_model_gate(self):
        import fan_backend
        with tempfile.TemporaryDirectory() as raw:
            identity = Path(raw)
            def put(**values):
                for name, value in values.items():
                    (identity / name).write_bytes(value if isinstance(value, bytes) else value.encode() + b"\n")
            with patch.object(module_runtime, "DMI_ROOT", raw):
                put(sys_vendor="LENOVO", product_name="83N0")  # board_name and bios_version unreadable
                self.assertTrue(fan_backend.Plugin.applicable())
                put(product_name="83N1", bios_version="RRCN16WW")  # Fails the gate: shown with its error.
                self.assertTrue(fan_backend.Plugin.applicable())
                put(product_name=b"\xff\xfe\n")  # Unreadable machine type: not surely another console.
                self.assertTrue(fan_backend.Plugin.applicable())
                for vendor, model in (("LENOVO", "83E1"), ("LENOVO", "83L3"), ("OTHER", "83N0")):
                    put(sys_vendor=vendor, product_name=model)
                    self.assertFalse(fan_backend.Plugin.applicable(), model)
            with patch.object(module_runtime, "DMI_ROOT", str(identity / "missing")):
                self.assertFalse(fan_backend.Plugin.applicable())  # No DMI at all

    def test_a_failing_device_read_does_not_stop_the_start(self):
        plugin = main.Plugin()
        with patch.object(main, "firmware_applies", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")), \
                patch.object(main.diagnostics, "event") as event:
            applicable, device = plugin._detect_device()
        self.assertEqual(set(applicable), set(main.MODULE_NAMES))
        self.assertEqual((device["firmware"], device["power_led"]), (False, False))
        self.assertEqual(event.call_args.args[:2], ("plugin", "device_unknown"))

    def test_a_failing_rule_hides_its_module_and_is_logged(self):
        plugin = main.Plugin()
        plugin._display = types.SimpleNamespace(applicable=lambda: 1 / 0)
        with patch.object(main.diagnostics, "event") as event:
            applicable, _ = plugin._detect_device()
        self.assertFalse(applicable["display"])
        self.assertEqual(event.call_args.args[:2], ("display", "applicability_unknown"))


class NotApplicableModuleTests(unittest.IsolatedAsyncioTestCase):
    """A module that does not apply never starts, stops, restores or answers; Manage Modules cannot turn it on."""

    async def asyncSetUp(self):
        stack = self.stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(main.decky, "DECKY_PLUGIN_SETTINGS_DIR",
                                         stack.enter_context(tempfile.TemporaryDirectory())))
        stack.enter_context(patch.object(main, "_installed_standalone_plugins", return_value=[]))
        stack.enter_context(patch.object(main, "capture_intent", return_value={}))
        self.addCleanup(module_runtime._stop_requested.clear)
        self.plugin = main.Plugin()
        for name in main.MODULE_NAMES:
            setattr(self.plugin, "_" + name, types.SimpleNamespace(
                _main=AsyncMock(), _migration=AsyncMock(), _unload=AsyncMock(), _uninstall=AsyncMock(),
                get_state=AsyncMock(return_value={"success": True}), set_enabled=AsyncMock(),
                applicable=lambda name=name: name not in ("fan", "display")))
        self.plugin._start_desktop = AsyncMock()

    async def test_it_never_runs_and_is_reported_as_not_applicable(self):
        # Even a withdrawal saved by an earlier release is not retried on a console it does not apply to.
        self.plugin._module_state["display"] = {"enabled": False, "pending": True, "resume": {}}
        with patch.object(main, "withdraw", new=AsyncMock(return_value="")) as withdraw:
            await self.plugin._migration()
            await self.plugin._main()
            status = self.plugin._module_status()
            with self.assertRaisesRegex(RuntimeError, "does not apply"):
                await self.plugin.fan_get_state()
            with self.assertRaisesRegex(RuntimeError, "does not apply"):
                await self.plugin.modules_set_enabled("fan", False)
            with self.assertRaisesRegex(RuntimeError, "does not apply"):
                await self.plugin.modules_set_enabled("display", True)
            await self.plugin._unload()
            await self.plugin._uninstall()
        withdraw.assert_not_awaited()
        for name in ("fan", "display"):
            component = getattr(self.plugin, "_" + name)
            for stage in ("_migration", "_main", "_unload", "_uninstall"):
                getattr(component, stage).assert_not_awaited()
            self.assertIs(status[name]["applicable"], False)
        self.plugin._fan.get_state.assert_not_awaited()
        self.plugin._fan.set_enabled.assert_not_awaited()
        self.plugin._battery._main.assert_awaited_once()
        self.assertIs(status["battery"]["applicable"], True)


class BandPolicyDeviceTests(unittest.TestCase):
    def test_band_policy_follows_the_mt7921e_driver_on_every_legion_go(self):
        plugin = wifi_backend.Plugin()
        for console, driver, expected in (("go1", "mt7921e", True), ("gos", "mt7921e", True),
                                          ("go2", "mt7921e", True), ("go2", "iwlwifi", False),
                                          (None, "mt7921e", False)):
            with self.subTest(console=console, driver=driver), \
                    patch.object(module_runtime, "legion_device", return_value=(console, "83XX")), \
                    patch.object(plugin, "_detect_wifi_driver", return_value=driver):
                self.assertIs(plugin._target_supported(), expected)
                info = plugin._get_device_info_sync()
                self.assertIs(wifi_backend.Plugin._high_band_discovery_enabled(
                    {**info, "band_policy": wifi_backend.BAND_POLICY_HIGH_ONLY,
                     "band_policy_state": {"mode": wifi_backend.BAND_POLICY_HIGH_ONLY, "owns_iwd": True}}), expected)

    def test_the_go_2_keeps_its_family_and_label(self):
        with patch.object(module_runtime, "legion_device", return_value=("go2", "83N0")):
            self.assertEqual(wifi_backend.Plugin()._detect_device_family(), ("83N0", "legion_go_2", "Legion Go 2"))


if __name__ == "__main__":
    unittest.main()

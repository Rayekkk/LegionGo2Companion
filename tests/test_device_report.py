# SPDX-License-Identifier: BSD-3-Clause
"""Device report: allowlisted fields only, nothing private, every section on its own, a user-owned file."""
import asyncio
import json
import os
import stat
import subprocess
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import support  # noqa: F401 - installs the fake decky module
import device_report
import main
import module_runtime
import tdp_backend

USER = "rayuser"
SECRETS = ("SERIAL123SECRET", "4C4C4544-0000-1111-2222-UUIDSECRET00", "BATSERIAL77", "USBSERIAL55",
           "ASSETTAG99", "aa:bb:cc:dd:ee:ff", "12:34:56:78:9A:BC", "MyHomeNet", USER, "PANELSN1", "FWSERIAL3",
           "SECRETNAME")  # a HID device of another vendor is not listed at all


def edid():
    data = bytearray(128)
    data[:8] = device_report.EDID_HEADER
    data[8:10] = (((ord("L") - 64) << 10) | ((ord("E") - 64) << 5) | (ord("N") - 64)).to_bytes(2, "big")
    data[10:12] = (0x8A3B).to_bytes(2, "little")
    data[12:16] = b"\x12\x34\x56\x78"  # the EDID serial number: never in the report
    data[54:72] = b"\x00\x00\x00\xff\x00" + b"PANELSN1\n    "
    data[72:90] = b"\x00\x00\x00\xfc\x00" + b"LGO2 OLED\n   "
    return bytes(data)


def facts(**changes):
    value = {"version": "2.3.0", "device": {"console": "go2", "model": "83N0", "firmware": True, "power_led": True,
                                             "controller_driver": True},
             "modules": {"fan": {"applicable": True, "enabled": True, "ready": False,
                                 "error": f"Settings in /home/{USER}/.config could not be read"},
                         "wifi": {"applicable": True, "enabled": True, "ready": False,
                                  "error": "nmcli could not activate MyHomeNet"},
                         "tdp": {"applicable": True, "enabled": True, "ready": True}},
             "inputplumber_restarts": 1, "tdp_caps": {"tdp_available": True, "min": 5},
             "fan": {"supported": True, "beta": False, "error": ""}, "log_dir": "/nonexistent", "user": USER}
    value.update(changes)
    return value


class Commands:
    """busctl, iw and pacman answers by their arguments; anything else fails like a missing service."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout, **_):
        self.calls.append((list(argv), timeout))
        args = " ".join(argv[1:])
        out = None
        if "InputManager Version" in args:
            out = json.dumps({"type": "s", "data": "0.78.0"})
        elif "DaemonVersion" in args:
            out = json.dumps({"type": "s", "data": "2.0.18"})
        elif "GetDevices" in args:
            device = {"Name": {"type": "s", "data": "Legion Go 2 Controller"}, "Plugin": {"type": "s", "data": "legion_hid"},
                      "Version": {"type": "s", "data": "0x0000041a"}, "Flags": {"type": "t", "data": 2},
                      "Serial": {"type": "s", "data": "FWSERIAL3"}, "Guid": {"type": "as", "data": ["1234-guid"]},
                      "InstanceIds": {"type": "as", "data": ["USB\\VID_17EF&PID_61EB&SN_FWSERIAL3"]}}
            other = {"Name": {"type": "s", "data": "NVMe"}, "Plugin": {"type": "s", "data": "nvme"}}
            out = json.dumps({"type": "aa{sv}", "data": [[device, other]]})
        elif argv[1:] == ["phy"]:
            out = ("Wiphy phy0\n\twiphy index: 0\n\tPermanent MAC: 12:34:56:78:9A:BC\n\tSSID: MyHomeNet\n"
                   "\tBand 1:\n\t\tFrequencies:\n\t\t\t* 2412.0 MHz [1] (20.0 dBm)\n"
                   "\t\t\t* 2484.0 MHz [14] (disabled)\n\t\tBitrates (non-HT):\n\t\t\t* 1.0 Mbps\n"
                   "\tBand 2:\n\t\tFrequencies:\n\t\t\t* 5180.0 MHz [36] (23.0 dBm)\n"
                   "\tBand 4:\n\t\tFrequencies:\n\t\t\t* 5955.0 MHz [1] (disabled)\n")
        if out is None:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, out, "")


class _Fixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for tool in ("busctl", "iw", "pacman"):
            (self.root / tool).write_text("")
        self.commands = Commands()
        for name, value in (("ROOT", str(self.root)), ("BUSCTL", str(self.root / "busctl")), ("IW", str(self.root / "iw")),
                            ("PACMAN", str(self.root / "pacman")), ("run", self.commands)):
            patcher = patch.object(device_report, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("_helper_verified", lambda: False),):
            patcher = patch.object(tdp_backend, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        events = patch("diagnostics_reader.read_events", return_value={"events": []})
        events.start()
        self.addCleanup(events.stop)

    def write(self, path, text):
        target = self.root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        (target.write_bytes if isinstance(text, bytes) else target.write_text)(text)

    def fake_console(self):
        dmi = {"sys_vendor": "LENOVO", "product_name": "83N0", "product_version": "Legion Go 8ASP2",
               "bios_version": "RRCN16WW", "ec_firmware_release": "1.16", "product_serial": "SERIAL123SECRET",
               "board_serial": "SERIAL123SECRET", "product_uuid": "4C4C4544-0000-1111-2222-UUIDSECRET00",
               "chassis_asset_tag": "ASSETTAG99"}
        for name, value in dmi.items():
            self.write(f"/sys/class/dmi/id/{name}", value + "\n")
        self.write("/etc/os-release", 'NAME="SteamOS"\nVERSION_ID=3.8.28\nBUILD_ID=20261001.1\nVARIANT_ID=steamdeck\n'
                                      f'HOME_URL="https://example.invalid/{USER}"\n')
        self.write("/proc/cpuinfo", "processor\t: 0\nmodel name\t: AMD Ryzen Z2 Extreme\n")
        attributes = "/sys/class/firmware-attributes/lenovo-wmi-other-0/attributes"
        for name, value in (("type", "integer"), ("current_value", "20"), ("min_value", "5"), ("max_value", "35")):
            self.write(f"{attributes}/ppt_pl1_spl/{name}", value + "\n")
        self.write(f"{attributes}/AssetTag/current_value", "ASSETTAG99\n")
        self.write("/sys/class/firmware-attributes/thinklmi/attributes/BootOrder/current_value", "usb\n")
        self.write("/sys/firmware/acpi/platform_profile", "balanced\n")
        self.write("/sys/firmware/acpi/platform_profile_choices", "low-power balanced performance custom\n")
        for name, value in (("type", "Battery"), ("capacity", "80"), ("status", "Charging"), ("manufacturer", "SUNWODA"),
                            ("model_name", "L24D4PG3"), ("serial_number", "BATSERIAL77"),
                            ("charge_types", "[Standard] Long_Life")):
            self.write(f"/sys/class/power_supply/BAT0/{name}", value + "\n")
        self.write("/sys/class/power_supply/ACAD/type", "Mains\n")
        self.write("/sys/class/power_supply/ACAD/online", "1\n")
        for name, value in (("type", "Battery"), ("scope", "Device"), ("capacity", "60")):
            self.write(f"/sys/class/power_supply/hid-USBSERIAL55-battery/{name}", value + "\n")
        self.write("/sys/class/hwmon/hwmon7/name", "hid_USBSERIAL55_battery\n")
        for name, value in (("idVendor", "17ef"), ("idProduct", "61eb"), ("product", "Legion Controller"),
                            ("bcdDevice", "0101"), ("serial", "USBSERIAL55")):
            self.write(f"/sys/bus/usb/devices/1-3/{name}", value + "\n")
        self.write("/sys/class/hwmon/hwmon5/name", "lenovo_wmi_other\n")
        self.write("/sys/class/hwmon/hwmon5/fan1_input", "3100\n")
        self.write("/sys/class/hwmon/hwmon5/pwm1_enable", "2\n")
        self.write("/sys/class/drm/card1-eDP-1/edid", edid())
        self.write("/usr/share/gamescope/scripts/00-gamescope/displays/lenovo.legiongo2.oled.lua", "")
        if os.name == "posix":  # Kernel names with ':' cannot exist on Windows.
            hid = "/sys/bus/hid/devices/0003:17EF:61EB.0001"
            self.write(f"{hid}/uevent", f"HID_ID=0003:000017EF:000061EB\nHID_NAME=Lenovo Legion Go 2\n"
                                        f"HID_UNIQ=aa:bb:cc:dd:ee:ff\nHID_PHYS=usb-0000:c4:00.3-3/input0\n")
            self.write("/sys/bus/hid/devices/0003:046D:C52B.0009/uevent", "HID_NAME=Foreign Mouse SECRETNAME\n")
            self.write(f"{hid}/left_handle/firmware_version", "0x41a\n")
            self.write(f"{hid}/os_mode", "windows\n")
            drivers = self.root / "sys/bus/hid/drivers/hid-lenovo-go"
            drivers.mkdir(parents=True)
            (self.root / hid.lstrip("/") / "driver").symlink_to(drivers)
            self.write("/sys/class/leds/go:rgb:joystick_rings/mode_index", "dynamic custom\n")
            self.write("/sys/class/leds/go:rgb:joystick_rings/mode", "custom\n")
            self.write("/sys/bus/iio/devices/iio:device0/name", "bmi260\n")
            self.write("/sys/bus/pci/devices/0000:01:00.0/class", "0x028000\n")
            self.write("/sys/bus/pci/devices/0000:01:00.0/vendor", "0x14c3\n")
            self.write("/sys/bus/pci/devices/0000:01:00.0/device", "0x0616\n")


class ReportContent(_Fixture):
    def test_allowlisted_fields_are_present_and_nothing_private_is(self):
        self.fake_console()
        events = {"events": [{"time": "2999-01-01T00:00:00.000+00:00", "level": "warning", "component": "fan",
                              "event": "control_fault", "context": {"reason": f"/home/{USER} MyHomeNet"}},
                             {"time": "2000-01-01T00:00:00.000+00:00", "level": "error", "component": "tdp",
                              "event": "old_failure"}]}
        with patch("diagnostics_reader.read_events", return_value=events):
            text = device_report.collect(facts())
        for secret in SECRETS:
            self.assertNotIn(secret.lower(), text.lower(), secret)
        for expected in ("Companion version: 2.3.0", device_report.PRIVACY_NOTE, "product_name: 83N0",
                         "bios_version: RRCN16WW", "ec_firmware_release: 1.16", "VERSION_ID: 3.8.28",
                         "InputPlumber: 0.78.0", "fwupd: 2.0.18", "CPU: AMD Ryzen Z2 Extreme",
                         "legion_device: console=go2 model=83N0", "InputPlumber restarts for a lost controller (this Companion start): 1",
                         "TDP: tdp_available=True wmi=None extras=None min=5", "Fan: supported=True beta=False", "Fan self-tested BIOS/EC pairs: RRCN16WW/1.16",
                         "lenovo-wmi-other-0/ppt_pl1_spl: type=integer; current_value=20; min_value=5; max_value=35",
                         "platform_profile: balanced", "BAT0: type=Battery", "charge_types=[Standard] Long_Life",
                         "capacity=80", "model_name=L24D4PG3", "ACAD: type=Mains online=1",
                         "USB 17ef:61eb product=Legion Controller bcdDevice=0101",
                         "hwmon5 name=lenovo_wmi_other files=fan1_input,pwm1_enable fan1_input=3100",
                         "card1-eDP-1: manufacturer=LEN product=0x8A3B name=LGO2 OLED",
                         "gamescope display scripts: lenovo.legiongo2.oled.lua",
                         "Wi-Fi bands: 2.4 GHz, 5 GHz\n",  # no channel lists: they name the country
                         "Legion Go 2 Controller: plugin=legion_hid version=0x0000041a", "guids=1234-guid",
                         "/home/<user>/.config", "fan.control_fault warning: 1",
                         "RyzenAdj limits ==\nnot installed or not verified"):
            self.assertIn(expected, text)
        self.assertNotIn("old_failure", text, "only the last 24 hours")
        self.assertNotIn("NVMe", text, "fwupd: only Legion/Lenovo plugins")
        self.assertNotIn("2484", text, "Wi-Fi: no channel lists")
        self.assertNotIn("BootOrder", text, "firmware attributes: lenovo-wmi-other only")
        if os.name == "posix":
            for expected in ("HID 0003:17EF:61EB.0001 name=Lenovo Legion Go 2 driver=hid-lenovo-go",
                             "  left_handle/: firmware_version", "  os_mode", "mode_index: dynamic custom",
                             "IIO iio:device0: bmi260", "PCI 0000:01:00.0 14c3:0616"):
                self.assertIn(expected, text)
            self.assertNotIn("0x41a", text, "hid-lenovo-go values are MCU requests: names only")
            self.assertNotIn("windows", text)

    def test_commands_use_fixed_argv_and_never_start_a_service(self):
        self.fake_console()
        device_report.collect(facts())
        for argv, timeout in self.commands.calls:
            self.assertLessEqual(timeout, device_report.COMMAND_SECONDS)
            if argv[0] == device_report.BUSCTL:
                self.assertIn("--auto-start=no", argv)
        self.assertNotIn("pacman", " ".join(" ".join(argv) for argv, _ in self.commands.calls),
                         "the running InputPlumber answered")

    def test_unknown_lenovo_without_any_applicable_module_still_gets_every_section(self):
        self.write("/sys/class/dmi/id/sys_vendor", "LENOVO\n")
        self.write("/sys/class/dmi/id/product_name", "83Z9\n")
        modules = {name: {"applicable": False, "enabled": True, "ready": True} for name in main.MODULE_NAMES}
        text = device_report.collect(facts(device={"console": None, "model": None, "firmware": False, "power_led": False,
                                                   "controller_driver": False}, modules=modules,
                                           inputplumber_restarts=None, tdp_caps="RuntimeError",
                                           fan={"supported": False, "beta": False, "error": "Fan Control supports only the Legion Go 2"}))
        for title, _ in device_report.SECTIONS:
            self.assertIn(f"== {title} ==", text)
        self.assertIn("product_name: 83Z9", text)
        self.assertIn("product_family: unavailable: not present", text)
        self.assertIn("legion_device: console=None", text)
        self.assertIn("module tdp: applicable=False", text)
        self.assertIn("not watched on this console", text)
        self.assertIn("TDP: unavailable: RuntimeError", text)

    def test_a_failing_or_slow_section_leaves_the_rest(self):
        self.fake_console()
        def broken(_context):
            raise RuntimeError("sysfs vanished")
        def slow(argv, timeout, **_):
            raise subprocess.TimeoutExpired(argv, timeout)
        sections = tuple((title, broken if title == "DMI" else section) for title, section in device_report.SECTIONS)
        with patch.object(device_report, "SECTIONS", sections), patch.object(device_report, "run", slow):
            text = device_report.collect(facts())
        self.assertIn("== DMI ==\nunavailable: RuntimeError: sysfs vanished", text)
        self.assertIn("InputPlumber: unavailable: timed out", text)
        self.assertIn("fwupd devices ==\nunavailable: timed out", text)
        self.assertIn("capacity=80", text)
        self.assertIn("card1-eDP-1: manufacturer=LEN", text)

    def test_the_whole_report_stops_at_its_time_limit(self):
        self.fake_console()
        text = device_report.collect(facts(), seconds=0)
        self.assertEqual(text.count("unavailable: report time limit reached"), len(device_report.SECTIONS) + 1)
        self.assertEqual(self.commands.calls, [])
        stopping = device_report.collect(facts(), stopped=lambda: True)
        self.assertEqual(stopping.count("unavailable: Companion is stopping"), len(device_report.SECTIONS) + 1)

    def test_ryzenadj_limit_rows_only_from_the_verified_helper(self):
        output = ("CPU Family: Strix Point\nSMU BIOS Interface Version: 24\nVersion: v0.17.0\n"
                  "|        Name         |   Value   |     Parameter      |\n"
                  "| STAPM LIMIT         |    25.000 | stapm-limit        |\n"
                  "| STAPM VALUE         |     5.432 |                    |\n"
                  "| PPT LIMIT FAST      |    35.000 | fast-limit         |\n"
                  "| THM LIMIT CORE      |   100.000 | tctl-temp          |\n"
                  "| EDC LIMIT VDD       |   140.000 | vrmmax-current     |\n")
        with patch.object(tdp_backend, "_helper_verified", return_value=True), \
                patch.object(tdp_backend, "_run_ryzenadj", return_value=(0, output, "")) as helper:
            lines = device_report._ryzenadj(device_report._Context(facts(), float("inf")))
        helper.assert_called_once()
        self.assertEqual(helper.call_args.args[0], ["--info"])
        self.assertEqual(lines, ["CPU Family: Strix Point", "STAPM LIMIT: 25.000", "PPT LIMIT FAST: 35.000",
                                 "THM LIMIT CORE: 100.000", "EDC LIMIT VDD: 140.000"])
        self.assertFalse(tdp_backend._ryzenadj_lock.locked())
        with patch.object(tdp_backend, "_helper_verified", return_value=False), \
                patch.object(tdp_backend, "_run_ryzenadj") as helper:
            device_report._ryzenadj(device_report._Context(facts(), float("inf")))
        helper.assert_not_called()

    def test_a_device_battery_name_never_reaches_the_report(self):
        # Tested before scrub(): the kernel names it after HID_UNIQ, which need not contain "serial".
        self.fake_console()
        context = device_report._Context(facts(), float("inf"))
        power, fan = "\n".join(device_report._power(context)), "\n".join(device_report._fan(context))
        self.assertNotIn("USBSERIAL55", power + fan)
        self.assertIn("peripheral2 type=Battery", power)
        self.assertIn("ACAD: type=Mains", power)
        self.assertIn("hwmon7 name=<removed>", fan)
        self.assertIn("hwmon5 name=lenovo_wmi_other", fan)

    def test_firmware_backed_values_are_read_under_the_firmware_lock(self):
        self.fake_console()
        locked = {}
        read = module_runtime.read_sysfs_text
        def record(path, *args, **kwargs):
            locked[os.path.relpath(path, self.root).replace(os.sep, "/")] = module_runtime.firmware_lock.locked()
            return read(path, *args, **kwargs)
        context = device_report._Context(facts(), float("inf"))
        with patch.object(module_runtime, "read_sysfs_text", record):
            device_report._power(context); device_report._fan(context); device_report._firmware_attributes(context)
        for path in ("sys/class/hwmon/hwmon5/fan1_input", "sys/class/power_supply/BAT0/capacity",
                     "sys/class/power_supply/ACAD/online", "sys/firmware/acpi/platform_profile",
                     "sys/class/firmware-attributes/lenovo-wmi-other-0/attributes/ppt_pl1_spl/current_value"):
            self.assertIs(locked.get(path), True, path)

    def test_ryzenadj_runs_only_while_tdp_control_is_on_and_ready(self):
        with patch.object(tdp_backend, "_helper_verified", return_value=True), \
                patch.object(tdp_backend, "_run_ryzenadj", return_value=(0, "CPU Family: x", "")) as helper:
            for enabled, ready in ((False, True), (True, False)):
                state = facts(modules={"tdp": {"applicable": True, "enabled": enabled, "ready": ready}})
                self.assertEqual(device_report._ryzenadj(device_report._Context(state, float("inf"))),
                                 ["skipped: TDP control is off"])
        helper.assert_not_called()

    def test_ryzenadj_never_holds_up_tdp(self):
        with patch.object(tdp_backend, "_helper_verified", return_value=True), \
                patch.object(tdp_backend, "_run_ryzenadj", return_value=(0, "CPU Family: x", "")) as helper:
            off = facts(modules={"tdp": {"applicable": True, "enabled": False, "ready": False}})
            self.assertEqual(device_report._ryzenadj(device_report._Context(off, float("inf"))),
                             ["skipped: TDP control is off"])
            closing = device_report._Context(facts(), float("inf"), stopped=lambda: True)
            self.assertEqual(device_report._ryzenadj(closing), ["unavailable: Companion is stopping"])
            helper.assert_not_called()
            with tdp_backend._ryzenadj_lock:  # TDP is applying or restoring: the report does not wait.
                self.assertEqual(device_report._ryzenadj(device_report._Context(facts(), float("inf"))),
                                 ["unavailable: busy"])
            helper.assert_not_called()
            device_report._ryzenadj(device_report._Context(facts(), float("inf")))
        # Worst-case lock hold: the run limit plus _spawn_ryzenadj's 2 s kill wait, inside the restore's 4 s wait.
        self.assertLess(helper.call_args.kwargs["timeout"] + 2.0, 4.0)

    def test_scrub_removes_macs_private_lines_homes_and_the_user(self):
        text = device_report.scrub(f"a 00-11-22-33-44-55 b\nSerial: X\nbssid x\nkeep /home/{USER}/x\n{USER}'s deck\n"
                                   "steamdeck stays", USER)
        self.assertEqual(text, "a <removed> b\n<removed>\n<removed>\nkeep /home/<user>/x\n<removed>'s deck\nsteamdeck stays\n")


# ERAM as the Go 2 / Go S DSDT lays it out, at a test address: ECMV at 0x00, TCPU at 0x07, the 32-byte SMBus
# buffer SMDA at 0x20 and the battery serial BTSN at 0x82.
EC_FIELDS = [(b"ECMV", 8), (None, 0x30), (b"TCPU", 8), (None, 0xC0), (b"SMDA", 256), (None, 0x210), (b"BTSN", 16)]
EC_BASE = 0x2300


def two_regions(first, second):
    """One DSDT holding the AML of two synthetic tables."""
    aml = first[36:] + second[36:]
    return b"DSDT" + (36 + len(aml)).to_bytes(4, "little") + bytes(28) + aml


class EcAndDsdt(_Fixture):
    def setUp(self):
        super().setUp()
        mmio = patch.object(device_report, "EC_MMIO", range(0x2000, 0x3000))  # the fake /dev/mem's EC page
        mmio.start()
        self.addCleanup(mmio.stop)
        memory = bytearray(0x3000)
        for i in range(0xFF):
            memory[EC_BASE + i] = (i * 7 + 1) & 0xFF
        memory[EC_BASE + 0x20:EC_BASE + 0x2C] = b"BATTSECRET42"
        memory[EC_BASE + 0x82:EC_BASE + 0x84] = b"\xAB\xCD"
        self.memory = bytes(memory)
        self.write("/dev/mem", self.memory)
        self.write("/sys/firmware/acpi/tables/DSDT", support.power_led_dsdt(base=EC_BASE, fields=EC_FIELDS))

    def go1(self, **changes):
        return facts(device={"console": "go1", "model": "83E1"}, fan={"supported": False, "beta": False, "error": ""},
                     **changes)

    @unittest.skipUnless(os.name == "posix", "mmap with PROT_READ requires POSIX")
    def test_ec_memory_is_read_only_through_a_prot_read_mapping_of_the_dsdt_region(self):
        os.chmod(self.root / "dev/mem", 0o444)  # An attempt to open it for writing fails (unless root).
        opened, mapped = [], []
        real_open, real_mmap = os.open, device_report.mmap.mmap
        def spy_open(path, flags, *args, **kwargs):
            opened.append((str(path), flags))
            return real_open(path, flags, *args, **kwargs)
        def spy_mmap(*args, **kwargs):
            mapped.append(kwargs)
            return real_mmap(*args, **kwargs)
        with patch.object(device_report.os, "open", spy_open), patch.object(device_report.mmap, "mmap", spy_mmap), \
                patch("fan_ec.Ports", side_effect=AssertionError("no EC port access")):
            lines = device_report._ec(device_report._Context(self.go1(), float("inf")))
        text = "\n".join(lines)
        self.assertIn(f"ERAM base=0x{EC_BASE:X} length=0xFF; blanked (--): BTSN SMDA", text)
        self.assertIn("ERAM+00: 01 08 0F 16 1D 24 2B 32", text, "ECMV and TCPU as stored")
        self.assertIn("ERAM+20: " + " ".join(["--"] * 16), text, "the SMBus buffer is blanked")
        self.assertIn("ERAM+80: 81 88 -- -- 9D", text, "the battery serial is blanked")
        self.assertNotIn("AB CD", text)
        self.assertEqual(len([line for line in lines if line.startswith("  ERAM+")]), 16)
        memory = [flags for path, flags in opened if path.endswith("mem")]
        self.assertEqual(len(memory), 1)
        self.assertEqual(memory[0] & (os.O_WRONLY | os.O_RDWR), 0)
        self.assertEqual([(m["prot"], m["offset"]) for m in mapped], [(device_report.mmap.PROT_READ, 0x2000)])
        self.assertEqual((self.root / "dev/mem").read_bytes(), self.memory)

    def test_only_the_dsdt_s_ec_window_and_its_page_are_read(self):
        reads = []
        def fake_read(base, length):
            reads.append((base, length))
            return bytes(length)
        cases = {
            "no EC window": (support.power_led_dsdt(region=b"XRAM"), []),
            "EmbeddedControl space": (support.power_led_dsdt(space=0x03), []),
            "window larger than 256 bytes": (support.power_led_dsdt(base=EC_BASE, length=0x400), []),
            "mailbox in the window's page": (two_regions(support.power_led_dsdt(base=EC_BASE),
                                                         support.power_led_dsdt(region=b"SMA2", base=0x2A00, length=0x80)),
                                             [(EC_BASE, 0xFF), (0x2A00, 0x80)]),
            "mailbox in another page": (two_regions(support.power_led_dsdt(base=EC_BASE),
                                                    support.power_led_dsdt(region=b"SMA2", base=0x5A00, length=0x80)),
                                        [(EC_BASE, 0xFF)]),
            "unknown field element": (support.power_led_dsdt(base=EC_BASE, fields=[(b"\x02xyz", 8)]), []),
            "window outside the EC's MMIO block": (support.power_led_dsdt(base=0x1300), []),
            **{f"battery serial in Field({path})": (support.path_field(support.power_led_dsdt(base=EC_BASE), prefix), [])
               for path, prefix in (("\\ERAM", b"\\"), ("^ERAM", b"^"), ("EC0.ERAM", b"\x2eEC0_"),
                                    ("\\_SB.EC0.ERAM", b"\\\x2f\x03_SB_EC0_"))},
        }
        for reason, (table, expected) in cases.items():
            with self.subTest(reason):
                reads.clear()
                self.write("/sys/firmware/acpi/tables/DSDT", table)
                with patch.object(device_report, "_read_physical", side_effect=fake_read):
                    lines = device_report._ec(device_report._Context(self.go1(), float("inf")))
                self.assertEqual(reads, expected, lines)
                if reason.startswith("battery serial"):
                    self.assertEqual(lines[0], "ERAM: skipped: its DSDT field map is not understood, "
                                               "so private fields cannot be blanked")
                if reason == "window outside the EC's MMIO block":
                    self.assertEqual(lines[0], "ERAM: skipped: unexpected address 0x1300, outside the EC's MMIO block")
                if reason == "no EC window":
                    self.assertEqual(lines, ["skipped: the DSDT declares no EC memory window (ERAM or ECMM)"])

    def test_go_2_with_the_fan_module_on_reads_no_ec_and_reports_fan_controls_readings(self):
        telemetry = {"kind": "telemetry", "active": True, "ec_version": "1.16", "rpm": 3100, "effective": 3100,
                     "override": 0, "ec_temperature": 52, "power_mode": 2, "temperatures": {"k10temp": 60}}
        fan = {"supported": True, "beta": False, "error": "", "telemetry": telemetry}
        with patch.object(device_report, "_read_physical") as read, \
                patch("fan_ec.Ports", side_effect=AssertionError("no EC port access")), \
                patch("fan_backend.read_automatic", side_effect=AssertionError("no passive EC read")):
            text = device_report.collect(facts(fan=fan))
            read.assert_not_called()
            text_off = device_report.collect(facts(fan=dict(fan, telemetry=None)))
        self.assertIn("EC memory (read-only) ==\nskipped: the Fan module is on", text)
        self.assertIn("Fan Control's last EC readings: ec_version=1.16 rpm=3100 effective=3100 override=0 "
                      "ec_temperature=52 power_mode=2\n", text)
        self.assertIn("Fan Control's last EC readings: none (no Fan Control session)", text_off)

    def test_other_hardware_gets_no_ec_read(self):
        with patch.object(device_report, "_read_physical") as read:
            text = device_report.collect(facts(device={"console": None, "model": None}))
        read.assert_not_called()
        self.assertIn("EC memory (read-only) ==\nskipped: not a Legion Go console", text)

    def test_dsdt_is_appended_unfiltered_and_restores_to_the_same_sha256(self):
        import base64, hashlib, random, re, zlib
        table = support.power_led_dsdt(base=EC_BASE, fields=EC_FIELDS)
        table = table[:4] + (len(table) + 3000).to_bytes(4, "little") + table[8:] + random.Random(1).randbytes(3000)
        self.write("/sys/firmware/acpi/tables/DSDT", table)
        with patch.object(device_report, "_read_physical", side_effect=lambda base, length: bytes(length)):
            first = device_report.collect(self.go1())
            block = first.split(device_report.DSDT_BEGIN)[-1].split(device_report.DSDT_END)[0]
            # A user name that occurs in the base64 as a word: the filter would have replaced it.
            user = next(part for part in re.split(r"[+/\n]", block) if len(part) >= 3)
            text = device_report.collect(self.go1(user=user))
        self.assertNotEqual(device_report.scrub(block, user), block + "\n", "the test name hits the filter")
        self.assertIn(f"size: {len(table)} bytes, sha256: {hashlib.sha256(table).hexdigest()}", text)
        self.assertIn(device_report.DSDT_RESTORE, text)
        encoded = text.split(device_report.DSDT_BEGIN)[-1].split(device_report.DSDT_END)[0]
        self.assertEqual(hashlib.sha256(zlib.decompress(base64.b64decode(encoded))).hexdigest(),
                         hashlib.sha256(table).hexdigest())
        self.assertTrue(text.rstrip().endswith(device_report.DSDT_END))
        self.assertIn(f"ERAM base=0x{EC_BASE:X}", text, "the EC section and the table share one DSDT read")

    def test_dsdt_over_the_size_limit_or_incomplete_is_left_out(self):
        with patch.object(device_report, "DSDT_LIMIT", 64), patch.object(device_report, "_read_physical") as read:
            text = device_report.collect(self.go1())
        read.assert_not_called()
        self.assertIn(f"== {device_report.DSDT_TITLE} ==\nunavailable: larger than 0 KiB", text)
        self.assertIn("EC memory (read-only) ==\nunavailable: larger than 0 KiB", text)
        self.write("/sys/firmware/acpi/tables/DSDT", support.power_led_dsdt()[:-1])
        self.assertIn("unavailable: not a complete DSDT", device_report.collect(self.go1()))
        self.assertNotIn(device_report.DSDT_BEGIN + "\n", device_report.collect(self.go1()))


@unittest.skipUnless(os.name == "posix", "the dir_fd save with ownership requires POSIX")
class SaveFile(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.uid = os.getuid() or 1000
        self.gid = os.getgid() if os.getuid() else 1000
        os.chown(self.home, self.uid, self.gid)

    def test_report_is_a_new_user_owned_file_on_the_desktop(self):
        path = Path(device_report.save("report\n", str(self.home), self.uid, self.gid))
        self.assertEqual(path.parent, self.home / "Desktop")
        self.assertRegex(path.name, r"^LegionGoCompanion-device-report-\d{8}-\d{6}\.txt$")
        self.assertEqual(path.read_text(), "report\n")
        info = path.stat()
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (self.uid, self.gid, 0o644))
        self.assertEqual(path.parent.stat().st_uid, self.uid)

    def test_a_failed_write_leaves_no_partial_report(self):
        with patch.object(device_report.os, "write", side_effect=OSError("disk full")), self.assertRaises(OSError):
            device_report.save("report\n", str(self.home), self.uid, self.gid)
        self.assertEqual(list((self.home / "Desktop").iterdir()), [])

    def test_the_users_xdg_desktop_folder_is_used(self):
        config = self.home / ".config"
        config.mkdir()
        (config / "user-dirs.dirs").write_text('XDG_DESKTOP_DIR="$HOME/Schreibtisch"\n')
        path = Path(device_report.save("x", str(self.home), self.uid, self.gid))
        self.assertEqual(path.parent, self.home / "Schreibtisch")

    def test_a_desktop_symlink_or_existing_name_is_never_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.home / "Desktop").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            device_report.save("x", str(self.home), self.uid, self.gid)
        self.assertEqual(list(outside.iterdir()), [])
        (self.home / "Desktop").unlink()
        (self.home / "Desktop").mkdir()
        target = self.root / "important"
        target.write_text("keep")
        with patch.object(device_report.datetime, "datetime") as clock:
            clock.now.return_value.strftime.return_value = "20261021-120000"
            (self.home / "Desktop" / "LegionGoCompanion-device-report-20261021-120000.txt").symlink_to(target)
            path = device_report.save("x", str(self.home), self.uid, self.gid)
            self.assertTrue(path.endswith("-20261021-120000-2.txt"), "same second: a new name")
            self.assertTrue(device_report.save("y", str(self.home), self.uid, self.gid).endswith("-3.txt"))
        self.assertEqual(target.read_text(), "keep")

    def test_unknown_user_or_foreign_home_is_refused(self):
        with self.assertRaisesRegex(ValueError, "desktop user"):
            device_report.save("x", str(self.home), -1, self.gid)
        with self.assertRaises(ValueError):
            device_report.save("x", str(self.home), self.uid + 1, self.gid)
        self.assertFalse((self.home / "Desktop").exists())


class ReportRpc(unittest.IsolatedAsyncioTestCase):
    def test_desktop_contract_and_admission(self):
        methods = json.loads((support.ROOT / "desktop/native/rpc-methods.json").read_text(encoding="utf-8"))
        self.assertIn("save_device_report", methods)
        self.assertIsNone(main._rpc_module("save_device_report"), "no module: shown on every console")
        self.assertTrue(main._pause_exempt("save_device_report", (), {}))

    async def test_saves_on_a_console_where_no_module_applies(self):
        with tempfile.TemporaryDirectory() as settings, \
                patch.object(main.decky, "DECKY_PLUGIN_SETTINGS_DIR", settings), \
                patch.object(main, "_installed_standalone_plugins", return_value=[]):
            plugin = support.LegacyPlugin()
            plugin._applicable = {name: False for name in main.MODULE_NAMES}
            plugin._device = {"console": None, "model": None}
            plugin._tdp = types.SimpleNamespace(get_caps=AsyncMock(return_value={"tdp_available": False}))
            account = types.SimpleNamespace(pw_name=USER, pw_dir="/home/" + USER, pw_uid=1000, pw_gid=1000)
            with patch.object(main, "_session_account", return_value=account), \
                    patch.object(device_report, "collect", return_value="text") as collect, \
                    patch.object(device_report, "save", return_value="/home/x/Desktop/r.txt") as save:
                results = await asyncio.gather(plugin.save_device_report(), plugin.save_device_report())
            self.assertEqual(results, [{"success": True, "path": "/home/x/Desktop/r.txt"}] * 2)
            collect.assert_called_once()  # Two clicks share one report.
            sent = collect.call_args.args[0]
            self.assertEqual(sent["user"], USER)
            self.assertFalse(sent["modules"]["tdp"]["applicable"])
            self.assertIsNone(sent["fan"]["telemetry"], "no Fan session: no EC readings to pass on")
            save.assert_called_once_with("text", "/home/" + USER, 1000, 1000)
            with patch.object(main, "_session_account", return_value=None):
                self.assertFalse((await plugin.save_device_report())["success"])
            await plugin._unload()

    async def test_a_click_after_a_timed_out_one_shares_the_running_collection(self):
        with tempfile.TemporaryDirectory() as settings, \
                patch.object(main.decky, "DECKY_PLUGIN_SETTINGS_DIR", settings), \
                patch.object(main, "_installed_standalone_plugins", return_value=[]):
            plugin = support.LegacyPlugin()
            plugin._applicable = {name: False for name in main.MODULE_NAMES}
            plugin._device = {"console": None, "model": None}
            plugin._tdp = types.SimpleNamespace(get_caps=AsyncMock(return_value={"tdp_available": False}))
            account = types.SimpleNamespace(pw_name=USER, pw_dir="/home/" + USER, pw_uid=1000, pw_gid=1000)
            release = threading.Event()
            def slow(*_args, **_kwargs):
                release.wait(5)
                return "text"
            with patch.object(main, "_session_account", return_value=account), \
                    patch.object(device_report, "TOTAL_SECONDS", -4.8), \
                    patch.object(device_report, "collect", side_effect=slow) as collect, \
                    patch.object(device_report, "save", return_value="/home/x/Desktop/r.txt"):
                self.assertFalse((await plugin.save_device_report())["success"], "timed out")
                second = asyncio.ensure_future(plugin.save_device_report())
                await asyncio.sleep(0.05)
                release.set()
                self.assertEqual(await second, {"success": True, "path": "/home/x/Desktop/r.txt"})
            collect.assert_called_once()
            await plugin._unload()


if __name__ == "__main__":
    unittest.main()

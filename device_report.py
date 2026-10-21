# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Device report: a read-only text file of this console's hardware and firmware details for a bug report.

Only the allowlisted fields below are collected, then the whole text passes a privacy filter. Nothing is
written to hardware, no controller is opened and no module lock is taken; firmware-backed sysfs reads
(ACPI/WMI methods) hold module_runtime.firmware_lock one at a time like every other reader. The EC is never
sent a command: its memory window is read through /dev/mem (PROT_READ) only where the live DSDT declares it
(_ec). The DSDT itself is appended after the privacy filter (_dsdt). Each section fails on its own
("unavailable: <reason>"), external programs get fixed argv and a time limit, and the whole collection
stops starting new sections after TOTAL_SECONDS.
"""

import base64
import collections
import datetime
import glob
import hashlib
import json
import mmap
import os
import platform
import re
import stat
import subprocess
import time
import zlib

import module_runtime
import rgb_backend
import tdp_backend
from fan_guardian import validated_firmware
from system_process import run

TOTAL_SECONDS = 15.0
COMMAND_SECONDS = 4.0
ROOT = ""  # Prefix of every absolute path read; tests point it at a fake tree.
BUSCTL = "/usr/bin/busctl"
IW = "/usr/bin/iw"
PACMAN = "/usr/bin/pacman"
MAX_ENTRIES = 64  # Per listed directory: a report stays readable and bounded.
PRIVACY_NOTE = "Read-only report. No serial numbers, MAC addresses, network names or user names."
DMI_FIELDS = ("sys_vendor", "product_name", "product_family", "product_version", "board_vendor", "board_name",
              "bios_vendor", "bios_version", "bios_date", "ec_firmware_release")
OS_FIELDS = ("NAME", "VERSION_ID", "BUILD_ID", "VARIANT_ID")
ATTRIBUTE_FIELDS = ("type", "current_value", "default_value", "min_value", "max_value", "scalar_increment",
                    "possible_values")
BATTERY_FIELDS = ("charge_types", "charge_behaviour", "charge_control_end_threshold", "capacity", "status",
                  "manufacturer", "model_name")
USB_VENDORS = frozenset({"17ef", "1a86", "28de"})
HID_VENDORS = frozenset({"17EF", "1A86"})
# Kernel-generic entries of a HID device directory; the rest is what its driver adds.
HID_GENERIC = frozenset({"driver", "subsystem", "power", "uevent", "report_descriptor", "modalias", "country",
                         "input", "hidraw", "leds", "firmware_node", "consumers", "suppliers", "waiting_for_supplier"})
COMPOSITE_PATH = re.compile(r"/org/shadowblip/InputPlumber/CompositeDevice[0-9]+")
EDID_HEADER = b"\x00\xff\xff\xff\xff\xff\xff\x00"
_MAC = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}(?![0-9A-Fa-f])")
_PRIVATE_LINE = re.compile(r"serial|uuid|asset|ssid", re.IGNORECASE)
_HOME = re.compile(r"/home/[^/\s]+")
# Kernel names that never carry an identifier; a HID device battery is named after HID_UNIQ (often a USB serial).
_SUPPLY_NAME = re.compile(r"BAT\d*|AC\d*|ACAD|ADP\d*|ucsi-source-psy-[A-Za-z0-9:._-]+")
_HWMON_NAME = re.compile(r"acpitz|k10temp|amdgpu|nvme|spd5118|lenovo_wmi_\w+|mt79\d\d_phy\d+|iwlwifi_\d+"
                         r"|BAT\d*|AC\d*|ACAD|ADP\d*|ucsi_source_psy_\w+")
RYZENADJ_SECONDS = 1.0  # With its 2 s kill wait, below the 4 s that TDP's restore waits for the ryzenadj lock.
DSDT = "/sys/firmware/acpi/tables/DSDT"
DEV_MEM = "/dev/mem"
DSDT_LIMIT = 512 << 10  # Lenovo's Legion Go DSDTs are 83-116 KiB (41-51 KiB compressed).
# EC memory as the Legion Go family's DSDTs name it: ERAM (Go 2, Go S) or ECMM (Go 1) is the EC's RAM
# window; EFAN (Go 1 fan table) and SMA2 (EC mailbox) are read only inside that window's page. Other
# SystemMemory regions of the EC device (GPIO, CMOS, eSPI registers) are never read.
EC_WINDOWS = (b"ERAM", b"ECMM")
EC_EXTRA = (b"EFAN", b"SMA2")
# Blanked bytes: a field named like a serial number (BTSN, SNUM, SER*) or a unique ID (*UID*), and any field
# wider than 32 bits (SMBus and battery buffers that can hold a serial or name string).
_EC_PRIVATE = re.compile(rb"SN|SER|UID")
FAN_TELEMETRY = ("ec_version", "rpm", "effective", "override", "ec_temperature", "power_mode")


class _Unavailable(Exception):
    """A short, already user-readable reason."""


def _reason(error):
    if isinstance(error, FileNotFoundError):
        return "not present"
    if isinstance(error, subprocess.TimeoutExpired):
        return "timed out"
    text = str(error).splitlines()[0][:80] if str(error) else ""
    return text if isinstance(error, _Unavailable) else f"{type(error).__name__}{': ' + text if text else ''}"


def _path(path):
    return ROOT + path


def _read(path, limit=512, firmware=False):
    """A small sysfs/proc text value, never through a final symlink. `firmware`: the read runs an ACPI/WMI method."""
    if firmware:
        with module_runtime.firmware_lock:
            return module_runtime.read_sysfs_text(path, limit)
    return module_runtime.read_sysfs_text(path, limit)


def _text_file(path, limit):
    """A small system text file (/etc/os-release is a link into /usr/lib)."""
    with open(_path(path), encoding="utf-8", errors="replace") as handle:
        return handle.read(limit)


def _value(path, limit=512, firmware=False):
    try:
        return _read(path, limit, firmware) or "(empty)"
    except Exception as exc:
        return f"unavailable: {_reason(exc)}"


def _entries(pattern):
    found = sorted(glob.glob(_path(pattern)))
    return found[:MAX_ENTRIES]


def _driver(directory):
    link = os.path.join(directory, "driver")
    return os.path.basename(os.readlink(link)) if os.path.islink(link) else "none"


class _Context:
    def __init__(self, facts, deadline, stopped=lambda: False):
        self.facts = facts
        self.deadline = deadline
        self.stopped = stopped
        self._dsdt = None

    def dsdt(self):
        """The live DSDT, read once for the EC section and the appended table."""
        if self._dsdt is None:
            try:
                data = rgb_backend.read_dsdt(_path(DSDT), DSDT_LIMIT)
                if len(data) > DSDT_LIMIT:
                    raise _Unavailable(f"larger than {DSDT_LIMIT >> 10} KiB")
                if data[:4] != b"DSDT" or int.from_bytes(data[4:8], "little") != len(data):
                    raise _Unavailable("not a complete DSDT")
                self._dsdt = data
            except Exception as exc:
                self._dsdt = exc
        if isinstance(self._dsdt, Exception):
            raise self._dsdt
        return self._dsdt

    def command(self, argv):
        """stdout of a fixed program, inside both its own and the report's time limit."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0.2:
            raise _Unavailable("report time limit reached")
        if not os.path.isfile(argv[0]):
            raise _Unavailable(f"{os.path.basename(argv[0])} is not installed")
        result = run(argv, min(COMMAND_SECONDS, remaining), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise _Unavailable(f"{os.path.basename(argv[0])} exited with {result.returncode}")
        return result.stdout[:262144]

    def busctl(self, *args):
        """A system-bus query that never starts a service; returns busctl's JSON `data`."""
        reply = json.loads(self.command([BUSCTL, "--system", "--auto-start=no", "--json=short", *args]))
        return reply["data"]


# ── Sections: each returns its lines; an exception becomes one "unavailable" line ──────────

def _dmi(context):
    return [f"{name}: {_value(_path('/sys/class/dmi/id/' + name), 128)}" for name in DMI_FIELDS]


def _system(context):
    lines = []
    try:
        release = {}
        for line in _text_file("/etc/os-release", 8192).splitlines():
            key, _, value = line.partition("=")
            release[key.strip()] = value.strip().strip('"')
        lines += [f"{key}: {release.get(key, 'not set')}" for key in OS_FIELDS]
    except Exception as exc:
        lines.append(f"os-release: unavailable: {_reason(exc)}")
    lines.append(f"kernel: {platform.release()}")
    try:
        version = context.busctl("get-property", "org.shadowblip.InputPlumber", "/org/shadowblip/InputPlumber/Manager",
                                 "org.shadowblip.InputManager", "Version")
    except Exception:
        try:  # Not running, or a version without the property: the installed package.
            version = context.command([PACMAN, "-Q", "inputplumber"]).split()[-1] + " (installed package)"
        except Exception as exc:
            version = f"unavailable: {_reason(exc)}"
    lines.append(f"InputPlumber: {version}")
    try:
        fwupd = context.busctl("get-property", "org.freedesktop.fwupd", "/", "org.freedesktop.fwupd", "DaemonVersion")
    except Exception as exc:
        fwupd = f"unavailable: {_reason(exc)} (fwupd may not be running)"
    lines.append(f"fwupd: {fwupd}")
    try:
        model = next((line.partition(":")[2].strip() for line in _text_file("/proc/cpuinfo", 65536).splitlines()
                      if line.startswith("model name")), "not reported")
    except Exception as exc:
        model = f"unavailable: {_reason(exc)}"
    lines.append(f"CPU: {model}")
    return lines


def _companion(context):
    facts = context.facts
    device = facts.get("device") or {}
    lines = [f"legion_device: console={device.get('console')} model={device.get('model')} "
             f"firmware={device.get('firmware')} power_led={device.get('power_led')} "
             f"controller_driver={device.get('controller_driver')}"]
    for name, entry in (facts.get("modules") or {}).items():
        state = " ".join(f"{key}={entry.get(key)}" for key in ("applicable", "enabled", "ready"))
        flags = "".join(f" {key}" for key in ("pending", "starting", "maintenance") if entry.get(key))
        error = str(entry.get("error") or "").replace("\n", " ")[:100]
        if error and name == "wifi":
            error = "(text left out: it can name a network)"
        lines.append(f"module {name}: {state}{flags}{' error=' + error if error else ''}")
    restarts = facts.get("inputplumber_restarts")
    lines.append("InputPlumber restarts for a lost controller (this Companion start): "
                 + ("not watched on this console" if restarts is None else str(restarts)))
    caps = facts.get("tdp_caps")
    if isinstance(caps, dict):
        lines.append("TDP: " + " ".join(f"{key}={caps.get(key)}" for key in (
            "tdp_available", "wmi", "extras", "min", "std", "max")))
        if caps.get("tdp_error"):
            lines.append(f"TDP error: {caps['tdp_error']}")
    else:
        lines.append(f"TDP: unavailable: {caps}")
    fan = facts.get("fan") or {}
    lines.append(f"Fan: supported={fan.get('supported')} beta={fan.get('beta')}"
                 + (f" error={str(fan['error'])[:100]}" if fan.get("error") else ""))
    try:  # The remembered file only; no EC access.
        pairs = sorted(validated_firmware())
        lines.append("Fan self-tested BIOS/EC pairs: " + (", ".join(f"{bios}/{ec}" for bios, ec in pairs) or "none"))
    except Exception as exc:
        lines.append(f"Fan self-tested BIOS/EC pairs: unavailable: {_reason(exc)}")
    return lines


def _firmware_attributes(context):
    lines = []
    for attribute in _entries("/sys/class/firmware-attributes/lenovo-wmi-other-*/attributes/*"):
        if time.monotonic() >= context.deadline:
            lines.append("unavailable: report time limit reached")
            break
        if not os.path.isdir(attribute):
            continue
        device = os.path.basename(os.path.dirname(os.path.dirname(attribute)))
        values = [f"{leaf}={_value(os.path.join(attribute, leaf), 1024, True)}" for leaf in ATTRIBUTE_FIELDS
                  if os.path.exists(os.path.join(attribute, leaf))]
        lines.append(f"{device}/{os.path.basename(attribute)}: {'; '.join(values) or 'no values'}")
    if not lines:
        lines.append("none")
    for name in ("platform_profile", "platform_profile_choices"):
        lines.append(f"{name}: {_value(_path('/sys/firmware/acpi/' + name), 512, True)}")
    return lines


def _power(context):
    lines = []
    for number, supply in enumerate(_entries("/sys/class/power_supply/*")):
        kind = _value(os.path.join(supply, "type"))
        fields = [f"type={kind}"]
        name = os.path.basename(supply)
        if not _SUPPLY_NAME.fullmatch(name) and _value(os.path.join(supply, "scope"), 16) == "Device":
            lines.append(f"peripheral{number} {' '.join(fields)}")
            continue
        if kind == "Mains":
            fields.append(f"online={_value(os.path.join(supply, 'online'), firmware=True)}")
        if name.startswith("BAT"):
            fields += [f"{field}={_value(os.path.join(supply, field), firmware=True)}" for field in BATTERY_FIELDS
                       if os.path.exists(os.path.join(supply, field))]
        lines.append(f"{name}: {' '.join(fields)}")
    return lines or ["none"]


_LIMIT_ROW = re.compile(r"\|\s*((?:STAPM|PPT|THM|TDC|EDC)[A-Z0-9 _]*LIMIT[A-Z0-9 _]*?)\s*\|\s*([^|]*?)\s*\|")


def _ryzenadj(context):
    # The pinned helper only, never downloaded here; its lock serialises SMU access with TDP.
    tdp = (context.facts.get("modules") or {}).get("tdp") or {}
    if not (tdp.get("enabled") and tdp.get("ready")):
        return ["skipped: TDP control is off"]  # No SMU request while the user has TDP control off.
    if not tdp_backend._helper_verified():
        return ["not installed or not verified (the report does not download it)"]
    if context.stopped():
        return ["unavailable: Companion is stopping"]
    if not tdp_backend._ryzenadj_lock.acquire(blocking=False):  # Never delays a TDP apply or restore.
        return ["unavailable: busy"]
    try:
        code, out, error = tdp_backend._run_ryzenadj(
            ["--info"], timeout=min(RYZENADJ_SECONDS, max(.2, context.deadline - time.monotonic())))
    finally:
        tdp_backend._ryzenadj_lock.release()
    if code != 0 and "CPU Family" not in out:
        return [f"unavailable: {error.strip()[:80] or f'ryzenadj exited with {code}'}"]
    lines = [line.strip() for line in out.splitlines() if line.strip().startswith("CPU Family")]
    lines += [f"{match.group(1)}: {match.group(2)}" for match in map(_LIMIT_ROW.search, out.splitlines()) if match]
    return lines or ["no limit rows reported"]


def _controllers(context):
    lines = []
    for device in _entries("/sys/bus/usb/devices/*"):
        try:
            vendor = _read(os.path.join(device, "idVendor"), 8).lower()
        except Exception:
            continue
        if vendor not in USB_VENDORS:
            continue
        product = _value(os.path.join(device, "idProduct"), 8)
        drivers = sorted({_driver(interface) for interface in glob.glob(os.path.join(device, "*:*"))})
        lines.append(f"USB {vendor}:{product} product={_value(os.path.join(device, 'product'), 128)} "
                     f"bcdDevice={_value(os.path.join(device, 'bcdDevice'), 8)} drivers={','.join(drivers) or 'none'}")
    for device in _entries("/sys/bus/hid/devices/*"):
        parts = os.path.basename(device).split(":")
        if len(parts) < 3 or parts[1].upper() not in HID_VENDORS:
            continue
        name = "unknown"
        try:
            for line in _read(os.path.join(device, "uevent"), 4096).splitlines():
                if line.startswith("HID_NAME="):  # Not HID_UNIQ/HID_PHYS: they can carry a serial or MAC.
                    name = line[9:]
        except Exception as exc:
            name = f"unavailable: {_reason(exc)}"
        driver = _driver(device)
        lines.append(f"HID {os.path.basename(device)} name={name} driver={driver}")
        if driver.startswith("hid-lenovo-go"):
            # Names only: reading this driver's values sends a request to the controller MCU.
            for entry in sorted(os.listdir(device))[:MAX_ENTRIES]:
                path = os.path.join(device, entry)
                if entry in HID_GENERIC or os.path.islink(path):
                    continue
                if os.path.isdir(path):
                    lines.append(f"  {entry}/: {' '.join(sorted(os.listdir(path))[:MAX_ENTRIES])}")
                else:
                    lines.append(f"  {entry}")
    for led in _entries("/sys/class/leds/*"):
        lines.append(f"LED {os.path.basename(led)}")
        if ":rgb:" in os.path.basename(led):
            for path in sorted(glob.glob(os.path.join(led, "*_index")) + glob.glob(os.path.join(led, "*_range"))):
                lines.append(f"  {os.path.basename(path)}: {_value(path)}")
    for device in _entries("/sys/bus/iio/devices/*"):
        lines.append(f"IIO {os.path.basename(device)}: {_value(os.path.join(device, 'name'), 128)}")
    try:
        tree = context.command([BUSCTL, "--system", "--auto-start=no", "tree", "org.shadowblip.InputPlumber",
                                "--list", "--no-pager"])
        paths = [line.strip() for line in tree.splitlines() if COMPOSITE_PATH.fullmatch(line.strip())]
        for path in paths[:16]:
            try:
                name = context.busctl("get-property", "org.shadowblip.InputPlumber", path,
                                      "org.shadowblip.Input.CompositeDevice", "Name")
            except Exception as exc:
                name = f"unavailable: {_reason(exc)}"
            lines.append(f"InputPlumber {path.rsplit('/', 1)[1]}: {name}")
        if not paths:
            lines.append("InputPlumber: no CompositeDevice")
    except Exception as exc:
        lines.append(f"InputPlumber devices: unavailable: {_reason(exc)}")
    return lines


def _fan(context):
    lines = []
    for monitor in _entries("/sys/class/hwmon/*"):
        files = sorted(os.path.basename(path) for path in glob.glob(os.path.join(monitor, "fan*"))
                       + glob.glob(os.path.join(monitor, "pwm*")))
        name = _value(os.path.join(monitor, "name"), 64)
        if not (_HWMON_NAME.fullmatch(name) or name.startswith("unavailable")):
            name = "<removed>"
        line = f"{os.path.basename(monitor)} name={name}"
        if files:
            line += " files=" + ",".join(files)
            line += "".join(f" {name}={_value(os.path.join(monitor, name), 32, True)}"
                            for name in files if re.fullmatch(r"fan[0-9]+_input", name))
        lines.append(line)
    return lines or ["none"]


def _edid_identity(data):
    """Manufacturer, product code and the 0xFC monitor name only; never the serial (bytes 12-15, 0xFF)."""
    if len(data) < 128 or data[:8] != EDID_HEADER:
        raise _Unavailable("not a valid EDID")
    word = data[8] << 8 | data[9]
    maker = "".join(chr(64 + (word >> shift & 31)) for shift in (10, 5, 0))
    product = data[10] | data[11] << 8
    name = ""
    for offset in (54, 72, 90, 108):
        block = data[offset:offset + 18]
        if block[:3] == b"\x00\x00\x00" and block[3] == 0xFC:
            name = block[5:18].split(b"\n")[0].decode("ascii", errors="replace").strip()
    return f"manufacturer={maker} product=0x{product:04X} name={name or 'not reported'}"


def _display(context):
    lines = []
    for edid in _entries("/sys/class/drm/card*-eDP-*/edid"):
        try:
            fd = os.open(edid, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise _Unavailable("not a regular file")
                identity = _edid_identity(os.read(fd, 1024))
            finally:
                os.close(fd)
        except Exception as exc:
            identity = f"unavailable: {_reason(exc)}"
        lines.append(f"{os.path.basename(os.path.dirname(edid))}: {identity}")
    if not lines:
        lines.append("eDP EDID: none")
    try:
        names = sorted(os.listdir(_path("/usr/share/gamescope/scripts/00-gamescope/displays")))[:MAX_ENTRIES]
        lines.append("gamescope display scripts: " + (", ".join(names) or "none"))
    except Exception as exc:
        lines.append(f"gamescope display scripts: unavailable: {_reason(exc)}")
    return lines


_BAND = re.compile(r"^\s*Band (\d+):")
_FREQUENCY = re.compile(r"^\s*\*\s*([0-9]+)(?:\.[0-9]+)? MHz \[[0-9]+\](.*)$")


def _network(context):
    lines = []
    for device in _entries("/sys/bus/pci/devices/*"):
        try:
            if not _read(os.path.join(device, "class"), 16).startswith("0x02"):
                continue
            ids = f"{_read(os.path.join(device, 'vendor'), 16)[2:]}:{_read(os.path.join(device, 'device'), 16)[2:]}"
        except Exception:
            continue
        lines.append(f"PCI {os.path.basename(device)} {ids} driver={_driver(device)}")
    if not lines:
        lines.append("PCI network controllers: none")
    try:
        # Only which bands have a usable channel: the channel lists would name the country.
        usable, in_band = set(), False
        for line in context.command([IW, "phy"]).splitlines():
            if _BAND.match(line):
                in_band = True
            elif in_band and (match := _FREQUENCY.match(line)) and "disabled" not in match.group(2):
                mhz = int(match.group(1))
                usable.add("2.4 GHz" if mhz < 3000 else "5 GHz" if mhz < 5925 else "6 GHz" if mhz <= 7125 else "")
        lines.append("Wi-Fi bands: " + (", ".join(band for band in ("2.4 GHz", "5 GHz", "6 GHz") if band in usable)
                                        or "none usable"))
    except Exception as exc:
        lines.append(f"Wi-Fi bands: unavailable: {_reason(exc)}")
    return lines


def _fwupd(context):
    reply = context.busctl("call", "org.freedesktop.fwupd", "/", "org.freedesktop.fwupd", "GetDevices")
    lines = []
    for device in reply[0] if reply else []:
        value = lambda key: (device.get(key) or {}).get("data")
        plugin = str(value("Plugin") or "")
        if not plugin.startswith(("legion", "lenovo")):
            continue
        flags = value("Flags")
        guids = value("Guid")
        lines.append(f"{value('Name')}: plugin={plugin} version={value('Version')} "
                     f"bootloader={value('VersionBootloader')} "
                     f"flags={hex(flags) if type(flags) is int else flags} "
                     f"guids={','.join(guids) if isinstance(guids, list) else guids}")
    return lines or ["no Legion or Lenovo devices"]


def _events(context):
    from diagnostics_reader import read_events
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    counts = collections.Counter()
    for record in read_events(context.facts["log_dir"])["events"]:
        if record["level"] in ("warning", "error", "critical") and \
                datetime.datetime.fromisoformat(record["time"]) >= since:
            counts[(record["component"], record["event"], record["level"])] += 1
    return [f"{component}.{event} {level}: {count}" for (component, event, level), count in sorted(counts.items())] \
        or ["no warnings or errors"]


def _read_physical(base, length):
    """`length` bytes at physical address `base`: /dev/mem opened read-only and mapped PROT_READ, read one byte
    load at a time (the DSDT declares these fields ByteAcc or AnyAcc). A memory read sends the EC no command."""
    page = base - base % mmap.PAGESIZE
    fd = os.open(_path(DEV_MEM), os.O_RDONLY | getattr(os, "O_SYNC", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        mapped = mmap.mmap(fd, base - page + length, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ, offset=page)
        try:
            return bytes([mapped[i] for i in range(base - page, base - page + length)])
        finally:
            mapped.close()
    finally:
        os.close(fd)


def _ec_regions(data):
    """(name, base, length) of the EC memory this DSDT declares: its one RAM window and the EC_EXTRA regions in
    the same page. IndexError/KeyError: a table this parser does not understand."""
    windows = [(name, *region) for name in EC_WINDOWS if (region := rgb_backend.aml_region(data, name))]
    if len(windows) != 1:
        return []
    page = windows[0][1] // mmap.PAGESIZE
    extra = [(name, *region) for name in EC_EXTRA if (region := rgb_backend.aml_region(data, name))]
    return windows + [region for region in extra if region[1] // mmap.PAGESIZE == page]


def _ec_dump(data, name, base, length):
    label = name.decode("ascii", "replace")
    fields = rgb_backend.aml_fields(data, name)
    if fields is None:
        return [f"{label}: skipped: its DSDT field map is not understood, so private fields cannot be blanked"]
    private, blanked = set(), set()
    for field, bit, width in fields:
        if width > 32 or _EC_PRIVATE.search(field):
            private.update(range(bit // 8, (bit + width + 7) // 8))
            blanked.add(field.decode("ascii", "replace"))
    raw = _read_physical(base, length)
    lines = [f"{label} base=0x{base:X} length=0x{length:X}; blanked (--): {' '.join(sorted(blanked)) or 'none'}"]
    for row in range(0, length, 16):
        lines.append(f"  {label}+{row:02X}: " + " ".join(
            "--" if i in private else f"{raw[i]:02X}" for i in range(row, min(row + 16, length))))
    return lines


def _ec(context):
    facts = context.facts
    if not (facts.get("device") or {}).get("console"):
        return ["skipped: not a Legion Go console"]
    fan = facts.get("fan") or {}
    if fan.get("supported") and ((facts.get("modules") or {}).get("fan") or {}).get("enabled"):
        # The Go 2's Fan module owns EC access: no read beside it, only what its own session already read.
        telemetry = fan.get("telemetry") or {}
        readings = " ".join(f"{key}={telemetry[key]}" for key in FAN_TELEMETRY if key in telemetry)
        return ["skipped: the Fan module is on, so the report reads no EC memory beside it",
                f"Fan Control's last EC readings: {readings or 'none (no Fan Control session)'}"]
    data = context.dsdt()
    try:
        regions = _ec_regions(data)
    except (IndexError, KeyError):
        raise _Unavailable("the DSDT's EC regions are not understood") from None
    if not regions:
        return ["skipped: the DSDT declares no EC memory window (ERAM or ECMM)"]
    lines = []
    for name, base, length in regions:
        if not 0 < length <= 256 or base % mmap.PAGESIZE + length > mmap.PAGESIZE:
            lines.append(f"{name.decode('ascii', 'replace')}: skipped: unexpected size 0x{length:X}")
            continue
        try:
            lines += _ec_dump(data, name, base, length)
        except Exception as exc:
            lines.append(f"{name.decode('ascii', 'replace')}: unavailable: {_reason(exc)}")
    lines.append("Not read: EC registers outside these DSDT regions (such as the Go 2 fan registers); "
                 "reading them needs EC port transactions.")
    return lines


DSDT_BEGIN = "-----BEGIN DSDT (zlib, base64)-----"
DSDT_END = "-----END DSDT-----"
DSDT_RESTORE = ("python3 -c \"import sys,base64,zlib;t=open(sys.argv[1]).read();"
                f"t=t.split('{DSDT_BEGIN}')[-1].split('{DSDT_END}')[0];"
                "open('DSDT.aml','wb').write(zlib.decompress(base64.b64decode(t)))\" <report file>")


def _dsdt(context):
    """The live DSDT, compressed and in base64. It is appended after scrub(): the filter could break the base64
    (random text can contain "ssid" or the user's name), and it has nothing to remove. The DSDT is the BIOS's
    ACPI code, the same on every console with that BIOS apart from addresses patched in at boot: serial numbers
    live in SMBIOS, MAC addresses and user names in the OS, none of them in this table."""
    data = context.dsdt()
    return [f"size: {len(data)} bytes, sha256: {hashlib.sha256(data).hexdigest()}",
            "Restore it as DSDT.aml (then: iasl -d DSDT.aml):", DSDT_RESTORE, DSDT_BEGIN,
            *base64.encodebytes(zlib.compress(data, 9)).decode("ascii").split(), DSDT_END]


SECTIONS = (("DMI", _dmi), ("System", _system), ("Companion", _companion),
            ("Firmware attributes", _firmware_attributes), ("Battery and power", _power),
            ("RyzenAdj limits", _ryzenadj), ("Controllers and input", _controllers), ("Fan sensors", _fan),
            ("EC memory (read-only)", _ec), ("Display", _display), ("Network", _network),
            ("fwupd devices", _fwupd), ("Companion events, last 24 hours", _events))
DSDT_TITLE = "ACPI DSDT (firmware table, not filtered)"


def scrub(text, user=""):
    """The last privacy pass over everything collected: MAC addresses, any line naming a serial, UUID, asset
    tag or SSID, home directories and the console user's name become <removed> or /home/<user>."""
    lines = []
    for line in text.splitlines():
        if _PRIVATE_LINE.search(line):
            lines.append("<removed>")
            continue
        line = _HOME.sub("/home/<user>", _MAC.sub("<removed>", line))
        if user:
            line = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(user)}(?![A-Za-z0-9_])", "<removed>", line)
        lines.append(line)
    return "\n".join(lines) + "\n"


def collect(facts, seconds=TOTAL_SECONDS, stopped=lambda: False):
    """The report text. `facts`: version, device, modules, inputplumber_restarts, tdp_caps, fan, log_dir, user.
    `stopped()`: Companion is stopping, so no further section starts."""
    context = _Context(facts, time.monotonic() + seconds, stopped)

    def run_sections(sections):
        lines = []
        for title, section in sections:
            lines += ["", f"== {title} =="]
            if time.monotonic() >= context.deadline or stopped():
                lines.append("unavailable: report time limit reached" if not stopped()
                             else "unavailable: Companion is stopping")
                continue
            try:
                lines += section(context)
            except Exception as exc:
                lines.append(f"unavailable: {_reason(exc)}")
        return lines
    body = run_sections(SECTIONS)
    created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    header = (f"Legion Go Companion device report\nCompanion version: {facts.get('version')}\n"
              f"Created: {created}\n{PRIVACY_NOTE}\n")
    return header + scrub("\n".join(body), facts.get("user") or "") + "\n".join(
        run_sections(((DSDT_TITLE, _dsdt),))) + "\n"


def save(text, home, uid, gid):
    """Write the report as a new file in the console user's Desktop folder, owned by that user; the path."""
    from companion_updates import open_user_dir
    directory, path = open_user_dir(home, uid, gid, "DESKTOP", "Desktop")
    stem = "LegionGoCompanion-device-report-" + datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%d-%H%M%S")
    try:
        for attempt in range(1, 11):  # A second report in the same second gets -2, -3, ...
            name = stem + (f"-{attempt}" if attempt > 1 else "") + ".txt"
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                             dir_fd=directory)
                break
            except FileExistsError:
                if attempt == 10:
                    raise
        try:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, 0o644)
            data = text.encode("utf-8")
            while data:
                data = data[os.write(fd, data):]
            os.fsync(fd)
        except BaseException:
            os.unlink(name, dir_fd=directory)  # Never leave a half-written report behind.
            raise
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    return path + "/" + name

# SPDX-License-Identifier: BSD-3-Clause

"""Shared test fixture: a stub `decky`, isolated settings and install dirs.

Import this before `main` or any backend.
"""
import contextlib
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_ROOT = Path(tempfile.mkdtemp(prefix="legiongo2companion-tests-"))
CURRENT_SETTINGS = SETTINGS_ROOT / "LegionGo2Companion"
CURRENT_SETTINGS.mkdir()


class Logger:
    def info(self, _message):
        pass

    def warning(self, _message):
        pass

    def error(self, _message):
        pass

    def debug(self, _message):
        pass


async def emit(_event, *_args):
    pass


decky = types.ModuleType("decky")
decky.logger = Logger()
decky.emit = emit
decky.DECKY_PLUGIN_SETTINGS_DIR = str(CURRENT_SETTINGS)
sys.modules["decky"] = decky


for unix_only in ("fcntl", "pwd"):
    try:
        __import__(unix_only)
    except ImportError:
        stub = types.ModuleType(unix_only)
        if unix_only == "pwd":
            stub.struct_passwd = object
            stub.getpwall = lambda: []
            stub.getpwnam = lambda _name: None
        sys.modules[unix_only] = stub

sys.path.insert(0, str(ROOT))
import display_backend  # noqa: E402
import main  # noqa: E402
# Keep the real inventory guard, but give it an isolated installation parent.
# The checkout's siblings can include unrelated, unreadable system directories.
ISOLATED_INSTALL = SETTINGS_ROOT / "plugins" / "LegionGo2Companion"
ISOLATED_INSTALL.mkdir(parents=True)
(ISOLATED_INSTALL / "plugin.json").write_bytes((ROOT / "plugin.json").read_bytes())
main.PLUGIN_DIR = str(ISOLATED_INSTALL)
# No file sink unless a test injects its own directory: never the real /var/log.
DEFAULT_LOG_DIR, main.diagnostics.LOG_DIR = main.diagnostics.LOG_DIR, None
import battery_backend  # noqa: E402
import controller_backend  # noqa: E402
import remap_backend  # noqa: E402
import rgb_backend  # noqa: E402
import tdp_backend  # noqa: E402
import vibration_backend  # noqa: E402
import wifi_backend  # noqa: E402

# The suite drives TDP on fake sysfs as a Legion Go 2; test_tdp_core covers the gate.
tdp_backend._legion_go_2 = lambda: True


# Stuck-helper harness: run a harmless stalled child where the code under test would spawn its
# real helper, keeping the caller's real pipes, timeouts, kill and reap logic.
STALL_SOURCE = "import time; time.sleep(30)"


def stalled_child_launcher(source=STALL_SOURCE, children=None):
    """Popen replacement running `source` in an isolated interpreter; evaluate it before patching Popen."""
    import subprocess
    original = subprocess.Popen

    def launch(_args, **kwargs):
        child = original([sys.executable, "-I", "-c", source], **kwargs)
        if children is not None:
            children.append(child)
        return child
    return launch


@contextlib.contextmanager
def stalled_child():
    """A started stalled helper with the pipes the real download/catalog helpers use; killed and reaped on exit."""
    import subprocess
    with subprocess.Popen([sys.executable, "-I", "-c", STALL_SOURCE], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as child:
        try:
            yield child
        finally:
            if child.poll() is None:
                child.kill()


def reap(children):
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)


def power_led_dsdt(base=0xFEEC2300, space=0x00, length=0xFF, fields=None):
    """Synthetic DSDT with the Go 2 power-LED layout: LPBL = ERAM byte 0x10 bit 6.

    A decoy Field over another region also names LPBL; only ERAM may count.
    """
    def encode(value):  # AML PkgLength, 1 or 2 bytes.
        return bytes([value]) if value < 64 else bytes([0x40 | value & 0x0F, value >> 4])

    def pkglen(size):  # A package's PkgLength also counts its own bytes.
        return encode(size + 1 if size + 1 < 64 else size + 2)
    if fields is None:
        fields = [(None, 0x80)] + [(name, 1) for name in
                                   (b"LSTE", b"LID2", b"EKTS", b"DISV", b"SMAF", b"S4RF", b"LPBL", b"OSRT")]
    elements = b"".join((b"\x00" if name is None else name) + encode(width) for name, width in fields)
    decoy = b"GNVS\x01" + b"LPBL" + bytes([8])
    aml = (b"\x5b\x81" + pkglen(len(decoy)) + decoy
           + b"\x5b\x80ERAM" + bytes([space]) + b"\x0c" + base.to_bytes(4, "little") + b"\x0b" + length.to_bytes(2, "little")
           + b"\x5b\x81" + pkglen(len(elements) + 5) + b"ERAM\x11" + elements)
    return b"DSDT" + (36 + len(aml)).to_bytes(4, "little") + bytes(28) + aml


def dns_stall_source(marker):
    """Child source: bounded_http's --fetch helper whose resolver records `marker`, then stalls."""
    helper = str(ROOT / "bounded_http.py")
    return ("import runpy,socket,sys,time; from pathlib import Path\n"
            "def resolver(*args,**kwargs):\n"
            f" Path({str(marker)!r}).write_text('entered')\n"
            " time.sleep(30)\n"
            " raise OSError('controlled DNS failure')\n"
            "socket.getaddrinfo=resolver\n"
            f"sys.argv=[{helper!r},'--fetch']\n"
            f"runpy.run_path({helper!r},run_name='__main__')\n")

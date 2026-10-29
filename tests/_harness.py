# SPDX-License-Identifier: BSD-3-Clause
# Shared from DeckyKit (shared/tests/_harness.py): change it there, then `kit sync`.

"""Stand-in for the `decky` module DeckyLoader injects, so main.py imports outside the loader.
Import it first in every backend test: `import _harness  # noqa: F401`.

Sources: DeckyLS/FSR4Injector/DeckyReshade/DeckyVibranceHDR tests/_harness.py (module stand-in, temp folders),
Ayaneo3Companion tests/_harness.py and LegionGo2Companion tests/support.py (DECKY_PLUGIN_DIR, emit, a hung run
dumps every thread's stack).
Guarantees: tests never touch the real home, settings or runtime folders (all under one fresh temp folder);
decky.logger is a real logging.Logger (assertLogs works) that prints nothing; decky.emit records events in
decky.emitted; a suite that hangs for 8 minutes exits with every thread's stack instead of stalling CI.
Deliberately not here: stand-ins for plugin modules (a plugin's own test support adds them).
"""

import faulthandler
import logging
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
HANG_SECONDS = 480


def install():
    if "decky" in sys.modules:
        return sys.modules["decky"]
    faulthandler.dump_traceback_later(HANG_SECONDS, exit=True)
    decky = types.ModuleType("decky")
    decky.logger = logging.getLogger("decky-test")
    decky.logger.addHandler(logging.NullHandler())
    decky.logger.propagate = False
    base = tempfile.mkdtemp(prefix="decky-test-")
    decky.DECKY_PLUGIN_DIR = ROOT
    decky.DECKY_PLUGIN_SETTINGS_DIR = os.path.join(base, "settings")
    decky.DECKY_PLUGIN_RUNTIME_DIR = os.path.join(base, "runtime")
    decky.DECKY_PLUGIN_LOG_DIR = os.path.join(base, "logs")
    decky.DECKY_USER_HOME = os.path.join(base, "home")
    decky.DECKY_HOME = os.path.join(base, "homebrew")
    decky.emitted = []

    async def emit(event, *args):
        decky.emitted.append((event, *args))

    decky.emit = emit
    sys.modules["decky"] = decky
    return decky


install()

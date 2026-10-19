#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Check that a release ZIP is accepted by the in-plugin updaters users already run.

Usage: python3 scripts/check-legacy-updaters.py LegionGo2Companion-X.Y.Z.zip

Loads companion_updates.py from the 2.1.0 and 2.2.0 tags (needs the tags fetched)
and runs the same checks those versions run on a downloaded release: the asset
size limit and CompanionUpdates._validate_archive.
"""
import json
from pathlib import Path
import subprocess
import sys
import types
import zipfile

TAGS = ("2.1.0", "2.2.0")
ROOT = Path(__file__).resolve().parent.parent
# Neighbour modules that later updaters import; the archive check uses none of them.
STUBS = {
    "decky": {},
    "diagnostics": {"event": lambda *args, **kwargs: None},
    "bounded_http": {"checked_url": None, "open_http": None, "tls_context": None},
}


def legacy_updater(tag):
    source = subprocess.run(["git", "show", f"{tag}:companion_updates.py"], cwd=ROOT,
                            check=True, capture_output=True, text=True).stdout
    for name, attrs in STUBS.items():
        sys.modules.setdefault(name, types.ModuleType(name)).__dict__.update(attrs)
    module = types.ModuleType(f"companion_updates_{tag.replace('.', '_')}")
    sys.modules[module.__name__] = module
    exec(compile(source, f"{tag}:companion_updates.py", "exec"), module.__dict__)
    return module


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__.strip().splitlines()[2])
    path = Path(argv[1])
    size = path.stat().st_size
    with zipfile.ZipFile(path) as archive:
        version = json.loads(archive.read("LegionGo2Companion/plugin.json"))["version"]
    failed = False
    for tag in TAGS:
        updater = legacy_updater(tag)
        try:
            if not 0 < size <= updater.MAX_DOWNLOAD:  # the size gate in _parse_release
                raise ValueError(f"The release ZIP is {size} bytes; the limit is {updater.MAX_DOWNLOAD}.")
            with path.open("rb") as stream:
                updater.CompanionUpdates._validate_archive(stream, version)
        except Exception as error:  # any rejection, including a malformed ZIP
            failed = True
            print(f"FAIL {tag} updater rejects {path.name}: {type(error).__name__}: {error}")
        else:
            print(f"OK   {tag} updater accepts {path.name} ({version}, {size} bytes)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Verify an experimental or release ZIP against the current local build."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import sys
import zipfile

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'plugin.json').read_text(encoding='utf-8'))
archive = Path(sys.argv[1]) if len(sys.argv) > 1 else root / 'artifacts' / f"LegionGo2Companion-{manifest['version']}.zip"
required = {'main.py', 'dk_fs.py', 'dk_process.py', 'desktop_bridge.py', 'desktop_install.py', 'desktop/launch.py',
            'diagnostics.py', 'diagnostics_reader.py', 'controller_firmware.py', 'controller_firmware_catalog.py',
            'src/firmware.tsx', 'docs/CONTROLLER_FIRMWARE.md',
            'dist/index.js', 'dist/desktop/legiongo2companion', 'dist/desktop/native-manifest.json',
            'dist/desktop/rpc-methods.json',
            'desktop/native/CMakeLists.txt', 'desktop/native/Main.qml',
            'plugin.json', 'LICENSE', 'NOTICE', 'SOURCE.md', 'third_party/decky-api/LICENSE'}
forbidden = {'.git', '.github', 'node_modules', '__pycache__', 'tests', 'artifacts',
             'build', 'bin', '.venv', 'venv', '.local-validation'}
with zipfile.ZipFile(archive) as payload:
    assert payload.testzip() is None, 'Corrupt ZIP member'
    names = payload.namelist()
    # 2.1.0's in-app updater rejects larger archives (companion_updates.MAX_DOWNLOAD).
    assert len(names) <= 1024 and archive.stat().st_size <= 32 * 1024 * 1024, 'Updater archive limits'
    assert sum(member.file_size for member in payload.infolist()) <= 128 * 1024 * 1024, 'Updater expansion limit'
    assert len(names) == len(set(names)), 'Duplicate members'
    assert {'LegionGo2Companion/' + name for name in required} <= set(names), 'Missing payload'
    # package.mjs output is reproducible: sorted names, one timestamp, fixed modes.
    assert names == sorted(names), 'Unsorted members'
    assert len({member.date_time for member in payload.infolist()}) == 1, 'Mixed timestamps'
    files = 0
    for member in payload.infolist():
        path = PurePosixPath(member.filename)
        assert member.external_attr >> 16 == (0o40755 if member.is_dir() else 0o100644), f'Mode: {path}'
        assert path.parts[0] == 'LegionGo2Companion' and '..' not in path.parts and '\\' not in member.filename
        assert not forbidden.intersection(path.parts), f'Private path: {path}'
        assert not stat.S_ISLNK(member.external_attr >> 16), f'Symlink: {path}'
        assert not path.name.startswith('.env') and path.suffix.lower() not in {'.pyc', '.pyo', '.pem', '.key', '.log'}
        assert not path.name.endswith(('_live.py', 'settings.json', 'settings.json.bak', 'settings.json.last-good',
                                       'session.json')) and '.corrupt-' not in path.name
        if member.is_dir():
            continue
        relative = path.relative_to('LegionGo2Companion')
        if relative.parts[:2] == ('third_party', 'decky-api'):
            source = root / 'node_modules/@decky/api' / Path(*relative.parts[2:])
        else:
            source = root / Path(*relative.parts)
        assert source.is_file() and payload.read(member) == source.read_bytes(), f'Stale/unexpected file: {path}'
        files += 1
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
checksum = archive.with_name(archive.name + '.sha256')
assert checksum.read_text(encoding='utf-8') == f'{digest}  {archive.name}\n', 'Stale/missing checksum file'
print(json.dumps({'archive': str(archive), 'manifest_version': manifest['version'],
                  'files': files, 'sha256': digest,
                  'status': 'PASS'}, indent=2))

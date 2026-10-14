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
archive = Path(sys.argv[1]) if len(sys.argv) > 1 else root / f"LegionGo2Companion-{manifest['version']}.zip"
required = {'main.py', 'bounded_http.py', 'desktop_bridge.py', 'desktop_install.py', 'desktop/launch.py',
            'controller_maintenance.py', 'controller_firmware_usb.py',
            'controller_firmware_usb_linux.py',
            'controller_firmware_install.py', 'controller_firmware_ota.py',
            'controller_firmware_input.py',
            'diagnostics.py', 'diagnostics_reader.py', 'controller_firmware.py', 'controller_firmware_catalog.py', 'controller_firmware_protocol.py',
            'controller_firmware_transfer.py', 'controller_firmware_journal.py',
            'controller_firmware_worker.py', 'controller_firmware_process.py',
            'controller_firmware_stage.py', 'controller_firmware_sleep.py', 'src/firmware.tsx', 'docs/CONTROLLER_FIRMWARE.md',
            'dist/index.js', 'dist/desktop/legiongo2companion', 'dist/desktop/native-manifest.json',
            'dist/desktop/rpc-methods.json', 'dist/desktop/licenses/LGPL-3.0-only.txt',
            'desktop/native/CMakeLists.txt', 'desktop/native/Main.qml',
            'plugin.json', 'LICENSE', 'NOTICE', 'SOURCE.md'}
forbidden = {'.git', '.github', 'node_modules', '__pycache__', 'tests', 'artifacts',
             'build', 'bin', '.venv', 'venv', '.local-validation'}
with zipfile.ZipFile(archive) as payload:
    assert payload.testzip() is None, 'Corrupt ZIP member'
    names = payload.namelist()
    assert len(names) <= 1024 and archive.stat().st_size <= 48 * 1024 * 1024, 'Updater archive limits'
    assert sum(member.file_size for member in payload.infolist()) <= 128 * 1024 * 1024, 'Updater expansion limit'
    assert len(names) == len(set(names)), 'Duplicate members'
    assert {'LegionGo2Companion/' + name for name in required} <= set(names), 'Missing payload'
    files = 0
    for member in payload.infolist():
        path = PurePosixPath(member.filename)
        assert path.parts[0] == 'LegionGo2Companion' and '..' not in path.parts and '\\' not in member.filename
        assert not forbidden.intersection(path.parts), f'Private path: {path}'
        assert not stat.S_ISLNK(member.external_attr >> 16), f'Symlink: {path}'
        assert not path.name.startswith('.env') and path.suffix.lower() not in {'.pyc', '.pyo', '.pem', '.key', '.log'}
        assert not path.name.endswith(('_live.py', 'settings.json', 'settings.json.bak', 'session.json', 'open.html'))
        if member.is_dir():
            continue
        relative = path.relative_to('LegionGo2Companion')
        if relative.parts[0] == 'THIRD_PARTY_SOURCES':
            source = root / 'node_modules/@decky/api' / Path(*relative.parts[2:])
        elif relative.parts[0] == 'THIRD_PARTY_LICENSES':
            source = root / 'node_modules/@decky/api/LICENSE'
        else:
            source = root / Path(*relative.parts)
        assert source.is_file() and payload.read(member) == source.read_bytes(), f'Stale/unexpected file: {path}'
        files += 1
print(json.dumps({'archive': str(archive), 'manifest_version': manifest['version'],
                  'files': files, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                  'status': 'PASS'}, indent=2))

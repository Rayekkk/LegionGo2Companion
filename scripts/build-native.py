#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Build the native UI in an explicit out-of-tree directory. No device access.

--bundle publishes only the executable and its hash manifest (normally into
dist/desktop). Qt itself is not shipped: the app links the system Qt 6.9+.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--qt', type=Path, required=True)
parser.add_argument('--build', type=Path, required=True)
parser.add_argument('--test', action='store_true')
parser.add_argument('--toolchain', type=Path)
parser.add_argument('--host-qt', type=Path)
parser.add_argument('--bundle', type=Path, help='Publish the Linux executable and manifest here.')
args = parser.parse_args()
environment = {key.upper(): value for key, value in os.environ.items()}
if os.name == 'nt' and not args.toolchain:
    vswhere = Path(os.environ['ProgramFiles(x86)']) / 'Microsoft Visual Studio/Installer/vswhere.exe'
    installation = subprocess.check_output([str(vswhere), '-latest', '-products', '*', '-requires',
        'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath'], text=True).strip()
    setup = Path(installation) / 'VC/Auxiliary/Build/vcvars64.bat'
    if not setup.is_file() or any(c in str(setup) for c in '%\r\n'):
        raise RuntimeError('A supported MSVC build environment is required.')
    with tempfile.TemporaryDirectory(prefix='companion-msvc-') as temporary:
        script = Path(temporary) / 'environment.cmd'
        script.write_text(f'@call "{setup}" >nul\n@set\n', encoding='utf-8')
        # Compiler environment is captured in memory; never log values/tokens.
        output = subprocess.check_output(['cmd.exe', '/d', '/c', str(script)], text=True)
        for line in output.splitlines():
            key, separator, value = line.partition('=')
            if separator and key and not key.startswith('='):
                environment[key.upper()] = value
environment['PATH'] = str(args.qt.resolve() / 'bin') + os.pathsep + environment.get('PATH', '')
environment['QT_QUICK_BACKEND'] = 'software'  # Local tests are not SteamOS GPU validation.
command = ['cmake', '-S', str(root / 'desktop/native'), '-B', str(args.build.resolve()),
           '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_PREFIX_PATH=' + str(args.qt.resolve()),
           '-DBUILD_TESTING=' + ('ON' if args.test else 'OFF')]
if os.name == 'nt' and not args.toolchain:
    command.append('-DCMAKE_CXX_COMPILER=' + shutil.which('cl.exe', path=environment['PATH']))
if args.toolchain:
    command.append('-DCMAKE_TOOLCHAIN_FILE=' + str(args.toolchain.resolve()))
if args.host_qt:
    command.append('-DQT_HOST_PATH=' + str(args.host_qt.resolve()))
for operation in (command, ['cmake', '--build', str(args.build.resolve()), '--parallel', '4']):
    subprocess.run(operation, env=environment, check=True)
if args.test:
    subprocess.run(['ctest', '--test-dir', str(args.build.resolve()), '--output-on-failure'], env=environment, check=True)


def dynamic_tags(data):
    if data[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', data, 18)[0] != 62:
        raise ValueError('Expected a little-endian x86-64 ELF executable.')
    start = struct.unpack_from('<Q', data, 32)[0]
    size, count = struct.unpack_from('<HH', data, 54)
    for index in range(count):
        kind, _flags, offset, _virtual, _physical, length = struct.unpack_from('<IIQQQQ', data, start + index * size)
        if kind == 2:  # PT_DYNAMIC
            return {struct.unpack_from('<q', data, position)[0] for position in range(offset, offset + length, 16)}
    return set()


if args.bundle:
    executable = args.build.resolve() / 'legiongo2companion'
    data = executable.read_bytes()
    # A build-tree search path (e.g. /tmp/...) would let another local user plant Qt libraries.
    if dynamic_tags(data) & {15, 29}:  # DT_RPATH, DT_RUNPATH
        raise ValueError('The executable embeds a library search path; it must use the system Qt.')
    args.bundle.mkdir(parents=True, exist_ok=True)
    target = args.bundle / 'legiongo2companion'
    target.write_bytes(data)
    target.chmod(0o755)
    manifest = {'schema': 2, 'platform': 'linux-x86_64', 'executable': 'legiongo2companion',
                'files': {'legiongo2companion': {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}}}
    (args.bundle / 'native-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(f'Bundled {target} ({len(data)} bytes); it links the system Qt at runtime.')

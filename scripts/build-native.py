#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Build the native UI in an explicit out-of-tree directory. No device access."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--qt', type=Path, required=True)
parser.add_argument('--build', type=Path, required=True)
parser.add_argument('--test', action='store_true')
parser.add_argument('--toolchain', type=Path)
parser.add_argument('--host-qt', type=Path)
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

#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# LegionGo2Companion managed Desktop launcher v1
"""Executes the native Qt app as the desktop user. No browser or root helper."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

# The app links the distribution's Qt; it was built against this minimum.
QT_MINIMUM = (6, 9)


def private_json(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or
                (hasattr(os, 'getuid') and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
            raise ValueError('Invalid Desktop file permissions.')
        with os.fdopen(fd, 'rb', closefd=False) as source:
            data = json.load(source)
    finally:
        os.close(fd)
    if not isinstance(data, dict):
        raise ValueError('Invalid Desktop configuration.')
    return data


def native_command(directory):
    config = private_json(directory / 'runtime.json')
    bundle = Path(config.get('bundle', ''))
    if not bundle.is_absolute() or not re.fullmatch(r'[a-f0-9]{64}', config.get('executable_sha256', '')):
        raise ValueError('Invalid native application location.')
    executable = bundle / 'legiongo2companion'
    if executable.is_symlink() or not executable.is_file():
        raise ValueError('Native application is missing.')
    if hashlib.sha256(executable.read_bytes()).hexdigest() != config['executable_sha256']:
        raise ValueError('Native application changed. Restart Companion to refresh its registration.')
    return [str(executable), '--backend-session', str(directory / 'session.json')]


def qt_problem(version):
    try:
        found = tuple(int(part) for part in version.split('.')[:2])
    except ValueError:
        found = ()
    if len(found) != 2 or found[0] != QT_MINIMUM[0] or found < QT_MINIMUM:
        return f'Qt {version} is installed.'
    return None


def system_qt_problem():
    """Why the system Qt cannot run the app, or None. Uses the loader's own search."""
    try:
        core = ctypes.CDLL('libQt6Core.so.6')
    except OSError:
        return 'Qt 6 is not installed.'
    core.qVersion.restype = ctypes.c_char_p
    return qt_problem(core.qVersion().decode('ascii', 'replace'))


def show_error(message):
    dialog = shutil.which('kdialog')
    if dialog:
        subprocess.run([dialog, '--error', message], check=False, timeout=15)
    else:
        print(message, file=sys.stderr)
    return 1


def main():
    directory = Path(__file__).parent
    try:
        problem = system_qt_problem()
        if problem:
            return show_error(f'{problem} The Legion Go Companion Desktop app needs the system Qt '
                              f'{QT_MINIMUM[0]}.{QT_MINIMUM[1]} or a newer Qt {QT_MINIMUM[0]} release '
                              '(included in SteamOS 3.8 and later). The Decky panel is not affected.')
        # The Desktop Mode autostart entry starts the app in the tray.
        command = native_command(directory) + (['--tray'] if sys.argv[1:] == ['--tray'] else [])
        # The native app displays its offline state when Decky is unavailable.
        # Only a private file path, never a capability, appears in arguments.
        os.execv(command[0], command)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return show_error('Start Decky and Legion Go Companion, then reopen this app.\n' + str(exc))


if __name__ == '__main__':
    raise SystemExit(main())

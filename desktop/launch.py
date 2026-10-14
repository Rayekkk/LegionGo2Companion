#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# LegionGo2Companion managed Desktop launcher v1
"""Executes the native Qt app as the desktop user. No browser or root helper."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys


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


def session_url(path):
    data = private_json(path)
    origin, token = data.get('origin', ''), data.get('token', '')
    if (not isinstance(origin, str) or not isinstance(token, str) or
            not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', origin) or
            not 1 <= int(origin.rsplit(':', 1)[1]) <= 65535 or
            not re.fullmatch(r'[A-Za-z0-9_-]{43}', token)):
        raise ValueError('Invalid Desktop session.')
    return origin, token


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
    environment = os.environ.copy()
    # Unprivileged UI only; system graphics drivers and platform selection
    # remain the desktop session's responsibility.
    environment['LD_LIBRARY_PATH'] = str(bundle / 'lib')
    environment['QT_PLUGIN_PATH'] = str(bundle / 'plugins')
    environment['QML_IMPORT_PATH'] = str(bundle / 'qml')
    environment.pop('QML2_IMPORT_PATH', None)
    return [str(executable), '--backend-session', str(directory / 'session.json')], environment


def main():
    directory = Path(__file__).parent
    try:
        command, environment = native_command(directory)
        # The native app displays its offline state when Decky is unavailable.
        # Only a private file path, never a capability, appears in arguments.
        os.execve(command[0], command, environment)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        message = 'Start Decky and Legion Go Companion, then reopen this app.\n' + str(exc)
        dialog = shutil.which('kdialog')
        if dialog:
            subprocess.run([dialog, '--error', message], check=False, timeout=15)
        else:
            print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

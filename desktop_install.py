# SPDX-License-Identifier: BSD-3-Clause
"""Register one user app at backend startup, without system packages/services.

Root never follows links in user-controlled parents. Pinned directory handles,
exclusive temporary files and rename-at keep publication inside those handles.
Uninstall only removes our named, marked artifacts; user settings are untouched.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

import dk_fs
from safe_settings import open_directory

MARKER = 'LegionGo2Companion managed Desktop'
APP_ID = 'legiongo2companion'


class DesktopInstall:
    def __init__(self, source, home, uid, gid):
        if not os.path.isabs(home) or any(ord(c) < 32 for c in home):
            raise ValueError('Invalid Desktop home directory.')
        self.source, self.home = Path(source), home
        self.uid, self.gid = uid, gid

    @classmethod
    def for_decky(cls, source, decky):
        if sys.platform != 'linux':
            return None
        import pwd
        name = getattr(decky, 'DECKY_USER', None) or os.environ.get('DECKY_USER')
        if not name:
            return None
        account = pwd.getpwnam(name)
        if account.pw_uid <= 0 or not os.path.isabs(account.pw_dir):
            raise ValueError('Desktop app requires a non-root Decky user.')
        return cls(source, account.pw_dir, account.pw_uid, account.pw_gid)

    def _directory(self, relative, create=True):
        def safe(fd):
            info = os.fstat(fd)
            if info.st_uid not in (0, self.uid) or info.st_mode & 0o022:
                raise ValueError('Unsafe Desktop installation directory.')
        made = (lambda part: (0o700 if part == APP_ID else 0o755, self.uid, self.gid)) if create else None
        current = open_directory(Path(self.home).parts[1:] + tuple(relative.split('/')), create=made, check=safe)
        try:
            if relative.endswith(APP_ID):
                os.fchmod(current, 0o700)
            return current
        except BaseException:
            os.close(current)
            raise

    @staticmethod
    def _ours(directory, filename):
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('Unexpected Desktop artifact.')
            return MARKER.encode() in os.read(fd, 4096)
        finally:
            os.close(fd)

    def _write(self, relative, filename, content, mode=0o600):
        directory = self._directory(relative)
        try:
            # The executable cannot carry the marker; its fixed name in our 0700 directory is the claim.
            if filename != APP_ID and self._ours(directory, filename) is False:
                raise ValueError('An unrecognized file occupies the Desktop app path.')
            dk_fs.write_atomic(filename, content, mode, folder_fd=directory, owner=(self.uid, self.gid),
                               strict_sync=True)
        finally:
            os.close(directory)

    def _published(self, relative, filename, expected):
        directory = self._directory(relative)
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return False
        finally:
            os.close(directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid or stat.S_IMODE(info.st_mode) != 0o700:
                return False
            with os.fdopen(fd, 'rb', closefd=False) as current:
                return hashlib.sha256(current.read()).hexdigest() == expected
        finally:
            os.close(fd)

    def install(self, origin, token):
        bundle = self.source.parent / 'dist' / 'desktop'
        manifest = json.loads((bundle / 'native-manifest.json').read_text(encoding='utf-8'))
        if manifest.get('platform') != 'linux-x86_64' or manifest.get('executable') != 'legiongo2companion':
            raise ValueError('Unsupported native Desktop payload.')
        executable = bundle / 'legiongo2companion'
        expected = manifest['files']['legiongo2companion']['sha256']
        fd = os.open(executable, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, self.uid) or info.st_mode & 0o022:
                raise ValueError('Unsafe native Desktop executable.')
            with os.fdopen(fd, 'rb', closefd=False) as source:
                data = source.read()
            if data[:6] != b'\x7fELF\x02\x01' or hashlib.sha256(data).hexdigest() != expected:
                raise ValueError('Native Desktop payload checksum mismatch.')
        finally:
            os.close(fd)
        private = '.local/share/' + APP_ID
        # The app runs a private copy: Decky's in-place ZIP extraction cannot rewrite a running
        # executable (ETXTBSY). Replacing the copy unlinks a running one, which then restarts.
        # An unchanged build keeps its inode, so a plain backend restart leaves the app running.
        if not self._published(private, APP_ID, expected):
            self._write(private, APP_ID, data, 0o700)
        self._write(private, 'runtime.json', json.dumps({'managed_by': MARKER,
                    'bundle': str(Path(self.home) / private), 'plugin': str(self.source.parent.resolve()),
                    'executable_sha256': expected}).encode())
        self._write(private, 'launch.py', (self.source / 'launch.py').read_bytes())
        icon = b'<!-- ' + MARKER.encode() + b' -->\n' + (self.source / 'icon.svg').read_bytes()
        self._write(private, 'icon.svg', icon)
        launcher = str(Path(self.home) / private / 'launch.py')
        # Desktop Exec is not shell code, but has its own quoting and field codes.
        quoted = launcher.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
        entry = (f'# {MARKER} v1\n[Desktop Entry]\nType=Application\nName=Legion Go Companion\n'
                 f'Comment=Desktop controls for the running Decky Companion backend\n'
                 f'Exec=/usr/bin/python3 "{quoted}"\n'
                 f'Icon={self.home}/{private}/icon.svg\nTerminal=false\nCategories=Game;Settings;\n'
                 'StartupNotify=true\n').encode()
        self._write('.local/share/applications', APP_ID + '.desktop', entry)
        # Plasma (Desktop Mode) only: the app starts hidden with its tray icon. "Show tray icon" off skips it.
        autostart = (f'# {MARKER} v1\n[Desktop Entry]\nType=Application\nName=Legion Go Companion\n'
                     f'Exec=/usr/bin/python3 "{quoted}" --tray\n'
                     f'Icon={self.home}/{private}/icon.svg\nTerminal=false\nOnlyShowIn=KDE;\nNoDisplay=true\n').encode()
        self._write('.config/autostart', APP_ID + '.desktop', autostart)
        self._write(private, 'session.json', json.dumps({'managed_by': MARKER, 'origin': origin, 'token': token}).encode())

    def remove(self, uninstall=False):
        paths = [('.local/share/' + APP_ID, 'session.json')]
        if uninstall:
            paths += [('.local/share/' + APP_ID, name) for name in (APP_ID, 'launch.py', 'icon.svg', 'runtime.json')]
            paths += [('.local/share/applications', APP_ID + '.desktop'), ('.config/autostart', APP_ID + '.desktop')]
        for relative, filename in paths:
            try:
                directory = self._directory(relative, create=False)
            except FileNotFoundError:
                continue
            try:
                ours = self._ours(directory, filename)
                if ours or (ours is False and filename == APP_ID):
                    os.unlink(filename, dir_fd=directory)
            finally:
                os.close(directory)
        if uninstall:
            try:
                parent = self._directory('.local/share', create=False)
            except FileNotFoundError:
                return
            try:
                os.rmdir(APP_ID, dir_fd=parent)
            except OSError:
                pass  # Missing, or still holds files that are not ours.
            finally:
                os.close(parent)

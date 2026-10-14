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
import secrets
import stat
import sys

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
        current = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = Path(self.home).parts[1:] + tuple(relative.split('/'))
            for part in parts:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o700 if part == APP_ID else 0o755, dir_fd=current)
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                    os.fchown(child, self.uid, self.gid)
                info = os.fstat(child)
                if info.st_uid not in (0, self.uid) or info.st_mode & 0o022:
                    os.close(child)
                    raise ValueError('Unsafe Desktop installation directory.')
                os.close(current)
                current = child
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

    def _write(self, relative, filename, content):
        directory = self._directory(relative)
        temp = '.companion-' + secrets.token_hex(12)
        try:
            if self._ours(directory, filename) is False:
                raise ValueError('An unrecognized file occupies the Desktop app path.')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb', closefd=False) as target:
                    target.write(content)
                    target.flush()
                    os.fsync(fd)
                os.fchown(fd, self.uid, self.gid)
            finally:
                os.close(fd)
            os.replace(temp, filename, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temp, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

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
            # Windows-created ZIPs do not reliably retain execute permissions.
            os.fchmod(fd, 0o755)
        finally:
            os.close(fd)
        private = '.local/share/' + APP_ID
        self._write(private, 'runtime.json', json.dumps({'managed_by': MARKER,
                    'bundle': str(bundle.resolve()), 'executable_sha256': expected}).encode())
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
        self._write(private, 'session.json', json.dumps({'managed_by': MARKER, 'origin': origin, 'token': token}).encode())

    def remove(self, uninstall=False):
        paths = [('.local/share/' + APP_ID, name) for name in ('session.json', 'open.html')]
        if uninstall:
            paths += [('.local/share/' + APP_ID, name) for name in ('launch.py', 'icon.svg', 'runtime.json')]
            paths += [('.local/share/applications', APP_ID + '.desktop')]
        for relative, filename in paths:
            try:
                directory = self._directory(relative, create=False)
            except FileNotFoundError:
                continue
            try:
                if self._ours(directory, filename):
                    os.unlink(filename, dir_fd=directory)
            finally:
                os.close(directory)

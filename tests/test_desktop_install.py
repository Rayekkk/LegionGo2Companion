# SPDX-License-Identifier: BSD-3-Clause
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import stat
import shutil
import tempfile
import unittest
from unittest.mock import patch

from desktop_install import DesktopInstall, APP_ID

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('companion_desktop_launcher', ROOT / 'desktop/launch.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'session.json'

    def session(self, data):
        self.path.write_text(json.dumps(data), encoding='utf-8')
        self.path.chmod(0o600)

    def test_regular_private_loopback_capability(self):
        self.session({'origin': 'http://127.0.0.1:40000', 'token': 'a' * 43})
        self.assertEqual(launcher.session_url(self.path), ('http://127.0.0.1:40000', 'a' * 43))

    def test_remote_hosts_invalid_shapes_and_ports_rejected(self):
        for data in ([], {'origin': 1}, {'origin': 'http://evil.invalid:80', 'token': 'a' * 43},
                     {'origin': 'http://127.0.0.1:0', 'token': 'a' * 43},
                     {'origin': 'http://127.0.0.1:65536', 'token': 'a' * 43},
                     {'origin': 'http://127.0.0.1:40000', 'token': 1}):
            self.session(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                launcher.session_url(self.path)

    def test_native_launcher_uses_private_path_and_matching_executable(self):
        executable = self.path.parent / 'legiongo2companion'
        executable.write_bytes(b'test executable, never executed')
        config = self.path.parent / 'runtime.json'
        config.write_text(json.dumps({'bundle': str(self.path.parent),
                         'executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest()}))
        config.chmod(0o600)
        command, environment = launcher.native_command(self.path.parent)
        self.assertEqual(command, [str(executable), '--backend-session', str(self.path)])
        self.assertNotIn('a' * 43, ' '.join(command))
        self.assertEqual(environment['QT_PLUGIN_PATH'], str(self.path.parent / 'plugins'))
        executable.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            launcher.native_command(self.path.parent)

    @unittest.skipUnless(hasattr(os, 'getuid'), 'Unix ownership validation')
    def test_public_session_permissions_rejected(self):
        self.session({'origin': 'http://127.0.0.1:40000', 'token': 'a' * 43})
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            launcher.session_url(self.path)


@unittest.skipUnless(hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'fchown'), 'POSIX dirfd installer: Linux CI/device pending')
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir(mode=0o700)
        source = Path(self.temp.name) / 'repo/desktop'; source.mkdir(parents=True)
        for name in ('launch.py', 'icon.svg'):
            shutil.copyfile(ROOT / 'desktop' / name, source / name)
        bundle = source.parent / 'dist/desktop'; bundle.mkdir(parents=True)
        executable = bundle / 'legiongo2companion'
        executable.write_bytes(b'\x7fELF\x02\x01' + b'isolated installer fixture, never executed')
        executable.chmod(0o644)
        (bundle / 'native-manifest.json').write_text(json.dumps({'platform': 'linux-x86_64',
            'executable': 'legiongo2companion', 'files': {'legiongo2companion':
            {'sha256': hashlib.sha256(executable.read_bytes()).hexdigest()}}}))
        self.executable = executable
        self.installer = DesktopInstall(source, str(self.home), os.getuid(), os.getgid())
        # Test setup is under /tmp, whose sticky public parent is legitimate for
        # isolated tests but intentionally rejected by production publication.
        self.real_fstat = os.fstat
        def safe_parent(fd):
            info = self.real_fstat(fd)
            if stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX:
                values = list(info); values[0] &= ~0o022
                return os.stat_result(values)
            return info
        self.parent = patch('desktop_install.os.fstat', side_effect=safe_parent)
        self.parent.start(); self.addCleanup(self.parent.stop)

    def test_install_update_unload_uninstall_and_user_files_preserved(self):
        self.installer.install('http://127.0.0.1:40000', 'a' * 43)
        private = self.home / '.local/share' / APP_ID
        entry = self.home / '.local/share/applications' / (APP_ID + '.desktop')
        self.assertIn('Name=Legion Go Companion', entry.read_text())
        self.assertNotIn('a' * 43, entry.read_text())
        self.assertEqual((private / 'session.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.executable.stat().st_mode & 0o777, 0o755)
        command, unused_environment = launcher.native_command(private)
        self.assertEqual(command[0], str(self.executable))
        unrelated = private / 'user-notes.txt'; unrelated.write_text('keep')
        self.installer.install('http://127.0.0.1:40001', 'b' * 43)
        self.assertEqual(json.loads((private / 'session.json').read_text())['token'], 'b' * 43)
        self.installer.remove()
        self.assertFalse((private / 'session.json').exists())
        self.assertTrue(entry.exists())
        self.installer.remove(True)
        self.assertFalse(entry.exists())
        self.assertEqual(unrelated.read_text(), 'keep')
        self.assertFalse((private / 'runtime.json').exists())

    def test_modified_native_payload_rejected_before_publication(self):
        self.executable.write_bytes(b'not the approved executable')
        with self.assertRaises(ValueError):
            self.installer.install('http://127.0.0.1:40000', 'a' * 43)
        self.assertFalse((self.home / '.local').exists())

    def test_symlink_parent_rejected_without_touching_target(self):
        elsewhere = self.home / 'elsewhere'; elsewhere.mkdir()
        (self.home / '.local').symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(OSError):
            self.installer.install('http://127.0.0.1:40000', 'a' * 43)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_unmarked_file_not_overwritten(self):
        self.installer.install('http://127.0.0.1:40000', 'a' * 43)
        path = self.home / '.local/share' / APP_ID / 'launch.py'
        path.write_text('user script')
        with self.assertRaises(ValueError):
            self.installer.install('http://127.0.0.1:40001', 'b' * 43)
        self.assertEqual(path.read_text(), 'user script')

    def test_remove_does_not_create_directories(self):
        self.installer.remove(True)
        self.assertFalse((self.home / '.local').exists())

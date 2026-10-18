# SPDX-License-Identifier: BSD-3-Clause
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import stat
import shutil
import subprocess
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

    def test_native_launcher_uses_private_path_and_matching_executable(self):
        executable = self.path.parent / 'legiongo2companion'
        executable.write_bytes(b'test executable, never executed')
        config = self.path.parent / 'runtime.json'
        config.write_text(json.dumps({'bundle': str(self.path.parent),
                         'executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest()}))
        config.chmod(0o600)
        command = launcher.native_command(self.path.parent)
        self.assertEqual(command, [str(executable), '--backend-session', str(self.path)])
        self.assertNotIn('a' * 43, ' '.join(command))
        executable.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            launcher.native_command(self.path.parent)

    def test_system_qt_must_be_qt6_at_least_the_build_minimum(self):
        for version in ('6.9.0', '6.9.1', '6.11.2', '6.20'):
            self.assertIsNone(launcher.qt_problem(version))
        for version in ('6.8.3', '5.15.2', '7.0.0', '', 'unknown'):
            self.assertIn('is installed', launcher.qt_problem(version))
        with patch.object(launcher.ctypes, 'CDLL', side_effect=OSError):
            self.assertEqual(launcher.system_qt_problem(), 'Qt 6 is not installed.')

    def test_missing_system_qt_reports_instead_of_starting_the_app(self):
        with patch.object(launcher, 'system_qt_problem', return_value='Qt 6 is not installed.'),                 patch.object(launcher, 'native_command') as command,                 patch.object(launcher.shutil, 'which', return_value=None),                 patch('sys.stderr') as stderr:
            self.assertEqual(launcher.main(), 1)
        command.assert_not_called()
        self.assertIn('Decky panel is not affected', ''.join(call.args[0] for call in stderr.write.call_args_list))

    @unittest.skipUnless(hasattr(os, 'getuid'), 'Unix ownership validation')
    def test_public_session_permissions_rejected(self):
        self.session({'origin': 'http://127.0.0.1:40000', 'token': 'a' * 43})
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            launcher.private_json(self.path)


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
        self.executable, self.manifest = executable, bundle / 'native-manifest.json'
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
        autostart = self.home / '.config/autostart' / (APP_ID + '.desktop')
        self.assertIn(f'Exec=/usr/bin/python3 "{private / "launch.py"}" --tray\n', autostart.read_text())
        self.assertIn('OnlyShowIn=KDE;', autostart.read_text())
        self.assertEqual((private / 'session.json').stat().st_mode & 0o777, 0o600)
        command = launcher.native_command(private)
        self.assertEqual(command[0], str(private / APP_ID))
        self.assertEqual((private / APP_ID).stat().st_mode & 0o777, 0o700)
        unrelated = private / 'user-notes.txt'; unrelated.write_text('keep')
        self.installer.install('http://127.0.0.1:40001', 'b' * 43)
        self.assertEqual(json.loads((private / 'session.json').read_text())['token'], 'b' * 43)
        self.installer.remove()
        self.assertFalse((private / 'session.json').exists())
        self.assertTrue(entry.exists())
        self.assertTrue(autostart.exists())
        self.installer.remove(True)
        self.assertFalse(entry.exists())
        self.assertFalse(autostart.exists())
        self.assertEqual(unrelated.read_text(), 'keep')
        self.assertFalse((private / 'runtime.json').exists())
        self.assertFalse((private / APP_ID).exists())

    def ship(self, program):
        shutil.copyfile(program, self.executable)
        self.manifest.write_text(json.dumps({'platform': 'linux-x86_64', 'executable': 'legiongo2companion',
            'files': {'legiongo2companion': {'sha256': hashlib.sha256(self.executable.read_bytes()).hexdigest()}}}))

    @unittest.skipUnless(Path('/proc/self/exe').exists() and shutil.which('sleep') and shutil.which('true'),
                         'Linux executables')
    def test_running_app_never_blocks_an_in_place_plugin_update(self):
        self.ship(shutil.which('sleep'))
        self.installer.install('http://127.0.0.1:40000', 'a' * 43)
        private = self.home / '.local/share' / APP_ID
        running = subprocess.Popen([launcher.native_command(private)[0], '30'])
        self.addCleanup(running.wait); self.addCleanup(running.kill)
        exe = Path(f'/proc/{running.pid}/exe')
        self.installer.install('http://127.0.0.1:40001', 'b' * 43)  # same build, e.g. a Decky restart
        self.assertFalse(os.readlink(exe).endswith(' (deleted)'))
        # Decky's "Install Plugin from ZIP" rewrites plugin files in place (ZipFile.extractall).
        with open(self.executable, 'wb') as target:
            target.write(Path(shutil.which('true')).read_bytes())
        self.ship(shutil.which('true'))
        self.installer.install('http://127.0.0.1:40002', 'c' * 43)
        self.assertTrue(os.readlink(exe).endswith(' (deleted)'))  # the app notices and restarts
        self.assertEqual(launcher.native_command(private)[0], str(private / APP_ID))
        self.assertEqual(subprocess.run([str(private / APP_ID)]).returncode, 0)
        self.installer.remove(True)
        self.assertFalse((self.home / '.local/share' / APP_ID).exists())

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

    def test_uninstall_removes_the_emptied_private_directory(self):
        self.installer.install('http://127.0.0.1:40000', 'a' * 43)
        self.installer.remove(True)
        self.assertFalse((self.home / '.local/share' / APP_ID).exists())

    def test_remove_does_not_create_directories(self):
        self.installer.remove(True)
        self.assertFalse((self.home / '.local').exists())

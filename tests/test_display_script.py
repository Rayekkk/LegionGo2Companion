# SPDX-License-Identifier: BSD-3-Clause
"""The Lua display script: where it goes and who writes it."""
import asyncio
from contextlib import ExitStack
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import support  # Decky/Linux doubles before importing the backend.
import display_backend as display
import display_script_io
from safe_settings import AtomicSettingsManager
from system_process import PYTHON


POSIX = unittest.skipUnless(os.name == 'posix', 'POSIX dir_fd and symlink semantics')


class ScriptTargetTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        for name in ('SCRIPT_DIR', 'INSTALLED_SCRIPT', 'SCRIPT_USER', 'LEGACY_SCRIPT_DIR', '_script_binding'):
            self.stack.enter_context(patch.object(display, name, getattr(display, name)))
        self.stack.enter_context(patch.object(display, 'pwd', types.SimpleNamespace(
            getpwuid=lambda uid: types.SimpleNamespace(pw_dir='/home/deck', pw_gid=1001, pw_uid=uid))))

    def config_dir(self, environ: bytes):
        real_stat, real_open = os.stat, open
        def fake_stat(path, *args, **kwargs):
            return types.SimpleNamespace(st_uid=1000) if path == '/proc/77' else real_stat(path, *args, **kwargs)
        def fake_open(path, *args, **kwargs):
            return io.BytesIO(environ) if path == '/proc/77/environ' else real_open(path, *args, **kwargs)
        with patch.object(display.os, 'stat', side_effect=fake_stat), \
             patch('builtins.open', side_effect=fake_open):
            return display._gamescope_config_dir(77)

    def test_config_dir_follows_gamescope_environment_then_account_home(self):
        self.assertEqual(self.config_dir(b'HOME=/root\0XDG_CONFIG_HOME=/home/deck/xdg\0'),
                         ('/home/deck/xdg', 1000, 1001))
        for environ in (b'HOME=/root\0', b'XDG_CONFIG_HOME=relative/dir\0', b''):
            with self.subTest(environ=environ):
                self.assertEqual(self.config_dir(environ), ('/home/deck/.config', 1000, 1001))

    def bind(self, euid, pid=77, owner=None, target=('/home/deck/.config', 1000, 1001)):
        with patch.object(display.os, 'geteuid', create=True, return_value=euid), \
             patch.object(display, '_gamescope_start_time', return_value=1.0), \
             patch.object(display, '_gamescope_pid', pid), \
             patch.object(display, '_display_owner', return_value=owner), \
             patch.object(display, '_gamescope_config_dir', return_value=target):
            return display._bind_script_dir()

    def test_root_binds_to_the_gamescope_user_and_unprivileged_keeps_its_own(self):
        legacy = display.INSTALLED_SCRIPT
        self.assertTrue(self.bind(euid=1000))
        self.assertEqual(display.INSTALLED_SCRIPT, legacy)
        self.assertIsNone(display.SCRIPT_USER)
        self.assertTrue(self.bind(euid=0))
        expected = os.path.join('/home/deck/.config', 'gamescope', 'scripts')
        self.assertEqual(display.SCRIPT_DIR, expected)
        self.assertEqual(display.INSTALLED_SCRIPT, os.path.join(expected, display.SCRIPT_NAME))
        self.assertEqual(display.SCRIPT_USER, ('/home/deck/.config', 1000, 1001))

    def test_desktop_mode_binds_to_the_x_display_owner(self):
        self.assertTrue(self.bind(euid=0, pid=None, owner=1000))
        self.assertEqual(display.SCRIPT_DIR, os.path.join('/home/deck', '.config', 'gamescope', 'scripts'))
        self.assertEqual(display.SCRIPT_USER, (os.path.join('/home/deck', '.config'), 1000, 1001))

    def test_root_without_a_known_user_never_installs_into_its_own_home(self):
        legacy = self.root / 'legacy'
        display.SCRIPT_DIR = display.LEGACY_SCRIPT_DIR = str(legacy)
        display.INSTALLED_SCRIPT = str(legacy / display.SCRIPT_NAME)
        display._script_binding = None
        with patch.object(display.os, 'geteuid', create=True, return_value=0), \
             patch.object(display, '_gamescope_start_time', return_value=None), \
             patch.object(display, '_gamescope_pid', None), \
             patch.object(display, '_display_owner', return_value=None):
            ok, note = display._install_script()
            self.assertIn('could not', display._uninstall_script())
        self.assertFalse(ok)
        self.assertIn('Gaming Mode user', note)
        self.assertFalse(legacy.exists())

    def test_writes_run_as_the_user_with_no_supplementary_groups(self):
        base = str(self.root / 'config')
        display.SCRIPT_USER = (base, 1000, 1001)
        display.SCRIPT_DIR = os.path.join(base, 'gamescope', 'scripts')
        target = os.path.join(display.SCRIPT_DIR, display.SCRIPT_NAME)
        with patch.object(display.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(display._script_write(target, b'lua'))
            run.return_value = subprocess.CompletedProcess([], 2)
            with self.assertRaises(FileNotFoundError):
                display._script_unlink(target)
            self.assertEqual(display._run_script_io('write', '/etc/passwd', b'x'), 1)
        self.assertEqual(run.call_count, 2)
        argv, kwargs = run.call_args_list[0].args[0], run.call_args_list[0].kwargs
        self.assertEqual(argv, [PYTHON, '-I', display.SCRIPT_IO, 'write', base, display.SCRIPT_NAME])
        self.assertEqual((kwargs['user'], kwargs['group'], kwargs['extra_groups']), (1000, 1001, []))
        self.assertEqual(kwargs['input'], b'lua')
        self.assertNotIn('LD_LIBRARY_PATH', kwargs['env'])

    def test_user_dir_files_the_user_does_not_own_are_not_read(self):
        display.SCRIPT_DIR = str(self.root)
        script = self.root / display.SCRIPT_NAME
        script.write_bytes(b'lua')
        display.SCRIPT_USER = (str(self.root), script.stat().st_uid + 1, 0)
        self.assertIsNone(display._read_bytes(str(script)))
        display.SCRIPT_USER = (str(self.root), script.stat().st_uid, 0)
        self.assertEqual(display._read_bytes(str(script)), b'lua')


@POSIX
class UserScriptIoTests(unittest.TestCase):
    """The helper itself, and the backend driving it, as the current user."""
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.base = self.root / 'config'
        self.dir = self.base / 'gamescope' / 'scripts'
        self.script = self.dir / display.SCRIPT_NAME
        self.backup = Path(str(self.script) + '.backup')
        self.legacy = self.root / 'root-config' / 'gamescope' / 'scripts'
        self.ours = Path(display.BUNDLED_SCRIPT).read_bytes()
        for name, value in {'SCRIPT_DIR': str(self.dir), 'INSTALLED_SCRIPT': str(self.script),
                            'SCRIPT_USER': (str(self.base), os.getuid(), os.getgid()),
                            'LEGACY_SCRIPT_DIR': str(self.legacy), '_script_binding': ('gamescope', 77)}.items():
            self.stack.enter_context(patch.object(display, name, value))
        self.stack.enter_context(patch.object(display, '_bind_script_dir', return_value=True))
        real_run = subprocess.run
        self.calls = []
        def unprivileged(argv, **kwargs):
            # Only root may drop to another uid; run the real helper as ourselves.
            self.calls.append({key: kwargs.pop(key) for key in ('user', 'group', 'extra_groups')})
            return real_run([sys.executable] + argv[1:], **kwargs)
        self.stack.enter_context(patch.object(display.subprocess, 'run', side_effect=unprivileged))

    def helper(self, *argv, data=b''):
        with patch.object(sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO(data))):
            return display_script_io.main(['display_script_io.py', *argv])

    def test_helper_creates_the_tree_and_writes_atomically_readable(self):
        self.assertEqual(self.helper('write', str(self.base), display.SCRIPT_NAME, data=b'lua'), 0)
        self.assertEqual(self.script.read_bytes(), b'lua')
        self.assertEqual(self.script.stat().st_mode & 0o777, 0o644)
        self.assertEqual(sorted(os.listdir(self.dir)), [display.SCRIPT_NAME])
        self.assertEqual(self.helper('unlink', str(self.base), display.SCRIPT_NAME), 0)
        self.assertEqual(self.helper('unlink', str(self.base), display.SCRIPT_NAME), 2)
        for argv in (('write', 'relative', 'x'), ('write', str(self.base), '../x'), ('chmod', str(self.base), 'x')):
            self.assertEqual(self.helper(*argv), 1)

    def test_helper_refuses_symlinked_dirs_and_non_regular_targets(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        self.base.mkdir()
        (self.base / 'gamescope').symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(self.helper('write', str(self.base), display.SCRIPT_NAME, data=b'lua'), 1)
        self.assertEqual(list(elsewhere.iterdir()), [])
        (self.base / 'gamescope').unlink()
        self.dir.mkdir(parents=True)
        victim = self.root / 'victim'
        victim.write_bytes(b'keep')
        self.script.symlink_to(victim)
        self.assertEqual(self.helper('write', str(self.base), display.SCRIPT_NAME, data=b'lua'), 1)
        self.assertEqual(victim.read_bytes(), b'keep')

    def test_install_backs_up_a_foreign_script_and_uninstall_restores_it(self):
        self.dir.mkdir(parents=True)
        self.script.write_bytes(b'-- hand tuned')
        ok, _ = display._install_script()
        self.assertTrue(ok)
        self.assertEqual(self.script.read_bytes(), self.ours)
        self.assertEqual(self.backup.read_bytes(), b'-- hand tuned')
        self.assertEqual(display._uninstall_script(), 'restored the display script that was there before')
        self.assertEqual(self.script.read_bytes(), b'-- hand tuned')
        self.assertFalse(self.backup.exists())
        self.assertTrue(self.calls)
        self.assertTrue(all(call == {'user': os.getuid(), 'group': os.getgid(), 'extra_groups': []}
                            for call in self.calls))

    def test_only_our_copies_are_removed_from_the_old_root_location(self):
        self.legacy.mkdir(parents=True)
        (self.legacy / display.SCRIPT_NAME).write_bytes(self.ours)
        (self.legacy / (display.SCRIPT_NAME + '.backup')).write_bytes(b'-- foreign')
        (self.legacy / 'other.lua').write_bytes(self.ours)
        self.assertTrue(display._install_script()[0])
        self.assertEqual(self.script.read_bytes(), self.ours)
        self.assertEqual(sorted(os.listdir(self.legacy)), sorted(['other.lua', display.SCRIPT_NAME + '.backup']))
        (self.legacy / display.SCRIPT_NAME).write_bytes(self.ours)
        self.assertEqual(display._uninstall_script(), 'removed the display script')
        self.assertFalse(self.script.exists())
        self.assertFalse((self.legacy / display.SCRIPT_NAME).exists())


class DisplayWriterRecoveryTests(unittest.TestCase):
    def test_display_writer_ignores_preexisting_staging_link(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            victim = root / "unrelated-file"
            victim.write_bytes(b"original unrelated data")
            target = root / "edid-original.bin"
            staging = Path(str(target) + ".lego2brightnessfix.tmp")
            # Hard links work without Windows symlink privileges. This tests
            # following an existing inode, not Linux privilege escalation.
            os.link(victim, staging)
            self.assertTrue(display._write_file_atomically(str(target), b"EDID"))
            self.assertEqual(victim.read_bytes(), b"original unrelated data")

    def test_script_cache_invalidates_after_replace(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)/"display.lua"
            path.write_bytes(b"first")
            self.assertEqual(display._read_script_bytes(str(path)), b"first")
            self.assertEqual(display._read_script_bytes(str(path)), b"first")
            other = Path(raw)/"replacement"
            other.write_bytes(b"second")
            os.replace(other, path)
            self.assertEqual(display._read_script_bytes(str(path)), b"second")

    @unittest.skipUnless(os.name == "posix", "Linux symlink and mode semantics")
    def test_display_writer_refuses_target_symlink_and_preserves_reader_access(self):
        with tempfile.TemporaryDirectory() as raw:
            victim, target = Path(raw)/"victim", Path(raw)/"script.lua"
            victim.write_bytes(b"keep")
            target.symlink_to(victim)
            self.assertFalse(display._write_file_atomically(str(target), b"new"))
            self.assertEqual(victim.read_bytes(), b"keep")
            target.unlink()
            self.assertTrue(display._write_file_atomically(str(target), b"lua"))
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)


class SettingsFrom210Tests(unittest.TestCase):
    def test_2_1_0_settings_and_root_script_start_without_error_and_land_in_setup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy, new_dir = root / 'root-config', root / 'deck-config' / 'scripts'
            legacy.mkdir()
            (legacy / display.SCRIPT_NAME).write_bytes(Path(display.BUNDLED_SCRIPTS[display.MODE_PQ]).read_bytes())
            manager = AtomicSettingsManager('display_settings', raw)
            # What 2.1.0 wrote: no EDID recovery keys, epoch-time session stamps.
            manager.replace({key: value for key, value in display.DEFAULT_SETTINGS.items()
                             if not key.startswith('edid_') or key == 'edid_fix'}
                            | {'panel_mode': display.MODE_PQ, 'active_mode': display.MODE_PQ,
                               'brightness_baseline': 400.0, 'brightness_baseline_session': 1.7e9})
            with ExitStack() as stack:
                for name, value in {'settings': manager, '_pick_display': Mock(return_value=True),
                                    '_read_bytes': Mock(return_value=None),
                                    '_identify_panel': Mock(return_value=(False, 'test')),
                                    '_find_backlight': Mock(return_value=''),
                                    '_watch_properties': Mock(return_value=asyncio.sleep(0)),
                                    'LEGACY_SCRIPT_DIR': str(legacy), 'SCRIPT_DIR': str(new_dir),
                                    'INSTALLED_SCRIPT': str(new_dir / display.SCRIPT_NAME)}.items():
                    stack.enter_context(patch.object(display, name, value))
                for name, value in {'_state': dict(display.Plugin._state), '_task': None, '_prop_task': None,
                                    '_loop': AsyncMock(), '_session_props': Mock(), '_edid_pass': Mock(),
                                    '_sync_loaded_script': Mock(),
                                    'native_display_active': Mock(return_value=False)}.items():
                    stack.enter_context(patch.object(display.Plugin, name, value))
                plugin = display.Plugin()

                async def run():
                    await plugin._main()
                    await asyncio.gather(plugin._task, plugin._prop_task)
                asyncio.run(run())
                state = display.Plugin._state
                self.assertEqual(state['settings_error'], '')
                self.assertEqual(state['panel_mode'], display.MODE_PQ)
                self.assertFalse(state['setup_done'])
            self.assertTrue((legacy / display.SCRIPT_NAME).exists())
            self.assertFalse(new_dir.exists())


if __name__ == '__main__':
    unittest.main()

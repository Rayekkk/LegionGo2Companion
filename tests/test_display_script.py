# SPDX-License-Identifier: BSD-3-Clause
"""Display start-up: the old display script cleanup, the EDID backup move and retired settings."""
import asyncio
from contextlib import ExitStack
import hashlib
import io
import json
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
import display_native as native
import display_script_io
from safe_settings import AtomicSettingsManager, CorruptSettings
from system_process import PYTHON
from test_display_edid import edid_fixture


POSIX = unittest.skipUnless(os.name == 'posix', 'POSIX dir_fd, symlink and mode semantics')
PQ, GAMMA = b'-- pq script\nlocal x = 1\n', b'-- gamma 2.2 script\nlocal x = 2\n'
OURS = frozenset(hashlib.sha256(data).hexdigest() for data in (PQ, GAMMA))


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

    def test_root_without_a_known_user_waits_instead_of_touching_its_own_home(self):
        legacy = self.root / 'legacy'
        legacy.mkdir()
        (legacy / display.SCRIPT_NAME).write_bytes(PQ)
        display.SCRIPT_DIR = display.LEGACY_SCRIPT_DIR = str(legacy)
        display.INSTALLED_SCRIPT = str(legacy / display.SCRIPT_NAME)
        display._script_binding = None
        with patch.object(display.os, 'geteuid', create=True, return_value=0), \
             patch.object(display, 'OLD_SCRIPT_SHA256', OURS), \
             patch.object(display, '_gamescope_start_time', return_value=None), \
             patch.object(display, '_gamescope_pid', None), \
             patch.object(display, '_display_owner', return_value=None):
            self.assertIsNone(display._remove_old_scripts(restore_backup=True))
        self.assertEqual((legacy / display.SCRIPT_NAME).read_bytes(), PQ)

    def test_changes_run_as_the_user_with_no_supplementary_groups(self):
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

    def test_scripts_we_shipped_stay_ours_with_either_line_ending_but_edits_do_not(self):
        with patch.object(display, 'OLD_SCRIPT_SHA256', OURS):
            for data in (PQ, GAMMA, PQ.replace(b'\n', b'\r\n')):
                self.assertTrue(display._is_our_script(data))
            for data in (None, b'', PQ + b'-- edited\n', b'-- hand tuned\n'):
                self.assertFalse(display._is_our_script(data))

    def test_user_dir_files_the_user_does_not_own_are_not_read(self):
        display.SCRIPT_DIR = str(self.root)
        script = self.root / display.SCRIPT_NAME
        script.write_bytes(b'lua')
        display.SCRIPT_USER = (str(self.root), script.stat().st_uid + 1, 0)
        self.assertIsNone(display._read_bytes(str(script)))
        display.SCRIPT_USER = (str(self.root), script.stat().st_uid, 0)
        self.assertEqual(display._read_bytes(str(script)), b'lua')


@POSIX
class OldScriptCleanupTests(unittest.TestCase):
    """The real helper removes or restores files as the current user."""
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.base = self.root / 'config'
        self.dir = self.base / 'gamescope' / 'scripts'
        self.script = self.dir / display.SCRIPT_NAME
        self.backup = Path(str(self.script) + '.backup')
        self.legacy = self.root / 'root-config' / 'gamescope' / 'scripts'
        self.dir.mkdir(parents=True)
        for name, value in {'SCRIPT_DIR': str(self.dir), 'INSTALLED_SCRIPT': str(self.script),
                            'SCRIPT_USER': (str(self.base), os.getuid(), os.getgid()),
                            'LEGACY_SCRIPT_DIR': str(self.legacy), '_script_binding': ('gamescope', 77),
                            'OLD_SCRIPT_SHA256': OURS}.items():
            self.stack.enter_context(patch.object(display, name, value))
        self.stack.enter_context(patch.object(display, '_bind_script_dir', return_value=True))
        real_run = subprocess.run
        self.calls = []
        def unprivileged(argv, **kwargs):
            # Only root may drop to another uid; run the real helper as ourselves.
            self.calls.append({key: kwargs.pop(key) for key in ('user', 'group', 'extra_groups')})
            return real_run([sys.executable] + argv[1:], **kwargs)
        self.stack.enter_context(patch.object(display.subprocess, 'run', side_effect=unprivileged))

    def files(self, current=None, backup=None):
        for path, data in ((self.script, current), (self.backup, backup)):
            path.unlink(missing_ok=True)
            if data is not None:
                path.write_bytes(data)

    def state(self):
        return tuple(path.read_bytes() if path.exists() else None for path in (self.script, self.backup))

    def test_every_start_leaves_steamos_or_the_users_own_script_in_charge(self):
        foreign, crlf = b'-- hand tuned\n', GAMMA.replace(b'\n', b'\r\n')
        cases = (  # (current, backup), SteamOS 3.8 result, SteamOS 3.9 result
            ((PQ, None), (None, None), (None, None)),
            ((crlf, PQ), (None, None), (None, None)),
            ((PQ, foreign), (foreign, None), (None, foreign)),
            ((None, foreign), (foreign, None), (None, foreign)),
            ((foreign, None), (foreign, None), (foreign, None)),
            ((foreign, foreign + b'old'), (foreign, foreign + b'old'), (foreign, foreign + b'old')),
            ((None, None), (None, None), (None, None)),
        )
        for files, on_38, on_39 in cases:
            for restore, expected in ((True, on_38), (False, on_39)):
                with self.subTest(files=files, restore_backup=restore):
                    self.files(*files)
                    self.assertIsNotNone(display._remove_old_scripts(restore_backup=restore))
                    self.assertEqual(self.state(), expected)
                    self.assertEqual(display._remove_old_scripts(restore_backup=restore),
                                     'kept a third-party display script' if expected[0] else 'nothing to remove',
                                     'a second start changes nothing more')
                    self.assertEqual(self.state(), expected)
        self.assertTrue(self.calls)
        self.assertTrue(all(call == {'user': os.getuid(), 'group': os.getgid(), 'extra_groups': []}
                            for call in self.calls))

    def test_links_and_files_the_user_does_not_own_are_left_alone(self):
        victim = self.root / 'victim'
        victim.write_bytes(PQ)
        self.script.symlink_to(victim)
        self.backup.write_bytes(b'-- hand tuned\n')
        self.assertEqual(display._remove_old_scripts(restore_backup=True), 'kept an unreadable display script')
        self.assertTrue(self.script.is_symlink())
        self.assertEqual(victim.read_bytes(), PQ)
        self.assertEqual(self.backup.read_bytes(), b'-- hand tuned\n')

    def test_only_our_copies_are_removed_from_the_old_root_location(self):
        self.legacy.mkdir(parents=True)
        (self.legacy / display.SCRIPT_NAME).write_bytes(PQ)
        (self.legacy / (display.SCRIPT_NAME + '.backup')).write_bytes(b'-- foreign')
        (self.legacy / 'other.lua').write_bytes(PQ)
        self.assertEqual(display._remove_old_scripts(restore_backup=False), 'nothing to remove')
        self.assertEqual(sorted(os.listdir(self.legacy)), sorted(['other.lua', display.SCRIPT_NAME + '.backup']))

    def test_cleanup_waits_for_a_known_profile_and_gamescope_user(self):
        self.files(PQ, b'-- hand tuned\n')
        state = dict(display.Plugin._state)
        with patch.object(display.Plugin, '_state', state):
            for status, bound, done in (('inconclusive', True, False), ('absent', False, False), ('absent', True, True)):
                with self.subTest(status=status, bound=bound), \
                        patch.object(display, '_bind_script_dir', return_value=bound), \
                        patch.object(native, 'detect_native_display_support', return_value={'status': status}):
                    self.assertEqual(display.Plugin._cleanup_old_display_fix(), done)
                    self.assertEqual(state['steamos_update_needed'], status == 'absent')
        self.assertEqual(self.state(), (b'-- hand tuned\n', None))

    def test_helper_creates_the_tree_and_writes_atomically_readable(self):
        helper = lambda *argv, data=b'': self.helper(*argv, data=data)
        self.dir.rmdir()
        self.assertEqual(helper('write', str(self.base), display.SCRIPT_NAME, data=b'lua'), 0)
        self.assertEqual(self.script.read_bytes(), b'lua')
        self.assertEqual(self.script.stat().st_mode & 0o777, 0o644)
        self.assertEqual(sorted(os.listdir(self.dir)), [display.SCRIPT_NAME])
        self.assertEqual(helper('unlink', str(self.base), display.SCRIPT_NAME), 0)
        self.assertEqual(helper('unlink', str(self.base), display.SCRIPT_NAME), 2)
        for argv in (('write', 'relative', 'x'), ('write', str(self.base), '../x'), ('chmod', str(self.base), 'x')):
            self.assertEqual(helper(*argv), 1)

    def test_helper_refuses_symlinked_dirs_and_non_regular_targets(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        self.dir.rmdir(); self.dir.parent.rmdir()
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

    def helper(self, *argv, data=b''):
        with patch.object(sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO(data))):
            return display_script_io.main(['display_script_io.py', *argv])


class EdidBackupMoveTests(unittest.TestCase):
    """2.3.0 kept the original EDID in the settings folder Decky hands to the user."""
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.private = self.root / 'var-lib'
        self.new = self.private / 'edid-original.bin'
        self.old = self.root / 'settings' / 'edid-original.bin'
        self.old.parent.mkdir()
        self.original, _ = edid_fixture()
        for name, value in {'EDID_BACKUP': str(self.new), 'OLD_EDID_BACKUP': str(self.old)}.items():
            self.stack.enter_context(patch.object(display, name, value))

    def test_a_valid_old_copy_moves_once_and_a_newer_copy_wins(self):
        self.old.write_bytes(self.original)
        self.assertEqual(display._load_edid_backup(), self.original)
        self.assertEqual(self.new.read_bytes(), self.original)
        self.assertFalse(self.old.exists())
        if os.name == 'posix':
            self.assertEqual(self.private.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.new.stat().st_mode & 0o777, 0o600)
        self.assertEqual(display._load_edid_backup(), self.original)
        self.old.write_bytes(self.original[:128] + self.original[128:256])
        self.assertEqual(display._load_edid_backup(), self.original, 'the private copy is newer')
        self.assertFalse(self.old.exists())

    def test_anything_that_is_not_an_edid_is_neither_moved_nor_used(self):
        for data in (b'', self.original[:100], self.original + b'\0', b'\x01' + self.original[1:]):
            with self.subTest(size=len(data)):
                self.old.write_bytes(data)
                self.assertIsNone(display._load_edid_backup())
                self.assertFalse(self.new.exists())
                self.assertEqual(self.old.read_bytes(), data)

    def test_the_old_copy_stays_until_the_move_succeeds(self):
        self.old.write_bytes(self.original)
        with patch.object(display.safe_settings, '_atomic_write_bytes', side_effect=OSError('read-only /var')):
            self.assertEqual(display._load_edid_backup(), self.original)
        self.assertEqual(self.old.read_bytes(), self.original)
        self.assertEqual(display._load_edid_backup(), self.original)
        self.assertFalse(self.old.exists())

    @POSIX
    def test_a_directory_others_can_write_is_not_trusted(self):
        self.private.mkdir()
        self.new.write_bytes(self.original)
        self.private.chmod(0o777)
        self.assertIsNone(display._load_edid_backup())
        self.assertFalse(display._save_edid_backup(self.original))

    @POSIX
    def test_a_settings_folder_linked_to_the_private_one_never_removes_the_private_copy(self):
        self.private.mkdir(mode=0o700)
        self.new.write_bytes(self.original)
        self.old.parent.rmdir()
        self.old.parent.symlink_to(self.private)  # the user owns the settings folder and can do this
        self.assertEqual(display._load_edid_backup(), self.original)
        self.assertEqual(self.new.read_bytes(), self.original)

    def test_an_interrupted_2_3_0_write_is_still_restored_from_the_moved_copy(self):
        import test_display_edid
        case = test_display_edid.DisplayWriteRecoveryTests()
        case.setUp(); self.addCleanup(case.doCleanups)
        case.interrupt_patch()  # 2.3.0 stopped half-way: journal in the settings, original in its folder
        self.assertIsNotNone(case.store.getSetting('edid_write_pending'))
        os.replace(case.backup, self.old)
        with patch.object(display, 'EDID_BACKUP', str(self.new)):
            display.Plugin._edid_original = display._load_edid_backup()
            self.assertTrue(display.Plugin._edid_restore())
        self.assertEqual(case.edid.read_bytes(), case.original)
        self.assertIsNone(case.store.getSetting('edid_write_pending'))
        self.assertFalse(self.new.exists())
        self.assertFalse(self.old.exists())


class DisplayWriterTests(unittest.TestCase):
    def test_display_writer_ignores_preexisting_staging_link(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            victim = root / "unrelated-file"
            victim.write_bytes(b"original unrelated data")
            target = root / "script.lua"
            staging = Path(str(target) + ".lego2brightnessfix.tmp")
            # Hard links work without Windows symlink privileges. This tests
            # following an existing inode, not Linux privilege escalation.
            os.link(victim, staging)
            self.assertTrue(display._write_file_atomically(str(target), b"lua"))
            self.assertEqual(victim.read_bytes(), b"original unrelated data")

    @POSIX
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


class DisplayStartTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.manager = AtomicSettingsManager('display_settings', str(self.root))
        self.stack.enter_context(patch.object(display, 'settings', self.manager))
        for name, value in {'_state': dict(display.Plugin._state), '_task': None, '_prop_task': None,
                            '_edid_original': None, '_cleanup_done': False,
                            '_cleanup_retry_after': 0.0, '_cleanup_retry_delay': display.GATE_INTERVAL_S}.items():
            self.stack.enter_context(patch.object(display.Plugin, name, value))

    def test_a_cleanup_that_keeps_failing_is_retried_ever_more_rarely(self):
        # Each attempt runs a helper process; a symlinked ~/.config/gamescope makes every one fail.
        clock = [100.0]
        remove = Mock(side_effect=OSError('refused a symlinked directory'))
        with patch.object(display, 'time', types.SimpleNamespace(monotonic=lambda: clock[0])),                 patch.object(display.Plugin, '_refresh_native', return_value='supported'),                 patch.object(display, '_remove_old_scripts', remove):
            for _ in range(1800):  # an hour of 2 s loop passes
                self.assertFalse(display.Plugin._cleanup_old_display_fix())
                clock[0] += display.GATE_INTERVAL_S
            self.assertLess(remove.call_count, 25)  # 1800 without the back-off
            remove.side_effect, remove.return_value = None, 'removed the old display script'
            clock[0] += display.CLEANUP_RETRY_MAX_S
            self.assertTrue(display.Plugin._cleanup_old_display_fix())

    def test_a_2_3_0_settings_file_keeps_only_the_edid_fix_and_its_recovery(self):
        pending = {'schema': 1, 'path': '/tmp/edid', 'device': 1, 'inode': 2,
                   'partial_sha256': None, 'original_sha256': 'x'}
        self.manager.replace({'enabled': True, 'edid_fix': False, 'panel_mode': 'hybrid',
                              'active_mode': 'hybrid', 'native_retired': True, 'script_reinstall': True,
                              'reset_in_progress': True, 'edid_write_pending': pending,
                              'edid_patched_identity': {'device': 1, 'inode': 2}})
        cleanup, edid = Mock(return_value=True), Mock()
        with patch.object(display, '_load_edid_backup', return_value=None), \
                patch.object(display, '_pick_display', return_value=True), \
                patch.object(display, '_watch_properties', new=AsyncMock()), \
                patch.object(display.Plugin, '_loop', new=AsyncMock()), \
                patch.object(display.Plugin, '_cleanup_old_display_fix', cleanup), \
                patch.object(display.Plugin, '_edid_pass', edid):
            plugin = display.Plugin()
            async def run():
                await plugin._main()
                await asyncio.gather(plugin._task, plugin._prop_task)
            asyncio.run(run())
        saved = json.loads(Path(self.manager.path).read_text(encoding='utf-8'))
        self.assertEqual(saved, {'edid_fix': False, 'edid_write_pending': pending,
                                 'edid_patched_identity': {'device': 1, 'inode': 2}})
        state = display.Plugin._state
        self.assertEqual((state['settings_error'], state['edid_fix']), ('', False))
        cleanup.assert_called_once_with()
        edid.assert_called_once_with()

    def test_the_loop_retries_the_cleanup_until_it_is_done_and_survives_a_failing_pass(self):
        results, passes = [False, False, True], []
        async def offload(fn, *args):
            return fn(*args)
        async def sleep(_seconds):
            if len(passes) >= 5:
                raise asyncio.CancelledError()
        def edid():
            passes.append(1)
            raise IndexError('index out of range')
        with patch.object(display.Plugin, '_cleanup_old_display_fix', side_effect=lambda: results.pop(0)) as cleanup, \
                patch.object(display.Plugin, '_edid_pass', side_effect=edid), \
                patch.object(display, '_offload', new=offload), \
                patch.object(display.asyncio, 'sleep', new=sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(display.Plugin()._loop())
        self.assertEqual(cleanup.call_count, 3)
        self.assertEqual(len(passes), 5)

    def test_lost_settings_block_startup_and_every_write(self):
        primary = self.root / "display_settings.json"
        backup = Path(str(primary) + ".bak")
        primary.write_text("{broken primary")
        backup.write_text("{broken backup")
        manager = AtomicSettingsManager("display_settings", str(self.root))
        before = (primary.read_bytes(), backup.read_bytes())
        self.assertTrue(manager.recovery_error)
        plugin = display.Plugin()
        with patch.object(display, "settings", manager), \
                patch.object(display, "_pick_display") as discover, \
                patch.object(display, "_remove_old_scripts") as cleanup, \
                patch.object(display.Plugin, "_edid_pass") as edid, \
                patch.object(display.Plugin, "_edid_restore") as restore:
            with self.assertRaises(CorruptSettings):
                asyncio.run(plugin._main())
            self.assertIsNone(plugin._task)
            self.assertIsNone(plugin._prop_task)
            self.assertIn("could not be recovered", asyncio.run(plugin.get_state())["settings_error"])
            with self.assertRaises(CorruptSettings):
                display.Plugin._store_settings({"edid_patched_identity": None})
            for operation in (lambda: plugin.set_edid_fix(True), plugin._unload):
                with self.assertRaises(CorruptSettings):
                    asyncio.run(operation())
            for operation in (discover, cleanup, edid, restore):
                operation.assert_not_called()
        self.assertEqual((primary.read_bytes(), backup.read_bytes()), before)


class GamescopeSessionTests(unittest.TestCase):
    def test_desktop_mode_backs_off_xprop_and_probes_at_once_when_gamescope_returns(self):
        now, spawns, gamescope = [0.0], [], [None]

        def run(argv, **_kw):  # KWin's Xwayland: an X server without GAMESCOPE atoms
            spawns.append(argv)
            return subprocess.CompletedProcess(argv, 0, "_NET_SUPPORTED(ATOM) = x\n", "")

        async def offload(fn, *args):
            return fn(*args)

        async def sleep(seconds):
            now[0] += seconds
            if now[0] >= 600:
                raise asyncio.CancelledError()

        with patch.object(display.Plugin, "_state", dict(display.Plugin._state)), \
             patch.object(display.Plugin, "_session_retry_after", 0.0), \
             patch.object(display.Plugin, "_session_retry_delay", display.GATE_INTERVAL_S), \
             patch.object(display, "_display_candidates", return_value=(":0",)), \
             patch.object(display, "_gamescope_start_time", side_effect=lambda: gamescope[0]), \
             patch.object(display, "_xprop_command", side_effect=lambda args, *_: ["xprop", *args]), \
             patch.object(display, "_xprop_env", return_value={}), \
             patch.object(display, "_xprop_user", return_value={}), \
             patch.object(display.subprocess, "run", side_effect=run), \
             patch.object(display.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(display.asyncio, "sleep", new=sleep), \
             patch.object(display, "_offload", new=offload):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(display._watch_properties())
            for tick in range(300):  # the EDID pass, every 2 s for 10 minutes
                now[0] = tick * display.GATE_INTERVAL_S
                display.Plugin._session_props()
            self.assertLess(len(spawns), 100)  # ~600 without the back-off: one xprop every pass
            spawns.clear()
            gamescope[0] = display._IDENTITY_BASE  # Gaming Mode is back
            display.Plugin._session_props()
            self.assertTrue(spawns)

    def test_session_identity_is_the_process_start_not_the_proc_inode_mtime(self):
        def stat_line(ticks):
            # comm may hold spaces and parentheses; starttime is field 22.
            return f"42 (gamescope (wl) S 1 42 42 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {ticks} 0 0"
        files = {"/proc/42/comm": "gamescope-wl\n", "/proc/42/stat": stat_line(1000),
                 "/proc/sys/kernel/random/boot_id": "0a1b2c3d-1111-2222-3333-444455556666\n"}
        def fake_open(path, *args, **kwargs):
            if path not in files:
                raise FileNotFoundError(path)
            return io.StringIO(files[path])
        def no_mtime(path, *args, **kwargs):
            raise AssertionError(f"identity must not use stat({path}) mtime")
        with patch.object(display, "_gamescope_pid", None), patch.object(display, "_gamescope_scan_after", 0.0), \
             patch.object(display, "_CLK_TCK", 100), patch.object(display, "open", fake_open, create=True), \
             patch.object(display.os, "listdir", return_value=["1", "42"]), patch.object(display.os, "stat", no_mtime):
            first = display._gamescope_start_time()
            self.assertEqual(display._gamescope_start_time(), first)
            files["/proc/sys/kernel/random/boot_id"] = "ffffffff-1111-2222-3333-444455556666\n"
            other_boot = display._gamescope_start_time()
            files["/proc/42/stat"] = stat_line(1500)
            restarted = display._gamescope_start_time()
        self.assertNotEqual(first, other_boot)
        self.assertAlmostEqual(restarted - other_boot, 5.0)


class NativeDisplayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.script = Path(directory.name) / "native.lua"
        patched = patch.object(native, "SYSTEM_SCRIPT", str(self.script))
        patched.start()
        self.addCleanup(patched.stop)

    def test_any_file_contents_indicate_support_without_reading_or_executing(self):
        for contents in (b"", b"-- Future native PQ support, no workaround flags", b"\xff\x00"):
            with self.subTest(contents=contents):
                self.script.write_bytes(contents)
                with patch("builtins.open", side_effect=AssertionError("No content reads")):
                    result = native.detect_native_display_support()
                self.assertTrue(result["supported"])
                self.assertEqual(result["status"], "supported")

    def test_profile_appearance_and_removal_are_observed_in_the_page_state(self):
        state = dict(display.Plugin._state)
        with patch.object(display.Plugin, "_state", state):
            self.assertTrue(asyncio.run(display.Plugin().get_state())["steamos_update_needed"])
            self.script.touch()
            self.assertFalse(asyncio.run(display.Plugin().get_state())["steamos_update_needed"])

    def test_a_directory_or_an_access_error_is_inconclusive_not_absent(self):
        self.script.mkdir()
        self.assertEqual(native.detect_native_display_support()["status"], "inconclusive")
        self.script.rmdir()
        for error in (PermissionError, OSError):
            with self.subTest(error=error), patch.object(native.os, "stat", side_effect=error):
                result = native.detect_native_display_support()
                self.assertFalse(result["supported"])
                self.assertEqual(result["status"], "inconclusive")


if __name__ == '__main__':
    unittest.main()

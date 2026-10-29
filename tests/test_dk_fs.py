# SPDX-License-Identifier: BSD-3-Clause
# Shared from DeckyKit (shared/tests/test_dk_fs.py): change it there, then `kit sync`.
# One test (or more) per guarantee in dk_fs.py's docstring; POSIX-only ones run in WSL/CI Linux.
import contextlib
import json
import os
import shutil
import stat
import tempfile
import time
import unittest
import unittest.mock
from unittest import mock

import _harness  # noqa: F401
import dk_fs

POSIX = os.name == "posix"


def can_symlink(folder):
    try:
        os.symlink(os.path.join(folder, "x"), os.path.join(folder, ".probe"))
        os.remove(os.path.join(folder, ".probe"))
        return True
    except (OSError, NotImplementedError):
        return False


def as_root():
    return mock.patch.object(dk_fs, "_is_root", return_value=True)


def read(path):
    with open(path, "rb") as f:
        return f.read()


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sub", "settings.json")

    def files(self):
        return sorted(os.listdir(os.path.dirname(self.path)))

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        dk_fs.write_atomic(self.path, b"one")
        dk_fs.write_atomic(self.path, b"two")
        self.assertEqual(read(self.path), b"two")
        self.assertEqual(self.files(), ["settings.json"])

    def test_a_file_object_is_copied_whole(self):
        with tempfile.TemporaryFile() as source:
            source.write(b"x" * (3 * dk_fs.CHUNK + 5))
            source.seek(0)
            dk_fs.write_atomic(self.path, source)
        self.assertEqual(len(read(self.path)), 3 * dk_fs.CHUNK + 5)

    def test_a_failed_write_keeps_the_old_file_and_removes_its_temp_file(self):
        dk_fs.write_atomic(self.path, b"old")
        with mock.patch.object(dk_fs.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            dk_fs.write_atomic(self.path, b"new")
        self.assertEqual(read(self.path), b"old")
        self.assertEqual(self.files(), ["settings.json"])

    def test_file_and_folder_are_fsynced(self):
        synced = []
        real = os.fsync
        with mock.patch.object(dk_fs.os, "fsync", side_effect=lambda fd: (synced.append(
                stat.S_ISDIR(os.fstat(fd).st_mode)), real(fd))):
            dk_fs.write_atomic(self.path, b"x")
        self.assertIn(False, synced, "the file")
        if POSIX:
            self.assertIn(True, synced, "and its folder")

    def test_never_writes_over_anything_but_a_regular_file(self):
        os.makedirs(self.path)  # a folder where the file should be
        with self.assertRaises(dk_fs.UnsafePath):
            dk_fs.write_atomic(self.path, b"x")

    def test_a_symlinked_file_is_refused_as_root_and_written_through_for_a_user(self):
        if not can_symlink(self.dir):
            self.skipTest("no symlinks on this host")
        target = os.path.join(self.dir, "dotfiles", "conf.toml")
        os.makedirs(os.path.dirname(target))
        write(target, b"old")
        os.makedirs(os.path.dirname(self.path))
        os.symlink(target, self.path)
        with as_root(), self.assertRaises(dk_fs.UnsafePath):
            dk_fs.write_atomic(self.path, b"root")
        with self.assertRaises(dk_fs.UnsafePath):
            dk_fs.write_atomic(self.path, b"strict", no_links=True)
        self.assertEqual(read(target), b"old")
        dk_fs.write_atomic(self.path, b"user")  # a dotfile manager's link stays, its target gets the content
        self.assertTrue(os.path.islink(self.path))
        self.assertEqual(read(target), b"user")
        self.assertEqual(dk_fs.read_bytes(self.path), b"user")
        with as_root(), self.assertRaises(dk_fs.UnsafePath):
            dk_fs.read_bytes(self.path)

    def test_a_symlinked_folder_is_refused_as_root_only(self):
        if not can_symlink(self.dir):
            self.skipTest("no symlinks on this host")
        real = os.path.join(self.dir, "real")
        os.makedirs(real)
        link = os.path.join(self.dir, "link")
        os.symlink(real, link, target_is_directory=True)
        dk_fs.write_atomic(os.path.join(link, "a"), b"user may")
        with as_root(), self.assertRaises(dk_fs.UnsafePath):
            dk_fs.write_atomic(os.path.join(link, "b"), b"root may not")
        with self.assertRaises(dk_fs.UnsafePath):
            dk_fs.write_atomic(os.path.join(link, "c"), b"x", no_links=True)
        self.assertEqual(sorted(os.listdir(real)), ["a"])

    def test_a_symlinked_folder_is_refused_for_reads_as_root_too(self):
        if not can_symlink(self.dir):
            self.skipTest("no symlinks on this host")
        real = os.path.join(self.dir, "real")
        os.makedirs(real)
        write(os.path.join(real, "s.json"), b'{"v": 1}')
        link = os.path.join(self.dir, "link")
        os.symlink(real, link, target_is_directory=True)
        self.assertEqual(dk_fs.read_bytes(os.path.join(link, "s.json")), b'{"v": 1}')  # a user plugin may
        for read_it in (dk_fs.read_bytes, dk_fs.load_json):
            with as_root(), self.assertRaises(dk_fs.UnsafePath, msg=read_it.__name__):
                read_it(os.path.join(link, "s.json"))
        self.assertEqual(sorted(os.listdir(real)), ["s.json"], "no copy made through the link")

    @unittest.skipUnless(POSIX, "O_NOFOLLOW on the folder is POSIX")
    def test_a_folder_swapped_for_a_symlink_after_the_check_is_still_refused(self):
        real = os.path.join(self.dir, "real")
        os.makedirs(real)
        link = os.path.join(self.dir, "link")
        os.symlink(real, link)
        with as_root(), mock.patch.object(dk_fs, "_make_folder"), self.assertRaises(dk_fs.UnsafePath):
            dk_fs.write_atomic(os.path.join(link, "x"), b"x")  # the check "passed": the open itself refuses
        self.assertEqual(os.listdir(real), [])

    @unittest.skipUnless(POSIX, "FIFOs are POSIX")
    def test_a_fifo_is_refused_for_writing_and_reading_without_blocking(self):
        os.makedirs(os.path.dirname(self.path))
        os.mkfifo(self.path)
        with self.assertRaises(dk_fs.UnsafePath):
            dk_fs.write_atomic(self.path, b"x")
        with self.assertRaises(dk_fs.UnsafePath):
            dk_fs.read_bytes(self.path)

    @unittest.skipUnless(POSIX, "dir_fd is POSIX")
    def test_every_step_works_relative_to_the_opened_folder(self):
        real = os.replace
        seen = []
        with mock.patch.object(dk_fs.os, "replace", side_effect=lambda *a, **k: (seen.append(k), real(*a, **k))):
            dk_fs.write_atomic(self.path, b"x")
        self.assertEqual(set(seen[0]), {"src_dir_fd", "dst_dir_fd"})

    @unittest.skipUnless(POSIX, "file modes are POSIX")
    def test_a_replaced_file_keeps_its_mode_and_a_new_one_gets_0644(self):
        dk_fs.write_atomic(self.path, b"x")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)
        os.chmod(self.path, 0o640)
        dk_fs.write_atomic(self.path, b"y")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        dk_fs.write_atomic(self.path, b"z", 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    @unittest.skipUnless(POSIX, "owners are POSIX")
    def test_new_files_get_the_folders_owner_and_replaced_files_keep_theirs(self):
        os.makedirs(os.path.dirname(self.path))
        folder = os.stat(os.path.dirname(self.path))
        with mock.patch.object(dk_fs.os, "geteuid", return_value=folder.st_uid + 1), \
                mock.patch.object(dk_fs.os, "fchown", wraps=os.fchown) as chown:
            dk_fs.write_atomic(self.path, b"x")  # a user process cannot chown: that must not fail the write
            self.assertEqual(chown.call_args[0][1:], (folder.st_uid, folder.st_gid))
            existing = os.stat(self.path)
            fake = os.stat_result((existing.st_mode, 0, 0, 0, 4242, 4343, 0, 0, 0, 0))
            real_stat = os.stat
            with mock.patch.object(dk_fs.os, "stat", side_effect=lambda p, **k: fake if p == "settings.json"
                                   else real_stat(p, **k)):
                dk_fs.write_atomic(self.path, b"y")
            self.assertEqual(chown.call_args[0][1:], (4242, 4343))

    @unittest.skipUnless(POSIX, "owners are POSIX")
    def test_a_given_owner_wins_over_the_replaced_files(self):
        dk_fs.write_atomic(self.path, b"old")
        with mock.patch.object(dk_fs.os, "fchown") as chown:
            dk_fs.write_atomic(self.path, b"new", owner=(os.getuid() + 1, os.getgid()))
        self.assertEqual(chown.call_args.args[1:], (os.getuid() + 1, os.getgid()))
        self.assertEqual(read(self.path), b"new")

    @unittest.skipUnless(POSIX, "folder modes are POSIX")
    def test_json_stores_create_a_private_folder(self):
        dk_fs.write_json(self.path, {"v": 1})
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700)
        dk_fs.write_atomic(os.path.join(self.dir, "plain", "f"), b"x")
        self.assertNotEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "plain")).st_mode), 0o700)

    @unittest.skipUnless(POSIX, "folder fsync is POSIX")
    def test_a_failed_folder_fsync_is_not_a_failed_write_unless_strict(self):
        real = os.fsync

        def folder_fails(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(5, "EIO")
            real(fd)

        self.assertTrue(dk_fs.write_atomic(self.path, b"old"))
        with mock.patch.object(dk_fs.os, "fsync", side_effect=folder_fails):
            self.assertFalse(dk_fs.write_atomic(self.path, b"new"), "the caller is told, the write stands")
            self.assertEqual(read(self.path), b"new")
            with self.assertRaises(OSError):
                dk_fs.write_atomic(self.path, b"newer", strict_sync=True)

    @unittest.skipUnless(POSIX, "orphans of killed (root) writes are cleaned on POSIX")
    def test_temp_files_of_a_killed_write_are_removed_by_the_next_write(self):
        os.makedirs(os.path.dirname(self.path))
        folder = os.path.dirname(self.path)
        for name, age in ((".settings.json.1.abc.tmp", 3600), (".settings.json.2.def.tmp", 0),
                          (".other.json.1.abc.tmp", 3600)):
            write(os.path.join(folder, name), b"")
            os.utime(os.path.join(folder, name), (time.time() - age,) * 2)
        dk_fs.write_atomic(self.path, b"x")
        self.assertEqual(self.files(), sorted([".other.json.1.abc.tmp", ".settings.json.2.def.tmp", "settings.json"]))

    @unittest.skipUnless(POSIX, "folder fds are POSIX")
    def test_a_write_into_an_opened_folder_stays_in_it_and_leaves_the_fd_open(self):
        fd = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        dk_fs.write_atomic("update.zip", b"zip", 0o644, folder_fd=fd)
        with open(os.path.join(self.dir, "update.zip"), "rb") as f:
            self.assertEqual(f.read(), b"zip")
        os.fstat(fd)  # still the caller's
        for bad in ("../escape.zip", "sub/x", "", ".."):
            with self.assertRaises(ValueError):
                dk_fs.write_atomic(bad, b"x", folder_fd=fd)
        self.assertEqual(sorted(os.listdir(self.dir)), ["update.zip"])

    def test_sha256_file_reads_in_blocks(self):
        path = os.path.join(self.dir, "big.bin")
        data = os.urandom(3 * dk_fs.CHUNK + 5)
        with open(path, "wb") as f:
            f.write(data)
        reads = []
        real_open = open

        class Spy:
            def __init__(self, *a, **k):
                self.f = real_open(*a, **k)

            def read(self, n=-1):
                reads.append(n)
                return self.f.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.f.close()

        with unittest.mock.patch("builtins.open", side_effect=Spy):
            self.assertEqual(dk_fs.sha256_file(path), __import__("hashlib").sha256(data).hexdigest())
        self.assertTrue(reads and all(0 < n <= dk_fs.CHUNK for n in reads), reads)

    @unittest.skipUnless(POSIX, "open_directory is POSIX")
    def test_open_directory_walks_without_symlinks_and_creates_with_owner(self):
        os.makedirs(os.path.join(self.dir, "a"))
        os.symlink(os.path.join(self.dir, "a"), os.path.join(self.dir, "link"))
        base = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY)
        fd = dk_fs.open_directory(["a", "b"], base, create=lambda part: (0o750,))
        os.close(fd)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "a", "b")).st_mode) & 0o750, 0o750)
        with self.assertRaises(OSError):
            dk_fs.open_directory(["link"], os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY))


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "settings.json")

    def test_reads_regular_files_only_and_at_most_limit_bytes(self):
        write(self.path, b"x" * 11)
        self.assertEqual(dk_fs.read_bytes(self.path, 11), b"x" * 11)
        with self.assertRaises(dk_fs.Damaged):
            dk_fs.read_bytes(self.path, 10)
        with self.assertRaises(FileNotFoundError):
            dk_fs.read_bytes(self.path + ".missing")
        with self.assertRaises((dk_fs.UnsafePath, IsADirectoryError, PermissionError)):
            dk_fs.read_bytes(self.dir)

    def test_json_is_read_strictly(self):
        self.assertEqual(dk_fs.parse_json(b'\xef\xbb\xbf{"a": [1, 2.5, "\\u00e9"]}'), {"a": [1, 2.5, "é"]})
        for bad in (b'{"a": 1, "a": 2}', b'{"a": NaN}', b'[Infinity]', b'{"a": "\xff"}', b'{"a": 1',
                    b"[" * 65 + b"]" * 65, b"[" * 100000, b"1" * 5000):
            with self.assertRaises(dk_fs.Damaged, msg=bad[:20]):
                dk_fs.parse_json(bad)
        dk_fs.parse_json(b"[" * 64 + b"]" * 64)

    def test_validate_refuses_content_that_parses(self):
        with self.assertRaises(dk_fs.Damaged):
            dk_fs.parse_json(b"[1]", dk_fs.require_object)

        def positive(value):
            if value["n"] < 0:
                raise ValueError("negative")

        with self.assertRaises(dk_fs.Damaged):
            dk_fs.parse_json(b'{"n": -1}', positive)


class JsonStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "settings.json")

    def damage(self, text=b'{"v": 3'):
        write(self.path, text)

    def aside(self):
        return [n for n in os.listdir(self.dir) if ".corrupt-" in n]

    def test_missing_or_empty_file_reads_as_default(self):
        self.assertEqual(dk_fs.load_json(self.path, {"d": 1}), dk_fs.Loaded({"d": 1}, "missing", ""))
        self.damage(b"")  # Decky's first-install file
        self.assertEqual(dk_fs.load_json(self.path, {"d": 1}).status, "missing")

    def test_the_last_good_copy_is_what_was_just_written(self):
        dk_fs.write_json(self.path, {"v": 1})
        dk_fs.write_json(self.path, {"v": 2})
        self.assertEqual(dk_fs.read_json(self.path + dk_fs.LAST_GOOD), {"v": 2})
        self.damage()
        loaded = dk_fs.load_json(self.path)
        self.assertEqual((loaded.value, loaded.status), ({"v": 2}, "recovered"), "recovery loses nothing")

    def test_an_inherited_file_gets_its_good_copy_on_the_first_read(self):
        self.damage(b'{"v": 7}')
        self.assertEqual(dk_fs.read_json(self.path), {"v": 7})
        self.assertEqual(dk_fs.read_json(self.path + dk_fs.LAST_GOOD), {"v": 7})

    def test_a_damaged_file_is_kept_aside_and_the_good_copy_put_back_in_one_step(self):
        dk_fs.write_json(self.path, {"v": 1})
        self.damage()
        loaded = dk_fs.load_json(self.path)
        self.assertEqual((loaded.value, loaded.status), ({"v": 1}, "recovered"))
        self.assertEqual(len(self.aside()), 1, "the damaged file is kept, never deleted")
        self.assertEqual(read(os.path.join(self.dir, self.aside()[0])), b'{"v": 3')
        self.assertEqual(dk_fs.load_json(self.path).status, "ok", "the good copy is back in place")

    def test_a_recovery_cut_short_is_finished_by_the_next_read(self):
        dk_fs.write_json(self.path, {"v": 1})
        self.damage()
        real = dk_fs.write_atomic
        with mock.patch.object(dk_fs, "write_atomic", side_effect=lambda p, *a, **k: real(p, *a, **k)
                               if ".corrupt-" in p else (_ for _ in ()).throw(OSError("killed"))):
            with self.assertRaises(OSError):
                dk_fs.load_json(self.path)
        self.assertEqual(read(self.path), b'{"v": 3', "never a moment without the file")
        self.assertEqual(dk_fs.load_json(self.path).value, {"v": 1})

    def test_a_missing_file_beside_a_good_copy_is_restored(self):
        dk_fs.write_json(self.path, {"v": 1})
        os.remove(self.path)  # e.g. killed between remove_json's two steps
        loaded = dk_fs.load_json(self.path)
        self.assertEqual((loaded.value, loaded.status), ({"v": 1}, "recovered"))
        self.assertEqual(self.aside(), [])

    def test_io_errors_are_raised_and_nothing_is_touched(self):
        dk_fs.write_json(self.path, {"v": 1})
        dk_fs.write_json(self.path, {"v": 2})
        before = {n: read(os.path.join(self.dir, n)) for n in os.listdir(self.dir)}
        real = dk_fs.read_bytes
        for error in (PermissionError(13, "EACCES"), OSError(5, "EIO")):
            def failing(path, *args, error=error, **kwargs):  # only the file itself; its good copy reads fine
                if path == self.path:
                    raise error
                return real(path, *args, **kwargs)
            with mock.patch.object(dk_fs, "read_bytes", side_effect=failing), self.assertRaises(OSError):
                dk_fs.load_json(self.path)
        self.assertEqual({n: read(os.path.join(self.dir, n)) for n in os.listdir(self.dir)}, before)

    def test_a_damaged_file_without_a_good_copy_stays_and_is_reported_lost(self):
        self.damage()
        for _ in range(2):  # a restart still sees it: never mistaken for a first install
            loaded = dk_fs.load_json(self.path, {"d": 1})
            self.assertEqual((loaded.value, loaded.status), ({"d": 1}, "lost"))
            self.assertIn("settings.json", loaded.detail)
        self.assertEqual(read(self.path), b'{"v": 3')
        self.assertEqual(self.aside(), [loaded.kept], "saved once, before the caller's next write replaces it")
        self.assertIn(f"kept as {loaded.kept}", loaded.detail)
        dk_fs.write_json(self.path, {"d": 1})
        self.assertEqual(read(os.path.join(self.dir, loaded.kept)), b'{"v": 3', "the damaged bytes survive")
        self.assertEqual(dk_fs.load_json(self.path).kept, "")

    def test_messages_name_the_file_never_the_folders_above_it(self):
        os.mkdir(self.path)  # a folder where the file should be
        calls = [lambda: dk_fs.write_json(self.path, {})]
        if dk_fs.POSIX:  # Windows cannot open a folder at all (PermissionError, not ours to word)
            calls.append(lambda: dk_fs.read_bytes(self.path))
        for call in calls:
            with self.assertRaises(dk_fs.UnsafePath) as caught:
                call()
            self.assertNotIn(self.dir, str(caught.exception))
            self.assertIn("settings.json", str(caught.exception))
        os.rmdir(self.path)
        self.damage()
        self.assertNotIn(self.dir, dk_fs.load_json(self.path).detail)

    def test_content_validate_refuses_starts_a_recovery(self):
        dk_fs.write_json(self.path, {"v": 1}, validate=dk_fs.require_object)
        self.damage(b"[1, 2]")
        loaded = dk_fs.load_json(self.path, validate=dk_fs.require_object)
        self.assertEqual((loaded.value, loaded.status), ({"v": 1}, "recovered"))
        with self.assertRaises(ValueError):
            dk_fs.write_json(self.path, [1], validate=dk_fs.require_object)

    def test_values_its_own_reader_would_refuse_are_never_written(self):
        dk_fs.write_json(self.path, {"v": 1})
        before = sorted(os.listdir(self.dir))
        deep = []
        for _ in range(70):
            deep = [deep]
        for value in ({"v": float("nan")}, {"v": "\ud800"}, {"v": "x" * 2048}, deep, {1: "a", "1": "b"}):
            with self.assertRaises(ValueError, msg=str(value)[:30]):
                dk_fs.write_json(self.path, value, limit=1024)
        self.assertEqual(sorted(os.listdir(self.dir)), before)
        self.assertEqual(dk_fs.read_json(self.path), {"v": 1})

    @unittest.skipUnless(POSIX, "file modes are POSIX")
    def test_json_stores_are_private_and_recovery_keeps_the_mode(self):
        dk_fs.write_json(self.path, {"v": 1})
        for path in (self.path, self.path + dk_fs.LAST_GOOD):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)
        self.damage()
        os.chmod(self.path, 0o640)
        dk_fs.load_json(self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_removed_json_does_not_come_back_from_its_copy(self):
        dk_fs.write_json(self.path, {"v": 1})
        dk_fs.remove_json(self.path)
        dk_fs.remove_json(self.path)  # already gone: no error
        self.assertEqual(dk_fs.load_json(self.path).status, "missing")
        self.assertEqual(os.listdir(self.dir), [])
        dk_fs.write_json(self.path, {"v": 1})
        with mock.patch.object(dk_fs.os, "remove") as remove:
            dk_fs.remove_json(self.path)
        # The copy goes first: a remove cut short in between leaves the file, never a copy that would come back.
        self.assertEqual([c.args[0] for c in remove.call_args_list], [self.path + dk_fs.LAST_GOOD, self.path])

    def test_written_json_is_readable_back(self):
        value = {"name": "Ärger", "list": [1, 2.5, True, None]}
        dk_fs.write_json(self.path, value)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), value)


    def test_a_missing_file_beside_a_damaged_copy_is_lost_not_a_first_install(self):
        write(self.path + dk_fs.LAST_GOOD, b"null")
        loaded = dk_fs.load_json(self.path, {"d": 1}, validate=dk_fs.require_object)
        self.assertEqual((loaded.value, loaded.status), ({"d": 1}, "lost"))
        self.assertIn("last-good copy", loaded.detail)
        self.damage(b"")  # Decky's empty file changes nothing
        self.assertEqual(dk_fs.load_json(self.path, validate=dk_fs.require_object).status, "lost")
        self.assertEqual(read(self.path + dk_fs.LAST_GOOD), b"null", "the damaged copy stays for inspection")

    @unittest.skipUnless(POSIX, "file modes are POSIX")
    def test_copies_a_read_makes_are_as_private_as_the_store(self):
        self.damage(b'{"v": 1}')
        os.chmod(self.path, 0o600)
        dk_fs.load_json(self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path + dk_fs.LAST_GOOD).st_mode), 0o600, "first-read copy")
        os.remove(self.path)
        dk_fs.load_json(self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600, "file restored from its copy")
        os.remove(self.path)
        os.remove(self.path + dk_fs.LAST_GOOD)
        self.damage(b'{"v": 2}')
        os.chmod(self.path, 0o644)
        dk_fs.load_json(self.path, mode=0o640)
        self.assertEqual(stat.S_IMODE(os.stat(self.path + dk_fs.LAST_GOOD).st_mode), 0o640, "mode wins")

    def test_an_unusable_last_good_copy_is_an_error_not_a_copy_left_stale(self):
        dk_fs.write_json(self.path, {"v": 1})
        copy = self.path + dk_fs.LAST_GOOD
        os.remove(copy)
        os.mkdir(copy)  # anything but a regular file
        for call in (lambda: dk_fs.load_json(self.path), lambda: dk_fs.write_json(self.path, {"v": 2})):
            with self.assertRaises(dk_fs.UnsafePath):
                call()
        self.assertEqual(json.loads(read(self.path)), {"v": 1}, "refused before any write")

    def test_a_symlinked_last_good_copy_is_refused_as_root(self):
        if not can_symlink(self.dir):
            self.skipTest("no symlinks on this host")
        dk_fs.write_json(self.path, {"v": 1})
        copy, target = self.path + dk_fs.LAST_GOOD, os.path.join(self.dir, "elsewhere")
        os.remove(copy)
        write(target, b"keep")
        os.symlink(target, copy)
        for call in (lambda: dk_fs.load_json(self.path), lambda: dk_fs.write_json(self.path, {"v": 2})):
            with as_root(), self.assertRaises(dk_fs.UnsafePath):
                call()
        self.assertEqual(read(target), b"keep")

    def test_write_json_tells_whether_the_copy_was_refreshed(self):
        self.assertTrue(dk_fs.write_json(self.path, {"v": 1}))
        real = dk_fs.write_atomic

        def copy_fails(path, *args, **kwargs):
            if path.endswith(dk_fs.LAST_GOOD):
                raise OSError("disk full")
            return real(path, *args, **kwargs)

        with mock.patch.object(dk_fs, "write_atomic", side_effect=copy_fails):
            self.assertFalse(dk_fs.write_json(self.path, {"v": 2}))
        self.assertEqual(json.loads(read(self.path)), {"v": 2})

    def test_an_empty_file_can_be_lost_intent_instead_of_a_first_install(self):
        self.damage(b"")
        loaded = dk_fs.load_json(self.path, {"d": 1}, empty_is_missing=False)
        self.assertEqual((loaded.value, loaded.status), ({"d": 1}, "lost"))
        self.assertEqual(read(self.path), b"", "the empty file stays")
        dk_fs.write_json(self.path, {"v": 1})
        self.damage(b"  \n")
        loaded = dk_fs.load_json(self.path, empty_is_missing=False)
        self.assertEqual((loaded.value, loaded.status), ({"v": 1}, "recovered"))
        self.assertEqual(json.loads(read(self.path)), {"v": 1})

    @unittest.skipUnless(POSIX, "file modes are POSIX")
    def test_mode_makes_an_existing_last_good_copy_private_too(self):
        dk_fs.write_json(self.path, {"v": 1}, mode=0o644)
        self.assertEqual(dk_fs.load_json(self.path, mode=0o600).status, "ok")
        for path in (self.path, self.path + dk_fs.LAST_GOOD):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)

    @unittest.skipUnless(POSIX, "file modes and FIFOs are POSIX")
    def test_mode_makes_an_existing_store_private_and_never_blocks_on_a_fifo(self):
        self.damage(b'{"v": 1}')
        os.chmod(self.path, 0o644)
        self.assertEqual(dk_fs.load_json(self.path, mode=0o600).status, "ok")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        os.remove(self.path)
        os.mkfifo(self.path, 0o644)
        with self.assertRaises(dk_fs.UnsafePath):
            dk_fs.load_json(self.path, mode=0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644, "a FIFO is never changed")


# A writer killed at a step (os._exit: no cleanup, like a crash or SIGKILL); the next read must find a whole state.
_KILLED = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
import dk_fs
path, checkpoint, operation = sys.argv[2], sys.argv[3], sys.argv[4]
replace = os.replace
def hit(when, name):  # "before:s.json", or a prefix ending in * for a name with a random part
    want = checkpoint[len(when):]
    return checkpoint.startswith(when) and (name == want or want.endswith("*") and name.startswith(want[:-1]))
def killing_replace(src, dst, **kw):
    name = os.path.basename(dst)
    if hit("before:", name):
        os._exit(9)
    replace(src, dst, **kw)
    if hit("after:", name):
        os._exit(9)
dk_fs.os.replace = killing_replace
if checkpoint == "half":
    def half(data, f):
        f.write(data[:len(data) // 2]); f.flush(); os.fsync(f.fileno()); os._exit(9)
    dk_fs._copy = half
if operation == "read":
    dk_fs.load_json(path)
else:
    dk_fs.write_json(path, json.loads(operation))
"""


class KilledWriterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "s.json")

    def kill(self, checkpoint, operation):
        import subprocess
        import sys
        done = subprocess.run([sys.executable, "-I", "-c", _KILLED, os.path.dirname(dk_fs.__file__), self.path,
                               checkpoint, operation], capture_output=True, timeout=30)
        self.assertEqual(done.returncode, 9, f"the writer never reached {checkpoint}: {done.stderr[-300:]!r}")

    def test_a_write_killed_at_any_step_leaves_the_old_or_the_new_state(self):
        for checkpoint, expected in (("half", 1), ("before:s.json", 1), ("after:s.json", 2),
                                     ("before:s.json.last-good", 2), ("after:s.json.last-good", 2)):
            with self.subTest(checkpoint=checkpoint):
                dk_fs.write_json(self.path, {"v": 1})
                self.kill(checkpoint, '{"v": 2}')
                loaded = dk_fs.load_json(self.path)
                self.assertEqual((loaded.value, loaded.status), ({"v": expected}, "ok"))
                self.assertEqual(dk_fs.read_json(self.path), {"v": expected}, "and it stays")

    def test_a_recovery_killed_at_any_step_is_finished_by_the_next_read(self):
        for checkpoint in ("before:s.json.corrupt-*", "after:s.json.corrupt-*", "before:s.json"):
            with self.subTest(checkpoint=checkpoint):
                dk_fs.write_json(self.path, {"v": 1})
                write(self.path, b'{"v": 3 ' + checkpoint.encode())  # new bytes: an existing copy is not reused
                self.kill(checkpoint, "read")
                loaded = dk_fs.load_json(self.path)
                self.assertEqual((loaded.value, loaded.status), ({"v": 1}, "recovered"))
                self.assertEqual(dk_fs.load_json(self.path).status, "ok")


class CommitTest(unittest.TestCase):
    def test_only_the_rename_runs_in_commit_and_it_can_cancel_the_write(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "f.bin")
        dk_fs.write_atomic(path, b"old")
        seen = []

        @contextlib.contextmanager
        def commit():
            seen.append(("enter", dk_fs.read_bytes(path), sorted(os.listdir(folder))))
            yield
            seen.append(("exit", dk_fs.read_bytes(path)))

        dk_fs.write_atomic(path, b"new", commit=commit)
        self.assertEqual(seen[0][:2], ("enter", b"old"))
        self.assertEqual(len(seen[0][2]), 2, "the new bytes are already written beside it when commit starts")
        self.assertEqual(seen[1], ("exit", b"new"))

        @contextlib.contextmanager
        def refuse():
            raise InterruptedError("stopping")
            yield  # pragma: no cover

        with self.assertRaises(InterruptedError):
            dk_fs.write_atomic(path, b"never", commit=refuse)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"new")
        self.assertEqual(os.listdir(folder), ["f.bin"], "a cancelled write leaves no temp file")


if __name__ == "__main__":
    unittest.main()

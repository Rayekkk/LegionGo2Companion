# SPDX-License-Identifier: BSD-3-Clause
# Shared from DeckyKit (shared/backend/dk_fs.py): change it there, then `kit sync`.

"""Files that must never be left half-written, read without trusting what is on disk.

Sources: LegionGo2Companion/safe_settings.py and Ayaneo3Companion/safe_settings.py (symlink refusal, dir_fd,
O_NOFOLLOW, owner, quarantine, backup of the current content, backup on first read, object root, limits, duplicate
keys, open_directory, private copies and a store made private on read, lost intent when only a damaged copy is
left, writes killed at every step; Legion: a symlinked folder refused for reads too, an unsafe last-good copy
reported instead of silently never refreshed, an empty file that is lost intent, a private existing copy), DeckyVibranceHDR/vibrance_settings.py (kept file mode, orphaned temp files, folder fsync
never fails a write, symlinked settings of a user plugin, Decky's empty first file), DeckyEQ/deckyeq/storage.py
(last-good fallback, validation hook, private folder), DeckyLS (first version), DeckyReshade (sha256_file for
files too big to hold whole; writing into a folder the caller opened, for the shared updater's walk to Downloads).

Guarantees:
- A write is atomic: temp file in the same folder (O_EXCL, never through a symlink), fsync, rename, fsync of the
  folder. A crash leaves the old file or the new one, never a mix. A failed write removes its temp file.
  The content may be bytes, a file object or a function writing into the open temp file, which runs only once the
  file exists (a folder that cannot be written fails before a costly build) and cancels the write by raising.
- Symlinks: with no_links (default: when running as root) a symlinked file or a symlinked last folder component
  is refused (UnsafePath) for writes and reads alike, on POSIX through O_NOFOLLOW on the opened folder, so it cannot
  be swapped in between check and use; links higher up are the system's choice. A user plugin (not root) reads and writes through a
  symlinked file (the link stays, its target is replaced atomically), as a dotfile manager expects.
- Anything but a regular file (FIFO, folder, device) is never written over or read; a read never blocks on a FIFO.
- On POSIX the folder is opened once and every step works relative to it; with folder_fd the caller's already
  vetted folder is used as it is (path is then a bare file name).
- A replaced file keeps its mode and owner (mode=None); a new file gets the folder's owner, so a root plugin
  writes user files as the user; owner=(uid, gid) sets the new file's owner whatever was there (a root-owned file
  of the same name in the user's Downloads does not stay root's). Folders are created with the umask (folder_mode to override; JSON stores 0o700).
- Once the new file is in place, a failed folder fsync is not a failed write (strict_sync=True makes it one);
  write_atomic answers False then (True otherwise), so a caller can log it (Legion's settings diagnostics).
- Temp files of a write killed mid-way (older than a minute) are removed by the next write of that file.
- Reads take at most `limit` bytes. JSON is read strictly: UTF-8, no duplicate keys, no NaN/Infinity, bounded
  nesting, integers within Python's digit limit; `validate` may refuse content that parses. A JSON write refuses
  anything its own reader would refuse, before touching any file.
- JSON stores keep <file>.last-good: a copy of the content just written (made on the first read too). Only
  damaged content (Damaged) starts a recovery, never an I/O error such as EACCES or EIO (raised as OSError, the
  files untouched). Recovery saves the damaged bytes to <file>.corrupt-<time> (never deleted) and then replaces the
  file with the good copy in one atomic step; a file missing while a good copy exists is restored the same way.
  With no good copy the damaged file stays in place and load_json reports it as lost, so a restart cannot
  mistake it for a first install; its bytes are saved as <file>.corrupt-<time> all the same (once per content),
  since the caller's next write replaces it. Loaded.kept names that copy. An empty file (Decky's first-install
  file) reads as missing; a missing (or empty) file beside a damaged copy is lost, not missing. With
  empty_is_missing=False (a store the plugin alone writes, e.g. a journal of hardware it owns) an empty file is
  damaged: restored from a good copy, else lost.
- A last-good copy that is there but unusable as a file (a refused symlink, a FIFO, a folder) is an error
  (UnsafePath from load_json and write_json, before any write), never a copy silently left stale.
  write_json answers whether the copy was refreshed (False: the change is on disk, its copy failed).
- Files load_json makes itself (the first-read copy, a restored file) get `mode`, by default the mode of the file
  they copy, so a private store never gains a readable copy. With `mode` given, an existing file and its existing
  last-good copy with another mode are set to it through their open descriptors (an old store made private on its
  first read; a FIFO never blocks).
- Error messages (UnsafePath, Damaged, Loaded.detail) name a file or folder, never the folders above it.
- load_json/read_json are for the plugin's own stores (they write .last-good and may repair the file); a file
  another program owns is read with parse_json(read_bytes(path)), which never writes.
- sha256_file reads in blocks, so a large DLL or archive is never held in memory whole.
- write_atomic(commit=...) runs only the rename inside the caller's context manager (DeckyEQ updates.py), so a
  caller that must stop publishing at once (the updater's close() on unload) never waits for a slow write or fsync.
- An OSError the system raises (EACCES, ENOSPC, EIO) is passed on as it is and may name the full path: a caller
  that shows errors to users turns it into text without it with shown(e) (strerror, else the type's name;
  UnsafePath keeps its own text) and logs the full error (DeckyLS, DeckyEQ, DeckyVibranceHDR did it locally).

Deliberately not here: locking (callers serialise writes to one file), schemas (pass `validate`), Decky's
SettingsManager API (Ayaneo3Companion's safe_settings.py is a thin layer on top, Legion's AtomicSettingsManager
will be), logging (no `decky`).
"""

import contextlib
import hashlib
import json
import os
import secrets
import stat
import time
from collections import namedtuple

LAST_GOOD = ".last-good"
MAX_JSON_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 64
ORPHAN_AGE = 60  # seconds; a live write finishes long before
CHUNK = 1 << 20
POSIX = os.name == "posix"

# status: "ok" | "missing" | "recovered" | "lost"; kept: name of the .corrupt-* copy of damaged content, or ""
Loaded = namedtuple("Loaded", "value status detail kept", defaults=("",))


class UnsafePath(OSError):
    """The path is a refused symlink or not a regular file (folder) where one is required. The message names the
    file or folder only, never the folders above it (they hold the user's name)."""


def _name(path):
    return os.path.basename(os.path.normpath(path)) or path


class Damaged(ValueError):
    """A file exists but its content cannot be used."""


def shown(error):
    """error's text for users. An OSError the system raises names its full path (with the user's name): only its
    description (strerror, else its type's name); UnsafePath's own text names no folder above the file, so it stays.
    Anything else is str(error). The caller logs the full error."""
    if isinstance(error, OSError) and not isinstance(error, UnsafePath):
        return error.strerror or type(error).__name__
    return str(error)


def _is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _strict(no_links):
    return _is_root() if no_links is None else no_links


def _target(path, no_links):
    """(folder, leaf, strict) for a write or read of path; a user plugin's symlinked file is followed here."""
    strict = _strict(no_links)
    path = os.path.abspath(path)
    if not strict and os.path.islink(path):
        path = os.path.realpath(path)
    folder, leaf = os.path.dirname(path), os.path.basename(path)
    if not leaf or leaf in (".", "..") or "\x00" in leaf:
        raise ValueError(f"not a file path: {path!r}")
    return folder, leaf, strict


def _make_folder(folder, strict, folder_mode):
    if not os.path.lexists(folder):
        if folder_mode is None:
            os.makedirs(folder, exist_ok=True)
        else:
            os.makedirs(folder, mode=folder_mode, exist_ok=True)
            os.chmod(folder, folder_mode)  # makedirs applies the umask
    info = os.lstat(folder)
    if stat.S_ISLNK(info.st_mode) and strict:
        raise UnsafePath(f"{_name(folder)} is a symbolic link")
    if not stat.S_ISDIR(os.stat(folder).st_mode):
        raise UnsafePath(f"{_name(folder)} is not a folder")


def open_directory(parts, fd=None, create=None, check=None):
    """Directory fd at `parts` below directory `fd` (default /; taken over), one component at a time, never through
    a symlink (POSIX). `create(part)` gives (mode, uid, gid) for a missing component; `check(fd)` vets each."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags) if fd is None else fd
    try:
        for part in parts:
            owner = None
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if create is None:
                    raise
                mode, *owner = create(part)
                os.mkdir(part, mode, dir_fd=fd)
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            if owner:
                os.fchown(fd, *owner)
            if check:
                check(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _copy(data, f):
    if isinstance(data, (bytes, bytearray, memoryview)):
        f.write(data)
    elif callable(data):  # writes the content itself (built while the file is open: DeckyVibranceHDR's LUT)
        data(f)
    else:  # a binary file object: copied in chunks, never held in memory whole
        while block := data.read(CHUNK):
            f.write(block)
    f.flush()
    os.fsync(f.fileno())


def _orphans(names, leaf):
    now = time.time()
    return [name for name, mtime in names
            if name.startswith(f".{leaf}.") and name.endswith(".tmp") and now - mtime > ORPHAN_AGE]


def sha256_file(path):
    """The SHA-256 of a file's content, read in blocks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def write_atomic(path, data, mode=None, *, no_links=None, strict_sync=False, folder_mode=None, folder_fd=None,
                 commit=None, owner=None):
    """Replace path with data: bytes, a binary file object read to its end, or a function data(file) that writes
    the content into the open temp file (binary; an exception it raises cancels the write). mode=None keeps the
    mode of the file being replaced (0o644 for a new one); no_links=None refuses symlinks only when running as root. folder_fd: an
    open folder (POSIX) to write path, a bare file name, in; the fd stays the caller's. commit: a callable giving a
    context manager the rename alone runs in (it may raise to cancel the write). owner: (uid, gid) of the new
    file (POSIX), instead of the replaced file's or the folder's."""
    commit = commit or contextlib.nullcontext
    if folder_fd is not None:
        if not POSIX or not path or path in (".", "..") or "/" in path or "\x00" in path:
            raise ValueError(f"not a file name for folder_fd: {path!r}")
        folder, leaf, strict = f"<folder fd {folder_fd}>", path, True
    else:
        folder, leaf, strict = _target(path, no_links)
        _make_folder(folder, strict, folder_mode)
    tmp = f".{leaf}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if not POSIX:  # Windows is a development host only: no dir_fd, but the same atomic rename
        target = os.path.join(folder, leaf)
        try:
            existing = os.lstat(target)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise UnsafePath(f"{leaf} is not a regular file")
        tmp_path = os.path.join(folder, tmp)
        try:
            with os.fdopen(os.open(tmp_path, flags, 0o600), "wb") as f:
                _copy(data, f)
            os.chmod(tmp_path, stat.S_IMODE(existing.st_mode) if existing and mode is None else mode or 0o644)
            with commit():
                os.replace(tmp_path, target)
        finally:
            if os.path.lexists(tmp_path):
                os.remove(tmp_path)
        return True

    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | (os.O_NOFOLLOW if strict else 0)
    try:
        dir_fd = os.dup(folder_fd) if folder_fd is not None else os.open(folder, dir_flags)
    except OSError as e:
        if strict and os.path.islink(folder):
            raise UnsafePath(f"{_name(folder)} is a symbolic link") from e
        raise
    tmp_fd = -1
    try:
        try:
            existing = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise UnsafePath(f"{leaf} is not a regular file")
        names = []
        for name in os.listdir(dir_fd):
            if name.startswith(f".{leaf}.") and name.endswith(".tmp"):
                try:
                    names.append((name, os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mtime))
                except OSError:
                    continue
        for name in _orphans(names, leaf):
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                pass  # someone else's file or already gone: never worth failing a write for
        uid, gid = owner or (lambda info: (info.st_uid, info.st_gid))(existing or os.fstat(dir_fd))
        tmp_fd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
        os.fchmod(tmp_fd, stat.S_IMODE(existing.st_mode) if existing and mode is None else mode or 0o644)
        if (uid, gid) != (os.geteuid(), os.getegid()):
            try:
                os.fchown(tmp_fd, uid, gid)
            except PermissionError:
                pass  # only root can give a file away; a user process writes its own files anyway
        with os.fdopen(tmp_fd, "wb") as f:
            tmp_fd = -1
            _copy(data, f)
        with commit():
            os.replace(tmp, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        try:
            os.fsync(dir_fd)
        except OSError:
            if strict_sync:
                raise
            # The new file is visible and its bytes are on disk: reporting a failure now would make the caller
            # roll back its state against the file that is actually there.
            return False
        return True
    finally:
        if tmp_fd >= 0:
            os.close(tmp_fd)
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        os.close(dir_fd)


def read_bytes(path, limit=MAX_JSON_BYTES, *, no_links=None):
    """The content of a regular file, at most limit bytes (Damaged if larger). Never waits on a FIFO; with no_links
    (default: as root) never follows a symlink. FileNotFoundError when there is no file."""
    folder, leaf, strict = _target(path, no_links)
    full = os.path.join(folder, leaf)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    dir_fd = None
    if strict:
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if not POSIX:  # no O_NOFOLLOW on Windows
            if os.path.islink(folder):
                raise UnsafePath(f"{_name(folder)} is a symbolic link")
            if stat.S_ISLNK(os.lstat(full).st_mode):
                raise UnsafePath(f"{leaf} is a symbolic link")
        else:
            try:
                dir_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            except OSError as e:
                if not isinstance(e, FileNotFoundError) and os.path.islink(folder):
                    raise UnsafePath(f"{_name(folder)} is a symbolic link") from e
                raise
    try:
        fd = os.open(leaf, flags, dir_fd=dir_fd) if dir_fd is not None else os.open(full, flags)
    except OSError as e:
        if strict and not isinstance(e, FileNotFoundError) and os.path.islink(full):  # O_NOFOLLOW: ELOOP
            raise UnsafePath(f"{leaf} is a symbolic link") from e
        raise
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
    try:  # before fdopen, which refuses a folder with its own error
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except BaseException:
        os.close(fd)
        raise
    if not regular:
        os.close(fd)
        raise UnsafePath(f"{leaf} is not a regular file")
    with os.fdopen(fd, "rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise Damaged(f"{leaf} is larger than {limit} bytes")
    return data


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Damaged(f"duplicate key {key!r}")
        result[key] = value
    return result


def _no_constant(name):
    raise Damaged(f"{name} is not valid JSON")


def _check_depth(value):
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        children = item.values() if isinstance(item, dict) else item if isinstance(item, (list, tuple)) else ()
        for child in children:
            if isinstance(child, (dict, list, tuple)):
                if depth >= MAX_JSON_DEPTH:
                    raise Damaged(f"nested deeper than {MAX_JSON_DEPTH} levels")
                stack.append((child, depth + 1))


def parse_json(data, validate=None):
    """Strict JSON from bytes: UTF-8 (BOM allowed), no duplicate keys, no NaN/Infinity, bounded nesting; then
    validate(value), whose ValueError or TypeError also means Damaged."""
    try:
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
        _check_depth(value)
        if validate is not None:
            validate(value)
    except Damaged:
        raise
    except (UnicodeError, RecursionError, ValueError, TypeError) as e:  # ValueError: also over-long integers
        raise Damaged(str(e)) from e
    return value


def require_object(value):
    """A validate for stores whose root must be a JSON object."""
    if not isinstance(value, dict):
        raise Damaged("the root is not a JSON object")


def _mode_of(path):
    try:
        return stat.S_IMODE(os.lstat(path).st_mode)
    except OSError:
        return None  # gone meanwhile: the copy gets write_atomic's default


def _harden(path, mode, no_links):
    """Set an existing regular file to mode through its descriptor (POSIX; never through a refused link, never
    blocking on a FIFO)."""
    if not POSIX:
        return
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | (os.O_NOFOLLOW if _strict(no_links) else 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) != mode:
            os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _copy_present(copy, no_links):
    """Whether the last-good copy exists; UnsafePath when it exists but cannot be used as a file."""
    try:
        info = os.lstat(copy)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        if _strict(no_links):
            raise UnsafePath(f"{_name(copy)} is a symbolic link")
        try:
            info = os.stat(copy)  # a user plugin's link is followed, as for the file itself
        except FileNotFoundError:
            return False  # a dangling link: the next write creates its target
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePath(f"{_name(copy)} is not a regular file")
    return True


def _good(path, limit, validate, no_links):
    """(raw, value) of a good file, None when it is missing; Damaged/OSError as they come."""
    try:
        raw = read_bytes(path, limit, no_links=no_links)
    except FileNotFoundError:
        return None
    return raw, parse_json(raw, validate)


def _stamp():
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(3)}"


def load_json(path, default=None, limit=MAX_JSON_BYTES, *, validate=None, no_links=None, mode=None,
              empty_is_missing=True):
    """Loaded(value, status, detail).
    "ok": the file. "missing": no file (or Decky's empty one) and no copy, value is default. "recovered": the
    file was damaged or missing and the last-good copy was put back. "lost": the file is damaged and there is no
    good copy, or it is missing and its copy is damaged; it stays where it is and value is default. OSError (EACCES,
    EIO, UnsafePath...) is raised and nothing is touched: the problem may pass, the content may be fine.
    mode: of the files this read makes, and of an existing file (see the module's guarantees).
    empty_is_missing=False: an empty file is damaged (restored from the copy, else lost), not a first install."""
    copy = path + LAST_GOOD
    try:
        raw = read_bytes(path, limit, no_links=no_links)
        problem = None
    except FileNotFoundError:
        raw, problem = None, "missing"
    except Damaged as e:
        raw, problem = None, f"{os.path.basename(path)}: {e}"
    if raw is not None and raw.strip():
        try:
            value = parse_json(raw, validate)
        except Damaged as e:
            problem = f"{os.path.basename(path)}: {e}"
        else:
            present = _copy_present(copy, no_links)
            if mode is not None:
                for target in (path, copy) if present else (path,):
                    try:
                        _harden(target, mode, no_links)
                    except OSError:
                        pass  # the content is fine; the next read tries again
            if not present:
                try:  # an inherited file gets its good copy at once, not only after the first change
                    write_atomic(copy, raw, _mode_of(path) if mode is None else mode, no_links=no_links)
                except OSError:
                    pass  # the file itself is fine; the next write tries again
            return Loaded(value, "ok", "")
    elif raw is not None and empty_is_missing:
        problem = "missing"  # empty: nothing to protect (Decky creates it, an interrupted first save leaves it)
    elif raw is not None:
        problem = f"{os.path.basename(path)}: the file is empty"
    try:
        good = _good(copy, limit, validate, no_links)
    except Damaged as e:
        good, copy_problem, copy_damaged = None, f"last-good copy: {e}", True
    else:
        copy_problem, copy_damaged = "" if good else "no last-good copy", False
    if good is None and problem == "missing" and not copy_damaged:
        return Loaded(default, "missing", "")
    if good is None and problem == "missing":
        return Loaded(default, "lost", f"the file was missing; {copy_problem}")  # the damaged copy stays as it is
    # The damaged bytes are saved before anything else: the caller's next write replaces the file.
    kept = _keep_damaged(path, raw, no_links) if raw is not None and raw.strip() else ""
    note = f" (kept as {kept})" if kept else ""
    if good is None:
        return Loaded(default, "lost", f"{problem}; {copy_problem}{note}", kept)
    detail = ("the file was missing" if problem == "missing" else problem) + note
    restored = mode if mode is not None else None if os.path.lexists(path) else _mode_of(copy)
    write_atomic(path, good[0], restored, no_links=no_links)  # one atomic step: never a moment without the file
    return Loaded(good[1], "recovered", detail, kept)


def _keep_damaged(path, raw, no_links):
    """Name of a <file>.corrupt-<time> copy holding raw: an existing one with the same bytes, else a new one."""
    folder, leaf = os.path.split(path)
    for name in sorted(os.listdir(folder or ".")):
        if name.startswith(f"{leaf}.corrupt-"):
            try:
                if read_bytes(os.path.join(folder, name), len(raw), no_links=no_links) == raw:
                    return name  # a restart reads the same damaged file again: one copy is enough
            except (OSError, Damaged):
                continue
    aside = f"{path}.corrupt-{_stamp()}"
    write_atomic(aside, raw, 0o600, no_links=no_links)
    return os.path.basename(aside)


def read_json(path, default=None, limit=MAX_JSON_BYTES, **options):
    """load_json(...).value: the file, its last-good copy, or default."""
    return load_json(path, default, limit, **options).value


def write_json(path, value, mode=0o600, *, limit=MAX_JSON_BYTES, validate=None, no_links=None, folder_mode=0o700):
    """Write value as JSON (private by default), then its last-good copy. Refuses anything load_json would refuse.
    True when the copy was refreshed too; UnsafePath, before any write, when the copy cannot be used as a file."""
    _check_depth(value)  # before dumps, which would overflow the stack on absurd nesting
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=1) + "\n").encode("utf-8")
    if len(data) > limit:
        raise ValueError(f"{os.path.basename(path)} would be larger than {limit} bytes")
    parse_json(data, validate)  # e.g. {1: .., "1": ..} becomes a duplicate key: never write what cannot be read
    _copy_present(path + LAST_GOOD, no_links)
    write_atomic(path, data, mode, no_links=no_links, folder_mode=folder_mode)
    try:
        write_atomic(path + LAST_GOOD, data, mode, no_links=no_links)
    except OSError:
        return False  # the change itself is safely on disk; the copy is refreshed by the next read or write
    return True


def remove_json(path):
    """Delete the file and its last-good copy (the copy first: a file missing beside a good copy means an
    interrupted write or recovery, which load_json repairs). Moved-aside damaged copies stay."""
    for candidate in (path + LAST_GOOD, path):
        try:
            os.remove(candidate)
        except FileNotFoundError:
            pass

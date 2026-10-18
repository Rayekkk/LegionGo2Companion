# SPDX-License-Identifier: BSD-3-Clause
"""Write or remove a gamescope display script as the session user.

display_backend runs this with the user's uid/gid and no supplementary groups,
so the kernel applies that user's permissions to their whole config tree and a
link planted there cannot redirect a root write. Standard library only: it runs
under the system interpreter, outside Decky.

Usage: display_script_io.py write|unlink <config dir> <file name>
The bytes to write come on stdin. Exit 0 on success, 2 when there is nothing
to remove, 1 on any other failure.
"""

import os
import stat
import sys

LIMIT = 32 * 1024
SUBDIRS = ("gamescope", "scripts")
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _scripts_dir(base: str, create: bool) -> int:
    """Descriptor of <base>/gamescope/scripts; neither component may be a link."""
    if create:
        os.makedirs(base, mode=0o755, exist_ok=True)
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | O_CLOEXEC)
    try:
        for name in SUBDIRS:
            if create:
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def write(base: str, leaf: str, data: bytes) -> None:
    fd = _scripts_dir(base, True)
    temporary = f".{leaf}.{os.getpid()}.tmp"
    try:
        try:
            existing = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise OSError(f"{leaf} is not a regular file")
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | O_CLOEXEC,
                      0o644, dir_fd=fd)
        with os.fdopen(out, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)  # gamescope only reads it; ignore the umask
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, leaf, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)


def unlink(base: str, leaf: str) -> None:
    fd = _scripts_dir(base, False)
    try:
        os.unlink(leaf, dir_fd=fd)
    finally:
        os.close(fd)


def main(argv) -> int:
    if len(argv) != 4 or argv[1] not in ("write", "unlink"):
        return 1
    _, op, base, leaf = argv
    if (not os.path.isabs(base) or leaf in ("", ".", "..")
            or os.path.basename(leaf) != leaf or "\x00" in base + leaf):
        return 1
    try:
        if op == "write":
            data = sys.stdin.buffer.read(LIMIT + 1)
            if len(data) > LIMIT:
                return 1
            write(base, leaf, data)
        else:
            unlink(base, leaf)
    except FileNotFoundError:
        return 2 if op == "unlink" else 1
    except OSError:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

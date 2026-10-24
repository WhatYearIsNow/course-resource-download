"""Nonblocking kernel locks. Lock files are persistent and must never be unlinked."""

from __future__ import annotations

import errno
import os
import stat
import threading
import time
from dataclasses import dataclass

_held = {}
_guard = threading.Lock()


@dataclass
class LockHandle:
    path: str
    fd: int
    released: bool = False


def acquire_lock(directory):
    """Return an opaque handle, or None when busy. OS releases it after crashes."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.normcase(os.path.join(os.path.realpath(directory), ".download.lock"))
    with _guard:
        if path in _held:
            return None
        if os.path.islink(path):
            raise ValueError("Refusing a symlink lock file")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Lock path must be a regular file")
            # msvcrt locks an existing byte. All contenders write the same byte.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError) as exc:
            os.close(fd)
            if isinstance(exc, OSError) and exc.errno in {
                errno.EACCES,
                errno.EAGAIN,
                errno.EDEADLK,
            }:
                return None
            raise
        handle = LockHandle(path, fd)
        _held[path] = handle
        return handle


def release_lock(lock):
    """Release our handle idempotently, keeping the inode/filename in place."""
    if not isinstance(lock, LockHandle):
        return
    with _guard:
        if lock.released:
            return
        try:
            os.lseek(lock.fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fd, fcntl.LOCK_UN)
        finally:
            os.close(lock.fd)
            lock.released = True
            _held.pop(lock.path, None)


def cleanup_stale_lock_files(directory, older_than_sec=3600):
    """Compatibility no-op. Age/PID-based lock recovery has an unavoidable race."""
    return 0


def cleanup_stale_parts(directory, older_than_sec=3600):
    """Remove only this tool's old staging files while holding its directory lock."""
    if older_than_sec < 0:
        raise ValueError("older_than_sec must be nonnegative")
    path = os.path.normcase(os.path.join(os.path.realpath(directory), ".download.lock"))
    with _guard:
        held_here = path in _held
    handle = None if held_here else acquire_lock(directory)
    if not held_here and handle is None:
        return 0
    removed = 0
    try:
        with os.scandir(directory) as entries:
            for item in entries:
                if not item.name.startswith(
                    (".crd-download-", ".crd-manifest-")
                ) or not item.name.endswith(".part"):
                    continue
                try:
                    if (
                        item.is_file(follow_symlinks=False)
                        and time.time() - item.stat(follow_symlinks=False).st_mtime > older_than_sec
                    ):
                        os.unlink(item.path)
                        removed += 1
                except OSError:
                    pass
    finally:
        release_lock(handle)
    return removed

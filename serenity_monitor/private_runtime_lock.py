"""Cross-process lock covering one complete private daily prepare run."""
from __future__ import annotations

import contextlib
import errno
import os
from pathlib import Path
from typing import BinaryIO, Iterator

from .private_runtime_paths import tighten_private_file


class PrivateRuntimeBusy(RuntimeError):
    """Another process already owns the private-runtime prepare lock."""


class PrivateRuntimeLockError(RuntimeError):
    """The operating-system lock could not be established safely."""


def _lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise PrivateRuntimeBusy("private_runtime_busy") from exc
            raise PrivateRuntimeLockError("private_runtime_lock_failed") from exc
    else:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise PrivateRuntimeBusy("private_runtime_busy") from exc
            raise PrivateRuntimeLockError("private_runtime_lock_failed") from exc


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def private_runtime_lock(path: str | Path) -> Iterator[None]:
    """Hold a non-blocking OS lock until the whole prepare sequence finishes."""

    lock_path = Path(path)
    if not lock_path.parent.is_dir():
        raise PrivateRuntimeLockError("private_runtime_root_missing")
    if lock_path.exists():
        tighten_private_file(lock_path)
    flags = os.O_RDWR | os.O_CREAT | int(getattr(os, "O_BINARY", 0))
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
    except OSError as exc:
        raise PrivateRuntimeLockError("private_runtime_lock_open_failed") from exc
    acquired = False
    try:
        if os.fstat(handle.fileno()).st_nlink != 1:
            raise PrivateRuntimeLockError("private_runtime_lock_hardlink_forbidden")
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        tighten_private_file(lock_path)
        _lock(handle)
        acquired = True
        yield
    finally:
        if acquired:
            try:
                _unlock(handle)
            except OSError:
                pass
        handle.close()


__all__ = [
    "PrivateRuntimeBusy",
    "PrivateRuntimeLockError",
    "private_runtime_lock",
]

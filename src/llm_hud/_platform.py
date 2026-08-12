from __future__ import annotations

import errno
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def try_lock_descriptor(descriptor: int) -> None:
    """Acquire a non-blocking exclusive lock on a managed lock file."""
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(
                error, "winerror", None
            ) in (33, 36):
                raise BlockingIOError(error.errno, str(error)) from error
            raise
        return
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_descriptor(descriptor: int) -> None:
    """Release a lock acquired by :func:`try_lock_descriptor`."""
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(descriptor, fcntl.LOCK_UN)


def set_descriptor_mode(descriptor: int, mode: int) -> None:
    """Apply POSIX file permissions where the platform implements them."""
    if os.name != "nt":
        os.fchmod(descriptor, mode)

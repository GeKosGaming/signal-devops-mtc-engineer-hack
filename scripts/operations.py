#!/usr/bin/env python3
"""One process-shared Linux flock for SIGNAL operations and their nested commands.

An inherited descriptor is only a hint: validate its actual file identity and
acquire the kernel lock before accepting it. Never unlock an inherited open
file description; closing a duplicate preserves the parent's lock.
"""
from __future__ import annotations
import argparse
import contextlib
import os
import stat
import sys
from pathlib import Path
from typing import Iterator

if os.name == 'posix':
    import fcntl

ROOT = Path(__file__).resolve().parents[1]
LOCK_FD_ENV = 'SIGNAL_OPERATION_LOCK_FD'


class OperationLockError(RuntimeError):
    pass


def lock_path(root: Path | None = None) -> Path:
    return (ROOT if root is None else Path(root)).resolve() / '.state/workflow.lock'


def _flock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise OperationLockError('Another SIGNAL operation is running.') from None
    except OSError:
        raise OperationLockError('Cannot acquire the SIGNAL operation lock.') from None


def inherited_lock_fd(root: Path | None = None) -> int | None:
    """Return a verified, kernel-locked inherited FD, or None if no hint exists."""
    marker = os.environ.get(LOCK_FD_ENV)
    if marker is None:
        return None
    if os.name != 'posix':
        raise OperationLockError('SIGNAL operations require a POSIX flock.')
    if not marker.isascii() or not marker.isdecimal() or len(marker) > 9 or int(marker) < 3:
        raise OperationLockError('Invalid inherited SIGNAL lock descriptor.')
    fd = int(marker)
    try:
        opened = os.fstat(fd)
        expected = os.lstat(lock_path(root))
    except OSError:
        raise OperationLockError('Inherited SIGNAL lock descriptor is not open for this workspace.') from None
    if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(expected.st_mode)
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)):
        raise OperationLockError('Inherited SIGNAL lock descriptor belongs to another file.')
    _flock(fd)
    return fd


def lock_pass_fds(root: Path | None = None) -> tuple[int, ...]:
    """Pass the real lock to nested operations; do not pass it to port-forwards."""
    if os.name != 'posix':
        return ()
    fd = inherited_lock_fd(root)
    return () if fd is None else (fd,)


@contextlib.contextmanager
def operation_lock(root: Path | None = None) -> Iterator[int]:
    if os.name != 'posix':
        raise OperationLockError('SIGNAL operations require a POSIX flock.')
    path = lock_path(root)
    previous = os.environ.get(LOCK_FD_ENV)
    fd = None
    try:
        inherited = inherited_lock_fd(root)
        if inherited is not None:
            fd = os.dup(inherited)
        else:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
            try:
                fd = os.open(path, flags, 0o600)
            except OSError:
                raise OperationLockError('Cannot open the SIGNAL operation lock.') from None
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OperationLockError('SIGNAL operation lock must be a regular file.')
            _flock(fd)
        os.set_inheritable(fd, False)
        os.environ[LOCK_FD_ENV] = str(fd)
        yield fd
    finally:
        if previous is None:
            os.environ.pop(LOCK_FD_ENV, None)
        else:
            os.environ[LOCK_FD_ENV] = previous
        if fd is not None:
            # LOCK_UN would also release a parent's lock on this shared open file
            # description. The kernel releases it after its final FD is closed.
            os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validate-inherited', action='store_true', required=True)
    args = parser.parse_args()
    try:
        if args.validate_inherited and inherited_lock_fd() is None:
            raise OperationLockError('No inherited SIGNAL operation lock descriptor.')
    except OperationLockError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

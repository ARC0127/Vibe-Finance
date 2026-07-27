from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


if os.name == "nt":
    import msvcrt
else:
    import fcntl


def fsync_directory(path: Path) -> None:
    """Flush a directory entry where the platform exposes that operation."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def advisory_file_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
    """Hold an advisory process lock for the lifetime of the context."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            # Windows has no shared byte-range lock. Serializing readers with
            # writers is conservative and preserves ledger safety.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

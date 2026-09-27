"""Single-instance lock for the scheduler.

Two schedulers sharing one state database would double the museum traffic and
produce two independent run streams. Reservations stop a *double write*, but not
the waste or the confusing history, so the loop refuses to start when another
live process already holds the lock.

The lock is a PID file. A lock whose process no longer exists is stale and is
reclaimed automatically, so a hard kill cannot wedge the scheduler.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger()


class LockUnavailableError(RuntimeError):
    """Raised when another live process already holds the lock."""


@dataclass
class ProcessLock:
    """A PID file guard, released explicitly or on process exit."""

    path: Path
    _fd: int | None = None

    @property
    def held(self) -> bool:
        """Whether this instance currently holds the lock."""
        return self._fd is not None

    def acquire(self) -> None:
        """Take the lock, or raise if a live process holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = self._holder_pid()
            if holder is not None and _is_alive(holder):
                raise LockUnavailableError(
                    f"Another pinterest-art-bot scheduler is already running "
                    f"(pid {holder}, lock {self.path})."
                ) from None
            logger.warning("scheduler_lock_stale_reclaimed", pid=holder, lock=str(self.path))
            self._remove()
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)

        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        self._fd = os.open(self.path, os.O_RDONLY)
        logger.info("scheduler_lock_acquired", lock=str(self.path), pid=os.getpid())

    def release(self) -> None:
        """Release the lock if held."""
        if self._fd is None:
            return
        os.close(self._fd)
        self._fd = None
        self._remove()
        logger.info("scheduler_lock_released", lock=str(self.path))

    def _holder_pid(self) -> int | None:
        """PID recorded in the lock file, if readable."""
        try:
            return int(self.path.read_text().strip())
        except (OSError, ValueError):
            return None

    def _remove(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


def _is_alive(pid: int) -> bool:
    """Whether a process with this PID is currently running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Owned by another user: assume it is alive rather than stealing the lock.
        return True
    return True

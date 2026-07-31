"""One bot process per data directory, enforced with a lock file.

Four copies of the bot ended up long-polling the same token at once, because
each "restart" killed a shell pipeline and left the Python process behind.
Telegram hands an update to whichever poller asks first, so taps were answered
at random by processes running older code: a menu button came back "unknown
command", and a callback came back "this confirmation is no longer valid".

Beyond the confusion, concurrent pollers are wrong for a bot that can trade.
`getUpdates` only retires an update once a later offset is requested, so two
pollers genuinely can both receive the same one. Today a duplicate confirm is
absorbed by accident - the pending map lives in the process that built the
preview, so the other one has no token to act on - but that is a coincidence,
not a guarantee, and it is not a thing to leave load-bearing.

So: acquire an exclusive lock at startup and refuse to run without it.

The lock records the pid and is checked against the live process table, so a
crash does not leave the bot permanently unstartable - a stale lock whose pid
is gone is reclaimed, while a lock held by a running process is honoured.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from polymarket_bot.config import Settings

_LOCK_FILENAME = "telegram_bot.lock"


class AlreadyRunning(RuntimeError):
    """Another bot process holds the lock."""

    def __init__(self, pid: int, path: Path) -> None:
        self.pid = pid
        self.path = path
        super().__init__(
            f"Another Polymarket Telegram bot is already running (pid {pid}).\n"
            f"Two pollers on one token answer each other's messages at random.\n"
            f"Stop that process first, or delete {path} if you are certain it is gone."
        )


def _pid_alive(pid: int) -> bool:
    """Is this pid a live process?

    Cross-platform without psutil: signal 0 performs the permission/existence
    check without delivering anything. On Windows, os.kill(pid, 0) raises
    OSError for a dead pid and returns for a live one.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else. Treat as alive - refusing to start is
        # the safe answer when we cannot tell.
        return True
    except OSError:
        return False
    return True


class SingleInstance:
    """Context manager holding the run lock for one data directory."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.path = settings.data_dir / _LOCK_FILENAME
        self._acquired = False

    def _read_owner(self) -> int | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        pid = data.get("pid") if isinstance(data, dict) else None
        return int(pid) if isinstance(pid, int) else None

    def acquire(self) -> None:
        self._settings.ensure_data_dir()
        owner = self._read_owner()
        if owner is not None and owner != os.getpid() and _pid_alive(owner):
            raise AlreadyRunning(owner, self.path)
        # Either no lock, a lock we already own, or one whose process is gone.
        # An unreadable lock file counts as stale: a corrupt lock must not make
        # the bot permanently unstartable.
        self.path.write_text(
            json.dumps({"pid": os.getpid()}, indent=2), encoding="utf-8"
        )
        self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        # Only remove a lock we still own - another process may have reclaimed
        # it as stale while this one was shutting down.
        if self._read_owner() == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
        self._acquired = False

    def __enter__(self) -> SingleInstance:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

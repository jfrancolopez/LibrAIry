from __future__ import annotations

import fcntl
import time
from pathlib import Path
from types import TracebackType

from librairy.config import Settings

#  One sentence for "something else is using the files right now", said the
#  same way wherever it is reached. Commit already said this; Undo answered the
#  same condition with a 500 System Fault page, which is the worst possible
#  reply from the button you press when you want a move taken back.
BUSY = "LibrAIry is busy; retry when the worker releases the lock"


class LockHeldError(RuntimeError):
    pass


#  How long a person-initiated action waits before it gives up and says so.
#  The worker takes the lock for a whole cycle — scan, dedup, analyse — and on a
#  library with work in it that is most of the time: measured free on 40 of 120
#  samples over a minute. Refusing on the first attempt made Undo a button you
#  pressed three times. SQLite is already configured to wait five seconds for
#  the same kind of contention (`PRAGMA busy_timeout=5000`); this is that idea,
#  applied to the file lock.
#
#  Background work does not wait. A worker cycle that queued behind a commit
#  would run the moment it ended, on top of it.
WAIT_SECONDS = 5.0
_RETRY_SECONDS = 0.1


class LibrAIryLock:
    def __init__(self, appdata_dir: Path, wait: float = 0.0) -> None:
        self.lock_path = appdata_dir / "librairy.lock"
        self.wait = wait
        self._file = None

    def __enter__(self) -> LibrAIryLock:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.lock_path.open("a+")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    raise LockHeldError("another LibrAIry process holds the lock") from exc
                time.sleep(min(_RETRY_SECONDS, max(deadline - time.monotonic(), 0)))
            else:
                return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None


def acquire_lock(settings: Settings | None = None, *, wait: float = 0.0) -> LibrAIryLock:
    """`wait` is for actions a person is standing in front of. Default is none."""
    if settings is None:
        settings = Settings()
    return LibrAIryLock(settings.appdata_dir, wait=wait)

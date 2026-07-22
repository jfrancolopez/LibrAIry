from __future__ import annotations

import fcntl
from pathlib import Path
from types import TracebackType

from librairy.config import Settings


class LockHeldError(RuntimeError):
    pass


class LibrAIryLock:
    def __init__(self, appdata_dir: Path) -> None:
        self.lock_path = appdata_dir / "librairy.lock"
        self._file = None

    def __enter__(self) -> LibrAIryLock:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.lock_path.open("a+")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._file.close()
            self._file = None
            raise LockHeldError("another LibrAIry process holds the lock") from exc
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


def acquire_lock(settings: Settings | None = None) -> LibrAIryLock:
    if settings is None:
        settings = Settings()
    return LibrAIryLock(settings.appdata_dir)

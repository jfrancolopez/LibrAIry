from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.locks import LockHeldError, acquire_lock


def settings_for(tmp_path: Path) -> Settings:
    return Settings(APPDATA_DIR=tmp_path / "appdata", _env_file=None)


def holder_script(appdata_dir: Path) -> str:
    return f"""
import os
import signal
import time
from pathlib import Path
from librairy.config import Settings
from librairy.locks import acquire_lock
settings = Settings(APPDATA_DIR=Path({str(appdata_dir)!r}), _env_file=None)
with acquire_lock(settings):
    print('locked', flush=True)
    signal.pause() if hasattr(signal, 'pause') else time.sleep(30)
"""


def test_second_acquirer_in_another_process_fails_fast(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-c", holder_script(settings.appdata_dir)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with (
            pytest.raises(LockHeldError, match="another LibrAIry process holds the lock"),
            acquire_lock(settings),
        ):
            pass
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_lock_released_on_normal_exit_and_exception(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with acquire_lock(settings):
        pass
    with acquire_lock(settings):
        pass

    with pytest.raises(RuntimeError, match="boom"), acquire_lock(settings):
        raise RuntimeError("boom")
    with acquire_lock(settings):
        pass


def test_lock_released_after_sigkill_of_holder(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-c", holder_script(settings.appdata_dir)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        deadline = time.time() + 5
        while True:
            try:
                with acquire_lock(settings):
                    return
            except LockHeldError:
                if time.time() > deadline:
                    raise
                time.sleep(0.05)
    finally:
        if process.poll() is None:
            process.kill()

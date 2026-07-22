from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.tools.common import ToolResult, posix_path


@dataclass(frozen=True)
class AudioFingerprint:
    duration: int | None
    fingerprint: str


def fingerprint(path: Path, settings: Settings) -> ToolResult:
    if shutil.which("fpcalc") is None:
        return ToolResult(False, error="missing binary: fpcalc")
    try:
        result = subprocess.run(
            ["fpcalc", "-plain", posix_path(path)],
            text=True,
            capture_output=True,
            timeout=settings.ai_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(False, error="timeout: fpcalc")
    if result.returncode != 0:
        error = result.stderr.strip() or f"fpcalc exited {result.returncode}"
        return ToolResult(False, error=error)
    return ToolResult(True, data=parse_fpcalc(result.stdout).__dict__)


def parse_fpcalc(output: str) -> AudioFingerprint:
    duration: int | None = None
    fingerprint_value = ""
    for line in output.splitlines():
        if line.startswith("DURATION="):
            duration = int(float(line.split("=", 1)[1]))
        elif line.startswith("FINGERPRINT="):
            fingerprint_value = line.split("=", 1)[1]
        elif line and not fingerprint_value:
            fingerprint_value = line.strip()
    return AudioFingerprint(duration, fingerprint_value)

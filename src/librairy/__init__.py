from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("librairy")
except PackageNotFoundError:  # pragma: no cover - source tree without editable install
    __version__ = "0.1.0"

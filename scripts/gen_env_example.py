from __future__ import annotations

from pathlib import Path

from librairy.config import Settings


def main() -> int:
    Path(".env.example").write_text(Settings.env_example_text(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

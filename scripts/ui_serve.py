#!/usr/bin/env python3
"""Serve the dev fixture library over HTTP, so a browser can *use* it.

    python scripts/ui_serve.py            # http://127.0.0.1:8765
    python scripts/ui_serve.py --port 9000

`ui_check.py` renders a page to a file and measures it. That answers "does
anything stick out past the edge", and it cannot answer "does this button
work" — a `file://` render has no server to post to, no scripts (they are
stripped), and no way to click anything twice.

Three of the problems this exists for were invisible to every DOM assertion in
the suite and to `ui_check` as well:

  * the `?` beside a filename opened a panel that a `-webkit-line-clamp` on the
    heading clipped to nothing, so it appeared dead
  * Preview only ever opened; a second click re-fetched and swapped the same
    markup over itself
  * a stale queue row printed its reason twice

All three are things you find by clicking. This serves the same fixture the
screenshots use, against a throwaway library in a temporary directory, so
clicking is safe: nothing here touches a real library, and the directory is
removed on the way out.

Development only, on exactly the same terms as `ui_check.py`:

  * no browser is installed in the production image
  * no browser service exists in docker-compose
  * nothing in `src/librairy` imports this file, or knows it exists

`tests/test_dev_tooling.py` asserts those rather than trusting this paragraph.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import uvicorn  # noqa: PLC0415

    from tests.dev.fixture import build_app  # noqa: PLC0415

    root = Path(tempfile.mkdtemp(prefix="librairy-ui-"))
    try:
        app = build_app(root)
        print(f"fixture library at {root}")  # noqa: T201
        print(f"serving http://{args.host}:{args.port}/review")  # noqa: T201
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

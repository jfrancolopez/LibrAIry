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
import atexit
import shutil
import signal
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    # The live installation had 95 files waiting, which is the condition under
    # which Review stops being a page and starts being a scroll. The default
    # fixture has none, so the problem is invisible in it.
    parser.add_argument(
        "--inbox", type=int, default=0, help="stage N inbox proposals as well"
    )
    args = parser.parse_args(argv)

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import uvicorn  # noqa: PLC0415

    from tests.dev.fixture import build_app, dev_providers, stage_inbox  # noqa: PLC0415

    root = Path(tempfile.mkdtemp(prefix="librairy-ui-"))

    #  `finally` was not enough, and the difference was eight abandoned
    #  libraries in `/var/folders` after one afternoon. Uvicorn installs its own
    #  SIGTERM handling and leaves through it, so the block below never ran when
    #  the server was stopped by anything other than Ctrl-C — which is how every
    #  scripted restart stops it.
    #
    #  The rule this file claims for itself is that nothing it starts outlives
    #  it. A directory holding a whole fixture library is something it started.
    def cleanup(*_signal_args: object) -> None:
        shutil.rmtree(root, ignore_errors=True)

    atexit.register(cleanup)
    for received in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous = signal.getsignal(received)

        def stop(number: int, frame: object, _previous=previous) -> None:  # noqa: ANN001
            cleanup()
            if callable(_previous):
                _previous(number, frame)
            else:
                raise SystemExit(128 + number)

        signal.signal(received, stop)

    try:
        #  Fixed answers where the real ones would leave the machine or read a
        #  real audio file. A fixture track is a few bytes of text: it has no
        #  tags and no fingerprint, and asking AcoustID about one from a
        #  development harness would be both useless and rude.
        dev_providers()
        app = build_app(root)
        if args.inbox:
            stage_inbox(app.state.conn, app.state.settings, args.inbox)
        print(f"fixture library at {root}")  # noqa: T201
        print(f"serving http://{args.host}:{args.port}/review")  # noqa: T201
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

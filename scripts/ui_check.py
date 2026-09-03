#!/usr/bin/env python3
"""Look at a LibrAIry page with a real browser. Development only.

    python scripts/ui_check.py review
    python scripts/ui_check.py review --width 1280
    python scripts/ui_check.py --list

This exists because DOM assertions kept passing while the page was visibly
wrong: a monospace slot sized for "94%" that a three-word label overflowed, a
mobile row whose buttons stretched to a container sized by its longest label.
Both were found by looking. Neither would ever have failed a test.

It is a tool for building LibrAIry, in the same sense as ruff or a debugger.
It is NOT part of the product:

  * no browser is installed in the production image
  * no browser service exists in docker-compose
  * nothing in `src/librairy` imports this file, or knows it exists
  * nothing starts a browser at runtime, on a timer, or in the background

`tests/test_dev_tooling.py` asserts each of those rather than trusting this
paragraph. Chrome is found on the host if it happens to be there, driven once,
and terminated; the profile it writes is a temporary directory removed on the
way out, including when the run fails.

Screenshots land in `.dev/ui/`, which is gitignored. Nothing here is imported
by the test suite either — this is run by hand, when a layout changed.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / ".dev" / "ui"

# Where a Mac and a Linux desktop each keep it. `LIBRAIRY_CHROME` wins, for the
# machine that keeps it somewhere else.
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
    "chromium-browser",
)

# Chrome clamps a headless window narrower than roughly 500px, so `--window-size
# =375` silently renders at 485-500 and a "mobile screenshot" taken that way is
# a lie. A true 375px layout viewport has to come from an iframe: the frame is
# the phone, the window is the desk.
MOBILE_WIDTH = 375
DESKTOP_WIDTH = 1280


class MissingBrowser(RuntimeError):
    """No Chrome on this machine. A clean failure, not a traceback."""


def find_chrome(explicit: str | None = None) -> str:
    import os

    for candidate in (explicit, os.environ.get("LIBRAIRY_CHROME"), *CHROME_CANDIDATES):
        if not candidate:
            continue
        if Path(candidate).is_file():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    raise MissingBrowser(
        "No Chrome or Chromium found. Install one, or set LIBRAIRY_CHROME to its "
        "path. This is a development-only tool; LibrAIry itself does not need a "
        "browser."
    )


class Chrome:
    """One headless Chrome, owning one temporary profile directory.

    Chrome on this machine does its job and then does not exit: it writes the
    PNG, prints the DOM, and sits there. Old and new headless behave the same
    way. So the harness owns the lifecycle instead of hoping for an exit code —
    it waits for the artifact, then terminates, waits, and kills if terminating
    was not enough. That is also the rule this tool is meant to demonstrate:
    nothing it starts is allowed to outlive it.

    `--user-data-dir` keeps the run out of the developer's real profile, which
    matters for more than tidiness — without it a headless run hands itself to
    an already-running Chrome and returns immediately, having done nothing. The
    directory goes away in `close()`, which the context manager calls even when
    the body raised.
    """

    TERMINATE_GRACE = 5.0

    def __init__(self, binary: str, timeout: float = 45.0) -> None:
        self.binary = binary
        self.timeout = timeout
        self.profile = Path(tempfile.mkdtemp(prefix="librairy-ui-profile-"))
        self.killed = 0

    def _command(self, args: list[str]) -> list[str]:
        return [
            self.binary,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            # Reading a file:// page from a file:// iframe is cross-origin
            # without this, and the overflow probe needs the frame's document.
            "--allow-file-access-from-files",
            # Also needed on --dump-dom runs: a visible scrollbar makes
            # scrollWidth exceed clientWidth and reads as a layout overflow.
            "--hide-scrollbars",
            f"--user-data-dir={self.profile}",
            *args,
        ]

    def _reap(self, process: subprocess.Popen) -> None:
        """Terminate, wait, kill, wait. Return only once it is really gone."""
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=self.TERMINATE_GRACE)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self.TERMINATE_GRACE)
            self.killed += 1

    def _drive(self, args: list[str], done: Callable[[], bool]) -> None:
        stdout = self.profile / "stdout"
        with stdout.open("wb") as sink:
            process = subprocess.Popen(  # noqa: S603
                self._command(args), stdout=sink, stderr=subprocess.DEVNULL
            )
            try:
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    if done() or process.poll() is not None:
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError(
                        f"Chrome produced nothing within {self.timeout}s. The page may "
                        f"not have loaded: {args[-1]}"
                    )
            finally:
                self._reap(process)

    def screenshot(self, url: str, target: Path, width: int, height: int) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        seen: list[int] = []

        def written() -> bool:
            # Stable across two polls, so a half-written PNG is not mistaken
            # for a finished one.
            if not target.exists():
                return False
            seen.append(target.stat().st_size)
            return len(seen) >= 2 and seen[-1] == seen[-2] > 0

        self._drive(
            [f"--window-size={width},{height}", f"--screenshot={target}",
             "--virtual-time-budget=4000", url],
            written,
        )
        return target

    def dom(self, url: str, width: int, height: int) -> str:
        stdout = self.profile / "stdout"
        self._drive(
            [f"--window-size={width},{height}", "--virtual-time-budget=4000", "--dump-dom", url],
            lambda: stdout.exists() and b"</html>" in stdout.read_bytes(),
        )
        return stdout.read_text(encoding="utf-8", errors="replace")

    def close(self) -> None:
        shutil.rmtree(self.profile, ignore_errors=True)


@contextmanager
def chrome(binary: str | None = None) -> Iterator[Chrome]:
    browser = Chrome(find_chrome(binary))
    try:
        yield browser
    finally:
        browser.close()


# --- the page under the microscope --------------------------------------------


def _inline_previews(html: str, client) -> str:  # noqa: ANN001
    """Fetch the page's thumbnails and embed them as `data:` URIs.

    Over `file://` a `/preview/items/…/thumb` resolves to nothing, so every
    picture on the page was a broken-image icon and a grid of photographs
    photographed as a grid of alt text. That is precisely the class of thing
    this harness exists to catch — a layout that measures clean and looks
    wrong — and it was invisible in the one presentation that is *made* of
    pictures.

    Bounded by what the page draws: at most a page's worth of thumbnails, each
    already capped at 320px by the route that renders it. A URL that answers
    404 is left alone, because "there is no picture here" is a state worth
    seeing rather than one to paper over.
    """
    for src in sorted(set(re.findall(r'"(/preview/items/\d+/thumb[^"]*)"', html))):
        response = client.get(src.replace("&amp;", "&"))
        if response.status_code != 200:
            continue
        kind = response.headers.get("content-type", "image/jpeg").split(";")[0]
        encoded = base64.b64encode(response.content).decode("ascii")
        html = html.replace(f'"{src}"', f'"data:{kind};base64,{encoded}"')
    return html


def render(page, root: Path, *, expand: tuple[str, ...] = ()) -> Path:  # noqa: ANN001
    """Build a fixture library, render one page, inline its CSS.

    Inlining matters: the file is opened over `file://`, where `/static/...`
    resolves to nothing, and a page with no stylesheet is not the page.
    """
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from tests.dev.fixture import build_fixture  # noqa: PLC0415

    client = build_fixture(root)
    if isinstance(page, tuple):
        #  A scene that only exists behind a POST. The filename cleanup pages
        #  are the reason: previewing reads every file in a folder, which is
        #  work somebody asks for rather than something a GET does — so the
        #  only way to look at the layout is to ask for it the way a person
        #  does.
        path, form = page
        client.get("/browse")
        token = client.cookies.get("csrf_token", "")
        response = client.post(
            path,
            data={**form, "csrf_token": token},
            headers={"x-csrf-token": token},
            follow_redirects=True,
        )
    else:
        response = client.get(page)
    html = response.text
    for href in set(re.findall(r'<link[^>]+href="(/static/[^"]+\.css)"', html)):
        css = client.get(href).text
        html = re.sub(rf'<link[^>]+href="{re.escape(href)}"[^>]*>', f"<style>{css}</style>", html)
    # Scripts would 404 as a file, and layout is what is being looked at.
    html = re.sub(r'<script[^>]+src="/static/[^"]+"[^>]*></script>', "", html)
    html = _inline_previews(html, client)
    for css_class in expand:
        html = html.replace(f'<details class="{css_class}"', f'<details open class="{css_class}"')
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"{_slug(page)}.html"
    target.write_text(html, encoding="utf-8")
    return target


def _slug(page: str) -> str:
    """A filename safe to put in an `iframe src`.

    A page with a query string produced `review?state=confident.html`, and the
    `?` in an iframe src is a query string — so the frame asked for `review`,
    got nothing, and the overflow probe reported "nothing past the edge" for a
    page it had never loaded. A silent pass is the worst answer a measurement
    tool can give.
    """
    if isinstance(page, tuple):
        page = f"{page[0]}-{'-'.join(str(value) for value in page[1].values())}"
    return re.sub(r"[^A-Za-z0-9]+", "-", page.strip("/")).strip("-") or "index"


# `checkVisibility()` and not `offsetParent !== null`, and this mattered.
#
# The children of a *closed* `<details>` still get a layout box in Chrome, laid
# out unconstrained, so they have client rects and a non-null offsetParent. The
# old test therefore reported the Review filter panel as 30px past a 375px
# screen — every run, for several passes — for a panel nobody could see. Open
# the panel and it fits with 150px to spare.
#
# A measurement tool that cries wolf is worse than none: the real overflows it
# found got the same shrug as the phantom. `checkVisibility` is the browser's
# own answer to "can a person see this", which is the question being asked.
PROBE = """
window.addEventListener('load', function () {
  setTimeout(function () {
    var d = document.querySelector('iframe').contentDocument;
    var w = d.documentElement.clientWidth, out = [];
    d.querySelectorAll('body *').forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.right <= w + 1) return;
      var shown = el.checkVisibility
        ? el.checkVisibility({checkVisibilityCSS: true, contentVisibilityAuto: true})
        : el.offsetParent !== null;
      if (!shown) return;
      out.push({
        el: el.tagName.toLowerCase() + '.' + String(el.className || '').split(' ')[0],
        right: Math.round(r.right),
        text: (el.textContent || '').trim().slice(0, 30)
      });
    });
    document.title = JSON.stringify({
      viewport: w, scrollWidth: d.documentElement.scrollWidth, past_edge: out.slice(0, 10)
    });
  }, 500);
});
"""


def frame(page: Path, width: int, height: int) -> Path:
    target = OUT / f"frame-{width}.html"
    target.write_text(
        "<!doctype html><meta charset=\"utf-8\">"
        f"<style>html,body{{margin:0;padding:0;background:#888}}"
        f"iframe{{width:{width}px;height:{height}px;border:0;display:block}}</style>"
        f'<iframe src="{page.name}"></iframe><script>{PROBE}</script>',
        encoding="utf-8",
    )
    return target


def overflow(browser: Chrome, page: Path, width: int, height: int) -> dict:
    dom = browser.dom(f"file://{frame(page, width, height)}", width, height)
    title = re.search(r"<title>(.*?)</title>", dom, re.S)
    if title is None:
        return {"error": "the probe never ran"}
    import html as html_module

    return json.loads(html_module.unescape(title.group(1)))


# --- driving ------------------------------------------------------------------


PAGES = {
    "review": "/review",
    "browse": "/browse",
    "commit": "/commit",
    "dashboard": "/",
    # The filter panel folds away when nothing is filtered, and a collapsed
    # panel measures as no overflow at all — which is how a real 375px
    # overflow in it survived several passes of this harness. Filtering forces
    # `<details open>`, so the thing being measured is on screen.
    "review-filters": "/review?state=confident",
    # A group under a filter, which is the only state in which its two counts
    # differ: the heading says how many files the group holds *and* how many
    # this view is about, and the buttons say which of the two they act on.
    # Unfiltered they are the same number and the difference is invisible.
    "review-group": "/review?min_confidence=0.9",
    # Same reasoning one level down: the evidence panel is the widest thing
    # Review can draw and it is closed by default, so it is invisible to a
    # measurement of the page as served.
    "review-details": "/review",
    # The advisory section, which is the widest thing on the page after the
    # evidence panel: four classes, each with its own vocabulary.
    "storage": "/review",
    # A document whose sources disagree, with the comparison open. Filtered
    # because the fixture's inbox is bigger than one page of decisions and the
    # `CRACKING` case is the only one that renders this block.
    "review-document": "/review?category=documents&max_confidence=0.85",
    # The files nothing could answer: three reasons, a four-column table of
    # paths, and a row of actions. Paths in a table are the shape most likely
    # to run past a 375px edge, and the fixture holds one file per reason so
    # all three sentences are on screen at once.
    "review-waiting": "/review",
    # Every waiting state at once. All of them are reachable without an
    # encoder, which is the point of building orchestration before execution.
    "queue": "/maintenance/optimization",
    # Five kinds of decision on one page, which is the only condition under
    # which Commit's whole job — telling them apart — can be looked at. The
    # fixture used to put one library correction here and nothing else.
    "commit-mixed": "/commit",
    "commit-open": "/commit",
    # The relationship panel on a Commit card: a headline, both filenames and
    # their destinations. Long sentences beside a bounded set of buttons, which
    # is exactly the shape that ran past the edge on the collection page.
    "commit-related": "/commit?type=set-aside",
    # The three quarantine views that have rows in them. Held is the page as it
    # opens; the other two are where a decision goes and what disposal leaves
    # behind, and neither had ever been photographed.
    "quarantine": "/quarantine",
    "quarantine-waiting": "/quarantine?view=waiting",
    "quarantine-delete": "/quarantine?view=delete-queue",
    # A journal with real plans in it, including an adoption and a disposal.
    "history": "/history",
    "search": "/browse?q=IMG_4021",
    "item": "/items/1",
    # Twenty-five photographs that look alike: the thumbnail grid, the facts
    # under each picture, the tray, and the selection controls. Finding 25 is
    # the burst in the dev fixture — stable, the same way `/items/1` is.
    "photo-group": "/review/audit/25/photos",
    # A folder whose files explain each other: a RAW with its render, a Live
    # Photo's two halves, and the unrelated same-stem pair that stays unpaired.
    "browse-related": "/browse/Photos?folder=2024%2FBackyard",
    # The same context in a result list.
    "search-related": "/browse?q=IMG_5200&root=library",
    # Everything decision memory has picked up, read-only. The widest thing on
    # it is a destination template beside two counts.
    "learned": "/review/learned",
    # One import, in detail: the section tabs, the bulk control, and fifty
    # member rows with a destination each. `CameraCard-Aug24` is the dev
    # fixture's camera card — twenty-seven files with three answers in them.
    "collection": "/review/collection/CameraCard-Aug24",
    # The same page for a folder whose companions are shown under the file
    # that explains them, which is the one nested list on it.
    "collection-companions": "/review/collection/Arrival%20(2016)",
    # A camera card whose Live Photos and RAW/JPEG pairs are known *before*
    # anything is filed — counts at the top, and a companion row that says
    # what it belongs to. The widest lines on it are those sentences.
    "collection-pairs": "/review/collection/CameraCard-Aug24?section=unresolved",
    # What is waiting to be removed, why, and since when. The widest lines on
    # it are the provenance path and the Format Policy note under a row.
    "delete-queue": "/delete-queue",
    # A blocked Undo, explained. The dev fixture files a photograph and then
    # corrects it, so the earlier decision names the later one.
    "history-blocked": "/history",
    # What needs a person, grouped by how much it matters. The widest lines on
    # it are the sentences under a concern, and there are three levels of them
    # stacked above the machinery panels.
    "health": "/health",
    # Two arrivals that want the same destination, each card naming the other.
    # The widest line is the shared destination path, which is unbreakable.
    "commit-conflict": "/commit?type=new-file",
    # An arriving RAW whose proposal points at a folder that preserves
    # originals. The note is a full sentence inside a row that already carries
    # a path, a badge row and an edit panel.
    "review-policy": "/review",
    # What the index and the disk disagree about after somebody rearranged
    # things: a folder that moved as a unit, a loose file, and bytes that are
    # in two places at once. The widest lines are two full paths stacked.
    "reconcile": "/reconcile",
    # Decisions taken back, beside the journal rather than in it. Each row is a
    # badge, a filename, a sentence and a destination path.
    "history-withdrawn": "/history?view=withdrawn",
    # The one page whose subject is a policy: four category sections, the two
    # protections told apart, and a measurement under them. The widest things
    # on it are a label beside a select and a folder path beside a Remove.
    "format-policy": "/settings/format-policy",
    # A filed track with a stored catalog identity: five facts, a 128-character
    # fingerprint and two releases, which is the widest thing the item page has
    # ever had to hold on a phone.
    "item-identity": "/items/28",
    # The two filename-cleanup shapes. Both are POST-only, because previewing
    # reads files — see `render` for why the harness can ask for them anyway.
    "normalize-branch": ("/browse/normalize", {"scope": "Music/Rock/Bowie"}),
    "normalize-album": (
        "/browse/normalize",
        {"scope": "Music/Rock/Bowie/Hunky Dory"},
    ),
}

# Scenes that need something opened before it can be looked at. A closed
# `<details>` is not a layout — see `PROBE` for what believing otherwise cost.
EXPANDED = {
    "review-details": ("audit-details",),
    "storage": ("storage-why",),
    #  Every disclosure a Commit card can carry, open at once: the widest an
    #  OPTIMIZE card gets is its storage accounting beside its file list, and a
    #  closed `<details>` measures as no layout at all.
    "commit-open": ("why", "commit-files"),
    #  Every album's fold open at once. A collapsed `<details>` measures as no
    #  layout at all, which is exactly how a real overflow inside one would
    #  survive this harness.
    "normalize-branch": ("album-detail",),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("page", nargs="?", default="review", help=f"one of {', '.join(PAGES)}")
    parser.add_argument("--width", type=int, default=DESKTOP_WIDTH)
    parser.add_argument("--height", type=int, default=2400)
    parser.add_argument("--chrome", default=None, help="path to a Chrome binary")
    parser.add_argument("--list", action="store_true", help="list the pages and stop")
    args = parser.parse_args(argv)

    if args.list:
        for name, path in PAGES.items():
            print(f"{name:<12} {path}")
        return 0
    if args.page not in PAGES:
        parser.error(f"unknown page {args.page!r}; try --list")

    with tempfile.TemporaryDirectory(prefix="librairy-ui-fixture-") as fixture_root:
        page = render(
            PAGES[args.page], Path(fixture_root), expand=EXPANDED.get(args.page, ())
        )
        print(f"rendered  {page}")
        try:
            with chrome(args.chrome) as browser:
                desktop = browser.screenshot(
                    f"file://{page}", OUT / f"{args.page}-desktop.png", args.width, args.height
                )
                print(f"desktop   {desktop}  ({args.width}px)")
                mobile = browser.screenshot(
                    f"file://{frame(page, MOBILE_WIDTH, args.height)}",
                    OUT / f"{args.page}-mobile.png",
                    MOBILE_WIDTH,
                    args.height,
                )
                print(f"mobile    {mobile}  ({MOBILE_WIDTH}px, in a frame)")
                measured = overflow(browser, page, MOBILE_WIDTH, args.height)
                print(
                    f"viewport  {measured.get('viewport')}"
                    f"  scrollWidth {measured.get('scrollWidth')}"
                )
                for item in measured.get("past_edge", []):
                    print(f"  past the edge: {item['el']} right={item['right']} {item['text']!r}")
                if not measured.get("past_edge"):
                    print("  nothing past the edge")
        except MissingBrowser as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

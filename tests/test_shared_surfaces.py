"""The shared controls, on every surface that has rows to put them on.

The previous pass repaired the `?` control and the Preview toggle and proved
both in a real browser — on Review and Browse. Search, History, Quarantine and
Commit had no rows in the dev fixture at all, so on those four the repair was
assumed rather than exercised. These tests hold the surfaces themselves: if a
page that lists files stops offering the shared control, that is a regression
whether or not anyone opens a browser.

The browser work these complement is in `scripts/ui_serve.py`; what a headless
run adds is whether the panel actually *paints*, which no assertion about
markup can answer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.scanner import scan_root
from librairy.web.thumbs import PLAYABLE_VIDEO

# The dev fixture is not an installed package — `scripts/ui_serve.py` reaches
# it the same way, by putting the repository root on the path. Importing it
# here is deliberate: the fixture is the one place that describes every surface
# at once, and a second copy of it would drift from the one the browser uses.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.dev.fixture import build_fixture  # noqa: E402
from tests.dev.media import TINY_MP4  # noqa: E402

SURFACES = [
    ("/review", "Library Review"),
    ("/commit", "Commit"),
    ("/history", "History"),
    ("/quarantine", "Quarantine"),
    ("/browse?q=IMG_4021", "Search results"),
]


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory):
    return build_fixture(tmp_path_factory.mktemp("surfaces"))


@pytest.mark.parametrize(("url", "name"), SURFACES)
def test_every_populated_surface_offers_the_shared_control(client, url, name) -> None:
    body = client.get(url).text

    assert 'class="ext-info-toggle"' in body, f"{name} has no extension control"


@pytest.mark.parametrize(("url", "name"), SURFACES)
def test_the_control_is_a_native_popover_button(client, url, name) -> None:
    """Which is what gives keyboard activation and Escape dismissal for free.

    The control this replaced was a `<details>`, and the panel it opened was
    clipped out of existence by a `-webkit-line-clamp` on the heading beside
    it. A popover renders in the top layer, outside every `overflow` and every
    stacking context, which is a property of the platform rather than of a
    stylesheet somebody might later change.
    """
    body = client.get(url).text

    assert 'popovertarget="ext-info-' in body
    assert "<div id=\"ext-info-" in body
    assert "popover" in body
    # No script, no `onclick` — the CSP forbids both, and a control that needed
    # them would be broken in exactly the deployment that matters.
    assert "onclick" not in body


@pytest.mark.parametrize(("url", "name"), SURFACES)
def test_panel_ids_are_unique_on_the_page(client, url, name) -> None:
    """Two panels sharing an id is how a click opens somebody else's answer.

    Not hypothetical: a shared `anchor-name` on fourteen controls resolved to
    the last element in document order, and every panel on Review opened
    3,986 px down the page.
    """
    body = client.get(url).text
    ids = [
        chunk.split('"')[0]
        for chunk in body.split('popovertarget="')[1:]
    ]

    assert len(ids) == len(set(ids)), f"{name} repeats a panel id"


def test_a_quicktime_type_is_never_announced_to_the_browser() -> None:
    """Chrome cannot play `video/quicktime`. Measured, not assumed:

        canPlayType('video/quicktime') === ''      // cannot play
        canPlayType('video/mp4')       === 'maybe'

    A `<source type="video/quicktime">` is rejected before a byte is fetched —
    `networkState` goes straight to NO_SOURCE — so every `.mov` in the library
    previewed as a poster frame over a dead player, silently. Phone videos are
    `.mov`, which makes this the single most likely video anyone owns.
    """
    assert "video/quicktime" not in PLAYABLE_VIDEO.values()
    assert PLAYABLE_VIDEO[".mov"] == "video/mp4"


def test_the_fixture_video_is_really_a_video(tmp_path: Path) -> None:
    """A preview test over undecodable bytes proves nothing.

    `play()` on ten bytes of text never resolves — it hung a real browser for
    thirty seconds — so "the player was paused on collapse" was assertable and
    unprovable. This is an ISO base-media file, which is also why declaring it
    as MP4 is honest.
    """
    assert TINY_MP4[4:12] == b"ftypisom"
    assert 1000 < len(TINY_MP4) < 20000


def test_the_video_preview_serves_the_original_bytes(client) -> None:
    body = client.get("/browse?q=IMG_4021").text
    assert "IMG_4021" in body

    item_id = body.split('/preview/items/')[1].split('/')[0].split('"')[0]
    panel = client.get(f"/preview/items/{item_id}").text

    assert 'type="video/mp4"' in panel
    media = client.get(f"/preview/items/{item_id}/media")
    assert media.status_code == 200
    assert media.content == TINY_MP4


@pytest.mark.parametrize(("url", "name"), SURFACES)
def test_preview_is_a_toggle_wherever_it_is_offered(client, url, name) -> None:
    """One control, two states. It used to only ever open: a second click
    re-fetched the same markup and swapped it over itself."""
    body = client.get(url).text
    if "data-preview-toggle" not in body:
        pytest.skip(f"{name} lists nothing previewable")

    assert 'aria-expanded="false"' in body
    assert "data-preview-url" in body
    assert "data-preview-target" in body


# --- physical truth against indexed truth -------------------------------------


def test_browse_sees_a_file_the_index_has_never_heard_of(client) -> None:
    """Browse walks the disk; Search reads the index. The difference is the
    point of having both, and the fixture had stopped demonstrating it.

    The unindexed file used to be written with everything else and its `items`
    row deleted after the first scan. `_adoptable_optimizations` rescans the
    library four times, so the row came straight back and both surfaces listed
    the file — a scene that proved the opposite of what it was for, silently,
    for as long as nobody looked.
    """
    on_disk = client.get("/browse/Music?folder=Pop/Stray").text
    indexed = client.get("/search/results?q=never-scanned").text

    assert "never-scanned.flac" in on_disk
    assert "never-scanned.flac" not in indexed


def test_the_reserved_optimization_namespace_is_on_neither_surface(client) -> None:
    """`__librairy_internal__` is LibrAIry's own workspace, not the library."""
    from librairy.reserved import RESERVED_TOP

    everywhere = "".join(
        client.get(url).text
        for url in ("/browse", "/browse/Music", "/search/results?q=optimization")
    )

    assert RESERVED_TOP not in everywhere


def test_scanning_the_unindexed_file_is_what_makes_search_find_it(tmp_path: Path) -> None:
    """The other half of the contract, and the proof the first half is real.

    A file Browse sees and Search does not is only interesting if scanning it
    changes that. Asserted on its own fixture rather than the shared one,
    because it ends by scanning — and the shared fixture's whole point is a
    file nothing has scanned.
    """
    from tests.dev.fixture import build_app

    root = tmp_path / "library-scan"
    app = build_app(root)
    client = TestClient(app)
    settings, conn = app.state.settings, app.state.conn
    stray = "Music/Pop/Stray/never-scanned.flac"

    assert (settings.library_dir / stray).is_file(), "the fixture wrote it to disk"
    assert "never-scanned.flac" not in client.get("/search/results?q=never-scanned").text

    scan_root(conn, "library", settings.library_dir, settings)

    assert "never-scanned.flac" in client.get("/search/results?q=never-scanned").text


def test_a_file_deleted_underneath_leaves_a_record_that_says_so(tmp_path: Path) -> None:
    """The disagreement in the other direction.

    Browse walks the disk, so a file removed outside LibrAIry is simply not
    there. Search reads the index, which still has a row until the scanner
    reconciles — and after it does, the row is retained and marked rather than
    dropped, because "this used to be here and is not any more" is a thing a
    person needs to be able to look up.
    """
    from tests.dev.fixture import build_app

    root = tmp_path / "library-drift"
    app = build_app(root)
    client = TestClient(app)
    settings, conn = app.state.settings, app.state.conn
    relpath = "Music/Pop/Prince/03 - Kiss.flac"
    item_id = conn.execute("SELECT id FROM items WHERE relpath=?", (relpath,)).fetchone()["id"]

    assert "03 - Kiss.flac" in client.get("/search/results?q=Kiss").text
    (settings.library_dir / relpath).unlink()
    scan_root(conn, "library", settings.library_dir, settings)

    missing = conn.execute(
        "SELECT missing_since FROM items WHERE id=?", (item_id,)
    ).fetchone()["missing_since"]
    detail = client.get(f"/items/{item_id}")

    assert missing is not None, "the scanner marks it rather than dropping it"
    assert "03 - Kiss.flac" not in client.get("/browse/Music?folder=Pop/Prince").text
    assert "03 - Kiss.flac" not in client.get("/search/results?q=Kiss").text
    assert detail.status_code == 200
    assert "Not on disk" in detail.text

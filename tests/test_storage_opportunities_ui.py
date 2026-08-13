"""Storage opportunities on the Review page: advisory, quiet, and separate.

Two claims carry this file.

`test_a_remux_never_inflates_the_storage_total` — a remux saves nothing and
exists for compatibility. Folding its zero into a savings figure would be the
same dishonesty as calling it an optimization: the number would be right and
the claim would be wrong.

`test_no_endpoint_reads_two_id_kinds` — the inbox posts `proposal_id`, the
audit posts `finding_id`, this posts `opportunity_id`. All three are small
integers, and the only thing keeping one out of another's handler is that no
handler reads two of them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from librairy import optimization
from librairy.config import Settings
from librairy.db import connect
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.web.app import create_app

MB = 1024 * 1024
GB = 1024 * MB


def scene(tmp_path: Path, rows: list[dict] | None = None):
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (settings.library_dir / "Music").mkdir(parents=True, exist_ok=True)
    (settings.library_dir / "Music" / "concert.wav").write_bytes(b"RIFF")
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    for row in rows or []:
        insert(conn, **row)
    return TestClient(create_app(settings, conn)), conn


def insert(
    conn,
    relpath="Music/concert.wav",
    kind="audio-to-flac",
    quality=optimization.LOSSLESS,
    current=842 * MB,
    estimated=510 * MB,
    from_label="WAV",
    to_label="FLAC",
    compute="low",
    protected_by="",
    reason="FLAC compresses audio without discarding any of it.",
    facts=(("Codec", "PCM"), ("Sample rate", "44.1 kHz")),
):
    conn.execute(
        """
        INSERT INTO optimization_opportunities(
          item_id, root, relpath, kind, quality, current_bytes, estimated_bytes,
          summary, reason, compute, from_label, to_label, protected_by, facts,
          fingerprint, rule_version, status, detected_at, updated_at
        ) VALUES (NULL, 'library', ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, 'fp', 1,
                  'open', ?, ?)
        """,
        (
            relpath, kind, quality, current, estimated, reason, compute,
            from_label, to_label, protected_by,
            json.dumps([list(pair) for pair in facts]), utc_now(), utc_now(),
        ),
    )


def post(client, url: str, data: dict):
    """A form post the way a browser makes one, token and all."""
    client.get("/review")
    token = client.cookies["csrf_token"]
    return client.post(
        url,
        data={**data, "csrf_token": token},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )


def section(html: str) -> str:
    assert 'id="storage-opportunities"' in html, "no storage section"
    return html.split('id="storage-opportunities"', 1)[1].split("</section>", 1)[0]


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", html))


# --- the zero state --------------------------------------------------------------


def test_an_efficient_library_gets_one_quiet_line(tmp_path: Path) -> None:
    """The real installation's result. "Nothing worth doing" is a good outcome
    and should read like one rather than like a feature that failed to load."""
    client, _ = scene(tmp_path)

    body = section(client.get("/review").text)

    assert "No worthwhile optimizations found." in text_of(body)
    assert "storage-list" not in body, "no empty table"
    assert len(text_of(body)) < 200, "the zero state should be compact"


def test_the_zero_state_shows_no_savings_figure(tmp_path: Path) -> None:
    client, _ = scene(tmp_path)

    shown = text_of(section(client.get("/review").text))

    assert "Estimated potential savings" not in shown
    assert "unknown" not in shown


# --- the classes -----------------------------------------------------------------


def test_a_wav_row_says_lossless(tmp_path: Path) -> None:
    client, _ = scene(tmp_path, [{}])

    shown = text_of(section(client.get("/review").text))

    assert "LOSSLESS" in shown
    assert "WAV → FLAC" in shown
    assert "Nothing is discarded" in shown


def test_a_lossy_row_says_what_it_costs(tmp_path: Path) -> None:
    client, _ = scene(
        tmp_path,
        [{
            "relpath": "Movies/film.mkv", "kind": "video-transcode",
            "quality": optimization.LOSSY, "current": 12 * GB, "estimated": 8 * GB,
            "from_label": "H264", "to_label": "HEVC", "compute": "high",
        }],
    )

    shown = text_of(section(client.get("/review").text))

    assert "LOSSY" in shown
    assert "permanently discarded" in shown
    assert "High" in shown


def test_a_remux_row_says_zero_not_unknown(tmp_path: Path) -> None:
    """Zero is a fact. `unknown` would not be, and hiding the line would let a
    compatibility suggestion look like a saving."""
    client, _ = scene(
        tmp_path,
        [{
            "relpath": "Movies/clip.mkv", "kind": "video-remux",
            "quality": optimization.REMUX, "current": 2 * GB, "estimated": 2 * GB,
            "from_label": "MKV", "to_label": "MP4",
        }],
    )

    shown = text_of(section(client.get("/review").text))

    assert "REMUX" in shown
    assert "Storage savings 0 B" in shown
    assert "Purpose Compatibility" in shown
    assert "unknown" not in shown


def test_a_remux_never_inflates_the_storage_total(tmp_path: Path) -> None:
    """The claim this file exists for."""
    client, _ = scene(
        tmp_path,
        [
            {},  # 842 MB -> 510 MB lossless
            {
                "relpath": "Movies/clip.mkv", "kind": "video-remux",
                "quality": optimization.REMUX, "current": 2 * GB, "estimated": 2 * GB,
            },
        ],
    )

    shown = text_of(section(client.get("/review").text))

    assert "2 files" in shown
    assert "332.0 MB" in shown, "only the lossless saving counts"
    assert "compatibility-only" in shown
    assert "Compatibility-only suggestions are not counted" in shown


def test_the_estimate_is_labelled_an_estimate(tmp_path: Path) -> None:
    """Nothing has encoded anything. A field that swaps meaning once a job
    runs is a field nobody can read."""
    client, _ = scene(tmp_path, [{}])

    shown = text_of(section(client.get("/review").text))

    assert "Estimated result" in shown
    assert "Estimated savings" in shown
    assert "Actual" not in shown


def test_the_quality_word_is_never_only_a_colour(tmp_path: Path) -> None:
    client, _ = scene(tmp_path, [{}])

    body = section(client.get("/review").text)

    assert ">LOSSLESS<" in body


def test_there_is_no_vague_optimize_button(tmp_path: Path) -> None:
    """The user must understand lossless from lossy before acting."""
    client, _ = scene(tmp_path, [{}])

    body = section(client.get("/review").text)

    assert ">Optimize<" not in body
    assert ">Optimise<" not in body


# --- protected -------------------------------------------------------------------


def test_a_protected_original_says_so_in_words(tmp_path: Path) -> None:
    client, _ = scene(
        tmp_path,
        [{"relpath": "Photos/Memories/clip.wav", "protected_by": "Photos/Memories"}],
    )

    body = section(client.get("/review").text)
    shown = text_of(body)

    assert "PROTECTED" in shown
    assert "never converted" in shown
    assert "data-storage-eligible" not in body, "a protected row cannot be queued"


def test_an_ordinary_row_is_eligible(tmp_path: Path) -> None:
    client, _ = scene(tmp_path, [{}])

    assert "data-storage-eligible" in section(client.get("/review").text)


# --- selection isolation ----------------------------------------------------------


def test_the_storage_form_posts_its_own_field(tmp_path: Path) -> None:
    client, _ = scene(tmp_path, [{}])

    body = section(client.get("/review").text)

    assert 'name="opportunity_id"' in body
    assert 'action="/review/storage/bulk"' in body
    assert 'name="proposal_id"' not in body
    assert 'name="finding_id"' not in body


def test_no_endpoint_reads_two_id_kinds() -> None:
    """Three workflows, three field names, three signatures. Enforced by
    reading the routes rather than by a filter inside a shared handler."""
    import inspect

    from librairy.web import app as app_module

    source = inspect.getsource(app_module)
    for route in ("/review/action", "/review/audit/bulk", "/review/storage/bulk"):
        body = source.split(f'"{route}"', 1)[1].split("@app.", 1)[0]
        names = [
            field
            for field in ("proposal_id", "finding_id", "opportunity_id")
            if f"{field}:" in body
        ]
        assert len(names) <= 1, f"{route} reads {names}"


def test_the_audit_bulk_endpoint_refuses_an_opportunity_id(tmp_path: Path) -> None:
    client, conn = scene(tmp_path, [{}])
    opportunity_id = conn.execute(
        "SELECT id FROM optimization_opportunities"
    ).fetchone()["id"]

    response = post(
        client, "/review/audit/bulk", {"action": "keep", "opportunity_id": opportunity_id}
    )

    # It cannot even name the field; the selection arrives empty.
    assert "Nothing was selected" in response.text
    assert conn.execute(
        "SELECT status FROM optimization_opportunities"
    ).fetchone()["status"] == "open"


def test_the_storage_endpoint_refuses_a_finding_id(tmp_path: Path) -> None:
    client, conn = scene(tmp_path, [{}])

    response = post(client, "/review/storage/bulk", {"action": "dismiss", "finding_id": 1})

    assert "Nothing was selected" in response.text


# --- no suggestion ----------------------------------------------------------------


def test_no_suggestion_stops_the_row_coming_back(tmp_path: Path) -> None:
    client, conn = scene(tmp_path, [{}])
    row_id = conn.execute("SELECT id FROM optimization_opportunities").fetchone()["id"]

    post(client, "/review/storage/bulk", {"action": "dismiss", "opportunity_id": row_id})

    assert conn.execute(
        "SELECT status FROM optimization_opportunities"
    ).fetchone()["status"] == "dismissed"
    assert "No worthwhile optimizations found." in text_of(
        section(client.get("/review").text)
    )


def test_a_dismissal_survives_a_re_scan_of_an_unchanged_file(tmp_path: Path) -> None:
    client, conn = scene(tmp_path, [{}])
    row_id = conn.execute("SELECT id FROM optimization_opportunities").fetchone()["id"]
    optimization.dismiss(conn, row_id)

    found = optimization.Opportunity(
        relpath="Music/concert.wav", kind="audio-to-flac",
        quality=optimization.LOSSLESS, current_bytes=842 * MB,
        estimated_bytes=510 * MB, summary="", reason="", fingerprint="fp",
    )
    optimization.record_opportunities(conn, [found])

    assert conn.execute(
        "SELECT status FROM optimization_opportunities"
    ).fetchone()["status"] == "dismissed"


def test_a_changed_file_reopens_the_question(tmp_path: Path) -> None:
    """"No thank you" was an answer about a particular suggestion, not a
    permanent vow of silence."""
    client, conn = scene(tmp_path, [{}])
    optimization.dismiss(
        conn, conn.execute("SELECT id FROM optimization_opportunities").fetchone()["id"]
    )

    found = optimization.Opportunity(
        relpath="Music/concert.wav", kind="audio-to-flac",
        quality=optimization.LOSSLESS, current_bytes=900 * MB,
        estimated_bytes=520 * MB, summary="", reason="",
        fingerprint="a-different-fingerprint",
    )
    optimization.record_opportunities(conn, [found])

    assert conn.execute(
        "SELECT status FROM optimization_opportunities"
    ).fetchone()["status"] == "open"


def test_a_new_rule_version_reopens_the_question(tmp_path: Path, monkeypatch) -> None:
    """Without this, a `No suggestion` against an early weak rule would
    silence a much better later one for the life of the file."""
    client, conn = scene(tmp_path, [{}])
    optimization.dismiss(
        conn, conn.execute("SELECT id FROM optimization_opportunities").fetchone()["id"]
    )
    monkeypatch.setattr(optimization, "RULE_VERSION", 2)

    found = optimization.Opportunity(
        relpath="Music/concert.wav", kind="audio-to-flac",
        quality=optimization.LOSSLESS, current_bytes=842 * MB,
        estimated_bytes=400 * MB, summary="", reason="", fingerprint="fp",
    )
    optimization.record_opportunities(conn, [found])

    assert conn.execute(
        "SELECT status FROM optimization_opportunities"
    ).fetchone()["status"] == "open"


def test_suppression_is_not_keyed_on_the_name_alone(tmp_path: Path) -> None:
    """A path is not an identity: the same name can hold different bytes."""
    client, conn = scene(tmp_path, [{}])
    row = conn.execute("SELECT * FROM optimization_opportunities").fetchone()

    assert row["fingerprint"], "no fingerprint recorded"
    assert row["rule_version"], "no rule version recorded"


# --- nothing is executed ------------------------------------------------------------


def test_the_page_offers_no_way_to_start_an_encode(tmp_path: Path) -> None:
    """There is no queue yet, and the UI must not pretend otherwise."""
    client, _ = scene(tmp_path, [{}])

    body = section(client.get("/review").text)

    assert "Use optimized file" not in body
    assert "ffmpeg" not in body.lower()


def test_rendering_the_page_writes_nothing(tmp_path: Path) -> None:
    client, conn = scene(tmp_path, [{}])
    before = conn.execute(
        "SELECT count(*), max(updated_at) FROM optimization_opportunities"
    ).fetchone()

    client.get("/review")
    client.get("/review")

    assert (
        conn.execute(
            "SELECT count(*), max(updated_at) FROM optimization_opportunities"
        ).fetchone()[:]
        == before[:]
    )


def test_the_class_mix_is_spelled_correctly(tmp_path: Path) -> None:
    """`lossless` is an adjective. Deriving a plural produced "2 losslesss",
    which survives review because nobody reads a word they wrote."""
    client, _ = scene(
        tmp_path,
        [
            {},
            {"relpath": "Music/b.wav"},
            {
                "relpath": "Movies/clip.mkv", "kind": "video-remux",
                "quality": optimization.REMUX, "current": 2 * GB, "estimated": 2 * GB,
            },
        ],
    )

    body = section(client.get("/review").text)
    # The count line only. The explanatory sentence below it says
    # "suggestions" correctly, and asserting over the whole section would be
    # asserting about that instead.
    mix = text_of(body.split('class="muted storage-mix"', 1)[1].split("</p>", 1)[0])

    assert "2 lossless" in mix
    assert "losslesss" not in mix
    assert "1 compatibility-only suggestion" in mix
    assert "suggestions" not in mix

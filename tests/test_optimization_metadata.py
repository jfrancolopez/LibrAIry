"""What a representation change inherits, table by table.

The previous pass reported "nothing is carried" from a partial list. This
checks that claim against every table actually tied to an item — including
`item_metadata`, which is created lazily at first use and so does not appear in
a fresh schema at all, and `catalog_identity`, which looks item-linked from its
name and is not.

The decision that matters most here is the one *not* to throw identity away. A
trusted TMDB or MusicBrainz answer should survive MKV -> MP4. It does, and not
because adoption copies it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from librairy.audit_catalog import Identity, recall, remember
from librairy.config import Settings
from librairy.db import connect
from librairy.optimization_adopt import CARRIED, NEVER_CARRIED, record_result_item
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.tools.common import ensure_metadata_cache, get_cached_metadata, set_cached_metadata

ORIGINAL = "Movies/Fight Club (1999)/Fight Club (1999).mkv"
RESULT = "Movies/Fight Club (1999)/Fight Club (1999).mp4"
FOLDER = "Movies/Fight Club (1999)"


@pytest.fixture
def scene(tmp_path: Path):
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True)
    conn = connect(settings)
    source = settings.library_dir / ORIGINAL
    source.parent.mkdir(parents=True)
    source.write_bytes(b"the original container" * 500)
    scan_root(conn, "library", settings.library_dir, settings)
    original = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath=?", (ORIGINAL,)
    ).fetchone()
    job_id = int(
        conn.execute(
            """
            INSERT INTO optimization_jobs(
              item_id, root, relpath, fingerprint, kind, quality, from_label,
              to_label, preset, source_bytes, estimated_bytes, actual_bytes,
              state, verified, output_relpath, staging_dir, queued_at, updated_at
            ) VALUES (?, 'library', ?, ?, 'remux', 'remux', 'MKV', 'MP4',
                      'mp4-stream-copy', 100, 100, 100, 'ready', 'passed',
                      'output.mp4', '', ?, ?)
            """,
            (original["id"], ORIGINAL, original["fingerprint"], utc_now(), utc_now()),
        ).lastrowid
    )
    (settings.library_dir / RESULT).write_bytes(b"the new container" * 500)
    return conn, settings, int(original["id"]), original["fingerprint"], job_id


def adopt(conn, settings, job_id: int) -> int:
    return record_result_item(conn, settings, relpath=RESULT, job_id=job_id)


# --- the inventory is complete ------------------------------------------------


def test_every_table_tied_to_an_item_has_a_decision(scene) -> None:
    """Including the two created lazily, which a PRAGMA on a fresh database
    does not show, and the two FTS shadows, which cannot declare a key."""
    from librairy.indexer import _ensure_pattern_table
    from scripts.inventory_item_tables import CLASSIFIED

    conn = scene[0]
    ensure_metadata_cache(conn)
    _ensure_pattern_table(conn)

    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    linked = {
        name
        for name in tables
        if name in {"search_fts", "content_fts"}
        or any(
            fk["table"] == "items"
            for fk in conn.execute(f'PRAGMA foreign_key_list("{name}")')  # noqa: S608
        )
    }

    assert linked <= set(CLASSIFIED), sorted(linked - set(CLASSIFIED))
    assert "item_metadata" in linked, "the lazily created cache must be in the inventory"


def test_item_metadata_is_in_the_schema_now_rather_than_made_on_demand(
    scene,
) -> None:
    """It used to be absent until something cached a probe, which is how a
    table with a real foreign key into `items` got missed by an audit that
    only reads the migrations. Migration 037 put it in the schema, where it can
    be migrated — which is what the second and third metadata tools needed."""
    conn = scene[0]
    present = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "item_metadata" in present

    ensure_metadata_cache(conn)  # still safe, and still a no-op

    keys = conn.execute("PRAGMA foreign_key_list(item_metadata)").fetchall()
    assert [(k["table"], k["from"]) for k in keys] == [("items", "item_id")]
    columns = {row[1] for row in conn.execute("PRAGMA table_info(item_metadata)")}
    assert {"item_id", "tool", "fingerprint", "payload"} <= columns


# --- byte-specific records do not travel ----------------------------------------


def test_no_vision_result_is_copied(scene) -> None:
    conn, settings, original_id, fingerprint, job_id = scene
    conn.execute(
        "INSERT INTO vision_results(item_id, fingerprint, provider, model, category,"
        " caption, subjects, tags, name_tokens, visible_text, confidence, created_at)"
        " VALUES (?, ?, 'p', 'm', 'movie', 'two men fighting', '', '', '', '', 0.9, ?)",
        (original_id, fingerprint, utc_now()),
    )

    result_id = adopt(conn, settings, job_id)

    assert conn.execute(
        "SELECT COUNT(*) FROM vision_results WHERE item_id=?", (result_id,)
    ).fetchone()[0] == 0


def test_no_content_extraction_is_copied(scene) -> None:
    conn, settings, original_id, fingerprint, job_id = scene
    conn.execute(
        "INSERT INTO content_extractions(item_id, fingerprint, extractor, chars,"
        " extracted_at) VALUES (?, ?, 'x', 10, ?)",
        (original_id, fingerprint, utc_now()),
    )

    result_id = adopt(conn, settings, job_id)

    assert conn.execute(
        "SELECT COUNT(*) FROM content_extractions WHERE item_id=?", (result_id,)
    ).fetchone()[0] == 0


def test_the_ffprobe_cache_is_not_copied(scene) -> None:
    """`item_metadata` holds codec, bitrate, duration, width, channels — every
    field a property of the encoding that just changed."""
    from librairy.optimization import TOOL

    conn, settings, original_id, fingerprint, job_id = scene
    set_cached_metadata(
        conn, original_id, fingerprint, TOOL,
        {"container": "mkv", "video_codec": "h264", "bitrate": 8_000_000},
        utc_now(),
    )

    result_id = adopt(conn, settings, job_id)

    assert conn.execute(
        "SELECT COUNT(*) FROM item_metadata WHERE item_id=?", (result_id,)
    ).fetchone()[0] == 0


def test_the_ffprobe_cache_cannot_be_read_across_a_fingerprint_change(scene) -> None:
    """Belt and braces: even a future change that did copy the row could not
    serve stale technical facts, because the read requires a fingerprint match.

    This is what makes reusing `result_item_id` across a re-run safe.
    """
    from librairy.optimization import TOOL

    conn, settings, original_id, fingerprint, job_id = scene
    set_cached_metadata(
        conn, original_id, fingerprint, TOOL, {"video_codec": "h264"}, utc_now()
    )

    assert get_cached_metadata(conn, original_id, fingerprint, TOOL) is not None
    assert get_cached_metadata(conn, original_id, "a-different-fingerprint", TOOL) is None


def test_no_duplicate_or_similarity_record_is_copied(scene) -> None:
    conn, settings, original_id, _, job_id = scene
    other = int(
        conn.execute(
            "INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,"
            " first_seen_at, last_seen_at) VALUES ('library','Movies/other.mkv',"
            " 1, 1, 'zz', 'discovered', ?, ?)",
            (utc_now(), utc_now()),
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO duplicate_reports(item_id, other_id, payload, created_at)"
        " VALUES (?, ?, '{}', ?)",
        (original_id, other, utc_now()),
    )
    conn.execute(
        "INSERT INTO similar_media_flags(item_id, similar_item_id, kind, score,"
        " status, created_at) VALUES (?, ?, 'video', 0.9, 'review', ?)",
        (original_id, other, utc_now()),
    )

    result_id = adopt(conn, settings, job_id)

    assert conn.execute(
        "SELECT COUNT(*) FROM duplicate_reports WHERE item_id=? OR other_id=?",
        (result_id, result_id),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM similar_media_flags WHERE item_id=? OR similar_item_id=?",
        (result_id, result_id),
    ).fetchone()[0] == 0


def test_no_audit_finding_is_copied(scene) -> None:
    conn, settings, original_id, fingerprint, job_id = scene
    conn.execute(
        "INSERT INTO audit_findings(item_id, root, relpath, kind, severity, summary,"
        " fingerprint, status, detected_at, updated_at)"
        " VALUES (?, 'library', ?, 'naming', 'info', 's', ?, 'open', ?, ?)",
        (original_id, ORIGINAL, fingerprint, utc_now(), utc_now()),
    )

    result_id = adopt(conn, settings, job_id)

    assert conn.execute(
        "SELECT COUNT(*) FROM audit_findings WHERE item_id=?", (result_id,)
    ).fetchone()[0] == 0


def test_no_backup_state_is_copied(scene) -> None:
    conn, settings, original_id, fingerprint, job_id = scene
    conn.execute(
        "INSERT INTO backup_queue(item_id, relpath, fingerprint, state, attempts,"
        " created_at, updated_at) VALUES (?, ?, ?, 'done', 0, ?, ?)",
        (original_id, ORIGINAL, fingerprint, utc_now(), utc_now()),
    )

    result_id = adopt(conn, settings, job_id)

    assert conn.execute(
        "SELECT COUNT(*) FROM backup_queue WHERE item_id=?", (result_id,)
    ).fetchone()[0] == 0


def test_no_proposal_is_copied(scene) -> None:
    conn, settings, original_id, _, job_id = scene
    conn.execute(
        "INSERT INTO proposals(item_id, category, clean_name, dest_relpath,"
        " confidence, status, evidence, created_at, updated_at)"
        " VALUES (?, 'movies', 'Fight Club', ?, 0.9, 'approved', '{}', ?, ?)",
        (original_id, ORIGINAL, utc_now(), utc_now()),
    )

    result_id = adopt(conn, settings, job_id)

    assert conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE item_id=?", (result_id,)
    ).fetchone()[0] == 0


# --- but logical identity is not thrown away ------------------------------------


def test_a_trusted_catalog_identity_survives_a_container_change(scene) -> None:
    """The one the user was right to be strict about.

    `catalog_identity` is keyed by the library-relative *folder*, not by the
    item — so a TMDB answer for `Movies/Fight Club (1999)` still describes that
    folder after the MKV inside it becomes an MP4. Nothing is carried and
    nothing is lost.
    """
    conn, settings, _, _, job_id = scene
    remember(conn, "movie", FOLDER, Identity(
        provider="tmdb", entity="movie", catalog_id="550",
        canonical_title="Fight Club",
    ))

    adopt(conn, settings, job_id)

    still = recall(conn, "movie", FOLDER, "tmdb")
    assert still is not None
    assert still.catalog_id == "550"
    assert still.canonical_title == "Fight Club"


def test_catalog_identity_has_no_link_to_items_at_all(scene) -> None:
    """Proved from the schema rather than from the table's name, which is what
    makes the previous test's reasoning safe rather than lucky."""
    conn = scene[0]

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(catalog_identity)")}
    keys = conn.execute("PRAGMA foreign_key_list(catalog_identity)").fetchall()

    assert "item_id" not in columns
    assert keys == []
    assert {"scope_kind", "scope_key"} <= columns


def test_adoption_keeps_the_file_in_its_folder_which_is_what_makes_that_true(
    scene,
) -> None:
    from librairy.optimization_adopt import target_relpath

    assert target_relpath(ORIGINAL, "output.mp4") == RESULT
    assert str(Path(RESULT).parent) == FOLDER == str(Path(ORIGINAL).parent)


def test_learned_destinations_are_keyed_by_name_not_by_item(scene) -> None:
    from librairy.indexer import _ensure_pattern_table

    conn = scene[0]
    _ensure_pattern_table(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(library_patterns)")}

    assert "item_id" not in columns
    assert {"kind", "key", "dest_base"} <= columns


# --- and the decision is enforced structurally ------------------------------------


def test_nothing_is_declared_as_carried(scene) -> None:
    assert CARRIED == ()


def test_the_module_writes_to_no_byte_specific_table(scene) -> None:
    """Reads the adoption module's own SQL. A future carry-forward has to
    change this list on purpose."""
    source = Path("src/librairy/optimization_adopt.py").read_text(encoding="utf-8")
    statements = re.findall(
        r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", source, re.I
    )
    written = {table.lower() for _, table in statements}

    assert written & set(NEVER_CARRIED) == set(), written & set(NEVER_CARRIED)
    assert written <= {
        "items", "optimization_jobs",
        # The planner half: a plan, its operations, and the record kept when
        # an approval is taken back. None of them carry anything forward.
        "plans", "plan_ops", "plan_withdrawals",
    }, written


def test_there_is_no_bulk_carry_forward_anywhere_in_adoption() -> None:
    """`INSERT INTO x SELECT ... FROM x WHERE item_id=old` is the shape that
    silently starts copying a table nobody classified."""
    source = Path("src/librairy/optimization_adopt.py").read_text(encoding="utf-8")

    assert not re.search(r"INSERT\s+INTO\s+\w+\s*(\([^)]*\))?\s*SELECT", source, re.I)
    # `SELECT * FROM plans` is a lookup, not a carry-forward. The shape that
    # would start copying an unclassified table is one that reads `items`.
    assert not re.search(r"SELECT\s+\*\s+FROM\s+items", source, re.I)

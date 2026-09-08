"""Every released schema, upgraded to head, with rows in it.

`test_fresh_db_migrates_to_current_schema` proves that 0 → head works, which is
what a new install does and the case that can never break unnoticed. It says
nothing at all about the case that actually costs somebody their afternoon:

    an installation that has been running for a year, upgrading

That path has been walked in anger exactly once — v1.2.0 shipped schema 10 and
v1.3.1 arrived at 47, thirty-seven migrations in a single release — and the
roadmap calls it load-bearing for that reason. So each released schema is built
here, **populated the way a real installation is populated**, and then migrated
to head with its rows checked afterwards.

Populated is the point. An empty database migrates through anything: the
migrations that can fail are the ones that rewrite rows, add a `NOT NULL`
column, tighten a `CHECK`, or backfill from a table that might be empty. A test
that migrates nothing proves that the SQL parses.

## Why the versions are listed rather than derived

    v1.0.0   schema 4
    v1.2.0   schema 10
    v1.3.1   schema 47

Written down because they are history, and history is not derivable from the
current source. If a fourth release happens, its schema is added here by hand,
by somebody who knows what shipped.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import MIGRATIONS, SCHEMA_VERSION, connect, migrate, user_version

#  What each released version's database actually looked like. The schema a
#  release shipped is a fact about the past; see `docs/CHANGELOG.md`, and
#  `librairy_release_identity` for why a git tag is not the answer.
RELEASED_SCHEMAS = {
    "v1.0.0": 4,
    "v1.2.0": 10,
    "v1.3.1": 47,
}


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for directory in (
        settings.appdata_dir,
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def at_schema(path: Path, version: int) -> sqlite3.Connection:
    """A database as it was at one released schema, and not one step further.

    Built by running the real migrations up to that point rather than by
    checking in a fixture file: a stored `.db` drifts from what the migrations
    actually produce, and the thing being tested is the migrations.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    for step in range(1, version + 1):
        conn.executescript(
            f"BEGIN;\n{MIGRATIONS[step]}\nPRAGMA user_version={step};\nCOMMIT;"
        )
    return conn


def populate(conn: sqlite3.Connection, version: int) -> None:
    """The rows a year-old installation has, in whatever form that schema had.

    Deliberately spare and deliberately *not* schema-aware beyond what it has
    to be: the columns used here are the ones that existed at schema 4 and have
    existed ever since, so one function populates every released version. A
    migration that breaks on these breaks on everybody's database.
    """
    conn.executemany(
        "INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("library", "Photos/2024/IMG_0001.jpg", 4_100_000, 1, "fp-a", "committed",
             "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
            ("library", "Music/Queen/Opera/01.flac", 30_000_000, 2, "fp-b", "committed",
             "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
            ("inbox", "holiday.jpg", 2_000_000, 3, "fp-c", "discovered",
             "2025-06-01T00:00:00+00:00", "2025-06-01T00:00:00+00:00"),
            #  A file that has gone. Every "live" filter in the program depends
            #  on this column, and a migration that rewrote items without it
            #  would resurrect deleted files.
            ("library", "Photos/2023/gone.jpg", 1_000, 4, "fp-d", "committed",
             "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00"),
        ],
    )
    conn.execute(
        "UPDATE items SET missing_since='2025-08-01T00:00:00+00:00'"
        " WHERE relpath='Photos/2023/gone.jpg'"
    )
    #  A committed decision and the history it produced: the two things an
    #  upgrade must never lose, and the reason rolling back is three steps
    #  rather than one.
    conn.execute(
        "INSERT INTO plans(id, status, plan_hash, created_at, approved_at, finished_at)"
        " VALUES ('plan-1', 'done', 'hash-1', '2025-02-01T00:00:00+00:00',"
        " '2025-02-01T00:00:00+00:00', '2025-02-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO plan_ops(plan_id, seq, op_type, item_id, src_root, src_relpath,"
        " src_fingerprint, dest_root, dest_relpath, result, final_relpath, executed_at)"
        " VALUES ('plan-1', 1, 'move', 1, 'inbox', 'IMG_0001.jpg', 'fp-a', 'library',"
        " 'Photos/2024/IMG_0001.jpg', 'ok', 'Photos/2024/IMG_0001.jpg',"
        " '2025-02-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO history(ts, plan_id, op_id, action, src_root, src_relpath,"
        " dest_root, dest_relpath, fingerprint, outcome)"
        " VALUES ('2025-02-01T00:00:00+00:00', 'plan-1', 1, 'move', 'inbox',"
        " 'IMG_0001.jpg', 'library', 'Photos/2024/IMG_0001.jpg', 'fp-a', 'ok')"
    )
    #  A proposal nobody has answered, written with whichever columns this
    #  schema had. `action` and `dest_root` arrived later, and an insert naming
    #  them would test nothing about schema 4 except that this file is wrong.
    wanted = {
        "item_id": 3,
        "category": "photos",
        "clean_name": "holiday.jpg",
        "dest_relpath": "Photos/2025/holiday.jpg",
        "confidence": 0.91,
        "status": "proposed",
        "action": "move",
        "dest_root": "library",
        "evidence": "[]",
        "created_at": "2025-06-01T00:00:00+00:00",
        "updated_at": "2025-06-01T00:00:00+00:00",
    }
    present = {row["name"] for row in conn.execute("PRAGMA table_info(proposals)")}
    columns = [name for name in wanted if name in present]
    conn.execute(
        f"INSERT INTO proposals({', '.join(columns)})"  # noqa: S608 - names are ours
        f" VALUES ({', '.join('?' * len(columns))})",
        [wanted[name] for name in columns],
    )
    del version
    conn.commit()


@pytest.mark.parametrize(("release", "schema"), sorted(RELEASED_SCHEMAS.items()))
def test_a_released_database_with_rows_in_it_reaches_head(
    tmp_path: Path, release: str, schema: int
) -> None:
    """The upgrade a real installation performs, for every version ever shipped."""
    path = tmp_path / f"{release}.db"
    conn = at_schema(path, schema)
    populate(conn, schema)
    assert user_version(conn) == schema

    migrate(conn)

    assert user_version(conn) == SCHEMA_VERSION
    #  Everything is still there, and the file that had gone is still gone.
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 4  # noqa: PLR2004
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM items WHERE missing_since IS NOT NULL"
        ).fetchone()[0]
        == 1
    )
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM plans WHERE status='done'").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize(("release", "schema"), sorted(RELEASED_SCHEMAS.items()))
def test_a_released_database_still_works_afterwards(
    tmp_path: Path, release: str, schema: int
) -> None:
    """Migrating is half of it. The program has to be able to *use* the result.

    An upgrade that leaves a database `PRAGMA integrity_check`-clean and every
    page unable to render is an upgrade that failed later, somewhere less
    obvious.
    """
    from librairy import project_status, transfer_status
    from librairy.web.dashboard import dashboard_data
    from librairy.web.health import health_data

    settings = settings_for(tmp_path)
    path = settings.appdata_dir / "librairy.db"
    upgraded = at_schema(path, schema)
    populate(upgraded, schema)
    upgraded.close()

    #  Opened the way the program opens it, which runs the migrations.
    conn = connect(settings)

    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    #  The four surfaces built across M3, against a database that started life
    #  before any of them existed.
    assert dashboard_data(conn, settings)["counts"] is not None
    assert health_data(conn, settings)["summary_status"] in ("OK", "WARN")
    assert transfer_status.destination_views(conn, settings) == []
    assert project_status.cards(conn) == []
    del release


def test_every_release_in_the_changelog_has_a_schema_recorded() -> None:
    """A fourth release must not quietly go untested.

    The list above is history and cannot be derived from the source, so the
    thing that can be checked is that nobody has shipped a version without
    writing its schema down here.
    """
    from tests.test_release_identity import RELEASED

    shipped = {f"v{version}" for version, _date in RELEASED}

    missing = shipped - set(RELEASED_SCHEMAS)
    assert not missing, f"no schema recorded for {sorted(missing)}"


def test_the_migration_path_is_unbroken() -> None:
    """Every step from 1 to head exists. A gap is an upgrade that stops."""
    assert sorted(MIGRATIONS) == list(range(1, SCHEMA_VERSION + 1))


def test_a_newer_database_is_refused_rather_than_downgraded(tmp_path: Path) -> None:
    """The other half of the release contract: a v1.2.0 image cannot read a
    schema-47 database, and must say so rather than try."""
    from librairy.db import DatabaseVersionError

    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 5}")

    with pytest.raises(DatabaseVersionError):
        migrate(conn)
    conn.close()

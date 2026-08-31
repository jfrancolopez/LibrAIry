"""Release acceptance: can somebody install, upgrade, recover and roll back?

Forty-seven schema generations and four thousand tests prove that the *code*
does what it says. None of them prove that a person can get the thing running,
move an existing installation forward without losing what they decided, or get
back to where they were when something goes wrong. Those are different
questions and this is where they are asked.

The rules this file works under:

* **Nothing here touches production.** Every path is a temporary directory.
* **A migration exiting zero is not a passing migration.** Each historical
  fixture carries data whose survival is checked by name.
* **Rollback is not "start the old image".** An application that has migrated a
  database beyond what an older build understands cannot be rolled back by
  swapping the image; the safe unit is the previous build *with* the
  pre-upgrade snapshot, and the tests below pin the refusal that makes that
  true rather than trusting documentation to say it.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy import __version__
from librairy.build_info import REVISION_ENV, describe
from librairy.config import Settings
from librairy.db import (
    MIGRATIONS,
    SCHEMA_VERSION,
    connect,
    database_path,
    migrate,
    user_version,
)
from librairy.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for root in (
        settings.appdata_dir,
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
    ):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


# --------------------------------------------------------------------------
# 1-7: release identity and packaging
# --------------------------------------------------------------------------


def test_the_version_command_answers_without_an_installation() -> None:
    """It is what you run *because* something is wrong — an unconfigured
    container, a read-only mount, a database that will not open. A version
    command that needs a working installation cannot be used when it matters.
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "librairy", "version"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"version: {__version__}" in result.stdout
    assert f"schema_supported: {SCHEMA_VERSION}" in result.stdout


def test_the_runtime_version_is_the_package_version(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    found = describe(conn)
    page = client.get("/dashboard").text

    assert found["version"] == __version__
    assert found["schema_supported"] == SCHEMA_VERSION
    assert found["schema_current"] == SCHEMA_VERSION
    assert found["migration_pending"] is False
    assert f"v{__version__}" in page


def test_a_build_with_no_recorded_revision_says_so_rather_than_guessing(
    monkeypatch,
) -> None:  # noqa: ANN001
    """There is deliberately no fallback that shells out to git. A container
    has no repository in it, and a fallback would report the builder's working
    tree — which is worse than an honest "unknown"."""
    monkeypatch.delenv(REVISION_ENV, raising=False)
    assert describe()["revision"] == "unknown"

    monkeypatch.setenv(REVISION_ENV, "0123456789abcdef")
    assert describe()["revision"] == "0123456789abcdef"


def test_the_image_carries_the_revision_it_was_built_from() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'ARG LIBRAIRY_REVISION=""' in dockerfile
    assert 'org.opencontainers.image.revision="${LIBRAIRY_REVISION}"' in dockerfile
    assert 'LIBRAIRY_REVISION="${LIBRAIRY_REVISION}"' in dockerfile
    assert "LIBRAIRY_REVISION=${{ github.sha }}" in workflow


def test_the_release_workflow_only_ever_builds_the_tag_it_was_given() -> None:
    """A release workflow that could compute its own tag is a workflow that can
    move one. This one fires on a pushed tag and names the image after that
    ref; nothing in it writes a tag back to the repository."""
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "type=ref,event=tag" in workflow
    for writing in ("git tag", "git push --tags", "actions/github-script"):
        assert writing not in workflow


def test_the_production_image_contains_no_developer_browser() -> None:
    """Chrome is a test harness. The production image ships the product."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8").lower()
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").lower()

    for banned in ("chrome", "chromium", "playwright", "selenium", "puppeteer"):
        assert banned not in dockerfile
        assert banned not in compose


def test_the_build_context_excludes_tests_secrets_and_developer_data() -> None:
    ignored = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("!")
    }

    for entry in (".env", ".env.*", "tests", "data", ".git", "*.sqlite3"):
        assert entry in ignored, f"{entry} would be copied into the image"


def test_the_repository_ships_no_real_provider_credential() -> None:
    """Targeted, and it never prints what it finds. A key-shaped string in a
    fixture is fine when it says so; one that looks live is a release
    blocker."""
    suspicious = re.compile(
        r"(sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}"
        r"|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----)"
    )
    offenders = []
    for path in [*ROOT.glob("*.md"), *ROOT.glob("*.toml"), *ROOT.glob("*.yml")]:
        if suspicious.search(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(path.name)
    wanted = {".py", ".md", ".yml", ".html", ".toml"}
    for folder in ("src", "docs", ".github"):
        for path in (ROOT / folder).rglob("*"):
            if not path.is_file() or path.suffix not in wanted:
                continue
            if suspicious.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == [], f"credential-shaped strings in: {offenders}"


# --------------------------------------------------------------------------
# 8-13: fresh installation
# --------------------------------------------------------------------------


def test_an_empty_database_migrates_to_the_current_head(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)

    assert user_version(conn) == SCHEMA_VERSION
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    #  Nothing invented on the way in. A fresh install that arrives with demo
    #  rows in it is an install nobody can trust the first count of.
    for table in ("items", "plans", "history", "proposals", "audit_findings"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


@pytest.mark.parametrize(
    "page",
    ["/dashboard", "/review", "/browse", "/commit", "/quarantine", "/history",
     "/delete-queue", "/reconcile", "/health", "/settings"],
)
def test_every_page_renders_on_an_empty_installation(tmp_path: Path, page: str) -> None:
    """Empty is the state every installation starts in and the one least often
    looked at. A traceback here is the first thing a new user would see."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    response = client.get(page)

    assert response.status_code == 200, page
    body = flat(response.text)
    assert "Traceback" not in body
    assert "System Fault" not in body


def test_a_fresh_install_needs_no_provider_credentials(tmp_path: Path) -> None:
    """Optional providers stay optional. Nothing about a missing TMDB key or an
    absent Ollama should stop the program starting."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/health").status_code == 200
    assert client.get("/review").status_code == 200


def test_the_first_file_goes_all_the_way_through(tmp_path: Path) -> None:
    """Discovery, analysis, Review, Approve, Commit, Browse, Search, Undo — the
    whole product, once, on one file, from nothing."""
    from librairy.executor import execute_plan
    from librairy.history import undo_plan
    from librairy.search import SearchFilters, search_items
    from librairy.web.commit import create_commit_plan
    from librairy.web.review import ReviewFilters, apply_review_action

    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    (settings.inbox_dir / "holiday.jpg").write_bytes(
        (ROOT / "tests/fixtures/tiny.jpg").read_bytes()
    )

    from librairy.classify import analyze_items
    from librairy.scanner import scan_root

    scan_root(conn, "inbox", settings.inbox_dir, settings)
    analyze_items(conn, settings, 10)
    proposal = conn.execute("SELECT id, dest_relpath FROM proposals").fetchone()
    assert proposal is not None, "analysis produced no proposal for the first file"

    assert "holiday.jpg" in flat(client.get("/review").text)
    apply_review_action(conn, "approve", ReviewFilters(), proposal_ids=[proposal["id"]])
    plan_id = create_commit_plan(conn, settings)
    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1
    filed = str(proposal["dest_relpath"])
    assert (settings.library_dir / filed).is_file()
    assert not (settings.inbox_dir / "holiday.jpg").exists()
    assert [row["relpath"] for row in search_items(conn, "holiday", SearchFilters())] == [
        filed
    ]

    results = undo_plan(conn, plan_id, settings)

    assert [result.outcome for result in results] == ["ok"]
    assert (settings.inbox_dir / "holiday.jpg").is_file()


def test_restarting_changes_nothing(tmp_path: Path) -> None:
    """A second start must not re-run a migration, re-analyse a decided file or
    lose a decision that had not been committed yet."""
    from librairy.planner import OperationSpec, approve_plan, create_plan
    from librairy.scanner import scan_root

    settings = settings_for(tmp_path)
    conn = connect(settings)
    (settings.library_dir / "Music").mkdir(parents=True, exist_ok=True)
    (settings.library_dir / "Music/song.flac").write_bytes(b"a recording")
    scan_root(conn, "library", settings.library_dir, settings)
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="library",
                src_relpath="Music/song.flac",
                dest_root="library",
                dest_relpath="Music/Queen/song.flac",
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    before = {
        "items": conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "schema": user_version(conn),
    }
    conn.close()

    #  The restart: a new process opening the same appdata directory.
    again = connect(settings)

    assert user_version(again) == before["schema"]
    assert again.execute("SELECT COUNT(*) FROM items").fetchone()[0] == before["items"]
    assert (
        again.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()[
            "status"
        ]
        == "approved"
    )
    assert again.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert (settings.library_dir / "Music/song.flac").is_file()


# --------------------------------------------------------------------------
# 14-25: upgrading an existing installation
# --------------------------------------------------------------------------

#  Representative generations, chosen from what the migrations actually did
#  rather than by picking round numbers.
#
#  A historical database is built by replaying the project's own migrations up
#  to that version and stopping. That *is* the historical schema — it is the
#  same statements, in the same order, that produced it at the time — and it is
#  the strongest evidence available without an archived production database,
#  which this pass may not touch. What it cannot prove is that a real database
#  of that era contained nothing else; the fixtures below carry representative
#  rows for exactly that reason.
ERAS = {
    10: "before the audit and the catalogs (search and backup exist)",
    22: "the audit and optimization era, before plan withdrawals",
    36: "before the multi-tool metadata cache",
    42: "after decision memory, before relationships and Format Policy",
    46: "the release before this one",
}


def historical(path: Path, version: int) -> sqlite3.Connection:
    """A database as this project's own migrations left it at `version`."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    for step in range(1, version + 1):
        conn.executescript(
            f"BEGIN;\n{MIGRATIONS[step]}\nPRAGMA user_version={step};\nCOMMIT;"
        )
    return conn


def seed(conn: sqlite3.Connection, version: int) -> dict[str, object]:
    """Representative state, using only what existed at that generation."""
    now = "2024-01-01T00:00:00+00:00"
    conn.executescript(
        f"""
        INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,
                          first_seen_at, last_seen_at)
        VALUES (1, 'library', 'Music/Queen/song.flac', 10, 1, 'fp-song',
                'committed', '{now}', '{now}'),
               (2, 'inbox', 'arriving.jpg', 20, 2, 'fp-arriving',
                'proposed', '{now}', '{now}');
        INSERT INTO plans(id, status, plan_hash, created_at, approved_at)
        VALUES ('historic-plan', 'approved', 'hash-1', '{now}', '{now}');
        INSERT INTO plan_ops(id, plan_id, seq, op_type, item_id, src_root,
                             src_relpath, src_fingerprint, dest_root, dest_relpath)
        VALUES (1, 'historic-plan', 0, 'move', 1, 'library',
                'Music/Queen/song.flac', 'fp-song', 'library',
                'Music/Rock/Queen/song.flac');
        INSERT INTO history(ts, plan_id, op_id, action, src_root, src_relpath,
                            dest_root, dest_relpath, fingerprint, outcome)
        VALUES ('{now}', 'done-plan', NULL, 'move', 'inbox', 'old.flac',
                'library', 'Music/Queen/song.flac', 'fp-song', 'ok');
        INSERT INTO settings(key, value) VALUES ('dashboard.welcome', 'dismissed');
        INSERT INTO proposals(item_id, category, clean_name, dest_relpath, confidence,
                              status, evidence, created_at, updated_at)
        VALUES (2, 'photos', 'arriving.jpg', 'Photos/2024/arriving.jpg', 0.9,
                'proposed', '[]', '{now}', '{now}');
        """
    )
    expected: dict[str, object] = {"items": 2, "history": 1, "proposals": 1}
    if version >= 6:
        conn.execute(
            "INSERT INTO quarantine_entries(item_id, reason, original_root,"
            " original_relpath, quarantined_at, plan_id)"
            " VALUES (1, 'user', 'library', 'Music/held.flac', ?, 'done-plan')",
            (now,),
        )
        expected["quarantine_entries"] = 1
    if version >= 16:
        conn.execute(
            "INSERT INTO audit_findings(root, relpath, kind, severity, summary,"
            " evidence, status, detected_at, updated_at)"
            " VALUES ('library', 'Music/Queen', 'loose-tracks', 'review',"
            " 'Four loose tracks.', '[]', 'open', ?, ?)",
            (now, now),
        )
        expected["audit_findings"] = 1
    if version >= 41:
        conn.execute(
            "INSERT INTO decision_events(kind, signature, specificity, features,"
            " outcome, dest_relpath, decided_at, settled_at)"
            " VALUES ('destination', 'music|flac', 1, '{}', 'Music/Queen',"
            " 'Music/Queen/song.flac', ?, ?)",
            (now, now),
        )
        expected["decision_events"] = 1
    if version >= 44:
        #  A folder scope rather than a category one: migration 044 already
        #  moved the existing music preference into `category`/`music`, so
        #  seeding another there would collide with the upgrade's own work.
        conn.execute(
            "INSERT INTO format_policy_scopes(scope_kind, scope_value,"
            " preserve_originals, created_at, updated_at)"
            " VALUES ('folder', 'Photos/Wedding', 1, ?, ?)",
            (now, now),
        )
        expected["format_policy_scopes"] = conn.execute(
            "SELECT COUNT(*) FROM format_policy_scopes"
        ).fetchone()[0]
    conn.commit()
    return expected


@pytest.mark.parametrize("version", sorted(ERAS))
def test_a_historical_database_upgrades_without_losing_what_was_decided(
    tmp_path: Path, version: int
) -> None:
    """A migration exiting zero is not a passing migration.

    Every row seeded below is something nobody can regenerate: an operation
    that ran, an approval that has not, a held file's provenance, a policy
    somebody configured, a decision the program learned from. The count is
    checked by name on the other side.
    """
    path = tmp_path / f"schema-{version}.sqlite3"
    old = historical(path, version)
    expected = seed(old, version)
    assert user_version(old) == version

    migrate(old)

    assert user_version(old) == SCHEMA_VERSION, ERAS[version]
    for table, count in expected.items():
        assert (
            old.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count  # noqa: S608
        ), f"{table} did not survive the upgrade from schema {version}"
    assert old.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert old.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("version", sorted(ERAS))
def test_an_approved_historical_plan_is_not_touched_by_upgrading(
    tmp_path: Path, version: int
) -> None:
    """An approval is a decision somebody made about files that have not moved.
    Migrating the database is not the moment to reconsider it, repair it, or
    quietly execute it."""
    path = tmp_path / f"plan-{version}.sqlite3"
    old = historical(path, version)
    seed(old, version)
    before = dict(
        old.execute("SELECT * FROM plans WHERE id='historic-plan'").fetchone()
    )
    ops_before = [
        dict(row)
        for row in old.execute(
            "SELECT seq, op_type, src_relpath, dest_relpath, src_fingerprint,"
            " executed_at FROM plan_ops WHERE plan_id='historic-plan' ORDER BY seq"
        )
    ]

    migrate(old)

    after = dict(old.execute("SELECT * FROM plans WHERE id='historic-plan'").fetchone())
    ops_after = [
        dict(row)
        for row in old.execute(
            "SELECT seq, op_type, src_relpath, dest_relpath, src_fingerprint,"
            " executed_at FROM plan_ops WHERE plan_id='historic-plan' ORDER BY seq"
        )
    ]

    assert after["status"] == "approved"
    assert after["plan_hash"] == before["plan_hash"]
    assert after["approved_at"] == before["approved_at"]
    assert ops_after == ops_before
    assert all(op["executed_at"] is None for op in ops_after)


@pytest.mark.parametrize("version", sorted(ERAS))
def test_search_is_correct_after_an_upgrade(tmp_path: Path, version: int) -> None:
    """Search is derived, so an upgrade may rebuild it — and must leave it
    correct rather than merely present.

    Two behaviours, and both are checked. An upgrade that crosses migration 046
    recreates the table and refills it, because FTS5 cannot add a column. An
    upgrade that starts *at* 46 does not rebuild, so the index a running
    installation already had has to survive untouched — which is why this
    fixture writes one the way the scanner would.
    """
    from librairy.search import sync_search_item

    path = tmp_path / f"search-{version}.sqlite3"
    old = historical(path, version)
    seed(old, version)
    if version >= 46:
        #  What a real installation at this generation would already hold.
        for row in old.execute("SELECT id FROM items"):
            sync_search_item(old, int(row["id"]))

    migrate(old)

    indexed = {
        str(row["relpath"])
        for row in old.execute(
            "SELECT i.relpath FROM search_fts s JOIN items i ON i.id = s.item_id"
        )
    }
    assert indexed == {"Music/Queen/song.flac", "arriving.jpg"}


def test_the_supported_upgrade_range_is_what_the_migrations_cover() -> None:
    """No gaps. A missing generation is a database somebody cannot move
    forward, and it would be found by an upgrade rather than by a test."""
    assert sorted(MIGRATIONS) == list(range(1, SCHEMA_VERSION + 1))
    assert max(MIGRATIONS) == SCHEMA_VERSION


def test_a_database_from_the_future_is_refused_rather_than_downgraded(
    tmp_path: Path,
) -> None:
    """The rollback rule, enforced instead of documented.

    An older build meeting a database a newer build has already migrated must
    stop. Running against it would mean writing rows whose shape it does not
    know, which is the one failure a backup cannot help with — so the safe
    rollback unit is the previous build *plus* its pre-upgrade snapshot.
    """
    from librairy.db import DatabaseVersionError

    path = tmp_path / "from-the-future.sqlite3"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for step in range(1, SCHEMA_VERSION + 1):
        conn.executescript(
            f"BEGIN;\n{MIGRATIONS[step]}\nPRAGMA user_version={step};\nCOMMIT;"
        )
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 5}")
    conn.commit()

    with pytest.raises(DatabaseVersionError) as refused:
        migrate(conn)

    assert "newer than this code supports" in str(refused.value)
    assert "refusing to write" in str(refused.value)


def test_a_failed_migration_leaves_the_pre_upgrade_copy_usable(
    tmp_path: Path,
) -> None:
    """Each migration is one transaction, so a step that fails leaves the
    database at the generation before it — not half way through one. What it
    does not give you is a way back from the steps that already succeeded,
    which is why the procedure is: copy first, then upgrade.
    """
    path = tmp_path / "will-fail.sqlite3"
    old = historical(path, 42)
    seed(old, 42)
    old.close()
    snapshot = tmp_path / "pre-upgrade.sqlite3"
    snapshot.write_bytes(path.read_bytes())

    broken = dict(MIGRATIONS)
    broken[43] = "CREATE TABLE this_is_not_valid_sql ((("
    reopened = sqlite3.connect(path)
    reopened.row_factory = sqlite3.Row
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("librairy.db.MIGRATIONS", broken)
        with pytest.raises(Exception):  # noqa: B017, PT011
            migrate(reopened)

    #  Stopped at the generation before the bad step, not inside it.
    assert user_version(reopened) == 42
    reopened.close()

    #  And the copy taken beforehand is exactly what it was.
    recovered = sqlite3.connect(snapshot)
    recovered.row_factory = sqlite3.Row
    assert user_version(recovered) == 42
    assert recovered.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    assert recovered.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# --------------------------------------------------------------------------
# 26-34: the recovery and rollback drill
# --------------------------------------------------------------------------


def meaningful(tmp_path: Path) -> tuple[Settings, sqlite3.Connection, dict]:
    """A small installation with one of everything worth losing."""
    from librairy.executor import execute_plan
    from librairy.format_policy import protect_folder
    from librairy.planner import OperationSpec, approve_plan, create_plan, utc_now
    from librairy.relationships import record as record_relationship
    from librairy.scanner import scan_root

    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath, body in {
        "Photos/Wedding/IMG_1.CR3": b"a raw original",
        "Photos/Wedding/IMG_1.JPG": b"its jpeg render",
        "Music/loose.flac": b"a recording",
        "Music/held.flac": b"one to set aside",
    }.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    scan_root(conn, "library", settings.library_dir, settings)

    def item(relpath: str) -> int:
        return int(
            conn.execute(
                "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
            ).fetchone()["id"]
        )

    record_relationship(
        conn,
        subject_item_id=item("Photos/Wedding/IMG_1.CR3"),
        companion_item_id=item("Photos/Wedding/IMG_1.JPG"),
        kind="raw_render",
        provenance="exif: same exposure",
    )
    protect_folder(conn, "Photos/Wedding", library_dir=settings.library_dir)
    conn.execute(
        "INSERT INTO decision_events(kind, signature, specificity, features, outcome,"
        " dest_relpath, decided_at, settled_at) VALUES ('destination', 'music|flac',"
        " 1, '{}', 'Music/Queen', 'Music/Queen/x.flac', ?, ?)",
        (utc_now(), utc_now()),
    )
    #  One decision that ran, so History and Quarantine have provenance.
    aside = create_plan(
        conn,
        [
            OperationSpec(
                op_type="quarantine",
                src_root="library",
                src_relpath="Music/held.flac",
                dest_root="quarantine",
                dest_relpath="2026-08-31/held.flac",
            )
        ],
        settings,
    )
    approve_plan(conn, aside, settings)
    execute_plan(conn, aside, settings)
    #  And one that has not, so a pending approval is part of the drill.
    waiting = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="library",
                src_relpath="Music/loose.flac",
                dest_root="library",
                dest_relpath="Music/Queen/loose.flac",
            )
        ],
        settings,
    )
    approve_plan(conn, waiting, settings)
    return settings, conn, {"aside": aside, "waiting": waiting}


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
        for table in (
            "history",
            "format_policy_scopes",
            "decision_events",
            "item_relationships",
            "quarantine_entries",
            "plans",
        )
    }


def test_a_snapshot_taken_before_an_upgrade_is_a_working_database(
    tmp_path: Path,
) -> None:
    """The whole rollback model rests on this being true, so it is the first
    thing checked rather than assumed."""
    from librairy.backup import snapshot_database

    settings, conn, _ = meaningful(tmp_path)
    before = counts(conn)

    snapshot = snapshot_database(settings, tmp_path / "snapshots" / "pre-upgrade.db")

    restored = sqlite3.connect(snapshot)
    restored.row_factory = sqlite3.Row
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert user_version(restored) == SCHEMA_VERSION
    assert counts(restored) == before


def test_an_imperfect_restore_is_explained_and_never_repaired(
    tmp_path: Path,
) -> None:
    """The realistic failure: the database came back from one moment and the
    files from another. Validation says what does not line up and changes
    nothing at all."""
    from librairy.restore_check import validate

    settings, conn, _ = meaningful(tmp_path)
    #  The filesystem moved on after the snapshot: one file rearranged by hand,
    #  one gone entirely.
    (settings.library_dir / "Music/Elsewhere").mkdir(parents=True, exist_ok=True)
    (settings.library_dir / "Music/loose.flac").rename(
        settings.library_dir / "Music/Elsewhere/loose.flac"
    )
    (settings.library_dir / "Photos/Wedding/IMG_1.JPG").unlink()
    from librairy.scanner import scan_root

    scan_root(conn, "library", settings.library_dir, settings)
    before = counts(conn)
    on_disk = sorted(
        path.relative_to(settings.library_dir).as_posix()
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    )

    report = validate(conn, settings)

    codes = {finding.code for finding in report.findings}
    assert "moved" in codes, "a rearranged file must not read as a loss"
    assert "missing" in codes
    assert counts(conn) == before, "validation changed the database"
    assert (
        sorted(
            path.relative_to(settings.library_dir).as_posix()
            for path in settings.library_dir.rglob("*")
            if path.is_file()
        )
        == on_disk
    )


def test_recovery_never_discards_what_a_person_decided(tmp_path: Path) -> None:
    """The distinction the whole recovery model turns on: a stale index is not
    the same thing as a decision you never made."""
    from librairy.restore_check import preserved, validate

    settings, conn, _ = meaningful(tmp_path)
    from librairy.scanner import scan_root

    (settings.library_dir / "Music/loose.flac").unlink()
    scan_root(conn, "library", settings.library_dir, settings)
    kept = dict(preserved(conn))

    validate(conn, settings)

    assert kept["committed operations"] >= 1
    assert kept["Format Policy scopes"] >= 1
    assert kept["remembered decisions"] == 1
    assert dict(preserved(conn)) == kept


def test_a_stale_measurement_is_never_attached_to_the_new_bytes(
    tmp_path: Path,
) -> None:
    from librairy.planner import utc_now
    from librairy.scanner import scan_root
    from librairy.tools.common import IMAGE_TOOL, get_cached_metadata, set_cached_metadata

    settings, conn, _ = meaningful(tmp_path)
    raw = settings.library_dir / "Photos/Wedding/IMG_1.CR3"
    item_id = int(
        conn.execute(
            "SELECT id FROM items WHERE relpath='Photos/Wedding/IMG_1.CR3'"
        ).fetchone()["id"]
    )
    original = str(
        conn.execute("SELECT fingerprint FROM items WHERE id=?", (item_id,)).fetchone()[
            "fingerprint"
        ]
    )
    set_cached_metadata(
        conn, item_id, original, IMAGE_TOOL, {"captured_at": "2024:06:01"}, utc_now()
    )

    #  A restore that brought back different bytes at the same path.
    raw.write_bytes(b"a different exposure entirely")
    scan_root(conn, "library", settings.library_dir, settings)
    current = str(
        conn.execute("SELECT fingerprint FROM items WHERE id=?", (item_id,)).fetchone()[
            "fingerprint"
        ]
    )

    assert current != original
    assert get_cached_metadata(conn, item_id, current, IMAGE_TOOL) is None
    from librairy.restore_check import validate

    assert "stale-measurements" in {f.code for f in validate(conn, settings).findings}


def test_rolling_back_means_the_previous_build_and_its_own_snapshot(
    tmp_path: Path,
) -> None:
    """The release contract, rehearsed.

    Upgrade, then do something. Rolling back is *not* pointing the previous
    build at the migrated database — that is refused. It is restoring the
    snapshot taken before the upgrade, which is a database the previous build
    understands, and accepting that what happened after the snapshot is not in
    it. The filesystem still holds those bytes, which is what reconciliation is
    for.
    """
    from librairy.backup import snapshot_database
    from librairy.db import DatabaseVersionError
    from librairy.restore_check import validate
    from librairy.scanner import scan_root

    settings, conn, plans = meaningful(tmp_path)
    snapshot = snapshot_database(settings, tmp_path / "snapshots" / "pre.db")
    before = counts(conn)

    #  After the snapshot: one more decision, carried out.
    from librairy.executor import execute_plan

    execute_plan(conn, plans["waiting"], settings)
    assert (settings.library_dir / "Music/Queen/loose.flac").is_file()
    assert counts(conn)["history"] > before["history"]
    conn.close()

    #  A previous build meeting a database migrated past what it knows: refused.
    live = database_path(settings)
    pretend_older = sqlite3.connect(live)
    pretend_older.row_factory = sqlite3.Row
    pretend_older.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    pretend_older.commit()
    with pytest.raises(DatabaseVersionError):
        migrate(pretend_older)
    pretend_older.close()

    #  The supported path: put the pre-upgrade snapshot back.
    live.write_bytes(snapshot.read_bytes())
    rolled_back = connect(settings)

    assert user_version(rolled_back) == SCHEMA_VERSION
    assert counts(rolled_back) == before
    #  And the disclosure this model owes the operator: the file that moved
    #  after the snapshot is still moved, and the restored database does not
    #  know about it. That is what reconciliation is for.
    assert (settings.library_dir / "Music/Queen/loose.flac").is_file()
    scan_root(conn := rolled_back, "library", settings.library_dir, settings)
    report = validate(conn, settings)
    assert "moved" in {finding.code for finding in report.findings}


def test_the_documented_rollback_never_tells_anybody_to_reuse_the_new_database(
    tmp_path: Path,  # noqa: ARG001
) -> None:
    """The one sentence a release must not contain."""
    docs = (ROOT / "docs/operations.md").read_text(encoding="utf-8").lower()

    assert "roll back" in docs or "rollback" in docs
    assert "pre-upgrade" in docs
    #  The claim that would make the whole model untrue.
    for wrong in (
        "just switch back to the old image",
        "simply start the previous image",
        "rollback is lossless",
    ):
        assert wrong not in docs


def test_the_documentation_keeps_the_four_words_apart(tmp_path: Path) -> None:  # noqa: ARG001
    """Backup, restore, reconcile and rollback are four different actions and
    the release notes may not use them as synonyms."""
    docs = (ROOT / "docs/operations.md").read_text(encoding="utf-8")

    for heading in ("## Backup", "## Restore", "## Reconcile", "## Roll back"):
        assert heading in docs, f"{heading} is missing from the operations guide"

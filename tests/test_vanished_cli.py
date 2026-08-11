"""`librairy vanished list` and `librairy vanished clear`.

The same lifecycle the Review page calls — `lifecycle.forget_vanished`, not a
second implementation. These run the real CLI in a subprocess against a
throwaway tree, so the wiring is exercised rather than mocked.

`list` doubles as the dry run: there is no --dry-run anywhere in this CLI to be
consistent with, and printing exactly the rows `clear` would act on is the same
answer under a name that already exists.

No AI provider, no catalog, no network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root


def env_for(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "APPDATA_DIR": str(tmp_path / "appdata"),
            "INBOX_DIR": str(tmp_path / "inbox"),
            "LIBRARY_DIR": str(tmp_path / "library"),
            "QUARANTINE_DIR": str(tmp_path / "quarantine"),
            "FILE_STABILITY_SECONDS": "0",
            # Belt and braces: nothing in this path reaches a provider, and if
            # something ever did the test would hang rather than pass quietly.
            "AI_ENABLED": "false",
        }
    )
    return env


def run_cli(tmp_path: Path, *args: str, as_json: bool = True):
    # --json before the subcommand, like every other CLI test: it is a global
    # flag, and a subcommand that redeclares it turns it back off.
    flags = ["--json"] if as_json else []
    return subprocess.run(
        [sys.executable, "-m", "librairy", *flags, *args],
        env=env_for(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def seed(conn, settings: Settings, relpath: str, *, root: str = "inbox", gone: bool = False) -> int:
    base = settings.inbox_dir if root == "inbox" else settings.library_dir
    path = base / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(relpath, encoding="utf-8")
    scan_root(conn, root, base, settings)
    item_id = conn.execute(
        "SELECT id FROM items WHERE root=? AND relpath=?", (root, relpath)
    ).fetchone()[0]
    upsert_proposal(
        conn,
        item_id=item_id,
        category="shows",
        clean_name=Path(relpath).name,
        dest_relpath=f"Shows/{Path(relpath).name}",
        confidence=0.82,
        evidence=[EvidenceEntry("tvmaze", "show", "Best Shot", 0.82)],
    )
    if gone:
        path.unlink()
        scan_root(conn, root, base, settings)
    return item_id


def fixture(tmp_path: Path):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    return conn, settings


# --- list -------------------------------------------------------------------


def test_list_is_empty_on_a_healthy_library(tmp_path: Path) -> None:
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "here.mkv")
    conn.commit()
    conn.close()

    result = run_cli(tmp_path, "vanished", "list")

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "clearable": 0,
        "already_resolved": 0,
        "entries": [],
    }


def test_list_reports_the_entries_and_their_state(tmp_path: Path) -> None:
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "_drop/gone.mkv", gone=True)
    conn.commit()
    conn.close()

    payload = json.loads(run_cli(tmp_path, "vanished", "list").stdout)

    assert payload["clearable"] == 1
    entry = payload["entries"][0]
    assert entry["root"] == "inbox"
    assert entry["relpath"] == "_drop/gone.mkv"
    assert entry["missing_since"]
    assert entry["state"] == "Waiting for review", "the label, not the raw status"
    assert entry["destination"].startswith("Would have been filed as: ")


def test_list_prints_no_host_path(tmp_path: Path) -> None:
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "gone.mkv", gone=True)
    conn.commit()
    conn.close()

    result = run_cli(tmp_path, "vanished", "list")

    assert str(settings.inbox_dir) not in result.stdout
    assert str(tmp_path) not in result.stdout


def test_list_scopes_by_root(tmp_path: Path) -> None:
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "inbox-gone.mkv", gone=True)
    seed(conn, settings, "Shows/lib-gone.mkv", root="library", gone=True)
    conn.commit()
    conn.close()

    everything = json.loads(run_cli(tmp_path, "vanished", "list").stdout)
    inbox = json.loads(run_cli(tmp_path, "vanished", "list", "--root", "inbox").stdout)

    assert everything["clearable"] == 2
    assert inbox["clearable"] == 1
    assert inbox["entries"][0]["relpath"] == "inbox-gone.mkv"


def test_list_separates_clearable_from_already_resolved(tmp_path: Path) -> None:
    """The 8-versus-7 case, on the command line."""
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "waiting.mkv", gone=True)
    rejected = seed(conn, settings, "rejected.mkv", gone=True)
    conn.execute("UPDATE proposals SET status='rejected' WHERE item_id=?", (rejected,))
    conn.commit()
    conn.close()

    payload = json.loads(run_cli(tmp_path, "vanished", "list").stdout)

    assert payload["clearable"] == 1
    assert payload["already_resolved"] == 1


def test_plain_output_is_one_line_per_entry(tmp_path: Path) -> None:
    """It used to be a Python repr of a list of dicts on a single line."""
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "one.mkv", gone=True)
    seed(conn, settings, "two.mkv", gone=True)
    conn.commit()
    conn.close()

    stdout = run_cli(tmp_path, "vanished", "list", as_json=False).stdout

    assert "clearable: 2" in stdout
    assert stdout.count("relpath=") == 2
    assert "[{" not in stdout


# --- clear ------------------------------------------------------------------


def test_clear_refuses_without_yes_and_says_what_it_would_do(tmp_path: Path) -> None:
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "gone.mkv", gone=True)
    conn.commit()
    conn.close()

    refused = run_cli(tmp_path, "vanished", "clear", "--root", "inbox")

    assert refused.returncode == 2
    payload = json.loads(refused.stdout)
    assert payload["error"] == "confirmation_required"
    assert payload["message"] == "vanished clear requires --yes"
    assert payload["would_clear"] == 1
    assert json.loads(run_cli(tmp_path, "vanished", "list").stdout)["clearable"] == 1


def test_clear_requires_a_root(tmp_path: Path) -> None:
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "gone.mkv", gone=True)
    conn.commit()
    conn.close()

    result = run_cli(tmp_path, "vanished", "clear", "--yes")

    assert result.returncode != 0
    assert "--root" in result.stderr


def test_clear_rejects_an_unknown_root(tmp_path: Path) -> None:
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "gone.mkv", gone=True)
    conn.commit()
    conn.close()

    result = run_cli(tmp_path, "vanished", "clear", "--root", "everything", "--yes")

    assert result.returncode != 0
    assert "invalid choice" in result.stderr


def test_clear_resolves_the_entries_and_deletes_nothing(tmp_path: Path) -> None:
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "here.mkv")
    seed(conn, settings, "gone.mkv", gone=True)
    conn.commit()
    conn.close()

    payload = json.loads(
        run_cli(tmp_path, "vanished", "clear", "--root", "inbox", "--yes").stdout
    )

    assert payload == {
        "cleared": 1,
        "root": "inbox",
        "files_deleted": 0,
        "records_deleted": 0,
    }
    conn = connect(settings_for(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE status='superseded'"
    ).fetchone()[0] == 1
    assert (settings.inbox_dir / "here.mkv").exists(), "the live file is still there"


def test_clear_scopes_by_root(tmp_path: Path) -> None:
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "inbox-gone.mkv", gone=True)
    seed(conn, settings, "Shows/lib-gone.mkv", root="library", gone=True)
    conn.commit()
    conn.close()

    run_cli(tmp_path, "vanished", "clear", "--root", "inbox", "--yes")
    remaining = json.loads(run_cli(tmp_path, "vanished", "list").stdout)

    assert remaining["clearable"] == 1
    assert remaining["entries"][0]["root"] == "library"


def test_clear_is_idempotent_and_succeeds_with_nothing_to_do(tmp_path: Path) -> None:
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "gone.mkv", gone=True)
    conn.commit()
    conn.close()

    first = run_cli(tmp_path, "vanished", "clear", "--root", "inbox", "--yes")
    second = run_cli(tmp_path, "vanished", "clear", "--root", "inbox", "--yes")
    empty = run_cli(tmp_path, "vanished", "clear", "--root", "library", "--yes")

    assert json.loads(first.stdout)["cleared"] == 1
    assert json.loads(second.stdout)["cleared"] == 0
    assert json.loads(empty.stdout)["cleared"] == 0
    assert first.returncode == second.returncode == empty.returncode == 0


def test_clear_cannot_touch_a_file_that_is_still_there(tmp_path: Path) -> None:
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "here.mkv")
    conn.commit()
    conn.close()

    payload = json.loads(
        run_cli(tmp_path, "vanished", "clear", "--root", "inbox", "--yes").stdout
    )

    assert payload["cleared"] == 0
    conn = connect(settings_for(tmp_path))
    assert conn.execute("SELECT status FROM proposals").fetchone()[0] == "proposed"


def test_clear_cannot_touch_a_file_that_came_back(tmp_path: Path) -> None:
    conn, settings = fixture(tmp_path)
    seed(conn, settings, "away.mkv", gone=True)
    conn.commit()
    conn.close()
    assert json.loads(run_cli(tmp_path, "vanished", "list").stdout)["clearable"] == 1

    (settings.inbox_dir / "away.mkv").write_text("back", encoding="utf-8")
    run_cli(tmp_path, "scan", "--root", "inbox")

    assert json.loads(run_cli(tmp_path, "vanished", "list").stdout)["clearable"] == 0
    payload = json.loads(
        run_cli(tmp_path, "vanished", "clear", "--root", "inbox", "--yes").stdout
    )
    assert payload["cleared"] == 0
    conn = connect(settings_for(tmp_path))
    assert conn.execute("SELECT status FROM proposals").fetchone()[0] == "proposed"


def test_the_cli_and_the_web_call_the_same_function(tmp_path: Path) -> None:
    """Not a mock: the CLI dispatch is asserted to reference the shared
    lifecycle function, so a second implementation cannot appear quietly."""
    import inspect

    from librairy import cli
    from librairy.lifecycle import forget_vanished
    from librairy.web import app as web_app

    assert cli.forget_vanished is forget_vanished
    assert web_app.forget_vanished is forget_vanished
    source = inspect.getsource(cli._vanished_command)
    assert "forget_vanished(conn, root=args.root)" in source
    assert "UPDATE proposals" not in source, "no second implementation"

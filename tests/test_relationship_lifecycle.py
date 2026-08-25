"""Relationship context surviving to the surfaces where files actually change.

LibrAIry knew these pairs on every page except the ones that matter. Browse
could say `Live Photo video of IMG_1234.HEIC`, and the last screen before the
disk changed described a file called `IMG_1234.MOV` and nothing else — so the
most important context disappeared at exactly the checkpoint it was for.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.relationships import LIVE_PHOTO, RAW_RENDER, SUBTITLE, record
from librairy.scanner import scan_root
from librairy.web.app import create_app


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


def client_for(tmp_path: Path) -> tuple[TestClient, sqlite3.Connection, Settings]:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def flat(text: str) -> str:
    """The page as one line, so an assertion is about words rather than wrapping."""
    return re.sub(r"\s+", " ", text)


def item_id(conn: sqlite3.Connection, root: str, relpath: str) -> int:
    row = conn.execute(
        "SELECT id FROM items WHERE root=? AND relpath=?", (root, relpath)
    ).fetchone()
    assert row is not None, f"{root}:{relpath} was never indexed"
    return int(row["id"])


def library_files(settings: Settings, names: dict[str, str]) -> None:
    for relpath, body in names.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def set_aside_plan(
    conn: sqlite3.Connection, settings: Settings, relpaths: list[str]
) -> str:
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="quarantine",
                src_root="library",
                src_relpath=relpath,
                dest_root="quarantine",
                dest_relpath=f"2026-08-25/{relpath}",
            )
            for relpath in relpaths
        ],
        settings,
    )
    #  One decision, several files — which is what every real set-aside is, and
    #  what puts the plan under `Set aside` on the Commit page rather than
    #  nowhere at all.
    conn.execute("UPDATE plans SET coherent=1 WHERE id=?", (plan_id,))
    approve_plan(conn, plan_id, settings)
    return plan_id


def a_live_photo(
    conn: sqlite3.Connection, settings: Settings, folder: str = "Photos/2024"
) -> tuple[int, int]:
    library_files(
        settings,
        {f"{folder}/IMG_1234.HEIC": "still", f"{folder}/IMG_1234.MOV": "motion"},
    )
    scan_root(conn, "library", settings.library_dir, settings)
    still = item_id(conn, "library", f"{folder}/IMG_1234.HEIC")
    motion = item_id(conn, "library", f"{folder}/IMG_1234.MOV")
    record(
        conn,
        companion_item_id=motion,
        subject_item_id=still,
        kind=LIVE_PHOTO,
        provenance="same Live Photo identifier 1B0F3A22",
    )
    return still, motion


def a_raw_pair(
    conn: sqlite3.Connection, settings: Settings, folder: str = "Photos/2024"
) -> tuple[int, int]:
    library_files(
        settings,
        {f"{folder}/IMG_5200.CR3": "raw", f"{folder}/IMG_5200.JPG": "render"},
    )
    scan_root(conn, "library", settings.library_dir, settings)
    raw = item_id(conn, "library", f"{folder}/IMG_5200.CR3")
    render = item_id(conn, "library", f"{folder}/IMG_5200.JPG")
    record(
        conn,
        companion_item_id=render,
        subject_item_id=raw,
        kind=RAW_RENDER,
        provenance="same camera and the same moment",
    )
    return raw, render


# --------------------------------------------------------------------------
# 14-17: the Commit card
# --------------------------------------------------------------------------


def test_commit_card_says_a_live_photo_is_being_separated(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    a_live_photo(conn, settings)
    set_aside_plan(conn, settings, ["Photos/2024/IMG_1234.MOV"])

    page = flat(client.get("/commit").text)

    assert "This will separate a Live Photo." in page
    assert "IMG_1234.MOV goes to Quarantine" in page
    assert "IMG_1234.HEIC stays in the library/Photos/2024" in page
    #  The invariant, stated on the card rather than only in a docstring.
    assert "Nothing extra is moved." in page


def test_commit_card_says_a_raw_and_its_render_are_being_separated(
    tmp_path: Path,
) -> None:
    client, conn, settings = client_for(tmp_path)
    a_raw_pair(conn, settings)
    set_aside_plan(conn, settings, ["Photos/2024/IMG_5200.JPG"])

    page = flat(client.get("/commit").text)

    assert "This will separate a RAW + JPEG pair." in page
    assert "1 RAW/JPEG pair will be split" in page


def test_commit_card_counts_a_large_group_rather_than_listing_it(
    tmp_path: Path,
) -> None:
    """A photo group can hold forty pairs. A card that renders them is not a card."""
    client, conn, settings = client_for(tmp_path)
    files: dict[str, str] = {}
    for number in range(1, 9):
        files[f"Photos/Card/IMG_{number:04d}.HEIC"] = f"still{number}"
        files[f"Photos/Card/IMG_{number:04d}.MOV"] = f"motion{number}"
    library_files(settings, files)
    scan_root(conn, "library", settings.library_dir, settings)
    for number in range(1, 9):
        record(
            conn,
            companion_item_id=item_id(
                conn, "library", f"Photos/Card/IMG_{number:04d}.MOV"
            ),
            subject_item_id=item_id(
                conn, "library", f"Photos/Card/IMG_{number:04d}.HEIC"
            ),
            kind=LIVE_PHOTO,
            provenance="same Live Photo identifier",
        )
    set_aside_plan(
        conn, settings, [f"Photos/Card/IMG_{n:04d}.MOV" for n in range(1, 9)]
    )

    page = flat(client.get("/commit").text)

    assert "8 Live Photos will be split" in page
    #  Three named, five counted — never eight paragraphs.
    assert page.count("This will separate a Live Photo.") == 3
    assert "and 5 more that would be separated" in page


def test_a_decision_that_keeps_a_pair_together_says_so(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    a_live_photo(conn, settings)
    set_aside_plan(
        conn,
        settings,
        ["Photos/2024/IMG_1234.HEIC", "Photos/2024/IMG_1234.MOV"],
    )

    page = flat(client.get("/commit").text)

    assert "Live Photo pair — both files move together." in page
    assert "This will separate a Live Photo." not in page


def test_a_subtitle_left_behind_is_named_on_the_commit_card(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    folder = "Movies/Arrival (2016)"
    library_files(
        settings,
        {
            f"{folder}/Arrival (2016).mkv": "film",
            f"{folder}/Arrival (2016).en.srt": "subs",
        },
    )
    scan_root(conn, "library", settings.library_dir, settings)
    record(
        conn,
        companion_item_id=item_id(conn, "library", f"{folder}/Arrival (2016).en.srt"),
        subject_item_id=item_id(conn, "library", f"{folder}/Arrival (2016).mkv"),
        kind=SUBTITLE,
        provenance="names the same file",
    )
    set_aside_plan(conn, settings, [f"{folder}/Arrival (2016).mkv"])

    page = flat(client.get("/commit").text)

    assert "This will separate a subtitle from its video." in page
    assert "Arrival (2016).en.srt stays in" in page


# --------------------------------------------------------------------------
# 18-21: Quarantine and restore
# --------------------------------------------------------------------------


def test_a_held_file_says_what_it_is_still_part_of(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    a_live_photo(conn, settings)
    plan_id = set_aside_plan(conn, settings, ["Photos/2024/IMG_1234.MOV"])
    execute_plan(conn, plan_id, settings)

    page = flat(client.get("/quarantine").text)

    assert "Live Photo still:" in page
    assert "library/Photos/2024/IMG_1234.HEIC" in page


def test_restoring_a_held_half_says_it_will_rejoin_the_other(
    tmp_path: Path,
) -> None:
    client, conn, settings = client_for(tmp_path)
    a_raw_pair(conn, settings)
    plan_id = set_aside_plan(conn, settings, ["Photos/2024/IMG_5200.JPG"])
    execute_plan(conn, plan_id, settings)

    page = flat(client.get("/quarantine").text)

    assert "RAW original:" in page
    assert "library/Photos/2024/IMG_5200.CR3" in page
    assert "Restore puts the two back together." in page


def test_a_whole_decision_says_what_it_is_made_of(tmp_path: Path) -> None:
    """Counts, not a regrouping. The restore boundary is still the plan."""
    client, conn, settings = client_for(tmp_path)
    files: dict[str, str] = {}
    for number in (1, 2, 3):
        files[f"Photos/Card/IMG_{number:04d}.HEIC"] = f"still{number}"
        files[f"Photos/Card/IMG_{number:04d}.MOV"] = f"motion{number}"
    for number in (7, 8):
        files[f"Photos/Card/DSC_{number:04d}.JPG"] = f"lonely{number}"
    library_files(settings, files)
    scan_root(conn, "library", settings.library_dir, settings)
    for number in (1, 2, 3):
        record(
            conn,
            companion_item_id=item_id(
                conn, "library", f"Photos/Card/IMG_{number:04d}.MOV"
            ),
            subject_item_id=item_id(
                conn, "library", f"Photos/Card/IMG_{number:04d}.HEIC"
            ),
            kind=LIVE_PHOTO,
            provenance="same Live Photo identifier",
        )
    plan_id = set_aside_plan(conn, settings, sorted(files))
    execute_plan(conn, plan_id, settings)

    page = flat(client.get("/quarantine").text)

    assert "3 Live Photos · 2 unrelated files" in page
    #  Still one decision, restored as one decision.
    assert "Restore all 8" in page


def test_a_pair_split_across_two_decisions_is_not_counted_as_one(
    tmp_path: Path,
) -> None:
    """Only pairs entirely inside a decision belong to it.

    A Live Photo whose still is still in the library is not something this
    restore is putting back together, and saying it was would promise an
    outcome the button does not produce.
    """
    client, conn, settings = client_for(tmp_path)
    a_live_photo(conn, settings)
    library_files(settings, {"Photos/2024/DSC_0001.JPG": "lonely"})
    scan_root(conn, "library", settings.library_dir, settings)
    plan_id = set_aside_plan(
        conn, settings, ["Photos/2024/IMG_1234.MOV", "Photos/2024/DSC_0001.JPG"]
    )
    execute_plan(conn, plan_id, settings)

    page = flat(client.get("/quarantine").text)

    assert "1 Live Photo" not in page
    assert "Live Photo still:" in page


# --------------------------------------------------------------------------
# 22-25: replacement, the delete queue, and undo
# --------------------------------------------------------------------------


def test_a_replacement_does_not_transfer_the_raw_pairing(tmp_path: Path) -> None:
    """The pairing belonged to the bytes being replaced.

    A JPEG paired with a RAW by capture metadata was paired because *those*
    bytes recorded that camera at that moment. A different export of the same
    photograph has to earn the pairing from its own metadata.
    """
    _, conn, settings = client_for(tmp_path)
    raw, render = a_raw_pair(conn, settings)
    library_files(settings, {"Photos/2024/IMG_5200-v2.JPG": "another export"})
    scan_root(conn, "library", settings.library_dir, settings)
    replacement = item_id(conn, "library", "Photos/2024/IMG_5200-v2.JPG")

    from librairy.relationship_impact import not_carried

    lines = not_carried(
        conn, replaced_item_id=render, replacing_item_id=replacement
    )

    assert len(lines) == 1
    assert "IMG_5200.CR3" in lines[0]
    assert "is its JPEG render" in lines[0]
    assert "not paired with it" in lines[0]
    assert conn.execute(
        "SELECT COUNT(*) FROM item_relationships WHERE low_item_id=? OR high_item_id=?",
        (replacement, replacement),
    ).fetchone()[0] == 0
    assert raw


def test_a_file_bound_for_the_delete_queue_says_what_is_related_to_it(
    tmp_path: Path,
) -> None:
    """Nothing is being separated — it left the library when it was set aside.

    "This still has a related file in your library" is the fact somebody about
    to queue it for deletion needs, and it is not a warning about a split.
    """
    client, conn, settings = client_for(tmp_path)
    a_live_photo(conn, settings)
    plan_id = set_aside_plan(conn, settings, ["Photos/2024/IMG_1234.MOV"])
    execute_plan(conn, plan_id, settings)
    entry = conn.execute("SELECT id FROM quarantine_entries").fetchone()
    csrf = client.get("/quarantine").cookies
    assert csrf is not None
    token = re.search(
        r'name="csrf_token" value="([^"]+)"', client.get("/quarantine").text
    )
    assert token is not None
    client.post(
        f"/quarantine/delete-queue/{entry['id']}",
        data={"csrf_token": token.group(1)},
    )

    page = flat(client.get("/commit").text)

    assert "This file has 1 related file in your library." in page
    assert "None of them are included in this decision." in page


def test_committing_a_split_leaves_the_other_half_exactly_where_it_was(
    tmp_path: Path,
) -> None:
    """The whole safety property, measured on the disk rather than in a page."""
    _, conn, settings = client_for(tmp_path)
    a_live_photo(conn, settings)
    plan_id = set_aside_plan(conn, settings, ["Photos/2024/IMG_1234.MOV"])

    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / "Photos/2024/IMG_1234.HEIC").is_file()
    assert not (settings.library_dir / "Photos/2024/IMG_1234.MOV").exists()
    #  And the relationship survives, because item identity did.
    assert conn.execute("SELECT COUNT(*) FROM item_relationships").fetchone()[0] == 1


def test_a_relationship_appearing_after_approval_stops_the_commit(
    tmp_path: Path,
) -> None:
    """Zero filesystem moves, not a best-effort partial one."""
    _, conn, settings = client_for(tmp_path)
    a_live_photo(conn, settings)
    plan_id = set_aside_plan(conn, settings, ["Photos/2024/IMG_1234.MOV"])
    motion = item_id(conn, "library", "Photos/2024/IMG_1234.MOV")
    library_files(settings, {"Photos/2024/IMG_1234.CR3": "raw"})
    scan_root(conn, "library", settings.library_dir, settings)
    record(
        conn,
        companion_item_id=motion,
        subject_item_id=item_id(conn, "library", "Photos/2024/IMG_1234.CR3"),
        kind=RAW_RENDER,
        provenance="same camera and the same moment",
    )

    summary = execute_plan(conn, plan_id, settings)

    assert summary.skipped_changed == 1
    assert summary.done == 0
    assert (settings.library_dir / "Photos/2024/IMG_1234.MOV").is_file()


def test_the_commit_page_explains_an_outdated_relationship(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    still, _ = a_live_photo(conn, settings)
    set_aside_plan(conn, settings, ["Photos/2024/IMG_1234.MOV"])
    (settings.library_dir / "Photos/2024/IMG_1234.HEIC").unlink()
    scan_root(conn, "library", settings.library_dir, settings)
    assert still

    page = flat(client.get("/commit").text)

    assert "Approval is outdated" in page
    assert "a related file is no longer there" in page


# --------------------------------------------------------------------------
# 5-6: the one interruption, and what it is not allowed to do
# --------------------------------------------------------------------------


def a_comparison(
    tmp_path: Path,
) -> tuple[TestClient, sqlite3.Connection, Settings, int]:
    """Two library photographs czkawka called similar, and their finding."""
    from librairy.audit import detect, gather, record_findings
    from librairy.planner import utc_now
    from librairy.similar_media import KIND

    client, conn, settings = client_for(tmp_path)
    raw, render = a_raw_pair(conn, settings)
    library_files(settings, {"Photos/2024/IMG_5200-alt.JPG": "another export"})
    scan_root(conn, "library", settings.library_dir, settings)
    other = item_id(conn, "library", "Photos/2024/IMG_5200-alt.JPG")
    first, second = sorted((render, other))
    conn.execute(
        "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id, kind,"
        " score, created_at) VALUES (?, ?, 'image', 0.95, ?)",
        (first, second, utc_now()),
    )
    view = gather(conn, settings, read_tags=False)
    record_findings(
        conn, [f for f in detect(view, conn=conn) if f.kind == KIND]
    )
    finding = conn.execute(
        "SELECT id FROM audit_findings WHERE kind=?", (KIND,)
    ).fetchone()
    assert finding is not None
    assert raw
    return client, conn, settings, int(finding["id"])


def csrf_of(client: TestClient, url: str = "/commit") -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', client.get(url).text)
    assert found is not None
    return found.group(1)


def test_a_decision_that_would_split_a_pair_is_shown_before_it_is_taken(
    tmp_path: Path,
) -> None:
    client, conn, settings, finding_id = a_comparison(tmp_path)
    assert settings

    answer = client.post(
        f"/review/audit/{finding_id}/comparison",
        data={
            "csrf_token": csrf_of(client),
            "keep": ["Photos/2024/IMG_5200-alt.JPG"],
        },
        follow_redirects=False,
    )

    page = flat(answer.text)
    assert answer.status_code == 200
    assert "This will separate a RAW + JPEG pair." in page
    assert "IMG_5200.JPG goes to Quarantine" in page
    assert "IMG_5200.CR3 stays in the library/Photos/2024" in page
    #  Nothing has happened yet. That is the whole difference between a
    #  warning and a report.
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_acknowledging_the_split_takes_the_decision_unchanged(
    tmp_path: Path,
) -> None:
    """Told, not overruled. Splitting a RAW from its render is normal."""
    client, conn, settings, finding_id = a_comparison(tmp_path)
    assert settings

    client.post(
        f"/review/audit/{finding_id}/comparison",
        data={
            "csrf_token": csrf_of(client),
            "keep": ["Photos/2024/IMG_5200-alt.JPG"],
            "acknowledge": "split",
        },
        follow_redirects=False,
    )

    sources = conn.execute(
        "SELECT src_relpath, dest_root FROM plan_ops ORDER BY seq"
    ).fetchall()
    #  Exactly the file that was chosen. Not the RAW as well.
    assert [row["src_relpath"] for row in sources] == ["Photos/2024/IMG_5200.JPG"]
    assert {row["dest_root"] for row in sources} == {"quarantine"}


def test_a_decision_that_separates_nothing_is_never_interrupted(
    tmp_path: Path,
) -> None:
    """Almost every decision. No extra screen, no extra click."""
    client, conn, settings, finding_id = a_comparison(tmp_path)
    assert settings
    conn.execute("DELETE FROM item_relationships")

    answer = client.post(
        f"/review/audit/{finding_id}/comparison",
        data={
            "csrf_token": csrf_of(client),
            "keep": ["Photos/2024/IMG_5200-alt.JPG"],
        },
        follow_redirects=False,
    )

    assert answer.status_code == 303
    assert conn.execute("SELECT COUNT(*) FROM plan_ops").fetchone()[0] == 1


# --------------------------------------------------------------------------
# The staged audit, still green and deliberately not extended
# --------------------------------------------------------------------------


def test_the_inbox_and_the_library_are_paired_by_one_rule(tmp_path: Path) -> None:
    """Two orchestrations, one evidence function — checked by behaviour.

    A second pairing implementation for arriving files is how the inbox
    eventually gets a *weaker* rule than the library, which is the exact
    failure this pairing was written to avoid. So the same near-miss must be
    refused on both sides of filing, and the same proof accepted on both.
    """
    from librairy.photo_pairs import pair
    from librairy.tools.common import IMAGE_TOOL, set_cached_metadata

    _, conn, settings = client_for(tmp_path)
    assert settings
    placed: dict[str, int] = {}
    for root, folder in (("library", "Photos/2024"), ("inbox", "CameraCard")):
        for name, identifier in (
            #  Proof on both sides: one identifier written into two files.
            ("IMG_1001.HEIC", "SHARED"),
            ("IMG_1001.MOV", "SHARED"),
            #  And the near miss on both sides: same stem, no identifier.
            ("IMG_5402.jpeg", ""),
            ("IMG_5402.MOV", ""),
        ):
            cursor = conn.execute(
                "INSERT INTO items(root, relpath, size, mtime_ns, fingerprint,"
                " state, first_seen_at, last_seen_at)"
                " VALUES (?, ?, 10, 1, ?, 'proposed', 'now', 'now')",
                (root, f"{folder}/{name}", f"fp:{root}:{name}"),
            )
            placed[f"{root}:{name}"] = int(cursor.lastrowid)
            set_cached_metadata(
                conn, int(cursor.lastrowid), f"fp:{root}:{name}", IMAGE_TOOL,
                {
                    "width": 1, "height": 1,
                    "taken": "2024:08:01 10:00:00" if identifier else
                             ("2024:08:01 10:00:00" if name.endswith("jpeg")
                              else "2024:08:01 10:00:20"),
                    "camera": "Apple iPhone 15 Pro",
                    "content_id": identifier, "unique_id": "",
                },
                "now",
            )

    assert pair(conn, roots=("inbox",)) == 1
    assert pair(conn, roots=("library",)) == 1

    kinds = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM item_relationships GROUP BY kind"
    ).fetchall()
    assert [(row["kind"], row["n"]) for row in kinds] == [(LIVE_PHOTO, 2)]

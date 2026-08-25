"""Existing decisions consulting the policy, rather than each inventing one.

A Settings page nothing reads is a Settings page that lies. These are the
assertions that the resolver is actually the thing comparisons, replacements
and Storage Optimization ask — and that the answer it gives them is bounded by
the authority model: policy under safety, learned habits under policy.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.format_policy import protect_folder, set_preferred_format
from librairy.scanner import scan_root
from librairy.web.app import create_app

ALBUM = "Music/Rock/Queen/A Night at the Opera"
KEEPSAKES = "Music/Family Recordings"
WEDDING = "Photos/Wedding"


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


def library(tmp_path: Path, files: dict[str, bytes]):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def item_id(conn: sqlite3.Connection, relpath: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
        ).fetchone()["id"]
    )


def flag(conn: sqlite3.Connection, left: str, right: str, kind: str = "audio") -> None:
    from librairy.planner import utc_now

    first, second = sorted((item_id(conn, left), item_id(conn, right)))
    conn.execute(
        "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id, kind,"
        " score, created_at) VALUES (?, ?, ?, 0.95, ?)",
        (first, second, kind, utc_now()),
    )


def finding_for(conn: sqlite3.Connection, settings: Settings):
    from librairy.audit import detect, gather, record_findings
    from librairy.similar_media import KIND

    record_findings(
        conn,
        [
            found
            for found in detect(gather(conn, settings, read_tags=False), conn=conn)
            if found.kind == KIND
        ],
    )
    return conn.execute(
        "SELECT * FROM audit_findings WHERE kind=?", (KIND,)
    ).fetchone()


def two_encodes(tmp_path: Path, folder: str = ALBUM):
    conn, settings = library(
        tmp_path,
        {
            f"{folder}/01 - Song.flac": b"F" * 3000,
            f"{folder}/01 - Song.mp3": b"M" * 700,
        },
    )
    flag(conn, f"{folder}/01 - Song.flac", f"{folder}/01 - Song.mp3")
    return conn, settings


def an_optimization_job(
    conn: sqlite3.Connection, relpath: str, *, state: str
) -> None:
    """One job row, with the opportunity it came from.

    Written by hand rather than run through the encoder: this is a test about
    the policy gate, and a real FLAC encode would be testing ffmpeg.
    """
    from librairy.planner import utc_now

    now = utc_now()
    conn.execute(
        "INSERT INTO optimization_opportunities(id, item_id, relpath, kind, quality,"
        " status, detected_at, updated_at)"
        " VALUES (1, ?, ?, 'audio-to-flac', 'lossless', 'open', ?, ?)",
        (item_id(conn, relpath), relpath, now, now),
    )
    conn.execute(
        "INSERT INTO optimization_jobs(id, opportunity_id, item_id, relpath, kind,"
        " quality, preset, state, queued_at, updated_at)"
        " VALUES (1, 1, ?, ?, 'audio-to-flac', 'lossless', 'flac', ?, ?, ?)",
        (item_id(conn, relpath), relpath, state, now, now),
    )


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


# --------------------------------------------------------------------------
# 33-36: the comparison surfaces
# --------------------------------------------------------------------------


def test_the_music_comparison_still_prefers_mp3(tmp_path: Path) -> None:
    """Unchanged behaviour, different source. This is the regression."""
    from librairy.similar_media import compare

    conn, settings = two_encodes(tmp_path)

    view = compare(conn, settings, finding_for(conn, settings), measure=False)

    assert view.preferred == f"{ALBUM}/01 - Song.mp3"


def test_the_preference_comes_from_the_central_resolver(tmp_path: Path) -> None:
    """Changed in one place, and the comparison follows.

    If the comparison had kept its own copy this would pass with the old value.
    """
    from librairy.similar_media import compare

    conn, settings = two_encodes(tmp_path)
    set_preferred_format(conn, "music", "flac")

    view = compare(conn, settings, finding_for(conn, settings), measure=False)

    assert view.preferred == f"{ALBUM}/01 - Song.flac"


def test_clearing_the_preference_leaves_the_comparison_neutral(
    tmp_path: Path,
) -> None:
    from librairy.similar_media import compare

    conn, settings = two_encodes(tmp_path)
    set_preferred_format(conn, "music", "")

    view = compare(conn, settings, finding_for(conn, settings), measure=False)

    assert view.preferred == ""


def test_two_photographs_get_no_preferred_badge(tmp_path: Path) -> None:
    """No photo representation preference exists, so none is shown."""
    from librairy.similar_media import compare

    conn, settings = library(
        tmp_path,
        {
            "Photos/2024/IMG_1.heic": b"H" * 3000,
            "Photos/2024/IMG_1.jpg": b"J" * 900,
        },
    )
    flag(conn, "Photos/2024/IMG_1.heic", "Photos/2024/IMG_1.jpg", kind="image")

    view = compare(conn, settings, finding_for(conn, settings), measure=False)

    assert view.preferred == ""


# --------------------------------------------------------------------------
# 37-39: protection blocks a representation decision and nothing else
# --------------------------------------------------------------------------


def test_a_protected_original_is_not_offered_as_the_one_to_set_aside(
    tmp_path: Path,
) -> None:
    from librairy.similar_media import compare

    conn, settings = two_encodes(tmp_path, folder=KEEPSAKES)
    protect_folder(conn, KEEPSAKES, library_dir=settings.library_dir)

    view = compare(conn, settings, finding_for(conn, settings), measure=False)

    #  Nothing preselected: preferring one of these is the first half of
    #  setting the other aside, and the other is the thing that was protected.
    assert view.preferred == ""
    assert all(member.protected for member in view.members)


def test_setting_a_protected_file_aside_is_blocked_not_warned_about(
    tmp_path: Path,
) -> None:
    """Blocked, because "are you sure?" over a keepsake is a dialog somebody
    dismisses. The policy is the owner's own explicit instruction."""
    from librairy.corrections import CorrectionRefused
    from librairy.similar_media import resolve

    conn, settings = two_encodes(tmp_path, folder=KEEPSAKES)
    protect_folder(conn, KEEPSAKES, library_dir=settings.library_dir)
    finding = finding_for(conn, settings)

    with pytest.raises(CorrectionRefused, match="protected by your Format Policy"):
        resolve(conn, settings, int(finding["id"]), [f"{KEEPSAKES}/01 - Song.mp3"])

    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert (settings.library_dir / KEEPSAKES / "01 - Song.flac").is_file()


def test_a_protected_photograph_cannot_be_set_aside_from_a_group(
    tmp_path: Path,
) -> None:
    from librairy.corrections import CorrectionRefused
    from librairy.similar_media import resolve

    conn, settings = library(
        tmp_path,
        {
            f"{WEDDING}/IMG_1.CR3": b"R" * 4000,
            f"{WEDDING}/IMG_1.jpg": b"J" * 900,
        },
    )
    flag(conn, f"{WEDDING}/IMG_1.CR3", f"{WEDDING}/IMG_1.jpg", kind="image")
    protect_folder(conn, WEDDING, library_dir=settings.library_dir)
    finding = finding_for(conn, settings)

    with pytest.raises(CorrectionRefused, match="protected by your Format Policy"):
        resolve(conn, settings, int(finding["id"]), [f"{WEDDING}/IMG_1.jpg"])


def test_protection_does_not_stop_the_file_being_filed_or_organised(
    tmp_path: Path,
) -> None:
    """The distinction that keeps this from becoming a second permissions system.

    You may well want a RAW wedding photograph moved into the right folder.
    What you do not want is an optimization or a format preference deciding the
    RAW is the dispensable one.
    """
    from librairy.executor import execute_plan
    from librairy.planner import OperationSpec, approve_plan, create_plan

    conn, settings = library(tmp_path, {f"{WEDDING}/IMG_1.CR3": b"R" * 400})
    protect_folder(conn, WEDDING, library_dir=settings.library_dir)
    (settings.library_dir / WEDDING / "2019").mkdir()

    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="library",
                src_relpath=f"{WEDDING}/IMG_1.CR3",
                dest_root="library",
                dest_relpath=f"{WEDDING}/2019/IMG_1.CR3",
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1
    assert (settings.library_dir / WEDDING / "2019" / "IMG_1.CR3").is_file()


def test_a_document_comparison_stays_neutral(tmp_path: Path) -> None:
    """EPUB against PDF is a question the owner answers each time."""
    from librairy.similar_media import compare

    conn, settings = library(
        tmp_path,
        {
            "Documents/Manuals/R7000.epub": b"E" * 900,
            "Documents/Manuals/R7000.pdf": b"P" * 3000,
        },
    )
    flag(conn, "Documents/Manuals/R7000.epub", "Documents/Manuals/R7000.pdf")

    view = compare(conn, settings, finding_for(conn, settings), measure=False)

    assert view.preferred == ""


# --------------------------------------------------------------------------
# 40-41: Storage Optimization, gated and otherwise untouched
# --------------------------------------------------------------------------


def test_an_opportunity_in_a_format_policy_folder_cannot_be_queued(
    tmp_path: Path,
) -> None:
    """The eligibility gate, end to end.

    A Format Policy folder says its originals are not to be traded away, and
    replacing one with a smaller encode is exactly that trade. The opportunity
    is still recorded and still shown — what it cannot become is a job.
    """
    from librairy import optimization_queue as queue
    from librairy.format_policy import protecting
    from librairy.planner import utc_now

    conn, settings = library(tmp_path, {f"{KEEPSAKES}/Grandad.wav": b"W" * 500})
    protect_folder(conn, KEEPSAKES, library_dir=settings.library_dir)
    relpath = f"{KEEPSAKES}/Grandad.wav"
    now = utc_now()
    conn.execute(
        """
        INSERT INTO optimization_opportunities(
          id, item_id, root, relpath, kind, quality, current_bytes, estimated_bytes,
          summary, reason, compute, from_label, to_label, protected_by, facts,
          fingerprint, rule_version, status, detected_at, updated_at
        ) VALUES (1, ?, 'library', ?, 'audio-to-flac', 'lossless', 1000, 500,
                  '', '', 'low', 'WAV', 'FLAC', ?, '[]', 'fp', 1, 'open', ?, ?)
        """,
        (item_id(conn, relpath), relpath, protecting(conn, relpath), now, now),
    )

    with pytest.raises(queue.QueueRefused, match="protected"):
        queue.enqueue(conn, 1)

    assert conn.execute("SELECT COUNT(*) FROM optimization_jobs").fetchone()[0] == 0


def test_both_optimization_gates_ask_the_one_resolver(tmp_path: Path) -> None:
    """Advisory time and the moment of action, from one function.

    Two copies of "is this protected" is how a folder ends up protected in the
    queue and not in the adoption. Asserted structurally, because the failure
    is silent.
    """
    import ast

    import librairy.optimization as advisory
    import librairy.optimization_preflight as preflight

    for module in (advisory, preflight):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        names = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "librairy.format_policy" in names, module.__name__
    assert tmp_path


def test_the_optimization_gate_asks_the_central_resolver(
    tmp_path: Path,
) -> None:
    """A folder protected through Format Policy stops an adoption, exactly as a
    protected root does — checked again at the moment of action, because a
    folder can be protected after an opportunity appears."""
    from librairy.format_policy import protecting

    conn, settings = library(tmp_path, {f"{KEEPSAKES}/Grandad.wav": b"W" * 500})
    assert protecting(conn, f"{KEEPSAKES}/Grandad.wav") == ""

    protect_folder(conn, KEEPSAKES, library_dir=settings.library_dir)

    assert protecting(conn, f"{KEEPSAKES}/Grandad.wav") == KEEPSAKES


def test_a_committed_optimization_is_not_rewritten_by_a_new_policy(
    tmp_path: Path,
) -> None:
    """History is what happened. A policy set today does not unmake it."""
    conn, settings = library(tmp_path, {f"{KEEPSAKES}/Grandad.wav": b"W" * 500})
    an_optimization_job(conn, f"{KEEPSAKES}/Grandad.wav", state="adopted")

    protect_folder(conn, KEEPSAKES, library_dir=settings.library_dir)

    row = conn.execute("SELECT state FROM optimization_jobs WHERE id=1").fetchone()
    assert row["state"] == "adopted"


# --------------------------------------------------------------------------
# 42-45: the authority model, and what policy still may not do
# --------------------------------------------------------------------------


def test_a_learned_habit_never_competes_with_an_explicit_policy(
    tmp_path: Path,
) -> None:
    """Decision Memory says "I have noticed what you usually do".
    Format Policy says "you told me what you prefer". The instruction wins.
    """
    from librairy.decisions import learned, record_representation

    conn, _ = library(tmp_path, {f"{ALBUM}/01 - Song.flac": b"F" * 10})
    for _ in range(4):
        record_representation(
            conn, category="music", formats=["flac", "mp3"], kept=["flac"], settled=True
        )

    patterns = learned(conn)
    found = next(item for item in patterns if item["outcome"] == "flac")

    #  Still recorded, still true, still shown — and shown as something the
    #  policy has already answered rather than as a competing recommendation.
    assert found["support"] == 4
    assert found["policy"] == (
        "Your Format Policy answers this: MP3 is your preferred Music format."
    )


def test_a_habit_about_a_category_with_no_policy_is_left_alone(
    tmp_path: Path,
) -> None:
    """Subordinate to policy is not the same as suppressed by it."""
    from librairy.decisions import learned, record_representation

    conn, _ = library(tmp_path, {"Photos/2024/IMG_1.jpg": b"J" * 10})
    for _ in range(4):
        record_representation(
            conn, category="photos", formats=["heic", "jpg"], kept=["jpg"], settled=True
        )

    found = next(item for item in learned(conn) if item["outcome"] == "jpg")

    assert found["policy"] == ""


def test_setting_a_policy_creates_no_approval_and_no_operation(
    tmp_path: Path,
) -> None:
    """The whole safety property. Policy is input to workflows, not a workflow."""
    conn, settings = two_encodes(tmp_path)
    before = sorted(path.name for path in (settings.library_dir / ALBUM).iterdir())

    set_preferred_format(conn, "music", "flac")
    protect_folder(conn, ALBUM, library_dir=settings.library_dir)
    from librairy.format_impact import analyse

    analyse(conn, settings)

    for table in ("plans", "plan_ops", "proposals", "optimization_jobs",
                  "optimization_opportunities", "quarantine_entries", "history"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
    assert sorted(path.name for path in (settings.library_dir / ALBUM).iterdir()) == before


def test_no_transcoding_was_introduced(tmp_path: Path) -> None:
    """Format Policy is not Storage Optimization, and must not grow into it.

    Asserted structurally, because the failure mode is a helpful import added
    later by somebody who thought a policy that can name a format ought to be
    able to produce one.
    """
    import ast

    import librairy.format_impact as impact
    import librairy.format_policy as policy

    for module in (policy, impact):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        names = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        names |= {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        for forbidden in (
            "librairy.optimization_exec",
            "librairy.optimization_process",
            "librairy.optimization_queue",
            "subprocess",
        ):
            assert forbidden not in names, f"{module.__name__} imports {forbidden}"


# --------------------------------------------------------------------------
# 46-55: the Settings surface
# --------------------------------------------------------------------------


def client_for(tmp_path: Path) -> tuple[TestClient, sqlite3.Connection, Settings]:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def csrf_of(client: TestClient) -> str:
    found = re.search(
        r'name="csrf_token" value="([^"]+)"',
        client.get("/settings/format-policy").text,
    )
    assert found is not None
    return found.group(1)


def test_the_format_policy_page_renders_the_current_state(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    page = flat(client.get("/settings/format-policy").text)

    assert "MP3 is your preferred existing representation." in page
    #  Three of the four ship neutral, and the page says so in words rather
    #  than with an empty control.
    assert page.count("No format preference is configured.") == 3


def test_changing_the_preferred_music_format_persists(tmp_path: Path) -> None:
    from librairy.format_policy import preferred_for

    client, conn, _ = client_for(tmp_path)

    client.post(
        "/settings/format-policy/preferred",
        data={"csrf_token": csrf_of(client), "category": "music", "preferred": "flac"},
    )

    assert preferred_for(conn, "music") == "flac"
    assert "FLAC is your preferred existing representation." in flat(
        client.get("/settings/format-policy").text
    )


def test_adding_and_removing_a_protected_folder(tmp_path: Path) -> None:
    from librairy.format_policy import protected_folders

    client, conn, settings = client_for(tmp_path)
    (settings.library_dir / "Photos" / "Wedding").mkdir(parents=True)

    client.post(
        "/settings/format-policy/protect",
        data={"csrf_token": csrf_of(client), "folder": "Photos/Wedding"},
    )
    assert protected_folders(conn) == ["Photos/Wedding"]

    client.post(
        "/settings/format-policy/unprotect",
        data={"csrf_token": csrf_of(client), "folder": "Photos/Wedding"},
    )
    assert protected_folders(conn) == []


def test_a_folder_outside_the_library_is_refused_on_the_page(
    tmp_path: Path,
) -> None:
    from librairy.format_policy import protected_folders

    client, conn, _ = client_for(tmp_path)

    answer = client.post(
        "/settings/format-policy/protect",
        data={"csrf_token": csrf_of(client), "folder": "../../etc"},
    )

    assert answer.status_code == 200
    #  Refused with the reason on the page, and nothing saved. The exact
    #  wording belongs to the containment check every path in LibrAIry shares.
    assert "error-note" in answer.text
    assert protected_folders(conn) == []


def test_the_two_kinds_of_protection_are_told_apart(tmp_path: Path) -> None:
    """A protected root stops a folder being queued at all; a Format Policy
    folder stops its originals being traded away. One word for both would leave
    somebody unable to tell which power they had configured."""
    from librairy.protected import set_protected_roots

    client, conn, settings = client_for(tmp_path)
    (settings.library_dir / "Photos" / "Wedding").mkdir(parents=True)
    protect_folder(conn, "Photos/Wedding", library_dir=settings.library_dir)
    set_protected_roots(conn, ["Photos/Memories"])

    page = flat(client.get("/settings/format-policy").text)

    assert "Preserve originals — No format preference or optimization may trade" in page
    assert "Protected root — Nothing in here may be queued for change at all." in page
    assert "LibrAIry can still index, search, organise and file these files" in page


def test_looking_at_the_page_measures_nothing(tmp_path: Path) -> None:
    """The analysis walks the whole index. A GET that did that would get slower
    the more somebody owned."""
    from librairy.format_impact import last

    client, conn, _ = client_for(tmp_path)

    assert client.get("/settings/format-policy").status_code == 200

    #  Nothing measured, so nothing stored. `setup` writes its own settings
    #  rows, which is why this asks about the report rather than the table.
    assert last(conn) is None


def test_the_impact_result_survives_a_reload(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    album = settings.library_dir / ALBUM
    album.mkdir(parents=True)
    (album / "01 - Song.flac").write_bytes(b"F" * 3000)
    (album / "01 - Song.mp3").write_bytes(b"M" * 700)
    scan_root(conn, "library", settings.library_dir, settings)

    client.post(
        "/settings/format-policy/analyse", data={"csrf_token": csrf_of(client)}
    )

    page = flat(client.get("/settings/format-policy").text)
    assert "already exist in MP3 and another format" in page
    assert "Last measured" in page

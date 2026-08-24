"""Thirty-seven photographs that look alike, and one decision about them.

    Photos/2024/Backyard/IMG_5100.jpg … IMG_5136.jpg

`similar_media` grew up on a FLAC beside an MP3, where the answer comes from a
table of six measured numbers with two rows in it. A phone burst is the same
*evidence* and a different *decision*, and the old code handled the difference
by refusing to write a finding at all past eight members — so thirty-seven
files nobody wanted twice were invisible.

These tests are about three things, in this order: that a large group exists at
all; that looking at one stays bounded whether it holds twenty-five members or
five hundred; and that resolving it is the decision that already worked, with a
longer list in it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.audit import record_findings
from librairy.config import Settings
from librairy.corrections import CorrectionRefused, undo_correction
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.photo_group import (
    KEEP,
    SET_ASIDE,
    approve,
    choices,
    choose,
    forget,
    is_large,
    load,
    size_of,
)
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.similar_media import KIND, PAGE_MEMBERS, SMALL_GROUP, detect
from librairy.web.commit_queue import queue_rows

FOLDER = "Photos/2024/Backyard"


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def burst(tmp_path: Path, count: int, *, exact: int = 0, suffix: str = "jpg"):
    """A group of `count` near-identical photos, `exact` of them byte-identical.

    Small files with distinct bytes: the members come from czkawka pairs, and
    a fixture that wrote real photographs would prove ffmpeg rather than the
    grouping. `exact` copies share bytes so the exact-subgroup rule has
    something true to find.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for number in range(count):
        path = settings.library_dir / FOLDER / f"IMG_{5100 + number}.{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "the same picture" if number < exact else f"picture {number}"
        path.write_text(body, encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    ids = [
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM items WHERE root='library' ORDER BY relpath"
        )
    ]
    #  A star, not a clique: czkawka pairs what it pairs, and the component is
    #  what joins them. One edge per member is the sparsest shape that still
    #  makes them one group, which is also the shape a real burst produces.
    for other in ids[1:]:
        first, second = sorted((ids[0], other))
        conn.execute(
            "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id,"
            " kind, score, created_at) VALUES (?, ?, 'image', 0.97, ?)",
            (first, second, utc_now()),
        )
    return conn, settings


def finding_row(conn):
    record_findings(conn, detect(conn))
    return conn.execute(
        "SELECT * FROM audit_findings WHERE kind=?", (KIND,)
    ).fetchone()


def no_tags(*_args, **_kwargs):
    """Stand in for exiftool: a fixture's text files have no metadata."""
    return []


# --- 1-4: a large group exists ------------------------------------------------


def test_a_group_larger_than_eight_is_no_longer_dropped(tmp_path: Path) -> None:
    """The bug this pass exists for. Nine files, and the row simply was not there."""
    conn, _ = burst(tmp_path, SMALL_GROUP + 1)

    findings = detect(conn)

    assert len(findings) == 1
    assert findings[0].kind == KIND


@pytest.mark.parametrize("count", [2, 8, 25, 100])
def test_groups_of_every_size_produce_exactly_one_finding(
    tmp_path: Path, count: int
) -> None:
    conn, settings = burst(tmp_path, count)

    findings = detect(conn)
    row = finding_row(conn)

    assert len(findings) == 1
    assert size_of(conn, row) == count


def test_a_five_hundred_member_group_is_visible(tmp_path: Path) -> None:
    conn, _ = burst(tmp_path, 500)

    row = finding_row(conn)

    assert row is not None
    assert size_of(conn, row) == 500
    assert is_large(conn, row) is True


def test_the_summary_says_what_a_large_group_is(tmp_path: Path) -> None:
    """Not "the same image as 36 others, encoded differently" — that is a claim
    about encoding that thirty-seven afternoon photographs do not support."""
    conn, _ = burst(tmp_path, 37)

    summary = detect(conn)[0].summary

    assert summary == "37 photos here look alike."


def test_the_evidence_does_not_list_five_hundred_paths(tmp_path: Path) -> None:
    """A finding is a row, not a manifest. The members are on the group's page."""
    conn, _ = burst(tmp_path, 500)

    evidence = detect(conn)[0].evidence

    assert len(evidence) <= 8
    assert any("more" in str(entry.detail) for entry in evidence)


def test_a_small_group_is_untouched(tmp_path: Path) -> None:
    """The two-file comparison keeps its own words and its own shape."""
    conn, _ = burst(tmp_path, 2)

    summary = detect(conn)[0].summary

    assert "encoded differently" in summary
    assert is_large(conn, finding_row(conn)) is False


# --- 5-9: bounded ------------------------------------------------------------


def test_one_page_of_a_large_group_is_one_page(tmp_path: Path) -> None:
    conn, settings = burst(tmp_path, 500)
    row = finding_row(conn)

    group = load(conn, settings, row, measure=False)

    assert group.total == 500
    assert len(group.members) == PAGE_MEMBERS
    assert group.has_next is True
    assert group.pages == -(-500 // PAGE_MEMBERS)


def test_paging_is_deterministic_and_covers_the_group(tmp_path: Path) -> None:
    conn, settings = burst(tmp_path, 100)
    row = finding_row(conn)

    seen: list[int] = []
    for page in range(1, group_pages(conn, settings, row) + 1):
        seen += [
            photo.item_id
            for photo in load(conn, settings, row, page=page, measure=False).members
        ]

    assert len(seen) == 100
    assert len(set(seen)) == 100
    #  And the same page twice is the same page: a tick placed on page 2 must
    #  not be sitting on a different photograph after a reload.
    first = [p.item_id for p in load(conn, settings, row, page=2, measure=False).members]
    again = [p.item_id for p in load(conn, settings, row, page=2, measure=False).members]
    assert first == again


def group_pages(conn, settings, row) -> int:
    return load(conn, settings, row, measure=False).pages


def test_drawing_a_page_never_runs_exiftool(tmp_path: Path, monkeypatch) -> None:
    """Analysis measures; pages read. This used to spawn one subprocess per
    page render, which is bounded and still the wrong side of the line."""
    from librairy.tools import exiftool

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a page render must not run exiftool")

    monkeypatch.setattr(exiftool, "extract_many", forbidden)
    conn, settings = burst(tmp_path, 500)
    row = finding_row(conn)

    group = load(conn, settings, row, measure=True)

    assert len(group.members) == PAGE_MEMBERS
    assert all(photo.facts == () for photo in group.members)


def test_measuring_reads_the_page_in_one_call(tmp_path: Path, monkeypatch) -> None:
    """One subprocess for the batch, not one per photograph, and never one per
    member of the group."""
    from librairy.photo_group import measure
    from librairy.tools import exiftool

    calls: list[int] = []

    def once(paths, _settings):  # noqa: ANN001, ANN202
        calls.append(len(paths))
        return [None] * len(paths)

    monkeypatch.setattr(exiftool, "extract_many", once)
    conn, settings = burst(tmp_path, 500)
    row = finding_row(conn)
    page = load(conn, settings, row, measure=False)

    measure(conn, settings, list(page.members))

    assert calls == [PAGE_MEMBERS]


def test_measured_photos_show_their_facts_from_the_cache(
    tmp_path: Path, monkeypatch
) -> None:
    from librairy.photo_group import measure
    from librairy.tools import exiftool
    from librairy.tools.exiftool import ImageMetadata

    def fake(paths, _settings):  # noqa: ANN001, ANN202
        return [
            ImageMetadata(
                tags={"ImageWidth": 4032, "ImageHeight": 3024},
                created_at="2024:06:18 14:32:01",
                camera="Apple iPhone 15",
            )
            for _ in paths
        ]

    monkeypatch.setattr(exiftool, "extract_many", fake)
    conn, settings = burst(tmp_path, 12)
    row = finding_row(conn)
    measure(conn, settings, list(load(conn, settings, row, measure=False).members))

    #  And now, with nothing allowed to run, the page shows what was measured.
    monkeypatch.setattr(
        exiftool, "extract_many",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no")),
    )
    group = load(conn, settings, row, measure=True)

    facts = dict(group.members[0].facts)
    assert facts["Pixels"] == "4032×3024"
    assert facts["Taken"] == "2024:06:18 14:32:01"
    assert facts["Camera"] == "Apple iPhone 15"


def test_a_re_edited_photo_loses_its_cached_measurements(tmp_path: Path, monkeypatch) -> None:
    """The fingerprint gate, on the photo side."""
    from librairy.photo_group import measure
    from librairy.tools import exiftool
    from librairy.tools.exiftool import ImageMetadata

    monkeypatch.setattr(
        exiftool, "extract_many",
        lambda paths, _s: [ImageMetadata(tags={"ImageWidth": 100, "ImageHeight": 50})
                           for _ in paths],
    )
    conn, settings = burst(tmp_path, 12)
    row = finding_row(conn)
    members = list(load(conn, settings, row, measure=False).members)
    measure(conn, settings, members)

    (settings.library_dir / members[0].relpath).write_text("edited", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)

    monkeypatch.setattr(
        exiftool, "extract_many",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no")),
    )
    group = load(conn, settings, row, measure=True)
    changed = next(p for p in group.members if p.relpath == members[0].relpath)

    assert changed.facts == ()


def test_drawing_a_group_asks_no_model_and_no_network(
    tmp_path: Path, monkeypatch
) -> None:
    """A comparison you cannot run offline is not this program's comparison."""
    import urllib.request

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a comparison must not reach the network")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    conn, settings = burst(tmp_path, 25)
    row = finding_row(conn)

    group = load(conn, settings, row, measure=False)

    assert group.total == 25


# --- 6, 26-27: exact copies stay exact ----------------------------------------


def test_byte_identical_members_are_labelled_and_counted(tmp_path: Path) -> None:
    """The hashes have already answered these. Nobody should be asked to look."""
    conn, settings = burst(tmp_path, 12, exact=3)
    row = finding_row(conn)

    group = load(conn, settings, row, measure=False)

    assert group.exact_sets == 1
    assert group.exact_members == 3
    marked = [photo for photo in group.members if photo.exact]
    assert len(marked) == 3
    assert {photo.exact_set for photo in marked} == {1}


def test_a_unique_photo_is_not_called_an_exact_copy(tmp_path: Path) -> None:
    conn, settings = burst(tmp_path, 12, exact=3)
    row = finding_row(conn)

    group = load(conn, settings, row, measure=False)

    assert any(not photo.exact for photo in group.members)
    assert all(photo.exact_set == 0 for photo in group.members if not photo.exact)


def test_exact_copies_can_be_filtered_to(tmp_path: Path) -> None:
    conn, settings = burst(tmp_path, 30, exact=4)
    row = finding_row(conn)

    group = load(conn, settings, row, only="exact", measure=False)

    assert group.matching == 4
    assert all(photo.exact for photo in group.members)
    #  The counts stay about the whole group, because that is what they are.
    assert group.total == 30


# --- 15-24: what the page shows -----------------------------------------------


def test_every_member_carries_the_facts_the_index_already_had(
    tmp_path: Path,
) -> None:
    conn, settings = burst(tmp_path, 10)
    row = finding_row(conn)

    photo = load(conn, settings, row, measure=False).members[0]

    assert photo.name == "IMG_5100.jpg"
    assert photo.folder == FOLDER
    assert photo.format == "JPG"
    assert photo.size > 0
    assert photo.file_date  # from mtime, and labelled as the file's date


def test_sorting_is_factual_and_deterministic(tmp_path: Path) -> None:
    """Every order ties-breaks on the path, so nothing swaps places between
    loads and moves a tick somebody has just placed."""
    conn, settings = burst(tmp_path, 10)
    row = finding_row(conn)

    by_size = load(conn, settings, row, sort="size", measure=False)
    again = load(conn, settings, row, sort="size", measure=False)

    assert [p.item_id for p in by_size.members] == [p.item_id for p in again.members]
    sizes = [p.size for p in by_size.members]
    assert sizes == sorted(sizes)


def test_no_member_is_marked_as_the_one_to_keep(tmp_path: Path) -> None:
    """Nothing is preselected, ranked, starred or recommended. That judgement
    is the whole content of the decision and is not a fact this program has."""
    conn, settings = burst(tmp_path, 20)
    row = finding_row(conn)

    group = load(conn, settings, row, measure=False)

    assert all(photo.kept for photo in group.members)
    assert group.kept == 20
    assert group.set_aside == 0


# --- 25, 28-33: the answer ----------------------------------------------------


def test_the_selection_survives_paging(tmp_path: Path) -> None:
    """It is stored, not carried in hidden fields — a mis-click must not throw
    away ten minutes of choosing."""
    conn, settings = burst(tmp_path, 60)
    row = finding_row(conn)
    second = load(conn, settings, row, page=2, measure=False).members[0]

    choose(conn, settings, int(row["id"]), second.item_id, SET_ASIDE)

    assert choices(conn, int(row["id"])) == {second.item_id: SET_ASIDE}
    first_page = load(conn, settings, row, page=1, measure=False)
    assert first_page.set_aside == 1
    assert first_page.kept == 59


def test_a_file_outside_the_group_cannot_be_answered(tmp_path: Path) -> None:
    conn, settings = burst(tmp_path, 12)
    stray = settings.library_dir / "Photos/2024/elsewhere.jpg"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("not in the group", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    row = finding_row(conn)
    other = conn.execute(
        "SELECT id FROM items WHERE relpath='Photos/2024/elsewhere.jpg'"
    ).fetchone()

    with pytest.raises(CorrectionRefused):
        choose(conn, settings, int(row["id"]), int(other["id"]), SET_ASIDE)


def test_a_selection_becomes_one_decision_with_many_operations(
    tmp_path: Path,
) -> None:
    conn, settings = burst(tmp_path, 37)
    row = finding_row(conn)
    group = load(conn, settings, row, measure=False)
    going = [photo for photo in _every(conn, settings, row)][8:]
    for photo in going:
        choose(conn, settings, int(row["id"]), photo.item_id, SET_ASIDE)

    plan_id = approve(conn, settings, int(row["id"]))

    ops = conn.execute(
        "SELECT op_type FROM plan_ops WHERE plan_id=?", (plan_id,)
    ).fetchall()
    assert len(ops) == 29
    assert {op["op_type"] for op in ops} == {"quarantine"}
    assert group.total == 37
    #  One plan, so one card, however many files it moves.
    rows = queue_rows(conn, settings, kind="set-aside")
    assert len(rows) == 1


def _every(conn, settings, row):
    from librairy.photo_group import _all

    return _all(conn, settings, row)


def test_kept_members_are_not_in_the_plan(tmp_path: Path) -> None:
    conn, settings = burst(tmp_path, 12)
    row = finding_row(conn)
    everyone = _every(conn, settings, row)
    for photo in everyone[3:]:
        choose(conn, settings, int(row["id"]), photo.item_id, SET_ASIDE)

    plan_id = approve(conn, settings, int(row["id"]))

    sources = {
        str(op["src_relpath"])
        for op in conn.execute(
            "SELECT src_relpath FROM plan_ops WHERE plan_id=?", (plan_id,)
        )
    }
    assert all(photo.relpath not in sources for photo in everyone[:3])


def test_setting_aside_every_photo_is_refused(tmp_path: Path) -> None:
    """The library must not be emptied of a picture in the name of tidying up
    the fact that there are several of it."""
    conn, settings = burst(tmp_path, 10)
    row = finding_row(conn)
    for photo in _every(conn, settings, row):
        choose(conn, settings, int(row["id"]), photo.item_id, SET_ASIDE)

    with pytest.raises(CorrectionRefused):
        approve(conn, settings, int(row["id"]))


def test_a_changed_member_refuses_the_whole_decision(tmp_path: Path) -> None:
    """A group selection is a statement about a snapshot. Half of it applied is
    not a state anybody approved."""
    conn, settings = burst(tmp_path, 12)
    row = finding_row(conn)
    everyone = _every(conn, settings, row)
    for photo in everyone[4:]:
        choose(conn, settings, int(row["id"]), photo.item_id, SET_ASIDE)
    plan_id = approve(conn, settings, int(row["id"]))

    (settings.library_dir / everyone[6].relpath).write_text("edited", encoding="utf-8")
    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    assert summary.skipped_changed == 8
    for photo in everyone:
        assert (settings.library_dir / photo.relpath).is_file()


def test_a_vanished_kept_photo_stops_the_others_being_set_aside(
    tmp_path: Path,
) -> None:
    """The kept members are why the rest are safe to move. If they are gone the
    decision is gone with them."""
    conn, settings = burst(tmp_path, 12)
    row = finding_row(conn)
    everyone = _every(conn, settings, row)
    for photo in everyone[1:]:
        choose(conn, settings, int(row["id"]), photo.item_id, SET_ASIDE)
    plan_id = approve(conn, settings, int(row["id"]))

    (settings.library_dir / everyone[0].relpath).unlink()
    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    for photo in everyone[1:]:
        assert (settings.library_dir / photo.relpath).is_file()


def test_committing_moves_the_unwanted_to_quarantine_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    conn, settings = burst(tmp_path, 15)
    row = finding_row(conn)
    everyone = _every(conn, settings, row)
    for photo in everyone[5:]:
        choose(conn, settings, int(row["id"]), photo.item_id, SET_ASIDE)
    plan_id = approve(conn, settings, int(row["id"]))

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 10
    for photo in everyone[:5]:
        assert (settings.library_dir / photo.relpath).is_file()
    for photo in everyone[5:]:
        assert not (settings.library_dir / photo.relpath).exists()
    held = list(settings.quarantine_dir.rglob("IMG_*.jpg"))
    assert len(held) == 10


def test_undo_puts_every_photo_back(tmp_path: Path) -> None:
    conn, settings = burst(tmp_path, 15)
    row = finding_row(conn)
    everyone = _every(conn, settings, row)
    for photo in everyone[5:]:
        choose(conn, settings, int(row["id"]), photo.item_id, SET_ASIDE)
    plan_id = approve(conn, settings, int(row["id"]))
    execute_plan(conn, plan_id, settings)

    undo_correction(conn, settings, plan_id)

    for photo in everyone:
        assert (settings.library_dir / photo.relpath).is_file()


def test_approving_clears_the_half_made_selection(tmp_path: Path) -> None:
    conn, settings = burst(tmp_path, 12)
    row = finding_row(conn)
    for photo in _every(conn, settings, row)[3:]:
        choose(conn, settings, int(row["id"]), photo.item_id, SET_ASIDE)

    approve(conn, settings, int(row["id"]))

    assert choices(conn, int(row["id"])) == {}


# --- 38-40: keeping all of them ------------------------------------------------


def test_keeping_all_of_them_makes_no_plan(tmp_path: Path) -> None:
    """There is no filesystem work in leaving things alone, and an empty plan
    would put a no-op in Commit, in History and in Undo."""
    from librairy.similar_media import compare, resolve

    conn, settings = burst(tmp_path, 20)
    row = finding_row(conn)
    view = compare(conn, settings, row, measure=False)

    plan_id = resolve(
        conn, settings, int(row["id"]), [member.relpath for member in view.members]
    )

    assert plan_id == ""
    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0


def test_keeping_all_of_them_stops_the_group_being_asked_about(
    tmp_path: Path,
) -> None:
    from librairy.similar_media import compare, resolve

    conn, settings = burst(tmp_path, 20)
    row = finding_row(conn)
    view = compare(conn, settings, row, measure=False)
    resolve(conn, settings, int(row["id"]), [m.relpath for m in view.members])

    assert detect(conn) == []


def test_a_group_whose_files_change_becomes_a_live_question_again(
    tmp_path: Path,
) -> None:
    """The dismissal was about those bytes. A re-export is a comparison nobody
    has been asked about."""
    from librairy.similar_media import compare, resolve

    conn, settings = burst(tmp_path, 12)
    row = finding_row(conn)
    view = compare(conn, settings, row, measure=False)
    resolve(conn, settings, int(row["id"]), [m.relpath for m in view.members])
    assert detect(conn) == []

    (settings.library_dir / view.members[3].relpath).write_text(
        "a different export", encoding="utf-8"
    )
    scan_root(conn, "library", settings.library_dir, settings)

    assert len(detect(conn)) == 1


def test_forgetting_a_selection_leaves_the_group_alone(tmp_path: Path) -> None:
    conn, settings = burst(tmp_path, 12)
    row = finding_row(conn)
    everyone = _every(conn, settings, row)
    choose(conn, settings, int(row["id"]), everyone[0].item_id, SET_ASIDE)

    forget(conn, int(row["id"]))

    group = load(conn, settings, row, measure=False)
    assert group.set_aside == 0
    assert group.kept == 12
    assert all(
        (settings.library_dir / photo.relpath).is_file() for photo in everyone
    )


def test_unticking_and_reticking_is_recorded_as_keep(tmp_path: Path) -> None:
    """"I looked at this and decided to keep it" is a different thing from
    "I never got to it", and the row says which."""
    conn, settings = burst(tmp_path, 12)
    row = finding_row(conn)
    photo = _every(conn, settings, row)[0]

    choose(conn, settings, int(row["id"]), photo.item_id, SET_ASIDE)
    choose(conn, settings, int(row["id"]), photo.item_id, KEEP)

    assert choices(conn, int(row["id"])) == {photo.item_id: KEEP}
    assert load(conn, settings, row, measure=False).set_aside == 0


# --- the page ------------------------------------------------------------------


def client_for(tmp_path: Path, count: int, **kwargs):
    from fastapi.testclient import TestClient

    from librairy.web.app import create_app

    conn, settings = burst(tmp_path, count, **kwargs)
    row = finding_row(conn)
    return TestClient(create_app(settings, conn)), conn, settings, row


def post(client, path: str, data=None):
    client.get("/review")
    token = client.cookies["csrf_token"]
    payload = dict(data or {})
    payload["csrf_token"] = token
    return client.post(
        path, data=payload, headers={"x-csrf-token": token}, follow_redirects=False
    )


def test_review_shows_a_large_group_as_a_row_not_a_grid(tmp_path: Path) -> None:
    """Thirty-seven thumbnails do not belong inside a list of findings."""
    client, _, _, row = client_for(tmp_path, 37)

    page = client.get("/review").text

    assert "37" in page
    assert f"/review/audit/{row['id']}/photos" in page
    assert "Review photos" in page
    #  And not the small shape's controls.
    assert "Keep all of them" not in page


def test_a_large_group_is_a_choice_and_never_bulk_approved(tmp_path: Path) -> None:
    from librairy.web.actionability import CHOICE
    from librairy.web.review import audit_view

    conn, settings = burst(tmp_path, 30)
    finding_row(conn)
    groups = audit_view(conn, settings)
    found = next(
        row
        for group in groups["audit_groups"]
        for row in group["findings"]
        if row["kind"] == KIND
    )

    assert found["status_kind"] == CHOICE
    #  A choice is never swept up by "approve all confident": the whole content
    #  of the row is a decision only a person can make.
    assert found["can_approve"] is False


def test_the_group_page_renders_a_bounded_grid(tmp_path: Path) -> None:
    client, _, _, row = client_for(tmp_path, 100)

    page = client.get(f"/review/audit/{row['id']}/photos").text

    assert page.count("/thumb") == PAGE_MEMBERS
    assert "IMG_5100.jpg" in page
    assert "Page 1 of" in page


def test_the_grid_asks_for_no_more_thumbnails_than_it_shows(tmp_path: Path) -> None:
    """A page never asks for more than it displays, and a group never asks for
    more than a page."""
    client, _, _, row = client_for(tmp_path, 500)

    page = client.get(f"/review/audit/{row['id']}/photos").text

    assert page.count('src="/preview/items/') == PAGE_MEMBERS
    assert 'loading="lazy"' in page


def test_the_page_shows_facts_and_no_recommendation(tmp_path: Path) -> None:
    client, _, _, row = client_for(tmp_path, 12)

    page = client.get(f"/review/audit/{row['id']}/photos").text

    assert "JPG" in page
    assert "File date" in page
    for word in ("Best", "Recommended", "Better", "Low quality"):
        assert word not in page


def test_exact_copies_are_labelled_on_the_page(tmp_path: Path) -> None:
    client, _, _, row = client_for(tmp_path, 12, exact=3)

    page = client.get(f"/review/audit/{row['id']}/photos").text

    assert "Exact copy" in page
    assert "byte-identical" in page


def test_selecting_a_page_only_answers_that_page(tmp_path: Path) -> None:
    """Without knowing what was shown, an unticked box and a photo on page 3
    look identical — and saving one page would set aside the whole group."""
    client, conn, settings, row = client_for(tmp_path, 60)
    shown = [
        photo.item_id
        for photo in load(conn, settings, row, measure=False).members
    ]

    response = post(
        client,
        f"/review/audit/{row['id']}/photos/select",
        {"shown": [str(item) for item in shown],
         "keep": [str(item) for item in shown[2:]]},
    )

    assert response.status_code == 303
    chosen = choices(conn, int(row["id"]))
    assert len(chosen) == PAGE_MEMBERS
    assert sum(1 for value in chosen.values() if value == SET_ASIDE) == 2
    assert load(conn, settings, row, measure=False).kept == 58


def test_approving_from_the_page_reaches_commit(tmp_path: Path) -> None:
    client, conn, settings, row = client_for(tmp_path, 20)
    for photo in _every(conn, settings, row)[5:]:
        choose(conn, settings, int(row["id"]), photo.item_id, SET_ASIDE)

    response = post(client, f"/review/audit/{row['id']}/photos/approve")

    assert response.status_code == 303
    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 1


def test_keeping_all_from_the_page_makes_no_plan(tmp_path: Path) -> None:
    client, conn, _, row = client_for(tmp_path, 20)

    response = post(client, f"/review/audit/{row['id']}/photos/keep-all")

    assert response.status_code == 303
    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0
    assert detect(conn) == []


def test_the_page_never_calls_a_model_or_a_provider(
    tmp_path: Path, monkeypatch
) -> None:
    """No AI, no catalog, no network — on this page or any other GET of it."""
    import urllib.request

    from librairy.ai import orchestrator

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the comparison page must not reach out")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    if hasattr(orchestrator, "classify_item"):
        monkeypatch.setattr(orchestrator, "classify_item", forbidden)
    client, _, _, row = client_for(tmp_path, 25)

    for _ in range(3):
        assert client.get(f"/review/audit/{row['id']}/photos").status_code == 200


def test_a_selection_of_nothing_is_refused_by_the_route(tmp_path: Path) -> None:
    client, conn, settings, row = client_for(tmp_path, 10)
    for photo in _every(conn, settings, row):
        choose(conn, settings, int(row["id"]), photo.item_id, SET_ASIDE)

    response = post(client, f"/review/audit/{row['id']}/photos/approve")

    assert response.status_code == 409


def test_a_small_group_keeps_the_comparison_it_had(tmp_path: Path) -> None:
    """Two encodes of one song still get the table and the named buttons."""
    client, _, _, row = client_for(tmp_path, 3)

    page = client.get("/review").text

    assert "Keep all of them" in page
    assert f"/review/audit/{row['id']}/photos" not in page


def test_quarantine_names_a_photo_that_was_actually_kept(tmp_path: Path) -> None:
    """The entry is written while the plan is running, so "still in the library"
    is not the same question as "kept" — seventeen of the eighteen leaving are
    still filed when the first one is written."""
    from librairy.web.quarantine import quarantine_data

    conn, settings = burst(tmp_path, 25)
    row = finding_row(conn)
    everyone = _every(conn, settings, row)
    kept = {photo.name for photo in everyone[:7]}
    for photo in everyone[7:]:
        choose(conn, settings, int(row["id"]), photo.item_id, SET_ASIDE)
    plan_id = approve(conn, settings, int(row["id"]))
    execute_plan(conn, plan_id, settings)

    entries = quarantine_data(conn, settings)["entries"]

    assert len(entries) == 18
    for entry in entries:
        named = Path(str(entry["duplicate_of"])).name
        assert named in kept, f"{entry['display_name']} points at {entry['duplicate_of']}"
        assert entry["kept_alongside"] == 6

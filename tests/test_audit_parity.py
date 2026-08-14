"""Library Audit has the inbox queue's interaction quality, not its meaning.

Preview, evidence, a `⋯` menu, selection and bulk actions — the same grammar,
because a second way of doing the same thing is a second thing to learn. What
must never converge is the *scope*: an inbox bulk action cannot reach a finding
about a file you already own, and an audit bulk action cannot reach a proposal.
That is asserted here from both directions.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import accept_correction
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import decode_evidence, upsert_proposal
from librairy.scanner import scan_root
from librairy.web.app import create_app
from librairy.web.review import apply_audit_bulk

TRACK = "Music/Pop/Queen/05 - Song.flac"
LYRICS = "Music/Pop/Queen/05 - Song.lrc"
PICTURE = "Photos/2024/holiday.jpg"
ALBUM = "Music/R&BSoul/Alicia Keys/Unplugged"
DEST = "Music/Rock/Queen/A Night at the Opera/05 - Song.flac"

TAG_EVIDENCE = [
    EvidenceEntry("tags", "artist", "Queen", 0.9),
    EvidenceEntry("filesystem", "current folder", "Pop", 0.9),
    EvidenceEntry("library-pattern", "existing folder", "Music/Rock/Queen", 0.85),
]

JPEG = Path("tests/fixtures/tiny.jpg")


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


def scene(tmp_path: Path, *findings: Finding, files: tuple[str, ...] = (TRACK, LYRICS)):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in files:
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        if relpath.endswith(".jpg") and JPEG.exists():
            path.write_bytes(JPEG.read_bytes())
        else:
            path.write_text(f"bytes of {relpath}", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    resolved = []
    for finding in findings or (correction(),):
        row = conn.execute(
            "SELECT id, fingerprint FROM items WHERE relpath=?", (finding.relpath,)
        ).fetchone()
        if row is not None:
            finding.item_id, finding.fingerprint = row["id"], row["fingerprint"]
        resolved.append(finding)
    record_findings(conn, resolved)
    return TestClient(create_app(settings, conn)), conn, settings


def correction(relpath: str = TRACK, dest: str = DEST) -> Finding:
    return Finding(
        relpath=relpath,
        kind="tag-path-mismatch",
        severity="high",
        summary="Tagged 'Queen' but filed under 'Pop'.",
        dest_relpath=dest,
        evidence=list(TAG_EVIDENCE),
    )


def observation(relpath: str = ALBUM) -> Finding:
    return Finding(
        relpath=relpath,
        kind="missing-artwork",
        severity="review",
        summary="'Unplugged': 2 tracks and no cover image.",
        evidence=[EvidenceEntry("filesystem", "album", "Unplugged", 0.8)],
    )


def findings_by_path(conn) -> dict[str, int]:
    return {
        row["relpath"]: row["id"]
        for row in conn.execute("SELECT id, relpath FROM audit_findings")
    }


def rows(body: str) -> str:
    return body.split('class="audit-list', 1)[1] if 'class="audit-list' in body else ""


# --- terminology --------------------------------------------------------------


def test_the_conversational_wording_is_gone(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction(), observation())

    body = client.get("/review").text

    assert "Your call" not in body
    assert "your call" not in body
    assert "Keep as it is" not in body


def test_no_change_is_the_wording(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction(), observation())

    assert ">Dismiss suggestion</button>" in rows(client.get("/review").text)


def test_stored_status_values_never_reach_a_control(tmp_path: Path) -> None:
    """`open`, `accepted` and `kept` are database states. The row says
    "Waiting for Commit", which is what someone is actually looking at."""
    client, conn, settings = scene(tmp_path, correction())
    accept_correction(conn, settings, findings_by_path(conn)[TRACK])

    section = rows(client.get("/review").text)
    controls = [line for line in section.splitlines() if "<button" in line or "badge" in line]

    assert "Waiting for Commit" in section
    for raw in (">open<", ">accepted<", ">kept<", ">corrected<", "settled"):
        assert all(raw not in line for line in controls), raw


def test_developer_words_stay_out_of_the_row(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())

    section = rows(client.get("/review").text)

    for word in ("fingerprint", "dest_relpath", "relpath", "item_id"):
        assert word not in section, word


# --- preview ------------------------------------------------------------------


def test_a_file_finding_offers_a_preview(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())

    assert "/preview/items/" in rows(client.get("/review").text)


def test_the_preview_is_the_current_file_not_the_suggestion(tmp_path: Path) -> None:
    """It resolves an item, and an item carries the root and relpath the file
    has *now* — so the suggested destination can never be its source."""
    client, conn, settings = scene(tmp_path, correction())
    item_id = conn.execute("SELECT id FROM items WHERE relpath=?", (TRACK,)).fetchone()["id"]

    section = rows(client.get("/review").text)

    assert f"/preview/items/{item_id}" in section
    assert "A Night at the Opera" not in section.split("/preview/items/")[1][:200]


def test_a_missing_file_offers_no_preview(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction())
    (settings.library_dir / TRACK).unlink()

    assert "/preview/items/" not in rows(client.get("/review").text)


def test_a_folder_finding_offers_no_preview(tmp_path: Path) -> None:
    """No Preview *control*: the row's Preview resolves the finding's own item,
    and a folder has none.

    The details panel may still show the cover of a track inside the folder —
    that is a different picture answering a different question, and it is why
    this asserts on the control rather than on the substring `/preview/items/`,
    which the artwork thumbnail legitimately uses.
    """
    client, *_ = scene(
        tmp_path, observation(), files=(f"{ALBUM}/01.flac", f"{ALBUM}/02.flac")
    )

    body = rows(client.get("/review").text)
    assert "hx-get=\"/preview/items/" not in body
    assert ">Preview<" not in body


def test_the_preview_uses_the_shared_endpoint_and_lightbox(tmp_path: Path) -> None:
    """No audit-specific preview infrastructure: the same route, the same card,
    and therefore the same fullscreen viewer and the same video teardown."""
    client, conn, settings = scene(
        tmp_path, correction(PICTURE, "Photos/2025/holiday.jpg"), files=(PICTURE,)
    )
    item_id = conn.execute("SELECT id FROM items WHERE relpath=?", (PICTURE,)).fetchone()["id"]

    card = client.get(f"/preview/items/{item_id}").text

    assert "preview-card" in card
    assert "data-lightbox" in card


def test_a_preview_that_cannot_render_still_answers(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction())
    item_id = conn.execute("SELECT id FROM items WHERE relpath=?", (TRACK,)).fetchone()["id"]

    response = client.get(f"/preview/items/{item_id}")

    assert response.status_code == 200
    assert "preview-card" in response.text


# --- evidence -----------------------------------------------------------------


def test_audit_evidence_decodes_at_all(tmp_path: Path) -> None:
    """It never did. `filesystem` was not a valid evidence source, so the one
    function that renders evidence threw its contents away."""
    client, conn, settings = scene(tmp_path, correction())
    stored = conn.execute("SELECT evidence FROM audit_findings").fetchone()["evidence"]

    entries = decode_evidence(stored)

    assert {entry.source for entry in entries} == {"tags", "filesystem", "library-pattern"}


def test_the_row_names_the_sources_it_actually_used(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())

    section = rows(client.get("/review").text)

    assert "Embedded tags" in section
    assert "On disk" in section
    assert "Your library" in section


def test_a_source_that_contributed_nothing_is_not_listed(tmp_path: Path) -> None:
    client, *_ = scene(
        tmp_path, observation(), files=(f"{ALBUM}/01.flac", f"{ALBUM}/02.flac")
    )

    section = rows(client.get("/review").text)

    labels = re.findall(r'<span class="badge badge-info">([^<]+)</span>', section)

    assert labels == ["On disk"]


def test_no_overall_percentage_is_claimed_for_a_finding(tmp_path: Path) -> None:
    """The audit records a weight per piece of evidence and never adds them
    up. A headline score here would be decoration presented as measurement."""
    client, *_ = scene(tmp_path, correction())

    section = rows(client.get("/review").text)

    assert "%</span>" not in section.split("why-list")[0]
    assert re.search(r'class="conf-score">\d+ sources?</span>', section)


def test_the_per_evidence_weights_are_the_stored_ones(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())

    section = rows(client.get("/review").text)

    assert "90%" in section
    assert "85%" in section


def test_the_evidence_bar_is_composition_and_says_so(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())

    section = rows(client.get("/review").text)

    assert "conf-track" in section
    assert 'aria-label="Evidence based on' in section


def test_the_audit_marker_and_the_evidence_bar_are_different_things(
    tmp_path: Path,
) -> None:
    """Purple means "this is about a file you already own". It must never also
    mean a confidence level."""
    css = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")

    assert ".badge-audit { color: var(--audit)" in css
    assert "--audit" not in css.split(".conf-part")[1].split("}")[0]


def test_the_why_panel_explains_the_current_path(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())

    section = rows(client.get("/review").text)

    assert "why-list" in section
    assert "<dt>Now</dt>" in section
    assert "<dt>Would become</dt>" in section


# --- the secondary menu -------------------------------------------------------


def test_a_row_has_a_labelled_secondary_menu(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())

    section = rows(client.get("/review").text)

    assert 'aria-label="More actions for' in section
    assert "⋯" in section


def test_open_in_browse_points_at_the_folder_the_file_is_in_now(
    tmp_path: Path,
) -> None:
    client, *_ = scene(tmp_path, correction())

    section = rows(client.get("/review").text)

    assert "/browse/music?folder=Pop/Queen" in section
    assert "/browse/music?folder=Rock" not in section


def test_a_missing_file_offers_no_browse_link(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction())
    (settings.library_dir / TRACK).unlink()

    assert ">Open in Browse</a>" not in rows(client.get("/review").text)


def test_view_details_appears_only_with_a_real_item(tmp_path: Path) -> None:
    """An unindexed finding has no item id, and one is not invented for it."""
    client, conn, settings = scene(tmp_path, observation(), files=(TRACK,))
    conn.execute("UPDATE audit_findings SET item_id=NULL")

    assert "View details" not in rows(client.get("/review").text)


def test_the_menu_holds_no_dead_controls(tmp_path: Path) -> None:
    client, *_ = scene(
        tmp_path, observation(), files=(f"{ALBUM}/01.flac", f"{ALBUM}/02.flac")
    )

    section = rows(client.get("/review").text)

    assert "Other options" not in section
    assert "Approve change" not in section


# --- selection ----------------------------------------------------------------


def test_the_two_selections_use_different_fields_and_forms(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction())
    item_id = conn.execute("SELECT id FROM items WHERE relpath=?", (LYRICS,)).fetchone()["id"]
    upsert_proposal(
        conn,
        item_id=item_id,
        category="music",
        clean_name="05 - Song.lrc",
        dest_relpath="Music/Rock/05 - Song.lrc",
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "music", 0.9)],
    )
    conn.execute("UPDATE items SET state='proposed' WHERE id=?", (item_id,))

    body = client.get("/review").text
    section = rows(body)

    assert 'name="finding_id"' in section
    assert 'form="audit-actions"' in section
    assert 'name="proposal_id"' not in section
    assert 'form="review-actions"' in body.split('class="audit-list', 1)[0]


def test_the_audit_checkbox_says_what_it_selects(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())

    section = rows(client.get("/review").text)

    assert 'aria-label="Select 05 - Song.flac"' in section


def test_the_inbox_bulk_endpoint_cannot_name_a_finding(tmp_path: Path) -> None:
    """Not a filter — a different signature. /review/action reads
    `proposal_id`, and a finding id posted there is simply not read."""
    import inspect

    from librairy.web.review import apply_review_action

    signature = inspect.signature(apply_review_action)

    assert "proposal_ids" in signature.parameters
    assert "finding_ids" not in signature.parameters


def test_the_audit_bulk_endpoint_cannot_name_a_proposal(tmp_path: Path) -> None:
    import inspect

    signature = inspect.signature(apply_audit_bulk)

    assert "finding_ids" in signature.parameters
    assert "proposal_ids" not in signature.parameters


def test_an_inbox_proposal_id_does_nothing_in_an_audit_bulk_action(
    tmp_path: Path,
) -> None:
    """Ids are looked up in audit_findings. A proposal id does not resolve."""
    client, conn, settings = scene(tmp_path, correction())
    item_id = conn.execute("SELECT id FROM items WHERE relpath=?", (LYRICS,)).fetchone()["id"]
    upsert_proposal(
        conn,
        item_id=item_id,
        category="music",
        clean_name="x.lrc",
        dest_relpath="Music/x.lrc",
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "music", 0.9)],
    )
    proposal_id = conn.execute("SELECT id FROM proposals").fetchone()["id"]

    result = apply_audit_bulk(conn, settings, "keep", [proposal_id + 10_000])

    assert result == "Nothing was selected."
    assert conn.execute("SELECT status FROM proposals").fetchone()["status"] == "proposed"


def test_clearing_the_audit_selection_leaves_the_inbox_alone() -> None:
    """The clear button is scoped to the audit's own boxes by field name."""
    script = Path("src/librairy/web/static/review.js").read_text(encoding="utf-8")
    clear = script.split(".audit-clear", 1)[1]

    assert "SCOPES[1]" in clear
    assert "proposal_id" not in clear.split("refreshCount(scope)")[0]


def test_the_two_scopes_are_configured_not_copied() -> None:
    script = Path("src/librairy/web/static/review.js").read_text(encoding="utf-8")

    assert script.count("var SCOPES") == 1
    assert 'field: "proposal_id"' in script
    assert 'field: "finding_id"' in script


# --- bulk actions -------------------------------------------------------------


def test_bulk_no_change_resolves_every_selected_finding(tmp_path: Path) -> None:
    client, conn, settings = scene(
        tmp_path,
        correction(),
        observation(),
        files=(TRACK, LYRICS, f"{ALBUM}/01.flac"),
    )
    ids = list(findings_by_path(conn).values())

    result = apply_audit_bulk(conn, settings, "keep", ids)

    assert result == "Dismissed 2. They stay in Dismissed and can be restored."
    statuses = {row["status"] for row in conn.execute("SELECT status FROM audit_findings")}
    assert statuses == {"kept"}


def test_bulk_no_change_deletes_nothing(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction())
    ids = list(findings_by_path(conn).values())

    apply_audit_bulk(conn, settings, "keep", ids)

    assert conn.execute("SELECT COUNT(*) FROM audit_findings").fetchone()[0] == 1


def test_bulk_reaudit_looks_at_each_folder_once(tmp_path: Path) -> None:
    client, conn, settings = scene(
        tmp_path, correction(), correction(LYRICS, "Music/Rock/x.lrc")
    )
    ids = list(findings_by_path(conn).values())

    result = apply_audit_bulk(conn, settings, "reaudit", ids)

    assert "1 folder" in result


def test_bulk_accept_only_touches_the_eligible_ones(tmp_path: Path) -> None:
    client, conn, settings = scene(
        tmp_path,
        correction(),
        observation(),
        files=(TRACK, LYRICS, f"{ALBUM}/01.flac"),
    )
    ids = list(findings_by_path(conn).values())

    result = apply_audit_bulk(conn, settings, "accept", ids)

    assert "Selected: 2" in result
    assert "Approved: 1" in result
    assert "Observation only: 1" in result
    assert result.endswith(".")
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1


def test_a_mixed_selection_is_explained_rather_than_silently_trimmed(
    tmp_path: Path,
) -> None:
    client, conn, settings = scene(tmp_path, correction(), observation())
    stale = findings_by_path(conn)[TRACK]
    (settings.library_dir / TRACK).write_text("changed", encoding="utf-8")

    result = apply_audit_bulk(conn, settings, "accept", list(findings_by_path(conn).values()))

    assert "Nothing was approved" in result
    assert "Changed since the audit" in result
    assert "Observation only: 1" in result
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert conn.execute(
        "SELECT status FROM audit_findings WHERE id=?", (stale,)
    ).fetchone()["status"] == "open"


def test_the_toolbar_counts_eligibility_before_the_button_is_pressed() -> None:
    script = Path("src/librairy/web/static/review.js").read_text(encoding="utf-8")

    assert "refreshEligibility" in script
    assert "eligible" in script
    assert "cannot be approved" in script


def test_there_is_no_bulk_approve_by_threshold(tmp_path: Path) -> None:
    """Inbox has "Approve all confident". Existing-library corrections carry a
    different risk and get no such shortcut."""
    client, *_ = scene(tmp_path, correction())

    section = client.get("/review").text.split('id="library-audit"', 1)[1]

    assert "Approve all" not in section
    assert "90%+" not in section


def test_an_unknown_bulk_action_is_refused(tmp_path: Path) -> None:
    import pytest

    client, conn, settings = scene(tmp_path, correction())

    with pytest.raises(ValueError, match="unknown audit action"):
        apply_audit_bulk(conn, settings, "delete_everything", [1])


def test_the_bulk_result_is_shown_on_the_page(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction())
    finding_id = findings_by_path(conn)[TRACK]
    client.get("/review")
    token = client.cookies["csrf_token"]

    response = client.post(
        "/review/audit/bulk",
        data={"action": "keep", "finding_id": [finding_id], "csrf_token": token},
        headers={"x-csrf-token": token},
    )

    assert response.status_code == 200
    assert "Dismissed 1." in response.text


# --- grouping and affected files ----------------------------------------------


def test_a_single_finding_gets_no_folder_heading(tmp_path: Path) -> None:
    """Six headings for six single findings is six lines of furniture."""
    client, *_ = scene(tmp_path, correction())

    body = client.get("/review").text

    assert "audit-group" not in body
    assert body.count('class="audit-list"') == 1


def test_two_findings_in_one_folder_do_get_a_heading(tmp_path: Path) -> None:
    client, *_ = scene(
        tmp_path, correction(), correction(LYRICS, "Music/Rock/Queen/x.lrc")
    )

    body = client.get("/review").text

    assert "audit-group" in body
    # "items" is the code's word for them, not the reader's.
    assert "2 findings" in body


def test_the_affected_list_names_the_companions(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())

    section = rows(client.get("/review").text)

    assert "Moves 2 files" in section
    assert "05 - Song.lrc" in section
    assert "primary" in section and "companion" in section


def test_the_affected_list_uses_the_shared_extension_control(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())

    affected = rows(client.get("/review").text).split("audit-affected", 1)[1]

    assert "ext-info" in affected


def test_an_unrelated_sibling_is_not_in_the_affected_list(tmp_path: Path) -> None:
    client, *_ = scene(
        tmp_path, correction(), files=(TRACK, "Music/Pop/Queen/06 - Other.flac")
    )

    section = rows(client.get("/review").text)

    assert "audit-affected" not in section
    assert "06 - Other.flac" not in section


# --- accessibility ------------------------------------------------------------


def test_the_library_audit_marker_is_text(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())

    assert "EXISTING LIBRARY" in rows(client.get("/review").text)


def test_the_state_is_never_carried_by_colour_alone(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction())
    (settings.library_dir / TRACK).write_text("changed", encoding="utf-8")

    section = rows(client.get("/review").text)

    assert "Needs analysis again" in section
    assert "The file changed after this was found" in section


def test_the_row_is_not_a_clickable_container(tmp_path: Path) -> None:
    """Nested controls inside a clickable row is how a Preview click becomes
    an Accept."""
    template = Path(
        "src/librairy/web/templates/partials/review_audit.html"
    ).read_text(encoding="utf-8")

    assert "onclick" not in template
    assert "audit-row" in template
    assert "hx-get" not in template.split("audit-row", 1)[1].split("row-body")[0]


# --- mobile -------------------------------------------------------------------


def audit_css() -> str:
    css = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")
    return css.split("/* --- Library audit")[1].split("/* --- File type info")[0]


def test_nothing_in_an_audit_row_is_wider_than_the_screen() -> None:
    """375px is the target. A fixed width in ch or px on a path, a bar or a
    button row is how a page starts scrolling sideways."""
    for rule in re.findall(r"[^{}]+\{[^}]*\}", audit_css()):
        if "max-width" in rule or "@media" in rule:
            continue
        fixed = re.findall(r"[^-]width:\s*(\d+)(px|rem|ch)", rule)
        for value, unit in fixed:
            # 8rem is the evidence bar, which the mobile block widens to 100%.
            assert not (unit == "px" and int(value) > 320), rule.strip()[:80]


def test_long_paths_wrap_instead_of_pushing_the_page_sideways() -> None:
    shared = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")
    clamp = shared.split(".proposal-name,\n.row-name {")[1].split("}")[0]
    assert "overflow-wrap: anywhere" in clamp


def test_the_evidence_bar_goes_full_width_on_a_phone() -> None:
    css = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")
    mobile = [
        block.split("\n}")[0]
        for block in css.split("@media (max-width: 40rem) {")[1:]
        if ".conf-track" in block.split("\n}")[0]
    ]

    assert mobile
    assert "width: 100%" in mobile[0]


def test_the_secondary_tray_stacks_on_a_phone() -> None:
    css = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")
    mobile = [
        block.split("\n}")[0]
        for block in css.split("@media (max-width: 40rem) {")[1:]
        if ".why-paths" in block.split("\n}")[0]
    ]

    assert mobile
    assert "grid-template-columns: 1fr" in mobile[0]


def test_an_already_accepted_correction_is_not_offered_again(tmp_path: Path) -> None:
    """The toolbar's eligible count has to match what the button will do."""
    client, conn, settings = scene(tmp_path, correction())
    accept_correction(conn, settings, findings_by_path(conn)[TRACK])

    section = rows(client.get("/review").text)

    assert "data-audit-eligible" not in section
    assert 'name="finding_id"' in section


def test_every_refusal_reason_is_reported_not_just_the_first(tmp_path: Path) -> None:
    client, conn, settings = scene(
        tmp_path,
        correction(),
        correction(LYRICS, "Music/Rock/x.lrc"),
        observation(),
        files=(TRACK, LYRICS, f"{ALBUM}/01.flac"),
    )
    accept_correction(conn, settings, findings_by_path(conn)[TRACK])
    (settings.library_dir / LYRICS).write_text("changed", encoding="utf-8")

    result = apply_audit_bulk(conn, settings, "accept", list(findings_by_path(conn).values()))

    assert "Already waiting for Commit: 1" in result
    assert "Changed since the audit" in result
    assert "Observation only: 1" in result


# --- one visual grammar -------------------------------------------------------


def test_both_sections_use_the_same_row_shell(tmp_path: Path) -> None:
    """Not a copy of the markup — the same CSS. `.row-shell` carries the grid,
    the padding and the border for an inbox row and a library row alike, so
    the two cannot drift into different shapes."""
    css = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")
    shell = css.split(".proposal,\n.row-shell {", 1)[1].split("}")[0]

    assert "display: grid" in shell
    assert "padding" in shell
    inbox = Path("src/librairy/web/templates/partials/review_row.html").read_text("utf-8")
    audit = Path("src/librairy/web/templates/partials/review_audit.html").read_text("utf-8")
    assert "row-shell" in inbox
    assert "row-shell" in audit


@pytest.mark.parametrize(
    "shared", ["row-head", "row-name", "row-meta", "row-line", "row-dest", "row-actions"]
)
def test_the_row_parts_are_shared_selectors(shared: str) -> None:
    css = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")
    audit = Path("src/librairy/web/templates/partials/review_audit.html").read_text("utf-8")

    assert f".{shared}" in css
    assert shared in audit


def test_why_and_preview_are_the_same_components(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())

    section = rows(client.get("/review").text)

    # The inbox row's own panel classes, not audit-specific ones.
    assert 'class="why"' in section
    assert "why-list" in section
    assert "proposal-preview" in section


def test_the_actions_use_the_inbox_button_hierarchy(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())

    section = rows(client.get("/review").text)

    assert 'class="btn-primary">Approve change' in section
    assert 'class="btn-ghost"' in section
    assert "action-gap" in section


def test_the_selection_column_matches_the_inbox(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, correction())
    inbox = Path("src/librairy/web/templates/partials/review_row.html").read_text("utf-8")

    section = rows(client.get("/review").text)

    assert 'class="row-pick"' in section
    assert "proposal-pick" in inbox


def test_an_observation_renders_no_empty_suggested_slot(tmp_path: Path) -> None:
    client, *_ = scene(
        tmp_path, observation(), files=(f"{ALBUM}/01.flac", f"{ALBUM}/02.flac")
    )

    section = rows(client.get("/review").text)

    assert "dest-arrow" not in section
    assert "change-after" not in section
    assert ALBUM.replace("&", "&amp;") in section


def test_a_one_component_change_is_shown_as_a_diff(tmp_path: Path) -> None:
    """A naming fix can be a single character. Nobody should diff two long
    paths by eye to find it."""
    client, conn, settings = scene(
        tmp_path,
        Finding(
            relpath="Photos/  Trip 2022/shot.jpg",
            kind="naming-inconsistency",
            severity="review",
            summary="Starts with a space.",
            dest_relpath="Photos/Trip 2022/shot.jpg",
        ),
        files=("Photos/  Trip 2022/shot.jpg",),
    )

    section = rows(client.get("/review").text)

    assert "change-before" in section
    assert "change-after" in section
    assert ">Photos/</span>" in section


def test_a_whole_path_change_shows_only_the_destination(tmp_path: Path) -> None:
    """Like the inbox row does. The old path is the row's own title."""
    client, *_ = scene(tmp_path, correction())

    section = rows(client.get("/review").text)

    assert "change-after" in section
    assert "change-before" not in section


def test_no_technical_caveat_is_repeated_on_every_row(tmp_path: Path) -> None:
    """It was on all six. The bar's label never claims a score, and the
    explanation lives in the docs rather than seven times down the page."""
    client, *_ = scene(tmp_path, correction(), observation())

    section = rows(client.get("/review").text)

    assert "single score" not in section
    assert "does not add" not in section


def test_the_zero_state_is_one_line(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))

    body = client.get("/review").text
    section = body.split('id="library-audit"', 1)[1]

    # Before an audit has ever run the line says nobody has looked, rather
    # than claiming a clean bill of health. Either way it is one line.
    assert "No audit has run yet" in section
    assert "audit-toolbar" not in section
    assert len(section.split("</section>")[0]) < 700


def test_every_section_announces_itself_the_same_way(tmp_path: Path) -> None:
    """Three lists now, and the claim is parity rather than arithmetic.

    Storage opportunities joined New files and Library Review. The point was
    never that there are two sections — it is that each one says what it is,
    in the same markup, so none of them looks like a continuation of the one
    above it.
    """
    client, *_ = scene(tmp_path, correction())

    body = client.get("/review").text

    headings = ["New files", "Library Review", "Storage opportunities"]
    assert body.count('class="section-head"') == len(headings)
    for heading in headings:
        assert f"<h2>{heading}</h2>" in body

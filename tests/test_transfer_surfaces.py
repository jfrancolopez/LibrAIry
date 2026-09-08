"""What the pages are allowed to say about backups — and the two things they are not.

Increment 8 adds no transfer semantics. It draws the ones already built, and
the whole risk is that drawing them quietly undoes them:

    a page could invent "up to date"
        `backup_runs` stores no such column, on purpose, because a flag like
        that only has to be wrong once. A template that computed one would
        have re-introduced it with none of the care.

    a page could present a partial observation as a whole one
        `divergence.record` takes `complete` with no default for exactly this
        reason. A count from a comparison that did not finish, shown alone, is
        a number pretending to be current.

The rest is calm: a drive in a drawer is where a backup drive lives, and
nothing on any page should suggest otherwise.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from librairy import attention, backup_runs, divergence, offline_drives, transfer_status
from librairy import destinations as dest
from librairy.config import Settings
from librairy.db import connect
from librairy.transfer_paths import MARKER
from librairy.transfer_plan import MANUAL, POLICY, Entry
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
    for directory in (
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
        settings.appdata_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def client_for(tmp_path: Path):  # noqa: ANN201
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def remote(conn, name: str = "NAS Backup", mode: str = dest.BACKUP) -> int:  # noqa: ANN001
    return dest.add_destination(
        conn,
        name=name,
        kind=dest.REMOTE,
        target=f"{name.split()[0].lower()}:/mnt/backup",
        modes=[mode],
    )


def drive(conn, settings, tmp_path: Path, name: str = "WD-8TB"):  # noqa: ANN001, ANN201
    mount = tmp_path / name
    mount.mkdir(parents=True, exist_ok=True)
    return offline_drives.register(conn, settings, name=name, path=str(mount)), mount


def extra(relpath: str, size: int = 5) -> Entry:
    return Entry(
        relpath=relpath, difference=dest.EXTRA, action=dest.REPORT, destination_size=size
    )


def text_of(html: str) -> str:
    body = html.split("</style>", 1)[-1]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


# --- the absence that has to survive being drawn --------------------------------------


def test_nothing_anywhere_claims_a_destination_is_up_to_date(tmp_path: Path) -> None:
    """The deliberate absence from increment 4, defended at the surface.

    Whether a destination is current is answered by comparing, every time it is
    asked. A page that said so from stored state would have re-invented the
    flag with none of the care that went into not having one.
    """
    client, conn, settings = client_for(tmp_path)
    destination_id = remote(conn)
    dest.set_policy(conn, category="photos", destination_id=destination_id, mode=dest.BACKUP)
    run = backup_runs.begin(
        conn, destination_id=destination_id, category="photos", mode=dest.BACKUP
    )
    backup_runs.finish(conn, run, succeeded=True, transferred=312)

    pages = "".join(
        text_of(client.get(where).text)
        for where in ("/settings", "/health", "/backups", "/dashboard")
    ).lower()

    for claim in ("up to date", "synced", "in sync", "fully backed up", "current backup"):
        assert claim not in pages, claim
    #  And what is offered instead: two dates, because they are two questions.
    assert "last attempted" in pages
    assert "last succeeded" in pages


def test_the_read_model_has_no_word_for_currentness() -> None:
    """Read from the syntax, so a future helper cannot add one quietly — and so
    that prose explaining why there is no such field does not count as one."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(transfer_status))
    names = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    } | {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    } | {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    for word in ("is_current", "up_to_date", "synced", "in_sync", "current"):
        assert word not in names, word


def test_last_attempted_and_last_succeeded_are_different_answers(tmp_path: Path) -> None:
    """A destination attempted hourly and last successful in March is exactly
    the state somebody needs to see, and one number cannot say it."""
    _client, conn, settings = client_for(tmp_path)
    destination_id = remote(conn)
    good = backup_runs.begin(
        conn, destination_id=destination_id, category="photos", mode=dest.BACKUP
    )
    backup_runs.finish(conn, good, succeeded=True, transferred=10)
    bad = backup_runs.begin(
        conn, destination_id=destination_id, category="photos", mode=dest.BACKUP
    )
    backup_runs.finish(conn, bad, succeeded=False, outcome="full")

    [view] = transfer_status.destination_views(conn, settings)

    assert view.last_failed
    assert view.last_attempted and view.last_succeeded
    #  Compared by which run each date came from rather than by the dates:
    #  `utc_now()` has one-second granularity and two runs in a test happen in
    #  the same second, which is the shape of bug this project has now met
    #  three times.
    assert backup_runs.last_run(conn, destination_id).id == bad
    assert backup_runs.last_success(conn, destination_id).id == good


def test_a_run_in_flight_does_not_erase_the_last_outcome(tmp_path: Path) -> None:
    _client, conn, settings = client_for(tmp_path)
    destination_id = remote(conn)
    failed = backup_runs.begin(
        conn, destination_id=destination_id, category="photos", mode=dest.BACKUP
    )
    backup_runs.finish(conn, failed, succeeded=False, outcome="full")
    backup_runs.begin(
        conn, destination_id=destination_id, category="music", mode=dest.BACKUP
    )

    [view] = transfer_status.destination_views(conn, settings)

    assert view.last_failed, "a run that had only started hid a failure"


def test_an_abandoned_run_is_interrupted_and_not_relabelled(tmp_path: Path) -> None:
    """A process killed mid-transfer leaves `running` for ever. Calling it
    succeeded or failed would be reporting an outcome nobody observed."""
    from datetime import UTC, datetime, timedelta

    client, conn, settings = client_for(tmp_path)
    destination_id = remote(conn)
    run = backup_runs.begin(
        conn, destination_id=destination_id, category="photos", mode=dest.BACKUP
    )
    long_ago = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
    conn.execute("UPDATE backup_runs SET started_at=? WHERE id=?", (long_ago, run))

    found = backup_runs.last_run(conn, destination_id)
    [view] = transfer_status.destination_views(conn, settings, runs=True)
    page = text_of(client.get("/backups").text)

    assert found.state == backup_runs.RUNNING, "the stored row was rewritten"
    assert found.unresolved
    assert view.runs[0].result == "Interrupted — outcome unknown"
    assert "Interrupted — outcome unknown" in page
    for wrong in ("Finished", "Failed"):
        assert wrong not in page


# --- a partial observation may not look like a whole one ------------------------------


def test_an_unverified_divergence_count_says_so(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    destination_id = remote(conn, "Studio Mirror", dest.MIRROR)
    divergence.record(
        conn,
        destination_id=destination_id,
        category="music",
        entries=[extra(f"Music/old-{index}.flac") for index in range(4)],
        complete=False,
    )

    [view] = transfer_status.destination_views(conn, settings)
    page = text_of(client.get(f"/backups/{destination_id}/only-here").text)

    assert "not fully verified" in view.only_here_sentence
    assert "not fully verified" in page


def test_a_complete_comparison_says_the_number_plainly(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    destination_id = remote(conn, "Studio Mirror", dest.MIRROR)
    divergence.record(
        conn,
        destination_id=destination_id,
        category="music",
        entries=[extra(f"Music/old-{index}.flac") for index in range(4)],
        complete=True,
    )

    [view] = transfer_status.destination_views(conn, settings)

    assert view.only_here_sentence == "4 only at the destination"
    assert "not fully verified" not in text_of(
        client.get(f"/backups/{destination_id}/only-here").text
    )


def test_the_divergence_page_reaches_every_file_not_a_sample(tmp_path: Path) -> None:
    """The correction from the increment-5 review, defended at the surface: a
    count with a sample under it cannot be worked through."""
    client, conn, _settings = client_for(tmp_path)
    destination_id = remote(conn, "Studio Mirror", dest.MIRROR)
    divergence.record(
        conn,
        destination_id=destination_id,
        category="music",
        entries=[extra(f"Music/Archive/track-{index:04d}.flac") for index in range(137)],
        complete=True,
    )

    seen: list[str] = []
    where = f"/backups/{destination_id}/only-here"
    for _ in range(20):
        html = client.get(where).text
        seen.extend(re.findall(r"<code>(Music/[^<]+)</code>", html))
        found = re.search(r'href="([^"]*only-here\?after=[^"]+)"', html)
        if not found:
            break
        where = found.group(1).replace("&amp;", "&")

    assert len(seen) == 137, f"the pages reached {len(seen)} of 137"  # noqa: PLR2004
    assert len(set(seen)) == len(seen)


# --- Health: what is wrong, what is worth knowing, what is normal ---------------------


def concerns(conn, settings) -> dict[str, str]:  # noqa: ANN001
    return {
        concern.code: concern.level
        for concern in attention.report(conn, settings).concerns
    }


def test_a_disconnected_drive_is_not_an_error(tmp_path: Path) -> None:
    """A registered drive is *supposed* to be in a drawer most of the time."""
    _client, conn, settings = client_for(tmp_path)
    registered, mount = drive(conn, settings, tmp_path)
    (mount / MARKER).unlink()
    offline_drives.look(conn, settings, registered)

    found = concerns(conn, settings)

    assert found.get("backup-drive-away") == attention.INFORMATION
    assert "backup-failing" not in found
    assert "backup-wrong-drive" not in found


def test_the_wrong_drive_needs_a_decision(tmp_path: Path) -> None:
    _client, conn, settings = client_for(tmp_path)
    registered, mount = drive(conn, settings, tmp_path)
    (mount / MARKER).write_text("librairy:not-ours\n", encoding="utf-8")
    offline_drives.look(conn, settings, registered)

    assert concerns(conn, settings).get("backup-wrong-drive") == attention.ACTION


def test_one_failure_is_worth_knowing_and_three_need_a_decision(
    tmp_path: Path,
) -> None:
    _client, conn, settings = client_for(tmp_path)
    destination_id = remote(conn)
    dest.set_policy(conn, category="photos", destination_id=destination_id, mode=dest.BACKUP)

    run = backup_runs.begin(
        conn, destination_id=destination_id, category="photos", mode=dest.BACKUP
    )
    backup_runs.finish(conn, run, succeeded=False, outcome="failed")
    assert concerns(conn, settings).get("backup-failing") == attention.ATTENTION

    for _ in range(attention.REPEATED):
        again = backup_runs.begin(
            conn, destination_id=destination_id, category="photos", mode=dest.BACKUP
        )
        backup_runs.finish(conn, again, succeeded=False, outcome="failed")

    assert concerns(conn, settings).get("backup-failing") == attention.ACTION


def test_only_at_the_destination_is_information_and_never_a_queue(
    tmp_path: Path,
) -> None:
    _client, conn, settings = client_for(tmp_path)
    destination_id = remote(conn, "Studio Mirror", dest.MIRROR)
    divergence.record(
        conn,
        destination_id=destination_id,
        category="music",
        entries=[extra("Music/old.flac")],
        complete=True,
    )

    [found] = [
        concern
        for concern in attention.report(conn, settings).concerns
        if concern.code == "backup-only-at-destination"
    ]

    assert found.level == attention.INFORMATION
    said = f"{found.headline} {found.detail}".lower()
    for word in ("delete", "remove them", "clean", "stale", "orphan", "extra"):
        assert word not in said, word


def test_marker_only_verification_is_shown_and_not_hidden(
    tmp_path: Path, monkeypatch
) -> None:
    from librairy import volumes

    _client, conn, settings = client_for(tmp_path)
    monkeypatch.setattr(volumes, "identity_for", lambda _path: "uuid:AAAA")
    registered, _mount = drive(conn, settings, tmp_path)
    monkeypatch.setattr(volumes, "identity_for", lambda _path: "")
    offline_drives.look(conn, settings, registered)

    [view] = transfer_status.destination_views(conn, settings)

    assert view.reduced
    assert concerns(conn, settings).get("backup-marker-only") == attention.INFORMATION


def test_a_quiet_installation_says_nothing_about_backups(tmp_path: Path) -> None:
    _client, conn, settings = client_for(tmp_path)

    found = concerns(conn, settings)

    assert not [code for code in found if code.startswith("backup-")]


# --- Settings: configuration, and nothing destructive ---------------------------------


def test_settings_offers_no_way_to_remove_anything_at_a_destination(
    tmp_path: Path,
) -> None:
    """There is no verb for it two layers down, so there is nothing for a
    button to post — and this is what would catch somebody inventing one."""
    client, conn, settings = client_for(tmp_path)
    remote(conn)
    drive(conn, settings, tmp_path)

    html = "".join(client.get(where).text for where in ("/settings", "/backups"))
    actions = set(re.findall(r'<form[^>]*action="([^"]+)"', html))

    for action in actions:
        for verb in ("delete", "purge", "prune", "clean", "sync", "remove-extra"):
            assert verb not in action.lower(), action
    words = text_of(html).lower()
    for phrase in ("clean destination", "remove extras", "sync exactly", "auto-delete"):
        assert phrase not in words, phrase


def test_no_rclone_option_can_be_typed_into_settings(tmp_path: Path) -> None:
    """A policy is a category, a destination and a mode. The adapter's
    allowlist is the only place options are decided, and there is no field on
    the page that reaches it."""
    client, conn, settings = client_for(tmp_path)
    remote(conn)
    drive(conn, settings, tmp_path)

    html = client.get("/settings").text
    names = set(re.findall(r'<(?:input|select|textarea)[^>]*name="([^"]+)"', html))

    for name in names:
        assert not name.startswith("--"), name
    for banned in ("flags", "options", "rclone_args", "extra_args", "command"):
        assert banned not in names, banned


def test_a_policy_can_be_added_removed_and_paused(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    destination_id = remote(conn)
    token = client.cookies["csrf_token"]

    client.post(
        "/settings/policies",
        data={
            "csrf_token": token,
            "category": "photos",
            "destination_id": destination_id,
            "mode": dest.BACKUP,
        },
        follow_redirects=False,
    )
    assert [policy.category for policy in dest.policies(conn)] == ["photos"]

    client.post(
        f"/settings/destinations/{destination_id}/enabled",
        data={"csrf_token": token, "enabled": "false"},
        follow_redirects=False,
    )
    assert dest.active(conn) == [], "a paused destination still had active work"
    [view] = transfer_status.destination_views(conn, settings)
    assert not view.enabled

    client.post(
        "/settings/policies/clear",
        data={"csrf_token": token, "category": "photos", "destination_id": destination_id},
        follow_redirects=False,
    )
    assert dest.policies(conn) == []


def test_the_same_place_cannot_be_two_destinations(tmp_path: Path) -> None:
    """The only way two enabled policies can cover the same files in two modes.
    Refused rather than given a precedence rule — a simple refusal is safer
    than a clever answer."""
    _client, conn, _settings = client_for(tmp_path)
    dest.add_destination(
        conn, name="One", kind=dest.REMOTE, target="nas:/mnt/backup", modes=[dest.BACKUP]
    )

    try:
        dest.add_destination(
            conn,
            name="Two",
            kind=dest.REMOTE,
            target="nas:/mnt/backup",
            modes=[dest.MIRROR],
        )
    except ValueError as refusal:
        assert "already a destination" in str(refusal)
    else:
        raise AssertionError("the same place became two destinations")


def test_checking_a_destination_writes_nothing_to_it(tmp_path: Path) -> None:
    """A check that wrote something to prove it could write would be a check
    that changed the thing it was checking."""
    client, conn, settings = client_for(tmp_path)
    registered, mount = drive(conn, settings, tmp_path)
    before = sorted(path.name for path in mount.iterdir())

    client.post(
        f"/settings/destinations/{registered.id}/verify",
        data={"csrf_token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )

    assert sorted(path.name for path in mount.iterdir()) == before
    assert offline_drives.presence(conn, registered.id).here


def test_forgetting_a_destination_touches_nothing_on_it(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    registered, mount = drive(conn, settings, tmp_path)
    (mount / "holiday.jpg").write_bytes(b"theirs")

    client.post(
        f"/settings/destinations/{registered.id}/forget",
        data={"csrf_token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )

    assert dest.destination(conn, registered.id) is None
    assert (mount / "holiday.jpg").read_bytes() == b"theirs"
    assert (mount / MARKER).exists()


# --- the Dashboard stays a dashboard --------------------------------------------------


def test_the_dashboard_block_is_small_and_a_drawer_is_not_an_alarm(
    tmp_path: Path,
) -> None:
    client, conn, settings = client_for(tmp_path)
    registered, mount = drive(conn, settings, tmp_path)
    (mount / MARKER).unlink()
    offline_drives.look(conn, settings, registered)

    found = transfer_status.overview(conn, settings)
    page = client.get("/dashboard").text

    assert found.disconnected == 1
    assert found.needs_looking_at == 0, "a drawer counted as something to look at"
    #  One block with a link out, not a backup console.
    assert page.count("/backups") <= 2  # noqa: PLR2004
    assert "hero-act" not in page.split("Backups", 1)[-1][:400]


def test_a_manual_send_reads_differently_from_a_schedule(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    registered, _mount = drive(conn, settings, tmp_path)
    scheduled = backup_runs.begin(
        conn,
        destination_id=registered.id,
        category="photos",
        mode=dest.OFFLINE,
        origin=POLICY,
    )
    backup_runs.finish(conn, scheduled, succeeded=True, transferred=3)
    sent = backup_runs.begin(
        conn,
        destination_id=registered.id,
        category="Photos/2024/Backyard",
        mode=dest.OFFLINE,
        origin=MANUAL,
    )
    backup_runs.finish(conn, sent, succeeded=True, transferred=29)

    page = text_of(client.get("/backups").text)

    assert "Sent from Browse" in page
    assert "Scheduled backup" in page


# --- nothing on any page carries a credential -----------------------------------------


def test_no_page_renders_anything_that_looks_like_a_secret(tmp_path: Path) -> None:
    """Redaction happens on the way *into* the record. These pages are the last
    place before a browser, and are checked as well."""
    client, conn, settings = client_for(tmp_path)
    destination_id = dest.add_destination(
        conn,
        name="Leaky",
        kind=dest.REMOTE,
        #  Nobody should be able to type this, and if they do it must not come
        #  back out on a page.
        target="https://user:hunter2@example.com/backup",
        modes=[dest.BACKUP],
    )
    run = backup_runs.begin(
        conn, destination_id=destination_id, category="photos", mode=dest.BACKUP
    )
    backup_runs.finish(
        conn,
        run,
        succeeded=False,
        outcome="failed",
        detail="rclone: --password hunter2 was rejected by the remote",
    )

    pages = "".join(
        client.get(where).text
        for where in ("/settings", "/health", "/backups", "/dashboard")
    )

    assert "hunter2" not in pages

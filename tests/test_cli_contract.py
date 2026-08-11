"""What a script is allowed to rely on.

Three things, and they are the whole contract:

* `--json` means JSON, wherever in the line you type it;
* stdout in JSON mode is exactly one document, success or failure;
* exit 0 means it happened (or there was nothing to do), 1 means it partly
  happened, 2 means it was refused.

The bug that prompted this: `librairy --json ai status` printed plain text,
because `ai status` declared its own `--json` and argparse copied that
subparser's default of `False` back over the global `True`. The tree audit
below makes that class of bug impossible to reintroduce quietly rather than
just fixing the one instance.

No AI provider, no catalog, no network — the one provider test here points at
a closed port on purpose.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from librairy import cli
from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root


def env_for(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "APPDATA_DIR": str(tmp_path / "appdata"),
            "INBOX_DIR": str(tmp_path / "inbox"),
            "LIBRARY_DIR": str(tmp_path / "library"),
            "QUARANTINE_DIR": str(tmp_path / "quarantine"),
            "FILE_STABILITY_SECONDS": "0",
            "AI_TIMEOUT": "1",
        }
    )
    env.update(overrides)
    return env


def run(tmp_path: Path, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "librairy", *args],
        env=env_for(tmp_path, **overrides),
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


def seed_vanished(tmp_path: Path, relpath: str = "gone.mkv") -> Settings:
    """One inbox item whose file has since disappeared."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    path = settings.inbox_dir / relpath
    path.write_text(relpath, encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = conn.execute("SELECT id FROM items WHERE relpath=?", (relpath,)).fetchone()[0]
    upsert_proposal(
        conn,
        item_id=item_id,
        category="shows",
        clean_name=relpath,
        dest_relpath=f"Shows/{relpath}",
        confidence=0.8,
        evidence=[EvidenceEntry("tvmaze", "show", "Best Shot", 0.8)],
    )
    path.unlink()
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    conn.commit()
    conn.close()
    return settings


def approved_plan(tmp_path: Path) -> str:
    settings_for(tmp_path)
    (tmp_path / "inbox" / "a.txt").write_text("a", encoding="utf-8")
    run(tmp_path, "scan")
    ops = tmp_path / "ops.json"
    ops.write_text(
        json.dumps(
            [
                {
                    "op_type": "move",
                    "src_relpath": "a.txt",
                    "dest_root": "library",
                    "dest_relpath": "Documents/a.txt",
                }
            ]
        ),
        encoding="utf-8",
    )
    plan_id = json.loads(run(tmp_path, "--json", "plan", "create", "--from-file", str(ops)).stdout)[
        "plan_id"
    ]
    run(tmp_path, "plan", "approve", plan_id)
    return plan_id


# --- the parser tree --------------------------------------------------------


def walk(parser: argparse.ArgumentParser, path: str = ""):
    """Every (command path, flag, action) in the tree, subcommands included."""
    children = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            children = action
            continue
        for flag in action.option_strings:
            yield path or "<global>", flag, action
    if children:
        for name, sub in children.choices.items():
            yield from walk(sub, f"{path} {name}".strip())


def test_no_subcommand_flag_shadows_a_global_one() -> None:
    """The audit, not the anecdote.

    `ai status` was the instance we found. This fails for any subcommand that
    redeclares a global flag with a real default, because argparse would copy
    that default back over whatever the user typed before the subcommand.
    """
    parser = cli.build_parser()
    globals_ = {
        flag for path, flag, _ in walk(parser) if path == "<global>" and flag != "--help"
    }
    offenders = [
        f"{path} {flag}"
        for path, flag, action in walk(parser)
        if path != "<global>" and flag in globals_ and action.default is not argparse.SUPPRESS
    ]
    assert offenders == []


def test_json_is_offered_by_every_subcommand() -> None:
    parser = cli.build_parser()
    paths = {path for path, _, _ in walk(parser)}
    with_json = {path for path, flag, _ in walk(parser) if flag == "--json"}
    assert paths == with_json, "every command takes --json, so placement never matters"


def test_group_commands_require_a_subcommand() -> None:
    """`librairy vanished` used to print nothing and exit 0."""
    for group in ("plan", "proposals", "quarantine", "db", "index", "ai", "vanished"):
        parser = cli.build_parser()
        try:
            parser.parse_args([group])
        except SystemExit as exc:
            assert exc.code == 2, group
        else:
            raise AssertionError(f"{group} accepted no subcommand")


def test_help_still_builds_for_every_command(tmp_path: Path) -> None:
    for args in (["--help"], ["ai", "--help"], ["vanished", "clear", "--help"]):
        result = run(tmp_path, *args)
        assert result.returncode == 0, args
        assert "usage: librairy" in result.stdout


# --- --json placement -------------------------------------------------------


def test_global_json_reaches_ai_status(tmp_path: Path) -> None:
    """The original bug: this printed `providers: 5` and a table."""
    result = run(tmp_path, "--json", "ai", "status")

    assert result.returncode == 0
    assert json.loads(result.stdout)["providers"]


def test_global_json_reaches_ai_test(tmp_path: Path) -> None:
    result = run(tmp_path, "--json", "ai", "test", "nosuch")

    assert json.loads(result.stdout)["error"] == "provider_not_found"


def test_both_placements_give_the_same_document(tmp_path: Path) -> None:
    before = run(tmp_path, "--json", "ai", "status")
    after = run(tmp_path, "ai", "status", "--json")

    assert before.stdout == after.stdout
    assert before.returncode == after.returncode == 0


def test_json_after_a_nested_subcommand_works(tmp_path: Path) -> None:
    seed_vanished(tmp_path)

    assert json.loads(run(tmp_path, "vanished", "list", "--json").stdout)["clearable"] == 1


def test_json_output_is_exactly_one_document(tmp_path: Path) -> None:
    seed_vanished(tmp_path)
    commands = (
        ("scan",),
        ("history",),
        ("quarantine", "list"),
        ("vanished", "list"),
        ("ai", "status"),
        ("db", "path"),
    )

    for command in commands:
        result = run(tmp_path, "--json", *command)
        decoder = json.JSONDecoder()
        payload, end = decoder.raw_decode(result.stdout)
        assert isinstance(payload, dict), command
        assert result.stdout[end:].strip() == "", f"{command} printed something after the JSON"


def test_a_refusal_in_json_mode_still_lands_on_stdout(tmp_path: Path) -> None:
    """So `librairy --json ... | jq .` is never handed half a stream."""
    seed_vanished(tmp_path)

    result = run(tmp_path, "--json", "vanished", "clear", "--root", "inbox")

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == "confirmation_required"
    assert "{" not in result.stderr


def test_human_mode_stays_human(tmp_path: Path) -> None:
    seed_vanished(tmp_path)

    result = run(tmp_path, "vanished", "list")

    assert result.stdout.startswith("clearable: 1")
    assert "{" not in result.stdout


def test_human_diagnostics_go_to_stderr(tmp_path: Path) -> None:
    seed_vanished(tmp_path)

    result = run(tmp_path, "vanished", "clear", "--root", "inbox")

    assert result.stdout == "", "a redirected stdout should not collect the complaint"
    assert "requires --yes" in result.stderr


# --- exit codes -------------------------------------------------------------


def test_success_exits_zero(tmp_path: Path) -> None:
    settings_for(tmp_path)
    (tmp_path / "inbox" / "a.txt").write_text("a", encoding="utf-8")

    result = run(tmp_path, "--json", "scan")

    assert result.returncode == 0
    assert json.loads(result.stdout)["hashed"] == 1


def test_a_safe_no_op_exits_zero(tmp_path: Path) -> None:
    """Nothing to do is an answer, not a fault."""
    settings_for(tmp_path)

    empty_scan = run(tmp_path, "--json", "scan")
    nothing_to_clear = run(tmp_path, "--json", "vanished", "clear", "--root", "inbox", "--yes")

    assert empty_scan.returncode == 0
    assert nothing_to_clear.returncode == 0
    assert json.loads(nothing_to_clear.stdout)["cleared"] == 0


def test_vanished_clear_refusal_exits_two(tmp_path: Path) -> None:
    seed_vanished(tmp_path)

    result = run(tmp_path, "--json", "vanished", "clear", "--root", "inbox")

    assert result.returncode == 2
    assert json.loads(result.stdout)["would_clear"] == 1
    assert json.loads(run(tmp_path, "--json", "vanished", "list").stdout)["clearable"] == 1


def test_vanished_clear_with_work_exits_zero(tmp_path: Path) -> None:
    seed_vanished(tmp_path)

    result = run(tmp_path, "--json", "vanished", "clear", "--root", "inbox", "--yes")

    assert result.returncode == 0
    assert json.loads(result.stdout)["cleared"] == 1


def test_commit_refusal_exits_two(tmp_path: Path) -> None:
    plan_id = approved_plan(tmp_path)

    result = run(tmp_path, "--json", "commit", plan_id)

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["error"] == "confirmation_required"
    assert payload["would_commit"] == plan_id


def test_undo_refusal_exits_two(tmp_path: Path) -> None:
    result = run(tmp_path, "--json", "undo", "--op", "1")

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["error"] == "confirmation_required"
    assert payload["would_undo"] == 1


def test_a_missing_argument_exits_two(tmp_path: Path) -> None:
    settings_for(tmp_path)

    result = run(tmp_path, "--json", "quarantine", "restore")

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == "argument_required"


def test_a_partly_finished_operation_exits_one(tmp_path: Path) -> None:
    """The plan was approved, then the file moved under it."""
    plan_id = approved_plan(tmp_path)
    (tmp_path / "inbox" / "a.txt").unlink()

    result = run(tmp_path, "--json", "commit", plan_id, "--yes")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["skipped_missing"] == 1
    assert payload["done"] == 0


def test_an_empty_answer_is_not_a_failure(tmp_path: Path) -> None:
    settings_for(tmp_path)

    result = run(tmp_path, "--json", "plan", "show", "no-such-plan")

    assert result.returncode == 0, "a plan that is not there is an empty answer, not an error"
    assert json.loads(result.stdout)["plan"] is None


def test_an_outright_failure_exits_two(tmp_path: Path) -> None:
    settings_for(tmp_path)

    result = run(tmp_path, "--json", "plan", "create", "--from-file", str(tmp_path / "nope.json"))

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == "internal_error"


def test_the_shell_reads_it_the_way_a_shell_reader_expects(tmp_path: Path) -> None:
    """`if librairy vanished clear ...; then` must take the right branch."""
    seed_vanished(tmp_path)
    script = (
        "if python -m librairy vanished clear --root inbox >/dev/null 2>&1; "
        "then echo cleared; else echo refused; fi; "
        "if python -m librairy vanished clear --root inbox --yes >/dev/null 2>&1; "
        "then echo cleared; else echo refused; fi"
    )

    result = subprocess.run(
        ["/bin/sh", "-c", script],
        env=env_for(tmp_path, PATH=f"{Path(sys.executable).parent}:{os.environ['PATH']}"),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.stdout.split() == ["refused", "cleared"]


# --- AI: the command versus what it reports ---------------------------------


def down_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def test_ai_status_succeeds_even_when_every_provider_is_offline(tmp_path: Path) -> None:
    """Reporting bad news is not the same as failing to report."""
    result = run(tmp_path, "--json", "ai", "status", OLLAMA_HOST=down_url())

    assert result.returncode == 0
    providers = json.loads(result.stdout)["providers"]
    assert providers and all(provider["last_ok_at"] is None for provider in providers)


def test_ai_test_against_a_dead_provider_exits_nonzero(tmp_path: Path) -> None:
    """Here the thing being asked for is the round trip, and it did not happen."""
    result = run(tmp_path, "--json", "ai", "test", "ollama-primary", OLLAMA_HOST=down_url())

    assert result.returncode == 1
    assert json.loads(result.stdout)["ok"] is False


def test_ai_test_naming_a_provider_that_does_not_exist_is_a_refusal(tmp_path: Path) -> None:
    result = run(tmp_path, "--json", "ai", "test", "nosuch")

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == "provider_not_found"


# --- the shared renderer ----------------------------------------------------


def emit(payload: dict[str, object], *, as_json: bool = False) -> tuple[str, str]:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        cli._emit(argparse.Namespace(json=as_json), payload)
    return out.getvalue(), err.getvalue()


def test_lists_render_one_line_each_not_a_python_repr() -> None:
    out, _ = emit({"entries": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]})

    assert out == "entries: 2\n  id=1  name=a\n  id=2  name=b\n"


def test_nested_dicts_render_without_a_python_repr() -> None:
    """`ai test` prints a health block; it used to arrive as `{'ok': True...}`."""
    out, _ = emit({"health": {"ok": True, "latency_ms": 12, "error": None}})

    assert out == "health: ok=True  latency_ms=12\n"


def test_nesting_inside_a_row_keeps_its_shape_without_a_repr() -> None:
    """`ai test --json`-less printed `models=('a', 'b')` until the live run."""
    out, _ = emit(
        {"health": {"ok": True, "models": ("a", "b")}, "answer": {"fields": {"title": "X"}}}
    )

    assert out == "health: ok=True  models=[a, b]\nanswer: fields=[title=X]\n"


def test_a_tuple_renders_like_a_list() -> None:
    out, _ = emit({"roots": ("inbox", "library")})

    assert out == "roots: 2\n  inbox\n  library\n"


def test_none_and_false_survive_the_renderer() -> None:
    out, _ = emit({"plan": None, "partial": False, "count": 0})

    assert out == "plan: None\npartial: False\ncount: 0\n"


def test_a_newline_in_a_value_does_not_look_like_a_new_key() -> None:
    out, _ = emit({"rationale": "line one\nline two"})

    assert out == "rationale: line one line two\n"


def test_unicode_is_not_escaped_in_human_output() -> None:
    out, _ = emit({"relpath": "Música/Café — ñ.mp3"})

    assert "Música/Café — ñ.mp3" in out


def test_json_mode_serialises_nested_and_unusual_values() -> None:
    out, _ = emit(
        {"path": Path("/data/library"), "entries": [{"n": 1}], "nested": {"a": [1, 2]}},
        as_json=True,
    )

    assert json.loads(out) == {
        "path": "/data/library",
        "entries": [{"n": 1}],
        "nested": {"a": [1, 2]},
    }


def test_json_mode_keeps_unicode_readable_and_valid() -> None:
    out, _ = emit({"relpath": "Música/ñ.mp3"}, as_json=True)

    assert json.loads(out)["relpath"] == "Música/ñ.mp3"


def test_quarantine_list_stays_readable(tmp_path: Path) -> None:
    settings_for(tmp_path)

    result = run(tmp_path, "quarantine", "list")

    assert result.stdout == "entries: 0\n"


def test_history_stays_readable(tmp_path: Path) -> None:
    plan_id = approved_plan(tmp_path)
    run(tmp_path, "commit", plan_id, "--yes")

    result = run(tmp_path, "history")

    assert result.stdout.startswith("history: 1\n  ")
    assert "action=move" in result.stdout
    assert "[{" not in result.stdout

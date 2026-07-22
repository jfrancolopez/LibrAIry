from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from librairy import __version__
from librairy.ai.base import ProviderConfig
from librairy.ai.orchestrator import provider_for_config
from librairy.ai.redact import build_view
from librairy.ai.registry import provider_chain
from librairy.ai.status import list_provider_status, upsert_provider_status
from librairy.classify import analyze_items
from librairy.config import Settings, validate_or_die
from librairy.content.extract import rebuild_content_index
from librairy.db import connect, database_path
from librairy.executor import execute_plan
from librairy.history import list_history, undo_op, undo_plan
from librairy.logging import configure_logging
from librairy.models import Item
from librairy.planner import (
    approve_plan,
    create_plan,
    create_plan_from_proposals,
    load_operation_specs,
)
from librairy.quarantine import list_quarantine_entries, restore_all, restore_entry
from librairy.scanner import scan_root
from librairy.search import rebuild_search_index
from librairy.supervisor import run_supervisor
from librairy.worker import run_forever, run_once


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="librairy",
        description="LibrAIry core safety engine",
    )
    parser.add_argument("--version", action="version", version=f"librairy {__version__}")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    subparsers = parser.add_subparsers(dest="command")

    scan = subparsers.add_parser("scan", help="Scan configured roots")
    scan.add_argument("--root", choices=["inbox", "library", "quarantine"], default="inbox")

    analyze = subparsers.add_parser("analyze", help="Analyze ready inbox items into proposals")
    analyze.add_argument("--limit", type=int)

    plan = subparsers.add_parser("plan", help="Create, inspect, and approve plans")
    plan_subparsers = plan.add_subparsers(dest="plan_command")
    create = plan_subparsers.add_parser("create", help="Create a draft plan")
    create.add_argument("--from-file", required=True)
    show = plan_subparsers.add_parser("show", help="Show a plan")
    show.add_argument("plan_id")
    approve = plan_subparsers.add_parser("approve", help="Approve a plan")
    approve.add_argument("plan_id")

    commit = subparsers.add_parser("commit", help="Execute an approved plan")
    commit.add_argument("plan_id")
    commit.add_argument("--yes", action="store_true", help="Confirm execution")

    history = subparsers.add_parser("history", help="Show operation history")
    history.add_argument("--plan")
    history.add_argument("-n", type=int, default=50)

    undo = subparsers.add_parser("undo", help="Undo history operations")
    group = undo.add_mutually_exclusive_group(required=True)
    group.add_argument("--op", type=int)
    group.add_argument("--plan")
    undo.add_argument("--yes", action="store_true", help="Confirm undo")

    proposals = subparsers.add_parser("proposals", help="Proposal utilities")
    proposal_subparsers = proposals.add_subparsers(dest="proposal_command")
    proposal_list = proposal_subparsers.add_parser("list", help="List proposals")
    proposal_list.add_argument("--status", default="proposed")
    proposal_show = proposal_subparsers.add_parser("show", help="Show proposal")
    proposal_show.add_argument("proposal_id", type=int)

    propose_plan = subparsers.add_parser("propose-plan", help="Create a draft plan from proposals")
    propose_plan.add_argument("--min-confidence", type=float, default=None)
    propose_plan.add_argument("--ids", nargs="*", type=int)

    quarantine = subparsers.add_parser("quarantine", help="Quarantine utilities")
    quarantine_subparsers = quarantine.add_subparsers(dest="quarantine_command")
    quarantine_subparsers.add_parser("list", help="List quarantine entries")
    quarantine_restore = quarantine_subparsers.add_parser(
        "restore", help="Restore quarantine entries"
    )
    quarantine_restore.add_argument("entry_id", nargs="?", type=int)
    quarantine_restore.add_argument("--all", action="store_true")

    db = subparsers.add_parser("db", help="Database utilities")
    db_subparsers = db.add_subparsers(dest="db_command")
    db_subparsers.add_parser("path", help="Print database path")
    db_subparsers.add_parser("migrate", help="Apply migrations")

    index = subparsers.add_parser("index", help="Search index utilities")
    index_subparsers = index.add_subparsers(dest="index_command")
    index_rebuild = index_subparsers.add_parser("rebuild", help="Rebuild the FTS search index")
    index_rebuild.add_argument(
        "--content",
        action="store_true",
        help="Rebuild document content FTS",
    )

    ai = subparsers.add_parser("ai", help="AI provider utilities")
    ai_subparsers = ai.add_subparsers(dest="ai_command")
    ai_status = ai_subparsers.add_parser("status", help="Show AI provider status")
    ai_status.add_argument("--json", action="store_true", help="Emit JSON output")
    ai_test = ai_subparsers.add_parser("test", help="Test an AI provider")
    ai_test.add_argument("provider", nargs="?")
    ai_test.add_argument("--json", action="store_true", help="Emit JSON output")

    worker = subparsers.add_parser("worker", help="Run the background worker")
    worker.add_argument("--once", action="store_true", help="Run one worker cycle and exit")

    subparsers.add_parser("run", help="Run web and worker under the supervisor")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        settings = validate_or_die()
        if args.command == "run":
            raise SystemExit(run_supervisor(settings))
        configure_logging(settings, component="cli", stream=sys.stderr)
        conn = connect(settings)
        result = _dispatch(args, conn, settings)
    except Exception as exc:
        _emit(args, {"error": str(exc)}, error=True)
        return 2
    if result is None:
        return 0
    _emit(args, result)
    if isinstance(result, dict) and result.get("partial"):
        return 1
    return 0


def _dispatch(args: argparse.Namespace, conn: sqlite3.Connection, settings: Settings):
    if args.command == "scan":
        root_path = getattr(settings, f"{args.root}_dir")
        summary = scan_root(conn, args.root, root_path, settings)
        return asdict(summary)
    if args.command == "analyze":
        return asdict(analyze_items(conn, settings, args.limit))
    if args.command == "plan":
        return _plan_command(args, conn, settings)
    if args.command == "commit":
        if not args.yes:
            return {"error": "commit requires --yes", "would_commit": args.plan_id}
        summary = execute_plan(conn, args.plan_id, settings)
        return asdict(summary) | {"partial": summary.partial}
    if args.command == "history":
        return {"history": [_row_dict(row) for row in list_history(conn, args.plan, args.n)]}
    if args.command == "undo":
        if not args.yes:
            return {"error": "undo requires --yes"}
        if args.op is not None:
            result = undo_op(conn, args.op, settings)
            return asdict(result)
        return {"results": [asdict(result) for result in undo_plan(conn, args.plan, settings)]}
    if args.command == "proposals":
        return _proposal_command(args, conn)
    if args.command == "propose-plan":
        min_confidence = (
            args.min_confidence
            if args.min_confidence is not None
            else settings.confidence_threshold
        )
        plan_id = create_plan_from_proposals(
            conn,
            settings,
            min_confidence=min_confidence,
            proposal_ids=args.ids,
        )
        return {"plan_id": plan_id, "status": "draft"}
    if args.command == "quarantine":
        return _quarantine_command(args, conn, settings)
    if args.command == "db":
        if args.db_command == "path":
            return {"path": str(database_path(settings))}
        if args.db_command == "migrate":
            return {"schema_version": conn.execute("PRAGMA user_version").fetchone()[0]}
    if args.command == "ai":
        return _ai_command(args, conn, settings)
    if args.command == "worker":
        if args.once:
            return asdict(run_once(conn, settings))
        run_forever(conn, settings)
        return {"stopped": True}
    if args.command == "index" and args.index_command == "rebuild":
        if args.content:
            return {"content_indexed": rebuild_content_index(conn, settings)}
        return {"indexed": rebuild_search_index(conn)}
    return None


def _plan_command(args: argparse.Namespace, conn: sqlite3.Connection, settings: Settings):
    if args.plan_command == "create":
        plan_id = create_plan(conn, load_operation_specs(Path(args.from_file)), settings)
        return {"plan_id": plan_id, "status": "draft"}
    if args.plan_command == "show":
        plan = conn.execute("SELECT * FROM plans WHERE id=?", (args.plan_id,)).fetchone()
        ops = conn.execute(
            "SELECT * FROM plan_ops WHERE plan_id=? ORDER BY seq",
            (args.plan_id,),
        ).fetchall()
        return {"plan": _row_dict(plan) if plan else None, "ops": [_row_dict(row) for row in ops]}
    if args.plan_command == "approve":
        plan_hash = approve_plan(conn, args.plan_id, settings)
        return {"plan_id": args.plan_id, "status": "approved", "plan_hash": plan_hash}
    return None


def _proposal_command(args: argparse.Namespace, conn: sqlite3.Connection):
    if args.proposal_command == "list":
        rows = conn.execute(
            "SELECT * FROM proposals WHERE status=? ORDER BY id",
            (args.status,),
        ).fetchall()
        return {"proposals": [_row_dict(row) for row in rows]}
    if args.proposal_command == "show":
        row = conn.execute("SELECT * FROM proposals WHERE id=?", (args.proposal_id,)).fetchone()
        return {"proposal": _row_dict(row) if row else None}
    return None


def _quarantine_command(args: argparse.Namespace, conn: sqlite3.Connection, settings: Settings):
    if args.quarantine_command == "list":
        return {"entries": [_row_dict(row) for row in list_quarantine_entries(conn)]}
    if args.quarantine_command == "restore":
        if args.all:
            return {"results": [asdict(result) for result in restore_all(conn, settings)]}
        if args.entry_id is None:
            return {"error": "restore requires entry_id or --all", "partial": True}
        return asdict(restore_entry(conn, args.entry_id, settings))
    return None


def _ai_command(args: argparse.Namespace, conn: sqlite3.Connection, settings: Settings):
    if args.ai_command == "status":
        provider_chain(conn, settings)
        return {"providers": [_row_dict(row) for row in list_provider_status(conn)]}
    if args.ai_command == "test":
        configs = provider_chain(conn, settings)
        config = _select_provider(configs, args.provider)
        if config is None:
            return {"ok": False, "error": "provider not found", "partial": True}
        provider = provider_for_config(config, settings)
        health = provider.health(settings.ai_timeout)
        answer = None
        if health.ok:
            answer = provider.classify(_synthetic_view(), settings.ai_timeout)
        ok = health.ok and answer is not None
        upsert_provider_status(conn, config, health, used=ok)
        return {
            "ok": ok,
            "provider": config.name,
            "health": asdict(health),
            "answer": answer.model_dump() if answer else None,
            "partial": not ok,
        }
    return None


def _select_provider(configs: list[ProviderConfig], name: str | None) -> ProviderConfig | None:
    if name is None:
        return configs[0] if configs else None
    return next((config for config in configs if config.name == name or config.kind == name), None)


def _synthetic_view():
    item = Item(
        id=0,
        root="inbox",
        relpath="synthetic-report.pdf",
        size=1024,
        mtime_ns=0,
        fingerprint=None,
        state="synthetic",
        first_seen_at="1970-01-01T00:00:00Z",
        last_seen_at="1970-01-01T00:00:00Z",
        missing_since=None,
    )
    return build_view(item, {"tags": {"title": "Synthetic Report"}}, ())


def _row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    return dict(row)


def _emit(args: argparse.Namespace, result: dict[str, object], error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    if getattr(args, "json", False):
        print(json.dumps(result, sort_keys=True), file=stream)
    else:
        for key, value in result.items():
            print(f"{key}: {value}", file=stream)

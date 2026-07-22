from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from librairy import __version__
from librairy.config import Settings, validate_or_die
from librairy.db import connect, database_path
from librairy.executor import execute_plan
from librairy.history import list_history, undo_op, undo_plan
from librairy.planner import approve_plan, create_plan, load_operation_specs
from librairy.scanner import scan_root


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

    db = subparsers.add_parser("db", help="Database utilities")
    db_subparsers = db.add_subparsers(dest="db_command")
    db_subparsers.add_parser("path", help="Print database path")
    db_subparsers.add_parser("migrate", help="Apply migrations")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        settings = validate_or_die()
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
    if args.command == "db":
        if args.db_command == "path":
            return {"path": str(database_path(settings))}
        if args.db_command == "migrate":
            return {"schema_version": conn.execute("PRAGMA user_version").fetchone()[0]}
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

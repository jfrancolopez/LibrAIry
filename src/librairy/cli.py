from __future__ import annotations

import argparse

from librairy import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="librairy",
        description="LibrAIry core safety engine",
    )
    parser.add_argument("--version", action="version", version=f"librairy {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("scan", help="Scan configured roots")
    plan = subparsers.add_parser("plan", help="Create, inspect, and approve plans")
    plan_subparsers = plan.add_subparsers(dest="plan_command")
    plan_subparsers.add_parser("create", help="Create a draft plan")
    plan_subparsers.add_parser("show", help="Show a plan")
    plan_subparsers.add_parser("approve", help="Approve a plan")
    subparsers.add_parser("commit", help="Execute an approved plan")
    subparsers.add_parser("history", help="Show operation history")
    subparsers.add_parser("undo", help="Undo history operations")
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

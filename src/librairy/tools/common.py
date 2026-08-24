from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from librairy.config import Settings


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: dict[str, Any] | list[Any] | None = None
    error: str | None = None


def run_json_tool(command: list[str], settings: Settings) -> ToolResult:
    binary = command[0]
    if shutil.which(binary) is None:
        return ToolResult(False, error=f"missing binary: {binary}")
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=settings.ai_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(False, error=f"timeout: {binary}")
    if result.returncode != 0:
        error = result.stderr.strip() or f"{binary} exited {result.returncode}"
        return ToolResult(False, error=error)
    try:
        return ToolResult(True, data=json.loads(result.stdout))
    except json.JSONDecodeError as exc:
        return ToolResult(False, error=f"invalid JSON from {binary}: {exc}")


#  The tools that own a cache row. Named here so a typo is a failed import
#  rather than a second, empty cache nobody notices — three of these describe
#  different things about one file and none is a substitute for another.
MEDIA_TOOL = "ffprobe-media"
DOCUMENT_TOOL = "document-meta"
IMAGE_TOOL = "exiftool-image"


def ensure_metadata_cache(conn: sqlite3.Connection) -> None:
    """The cache table, for a database that predates it being in the schema.

    One row per item **per tool**. It used to be one row per item, which is why
    a test failed if a second writer ever appeared: with `item_id` alone as the
    key, a document reader and a media reader would have taken turns
    overwriting each other's answer. Migration 037 gave each tool its own row;
    this keeps the same shape for anything that reaches here first.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS item_metadata (
          id          INTEGER PRIMARY KEY,
          item_id     INTEGER NOT NULL REFERENCES items(id),
          tool        TEXT NOT NULL,
          fingerprint TEXT NOT NULL,
          payload     TEXT NOT NULL,
          updated_at  TEXT NOT NULL,
          UNIQUE (item_id, tool)
        )
        """
    )


def get_cached_metadata(
    conn: sqlite3.Connection,
    item_id: int,
    fingerprint: str,
    tool: str,
) -> dict[str, Any] | None:
    ensure_metadata_cache(conn)
    row = conn.execute(
        "SELECT payload FROM item_metadata WHERE item_id=? AND fingerprint=? AND tool=?",
        (item_id, fingerprint, tool),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["payload"])


def set_cached_metadata(
    conn: sqlite3.Connection,
    item_id: int,
    fingerprint: str,
    tool: str,
    payload: dict[str, Any],
    updated_at: str,
) -> None:
    ensure_metadata_cache(conn)
    conn.execute(
        """
        INSERT INTO item_metadata(item_id, tool, fingerprint, payload, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(item_id, tool) DO UPDATE SET
          fingerprint=excluded.fingerprint,
          payload=excluded.payload,
          updated_at=excluded.updated_at
        """,
        (item_id, tool, fingerprint, json.dumps(payload, sort_keys=True), updated_at),
    )


def posix_path(path: Path) -> str:
    return path.as_posix()

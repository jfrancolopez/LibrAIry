"""What someone needs to reach these files from outside the portal.

LibrAIry serves no file-sharing protocol of its own — this page exists so you
never have to guess which path to hand to Samba, and so the difference between
the container's `/data/library` and the host's real directory is impossible to
miss. Getting that wrong is the single most common setup mistake: the container
path works inside the container and nowhere else.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.web.auth import has_admin_password


@dataclass(frozen=True)
class RootInfo:
    name: str
    host_path: str
    container_path: str
    files: int
    size: str
    shareable: bool
    note: str


def access_data(conn: sqlite3.Connection, settings: Settings) -> dict[str, object]:
    roots = [
        RootInfo(
            "library",
            str(settings.host_library_dir),
            str(settings.library_dir),
            *_usage(conn, "library"),
            True,
            "The organised library. This is the one to share.",
        ),
        RootInfo(
            "inbox",
            str(settings.host_inbox_dir),
            str(settings.inbox_dir),
            *_usage(conn, "inbox"),
            True,
            "Drop files here. Sharing it lets you add files from another machine.",
        ),
        RootInfo(
            "quarantine",
            str(settings.host_quarantine_dir),
            str(settings.quarantine_dir),
            *_usage(conn, "quarantine"),
            False,
            "Duplicates set aside. Restorable from the Quarantine tab.",
        ),
        RootInfo(
            "appdata",
            str(getattr(settings, "host_appdata_dir", "")) or "(not mapped)",
            str(settings.appdata_dir),
            0,
            "",
            False,
            "LibrAIry's own database and caches. Back it up; do not share it.",
        ),
    ]
    return {
        "roots": roots,
        "port": settings.dashboard_port,
        "puid": os.environ.get("PUID", "99"),
        "pgid": os.environ.get("PGID", "100"),
        "portal_protected": has_admin_password(conn),
        "auth_required": settings.auth_required,
        "library_host_path": str(settings.host_library_dir),
        "share_examples": _share_examples(str(settings.host_library_dir)),
    }


def _usage(conn: sqlite3.Connection, root: str) -> tuple[int, str]:
    row = conn.execute(
        "SELECT COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes FROM items WHERE root=?",
        (root,),
    ).fetchone()
    return int(row["files"]), human_bytes(int(row["bytes"]))


def _share_examples(library: str) -> list[dict[str, str]]:
    """Commands with the real path already filled in.

    Retyping a path from a screenshot is where typos come from, so these are
    meant to be copied rather than read.
    """
    share = Path(library).name or "library"
    return [
        {
            "system": "UNRAID",
            "detail": "Shares → the share containing this path → SMB: Export = Yes.",
            "command": f"# host path to export\n{library}",
        },
        {
            "system": "Linux (Samba)",
            "detail": "Add to /etc/samba/smb.conf, then: sudo systemctl restart smbd",
            "command": (
                f"[{share}]\n"
                f"   path = {library}\n"
                f"   browseable = yes\n"
                f"   read only = yes\n"
                f"   guest ok = no"
            ),
        },
        {
            "system": "macOS",
            "detail": "System Settings → General → Sharing → File Sharing → add this folder.",
            "command": f"open 'smb://$(hostname -s).local/{share}'",
        },
        {
            "system": "Windows",
            "detail": "Right-click the folder → Properties → Sharing → Advanced Sharing.",
            "command": f"net use L: \\\\SERVER\\{share} /persistent:yes",
        },
    ]


def human_bytes(size: int | None) -> str:
    if not size or size < 0:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "0 B"

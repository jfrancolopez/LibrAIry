from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from librairy.config import Settings

SCHEMA_VERSION = 11


class DatabaseVersionError(RuntimeError):
    pass


MIGRATION_001 = """
CREATE TABLE items (
  id            INTEGER PRIMARY KEY,
  root          TEXT NOT NULL CHECK (root IN ('inbox','library','quarantine')),
  relpath       TEXT NOT NULL,
  size          INTEGER NOT NULL,
  mtime_ns      INTEGER NOT NULL,
  fingerprint   TEXT,
  state         TEXT NOT NULL DEFAULT 'discovered',
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  missing_since TEXT,
  UNIQUE (root, relpath)
);
CREATE TABLE plans (
  id          TEXT PRIMARY KEY,
  status      TEXT NOT NULL CHECK (status IN ('draft','approved','executing','done','failed')),
  plan_hash   TEXT,
  created_at  TEXT NOT NULL,
  approved_at TEXT,
  finished_at TEXT
);
CREATE TABLE plan_ops (
  id              INTEGER PRIMARY KEY,
  plan_id         TEXT NOT NULL REFERENCES plans(id),
  seq             INTEGER NOT NULL,
  op_type         TEXT NOT NULL CHECK (op_type IN ('move','quarantine')),
  item_id         INTEGER REFERENCES items(id),
  src_root        TEXT NOT NULL,
  src_relpath     TEXT NOT NULL,
  src_fingerprint TEXT NOT NULL,
  dest_root       TEXT NOT NULL,
  dest_relpath    TEXT NOT NULL,
  result          TEXT,
  final_relpath   TEXT,
  executed_at     TEXT,
  UNIQUE (plan_id, seq),
  UNIQUE (plan_id, src_root, src_relpath)
);
CREATE TABLE history (
  id          INTEGER PRIMARY KEY,
  ts          TEXT NOT NULL,
  plan_id     TEXT,
  op_id       INTEGER,
  action      TEXT NOT NULL,
  src_root    TEXT NOT NULL,
  src_relpath TEXT NOT NULL,
  dest_root   TEXT NOT NULL,
  dest_relpath TEXT NOT NULL,
  fingerprint TEXT,
  outcome     TEXT NOT NULL
);
CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  csrf_token TEXT NOT NULL
);
CREATE INDEX idx_items_fingerprint ON items(fingerprint);
CREATE INDEX idx_items_state ON items(state);
CREATE INDEX idx_plan_ops_plan_id ON plan_ops(plan_id);
CREATE INDEX idx_history_plan_id ON history(plan_id);
"""

MIGRATION_002 = """
CREATE TABLE groups (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('album','season','photo_event','project','archive')),
  label      TEXT NOT NULL,
  dest_base  TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE proposals (
  id            INTEGER PRIMARY KEY,
  item_id       INTEGER NOT NULL REFERENCES items(id),
  category      TEXT NOT NULL CHECK (category IN
                  ('music','movies','shows','photos','documents','books','projects','misc')),
  clean_name    TEXT NOT NULL,
  dest_relpath  TEXT,
  confidence    REAL NOT NULL,
  group_id      INTEGER REFERENCES groups(id),
  status        TEXT NOT NULL DEFAULT 'proposed'
                CHECK (status IN (
                  'proposed','approved','rejected','postponed','committed','superseded'
                )),
  evidence      TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (item_id)
);
CREATE INDEX idx_proposals_status ON proposals(status);
CREATE INDEX idx_proposals_category ON proposals(category);
CREATE INDEX idx_proposals_group_id ON proposals(group_id);
CREATE INDEX idx_groups_kind ON groups(kind);
"""

MIGRATION_003 = """
CREATE TABLE provider_status (
  name         TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  endpoint     TEXT,
  model        TEXT,
  enabled      INTEGER NOT NULL DEFAULT 0,
  last_ok_at   TEXT,
  last_error   TEXT,
  latency_ms   INTEGER,
  last_used_at TEXT
);
CREATE INDEX idx_provider_status_kind ON provider_status(kind);
CREATE INDEX idx_provider_status_enabled ON provider_status(enabled);
"""

MIGRATION_004 = """
ALTER TABLE provider_status ADD COLUMN available_models TEXT NOT NULL DEFAULT '[]';
"""

MIGRATION_005 = """
CREATE TABLE worker_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE similar_media_flags (
  id              INTEGER PRIMARY KEY,
  item_id         INTEGER NOT NULL REFERENCES items(id),
  similar_item_id INTEGER NOT NULL REFERENCES items(id),
  kind            TEXT NOT NULL CHECK (kind IN ('image','video','audio','duplicate')),
  score           REAL,
  status          TEXT NOT NULL DEFAULT 'review'
                  CHECK (status IN ('review','dismissed','resolved')),
  created_at      TEXT NOT NULL,
  UNIQUE (item_id, similar_item_id, kind)
);
CREATE INDEX idx_similar_media_flags_status ON similar_media_flags(status);
CREATE INDEX idx_similar_media_flags_item_id ON similar_media_flags(item_id);
"""

MIGRATION_006 = """
CREATE TABLE quarantine_entries (
  id               INTEGER PRIMARY KEY,
  item_id          INTEGER NOT NULL REFERENCES items(id),
  reason           TEXT NOT NULL CHECK (reason IN ('exact_duplicate','similar_media','user')),
  duplicate_of     INTEGER REFERENCES items(id),
  original_root    TEXT NOT NULL,
  original_relpath TEXT NOT NULL,
  quarantined_at   TEXT,
  restored_at      TEXT,
  plan_id          TEXT
);
CREATE INDEX idx_quarantine_entries_item_id ON quarantine_entries(item_id);
CREATE INDEX idx_quarantine_entries_restored_at ON quarantine_entries(restored_at);
"""

MIGRATION_007 = """
ALTER TABLE proposals ADD COLUMN action TEXT NOT NULL DEFAULT 'move';
ALTER TABLE proposals ADD COLUMN dest_root TEXT NOT NULL DEFAULT 'library';
"""

MIGRATION_008 = """
CREATE VIRTUAL TABLE search_fts USING fts5(
  name,
  clean_name,
  tags,
  artist,
  album,
  title,
  show,
  genre,
  event,
  category UNINDEXED,
  root UNINDEXED,
  item_id UNINDEXED,
  tokenize='unicode61 remove_diacritics 2'
);
"""

MIGRATION_009 = """
CREATE VIRTUAL TABLE content_fts USING fts5(
  text,
  item_id UNINDEXED,
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE content_extractions (
  item_id      INTEGER PRIMARY KEY REFERENCES items(id),
  fingerprint  TEXT NOT NULL,
  extractor    TEXT,
  chars        INTEGER NOT NULL DEFAULT 0,
  truncated    INTEGER NOT NULL DEFAULT 0,
  attempts     INTEGER NOT NULL DEFAULT 0,
  extracted_at TEXT,
  error        TEXT
);
CREATE INDEX idx_content_extractions_error ON content_extractions(error);
"""

MIGRATION_010 = """
CREATE TABLE backup_queue (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL REFERENCES items(id),
  relpath     TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  state       TEXT NOT NULL DEFAULT 'queued'
              CHECK (state IN ('queued','copying','done','failed')),
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE (item_id, relpath, fingerprint)
);
CREATE INDEX idx_backup_queue_state ON backup_queue(state);
CREATE INDEX idx_backup_queue_item_id ON backup_queue(item_id);
"""

# Repair, not schema. Until this release, proposals were marked committed only
# by the web commit route, and only when the whole plan succeeded — so a plan
# with one skipped file, or any commit made from the CLI, left proposals sitting
# at 'proposed' for files that had already moved. They came back in Review as
# rows proposing to move a file to exactly where it already was: 140 of 239 on
# the author's machine.
#
# The condition is deliberately narrow. A proposal is only closed when the item
# is *already standing at that proposal's destination*, which can only be true
# if the move happened. No file is touched, and nothing still to do is affected.
MIGRATION_011 = """
UPDATE proposals
SET status='committed', updated_at=datetime('now')
WHERE status='proposed'
  AND item_id IN (
    SELECT p.item_id FROM proposals p JOIN items i ON i.id = p.item_id
    WHERE p.dest_relpath IS NOT NULL
      AND i.root = p.dest_root
      AND i.relpath = p.dest_relpath
  );
"""

MIGRATIONS = {
    1: MIGRATION_001,
    2: MIGRATION_002,
    3: MIGRATION_003,
    4: MIGRATION_004,
    5: MIGRATION_005,
    6: MIGRATION_006,
    7: MIGRATION_007,
    8: MIGRATION_008,
    9: MIGRATION_009,
    10: MIGRATION_010,
    11: MIGRATION_011,
}


def database_path(settings: Settings) -> Path:
    return settings.appdata_dir / "librairy.db"


# WAL coordinates readers and writers through a shared-memory file that SQLite
# maps with mmap. That only works when every process sees the same bytes. On a
# network or FUSE filesystem the -shm mapping is effectively per-process, so the
# web process and the worker each believe they own the WAL and the index rots.
#
# This is not theoretical: Docker Desktop for macOS corrupted LibrAIry's index
# within a single analyze run, twice, and UNRAID's /mnt/user shares are the same
# class of filesystem.
#
# This is an allowlist rather than a blocklist on purpose. Docker Desktop
# reports its bind mounts as "fakeowner", which no blocklist would have
# anticipated, and the next runtime will invent another name. An unrecognised
# filesystem is far more likely to be exotic than to be a plain local disk, and
# the two ways of being wrong are not symmetric: guessing DELETE costs some
# write throughput, guessing WAL costs the index.
WAL_SAFE_FSTYPES = frozenset(
    {
        "btrfs",
        "exfat",
        "ext2",
        "ext3",
        "ext4",
        "f2fs",
        "jfs",
        "overlay",  # the container's own writable layer, always local
        "reiserfs",
        "tmpfs",
        "vfat",
        "xfs",
        "zfs",
    }
)


def connect(settings: Settings | None = None, path: Path | None = None) -> sqlite3.Connection:
    if settings is None:
        settings = Settings()
    db_path = path or database_path(settings)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_pragmas(conn, journal_mode=journal_mode_for(db_path))
    migrate(conn)
    return conn


def apply_pragmas(conn: sqlite3.Connection, journal_mode: str = "WAL") -> None:
    conn.execute(f"PRAGMA journal_mode={journal_mode}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


def journal_mode_for(db_path: Path) -> str:
    """WAL on a local disk, DELETE where its shared memory cannot be trusted.

    DELETE is slower under write load but uses only POSIX advisory locks, which
    these filesystems do implement correctly. Losing some throughput on a
    network share is a fair trade for an index that survives the night.

    Off Linux there is no /proc/self/mountinfo to consult; that is the developer
    path, one process at a time, so WAL stands.
    """
    override = os.environ.get("SQLITE_JOURNAL_MODE", "").strip().upper()
    if override in {"WAL", "DELETE", "TRUNCATE", "PERSIST"}:
        return override
    fstype = filesystem_type(db_path)
    if fstype is None:
        return "WAL"
    return "WAL" if fstype in WAL_SAFE_FSTYPES else "DELETE"


def filesystem_type(target: Path) -> str | None:
    """Filesystem backing `target`, from /proc/self/mountinfo. None off Linux.

    Returns the type of the *longest* matching mount point, so a bind mount at
    /data/appdata wins over the / it sits inside.
    """
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target

    best: tuple[int, str] | None = None
    for line in mountinfo.splitlines():
        head, _, tail = line.partition(" - ")
        fields = head.split()
        details = tail.split()
        if len(fields) < 5 or not details:
            continue
        mount_point, fstype = fields[4], details[0]
        try:
            mount_path = Path(mount_point)
            if resolved != mount_path and mount_path not in resolved.parents:
                continue
        except (OSError, ValueError):
            continue
        depth = len(mount_path.parts)
        if best is None or depth > best[0]:
            best = (depth, fstype)
    return best[1] if best else None


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> None:
    current = user_version(conn)
    starting_version = current
    if current > SCHEMA_VERSION:
        raise DatabaseVersionError(
            f"Database schema version {current} is newer than this code supports "
            f"({SCHEMA_VERSION}); refusing to write."
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        migration = MIGRATIONS[version]
        try:
            conn.executescript(f"BEGIN;\n{migration}\nPRAGMA user_version={version};\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    if starting_version < 8 <= SCHEMA_VERSION:
        from librairy.search import rebuild_search_index

        rebuild_search_index(conn)

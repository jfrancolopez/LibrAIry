from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from librairy.config import Settings

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 59


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
                  ('music','music_videos','movies','shows','photos','documents',
                   'books','projects','misc')),
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

MIGRATION_012 = """
CREATE TABLE duplicate_reports (
  item_id    INTEGER NOT NULL REFERENCES items(id),
  other_id   INTEGER NOT NULL REFERENCES items(id),
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (item_id, other_id)
);
CREATE INDEX idx_duplicate_reports_other ON duplicate_reports(other_id);
"""

MIGRATION_013 = """
CREATE TABLE review_undo (
  id          INTEGER PRIMARY KEY,
  action      TEXT NOT NULL,
  summary     TEXT NOT NULL,
  snapshot    TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
"""

#  A ripped disc is a group like an album is a group, and it is not any of the
#  five kinds already listed. Calling it an "archive" to avoid a migration would
#  put a wrong word in the data forever to save one table rebuild.
MIGRATION_014 = """
CREATE TABLE groups_new (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN
               ('album','season','photo_event','project','archive','disc')),
  label      TEXT NOT NULL,
  dest_base  TEXT,
  created_at TEXT NOT NULL
);
INSERT INTO groups_new(id, kind, label, dest_base, created_at)
  SELECT id, kind, label, dest_base, created_at FROM groups;
DROP TABLE groups;
ALTER TABLE groups_new RENAME TO groups;
-- Dropping the table took its index with it.
CREATE INDEX idx_groups_kind ON groups(kind);
"""

#  What a local model saw when it looked at an image. One row per item, keyed
#  by the fingerprint it was looking at: re-analysing a file that has not
#  changed reuses the answer rather than spending another pass of inference on
#  a picture that is still the same picture.
#
#  Kept out of the evidence blob because it is the one piece of analysis that
#  is worth reading on its own — a caption and the text out of a screenshot are
#  what a search will want long after the proposal it belonged to is committed
#  and gone.
MIGRATION_015 = """
CREATE TABLE vision_results (
  item_id      INTEGER PRIMARY KEY REFERENCES items(id),
  fingerprint  TEXT NOT NULL,
  provider     TEXT NOT NULL,
  model        TEXT NOT NULL,
  category     TEXT,
  caption      TEXT,
  subjects     TEXT NOT NULL DEFAULT '[]',
  tags         TEXT NOT NULL DEFAULT '[]',
  -- Stored so that re-analysing an unchanged file rebuilds the same filename
  -- from the cache. Without them the second pass would quietly drop the words
  -- the first pass added.
  name_tokens  TEXT NOT NULL DEFAULT '[]',
  visible_text TEXT,
  confidence   REAL,
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_vision_results_fingerprint ON vision_results(fingerprint);
"""

MIGRATION_016 = """
CREATE TABLE audit_findings (
  id            INTEGER PRIMARY KEY,
  -- NULL for a physical file nothing has indexed: the audit can see it even
  -- though no items row exists yet, and saying so is one of its findings.
  item_id       INTEGER REFERENCES items(id),
  root          TEXT NOT NULL,
  relpath       TEXT NOT NULL,
  kind          TEXT NOT NULL,
  severity      TEXT NOT NULL,
  summary       TEXT NOT NULL,
  -- NULL for an observation. Not every finding is a move, and overloading a
  -- destination onto "this album has no cover" would make Commit believe it
  -- had somewhere to put something.
  dest_root     TEXT,
  dest_relpath  TEXT,
  evidence      TEXT NOT NULL DEFAULT '[]',
  -- What the file was when the finding was made. Applying a correction to a
  -- file that has changed since is exactly what must not happen.
  fingerprint   TEXT,
  status        TEXT NOT NULL DEFAULT 'open',
  detected_at   TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  -- Re-running the audit re-finds the same thing; it must not stack up.
  UNIQUE(root, relpath, kind)
);
CREATE INDEX idx_audit_findings_status ON audit_findings(status);
CREATE INDEX idx_audit_findings_kind ON audit_findings(kind);
"""

MIGRATION_017 = """
-- Which audit finding a plan is executing, if any. A plan with this set is a
-- correction to a file the owner already had, not a new file being filed, and
-- Commit and History both have to be able to say so out loud.
ALTER TABLE plans ADD COLUMN audit_finding_id INTEGER REFERENCES audit_findings(id);
-- Whether an operation is the file the finding is about or a companion coming
-- with it. Presentation, and the reason a partial result can say which half
-- of an album moved.
ALTER TABLE plan_ops ADD COLUMN role TEXT NOT NULL DEFAULT 'primary';
-- The plan an accepted finding is waiting on, so Review can say "waiting for
-- Commit" and a finding cannot be accepted twice into two competing plans.
ALTER TABLE audit_findings ADD COLUMN plan_id TEXT REFERENCES plans(id);
CREATE INDEX idx_plans_audit_finding ON plans(audit_finding_id);
"""

MIGRATION_018 = """
-- What a catalog said this folder is, kept so the next audit does not have to
-- ask again by string.
--
-- Identity used to be discarded the moment classification finished, which is
-- why cover art could not be fetched later: nothing remembered that
-- `Music/R&BSoul/Alicia Keys/Unplugged (20th Anniversary)` was MusicBrainz
-- release 8f3b… A string search is not an identity — it is a guess that has
-- to be re-made, re-rate-limited and re-risked on every pass.
--
-- Deliberately not the whole response. Five columns that stay useful: who
-- answered, what kind of thing it is, its stable id, and the canonical names,
-- which are the only part a finding ever quotes.
CREATE TABLE catalog_identity (
  id INTEGER PRIMARY KEY,
  -- 'album' | 'movie' | 'show'. What `scope_key` names.
  scope_kind TEXT NOT NULL,
  -- The library-relative folder. Identity belongs to the folder, not to each
  -- of its forty tracks.
  scope_key TEXT NOT NULL,
  provider TEXT NOT NULL,
  entity TEXT NOT NULL,
  -- Empty means "asked, and this provider had no answer". Kept, so a fruitless
  -- lookup is not repeated on every audit; `looked_up_at` is how it expires.
  catalog_id TEXT NOT NULL DEFAULT '',
  canonical_title TEXT NOT NULL DEFAULT '',
  canonical_artist TEXT NOT NULL DEFAULT '',
  artist_id TEXT NOT NULL DEFAULT '',
  looked_up_at TEXT NOT NULL,
  UNIQUE(scope_kind, scope_key, provider)
);
CREATE INDEX idx_catalog_identity_scope ON catalog_identity(scope_kind, scope_key);
"""

MIGRATION_019 = """
-- One requested audit. Not a queue of many: an audit is idempotent and the
-- second request for the same scope is the same question, so a pending run is
-- reused rather than stacked.
--
-- This is a row, not a thread. The existing worker picks it up *after* inbox
-- work, one bounded slice per cycle, so a full library reconciliation can
-- never stand between a newly dropped file and its proposal. There is no
-- daemon, no schedule and no second process — deliberately, and see
-- `audit_job` for the whole argument.
CREATE TABLE audit_runs (
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL DEFAULT '',
  -- queued | running | complete | failed | cancelled
  state TEXT NOT NULL DEFAULT 'queued',
  -- Which stage is next to run. Stages are the resume point: a slice that
  -- runs out of time leaves the stage unchanged and continues next cycle.
  stage TEXT NOT NULL DEFAULT 'scan',
  -- JSON. What has been counted so far, so the page can say "89 / 140 files"
  -- rather than "working".
  counters TEXT NOT NULL DEFAULT '{}',
  -- Set by the user; read between items. An audit only ever reads the
  -- library, so stopping half way leaves nothing half done.
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  requested_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX idx_audit_runs_state ON audit_runs(state);
"""

MIGRATION_020 = """
-- Storage opportunities: things that *could* be smaller, and never are.
--
-- A separate table from `audit_findings`, deliberately. A badly organised
-- album needs attention; a 10 GB film that could be 6 GB is merely an
-- opportunity, and mixing the two turns a list of problems into a list of
-- suggestions nobody finishes reading. It also keeps the selection scopes
-- apart: an `opportunity_id` must never be accepted by an endpoint that
-- expects a `finding_id`, and separate tables make that structural rather
-- than a convention.
--
-- Nothing in this table has been done. Discovery is `ffprobe` and arithmetic;
-- conversion is a queued job that a person asks for.
CREATE TABLE optimization_opportunities (
  id INTEGER PRIMARY KEY,
  item_id INTEGER REFERENCES items(id),
  root TEXT NOT NULL DEFAULT 'library',
  relpath TEXT NOT NULL,
  -- audio-to-flac | video-remux | video-transcode
  kind TEXT NOT NULL,
  -- lossless | remux | lossy | derivative. What it costs you, not how big the
  -- number is: "save 4 GB" means nothing without this.
  quality TEXT NOT NULL,
  current_bytes INTEGER NOT NULL DEFAULT 0,
  -- Arithmetic on a bitrate, never a measurement. A job that runs records its
  -- result separately so estimate and actual can be compared later.
  estimated_bytes INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  compute TEXT NOT NULL DEFAULT 'medium',
  from_label TEXT NOT NULL DEFAULT '',
  to_label TEXT NOT NULL DEFAULT '',
  -- Non-empty when the file sits inside a protected root. Such a row is still
  -- recorded and still shown; it simply cannot be queued.
  protected_by TEXT NOT NULL DEFAULT '',
  facts TEXT NOT NULL DEFAULT '[]',
  -- What the file was when this was decided. A dismissal is tied to it, so a
  -- changed file is reconsidered rather than staying silent forever.
  fingerprint TEXT,
  -- Which version of the rules produced this. Without it, a `No suggestion`
  -- recorded against a weak early rule would suppress a much better later one
  -- for the life of the file.
  rule_version INTEGER NOT NULL DEFAULT 1,
  -- open | dismissed
  status TEXT NOT NULL DEFAULT 'open',
  detected_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (root, relpath, kind)
);
CREATE INDEX idx_optimization_status ON optimization_opportunities(status);
"""


MIGRATION_021 = """
-- One approved optimization, frozen at the moment the user approved it.
--
-- A queued job means: *this* operation, against *this* file, as it was when
-- somebody looked at it. So the source fingerprint, the preset and the
-- estimates are copied in rather than looked up later. The worker never asks
-- the advisor again — if it did, an application upgrade could quietly change
-- what runs, and the user would have approved one thing and got another.
--
-- The same principle as an immutable commit plan, for the same reason.
CREATE TABLE optimization_jobs (
  id INTEGER PRIMARY KEY,
  opportunity_id INTEGER REFERENCES optimization_opportunities(id),
  item_id INTEGER REFERENCES items(id),
  root TEXT NOT NULL DEFAULT 'library',
  relpath TEXT NOT NULL,
  -- What the file was when this was approved. Revalidated before the job may
  -- start; a mismatch stops it rather than silently re-targeting new bytes.
  fingerprint TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  quality TEXT NOT NULL,
  from_label TEXT NOT NULL DEFAULT '',
  to_label TEXT NOT NULL DEFAULT '',
  -- The named operation, not an ffmpeg command line. A job describes intent;
  -- the argv is built later by trusted code from that intent, so nothing a
  -- form can post ever reaches a subprocess.
  preset TEXT NOT NULL,
  preset_version INTEGER NOT NULL DEFAULT 1,
  rule_version INTEGER NOT NULL DEFAULT 1,
  source_bytes INTEGER NOT NULL DEFAULT 0,
  estimated_bytes INTEGER NOT NULL DEFAULT 0,
  -- Kept apart from the estimate above, always. Overwriting one with the
  -- other would destroy the only way to find out whether the advisor is any
  -- good.
  actual_bytes INTEGER,
  runtime_seconds REAL,
  -- manual | window. Which gate this job asked to wait behind.
  run_policy TEXT NOT NULL DEFAULT 'window',
  -- queued | waiting | running | verifying | ready | failed | cancelled |
  -- stale. Small on purpose: the *reason* for waiting is a separate column,
  -- so a new reason never needs a new state.
  state TEXT NOT NULL DEFAULT 'queued',
  wait_reason TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT '',
  staging_dir TEXT NOT NULL DEFAULT '',
  queued_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_optimization_jobs_state ON optimization_jobs(state);
-- One live job per file per operation. Enforced here rather than in the
-- handler, because "the UI disables the button" is not a constraint.
CREATE UNIQUE INDEX idx_optimization_jobs_live
  ON optimization_jobs(root, relpath, kind, preset)
  WHERE state IN ('queued', 'waiting', 'running', 'verifying', 'ready');
"""


MIGRATION_022 = """
-- How a vision answer was obtained, so a video's answer and a photo's answer
-- can live in one table without pretending to be the same kind of thing.
--
-- `image` is a picture the model was shown directly. A video is never shown to
-- a model at all: `thumbnail` means one already-rendered frame, and
-- `contact-sheet` means three frames stacked into a single JPEG. The version
-- suffix is part of the value — `contact-sheet-v1` — because changing which
-- frames are sent can change the answer, and a cached answer from the old
-- strategy must not be reused under the new one.
ALTER TABLE vision_results ADD COLUMN strategy TEXT NOT NULL DEFAULT 'image';
"""

MIGRATION_023 = """
-- A finding may have at most one ACTIVE correction plan.
--
-- LibrAIry stored the answer to "is this already approved?" twice — the
-- finding's status and the plan pointing at it — and nothing made them agree.
-- They did not: the live database held a finding at `open` while an approved,
-- unexecuted plan named it, so Review offered to approve it again. A second
-- approval would have built a second plan over the same files and orphaned the
-- first, with two hashes claiming the same moves.
--
-- Partial, so it constrains only what it means to. `draft` is not an approval,
-- and `done`/`failed` have stopped claiming anything: a finding is free to
-- carry any number of finished plans, which is what makes a corrected folder
-- correctable again later. NULL `audit_finding_id` is every ordinary inbox
-- plan, and those are not per-finding at all.
--
-- SQLite checks this on UPDATE as well as INSERT, so it also stops a finished
-- plan being promoted back to `approved` behind a finding that has since
-- acquired a new one. The service layer refuses first, with a sentence a
-- person can read; this is what remains true when the request does not come
-- from the UI at all.
CREATE UNIQUE INDEX idx_plans_one_active_per_finding
  ON plans(audit_finding_id)
  WHERE audit_finding_id IS NOT NULL AND status IN ('approved', 'executing');

-- Approved, then taken back before anything ran.
--
-- Withdrawal removes the plan rather than mutating it, because an approved
-- plan is immutable and a half-edited one would be neither the thing that was
-- approved nor a thing anybody approved. That is safe precisely because
-- nothing executed: no journal entry, no moved file, no partial state. But it
-- did happen, and a system that cannot say "you approved this on Tuesday and
-- changed your mind on Wednesday" cannot explain itself later.
--
-- Deliberately not the History table. `history` records operations that
-- touched the filesystem, and a withdrawal touched nothing; putting it there
-- would mean Undo had something to reverse. This is provenance, one row per
-- event, and it is never a second reversal path.
CREATE TABLE plan_withdrawals (
  id               INTEGER PRIMARY KEY,
  plan_id          TEXT NOT NULL,
  plan_hash        TEXT,
  audit_finding_id INTEGER,
  relpath          TEXT NOT NULL,
  dest_relpath     TEXT,
  op_count         INTEGER NOT NULL DEFAULT 0,
  approved_at      TEXT,
  withdrawn_at     TEXT NOT NULL
);
CREATE INDEX idx_plan_withdrawals_finding ON plan_withdrawals(audit_finding_id);
"""

MIGRATION_024 = """
-- A quarantine decision is a plan, like everything else that moves a file.
--
-- It was not. "Mark for deletion" on a held file called the executor's move
-- helper directly, inside the request: the file moved to `_to-delete` before
-- the response was written, with no plan, nothing in Commit, and a one-line
-- confirmation appended to the bottom of a long page. Every other way of
-- moving a file in LibrAIry goes approve → Commit → journal → Undo, and this
-- one went straight to the disk.
--
-- Reusing `plans` rather than inventing a pending-quarantine table is the
-- point: the hash check before execution, the all-or-nothing group, the
-- journal entry and the existing Undo all come with it, and Commit can show a
-- quarantine move beside a correction because they are the same kind of thing.
ALTER TABLE plans ADD COLUMN quarantine_entry_id INTEGER REFERENCES quarantine_entries(id);
CREATE INDEX idx_plans_quarantine_entry ON plans(quarantine_entry_id);

-- One pending decision per held file, for the same reason a finding may have
-- only one active correction: two approved plans over one file are two answers
-- to a question that has one.
CREATE UNIQUE INDEX idx_plans_one_active_per_quarantine
  ON plans(quarantine_entry_id)
  WHERE quarantine_entry_id IS NOT NULL AND status IN ('approved', 'executing');
"""

MIGRATION_025 = """
-- What is needed to own a child process across worker restarts, and to tell
-- the truth about what came out of it.
--
-- `owner_token` is the identity of the worker *run* that launched the job, not
-- a PID. PIDs are reused, and a stored PID that now belongs to somebody else's
-- ffmpeg is exactly how a media server gets killed by a tidy-up routine. A
-- token minted once per worker process settles "is this still mine" without
-- consulting the process table at all; the PID and its kernel start time are
-- recorded as well, and the two together identify the process precisely enough
-- to terminate an orphan of our own without ever matching a stranger's.
ALTER TABLE optimization_jobs ADD COLUMN owner_token TEXT NOT NULL DEFAULT '';
ALTER TABLE optimization_jobs ADD COLUMN pid INTEGER;
ALTER TABLE optimization_jobs ADD COLUMN pid_started INTEGER;

-- Progress as FFmpeg reports it, not as a percentage guessed from file size.
ALTER TABLE optimization_jobs ADD COLUMN progress REAL NOT NULL DEFAULT 0;
ALTER TABLE optimization_jobs ADD COLUMN out_time_seconds REAL NOT NULL DEFAULT 0;
ALTER TABLE optimization_jobs ADD COLUMN duration_seconds REAL NOT NULL DEFAULT 0;
ALTER TABLE optimization_jobs ADD COLUMN progress_at TEXT;

-- The verification verdict, separate from the exit code. An encoder that
-- returns 0 has proved that it did not crash, which is not the same as having
-- produced the file that was asked for.
ALTER TABLE optimization_jobs ADD COLUMN verified TEXT NOT NULL DEFAULT '';
ALTER TABLE optimization_jobs ADD COLUMN output_relpath TEXT NOT NULL DEFAULT '';

-- A bounded tail, never the whole stderr. The full log lives in the job's
-- staging directory and is removed with it.
ALTER TABLE optimization_jobs ADD COLUMN log_tail TEXT NOT NULL DEFAULT '';
"""

MIGRATION_026 = """
-- Adoption provenance: which job produced a library file, and which plan
-- adopted it.
--
-- Deliberately not overloaded onto `audit_finding_id`. A correction and an
-- optimization are different kinds of claim about a file, and one column
-- holding either would make every query about provenance ask "which sort of
-- id is this" before it could answer anything.
--
-- The hash on the operation is an *integrity* check: it proves the bytes are
-- the ones the plan expected. It does not prove they are the verified output
-- of this job -- a different file with identical bytes satisfies it, and so
-- does a stale output left in a job directory by an interrupted run. That is
-- what this column is for: the executor resolves the generated source through
-- the job, so the chain plan -> job -> recorded output path -> recorded output
-- fingerprint -> op fingerprint -> bytes on disk must agree at every link.
ALTER TABLE plans ADD COLUMN optimization_job_id INTEGER
  REFERENCES optimization_jobs(id);
CREATE INDEX idx_plans_optimization_job ON plans(optimization_job_id);

-- One active adoption per job, enforced here rather than by drawing or not
-- drawing a button -- the same rule, and the same reason, as one active
-- correction per finding.
CREATE UNIQUE INDEX idx_plans_one_active_per_optimization
  ON plans(optimization_job_id)
  WHERE optimization_job_id IS NOT NULL AND status IN ('approved', 'executing');

-- The library item the job's output became, once adopted. Nullable because it
-- does not exist until then, and it survives an Undo: the row is marked
-- missing rather than deleted, so re-adoption reuses it and the link holds.
ALTER TABLE optimization_jobs ADD COLUMN result_item_id INTEGER
  REFERENCES items(id);

-- Why a preserved original is in quarantine. `quarantine_entries.reason` is
-- CHECK-constrained to ('exact_duplicate','similar_media','user'), and SQLite
-- cannot widen a CHECK -- so a preserved original would otherwise have to read
-- "you said you did not want it", which is the opposite of what happened.
-- Provenance rather than a fourth reason string.
ALTER TABLE quarantine_entries ADD COLUMN optimization_job_id INTEGER
  REFERENCES optimization_jobs(id);
CREATE INDEX idx_quarantine_optimization_job
  ON quarantine_entries(optimization_job_id);
"""

MIGRATION_027 = """
-- The hash of the verified output, recorded at the moment it was verified.
--
-- Migration 026 says the resolver must bind
--
--     plan -> job -> recorded output path -> recorded output fingerprint
--          -> op fingerprint -> bytes on disk
--
-- and the fourth link did not exist: verification recorded `verified='passed'`
-- and a byte count, and nothing that identifies *which bytes* passed. Without
-- it the strongest available check is "the op's hash matches the file in the
-- job directory", which a stale output from an interrupted run satisfies
-- trivially -- it is in the right directory under the right name.
--
-- Empty means "no verified output", which is what every pre-existing row
-- honestly is: those jobs were verified before this column existed, so there
-- is no recorded hash for them and the resolver refuses them rather than
-- inventing one by hashing whatever is there now. Re-running the job records
-- one.
ALTER TABLE optimization_jobs ADD COLUMN output_fingerprint TEXT NOT NULL
  DEFAULT '';
"""

MIGRATION_028 = """
-- How the remote copy was actually compared, recorded when it was compared.
--
-- A `done` row asserts that the bytes named by its own `fingerprint` are on
-- the remote. That assertion is now established locally -- the source is
-- hashed against the request before the copy and again after it -- but the
-- remaining link, remote-vs-local, belongs to `rclone check`, and how strong
-- that link is depends entirely on the backend: a common hash where both sides
-- can produce one, size alone where they cannot.
--
-- LibrAIry's fingerprint is blake2b and no rclone backend offers blake2b, so
-- the recorded fingerprint can never be compared against a remote hash
-- directly. Pretending otherwise would be the same category of untruth this
-- migration exists to prevent, so the strength of each row's verification is
-- read from rclone's own report and written down.
--
-- Empty is what every pre-existing row honestly is: copied before anything
-- recorded this, so unknown rather than assumed good.
ALTER TABLE backup_queue ADD COLUMN verified TEXT NOT NULL DEFAULT '';
"""

#  `music_videos` has been a category since phase 2. It is in `models.Category`,
#  it has its own destination templates, `test_music_video_paths.py` asserts the
#  shape of them — and this CHECK constraint never heard about it, so the one
#  thing that would have made all of that reachable was an INSERT that could not
#  succeed. Nobody found out, because until now nothing produced the category.
#
#  A table rebuild rather than a wider constraint copied from somewhere: the
#  list is the schema's own statement of what a category is, and the only way to
#  change it is to say so.
MIGRATION_029 = """
-- Every column the table has at version 28, including the two migration 007
-- added. A rebuild that copies the original CREATE and forgets what came after
-- it does not fail loudly; it silently drops columns, and the first sign is a
-- quarantine proposal that has lost the fact that it was a quarantine.
CREATE TABLE proposals_new (
  id            INTEGER PRIMARY KEY,
  item_id       INTEGER NOT NULL REFERENCES items(id),
  category      TEXT NOT NULL CHECK (category IN
                  ('music','music_videos','movies','shows','photos','documents',
                   'books','projects','misc')),
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
  action        TEXT NOT NULL DEFAULT 'move',
  dest_root     TEXT NOT NULL DEFAULT 'library',
  UNIQUE (item_id)
);
INSERT INTO proposals_new(id, item_id, category, clean_name, dest_relpath, confidence,
                          group_id, status, evidence, created_at, updated_at,
                          action, dest_root)
  SELECT id, item_id, category, clean_name, dest_relpath, confidence,
         group_id, status, evidence, created_at, updated_at,
         action, dest_root FROM proposals;
DROP TABLE proposals;
ALTER TABLE proposals_new RENAME TO proposals;
-- Dropping the table took all three of its indexes with it.
CREATE INDEX idx_proposals_status ON proposals(status);
CREATE INDEX idx_proposals_category ON proposals(category);
CREATE INDEX idx_proposals_group_id ON proposals(group_id);
"""

#  One answer to one collision in one merge. Two folders each holding a
#  `cover.jpg` is a question no rule answers, so the person answers it — and a
#  merge with six conflicts is six separate decisions made while reading six
#  pairs of files. Losing them on a refresh would make the page an exam.
#
#  Deliberately not a general "choices" table. This is the smallest durable
#  representation of the one thing that has to survive between reading a
#  conflict and approving the merge; a framework for choices in general would
#  be a schema for a problem nobody has yet.
MIGRATION_030 = """
CREATE TABLE merge_choices (
  id               INTEGER PRIMARY KEY,
  audit_finding_id INTEGER NOT NULL REFERENCES audit_findings(id),
  -- The *incoming* file, which is the one the decision is about. The file it
  -- collides with is wherever the merge would have put this one, so naming it
  -- again would be a second copy of a fact the plan already derives.
  relpath          TEXT NOT NULL,
  choice           TEXT NOT NULL CHECK (choice IN
                     ('keep-existing','use-incoming','keep-both')),
  decided_at       TEXT NOT NULL,
  UNIQUE (audit_finding_id, relpath)
);
CREATE INDEX idx_merge_choices_finding ON merge_choices(audit_finding_id);
"""

#  The other shape of choice, and it needed its own two columns rather than a
#  row in `merge_choices`. That table answers "which of these two files wins",
#  keyed by the incoming file; this one answers "which of these folders is the
#  one this artist lives in", and there is exactly one answer per finding. A
#  single table spanning both would have to leave half its columns empty for
#  whichever kind of question it was holding.
MIGRATION_031 = """
CREATE TABLE destination_choices (
  audit_finding_id INTEGER PRIMARY KEY REFERENCES audit_findings(id),
  -- One of the candidate folders the finding itself named. Validated against
  -- them again when it is read, because a folder can be renamed or emptied
  -- between choosing and approving.
  dest_relpath     TEXT NOT NULL,
  decided_at       TEXT NOT NULL
);
"""

#  One answer per *thing being placed*, rather than one per finding.
#
#  Migration 031 gave `destination_choices` a single row per finding, because
#  the only question it answered was "which of these two folders does this
#  artist use". `loose-tracks` asks the same question — where does this belong
#  — once per track, and one answer for the group is the wrong answer: five
#  loose tracks are commonly five different albums.
#
#  So the table grows a subject instead of gaining a sibling. An `artist-split`
#  row names the finding's own path and a `loose-tracks` row names one track,
#  and neither needs a magic value to say which it is. A NULL destination is
#  the one thing per-item choice adds: "leave this one where it is" is a real
#  answer, and it is not a move.
MIGRATION_032 = """
CREATE TABLE destination_choices_new (
  id               INTEGER PRIMARY KEY,
  audit_finding_id INTEGER NOT NULL REFERENCES audit_findings(id),
  -- What is being placed: the folder for a whole-finding choice, one file for
  -- a per-item one.
  relpath          TEXT NOT NULL,
  -- NULL means "leave it where it is", which is an answer and not an absence.
  dest_relpath     TEXT,
  decided_at       TEXT NOT NULL,
  UNIQUE (audit_finding_id, relpath)
);
INSERT INTO destination_choices_new(audit_finding_id, relpath, dest_relpath, decided_at)
  SELECT c.audit_finding_id, f.relpath, c.dest_relpath, c.decided_at
  FROM destination_choices c JOIN audit_findings f ON f.id = c.audit_finding_id;
DROP TABLE destination_choices;
ALTER TABLE destination_choices_new RENAME TO destination_choices;
CREATE INDEX idx_destination_choices_finding ON destination_choices(audit_finding_id);
"""

#  "All of this runs, or none of it does", said as a property of the plan.
#
#  The executor has always had this rule and has always inferred it from
#  `audit_finding_id IS NOT NULL` — a proxy that was true for every coherent
#  plan that existed at the time. A comparison between an arriving file and the
#  library copy it resembles is the first one that is not an audit finding and
#  still must not half-apply: quarantining the filed copy without landing the
#  arrival would leave somebody with the recording in Quarantine and nothing in
#  the library.
#
#  So the property gets a column instead of a third proxy. The existing rule is
#  left exactly as it was; this widens it rather than replacing it.
MIGRATION_033 = """
ALTER TABLE plans ADD COLUMN coherent INTEGER NOT NULL DEFAULT 0;
"""

#  What a dismissal was actually about.
#
#  "Keep both" and an explicit Restore both mean the same thing — the person
#  wants these two representations — and both have to stop the next audit
#  asking again. Marking the czkawka pair `dismissed` does that, and on its own
#  it does it forever: re-encode one of the two files and the answer given about
#  the old pair silently covers a pair nobody has ever seen.
#
#  So the dismissal records *which two files* it was about. The pair is
#  suppressed while both are still those bytes and becomes a live comparison
#  again the moment either of them materially changes. Old dismissals, which
#  recorded nothing, stay dismissed: a NULL here means "dismissed the old way",
#  not "dismissed about nothing".
MIGRATION_034 = """
ALTER TABLE similar_media_flags ADD COLUMN dismissed_fingerprints TEXT;
"""

MIGRATION_035 = """
-- What one *file* was identified as, which is not what `catalog_identity`
-- holds. That table answers "what release is this folder?", keyed by folder,
-- one row per provider — right for an album, and unable to say anything about
-- a track lying loose in an artist folder alongside eleven others that belong
-- to different releases.
--
-- Two things a track needs that a folder does not:
--
--   * **item scope.** The identity belongs to the file. Its folder is shared
--     with every other loose track under the same artist.
--   * **several releases.** A recording appears on the original album, on a
--     greatest-hits, and on a remaster, and picking the first one the API
--     returned is exactly the invention this whole feature refuses to make.
--     They are candidates, and choosing between them is the person's job.
--
-- `fingerprint` is the file's content hash at the moment it was identified.
-- A re-encode is a different file, and an identity recorded against bytes
-- that no longer exist is not evidence about the bytes that do.
CREATE TABLE track_identity (
  item_id      INTEGER PRIMARY KEY REFERENCES items(id),
  fingerprint  TEXT NOT NULL DEFAULT '',
  provider     TEXT NOT NULL,
  -- Empty means "asked, and nothing came back". Kept, so the same fruitless
  -- lookup is not run again on the next click; `looked_up_at` expires it.
  recording_id TEXT NOT NULL DEFAULT '',
  artist       TEXT NOT NULL DEFAULT '',
  artist_id    TEXT NOT NULL DEFAULT '',
  title        TEXT NOT NULL DEFAULT '',
  score        REAL,
  -- JSON array of {id, title, group_id, year, kind}. A list because a
  -- recording has one identity and many places it was released.
  releases     TEXT NOT NULL DEFAULT '[]',
  looked_up_at TEXT NOT NULL
);
"""

MIGRATION_036 = """
-- Which members of one visual group the person wants to keep.
--
-- A comparison of two files needs no storage: the answer is one press and the
-- plan is made on the spot. A group of thirty-seven photographs is different
-- in kind — the answer is a *set*, built up a page at a time, and it has to
-- survive scrolling, sorting and coming back after lunch. Holding it in hidden
-- form fields would mean one mis-click discards a decision somebody spent ten
-- minutes making.
--
-- Absence means keep. That is the conservative direction on purpose: a group
-- opens with everything kept and the person unticks what they want set aside,
-- so a half-finished session that is approved by accident sets aside only what
-- was explicitly chosen. `keep` is stored too, rather than deleting the row,
-- so "I looked at this and decided to keep it" is distinguishable from "I
-- never got to it" for anything that later wants to know.
CREATE TABLE similar_media_choices (
  audit_finding_id INTEGER NOT NULL REFERENCES audit_findings(id),
  item_id          INTEGER NOT NULL REFERENCES items(id),
  decision         TEXT NOT NULL CHECK (decision IN ('keep','set-aside')),
  created_at       TEXT NOT NULL,
  PRIMARY KEY (audit_finding_id, item_id)
);
CREATE INDEX idx_similar_media_choices_finding
  ON similar_media_choices(audit_finding_id);
"""

MIGRATION_037 = """
-- One metadata cache row per item *per tool*.
--
-- `item_metadata` was keyed by `item_id` alone, because for a long time one
-- reader wrote it: the ffprobe cache of codec, bitrate and duration. A test
-- deliberately failed if a second writer appeared, which was the right guard
-- at the time — a second tool would have silently overwritten the first, and
-- a row saying `tool='exiftool-image'` where a caller expected ffprobe is
-- worse than no cache at all.
--
-- There are now three genuine readers: media technicals, document identity
-- (pdfinfo/pdftotext/EPUB) and image metadata (exiftool). They describe
-- different things about the same file and none of them is a substitute for
-- another, so each owns its own row. The tripwire is replaced by the
-- invariant it was protecting: a tool cannot clobber another tool's row.
--
-- `fingerprint` stays and stays load-bearing. A cached duration, page count or
-- capture time describes *those bytes*; after a re-encode it is a fact about a
-- file that no longer exists, so a read whose fingerprint does not match is a
-- miss rather than a stale answer.
--
-- Created here rather than left to `ensure_metadata_cache`'s CREATE TABLE IF
-- NOT EXISTS, so the shape is in the schema where it can be migrated.
CREATE TABLE IF NOT EXISTS item_metadata (
  item_id     INTEGER PRIMARY KEY REFERENCES items(id),
  fingerprint TEXT NOT NULL,
  tool        TEXT NOT NULL,
  payload     TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE TABLE item_metadata_new (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL REFERENCES items(id),
  tool        TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  payload     TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE (item_id, tool)
);
INSERT INTO item_metadata_new(item_id, tool, fingerprint, payload, updated_at)
  SELECT item_id, tool, fingerprint, payload, updated_at FROM item_metadata;
DROP TABLE item_metadata;
ALTER TABLE item_metadata_new RENAME TO item_metadata;
CREATE INDEX idx_item_metadata_item ON item_metadata(item_id);
"""

MIGRATION_038 = """
-- "I want both formats of this book", remembered so it is not asked again.
--
-- A similar-media group records that answer on the czkawka pairs behind it,
-- because that is where its membership came from. A work group has no pairs:
-- its membership is an ISBN or a DOI, so the answer is recorded against the
-- identifier and against **the exact files it was given about**.
--
-- `fingerprints` is the sorted content hashes of the members, joined. That is
-- what makes the suppression expire honestly: replace the PDF with a better
-- scan and the set no longer matches, so the question comes back — which is
-- right, because nobody has been asked about the new file. Suppressing by
-- title or by path would have survived that and quietly hidden it.
CREATE TABLE document_work_choices (
  scheme       TEXT NOT NULL,
  identifier   TEXT NOT NULL,
  fingerprints TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (scheme, identifier)
);
"""

MIGRATION_039 = """
-- What LibrAIry knows about two files belonging together.
--
-- Companion handling has existed for a year as *classification logic*: the
-- classifier worked out that `Movie.en.srt` names `Movie.mkv`, pointed the
-- subtitle at the video's destination, and then forgot. Nothing else could
-- ask. Item Detail could not say "this film has two subtitles and a poster",
-- an inbox collection could not tell a companion from an unexplained file,
-- and every future feature that needs the same answer would have had to work
-- it out again from filenames.
--
-- The pair is stored canonically — `low_item_id < high_item_id` — so one
-- relationship is one row and A-to-B cannot coexist with B-to-A. Direction,
-- where a kind has one, is stored as a *value*: `companion_item_id` names
-- which of the two is the companion. Reading the role off the column order
-- would make it an accident of insertion.
--
-- `provenance` records why LibrAIry believes it, in the words that were
-- actually true: `stem+suffix` for a subtitle that names its video,
-- `folder-artwork` for a cover that its album agreed on. Never "AI said so" —
-- nothing here is inferred by a model, and a deterministic rule that cannot
-- say which rule it was is not evidence.
CREATE TABLE item_relationships (
  id                INTEGER PRIMARY KEY,
  low_item_id       INTEGER NOT NULL REFERENCES items(id),
  high_item_id      INTEGER NOT NULL REFERENCES items(id),
  kind              TEXT NOT NULL CHECK (kind IN
                      ('subtitle','lyrics','cue','artwork')),
  companion_item_id INTEGER NOT NULL REFERENCES items(id),
  provenance        TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  CHECK (low_item_id < high_item_id),
  UNIQUE (low_item_id, high_item_id, kind)
);
CREATE INDEX idx_item_relationships_low ON item_relationships(low_item_id);
CREATE INDEX idx_item_relationships_high ON item_relationships(high_item_id);
"""

MIGRATION_040 = """
-- The committed decision a restore is putting back.
--
-- One photo comparison can send ninety files to Quarantine as a single
-- answer, and until now the only way to reverse it was ninety separate
-- restores — each its own plan, its own Commit card and its own History
-- line. The decision boundary that produced them was already recorded:
-- `quarantine_entries.plan_id` says these files moved together *because of
-- one thing*. This column is the other half of that sentence, on the plan
-- that puts them back.
--
-- It is deliberately a plan id and not a date, a folder or a reason string.
-- Those would group files that merely resemble each other; this groups files
-- that actually left together.
ALTER TABLE plans ADD COLUMN restore_of_plan_id TEXT REFERENCES plans(id);
"""

MIGRATION_041 = """
-- What the owner actually decided, and what was true about the file when they
-- decided it.
--
-- History already records what *moved*: a source, a destination, a hash and an
-- outcome. It cannot answer the question this table exists for — *why would
-- somebody choose that again* — because the cues that made the choice are not
-- in it, and re-deriving them later would re-classify the file with today's
-- rules rather than the ones that were in front of the person at the time.
--
-- So this stores the cues and the answer, and nothing else. Whether the
-- decision *completed* is not stored: it is read from the journal, by plan,
-- which is also where a later Undo shows up. One record of what happened, one
-- record of what was chosen, and no third place for them to disagree.
--
-- `features` is normalized cues — a document type, an organization, a
-- category, a set of formats. Never document text, never a filename as an
-- opaque equality, never anything from a page of a bank statement.
--
-- `outcome` is a *template* wherever the destination policy is one:
-- `Documents/Financial/{year}` rather than `Documents/Financial/2024`, so four
-- statements from 2024 do not teach LibrAIry to file a 2026 statement under
-- 2024.
CREATE TABLE decision_events (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,
  -- The canonical form of `features`, which is what a lookup matches on.
  signature    TEXT NOT NULL,
  -- How many cues had to agree. A narrower pattern beats a broader one by
  -- this number and by nothing else — there is no score here.
  specificity  INTEGER NOT NULL,
  features     TEXT NOT NULL,
  outcome      TEXT NOT NULL,
  item_id      INTEGER REFERENCES items(id),
  -- The plan that carried it out, stamped when the file actually moved. NULL
  -- means one of two things and they are told apart by `settled_at`: not yet
  -- committed, or a decision that never had a plan because the answer was to
  -- leave everything where it is.
  plan_id      TEXT REFERENCES plans(id),
  dest_relpath TEXT,
  decided_at   TEXT NOT NULL,
  settled_at   TEXT
);
CREATE INDEX idx_decision_events_signature ON decision_events(signature);
CREATE INDEX idx_decision_events_plan ON decision_events(plan_id);

-- "Do not suggest this again." Not a deletion: the decisions that formed the
-- pattern are still what happened, and still show in History. This says the
-- owner does not want to be offered the conclusion.
CREATE TABLE decision_suppressions (
  signature  TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);
"""

MIGRATION_042 = """
-- Two more relationship kinds, now that there is evidence for them.
--
-- `raw_render` and `live_photo` were refused when relationships were first
-- written down, and the refusal was right: the only thing available then was a
-- shared filename stem, and the counterexample is the *common* case — a phone
-- camera folder where `IMG_9323.jpeg` sits beside an entirely unrelated
-- `IMG_9323.MOV`. Pairing on that would have invented a fact about somebody's
-- family photographs.
--
-- What changed is the metadata cache. `exiftool-image` now holds capture time,
-- camera and Apple's content identifier against the exact bytes they were read
-- from, so the pairing can be established from what the files record rather
-- than from what they are called. A stem is still not evidence; it is only the
-- reason to look.
--
-- SQLite cannot widen a CHECK, so the table is rebuilt. Every existing
-- relationship is carried across unchanged.
CREATE TABLE item_relationships_new (
  id                INTEGER PRIMARY KEY,
  low_item_id       INTEGER NOT NULL REFERENCES items(id),
  high_item_id      INTEGER NOT NULL REFERENCES items(id),
  kind              TEXT NOT NULL CHECK (kind IN
                      ('subtitle','lyrics','cue','artwork','raw_render','live_photo')),
  companion_item_id INTEGER NOT NULL REFERENCES items(id),
  provenance        TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  CHECK (low_item_id < high_item_id),
  UNIQUE (low_item_id, high_item_id, kind)
);
INSERT INTO item_relationships_new
  (id, low_item_id, high_item_id, kind, companion_item_id, provenance, created_at)
  SELECT id, low_item_id, high_item_id, kind, companion_item_id, provenance, created_at
  FROM item_relationships;
DROP INDEX IF EXISTS idx_item_relationships_low;
DROP INDEX IF EXISTS idx_item_relationships_high;
DROP TABLE item_relationships;
ALTER TABLE item_relationships_new RENAME TO item_relationships;
CREATE INDEX idx_item_relationships_low ON item_relationships(low_item_id);
CREATE INDEX idx_item_relationships_high ON item_relationships(high_item_id);
"""

MIGRATION_043 = """
-- What a decision was told about the relationships it touches.
--
-- A plan is immutable and hash-checked, so what it will *do* cannot change
-- between approval and Commit. What it *means* can. "This will separate a Live
-- Photo — the still stays in Photos" is a statement about a file the plan does
-- not contain and therefore never checks: the half that stays behind. If that
-- half is deleted, replaced, or newly paired with something else in the
-- meantime, the sentence the person read when they approved is no longer true,
-- and Commit would run under a topology nobody was shown.
--
-- So the affected relationships are frozen here at approval. Only the ones
-- this plan touches — never a copy of the relationship table — and only the
-- fields needed to ask "does this still mean the same thing?".
CREATE TABLE plan_relationships (
  id             INTEGER PRIMARY KEY,
  plan_id        TEXT NOT NULL REFERENCES plans(id),
  kind           TEXT NOT NULL,
  low_item_id    INTEGER NOT NULL,
  high_item_id   INTEGER NOT NULL,
  -- What the person was shown: moves_together, split, both_remain or stale.
  state          TEXT NOT NULL,
  -- The member this plan does NOT operate on, when there is one. NULL when
  -- both halves are in the plan, because both are then covered by the
  -- fingerprint the executor already verifies on every operation.
  outside_item_id     INTEGER,
  -- That member's fingerprint at approval. NULL when it had none recorded.
  outside_fingerprint TEXT,
  created_at     TEXT NOT NULL,
  UNIQUE (plan_id, low_item_id, high_item_id, kind)
);
CREATE INDEX idx_plan_relationships_plan ON plan_relationships(plan_id);

-- Whether this plan was approved by a version of LibrAIry that looked at
-- relationships at all. Plans approved before this feature existed have 0 and
-- keep their old semantics exactly: they are not retroactively required to
-- carry a snapshot, and Commit does not invent a refusal for them. A plan
-- approved now has 1 even when it touches no relationship, which is what tells
-- "checked, and there were none" apart from "never checked".
ALTER TABLE plans ADD COLUMN relationships_checked INTEGER NOT NULL DEFAULT 0;
"""

MIGRATION_044 = """
-- What the owner has said they prefer, permit and protect.
--
-- One table, because the alternative had already started: `music.preferred_format`
-- lived in `settings`, `optimization.protected_roots` lived in `settings` under
-- a different name and a different meaning, and every new domain that wanted a
-- format opinion was going to invent a third. A page of independent toggles is
-- not a policy — nothing can consult it, nothing can explain it, and no two
-- subsystems can be made to agree.
--
-- Three genuinely different things live here and are deliberately not
-- collapsed into one column:
--
--   preferred_format    among representations that ALREADY EXIST, which one
--                       does the owner want. It creates nothing.
--   allow_*_transform   may LibrAIry ever *propose* making one. NULL means
--                       the owner has not said, which is not the same as no.
--   preserve_originals  this scope's originals are not to be traded away by
--                       any representation preference or optimization.
--
-- Scope precedence is most-specific-wins, per field, among the scopes that
-- actually state that field: folder, then category, then global. A scope that
-- says nothing about a field does not overrule a broader one that does.
CREATE TABLE format_policy_scopes (
  id                 INTEGER PRIMARY KEY,
  scope_kind         TEXT NOT NULL CHECK (scope_kind IN ('global','category','folder')),
  -- '' for global, a taxonomy slug for category, a library-relative path for
  -- folder. Never an absolute path: this database is restored onto other
  -- machines, and a policy that stops applying because the mount point moved
  -- is a protection somebody believes they have.
  scope_value        TEXT NOT NULL,
  preferred_format   TEXT NOT NULL DEFAULT '',
  -- Tri-state on purpose. NULL is "not stated" and is the default everywhere,
  -- so adding this table changes no existing behaviour: Storage Optimization
  -- goes on proposing exactly what it proposed before until somebody says
  -- otherwise.
  preserve_originals       INTEGER,
  allow_lossy_transform    INTEGER,
  allow_lossless_transform INTEGER,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  UNIQUE (scope_kind, scope_value)
);
CREATE INDEX idx_format_policy_kind ON format_policy_scopes(scope_kind);

-- The MP3 preference moves here rather than being copied here. Two rows that
-- both claim to be the preferred music format can disagree, and the one being
-- read would then depend on which module asked.
INSERT INTO format_policy_scopes
  (scope_kind, scope_value, preferred_format, created_at, updated_at)
SELECT 'category', 'music',
       lower(trim(COALESCE((SELECT value FROM settings WHERE key='music.preferred_format'),
                           'mp3'))),
       datetime('now'), datetime('now');
DELETE FROM settings WHERE key='music.preferred_format';
"""

MIGRATION_045 = """
-- Undo asks which later decisions consumed the state an earlier one created,
-- and the answer comes from `plan_ops.item_id` — the identity a file keeps
-- across every move. Without this index that question is a full scan of every
-- operation ever executed, once per plan on the History page.
--
-- No new table. What a plan did is already recorded, immutably, in `plan_ops`
-- and `history`; a dependency graph stored alongside them would be a second
-- account of the same events, free to disagree with the first.
CREATE INDEX IF NOT EXISTS idx_plan_ops_item ON plan_ops(item_id);
CREATE INDEX IF NOT EXISTS idx_history_op ON history(op_id);
"""

MIGRATION_046 = """
-- What LibrAIry knows a file *is*, made searchable.
--
-- A film identified against TMDB, a recording against MusicBrainz, a paper by
-- its DOI — none of it reached Search, which was built from the filename, the
-- proposal and the tags. A library that knew perfectly well it held *Arrival*
-- could not find it under that name if the file was called
-- `arrvl.2016.PROPER.1080p.x264-GRP.mkv`, which is the exact case
-- identification exists for.
--
-- A new column rather than more text in `title`: the physical name, the
-- embedded tags and the catalog identity are three facts about one file, and
-- collapsing them would quietly rewrite what a file says about itself.
--
-- FTS5 cannot add a column, so the table is recreated. Everything in it is
-- derived from `items`, `proposals` and the identity tables, which is what
-- makes it rebuildable — `migrate` repopulates it immediately afterwards.
DROP TABLE IF EXISTS search_fts;
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
  identity,
  category UNINDEXED,
  root UNINDEXED,
  item_id UNINDEXED,
  tokenize='unicode61 remove_diacritics 2'
);
"""

MIGRATION_047 = """
-- Two records of decisions that move no bytes.
--
-- `reconciliations` is what LibrAIry recognised rather than what it did. When
-- somebody moves an album with Finder, the file is fine and the *index* is
-- wrong; agreeing to recognise it at its new path changes an understanding and
-- touches nothing on disk. That is not History — History is the journal of
-- operations that moved files, and putting a recognition in it would claim a
-- move that never happened. It is not derivable either: the candidate is
-- derived from fingerprints, but the fact that a person accepted one is a
-- decision, and decisions are recorded.
CREATE TABLE reconciliations (
  id            INTEGER PRIMARY KEY,
  item_id       INTEGER NOT NULL REFERENCES items(id),
  -- 'moved' today. Named so a later kind does not have to pretend to be this
  -- one.
  kind          TEXT NOT NULL DEFAULT 'moved',
  from_root     TEXT NOT NULL,
  from_relpath  TEXT NOT NULL,
  to_root       TEXT NOT NULL,
  to_relpath    TEXT NOT NULL,
  -- The bytes that proved the two paths were the same file. Recorded so the
  -- record can be checked later rather than believed.
  fingerprint   TEXT NOT NULL DEFAULT '',
  -- Set when this was one member of a folder that moved as a unit, so a
  -- subtree reads back as one decision rather than thirty.
  batch         TEXT NOT NULL DEFAULT '',
  decided_at    TEXT NOT NULL
);
CREATE INDEX idx_reconciliations_item ON reconciliations(item_id);
CREATE INDEX idx_reconciliations_batch ON reconciliations(batch);

-- Why an approval was taken back, where the code that took it back knows.
--
-- `plan_withdrawals` recorded that a decision was withdrawn and nothing about
-- who withdrew it or why. That was tolerable while withdrawals were rare; now
-- that two waiting decisions can be found to contradict each other, sending
-- one back is a thing the program actively asks people to do, and "you
-- withdrew this" without saying which conflict it resolved is a record that
-- explains nothing.
--
-- Empty means the withdrawal predates these columns or the caller genuinely
-- did not know. It is never filled in by guessing afterwards.
ALTER TABLE plan_withdrawals ADD COLUMN source TEXT NOT NULL DEFAULT '';
ALTER TABLE plan_withdrawals ADD COLUMN reason TEXT NOT NULL DEFAULT '';
ALTER TABLE plan_withdrawals ADD COLUMN conflicted_with TEXT;
CREATE INDEX idx_plan_withdrawals_at ON plan_withdrawals(withdrawn_at);
"""

MIGRATION_048 = """
-- A pair can be found from either end, and only one end was indexed.
--
-- `similar_media_flags` records one row per pair, and which of the two files
-- is `item_id` is an accident of the order czkawka reported them. Asking "what
-- is this file paired with" therefore has to look in both columns, and the
-- half that looked in `similar_item_id` scanned the table — once per row of
-- Review, which is not visible in a page of fifty and is the whole page at a
-- million.
--
-- The UNIQUE constraint already indexes `item_id` as its leading column. This
-- is the other end of the same question.
CREATE INDEX IF NOT EXISTS idx_similar_media_flags_similar_item_id
  ON similar_media_flags(similar_item_id);
"""


MIGRATION_049 = """
-- How much attention a proposal is worth, decided from its own evidence.
--
-- Stored rather than derived, for the same reason `confidence` is: Review has
-- to *count* these — "24 settled by identity" is a number above a list, not a
-- number found by reading twenty-four rows — and the evidence it is derived
-- from is a JSON blob no index can answer a question about. Testing it in SQL
-- would mean scanning every pending proposal on every page render, which is
-- the shape M1-01 spent a day removing from this page.
--
-- Written by `upsert_proposal` from the evidence it already validates, so it
-- is recomputed exactly when the evidence changes and can never describe an
-- older analysis. The rule lives in `librairy/confidence_tiers.py`.
--
-- NULL means "written before this column existed". Backfilled below for the
-- two tiers that can be decided in SQL; the settled tier cannot, because it is
-- a question about evidence, so those rows re-tier on their next analysis and
-- read as `suggested` until then. Erring downward on an upgrade is the only
-- safe direction: a row that should be settled and reads as suggested costs
-- one look, and the reverse would put a file in front of Commit on the
-- strength of a migration.
ALTER TABLE proposals ADD COLUMN tier TEXT;

UPDATE proposals SET tier = CASE
  WHEN dest_relpath IS NULL OR dest_relpath = '' THEN 'uncertain'
  WHEN confidence >= 0.85 THEN 'suggested'
  ELSE 'uncertain' END
WHERE tier IS NULL;

CREATE INDEX IF NOT EXISTS idx_proposals_tier ON proposals(tier, status);
"""


MIGRATION_050 = """
-- Where a file was filed to, asked by destination.
--
-- Search prints "filed here N times before" against every result, and History
-- answers the same question about a path. Both looked it up by
-- (dest_root, dest_relpath) and neither column was indexed, so each question
-- was a scan of the whole journal — fifty of them per page of results.
CREATE INDEX IF NOT EXISTS idx_history_destination
  ON history(dest_relpath, dest_root);
"""


MIGRATION_051 = """
-- Files held because there was nothing left worth asking.
--
-- Analysis used to fall back to whatever the filename suggested when the
-- configured AI provider was off or unreachable: `ai/orchestrator.py` logged
-- "providers unavailable ... continuing with deterministic results" and a
-- guess became a proposal that looked exactly like a considered answer.
--
-- One row per held file, keyed by the item so that recording a hold twice is
-- one row and not two -- an outage that lasts eleven worker cycles must not
-- leave eleven records of the same file. `since` survives every re-hold; only
-- `attempts` and `updated_at` move.
--
-- Not a job queue. There is no payload, no ordering, no lease and no worker
-- id: the work to be done is already described by the item, and the state
-- machine that owns it is `items.state`. This table only says *why* a file is
-- in the state it is in, and what the owner has said about it.
CREATE TABLE processing_waits (
  item_id     INTEGER PRIMARY KEY REFERENCES items(id),
  reason      TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '',
  attempts    INTEGER NOT NULL DEFAULT 1,
  paused      INTEGER NOT NULL DEFAULT 0,
  released_at TEXT,
  since       TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX idx_processing_waits_reason ON processing_waits(reason);
CREATE INDEX idx_processing_waits_resume ON processing_waits(paused, updated_at);
"""


MIGRATION_052 = """
-- A habit the owner promoted into a policy.
--
-- Decision Memory notices that the same choice keeps being made and offers the
-- answer; that is a *suggestion*, and it goes quiet on its own the moment the
-- history stops agreeing with itself. A rule is what the owner says when they
-- have seen the pattern and want it kept: "yes, that is my filing policy".
--
-- The two are the same machinery and deliberately not the same authority. A
-- rule is still level four -- it may fill an answer in and it may never
-- approve, commit or move anything -- and the only thing that creates one is a
-- person pressing a button. Repetition earns the *offer*; it never earns the
-- rule.
--
-- `signature` is the promoted pattern, verbatim from `decision_events`, so a
-- rule and the decisions behind it can never describe different things.
-- `scope` is 'category' for the pattern as learned and 'global' only where
-- somebody deliberately widened it: an automatic generalization from one
-- domain to another is the thing this whole feature exists not to do.
--
-- `overrides` counts the times somebody filed a matching file somewhere else
-- since. It is shown and it is never acted on: disabling a policy the owner
-- wrote down is the owner's decision, not a threshold's.
CREATE TABLE decision_rules (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,
  signature  TEXT NOT NULL UNIQUE,
  scope      TEXT NOT NULL DEFAULT 'category',
  features   TEXT NOT NULL,
  outcome    TEXT NOT NULL,
  name       TEXT NOT NULL,
  enabled    INTEGER NOT NULL DEFAULT 1,
  support    INTEGER NOT NULL DEFAULT 0,
  overrides  INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_decision_rules_enabled ON decision_rules(enabled, kind);
"""


MIGRATION_053 = """
-- Tags that outlive the path they were written on.
--
-- A hashtag is the one thing in a filename somebody typed on purpose, and it
-- used to survive exactly as long as the proposal that read it: the tag was
-- captured into the proposal's frozen evidence, stripped out of the clean name
-- on the way to the library, and then gone. Re-analysing a filed file read the
-- library path, where the tag no longer is, and learned nothing.
--
-- Keyed to the **item** rather than to a path, because the item is what
-- survives filing. `items.relpath` changes when a file moves; `items.id` does
-- not, which is what makes `#ProjectHouse` still true a year later.
--
-- `source` is provenance, and it is a real distinction: "you tagged this file"
-- and "it is in a tagged folder" are different claims, and the second is
-- inherited by everything beneath the folder.
CREATE TABLE item_tags (
  item_id  INTEGER NOT NULL REFERENCES items(id),
  tag      TEXT NOT NULL,
  label    TEXT NOT NULL,
  source   TEXT NOT NULL,
  detail   TEXT NOT NULL DEFAULT '',
  added_at TEXT NOT NULL,
  PRIMARY KEY (item_id, tag)
);
CREATE INDEX idx_item_tags_tag ON item_tags(tag);

-- A tag somebody promoted into a Project.
--
-- Deliberately *only* a tag and a name. A Project's members are the items
-- carrying its tag, read from `item_tags` -- a membership table here would be
-- a second copy of that, free to disagree with it, and needing to be written
-- by everything that ever tags anything.
--
-- Not the same thing as the `Projects/` folder in the library taxonomy, and
-- never to be conflated with it: that is a place on disk that files are moved
-- into, and this is a view across files wherever they already live. See
-- `docs/ui-vocabulary.md`.
CREATE TABLE projects (
  id         INTEGER PRIMARY KEY,
  tag        TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Every tag that is already known, moved to where it now lives.
--
-- Without this, upgrading loses them: a tag used to be findable because the
-- search index read it out of the live proposal's evidence, and the index now
-- reads `item_tags`. Somebody who tagged four hundred photographs last year
-- would have searched for `#Vacation2026` after the upgrade and found nothing.
--
-- The old evidence stored the *normalised* tag in `detail` -- `_sanitize_tag`
-- ran before the entry was written -- so it is both the tag and the best label
-- available, and no re-normalisation is needed or possible here.
INSERT OR IGNORE INTO item_tags(item_id, tag, label, source, detail, added_at)
SELECT p.item_id,
       json_extract(e.value, '$.detail'),
       json_extract(e.value, '$.detail'),
       'folder',
       '',
       COALESCE(p.created_at, '')
FROM proposals p, json_each(p.evidence) e
WHERE p.status != 'superseded'
  AND json_valid(p.evidence)
  AND json_extract(e.value, '$.source') = 'hashtag'
  AND COALESCE(json_extract(e.value, '$.detail'), '') <> '';
"""


#  Documents that are one decision, and the reason they are.
#
#  Three columns and one rebuild. `group_key` is what two documents have to
#  share for a group to exist, `group_hint` carries the words that key was
#  built from — the heading and the sentence under it — and `groups.reason` is
#  where that sentence lands once the group is real. The hint is beside the key
#  rather than derived from it because a key is normalised and a heading is
#  not: `book_series|books|programming rust` is what matches, and
#  `Programming Rust` is what a person reads.
#
#  The `kind` CHECK is rebuilt for the same reason migration 014 rebuilt it for
#  a ripped disc: calling a book series an "archive" to avoid a table rebuild
#  would put a wrong word in the data for ever to save one statement.
MIGRATION_054 = """
ALTER TABLE proposals ADD COLUMN group_key TEXT;
ALTER TABLE proposals ADD COLUMN group_hint TEXT;
CREATE INDEX idx_proposals_group_key ON proposals(group_key);

CREATE TABLE groups_new (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN
               ('album','season','photo_event','project','archive','disc',
                'book_series','document_set','tagged_set')),
  label      TEXT NOT NULL,
  dest_base  TEXT,
  -- What makes these files one decision, in a sentence, written when the
  -- reason was actually known. Empty for the kinds that predate it: an album
  -- is an album, and the heading has never needed a line under it.
  reason     TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
INSERT INTO groups_new(id, kind, label, dest_base, reason, created_at)
  SELECT id, kind, label, dest_base, '', created_at FROM groups;
DROP TABLE groups;
ALTER TABLE groups_new RENAME TO groups;
-- Dropping the table took its index with it.
CREATE INDEX idx_groups_kind ON groups(kind);
"""


#  The first table in this program with a memory of size.
#
#  One row per metric per day, and the primary key is the whole idempotence
#  story: a rollup run twice for one day replaces its own answer rather than
#  appending a second "daily" row. `kind` is stored rather than only declared
#  in code so that somebody reading the raw table can tell a snapshot from a
#  count — averaging two gauges is meaningful and adding them is not, and the
#  other way round for counts.
#
#  Nothing operational reads it. Losing it degrades trends and breaks nothing.
MIGRATION_055 = """
CREATE TABLE metrics_daily (
  day      TEXT NOT NULL,          -- a UTC day, YYYY-MM-DD
  metric   TEXT NOT NULL,
  kind     TEXT NOT NULL CHECK (kind IN ('gauge', 'count')),
  value    INTEGER NOT NULL,
  -- When the number was taken. A gauge for today is a snapshot at a moment,
  -- and a chart that says "as of 14:00" is telling the truth where one
  -- labelled with the date alone is rounding it.
  taken_at TEXT NOT NULL,
  PRIMARY KEY (day, metric)
);
"""


#  Where Library content is copied to, and what each place is for.
#
#  `modes` is a capability list rather than one value: a NAS can be a Backup or
#  a Mirror and a drive in a drawer can only be an Offline Backup, and a policy
#  naming a mode its destination cannot satisfy would be permanently failing.
#
#  The unique key on (category, destination) is the whole of "overlap is
#  deterministic": a category may go to several destinations, and a category
#  and a destination have exactly one mode between them. Two rows disagreeing
#  about what a destination is *for* is the one ambiguity nobody could resolve
#  by reading the screen.
#
#  Nothing here can express deletion. See `librairy/destinations.py`.
MIGRATION_056 = """
CREATE TABLE backup_destinations (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  kind       TEXT NOT NULL CHECK (kind IN ('local', 'remote')),
  -- An rclone remote (`nas:library`) or an absolute local path. Never used as
  -- transfer authority on its own -- see `librairy/transfer_paths.py`.
  target     TEXT NOT NULL,
  modes      TEXT NOT NULL,
  -- For an offline drive: what identifies the volume rather than wherever the
  -- operating system mounted it this morning.
  identity   TEXT NOT NULL DEFAULT '',
  enabled    INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE backup_policies (
  id             INTEGER PRIMARY KEY,
  category       TEXT NOT NULL,
  destination_id INTEGER NOT NULL REFERENCES backup_destinations(id),
  mode           TEXT NOT NULL CHECK (mode IN ('backup', 'mirror', 'offline')),
  enabled        INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL,
  UNIQUE (category, destination_id)
);
CREATE INDEX idx_backup_policies_destination ON backup_policies(destination_id);
"""


#  The other half of an offline drive's identity.
#
#  The marker file answers "was this drive registered with LibrAIry" and can be
#  cloned; the volume id answers "is this the same filesystem" and cannot, but
#  is not available on every platform. Neither is sufficient and both are
#  cheap. Empty is a legitimate value — see `librairy/volumes.py`.
MIGRATION_057 = """
ALTER TABLE backup_destinations ADD COLUMN volume TEXT NOT NULL DEFAULT '';
"""


#  What each backup run did.
#
#  Note what is *not* here: no "this destination is up to date" column. That
#  absence is the design. A stored flag would have to be right about every way
#  a transfer can end — killed, disconnected, disk full, a file that vanished
#  halfway — and only has to be wrong once for a backup to sit there saying it
#  is fine. Whether a destination is current is answered by comparing, every
#  time it is asked. See `librairy/backup_runs.py`.
MIGRATION_058 = """
CREATE TABLE backup_runs (
  id              INTEGER PRIMARY KEY,
  destination_id  INTEGER NOT NULL REFERENCES backup_destinations(id),
  category        TEXT NOT NULL,
  mode            TEXT NOT NULL,
  state           TEXT NOT NULL CHECK (state IN
                    ('planned','running','succeeded','failed')),
  started_at      TEXT NOT NULL,
  finished_at     TEXT,
  -- What the comparison said before anything moved.
  planned_copies  INTEGER NOT NULL DEFAULT 0,
  planned_updates INTEGER NOT NULL DEFAULT 0,
  -- Files at the destination the library no longer has. Recorded because it is
  -- worth knowing and shown because it is worth seeing. Never acted on.
  destination_only INTEGER NOT NULL DEFAULT 0,
  -- What actually moved. True whether or not the run finished: 73 files
  -- reaching a destination is a fact, and it is not permission to call the
  -- destination current.
  transferred     INTEGER NOT NULL DEFAULT 0,
  bytes_sent      INTEGER NOT NULL DEFAULT 0,
  outcome         TEXT NOT NULL DEFAULT '',
  detail          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_backup_runs_destination ON backup_runs(destination_id, id DESC);
"""


#  What is only at a destination, and how much of it there is.
#
#  Two tables because they answer two questions and only one of them scales: a
#  *count* is one row per destination and category, and *which files* is a
#  bounded sample. A destination holding four hundred thousand files the
#  library no longer has is worth being told about; storing four hundred
#  thousand path strings and rewriting them hourly is not the way to tell
#  somebody.
#
#  Keyed on the file rather than on the run: a file sitting there across ten
#  Mirror runs is one fact about today, not ten findings. See
#  `librairy/divergence.py`.
MIGRATION_059 = """
CREATE TABLE backup_divergence (
  destination_id INTEGER NOT NULL REFERENCES backup_destinations(id),
  category       TEXT NOT NULL,
  relpath        TEXT NOT NULL,
  size           INTEGER NOT NULL DEFAULT 0,
  -- Both dates are worth having: "there since March" and "checked twenty
  -- minutes ago" are different reassurances.
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  PRIMARY KEY (destination_id, relpath)
);
CREATE TABLE backup_divergence_totals (
  destination_id INTEGER NOT NULL REFERENCES backup_destinations(id),
  category       TEXT NOT NULL,
  -- The complete number, from the comparison. Never a count of the rows above.
  count          INTEGER NOT NULL DEFAULT 0,
  checked_at     TEXT NOT NULL,
  PRIMARY KEY (destination_id, category)
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
    12: MIGRATION_012,
    13: MIGRATION_013,
    14: MIGRATION_014,
    15: MIGRATION_015,
    16: MIGRATION_016,
    17: MIGRATION_017,
    18: MIGRATION_018,
    19: MIGRATION_019,
    20: MIGRATION_020,
    21: MIGRATION_021,
    22: MIGRATION_022,
    23: MIGRATION_023,
    24: MIGRATION_024,
    25: MIGRATION_025,
    26: MIGRATION_026,
    27: MIGRATION_027,
    28: MIGRATION_028,
    29: MIGRATION_029,
    30: MIGRATION_030,
    31: MIGRATION_031,
    32: MIGRATION_032,
    33: MIGRATION_033,
    34: MIGRATION_034,
    35: MIGRATION_035,
    36: MIGRATION_036,
    37: MIGRATION_037,
    38: MIGRATION_038,
    39: MIGRATION_039,
    40: MIGRATION_040,
    41: MIGRATION_041,
    42: MIGRATION_042,
    43: MIGRATION_043,
    44: MIGRATION_044,
    45: MIGRATION_045,
    46: MIGRATION_046,
    47: MIGRATION_047,
    48: MIGRATION_048,
    49: MIGRATION_049,
    50: MIGRATION_050,
    51: MIGRATION_051,
    52: MIGRATION_052,
    53: MIGRATION_053,
    54: MIGRATION_054,
    55: MIGRATION_055,
    56: MIGRATION_056,
    57: MIGRATION_057,
    58: MIGRATION_058,
    59: MIGRATION_059,
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


_savepoints = 0


@contextmanager
def transaction(conn: sqlite3.Connection):
    """All of these writes, or none of them.

    Every connection here is opened `isolation_level=None`, which means
    autocommit: `with conn:` reads like a transaction and is not one. A
    multi-row change that raised half way through therefore left the half
    behind — a staged proposal retargeted at the delete pile with no approval
    to go with it was found exactly this way.

    A named SAVEPOINT rather than BEGIN so that nesting is legal; SQLite has no
    nested BEGIN, and these helpers call each other.
    """
    global _savepoints
    _savepoints += 1
    name = f"librairy_{_savepoints}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")
    finally:
        _savepoints -= 1


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


# How long a write on a render path is willing to wait for the writer lock
# before giving up. Deliberately far below the 5s connection-wide timeout: an
# optional write that blocks the page for five seconds has turned an
# intermittent error into an intermittent freeze, which is not an improvement.
RENDER_WRITE_TIMEOUT_MS = 250


@contextmanager
def impatient(conn: sqlite3.Connection, timeout_ms: int = RENDER_WRITE_TIMEOUT_MS):
    """Briefly shorten this connection's tolerance for a held writer lock.

    Everything off the render path keeps the ordinary five seconds, because a
    worker that has to retry a real write is a worker doing its job slowly,
    whereas a page that has to retry is a person watching a blank tab.
    """
    previous = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
    try:
        yield
    finally:
        conn.execute(f"PRAGMA busy_timeout={int(previous)}")


def best_effort_write(
    conn: sqlite3.Connection, statement: str, params: tuple, *, what: str
) -> bool:
    """A write the page does not depend on. Lock contention is not an error.

    Narrow on purpose. Only "database is locked"/"busy" is absorbed, and only
    for writes whose failure changes nothing the reader can see — a malformed
    statement, a constraint violation or a corrupt file still raises, because
    swallowing those would turn a real fault into a page that silently lies.

    It also gives up quickly. Absorbing the error but waiting the full busy
    timeout first would trade a rare 500 for a routine five-second stall.
    """
    try:
        with impatient(conn):
            conn.execute(statement, params)
    except sqlite3.OperationalError as exc:
        if not is_locked(exc):
            raise
        LOGGER.debug("skipped %s: %s", what, exc)
        return False
    return True


def is_locked(exc: Exception) -> bool:
    """True only for SQLite's writer-contention errors."""
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _migration_error(version: int, exc: Exception) -> Exception:
    """Say what a migration hit, in terms of the data rather than the index.

    Only the constraint migrations get this treatment, and only to point at the
    command that explains the conflict. A migration that fails because the data
    already breaks the rule it is about to enforce is not a bug in the
    migration, and "UNIQUE constraint failed: plans.audit_finding_id" tells
    nobody which correction to look at. The migration is *not* made to pass by
    deleting the offending rows: which of two approvals a person meant is not
    something a startup path may decide.
    """
    if version == 23 and isinstance(exc, sqlite3.IntegrityError):
        return DatabaseVersionError(
            "This database already has more than one active correction plan for "
            "a single finding, which is the state this version prevents. Run "
            "`librairy db check` to see the conflicting plans; nothing was "
            "changed."
        )
    return exc


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
        except Exception as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise _migration_error(version, exc) from exc
    #  The search index is derived, so a migration that changes its shape has
    #  to refill it. Migration 008 created it; migration 046 added the identity
    #  column and dropped the table to do so — either way an upgraded database
    #  reaches this line with an index that does not match the code.
    if starting_version < 8 <= SCHEMA_VERSION or starting_version < 46 <= SCHEMA_VERSION:
        from librairy.search import rebuild_search_index

        rebuild_search_index(conn)
    #  The moment automatic approval became possible on this database, so that
    #  turning it on does not reach backwards. Written here rather than at
    #  first use because a fresh install must be stamped *before* it has any
    #  proposals, and an upgrade must be stamped before the worker next runs.
    #  See `librairy/settled_queue.py`.
    if starting_version < 49 <= SCHEMA_VERSION:
        from librairy.settled_queue import stamp_activation

        stamp_activation(conn)

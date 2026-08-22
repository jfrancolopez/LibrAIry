from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from librairy.config import Settings

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 35


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
    if starting_version < 8 <= SCHEMA_VERSION:
        from librairy.search import rebuild_search_index

        rebuild_search_index(conn)

# Running LibrAIry

One canonical path for the things an operator does: install it, upgrade it,
back it up, put it back, work out whether it still describes your files, and
get out of trouble. The deeper documents are linked where they matter; this is
the page to read first and the one to keep open during an upgrade.

Four words are used precisely here and never as synonyms:

```
BACKUP      preserve state
RESTORE     put saved state back
RECONCILE   work out which saved facts still describe the current bytes
ROLL BACK   return to a previous build with a database it understands
```

## What version am I running?

```
docker compose exec librairy librairy version
```

```
version: 1.3.1
schema_supported: 47
revision: 9f2c41ab…        # "unknown" for a build with none recorded
```

The version also appears in the web footer. `schema_supported` is what this
build can migrate to; run it inside a configured installation and it also
reports `schema_current` — what your database is actually at — and whether a
migration is pending. It answers without a database, a configuration or a
writable mount, because it is what you run when something is wrong.

## Install

Prerequisites: Docker with Compose. Nothing else — every helper LibrAIry shells
out to (ffmpeg, exiftool, poppler, fpcalc, rmlint, czkawka, rclone) is inside
the image.

1. Copy `.env.example` to `.env` and set your four host paths:
   `HOST_INBOX_DIR`, `HOST_LIBRARY_DIR`, `HOST_QUARANTINE_DIR`,
   `HOST_APPDATA_DIR`.
2. `docker compose up -d`
3. Open `http://<host>:8080` and set a password. `DASHBOARD_PORT` changes
   that number; under compose it is the host side only, and the port inside
   the container stays 8080.

The database is created and migrated on first start. No AI provider and no
catalog key is required: without them LibrAIry organises on its own rules, and
every page still works. See [configuration](configuration.md) for the full list
and [Docker install](install-docker.md) / [Unraid](install-unraid.md) for
host-specific notes.

**Volumes and ownership.** The image runs as uid 1000 by default. Compose starts
the container as root so the entrypoint can remap to `PUID`/`PGID` (99:100 by
default, which is what Unraid uses) and chown the four mounts before dropping
privileges. Set `LIBRAIRY_USER=1000:1000` to skip remapping, in which case the
host directories must already be owned by that uid. If a mount is not writable
the entrypoint says so and exits rather than failing later in a way that looks
like a bug in LibrAIry.

## Backup

`HOST_APPDATA_DIR` holds the database, settings and thumbnails. The library,
inbox and quarantine roots hold your files. Both halves matter and they are not
the same thing.

The built-in one-way backup copies library files to an rclone remote and sends
a database snapshot up after any run that copied something. See
[one-way backup](backup.md).

**The two snapshots are not coordinated.** The database copy and the file copies
are taken at different moments, and nothing claims otherwise. That is exactly
why [Reconcile](restore-reconciliation.md) exists, and why a restore is not
finished until you have run it.

## Upgrade

**Before**

1. `docker compose exec librairy librairy version` — write down the version and
   `schema_current`.
2. `docker compose down`
3. Copy `HOST_APPDATA_DIR` somewhere safe. This copy is the only thing that
   makes a rollback possible; take it before anything else.
4. Check the copy is readable and not zero bytes.

**Upgrade**

5. `docker compose pull` (or `docker compose build` for a local image)
6. `docker compose up -d`
7. `docker compose logs -f librairy` — migrations run at startup and are logged.
8. `docker compose exec librairy librairy version` — `schema_current` should
   equal `schema_supported`, and `migration_pending` should be false.

**After**

9. Open Health, then Review and Commit. Anything that was waiting for Commit is
   still waiting; nothing is executed or cancelled by an upgrade.
10. If the filesystem changed while the app was down, open
    [Reconcile](restore-reconciliation.md).

**Supported range.** Every schema generation from 1 to 47 has a migration and
the chain is tested end to end from representative historical databases. There
is no intermediate release you have to stop at.

Each migration runs in its own transaction, so a step that fails leaves the
database at the generation *before* it rather than half way through one. What
that does not give you is a way back from the steps that already succeeded —
which is the whole reason step 3 is not optional.

## Restore

1. `docker compose down`
2. Put `HOST_APPDATA_DIR` back from your copy.
3. Put the library/inbox/quarantine roots back, or point `.env` at where they
   are now.
4. `docker compose up -d`
5. Rebuild the search index if it is stale: Health → *Rebuild index*, or
   `librairy index rebuild`.
6. **Reconcile.** See below — a restore is not finished without it.

## Reconcile

Restoring puts state back. Reconciling works out which of that state still
describes the bytes you actually have — because the database snapshot and the
files may be from different moments, and somebody may have moved things in the
meantime.

1. Scan, so the index describes what is on disk.
2. Open **Reconcile**.

It reports what still holds, what can be rebuilt, and what needs you. It writes
nothing, opens no file and moves nothing. Files whose exact bytes turn up at a
different path are offered as moves you can recognise; recognising one updates
LibrAIry's record without touching the file. Identical bytes in more than one
place are left ambiguous rather than guessed.

What no rebuild can remove: History, Format Policy, Decision Memory,
suppressions, withdrawals and recognised moves. A stale index is not the same
thing as a decision you never made. Full detail in
[restore reconciliation](restore-reconciliation.md).

## Roll back

**A container rollback is not a database rollback.**

Once an upgrade has migrated your database, an older build cannot run against
it. LibrAIry refuses rather than trying: a build that met a schema it does not
understand would write rows in shapes it does not know, and that is the one
failure a backup cannot help with. The refusal is enforced in code, not
promised in this document.

So the safe rollback unit is three things together:

```
the previous image
+ the pre-upgrade database snapshot
+ whatever filesystem reconciliation the gap needs
```

**If the upgrade has not run yet** — you pulled a new image but never started
it, so the database was never migrated — pull the previous tag and start it.
Nothing else is needed.

**If the upgrade has run:**

1. `docker compose down`
2. Restore `HOST_APPDATA_DIR` from the pre-upgrade copy you took at step 3
   of the upgrade.
3. Pin the previous image tag in `.env` or `docker-compose.yml`.
4. `docker compose up -d`
5. `docker compose exec librairy librairy version` — confirm you are on the
   version you meant, against a schema it supports.
6. **Reconcile.** The files are almost certainly ahead of the restored
   database.

**What a rollback costs you.** The restored database is the pre-upgrade one, so anything decided *after* that snapshot is not in it: approvals,
commits, undos, learned decisions, recognised moves. The files those decisions
moved are still where they were moved to — the filesystem was never rolled
back. That gap is real, it is expected, and Reconcile is how you close it.

Rollback is not lossless. Anyone who tells you to just start the previous image
against the current database is describing a different program.

## What LibrAIry never does on its own

- delete a file, ever — the delete queue is a folder you empty yourself
- overwrite a file
- move anything without an explicit approval and a Commit
- transcode or re-encode
- reorganise in the background, or on a timer
- call an AI provider or a catalog to draw a page
- reverse a decision that a later decision was built on
- repair what a validation finds

See [using LibrAIry](using-librairy.md) for the workflow itself, and
[Health](health.md) for what currently needs attention.

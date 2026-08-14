# Troubleshooting

Start with the Health page. It reports worker heartbeat, provider status, tools, disk space, database quick-check, and search-index rebuild.

## Startup Validation

If the container exits, run `docker logs librairy`. Startup failures are numbered and plain-language:

### Startup Errors

- Missing root path: create the host directory or fix the mount.
- Unwritable root path: fix ownership or set `PUID`/`PGID`.
- Inbox inside library or other nested roots: use separate top-level folders.
- Database cannot open: check appdata permissions and free disk space.
- Port is busy: change `DASHBOARD_PORT` or the compose port mapping.

## "database disk image is malformed"

The index is rebuildable and **no user files are affected** — LibrAIry never stores
anything in SQLite that it cannot recover by rescanning.

As of v1.2.0 LibrAIry picks its SQLite journal mode from the filesystem holding
`/data/appdata`: WAL on a local disk, DELETE on anything else. WAL synchronises
processes through an `mmap`'d shared-memory file, and on network or FUSE filesystems
each process gets its own view of it — the web process and the worker then both believe
they own the write-ahead log, and the index rots. This reproduced on Docker Desktop for
macOS within a single analyze run, and UNRAID's `/mnt/user` shares are the same class of
filesystem.

Check what was chosen:

```bash
docker exec librairy python3 -c "from pathlib import Path; from librairy.db import filesystem_type, journal_mode_for; p=Path('/data/appdata/librairy.db'); print(filesystem_type(p), journal_mode_for(p))"
```

Set `SQLITE_JOURNAL_MODE=WAL` or `=DELETE` to override the choice. Prefer moving appdata
to a local disk over forcing WAL onto a share.

To repair an already-corrupted index, stop the container first, then:

```bash
sqlite3 librairy.db ".recover" | sqlite3 librairy.db.fixed && sqlite3 librairy.db.fixed "PRAGMA integrity_check;"
```

Replace `librairy.db` with the rebuilt file (delete any `-wal`/`-shm` files beside it)
and start the container. If that fails, delete `librairy.db` entirely and rescan — you
lose the review queue and history, not a single file.

## Tools

The image includes `ffprobe`, `exiftool`, `fpcalc`, `rmlint`, and `czkawka_cli`. Missing or failing tools show warnings in Health with remedy hints.

## A Library Review Row Disagrees With Commit

A correction is approved in Review and Commit shows nothing, or a row offers
*Approve change* for something already waiting. Both are the same underlying
thing: the finding's status and the plan pointing at it no longer agree.

Run `librairy db check`. It reads and changes nothing:

```
1 inconsistency(ies); 1 can be repaired automatically, 0 need a decision.
 - open-finding-with-active-plan: Music/Pop/… — an approved plan is waiting
   for Commit, but the finding reads as unanswered
```

The pages themselves are already honest about this: an approved plan outranks
the status everywhere, so such a row shows **Waiting for Commit** and offers no
second approval. Repair is about the stored rows, not about what you see.

```
librairy db repair --finding-plan-state --yes
```

Repair applies only the cases where the database already contains the answer.
If anything ambiguous is reported — two active plans for one finding above all
— it refuses the entire run rather than guessing which approval you meant. Send
the correction back to Review and approve it again if that happens.

Nothing repairs itself at startup, deliberately: a database that quietly fixes
its own history hides the bug that produced it.

## Search Looks Stale, Or Incomplete

Two different problems.

**Files missing from results but visible in Browse** — they have not been
scanned. Run `librairy scan --root library`.

**Search says the index needs rebuilding** — the FTS index itself is damaged.
It fails quietly: a corrupt index returns *fewer* rows rather than an error, so
a short result looks like a genuine miss. Health and `librairy db check` report
it; Search shows a warning above the results.

```bash
librairy index rebuild
```

The rebuild drops and recreates the index from your item records. It reads no
media, changes no file, and cannot lose anything — every field in the index is
derived. Browse is unaffected throughout: it reads the filesystem and needs no
index at all.

## Logs

Logs are written to stdout for Docker and to `HOST_APPDATA_DIR/logs/librairy.log` with rotation. Set `LOG_LEVEL=DEBUG` temporarily for more detail. API keys and session tokens are redacted before records are emitted.

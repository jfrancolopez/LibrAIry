# Backup And Restore

Back up appdata and your mounted file roots with your normal NAS or backup tooling.

## Appdata

`HOST_APPDATA_DIR` contains the SQLite database, settings, thumbnails, and future logs. Stop the container before a simple file copy, or use SQLite backup tooling if backing up live.

## Library And Quarantine

The library contains approved committed files. Quarantine contains reversible duplicate/review storage. LibrAIry never deletes files from either root.

## Restore

1. Stop LibrAIry.
2. Restore appdata to `HOST_APPDATA_DIR`.
3. Restore inbox/library/quarantine roots to the same host paths, or update `.env` host paths.
4. Start LibrAIry.
5. Run `librairy index rebuild` or press Rebuild Search Index in Health.

The search index is derived. If it is missing or stale, rebuild it.

## After a restore: reconcile

Putting the bytes and the database back is half the job. The other half is
establishing that they still describe each other — the two snapshots are not
necessarily of the same moment, and the filesystem may have been rearranged
since either of them.

6. Scan, so the index describes what is actually on disk.
7. Open **Reconcile**. It reports what still holds, what can be rebuilt, and
   what needs you. It writes nothing and moves nothing.

Files whose exact bytes turn up at a different path are offered as moves you
can recognise; recognising one updates LibrAIry's record without touching the
file. Your decisions — History, Format Policy, Decision Memory, suppressions,
withdrawals — are never derived from the files and so are never rebuilt away.

See [Restoring, and agreeing about what you have](restore-reconciliation.md).

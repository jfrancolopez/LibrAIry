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

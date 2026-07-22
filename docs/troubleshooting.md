# Troubleshooting

Start with the Health page. It reports worker heartbeat, provider status, tools, disk space, database quick-check, and search-index rebuild.

## Startup Validation

If the container exits, run `docker logs librairy`. Startup failures are numbered and plain-language:

- Missing root path: create the host directory or fix the mount.
- Unwritable root path: fix ownership or set `PUID`/`PGID`.
- Inbox inside library or other nested roots: use separate top-level folders.
- Database cannot open: check appdata permissions and free disk space.
- Port is busy: change `DASHBOARD_PORT` or the compose port mapping.

## Tools

The image includes `ffprobe`, `exiftool`, `fpcalc`, `rmlint`, and `czkawka_cli`. Missing or failing tools show warnings in Health with remedy hints.

## Search Looks Stale

Run `librairy index rebuild` or use the Health screen rebuild button. The FTS index is derived state and can be rebuilt from SQLite item/proposal metadata.

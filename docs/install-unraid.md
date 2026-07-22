# Install on UNRAID

This guide installs LibrAIry as one Docker container with four bind-mounted shares.

## Prepare Shares

Create or choose these host paths:

```text
/mnt/user/librAIry/inbox
/mnt/user/librAIry/library
/mnt/user/librAIry/quarantine
/mnt/user/appdata/librairy
```

Fresh installs need nothing else. If your library still uses the legacy `RAM/`/`ROM/` zones: LibrAIry will never restructure an existing library. Option A (recommended): manually move top-level content to the plain categories (`Music/`, `Movies/`, `Shows/`, `Photos/`, `Documents/`, `Books/`, `Projects/`, `Misc/`) before first indexing. Option B: leave it; the indexer is structure-agnostic, old paths remain searchable/browsable, and newly committed files land in the plain categories at the library root, so the zones fade into legacy corners over time.

## Add The Template

Until LibrAIry is submitted to Community Apps, add the template from this repository:

```text
https://raw.githubusercontent.com/jfrancolopez/LibrAIry/main/packaging/unraid/librairy.xml
```

In UNRAID, open Docker, choose Add Container, select the LibrAIry template, then fill:

- WebUI Port: `8080` unless already used.
- Inbox Path: where you drop files to organize.
- Library Path: where approved files are moved.
- Quarantine Path: reversible duplicate/review storage.
- Appdata Path: SQLite database, settings, thumbnails, and logs.
- PUID/PGID: default `99`/`100` (`nobody:users`) works for standard UNRAID shares.
- Ollama Host: your LAN Ollama URL, often `http://host.docker.internal:11434` or `http://<machine-ip>:11434`.

Open the WebUI at `http://<unraid-ip>:8080`, create the admin password, then drop files into the inbox.

## Ownership

The container starts as root only long enough to map the internal `librairy` user to `PUID:PGID`, fix mounted directory ownership, and drop privileges. Files created in the library should be owned by the mapped UNRAID user/group.

## Health Check

UNRAID should show the container healthy after the web portal answers `/healthz`. If it does not, open container logs; startup validation prints numbered plain-language errors for missing paths, unwritable shares, nested paths, database failures, and port conflicts.

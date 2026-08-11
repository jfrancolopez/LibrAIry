# Install With Docker

LibrAIry runs as one container with four mounted directories.

## Compose Quickstart

```bash
cp .env.example .env
mkdir -p data/inbox data/library data/quarantine data/appdata
docker compose up -d --build
```

Open `http://localhost:8080` and drop files into the configured inbox. The portal is open by default; set a password in Settings -> Portal Security, or set `AUTH_REQUIRED=true` to require one from first run.

## Required Paths

Set these in `.env` for real use:

```text
HOST_INBOX_DIR=/path/to/inbox
HOST_LIBRARY_DIR=/path/to/library
HOST_QUARANTINE_DIR=/path/to/quarantine
HOST_APPDATA_DIR=/path/to/appdata
```

Inside the container they always mount as:

```text
/data/inbox
/data/library
/data/quarantine
/data/appdata
```

## Using Test Folders On macOS

For a safe local drill, point the host paths at throwaway folders on your Desktop:

```bash
mkdir -p ~/Desktop/librairy-test-inbox \
  ~/Desktop/librairy-test-library \
  ~/Desktop/librairy-test-quarantine \
  ~/Desktop/librairy-test-appdata
```

Then set these values in `.env`:

```text
HOST_INBOX_DIR=/Users/<you>/Desktop/librairy-test-inbox
HOST_LIBRARY_DIR=/Users/<you>/Desktop/librairy-test-library
HOST_QUARANTINE_DIR=/Users/<you>/Desktop/librairy-test-quarantine
HOST_APPDATA_DIR=/Users/<you>/Desktop/librairy-test-appdata
```

Apply the new bind mounts with `docker compose up -d --build`. Docker Desktop must be allowed to share the parent folder; `~/Desktop` is shared by default on standard macOS installs. The Settings screen shows these host paths read-only so you can confirm which folders the running container is using.

## Plain Docker Run

The image runs as the unprivileged `librairy` user (uid 1000) by default. `PUID`/`PGID`
remapping needs root to chown the mounts, so pass `--user 0:0` when you want it — the
entrypoint still drops to `PUID:PGID` before starting anything. Without `--user 0:0`,
make sure the host directories are owned by uid 1000, or the container stops with a
message naming the directory it cannot write.

```bash
docker run -d --name librairy \
  --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  -p 8080:8080 \
  --user 0:0 \
  -e PUID=99 \
  -e PGID=100 \
  -e OLLAMA_HOST=http://host.docker.internal:11434 \
  -v /path/to/inbox:/data/inbox \
  -v /path/to/library:/data/library \
  -v /path/to/quarantine:/data/quarantine \
  -v /path/to/appdata:/data/appdata \
  ghcr.io/jfrancolopez/librairy:latest
```

## First Checks

```bash
docker logs librairy
docker exec librairy librairy --version
docker exec librairy librairy ai status
```

If startup fails, the logs list numbered errors in plain language. Common fixes are creating the host directories, correcting ownership for `PUID:PGID`, separating nested inbox/library paths, or changing `DASHBOARD_PORT`.

## Reclaiming Docker disk space

Rebuilding the image repeatedly — during development, or after several
upgrades — leaves layers behind that nothing references. On Docker Desktop
these accumulate inside a fixed-size VM disk, and when it fills the symptom is
not obvious: the container dies with `No space left on device` writing a
temporary file, and no LibrAIry log explains why.

See what is actually there first:

```bash
docker system df
```

The two lines that grow are **Images** and **Build cache**. Both are
regenerable — an image can be rebuilt or re-pulled, cache is only a speed-up:

```bash
docker image prune
```

```bash
docker builder prune --keep-storage 2GB
```

Check the space came back, from inside the VM rather than from the host — on
macOS the host file stays the same size:

```bash
docker run --rm --privileged alpine df -h /
```

**Never pass `--volumes`, and never use `docker system prune --volumes`.** If
your inbox, library, quarantine and appdata are bind mounts they are not at
risk, but a named volume holding appdata would be, and the flag gives no
warning. There is nothing in a Docker volume that a LibrAIry cleanup needs.

Two other things worth knowing before reaching for `docker image prune -a`: it
removes every image not attached to a container, including other projects' and
any release tag you have not pushed. And a stopped container's writable layer
counts towards **Containers** in `docker system df` — often the largest line —
but removing a stopped container discards whatever is in that layer, so leave
any you did not create yourself.

This is a manual step on purpose. There is no automated cleanup job and none
should be added: it would eventually run against something you wanted.

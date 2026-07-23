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

```bash
docker run -d --name librairy \
  --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  -p 8080:8080 \
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

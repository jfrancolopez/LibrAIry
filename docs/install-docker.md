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
docker builder prune --reserved-space 4GB
```

(Older Docker spells that flag `--keep-storage`.) Give it a floor rather than
letting it take everything: cache is what makes the next rebuild quick, and
`docker builder prune -a` throws away the part that is still doing its job.

**`docker system df` is the honest number, not the image list.** Every LibrAIry
image is about 880 MB and roughly 650 MB of that is a base shared with all the
others, so adding up the `SIZE` column of eighteen stale builds suggests 15 GB
where the truth is nearer 4. The `RECLAIMABLE` column already accounts for the
sharing; `docker system df -v` shows the unique size per image if you want to
see where it went.

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

### Which LibrAIry images to keep

`docker compose build` retags `librairy:latest` and leaves the previous image
untagged, so one dangling image appears per rebuild. Eighteen of them collected
in a fortnight of development. That is normal, and the fix is knowing which
three to keep rather than pruning by age:

| Keep | Why |
|---|---|
| the image the container is running | it is the thing that is running |
| the previous known-good one | the rollback, if the new one is wrong |
| any published release tag | it is not rebuildable from `latest` |

Tag the rollback so a prune cannot take it, and so that in six months you know
what it was:

```bash
docker tag <old-image-id> librairy:pre-deploy-$(date +%Y%m%d)
```

Everything else with `<none>` for a name and no container attached is a
superseded build. Check that nothing references it — a dangling image can still
back a *stopped* container, and the buildx builder is one — then remove those
ids specifically rather than reaching for a prune that decides for you:

```bash
docker ps -a --format '{{.Names}} {{.Image}}'
```

```bash
docker rmi <id> <id> <id>
```

Before deleting anything, know which is which. `docker inspect <id> --format
'{{index .Config.Labels "org.opencontainers.image.title"}}'` says whether an
untagged image is even LibrAIry's; several projects' leftovers look identical
in `docker images`.

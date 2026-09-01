# Deploying a release to a NAS

**This is about putting a *published* LibrAIry release onto a machine that
already holds your library.** It is deliberately not written for UNRAID, or for
Synology, or for TrueNAS. Every one of them is a Linux box running Docker with
your files on a disk, and everything below only needs that much.

The companion documents are [Running LibrAIry](operations.md), which covers
install, backup, restore and reconcile in general, and
[Docker install](install-docker.md) for a first installation. This one is about
the specific, riskier act of replacing a version that is already holding data.

## The one-paragraph version

Find out what is really running. Take your own database snapshot, because
nothing takes one for you. Save the old image to a file, because it may not be
pullable. Migrate a *copy* first. Rehearse going back. Then upgrade, and leave
the files alone for a few minutes while you look.

## Never build on the NAS

The repository's `docker-compose.yml` contains `build:`. It exists so that a
developer can run `docker compose up --build` and get their working tree. That
is exactly the wrong thing on a machine holding your library: it produces an
image tagged `librairy:latest` whose source commit nothing records, and the next
`up --build` silently replaces it with something else.

Use [`docker-compose.release.yml`](../docker-compose.release.yml) instead. It has
no `build:` at all, so it can only run something that was published:

```bash
docker compose -p librairy -f docker-compose.release.yml up -d
```

Pin the exact version, never `latest`:

```yaml
image: ghcr.io/jfrancolopez/librairy:v1.3.1
```

`latest` moves. A version tag does not, and the image carries
`org.opencontainers.image.revision` so you can always ask a running container
which commit built it:

```bash
docker exec librairy librairy version
```

## Before you touch anything

### Find out what is actually running

Do not trust a note you wrote last time, and do not trust the compose file — the
container is the truth, because it was created from whatever the compose file
said *then*.

```bash
docker ps -a
docker inspect librairy --format '{{json .Mounts}}'
docker inspect librairy --format '{{.Image}}'
docker image inspect <that id> --format '{{json .RepoDigests}}'
```

The mounts tell you where appdata, inbox, library and quarantine really are.
Derive the paths from there, and from nowhere else.

### Find out what version and schema it is on

Do not start the container to ask. Ask the image, in a throwaway with no mounts
and no network:

```bash
docker run --rm --network none --entrypoint python <image> \
  -c 'import librairy; print(librairy.__version__)'
```

Older builds have no `librairy version` subcommand, no `build_info` module and
no revision label. If that is what you find, say so plainly and write down the
**image ID** — it is the only identity that build has.

For the schema, copy the database and read the copy (below). Never read the
number off a changelog.

## Take the snapshot yourself

**LibrAIry does not snapshot your database before migrating.** `db.migrate()`
applies each version in its own transaction and stops on the first failure; it
never copies anything first. Any `librairy.db.before-schema-NN` files you find
in appdata were put there by hand.

Stop the container, then:

```bash
docker compose -p librairy -f docker-compose.release.yml stop
cp appdata/librairy.db /somewhere/safe/librairy.db.pre-upgrade
shasum -a 256 /somewhere/safe/librairy.db.pre-upgrade
```

Check the copy is a working database, not just a file that exists:

```bash
sqlite3 /somewhere/safe/librairy.db.pre-upgrade \
  'PRAGMA user_version; PRAGMA integrity_check; PRAGMA foreign_key_check;'
```

Record the counts you care about — items, proposals, plans by status, plan
operations, history, quarantine entries. They are what you will compare against
afterwards.

**The snapshot is not optional, and the reason is one-directional.** The new
code refuses to open a database whose schema is *newer* than it supports, so
after the upgrade the old image will not start on the upgraded database at all.
There is no image-only rollback. Going back means going back to this file.

## Save the old image to a file

A locally built image has no `RepoDigests`. It was never pushed, so it cannot be
pulled again, and one `docker image prune` removes the only copy.

```bash
docker image tag <old image> librairy:pre-<new version>
docker image save <old image> librairy:pre-<new version> -o old-image.tar
shasum -a 256 old-image.tar
```

The extra tag stops a later rebuild of `librairy:latest` from taking its place.
Check the archive really contains what you think:

```bash
tar -xOf old-image.tar manifest.json
docker load -i old-image.tar     # must name the same config digest
```

## Save the configuration

Copy the compose file, the `.env`, and `docker inspect` output somewhere with
mode 600. The `.env` holds provider keys — treat it as a secret, not as a
document.

## Migrate a copy first

Take a *second* copy of the snapshot and run the real migration against it, with
the real published image, and with no media mounted:

```bash
docker run --rm --network none \
  -e APPDATA_DIR=/data/appdata -e INBOX_DIR=/data/inbox \
  -e LIBRARY_DIR=/data/library -e QUARANTINE_DIR=/data/quarantine \
  -v /tmp/dryrun/appdata:/data/appdata \
  -v /tmp/dryrun/inbox:/data/inbox \
  -v /tmp/dryrun/library:/data/library \
  -v /tmp/dryrun/quarantine:/data/quarantine \
  ghcr.io/jfrancolopez/librairy:vX.Y.Z librairy db migrate
```

Then check the copy: schema at the expected number, `integrity_check ok`,
`foreign_key_check` empty, and **every count from before still the same**. New
tables appearing with zero rows is the upgrade doing its job. An existing count
changing is not.

If you can spare the disk, start a throwaway container on the migrated copy and
open every page. On a filesystem with cheap snapshots or reflinks — btrfs, ZFS,
APFS — you can clone the whole media tree for this at almost no cost and get a
fully realistic test without touching the real one.

## Rehearse going back

A rollback plan you have not run is a guess. Start the *old* image against a
copy of the *pre-upgrade* snapshot and confirm it comes up healthy and serves
its pages. That is the whole claim you are relying on, and it takes two minutes.

## Then deploy

```bash
docker compose -p librairy -f docker-compose.release.yml pull
docker compose -p librairy -f docker-compose.release.yml up -d
docker compose -p librairy -f docker-compose.release.yml logs -f
```

Watch the migration go past. Then check, in this order:

- container running, healthcheck `healthy`
- `docker exec librairy librairy version` — version, schema and revision are the ones you meant to deploy
- `PRAGMA user_version` on the real database is the new schema
- `integrity_check` ok, `foreign_key_check` empty
- your counts match what you recorded

## Leave the files alone for a few minutes

After the migration and before you call it done: **do not Commit, Undo, Restore,
recognise a move, queue a deletion, change Format Policy, or approve anything.**
Read-only pages are fine — that is the point.

The moment a decision executes under the new version, files move, and the
snapshot you took describes a library that no longer exists in that shape.
Before that, rolling back is restoring a file. After it, rolling back is
restoring a file *and* reconciling a filesystem, which is a different and much
longer afternoon.

Use the window to look at Health, Reconcile, History, Search and the pending
plans, and to compare file counts against what you recorded.

Then carry on normally.

## Rolling back

1. Stop the new container.
2. Move the migrated database aside — do not delete it, it is the evidence.
3. Restore the pre-upgrade snapshot and verify its checksum.
4. `docker load -i old-image.tar` if the old image is gone.
5. Start the old version from the configuration you saved.
6. Verify version, schema, integrity and counts.
7. **Compare the filesystem to what you recorded.** If anything moved under the
   new version, the restored database is now wrong about where files are. Do not
   stop at step 6 — use Reconcile deliberately, or restore the files too.

## Notes that are actually about NASes

**Where appdata lives matters more than where the media lives.** LibrAIry picks
its SQLite journal mode from the filesystem under the database: WAL on a local
disk (ext4, xfs, btrfs, zfs, f2fs and friends), and DELETE on anything it does
not recognise as WAL-safe — which is what a network share is. DELETE is slower
under write load and uses only POSIX advisory locks, which network filesystems
implement correctly. That trade is made for you and needs no configuration, but
it does mean **appdata on an SMB or NFS mount will be slower than appdata on the
box's own disk**. Put appdata on local storage where you can. `SQLITE_JOURNAL_MODE`
overrides the choice if you have a reason.

**`DASHBOARD_PORT` is the host side of the mapping**, not the port the
application binds. Inside the container it is always 8080 — `EXPOSE`, the
healthcheck and the right side of the mapping all agree. Setting it to anything
else and letting `env_file` hand it to the application makes the app listen on a
port nothing is published to, and the container sits unhealthy for ever with a
UI you cannot reach.

**`PUID` and `PGID` decide who owns what.** The image runs as a non-root user;
the entrypoint remaps to the ids you give it and drops privileges. Use the ids
that own the share on the NAS — on UNRAID that is `99:100`, on other systems it
is whatever `id` reports for the account that owns the files. Set
`LIBRAIRY_USER=1000:1000` to skip remapping entirely, in which case the
directories have to already be owned by that uid.

**Check the architecture.** Releases publish `linux/amd64` and `linux/arm64`.
Most NAS boxes are amd64; ARM boards are not. Docker picks correctly on its own,
but if you moved an image by hand, confirm it:

```bash
docker image inspect <image> --format '{{.Os}}/{{.Architecture}}'
```

**Check free space before you start.** Migration, index rebuilds and thumbnails
all want room, and a NAS that has been filling up for a year is the normal case.

## What this does not cover

Backups. LibrAIry's own backup is one-way and off by default — see
[One-way backup](backup.md) and [Backup and restore](backup-restore.md). A
database snapshot is not a backup of your files, and the deployment steps above
assume you already have whatever file-level protection you intend to have. If
you do not, that is a thing to fix before an upgrade, not during one.

# One-Way Backup

Backups are disabled by default. LibrAIry uses `rclone copy`, never `sync`, `delete`, `purge`, or `move`.

1. Configure a remote with rclone outside LibrAIry.
2. Mount or place `rclone.conf` at `<appdata>/rclone/rclone.conf`.
3. Enable backup in Settings and set a remote such as `b2:librairy-backup`.
4. Committed library files are queued and copied out on worker cycles.

## What "backed up" means

A queue entry records the file, the path, and the **fingerprint of the exact bytes** it was asked to copy. It is only marked done when those bytes are the ones that went up:

1. the source is hashed and has to match the request before anything is sent,
2. `rclone copy` transfers it,
3. `rclone check` compares the remote against the local file,
4. the source is hashed again and has to still match the request.

Step 4 matters more than it looks. A file can change while it is being copied — an optimized version adopted, a re-encode, an external edit — and without it the transfer would succeed, the check would compare the new bytes against the new bytes, agree, and mark the *old* fingerprint done. The record would then claim bytes were off-site that were never sent anywhere.

If the file changed, the request is discarded rather than retried: no number of attempts can copy bytes that are no longer on the disk. Whatever is at that path now gets its own request, and Health reports anything left that looks untrue.

Verification strength depends on the remote. `rclone check` compares hashes where both sides can produce a common one and sizes where they cannot; LibrAIry's own fingerprint is blake2b, which no rclone backend offers, so that link is closed locally by the two hashes above rather than by asking the remote. Which comparison each backup got is recorded, and Health says so when a remote could only offer size.

Backup failures never roll back commits and never mutate local files from remote state. Restore is manual with rclone, for example:

```bash
rclone copy b2:librairy-backup /mnt/user/library-restore
```

What leaves the machine: organized library file contents, and a SQLite appdata snapshot when that option is enabled.

# One-Way Backup

Backups are disabled by default. LibrAIry uses `rclone copy`, never `sync`, `delete`, `purge`, or `move`.

1. Configure a remote with rclone outside LibrAIry.
2. Mount or place `rclone.conf` at `<appdata>/rclone/rclone.conf`.
3. Enable backup in Settings and set a remote such as `b2:librairy-backup`.
4. Committed library files are queued and copied out on worker cycles.

Backup failures never roll back commits and never mutate local files from remote state. Restore is manual with rclone, for example:

```bash
rclone copy b2:librairy-backup /mnt/user/library-restore
```

What leaves the machine: organized library file contents, and a SQLite appdata snapshot when that option is enabled.

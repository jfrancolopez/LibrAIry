# Using LibrAIry

1. Open the portal. It goes straight to the dashboard; setting a password is optional (Settings -> Portal Security).
2. Drop files or folders into the inbox host path.
3. Watch Dashboard and Health for worker progress.
4. Open Review, approve/edit/reject/postpone proposals.
5. Open Commit, create a plan, inspect it, then execute.
6. Use Search and Browse to find indexed files.
7. Use History to inspect or undo committed filesystem operations.

LibrAIry never commits automatically. Analysis only writes database proposals. File moves happen only through an approved immutable plan.

## Duplicates

Exact duplicates are staged for reversible quarantine review. Similar media flags are informational and require human judgment. LibrAIry never deletes duplicate files.

## Accessing Files

Use the Access page for SMB/FTP/WebDAV pointers. LibrAIry does not serve those protocols; your NAS or operating system does.

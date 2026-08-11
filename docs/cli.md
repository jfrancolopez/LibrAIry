# The command line

Everything the portal does to your library, it does by calling the same
functions the CLI calls. The commands exist so maintenance can live in a cron
job or a script instead of a browser tab.

Inside the container:

```bash
docker exec librairy librairy vanished list
```

## `--json`

`--json` is a global flag, and it works wherever you type it. These are the
same command:

```bash
librairy --json ai status
```

```bash
librairy ai status --json
```

In JSON mode **stdout is exactly one JSON document** — whether the command
succeeded, refused, or failed — so a pipe never gets half a stream:

```bash
librairy --json vanished list --root inbox | jq '.entries[].relpath'
```

Logs go to stderr and never mix into it.

Without `--json` the output is line-oriented, one `key: value` per line, with
lists indented underneath:

```
clearable: 2
already_resolved: 1
entries: 2
  root=inbox  relpath=_drop/gone.mkv  state=Waiting for review
  root=inbox  relpath=_drop/other.mkv  state=Approved, not committed
```

In that mode, results go to stdout and diagnostics go to stderr, so
`librairy ... > out.txt` collects the answer and not the complaint.

## Exit codes

| Code | Means |
|---|---|
| `0` | It worked — or there was nothing to do. |
| `1` | It partly worked: some operations were skipped or failed. |
| `2` | It was refused, the request was invalid, or it errored out. |

**Nothing to do is success.** `vanished clear --yes` when there is nothing left
to clear prints `cleared: 0` and exits `0`; that is an answer, not a fault. So
does a scan that finds no new files.

`1` is for work that ran and came up short — a commit where a source file had
moved under the plan reports `skipped_missing` and exits `1`.

`2` covers everything you asked for that did not happen: a missing `--yes`, an
argument that was not supplied, a plan that will not validate, a provider name
that does not exist, another LibrAIry process holding the lock.

That means a shell reads the way you would expect:

```bash
if librairy vanished clear --root inbox --yes; then echo done; else echo failed; fi
```

## Confirmation

Commands that change things need `--yes`: `commit`, `undo`, `vanished clear`.

Without it they tell you what they would have done, change nothing, and **exit
`2`** — a refusal is a failure, not a quiet success, and a script that ignores
the exit code should not be able to believe the work happened.

```bash
librairy --json vanished clear --root inbox
```

```json
{
  "error": "confirmation_required",
  "message": "vanished clear requires --yes",
  "root": "inbox",
  "would_clear": 7
}
```

Every failure payload carries a stable `error` code and a human `message`. The
codes are `confirmation_required`, `argument_required`, `provider_not_found`
and `internal_error`, so a script can tell "run it again with `--yes`" from
"that plan does not exist" without reading English.

There is no `--dry-run`. Where a preview is useful, a read-only command already
gives it: `vanished list` prints exactly the entries `vanished clear` would
resolve, and `plan show` prints exactly the operations `commit` would run.

## `ai status` versus `ai test`

They fail differently, on purpose.

```bash
librairy --json ai status
```

Exits `0` even when every provider is offline. You asked for the status and you
got it; *offline* is the answer, not a failure to answer.

```bash
librairy ai test ollama-primary
```

Exits `1` when the provider is unreachable or the round trip does not complete,
because the round trip is the thing you asked for. Naming a provider that does
not exist is a different matter — that is `provider_not_found` and exit `2`.

## Grouped commands

`plan`, `proposals`, `quarantine`, `db`, `index`, `ai` and `vanished` all take a
subcommand and will not run without one. `librairy vanished` on its own prints
its usage and exits `2` rather than doing nothing quietly.

## Commands

| Command | Does |
|---|---|
| `scan [--root inbox\|library\|quarantine]` | Index what is on disk. `--root library` also rebuilds the folder-pattern map. |
| `analyze [--limit N] [--reanalyze]` | Turn scanned items into proposals. |
| `proposals list \| show <id>` | Read the review queue. |
| `propose-plan [--min-confidence X] [--ids ...]` | Compile proposals into a draft plan. |
| `plan create --from-file <spec> \| show <id> \| approve <id>` | Build, inspect and approve a plan. |
| `commit <plan-id> --yes` | Execute an approved plan. |
| `history [--plan <id>] [-n N]` | The journal. |
| `undo --op <id> \| --plan <id> --yes` | Put moves back. |
| `quarantine list \| restore <id> \| restore --all` | The shelf. |
| `vanished list \| clear --root <root> --yes` | Records whose file is no longer on disk. Deletes no file and no record. |
| `audit run [--scope <folder>] [--no-tags]` | Examine files already in the library. Writes findings; moves nothing. |
| `audit list [--scope <folder>]` | Show open findings. |
| `index rebuild [--content]` | Rebuild the search index from existing rows. Discovers nothing. |
| `db path \| migrate` | Where the database is; apply migrations. |
| `ai status \| test [provider]` | Provider configuration and a live round trip. |
| `worker [--once]` | Run the background worker. |
| `run` | Web and worker under the supervisor — what the container does. |

## If you scripted against an older build

Three things changed when the contract was pinned down:

- **Refusals now exit `2`.** `commit`, `undo`, `vanished clear` and
  `quarantine restore` without their required argument used to print a
  complaint and exit `0`. A script that only checked the exit code was told
  the work had happened.
- **`error` is now a code, not a sentence.** `"error": "commit requires --yes"`
  became `"error": "confirmation_required"` with the sentence moved to
  `"message"`.
- **In `--json` mode, errors print on stdout**, alongside every other JSON
  document, rather than on stderr.

`--json` placement, command names, and successful payloads are unchanged.

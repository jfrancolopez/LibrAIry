# Health — what needs attention now

Health used to answer *is the machinery working*: are the helper binaries
installed, does the AI endpoint reply, is there disk space, did the backup run.
All of that is worth knowing and none of it is what you open a health page to
find out once LibrAIry has been running for a while. By then the question is:

> is anything waiting on me, and is anything quietly wrong?

Both halves are already in the database. An approval whose source has moved on
is a row in `plan_ops` beside a row in `items`. A queued file somebody deleted
over SMB is a `missing_since`. An audit that stopped half way is an
`audit_runs` row. Nothing on this page is measured, probed or discovered — it
is read, and it is read from the same tables the workflow that owns it reads.

## Three levels, and they mean something

```
Needs a decision   something can no longer do what it says it will
Worth knowing      true, and better known before it becomes the first kind
Information        current operational state, and not a problem
```

`Critical / High / Medium / Low` is incident-response vocabulary. Applied to a
photograph nobody has measured yet it turns an ordinary backlog into a wall of
warnings, and a page that cries wolf once is a page people scroll past.

The clearest case is **blocked Undo**. Four decisions that cannot be reversed
because later ones were built on them is the safeguard *working*. Nothing is
lost, nothing is broken, and there is nothing to do about it — so it is
information, and it says which decision to reverse first if you want the
earlier one back.

| state | level | why |
| --- | --- | --- |
| a waiting decision's file changed or vanished | needs a decision | the approval cannot run |
| two waiting decisions conflict | needs a decision | only one of them can be right |
| a queued file changed or is gone | needs a decision | Restore would put back different bytes |
| the last audit failed | needs a decision | stages after it never ran, and it stopped on an error |
| the last audit was stopped | worth knowing | later stages did not run |
| arriving photographs not yet measured | worth knowing | their companions cannot be established until they are |
| the Format Policy impact snapshot is out of date | worth knowing | the figures no longer describe the library |
| files present but not in the search index | worth knowing | Browse finds them, Search does not |
| files waiting in the delete queue | information | nothing is removed unless you do it |
| a decision cannot be undone yet | information | the sequencing rule, working |
| folders set to preserve originals | information | you configured them |

## Health does not repair anything

`GET /health` writes zero rows. It runs no subprocess, opens no file and calls
no provider. Every remedy lives on the page that owns the workflow, where the
rest of the context is — so each concern carries a link and nothing else.

There is no *Fix all*. A button that answers six different problems at once is
six decisions taken by one press, and every one of those decisions is about
files.

Two things this specifically does **not** do:

* **It never re-measures the Format Policy impact.** Measuring walks every
  indexed library row. A page that did that while drawing itself would be the
  slowest page in the program and would get slower the more you own. Health
  reports the snapshot's age; the Format Policy page re-measures on request.
* **It never calls an audit overdue.** LibrAIry has no configured audit
  cadence — an audit is something you start — so there is nothing to be late
  for. It says *last completed five days ago* and leaves the judgement to you.

## What it can and cannot see

Stale approvals are answered **from the index**, not by hashing. `plan_drift`
opens every source file and is right for one card; a page summarising the whole
queue cannot open every file in it. So Health compares
`plan_ops.src_fingerprint` against what the scanner last recorded in `items`.

That can *under*-report — a file changed since the last scan looks unchanged
here — and under-reporting is the safe direction: the Commit card re-hashes
when it is drawn, and the executor re-hashes again before it moves anything.
Health points at the workflow; the workflow decides.

Blocked-Undo counting is bounded to the **200 most recent decisions**, and the
page says so. A count over the whole journal is a self-join over every
operation ever carried out. The per-plan answer — the one that actually gates a
reversal — is never bounded.

## Bounded, at every size

Counts come from aggregate SQL; examples are capped at three; nothing loads a
row per plan, per history entry or per queued file into Python. The page body
is the same size on a library with a hundred waiting decisions and one with ten
thousand.

See also [Undo and the order of decisions](undo-sequencing.md),
[Waiting-decision conflicts](plan-conflicts.md), and
[The delete queue](delete-queue.md).

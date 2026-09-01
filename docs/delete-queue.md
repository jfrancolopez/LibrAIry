# The delete queue

**Nothing in it is ever deleted by LibrAIry.**

`quarantine/_to-delete` is where a file goes when you answer *I am finished with
this*. LibrAIry has never emptied that folder and does not offer to: emptying it
is a thing you do, deliberately, in your own file manager.

What was missing was any way to look. The queue was a folder — you could put
files in and had no way to see what was in there, how much of the disk it was
holding, when any of it arrived, or which decision sent it.

## What the page shows

```
DELETE QUEUE
  5 files · 387.7 KB still on disk, waiting · oldest queued 42 days ago

QUEUED TOGETHER
  4 files · 992 B — 20 August
    IMG_9001.JPG  IMG_9002.JPG  IMG_9003.JPG  IMG_5402.jpeg

WAITING TO BE REMOVED
  IMG_9003.JPG      248 B · queued 42 days ago
    came from library/Photos/Wedding/IMG_9003.JPG
    The folder this came from, Photos/Wedding, is now set to preserve
    originals. Queuing it was still your decision — this is only worth
    knowing before you remove it.
    [Restore]  [Details]
```

**Waiting, never saved.** Every byte counted is still on the disk, in full.
`387.7 KB saved` would be a claim about storage the disk does not support until
somebody has actually removed it.

**Grouped by the decision that sent them**, never by date or reason. Two answers
you gave separately stay two decisions.

**Provenance is read, never reconstructed.** Where a file came from and why come
from the quarantine entry and the plan that moved it — not from parsing the
filename it happens to have.

## The state of the bytes

| | means |
| --- | --- |
| — | there, and the bytes that were queued |
| **Not on disk** | something removed it outside LibrAIry |
| **Changed since it was queued** | these are not the bytes the decision was about |

Both are answered from the index rather than by hashing on page load. `Restore`
is not offered for either, because restoring would put something else back where
the original used to be.

## Restore

Deferred, like every other decision: nothing moves on the click. It becomes an
approved plan and a card in Commit, verified by hash on the way — the same
implementation as a restore from Quarantine, so a file that came back from the
delete queue and one that came back from Quarantine cannot behave differently.

## Current context, historical decision

A file queued last month may now sit under a folder you have since set to
[preserve originals](architecture/format-policy.md), or be half of a [Live
Photo](architecture/relationships.md). Both are worth knowing before it is removed for good,
and neither retroactively cancels the decision that put it there. LibrAIry shows
them and pulls nothing back out on its own.

## What this page cannot do

No `Empty queue`. No `Delete all`. No expiry, no thirty-day rule, no schedule.
Adding any of them is a separate decision with its own safety work — not a
button on a page whose purpose is to let you look before you act.

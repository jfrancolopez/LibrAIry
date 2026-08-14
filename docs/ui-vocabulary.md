# UI vocabulary

One word per idea, across every page. Different pages using different verbs for
the same action is what made LibrAIry feel like several applications sharing a
navigation bar.

This is a developer reference. It is not shown to users; it exists so that a
new control can be named by looking something up rather than by inventing.

## Decisions

| Say | For | Never |
|---|---|---|
| **Approve** | admitting a new file from the inbox | Accept, Confirm |
| **Approve change** | changing a file already in the library | Accept correction |
| **Dismiss suggestion** | recording that you do not want a suggestion | No change, Ignore |
| **Restore suggestion** | putting a dismissed suggestion back | Undismiss |
| **Restore** | putting a quarantined *file* back where it came from | Put it back |
| **Delete queue** | gathering a held file into the folder you empty yourself | ~~Mark for deletion~~ |

`Mark for deletion` is banned. It reads as "a deletion has been arranged", and
LibrAIry has never deleted a file. Worse, the same label sat on two buttons with
opposite behaviour — one moved a file immediately, one waited for Commit.

The three structural choices for a multi-artist folder — **Keep together**,
**Organize individually**, **Leave current layout** — keep their own names. They
are three different libraries, not three ways of saying no.

## Before execution

| Say | For |
|---|---|
| **Commit** | carry out what was approved |
| **Send back to Review** | return an approved correction to the queue |
| **Remove approval** / **Remove old approval** | withdraw an approval, the second when it has gone stale |
| **Cancel request** | withdraw a quarantine decision |

Never **Undo** here. Nothing has moved yet, and teaching people that Undo
sometimes means "before" is how somebody comes to believe it will rescue a
commit they never made.

## After execution

| Say | For |
|---|---|
| **Undo** | reverse files that actually moved |
| **Undo plan** | reverse every operation in one plan |

There is one Undo, hash-verified, shared by History, Commit and the correction
pages. A second reversal path is a second thing to get wrong.

## Information

**Details** · **Evidence** · **Compare** · **Preview** · **Files** ·
**About .EXT**

These open something. They never change state, so they are never styled as
primary actions.

## Structure

**Expand** · **Collapse**

Visual only. Collapsing a group is not dismissing it, and no collapse state is
persisted anywhere.

## States a row can be in

    Ready to approve · Observation · Needs analysis again · Not on disk
    Waiting for Commit · Approval is outdated · Applying · Corrected · Dismissed

Derived, never the stored status column. See `web/actionability.py`.

## Operation types on Commit

    FILE · MOVE · RESTORE · DELETE QUEUE

Always rendered as text. Colour may reinforce a badge; it may never be the only
thing carrying the meaning.

## Rules for a label

- One to three words. Longer than that and it is explanation wearing a button —
  put the explanation in the text underneath.
- A verb, unless it names a place you are going.
- Sentence case. Not Title Case, and not SHOUTING.
- The same action gets the same word on every page.

## Empty states

Every list says what would appear there and how things get there. Never "No
data" or "Nothing here".

    Quarantine is empty.
    Files set aside for review or safely isolated appear here.

    Nothing waiting to commit.
    Approved changes appear here before LibrAIry moves anything.

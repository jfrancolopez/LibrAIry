# UI vocabulary

One word per idea, across every page. Different pages using different verbs for
the same action is what made LibrAIry feel like several applications sharing a
navigation bar.

This is a developer reference. It is not shown to users; it exists so that a
new control can be named by looking something up rather than by inventing.

## The lifecycle every decision follows

Almost everything a person does in LibrAIry is one of these, in this order.
A screen may skip a step; it may not reorder them, and it may not use one
step's words for another step's act.

    Observe / Analyse       LibrAIry looks and forms an opinion. Nothing moves.
        |
    Review                  You read the opinion. Nothing moves.
        |
    Approve                 You decide. Nothing moves.
        |
    Waiting for Commit      The decision exists as a plan. Nothing has moved.
        |
    Commit                  The executor moves files, hash-verified, journalled.
        |
    History                 What happened, permanently.
        |
    Undo                    Reverse it, where the files are still where the
                            journal says they are.

Deviations exist and are deliberate. `Keep original` and `Restore original`
act immediately because they reverse a commit that has already happened —
they are Undo wearing the name of the outcome a person wants. `Analyse again`
and `Dismiss suggestion` change no file at all.

## The nine words the whole product is built from

| Word | Means exactly |
|---|---|
| **Current** | where the file is now |
| **After Commit** | where it would be if you committed |
| **Waiting for Commit** | approved, and nothing has moved |
| **Undo** | reverse something that actually happened |
| **Cancel request** / **Send back to Review** | withdraw something that has *not* happened |
| **Dismissed** | you told LibrAIry not to suggest this for now |
| **Restore suggestion** | reconsider a dismissed one |
| **Preserved original** | the original kept when an optimized version was adopted |
| **Delete queue** | a folder you empty yourself; LibrAIry has still deleted nothing |

Two rules follow from the table, and `tests/test_control_inventory.py` holds
both against every control on every populated page:

- **No two controls share a word and mean different things.** `Commit` was on
  the button that commits *and* on a link to the Commit page. `Cancel` was
  beside `Cancel request` while meaning "stop the encoder".
- **No two words mean the same thing.** Getting to Commit had four labels;
  re-running the analyser had two; asking a provider whether it answers had
  three.

Where a repeat is deliberate — a search box on each page, a bulk control
beside its row-level twin — it is listed in that test with the reason. The
list is the specification; a label not in it and not unique is drift.

## Decisions

| Say | For | Never |
|---|---|---|
| **Approve** | admitting a new file from the inbox | Accept, Confirm |
| **Approve change** | changing a file already in the library | Accept correction |
| **Dismiss suggestion** | recording that you do not want a suggestion | No change, Ignore |
| **Restore suggestion** | putting a dismissed suggestion back | Undismiss |
| **Restore** | putting a quarantined *file* back where it came from | Put it back |
| **Delete queue** | gathering a held file into the folder you empty yourself | ~~Mark for deletion~~ |
| **Restore original** | undoing an optimization: the preserved original becomes the live file again | Undo optimization, Revert |
| **Keep original** | taking a preserved original back out of the delete queue, leaving the optimized version live | Remove from delete queue |

**Restore original** and **Keep original** are deliberately different decisions
rather than one with a confirmation. "I have changed my mind about deleting
this" is not "I want the old file back", and from the delete queue both are
available at once.

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

## Words this pass retired

Every one of these was live in the product and meant the same as the word
beside it. They are listed so that a future search for "why is it not called
X" has an answer.

| Was | Now | Where |
|---|---|---|
| Commit 2 / Commit them / Back to Commit | **View in Commit** | Review, Dashboard, commit progress |
| Send all back / Send back | **Send back to Review** / **Cancel request** | Commit |
| Re-analyse | **Analyse again** | Review inbox rows |
| Open details | **View details** | Search results |
| View details (→ Health) | **Open Health** | the search index warning |
| Test it / Test connection | **Test** | Settings catalogs, LM Studio |
| Cancel (stop an encoder) | **Stop** | optimization queue |
| discard | **Discard changes** | Settings save bar |
| No suggestion | **Dismiss suggestions** | storage opportunities |
| Review details | **Details** | library review row |
| Apply | **Apply filters** | Review filter panel |
| Undo (a review decision) | **Cancel decision** | Review undo bar |
| Marked for deletion | **Headed for the delete queue** | vanished files, review undo |
| "Put it back" | **Restore** | quarantine caption |
| Waiting on you | **Suggested for quarantine** | Quarantine |

## Empty states

Every list says what would appear there and how things get there. Never "No
data" or "Nothing here".

    Quarantine is empty.
    Files set aside for review or safely isolated appear here.

    Nothing waiting to commit.
    Approved changes appear here before LibrAIry moves anything.

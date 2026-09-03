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
| **Waiting for AI** | analysis stopped rather than guess, and is waiting for a provider |
| **Tag** | what a file is about, written by you — searchable, and evidence on the next decision about it |
| **Project** | files that belong together, wherever they live — a view, never a place |
| **Project folder** | `Projects/{project}/` — a filing destination on disk |
| **Undo** | reverse something that actually happened |
| **Cancel request** / **Send back to Review** | withdraw something that has *not* happened |
| **Dismissed** | you told LibrAIry not to suggest this for now |
| **Restore suggestion** | reconsider a dismissed one |
| **Preserved original** | the original kept when an optimized version was adopted |
| **Delete queue** | a folder you empty yourself; LibrAIry has still deleted nothing |
| **Set aside** | a file moved out of the library into Quarantine, where it can be restored |

**"Project" is two things, and they are told apart by one word.** A **Project**
is a promoted tag: a view across the library, whose members are the files
carrying that tag wherever they happen to live. A **Project folder** is
`Projects/{project}/`, a real directory that files are moved into, and it is
what the `projects` category files things as. Promoting `#ProjectHouse` moves
no file into `Projects/`; filing something into `Projects/` creates no Project.

The category is therefore labelled **Project folders** wherever a category name
is shown, so that a badge and a page heading cannot read as the same thing.
`tests/test_tags.py` holds the distinction.

**"Waiting" is never a word on its own.** Three things in LibrAIry wait and they
wait for different events: an approval waits for Commit, an encode waits for a
slot, and a held file waits for an AI provider. Each is written out in full
wherever it appears — *Waiting for Commit*, *Waiting* as an optimization job
state inside the queue that owns it, *Waiting for AI* — and the section heading
for the third is **Needs more processing**, which is what the reader wants to
know before they want to know why.

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
| **Set this copy aside** | choosing which of two identical files goes to Quarantine | Delete, Remove duplicate, Keep this one |
| **Set aside duplicate** | the same act on an *arrival* that is already in the library | Approve, Reject, Skip |
| **Keep existing** / **Use incoming** / **Keep both** | answering one collision inside a folder merge | Overwrite, Replace, Skip |
| **Restore original** | undoing an optimization: the preserved original becomes the live file again | Undo optimization, Revert |
| **Keep original** | taking a preserved original back out of the delete queue, leaving the optimized version live | Remove from delete queue |

**Keep existing** and **Use incoming** are named for the *outcome*, never for
the mechanism. `Overwrite` and `Replace` are both wrong twice over: nothing is
overwritten — the copy that loses goes to Quarantine — and both words describe
what happens to bytes rather than which file you end up with.

**Set aside** is not **Delete queue** and not **Quarantine**. The delete queue is
where you put something you have finished with; setting a copy aside is the
decision that one of two identical files is the spare. It is phrased as an act
on *this copy* — "Set **this** copy aside" — because the row carries one control
per file and a label that did not say which would be the worst button in the
product.

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

Four words, and which one you use depends on *what* is being withdrawn and
*where it goes back to*. They are one family — never **Undo** — and the family
is what a reader learns; the object is what tells them apart.

| Say | For | Goes back to |
|---|---|---|
| **Commit** | carry out what was approved | — |
| **Send back to Review** | withdraw an approval, from Commit | the Review queue it came from |
| **Remove approval** / **Remove old approval** | withdraw an approval where it was made, the second when it has gone stale | nowhere: the row is already in front of you |
| **Cancel request** | withdraw a quarantine decision or an adopted optimization | nowhere: there was no queue, only a request |
| **Cancel decision** | withdraw the last thing you did in Review, whatever it was | the queue, undone |

**Cancel decision** is the only one that is time-ordered rather than
row-scoped: it takes back one press of Approve, Quarantine, Later or Analyse
again, over however many files that press covered. Singular on purpose — one
press is one decision — and the sentence beside it carries the count:

    Approved 3 files.   [Cancel decision]
    Put off 1 file.     [Cancel decision]

It was **Undo**, which is reserved for bytes that moved. Two words for
"reverse" would eventually teach somebody that Undo works before Commit as
well, and the one place that belief is expensive is the one place it is wrong.

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
    Conflicts with another decision

Derived, never the stored status column. See `web/actionability.py`.

`Approval is outdated` and `Conflicts with another decision` both stop Commit
being offered and they are **not** the same badge. The first says a file
changed underneath an approval; the second says two decisions were approved
that cannot both happen. Telling somebody the wrong one sends them looking in
the wrong place.

## Reconciling

**Recognize new location**

The one control on the Reconcile page. It agrees that a file is where it now
is; it moves nothing, and it never puts the file back where LibrAIry would have
filed it. Deliberately not `Restore`, `Relink` or `Fix` — the first two already
mean something else here, and nothing is being fixed.

There is no `Re-open` on a withdrawn decision. A month-old withdrawal may name
files that have since moved, so making the decision again is a decision, not a
button.

## How much something matters, on Health

    Needs a decision · Worth knowing · Information

Three levels, and each has to mean something — see [health.md](architecture/health.md).
Not `Critical / High / Medium / Low`: that is incident-response vocabulary, and
applying it to an ordinary backlog teaches people that the red section contains
things that are fine.

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

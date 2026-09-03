# Decision memory and smart suggestions

**LibrAIry learns from explicit decisions that completed, and uses what it
learns to *suggest*. It never uses it to act.**

This is a permanent product principle, not a feature of one screen. Every
domain — documents, music, photographs, whatever comes next — may eventually
consume it, and every one of them consumes it the same way: as the weakest kind
of evidence in the program.

## The problem it solves

Review is where a person teaches this program things. Until decision memory, it
forgot every lesson the moment the file moved. File the fourth Honda manual
under `Documents/Manuals/Honda Motor Co/` and the fifth arrives knowing
nothing — so the same correction is made for ever, and the only way to stop
making it is to make the classifier cleverer about Honda specifically.

```
Before          evidence → LibrAIry proposes → you decide

Now             evidence
                  + your completed decisions
                → LibrAIry proposes what you usually choose
                → you confirm or override
```

**It accelerates Review. It does not bypass Review.** "Smart" here means fewer
repetitive decisions — never fewer opportunities to catch a bad one.

## Authority

Five kinds of thing can have an opinion about a file. They are not equal, and
the order is fixed:

| | | may be overridden by |
|---|---|---|
| **1. Safety invariant** | never overwrite, never delete, revalidate hashes before moving | nothing |
| **2. Explicit user policy** | `music.preferred_format = mp3`, a promoted rule | the owner changing it |
| **3. Strong current evidence** | a catalog identity, an ISBN, a DOI | better evidence about the same file |
| **4. Explicit user evidence** | `#ProjectHouse` — what the owner said about *this* file | a fact about the file's identity |
| **5. Learned suggestion** | "you filed six Honda manuals here" | all of the above |
| **6. Weak heuristic / model cue** | a filename that looks like a year | all of the above |

A learned pattern sits at (5) deliberately. It is a statement about files that
*resembled* this one; a catalog identity is a statement about *this* one. Six
Queen tracks filed under one release is a habit. MusicBrainz saying this
recording belongs to another release is a fact, and the fact wins.

### A tag and a habit about tags are two different facts

(4) is above (5) because a person wrote it, on purpose, about the file in front
of you. It counts in the decision being made **now** — it does not wait to be
learned from:

    #ProjectHouse                    what the owner is saying, now
    "they file #ProjectHouse docs    what LibrAIry has learned they tend to do
     under Documents/House"           with that kind of hint

Both are real and the program keeps both. The first joins the file to that
Project immediately, is evidence on the proposal, and is asked before an
inferred cue when both could answer. The second is a count of decisions, and
still needs the decisions.

What (4) may **not** do is name a destination or pick a category. It is a
statement about *context*, and context does not identify a file: `#ProjectHouse`
on an installer leaves the installer alone. So it never contradicts (1)–(3) —
it is not answering their question — and where the owner's own policy at (2)
says where something goes, a tag is not a way around it.

Explicit policy at (2) outranks it for a related reason: the owner said what
they wanted, in a setting, on purpose. If they then override that policy
repeatedly, that is worth *telling* them. It is not grounds for LibrAIry to
change the setting on their behalf.

## What counts as a lesson

**Learned from:** an explicit choice that completed.

- a destination approved in Review, and then committed
- a representation kept when the members differed in format, once the plan ran
- an explicit no-op — *keep both formats*, *keep all of them* — which completes
  the moment it is given, because there is no plan for a commit to run

**Never learned from:** anything that is not a completed decision.

- previewing, opening Evidence, expanding a panel, hovering
- selecting an option before approving
- a failed or refused commit — the choice never happened to a file
- a plan that was withdrawn
- what the scanner or the classifier produced on its own

**Undo takes the lesson back.** Choosing A, committing, undoing and then
choosing B must not leave A remembered as something that worked. Whether a
decision was reversed is *read from the journal* rather than stored a second
time, so the two can never disagree.

## What a pattern is made of

Cues that recur, and nothing else:

    category · document type · organization · author · year
    source folder · media format

Never a filename as an equality (`invoice-82741.pdf` matches one file for
ever), and never anything the document *says*. No body text, no OCR, no
account numbers, no amounts. Decision memory holds `type=financial,
organization=Chase` and never the sentence that told LibrAIry so.

Destinations are learned as **templates** wherever the filing policy is one:

    Documents/Financial/{year}          not  Documents/Financial/2024
    Documents/Manuals/{organization}    not  Documents/Manuals/Honda Motor Co

Four statements from 2024 teach where statements go — not that everything goes
in the 2024 drawer. A template that needs a cue the current file does not carry
produces no suggestion at all, because half a path is not a destination.

## When it speaks

- **Three completed decisions.** One is a precedent, two is a coincidence.
- **More confirmations than twice the departures.** Five against four is two
  habits, not a preference, and "you usually choose A" would be a claim the
  decisions do not support. A divided history produces *no* suggestion rather
  than a majority.
- **The narrowest matching pattern wins**, decided by how many cues had to
  agree and by nothing else. Honda manuals beat manuals; manuals beat
  documents. There is no score, which is why there is nothing to explain away.
- **Only when it would change something.** A suggestion agreeing with the
  destination already proposed is furniture.

Every suggestion says its evidence in decisions:

> Suggested from 6 previous decisions about category documents, document type
> Manual, organization Honda Motor Co.

Never a confidence percentage. A count can be checked; a score cannot.

## What it may do

Nothing, on its own. A suggestion is a sentence and two buttons:

- **Use this suggestion** — resolves the pattern again *now* (it may have been
  suppressed or outgrown since the page was drawn) and then performs the
  ordinary destination edit. The proposal is still `proposed` afterwards, still
  needs approving, and still moves nothing until Commit.
- **Don't suggest this pattern** — turns off the conclusion, including any
  broader cue that reaches the same answer. It does not delete the decisions
  behind it: those are what happened, and they stay in History.

A suggestion never makes a `CHOICE` row approvable. An unresolved choice with a
suggestion on it is still an unresolved choice.

## Where it lives

    decision_events        one explicit choice: its cues, its answer, and the
                           plan that carried it out. Completion and reversal
                           are read from the journal, not stored here.
    decision_suppressions  conclusions the owner asked not to be offered.

Local, always. Decision history is never sent to a model, a cloud provider or a
catalog — it needs none of them. There is no training step, no embedding, no
vector store and no background daemon: patterns are counted from the events at
the moment a page asks, in one query for the whole page.

`/review/learned` lists every active pattern read-only, so a suggestion in
Review is never the first time somebody hears that a pattern exists.

## Roadmap

| phase | | status |
|---|---|---|
| **1** | suggest only | **done** |
| 2 | the owner may explicitly promote a suggestion to a rule | future |
| 3 | explicitly trusted rules may auto-fill or auto-approve | future |

Even at phase 3, **filesystem changes still converge at Commit**. More
automation, in exchange for nothing from the safety architecture.

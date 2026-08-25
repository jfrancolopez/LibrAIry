# Relationships between files

**A relationship is evidence about files. It may explain a decision or warn
that a decision will break something. It may never take one.**

This is a permanent safety principle, alongside [decision
memory](decision-memory.md), and it is the same shape: LibrAIry is allowed to
know more than it acts on.

## What a relationship is

Two files that belong together, established once and written down:

| kind | example | established by |
| --- | --- | --- |
| `subtitle` | `Arrival (2016).en.srt` beside `Arrival (2016).mkv` | naming |
| `lyrics` | `05 - Song.lrc` beside `05 - Song.flac` | naming |
| `cue` | `Album.cue` describing a folder's audio | naming |
| `artwork` | `cover.jpg` in a folder whose tracks agree on one album | folder |
| `raw_render` | `IMG_5200.CR3` and `IMG_5200.JPG` | capture metadata |
| `live_photo` | `IMG_1234.HEIC` and `IMG_1234.MOV` | Apple content identifier |

The last two are **never** established from a shared filename stem. A phone
camera folder where `IMG_9323.jpeg` sits beside an unrelated `IMG_9323.MOV` is
the ordinary case, not the exotic one. A stem is the reason to look; it is
never the reason to believe.

Pairs are established for arriving files as well as filed ones, by the same
rules, so a camera card knows its Live Photos **before** you are asked where to
put them.

## What it may do

- **Explain.** `Subtitle for Arrival (2016).mkv` beside a `.srt` in Browse and
  Search, so a file with no obvious reason to exist has one.
- **Warn.** *This will separate a Live Photo. `IMG_1234.MOV` goes to
  Quarantine; `IMG_1234.HEIC` stays in Photos.*
- **Offer.** Where the other half is part of the same decision, a button that
  says `Set aside both`.
- **Invalidate.** If the file a warning was about disappears, changes, or gains
  a new pairing between approval and Commit, the approval is outdated and
  nothing moves.

## What it may never do

- **Add an operation to a plan.** Not a quarantine, not a restore, not a move,
  not a delete-queue request. The only thing that puts a file in a plan is
  somebody pressing a button that names it.
- **Overrule an explicit choice.** Splitting a RAW from its render is a normal
  thing to want — keep the negative, send the render away. Being told is the
  feature; being stopped is not.
- **Transfer itself to different bytes.** Replacing a JPEG that was paired with
  a RAW does not pair the *new* JPEG with that RAW. The pairing described the
  bytes being replaced. The replacement earns it from its own metadata or does
  not have it.
- **Invent a destination.** A relationship is not an answer to "where does this
  go". If one half of a pair is ready to file and the other is not, approving
  the ready ones approves exactly the ready ones.

## Not all kinds mean the same thing

There is no global rule that related files move together, because that claim is
false.

```
subtitle / lyrics / cue   must sit BESIDE the file they describe.
                          Moving one and not the other orphans it even
                          when both stay in the library.

raw_render / live_photo   established from what the bytes record, so the
                          pair survives any reorganisation. Only leaving
                          the library separates them.

artwork                   belongs to a folder's release, not to track
                          five. "Setting aside one MP3 would separate
                          cover.jpg from the MP3" is nonsense, so the
                          only artwork warning is the release leaving
                          entirely.
```

## What is frozen at approval

Only the relationships a plan touches, and only enough of them to ask whether
the decision still means the same thing:

- the pair and its kind
- the state you were shown — together, split, unaffected
- the fingerprint of the half that is **not** in the plan

That half is the one nothing else would ever check, because it is not the
source of any operation — and it is exactly the file the warning was about.

Plans approved before LibrAIry understood relationships carry no snapshot and
keep their old behaviour exactly. They are not retroactively required to have
one, and Commit does not invent a refusal for them.

A relationship *disappearing* is deliberately not treated as drift. If better
metadata says two photographs were never a pair, every operation in the plan is
still the operation that was approved on the file it named — the warning simply
turned out to be unnecessary. Correcting the catalogue must not cancel a
decision about the disk.

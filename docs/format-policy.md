# Format Policy

**Among representations that are valid choices, what does the owner prefer or
prohibit?** That is the whole of the question this answers, and the care is all
in the questions it does *not* answer.

Four horizontal concepts, deliberately separate:

| | answers |
| --- | --- |
| **Identity** | are these the same recording, work or exposure? |
| **[Relationship](relationships.md)** | do these files belong together? |
| **Format Policy** | among valid choices, what do you prefer or forbid? |
| **[Decision memory](decision-memory.md)** | what have you repeatedly chosen before? |

Collapsing any two of them breaks something. A format preference that decided
identity would make a live take and a studio take one recording because they
are both FLAC. A relationship read as a format choice would make a Live Photo's
video a smaller copy of its still. A learned habit read as a policy would turn
"you did this four times" into "you told me to".

## The three things a policy says

```
PREFERRED REPRESENTATION   among copies that ALREADY exist, which one you want.
                           It creates nothing: if the only copy is a FLAC there
                           is no MP3 to prefer and nothing happens.

TRANSFORMATION PERMISSION  may LibrAIry ever *propose* making one. Three states —
                           unset is "you have not said", which is not "no", and
                           is what every category starts as.

PRESERVE ORIGINALS         this folder's originals are not to be traded away by
                           any representation preference or optimization.
```

## What ships configured

Exactly one thing: **Music → MP3**, which is where the existing
`music.preferred_format` setting moved to. Photos, Video and Documents are
neutral, and stay neutral until somebody says otherwise. LibrAIry being *able*
to compare photographs or re-encode video is a capability, not an opinion about
what you want — so there is no "prefer JPEG", no "prefer H.265", and no "delete
RAW".

## Precedence

Per field, most specific first, among the scopes that actually state that field:

```
folder (longest match)  >  category  >  global
```

A scope silent about a field does not overrule a broader one that speaks, which
is what lets `Music → MP3` survive protecting one folder inside it. And in the
wider authority model:

```
safety invariants          no overwrite, no automatic delete, fingerprint
                           verification, containment, relationship-aware approval
explicit Format Policy     what you told LibrAIry you prefer and protect
strong current evidence    catalog identity, ISBN, DOI, capture metadata
learned suggestion         what you have usually done
weak heuristics
```

Policy never overrides safety. A learned habit never overrides policy — a
pattern like *you have kept FLAC four times* is still recorded, still shown,
and is labelled as something the policy has already answered rather than
offered as a competing recommendation.

## Two kinds of protection, told apart

| | what it stops |
| --- | --- |
| **Preserve originals** (Format Policy) | no format preference and no optimization may decide this folder's originals are the dispensable copy |
| **Protected root** (Storage Optimization) | nothing in the folder may be queued for change at all |

The first is the one you want for a RAW wedding or a WAV keepsake. **It is not
a filesystem permission** — LibrAIry can still index, search, organise and file
a protected file, and you may well want that RAW photograph moved into the
right folder. What nothing may do is decide the RAW is dispensable because the
JPEG exists.

Where a decision would trade away a protected original, it is **blocked**
rather than warned about: "are you sure?" over a keepsake is a dialog people
dismiss, and the policy is an instruction you gave.

## Where the file is, and where it is going

Policy resolves against a path. An arriving file has two of them: the one it
occupies now, and the one its proposal points at.

```
resolve(conn, relpath)                    where it is now
after_filing(conn, destination=...)       where it would be after Commit
```

One resolver, one precedence table, one set of scopes — a second reader for
inbox files would be a second set of rules over the same table, free to
disagree the day somebody adds a field. The argument is named for the question
and the answer is marked `prospective`, because confusing the two is the whole
risk here.

An arriving RAW whose proposal says `Photos/Wedding` is **not protected**. It
is on its way somewhere that will protect it, and Review says so:

> After filing, this original will be protected from representation-changing
> workflows, because Photos/Wedding is set to preserve originals.

That is context, not a refusal, and it is shown only where the destination
actually has an opinion — a note on every arrival is noise on the page where
you are choosing a folder. Three consequences follow, and each is pinned by a
test:

* **Filing into a protected folder is still allowed.** Preserve-originals stops
  a representation preference deciding a file is dispensable. It has never
  stopped LibrAIry putting a photograph somewhere better.
* **Protecting a folder does not make an ordinary filing into it stale.** The
  decision still does exactly what it said. Protecting it *does* invalidate a
  waiting decision that would move an original out of the library, because that
  is precisely the trade the folder now forbids.
* **A proposed destination does not protect the arriving bytes.** Choosing to
  keep a filed copy — which sends the arrival to Quarantine — is not blocked by
  a folder the arrival has never been in. The other direction is: an arrival
  may not displace a *filed* protected original, and that is refused before any
  plan exists.

A preferred format and a refused transformation behave the same way. Filing a
lone FLAC into a folder that prefers MP3 is fine — a preference is among
representations that exist, and there is no MP3 to prefer. Policy applies to
the operation being performed, and this operation is putting a file in a
folder.

## Relationships outrank format simplification

A RAW and its JPEG render genuinely are one exposure in two encodings, so
preferring between them is a coherent question. A Live Photo's MOV is not a
smaller HEIC, and a subtitle is not a compact film — so no format preference
applies across those, whatever it says.

## What it never does

- convert, transcode, move, quarantine, delete, approve, or create a plan
- establish that two files are the same thing
- invent a preference for a category nobody has configured

Policy is **input to workflows, not a workflow**. Every filesystem change still
goes through an explicit decision and Commit.

## Impact analysis

`Analyse impact` is a read-only dry run over the index. It writes nothing but
its own cached result and reports three things separately, because adding them
together is how a storage claim becomes a lie:

- **existing representations** — recordings you already have in both formats
- **potential conversions** — where the preferred copy does not exist and would
  have to be made
- **protected** — counted and sized, with no argument attached

The wording follows Storage Optimization's: bytes that *would eventually leave
active representation storage*, never *savings*. LibrAIry does not delete, and
a preference does not either.

It is a snapshot, and says so. If the library's file count has moved since,
the result is labelled as possibly out of date rather than read as execution
truth.

# Audit: catalog contribution and UI consistency

Phase 1 of the catalog-intelligence and UX-consistency work. Written before any
behaviour changed, against the running instance and the author's real database
(243 inbox items, 145 committed moves, 177 journal rows).

The rule from the last audit still applies: **a function existing does not mean
the feature works.** Every claim below was checked by following the call path or
by reading what the live database actually contains.

## Part A — catalogs

### Every provider, and where it lands

| Provider | Applies to | Returns | Affects class. | Affects dest. | Affects name | Artwork | In alternatives | Persisted | Reusable without re-lookup |
|---|---|---|---|---|---|---|---|---|---|
| ffprobe tags | audio | artist/album/title/year/genre/track | ✅ 0.86–0.90 | ✅ | ✅ | embedded only | ✅ | evidence | ✅ evidence row |
| AcoustID | audio, untagged only | recording MBID + score | ✅ 0.80 | via MB | via MB | ❌ | ✅ | evidence | ✅ MBID kept |
| MusicBrainz | audio | artist/album/title/year/track, release MBID | ✅ 0.90 | ✅ | ✅ | via release MBID | ✅ | evidence | ✅ release_id kept |
| Discogs | audio, filename fallback | artist/album/year/genre | ✅ 0.80 | ✅ | ✅ | exposed, unused | ✅ | evidence | ✅ |
| Last.fm | audio, genre gap only | genre | genre only | ✅ (genre is a path part) | ❌ | ❌ | ✅ | evidence | ✅ |
| TMDB | video | title/year/genre, TMDB id | ✅ | ✅ | ✅ | exposed, **unused** | ✅ | evidence | ✅ id kept |
| TVmaze | video (series) | show/season/episode | ✅ | ✅ | ✅ | exposed, **unused** | ✅ | evidence | ✅ |
| Open Library | documents | title/author/year | ✅ | ✅ | ✅ | exposed, unused | ✅ | evidence | ✅ |
| Cover Art Archive | audio | 250px front cover | ❌ | ❌ | ❌ | ✅ the whole point | ❌ | thumb cache only | ✅ cached |
| Vision (local) | images | caption/tags/subjects/OCR | ✅ | ✅ | ✅ | n/a | ✅ | `vision_results` | ✅ |
| Library patterns | music/movies/shows | existing dest_base | ❌ | ✅ | ❌ | n/a | ❌ | `library_patterns` | ✅ |

**No provider is unwired from the classifier except one.** The previous audit's
failure mode has not recurred: all eight catalogs reach `classify_item` through
`_*_lookup` factories gated on `catalog_enabled`, and each contributes an
`EvidenceEntry` that Review's Why panel already renders.

### Gap A1 — Cover Art Archive never touches organization

`tools/coverart.py` is reachable from exactly two places: `catalog_probe`
(the Settings Test button) and `web/thumbs.py` (album art on a preview card).
Its own module docstring states the intent: *"Art is never written into the
library. v1 renames and moves; it does not add files to your collection."*

That was a deliberate v1 boundary, not an oversight. Extending it is Part A's
artwork request, and it must go through the normal proposal → approve → commit
path to stay inside the safety guarantees.

### Gap A2 — artwork in the inbox is filed as a personal photo

This is the concrete, reproducible bug. From the live database:

```
Alicia Keys - Unplugged (20th Anniversary) R&BSoul (2025) 320_kbps/cover.jpg
  -> photos  Photos/2025/Alicia-Keys-Unplugged-(20th-Anniversary)-R-and-BSoul-.../
             cover-Alicia-Keys-Unplugged-(20th-Anniversary)-R-and-BSoul-....jpg   0.90

V.A. - Best Road Trip Disco Fever Classics (2023 Pop) [Flac 16-44]/Cover.jpg
  -> photos  Photos/2023/V.A.-Best-Road-Trip-Disco-Fever-Classics-.../Cover-....jpg  0.90

Cracking the Coding Interview 189 Programming Questions and Solutions/cover.jpg
  -> misc    (no destination)                                                     0.40
```

A file literally named `cover.jpg`, sitting inside a folder of FLAC tracks that
LibrAIry has already identified as an album, is proposed as a photograph — with
the whole release name glued onto it by the photo namer, at 0.90 confidence.
The album's own tracks are meanwhile filed correctly under
`Music/R&BSoul/Alicia Keys/Unplugged (20th Anniversary)/`.

### The trap: proximity is not evidence

The obvious fix — "an image next to media is artwork" — is wrong on this exact
library, and the data says so. Eight inbox folders hold an image beside a video.
**Seven of them are phone camera folders**, where a `.jpeg` sits next to an
unrelated `.MOV`:

```
00/CB75D2F5-.../IMG_9323.jpeg  -> Photos/Unknown/Unsorted/IMG_9323-airport-terminal-travel.jpeg
00/A34735BF-.../IMG_6172.jpeg  -> Photos/Unknown/Unsorted/IMG_6172-baby-sleeping-pacifier.jpeg
```

Those are family photographs and they are currently filed correctly. A
proximity rule would turn all seven into movie posters. So the discriminator
has to be the **filename**, not the neighbourhood:

- the stem is a conventional artwork name (`cover`, `folder`, `poster`,
  `fanart`, `front`, `albumart`, `season NN`) — `IMG_9323` is not, and never
  becomes one;
- **and** the directory's other files were classified into one owning group
  (album / movie / season) with a destination.

Both conditions, deterministic, explainable in one line in the Why panel.

### Gap A3 — library-pattern reuse: verified working for music and movies

Traced against the real pattern map (30 patterns learned from the genre-first
`Music/Pop/...` library):

- **Music** — library has `Music/Pop/Abba/Arrival/`; catalog says genre Disco;
  template renders `Music/Disco/ABBA/Arrival/x.flac`; `apply_library_pattern`
  normalizes `ABBA`→`abba`, finds `Music/Pop/Abba`, rebases to
  `Music/Pop/Abba/Arrival/x.flac`. **Correct**, album boundary preserved.
- **Movies** — keyed on `The Matrix (1999)`, matches both the flat
  `Movies/The Matrix (1999)/` and genre-first `Movies/General/The Matrix (1999)/`
  layouts. **Correct.**
- **Shows** — `_SEASON` skips `Season 01` so a season folder is never registered
  as a show name. **Correct.**

One real limit: `TOP_LEVEL_KINDS` recognises `Music`, `Shows`, `Movies` — the
names LibrAIry's own templates produce. A library whose top level is `TV/`
rather than `Shows/` teaches nothing. Worth noting, not worth guessing at.

### Gap A4 — evidence ordering is right, but implicit

`CASCADE_EVIDENCE_SOURCES` puts catalog above vision above ai, and
`apply_ai_if_needed` returns early once confidence clears the threshold, so a
TMDB id does outrank an LLM guess today. This is enforced by ordering rather
than by a rule that would fail loudly if reordered.

### Priority

| # | Change | Value | Risk | Status |
|---|---|---|---|---|
| 1 | Recognise artwork in the destination; never propose a duplicate | high | low | **done** |
| 2 | Associate canonically-named incoming artwork with its media group | high | low | **done** |
| 3 | Propose artwork into an already-filed media folder | medium | medium | **done**, via the journal |
| 4 | Download provider artwork | low | high | **deferred** |

3 turned out to be the case that actually matters here, not 2: on this library
the album had already been committed, so there was no inbox sibling to anchor
to and the only surviving link was the journal's record of where the tracks
went. 4 stays deferred deliberately — the first three cover the usefulness
without turning LibrAIry into a daemon that fetches images at the NAS.

## Provider artwork — audited, and deliberately not built

Priority 4. The question was whether the path is clean enough to implement the
smallest safe version. It is not, for two separate reasons, both concrete.

### Which catalogs expose artwork, for what

| Catalog | Media | Artwork exposed | Sizes |
|---|---|---|---|
| Cover Art Archive | music | front cover, by release MBID | `front-250`, `front-500`, `front-1200` |
| TMDB | movies, TV | `poster_path` in every search result | any `w92…w780`, `original` |
| TVmaze | TV | `image.medium` / `image.original` | two fixed |
| Discogs | music | `cover_image`, `thumb` | two fixed |
| Open Library | books | cover by ISBN/OLID | S / M / L |

So artwork is plentiful. That is not the blocker.

### Blocker 1 — no stable identity is persisted

Artwork needs an ID, and LibrAIry keeps titles.

- `classify_video` records `EvidenceEntry("tmdb", "title", title)`. The TMDB
  **id** and `poster_path` are in the response and are dropped. `tmdb._CACHE`
  is an in-memory dict, gone at restart.
- `classify_music` records a MusicBrainz `release_id` — but only on the
  AcoustID branch, which runs *only for files with no usable tags*. A normally
  tagged album, the common case at 0.90, never does a MusicBrainz lookup and
  so has no MBID.
- Measured on the live database: **15 catalog evidence entries, 0 of which
  carry an id.**

Proposing artwork today would therefore mean a fresh catalog query per album or
film, at proposal time, keyed on a title — which is exactly the weak identity
the rest of this work has been removing. "Do not download until there is a
high-confidence media match" cannot be honoured when the match is a string.

**Prerequisite:** persist the catalog id as evidence (`tmdb/id`,
`musicbrainz/release_id` on the tagged path too). Additive, cheap, and worth
doing on its own for explainability — Review could then say *"TMDB 603"*
instead of *"matched An American Carol"*.

### Blocker 2 — a download is not an item

Every guarantee in LibrAIry hangs off one shape: a proposal moves an **existing
inbox item** to a validated destination, hash-verified, journalled, undoable. A
poster fetched from TMDB is not an inbox item, so it cannot be proposed. The
two ways out:

1. **Download into the inbox** and let it be scanned, proposed, reviewed and
   committed like any other file. Changes no semantics at all — the artwork
   association written in this pass would then pick it up for free. It does
   mean LibrAIry writing into the user's inbox, which it has never done.
2. **A new op type that creates a file at commit.** This touches the immutable
   plan and the undo model, which is precisely what must not be touched.

(1) is the right shape when this is built. It is still a real decision — the
inbox stops being purely the owner's — and it is the owner's to make.

### Verdict

Stop. Two prerequisites, neither of them artwork work: persist catalog ids,
then decide whether LibrAIry may write into the inbox. The first three artwork
priorities already cover the useful cases without either.

## Part B — UI

Review is the baseline: compact rows, one line of identity, detail behind
`<details>`, actions next to what they act on, colour that means confidence.

| Screen | Verdict |
|---|---|
| Review | baseline |
| Browse | aligned — shares preview card and viewer |
| Commit | aligned — plan table is genuinely tabular |
| Dashboard | acceptable; metric tiles read well |
| Settings | long but grouped; a configuration page, not a feed |
| Health | acceptable |
| **Quarantine** | **weakest.** Three stacked `metric wide` sections; "Already moved out" is a raw `<table>` that cannot reflow on a phone; no preview, no Why on moved-out rows; every action always visible |
| **History** | **needs an information model.** Grouped by plan, but no date grouping, no event-type filter, hard `LIMIT 50` with no pager, and the count line compares moves against all 177 rows including settings changes |

### What the journal can actually distinguish

The user's suggested filters must be checked against the data before they are
built. The live journal has exactly three action values:

```
move            ok    dest_root=library     145
undo_move       ok    dest_root=inbox         5
settings_change ...   dest_root=inbox        27
```

There is no approval, re-analysis or error event in the journal — approvals are
proposal state transitions and are never journalled. So the honest filter set,
each derivable from `(action, dest_root, outcome)` with **no schema change**, is:

| Filter | Predicate | Live count |
|---|---|---|
| Filed | `action='move' AND dest_root='library'` | 145 |
| Quarantined | `action='move' AND dest_root='quarantine'` | 0 |
| Undone | `action='undo_move'` | 5 |
| Settings | `action='settings_change'` | 27 |
| Failed | `outcome != 'ok'` | 0 |

"Approvals", "Analysis" and "Errors" as separate categories would be fabricated.
Grouping stays presentation-only: date headings and plan summaries computed at
render time, individual journal rows untouched.

### Priority

| # | Change | Value | Risk |
|---|---|---|---|
| 1 | Quarantine: rows with hierarchy, Why, previews; table → reflowable rows | high | low |
| 2 | History: date grouping + honest event filters, Find integrated | high | low |
| 3 | History: pager past the first 50 | medium | low |
| 4 | Shared pattern extraction, only where already repeated | low | low |

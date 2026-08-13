"""Is this collection a real release, or a folder somebody made once?

A multi-artist folder is the one case where "tidy this up" has two opposite
right answers, and picking the wrong one destroys something. *Now That's What
I Call Music 42* is a release: it has a catalogue number, a cover, a running
order, and splitting it into forty artist folders is vandalism. `Stuff for the
car` is not a release: it is a folder somebody filled once, and keeping it
whole means forty artists are missing from where they belong.

Nothing about the *filesystem* tells you which one you are looking at. Both
shapes are a folder with tracks by many people in it. So this module refuses
to answer from structure and asks three questions in order of how much they
prove:

1. **Does a catalog know this release?** MusicBrainz or Discogs returning a
   release id is external, checkable, and beats everything below it.
2. **Do the files agree with each other?** Forty-five tracks that all name the
   same album, all name the same album artist, number themselves 1 to 45 with
   no gaps and no repeats, all carry the same barcode and the same cover —
   that is a release description, even if no catalog has heard of it. It is
   weaker than a catalog hit because it is self-reported, and one bad batch
   tag would write it. So contradictions veto it outright.
3. **Has the owner already decided?** A previous `No change` on this exact
   question is the strongest evidence of all, because it is the only kind
   that knows what the folder is *for*. That lives in the existing audit
   resolution mechanism; this module only has to not overrule it.

Three verdicts, and the third is the only one that suggests taking anything
apart:

* `RECOGNIZED` — a catalog names it. Keep it together, in one folder.
* `CUSTOM` — the files describe one coherent release, no catalog agrees.
  Keep it together *or* organise the tracks individually; this is a judgment
  call and Review says so rather than picking.
* `LOOSE` — no release identity worth the name. Organise the tracks by their
  own artist and album.

**The rule this module exists to enforce**, and the one worth stating on its
own: an unrecognised collection name must never be inherited into every
artist's hierarchy. `Artist A/Stuff for the car/`, `Artist B/Stuff for the
car/`, twenty-seven times over, is the worst of both structures — the album is
not together, and every artist folder now claims an album that does not exist.
Either the collection is real and stays in one folder, or it is not and the
tracks go to their own albums. `dissolve_to` therefore refuses to emit any
destination containing the collection title, and there is a test that holds it
to that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from librairy.audit_music import COMPILATION_ARTISTS, key
from librairy.models import EvidenceEntry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from librairy.audit import LibraryView
    from librairy.audit_catalog import Identity
    from librairy.audit_music import Album

RECOGNIZED = "recognized"
CUSTOM = "custom"
LOOSE = "loose"

# What people name the folder they keep compilations in. Used to *read* an
# existing convention, never to impose one — if the library already has a
# `Compilations/` folder, a suggestion that says `Various Artists/` is asking
# the owner to keep two.
COMPILATION_HOMES = ("various artists", "various", "compilations", "va", "v.a.")
DEFAULT_HOME = "Various Artists"

# How many corroborating signals make self-reported tags into a release
# identity. Three is the point where a single mistyped field stops being
# enough on its own: a complete track run plus a matching total plus a shared
# barcode is not something a careless tagger produces by accident.
STRONG_SIGNALS = 3

# How many per-track destinations a finding carries. Enough to show the shape
# of a real collection in full; bounded because the evidence blob is read on
# every render of the row, and a thousand-track folder should not make Review
# slower for everyone else.
MAX_PREVIEW_MOVES = 200


@dataclass(frozen=True)
class Verdict:
    """What this collection is, why, and what to do about it."""

    kind: str
    title: str
    tracks: int
    artists: int
    total_bytes: int
    folders: tuple[str, ...]
    signals: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    agreements: tuple[Fact, ...] = ()
    conflicts: tuple[Fact, ...] = ()
    catalogs: tuple[Identity, ...] = ()
    home: str | None = None
    dissolve_to: tuple[tuple[str, str], ...] = ()
    disagreement: str = ""

    @property
    def keeps_together(self) -> bool:
        return self.kind != LOOSE

    @property
    def label(self) -> str:
        return {
            RECOGNIZED: "Recognized compilation",
            CUSTOM: "Custom compilation",
            LOOSE: "Loose collection",
        }[self.kind]


@dataclass
class Facts:
    """Everything the member tracks say about themselves, gathered once."""

    tracks: list[str] = field(default_factory=list)
    albums: set[str] = field(default_factory=set)
    album_artists: set[str] = field(default_factory=set)
    artists: set[str] = field(default_factory=set)
    numbers: list[int] = field(default_factory=list)
    totals: set[int] = field(default_factory=set)
    barcodes: set[str] = field(default_factory=set)
    years: set[str] = field(default_factory=set)
    compilation_marked: int = 0
    with_art: int = 0
    total_bytes: int = 0
    by_artist: dict[str, list[str]] = field(default_factory=dict)


def gather_facts(view: LibraryView, members: list[Album]) -> Facts:
    """Read the tags of every track in the collection. No disk access here —
    `audit.gather` already probed these files once and this reads its notes."""
    facts = Facts()
    for album in members:
        for relpath in album.tracks:
            tags = view.tags.get(relpath) or {}
            facts.tracks.append(relpath)
            if album_tag := (tags.get("album") or "").strip():
                facts.albums.add(album_tag)
            if album_artist := (tags.get("album_artist") or "").strip():
                facts.album_artists.add(album_artist)
            artist = (tags.get("artist") or "").strip()
            if artist:
                facts.artists.add(artist)
            facts.by_artist.setdefault(artist or "Unknown artist", []).append(relpath)
            if (number := _first_int(tags.get("track"))) is not None:
                facts.numbers.append(number)
            if (total := _first_int(tags.get("tracktotal"))) is not None:
                facts.totals.add(total)
            for tag in ("upc", "barcode"):
                if value := (tags.get(tag) or "").strip():
                    facts.barcodes.add(value)
            if year := (tags.get("date") or tags.get("year") or "").strip()[:4]:
                facts.years.add(year)
            if _marks_compilation(tags):
                facts.compilation_marked += 1
            if view.artwork.get(relpath):
                facts.with_art += 1
            row = view.indexed.get(relpath)
            if row is not None and row["size"]:
                facts.total_bytes += int(row["size"])
    return facts


def _marks_compilation(tags: dict[str, str]) -> bool:
    """An explicit "this is a compilation" flag, however the tagger spelled it."""
    if (tags.get("mediatype") or "").strip().casefold() == "compilation":
        return True
    if (tags.get("compilation") or "").strip() in {"1", "true", "yes"}:
        return True
    return any(key(name) in COMPILATION_ARTISTS for name in (tags.get("album_artist"),) if name)


def _first_int(value: str | None) -> int | None:
    """`31`, `31/45` and `1.31` all mean 31."""
    if not value:
        return None
    digits = ""
    for char in str(value).strip():
        if char.isdigit():
            digits += char
        elif digits:
            break
        else:
            return None
    return int(digits) if digits else None


@dataclass(frozen=True)
class Fact:
    """One thing the files say, and how many of them said it.

    The count is the point. "Album: Best Road Trip Disco Fever Classics" is an
    assertion; "45 of 45 tracks agree" is the reason to believe it, and it is
    also how a reader spots the case where forty-four agree and one does not.
    """

    label: str
    value: str
    agree: int = 0
    total: int = 0
    conflict: bool = False

    @property
    def note(self) -> str:
        if not self.total:
            return ""
        noun = "track" if self.total == 1 else "tracks"
        if self.conflict:
            return f"{self.agree} of {self.total} {noun} disagree"
        return f"{self.agree} of {self.total} {noun} agree"


def read_facts(facts: Facts) -> tuple[list[Fact], list[Fact]]:
    """The same evidence as `read_signals`, as labelled rows for the UI.

    Kept beside the sentence version rather than replacing it: Why reads
    better as prose, and a details table reads better as rows. Both are
    derived from one gather so they cannot disagree with each other.
    """
    count = len(facts.tracks)
    agree: list[Fact] = []
    conflict: list[Fact] = []

    if len(facts.albums) == 1:
        agree.append(Fact("Album", next(iter(facts.albums)), count, count))
    elif facts.albums:
        conflict.append(
            Fact("Album", " / ".join(sorted(facts.albums)), len(facts.albums), count, True)
        )

    if len(facts.album_artists) == 1:
        agree.append(Fact("Album artist", next(iter(facts.album_artists)), count, count))
    elif facts.album_artists:
        conflict.append(
            Fact(
                "Album artist",
                " / ".join(sorted(facts.album_artists)),
                len(facts.album_artists),
                count,
                True,
            )
        )

    numbers = sorted(facts.numbers)
    repeated = sorted({n for n in numbers if numbers.count(n) > 1})
    if repeated:
        conflict.append(
            Fact(
                "Track sequence",
                f"{', '.join(str(n) for n in repeated[:4])} used more than once",
                0,
                count,
                True,
            )
        )
    elif numbers and numbers == list(range(1, len(numbers) + 1)) and len(numbers) == count:
        agree.append(
            Fact("Track sequence", f"1-{count}, complete with no gaps", count, count)
        )
    elif numbers:
        agree.append(Fact("Track sequence", "incomplete", len(numbers), count))

    if len(facts.totals) == 1:
        total = next(iter(facts.totals))
        if total == count:
            agree.append(Fact("Track total", str(total), count, count))
        else:
            conflict.append(
                Fact("Track total", f"tags say {total}, {count} are here", 0, count, True)
            )
    elif len(facts.totals) > 1:
        conflict.append(
            Fact(
                "Track total",
                " / ".join(str(total) for total in sorted(facts.totals)),
                len(facts.totals),
                count,
                True,
            )
        )

    if len(facts.barcodes) == 1:
        agree.append(Fact("Barcode", next(iter(facts.barcodes)), count, count))
    elif facts.barcodes:
        conflict.append(Fact("Barcode", f"{len(facts.barcodes)} different", 0, count, True))

    if len(facts.years) == 1:
        agree.append(Fact("Year", next(iter(facts.years)), count, count))
    if facts.compilation_marked == count and count:
        agree.append(Fact("Media type", "Compilation", count, count))
    if facts.with_art and count:
        agree.append(
            Fact("Embedded artwork", "Front cover in the tracks", facts.with_art, count)
        )
    return agree, conflict


def read_signals(facts: Facts) -> tuple[list[str], list[str]]:
    """The corroborations and the contradictions, in words a person can check.

    Both lists go into the finding's evidence, because "we decided this is a
    real release" is not a claim anyone can argue with, and "tracks 1-45, none
    missing, none repeated" is.
    """
    signals: list[str] = []
    contradictions: list[str] = []
    count = len(facts.tracks)

    numbers = sorted(facts.numbers)
    repeated = sorted({n for n in numbers if numbers.count(n) > 1})
    if repeated:
        contradictions.append(
            f"track number{'s' if len(repeated) > 1 else ''} "
            f"{', '.join(str(n) for n in repeated[:4])} used more than once"
        )
    elif numbers and numbers == list(range(1, len(numbers) + 1)) and len(numbers) == count:
        signals.append(f"tracks 1-{count} complete, none missing and none repeated")

    if len(facts.totals) > 1:
        contradictions.append(
            "the tracks disagree about how many tracks the release has "
            f"({', '.join(str(total) for total in sorted(facts.totals))})"
        )
    elif facts.totals and next(iter(facts.totals)) == count:
        signals.append(f"every track says the release has {count} tracks, and {count} are here")
    elif facts.totals:
        contradictions.append(
            f"the tags say {next(iter(facts.totals))} tracks but {count} are here"
        )

    if len(facts.barcodes) > 1:
        contradictions.append("more than one barcode across the tracks")
    elif facts.barcodes:
        signals.append(f"one barcode on every track: {next(iter(facts.barcodes))}")

    if len(facts.album_artists) == 1:
        signals.append(f"one album artist on every track: {next(iter(facts.album_artists))}")
    elif len(facts.album_artists) > 1:
        contradictions.append(
            f"{len(facts.album_artists)} different album artists across the tracks"
        )

    if facts.compilation_marked == count and count:
        signals.append("every track is tagged as part of a compilation")

    if facts.with_art == count and count:
        signals.append("the same cover is embedded in every track")

    if len(facts.years) == 1:
        signals.append(f"one release year on every track: {next(iter(facts.years))}")

    return signals, contradictions


def classify_collection(
    view: LibraryView,
    members: list[Album],
    *,
    catalogs: tuple[Identity, ...] = (),
    convention: str = "",
) -> Verdict:
    """Recognized, custom, or loose — and the evidence for saying so."""
    facts = gather_facts(view, members)
    signals, contradictions = read_signals(facts)
    agreements, conflicts = read_facts(facts)
    title = next(iter(facts.albums)) if len(facts.albums) == 1 else members[0].album_tag
    folders = tuple(sorted(album.folder for album in members))
    matched = tuple(identity for identity in catalogs if identity.matched)

    if matched:
        kind = RECOGNIZED
    elif len(signals) >= STRONG_SIGNALS and not contradictions:
        kind = CUSTOM
    else:
        kind = LOOSE

    branch = _shared_branch(members)
    home = _home_for(branch, title, matched, convention) if kind != LOOSE else None
    dissolve = _dissolve_plan(view, facts, branch, title) if kind == LOOSE else ()

    return Verdict(
        kind=kind,
        title=title,
        tracks=len(facts.tracks),
        artists=len(facts.artists),
        total_bytes=facts.total_bytes,
        folders=folders,
        signals=tuple(signals),
        contradictions=tuple(contradictions),
        agreements=tuple(agreements),
        conflicts=tuple(conflicts),
        catalogs=tuple(catalogs),
        home=home,
        dissolve_to=dissolve,
        disagreement=_disagreement(matched),
    )


def _disagreement(matched: tuple[Identity, ...]) -> str:
    """Two catalogs, two different titles. Say so rather than picking one.

    Consensus is worth more than either witness alone, so a disagreement has
    to be visible — otherwise "MusicBrainz and Discogs both call this X" and
    "one of them does, the other says Y" read identically in Review.
    """
    titles = {identity.canonical_title for identity in matched if identity.canonical_title}
    if len(titles) < 2:
        return ""
    named = ", ".join(
        f"{identity.provider} calls it {identity.canonical_title!r}"
        for identity in matched
        if identity.canonical_title
    )
    return f"The catalogs do not agree: {named}."


def _shared_branch(members: list[Album]) -> str:
    """`Music/Pop`, but only when every member is already under it.

    A collection straddling `Music/Pop` and `Music/Soul` has no obvious home,
    and guessing one would move music between the genre folders the owner
    chose. Better to describe the problem and suggest nothing.
    """
    branches = {album.branch for album in members}
    return next(iter(branches)) if len(branches) == 1 and all(branches) else ""


def library_convention(view: LibraryView) -> str:
    """The name this library already uses for compilations, if it uses one.

    Read from the folders that exist rather than assumed, so a library that
    files these under `Compilations/` is not told to start a second folder
    called `Various Artists/`.
    """
    for relpath in view.files:
        for part in PurePosixPath(relpath).parts[:-1]:
            if part.casefold() in COMPILATION_HOMES:
                return part
    return ""


def _home_for(
    branch: str, title: str, matched: tuple[Identity, ...], convention: str
) -> str | None:
    """One folder for the whole release.

    Falls back to `Various Artists` when the library has no convention yet,
    which is a real choice and worth defending: the alternative was to suggest
    nothing, and a finding that says "this is one album in twenty-seven
    folders" without saying where it should go leaves the owner to invent the
    answer that the audit already knows.
    """
    if not branch or not title:
        return None
    artist = convention or DEFAULT_HOME
    for identity in matched:
        if identity.canonical_artist:
            artist = identity.canonical_artist
            break
    canonical_title = next(
        (identity.canonical_title for identity in matched if identity.canonical_title), title
    )
    return f"{branch}/{artist}/{canonical_title}"


def _dissolve_plan(
    view: LibraryView, facts: Facts, branch: str, title: str
) -> tuple[tuple[str, str], ...]:
    """Where each track goes once the collection stops being an album.

    The album folder comes from the track's *own* release, never from the
    collection — that is the whole point. When nothing identifies the track's
    real album, the track sits directly under its artist rather than under an
    invented folder. An empty album component is honest; repeating the
    collection name would not be.
    """
    if not branch:
        return ()
    plan: list[tuple[str, str]] = []
    for artist, tracks in sorted(facts.by_artist.items()):
        for relpath in sorted(tracks):
            name = PurePosixPath(relpath).name
            album = _own_album(view, relpath, title)
            parts = [branch, artist, album, name] if album else [branch, artist, name]
            plan.append((relpath, "/".join(parts)))
    return tuple(plan)


def _own_album(view: LibraryView, relpath: str, collection_title: str) -> str:
    """The track's real album, or nothing.

    The `album` tag on a track inside a collection *is* the collection name —
    that is how it got there — so it is exactly the value that must not be
    used. Anything else the tags offer is the track's own release.
    """
    tags = view.tags.get(relpath) or {}
    for candidate in (tags.get("originalalbum"), tags.get("album")):
        value = (candidate or "").strip()
        if value and key(value) != key(collection_title):
            return value
    return ""


def evidence_for(verdict: Verdict) -> list[EvidenceEntry]:
    """The verdict, written out so Review can show the reasoning."""
    entries = [
        EvidenceEntry("library-pattern", "collection", verdict.label, 0.95),
        EvidenceEntry("tags", "album", verdict.title, 0.95),
        EvidenceEntry("filesystem", "tracks", str(verdict.tracks), 0.9),
        EvidenceEntry("filesystem", "artists", str(verdict.artists), 0.9),
    ]
    if verdict.total_bytes:
        # Measured here, once, rather than recounted on every page view. A
        # Review row that walked twenty-seven directories to render would get
        # slower the larger the library grew, which is backwards.
        entries.append(
            EvidenceEntry("filesystem", "total bytes", str(verdict.total_bytes), 0.9)
        )
    for identity in verdict.catalogs:
        entries.append(
            EvidenceEntry(
                identity.provider,
                "release",
                identity.canonical_title or "No matching release found",
                0.95 if identity.matched else 0.4,
                note="Searched by barcode and exact title",
                status="matched" if identity.matched else "no-match",
            )
        )
    # Every fact twice, on purpose: as a sentence for Why, which reads as
    # prose, and as a labelled row for the details table, which reads as a
    # table. Both come from one gather, so they cannot drift apart.
    entries.extend(
        EvidenceEntry("tags", "agreement", signal, 0.85) for signal in verdict.signals
    )
    entries.extend(
        EvidenceEntry("tags", "disagreement", problem, 0.85)
        for problem in verdict.contradictions
    )
    for fact in (*verdict.agreements, *verdict.conflicts):
        entries.append(
            EvidenceEntry(
                "tags",
                f"fact:{fact.label}",
                fact.value,
                0.5 if fact.conflict else 0.9,
                note=fact.note,
                status="conflict" if fact.conflict else "agree",
            )
        )
    if verdict.disagreement:
        entries.append(EvidenceEntry("catalog", "conflict", verdict.disagreement, 0.7))
    entries.extend(
        EvidenceEntry("filesystem", "folder", folder, 0.9) for folder in verdict.folders
    )
    # Where each track would go if the collection were taken apart. Carried as
    # evidence so Review can show the consequence *before* anyone chooses it —
    # "organise individually" is an abstraction until you can see that Chic's
    # track lands under `Music/Disco/Chic/`. Source in the detail, destination
    # in the note, because a path can contain any separator you might pick.
    entries.extend(
        EvidenceEntry("filesystem", "move", source, 0.8, note=destination)
        for source, destination in verdict.dissolve_to[:MAX_PREVIEW_MOVES]
    )
    return entries


def summarize(verdict: Verdict) -> str:
    """The one sentence at the top of the Review row."""
    where = f"{verdict.tracks} tracks by {verdict.artists} artists"
    if verdict.kind == RECOGNIZED:
        names = " and ".join(
            sorted({identity.provider for identity in verdict.catalogs if identity.matched})
        )
        return (
            f"{verdict.title!r} is a recognised compilation — {where}, "
            f"and {names} identifies it as one release. It is currently spread "
            f"across {len(verdict.folders)} artist folders."
        )
    if verdict.kind == CUSTOM:
        return (
            f"{verdict.title!r} looks like one compilation — {where} that agree "
            f"with each other — but no configured catalog recognises the release. "
            f"It is currently spread across {len(verdict.folders)} artist folders."
        )
    return (
        f"{verdict.title!r} is {where} with no reliable release identity, "
        f"spread across {len(verdict.folders)} folders. The tracks would be "
        f"better organised under their own artists and albums."
    )

"""Library Audit: what is already filed, and whether it should stay that way.

Inbox Review asks *where should this new file go?* This asks a different
question — *this file is already in my library; is anything about it wrong?* —
and the two must never be confused, because a correction to a file you already
own is a bigger deal than filing one you just dropped in.

Three rules hold the whole thing together:

* **Analysis never writes to the library.** An audit reads the filesystem, the
  index and embedded tags. It produces rows in `audit_findings` and nothing
  else. No rename, no move, no delete, not even for a finding it is certain of.
* **Silence is the goal.** A healthy file produces no row. An audit that
  reports eight hundred harmless style differences is worse than no audit, so
  every detector here has to justify itself against a real library that is
  mostly fine.
* **Your layout is evidence, not a mistake.** `Music/Pop/Abba/` is an
  established convention even if a catalog would call Abba disco. The question
  is never "does this match a taxonomy" but "is this file out of step with the
  way *you* organise things".

It walks with `scanner.visible_files`, the same predicate Browse uses, so the
two can never disagree about what exists.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from librairy.classify.companions import SIDECAR_KINDS
from librairy.config import Settings
from librairy.models import EvidenceEntry
from librairy.planner import utc_now
from librairy.scanner import visible_files

LOGGER = logging.getLogger(__name__)

# What a finding is about. Only kinds with real detection logic behind them —
# there is no value in a category that exists to make the page look busy.
KINDS = {
    "unexpected-file-type": "Unexpected file type",
    "loose-file": "Loose file",
    # Two naming kinds, split by what can safely be executed rather than by
    # what the problem is. Renaming a file is one move the existing plan
    # already represents; renaming a folder is every file beneath it, which is
    # a different proof. See EXECUTABLE_KINDS.
    "naming-cleanup": "Naming cleanup",
    "naming-inconsistency": "Naming inconsistency",
    "tag-path-mismatch": "Tags disagree with the folder",
    "duplicate": "Possible duplicate",
    "missing-artwork": "Missing artwork",
    "unindexed": "Not indexed",
    "system-junk": "System file",
    # Music reconciliation. Each of these is about a folder or a set of them,
    # which is why none appear in EXECUTABLE_KINDS. See `audit_music`.
    "split-album": "One album in several folders",
    # Three verdicts on a multi-artist folder, and they are separate kinds
    # rather than one kind with a field because they ask for different
    # decisions: keep it, choose, or take it apart. See `audit_compilation`.
    "collection-recognized": "Recognized compilation",
    "collection-custom": "Custom compilation",
    "collection-loose": "Loose collection",
    "artist-split": "Artist filed in two places",
    "album-name-mismatch": "Folder name disagrees with the tags",
    "track-numbering": "Tracks missing from an album",
    "naming-outlier": "Named unlike its neighbours",
    "loose-tracks": "Loose tracks beside album folders",
    # Tier 2. A catalog only ever gets a say when the embedded tags already
    # agree with it against the folder — see `audit_catalog`.
    "catalog-name-mismatch": "A catalog spells this differently",
    # Not the same claim as "missing artwork": the album has a picture, it is
    # just inside the files rather than beside them, which matters to some
    # players and not others.
    "artwork-not-on-disk": "Artwork is embedded but not on disk",
}

# Kinds whose correction is a concrete, deterministic filesystem move that
# someone has reasoned about end to end. Everything else is an observation:
# true, worth showing, and with no move that answers it.
#
# The test is deliberately the *kind* and not "has a dest_relpath". A detector
# added later could set a destination without anyone having thought about what
# executing it would mean, and the allowlist is where that thinking is
# recorded. Three kinds were considered and left out on purpose:
#
# * `naming-inconsistency` is always about a *folder*. Renaming one is not one
#   move but every file beneath it, and the existing correction group resolves
#   a primary plus its companions in one directory — not a subtree. Until that
#   is built and proven, a folder rename stays a suggestion. This is why
#   `JAMES BROWN` is shown with its corrected spelling and no button.
# * `duplicate` has a correct answer — quarantine the copy — but that is a
#   different action class with its own safety semantics, not a move.
# * `missing-artwork`, `unindexed` and `system-junk` describe files that are
#   exactly where they belong.
EXECUTABLE_KINDS = frozenset({"tag-path-mismatch", "naming-cleanup"})

# Kinds whose `relpath` names a folder rather than a file. The UI needs this to
# decide three things it would otherwise get wrong: whether to offer the
# extension-info badge, whether Preview means anything, and whether the title
# is a filename. It lived in the web layer as a hand-kept pair, and the first
# six kinds added after it were all folders and all rendered as files.
#
# Declared beside KINDS so a new kind has to say which it is, and asserted
# exhaustive by `test_audit_music`.
FOLDER_KINDS = frozenset(
    {
        "missing-artwork",
        "naming-inconsistency",
        "split-album",
        "collection-recognized",
        "collection-custom",
        "collection-loose",
        "artist-split",
        "album-name-mismatch",
        "track-numbering",
        "loose-tracks",
        "catalog-name-mismatch",
        "artwork-not-on-disk",
    }
)

# "high" is worth acting on; "review" needs your judgement. Deliberately two
# bands and not five — a third would only invite arguing about the boundary.
SEVERITIES = ("high", "review")

AUDIO = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma", ".aiff", ".alac"}
VIDEO = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".mpg", ".mpeg", ".ts", ".webm"}
IMAGE = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".tiff", ".webp", ".bmp", ".dng", ".raw"}
DOCUMENT = {".pdf", ".epub", ".mobi", ".doc", ".docx", ".txt", ".rtf", ".odt", ".xlsx", ".csv"}

# Files that belong beside media rather than being media. A .srt under Music
# is not "an unexpected file type", it is a sidecar doing its job.
#
# Derived, not listed. This was a hand-written set and it had already drifted
# both ways within one release: it claimed .log was a companion when the
# classifier treats it as extractable text, and it had never heard of .ass,
# .ssa, .vtt or .md5. One definition, in the module that owns the concept.
COMPANION = frozenset(SIDECAR_KINDS)
COVER_NAMES = {"cover", "folder", "front", "album", "albumart", "poster", "thumb"}

# Named by their operating system, not by anyone who wanted them.
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", ".directory", ".spotlight-v100"}

# Which extensions belong under which top-level folder. Only the categories
# where being wrong is unambiguous: a PDF under Music is a mistake, a .gcode
# under Projects is the point.
FOLDER_EXPECTS = {
    "music": AUDIO,
    "movies": VIDEO,
    "shows": VIDEO,
    "photos": IMAGE,
    "books": DOCUMENT,
}

# Never rename these, and never reason about the folder that contains them as
# though it were an album. A DVD rip is a structure, not a naming style.
DVD_MARKERS = {"video_ts", "audio_ts", "bdmv", "certificate"}


@dataclass
class Finding:
    relpath: str
    kind: str
    severity: str
    summary: str
    evidence: list[EvidenceEntry] = field(default_factory=list)
    dest_relpath: str | None = None
    item_id: int | None = None
    fingerprint: str | None = None
    root: str = "library"

    @property
    def is_correction(self) -> bool:
        """A finding with somewhere to put the file, versus one that only
        tells you something. Commit must never assume the first."""
        return self.dest_relpath is not None


@dataclass(frozen=True)
class AuditSummary:
    scope: str
    files_seen: int
    findings: int
    high: int
    review: int
    kinds: dict[str, int]
    # What the catalog tier did, if it ran. Reported rather than hidden: "AI
    # unavailable for 3 ambiguous files" is a successful audit that should say
    # which part of itself was missing.
    catalog: object | None = None


@dataclass
class LibraryView:
    """Everything the detectors are allowed to look at, gathered once.

    Passing this around rather than a connection keeps every detector a pure
    function of what was read, which is what makes them testable without a
    library on disk.
    """

    files: list[str]
    indexed: dict[str, sqlite3.Row]
    fingerprints: dict[str, list[str]]
    tags: dict[str, dict[str, str]]
    junk: list[str]
    # Whether each audio file carries a picture frame. Free: the probe that
    # read the tags already reported the streams, and asking ffprobe a second
    # time later — once per album, from the artwork stage — was paying twice
    # for an answer already in hand.
    artwork: dict[str, bool] = field(default_factory=dict)

    def top(self, relpath: str) -> str:
        return relpath.split("/", 1)[0].lower()

    def parent(self, relpath: str) -> str:
        return relpath.rpartition("/")[0]


def audit_library(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    scope: str = "",
    read_tags: bool = True,
    use_catalogs: bool = True,
) -> AuditSummary:
    """Examine a scope of the library and record what looks wrong.

    Writes to `audit_findings` and `catalog_identity`, and to nothing else on
    disk. `scope` is a relative folder — `Music`, `Music/Pop` — or empty for
    everything.

    The tiers cost very different amounts, so each can be turned off without
    turning off the ones below it. `read_tags=False` is a fast structural pass;
    `use_catalogs=False` keeps the whole audit local. A catalog that is
    disabled, unreachable or slow degrades to "no answer" — the deterministic
    findings do not depend on it and the audit still succeeds.
    """
    from librairy.audit_catalog import CatalogRun

    view = gather(conn, settings, scope=scope, read_tags=read_tags)
    run = CatalogRun()
    findings = detect(view, conn=conn if use_catalogs else None, run=run)
    record_findings(conn, findings, scope=scope)
    kinds: dict[str, int] = defaultdict(int)
    for finding in findings:
        kinds[finding.kind] += 1
    return AuditSummary(
        scope=scope or "the whole library",
        files_seen=len(view.files),
        findings=len(findings),
        high=sum(1 for finding in findings if finding.severity == "high"),
        review=sum(1 for finding in findings if finding.severity == "review"),
        kinds=dict(kinds),
        catalog=run,
    )


def gather(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    scope: str = "",
    read_tags: bool = True,
) -> LibraryView:
    """One read of the world. Nothing below this line touches the disk."""
    patterns = settings.ignore_patterns
    base = settings.library_dir / scope if scope else settings.library_dir
    prefix = scope.strip("/")
    files = visible_files(base, patterns, prefix=prefix) if base.is_dir() else []

    indexed = {
        row["relpath"]: row
        for row in conn.execute(
            "SELECT * FROM items WHERE root='library' AND missing_since IS NULL"
        )
    }
    fingerprints: dict[str, list[str]] = defaultdict(list)
    for relpath, row in indexed.items():
        if row["fingerprint"]:
            fingerprints[row["fingerprint"]].append(relpath)

    tags: dict[str, dict[str, str]] = {}
    artwork: dict[str, bool] = {}
    if read_tags:
        for relpath in files:
            if PurePosixPath(relpath).suffix.lower() in AUDIO:
                tags[relpath], artwork[relpath] = _audio_facts(
                    settings.library_dir / relpath, settings
                )

    return LibraryView(
        files=files,
        indexed=indexed,
        fingerprints=dict(fingerprints),
        tags=tags,
        junk=_junk_files(base, prefix),
        artwork=artwork,
    )


def _audio_facts(path: Path, settings: Settings) -> tuple[dict[str, str], bool]:
    """The tags and whether there is a cover inside, from one ffprobe call.

    A cover inside a FLAC is a video stream with the `attached_pic`
    disposition, which is the same shape ffprobe reports for an mp3's APIC
    frame. Both come back from the probe that was being run for the tags
    anyway, so reading them together costs nothing and saves the artwork stage
    a second pass over the same files.
    """
    try:
        from librairy.tools.ffprobe import probe

        result = probe(path, settings)
    except Exception:  # noqa: BLE001 - metadata is best-effort
        return {}, False
    if not result.ok or not isinstance(result.data, dict):
        return {}, False
    raw = result.data.get("tags")
    tags = {str(k).lower(): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    has_art = any(
        stream.get("disposition", {}).get("attached_pic")
        or stream.get("codec_type") == "video"
        for stream in result.data.get("streams") or ()
        if isinstance(stream, dict)
    )
    return tags, has_art


def detect(
    view: LibraryView,
    *,
    conn: sqlite3.Connection | None = None,
    run: object | None = None,
    skip: frozenset[str] = frozenset(),
    collections: bool = True,
) -> list[Finding]:
    """Every detector, over one gathered view.

    Three tiers, split by what each one costs rather than by how much it
    matters:

    * **Tier 0** reads the filesystem and the index. Microseconds, always run.
    * **Tier 1** (`audit_music`) needs embedded tags, roughly 30 ms a file, so
      it does nothing at all when tags were not gathered — `read_tags=False`
      is a fast structural pass, not a broken audit.
    * **Tier 2** (`audit_catalog`) leaves the machine. It needs a connection,
      because an answer worth waiting for is worth remembering, and it is
      skipped entirely when the caller passes none.

    Each tier can be absent without the ones below it changing their answers.

    `skip` names detectors a caller is doing better itself. The staged runner
    passes `missing-artwork`, because its own artwork stage asks the same
    question with more to go on — embedded pictures and a catalog — and two
    answers to one question is how a single missing cover became nine rows.

    `collections=False` is the same idea one level up: the staged runner
    judges multi-artist folders in its catalog stage, where it knows what the
    catalogs said. The groups are still claimed so the later detectors stay
    quiet about them.
    """
    from librairy import audit_music

    findings: list[Finding] = []
    for kind, detector in (
        ("unexpected-file-type", _unexpected_file_types),
        ("loose-file", _loose_files),
        ("naming-cleanup", _naming_hygiene),
        ("naming-inconsistency", _naming_inconsistencies),
        ("tag-path-mismatch", _tag_path_mismatches),
        ("duplicate", _duplicates),
        ("missing-artwork", _missing_artwork),
        ("unindexed", _unindexed),
        ("system-junk", _system_junk),
    ):
        if kind in skip:
            continue
        findings.extend(detector(view))
    if view.tags:
        findings.extend(audit_music.detect(view, collections=collections))
        if conn is not None:
            findings.extend(_catalog_tier(conn, view, run))
    for finding in findings:
        row = view.indexed.get(finding.relpath)
        if row is not None:
            finding.item_id = row["id"]
            finding.fingerprint = row["fingerprint"]
    return sorted(findings, key=lambda finding: (finding.relpath, finding.kind))


def _catalog_tier(conn: sqlite3.Connection, view: LibraryView, run: object | None) -> list[Finding]:
    """Ask outside, once, and never let the answer decide the audit's fate."""
    from librairy import audit_catalog, audit_music

    try:
        lookup = audit_catalog.musicbrainz_lookup(conn)
        return audit_catalog.reconcile_music(
            conn, view, audit_music.albums_in(view), lookup, run=run
        )
    except Exception:  # noqa: BLE001 - a catalog outage is not an audit failure
        LOGGER.warning("catalog reconciliation skipped", exc_info=True)
        return []


# --- detectors ---------------------------------------------------------------


def _unexpected_file_types(view: LibraryView) -> list[Finding]:
    """A PDF under Music. Deterministic and obvious — no model required.

    Only fires for the categories where the expectation is unambiguous, and
    never for a sidecar: a .cue beside a .flac is doing its job.
    """
    findings = []
    for relpath in view.files:
        expected = FOLDER_EXPECTS.get(view.top(relpath))
        suffix = PurePosixPath(relpath).suffix.lower()
        if expected is None or not suffix or suffix in expected or suffix in COMPANION:
            continue
        if _in_dvd_structure(relpath):
            continue
        # Artwork inside a music folder is artwork, not an intruder.
        if suffix in IMAGE and view.top(relpath) in {"music", "movies", "shows"}:
            continue
        findings.append(
            Finding(
                relpath=relpath,
                kind="unexpected-file-type",
                severity="high",
                summary=f"A {suffix} file under {relpath.split('/')[0]}.",
                evidence=[
                    EvidenceEntry("filesystem", "extension", suffix, 0.95),
                    EvidenceEntry("filesystem", "folder", relpath.split("/")[0], 0.95),
                ],
            )
        )
    return findings


def _loose_files(view: LibraryView) -> list[Finding]:
    """A track sitting at a depth where its neighbours use folders.

    The comparison is against the siblings, not against a rule: if every other
    thing at this level is a directory holding albums, a bare file here is out
    of step. If half the library is flat, it is a style and not a finding.
    """
    depths: dict[str, list[str]] = defaultdict(list)
    for relpath in view.files:
        depths[view.parent(relpath)].append(relpath)
    typical = _typical_depth(view)
    if typical is None:
        return []
    findings = []
    for parent, siblings in depths.items():
        depth = len(parent.split("/")) if parent else 0
        if depth >= typical or view.top(siblings[0]) not in FOLDER_EXPECTS:
            continue
        for relpath in siblings:
            if PurePosixPath(relpath).suffix.lower() in COMPANION | IMAGE:
                continue
            if _in_dvd_structure(relpath):
                continue
            findings.append(
                Finding(
                    relpath=relpath,
                    kind="loose-file",
                    severity="review",
                    summary=(
                        f"Sits {depth} folder(s) deep where the rest of your library uses "
                        f"{typical}."
                    ),
                    evidence=[
                        EvidenceEntry("filesystem", "depth", str(depth), 0.7),
                        EvidenceEntry("library-pattern", "typical depth", str(typical), 0.7),
                    ],
                )
            )
    return findings


def _typical_depth(view: LibraryView) -> int | None:
    """How deep this library usually files things, by simple majority.

    Returns None when there is no majority: an inconsistent library has no
    convention to be inconsistent with, and inventing one would produce a
    finding for every file in it.
    """
    counts: dict[int, int] = defaultdict(int)
    for relpath in view.files:
        if PurePosixPath(relpath).suffix.lower() in COMPANION | IMAGE:
            continue
        counts[len(relpath.split("/")) - 1] += 1
    if not counts:
        return None
    total = sum(counts.values())
    depth, count = max(counts.items(), key=lambda pair: pair[1])
    return depth if count * 2 > total else None


def _naming_hygiene(view: LibraryView) -> list[Finding]:
    """Path components that break LibrAIry's own naming rules.

    Deterministic: leading spaces, doubled spaces, emoji, typographic quotes,
    characters Windows rejects. No tags, no catalog and no model needed to be
    sure about any of them.

    It checks *components*, not paths, and reports each bad component once —
    so a folder called `  Vacation 2022 🔥 ` holding forty files is one finding
    and not forty. That is the same lesson the artwork detector learned when a
    45-track compilation produced twenty-eight identical rows.

    House style is deliberately not enforced here. `slugify` would also turn
    every space into a dash and drop apostrophes, and against the author's real
    library that rewrites 118 of 140 files — see the note in `naming.py`.
    """
    from librairy.naming import hygiene_issues, is_structural, tidy_component

    seen: dict[str, Finding] = {}
    for relpath in view.files:
        parts = relpath.split("/")
        for index, part in enumerate(parts):
            key = "/".join(parts[: index + 1])
            if key in seen or is_structural(part, tuple(parts[:index])):
                continue
            is_file = index == len(parts) - 1
            issues = hygiene_issues(part, is_filename=is_file)
            if not issues:
                continue
            tidy = tidy_component(part, is_filename=is_file)
            if tidy == part:
                continue
            parent = "/".join(parts[:index])
            seen[key] = Finding(
                relpath=key,
                # A file can be renamed by one move the plan already
                # represents. A folder is every file beneath it.
                kind="naming-cleanup" if is_file else "naming-inconsistency",
                severity="high" if _breaks_things(issues) else "review",
                summary=" ".join(issue.detail for issue in issues),
                dest_relpath=f"{parent}/{tidy}" if parent else tidy,
                evidence=[
                    EvidenceEntry("filesystem", "name", part, 1.0),
                    *[
                        EvidenceEntry("filesystem", issue.rule, issue.detail, 1.0)
                        for issue in issues
                    ],
                ],
            )
    return list(seen.values())


def _breaks_things(issues: list) -> bool:
    """Cosmetic, or actually broken on somebody's laptop."""
    serious = {"windows-forbidden", "control", "reserved", "invisible", "too-long"}
    return any(issue.rule in serious for issue in issues)


def _naming_inconsistencies(view: LibraryView) -> list[Finding]:
    """A folder shouting when its own files say it has a name with lower case.

    `str.isupper()` is not the test and must never be. `ABBA`, `MF DOOM`,
    `NASA` and `AC/DC` are all correctly upper case, and "lower-case it because
    it looks loud" would turn a right name into a wrong one.

    So the evidence decides. The tags inside the folder know what the artist is
    actually called: if they say `James Brown` where the folder says
    `JAMES BROWN`, that is a real inconsistency with a real correction. If they
    say `ABBA`, the folder is right and there is no finding. With no tags at
    all it falls back to the weaker sibling-convention signal and proposes
    nothing — an observation, not a rename.
    """
    by_parent: dict[str, set[str]] = defaultdict(set)
    for relpath in view.files:
        parts = relpath.split("/")
        for index in range(1, len(parts) - 1):
            by_parent["/".join(parts[:index])].add(parts[index])
    findings = []
    for parent, names in sorted(by_parent.items()):
        shouting = sorted(name for name in names if _is_shouting(name))
        for name in shouting:
            folder = f"{parent}/{name}"
            canonical = _canonical_casing(view, folder, name)
            if canonical == name:
                # The tags agree with the folder. ABBA is called ABBA.
                continue
            if canonical:
                findings.append(
                    Finding(
                        relpath=folder,
                        kind="naming-inconsistency",
                        severity="high",
                        summary=(
                            f"The files inside are tagged {canonical!r}, "
                            f"but the folder is {name!r}."
                        ),
                        dest_relpath=f"{parent}/{canonical}",
                        evidence=[
                            EvidenceEntry("filesystem", "folder", name, 1.0),
                            EvidenceEntry("tags", "canonical name", canonical, 0.9),
                        ],
                    )
                )
                continue
            # No tags to appeal to. The siblings are the only evidence, and
            # they are weak enough that four of them are the minimum.
            others = names - set(shouting)
            if len(names) < 4 or len(shouting) * 2 >= len(names) or not others:
                continue
            findings.append(
                Finding(
                    relpath=folder,
                    kind="naming-inconsistency",
                    severity="review",
                    summary=(
                        f"Capitalised differently from the {len(others)} folders beside it."
                    ),
                    evidence=[
                        EvidenceEntry("filesystem", "folder", name, 0.6),
                        EvidenceEntry(
                            "library-pattern", "siblings", ", ".join(sorted(others)[:3]), 0.6
                        ),
                    ],
                )
            )
    return findings


def _canonical_casing(view: LibraryView, folder: str, name: str) -> str | None:
    """What the files inside call this folder, if they agree and it is a name.

    Only the same name spelled differently counts. A tag that is a different
    string altogether is a `tag-path-mismatch`, which is a different finding
    with a different answer, and letting this one propose a rename as well
    would give the same file two competing corrections.
    """
    # Both tags, not the usual album_artist-then-artist preference. On a
    # compilation the album artist is "Various Artists", which is a true
    # statement about the album and says nothing about the artist folder the
    # track sits in — and preferring it is why `JAMES BROWN` found no
    # canonical spelling on the author's real library.
    prefix = f"{folder}/"
    values = {
        (tags.get(field) or "").strip()
        for relpath, tags in view.tags.items()
        if relpath.startswith(prefix)
        for field in ("album_artist", "artist")
    }
    candidates = {value for value in values if value and _same(value, name)}
    if len(candidates) != 1:
        return None
    canonical = candidates.pop()
    from librairy.naming import hygiene_issues

    # A tag with its own naming problems is not an improvement.
    return canonical if not hygiene_issues(canonical) else None


def _is_shouting(name: str) -> bool:
    letters = [char for char in name if char.isalpha()]
    return len(letters) > 3 and all(char.isupper() for char in letters)


def _tag_path_mismatches(view: LibraryView) -> list[Finding]:
    """Embedded tags say one artist; the folder says another.

    This is the only detector that proposes a destination, and it is
    deliberately hard to satisfy: the tagged artist must *already have a folder
    somewhere in your library*, so the suggestion is always to move a file to
    a place you built, never to a place a catalog invented. Compilations —
    where the album folder is shared and the artist varies — are the reason
    the album name has to match too.
    """
    artist_homes = _artist_homes(view)
    findings = []
    for relpath, tags in view.tags.items():
        artist = (tags.get("album_artist") or tags.get("artist") or "").strip()
        parts = relpath.split("/")
        if not artist or len(parts) < 3:
            continue
        folder_artist = parts[-3]
        if _same(artist, folder_artist):
            continue
        home = artist_homes.get(_key(artist))
        if home is None or home == "/".join(parts[:-2]):
            continue
        album = (tags.get("album") or "").strip()
        dest = f"{home}/{parts[-2]}/{parts[-1]}"
        findings.append(
            Finding(
                relpath=relpath,
                kind="tag-path-mismatch",
                severity="high",
                summary=f"Tagged {artist!r} but filed under {folder_artist!r}.",
                dest_relpath=dest,
                evidence=[
                    EvidenceEntry("tags", "artist", artist, 0.9),
                    EvidenceEntry("tags", "album", album, 0.8) if album else None,
                    EvidenceEntry("filesystem", "current folder", folder_artist, 0.9),
                    EvidenceEntry("library-pattern", "existing folder", home, 0.85),
                ],
            )
        )
    for finding in findings:
        finding.evidence = [entry for entry in finding.evidence if entry is not None]
    return findings


def _artist_homes(view: LibraryView) -> dict[str, str]:
    """Where each artist folder already lives, keyed for loose comparison."""
    homes: dict[str, str] = {}
    for relpath in view.files:
        parts = relpath.split("/")
        if len(parts) >= 3 and view.top(relpath) == "music":
            homes.setdefault(_key(parts[-3]), "/".join(parts[:-2]))
    return homes


def _duplicates(view: LibraryView) -> list[Finding]:
    """The same bytes in two places. One finding, not two."""
    findings = []
    for fingerprint, paths in sorted(view.fingerprints.items()):
        if len(paths) < 2:
            continue
        keep, *rest = sorted(paths)
        if keep not in view.files and not any(path in view.files for path in rest):
            continue
        evidence = [
            EvidenceEntry("fingerprint", "blake2b", fingerprint[:16], 1.0),
            *[EvidenceEntry("filesystem", "also at", path, 1.0) for path in rest[:3]],
        ]
        # Identical bytes are identical sizes, so one number describes the
        # whole set — and "4.8 MB each" is what makes a duplicate row worth
        # acting on rather than merely true.
        row = view.indexed.get(keep) or next(
            (view.indexed[path] for path in rest if path in view.indexed), None
        )
        if row is not None and row["size"]:
            evidence.append(EvidenceEntry("filesystem", "each", str(row["size"]), 1.0))
        findings.append(
            Finding(
                relpath=keep,
                kind="duplicate",
                severity="review",
                summary=f"Identical bytes to {len(rest)} other file(s) in your library.",
                evidence=evidence,
            )
        )
    return findings


def _missing_artwork(view: LibraryView) -> list[Finding]:
    """An album with no cover anywhere in it — one finding per album.

    Written per-folder first, and the real library said no: a 45-track disco
    compilation filed one-artist-per-folder produced twenty-eight identical
    rows for a single missing cover. An album is the thing a cover belongs to,
    so the grouping key is the album, and a compilation spread across
    twenty-seven artist folders is one answer to one question.
    """
    albums: dict[str, list[str]] = defaultdict(list)
    for relpath in view.files:
        parent = view.parent(relpath)
        if parent and view.top(relpath) == "music":
            albums[PurePosixPath(parent).name].append(relpath)
    findings = []
    for album, contents in sorted(albums.items()):
        audio = [path for path in contents if PurePosixPath(path).suffix.lower() in AUDIO]
        if len(audio) < 2:
            # One loose track is not evidence of an album missing its cover.
            continue
        if any(_is_cover(path) for path in contents):
            continue
        folders = sorted({view.parent(path) for path in audio})
        findings.append(
            Finding(
                relpath=folders[0],
                kind="missing-artwork",
                severity="review",
                summary=(
                    f"{album!r}: {len(audio)} tracks and no cover image"
                    + (f", across {len(folders)} folders." if len(folders) > 1 else ".")
                ),
                evidence=[
                    EvidenceEntry("filesystem", "album", album, 0.8),
                    EvidenceEntry("filesystem", "tracks", str(len(audio)), 0.8),
                ],
            )
        )
    return findings


def _is_cover(relpath: str) -> bool:
    path = PurePosixPath(relpath)
    return (
        path.suffix.lower() in IMAGE and path.stem.lower().replace(" ", "") in COVER_NAMES
    )


def _unindexed(view: LibraryView) -> list[Finding]:
    """On disk, and nothing has ever looked at it.

    Browse shows these already; the audit is where you can do something about
    one without dragging it back through the inbox by hand.
    """
    return [
        Finding(
            relpath=relpath,
            kind="unindexed",
            severity="review",
            summary="On disk but never scanned, so Search cannot find it.",
            evidence=[EvidenceEntry("filesystem", "present", "yes", 1.0)],
        )
        for relpath in view.files
        if relpath not in view.indexed
    ]


def _system_junk(view: LibraryView) -> list[Finding]:
    """`.DS_Store` and friends. Reported, never deleted, never quarantined."""
    return [
        Finding(
            relpath=relpath,
            kind="system-junk",
            severity="review",
            summary="Left behind by an operating system, not by you.",
            evidence=[EvidenceEntry("filesystem", "name", PurePosixPath(relpath).name, 1.0)],
        )
        for relpath in view.junk
    ]


def _junk_files(base: Path, prefix: str) -> list[str]:
    """Walked separately because the shared ignore predicate hides them.

    Browse is right to hide `.DS_Store`; the audit is the one place that wants
    to know they are there.
    """
    found: list[str] = []
    if not base.is_dir():
        return found
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name.lower() in JUNK_NAMES or path.name.startswith("._"):
            relative = path.relative_to(base).as_posix()
            found.append(f"{prefix}/{relative}" if prefix else relative)
    return found


def _in_dvd_structure(relpath: str) -> bool:
    return any(part.lower() in DVD_MARKERS for part in relpath.split("/")[:-1])


def _same(left: str, right: str) -> bool:
    return _key(left) == _key(right)


def _key(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())


# --- persistence -------------------------------------------------------------


def record_findings(
    conn: sqlite3.Connection, findings: list[Finding], *, scope: str = ""
) -> None:
    """Store what this run found, and retire what it no longer does.

    A finding you have already answered stays answered: a `kept` row is left
    alone unless the file itself changed, so the same audit next week does not
    ask you the same question again. Open rows for problems that have since
    gone away are dropped, because a list that only grows stops being read.
    """
    now = utc_now()
    seen = {(finding.root, finding.relpath, finding.kind) for finding in findings}
    live = _live_item_ids(conn, findings)
    for finding in findings:
        # The staged audit reads the index at its first slice and writes at its
        # last, minutes and several worker cycles later. In between, the
        # scanner is free to re-index a file — and an `item_id` captured
        # before that no longer resolves, which the foreign key rejects and
        # which failed a whole run on the live installation. The finding is
        # still true; only the link to a row is stale, and a finding whose
        # file is not indexed is a case this table already models.
        if finding.item_id is not None and finding.item_id not in live:
            finding.item_id = None
        existing = conn.execute(
            "SELECT id, status, fingerprint FROM audit_findings "
            "WHERE root=? AND relpath=? AND kind=?",
            (finding.root, finding.relpath, finding.kind),
        ).fetchone()
        # You said this was deliberate. Only a changed file reopens it.
        if (
            existing
            and existing["status"] == "kept"
            and existing["fingerprint"] == finding.fingerprint
        ):
            continue
        # An approval survives re-discovery.
        #
        # This is where the live inconsistency came from. The audit re-finds a
        # problem it has already reported — which it must, since the files have
        # not moved yet — and the upsert below wrote `status='open'`
        # unconditionally. A finding that had been approved, and whose plan was
        # sitting in Commit waiting to be run, silently became an open question
        # again while still pointing at that plan. Review then offered to
        # approve it a second time.
        #
        # The newer evidence is still worth keeping: the summary, the severity
        # and the fingerprint are what the row *is*, and refusing to update them
        # would leave the page describing a library that has moved on. Only the
        # status is protected, because only the status is a decision.
        if existing and _has_active_plan(conn, existing["id"]):
            _update_evidence(conn, existing["id"], finding, now)
            continue
        conn.execute(
            """
            INSERT INTO audit_findings(
              item_id, root, relpath, kind, severity, summary, dest_root, dest_relpath,
              evidence, fingerprint, status, detected_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            ON CONFLICT(root, relpath, kind) DO UPDATE SET
              item_id=excluded.item_id,
              severity=excluded.severity,
              summary=excluded.summary,
              dest_root=excluded.dest_root,
              dest_relpath=excluded.dest_relpath,
              evidence=excluded.evidence,
              fingerprint=excluded.fingerprint,
              status='open',
              updated_at=excluded.updated_at
            """,
            (
                finding.item_id,
                finding.root,
                finding.relpath,
                finding.kind,
                finding.severity,
                finding.summary,
                "library" if finding.dest_relpath else None,
                finding.dest_relpath,
                json.dumps([entry.__dict__ for entry in finding.evidence]),
                finding.fingerprint,
                now,
                now,
            ),
        )
    _retire_resolved(conn, seen, scope)


def _has_active_plan(conn: sqlite3.Connection, finding_id: int) -> bool:
    from librairy.correction_state import active_plans

    return bool(active_plans(conn, finding_id))


def _update_evidence(
    conn: sqlite3.Connection, finding_id: int, finding: Finding, now: str
) -> None:
    """Refresh what the audit now knows, without touching the decision.

    The fingerprint comes along deliberately. It is the record of what the file
    was when this was last looked at, and the pending plan's own staleness is
    measured against the *plan's* copies of it, not this one — so updating here
    keeps the finding honest without quietly making an outdated approval look
    current. See `correction_state.plan_drift`.
    """
    conn.execute(
        "UPDATE audit_findings SET item_id=?, severity=?, summary=?, dest_root=?,"
        " dest_relpath=?, evidence=?, fingerprint=?, updated_at=? WHERE id=?",
        (
            finding.item_id,
            finding.severity,
            finding.summary,
            "library" if finding.dest_relpath else None,
            finding.dest_relpath,
            json.dumps([entry.__dict__ for entry in finding.evidence]),
            finding.fingerprint,
            now,
            finding_id,
        ),
    )


def _live_item_ids(conn: sqlite3.Connection, findings: list[Finding]) -> set[int]:
    """Which of the item ids these findings carry still exist.

    One query for the whole batch rather than one per finding: a whole-library
    audit records hundreds of rows, and this runs inside the write that the web
    request is waiting behind.
    """
    wanted = sorted({finding.item_id for finding in findings if finding.item_id is not None})
    if not wanted:
        return set()
    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"SELECT id FROM items WHERE id IN ({placeholders})", wanted  # noqa: S608
    )
    return {int(row["id"]) for row in rows}


def _retire_resolved(
    conn: sqlite3.Connection, seen: set[tuple[str, str, str]], scope: str
) -> None:
    prefix = f"{scope.strip('/')}/" if scope else ""
    rows = conn.execute(
        "SELECT id, root, relpath, kind, plan_id FROM audit_findings WHERE status='open'"
    ).fetchall()
    for row in rows:
        if prefix and not row["relpath"].startswith(prefix):
            continue
        if (row["root"], row["relpath"], row["kind"]) not in seen:
            # Never delete a finding a plan still points at. An `open` row is
            # normally nobody's, but the pair of foreign keys can leave one
            # holding an approved plan — and deleting it would leave that plan
            # in Commit with nothing to explain what it was for, which is worse
            # than the inconsistency it came from. `librairy db check` reports
            # these; retiring the list quietly is not a repair.
            if row["plan_id"] or _has_active_plan(conn, row["id"]):
                continue
            conn.execute("DELETE FROM audit_findings WHERE id=?", (row["id"],))


def open_findings(
    conn: sqlite3.Connection, *, scope: str = "", include_accepted: bool = False
) -> list[sqlite3.Row]:
    """Findings still awaiting an answer.

    `include_accepted` adds the ones already approved and waiting for Commit.
    Review wants them — a correction you accepted should stay visible, saying
    what it is waiting for — while the CLI's "what is open" count does not.
    """
    statuses = ("open", "accepted") if include_accepted else ("open",)
    return findings_with_status(conn, statuses, scope=scope)


def findings_with_status(
    conn: sqlite3.Connection, statuses: tuple[str, ...], *, scope: str = ""
) -> list[sqlite3.Row]:
    """Findings in any of the given database statuses.

    One query rather than one per view, because the four Review workloads
    (open, waiting for Commit, dismissed, corrected) differ only in this tuple
    and every column below has to be identical for the row renderer to work.
    """
    placeholders = ",".join("?" for _ in statuses)
    # `i.size` comes along so a finding about a file can show how big it is.
    # A left join, because a finding can name a file nothing has indexed —
    # "not indexed" is one of the things the audit reports — and an inner join
    # would silently drop exactly those rows.
    sql = (
        "SELECT f.*, i.size AS item_size FROM audit_findings f "  # noqa: S608
        f"LEFT JOIN items i ON i.id = f.item_id WHERE f.status IN ({placeholders})"
    )
    params: list[object] = list(statuses)
    if scope:
        sql += " AND f.relpath LIKE ?"
        params.append(f"{scope.strip('/')}/%")
    return list(conn.execute(f"{sql} ORDER BY f.severity DESC, f.relpath", params))


def finding_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM audit_findings GROUP BY status"
    ).fetchall()
    counts = {row["status"]: row["count"] for row in rows}
    counts["open"] = counts.get("open", 0)
    return counts


def keep_as_is(conn: sqlite3.Connection, finding_id: int) -> None:
    """"I do not want this suggestion." The row stays as a record of the answer.

    Never a delete. The record is what makes the decision reversible, what
    keeps the next identical audit quiet, and what lets someone ask months
    later why their library looks the way it does. `restore_suggestion` is the
    other half, and the pair is the reason this is safe to press.
    """
    conn.execute(
        "UPDATE audit_findings SET status='kept', updated_at=? WHERE id=?",
        (utc_now(), finding_id),
    )


def restore_suggestion(conn: sqlite3.Connection, finding_id: int) -> bool:
    """Put a dismissed suggestion back into the active list.

    Only from `kept`, and deliberately so. Restoring an executed correction
    would mean re-proposing a move that already happened, and restoring one
    that is waiting for Commit would leave two rows claiming the same plan.
    Those have their own reversals — Undo and Remove approval — and one
    control that sometimes means three different things is how people stop
    trusting all three.

    No re-analysis. The evidence that produced the suggestion is still the
    evidence; if the file itself changed, the row will say so on its own,
    because staleness is measured at render time against the fingerprint.
    """
    changed = conn.execute(
        "UPDATE audit_findings SET status='open', updated_at=? WHERE id=? AND status='kept'",
        (utc_now(), finding_id),
    )
    return changed.rowcount > 0


def sanitize_scope(scope: str, library_dir: Path | None = None) -> str:
    """A scope is a folder inside the library, or nothing at all.

    An empty scope is the whole library and is legitimate; anything else has
    to survive the same containment check every other path in LibrAIry does,
    because this one arrives from a form.
    """
    from librairy.paths import validate_relpath

    clean = scope.strip().strip("/")
    if not clean:
        return ""
    if library_dir is not None:
        validate_relpath(library_dir, clean, kind="scope")
    elif ".." in clean.split("/") or clean.startswith("~") or "\\" in clean:
        from librairy.paths import PathValidationError

        raise PathValidationError("scope escapes the library")
    return clean

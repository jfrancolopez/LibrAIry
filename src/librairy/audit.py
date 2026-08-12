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
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from librairy.classify.companions import SIDECAR_KINDS
from librairy.config import Settings
from librairy.models import EvidenceEntry
from librairy.planner import utc_now
from librairy.scanner import visible_files

# What a finding is about. Only kinds with real detection logic behind them —
# there is no value in a category that exists to make the page look busy.
KINDS = {
    "unexpected-file-type": "Unexpected file type",
    "loose-file": "Loose file",
    "naming-inconsistency": "Naming inconsistency",
    "tag-path-mismatch": "Tags disagree with the folder",
    "duplicate": "Possible duplicate",
    "missing-artwork": "Missing artwork",
    "unindexed": "Not indexed",
    "system-junk": "System file",
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
# * `naming-inconsistency` proposes no destination and never will from here —
#   it is about a *folder*, and the corrected spelling of `JAMES BROWN` is a
#   judgement ("James Brown"? "James Brown & The J.B.'s"?) that this module
#   cannot make. Renaming a folder is also not one move but every file in it.
# * `duplicate` has a correct answer — quarantine the copy — but that is a
#   different action class with its own safety semantics, not a move.
# * `missing-artwork`, `unindexed` and `system-junk` describe files that are
#   exactly where they belong.
EXECUTABLE_KINDS = frozenset({"tag-path-mismatch"})

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
) -> AuditSummary:
    """Examine a scope of the library and record what looks wrong.

    Writes to `audit_findings` and to nothing else. `scope` is a relative
    folder — `Music`, `Music/Pop` — or empty for everything.
    """
    view = gather(conn, settings, scope=scope, read_tags=read_tags)
    findings = detect(view)
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
    if read_tags:
        from librairy.classify import _audio_tags

        for relpath in files:
            if PurePosixPath(relpath).suffix.lower() in AUDIO:
                tags[relpath] = _audio_tags(settings.library_dir / relpath, settings)

    return LibraryView(
        files=files,
        indexed=indexed,
        fingerprints=dict(fingerprints),
        tags=tags,
        junk=_junk_files(base, prefix),
    )


def detect(view: LibraryView) -> list[Finding]:
    """Every detector, over one gathered view."""
    findings: list[Finding] = []
    for detector in (
        _unexpected_file_types,
        _loose_files,
        _naming_inconsistencies,
        _tag_path_mismatches,
        _duplicates,
        _missing_artwork,
        _unindexed,
        _system_junk,
    ):
        findings.extend(detector(view))
    for finding in findings:
        row = view.indexed.get(finding.relpath)
        if row is not None:
            finding.item_id = row["id"]
            finding.fingerprint = row["fingerprint"]
    return sorted(findings, key=lambda finding: (finding.relpath, finding.kind))


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


def _naming_inconsistencies(view: LibraryView) -> list[Finding]:
    """A folder shouting among siblings that do not.

    `JAMES BROWN` next to `Barry White` and `Diana Ross` is a real
    inconsistency, but it is a judgement call and never a correction — the
    audit will not rename a folder because it dislikes the case.
    """
    by_parent: dict[str, set[str]] = defaultdict(set)
    for relpath in view.files:
        parts = relpath.split("/")
        for index in range(1, len(parts) - 1):
            by_parent["/".join(parts[:index])].add(parts[index])
    findings = []
    for parent, names in sorted(by_parent.items()):
        if len(names) < 4:
            continue
        shouting = sorted(name for name in names if _is_shouting(name))
        if not shouting or len(shouting) * 2 >= len(names):
            continue
        for name in shouting:
            findings.append(
                Finding(
                    relpath=f"{parent}/{name}",
                    kind="naming-inconsistency",
                    severity="review",
                    summary=(
                        f"Capitalised differently from the {len(names) - len(shouting)} "
                        f"folders beside it."
                    ),
                    evidence=[
                        EvidenceEntry("filesystem", "folder", name, 0.6),
                        EvidenceEntry(
                            "library-pattern",
                            "siblings",
                            ", ".join(sorted(names - set(shouting))[:3]),
                            0.6,
                        ),
                    ],
                )
            )
    return findings


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
                    EvidenceEntry("embedded-tags", "artist", artist, 0.9),
                    EvidenceEntry("embedded-tags", "album", album, 0.8) if album else None,
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
        findings.append(
            Finding(
                relpath=keep,
                kind="duplicate",
                severity="review",
                summary=f"Identical bytes to {len(rest)} other file(s) in your library.",
                evidence=[
                    EvidenceEntry("fingerprint", "blake2b", fingerprint[:16], 1.0),
                    *[EvidenceEntry("filesystem", "also at", path, 1.0) for path in rest[:3]],
                ],
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
    for finding in findings:
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


def _retire_resolved(
    conn: sqlite3.Connection, seen: set[tuple[str, str, str]], scope: str
) -> None:
    prefix = f"{scope.strip('/')}/" if scope else ""
    rows = conn.execute(
        "SELECT id, root, relpath, kind FROM audit_findings WHERE status='open'"
    ).fetchall()
    for row in rows:
        if prefix and not row["relpath"].startswith(prefix):
            continue
        if (row["root"], row["relpath"], row["kind"]) not in seen:
            conn.execute("DELETE FROM audit_findings WHERE id=?", (row["id"],))


def open_findings(
    conn: sqlite3.Connection, *, scope: str = "", include_accepted: bool = False
) -> list[sqlite3.Row]:
    """Findings still awaiting an answer.

    `include_accepted` adds the ones already approved and waiting for Commit.
    Review wants them — a correction you accepted should stay visible, saying
    what it is waiting for — while the CLI's "what is open" count does not.
    """
    statuses = "('open','accepted')" if include_accepted else "('open')"
    sql = f"SELECT * FROM audit_findings WHERE status IN {statuses}"  # noqa: S608
    params: list[object] = []
    if scope:
        sql += " AND relpath LIKE ?"
        params.append(f"{scope.strip('/')}/%")
    return list(conn.execute(f"{sql} ORDER BY severity DESC, relpath", params))


def finding_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM audit_findings GROUP BY status"
    ).fetchall()
    counts = {row["status"]: row["count"] for row in rows}
    counts["open"] = counts.get("open", 0)
    return counts


def keep_as_is(conn: sqlite3.Connection, finding_id: int) -> None:
    """"This is deliberate." The row stays as a record of the answer."""
    conn.execute(
        "UPDATE audit_findings SET status='kept', updated_at=? WHERE id=?",
        (utc_now(), finding_id),
    )


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

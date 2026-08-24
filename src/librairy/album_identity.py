"""Eleven tracks that already agree, asked once instead of eleven times.

    Music/Rock/The Clash/
        t01.flac  identified: Combat Rock
        t02.flac  identified: Combat Rock
        ...        nine more of them
        t11.flac  album tag:  Combat Rock
        t12.flac  nothing at all

`track_filing` asks every one of these separately, and that is the right shape
when the answers differ — two loose tracks beside two album folders are
commonly two different albums. It is the wrong shape when the answers are the
same, because then it is one conclusion typed out twelve times, and a person
pressing `Use Combat Rock` eleven times is not making eleven decisions.

So this reads the evidence that is **already persisted** and asks whether it
adds up to one. Nothing here fingerprints a file, calls MusicBrainz, or writes
an identity: the members' `track_identity` rows and the finding's own tag
evidence are the whole input, and both were recorded by something somebody
asked for. Aggregation that went back to the network would be a page render
with a provider call in it.

**When a group is one album.** Every open member is classified against each
candidate release, and only two classes support it — the file's own album tag,
or a stored identity whose recording is on that release. A candidate survives
only if **no member has positive evidence for a different album**. Seven tracks
saying `News of the World` and four saying `Greatest Hits` is not an album
conclusion, it is two, and forcing it would file four files into a release they
are not on. Those groups fall back to the per-track question they already had.

**Several candidates is a choice, not a tie to break.** A recording is on the
original album, on a compilation and on a remaster, and when every member is on
all three, all three are coherent. That is a fact about somebody's library
rather than about the files, so each is offered with what tells it apart and
none is picked. `Greatest Hits` and `News of the World` are never merged
because their tracklists overlap — overlapping is what a compilation *is*.

**Exceptions are shown, not absorbed.** A member with nothing to go on, or one
whose catalog artist disagrees with the folder it is sitting in, does not join
the conclusion and does not veto it either: it stays an open per-track
question, and the row says so in counts rather than in a percentage. Nine
matches and one unresolved is two facts; "90% confident" is neither of them.

**Approving it is the existing filing workflow.** Each supporting member is
given the destination through `track_filing.answer`, which re-checks that this
track may go to that folder, and the plan that follows is the ordinary
loose-track plan: N moves, one immutable decision, one Commit card, one Undo.
There is no album executor and no `mkdir` — a folder comes into being because
files arrive in it.

**Stale evidence cannot approve anything.** Identities are read through
`track_filing`, which recalls them against each file's current fingerprint, so
a track re-ripped since it was identified contributes nothing and the aggregate
is recomputed from what is true now.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings

#  Under this a group is not worth one control. Two tracks agreeing is two
#  presses, which is what the per-track row already does well, and a summary
#  over two rows is a heading with nothing under it.
MIN_MEMBERS = 3

#  How a member relates to one candidate release. The first two support it and
#  the last three do not, and only `OTHER` refuses it — see the module note.
EXACT = "catalog"
TAGGED = "tags"
OTHER = "other-release"
UNRESOLVED = "unresolved"
ARTIST_CONFLICT = "artist-conflict"

SUPPORTING = (EXACT, TAGGED)

REASON = {
    EXACT: "Identified from the audio",
    TAGGED: "Album tag inside the file",
    OTHER: "Identified as a different release",
    UNRESOLVED: "Nothing says what this one is",
    ARTIST_CONFLICT: "The catalog names a different artist",
}


@dataclass(frozen=True)
class Member:
    """One loose track, and how it stands to one candidate release."""

    relpath: str
    evidence: str
    detail: str = ""

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name

    @property
    def reason(self) -> str:
        return REASON.get(self.evidence, "")

    @property
    def supports(self) -> bool:
        return self.evidence in SUPPORTING


@dataclass(frozen=True)
class Conclusion:
    """One release these tracks could be filed as, and everything against it."""

    relpath: str
    name: str
    artist: str
    members: tuple[Member, ...]
    exceptions: tuple[Member, ...] = ()
    exists: bool = False
    year: int = 0
    kind: str = ""
    #  Members whose stored identity is the *same recording* as another
    #  member's. Two files of one recording inside one album is a fact worth
    #  saying out loud; it is not a reason to refuse the album, because the
    #  name collision it might cause is already answered by the merge choice
    #  the per-track workflow puts underneath it.
    repeats: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        """`Album · 1977` — what distinguishes this release from the others."""
        parts = [part for part in (self.kind, str(self.year) if self.year else "") if part]
        return " · ".join(parts)

    @property
    def exact(self) -> int:
        return sum(1 for member in self.members if member.evidence == EXACT)

    @property
    def tagged(self) -> int:
        return sum(1 for member in self.members if member.evidence == TAGGED)

    @property
    def unresolved(self) -> int:
        return sum(1 for member in self.exceptions if member.evidence == UNRESOLVED)

    @property
    def conflicts(self) -> int:
        return sum(1 for member in self.exceptions if member.evidence == ARTIST_CONFLICT)

    @property
    def counts(self) -> tuple[tuple[str, int], ...]:
        """The evidence as numbers, because numbers can be checked.

        No aggregate score and no percentage. "9 identified, 2 album tags, 1
        unresolved" is four facts a person can act on separately; one number
        made out of them is a claim that hides which is which.
        """
        found = [
            ("identified from the audio", self.exact),
            ("matching album tags", self.tagged),
            ("with nothing to go on", self.unresolved),
            ("by a different artist", self.conflicts),
        ]
        return tuple((label, count) for label, count in found if count)


@dataclass(frozen=True)
class Aggregate:
    """What a group of loose tracks agrees on, if anything."""

    finding_id: int
    artist: str
    open_tracks: int
    conclusions: tuple[Conclusion, ...]

    @property
    def artist_name(self) -> str:
        return PurePosixPath(self.artist).name

    @property
    def single(self) -> bool:
        """One release, and the row can offer to file them as it."""
        return len(self.conclusions) == 1

    @property
    def choice(self) -> bool:
        """Several coherent releases. A person picks; nothing is ranked."""
        return len(self.conclusions) > 1


# --- reading the evidence -----------------------------------------------------------


def aggregate(
    conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row
) -> Aggregate | None:
    """One conclusion over the loose tracks in this finding, or None.

    None is the ordinary answer and means the per-track question stands: the
    finding is not a filing one, the group is too small, the members disagree,
    or they have already been answered.
    """
    from librairy.track_filing import plan_filing

    view = plan_filing(conn, settings, row, verify=False)
    if view is None:
        return None
    return from_view(view, row)


def from_view(view, row: sqlite3.Row) -> Aggregate | None:  # noqa: ANN001
    """The aggregate over an already-built filing view.

    Split out because both the page and the action have the view in hand and
    building it twice would be two sets of the same queries per request.
    """
    from librairy.audit_music import key
    from librairy.naming import tidy_component
    from librairy.track_filing import _commonest, _tag_claims

    #  Answered tracks are out of it. Somebody has already said where each of
    #  those goes, and an album-level control that quietly reopened their
    #  answers would be the software overruling them.
    tracks = tuple(track for track in view.tracks if not track.answered)
    if len(tracks) < MIN_MEMBERS:
        return None
    tagged = {claim.relpath: claim.album for claim in _tag_claims(row, tracks)}
    releases = {track.relpath: _releases(track) for track in tracks}
    existing = {key(album.name): album.relpath for album in view.albums}

    spellings: dict[str, list[str]] = {}
    detail: dict[str, tuple[int, str]] = {}
    for relpath, found in releases.items():
        for release in found:
            if not release.title:
                continue
            spellings.setdefault(key(release.title), []).append(release.title)
            detail.setdefault(key(release.title), (release.year, release.kind))
        album = tagged.get(relpath, "")
        if album:
            spellings.setdefault(key(album), []).append(album)

    conclusions = []
    for candidate, said in sorted(spellings.items()):
        found = _conclusion(
            view,
            tracks,
            candidate,
            tagged=tagged,
            releases=releases,
            name=tidy_component(_commonest(said)),
            existing=existing,
            detail=detail.get(candidate, (0, "")),
        )
        if found is not None:
            conclusions.append(found)
    if not conclusions:
        return None
    return Aggregate(
        finding_id=view.finding_id,
        artist=view.artist,
        open_tracks=len(tracks),
        conclusions=tuple(conclusions),
    )


def _conclusion(  # noqa: PLR0913
    view,  # noqa: ANN001
    tracks: tuple,
    candidate: str,
    *,
    tagged: dict[str, str],
    releases: dict[str, tuple],
    name: str,
    existing: dict[str, str],
    detail: tuple[int, str],
) -> Conclusion | None:
    """This release as a conclusion over these tracks, or None if it is not one.

    The refusal that matters is `OTHER`: a member with positive evidence for a
    *different* album. One of those and this is not one album, whatever the
    majority says — a rule that filed the minority anyway would be filing files
    into a release they are demonstrably not on.
    """
    if not name:
        return None
    members: list[Member] = []
    exceptions: list[Member] = []
    for track in tracks:
        member = _member(track, candidate, tagged, releases)
        if member.evidence == OTHER:
            return None
        (members if member.supports else exceptions).append(member)
    if len(members) < MIN_MEMBERS:
        return None
    year, kind = detail
    return Conclusion(
        relpath=existing.get(candidate) or f"{view.artist}/{name}",
        name=PurePosixPath(existing[candidate]).name if candidate in existing else name,
        artist=PurePosixPath(view.artist).name,
        members=tuple(members),
        exceptions=tuple(exceptions),
        exists=candidate in existing,
        year=year,
        kind=kind,
        repeats=_repeats(members, {track.relpath: track for track in tracks}),
    )


def _member(
    track,  # noqa: ANN001
    candidate: str,
    tagged: dict[str, str],
    releases: dict[str, tuple],
) -> Member:
    """How one track stands to one candidate release.

    The artist disagreement is checked first and wins. A file whose audio the
    catalog says is by somebody else is not one this decision should sweep up,
    even when its own tag names the album — that disagreement is the useful
    part and filing on either reading would bury it.
    """
    from librairy.audit_music import key

    if track.conflict:
        return Member(track.relpath, ARTIST_CONFLICT, track.conflict)
    album = tagged.get(track.relpath, "")
    if album and key(album) == candidate:
        return Member(track.relpath, TAGGED, album)
    titles = {key(release.title): release.title for release in releases[track.relpath]}
    if candidate in titles:
        identity = track.identity
        return Member(track.relpath, EXACT, getattr(identity, "title", ""))
    if album:
        return Member(track.relpath, OTHER, album)
    if titles:
        return Member(track.relpath, OTHER, ", ".join(sorted(titles.values())))
    return Member(track.relpath, UNRESOLVED)


def _releases(track) -> tuple:  # noqa: ANN001
    identity = track.identity
    if identity is None or not getattr(identity, "matched", False):
        return ()
    return tuple(identity.releases)


def _repeats(members: list[Member], tracks: dict) -> tuple[str, ...]:
    """Members whose identity is the same recording as another member's.

    All MusicBrainz gives back here is the recording a file is, so this is the
    only tracklist check available without asking for release detail nobody
    asked for. It is reported and never acted on: this module does not know
    whether the second one is a duplicate rip or a deliberate second copy.
    """
    seen: dict[str, list[str]] = {}
    for member in members:
        identity = getattr(tracks.get(member.relpath), "identity", None)
        recording = getattr(identity, "recording_id", "") if identity else ""
        if recording:
            seen.setdefault(recording, []).append(member.name)
    return tuple(
        sorted(name for names in seen.values() if len(names) > 1 for name in names)
    )


# --- the decision -------------------------------------------------------------------


def file_as(
    conn: sqlite3.Connection, settings: Settings, finding_id: int, dest_relpath: str
) -> int:
    """Send every supporting member to one album. Returns how many were set.

    It sets answers and nothing else — no plan, no move, no folder. What it
    writes is exactly what pressing the per-track button would have written
    eleven times, through the same function, so every rule that guards a
    per-track answer guards this one: a track may only be sent to a folder its
    own evidence named.
    """
    from librairy.corrections import CorrectionRefused, load_finding
    from librairy.track_filing import answer

    row = load_finding(conn, finding_id)
    found = aggregate(conn, settings, row)
    if found is None:
        raise CorrectionRefused("these tracks do not agree on one release")
    conclusion = next(
        (item for item in found.conclusions if item.relpath == dest_relpath), None
    )
    if conclusion is None:
        raise CorrectionRefused(
            "that is not a release these tracks agree on"
        )
    for member in conclusion.members:
        answer(conn, settings, finding_id, member.relpath, dest_relpath)
    return len(conclusion.members)


def leave_all(
    conn: sqlite3.Connection, settings: Settings, finding_id: int
) -> int:
    """Answer every open track with `Leave here`. Returns how many.

    A real answer for a shelf of singles, and the same answer the per-track
    control gives — which means no plan operation at all for any of them. It
    touches only tracks nobody has answered, so it cannot overwrite a
    destination somebody chose.
    """
    from librairy.corrections import CorrectionRefused, load_finding
    from librairy.track_filing import LEAVE, answer, plan_filing

    row = load_finding(conn, finding_id)
    view = plan_filing(conn, settings, row, verify=False)
    if view is None:
        raise CorrectionRefused("there is nothing left to file here")
    open_tracks = [track for track in view.tracks if not track.answered]
    for track in open_tracks:
        answer(conn, settings, finding_id, track.relpath, LEAVE)
    return len(open_tracks)

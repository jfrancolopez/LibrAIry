"""What a decision would do to the pairs LibrAIry already knows about.

Relationships became data two passes ago, and then stopped at Item Detail. That
left the knowledge in the one place where nothing happens: Browse and Search
could say `Live Photo video of IMG_1234.HEIC`, and the *last screen before
bytes move* described a file called `IMG_1234.MOV` and nothing else. The most
important context disappeared at the only checkpoint that mattered.

This module answers one question for an explicit set of planned operations:

    which relationships that exist right now does this decision touch,
    and what happens to each of them?

Three things it deliberately is not.

**It is not a rule that related files move together.** That claim is false, and
saying it globally would be worse than saying nothing. A RAW and its JPEG are
routinely split on purpose — keep the negative, send the render away — and a
program that refused, or that quietly dragged the RAW along, would be wrong
about the ordinary case. What the owner is owed is *being told*, not being
overruled.

**It is not a planner.** Nothing here adds, removes or edits an operation. It
reads a plan and returns sentences. A relationship may change what a page
explains and what choices it offers; it may never change what is in the plan
without somebody pressing something.

**It is not one rule for all six kinds.** Separation means different things:

    subtitle / lyrics / cue   a sidecar has to sit *beside* its file to work,
                              so leaving one behind in the old folder orphans
                              it even when both stay in the library
    raw_render / live_photo   established from capture metadata, so the pair
                              survives any amount of reorganisation; only
                              leaving the library separates them
    artwork                   belongs to a folder's release, not to track
                              five. "Setting aside one MP3 would separate
                              cover.jpg from the MP3" is nonsense, and the
                              only artwork fact worth reporting is the release
                              leaving *entirely*.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from librairy.relationships import (
    ARTWORK,
    CUE,
    KINDS,
    LIVE_PHOTO,
    LYRICS,
    RAW_RENDER,
    SUBTITLE,
)

#  A sidecar has to be found next to the file it describes. A player looks in
#  the same directory for `Movie.en.srt`; moved one folder up it is a file
#  nobody will ever see again. So for these, "together" means the same folder.
ADJACENT = frozenset({SUBTITLE, LYRICS, CUE})
#  Established from what the bytes record, not from where they sit. Reorganise
#  the whole photo library and a Live Photo is still a Live Photo. Only leaving
#  the library — quarantine, the delete queue, back to the inbox — separates
#  these.
PAIRED = frozenset({RAW_RENDER, LIVE_PHOTO})
#  Folder-scoped, and reported only when the whole release goes.
RELEASE = frozenset({ARTWORK})

MOVES_TOGETHER = "moves_together"
SPLIT = "split"
BOTH_REMAIN = "both_remain"
STALE = "stale"

STATES = (MOVES_TOGETHER, SPLIT, BOTH_REMAIN, STALE)

#  Order the kinds are reported in, so a summary reads the same twice.
KIND_ORDER = {kind: index for index, kind in enumerate(KINDS)}

# What one relationship of this kind is called, singular and plural.
PAIR_NAME = {
    SUBTITLE: ("subtitle", "subtitles"),
    LYRICS: ("lyrics file", "lyrics files"),
    CUE: ("cue sheet", "cue sheets"),
    ARTWORK: ("folder's artwork", "folders' artwork"),
    RAW_RENDER: ("RAW/JPEG pair", "RAW/JPEG pairs"),
    LIVE_PHOTO: ("Live Photo", "Live Photos"),
}

#  What the page says when a decision would pull one half away from the other.
#  Wording is per kind because the consequence is per kind: an orphaned
#  subtitle is a file that has stopped working, while a separated RAW/JPEG pair
#  is very often exactly what somebody meant to do.
SPLIT_HEADLINE = {
    SUBTITLE: "This will separate a subtitle from its video.",
    LYRICS: "This will separate a lyrics file from its track.",
    CUE: "This will separate a cue sheet from the audio it describes.",
    ARTWORK: "This will leave a folder's artwork with nothing it belongs to.",
    RAW_RENDER: "This will separate a RAW + JPEG pair.",
    LIVE_PHOTO: "This will separate a Live Photo.",
}

TOGETHER_HEADLINE = {
    SUBTITLE: "Subtitle and video move together.",
    LYRICS: "Lyrics and track move together.",
    CUE: "Cue sheet and audio move together.",
    ARTWORK: "Artwork moves with the release.",
    RAW_RENDER: "RAW/JPEG pair — both files move together.",
    LIVE_PHOTO: "Live Photo pair — both files move together.",
}

#  The same labels part-way through a sentence. A table rather than a rule,
#  because no rule gets both "its subtitle" and "its Live Photo video" right —
#  RAW and JPEG are acronyms and Live Photo is a product name, and a heuristic
#  clever enough to know that is a heuristic waiting to be wrong about the
#  seventh one.
MID_SENTENCE = {
    "Subtitle": "subtitle",
    "Video": "video",
    "Lyrics": "lyrics",
    "Track": "track",
    "Cue sheet": "cue sheet",
    "Audio": "audio",
    "Artwork": "artwork",
    "Release": "release",
    "JPEG render": "JPEG render",
    "RAW original": "RAW original",
    "Live Photo video": "Live Photo video",
    "Live Photo still": "Live Photo still",
}

# Where a decision can send a file, in a word somebody would use for it.
ROOT_LABEL = {
    "library": "the library",
    "quarantine": "Quarantine",
    "inbox": "the inbox",
}


@dataclass(frozen=True)
class Move:
    """One planned operation, reduced to what a relationship cares about.

    Not a plan operation: the same question is asked before a plan exists, on
    the comparison page and in Review, where the answer is what decides which
    buttons to draw.
    """

    item_id: int
    dest_root: str
    dest_relpath: str


@dataclass(frozen=True)
class Member:
    """One half of a relationship, and where this decision leaves it."""

    item_id: int
    relpath: str
    root: str
    fingerprint: str
    live: bool
    moves: bool
    dest_root: str
    dest_relpath: str

    @property
    def name(self) -> str:
        return self.relpath.rsplit("/", 1)[-1]

    @property
    def after_root(self) -> str:
        return self.dest_root if self.moves else self.root

    @property
    def after_relpath(self) -> str:
        return self.dest_relpath if self.moves else self.relpath

    @property
    def after_folder(self) -> str:
        parent = str(PurePosixPath(self.after_relpath).parent)
        return "" if parent == "." else parent

    @property
    def destination(self) -> str:
        """Where it ends up, said the way a card says it.

        The folder for the library and the inbox, because that is where
        somebody would go and look. Just "Quarantine" for quarantine: its
        internal date folders are LibrAIry's filing, not a place anybody
        thinks in, and naming one only makes the sentence longer.
        """
        label = ROOT_LABEL.get(self.after_root, self.after_root)
        folder = "" if self.after_root == "quarantine" else self.after_folder
        return f"{label}/{folder}" if folder else label


@dataclass(frozen=True)
class Touched:
    """One current relationship, and what this decision does to it."""

    kind: str
    state: str
    companion: Member
    subject: Member

    @property
    def key(self) -> tuple[str, int, int]:
        low, high = sorted((self.companion.item_id, self.subject.item_id))
        return (self.kind, low, high)

    @property
    def members(self) -> tuple[Member, Member]:
        return (self.subject, self.companion)

    @property
    def outside(self) -> Member | None:
        """The half this plan does not operate on, when there is one.

        The one the executor never checks, because it is not a source of any
        operation — and therefore the one whose disappearance would otherwise
        go unnoticed between approval and Commit.
        """
        staying = [member for member in self.members if not member.moves]
        return staying[0] if len(staying) == 1 else None

    @property
    def moving(self) -> list[Member]:
        return [member for member in self.members if member.moves]

    @property
    def headline(self) -> str:
        if self.state == SPLIT:
            return SPLIT_HEADLINE.get(self.kind, "This will separate two related files.")
        if self.state == MOVES_TOGETHER:
            return TOGETHER_HEADLINE.get(self.kind, "Both files move together.")
        return ""

    @property
    def detail(self) -> str:
        """The two half-sentences under the headline, or an empty string.

        Says what happens to each half by name. A warning that does not name
        the file staying behind is a warning somebody has to go and check.
        """
        if self.state == SPLIT:
            outside = self.outside
            moving = self.moving
            if outside is None or not moving:
                #  Both halves move, to places that do not keep them together.
                first, second = self.members
                return (
                    f"{first.name} goes to {first.destination}; "
                    f"{second.name} goes to {second.destination}."
                )
            names = ", ".join(member.name for member in moving)
            return (
                f"{names} goes to {moving[0].destination}; "
                f"{outside.name} stays in {outside.destination}."
            )
        if self.state == MOVES_TOGETHER:
            return ", ".join(member.name for member in self.members)
        return ""


@dataclass(frozen=True)
class Impact:
    """Every current relationship an explicit set of operations touches."""

    touched: list[Touched] = field(default_factory=list)

    @property
    def splits(self) -> list[Touched]:
        return [item for item in self.touched if item.state == SPLIT]

    @property
    def together(self) -> list[Touched]:
        return [item for item in self.touched if item.state == MOVES_TOGETHER]

    @property
    def any_split(self) -> bool:
        return bool(self.splits)

    @property
    def relevant(self) -> list[Touched]:
        """The ones worth showing: something happens to them.

        `both_remain` and `stale` are computed and recorded — the first so a
        card can say "nothing is being separated" and mean it, the second so a
        historical pair is never counted as current impact — but neither is a
        sentence anybody needs to read.
        """
        return [item for item in self.touched if item.state in (SPLIT, MOVES_TOGETHER)]

    def summary(self) -> list[str]:
        """Counts, not a list of every pair.

        A photo group can hold forty pairs, and a Commit card that renders all
        of them stops being a card. "1 RAW/JPEG pair will be split" is the
        number somebody needs to decide whether to look closer; Details is
        where looking closer happens.
        """
        counts: dict[tuple[str, str], int] = {}
        seen: set[tuple[str, int]] = set()
        for item in self.relevant:
            if item.kind in RELEASE:
                #  One release, one line — however many tracks agree on it.
                marker = (item.kind, item.companion.item_id)
                if marker in seen:
                    continue
                seen.add(marker)
            counts[(item.kind, item.state)] = counts.get((item.kind, item.state), 0) + 1
        lines: list[str] = []
        for (kind, state), count in sorted(
            counts.items(), key=lambda pair: (KIND_ORDER.get(pair[0][0], 99), pair[0][1])
        ):
            singular, plural = PAIR_NAME.get(kind, ("related pair", "related pairs"))
            noun = singular if count == 1 else plural
            verb = (
                ("stays together" if count == 1 else "stay together")
                if state == MOVES_TOGETHER
                else ("will be split" if count == 1 else "will be split")
            )
            lines.append(f"{count} {noun} {verb}")
        return lines


def assess(conn: sqlite3.Connection, moves: list[Move]) -> Impact:
    """Which live relationships these operations touch, and how.

    Four queries whatever the size of the decision: the relationships, their
    members, the artwork fan-out, and nothing per operation. A plan over five
    hundred files asks the database the same number of questions as a plan over
    one, which is the only way this can live on the Commit page.
    """
    planned = {move.item_id: move for move in moves if move.item_id is not None}
    if not planned:
        return Impact([])
    rows = _relationship_rows(conn, list(planned))
    if not rows:
        return Impact([])
    member_ids = {int(row["low_item_id"]) for row in rows}
    member_ids |= {int(row["high_item_id"]) for row in rows}
    facts = _item_facts(conn, sorted(member_ids))
    orphaned = _orphaned_artwork(conn, rows, planned, facts)

    touched: list[Touched] = []
    for row in rows:
        kind = str(row["kind"])
        companion_id = int(row["companion_item_id"])
        low, high = int(row["low_item_id"]), int(row["high_item_id"])
        subject_id = high if companion_id == low else low
        companion = _member(companion_id, facts, planned)
        subject = _member(subject_id, facts, planned)
        if companion is None or subject is None:
            continue
        touched.append(
            Touched(
                kind=kind,
                state=_state(kind, companion, subject, orphaned=orphaned),
                companion=companion,
                subject=subject,
            )
        )
    touched.sort(key=lambda item: (KIND_ORDER.get(item.kind, 99), item.subject.relpath))
    return Impact(touched)


def for_plan(conn: sqlite3.Connection, plan_id: str) -> Impact:
    """The same question, asked of a plan that already exists."""
    return assess(conn, plan_moves(conn, plan_id))


def plan_moves(conn: sqlite3.Connection, plan_id: str) -> list[Move]:
    """A plan's operations, reduced to the item each one moves and where.

    Operations without an item are skipped rather than guessed at. The only
    ones are optimization sources — generated files under appdata that have no
    `items` row by construction, and therefore no relationship either.
    """
    rows = conn.execute(
        "SELECT item_id, dest_root, dest_relpath FROM plan_ops"
        " WHERE plan_id=? AND item_id IS NOT NULL ORDER BY seq",
        (plan_id,),
    ).fetchall()
    return [
        Move(
            item_id=int(row["item_id"]),
            dest_root=str(row["dest_root"]),
            dest_relpath=str(row["dest_relpath"]),
        )
        for row in rows
    ]


def _relationship_rows(
    conn: sqlite3.Connection, item_ids: list[int]
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(item_ids))
    return conn.execute(
        f"""
        SELECT id, kind, low_item_id, high_item_id, companion_item_id
        FROM item_relationships
        WHERE low_item_id IN ({placeholders}) OR high_item_id IN ({placeholders})
        ORDER BY kind, id
        """,  # noqa: S608 - placeholders are counted from the id list
        [*item_ids, *item_ids],
    ).fetchall()


def _item_facts(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, sqlite3.Row]:
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"SELECT id, root, relpath, fingerprint, missing_since FROM items"  # noqa: S608
        f" WHERE id IN ({placeholders})",
        item_ids,
    ).fetchall()
    return {int(row["id"]): row for row in rows}


def _member(
    item_id: int, facts: dict[int, sqlite3.Row], planned: dict[int, Move]
) -> Member | None:
    row = facts.get(item_id)
    if row is None:
        return None
    move = planned.get(item_id)
    return Member(
        item_id=item_id,
        relpath=str(row["relpath"]),
        root=str(row["root"]),
        fingerprint=str(row["fingerprint"] or ""),
        live=row["missing_since"] is None,
        moves=move is not None,
        dest_root=move.dest_root if move else "",
        dest_relpath=move.dest_relpath if move else "",
    )


def _place(kind: str, member: Member, *, after: bool) -> tuple[str, ...]:
    """Where a member counts as being, for this kind of relationship.

    The whole of the per-kind semantics is here. A pair established from
    capture metadata is together anywhere in the library; a sidecar is together
    only in the same directory.
    """
    root = member.after_root if after else member.root
    if kind in PAIRED:
        return (root,)
    relpath = member.after_relpath if after else member.relpath
    parent = str(PurePosixPath(relpath).parent)
    return (root, "" if parent == "." else parent)


def _state(
    kind: str, companion: Member, subject: Member, *, orphaned: set[int]
) -> str:
    """One relationship's fate under this decision.

    `stale` first, and it is not a warning: a relationship whose other half is
    no longer in the library is a record of something that used to be true, and
    counting it as current impact would have Commit warn about separating a
    file from one that is not there.

    A pair that is *already* apart is not split by this decision either. The
    sentence being offered is "this will separate them", and it has to be true
    for that to be worth interrupting somebody with.
    """
    if not companion.live or not subject.live:
        return STALE
    if kind in RELEASE:
        #  A release loses its artwork only when the last of its media leaves.
        #  Anything short of that is the nonsense sentence this rule exists to
        #  prevent — twelve tracks in the folder, one set aside, and a warning
        #  that the cover has been separated from track five.
        if companion.item_id in orphaned:
            return SPLIT
        return MOVES_TOGETHER if companion.moves and subject.moves else BOTH_REMAIN
    before = (_place(kind, companion, after=False), _place(kind, subject, after=False))
    after = (_place(kind, companion, after=True), _place(kind, subject, after=True))
    if after[0] == after[1]:
        return MOVES_TOGETHER if companion.moves and subject.moves else BOTH_REMAIN
    if before[0] != before[1]:
        return BOTH_REMAIN
    return SPLIT


def _orphaned_artwork(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    planned: dict[int, Move],
    facts: dict[int, sqlite3.Row],
) -> set[int]:
    """Artwork items every one of whose live media this decision takes away.

    One query for all of them. The artwork itself moving with the release is
    not an orphan — that is the album being filed, which is the ordinary case
    and the one nobody needs told about.
    """
    artwork_ids = {
        int(row["companion_item_id"]) for row in rows if str(row["kind"]) == ARTWORK
    }
    if not artwork_ids:
        return set()
    placeholders = ",".join("?" * len(artwork_ids))
    members = conn.execute(
        f"""
        SELECT r.companion_item_id AS artwork_id,
               CASE WHEN r.low_item_id = r.companion_item_id
                    THEN r.high_item_id ELSE r.low_item_id END AS media_id
        FROM item_relationships r
        WHERE r.kind = ? AND r.companion_item_id IN ({placeholders})
        """,  # noqa: S608 - placeholders are counted from the id set
        (ARTWORK, *sorted(artwork_ids)),
    ).fetchall()
    extra = {int(row["media_id"]) for row in members} - set(facts)
    if extra:
        facts = {**facts, **_item_facts(conn, sorted(extra))}
    remaining: dict[int, int] = dict.fromkeys(artwork_ids, 0)
    leaving: dict[int, int] = dict.fromkeys(artwork_ids, 0)
    for row in members:
        artwork_id, media_id = int(row["artwork_id"]), int(row["media_id"])
        fact = facts.get(media_id)
        if fact is None or fact["missing_since"] is not None:
            continue
        remaining[artwork_id] += 1
        artwork = _member(artwork_id, facts, planned)
        media = _member(media_id, facts, planned)
        if artwork is None or media is None:
            continue
        if _place(ARTWORK, media, after=True) != _place(ARTWORK, artwork, after=True):
            leaving[artwork_id] += 1
    return {
        artwork_id
        for artwork_id, total in remaining.items()
        if total and leaving[artwork_id] == total
    }


#  Why an approval no longer means what it said. Each one is a sentence a
#  person reads on the Commit card, so each says what changed rather than
#  naming a state.
DRIFT_GONE = "a related file is no longer there"
DRIFT_CHANGED = "a related file has changed since this was approved"
DRIFT_NEW = "a new related file has appeared since this was approved"


def snapshot(conn: sqlite3.Connection, plan_id: str) -> Impact:
    """Freeze the relationships this plan touches, at approval.

    Called once, from `approve_plan`. What it records is small on purpose: the
    pair, its kind, the state the person was shown, and — for the half that is
    *not* an operation — enough to notice it changing. Every member that is an
    operation already has its fingerprint verified at execution, so recording
    it again would be a second copy of a fact that is already checked.

    `plans.relationships_checked` is set whether or not anything was found.
    That flag is the whole of the compatibility story: a plan approved before
    this existed has 0 and keeps its old semantics exactly, and a plan approved
    now that touches nothing has 1 and an empty snapshot — which is a different
    thing from never having looked.
    """
    from librairy.planner import utc_now

    impact = for_plan(conn, plan_id)
    now = utc_now()
    for item in impact.touched:
        if item.state == STALE:
            #  A pair whose other half is already gone is not context for this
            #  decision, so freezing it would only manufacture drift later.
            continue
        outside = item.outside
        low, high = sorted((item.companion.item_id, item.subject.item_id))
        conn.execute(
            """
            INSERT INTO plan_relationships
              (plan_id, kind, low_item_id, high_item_id, state,
               outside_item_id, outside_fingerprint, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (plan_id, low_item_id, high_item_id, kind) DO NOTHING
            """,
            (
                plan_id,
                item.kind,
                low,
                high,
                item.state,
                outside.item_id if outside else None,
                (outside.fingerprint or None) if outside else None,
                now,
            ),
        )
    conn.execute("UPDATE plans SET relationships_checked=1 WHERE id=?", (plan_id,))
    return impact


def snapshot_rows(conn: sqlite3.Connection, plan_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM plan_relationships WHERE plan_id=? ORDER BY kind, low_item_id",
        (plan_id,),
    ).fetchall()


def was_checked(conn: sqlite3.Connection, plan_id: str) -> bool:
    row = conn.execute(
        "SELECT relationships_checked FROM plans WHERE id=?", (plan_id,)
    ).fetchone()
    return bool(row and row["relationships_checked"])


def drift(conn: sqlite3.Connection, plan_id: str) -> str:
    """Does this approval still mean what it said about related files?

    The plan itself cannot have changed — it is immutable and hash-checked. The
    files it does *not* contain can, and those are exactly the ones the warning
    was about. Three things make the sentence untrue:

    * the half that was going to stay behind is gone
    * that half is different bytes than it was
    * a relationship now exists that this decision would split, and nobody was
      shown it

    A relationship *disappearing* is deliberately not drift. If metadata is
    corrected and two photos turn out not to be a pair after all, every
    operation in the plan is still exactly the operation that was approved, on
    exactly the files it named — the warning simply turned out to be
    unnecessary. Refusing there would make a correction to the catalogue cancel
    a decision about the filesystem.

    Returns "" when nothing material changed. Plans approved before
    relationships were understood return "" always: see `snapshot`.
    """
    if not was_checked(conn, plan_id):
        return ""
    before = snapshot_rows(conn, plan_id)
    outside_ids = [
        int(row["outside_item_id"])
        for row in before
        if row["outside_item_id"] is not None
    ]
    facts = _item_facts(conn, outside_ids) if outside_ids else {}
    for row in before:
        if row["outside_item_id"] is None:
            continue
        fact = facts.get(int(row["outside_item_id"]))
        if fact is None or fact["missing_since"] is not None:
            return DRIFT_GONE
        recorded = row["outside_fingerprint"]
        if recorded and str(fact["fingerprint"] or "") != str(recorded):
            return DRIFT_CHANGED
    known = {(str(row["kind"]), int(row["low_item_id"]), int(row["high_item_id"]))
             for row in before}
    for item in for_plan(conn, plan_id).splits:
        if item.key not in known:
            return DRIFT_NEW
    return ""


#  How many relationships one card names before it starts counting instead.
#
#  A photo group can hold forty pairs. A card that renders all of them is not a
#  card any more, and the reader loses the decision in the evidence for it — so
#  the summary counts, a bounded few are named, and Details is where the rest
#  lives. Truncation is always *said*, never silent.
SHOWN = 3


def distinct(items: list[Touched]) -> list[Touched]:
    """One entry per thing, not one per relationship row.

    Twelve tracks agreeing on one cover is twelve rows and one fact.
    """
    seen: set[int] = set()
    kept: list[Touched] = []
    for item in items:
        if item.kind in RELEASE:
            if item.companion.item_id in seen:
                continue
            seen.add(item.companion.item_id)
        kept.append(item)
    return kept


def card(impact: Impact, *, limit: int = SHOWN) -> dict[str, object] | None:
    """One bounded shape, for every surface that has to show this.

    Commit, Quarantine and the comparison page all answer the same question and
    should not each grow their own arithmetic for it.
    """
    splits = distinct(impact.splits)
    together = distinct(impact.together)
    if not splits and not together:
        return None
    return {
        "summary": impact.summary(),
        "splits": [
            {"headline": item.headline, "detail": item.detail} for item in splits[:limit]
        ],
        "splits_more": max(0, len(splits) - limit),
        "together": [
            {"headline": item.headline, "detail": item.detail}
            for item in together[:limit]
        ],
        "together_more": max(0, len(together) - limit),
        "any_split": bool(splits),
    }


def not_carried(
    conn: sqlite3.Connection, *, replaced_item_id: int, replacing_item_id: int
) -> list[str]:
    """Relationships the outgoing file has that the incoming one does not.

    A replacement swaps two representations of one thing. It does **not**
    transfer what was established about the bytes being replaced — a JPEG
    paired with a RAW by capture metadata was paired because *those* bytes
    recorded that camera at that moment, and a different export of the same
    photograph has to earn the same pairing from its own metadata or not have
    it.

    That invariant already held, silently, by nobody writing the code that
    would have broken it. This is the sentence that makes it visible, because
    "the pair quietly disappeared" and "the pair was deliberately not assumed"
    look identical from the outside.
    """
    from librairy.relationships import LABEL, SUBJECT_LABEL, for_items

    found = for_items(conn, [replaced_item_id, replacing_item_id])
    kept = {(item.kind, item.item_id) for item in found.get(replacing_item_id, [])}
    lines: list[str] = []
    for item in found.get(replaced_item_id, []):
        if (item.kind, item.item_id) in kept:
            continue
        #  What the *replaced* file is to the other one. `Related.label` answers
        #  for the other half, which is the opposite of what this sentence
        #  needs — printing it would call the RAW a RAW while claiming to
        #  describe the JPEG.
        role = (
            SUBJECT_LABEL.get(item.kind, item.kind)
            if item.companion
            else LABEL.get(item.kind, item.kind)
        )
        lines.append(
            f"{item.name} — the version being replaced is its"
            f" {MID_SENTENCE.get(role, role.lower())};"
            f" the new file is not paired with it."
        )
    return lines


def if_set_aside(conn: sqlite3.Connection, item_ids: list[int]) -> Impact:
    """What setting exactly these files aside would do to known pairs.

    Asked *before* a plan exists, because that is when the answer can still
    change what somebody chooses. The destination is only ever read for which
    root it is, so the basename is enough here — a comparison has not decided
    on a quarantine filename yet and inventing one would look like a promise.
    """
    if not item_ids:
        return Impact([])
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"SELECT id, relpath FROM items WHERE id IN ({placeholders})",  # noqa: S608
        item_ids,
    ).fetchall()
    return assess(
        conn,
        [
            Move(
                item_id=int(row["id"]),
                dest_root="quarantine",
                dest_relpath=str(row["relpath"]).rsplit("/", 1)[-1],
            )
            for row in rows
        ],
    )

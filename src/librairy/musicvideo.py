"""Reading a DJ filename, and knowing a version from a duplicate.

A DJ collection breaks the assumption every other part of LibrAIry makes about
music: that a song is a thing, and two files with the same artist and title are
the same file. In a video pool they are usually not. `In Da Club (Clean)` and
`In Da Club (Dirty)` are both wanted, both kept, and quarantining either one is
the single worst thing this software could do to a working collection.

So **the version is part of the identity**, and this module has two jobs:

* pull `artist`, `title` and `version` out of names that were written by
  twenty different pools over twenty years, deterministically and without
  guessing;
* answer "are these the same recording?" separately from "are these the same
  file?", so the duplicate detector can tell a second download from a
  legitimate edit.

Deliberately no regex zoo. There are four or five shapes that cover most of a
real collection, and a long tail that no pattern will ever catch — so the
parser reports what it is confident about, leaves the rest empty, and says so.
Something upstream can ask a model about the remainder; inventing an artist
from a filename nobody can read is worse than admitting the file is unknown.

Nothing here sanitises. `naming.slugify_filename` is the one naming policy and
this module never writes a path — it reads names and returns fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# The vocabulary a DJ collection actually uses. Grouped by what the token says
# about the file, because the groups are what Browse will eventually filter on
# and what the duplicate rules care about — an `(Acapella)` and an
# `(Instrumental)` of one song are as different as two songs.
VERSION_TOKENS: dict[str, tuple[str, ...]] = {
    "content": (
        "clean", "dirty", "explicit", "censored", "uncensored",
    ),
    "length": (
        "radio edit", "radio", "extended mix", "extended", "short edit",
        "full length", "quick hitter", "quickie",
    ),
    "structure": (
        "intro edit", "intro", "outro edit", "outro", "transition",
        "acapella", "acappella", "instrumental", "karaoke",
    ),
    "arrangement": (
        "club mix", "club edit", "remix", "re-edit", "reedit", "edit",
        "redrum", "re-drum", "bootleg", "mashup", "mash-up", "refix",
        "vip mix", "dub", "dub mix",
    ),
    "source": (
        "live", "remastered", "video edit", "lyric video", "official video",
        "music video", "visualizer",
    ),
}
# Longest first, so `extended mix` is not matched as `extended` and
# `intro edit` is not matched as `intro`.
_ALL_TOKENS = sorted(
    ((token, group) for group, tokens in VERSION_TOKENS.items() for token in tokens),
    key=lambda pair: -len(pair[0]),
)

# How a credit joins two artists. Order matters for the same reason.
FEATURE_MARKERS = ("featuring", "feat.", "feat", "ft.", "ft", "with")
JOINT_MARKERS = (" vs. ", " vs ", " versus ", " & ", " and ", " x ", " X ")

# Bracket styles a pool might use around the version.
_BRACKETED = re.compile(r"[(\[{]([^)\]}]+)[)\]}]")
# `01 - Artist - Title`, `01. Artist - Title`, `01 Artist - Title`.
#
# Two shapes, and the distinction is load-bearing: a track number is either
# followed by punctuation, or zero-padded. Neither is true of `50 Cent`, and
# an earlier version of this pattern filed him as `Cent`.
_LEADING_NUMBER = re.compile(r"^\s*(?:\d{1,3}\s*[-._)]\s*|0\d{1,2}\s+)")

# Version words that are safe to read without brackets, because no song is
# called them. `Live`, `Dub` and `Edit` are deliberately absent: *Live and Let
# Die* is a title, not a live recording, and there is no way to tell from the
# name alone.
_TRAILING_TOKENS = (
    "extended mix", "extended", "radio edit", "club mix", "intro edit",
    "outro edit", "acapella", "acappella", "instrumental", "clean",
    "dirty", "explicit",
)
_TRAILING = re.compile(
    r"\s+(" + "|".join(re.escape(token) for token in _TRAILING_TOKENS) + r")\s*$",
    re.IGNORECASE,
)
# A separator with space around it is a real separator; a hyphen inside a word
# is part of the word. `Jay-Z - Song` must not split at `Jay-Z`.
_SEPARATOR = re.compile(r"\s+[-–—]\s+|\s+_\s+")


@dataclass(frozen=True)
class ParsedName:
    """What a filename could be read to say. Empty fields mean "not said"."""

    primary_artist: str = ""
    featured_artists: tuple[str, ...] = ()
    credited_artist: str = ""
    title: str = ""
    versions: tuple[str, ...] = ()
    version_groups: tuple[str, ...] = ()
    remixer: str = ""
    confident: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def work_key(self) -> str:
        """What song this is, ignoring which version of it.

        The identity two files share when they are the same recording in
        different edits. Empty when the name could not be read, and an empty
        key never matches anything — an unparsed file is not "the same song"
        as another unparsed file.
        """
        if not self.primary_artist or not self.title:
            return ""
        return f"{_key(self.primary_artist)}|{_key(self.title)}"

    @property
    def version_key(self) -> str:
        """The specific file. Two of these matching is a real duplicate claim."""
        if not self.work_key:
            return ""
        return f"{self.work_key}|{'+'.join(sorted(self.versions))}"


def parse(filename: str) -> ParsedName:
    """Read a DJ filename. Confident only when the shape is unambiguous."""
    stem = _stem(filename)
    if not stem.strip():
        return ParsedName(notes=("the name is empty",))

    body, versions, groups = _take_versions(stem)
    body = _LEADING_NUMBER.sub("", body).strip()
    credit, title, sure = _split_credit(body)
    if not title:
        # No separator anywhere. `song_final.mp4` says nothing about who made
        # it, and a title-cased guess would become a folder that outlives the
        # guess.
        return ParsedName(
            title=_tidy(body),
            versions=versions,
            version_groups=groups,
            notes=("no artist/title separator found",),
        )

    # A version word at the end of an unbracketed title: `TITANIUM EXTENDED`.
    # Pools write these constantly and losing them would merge two files.
    title, versions, groups = _take_trailing(title, versions, groups)
    primary, featured = _split_credit_parts(credit)
    remixer = _remixer(versions)
    return ParsedName(
        primary_artist=primary,
        featured_artists=featured,
        credited_artist=_tidy(credit),
        title=_tidy(title),
        versions=versions,
        version_groups=groups,
        remixer=remixer,
        confident=bool(primary and title and sure),
        notes=() if sure else ("split on underscores, which is a guess",),
    )


def _take_trailing(
    title: str, versions: tuple[str, ...], groups: tuple[str, ...]
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Repeatedly strip a trailing version word. `Song Extended Clean` is both.

    Only from `_TRAILING_TOKENS`, and only at the end — a title that *starts*
    with one of these words is a title.
    """
    found = list(versions)
    seen = list(groups)
    while (match := _TRAILING.search(title)) and len(title) > len(match.group(0)):
        label, group = _classify(match.group(1))
        if label is None:  # pragma: no cover - every token is classifiable
            break
        found.insert(0, label)
        if group and group not in seen:
            seen.append(group)
        title = title[: match.start()].strip()
    return title, tuple(found), tuple(seen)


def _stem(filename: str) -> str:
    """Drop a file extension, and only a plausible one.

    `Artist - Song (Extended Mix).mp4` loses `.mp4`; `Artist - Song vol.2`
    keeps everything, because `.2` is not an extension.
    """
    head, dot, tail = filename.rpartition(".")
    if dot and 1 <= len(tail) <= 5 and tail.isalnum() and not tail.isdigit():
        return head
    return filename


def _take_versions(stem: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Pull every bracketed version marker out, leaving the rest of the name.

    Only bracketed text is considered, and only when it is a known token. A
    bare word is left alone: `Artist - Live and Let Die` is a song, not a live
    recording, and there is no way to tell those apart without the brackets.
    """
    found: list[str] = []
    groups: list[str] = []

    def take(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        label, group = _classify(inner)
        if label is None:
            return match.group(0)
        found.append(label)
        if group not in groups:
            groups.append(group)
        return " "

    body = _BRACKETED.sub(take, stem)
    return body, tuple(found), tuple(groups)


def _classify(inner: str) -> tuple[str | None, str]:
    """`Extended Mix` -> version. `Tiesto Remix` -> version, with a remixer.

    The whole bracketed phrase is kept rather than the token alone, because
    `Tiesto Remix` and `Armand Van Helden Remix` are different files and
    reducing both to `remix` would make them look like one.
    """
    lowered = inner.casefold()
    for token, group in _ALL_TOKENS:
        if lowered == token or lowered.endswith(f" {token}") or lowered.startswith(f"{token} "):
            return _tidy(inner), group
    return None, ""


def _remixer(versions: tuple[str, ...]) -> str:
    """`Tiesto Remix` -> `Tiesto`. Named, but never made into a folder.

    A remix has a real second author worth recording. It does not get a
    directory: `Music Videos/House/Tiesto/` for someone else's song filed
    under the remixer would put the track where nobody looks for it.
    """
    for version in versions:
        lowered = version.casefold()
        # `edit` is not on this list on purpose. `DJ Intro Edit` is a
        # structural version, and reading it as "a remix by DJ Intro" invents
        # a person — which then becomes an artist folder.
        for token in ("remix", "re-edit", "bootleg", "mashup", "mash-up", "refix"):
            if lowered.endswith(f" {token}"):
                candidate = version[: -len(token)].strip()
                if _classify(candidate)[0] is None:
                    return candidate
    return ""


def _split_credit(body: str) -> tuple[str, str, bool]:
    """Artist and title, at the first real separator, and whether it is sure.

    First and not last: `Artist - Song - Something` is far more often an
    artist and a two-part title than a two-part artist and a title.

    The underscore form is the third value's whole reason. `Artist_Title_Extended`
    and `song_clean_final` are the same shape, and only one of them names an
    artist — so the split is made and marked unsure, rather than made
    confidently or refused. An unsure parse is not allowed to become a folder;
    it goes to Review, or to a model, as the file nobody could read.
    """
    parts = _SEPARATOR.split(body, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip(), True
    if "_" in body and " " not in body:
        head, _, tail = body.partition("_")
        if head.strip() and tail.strip():
            return head.replace("_", " ").strip(), tail.replace("_", " ").strip(), False
    return body, "", False


def _split_credit_parts(credit: str) -> tuple[str, tuple[str, ...]]:
    """The primary artist, and everyone else named in the credit.

    A featured artist is a guest on somebody's record, so the primary is the
    name before the marker. Joint billing is different — `Calvin Harris & Dua
    Lipa` is one act for filing purposes — and the first name is taken as the
    primary because the physical hierarchy needs to be stable more than it
    needs to be fair. The full credit is preserved either way.
    """
    lowered = credit.casefold()
    for marker in FEATURE_MARKERS:
        for pattern in (f" {marker} ",):
            index = lowered.find(pattern)
            if index > 0:
                primary = credit[:index].strip()
                rest = credit[index + len(pattern) :].strip()
                return _tidy(primary), _split_names(rest)
    for marker in JOINT_MARKERS:
        index = lowered.find(marker.casefold())
        if index > 0:
            return _tidy(credit[:index].strip()), _split_names(credit[index + len(marker) :])
    return _tidy(credit), ()


def _split_names(text: str) -> tuple[str, ...]:
    names = re.split(r"\s*(?:,|&| and | x | vs\.? )\s*", text, flags=re.IGNORECASE)
    return tuple(_tidy(name) for name in names if name.strip())


def _tidy(text: str) -> str:
    """Whitespace only. Sanitisation is `naming`'s job and happens later."""
    return re.sub(r"\s+", " ", text.replace("_", " ")).strip(" -_.")


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


# --- duplicates versus versions ------------------------------------------------

DUPLICATE = "duplicate"
POSSIBLE = "possible-duplicate"
RELATED = "related-version"
UNRELATED = "unrelated"


def relationship(
    left: ParsedName,
    right: ParsedName,
    *,
    same_bytes: bool = False,
    similar: bool = False,
) -> str:
    """How two music video files are related, in the order the answers cost.

    Identical bytes is the only claim that needs no interpretation, so it is
    checked first and outranks everything — two files with the same hash are
    the same file whatever their names say.

    Below that, the rule this module exists for: **matching artist and title
    is not enough**. `In Da Club (Clean)` and `In Da Club (Dirty)` share a
    work key and differ in version, and calling that a duplicate would offer
    to quarantine half a working DJ collection.
    """
    if same_bytes:
        return DUPLICATE
    if not left.work_key or not right.work_key or left.work_key != right.work_key:
        return POSSIBLE if similar else UNRELATED
    if set(left.versions) != set(right.versions):
        return RELATED
    return POSSIBLE if similar else RELATED


def group_versions(parsed: dict[str, ParsedName]) -> dict[str, list[str]]:
    """Files grouped by which song they are, ignoring which version.

    The shape a future Browse needs — `Titanium` with `Original`, `Clean`,
    `Extended Mix` underneath it — without any of that becoming a folder.
    Names that could not be read are left out rather than piled into one
    group: unknown is not a song.
    """
    groups: dict[str, list[str]] = {}
    for relpath, name in sorted(parsed.items()):
        if not name.work_key:
            continue
        groups.setdefault(name.work_key, []).append(relpath)
    return groups


def version_label(name: ParsedName) -> str:
    """What to call this version in a list. `Original` when it is unmarked."""
    return " · ".join(name.versions) if name.versions else "Original"

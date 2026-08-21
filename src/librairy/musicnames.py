"""What LibrAIry calls a track it has just identified, and how it reads it back.

For most of what this software files, the filename is a label: nobody opens
`Photos/2024/August/IMG_5150.jpeg` expecting to learn anything from the name,
so house style — every space a dash, every apostrophe gone — costs nothing.

Music is the second category where that is wrong, and it is wrong for a
different reason than music videos were. A music video's name is *parsed*: the
identity lives in the filename because there is no folder layer to carry it. An
album track's identity is already in the folders — `Music/Rock/Queen/A Night at
the Opera/` says the artist and the album — and what is left in the filename is
the one thing a person actually reads while looking for a song:

    01-Death-on-Two-Legs.flac        what LibrAIry used to write
    01 - Death on Two Legs.flac      what it writes now

Both are safe. The first is safe *and unreadable*, and it is unreadable in
exactly the place a reader is looking: a music player's track list, a file
manager sorted by name, a CD burned for the car. `Don't Stop Me Now` and
`Rock 'n' Roll` lose their apostrophes, `You're My Best Friend` becomes
`Youre-My-Best-Friend`, and none of that damage bought any safety —
`naming.media_filename` already refuses everything a filesystem, a shell or an
SMB share objects to.

So this module owns one grammar, in both directions:

    [<disc>-]<track> - <title><ext>

    01 - Death on Two Legs.flac
    11 - Bohemian Rhapsody.flac
    1-01 - Song.flac                 only when the disc matters
    Death on Two Legs.flac           only when there is no track number

`parse` reads that back, and reads no further than the grammar promises. Track
number and title are recoverable from a name LibrAIry wrote; artist and album
are not in the name and are not guessed from it — they are the folders the file
is sitting in, and asking a filename for them is how a track called
`Live and Let Die` becomes a live recording.

Not the music-video formatter, and deliberately not sharing code with it.
`Artist - Title (Version).ext` exists because a DJ pool has no folder layer and
`(Clean)` and `(Dirty)` are two files worth keeping. An album track has both
layers, so repeating the artist in every filename would be noise inside a
folder already named after them. Related ideas, distinct formatters — see
`musicvideo.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from librairy.naming import media_filename

# What separates the number from the title. A dash with spaces around it, which
# is what a tagger, a ripper and a person all write, and what makes the number
# read as a number rather than as the first word of the title.
SEPARATOR = " - "
# `1-01 - Song`: the disc, then the track. Only written when the disc is
# meaningful — see `canonical_name`.
DISC_SEPARATOR = "-"

# The grammar above and nothing else. Anchored, so a name that does not follow
# it is reported as unparsed rather than half-read: this answers "did LibrAIry
# write this?", and a maybe is worth nothing.
_PARSED = re.compile(r"^(?:(\d{1,2})-)?(\d{1,3})\s-\s(?P<title>\S.*)$")


@dataclass(frozen=True)
class ParsedTrack:
    """What a filename following the grammar says. Empty fields mean "not said"."""

    track: int = 0
    disc: int = 0
    title: str = ""

    @property
    def parsed(self) -> bool:
        return bool(self.title)


def canonical_name(
    title: str, ext: str = "", *, track: int = 0, disc: int = 0, discs: int = 0
) -> str:
    """The filename for one track, readable and safe in that order.

    The disc number is written when it distinguishes something and left out
    when it does not: a single-disc album whose tags happen to say `1/1` gets
    `01 - Song.flac`, because `1-01 - Song.flac` is a prefix that answers a
    question nobody asked. Two discs, or a track on disc two, and it is back.

    Missing track number is not an error and not a `00`. A single, a stray, a
    file whose tags never had one: the title is the whole name, which is also
    what `parse` will read back off it.
    """
    stem = _stem(title, track=track, disc=disc, discs=discs)
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    return media_filename(f"{stem}{ext}", fallback="untitled")


def _stem(title: str, *, track: int, disc: int, discs: int) -> str:
    clean = " ".join(str(title or "").split()) or "Untitled"
    if track <= 0:
        return clean
    numbered = f"{track:02d}{SEPARATOR}{clean}"
    if disc > 1 or (disc > 0 and discs > 1):
        return f"{disc}{DISC_SEPARATOR}{numbered}"
    return numbered


def parse(name: str) -> ParsedTrack:
    """Track number, disc and title, off a name that follows the grammar.

    Round-trip and nothing more. A numbered name gives back the number and the
    title it was built from; an unnumbered one gives back track `0` and the
    stem, which is exactly what `canonical_name` writes for a track whose tags
    never had a number. Neither branch guesses: `track_final2.mp3` is reported
    as a track called `track_final2`, not as track 2, because the digit at the
    end of a name is not a track number and treating it as one is how a library
    ends up renumbered by a parser.

    Artist and album are never here. They are the folders above the file.
    """
    stem = name.rsplit(".", 1)[0] if "." in name[1:] else name
    match = _PARSED.match(stem.strip())
    if match is None:
        stem = stem.strip()
        return ParsedTrack(title=stem) if stem and not stem.isdigit() else ParsedTrack()
    return ParsedTrack(
        track=int(match.group(2)),
        disc=int(match.group(1) or 0),
        title=match.group("title").strip(),
    )

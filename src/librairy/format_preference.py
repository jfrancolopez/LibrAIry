"""Which music format this library's owner actually wants to keep.

    Music/Rock/Queen/A Night at the Opera/
        01 - Death on Two Legs.flac    31 MB
        01 - Death on Two Legs.mp3      7 MB

`similar_media` refuses to answer this, and it is right to: lossless is bigger
and you may be filling a phone, and nothing measurable says which of those
matters more. But "nothing measurable says" is not the same as "nobody has
said". The owner of this library has said **MP3**, and a program that makes
them press the same button on every album they own is not being neutral, it is
being forgetful.

So this is a **preference**, stated in exactly those terms:

    Preferred for your music library: MP3

and never in these:

    MP3 is better · higher quality · recommended by LibrAIry

The distinction is the whole reason this module exists separately from the
comparison code. A hidden `suffix == ".mp3"` inside `similar_media` would be a
quality claim wearing a rule's clothes, unreadable and untestable. This is one
named setting, one key in the settings table, and one function everything else
asks.

**What a preference is allowed to do.** It preselects, it labels, and it stops
there. Nothing here moves, deletes, renames, transcodes or flips a file, and no
plan comes into being because a row rendered. Every decision still goes through
Approve and Commit, and the preference can be overridden on any single one of
them — it is the starting point of an answer, not the answer.

**What it is not allowed to touch.** Identity outranks format, always. The
preference applies only where LibrAIry already knows two files are the same
recording, and "the same recording" is a fact from a catalog identity or from
the library's own naming — never from a format. A live MP3 and a studio FLAC
are two recordings and stay two recordings; a 2011 remaster is not the album;
an acoustic take is its own take. And two MP3s of one recording have no
preferred one, because the preference is about formats and both of them are
the format.

It also creates nothing. If the only copy is a FLAC, there is no MP3 to prefer
and nothing happens — no conversion, no ffmpeg, no optimization job, no
`Convert to MP3` offer. Preferring a format LibrAIry has is not the same as
manufacturing one it does not.
"""

from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath

#  Where the value lives now. It used to be a `settings` row keyed
#  `music.preferred_format`, and migration 044 moved it into the central
#  Format Policy as the `music` category scope — one authoritative value, read
#  through one resolver, so a Settings page and a comparison row cannot
#  disagree about what the owner said. This module stayed: it is the *music*
#  vocabulary — which extensions are music, what they are called on screen, and
#  when a preference may be applied at all — and none of that belongs in a
#  general policy resolver.
CATEGORY = "music"

#  What the migration seeds when nothing had been configured. A default rather
#  than a hard-coded rule: the policy is what decides, and this is what it says
#  when nobody has changed it.
DEFAULT = "mp3"

#  Formats the preference can be *about*. Anything outside this is not a music
#  representation question, and a file whose extension is not here never counts
#  as an alternative to be set aside.
MUSIC_FORMATS = frozenset(
    {"mp3", "flac", "aac", "m4a", "alac", "ogg", "oga", "opus", "wav", "aiff",
     "aif", "wma", "wv", "ape"}
)

#  Their own name for themselves, where the extension is not it. Printed on the
#  row, so `M4A` does not appear beside `MP3` as if the reader should know they
#  are comparable things.
DISPLAY = {"m4a": "M4A", "oga": "OGG", "aif": "AIFF"}

#  Music Videos are named and parsed by their own formatter and are video
#  besides. The preference must never reach them, and it must never be
#  triggered by a film that happens to carry an MP3 audio stream.
NOT_MUSIC = ("Music Videos",)


def preferred(conn: sqlite3.Connection) -> str:
    """The declared preferred music format, lower-case and without a dot.

    Read through the central policy, never from a second copy. Empty is a real
    answer — somebody may clear the preference — and callers here treat it as
    "prefer nothing", which is exactly what `prefer_among` already does with a
    format none of the candidates have.
    """
    from librairy.format_policy import canonical, preferred_for

    value = canonical(preferred_for(conn, CATEGORY))
    return value if value in MUSIC_FORMATS else ""


def set_preferred(conn: sqlite3.Connection, value: str) -> None:
    """Declare a different one. Refuses anything that is not a music format."""
    from librairy.format_policy import PolicyError, set_preferred_format

    clean = str(value or "").strip().lower().lstrip(".")
    if clean not in MUSIC_FORMATS:
        raise ValueError(f"{value!r} is not a music format LibrAIry knows")
    try:
        set_preferred_format(conn, CATEGORY, clean)
    except PolicyError as exc:  # pragma: no cover - MUSIC_FORMATS already refused it
        raise ValueError(str(exc)) from exc


def name(conn: sqlite3.Connection) -> str:
    """`MP3` — for a sentence, not a filename. Empty when none is configured."""
    value = preferred(conn)
    return DISPLAY.get(value, value.upper()) if value else ""


def sentence(conn: sqlite3.Connection) -> str:
    """The one line a row prints. Whose preference it is, said out loud.

    Empty when nothing is configured, so a caller that prints it unconditionally
    prints nothing rather than a sentence about a preference that is not there.
    """
    found = name(conn)
    return f"{found} is your preferred music format." if found else ""


def format_of(relpath: str) -> str:
    return PurePosixPath(relpath).suffix.lstrip(".").lower()


def is_music(relpath: str) -> bool:
    """Audio, and not something with its own naming rules.

    Extension-based, deliberately: this is a question about representations,
    and the representation is the extension. A video containing an MP3 stream
    is a video.
    """
    parts = [part for part in str(relpath).split("/") if part]
    if parts and parts[0] in NOT_MUSIC:
        return False
    return format_of(relpath) in MUSIC_FORMATS


def is_preferred(conn: sqlite3.Connection, relpath: str) -> bool:
    #  Compared canonically, because `.aif` and `.aiff` are one format under
    #  two spellings and a preference for either has to match both.
    from librairy.format_policy import canonical

    wanted = canonical(preferred(conn))
    return bool(wanted) and is_music(relpath) and canonical(format_of(relpath)) == wanted


def label_for(conn: sqlite3.Connection, relpath: str) -> str:
    """`FLAC`, `MP3` — the format, for printing beside a filename."""
    value = format_of(relpath)
    return DISPLAY.get(value, value.upper())


def prefer_among(conn: sqlite3.Connection, relpaths: list[str]) -> str:
    """The one of these the owner has said they want, or "".

    Empty in every case where a preference would be a guess rather than an
    application of what somebody said:

    * none of them is the preferred format — there is nothing to prefer, and
      LibrAIry does not make one;
    * **more than one is** — two MP3s of one recording is a real question and
      the preference has no opinion about it. Bitrate, size and date are not
      preferences anybody stated, and inventing one here would be the hidden
      quality rule this module exists to avoid;
    * any of them is not music at all.
    """
    candidates = [relpath for relpath in relpaths if is_music(relpath)]
    if len(candidates) != len(relpaths) or len(candidates) < 2:
        return ""
    matching = [relpath for relpath in candidates if is_preferred(conn, relpath)]
    return matching[0] if len(matching) == 1 else ""


def equivalent(
    conn: sqlite3.Connection, relpaths: list[str], *, recordings: list[str] | None = None
) -> bool:
    """Whether these are the same recording, in the two ways LibrAIry can know.

    A format preference must never decide a question about *which recording*.
    So it is applied only where the equivalence came from somewhere else:

    * **a catalog identity** — every one of them resolved by its own audio to
      the same MusicBrainz recording. This is the strong form, and it is what
      `filed_replace` already requires before it will move a file into
      another's path.
    * **the library's own naming** — the same stem in the same folder, which
      is a statement LibrAIry itself makes when it files a track: `01 - Song`
      is the track, and `.flac` and `.mp3` are two copies of it.

    Both refuse the cases that matter. `01 - Song (Live).mp3` is a different
    stem; a 2011 remaster is a different release with a different folder; an
    acoustic take is a different title. Nothing here compares titles for
    resemblance, so nothing here can decide that two similar names are one
    song.
    """
    if len(relpaths) < 2:
        return False
    known = [value for value in (recordings or []) if value]
    if len(known) == len(relpaths) and len(set(known)) == 1:
        return True
    stems = {
        (str(PurePosixPath(relpath).parent), PurePosixPath(relpath).stem)
        for relpath in relpaths
    }
    return len(stems) == 1

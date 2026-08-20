"""Deciding that a video is a music video, which is not the same as hoping so.

`librairy/musicvideo.py` can read `50 Cent - In Da Club (Clean).mp4` and tell
you the artist, the song and the version. It has been able to do that for a
while, and nothing called it: reading a name is not the same as claiming to
know what kind of file it is. `Kubrick - Barry Lyndon.mkv` parses just as
cleanly and is a film.

So this module is only about the second question — *is there evidence this is a
music video?* — and it answers it from two things, neither of which is the
extension and neither of which is a picture:

* **the folder somebody put it in.** A `Music Videos/` directory is a person
  saying what these are. It is the strongest signal available and the only one
  that beats a catalogued film, because a film sitting in that folder means the
  folder is wrong about one file, not that the folder means nothing.
* **a version marker that only a video has.** `(Official Video)`,
  `(Lyric Video)`, `(Visualizer)`. `(Live)` and `(Remastered)` are deliberately
  not in that set — they are equally true of an audio release and would file
  concert recordings as videos on the strength of a word.

Both require the filename to have parsed *confidently*: an artist and a title
either side of a real separator. Without that there is nobody to make a folder
for, and inventing one produces a directory that outlives the guess. The one
exception is the explicit folder, where `Unknown Artist` is recorded honestly
and the file lands below the confidence threshold for a person to decide.

What is deliberately absent: anything a model saw. A frame showing a performer
on a stage is equally consistent with a family video, a concert bootleg and a
DJ music video, and only one of those has an architecture. `video_vision`
contributes words, never this category.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy import musicvideo
from librairy.models import EvidenceEntry

# What a person calls the folder. Compared after folding case and turning
# separators into spaces, so `music_videos`, `Music-Videos` and `MUSIC VIDEOS`
# are one name. Bare `MV` is deliberately absent: two letters that also start
# a hundred other words is not somebody telling you anything.
FOLDER_NAMES = frozenset(
    {
        "music videos",
        "music video",
        "musicvideos",
        "musicvideo",
        "music vids",
        "musicvids",
        "mvids",
    }
)

# Version markers that say *video* and not merely *version*. Every one of these
# is a member of `musicvideo.VERSION_TOKENS["source"]`, which is asserted rather
# than assumed — the parser owns the vocabulary, and a subset that drifted out
# of it would silently stop matching.
#
# `live` and `remastered` are in that group too and are not here. A live
# recording is as often audio as video, and filing a concert bootleg under
# Music Videos on the strength of one word is the mistake this whole module is
# arranged to avoid.
VIDEO_VERSIONS = frozenset(
    {"official video", "music video", "lyric video", "video edit", "visualizer"}
)

# Said in the row's evidence, and used by the audit to explain itself.
FOLDER_SIGNAL = "source folder"
VERSION_SIGNAL = "version"

UNKNOWN_ARTIST = "Unknown Artist"
FALLBACK_GENRE = "General"

_SEPARATORS = re.compile(r"[_\-.]+")


@dataclass(frozen=True)
class MusicVideoRead:
    """What the path and the filename together say this file is."""

    parsed: musicvideo.ParsedName
    artist: str
    genre: str
    confidence: float
    signals: tuple[str, ...]
    evidence: tuple[EvidenceEntry, ...]

    @property
    def from_folder(self) -> bool:
        """Somebody filed this under Music Videos. Not a guess about a name."""
        return FOLDER_SIGNAL in self.signals

    @property
    def named(self) -> bool:
        return self.artist != UNKNOWN_ARTIST


def read(relpath: str) -> MusicVideoRead | None:
    """What this path says about being a music video, or None if it says nothing.

    None is the common answer and the safe one: every video that is not
    positively identified goes on to the film and episode classifiers exactly as
    it did before.
    """
    path = PurePosixPath(relpath)
    folders = tuple(path.parts[:-1])
    parsed = musicvideo.parse(path.name)
    version = _video_version(parsed)
    at = _music_video_folder(folders)

    signals: list[str] = []
    evidence: list[EvidenceEntry] = []
    if at is not None:
        signals.append(FOLDER_SIGNAL)
        evidence.append(EvidenceEntry("filesystem", "source folder", folders[at], 0.9))
    if version:
        signals.append(VERSION_SIGNAL)
        evidence.append(EvidenceEntry("heuristic", "version", version, 0.8))
    if not signals:
        return None

    if not parsed.confident:
        # A name nobody can read is not a music video on the strength of a
        # bracketed word. Inside an explicit folder it is one, filed under a
        # name that says it is unknown rather than one that pretends.
        if at is None:
            return None
        evidence.append(
            EvidenceEntry("heuristic", "artist", "no artist could be read", 0.4)
        )
        return MusicVideoRead(
            parsed=parsed,
            artist=UNKNOWN_ARTIST,
            genre=_genre(folders, at, ""),
            confidence=0.55,
            signals=tuple(signals),
            evidence=tuple(evidence),
        )

    evidence.append(EvidenceEntry("heuristic", "artist", parsed.primary_artist, 0.8))
    evidence.append(EvidenceEntry("heuristic", "title", parsed.title, 0.8))
    genre = _genre(folders, at, parsed.primary_artist)
    if at is not None and genre != FALLBACK_GENRE:
        evidence.append(EvidenceEntry("filesystem", "genre", genre, 0.85))
    return MusicVideoRead(
        parsed=parsed,
        artist=parsed.primary_artist,
        genre=genre,
        confidence=_confidence(signals),
        signals=tuple(signals),
        evidence=tuple(evidence),
    )


def canonical_name(parsed: musicvideo.ParsedName, ext: str, fallback: str) -> str:
    """`Artist feat. Guest - Title (Version).ext` — the form the parser reads back.

    Rebuilt from the parsed fields rather than tidied in place, because that is
    what makes two pools' spellings of the same file converge:
    `01. artist_title_extended.mp4` and `Artist - Title (Extended Mix).mp4` end
    up the same shape. The full credit stays in the name — it is searchable
    there and costs nothing, whereas a
    `Music Videos/House/David Guetta feat. Sia/` folder is how a collection
    grows a thousand one-off collaborations.

    The separators are the point. ` - ` is what `musicvideo.parse` splits on and
    the brackets are what it reads the version out of, so a name built here and
    then slugged would be a name LibrAIry could not read back — see
    `naming.media_filename` and `taxonomy.PARSED_FILENAME_CATEGORIES`. This
    returns the display form; `render_destination` makes it safe, through the
    one sanitiser, exactly as it does for every other category.
    """
    from librairy.naming import media_filename

    credit = parsed.credited_artist or parsed.primary_artist
    if not credit or not parsed.title:
        return media_filename(f"{fallback}{ext}")
    versions = "".join(f" ({version})" for version in parsed.versions)
    return media_filename(f"{credit} - {parsed.title}{versions}{ext}")


# --- reading the path ------------------------------------------------------------


def _normalize(component: str) -> str:
    return re.sub(r"\s+", " ", _SEPARATORS.sub(" ", component)).strip().casefold()


def _music_video_folder(folders: tuple[str, ...]) -> int | None:
    """Where in the path somebody said "these are music videos", innermost first.

    Innermost because a library can legitimately hold
    `Music Videos/Live/Music Videos/` — somebody's second sorting — and the
    nearest one is the one describing this file.
    """
    for index in range(len(folders) - 1, -1, -1):
        if _normalize(folders[index]) in FOLDER_NAMES:
            return index
    return None


def _video_version(parsed: musicvideo.ParsedName) -> str:
    """The parsed version marker that says *video*, if one of them does."""
    for version in parsed.versions:
        folded = version.casefold()
        if any(token in folded for token in VIDEO_VERSIONS):
            return version
    return ""


def _genre(folders: tuple[str, ...], at: int | None, artist: str) -> str:
    """The genre the person filing this already chose, or the honest fallback.

    Read from the path and never from a model. What sits between the
    `Music Videos` folder and the file is the layout somebody built, and the
    only ambiguity worth resolving is a single component: `Music Videos/House/`
    is a genre, `Music Videos/Daft Punk/` is an artist. The parsed artist name
    settles it, which is why this is asked after the name has been read.

    `General` rather than a guess. A manufactured genre becomes a directory,
    and a directory outlives whatever produced it.
    """
    if at is None:
        return FALLBACK_GENRE
    below = folders[at + 1 :]
    if not below:
        return FALLBACK_GENRE
    if artist and _key(below[0]) == _key(artist):
        return FALLBACK_GENRE
    return below[0]


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _confidence(signals: list[str]) -> float:
    """Two independent signals are worth more than either alone.

    None of these reach the certainty a catalog match gives a film, and that is
    correct: nothing outside the library has been asked. They are above the
    default threshold because a person's own folder is strong evidence about
    their own files.
    """
    if len(signals) > 1:
        return 0.92
    return 0.9 if signals == [FOLDER_SIGNAL] else 0.86

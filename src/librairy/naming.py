"""Turning whatever a file is called into a name that survives everywhere.

The names LibrAIry proposes end up in URLs, in shell commands, in rsync and
rclone arguments, on SMB shares read by Windows, and in the addresses of the
portal's own pages. A folder called "R&B Soul" is legal on every filesystem
LibrAIry runs on and still breaks half of those. So does a trailing space, an
emoji, a colon, and a name that happens to be "CON".

This module is only about *proposing* names. It never touches a file that is
already filed: the sanitiser runs when a destination is worked out, and moving
anything still needs an approved plan.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

SEPARATOR = "-"
# Kept because they carry meaning in the names people already use: "(2005)"
# for a year, "_" as a deliberate separator, "." before an extension.
KEEP = frozenset("-_.()")
# Illegal on Windows/SMB, so a share full of them is a share the owner cannot
# open from a laptop even though the NAS accepted the write.
WINDOWS_FORBIDDEN = frozenset("<>:/\\|?*")
# Removed rather than turned into a separator: these sit inside a word, so
# "Alicia's Prayer" has to become Alicias-Prayer and not Alicia-s-Prayer.
DROPPED = frozenset("'’‘`\"“”")
# Windows refuses these as filenames whatever the extension.
RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)
# 255 bytes is the per-component limit almost everywhere. Leaving room means a
# collision suffix -- " (2)" -- and a backup tool's own suffixes still fit.
MAX_COMPONENT = 120
# Spelled out rather than dropped, because dropping it changes the meaning:
# "Rock & Roll" must not become "Rock Roll".
SPELLED_OUT = {"&": "and"}
_SEPARATOR_RUN = re.compile(rf"{re.escape(SEPARATOR)}{{2,}}")


def slugify(text: str, *, fallback: str = "untitled") -> str:
    """One path component, safe to put in a URL, a shell word and an SMB share.

    Letters and digits in any script are kept -- a name in Japanese is a name,
    not a "weird character". What goes is punctuation that means something to
    a shell or a URL, emoji and other symbols, and every run of whitespace,
    which becomes a single dash.
    """
    text = unicodedata.normalize("NFC", text)
    out: list[str] = []
    for char in text:
        if char in DROPPED:
            continue
        if char in SPELLED_OUT:
            out.append(f"{SEPARATOR}{SPELLED_OUT[char]}{SEPARATOR}")
        elif char in WINDOWS_FORBIDDEN:
            out.append(SEPARATOR)
        elif char in KEEP or char.isalnum():
            out.append(char)
        else:
            # Emoji, currency signs, arrows, control characters, whitespace.
            out.append(SEPARATOR)
    slug = _SEPARATOR_RUN.sub(SEPARATOR, "".join(out))
    # A leading dot hides the file; a trailing dot is silently dropped by
    # Windows, which turns "a." and "a" into a collision nobody asked for.
    slug = slug.strip(f"{SEPARATOR}._ ")
    slug = slug[:MAX_COMPONENT].rstrip(f"{SEPARATOR}._")
    if not slug or slug.upper() in RESERVED:
        return f"{slug}_" if slug else fallback
    return slug


def slugify_filename(name: str, *, fallback: str = "untitled") -> str:
    """Same, but the extension is kept intact and does not eat the length cap."""
    path = PurePosixPath(name)
    suffix = path.suffix.lower() if len(path.suffix) <= 10 else ""
    stem = name[: -len(suffix)] if suffix else name
    return f"{slugify(stem, fallback=fallback)}{suffix}"


#  The folders an optical disc keeps its structure in. Inside one of these,
#  filenames are not descriptive text: VTS_01_1.VOB is named that because a
#  player looks for exactly that, and VIDEO_TS.IFO points at its siblings by
#  name. Tidying them produces a folder that looks neater and no longer plays.
DISC_DIRECTORIES = frozenset({"VIDEO_TS", "AUDIO_TS", "BDMV", "CERTIFICATE"})


def tidy_relpath(relpath: str) -> str:
    """Every component of a rendered destination, sanitised.

    Per-field sanitising is not enough on its own: "Movies/{title} ({year})"
    joins a clean title to a literal space in the template, and produced
    "The-Matrix (1999)" -- half tidy, which is worse than either. Doing it
    once over the finished path is also what stops a new template from
    reintroducing the problem.

    One exception, and it earns itself: from a disc directory downwards the
    names are a contract with a DVD player rather than a description of
    anything, so they are only made *safe* -- control characters and
    separators removed -- and never rewritten.
    """
    parts = [part for part in relpath.split("/") if part]
    if not parts:
        return ""
    tidy: list[str] = []
    structural = False
    for index, part in enumerate(parts):
        structural = structural or part.upper() in DISC_DIRECTORIES
        if structural:
            tidy.append(_safe_component(part))
        elif index == len(parts) - 1:
            tidy.append(slugify_filename(part))
        else:
            tidy.append(slugify(part))
    return "/".join(tidy)


def _safe_component(part: str) -> str:
    """Safe to be a path component, and otherwise left exactly as it is."""
    from librairy.paths import PathValidationError, sanitize_component

    try:
        return sanitize_component(part)
    except PathValidationError:
        return slugify(part)


def is_clean(name: str) -> bool:
    """Whether a name would survive slugify unchanged.

    Used to decide whether a rename is worth proposing at all: an already-tidy
    filename should not turn up in Review as a move to itself.
    """
    return slugify_filename(name) == name

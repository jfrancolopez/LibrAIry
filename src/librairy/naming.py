"""Turning whatever a file is called into a name that survives everywhere.

The names LibrAIry proposes end up in URLs, in shell commands, in rsync and
rclone arguments, on SMB shares read by Windows, and in the addresses of the
portal's own pages. A folder called "R&B Soul" is legal on every filesystem
LibrAIry runs on and still breaks half of those. So does a trailing space, an
emoji, a colon, and a name that happens to be "CON".

This module is only about *proposing* names. It never touches a file that is
already filed: the sanitiser runs when a destination is worked out, and moving
anything still needs an approved plan.

**Two jobs, and they are not the same job.** Safety is "this name cannot break
a filesystem, a shell or a URL". Style is "this is how LibrAIry spells a name it
invented". `slugify` does both at once, and for most of what LibrAIry files that
is right — nobody needs to read `Photos/2024/August/IMG_5150.jpeg` back.

For one category it is wrong, and the failure is precise: a music video's
filename *is* its identity. `musicvideo.parse` reads the artist and the title
either side of ` - ` and the version out of the brackets — and `slugify` turns
every space into a dash, so a file LibrAIry filed itself could no longer be read
by the code that named it. `media_filename` is the other half of the split: the
same hygiene, none of the restyling, so what goes in comes back out.

    slugify            invented names. Space -> dash, apostrophe dropped,
                       & -> and. Right when nothing has to read the name back.
    media_filename     names whose punctuation is load-bearing. Made safe and
                       otherwise left alone.

`taxonomy.PARSED_FILENAME_CATEGORIES` is the list of categories on the second
side of that line, and it has one member. Widening it is a product decision
about how a library reads, not a refactor.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.paths import CONTROL_CHARS

SEPARATOR = "-"

#  A UUID, a long hex blob, or a bare number. This is how a phone or a
#  messaging app names a folder when it has nothing to say about the contents,
#  and it is never worth carrying into a name a person has to read.
#
#  Lives here, in the naming module, because three places need the same answer
#  and each of them getting it separately is how an iMessage export ended up
#  filed as Photos/Unknown/01B583D3-1D28-4B3A-A5DD-9471447CFA27/ with the same
#  UUID appended to every filename inside it.
NOISE_RE = re.compile(
    r"(?i)^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{16,}|\d+)$"
)
#  The same UUID, found anywhere inside a longer string rather than being the
#  whole of it: "IMG_1423-0373923B-123F-4ABF-9B6E-2229413CEED4" is a filename
#  that already went wrong once.
EMBEDDED_UUID_RE = re.compile(
    r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def is_noise(label: str) -> bool:
    """Whether this is a machine's placeholder rather than anybody's name.

    The UUID comes out first, so the forms that pair one with a number —
    iMessage writes `78726114145__D68BA48A-94F5-4023-8D03-F6400AD555F3` — are
    recognised as the noise they are rather than as a name with a long suffix.
    """
    text = EMBEDDED_UUID_RE.sub(" ", label).strip(" -_.")
    return not text or bool(NOISE_RE.match(text))
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


# --- naming hygiene, for names LibrAIry did not choose ------------------------
#
# `slugify` answers "what should LibrAIry call this new file?" and its answer
# is house style: whitespace becomes a dash, apostrophes are dropped, `&`
# becomes `and`. That is right for a name being invented, and wrong as a
# verdict on a library somebody else already organised.
#
# Measured before this was written: of 140 files in the author's real library,
# **118 would be renamed by `tidy_relpath`**, and 142 of 183 path components
# contain a space. Auditing against house style would report 84% of the
# library as broken — the "eight hundred harmless warnings" the audit exists
# to avoid, and a direct contradiction of its founding rule that your layout
# is evidence rather than a mistake.
#
# So the audit enforces the *hygiene* half: the rules that say a name is
# damaged rather than differently-styled. Same module, same character tables,
# one policy split at a line that is written down here:
#
#   hygiene (audited)      leading/trailing/repeated whitespace, control
#                          characters, Windows-forbidden characters, emoji and
#                          other symbols, typographic quotes, reserved device
#                          names, trailing dots, over-length, non-NFC forms
#   house style (not)      space -> dash, ASCII apostrophe dropped, & -> and
#
# The ASCII apostrophe is the one place the audit is deliberately narrower
# than `slugify`. It is legal on every filesystem LibrAIry targets and it
# appears in real titles — `Guns N' Roses`, `You're My Best Friend` — so
# flagging it would turn correct names into worse ones. Typographic quotes
# (`’ ‘ “ ”`) are still hygiene: they are almost always a copy-paste artifact
# and they are what makes two visually identical paths different strings.

# Everything `slugify` drops except the plain apostrophe. `"` and `` ` `` are
# not punctuation anybody puts in a title on purpose and both break shell
# quoting; `'` is in real names and is safe everywhere LibrAIry runs.
KEPT_QUOTE = "'"
DROPPED_QUOTES = frozenset(DROPPED) - {KEPT_QUOTE}
SMART_QUOTES = frozenset("’‘“”„‟′″")
# Whitespace that is not a plain space, plus the zero-width and directional
# characters that make two identical-looking names different strings.
ODD_SPACE = re.compile(r"[\t\n\r\f\v   -     　]")
INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")
REPEATED_SPACE = re.compile(r"  +")


@dataclass(frozen=True)
class NamingIssue:
    """One rule a path component breaks, in words fit for the page."""

    rule: str
    detail: str


def hygiene_issues(component: str, *, is_filename: bool = False) -> list[NamingIssue]:
    """Every hygiene rule this one path component breaks.

    Deterministic and evidence-free: none of these needs tags, a catalog or a
    model to be certain about. Casing is deliberately absent — it is a
    judgement about a name rather than a defect in one, and it is decided
    elsewhere with evidence.
    """
    stem, suffix = _split_name(component) if is_filename else (component, "")
    issues: list[NamingIssue] = []
    if component != component.strip():
        where = "starts" if component != component.lstrip() else "ends"
        issues.append(NamingIssue("edge-space", f"{where.capitalize()} with a space."))
    if is_filename and stem != stem.rstrip():
        issues.append(NamingIssue("space-before-extension", "Has a space before the extension."))
    if REPEATED_SPACE.search(component):
        issues.append(NamingIssue("repeated-space", "Contains a run of two or more spaces."))
    if ODD_SPACE.search(component):
        issues.append(NamingIssue("odd-space", "Contains a tab, newline or unusual space."))
    if INVISIBLE.search(component):
        issues.append(NamingIssue("invisible", "Contains invisible formatting characters."))
    if CONTROL_CHARS.search(component):
        issues.append(NamingIssue("control", "Contains control characters."))
    forbidden = sorted({char for char in component if char in WINDOWS_FORBIDDEN})
    if forbidden:
        issues.append(
            NamingIssue(
                "windows-forbidden",
                f"Contains {' '.join(forbidden)}, which Windows and SMB shares reject.",
            )
        )
    if any(char in SMART_QUOTES or char in DROPPED_QUOTES for char in component):
        issues.append(NamingIssue("smart-quotes", "Contains typographic quote characters."))
    symbols = sorted({char for char in component if _is_symbol(char)})
    if symbols:
        issues.append(
            NamingIssue("symbol", f"Contains {' '.join(symbols)}, which is not part of a name.")
        )
    if unicodedata.normalize("NFC", component) != component:
        issues.append(NamingIssue("unicode-form", "Uses a decomposed Unicode form."))
    if stem.endswith("."):
        issues.append(NamingIssue("trailing-dot", "Ends in a dot, which Windows silently drops."))
    if stem.upper() in RESERVED:
        issues.append(NamingIssue("reserved", f"{stem} is a reserved device name on Windows."))
    if len(component) > MAX_COMPONENT:
        issues.append(NamingIssue("too-long", f"Longer than {MAX_COMPONENT} characters."))
    return issues


def tidy_component(component: str, *, is_filename: bool = False) -> str:
    """The same name with its hygiene problems fixed and nothing else changed.

    Spaces stay spaces and apostrophes stay apostrophes: this repairs a name,
    it does not restyle one. An unsafe character is *removed* rather than
    turned into a dash, because the surrounding text is space-joined — turning
    `🔥 Queen 🔥` into `-Queen-` would fix one problem by causing another.
    """
    stem, suffix = _split_name(component) if is_filename else (component, "")
    cleaned = unicodedata.normalize("NFC", stem)
    # Odd spacing becomes a space before control characters are stripped: a
    # tab is both, and removing it would join two words into one.
    cleaned = ODD_SPACE.sub(" ", cleaned)
    cleaned = CONTROL_CHARS.sub("", cleaned)
    cleaned = INVISIBLE.sub("", cleaned)
    cleaned = "".join(
        ""
        if char in SMART_QUOTES or char in DROPPED_QUOTES or _is_symbol(char)
        else char
        for char in cleaned
    )
    cleaned = "".join("-" if char in WINDOWS_FORBIDDEN else char for char in cleaned)
    # A run of forbidden characters is one problem, not five dashes.
    cleaned = _SEPARATOR_RUN.sub(SEPARATOR, cleaned)
    cleaned = REPEATED_SPACE.sub(" ", cleaned).strip().strip("-").strip().rstrip(".")
    cleaned = cleaned[:MAX_COMPONENT].strip()
    if not cleaned:
        return slugify(component)
    if cleaned.upper() in RESERVED:
        cleaned = f"{cleaned}_"
    return f"{cleaned}{suffix}"


def media_filename(display: str, *, fallback: str = "untitled") -> str:
    """A filename that keeps the punctuation its meaning depends on.

    Safety and nothing else: NFC, no control characters, no invisibles, no
    Windows-forbidden characters, no reserved device name, no trailing dot, no
    traversal, capped to a length every filesystem accepts. The extension
    survives intact, and so does everything a reader — human or parser — needs.

    `Daft Punk - Around the World (Official Video).mkv` comes back unchanged.
    `AC/DC - Back In Black.mkv` comes back as `AC-DC - Back In Black.mkv`,
    because a slash in a filename is a directory separator and no amount of
    wanting it to be a band name changes that.
    """
    stem, suffix = _split_name(display)
    cleaned = tidy_component(stem)
    if not cleaned.strip():
        #  A name made entirely of things that cannot be in a filename. House
        #  style is the right fallback there — there is nothing left to
        #  preserve — and the extension is kept out of it, because `.mp4` is
        #  what a player looks at and not part of anybody's title.
        cleaned = slugify(stem, fallback=fallback)
    return f"{cleaned}{suffix}"


def _split_name(name: str) -> tuple[str, str]:
    """Stem and suffix, keeping every dot a subtitle depends on.

    `Movie.en.forced.srt` is stem `Movie.en.forced` and suffix `.srt`, so the
    language and forced markers survive being tidied — losing them is how two
    subtitles collapse into one filename no player can tell apart.
    """
    path = PurePosixPath(name)
    suffix = path.suffix if 0 < len(path.suffix) <= 10 else ""
    return (name[: -len(suffix)] if suffix else name), suffix


def _is_symbol(char: str) -> bool:
    """Emoji, arrows, currency signs — a picture, not a letter.

    Anything alphanumeric in any script is a name: Japanese is a name, not a
    weird character. `KEEP` covers the punctuation people genuinely use.
    """
    if char.isalnum() or char.isspace() or char in KEEP or char in DROPPED:
        return False
    if char in WINDOWS_FORBIDDEN:
        # `<` and `>` are maths symbols to Unicode and forbidden characters to
        # Windows. The forbidden rule owns them, so a run of them collapses to
        # one separator instead of vanishing and joining two words.
        return False
    return unicodedata.category(char).startswith("S") or ord(char) > 0x2100


def is_structural(component: str, parts: tuple[str, ...] = ()) -> bool:
    """Names that are a contract with a player rather than a description.

    `VIDEO_TS.IFO` points at its siblings by name and `VTS_01_1.VOB` is what a
    DVD player looks for. Tidying either produces a folder that reads better
    and no longer plays, so nothing inside a disc directory is ever audited
    for style.
    """
    if component.upper() in DISC_DIRECTORIES:
        return True
    if any(part.upper() in DISC_DIRECTORIES for part in parts):
        return True
    stem = PurePosixPath(component).stem.upper()
    return stem in DISC_DIRECTORIES or bool(re.match(r"^VTS_\d+_\d+$", stem))

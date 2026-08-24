from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any

from librairy.naming import media_filename, slugify, tidy_component, tidy_relpath
from librairy.paths import PathValidationError, sanitize_component, validate_dest

CATEGORIES = (
    "music",
    "music_videos",
    "movies",
    "shows",
    "photos",
    "documents",
    "books",
    "projects",
    "misc",
)
#  Categories whose filename is read rather than merely stored.
#
#  A music video is named `Artist - Title (Version).ext` and `musicvideo.parse`
#  reads exactly that back — which is what makes two pool spellings of one file
#  converge, what keeps `(Clean)` and `(Dirty)` apart, and what lets the audit
#  recognise a file LibrAIry filed itself. House style turns every space into a
#  dash and destroys the separator the whole scheme rests on, so this category
#  is made *safe* rather than restyled. See `naming.media_filename`.
#
#  Music joined it for a related but distinct reason, and only once the rest of
#  Music organisation was finished. A track's identity is in its folders —
#  `Music/Rock/Queen/A Night at the Opera/` says artist and album — so the
#  filename carries the one thing left, which is the part a person reads in a
#  player's track list. `01-Death-on-Two-Legs.flac` is safe and unreadable, and
#  the unreadability bought nothing: `media_filename` already refuses every
#  character a filesystem, a shell or an SMB share objects to. The grammar the
#  names follow, in both directions, is `musicnames.py`.
#
#  Two members, deliberately, and this is the whole of the list. Adding a third
#  is a decision about how a library reads and changes the name of every file
#  filed under whatever is added — it is not a refactor.
PARSED_FILENAME_CATEGORIES = frozenset({"music_videos", "music"})

#  Categories whose *paths* are written for a person to read rather than
#  slugified into house style. The music ones are here because a parser reads
#  their filenames back; documents and books are here for the opposite reason
#  — nothing parses them, and `Documents/Manuals/Honda-Motor-Co/2024-CR-V-
#  Owners-Manual.pdf` is a worse answer than the title the document already
#  carries. Safety still wins: `tidy_component` removes what a filesystem
#  cannot hold and changes nothing else.
READABLE_PATH_CATEGORIES = PARSED_FILENAME_CATEGORIES | {"documents", "books"}

DEFAULT_STYLE = "conventional"
DEFAULT_STYLES = {
    "music": "genre-first",
    "music_videos": "genre-first",
    "movies": "genre-first",
    "shows": "genre-first",
}

# The album layer is the difference between these two, and it is deliberate.
#
# For an album of FLACs, `Artist/Album/Track` is how the music was released and
# how people look for it. For a DJ video collection it is a layer you fight:
# remixes, intro edits, pool releases and mashups either belong to no album or
# to one that has nothing to do with the file. Half the folders would be a real
# release and half would be an invention, and you could not tell which by
# looking.
#
# So music videos are **tracks, not albums**: `Music Videos/Genre/Artist/File`,
# three levels, and the filename carries the identity that matters to a DJ —
# featured artists, remix, edit, clean or dirty. Album, year, BPM, secondary
# genres and the pool it came from are all worth knowing and all live in the
# index, where they can be searched and filtered without becoming directories.
#
# `test_music_video_paths.py` asserts no music-video template contains
# `{album}` and that the music templates still do, because the failure mode
# here is quiet: a shared "music-like" helper grows an album component one
# refactor at a time and nobody notices until the library has been restructured.
TEMPLATES: dict[str, dict[str, str]] = {
    "music": {
        "conventional": "Music/{artist}/{album}/{clean_name}",
        "genre-first": "Music/{genre}/{artist}/{album}/{clean_name}",
    },
    "music_videos": {
        "conventional": "Music Videos/{artist}/{clean_name}",
        "genre-first": "Music Videos/{genre}/{artist}/{clean_name}",
    },
    "movies": {
        "conventional": "Movies/{title} ({year})/{clean_name}",
        "genre-first": "Movies/{genre}/{title} ({year})/{clean_name}",
    },
    "shows": {
        "conventional": "Shows/{show}/Season {season:02d}/{clean_name}",
        "genre-first": "Shows/{genre}/{show}/Season {season:02d}/{clean_name}",
    },
    "photos": {"conventional": "Photos/{year}/{event}/{clean_name}"},
    "documents": {"conventional": "Documents/{year}/{clean_name}"},
    "books": {
        "conventional": "Books/{author}/{title}/{clean_name}",
        "genre-first": "Books/{genre}/{author}/{title}/{clean_name}",
    },
    "projects": {"conventional": "Projects/{project}/{clean_name}"},
    "misc": {"conventional": "Misc/{clean_name}"},
}


#  The Documents hierarchy, one template per broad type. Four branches and no
#  more: `Reports`, `Forms`, `Statements` and `Reference` are distinctions this
#  classifier cannot reliably support, and a taxonomy with twenty directories in
#  it is a taxonomy whose folders disagree with each other.
#
#  Each type has a ladder of its own, and every rung down has *less* structure
#  rather than invented structure. There is no `Unknown Manufacturer` and no
#  `General`: a folder that exists to keep the depth uniform is a folder that
#  claims to know something.
DOCUMENT_TEMPLATES = {
    "manual": (
        ("organization", "Documents/Manuals/{organization}/{clean_name}"),
        ((), "Documents/Manuals/{clean_name}"),
    ),
    "financial": (
        ("year", "Documents/Financial/{year}/{clean_name}"),
        ((), "Documents/Financial/{clean_name}"),
    ),
    "paper": (
        ("author", "Documents/Papers/{author}/{clean_name}"),
        ("year", "Documents/Papers/{year}/{clean_name}"),
        ((), "Documents/Papers/{clean_name}"),
    ),
}


def document_template(kind: str, fields: dict[str, Any]) -> str:
    """The deepest branch this document has the evidence for.

    Absence of evidence produces a shallower path, never a placeholder one.
    A manual whose manufacturer is not trustworthy is filed as a manual —
    `Documents/Manuals/2024 CR-V Owner's Manual.pdf` — because that is what is
    known about it, and `Unknown Manufacturer/` would be a directory named
    after a thing nobody established.
    """
    for token, template in DOCUMENT_TEMPLATES.get(kind, ()):
        if not token or fields.get(token):
            return template
    return ""


@dataclass(frozen=True)
class RenderResult:
    relpath: str | None
    reason: str | None = None


def render_destination(
    category: str,
    fields: dict[str, Any],
    *,
    library_root: Path,
    conn: sqlite3.Connection | None = None,
    style: str | None = None,
) -> RenderResult:
    template = _template_for(category, style or template_style(conn, category))
    missing = _missing_tokens(template, fields)
    if missing:
        return RenderResult(None, f"missing tokens: {', '.join(missing)}")
    try:
        safe_fields = _safe_fields(fields, category)
        relpath = _render_path(template, safe_fields, category)
        validate_dest(library_root, relpath)
    except (KeyError, ValueError, PathValidationError) as exc:
        return RenderResult(None, str(exc))
    return RenderResult(relpath)


def render_template(
    template: str,
    category: str,
    fields: dict[str, Any],
    *,
    library_root: Path,
) -> RenderResult:
    """Render one explicit template, through the same machinery as the rest.

    The document hierarchy chooses its branch from the evidence rather than
    from a per-category style, so it needs the template resolved before this
    rather than looked up inside it. Everything after that is shared on
    purpose: one sanitizer, one path tidier, one `validate_dest`. A second
    spelling of any of those is a second answer to "is this path safe".
    """
    missing = _missing_tokens(template, fields)
    if missing:
        return RenderResult(None, f"missing tokens: {', '.join(missing)}")
    try:
        relpath = _render_path(template, _safe_fields(fields, category), category)
        validate_dest(library_root, relpath)
    except (KeyError, ValueError, PathValidationError) as exc:
        return RenderResult(None, str(exc))
    return RenderResult(relpath)


def document_name(title: str, ext: str = "") -> str:
    """A document's filename: the title it carries, made safe and no more.

    `media_filename` and not `slugify`, and the difference is the whole point
    of reading the file. `2024 CR-V Owner's Manual.pdf` is what the document
    calls itself; `2024-CR-V-Owners-Manual.pdf` is house style applied to a
    title that did not need styling. Safety still wins — a slash is still a
    directory separator — and a title made entirely of unusable characters
    falls back to the house slug, because then there is nothing to preserve.
    """
    from librairy.naming import media_filename

    if ext and not ext.startswith("."):
        ext = f".{ext}"
    return media_filename(f"{_strip_hashtags(title).strip()}{ext}")


def _render_path(template: str, safe_fields: dict[str, Any], category: str) -> str:
    """Fill the template, tidying what came from the fields and not the template.

    The distinction is the whole function. Field values are metadata off a tag,
    a catalog or a filename, and they go through `slugify` and then through
    `tidy_relpath` again because joining them to literal text can produce
    something neither pass would have allowed on its own — `Movies/{title}
    ({year})` glues a clean title to a literal space and a pair of brackets, and
    tidying only the field left `The-Matrix (1999)`, half done.

    A component made *entirely* of literal text is different. It was written
    here, in this file, by someone choosing what the folder is called. Running
    it through the same slug turned `Music Videos` into `Music-Videos` — a
    folder named after a rule about untrusted input rather than after the thing
    it holds. `Music`, `Movies`, `Shows` and `Photos` never noticed because a
    single word survives slugification unchanged.
    """
    tidy = _tidy_for(category)
    components: list[str] = []
    for component in template.split("/"):
        rendered = component.format(**safe_fields)
        components.append(rendered if _is_literal(component) else tidy(rendered))
    return "/".join(part for part in components if part)


def _tidy_for(category: str):  # noqa: ANN202
    """How this category's path components are made safe.

    Two answers, and the second one has exactly one category in it. See
    `naming.py` for the split: most of what LibrAIry files is named for a person
    to glance at, and one thing is named for a parser to read back.
    """
    if category not in READABLE_PATH_CATEGORIES:
        return tidy_relpath
    return lambda component: "/".join(
        tidy_component(part) for part in component.split("/") if part
    )


def _is_literal(component: str) -> bool:
    """True when this template component has no field in it at all."""
    return all(name is None for _, name, _, _ in Formatter().parse(component))


def template_style(conn: sqlite3.Connection | None, category: str) -> str:
    default = DEFAULT_STYLES.get(category, DEFAULT_STYLE)
    if conn is None:
        return default
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (f"templates.{category}.style",),
    ).fetchone()
    if row is None:
        return default
    value = json.loads(row["value"])
    return str(value)


def set_template_style(conn: sqlite3.Connection, category: str, style: str) -> None:
    _template_for(category, style)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (f"templates.{category}.style", json.dumps(style)),
    )


def clean_name_from_title(title: str, ext: str = "") -> str:
    base = slugify(_strip_hashtags(title), fallback=sanitize_component(title))
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    return f"{base}{ext}"


def _template_for(category: str, style: str) -> str:
    if category not in TEMPLATES:
        raise ValueError(f"unknown category: {category}")
    styles = TEMPLATES[category]
    if style not in styles:
        if DEFAULT_STYLE in styles:
            return styles[DEFAULT_STYLE]
        raise ValueError(f"style {style} is not available for {category}")
    return styles[style]


def _missing_tokens(template: str, fields: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for _, field_name, _, _ in Formatter().parse(template):
        if field_name and field_name not in fields:
            missing.append(field_name)
    return sorted(set(missing))


def _safe_fields(fields: dict[str, Any], category: str) -> dict[str, Any]:
    """Every token in a destination template, made safe to be a path component.

    Sanitising here rather than at each call site is what makes the guarantee
    hold: a new template or a new metadata source cannot introduce a folder
    with a space, an ampersand or an emoji in it without going through this.

    Which *kind* of safe depends on the category. A music video's filename is
    read back by `musicvideo.parse` — the artist and the title either side of
    ` - `, the version in brackets — so slugging it would mean LibrAIry could
    not read a name it wrote itself. A document's is read by a person, and
    `Honda-Motor-Co/2024-CR-V-Owners-Manual.pdf` is house style applied to a
    title that came with its own punctuation. Both keep what they have and lose
    only what is unsafe; everything else gets house style.
    """
    parsed = category in READABLE_PATH_CATEGORIES
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, int):
            safe[key] = value
            continue
        text = _strip_hashtags(str(value))
        if parsed:
            safe[key] = (
                media_filename(text) if key == "clean_name" else tidy_component(text)
            )
            continue
        # clean_name is the filename, so its extension must not be mangled by
        # the length cap or absorbed into the slug. tidy_relpath rather than
        # slugify_filename because a disc files a whole structure under one
        # proposal — "VIDEO_TS/VTS_01_1.VOB" is one clean_name with a folder in
        # it, and for an ordinary single-component name the two agree.
        safe[key] = tidy_relpath(text) if key == "clean_name" else slugify(text)
    return safe


def _strip_hashtags(value: str) -> str:
    return re.sub(r"#[^\s#]+", "", value).replace("#", "").strip()

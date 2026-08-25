"""What the owner prefers, permits and protects — asked in one place.

LibrAIry had one explicit format preference and it worked: `MP3` beside a FLAC,
preselected, labelled, overridable, never acted on. What it did not have was
anywhere for the *second* one to go. Photos would have grown a preference,
Storage Optimization already had a protected-roots list under its own name, and
Documents would eventually have had an opinion about EPUB — four settings, four
readers, and no way for a comparison page and an optimization queue to agree
about the same folder.

So this is the horizontal one. It answers exactly one question:

    among representations that are valid choices,
    what does the owner prefer, permit, or forbid?

and it is careful about the three questions it is **not** answering.

**It is not identity.** Whether two files are the same recording is decided by
a catalog identity or by the library's own naming, before a format preference
is relevant at all. Policy says what to prefer among valid choices; it never
makes two files into choices.

**It is not a workflow.** Nothing here transcodes, quarantines, plans,
approves, moves or deletes. It is read by workflows. A policy that could act
would be a rule engine, and a rule engine is the thing this program has spent
every pass refusing to become.

**It is not a filesystem permission.** `Preserve originals` on
`Photos/Wedding` means no representation preference and no optimization may
decide that the RAW is the dispensable one. It does **not** mean LibrAIry may
never file that photograph into a better folder. Those are separate powers, and
the stronger one already exists as `optimization.protected_roots`, which stops
a folder being queued for change at all. Conflating them would give the program
a second, quieter permissions system that nobody asked for and nobody could
explain.

Three separate concepts, deliberately not one column:

    preferred_format    among representations that ALREADY exist, which one.
                        Creates nothing. If the only copy is a FLAC there is
                        no MP3 to prefer and nothing happens.

    allow_*_transform   may LibrAIry ever *propose* making one. Tri-state:
                        unset is "the owner has not said", which is not "no",
                        and is what every scope starts as.

    preserve_originals  this scope's originals are not to be traded away by a
                        representation preference or an optimization.

**Precedence is per field, most specific first**, among the scopes that
actually state that field:

    folder (longest match)  >  category  >  global

A scope silent about a field does not overrule a broader one that speaks. That
is what makes `Music → MP3` survive somebody protecting one folder inside it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

GLOBAL = "global"
CATEGORY = "category"
FOLDER = "folder"
SCOPE_KINDS = (GLOBAL, CATEGORY, FOLDER)

#  Where a protection came from, because the two mean different things and a
#  page that says "protected" without saying which has told somebody nothing.
BY_POLICY = "format-policy"
BY_ROOT = "protected-root"

#  Formats a preference may name, per category. A preference for a format that
#  category cannot hold is not a preference, it is a typo — and refusing it at
#  the point of saving is the only place the owner can still fix it.
KNOWN_FORMATS: dict[str, frozenset[str]] = {
    "music": frozenset(
        {"mp3", "flac", "aac", "m4a", "alac", "ogg", "oga", "opus", "wav",
         "aiff", "aif", "wma", "wv", "ape"}
    ),
    "photos": frozenset({"jpg", "jpeg", "heic", "heif", "png", "webp", "tif", "tiff"}),
    "movies": frozenset({"mkv", "mp4", "m4v", "avi", "mov", "webm"}),
    "shows": frozenset({"mkv", "mp4", "m4v", "avi", "mov", "webm"}),
    "music_videos": frozenset({"mkv", "mp4", "m4v", "avi", "mov", "webm"}),
    "documents": frozenset({"pdf", "epub", "mobi", "azw3", "djvu", "txt", "docx"}),
    "books": frozenset({"pdf", "epub", "mobi", "azw3", "djvu"}),
}

#  Two spellings of one format. Both are accepted wherever a format is read —
#  a library holds `.aif` and `.aiff` files and both are AIFF — but only the
#  canonical one is ever *offered*, because a picker listing "AIFF" twice looks
#  like a bug and gives somebody two ways to say the same thing.
ALIAS = {"aif": "aiff", "jpg": "jpeg", "oga": "ogg", "heif": "heic", "tif": "tiff"}


def canonical(suffix: str) -> str:
    """One spelling for a format, so `.aif` and `.aiff` compare equal."""
    value = str(suffix or "").strip().lower().lstrip(".")
    return ALIAS.get(value, value)


#  Their own name for themselves, where the extension is not it.
DISPLAY = {"m4a": "M4A", "aiff": "AIFF", "jpeg": "JPEG"}

#  Which categories the Format Policy page offers a section for. Not every
#  taxonomy slug: `projects` and `misc` are not representation questions, and a
#  page that asked about them would be asking somebody to invent an answer.
SECTIONS = ("music", "photos", "movies", "documents")
SECTION_LABEL = {
    "music": "Music",
    "photos": "Photos",
    "movies": "Video",
    "documents": "Documents",
}
#  What a category section is really about, where one slug speaks for more than
#  itself. Video policy covers films, shows and music videos, because a person
#  configuring "video" means all of them and would be baffled to find it
#  applied to one.
SECTION_COVERS = {
    "music": ("music",),
    "photos": ("photos",),
    "movies": ("movies", "shows", "music_videos"),
    "documents": ("documents", "books"),
}
#  The reverse, so a category resolves to the section that speaks for it.
SECTION_OF = {
    member: section for section, members in SECTION_COVERS.items() for member in members
}


class PolicyError(ValueError):
    """A policy that cannot be saved as written."""


@dataclass(frozen=True)
class Scope:
    """One configured scope, exactly as stored."""

    scope_kind: str
    scope_value: str
    preferred_format: str = ""
    preserve_originals: bool | None = None
    allow_lossy_transform: bool | None = None
    allow_lossless_transform: bool | None = None

    @property
    def described(self) -> str:
        if self.scope_kind == GLOBAL:
            return "your whole library"
        if self.scope_kind == CATEGORY:
            return SECTION_LABEL.get(self.scope_value, self.scope_value.title())
        return self.scope_value

    @property
    def specificity(self) -> int:
        """How narrow this scope is. Bigger wins.

        A folder beats a category beats global, and a deeper folder beats a
        shallower one — which is the whole of the precedence rule, written as a
        number so the comparison cannot be re-derived differently in two
        places.
        """
        if self.scope_kind == GLOBAL:
            return 0
        if self.scope_kind == CATEGORY:
            return 1
        return 1 + len(_parts(self.scope_value))


@dataclass(frozen=True)
class Policy:
    """The effective answer for one file, and where every part of it came from."""

    relpath: str
    category: str
    preferred_format: str = ""
    preferred_from: str = ""
    protected_original: bool = False
    protected_by: str = ""
    protection_kind: str = ""
    allow_lossy: bool | None = None
    allow_lossless: bool | None = None
    transform_from: str = ""

    @property
    def preferred_label(self) -> str:
        value = self.preferred_format
        return DISPLAY.get(value, value.upper()) if value else ""

    @property
    def explanation(self) -> str:
        """Why this file has the policy it has, in one sentence.

        Never a score and never a bare word. "Protected" without naming the
        folder that protects it is a fact somebody has to go and look up, and
        the looking-up is where they decide the program is guessing.
        """
        if self.protected_original:
            if self.protection_kind == BY_ROOT:
                return (
                    f"{self.protected_by} is a protected root, so nothing here "
                    f"may be queued for change."
                )
            return (
                f"This file is inside {self.protected_by}, which is set to "
                f"preserve originals."
            )
        if self.preferred_format:
            return (
                f"{self.preferred_label} is your preferred format for "
                f"{self.preferred_from}."
            )
        section = SECTION_LABEL.get(SECTION_OF.get(self.category, ""), "")
        if section:
            return f"No format preference is configured for {section}."
        return "No format preference is configured."

    @property
    def stated(self) -> bool:
        """Whether this policy says anything at all about this file."""
        return bool(
            self.preferred_format
            or self.protected_original
            or self.allow_lossy is not None
            or self.allow_lossless is not None
        )


def _parts(relpath: str) -> tuple[str, ...]:
    """Path components, case-folded — the same rule `protected.py` uses.

    Case-insensitive because these libraries live on APFS and SMB as often as
    on ext4, and a protection that stops working when somebody types
    `photos/wedding` is not a protection.
    """
    text = str(relpath).strip().strip("/")
    if not text:
        return ()
    return tuple(
        part.casefold() for part in PurePosixPath(text).parts if part not in {".", ""}
    )


@dataclass(frozen=True)
class Index:
    """Every configured scope, arranged so one path costs its own depth.

    Scanning the whole list per file is fine for a comparison of four and
    quadratic-ish for a page of five hundred against ten thousand protected
    folders — 5,000,000 path comparisons to answer 500 questions. A file's
    folder scopes are exactly the prefixes of its own path, so looking those
    up is `depth` dictionary hits and the number of configured scopes stops
    mattering.
    """

    folders: dict[tuple[str, ...], Scope]
    categories: dict[str, Scope]
    everything: Scope | None

    def applying(self, relpath: str, category: str) -> list[Scope]:
        """The scopes covering this path, narrowest first.

        Component-wise, never `startswith`: the key is a tuple of path parts,
        so `Photos/Wedding` cannot protect `Photos/WeddingExports` — which a
        prefix comparison says it does.
        """
        parts = _parts(relpath)
        found = [
            self.folders[parts[:depth]]
            for depth in range(len(parts), 0, -1)
            if parts[:depth] in self.folders
        ]
        for slug in (category, SECTION_OF.get(category, "")):
            scope = self.categories.get(slug)
            if scope is not None and scope not in found:
                found.append(scope)
        if self.everything is not None:
            found.append(self.everything)
        return found


def index(conn: sqlite3.Connection) -> Index:
    """Read the scope table once and arrange it for lookup."""
    folders: dict[tuple[str, ...], Scope] = {}
    categories: dict[str, Scope] = {}
    everything: Scope | None = None
    for scope in scopes(conn):
        if scope.scope_kind == FOLDER:
            folders[_parts(scope.scope_value)] = scope
        elif scope.scope_kind == CATEGORY:
            categories[scope.scope_value] = scope
        else:
            everything = scope
    return Index(folders=folders, categories=categories, everything=everything)


def scopes(conn: sqlite3.Connection) -> list[Scope]:
    """Every configured scope, narrowest first."""
    rows = conn.execute(
        "SELECT scope_kind, scope_value, preferred_format, preserve_originals,"
        " allow_lossy_transform, allow_lossless_transform"
        " FROM format_policy_scopes"
    ).fetchall()
    found = [
        Scope(
            scope_kind=str(row["scope_kind"]),
            scope_value=str(row["scope_value"]),
            preferred_format=str(row["preferred_format"] or ""),
            preserve_originals=_flag(row["preserve_originals"]),
            allow_lossy_transform=_flag(row["allow_lossy_transform"]),
            allow_lossless_transform=_flag(row["allow_lossless_transform"]),
        )
        for row in rows
    ]
    found.sort(key=lambda scope: (-scope.specificity, scope.scope_value))
    return found


def _flag(value: object) -> bool | None:
    return None if value is None else bool(value)


def resolve(
    conn: sqlite3.Connection,
    relpath: str,
    *,
    category: str = "",
    cached: Index | None = None,
    roots: tuple[str, ...] | None = None,
) -> Policy:
    """The effective policy for one library-relative path.

    `cached` and `roots` are how a page of fifty rows asks fifty times without
    fifty queries: read the scopes once, pass the index in. The answer is
    identical either way — this is a pure function of the scopes and the path.
    """
    from librairy.protected import protected_roots, protecting_root

    slug = category or _category_of(relpath)
    applicable = (index(conn) if cached is None else cached).applying(relpath, slug)

    preferred, preferred_from = "", ""
    for scope in applicable:
        if scope.preferred_format:
            preferred, preferred_from = scope.preferred_format, scope.described
            break
    protect, protect_by = False, ""
    for scope in applicable:
        if scope.preserve_originals is not None:
            protect, protect_by = scope.preserve_originals, scope.described
            break
    lossy, lossless, transform_from = None, None, ""
    for scope in applicable:
        if lossy is None and scope.allow_lossy_transform is not None:
            lossy, transform_from = scope.allow_lossy_transform, scope.described
        if lossless is None and scope.allow_lossless_transform is not None:
            lossless = scope.allow_lossless_transform
            transform_from = transform_from or scope.described

    #  The stronger, separate feature. A protected root stops a folder being
    #  queued for change at all, which certainly includes changing its
    #  representation — so it protects here too, and says which of the two it
    #  is. See this module's docstring for why they are not one thing.
    configured = protected_roots(conn) if roots is None else roots
    root = protecting_root(relpath, configured)
    kind = BY_POLICY if protect else ""
    if root:
        protect, protect_by, kind = True, root, BY_ROOT

    return Policy(
        relpath=str(relpath),
        category=slug,
        preferred_format=preferred,
        preferred_from=preferred_from,
        protected_original=protect,
        protected_by=protect_by if protect else "",
        protection_kind=kind,
        allow_lossy=lossy,
        allow_lossless=lossless,
        transform_from=transform_from,
    )


#  Where a category lives in a filed library, so a path can be read back to the
#  thing it is. Only used when the caller has no category to hand — a
#  `proposals` row or an `items` row usually does.
_TOP_LEVEL = {
    "Music": "music",
    "Music Videos": "music_videos",
    "Movies": "movies",
    "Shows": "shows",
    "Photos": "photos",
    "Documents": "documents",
    "Books": "books",
    "Projects": "projects",
}


def _category_of(relpath: str) -> str:
    parts = [part for part in str(relpath).split("/") if part]
    return _TOP_LEVEL.get(parts[0], "misc") if parts else "misc"


def preferred_for(conn: sqlite3.Connection, category: str) -> str:
    """The preferred format for a whole category, with no path in hand.

    What a Settings page asks. Resolved through the same scope table, so the
    page cannot show one answer while a comparison row shows another.
    """
    slug = SECTION_OF.get(category, category)
    for scope in scopes(conn):
        if scope.scope_kind == FOLDER or not scope.preferred_format:
            continue
        if scope.scope_kind == GLOBAL or scope.scope_value in (
            slug,
            *SECTION_COVERS.get(slug, ()),
        ):
            return scope.preferred_format
    return ""


def set_preferred_format(
    conn: sqlite3.Connection, category: str, value: str
) -> str:
    """Declare — or clear — the preferred existing representation for a category.

    An empty value clears it, and clearing is a real answer: "no preference"
    is the state Photos, Video and Documents ship in, and a page that can only
    ever add preferences is a page that traps somebody in one.
    """
    slug = SECTION_OF.get(category, category)
    if slug not in SECTIONS:
        raise PolicyError(f"{category!r} is not a category with a format policy")
    clean = canonical(value)
    if clean and clean not in KNOWN_FORMATS.get(slug, frozenset()):
        raise PolicyError(
            f"{value!r} is not a format LibrAIry recognises for "
            f"{SECTION_LABEL.get(slug, slug)}"
        )
    _upsert(conn, CATEGORY, slug, preferred_format=clean)
    return clean


def protect_folder(
    conn: sqlite3.Connection, folder: str, *, library_dir=None, preserve: bool = True  # noqa: ANN001
) -> str:
    """Set a folder to preserve its originals.

    Validated on the way in, through the same containment check every other
    path in LibrAIry uses. A scope that escapes the library is a configuration
    mistake, and the moment somebody tries to save it is the only moment they
    can still fix it.
    """
    cleaned = str(folder or "").strip().strip("/")
    if not cleaned:
        raise PolicyError("name a folder inside your library")
    if library_dir is not None:
        from librairy.paths import PathValidationError, validate_relpath

        try:
            target = validate_relpath(library_dir, cleaned, kind="protected folder")
        except PathValidationError as exc:
            raise PolicyError(str(exc)) from exc
        #  And it has to be a folder that is actually there. Containment alone
        #  would accept `etc/passwd` — an absolute path with its leading slash
        #  taken off — and store a protection over a folder nobody has. A
        #  protection somebody believes they have and does not is worse than
        #  no protection, so a typo is refused where it can still be corrected.
        if not target.is_dir():
            raise PolicyError(f"there is no folder called {cleaned} in your library")
    elif ".." in cleaned.split("/") or cleaned.startswith("~") or "\\" in cleaned:
        raise PolicyError("that folder is outside your library")
    _upsert(conn, FOLDER, cleaned, preserve_originals=preserve)
    return cleaned


def unprotect_folder(conn: sqlite3.Connection, folder: str) -> None:
    """Remove a folder scope entirely.

    Deleted rather than set to "not protected", because a row that says nothing
    is a row that will confuse whoever reads this table next.
    """
    cleaned = str(folder or "").strip().strip("/")
    conn.execute(
        "DELETE FROM format_policy_scopes WHERE scope_kind=? AND scope_value=?",
        (FOLDER, cleaned),
    )


def set_transforms(
    conn: sqlite3.Connection,
    category: str,
    *,
    lossy: bool | None = None,
    lossless: bool | None = None,
) -> None:
    """Say whether LibrAIry may ever propose making a new representation.

    A different question from which existing one is preferred, and stored
    separately for that reason. `None` leaves the field as it was — including
    leaving it unstated, which is what every scope starts as and what keeps
    this table from changing behaviour the day it appears.
    """
    slug = SECTION_OF.get(category, category)
    if slug not in SECTIONS:
        raise PolicyError(f"{category!r} is not a category with a format policy")
    _upsert(
        conn, CATEGORY, slug, allow_lossy_transform=lossy, allow_lossless_transform=lossless
    )


def protected_folders(conn: sqlite3.Connection) -> list[str]:
    """The folder scopes set to preserve originals, narrowest first."""
    return [
        scope.scope_value
        for scope in scopes(conn)
        if scope.scope_kind == FOLDER and scope.preserve_originals
    ]


_FIELDS = (
    "preferred_format",
    "preserve_originals",
    "allow_lossy_transform",
    "allow_lossless_transform",
)


def _upsert(conn: sqlite3.Connection, kind: str, value: str, **fields: object) -> None:
    """Write the named fields of one scope, leaving the others alone.

    Per field rather than per row: setting a category's preferred format must
    not silently blank whatever it said about transformations, which is
    exactly what an `INSERT OR REPLACE` of the whole row would do.
    """
    from librairy.planner import utc_now

    now = utc_now()
    conn.execute(
        "INSERT INTO format_policy_scopes(scope_kind, scope_value, created_at, updated_at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT (scope_kind, scope_value) DO NOTHING",
        (kind, value, now, now),
    )
    for name, given in fields.items():
        if given is None or name not in _FIELDS:
            continue
        stored = given if name == "preferred_format" else int(bool(given))
        conn.execute(
            f"UPDATE format_policy_scopes SET {name}=?, updated_at=?"  # noqa: S608
            f" WHERE scope_kind=? AND scope_value=?",
            (stored, now, kind, value),
        )
    if "preferred_format" in fields and fields["preferred_format"] == "":
        #  Clearing is a real answer and needs its own statement: the loop
        #  above skips falsy values so that `set_transforms` can leave a
        #  format alone, and "no preference" would otherwise be unsayable.
        conn.execute(
            "UPDATE format_policy_scopes SET preferred_format='', updated_at=?"
            " WHERE scope_kind=? AND scope_value=?",
            (now, kind, value),
        )


#  The one companion kind whose two halves really are one thing in two
#  encodings. A RAW and its JPEG render *are* alternative representations of a
#  single exposure, and a preference among them is a coherent question.
#
#  Every other kind is two different things that travel together. A Live
#  Photo's MOV is not a smaller HEIC; a subtitle is not a compact film. So a
#  format preference has nothing to say about them, and a preference that
#  reached them would quietly answer a question about *which file to keep* by
#  looking at file extensions.
ALTERNATIVE_ENCODINGS = frozenset({"raw_render"})


def blocking_relationship(conn: sqlite3.Connection, item_ids: list[int]) -> str:
    """The relationship that makes a format preference meaningless here, or "".

    Relationship identity outranks format simplification. `prefer HEIC` must
    never come to mean `set the MOV aside`, and the reason it must not is that
    the MOV is a companion rather than another encoding — which is a fact
    LibrAIry now holds rather than infers.
    """
    if len(item_ids) < 2:
        return ""
    placeholders = ",".join("?" * len(item_ids))
    row = conn.execute(
        f"SELECT kind FROM item_relationships"  # noqa: S608 - counted placeholders
        f" WHERE low_item_id IN ({placeholders}) AND high_item_id IN ({placeholders})"
        f" ORDER BY kind LIMIT 1",
        [*item_ids, *item_ids],
    ).fetchone()
    if row is None:
        return ""
    kind = str(row["kind"])
    return "" if kind in ALTERNATIVE_ENCODINGS else kind


def protected_among(
    conn: sqlite3.Connection, relpaths: list[str], *, category: str = ""
) -> dict[str, Policy]:
    """Which of these are protected, resolved once for the whole list.

    A comparison page asks about every member; reading the scope table once and
    the protected roots once is the difference between a page that costs one
    query and a page that costs one per photograph.
    """
    from librairy.protected import protected_roots

    if not relpaths:
        return {}
    cached, roots = index(conn), protected_roots(conn)
    return {
        relpath: resolve(conn, relpath, category=category, cached=cached, roots=roots)
        for relpath in relpaths
    }


def protecting(
    conn: sqlite3.Connection,
    relpath: str,
    *,
    cached: Index | None = None,
    roots: tuple[str, ...] | None = None,
) -> str:
    """The folder protecting this path from representation change, or "".

    The single question Storage Optimization asks. It used to ask
    `protected.protecting_root` alone, which answers only about the stronger
    protected-root list; a Format Policy folder means the same thing here —
    these originals are not to be traded away — so both are one answer at the
    point of use, and the resolver says which of the two it was.
    """
    found = resolve(conn, relpath, cached=cached, roots=roots)
    return found.protected_by if found.protected_original else ""


def answers(conn: sqlite3.Connection, kind: str, features: dict) -> str:
    """The explicit policy that already answers this learned pattern, or "".

    The authority model, made checkable. Decision Memory says *I have noticed
    what you usually do*; Format Policy says *you told me what you prefer*. An
    explicit instruction outranks an observed habit, always — so a pattern
    like "you have kept FLAC four times" is not offered as a competing
    recommendation while the policy says MP3. It is still true, still recorded,
    and still shown on the learned page as something the policy has answered.

    Deliberately not a deletion and deliberately not a contradiction warning:
    the person may well have been overriding their own policy on purpose, and
    noticing *that* is a later feature that nobody has asked for yet.
    """
    if kind not in {"representation", "allowed"}:
        return ""
    category = str(features.get("category") or "")
    wanted = preferred_for(conn, SECTION_OF.get(category, category))
    if not wanted:
        return ""
    formats = {
        canonical(part) for part in str(features.get("formats") or "").split("+") if part
    }
    if canonical(wanted) not in formats:
        return ""
    label = DISPLAY.get(canonical(wanted), canonical(wanted).upper())
    section = SECTION_LABEL.get(SECTION_OF.get(category, category), category.title())
    return f"Your Format Policy answers this: {label} is your preferred {section} format."

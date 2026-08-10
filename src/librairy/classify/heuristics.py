from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from librairy.classify.photo_names import photo_name
from librairy.config import Settings
from librairy.models import EvidenceEntry
from librairy.naming import EMBEDDED_UUID_RE, is_noise
from librairy.taxonomy import RenderResult, clean_name_from_title, render_destination

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp", ".bmp", ".tiff", ".avif"}
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav"}
EBOOK_EXTS = {".epub", ".mobi", ".djvu", ".azw", ".azw3", ".fb2"}
FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2", ".eot"}
MODEL_EXTS = {".stl", ".obj", ".fbx", ".3mf", ".blend", ".step", ".stp", ".gltf", ".glb"}
PRINT_EXTS = {".gcode", ".nc", ".cnc", ".bgcode"}
PROJECT_MARKERS = {
    ".git",
    ".hg",
    ".svn",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "Dockerfile",
    "docker-compose.yml",
}
SCREENSHOT_RE = re.compile(
    r"^(screenshot|screen shot|screengrab|capture|vlcsnap|snap|scr[-_])", re.I
)
CAMERA_RE = re.compile(r"^(IMG|DSC|DSCN|DSCF|PIC|PICT|GOPR|DJI|MVIMG)", re.I)
# Artwork that belongs to the album/film sitting beside it, not in Photos/.
ARTWORK_STEMS = {
    "albumart",
    "artwork",
    "back",
    "banner",
    "cd",
    "cover",
    "disc",
    "fanart",
    "folder",
    "front",
    "poster",
    "thumb",
    "thumbnail",
}
ARTWORK_COMPANION_EXTS = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav",
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".vob",
    ".epub", ".mobi", ".azw3", ".pdf",
}
# Files that only mean anything next to the media they describe: subtitles,
# playlists, checksums, scene info. Filing one on its own separates it from the
# thing it belongs to, which is worse than leaving it for the owner to decide.
COMPANION_EXTS = {
    ".cue",
    ".idx",
    ".m3u",
    ".m3u8",
    ".md5",
    ".nfo",
    ".sfv",
    ".srt",
    ".ssa",
    ".sub",
    ".vtt",
}
# Folders that say "images live here", not "these images are one event".
GENERIC_IMAGE_PARENTS = {
    "camera",
    "camera roll",
    "dcim",
    "desktop",
    "downloads",
    "images",
    "img",
    "media",
    "photos",
    "pics",
    "pictures",
}
_YMD_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)")
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
BACKUP_RE = re.compile(r"backup|time.machine|system.?backup|incremental|carbon.?copy", re.I)
SEASON_RE = re.compile(r"\bS(?:eason)?\s*0*(\d+)\b", re.I)


@dataclass(frozen=True)
class HeuristicResult:
    category: str
    clean_name: str
    dest_relpath: str | None
    confidence: float
    evidence: tuple[EvidenceEntry, ...]
    fields: dict[str, object]
    hidden_unhide_name: str | None = None
    reason: str | None = None


def classify_path(path: Path, settings: Settings) -> HeuristicResult | None:
    if path.is_file():
        return _classify_file(path, settings)
    if not path.is_dir():
        return None
    files = [file for file in path.rglob("*") if file.is_file() and not file.name.startswith(".")]
    exts = Counter(file.suffix.lower() for file in files)
    stems = [file.stem.lower() for file in files]
    markers = {entry.name for entry in path.iterdir() if entry.name in PROJECT_MARKERS}
    checks = [
        _project(path, settings, markers),
        _backup(path, settings),
        _model_project(path, settings, exts),
        _font_collection(path, settings, exts),
        _ebook_collection(path, settings, exts),
        _screenshot_collection(path, settings, stems),
        _camera_roll(path, settings, stems, exts),
        _season_folder(path, settings),
        _untagged_album(path, settings, exts, stems),
    ]
    return next((result for result in checks if result is not None), None)


def _classify_file(path: Path, settings: Settings) -> HeuristicResult | None:
    suffix = path.suffix.lower()
    stem = path.stem[1:] if path.name.startswith(".") else path.stem
    if suffix in IMAGE_EXTS and SCREENSHOT_RE.match(stem):
        # "Screenshot 2022-03-01 093819.png" carries its own date. Hardcoding 0
        # here filed every screenshot ever taken under a literal Photos/0/.
        named = photo_name(stem, suffix)
        return _result(
            "photos",
            "Screenshots",
            0.88,
            {
                "year": _year_from_name(stem) or "Unknown",
                "event": "Screenshots",
                "clean_name": named.name,
            },
            settings,
            named.reason or "filename matches screenshot pattern",
            hidden=path.name[1:] if path.name.startswith(".") else None,
        )
    if suffix in IMAGE_EXTS:
        return _image_file(path, settings, suffix, stem)
    if suffix in HOME_VIDEO_EXTS and _is_home_video(stem):
        return _home_video(path, settings, suffix, stem)
    if suffix in COMPANION_EXTS:
        # No sibling test any more. It used to require media in the same folder,
        # which fails exactly when it matters most: once an album is committed
        # its tracks are gone and only the .m3u and .nfo are left, so the rule
        # stopped applying and the AI stepped in and invented a release for
        # them. The extension is decisive on its own — a .srt is never a film.
        #
        # Below the threshold on purpose, so this never files itself. Its
        # destination comes from the media it describes, in the association
        # pass; if there is no such media it stays here for the owner to place.
        beside = " for the media beside it" if _has_media_sibling(path) else ""
        return _result(
            "misc",
            path.name,
            0.4,
            {"clean_name": path.name},
            settings,
            f"companion file{beside} — it follows what it describes, not its own name",
        )
    if suffix in MODEL_EXTS | PRINT_EXTS:
        project = _project_name_for(path, settings, stem)
        return _result(
            "projects",
            clean_name_from_title(stem, suffix),
            0.86,
            {"project": project, "clean_name": clean_name_from_title(stem, suffix)},
            settings,
            "3D model / print file",
        )
    if suffix in EBOOK_EXTS:
        return _result(
            "books",
            stem,
            0.85,
            {
                "author": "Unknown Author",
                "title": stem,
                "genre": "General",
                "clean_name": clean_name_from_title(stem, suffix),
            },
            settings,
            "ebook extension",
        )
    if suffix in FONT_EXTS:
        return _result(
            "misc",
            stem,
            0.88,
            {"clean_name": clean_name_from_title(stem, suffix)},
            settings,
            "font extension",
        )
    return None


def _image_file(
    path: Path, settings: Settings, suffix: str, stem: str
) -> HeuristicResult | None:
    """A loose image file. Photos/ unless it is artwork for the media beside it.

    Without this, an image that is not a screenshot fell through every check
    here and landed in the document classifier's unknown-extension branch:
    misc at 0.30, below the threshold, so it never even got a destination.
    """
    if _is_artwork_sidecar(path, stem):
        # Deliberately below the threshold: this file belongs with its album or
        # film, and v1 has no way to move a sidecar along with its media. Left
        # pending so the decision stays with the owner rather than filing the
        # cover of an album under Photos/.
        return _result(
            "misc",
            clean_name_from_title(stem, suffix),
            0.4,
            {"clean_name": clean_name_from_title(stem, suffix)},
            settings,
            f"artwork for the media beside it ({path.parent.name})",
        )

    year = _year_from_name(stem) or _year_from_name(path.parent.name) or "Unknown"
    event = _event_from_parent(path, settings)
    named = photo_name(stem, suffix, event=event)
    if CAMERA_RE.match(stem):
        confidence, detail = 0.88, "camera filename pattern"
    else:
        confidence, detail = 0.85, "image extension"
    return _result(
        "photos",
        named.name,
        confidence,
        {"year": year, "event": event, "clean_name": named.name},
        settings,
        named.reason or detail,
    )


# A clip off a phone or a camera is the same kind of thing as a photo from the
# same afternoon, and belongs in the same folder. Only the containers phones
# actually write; a .mkv or an .avi is somebody's film collection.
HOME_VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".3gp"}


def _is_home_video(stem: str) -> bool:
    """Whether this video is somebody's clip rather than a film to look up.

    Seventeen .MOV files off a phone were being handed to TMDB as film titles,
    and since a UUID matches nothing they came back at 0.65 with no
    destination — proposing to file a home video as
    `Movies/General/255Bea56-53F5-4D71-B0F4-A2F78Cfd5667-(0)/`.

    A bare number is deliberately *not* enough on its own: `1917.mp4` is a
    film, and TMDB can say so. It takes a camera prefix or a UUID.
    """
    if CAMERA_RE.match(stem):
        return True
    return stem != EMBEDDED_UUID_RE.sub(" ", stem) and is_noise(stem)


def _home_video(path: Path, settings: Settings, suffix: str, stem: str) -> HeuristicResult:
    """Filed exactly like a photo, because that is what it sits beside.

    `IMG_0585.MOV` and `IMG_0585.jpeg` came out of the same phone a second
    apart. Sending one to Photos/2024/ and the other to a film catalogue is
    the wrong answer twice.
    """
    year = _year_from_name(stem) or _year_from_name(path.parent.name) or "Unknown"
    event = _event_from_parent(path, settings)
    named = photo_name(stem, suffix, event=event)
    return _result(
        "photos",
        named.name,
        0.85,
        {"year": year, "event": event, "clean_name": named.name},
        settings,
        named.reason or "clip from a phone or camera, not a film",
    )


def _is_artwork_sidecar(path: Path, stem: str) -> bool:
    """cover.jpg is artwork only when there is something for it to be art *of*."""
    if stem.strip().lower().replace("_", " ").replace("-", " ") not in ARTWORK_STEMS:
        return False
    return _has_media_sibling(path)


def _has_media_sibling(path: Path) -> bool:
    try:
        siblings = list(path.parent.iterdir())
    except OSError:
        return False
    return any(
        entry.is_file() and entry.suffix.lower() in ARTWORK_COMPANION_EXTS for entry in siblings
    )


def _project_name_for(path: Path, settings: Settings, stem: str) -> str:
    """The containing folder is the project, unless the file sits loose."""
    parent = path.parent
    try:
        if parent.resolve() == Path(settings.inbox_dir).resolve():
            return _clean(stem)
    except OSError:
        pass
    return _clean(parent.name) if parent.name else _clean(stem)


def _event_from_parent(path: Path, settings: Settings) -> str:
    """Keep the owner's own grouping: the containing folder is the event.

    A generic dumping ground ("Pictures", "DCIM") says nothing, and neither
    does the inbox root itself, so those become Unsorted.
    """
    parent = path.parent
    try:
        if parent.resolve() == Path(settings.inbox_dir).resolve():
            return "Unsorted"
    except OSError:
        pass
    name = parent.name.strip()
    # A UUID is not an event. iMessage gives every attachment its own folder
    # named after one, which filed thirty-two photographs into thirty-two
    # separate folders called Photos/Unknown/01B583D3-1D28-4B3A-…/ — the same
    # noise the grouping already learned to ignore, one layer further down.
    if not name or name.lower() in GENERIC_IMAGE_PARENTS or is_noise(name):
        return "Unsorted"
    return _clean(name)


def _year_from_name(name: str) -> int | None:
    match = _YMD_RE.search(name) or _YEAR_RE.search(name)
    return int(match.group(1)) if match else None


def _project(path: Path, settings: Settings, markers: set[str]) -> HeuristicResult | None:
    if not markers:
        return None
    name = _clean(path.name)
    return _result(
        "projects", name, 0.92, {"project": name, "clean_name": name}, settings, "project markers"
    )


def _backup(path: Path, settings: Settings) -> HeuristicResult | None:
    if not BACKUP_RE.search(path.name):
        return None
    name = _clean(path.name)
    return _result("misc", name, 0.9, {"clean_name": name}, settings, "backup/archive folder")


def _model_project(path: Path, settings: Settings, exts: Counter[str]) -> HeuristicResult | None:
    total = sum(exts.values())
    count = sum(exts[ext] for ext in MODEL_EXTS | PRINT_EXTS)
    if total == 0 or count / total < 0.5:
        return None
    name = _clean(path.name)
    return _result(
        "projects", name, 0.91, {"project": name, "clean_name": name}, settings, "3D/print files"
    )


def _font_collection(path: Path, settings: Settings, exts: Counter[str]) -> HeuristicResult | None:
    total = sum(exts.values())
    count = sum(exts[ext] for ext in FONT_EXTS)
    if total < 3 or count / total < 0.7:
        return None
    name = _clean(path.name)
    return _result("misc", name, 0.92, {"clean_name": name}, settings, "font collection")


def _ebook_collection(path: Path, settings: Settings, exts: Counter[str]) -> HeuristicResult | None:
    total = sum(exts.values())
    count = sum(exts[ext] for ext in EBOOK_EXTS)
    if total < 2 or count / total < 0.65:
        return None
    name = _clean(path.name)
    return _result(
        "books",
        name,
        0.89,
        {"author": "Unknown Author", "title": name, "genre": "General", "clean_name": name},
        settings,
        "ebook collection",
    )


def _screenshot_collection(
    path: Path, settings: Settings, stems: list[str]
) -> HeuristicResult | None:
    frac = _fraction(stems, SCREENSHOT_RE)
    if "screenshot" not in path.name.lower() and frac <= 0.6:
        return None
    return _result(
        "photos",
        "Screenshots",
        0.88,
        {"year": 0, "event": "Screenshots", "clean_name": "Screenshots"},
        settings,
        "screenshot collection",
    )


def _camera_roll(
    path: Path, settings: Settings, stems: list[str], exts: Counter[str]
) -> HeuristicResult | None:
    total = sum(exts.values())
    img_frac = sum(exts[ext] for ext in IMAGE_EXTS) / max(1, total)
    cam_frac = _fraction(stems, CAMERA_RE)
    if img_frac < 0.7 or (
        path.name.lower() not in {"dcim", "camera", "photos", "pictures"} and cam_frac <= 0.5
    ):
        return None
    name = _clean(path.name)
    return _result(
        "photos",
        name,
        0.92,
        {"year": 0, "event": name, "clean_name": name},
        settings,
        "camera roll",
    )


def _season_folder(path: Path, settings: Settings) -> HeuristicResult | None:
    match = SEASON_RE.search(path.name)
    if not match:
        return None
    season = int(match.group(1))
    show = _clean(path.parent.name or "Unknown Show")
    return _result(
        "shows",
        show,
        0.87,
        {
            "show": show,
            "season": season,
            "episode": 1,
            "genre": "General",
            "clean_name": f"Season {season:02d}",
        },
        settings,
        "season folder",
    )


def _untagged_album(
    path: Path, settings: Settings, exts: Counter[str], stems: list[str]
) -> HeuristicResult | None:
    total = sum(exts.values())
    audio_count = sum(exts[ext] for ext in AUDIO_EXTS)
    numbered = [stem for stem in stems if re.match(r"^0*[1-9]\d?\s*[-._]", stem)]
    if total < 3 or audio_count / total < 0.7 or len(numbered) / max(1, len(stems)) < 0.5:
        return None
    album = _clean(path.name)
    return _result(
        "music",
        album,
        0.78,
        {"artist": "Unknown Artist", "album": album, "genre": "General", "clean_name": album},
        settings,
        "untagged album",
    )


def _result(
    category: str,
    clean_name: str,
    confidence: float,
    fields: dict[str, object],
    settings: Settings,
    detail: str,
    hidden: str | None = None,
) -> HeuristicResult:
    rendered = RenderResult(None, "below confidence threshold")
    if confidence >= settings.confidence_threshold:
        rendered = render_destination(category, fields, library_root=settings.library_dir)
    return HeuristicResult(
        category,
        clean_name,
        rendered.relpath,
        confidence,
        (EvidenceEntry("heuristic", "category", detail, confidence),),
        fields,
        hidden,
        rendered.reason,
    )


def _fraction(stems: list[str], pattern: re.Pattern[str]) -> float:
    return sum(1 for stem in stems if pattern.match(stem)) / max(1, len(stems))


def _clean(value: str) -> str:
    return clean_name_from_title(value.replace("_", " "))

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.models import EvidenceEntry
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
        return _result(
            "photos",
            "Screenshots",
            0.88,
            {"year": 0, "event": "Screenshots", "clean_name": clean_name_from_title(stem, suffix)},
            settings,
            "filename matches screenshot pattern",
            hidden=path.name[1:] if path.name.startswith(".") else None,
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

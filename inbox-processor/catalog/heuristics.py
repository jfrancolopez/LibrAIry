"""
Rule-based pre-classifier that handles obvious cases WITHOUT AI or catalog APIs.

This runs BEFORE catalog API lookups.  High-confidence pattern matches skip both
the catalog and AI layers entirely — saving time and cost.

Design principle: better to return None (uncertain) than to return a wrong result.
Only classify when the evidence is overwhelming.
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter
from pathlib import Path

from utils import (
    LIBRARY_DIR, AUDIO_EXTS, VIDEO_EXTS,
    normalize_music_genre, sanitize_name, make_base_result,
)

# ── File-type buckets ─────────────────────────────────────────────────────────

_IMAGE_EXTS   = frozenset({'jpg','jpeg','png','gif','heic','webp','bmp','tiff','tif','raw','cr2','nef','arw','dng','avif'})
_DOC_EXTS     = frozenset({'pdf','epub','mobi','djvu','doc','docx','odt','rtf','txt'})
_EBOOK_EXTS   = frozenset({'epub','mobi','djvu','azw','azw3','fb2'})
_FONT_EXTS    = frozenset({'ttf','otf','woff','woff2','eot'})
_MODEL_EXTS   = frozenset({'stl','obj','fbx','3mf','blend','step','stp','iges','igs','dae','gltf','glb'})
_PRINT_EXTS   = frozenset({'gcode','nc','cnc','bgcode'})
_ARCHIVE_EXTS = frozenset({'zip','7z','rar','tar','gz','bz2','xz','tgz','tbz','txz'})
_CODE_EXTS    = frozenset({'py','js','ts','jsx','tsx','java','cpp','c','h','go','rs','rb','php','swift','kt','cs','sh','bash','ps1','bat'})

# Filename patterns that indicate screenshots
_SCREENSHOT_RE = re.compile(
    r'^(screenshot|screen shot|screengrab|capture|vlcsnap|snap|scr[-_]|'
    r'screen[-_]|print[-_]?screen|prtsc)',
    re.I,
)

# Camera-roll filename patterns: IMG_1234, DSC_1234, DSCN_1234, GOPR, etc.
_CAMERA_RE = re.compile(
    r'^(IMG|DSC|DSCN|DSCF|PIC|PICT|P\d{4}|GOPR|GH\d{2}|DJI|'
    r'P_\d{8}|IMG_E?\d{4,}|MVIMG)',
    re.I,
)

# System / backup folder name patterns
_BACKUP_FOLDER_RE = re.compile(
    r'backup|time.machine|windows.?backup|system.?backup|'
    r'time\s*capsule|incremental|full.?backup|carbon.?copy',
    re.I,
)

# OS root-like folder structure markers
_OS_STRUCTURE_RE = re.compile(
    r'^(users?|home|documents and settings|program files|'
    r'windows|system32|syswow64|appdata|library|etc|var|usr|opt|bin|sbin|'
    r'volumes|private|applications)$',
    re.I,
)

# Season folder patterns
_SEASON_RE = re.compile(r'\bS(?:eason)?\s*0*(\d+)\b', re.I)

# Project markers (files/folders that indicate a software project)
_PROJECT_MARKERS = frozenset({
    '.git', '.hg', '.svn',
    'package.json', 'package-lock.json', 'yarn.lock',
    'Makefile', 'CMakeLists.txt', 'setup.py', 'pyproject.toml',
    'Cargo.toml', 'go.mod', 'build.gradle', 'pom.xml',
    '.xcode', '.xcodeproj', '.xcworkspace',
    'Dockerfile', 'docker-compose.yml',
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ext(filename: str) -> str:
    return Path(filename).suffix.lstrip('.').lower()


def _scan_folder(folder: Path, max_files: int = 500):
    """
    Return (files, ext_counter, name_stems, has_hidden, markers) for a folder.

    files        — list of Path objects
    ext_counter  — Counter of extensions
    name_stems   — list of lowercase file stems
    has_hidden   — any file starts with '.'
    markers      — set of project-marker names found at root level
    """
    files: list[Path] = []
    exts: Counter = Counter()
    stems: list[str] = []
    has_hidden = False
    markers: set[str] = set()

    # Check root-level markers
    try:
        for entry in folder.iterdir():
            if entry.name in _PROJECT_MARKERS:
                markers.add(entry.name)
            if entry.name.startswith('.') and entry.is_dir():
                markers.add(entry.name)  # catches .git, .hg etc.
    except PermissionError:
        pass

    # Walk contents
    count = 0
    for f in folder.rglob('*'):
        if not f.is_file():
            continue
        if f.name.startswith('.'):
            has_hidden = True
            continue
        if f.name in {'Thumbs.db', 'desktop.ini', '.DS_Store'}:
            continue
        ext = _ext(f.name)
        exts[ext] += 1
        stems.append(f.stem.lower())
        files.append(f)
        count += 1
        if count >= max_files:
            break

    return files, exts, stems, has_hidden, markers


def _dominant(counter: Counter, threshold: float = 0.6) -> tuple[str, float]:
    """Return (top_key, fraction) if one key dominates by threshold."""
    total = sum(counter.values())
    if not total:
        return '', 0.0
    top_key, top_count = counter.most_common(1)[0]
    frac = top_count / total
    return top_key, frac if frac >= threshold else 0.0


def _fraction_matching(stems: list[str], pattern: re.Pattern) -> float:
    if not stems:
        return 0.0
    return sum(1 for s in stems if pattern.match(s)) / len(stems)


def _folder_name_lower(path) -> str:
    return Path(path).name.lower()


def _is_likely_hidden_file(name: str) -> bool:
    return name.startswith('.')


# ── Result builders ───────────────────────────────────────────────────────────

def _make(bundle_type, suggested_name, path, confidence, reasoning,
          genre, category, storage_zone, source_path, is_folder,
          tags=None, files_list=None, year=None):
    return make_base_result(
        bundle_type=bundle_type,
        suggested_name=suggested_name,
        recommended_path=path,
        confidence=confidence,
        reasoning=reasoning,
        genre=genre,
        category=category,
        storage_zone=storage_zone,
        source_path=source_path,
        is_folder=is_folder,
        tags=(tags or []) + ['heuristic'],
        files=files_list or [],
        metadata_extra={'year': year},
    )


# ── Individual classifiers ────────────────────────────────────────────────────

def _classify_system_backup(folder: Path, folder_name_lc: str) -> dict | None:
    """Backup / system export folders."""
    if not _BACKUP_FOLDER_RE.search(folder_name_lc):
        return None
    suggested = sanitize_name(folder.name)
    path = f"{LIBRARY_DIR}/ROM/Archives/{suggested}/"
    return _make(
        'Archive', suggested, path, 0.90,
        f"Folder name '{folder.name}' matches backup pattern — classified as archive",
        'General', 'Archive', 'ROM', str(folder), True,
        tags=['backup', 'archive'],
    )


def _classify_os_structure(folder: Path, exts: Counter) -> dict | None:
    """Folders that look like exported OS directory trees."""
    root_parts = [p.lower() for p in folder.parts]
    os_hits = sum(1 for p in folder.iterdir() if _OS_STRUCTURE_RE.match(p.name)) if folder.exists() else 0
    if os_hits < 3:
        return None
    suggested = sanitize_name(folder.name)
    path = f"{LIBRARY_DIR}/ROM/Archives/{suggested}/"
    return _make(
        'Archive', suggested, path, 0.88,
        f"Root contains {os_hits} OS-style subdirectories — classified as system export",
        'General', 'Archive', 'ROM', str(folder), True,
        tags=['system', 'backup', 'archive'],
    )


def _classify_software_project(folder: Path, markers: set) -> dict | None:
    """Source code / software project."""
    if not markers:
        return None
    found = markers & _PROJECT_MARKERS
    if not found:
        return None
    suggested = sanitize_name(folder.name)
    path = f"{LIBRARY_DIR}/ROM/Misc/Code/{suggested}/"
    return _make(
        'SoftwareProject', suggested, path, 0.92,
        f"Project markers found: {', '.join(sorted(found))}",
        'Code', 'Code', 'ROM', str(folder), True,
        tags=['code', 'software', 'project'],
    )


def _classify_3d_project(folder: Path, exts: Counter) -> dict | None:
    """3D model + print project."""
    model_count = sum(exts[e] for e in _MODEL_EXTS if e in exts)
    print_count  = sum(exts[e] for e in _PRINT_EXTS  if e in exts)
    total = sum(exts.values())
    if total == 0 or (model_count + print_count) / total < 0.5:
        return None
    suggested = sanitize_name(folder.name)
    path = f"{LIBRARY_DIR}/RAM/3dModels/Projects/{suggested}/"
    return _make(
        'ModelBundle', suggested, path, 0.91,
        f"{model_count} model files + {print_count} print files",
        'General', 'Model', 'RAM', str(folder), True,
        tags=['3d', 'model', 'print'],
    )


def _classify_font_collection(folder: Path, exts: Counter) -> dict | None:
    font_count = sum(exts[e] for e in _FONT_EXTS if e in exts)
    total = sum(exts.values())
    if total < 3 or font_count / total < 0.7:
        return None
    suggested = sanitize_name(folder.name)
    path = f"{LIBRARY_DIR}/ROM/Misc/Fonts/{suggested}/"
    return _make(
        'FontCollection', suggested, path, 0.92,
        f"{font_count}/{total} files are fonts",
        'General', 'Font', 'ROM', str(folder), True,
        tags=['fonts', 'design'],
    )


def _classify_ebook_collection(folder: Path, exts: Counter) -> dict | None:
    ebook_count = sum(exts[e] for e in _EBOOK_EXTS if e in exts)
    total = sum(exts.values())
    if total < 2 or ebook_count / total < 0.65:
        return None
    suggested = sanitize_name(folder.name)
    path = f"{LIBRARY_DIR}/ROM/Documents/Books/{suggested}/"
    return _make(
        'DocumentSet', suggested, path, 0.89,
        f"{ebook_count}/{total} files are ebooks",
        'Books', 'Document', 'ROM', str(folder), True,
        tags=['ebooks', 'books', 'documents'],
    )


def _classify_screenshot_collection(folder: Path, stems: list[str]) -> dict | None:
    """Standalone screenshot folder."""
    folder_lc = folder.name.lower()
    name_is_screenshots = 'screenshot' in folder_lc or 'screen shot' in folder_lc
    frac = _fraction_matching(stems, _SCREENSHOT_RE)
    if not (name_is_screenshots or frac > 0.6):
        return None
    path = f"{LIBRARY_DIR}/ROM/Images/Screenshots/"
    return _make(
        'Screenshot', 'Screenshots', path, 0.88,
        f"Folder name '{folder.name}' and {frac*100:.0f}% of files match screenshot pattern",
        'General', 'Image', 'ROM', str(folder), True,
        tags=['screenshot', 'image'],
    )


def _classify_camera_roll(folder: Path, stems: list[str], exts: Counter) -> dict | None:
    """Camera roll / DCIM."""
    folder_lc = folder.name.lower()
    dcim_name = folder_lc in ('dcim', 'camera', 'photos', 'pictures')
    img_frac   = sum(exts[e] for e in _IMAGE_EXTS if e in exts) / max(1, sum(exts.values()))
    cam_frac   = _fraction_matching(stems, _CAMERA_RE)

    if img_frac < 0.7:
        return None
    if not (dcim_name or cam_frac > 0.5):
        return None

    suggested = sanitize_name(folder.name)
    path = f"{LIBRARY_DIR}/ROM/Photos/Camera/{suggested}/"
    conf = 0.92 if dcim_name else round(0.72 + cam_frac * 0.20, 3)
    return _make(
        'PhotoAlbum', suggested, path, conf,
        f"Camera roll: {img_frac*100:.0f}% images, {cam_frac*100:.0f}% camera-named",
        'Camera', 'Image', 'ROM', str(folder), True,
        tags=['photo', 'camera', 'personal'],
    )


def _classify_season_folder(folder: Path) -> dict | None:
    """Folder named 'Season XX' or 'S01'."""
    m = _SEASON_RE.search(folder.name)
    if not m:
        return None
    season_num = int(m.group(1))
    # Show name is the parent folder
    show_name = sanitize_name(folder.parent.name) if folder.parent.name else 'Unknown_Show'
    suggested = show_name
    path = f"{LIBRARY_DIR}/RAM/Shows/General/{show_name}/Season_{season_num:02d}/"
    return _make(
        'TVShow', suggested, path, 0.87,
        f"Folder name matches Season {season_num} pattern",
        'General', 'Video', 'RAM', str(folder), True,
        tags=['tv', 'show', f'season{season_num:02d}'],
    )


def _classify_music_untagged(folder: Path, files: list[Path], exts: Counter, stems: list[str]) -> dict | None:
    """Audio folder with sequential track numbering but no catalog match yet."""
    audio_count = sum(exts[e] for e in AUDIO_EXTS if e in exts)
    total = sum(exts.values())
    if total < 3 or audio_count / total < 0.7:
        return None

    # Check sequential numbering: stems like "01 - title", "02_title", etc.
    numbered = [s for s in stems if re.match(r'^0*[1-9]\d?\s*[-._]', s)]
    frac_numbered = len(numbered) / max(1, len(stems))
    if frac_numbered < 0.5:
        return None

    suggested = sanitize_name(folder.name)
    path = f"{LIBRARY_DIR}/RAM/Music/General/Albums/{suggested}/"
    return _make(
        'MusicAlbum', suggested, path, 0.78,
        f"{audio_count} audio files, {frac_numbered*100:.0f}% have track numbers — likely album",
        'General', 'Music', 'RAM', str(folder), True,
        tags=['music', 'album', 'untagged'],
    )


# ── Hidden file pass ──────────────────────────────────────────────────────────

def flag_hidden_files(result: dict) -> dict:
    """
    Add unhide=True and strip the leading dot from rename_to for any hidden file
    entries.  Operates on the files[] list of any result dict.
    """
    for f in result.get('files', []):
        name = f.get('original_name', '') or Path(f.get('original_path', '')).name
        if name.startswith('.') and len(name) > 1:
            f['unhide'] = True
            rename = f.get('rename_to', name)
            if rename.startswith('.'):
                f['rename_to'] = rename[1:]
    return result


# ── Single-file classifiers ───────────────────────────────────────────────────

def _classify_single_file(file: Path) -> dict | None:
    """Rule-based classification for a single file."""
    ext = _ext(file.name)
    stem_lc = file.stem.lower()
    is_hidden = file.name.startswith('.')
    name = file.name[1:] if is_hidden else file.name  # strip leading dot for name

    # Screenshot
    if ext in _IMAGE_EXTS and _SCREENSHOT_RE.match(stem_lc):
        path = f"{LIBRARY_DIR}/ROM/Images/Screenshots/"
        r = _make('Screenshot', sanitize_name(file.stem), path, 0.88,
                  "Filename matches screenshot pattern",
                  'General', 'Image', 'ROM', str(file), False)
        if is_hidden:
            r['files'] = [{'original_path': str(file), 'original_name': file.name,
                           'rename_to': name, 'unhide': True,
                           'category': 'Image', 'recommended_path': path,
                           'track_number': None, 'file_size': '?',
                           'file_extension': ext, 'keep_original': False,
                           'needs_processing': False, 'metadata': {}}]
        return r

    # Ebook
    if ext in _EBOOK_EXTS:
        suggested = sanitize_name(file.stem)
        path = f"{LIBRARY_DIR}/ROM/Documents/Books/{suggested}/"
        return _make('DocumentSet', suggested, path, 0.85,
                     f"File extension '{ext}' is an ebook format",
                     'Books', 'Document', 'ROM', str(file), False)

    # Font
    if ext in _FONT_EXTS:
        suggested = sanitize_name(file.stem)
        path = f"{LIBRARY_DIR}/ROM/Misc/Fonts/{suggested}/"
        return _make('FontCollection', suggested, path, 0.88,
                     f"File extension '{ext}' is a font format",
                     'General', 'Font', 'ROM', str(file), False)

    return None


# ── Main entry point ──────────────────────────────────────────────────────────

def classify(item_path: str) -> dict | None:
    """
    Try to classify item_path using fast local heuristics.

    Returns a step3-compatible result dict, or None if uncertain.
    Hidden files within the result are automatically flagged for un-hiding.
    """
    p = Path(item_path)

    if p.is_file():
        result = _classify_single_file(p)
        if result and p.name.startswith('.'):
            flag_hidden_files(result)
        return result

    if not p.is_dir():
        return None

    files, exts, stems, has_hidden, markers = _scan_folder(p)
    folder_name_lc = p.name.lower()

    # Run classifiers in priority order (most specific / most confident first)
    checks = [
        _classify_software_project(p, markers),
        _classify_system_backup(p, folder_name_lc),
        _classify_os_structure(p, exts),
        _classify_3d_project(p, exts),
        _classify_font_collection(p, exts),
        _classify_ebook_collection(p, exts),
        _classify_screenshot_collection(p, stems),
        _classify_camera_roll(p, stems, exts),
        _classify_season_folder(p),
        _classify_music_untagged(p, files, exts, stems),
    ]

    result = next((r for r in checks if r is not None), None)

    if result:
        flag_hidden_files(result)

    return result

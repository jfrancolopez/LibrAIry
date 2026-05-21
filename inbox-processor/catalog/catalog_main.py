#!/usr/bin/env python3
"""
LibrAIry Catalog Lookup — entry point for step3_classify.sh

Usage: catalog_main.py <item_path> <output_json>

Exit 0 + writes output_json : catalog matched (AI not needed)
Exit 1                       : no catalog match (AI fallback required)

Priority:
  1. Library index   — existing library consulted first to preserve genre consistency
  2. Catalog APIs    — MusicBrainz/AcoustID (music), TMDB (video)
"""
import os
import sys
import json
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from utils import AUDIO_EXTS, VIDEO_EXTS, LIBRARY_DIR, sanitize_name

# Heuristics run first — no network, no cost.
import heuristics as _heuristics

# Library index built once at module load time.
from library_index import LibraryIndex
_index = LibraryIndex()
_index.build()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _apply_consistency(result: dict) -> dict:
    """
    Override genre and path in `result` if the library index shows this artist /
    title already lives somewhere specific.  Returns the (possibly modified) dict.

    This guarantees that re-runs always route the same artist to the same genre
    folder, preventing structural drift.
    """
    if result is None:
        return result

    bundle_type = result.get('bundle_type', '')
    original_genre = result.get('genre', 'General')

    # ── Music ─────────────────────────────────────────────────────────────────
    if bundle_type in ('MusicAlbum', 'MusicSingle'):
        artist = result.get('_artist')
        hit = _index.lookup_artist(artist) if artist else None
        if hit and hit['genre'] != original_genre:
            new_genre = hit['genre']
            print(
                f"[index] Consistency override: artist='{artist}' "
                f"{original_genre} → {new_genre} (library has it in {new_genre})",
                file=sys.stderr,
            )
            _patch_music_path(result, new_genre)
            result['reasoning'] += (
                f" | Library index: existing copies in {new_genre} — genre enforced"
            )
            result['confidence'] = min(1.0, result.get('confidence', 0.9) + 0.07)
        return result

    # ── Movies ────────────────────────────────────────────────────────────────
    if bundle_type == 'VideoBundle' and result.get('video_context') == 'movie':
        title = result.get('_title')
        hit = _index.lookup_movie(title) if title else None
        if hit and hit['genre'] != original_genre:
            new_genre = hit['genre']
            print(
                f"[index] Consistency override: movie='{title}' "
                f"{original_genre} → {new_genre}",
                file=sys.stderr,
            )
            _patch_video_path(result, new_genre, 'Movies')
            result['reasoning'] += (
                f" | Library index: existing copy in {new_genre} — genre enforced"
            )
            result['confidence'] = min(1.0, result.get('confidence', 0.9) + 0.07)
        return result

    # ── TV shows ──────────────────────────────────────────────────────────────
    if bundle_type == 'TVShow':
        title = result.get('_title')
        hit = _index.lookup_show(title) if title else None
        if hit and hit['genre'] != original_genre:
            new_genre = hit['genre']
            print(
                f"[index] Consistency override: show='{title}' "
                f"{original_genre} → {new_genre}",
                file=sys.stderr,
            )
            _patch_video_path(result, new_genre, 'Shows')
            result['reasoning'] += (
                f" | Library index: existing show in {new_genre} — genre enforced"
            )
            result['confidence'] = min(1.0, result.get('confidence', 0.9) + 0.07)
        return result

    return result


def _patch_music_path(result: dict, new_genre: str):
    """Rewrite recommended_path and per-file paths with the corrected genre."""
    result['genre'] = new_genre
    sname = result.get('suggested_name', '')
    bundle_type = result.get('bundle_type', '')
    sub = 'Singles' if bundle_type == 'MusicSingle' else 'Albums'
    new_path = f"{LIBRARY_DIR}/RAM/Music/{new_genre}/{sub}/{sname}/"
    result['recommended_path'] = new_path
    for f in result.get('files', []):
        f['recommended_path'] = new_path


def _patch_video_path(result: dict, new_genre: str, media_root: str):
    """Rewrite recommended_path for a movie or TV result."""
    result['genre'] = new_genre
    sname = result.get('suggested_name', '')
    season_suffix = ''
    if result.get('bundle_type') == 'TVShow':
        sfp = result.get('subfolder_plan', {})
        if sfp.get('enabled'):
            # Extract Season_NN from the existing path
            old_path = result.get('recommended_path', '')
            import re
            m = re.search(r'(Season_\d+)', old_path)
            season_suffix = f"/{m.group(1)}" if m else ''
    new_path = f"{LIBRARY_DIR}/RAM/{media_root}/{new_genre}/{sname}{season_suffix}/"
    result['recommended_path'] = new_path
    for f in result.get('files', []):
        f['recommended_path'] = new_path


def _dominant_type(path):
    """Return ('audio'|'video'|'other', first_file_or_None) for a directory."""
    counts = {'audio': 0, 'video': 0}
    first = {'audio': None, 'video': None}
    for f in Path(path).rglob('*'):
        if not f.is_file():
            continue
        ext = f.suffix.lstrip('.').lower()
        if ext in AUDIO_EXTS:
            counts['audio'] += 1
            if first['audio'] is None:
                first['audio'] = f
        elif ext in VIDEO_EXTS:
            counts['video'] += 1
            if first['video'] is None:
                first['video'] = f
    if counts['audio'] >= counts['video'] and counts['audio'] > 0:
        return 'audio', first['audio']
    if counts['video'] > 0:
        return 'video', first['video']
    return 'other', None


def lookup(item_path):
    """
    Return a classification dict (with consistency applied) or None.

    Priority:
      1. Heuristics   — fast local pattern matching, no network
      2. Library index override — keep genre consistent with existing library
      3. Catalog APIs — MusicBrainz/AcoustID (music), TMDB (video)
    """
    # ── Step 1: Heuristics ────────────────────────────────────────────────────
    heuristic_result = _heuristics.classify(item_path)
    if heuristic_result:
        confidence = heuristic_result.get('confidence', 0.0)
        bundle_type = heuristic_result.get('bundle_type', '')
        print(
            f"[heuristic] ✓ {bundle_type} (confidence: {confidence}) — "
            f"{heuristic_result.get('reasoning', '')[:80]}",
            file=sys.stderr,
        )
        # Still apply consistency check (index may override the path/genre)
        return _apply_consistency(heuristic_result)

    p = Path(item_path)

    if p.is_file():
        ext = p.suffix.lstrip('.').lower()
        if ext in AUDIO_EXTS:
            from music_lookup import MusicLookup
            result = MusicLookup().lookup_file(item_path)
        elif ext in VIDEO_EXTS:
            from video_lookup import VideoLookup
            result = VideoLookup().lookup(item_path)
        else:
            return None

    elif p.is_dir():
        dom_type, sample = _dominant_type(item_path)
        if dom_type == 'audio':
            from music_lookup import MusicLookup
            result = MusicLookup().lookup_folder(item_path, sample)
        elif dom_type == 'video':
            from video_lookup import VideoLookup
            result = VideoLookup().lookup(item_path)
        else:
            return None

    else:
        return None

    _heuristics.flag_hidden_files(result)
    return _apply_consistency(result)


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <item_path> <output_json>", file=sys.stderr)
        sys.exit(2)

    item_path = sys.argv[1]
    output_json = sys.argv[2]

    if not Path(item_path).exists():
        print(f"[catalog] Path does not exist: {item_path}", file=sys.stderr)
        sys.exit(1)

    result = lookup(item_path)

    if result:
        result['source_path'] = item_path
        result['is_folder'] = Path(item_path).is_dir()
        # Strip internal fields before writing (not needed downstream)
        result.pop('_artist', None)
        result.pop('_title', None)
        with open(output_json, 'w', encoding='utf-8') as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
        print(
            f"[catalog] ✓ Matched: {result.get('bundle_type')} → {result.get('recommended_path')}",
            file=sys.stderr,
        )
        sys.exit(0)

    print(f"[catalog] No match for: {item_path}", file=sys.stderr)
    sys.exit(1)


if __name__ == '__main__':
    main()

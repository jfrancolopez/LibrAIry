#!/usr/bin/env python3
"""
LibrAIry Catalog Lookup — entry point for step3_classify.sh

Usage: catalog_main.py <item_path> <output_json>

Exit 0 + writes output_json : catalog matched (AI not needed)
Exit 1                       : no catalog match (AI fallback required)
"""
import os
import sys
import json
from pathlib import Path

# Ensure the catalog package directory is on sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from utils import AUDIO_EXTS, VIDEO_EXTS


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
    """Return a classification dict or None."""
    p = Path(item_path)

    if p.is_file():
        ext = p.suffix.lstrip('.').lower()
        if ext in AUDIO_EXTS:
            from music_lookup import MusicLookup
            return MusicLookup().lookup_file(item_path)
        if ext in VIDEO_EXTS:
            from video_lookup import VideoLookup
            return VideoLookup().lookup(item_path)
        return None

    if p.is_dir():
        dom_type, sample = _dominant_type(item_path)
        if dom_type == 'audio':
            from music_lookup import MusicLookup
            return MusicLookup().lookup_folder(item_path, sample)
        if dom_type == 'video':
            from video_lookup import VideoLookup
            return VideoLookup().lookup(item_path)
        return None

    return None


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
        with open(output_json, 'w', encoding='utf-8') as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
        print(f"[catalog] ✓ Matched: {result.get('bundle_type')} → {result.get('recommended_path')}", file=sys.stderr)
        sys.exit(0)

    print(f"[catalog] No match for: {item_path}", file=sys.stderr)
    sys.exit(1)


if __name__ == '__main__':
    main()

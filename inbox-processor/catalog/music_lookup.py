"""
Music catalog lookup.

Priority:
  1. Embedded ID3/FLAC/AAC tags via ffprobe  — zero cost, works offline
  2. AcoustID fingerprint → MusicBrainz      — free API key, rate-limited
"""
import os
import sys
import json
import re
import subprocess
import time
import urllib.request
import urllib.parse
from pathlib import Path
from collections import Counter

from utils import (
    LIBRARY_DIR, AUDIO_EXTS,
    normalize_music_genre, sanitize_name, format_size, make_base_result,
)

ACOUSTID_KEY = os.environ.get('ACOUSTID_KEY', '')
MB_RATE_LIMIT = float(os.environ.get('MB_RATE_LIMIT', '1.1'))

_last_mb_request = 0.0


# ──────────────────────────────────────────────
# Low-level helpers
# ──────────────────────────────────────────────

def _mb_get(url):
    global _last_mb_request
    elapsed = time.time() - _last_mb_request
    if elapsed < MB_RATE_LIMIT:
        time.sleep(MB_RATE_LIMIT - elapsed)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'LibrAIry/2.0 (https://github.com/jfrancolopez/LibrAIry)',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            _last_mb_request = time.time()
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[music] MusicBrainz request failed {url}: {e}", file=sys.stderr)
        return None


def _extract_tags(file_path):
    """Return a lowercase-keyed dict of audio tags via ffprobe."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_format', str(file_path)],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return {}
        data = json.loads(r.stdout)
        raw = data.get('format', {}).get('tags', {})
        return {k.lower(): v.strip() for k, v in raw.items() if v and v.strip()}
    except Exception as e:
        print(f"[music] ffprobe failed for {file_path}: {e}", file=sys.stderr)
        return {}


def _get_fingerprint(file_path):
    """Return (fingerprint_str, duration_float) using fpcalc (Chromaprint)."""
    try:
        r = subprocess.run(
            ['fpcalc', '-json', str(file_path)],
            capture_output=True, text=True, timeout=90,
        )
        if r.returncode != 0:
            return None, None
        data = json.loads(r.stdout)
        return data.get('fingerprint'), data.get('duration')
    except FileNotFoundError:
        print("[music] fpcalc not found — install chromaprint (apt install chromaprint-utils)", file=sys.stderr)
        return None, None
    except Exception as e:
        print(f"[music] fpcalc failed for {file_path}: {e}", file=sys.stderr)
        return None, None


def _acoustid_lookup(fingerprint, duration):
    """Submit fingerprint to AcoustID, return best recording dict or None."""
    if not ACOUSTID_KEY or not fingerprint or not duration:
        return None
    url = (
        'https://api.acoustid.org/v2/lookup'
        f'?client={urllib.parse.quote(ACOUSTID_KEY)}'
        f'&fingerprint={urllib.parse.quote(str(fingerprint))}'
        f'&duration={int(duration)}'
        '&meta=recordings+releases+releasegroups+tracks+compress'
    )
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'LibrAIry/2.0'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        results = sorted(data.get('results', []), key=lambda r: r.get('score', 0), reverse=True)
        if not results:
            return None
        best = results[0]
        recs = best.get('recordings', [])
        if not recs:
            return None
        rec = recs[0]
        score = best.get('score', 0)

        artists = rec.get('artists', [])
        artist_name = ' & '.join(a.get('name', '') for a in artists) if artists else ''

        releases = rec.get('releases', [])
        album, year, track_num = '', None, None
        if releases:
            rel = releases[0]
            album = rel.get('title', '')
            date = rel.get('date', {})
            if isinstance(date, dict):
                year = date.get('year')
            mediums = rel.get('mediums', [])
            if mediums:
                tracks = mediums[0].get('tracks', [])
                if tracks:
                    track_num = tracks[0].get('position')

        return {
            'recording_id': rec.get('id'),
            'title': rec.get('title', ''),
            'artist': artist_name,
            'album': album,
            'year': year,
            'track': track_num,
            'score': score,
        }
    except Exception as e:
        print(f"[music] AcoustID lookup failed: {e}", file=sys.stderr)
        return None


def _mb_genres(recording_id):
    """Fetch genre tags from MusicBrainz for a recording ID."""
    if not recording_id:
        return []
    url = f'https://musicbrainz.org/ws/2/recording/{recording_id}?inc=tags+genres&fmt=json'
    data = _mb_get(url)
    if not data:
        return []
    genres = [g.get('name', '') for g in data.get('genres', [])]
    tags = [t.get('name', '') for t in data.get('tags', [])]
    return genres + tags


def _pick_genre(raw_genres):
    for g in raw_genres:
        norm = normalize_music_genre(g)
        if norm not in ('General', g.title().replace(' ', '')):
            return norm
    return 'General'


# ──────────────────────────────────────────────
# Result builder
# ──────────────────────────────────────────────

def _year_int(raw):
    """Parse a year from various tag formats."""
    if not raw:
        return None
    m = re.search(r'\b(19\d{2}|20\d{2})\b', str(raw))
    return int(m.group(1)) if m else None


def _build_file_entry(file_path, dest_path, track_num=None, rename_to=None):
    fp = Path(file_path)
    ext = fp.suffix.lstrip('.')
    stem = fp.stem
    if rename_to is None:
        # Strip leading track number from stem
        m = re.match(r'^(\d{1,3})\s*[-._]?\s*', stem)
        clean_stem = sanitize_name(stem[m.end():] if m else stem)
        tn = int(m.group(1)) if m else track_num
        if tn is not None:
            rename_to = f"{tn:02d}_{clean_stem}.{ext}"
        else:
            rename_to = f"{clean_stem}.{ext}"
    try:
        size = format_size(os.path.getsize(file_path))
    except OSError:
        size = '0 B'
    return {
        'original_path': str(file_path),
        'original_name': fp.name,
        'category': 'Audio',
        'rename_to': rename_to,
        'recommended_path': dest_path,
        'track_number': track_num,
        'file_size': size,
        'file_extension': ext,
        'keep_original': False,
        'needs_processing': False,
        'metadata': {},
    }


def _build_result(artist, album, year, genre, track_title, source_path, confidence, reasoning, files=None):
    """Assemble the final step3-compatible JSON dict."""
    artist_c = sanitize_name(artist) if artist else 'Unknown_Artist'
    album_c = sanitize_name(album) if album else None
    year_s = str(year) if year else None

    if album_c:
        parts = [artist_c, album_c]
        if year_s:
            parts.append(year_s)
        suggested_name = '_'.join(parts)
        bundle_type = 'MusicAlbum'
        dest = f"{LIBRARY_DIR}/RAM/Music/{genre}/Albums/{suggested_name}/"
    else:
        title_c = sanitize_name(track_title) if track_title else 'Unknown_Track'
        parts = [artist_c, title_c]
        if year_s:
            parts.append(year_s)
        suggested_name = '_'.join(parts)
        bundle_type = 'MusicSingle'
        dest = f"{LIBRARY_DIR}/RAM/Music/{genre}/Singles/{suggested_name}/"

    is_folder = Path(source_path).is_dir()
    file_entries = files or []

    result = make_base_result(
        bundle_type=bundle_type,
        suggested_name=suggested_name,
        recommended_path=dest,
        confidence=confidence,
        reasoning=reasoning,
        genre=genre,
        category='Music',
        storage_zone='RAM',
        source_path=source_path,
        is_folder=is_folder,
        tags=[artist_c.lower(), genre.lower(), 'catalog'],
        files=file_entries,
        metadata_extra={'year': year},
    )
    result['subcategory'] = 'Albums' if album_c else 'Singles'
    result['_artist'] = artist   # raw artist name — used by library_index consistency check
    return result


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

class MusicLookup:

    def lookup_file(self, file_path):
        """Return classification dict for a single audio file, or None."""
        # Strategy 1: embedded tags
        tags = _extract_tags(file_path)
        artist = tags.get('artist') or tags.get('albumartist') or tags.get('album_artist')
        album = tags.get('album')
        title = tags.get('title')
        year = _year_int(tags.get('date') or tags.get('year'))
        genre_raw = tags.get('genre', '')
        track_raw = tags.get('track', '')
        if '/' in str(track_raw):
            track_raw = str(track_raw).split('/')[0]
        try:
            track_num = int(str(track_raw).strip())
        except (ValueError, TypeError):
            track_num = None

        if artist and (album or title):
            genre = normalize_music_genre(genre_raw) if genre_raw else 'General'
            confidence = 0.92 if (artist and album) else 0.82
            reasoning = f"Embedded tags: artist='{artist}', album='{album or ''}', title='{title or ''}'"
            entry = _build_file_entry(file_path, '', track_num=track_num)
            result = _build_result(artist, album, year, genre, title, file_path, confidence, reasoning)
            entry['recommended_path'] = result['recommended_path']
            result['files'] = [entry]
            return result

        # Strategy 2: AcoustID fingerprint
        if ACOUSTID_KEY:
            fp, dur = _get_fingerprint(file_path)
            if fp and dur:
                match = _acoustid_lookup(fp, dur)
                if match and match['score'] > 0.65:
                    genres = _mb_genres(match.get('recording_id')) if match.get('recording_id') else []
                    genre = _pick_genre(genres) if genres else 'General'
                    confidence = round(match['score'] * 0.95, 3)
                    reasoning = (
                        f"AcoustID fingerprint (score={match['score']:.2f}): "
                        f"'{match.get('artist', '')}' / '{match.get('album', '')}' / '{match.get('title', '')}'"
                    )
                    entry = _build_file_entry(file_path, '', track_num=match.get('track'))
                    result = _build_result(
                        match.get('artist'), match.get('album'), match.get('year'),
                        genre, match.get('title'), file_path, confidence, reasoning,
                    )
                    entry['recommended_path'] = result['recommended_path']
                    result['files'] = [entry]
                    return result

        return None

    def lookup_folder(self, folder_path, sample_file=None):
        """Return classification dict for a music folder, or None."""
        folder = Path(folder_path)
        audio_files = sorted(
            f for f in folder.rglob('*')
            if f.is_file() and f.suffix.lstrip('.').lower() in AUDIO_EXTS
        )
        if not audio_files:
            return None

        artists, albums, years, genres_raw = [], [], [], []
        for af in audio_files:
            tags = _extract_tags(af)
            a = tags.get('artist') or tags.get('albumartist') or tags.get('album_artist')
            al = tags.get('album')
            g = tags.get('genre')
            y = _year_int(tags.get('date') or tags.get('year'))
            if a: artists.append(a)
            if al: albums.append(al)
            if g: genres_raw.append(g)
            if y: years.append(y)

        total = len(audio_files)
        artist = Counter(artists).most_common(1)[0][0] if artists else None
        album = Counter(albums).most_common(1)[0][0] if albums else None
        year = Counter(years).most_common(1)[0][0] if years else None
        genre_raw = Counter(genres_raw).most_common(1)[0][0] if genres_raw else ''
        artist_cov = len(artists) / total

        if artist and artist_cov > 0.5:
            genre = normalize_music_genre(genre_raw) if genre_raw else 'General'
            confidence = min(0.95, 0.70 + artist_cov * 0.25)
            reasoning = (
                f"Folder scan: {total} audio files, "
                f"artist='{artist}' ({int(artist_cov*100)}%), album='{album or '?'}'"
            )
            result = _build_result(artist, album, year, genre, None, folder_path, confidence, reasoning)
            dest = result['recommended_path']
            result['files'] = [_build_file_entry(str(af), dest) for af in audio_files]
            return result

        # Fallback: fingerprint the first audio file for album-level match
        if ACOUSTID_KEY and sample_file:
            single = self.lookup_file(str(sample_file))
            if single:
                single['source_path'] = folder_path
                single['is_folder'] = True
                single['reasoning'] += f' (sample file: {Path(sample_file).name})'
                dest = single['recommended_path']
                single['files'] = [_build_file_entry(str(af), dest) for af in audio_files]
                return single

        return None

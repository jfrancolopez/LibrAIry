"""
Video catalog lookup via TMDB.

Parses the filename for title+year, searches TMDB for movies then TV shows,
and returns a step3-compatible classification dict.
Requires TMDB_KEY env var (free account at themoviedb.org).
"""
import os
import sys
import json
import re
import urllib.request
import urllib.parse
from pathlib import Path

from utils import (
    LIBRARY_DIR, VIDEO_EXTS,
    sanitize_name, format_size, make_base_result,
    MOVIE_GENRE_MAP, TV_GENRE_MAP,
)

TMDB_KEY = os.environ.get('TMDB_KEY', '')
TMDB_BASE = 'https://api.themoviedb.org/3'

# Tokens that indicate quality/source — strip them before title search
_JUNK_RE = re.compile(
    r'\b(WEBRip|BluRay|BDRip|DVDRip|HDTV|WEB[-.]DL|UHD|HDR|SDR|'
    r'1080p|720p|480p|2160p|4K|REMUX|x264|x265|HEVC|AVC|'
    r'AAC|AC3|DTS|DD5\.1|Atmos|TrueHD|PROPER|REPACK|EXTENDED|'
    r'THEATRICAL|UNRATED|DC|YTS|YIFY|RARBG|PSA|MeGusta|EVO|SPARKS|FGT)\b',
    re.I,
)


# ──────────────────────────────────────────────
# Low-level TMDB helpers
# ──────────────────────────────────────────────

def _tmdb_get(endpoint, params=None):
    if not TMDB_KEY:
        return None
    p = {'api_key': TMDB_KEY, 'language': 'en-US'}
    if params:
        p.update(params)
    url = f"{TMDB_BASE}{endpoint}?{urllib.parse.urlencode(p)}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'LibrAIry/2.0'})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[video] TMDB request failed {url}: {e}", file=sys.stderr)
        return None


def _search_movie(title, year=None):
    params = {'query': title}
    if year:
        params['year'] = year
    data = _tmdb_get('/search/movie', params)
    results = (data or {}).get('results', [])
    if not results and year:
        # Retry without year constraint
        data = _tmdb_get('/search/movie', {'query': title})
        results = (data or {}).get('results', [])
    return results[0] if results else None


def _search_tv(title, year=None):
    params = {'query': title}
    if year:
        params['first_air_date_year'] = year
    data = _tmdb_get('/search/tv', params)
    results = (data or {}).get('results', [])
    if not results and year:
        data = _tmdb_get('/search/tv', {'query': title})
        results = (data or {}).get('results', [])
    return results[0] if results else None


# ──────────────────────────────────────────────
# Filename parser
# ──────────────────────────────────────────────

def _parse_video_name(file_path):
    """
    Extract title, year, season, and episode from a video filename or folder name.
    Returns a dict with keys: title, year, season, episode, is_tv_episode.
    """
    p = Path(file_path)
    name = p.stem if p.is_file() else p.name

    # Season/episode marker: S01E02 or s1e2
    se_match = re.search(r'\bS(\d{1,2})E(\d{1,2})\b', name, re.I)

    # Standalone year
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', name)
    year = int(year_match.group(1)) if year_match else None

    # Cut the title at the earliest of: year, S01E02, or first junk token
    cut = len(name)
    if year_match:
        cut = min(cut, year_match.start())
    if se_match:
        cut = min(cut, se_match.start())
    junk = _JUNK_RE.search(name)
    if junk:
        cut = min(cut, junk.start())

    raw_title = name[:cut].rstrip(' .([{-_')
    # Normalise separators
    title = re.sub(r'[._\-]+', ' ', raw_title).strip()
    title = re.sub(r'\s+', ' ', title)

    return {
        'title': title,
        'year': year,
        'season': int(se_match.group(1)) if se_match else None,
        'episode': int(se_match.group(2)) if se_match else None,
        'is_tv_episode': bool(se_match),
    }


# ──────────────────────────────────────────────
# Result builders
# ──────────────────────────────────────────────

def _genre_from_ids(genre_ids, genre_map):
    for gid in (genre_ids or []):
        if gid in genre_map:
            return genre_map[gid]
    return 'General'


def _file_entry(file_path, dest_path, rename_to=None):
    p = Path(file_path)
    ext = p.suffix.lstrip('.')
    if rename_to is None:
        rename_to = f"{sanitize_name(p.stem)}.{ext}"
    try:
        size = format_size(os.path.getsize(file_path))
    except OSError:
        size = '0 B'
    return {
        'original_path': str(file_path),
        'original_name': p.name,
        'category': 'Video',
        'rename_to': rename_to,
        'recommended_path': dest_path,
        'track_number': None,
        'file_size': size,
        'file_extension': ext,
        'keep_original': False,
        'needs_processing': False,
        'metadata': {},
    }


def _build_movie_result(tmdb, file_path, confidence, reasoning, parsed):
    title = tmdb.get('title') or parsed['title']
    raw_year = (tmdb.get('release_date') or '')[:4]
    year = int(raw_year) if raw_year.isdigit() else parsed.get('year')
    genre = _genre_from_ids(tmdb.get('genre_ids', []), MOVIE_GENRE_MAP)
    title_c = sanitize_name(title)
    sname = f"{title_c}_{year}" if year else title_c
    dest = f"{LIBRARY_DIR}/RAM/Movies/{genre}/{sname}/"
    is_folder = Path(file_path).is_dir()
    files = []
    if not is_folder:
        p = Path(file_path)
        ext = p.suffix.lstrip('.')
        files = [_file_entry(file_path, dest, rename_to=f"{sname}.{ext}")]

    result = make_base_result(
        bundle_type='VideoBundle', suggested_name=sname, recommended_path=dest,
        confidence=confidence, reasoning=reasoning, genre=genre,
        category='Video', storage_zone='RAM', source_path=file_path,
        is_folder=is_folder, tags=[genre.lower(), 'movie', 'catalog'],
        files=files, metadata_extra={'year': year},
    )
    result['video_context'] = 'movie'
    result['subcategory'] = 'Movies'
    result['_title'] = title   # raw title — used by library_index consistency check
    return result


def _build_tv_result(tmdb, file_path, confidence, reasoning, parsed):
    title = tmdb.get('name') or parsed['title']
    raw_year = (tmdb.get('first_air_date') or '')[:4]
    year = int(raw_year) if raw_year.isdigit() else parsed.get('year')
    genre = _genre_from_ids(tmdb.get('genre_ids', []), TV_GENRE_MAP)
    title_c = sanitize_name(title)
    season = parsed.get('season')
    episode = parsed.get('episode')

    sname = title_c
    if season:
        dest = f"{LIBRARY_DIR}/RAM/Shows/{genre}/{sname}/Season_{season:02d}/"
    else:
        dest = f"{LIBRARY_DIR}/RAM/Shows/{genre}/{sname}/"

    is_folder = Path(file_path).is_dir()
    files = []
    if not is_folder:
        p = Path(file_path)
        ext = p.suffix.lstrip('.')
        ep_label = f"S{season:02d}E{episode:02d}" if (season and episode) else sanitize_name(p.stem)
        files = [_file_entry(file_path, dest, rename_to=f"{sname}_{ep_label}.{ext}")]

    result = make_base_result(
        bundle_type='TVShow', suggested_name=sname, recommended_path=dest,
        confidence=confidence, reasoning=reasoning, genre=genre,
        category='Video', storage_zone='RAM', source_path=file_path,
        is_folder=is_folder, tags=[genre.lower(), 'tv', 'catalog'],
        files=files, metadata_extra={'year': year},
    )
    result['video_context'] = 'tv_show'
    result['subcategory'] = 'Shows'
    result['_title'] = title   # raw title — used by library_index consistency check
    result['subfolder_plan'] = {
        'enabled': bool(season),
        'map': {},
        'reasoning': f"Season {season}" if season else "TV Show",
    }
    result['actions']['create_subfolders'] = bool(season)
    result['actions']['preserve_structure'] = True
    return result


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

class VideoLookup:

    def lookup(self, file_path):
        """Return classification dict for a video file or folder, or None."""
        if not TMDB_KEY:
            print('[video] TMDB_KEY not set — skipping video catalog lookup', file=sys.stderr)
            return None

        parsed = _parse_video_name(file_path)
        title = parsed.get('title', '').strip()
        if not title or len(title) < 2:
            return None

        # TV episode with S01E02 marker → try TV first
        if parsed.get('is_tv_episode'):
            tv = _search_tv(title, parsed.get('year'))
            if tv:
                vc = tv.get('vote_count', 0)
                conf = round(min(0.93, 0.68 + vc / 4000), 3)
                reasoning = (
                    f"TMDB TV match: '{tv.get('name', '')}' "
                    f"(id={tv.get('id')}, votes={vc})"
                )
                return _build_tv_result(tv, file_path, conf, reasoning, parsed)

        # Movie search
        movie = _search_movie(title, parsed.get('year'))
        if movie:
            vc = movie.get('vote_count', 0)
            conf = round(min(0.93, 0.68 + vc / 8000), 3)
            reasoning = (
                f"TMDB movie match: '{movie.get('title', '')}' "
                f"({(movie.get('release_date') or '')[:4]}) "
                f"(id={movie.get('id')}, votes={vc})"
            )
            return _build_movie_result(movie, file_path, conf, reasoning, parsed)

        # Ambiguous — try TV show search (no S01E02 marker)
        tv = _search_tv(title, parsed.get('year'))
        if tv and tv.get('vote_count', 0) > 20:
            vc = tv.get('vote_count', 0)
            conf = round(min(0.80, 0.55 + vc / 4000), 3)
            reasoning = (
                f"TMDB TV ambiguous match: '{tv.get('name', '')}' "
                f"(id={tv.get('id')}, votes={vc})"
            )
            return _build_tv_result(tv, file_path, conf, reasoning, parsed)

        return None

"""
Library consistency index.

Scans the existing library on startup and builds lookup tables so that
the same artist / movie / show always lands in the same genre folder,
regardless of when files are processed.

Cache: $REPORTS_DIR/library_index.json  (rebuilt if >TTL seconds old)
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from utils import LIBRARY_DIR, AUDIO_EXTS

REPORTS_DIR = os.environ.get('REPORTS_DIR', '/data/reports')
_CACHE_PATH = Path(REPORTS_DIR) / 'library_index.json'
_TTL = int(os.environ.get('LIBRARY_INDEX_TTL', '86400'))   # 24 h default


def _norm(name: str) -> str:
    """Collapse a name to a stable alphanumeric key for fuzzy matching."""
    return re.sub(r'[^a-z0-9]', '', name.lower().strip())


def _ffprobe_artist(file_path) -> str | None:
    """Extract artist tag from one audio file via ffprobe."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', str(file_path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        tags = json.loads(r.stdout).get('format', {}).get('tags', {})
        artist = (
            tags.get('artist') or tags.get('ARTIST') or
            tags.get('albumartist') or tags.get('ALBUMARTIST') or
            tags.get('album_artist') or tags.get('ALBUM_ARTIST')
        )
        return artist.strip() if artist else None
    except Exception:
        return None


def _first_audio(folder: Path):
    """Return the first audio file found inside a folder, or None."""
    for f in folder.rglob('*'):
        if f.is_file() and f.suffix.lstrip('.').lower() in AUDIO_EXTS:
            return f
    return None


def _folder_to_title(folder_name: str) -> str:
    """Convert a sanitized folder name like 'The_Dark_Knight_2008' to 'The Dark Knight'."""
    name = folder_name.replace('_', ' ')
    # Strip trailing 4-digit year
    name = re.sub(r'\s+\d{4}$', '', name).strip()
    return name if name else folder_name


class LibraryIndex:
    """
    Lookup tables built from the existing library structure.

    music:   normalized_artist  → {'genre': str, 'canonical': str, 'paths': [str]}
    movies:  normalized_title   → {'genre': str, 'canonical': str, 'path': str}
    shows:   normalized_title   → {'genre': str, 'canonical': str, 'path': str}
    """

    def __init__(self):
        self.music: dict = {}
        self.movies: dict = {}
        self.shows: dict = {}
        self._loaded = False

    # ── Public API ────────────────────────────────────────────────────────────

    def build(self):
        """Load from cache if fresh, otherwise scan the library."""
        if _CACHE_PATH.exists():
            age = time.time() - _CACHE_PATH.stat().st_mtime
            if age < _TTL:
                try:
                    data = json.loads(_CACHE_PATH.read_text(encoding='utf-8'))
                    self.music = data.get('music', {})
                    self.movies = data.get('movies', {})
                    self.shows = data.get('shows', {})
                    self._loaded = True
                    n = len(self.music) + len(self.movies) + len(self.shows)
                    print(f"[index] Loaded cache ({n} entries, {age/3600:.1f}h old)", file=sys.stderr)
                    return
                except Exception as e:
                    print(f"[index] Cache unreadable ({e}), rebuilding", file=sys.stderr)

        print("[index] Building library index (first run or cache expired)…", file=sys.stderr)
        t0 = time.time()
        self._scan_music()
        self._scan_movies()
        self._scan_shows()
        self._save()
        self._loaded = True
        elapsed = time.time() - t0
        n = len(self.music) + len(self.movies) + len(self.shows)
        print(f"[index] Built: {len(self.music)} artists, {len(self.movies)} movies, "
              f"{len(self.shows)} shows — {elapsed:.1f}s", file=sys.stderr)

    def lookup_artist(self, artist_name: str) -> dict | None:
        """Return {'genre', 'canonical', 'paths'} for a known artist, or None."""
        if not artist_name:
            return None
        return self.music.get(_norm(artist_name))

    def lookup_movie(self, title: str) -> dict | None:
        """Return {'genre', 'canonical', 'path'} for a known movie title, or None."""
        if not title:
            return None
        return self.movies.get(_norm(title))

    def lookup_show(self, title: str) -> dict | None:
        """Return {'genre', 'canonical', 'path'} for a known TV show, or None."""
        if not title:
            return None
        return self.shows.get(_norm(title))

    def register_artist(self, artist_name: str, genre: str, path: str):
        """Record a newly committed artist so future runs stay consistent."""
        if not artist_name:
            return
        key = _norm(artist_name)
        if key not in self.music:
            self.music[key] = {'genre': genre, 'canonical': artist_name, 'paths': []}
        entry = self.music[key]
        if path and path not in entry['paths']:
            entry['paths'].append(path)
        self._save()

    def register_movie(self, title: str, genre: str, path: str):
        """Record a newly committed movie."""
        if not title:
            return
        self.movies[_norm(title)] = {'genre': genre, 'canonical': title, 'path': path}
        self._save()

    def register_show(self, title: str, genre: str, path: str):
        """Record a newly committed TV show."""
        if not title:
            return
        self.shows[_norm(title)] = {'genre': genre, 'canonical': title, 'path': path}
        self._save()

    # ── Scan helpers ──────────────────────────────────────────────────────────

    def _scan_music(self):
        music_root = Path(LIBRARY_DIR) / 'RAM' / 'Music'
        if not music_root.exists():
            return
        for genre_dir in sorted(music_root.iterdir()):
            if not genre_dir.is_dir():
                continue
            genre = genre_dir.name
            # depth-2: Albums/ or Singles/
            for sub_dir in sorted(genre_dir.iterdir()):
                if not sub_dir.is_dir():
                    continue
                # depth-3: individual album/single bundles
                for bundle_dir in sorted(sub_dir.iterdir()):
                    if not bundle_dir.is_dir():
                        continue
                    artist = self._artist_for_bundle(bundle_dir)
                    if not artist:
                        continue
                    key = _norm(artist)
                    if key not in self.music:
                        self.music[key] = {
                            'genre': genre,
                            'canonical': artist,
                            'paths': [],
                        }
                    self.music[key]['paths'].append(str(bundle_dir))

    def _artist_for_bundle(self, bundle_dir: Path) -> str | None:
        """Try to read artist tag from an audio file in the bundle; fall back to folder parse."""
        audio = _first_audio(bundle_dir)
        if audio:
            artist = _ffprobe_artist(audio)
            if artist:
                return artist

        # Fallback: folder name is '{Artist}_{Album}_{Year}' or '{Artist}_{Title}_{Year}'.
        # Since we can't know where the artist name ends, we read the first underscore-segment
        # that does NOT look like a year and is not very short.
        # This is approximate; the tag-based path is always preferred.
        parts = bundle_dir.name.split('_')
        if not parts:
            return None
        # Collect parts until we hit a 4-digit year
        artist_parts = []
        for part in parts:
            if re.fullmatch(r'\d{4}', part):
                break
            artist_parts.append(part)
        if not artist_parts:
            return None
        # Heuristic: take the first half of the remaining parts as the artist
        mid = max(1, len(artist_parts) // 2)
        return ' '.join(artist_parts[:mid])

    def _scan_movies(self):
        movies_root = Path(LIBRARY_DIR) / 'RAM' / 'Movies'
        if not movies_root.exists():
            return
        for genre_dir in sorted(movies_root.iterdir()):
            if not genre_dir.is_dir():
                continue
            genre = genre_dir.name
            for movie_dir in sorted(genre_dir.iterdir()):
                if not movie_dir.is_dir():
                    continue
                title = _folder_to_title(movie_dir.name)
                if not title:
                    continue
                key = _norm(title)
                if key not in self.movies:   # first occurrence wins
                    self.movies[key] = {
                        'genre': genre,
                        'canonical': title,
                        'path': str(movie_dir),
                    }

    def _scan_shows(self):
        shows_root = Path(LIBRARY_DIR) / 'RAM' / 'Shows'
        if not shows_root.exists():
            return
        for genre_dir in sorted(shows_root.iterdir()):
            if not genre_dir.is_dir():
                continue
            genre = genre_dir.name
            for show_dir in sorted(genre_dir.iterdir()):
                if not show_dir.is_dir():
                    continue
                title = _folder_to_title(show_dir.name)
                if not title:
                    continue
                key = _norm(title)
                if key not in self.shows:
                    self.shows[key] = {
                        'genre': genre,
                        'canonical': title,
                        'path': str(show_dir),
                    }

    def _save(self):
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_PATH.write_text(
                json.dumps({
                    'music': self.music,
                    'movies': self.movies,
                    'shows': self.shows,
                    'built_at': time.time(),
                }, indent=2, ensure_ascii=False),
                encoding='utf-8',
            )
        except Exception as e:
            print(f"[index] Warning: could not save cache: {e}", file=sys.stderr)

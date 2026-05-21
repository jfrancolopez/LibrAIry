"""Shared utilities for catalog lookups."""
import re
import os

LIBRARY_DIR = os.environ.get('LIBRARY_DIR', '/data/library')

# Music genre normalization: MusicBrainz/embedded tags → library folder name
MUSIC_GENRE_MAP = {
    'rock': 'Rock', 'hard rock': 'Rock', 'indie rock': 'Rock', 'alternative rock': 'Rock',
    'alternative': 'Rock', 'punk': 'Rock', 'punk rock': 'Rock', 'grunge': 'Rock',
    'pop': 'Pop', 'pop rock': 'Pop', 'dance pop': 'Pop', 'teen pop': 'Pop',
    'hip hop': 'HipHop', 'hip-hop': 'HipHop', 'rap': 'HipHop', 'trap': 'HipHop',
    'drill': 'HipHop', 'hip hop/rap': 'HipHop',
    'jazz': 'Jazz', 'bebop': 'Jazz', 'smooth jazz': 'Jazz', 'fusion': 'Jazz',
    'classical': 'Classical', 'baroque': 'Classical', 'opera': 'Classical',
    'orchestral': 'Classical', 'contemporary classical': 'Classical',
    'electronic': 'Electronic', 'techno': 'Electronic', 'house': 'Electronic',
    'edm': 'Electronic', 'trance': 'Electronic', 'drum and bass': 'Electronic',
    'ambient': 'Electronic', 'synth': 'Electronic', 'synth-pop': 'Electronic',
    'electropop': 'Electronic', 'electronica': 'Electronic', 'dubstep': 'Electronic',
    'metal': 'Metal', 'heavy metal': 'Metal', 'death metal': 'Metal',
    'black metal': 'Metal', 'thrash metal': 'Metal', 'doom metal': 'Metal',
    'progressive metal': 'Metal', 'nu-metal': 'Metal',
    'country': 'Country', 'bluegrass': 'Country', 'country pop': 'Country',
    'r&b': 'RnB', 'rnb': 'RnB', 'soul': 'RnB', 'funk': 'RnB',
    'rhythm and blues': 'RnB', 'neo soul': 'RnB',
    'reggae': 'Reggae', 'dancehall': 'Reggae', 'ska': 'Reggae',
    'folk': 'Folk', 'acoustic': 'Folk', 'singer-songwriter': 'Folk',
    'indie folk': 'Folk', 'folk rock': 'Folk',
    'blues': 'Blues', 'electric blues': 'Blues', 'delta blues': 'Blues',
    'latin': 'Latin', 'salsa': 'Latin', 'bossa nova': 'Latin', 'reggaeton': 'Latin',
    'gospel': 'Gospel', 'christian': 'Gospel', 'christian rock': 'Gospel',
    'new age': 'NewAge', 'meditation': 'NewAge',
    'soundtrack': 'Soundtrack', 'score': 'Soundtrack', 'film score': 'Soundtrack',
    'world': 'World', 'world music': 'World',
    'jazz-funk': 'Jazz', 'jazz fusion': 'Jazz',
    'progressive rock': 'Rock', 'psychedelic rock': 'Rock',
    'disco': 'Pop', 'pop/rock': 'Pop',
}

# TMDB movie genre IDs → library folder name
MOVIE_GENRE_MAP = {
    28: 'Action', 12: 'Adventure', 16: 'Animation', 35: 'Comedy',
    80: 'Crime', 99: 'Documentary', 18: 'Drama', 10751: 'Family',
    14: 'Fantasy', 36: 'History', 27: 'Horror', 10402: 'Music',
    9648: 'Mystery', 10749: 'Romance', 878: 'SciFi', 10770: 'TVMovie',
    53: 'Thriller', 10752: 'War', 37: 'Western',
}

# TMDB TV genre IDs → library folder name
TV_GENRE_MAP = {
    10759: 'Action', 16: 'Animation', 35: 'Comedy', 80: 'Crime',
    99: 'Documentary', 18: 'Drama', 10751: 'Family', 10762: 'Kids',
    9648: 'Mystery', 10763: 'News', 10764: 'Reality', 10765: 'SciFi',
    10766: 'Soap', 10767: 'Talk', 10768: 'War', 37: 'Western',
}

AUDIO_EXTS = frozenset({
    'mp3', 'flac', 'wav', 'ogg', 'aac', 'm4a', 'wma', 'opus',
    'alac', 'ape', 'aiff', 'dsf', 'm4b', 'mka',
})

VIDEO_EXTS = frozenset({
    'mp4', 'mkv', 'avi', 'mov', 'webm', 'wmv', 'm4v', 'flv',
    'mpg', 'mpeg', 'ts', 'm2ts', 'vob', 'mts', 'ogv', '3gp',
})


def normalize_music_genre(genre_str):
    """Normalize a genre string to library music folder name."""
    if not genre_str:
        return 'General'
    g = genre_str.lower().strip()
    # Direct map lookup
    if g in MUSIC_GENRE_MAP:
        return MUSIC_GENRE_MAP[g]
    # Partial match — check if any key is contained in the genre string
    for key, val in MUSIC_GENRE_MAP.items():
        if key in g:
            return val
    # Title-case the raw genre, removing spaces
    return genre_str.split('/')[0].strip().title().replace(' ', '')


def sanitize_name(name):
    """Sanitize a string for use as a folder or file name component."""
    if not name:
        return 'Unknown'
    # Strip common quality/source release tags
    name = re.sub(
        r'\b(WEBRip|BluRay|BDRip|DVDRip|HDTV|WEB[-.]DL|UHD|HDR|SDR|'
        r'1080p|720p|480p|2160p|4K|REMUX|x264|x265|HEVC|AVC|'
        r'AAC|AC3|DTS|DD5\.1|Atmos|TrueHD|PROPER|REPACK|'
        r'EXTENDED|THEATRICAL|UNRATED|DC|YTS|YIFY|RARBG)\b',
        '', name, flags=re.I
    )
    name = re.sub(r'\[[\w.\s]+\]', '', name)   # [GROUP.TAG]
    name = re.sub(r'\([\w.\s]+\)', '', name)   # (GROUP.TAG)
    name = re.sub(r'[^\w\s\-]', ' ', name)    # special chars → space
    name = re.sub(r'[\s\-]+', '_', name)       # spaces/dashes → underscore
    name = re.sub(r'_+', '_', name)
    name = name.strip('_')
    return name if name else 'Unknown'


def format_size(bytes_size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} PB"


def make_base_result(bundle_type, suggested_name, recommended_path, confidence, reasoning,
                     genre, category, storage_zone, source_path, is_folder,
                     tags=None, files=None, metadata_extra=None):
    """Build the full step3-compatible JSON structure."""
    return {
        "bundle_type": bundle_type,
        "suggested_name": suggested_name,
        "recommended_path": recommended_path,
        "confidence": round(confidence, 3),
        "reasoning": reasoning,
        "tags": tags or [],
        "category": category,
        "storage_zone": storage_zone,
        "genre": genre,
        "subcategory": "General",
        "os": "unknown",
        "usecase": "entertainment",
        "platform": "unknown",
        "video_context": None,
        "subfolder_plan": {"enabled": False, "map": {}, "reasoning": "Catalog classification"},
        "actions": {
            "move": True,
            "rename": True,
            "extract_year": True,
            "create_subfolders": False,
            "generate_tags": False,
            "verify_duplicates": True,
            "preserve_structure": is_folder,
            "flatten_hierarchy": not is_folder,
        },
        "warnings": [],
        "recommendations": [],
        "processing_notes": {
            "special_handling": "Catalog",
            "estimated_time_seconds": 5,
            "risk_level": "low",
        },
        "bundle_coherence_score": 1.0,
        "metadata": {
            "year": metadata_extra.get('year') if metadata_extra else None,
            "file_count": len(files) if files else 1,
            "dominant_category": category.lower(),
            "dominant_extension": "unknown",
            "file_type_distribution": {category.lower(): len(files) if files else 1},
            "size_total": "unknown",
            "has_subfolders": is_folder,
            "subfolder_names": [],
            "contains_sensitive_data": False,
            "detected_language": "None",
            **(metadata_extra or {}),
        },
        "files": files or [],
        "related_items": [],
        "source_path": source_path,
        "is_folder": is_folder,
    }

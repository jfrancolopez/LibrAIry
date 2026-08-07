"""What sort of file this is, by extension. One list, not four.

Preview rendering, browse tiles and duplicate comparison all need to know
whether a file is a picture, a video, a song or a document, and they need to
agree — a file previewed as a document but compared as "unsupported" is a bug
waiting to be reported as two unrelated ones.
"""

from __future__ import annotations

from pathlib import Path, PurePath

from librairy.content.extract import TEXT_SUFFIXES

IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic", ".avif", ".webp"}
)
VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac"})
# Anything librairy.content.extract can pull text out of, kept in step with it
# rather than listed twice -- the two lists had drifted, and .csv, .tsv and
# .log were previewed as documents with nothing in the body.
DOCUMENT_EXTENSIONS = frozenset(TEXT_SUFFIXES | {".pdf", ".docx", ".epub"})


def kind_for(path: Path | PurePath | str) -> str:
    """One of: image, video, audio, document, unsupported."""
    suffix = PurePath(str(path)).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in DOCUMENT_EXTENSIONS:
        return "document"
    return "unsupported"

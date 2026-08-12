"""What sort of file this is, by extension. One list, not four.

Preview rendering, browse tiles and duplicate comparison all need to know
whether a file is a picture, a video, a song or a document, and they need to
agree — a file previewed as a document but compared as "unsupported" is a bug
waiting to be reported as two unrelated ones.

**Three registries name extensions, and they answer three different
questions.** They are supposed to disagree, so before widening any of them:

* `filetypes.REGISTRY` — *what does this extension mean?* Reference text for a
  human, 86 entries, shown behind the `?` beside a filename. Wide on purpose:
  it exists precisely for the extensions nothing else understands.
* `mediakind.DOCUMENT_EXTENSIONS` and friends (this module) — *what can
  LibrAIry do with it?* It drives exactly two things: which preview renderer
  runs, and how duplicates are compared. `"unsupported"` is an honest answer
  meaning "no renderer exists", not a judgement about the file.
* `companions.SIDECAR_KINDS` — *does this file belong to another file?* A
  relational role, not a format. It is the narrowest of the three and the only
  one that changes where a file is filed.

Two known, deliberate differences, both verified against the real library and
locked down in `tests/test_taxonomy_boundary.py`:

1. 32 extensions are documents here but absent from the registry — `.c`,
   `.html`, `.svg`, `.tex`, `.tsx` and the rest of the source-and-markup set.
   Informational only. They came in with `TEXT_SUFFIXES` because text can be
   extracted from them; they are not worth 32 hand-written descriptions.
2. Nine office formats — `.doc`, `.docx` aside, plus `.odp`, `.ods`, `.odt`,
   `.ppt`, `.pptx`, `.rtf`, `.xls`, `.xlsx` — are categorised `documents` by
   the classifier and described by the registry, but are `"unsupported"` here.
   That is correct. There is no Pillow and no office renderer in the image, so
   promoting them would draw a preview card promising a page image that cannot
   be produced. The two real `.xlsx` files in the library are filed correctly
   under `documents` at 0.85; nothing is misclassified by this gap.

A sidecar can also be a document here — `.srt` and `.nfo` are both — and that
is fine, because the axes are independent. `filetypes` resolves the display
role by asking `SIDECAR_KINDS` first, so the `?` panel says "companion
(subtitle)" rather than contradicting the classifier.
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

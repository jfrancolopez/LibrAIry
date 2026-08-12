"""Three registries name extensions. This is the wall between them.

`filetypes.REGISTRY` explains a format to a human, `mediakind` says what
LibrAIry can *do* with it, and `companions.SIDECAR_KINDS` says whether it
belongs to another file. They are supposed to disagree, and the disagreements
are listed here so that widening one of them cannot quietly widen another.

The failure this guards against is concrete: the `?` panel exists to describe
strange extensions, so it will keep growing. The day someone "fixes" the
mismatch by pointing the classifier at the registry, every `.c` file in the
library becomes a document and the preview pane starts promising page images
it cannot render.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy import audit, filetypes
from librairy.classify.companions import (
    MEDIA_EXTS,
    NAMES_ONE_FILE,
    SIDECAR_KINDS,
    SIDECAR_LABEL,
    is_companion,
    sidecar_kind,
)
from librairy.classify.documents import DOCUMENT_EXTS
from librairy.mediakind import DOCUMENT_EXTENSIONS, kind_for

SRC = Path(__file__).resolve().parents[1] / "src" / "librairy"


# --- one definition of "companion" --------------------------------------------


def test_the_audit_derives_its_companion_set_from_the_classifier() -> None:
    """Not a copy that happens to agree today: the same extensions, always."""
    assert frozenset(SIDECAR_KINDS) == audit.COMPANION


def test_the_audit_knows_every_sidecar_the_classifier_knows() -> None:
    """The hand-written set had never heard of .ass, .ssa, .vtt or .md5, so a
    subtitle under Music was reported as an unexpected file type."""
    for extension in (".ass", ".ssa", ".vtt", ".md5"):
        assert extension in audit.COMPANION


def test_the_audit_no_longer_calls_a_log_file_a_companion() -> None:
    """It drifted the other way too: `.log` is extractable text to the
    classifier, and only the audit thought it was a sidecar."""
    assert ".log" not in audit.COMPANION
    assert sidecar_kind("rip.log") is None


def test_no_second_companion_extension_set_survives_anywhere() -> None:
    """A literal `".srt"` next to a literal `".nfo"` outside companions.py is
    how the first duplicate started."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "companions.py":
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if '".srt"' in line and '".nfo"' in line:
                offenders.append(f"{path.name}: {line.strip()}")
    assert offenders == []


def test_every_sidecar_kind_has_a_word_for_it() -> None:
    """The Why panel prints the label; a kind without one reads as
    'companion file', which tells the user nothing."""
    for extension, kind in SIDECAR_KINDS.items():
        assert kind in SIDECAR_LABEL, f"{extension} -> {kind}"


# --- sidecar semantics that must not have moved -------------------------------


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("Movie.srt", "subtitle"),
        ("Movie.vtt", "subtitle"),
        ("Release.nfo", "metadata"),
        ("Album.m3u", "playlist"),
        ("Album.cue", "cue"),
        ("Song.lrc", "lyrics"),
    ],
)
def test_known_sidecars_keep_their_kind(name: str, kind: str) -> None:
    assert sidecar_kind(name) == kind
    assert is_companion(name) is True


def test_an_nfo_is_a_companion_even_though_it_is_also_text() -> None:
    """Both axes are true at once. The relational role is the one that decides
    where the file goes, and the one the UI shows."""
    assert kind_for("Release.nfo") == "document"
    assert sidecar_kind("Release.nfo") == "metadata"
    assert filetypes.extension_info("Release.nfo").role == "Companion (metadata file)"


def test_only_sidecars_that_name_one_file_get_renamed_with_it() -> None:
    """A subtitle and a lyrics file are found by filename, so they follow the
    media's final stem. An .m3u or an .nfo describes the release and keeps its
    own name -- two .nfo files in a folder are often the only thing telling
    them apart."""
    assert frozenset({"subtitle", "lyrics"}) == NAMES_ONE_FILE
    assert sidecar_kind("Album.m3u") not in NAMES_ONE_FILE
    assert sidecar_kind("Album.cue") not in NAMES_ONE_FILE


def test_a_sidecar_is_never_media() -> None:
    assert not (set(SIDECAR_KINDS) & MEDIA_EXTS)


# --- registry breadth is informational ----------------------------------------


REGISTRY_EXTENSIONS = frozenset(key for key in filetypes.REGISTRY if key.startswith("."))

# Documents to the extractor, absent from the registry: source and markup that
# text can be pulled out of. Informational only -- nobody needs 32 hand-written
# descriptions of `.hpp`.
TEXT_ONLY = frozenset(
    {
        ".bash", ".c", ".cfg", ".cpp", ".cs", ".css", ".go", ".h", ".hpp",
        ".htm", ".html", ".java", ".jsx", ".kt", ".lua", ".markdown", ".mjs",
        ".org", ".php", ".properties", ".ps1", ".r", ".rb", ".rs", ".rst",
        ".scss", ".svg", ".swift", ".tex", ".tsv", ".tsx", ".zsh",
    }
)

# Categorised `documents` by the classifier, described by the registry, and
# still "unsupported" here -- because there is no renderer for them in the
# image. Promoting them would draw a preview card promising a page image that
# cannot be produced. See the module docstring in mediakind.py.
NO_RENDERER = frozenset(
    {".doc", ".odp", ".ods", ".odt", ".ppt", ".pptx", ".rtf", ".xls", ".xlsx"}
)


def test_the_extractor_document_set_differs_from_the_registry_exactly_here() -> None:
    assert DOCUMENT_EXTENSIONS - REGISTRY_EXTENSIONS == TEXT_ONLY


def test_office_formats_are_documents_to_the_classifier_and_unrenderable_here() -> None:
    """The .xlsx files in a real library are filed correctly. The gap is about
    previews, not about where the file goes."""
    for extension in NO_RENDERER:
        assert extension in DOCUMENT_EXTS, extension
        assert kind_for(f"sheet{extension}") == "unsupported", extension


def test_that_is_the_whole_disagreement() -> None:
    """If someone adds an office format to one side only, this fails."""
    unrenderable = {ext for ext in DOCUMENT_EXTS if kind_for(f"x{ext}") == "unsupported"}
    assert unrenderable == NO_RENDERER


def test_describing_an_extension_does_not_classify_it() -> None:
    """The registry is the widest of the three on purpose, and knows plenty
    the classifier does not -- `.bup`, `.pluginPayloadAttachment`, `.exe`.
    Every one of those still comes back "unsupported" from the axis that
    decides, and describing more of them can never change that."""
    decided = DOCUMENT_EXTENSIONS | frozenset(DOCUMENT_EXTS) | frozenset(SIDECAR_KINDS)
    registry_only = REGISTRY_EXTENSIONS - decided
    assert registry_only, "the registry should know more than the classifier"
    for extension in registry_only:
        assert filetypes.extension_info(f"file{extension}").label, extension
        assert kind_for(f"file{extension}") in {"image", "video", "audio", "unsupported"}
        assert kind_for(f"file{extension}") != "document", extension


def test_the_registry_is_not_imported_by_anything_that_decides() -> None:
    """The import direction is the wall. filetypes reads the classifier; the
    classifier must never read filetypes."""
    deciders = [
        SRC / "mediakind.py",
        SRC / "classify" / "__init__.py",
        SRC / "classify" / "companions.py",
        SRC / "classify" / "documents.py",
        SRC / "audit.py",
        SRC / "planner.py",
    ]
    for path in deciders:
        imports = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith(("import ", "from ")) and "filetypes" in line
        ]
        assert imports == [], f"{path.name}: {imports}"


# --- shapes that must not move ------------------------------------------------


def test_a_mov_is_a_video_not_a_document() -> None:
    """`.mov` sits beside `.jpeg` in phone camera folders and is the one
    extension most likely to be mistaken for something else."""
    assert kind_for("IMG_9323.MOV") == "video"
    assert ".mov" in MEDIA_EXTS


def test_dvd_structure_stays_out_of_every_document_set() -> None:
    """`VTS_01_1.VOB` and its .IFO/.BUP siblings are the disc. Calling any of
    them a document would put a text extractor on a video stream and break the
    one folder shape that must be copied verbatim."""
    for extension in (".vob", ".ifo", ".bup"):
        assert extension not in DOCUMENT_EXTENSIONS, extension
        assert extension not in DOCUMENT_EXTS, extension
        assert extension not in SIDECAR_KINDS, extension
        assert filetypes.extension_info(f"VTS_01_1{extension}").label


def test_the_dvd_extensions_are_still_described_to_the_user() -> None:
    """Protected from classification, not hidden from the person reading the
    row -- `.BUP` is exactly the sort of extension the `?` exists for."""
    assert "DVD" in filetypes.extension_info("VIDEO_TS.BUP").label
    assert "DVD" in filetypes.extension_info("VTS_01_0.IFO").label

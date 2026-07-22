from __future__ import annotations

from pathlib import Path

from librairy.classify.documents import classify_document_like, clean_title
from librairy.config import Settings


def settings_for(tmp_path: Path) -> Settings:
    return Settings(LIBRARY_DIR=tmp_path / "library", CONFIDENCE_THRESHOLD=0.8, _env_file=None)


def test_book_extension_classifies_with_destination(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.library_dir.mkdir()

    result = classify_document_like("Books/Dune.epub", settings=settings)

    assert result.category == "books"
    assert result.confidence >= 0.8
    assert result.dest_relpath == "Books/Unknown Author/Dune/Dune.epub"
    assert result.evidence[0].source == "heuristic"


def test_ambiguous_pdf_is_pending_with_evidence(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.library_dir.mkdir()

    result = classify_document_like("scan001.pdf", settings=settings)

    assert result.category == "documents"
    assert result.dest_relpath is None
    assert result.reason == "below confidence threshold"
    assert result.evidence


def test_project_marker_classifies_project(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.library_dir.mkdir()

    result = classify_document_like("Demo_Project/package.json", settings=settings)

    assert result.category == "projects"
    assert result.dest_relpath == "Projects/Demo Project/Demo Project"


def test_misc_fallback_stays_pending_below_threshold(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.library_dir.mkdir()

    result = classify_document_like("random.bin", settings=settings)

    assert result.category == "misc"
    assert result.dest_relpath is None


def test_clean_name_normalizes_unicode_dots_and_release_tags() -> None:
    assert clean_title("Résumé.2026.PROPER.1080p") == "Résumé 2026"

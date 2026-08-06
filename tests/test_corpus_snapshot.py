from __future__ import annotations

import json
from pathlib import Path

from librairy.classify import classify_item
from librairy.config import Settings
from librairy.naming import tidy_relpath


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        LIBRARY_DIR=tmp_path / "library",
        CONFIDENCE_THRESHOLD=0.8,
        TMDB_KEY="",
        ACOUSTID_KEY="",
        _env_file=None,
    )
    settings.library_dir.mkdir()
    return settings


def test_golden_corpus_snapshot_all_categories(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    files = [
        "unknown.mp3",
        "The.Matrix.1999.mkv",
        "example.show.s02e05.mkv",
        "Screenshot 2026.png",
        "Trip to Lisbon/IMG_4821.jpg",
        "scan001.pdf",
        "Dune.epub",
        "Demo_Project/package.json",
        "random.bin",
    ]
    for relpath in files:
        path = inbox / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")

    settings = settings_for(tmp_path)
    actual = []
    for relpath in files:
        result = classify_item(inbox / relpath, relpath, settings)
        actual.append(
            {
                "category": result.category,
                "dest": result.dest_relpath,
                "name": result.clean_name,
            }
        )

    expected = json.loads(
        Path("tests/fixtures/corpus/expected_proposals.json").read_text(encoding="utf-8")
    )
    assert actual == expected


def test_no_proposed_destination_carries_a_space_or_an_ampersand(tmp_path: Path) -> None:
    """The guarantee, checked against the corpus rather than one template.

    Every name LibrAIry proposes ends up in a URL, a shell word and an SMB
    share. Asserting it here means a new template or a new metadata source
    cannot quietly reintroduce "R&B Soul".
    """
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    files = [
        "Rock & Roll/IMG_1001.jpg",
        "Trip to Lisbon/IMG_4821.jpg",
        "Screenshot from 2022-07-11 14-22-24.png",
        "Familia y Amigos 100% 🎉.jpg",
    ]
    for relpath in files:
        path = inbox / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")

    settings = settings_for(tmp_path)
    destinations = [
        classify_item(inbox / relpath, relpath, settings).dest_relpath for relpath in files
    ]

    assert all(dest for dest in destinations), "every one of these should get a destination"
    for dest in destinations:
        assert dest is not None
        assert " " not in dest, dest
        assert "&" not in dest, dest
        assert dest == tidy_relpath(dest), dest

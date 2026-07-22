from __future__ import annotations

from librairy.ai.redact import RedactedItemView, build_view
from librairy.models import EvidenceEntry, Item


def item(relpath: str = "Vacation 2026 #italy/IMG_001.jpg") -> Item:
    return Item(
        id=1,
        root="inbox",
        relpath=relpath,
        size=2_500_000,
        mtime_ns=1,
        fingerprint="abc",
        state="discovered",
        first_seen_at="2026-01-01T00:00:00Z",
        last_seen_at="2026-01-01T00:00:00Z",
        missing_since=None,
    )


def test_hostile_metadata_cannot_reach_serialized_view() -> None:
    metadata = {
        "tags": {
            "title": "/data/inbox/Paris/secret.jpg",
            "artist": "47.620500 -122.349300",
            "album": "Safe Album",
            "genre": "Travel",
            "GPSLatitude": "47.620500",
            "GPSLongitude": "-122.349300",
            "City": "Paris",
            "Country": "France",
            "SerialNumber": "CAMERA-SERIAL-123",
        },
        "gps_latitude": "47.620500",
        "gps_longitude": "-122.349300",
        "city": "Paris",
        "country": "France",
        "camera": "CAMERA-SERIAL-123",
        "created_at": "2026:07:21 14:33:02",
    }
    evidence = (EvidenceEntry("heuristic", "category", "/data/inbox Paris 47.620500", 0.4),)

    payload = build_view(item(), metadata, evidence).model_dump_json()

    assert "/data/" not in payload
    assert "47.620500" not in payload
    assert "-122.349300" not in payload
    assert "Paris" not in payload
    assert "France" not in payload
    assert "CAMERA-SERIAL-123" not in payload
    assert "14:33:02" not in payload
    assert "Safe Album" in payload
    assert "2026" in payload


def test_schema_has_no_location_or_gps_fields() -> None:
    schema_text = str(RedactedItemView.model_json_schema()).lower()

    assert "gps" not in schema_text
    assert "latitude" not in schema_text
    assert "longitude" not in schema_text
    assert "city" not in schema_text
    assert "country" not in schema_text
    assert "location" not in schema_text


def test_display_paths_are_relative_and_unicode_safe() -> None:
    view = build_view(item("Álbum #familia/子/IMG_001.jpg"), {}, ())

    assert view.display_path == "Álbum #familia/子/IMG_001.jpg"
    assert view.folder_chain == ("Álbum #familia", "子")
    assert view.hashtag_hints == ("familia",)


def test_sibling_list_is_bounded_and_component_only() -> None:
    siblings = [f"/data/inbox/folder/file-{index}.jpg" for index in range(25)]

    view = build_view(item(), {}, (), siblings)

    assert len(view.sibling_file_names) == 20
    assert all("/" not in sibling for sibling in view.sibling_file_names)

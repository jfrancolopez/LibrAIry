from __future__ import annotations

import pytest

from librairy.classify.photo_names import photo_name


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        # GNOME, Windows, macOS: a desktop tool never names the app.
        ("Screenshot from 2022-07-11 14-22-24", "screenshot-2022-07-11-142224.png"),
        ("Screenshot 2022-03-01 093841", "screenshot-2022-03-01-093841.png"),
        ("Screen Shot 2022-03-01 at 09.38.41", "screenshot-2022-03-01-093841.png"),
        # Android and iOS both stamp the app in, which is the tell.
        ("Screenshot_20240612-101112_Chrome", "phone-screenshot-2024-06-12-101112.png"),
        (
            "Screenshot_2024-06-12-10-11-12-345_com.instagram.android",
            "phone-screenshot-2024-06-12-101112.png",
        ),
        # A frame grabbed out of a video is not a screenshot of anything.
        ("vlcsnap-2022-04-23-12h46m20s082", "video-frame-2022-04-23-124620.png"),
    ],
)
def test_captures_are_renamed_to_their_timestamp(stem: str, expected: str) -> None:
    assert photo_name(stem, ".png").name == expected


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("IMG_20240612_101112", "IMG-2024-06-12-101112.jpg"),
        ("PXL_20240612_101112123", "PXL-2024-06-12-101112.jpg"),
        ("DSC_20240612", "DSC-2024-06-12.jpg"),
    ],
)
def test_camera_files_keep_the_prefix_and_gain_a_readable_date(stem: str, expected: str) -> None:
    """IMG and VID say photo or clip; GOPR and DJI say which device shot it."""
    assert photo_name(stem, ".jpg").name == expected


def test_a_photo_with_no_date_is_named_after_the_folder_it_came_from() -> None:
    result = photo_name("IMG_8654", ".heic", event="Trip to Lisbon")

    assert result.name == "IMG_8654-Trip-to-Lisbon.heic"
    assert result.reason


def test_a_generic_folder_name_is_not_worth_adding() -> None:
    """"IMG_8654-Camera-Roll.heic" is longer and says nothing."""
    assert photo_name("IMG_8654", ".heic", event="Camera Roll").name == "IMG_8654.heic"
    assert photo_name("IMG_8654", ".heic", event="Unknown").name == "IMG_8654.heic"


def test_the_folder_is_not_repeated_when_the_name_already_says_it() -> None:
    result = photo_name("Lisbon rooftop", ".jpg", event="Lisbon")

    assert result.name == "Lisbon-rooftop.jpg"


def test_a_year_alone_is_not_a_capture_time() -> None:
    """No date beats a guessed one, so this keeps its own name, tidied."""
    result = photo_name("Screenshot 2026", ".png")

    assert result.name == "Screenshot-2026.png"
    assert not result.renamed


def test_a_twelve_hour_stamp_is_converted_not_copied() -> None:
    """Read as 24-hour, a 1pm screenshot would file as 01:38."""
    assert photo_name("Screen Shot 2024-06-12 at 1.38.41 PM", ".png").name == (
        "screenshot-2024-06-12-133841.png"
    )
    assert photo_name("Screen Shot 2024-06-12 at 12.05.00 AM", ".png").name == (
        "screenshot-2024-06-12-000500.png"
    )

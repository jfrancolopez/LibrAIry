"""What an extension means — the same answer everywhere, and never a verdict.

Two things are being defended. First, that this stays *reference text*: `.mp4`
is a video container and not a movie, `.jpg` is an image and not a photo, and
nothing here says a file is safe to delete. Second, that it cannot drift from
the classifier — the roles come from `SIDECAR_KINDS` and `mediakind`, so the
tests assert against those rather than against a second copy of the truth.

Static and local: no file is opened, no provider is asked, nothing is fetched.
"""

from __future__ import annotations

import inspect

from librairy import filetypes
from librairy.classify.companions import SIDECAR_KINDS
from librairy.filetypes import REGISTRY, aria_label, extension_info, resolve_extension


def info(name: str):
    return extension_info(name)


def text_of(name: str) -> str:
    entry = info(name)
    return " ".join(
        [entry.label, entry.description, entry.common_context, entry.caution]
    ).lower()


# --- resolution ---------------------------------------------------------------


def test_a_known_extension_resolves_to_its_entry() -> None:
    entry = info("holiday.mp4")

    assert entry.extension == ".mp4"
    assert entry.label == "Video container"
    assert entry.known is True


def test_lookup_is_case_insensitive() -> None:
    assert info("VIDEO_TS.IFO").label == info("video_ts.ifo").label
    assert info("track.FLAC").extension == ".flac"
    assert info("clip.MoV").label == "Video container"


def test_an_unknown_extension_gets_a_safe_generic_answer() -> None:
    entry = info("thing.pluginPayloadAttachment")

    assert entry.known is False
    assert entry.label == "Unknown file type"
    assert "no built-in description" in entry.description
    assert entry.extension == ".pluginPayloadAttachment", "keeps the case it was written in"


def test_an_unknown_extension_does_not_crash_on_anything_odd() -> None:
    for name in ("", "   ", ".", "..", "a.", "x.😀", "no-extension-here", "..hidden.."):
        entry = info(name)
        assert isinstance(entry.label, str) and entry.label


def test_a_file_with_no_extension_is_handled_cleanly() -> None:
    entry = info("README")

    assert entry.extension == ""
    assert entry.label == "No file extension"
    assert entry.known is False


def test_a_compound_archive_extension_is_read_as_a_pair() -> None:
    assert resolve_extension("backup.tar.gz") == ".tar.gz"
    assert info("backup.tar.gz").label == "Compressed archive"
    assert info("backup.tar.bz2").extension == ".tar.bz2"


def test_a_dotted_subtitle_name_still_resolves_to_the_subtitle() -> None:
    """`movie.en.forced.srt` — the language and forced markers are stem, not
    type. Subtitle suffix preservation depends on this staying true."""
    assert resolve_extension("Movie.en.forced.srt") == ".srt"
    assert info("Movie.en.forced.srt").label == "Subtitle"


def test_a_dotfile_named_for_its_purpose_is_recognised() -> None:
    entry = info(".DS_Store")

    assert entry.extension == ".DS_Store"
    assert "finder" in entry.description.lower()


# --- the awkward formats this exists for --------------------------------------


def test_ifo_explains_the_dvd_structure() -> None:
    entry = info("VIDEO_TS.IFO")

    assert "dvd" in entry.label.lower()
    assert "video_ts" in entry.common_context.lower()
    assert "preserved" in entry.caution.lower(), "and says why the name is left alone"


def test_bup_describes_a_backup_of_the_metadata() -> None:
    assert "backup" in text_of("VIDEO_TS.BUP")
    assert "ifo" in text_of("VIDEO_TS.BUP")


def test_vob_describes_a_dvd_video_object() -> None:
    entry = info("VTS_01_1.VOB")

    assert entry.label == "DVD video object"
    assert "video" in entry.description.lower()


def test_nfo_is_a_metadata_sidecar() -> None:
    entry = info("00.Info.nfo")

    assert "metadata" in entry.label.lower()
    assert entry.role.startswith("Companion")


def test_m3u_is_a_playlist() -> None:
    entry = info("Album.m3u")

    assert entry.label == "Playlist"
    assert "playlist" in entry.role.lower()


def test_cue_is_a_cue_sheet() -> None:
    entry = info("disc.cue")

    assert entry.label == "Cue sheet"
    assert "track boundaries" in entry.description


def test_lrc_is_lyrics_that_belong_beside_a_track() -> None:
    entry = info("song.lrc")

    assert "lyrics" in entry.label.lower()
    assert "beside the matching music track" in entry.common_context


def test_srt_advice_matches_how_subtitles_are_actually_filed() -> None:
    """The caution has to agree with the companion logic: subtitles are matched
    by filename stem, which is why the stem is preserved."""
    entry = info("Show.S01E01.srt")

    assert entry.role == "Companion (subtitle)"
    assert "stem" in entry.caution.lower()


def test_idx_and_sub_are_described_as_a_pair() -> None:
    assert "idx" in text_of("movie.sub")
    assert "sub" in text_of("movie.idx")


# --- it explains formats; it does not classify --------------------------------


def test_mov_is_a_container_and_not_a_movie() -> None:
    """The .MOV misclassification was worth fixing once. This is the second
    place it could have come back."""
    entry = info("IMG_9323.MOV")
    blob = text_of("IMG_9323.MOV")

    assert entry.label == "Video container"
    assert "movie" not in blob.replace("may contain personal video", "")
    assert "personal video" in blob


def test_mp4_is_a_container_and_not_a_film() -> None:
    assert info("a.mp4").label == "Video container"
    assert "film" in info("a.mp4").common_context, "as one possibility among several"
    assert "clip" in info("a.mp4").common_context


def test_jpg_is_an_image_and_not_a_photo() -> None:
    entry = info("IMG_1234.jpg")

    assert entry.label == "JPEG image"
    assert entry.role == "Image"
    assert "photograph" in entry.description or "photographic" in entry.description


def test_nothing_tells_you_a_file_is_safe_to_delete() -> None:
    """Quarantine shows this control beside files you are deciding about. It
    must inform the decision, never make it."""
    banned = ("safe to delete", "you can delete", "junk", "useless file", "worthless")
    for entry in [*REGISTRY.values(), *filetypes.NAMED.values()]:
        blob = " ".join(
            [entry.label, entry.description, entry.common_context, entry.caution]
        ).lower()
        for phrase in banned:
            assert phrase not in blob, f"{entry.extension}: {phrase!r}"


def test_ds_store_is_described_rather_than_judged() -> None:
    entry = info(".DS_Store")
    blob = text_of(".DS_Store")

    assert "settings" in entry.label.lower()
    assert "safe to delete" not in blob
    assert "nothing here removes it for you" in blob


# --- agreement with the classifier --------------------------------------------


def test_every_companion_extension_reports_a_companion_role() -> None:
    """Derived, not restated: if the classifier learns a new sidecar kind the
    UI follows automatically, and the two cannot contradict each other."""
    for extension in SIDECAR_KINDS:
        entry = info(f"file{extension}")
        assert entry.role.startswith("Companion"), f"{extension} -> {entry.role!r}"


def test_media_roles_come_from_the_shared_kind_helper() -> None:
    assert info("a.flac").role == "Audio"
    assert info("a.mkv").role == "Video"
    assert info("a.png").role == "Image"
    assert info("a.pdf").role == "Document"


def test_a_companion_role_wins_over_the_media_kind() -> None:
    """`.vtt` is text, but LibrAIry treats it as a subtitle companion."""
    assert info("a.vtt").role == "Companion (subtitle)"


# --- safety -------------------------------------------------------------------


def test_nothing_here_reads_a_file_or_reaches_the_network() -> None:
    source = inspect.getsource(filetypes)
    for forbidden in (
        "open(",
        "urlopen",
        "requests",
        "httpx",
        "subprocess",
        "socket",
        "provider",
        "classify(",
    ):
        assert forbidden not in source, forbidden


def test_the_answer_never_contains_a_host_path_or_file_contents(tmp_path) -> None:
    secret = tmp_path / "secret.mp4"
    secret.write_text("the contents of the file", encoding="utf-8")

    entry = info(str(secret))

    blob = " ".join([entry.extension, entry.label, entry.description, entry.common_context])
    assert str(tmp_path) not in blob
    assert "secret" not in blob
    assert "the contents" not in blob


def test_the_same_name_always_gives_the_same_answer() -> None:
    assert info("A/B/C/thing.IFO") == info("thing.ifo") == info("elsewhere/THING.Ifo")


def test_the_accessible_label_names_the_extension() -> None:
    assert aria_label("VIDEO_TS.IFO") == "About .ifo files"
    assert aria_label("x.pluginPayloadAttachment") == "About .pluginPayloadAttachment files"
    assert aria_label("README") == "About this file's type"


def test_a_mime_fallback_is_offered_only_when_the_extension_is_unknown() -> None:
    known = info("a.mp4")
    unknown = info("a.pluginPayloadAttachment")

    assert known.mime == "video/mp4"
    assert unknown.mime == "", "nothing to guess, and no guess is offered"


def test_the_registry_covers_the_formats_this_project_actually_meets() -> None:
    required = {
        ".ifo", ".bup", ".vob", ".nfo", ".m3u", ".m3u8", ".cue", ".lrc",
        ".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt",
        ".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif",
        ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v",
        ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".txt", ".md", ".rtf", ".csv",
        ".zip", ".rar", ".7z", ".tar", ".gz",
        ".dmg", ".iso", ".exe", ".msi", ".app", ".pkg",
        ".json", ".yaml", ".yml", ".toml", ".xml", ".ini", ".conf", ".log", ".sql",
        ".py", ".sh", ".js", ".ts",
    }

    assert required <= set(REGISTRY), sorted(required - set(REGISTRY))


def test_every_entry_says_something_and_says_it_briefly() -> None:
    for entry in REGISTRY.values():
        assert entry.label and entry.description, entry.extension
        assert entry.description.endswith("."), entry.extension
        assert len(entry.description) < 200, entry.extension

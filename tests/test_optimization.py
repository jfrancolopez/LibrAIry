"""Finding an optimization, and never taking it.

Every test here is about the *advisor*. There is deliberately no test that a
transcode ran, because there is deliberately no code that runs one: scanning
with ffprobe is cheap and re-encoding forty gigabytes is not, and the whole
design rests on those being separate phases.

Two claims carry the rest. `test_flac_is_never_replaced_by_mp3` — a FLAC is
already a compressed lossless master, and "optimising" it into an MP3 while
keeping the original makes the library *bigger*. And
`test_a_wasteful_file_is_offered_the_transcode_not_the_remux` — because the
opposite order looks more cautious, was what I wrote first, and answers a
compatibility question when a storage question was asked.
"""

from __future__ import annotations

import pytest

from librairy.optimization import (
    AUDIO_MIN_BYTES,
    HIGH,
    LOSSLESS,
    LOSSY,
    LOW,
    REMUX,
    VIDEO_MIN_BYTES,
    MediaFacts,
    advise,
)

GB = 1024 * 1024 * 1024
MB = 1024 * 1024


def wav(size: int = 800 * MB, duration: float = 2400) -> MediaFacts:
    return MediaFacts(
        container=".wav", size=size, duration=duration,
        audio_codec="pcm_s16le", audio_channels=2, sample_rate=44100, bit_depth=16,
        audio_streams=1,
    )


def video(
    codec: str = "h264",
    size: int = 18 * GB,
    duration: float = 7200,
    height: int = 1080,
    container: str = ".mkv",
    bitrate: int = 0,
    audio: str = "ac3",
    subtitles: int = 0,
    audio_streams: int = 1,
) -> MediaFacts:
    return MediaFacts(
        container=container, size=size, duration=duration,
        video_codec=codec, width=height * 16 // 9, height=height,
        frame_rate="24000/1001", video_bitrate=bitrate,
        audio_codec=audio, audio_streams=audio_streams, subtitle_streams=subtitles,
    )


# --- audio ------------------------------------------------------------------------


def test_a_wav_is_offered_flac() -> None:
    opportunity = advise("Music/concert.wav", wav())

    assert opportunity.kind == "audio-to-flac"
    assert opportunity.to_label == "FLAC"
    assert opportunity.estimated_saving > 0


def test_the_wav_recommendation_is_lossless() -> None:
    """Nothing is discarded, and the label has to say so — otherwise it reads
    like every other "save space" suggestion."""
    opportunity = advise("Music/concert.wav", wav())

    assert opportunity.quality == LOSSLESS
    assert "without discarding" in opportunity.reason


@pytest.mark.parametrize("suffix", [".wav", ".aiff", ".aif"])
def test_every_uncompressed_container_is_a_candidate(suffix: str) -> None:
    facts = MediaFacts(container=suffix, size=800 * MB, duration=2400, audio_codec="pcm_s16be")

    assert advise(f"Music/take{suffix}", facts) is not None


def test_flac_is_never_replaced_by_mp3() -> None:
    """A FLAC is already a compressed lossless master. Turning it into an MP3
    is not an optimization — it is a smaller copy, and if the original stays
    the library got bigger."""
    facts = MediaFacts(container=".flac", size=400 * MB, duration=2400, audio_codec="flac")

    assert advise("Music/album.flac", facts) is None


def test_lossy_audio_is_not_re_encoded_to_be_smaller() -> None:
    """Lossy to lossy compounds the loss, and saves little."""
    facts = MediaFacts(container=".mp3", size=90 * MB, duration=3600, audio_codec="mp3")

    assert advise("Music/mix.mp3", facts) is None


def test_a_small_wav_is_not_worth_mentioning() -> None:
    """An advisor that reports every file is an advisor that gets switched off."""
    assert advise("Music/blip.wav", wav(size=4 * MB, duration=20)) is None


def test_the_audio_floor_is_where_it_says_it_is() -> None:
    just_under = round(AUDIO_MIN_BYTES / 0.38) - MB
    just_over = round(AUDIO_MIN_BYTES / 0.38) + 10 * MB

    assert advise("Music/a.wav", wav(size=just_under)) is None
    assert advise("Music/b.wav", wav(size=just_over)) is not None


def test_a_short_encode_is_cheap_and_a_long_one_is_not() -> None:
    assert advise("Music/a.wav", wav(duration=120)).compute == LOW
    assert advise("Music/b.wav", wav(duration=5400)).compute != LOW


# --- video: usually nothing --------------------------------------------------------


@pytest.mark.parametrize("codec", ["hevc", "av1", "vp9"])
def test_an_already_efficient_codec_is_never_re_encoded(codec: str) -> None:
    """Not a blanket "video should be HEVC". Re-encoding these buys little and
    costs everything.

    Asserted as "no transcode" rather than "no opportunity": the same file in
    a Matroska container may still be worth repackaging, and that is a
    compatibility offer with no quality cost. Only the expensive, destructive
    branch has to stay quiet.
    """
    assert advise("Movies/film.mp4", video(codec=codec, container=".mp4")) is None
    offered = advise("Movies/film.mkv", video(codec=codec, audio="aac"))
    assert offered is None or offered.kind != "video-transcode"


def test_a_sensibly_encoded_h264_is_never_re_encoded() -> None:
    """8 Mbps at 1080p is a good encode, not a problem."""
    assert advise("Movies/film.mp4", video(bitrate=8_000_000, container=".mp4")) is None
    offered = advise("Movies/film.mkv", video(bitrate=8_000_000, audio="aac"))
    assert offered is None or offered.kind != "video-transcode"


def test_a_small_video_is_left_alone() -> None:
    """Below both floors there is nothing to say, in either branch."""
    assert advise("Movies/clip.mkv", video(size=60 * MB, duration=120)) is None


def test_the_video_floor_is_where_it_says_it_is() -> None:
    assert VIDEO_MIN_BYTES == 150 * MB


# --- video: when there is something to say -------------------------------------------


def test_a_wasteful_h264_can_be_offered_a_transcode() -> None:
    opportunity = advise("Movies/film.mkv", video(bitrate=30_000_000, container=".mp4"))

    assert opportunity.kind == "video-transcode"
    assert opportunity.quality == LOSSY
    assert opportunity.to_label == "HEVC"
    assert "unusually high" in opportunity.reason


def test_a_legacy_codec_can_be_offered_a_transcode() -> None:
    opportunity = advise(
        "Movies/old.mpg",
        video(codec="mpeg2video", container=".mpg", size=8 * GB, bitrate=9_000_000),
    )

    assert opportunity.kind == "video-transcode"
    assert "older codec" in opportunity.reason


def test_a_transcode_is_labelled_lossy_and_expensive() -> None:
    opportunity = advise("Movies/film.mkv", video(bitrate=30_000_000, container=".mp4"))

    assert opportunity.quality == LOSSY
    assert opportunity.compute == HIGH


def test_a_wasteful_file_is_offered_the_transcode_not_the_remux() -> None:
    """A 30 Mbps 1080p H.264 in Matroska qualifies for both. Offering the
    remux — cheaper, discards nothing — answers a compatibility question while
    a storage question was asked, and reports 0% on the file with the largest
    saving in the library."""
    opportunity = advise("Movies/film.mkv", video(bitrate=30_000_000, audio="aac"))

    assert opportunity.kind == "video-transcode"
    assert opportunity.estimated_saving > 0


# --- remux ---------------------------------------------------------------------------


def test_a_compatible_stream_in_an_awkward_container_is_a_remux() -> None:
    opportunity = advise("Movies/film.mkv", video(bitrate=6_000_000, audio="aac"))

    assert opportunity.kind == "video-remux"
    assert opportunity.quality == REMUX


def test_a_remux_promises_no_savings() -> None:
    """A remux that promised space would be caught out on the first job."""
    opportunity = advise("Movies/film.mkv", video(bitrate=6_000_000, audio="aac"))

    assert opportunity.estimated_bytes == opportunity.current_bytes
    assert opportunity.estimated_saving == 0
    assert "compatibility rather than space" in opportunity.reason


def test_a_remux_is_cheap() -> None:
    assert advise("Movies/f.mkv", video(bitrate=6_000_000, audio="aac")).compute == LOW


def test_a_file_already_in_a_compatible_container_needs_no_remux() -> None:
    assert advise("Movies/f.mp4", video(bitrate=6_000_000, audio="aac", container=".mp4")) is None


def test_a_stream_the_target_cannot_carry_is_not_offered_a_remux() -> None:
    """Suggesting a container that cannot hold the audio would turn a copy
    into a re-encode, which is a different promise entirely."""
    assert advise("Movies/f.mkv", video(bitrate=6_000_000, audio="truehd")) is None


def test_subtitles_are_never_silently_dropped() -> None:
    """MP4's subtitle support is worse than Matroska's, and losing a subtitle
    track to change a container is exactly what must not happen quietly."""
    assert advise("Movies/f.mkv", video(bitrate=6_000_000, audio="aac", subtitles=3)) is None


def test_alternate_audio_tracks_are_reported_and_kept() -> None:
    opportunity = advise(
        "Movies/f.mkv", video(bitrate=30_000_000, audio="aac", audio_streams=4)
    )

    assert ("Audio tracks", "4 — all kept") in opportunity.facts


# --- what the row promises -----------------------------------------------------------


def test_resolution_and_frame_rate_are_preserved_and_shown() -> None:
    """Downscaling and frame-rate conversion are destructive choices, not
    default optimizations."""
    opportunity = advise("Movies/f.mkv", video(bitrate=30_000_000, height=1080))

    facts = dict(opportunity.facts)
    assert facts["Resolution"] == "1920x1080"
    assert facts["Frame rate"] == "23.976 fps"
    assert "1080" in opportunity.summary or "same resolution" in opportunity.summary


def test_a_4k_source_is_not_reduced_to_1080p() -> None:
    """A future 4K file should not be destroyed because one playback device is
    1080p. It may be the archival master."""
    opportunity = advise("Movies/f.mkv", video(height=2160, bitrate=80_000_000, size=60 * GB))

    assert dict(opportunity.facts)["Resolution"] == "3840x2160"
    assert "720" not in opportunity.summary and "1080" not in opportunity.summary


def test_every_class_has_a_label_and_a_plain_explanation() -> None:
    from librairy.optimization import CLASS_LABEL, CLASS_MEANING

    for name in (LOSSLESS, REMUX, LOSSY, "derivative"):
        assert CLASS_LABEL[name].isupper()
        assert len(CLASS_MEANING[name]) > 40


def test_an_estimate_is_arithmetic_and_says_so() -> None:
    """Nothing here has encoded anything, so every number is arithmetic on a
    bitrate. Calling it a measurement is a lie that gets found out once."""
    opportunity = advise("Music/a.wav", wav())

    assert opportunity.estimated_bytes != opportunity.current_bytes
    assert hasattr(opportunity, "estimated_saving")
    assert not hasattr(opportunity, "actual_bytes"), "measured size belongs to a job"


# --- the cache ---------------------------------------------------------------------


def test_an_unchanged_file_is_probed_once(tmp_path, monkeypatch) -> None:
    """Thousands of files must not mean thousands of ffprobe calls on every
    audit."""
    from librairy import optimization
    from librairy.config import Settings
    from librairy.db import connect
    from librairy.scanner import scan_root

    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata", INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library", QUARANTINE_DIR=tmp_path / "q",
        FILE_STABILITY_SECONDS=0, AUTH_REQUIRED=False, _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    track = settings.library_dir / "a.wav"
    track.write_bytes(b"riff")
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    row = conn.execute("SELECT id, fingerprint FROM items LIMIT 1").fetchone()
    calls: list[int] = []
    monkeypatch.setattr(
        optimization, "probe_media", lambda *_a: calls.append(1) or wav()
    )

    first = optimization.facts_for(conn, settings, row["id"], row["fingerprint"], track)
    second = optimization.facts_for(conn, settings, row["id"], row["fingerprint"], track)

    assert len(calls) == 1, "the second look should have read the cache"
    assert first == second

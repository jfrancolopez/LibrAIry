"""Storage Optimization: finding opportunities, and never taking them.

The whole feature is built on one separation, and everything else follows from
it. **Finding an optimization is cheap. Performing one is not.** `ffprobe`
reads a header; a transcode reads and rewrites forty gigabytes. So this module
does only the first: it reads metadata LibrAIry mostly already has, works out
what *could* be smaller, and stops. Nothing here starts ffmpeg, and there is no
code path from discovery to conversion.

That is not a stylistic choice. A NAS that quietly starts transcoding because
an audit ran is a NAS whose other containers stop responding, and the person
who asked for a file organiser did not ask for that.

Three ideas the UI depends on being real:

**Lossless, remux and lossy are different things.** `Save 4 GB` means nothing
on its own — it could be the same audio in a better container, or it could be
information permanently discarded. WAV to FLAC keeps every sample. A remux
copies the streams untouched and changes only the box around them. A lossy
transcode decodes and re-encodes, and some of the original does not come back.
These are separate classes with separate words, never one green button.

**FLAC is not a problem to be solved.** It is already a compressed lossless
master. Turning it into MP3 is not an optimization, it is a smaller *copy* —
and if the original stays, the library got bigger. That case exists, it is
called a derivative, and it is never the recommendation.

**Doing nothing is the common answer.** An advisor that reports every file is
an advisor that gets switched off, so a candidate has to clear both a
percentage and a byte floor before it is worth anyone's attention. HEVC that
is already reasonably sized, a small MP3, a modest H.264 rip — no row at all.

Estimates are labelled as estimates. Nothing here has encoded anything, so
every number is arithmetic on a bitrate, and calling it a measurement would be
a lie that gets found out exactly once.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from librairy.config import Settings

# --- what a change costs you ---------------------------------------------------

LOSSLESS = "lossless"
REMUX = "remux"
LOSSY = "lossy"
DERIVATIVE = "derivative"

CLASS_LABEL = {
    LOSSLESS: "LOSSLESS",
    REMUX: "REMUX",
    LOSSY: "LOSSY",
    DERIVATIVE: "DERIVATIVE",
}
CLASS_MEANING = {
    LOSSLESS: "Same audio content, stored more efficiently. Nothing is discarded.",
    REMUX: (
        "Video and audio are copied without re-encoding. This is fast and does "
        "not reduce quality; it may not save much space either."
    ),
    LOSSY: (
        "Re-encoding can reduce size, but some of the original information is "
        "permanently discarded."
    ),
    DERIVATIVE: (
        "A smaller additional copy. The original is kept, so this increases "
        "total storage rather than reducing it."
    ),
}

LOW, MEDIUM, HIGH = "low", "medium", "high"
COST_LABEL = {LOW: "Low", MEDIUM: "Medium", HIGH: "High"}

# --- when an opportunity is worth mentioning -----------------------------------
#
# Two floors, and both have to clear. A percentage alone reports a 40 MB file
# that could be 30; a byte floor alone reports a 40 GB file that could be 39.5.
# Deliberately conservative: the cost of a missed opportunity is that a disk
# stays as full as it already was, and the cost of a noisy one is that the
# whole feature gets ignored.
VIDEO_MIN_PERCENT = 20
# 250 MB was the starting suggestion and measurement moved it. With a 20%
# floor as well, 250 MB means nothing under about 1.2 GB can ever qualify —
# which excludes essentially every television episode and leaves the feature
# talking only about feature films. 150 MB lets a 750 MB episode through and
# still ignores the trivia.
VIDEO_MIN_BYTES = 150 * 1024 * 1024
AUDIO_MIN_PERCENT = 25
# Measured rather than guessed: 200 seconds of 16-bit 48 kHz stereo PCM is
# 37 MB, and FLAC takes about 14 MB off it. A 20 MB floor would have missed
# every single track and only ever fired on whole-concert recordings, which is
# the wrong half of the problem.
AUDIO_MIN_BYTES = 10 * 1024 * 1024
# A remux saves almost nothing and is offered for compatibility, so it is not
# held to a savings floor — but it is held to a size floor, because remuxing a
# 3 MB clip is not worth a row.
REMUX_MIN_BYTES = 200 * 1024 * 1024

# Uncompressed PCM in a container. These are the strong audio candidates: FLAC
# stores the identical samples and typically lands between 50% and 70%.
UNCOMPRESSED_AUDIO = {".wav", ".aiff", ".aif", ".aifc"}
# What FLAC usually achieves on real material. Used for the estimate only, and
# reported as a range rather than a number so nobody reads it as a promise.
FLAC_RATIO = 0.62

# Video codecs by how much room they have left. A file already in an efficient
# codec is usually finished — re-encoding it buys little and costs everything.
EFFICIENT_VIDEO = {"hevc", "h265", "av1", "vp9"}
LEGACY_VIDEO = {"mpeg2video", "mpeg4", "msmpeg4v3", "wmv3", "vc1", "dvvideo", "prores"}
# Above this, an H.264 encode at 1080p is paying for bits nobody can see. A
# Blu-ray remux sits around 25-35 Mbps; a good 1080p HEVC is 6-10.
H264_BITRATE_CEILING = {1080: 12_000_000, 720: 6_000_000, 2160: 40_000_000}
# What a careful HEVC encode of the same material tends to need.
HEVC_TARGET_BITRATE = {2160: 18_000_000, 1080: 7_000_000, 720: 3_500_000}

# Containers that a broad range of players and TVs accept without help.
COMPATIBLE_CONTAINERS = {".mp4", ".m4v"}
# Streams an MP4 can carry, so a remux is a copy rather than a re-encode.
MP4_SAFE_VIDEO = {"h264", "hevc", "av1", "mpeg4"}
MP4_SAFE_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}


@dataclass(frozen=True)
class MediaFacts:
    """What one `ffprobe` said, reduced to the fields a decision needs."""

    container: str = ""
    duration: float = 0.0
    size: int = 0
    video_codec: str = ""
    width: int = 0
    height: int = 0
    frame_rate: str = ""
    video_bitrate: int = 0
    audio_codec: str = ""
    audio_bitrate: int = 0
    audio_channels: int = 0
    sample_rate: int = 0
    bit_depth: int = 0
    audio_streams: int = 0
    subtitle_streams: int = 0

    @property
    def is_video(self) -> bool:
        return bool(self.video_codec) and self.height > 0

    @property
    def tier(self) -> int:
        """The nearest standard height, for looking up a bitrate expectation."""
        for tier in (2160, 1080, 720):
            if self.height >= tier - 100:
                return tier
        return 480


@dataclass(frozen=True)
class Opportunity:
    """One thing that could be done, and everything needed to decline it."""

    relpath: str
    kind: str
    quality: str
    current_bytes: int
    estimated_bytes: int
    summary: str
    reason: str
    compute: str = MEDIUM
    from_label: str = ""
    to_label: str = ""
    protected_by: str = ""
    facts: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # Set by the caller that knows the index. The advisor is a pure function of
    # a path and a probe, so it does not look these up itself.
    item_id: int | None = None
    fingerprint: str = ""

    @property
    def estimated_saving(self) -> int:
        return max(0, self.current_bytes - self.estimated_bytes)

    @property
    def estimated_percent(self) -> int:
        if not self.current_bytes:
            return 0
        return round(self.estimated_saving / self.current_bytes * 100)

    @property
    def protected(self) -> bool:
        return bool(self.protected_by)

    @property
    def eligible(self) -> bool:
        """Whether this could be queued. Protected originals never can."""
        return not self.protected


# --- reading the media ---------------------------------------------------------

TOOL = "ffprobe-media"


def facts_for(
    conn: sqlite3.Connection,
    settings: Settings,
    item_id: int,
    fingerprint: str,
    path: Path,
) -> MediaFacts | None:
    """Probe one file, or read the answer given last time.

    Keyed by the fingerprint, so a file that has not changed is never probed
    twice — which is the difference between a storage check that costs nothing
    on the second audit and one that walks a thousand files with ffprobe every
    time. Uses the `item_metadata` cache that already exists rather than
    inventing a second one.
    """
    from librairy.tools.common import get_cached_metadata, set_cached_metadata

    if fingerprint:
        cached = get_cached_metadata(conn, item_id, fingerprint, TOOL)
        if cached is not None:
            return MediaFacts(**cached)
    _PROBES["count"] += 1
    probed = probe_media(settings, path)
    if probed is None:
        return None
    if fingerprint:
        from librairy.planner import utc_now

        set_cached_metadata(conn, item_id, fingerprint, TOOL, probed.__dict__, utc_now())
    return probed


# How many times the cache could not answer and ffprobe had to be run.
# Counted at the cache boundary rather than inside `probe_media`, because the
# claim worth checking is "an unchanged library needs no probes" — and a
# counter that lives inside the subprocess wrapper reads zero the moment a
# test substitutes it, which makes the proof prove nothing.
#
# This is emphatically not a count of items inspected. Eight files inspected
# with eight cache hits is eight and zero.
_PROBES = {"count": 0}


def probe_calls() -> int:
    return _PROBES["count"]


def reset_probe_calls() -> None:
    _PROBES["count"] = 0


def probe_media(settings: Settings, path: Path) -> MediaFacts | None:
    """One `ffprobe`, flattened. None when the file is not media at all."""
    from librairy.tools.ffprobe import probe

    try:
        result = probe(path, settings)
    except Exception:  # noqa: BLE001 - an unreadable file is not an opportunity
        return None
    if not result.ok or not isinstance(result.data, dict):
        return None
    streams = [s for s in (result.data.get("streams") or ()) if isinstance(s, dict)]
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            # An attached cover is a video stream. It is not the film.
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        {},
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return MediaFacts(
        container=path.suffix.lower(),
        duration=float(result.data.get("duration") or 0.0),
        size=size,
        video_codec=str(video.get("codec_name") or ""),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        frame_rate=str(video.get("avg_frame_rate") or ""),
        video_bitrate=_int(video.get("bit_rate")),
        audio_codec=str(audio.get("codec_name") or ""),
        # Every audio stream, not the first: a film with a commentary track
        # and a foreign dub is paying for all of them, and pretending the
        # extras are video is how a well-encoded file looks wasteful.
        audio_bitrate=sum(
            _int(stream.get("bit_rate"))
            for stream in streams
            if stream.get("codec_type") == "audio"
        ),
        audio_channels=int(audio.get("channels") or 0),
        sample_rate=_int(audio.get("sample_rate")),
        bit_depth=_int(audio.get("bits_per_raw_sample") or audio.get("bits_per_sample")),
        audio_streams=sum(1 for s in streams if s.get("codec_type") == "audio"),
        subtitle_streams=sum(1 for s in streams if s.get("codec_type") == "subtitle"),
    )


def _int(value: object) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


# --- deciding whether there is anything worth saying ---------------------------


def advise(
    relpath: str,
    facts: MediaFacts,
    *,
    protected_by: str = "",
    refused: frozenset[str] = frozenset(),
) -> Opportunity | None:
    """The one entry point. None means "this file is fine", which is common.

    `refused` is the quality classes the owner's Format Policy explicitly
    forbids for this path. Empty by default and empty whenever nobody has
    answered — unset is not "no", and reading it as "no" would switch off
    behaviour that worked before there was a policy to consult.

    It can only ever take an opportunity away. Permitting lossy conversions
    does not conjure an encoder for one, so nothing here is reachable by
    setting a permission to true.
    """
    suffix = PurePosixPath(relpath).suffix.lower()
    found = None
    if suffix in UNCOMPRESSED_AUDIO:
        found = _uncompressed_audio(relpath, facts, protected_by)
    elif facts.is_video:
        found = _video(relpath, facts, protected_by)
    if found is not None and found.quality in refused:
        return None
    return found


def _uncompressed_audio(
    relpath: str, facts: MediaFacts, protected_by: str
) -> Opportunity | None:
    """WAV and AIFF store PCM uncompressed. FLAC stores the identical samples.

    The strongest opportunity there is: nothing is discarded, the encode is
    cheap, and the saving is large and reliable. The estimate is a range in
    the summary because compression depends on the material — quiet acoustic
    material compresses far better than a loud master.
    """
    estimated = round(facts.size * FLAC_RATIO)
    if not _worth_it(facts.size, estimated, AUDIO_MIN_PERCENT, AUDIO_MIN_BYTES):
        return None
    return Opportunity(
        relpath=relpath,
        kind="audio-to-flac",
        quality=LOSSLESS,
        current_bytes=facts.size,
        estimated_bytes=estimated,
        summary=f"{facts.container.lstrip('.').upper()} could be stored as FLAC",
        reason=(
            "FLAC compresses audio without discarding any of it. This file stores "
            "the same audio uncompressed."
        ),
        compute=LOW if facts.duration and facts.duration < 600 else MEDIUM,
        from_label=facts.container.lstrip(".").upper() or "PCM",
        to_label="FLAC",
        protected_by=protected_by,
        facts=_audio_facts(facts),
    )


def _video(relpath: str, facts: MediaFacts, protected_by: str) -> Opportunity | None:
    """Three answers, and the first one is usually "nothing".

    The transcode is considered first, and the reason is worth writing down
    because the opposite order looks more cautious and is wrong. A 30 Mbps
    1080p H.264 in a Matroska container qualifies for both: it could be
    repackaged as MP4, and it could be re-encoded to save two hundred
    megabytes. Offering the remux — because it is cheaper and discards
    nothing — answers a question about *compatibility* while a question about
    *storage* was being asked, and reports `0%` on the file with the largest
    saving in the library.

    So: where there is a real saving, that is the storage opportunity. The
    remux is what is left for files that are already sensibly encoded and
    merely in an awkward container.
    """
    if transcode := _transcode(relpath, facts, protected_by):
        return transcode
    return _remux(relpath, facts, protected_by)


def _remux(relpath: str, facts: MediaFacts, protected_by: str) -> Opportunity | None:
    """The container is the only thing in the way.

    Offered for compatibility, not for space — and the summary says so, since
    a remux that promised savings would be caught out on the first job.
    """
    if facts.container in COMPATIBLE_CONTAINERS:
        return None
    if facts.video_codec not in MP4_SAFE_VIDEO:
        return None
    if facts.audio_codec and facts.audio_codec not in MP4_SAFE_AUDIO:
        return None
    if facts.size < REMUX_MIN_BYTES:
        return None
    # Subtitles are the reason this is narrow. MP4's subtitle support is worse
    # than Matroska's, and silently dropping a subtitle track to change a
    # container is exactly the kind of thing that must never happen quietly.
    if facts.subtitle_streams:
        return None
    return Opportunity(
        relpath=relpath,
        kind="video-remux",
        quality=REMUX,
        current_bytes=facts.size,
        # Container overhead only. Saying "about the same" is the honest
        # estimate and the summary leads with compatibility instead.
        estimated_bytes=facts.size,
        summary=f"{facts.container.lstrip('.').upper()} could be repackaged as MP4",
        reason=(
            "The video and audio are already in formats MP4 can carry, so they "
            "would be copied without re-encoding. This is about compatibility "
            "rather than space."
        ),
        compute=LOW,
        from_label=facts.container.lstrip(".").upper(),
        to_label="MP4",
        protected_by=protected_by,
        facts=_video_facts(facts),
    )


def _transcode(relpath: str, facts: MediaFacts, protected_by: str) -> Opportunity | None:
    """Only where the source is paying for bits nobody can see.

    Never a blanket "video should be HEVC". A file already in an efficient
    codec is left alone; so is one whose bitrate is already sensible for its
    resolution. What is left is the genuinely wasteful: a legacy codec, or an
    H.264 encode at a bitrate far above what the resolution needs.
    """
    codec = facts.video_codec.lower()
    if codec in EFFICIENT_VIDEO:
        return None
    bitrate, source = video_bitrate(facts)
    if not bitrate:
        return None
    tier = facts.tier
    legacy = codec in LEGACY_VIDEO
    ceiling = H264_BITRATE_CEILING.get(tier, 12_000_000)
    if not legacy and bitrate <= ceiling:
        return None
    target = HEVC_TARGET_BITRATE.get(tier, 7_000_000)
    if target >= bitrate:
        return None
    estimated = round(facts.size * (target / bitrate))
    if not _worth_it(facts.size, estimated, VIDEO_MIN_PERCENT, VIDEO_MIN_BYTES):
        return None
    why = (
        f"{codec.upper()} is an older codec that needs more space for the same picture."
        if legacy
        else (
            f"The source runs at about {round(bitrate / 1_000_000)} Mbps, which is "
            f"unusually high for {facts.height}p in this codec."
        )
    )
    return Opportunity(
        relpath=relpath,
        kind="video-transcode",
        quality=LOSSY,
        current_bytes=facts.size,
        estimated_bytes=estimated,
        summary=f"{codec.upper()} could be re-encoded as HEVC at the same resolution",
        reason=why,
        compute=HIGH,
        from_label=codec.upper(),
        to_label="HEVC",
        protected_by=protected_by,
        facts=_video_facts(facts),
    )


def video_bitrate(facts: MediaFacts) -> tuple[int, str]:
    """What the video alone costs, and how confident that number is.

    Three sources, best first, and the difference between them matters because
    this number decides whether a file gets recommended for a lossy re-encode.

    1. **The video stream's own `bit_rate`.** Exact, and what ffprobe reports
       for most MP4s.
    2. **File size minus the audio.** Matroska usually declares no per-stream
       video bitrate but does declare the audio's, so subtracting it recovers
       most of the accuracy. A 640 kbps AC3 track on a two-hour film is over
       half a gigabyte; counting it as video is not a rounding error.
    3. **Size over duration.** Everything included. Only when nothing better
       exists.

    Each fallback overstates the video, and overstating pushes a borderline
    file *toward* a transcode — the wrong direction to be wrong in. Hence the
    order, and hence the label, which the UI shows so an estimate built on
    guesswork does not read like a measurement.
    """
    if facts.video_bitrate > 0:
        return facts.video_bitrate, "measured"
    if facts.duration <= 0 or facts.size <= 0:
        return 0, ""
    total = round(facts.size * 8 / facts.duration)
    if facts.audio_bitrate > 0 and facts.audio_bitrate < total:
        # Container overhead stays counted as video, which keeps this an
        # overstatement rather than turning it into an understatement.
        return total - facts.audio_bitrate, "estimated from size, audio subtracted"
    return total, "estimated from size"


def _implied_bitrate(facts: MediaFacts) -> int:
    """The number alone, for callers that do not need to say where it came from."""
    return video_bitrate(facts)[0]


def _worth_it(current: int, estimated: int, min_percent: int, min_bytes: int) -> bool:
    """Both floors, or no row. See the constants for why there are two."""
    saving = current - estimated
    if saving < min_bytes:
        return False
    return current > 0 and (saving / current) * 100 >= min_percent


def _audio_facts(facts: MediaFacts) -> tuple[tuple[str, str], ...]:
    rows = [("Codec", facts.audio_codec.upper() or "PCM")]
    if facts.sample_rate:
        rows.append(("Sample rate", f"{facts.sample_rate / 1000:g} kHz"))
    if facts.bit_depth:
        rows.append(("Bit depth", f"{facts.bit_depth}-bit"))
    if facts.audio_channels:
        rows.append(("Channels", str(facts.audio_channels)))
    if facts.duration:
        rows.append(("Duration", _duration(facts.duration)))
    return tuple(rows)


def _video_facts(facts: MediaFacts) -> tuple[tuple[str, str], ...]:
    rows = [("Video", facts.video_codec.upper())]
    if facts.width and facts.height:
        rows.append(("Resolution", f"{facts.width}x{facts.height}"))
    if rate := _frame_rate(facts.frame_rate):
        rows.append(("Frame rate", rate))
    bitrate, source = video_bitrate(facts)
    if bitrate:
        label = f"{bitrate / 1_000_000:.1f} Mbps"
        rows.append(("Video bitrate", label if source == "measured" else f"{label} ({source})"))
    if facts.audio_bitrate:
        rows.append(("Audio bitrate", f"{facts.audio_bitrate / 1000:.0f} kbps"))
    if facts.audio_codec:
        rows.append(("Audio", facts.audio_codec.upper()))
    if facts.audio_streams > 1:
        rows.append(("Audio tracks", f"{facts.audio_streams} — all kept"))
    if facts.subtitle_streams:
        rows.append(("Subtitles", f"{facts.subtitle_streams} — all kept"))
    if facts.duration:
        rows.append(("Duration", _duration(facts.duration)))
    return tuple(rows)


def _frame_rate(raw: str) -> str:
    """`24000/1001` is how ffprobe says 23.976, and the difference matters."""
    if "/" not in raw:
        return raw
    numerator, _, denominator = raw.partition("/")
    try:
        value = int(numerator) / int(denominator)
    except (ValueError, ZeroDivisionError):
        return ""
    return f"{value:.3f}".rstrip("0").rstrip(".") + " fps" if value else ""


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


# --- recording what was found ---------------------------------------------------
#
# Nothing below runs ffmpeg either. These write rows and read rows; the row is
# the end of the advisor's job.

# Bumped when the rules change meaningfully. A `No suggestion` recorded against
# an early rule must not silence a later, better one for the life of the file.
RULE_VERSION = 1


def record_opportunities(
    conn: sqlite3.Connection, found: list[Opportunity], *, scope: str = ""
) -> None:
    """Store this run's opportunities and retire the ones that no longer apply.

    A dismissal survives, exactly as `keep_as_is` does for findings — but only
    while the file and the rules are the same. The moment either changes the
    row is reopened, because "no thank you" was an answer about a particular
    suggestion and not a permanent vow of silence.
    """
    import json

    from librairy.planner import utc_now

    now = utc_now()
    seen = {(item.relpath, item.kind) for item in found}
    for item in found:
        existing = conn.execute(
            "SELECT status, fingerprint, rule_version FROM optimization_opportunities "
            "WHERE root='library' AND relpath=? AND kind=?",
            (item.relpath, item.kind),
        ).fetchone()
        if (
            existing
            and existing["status"] == "dismissed"
            and existing["fingerprint"] == item.fingerprint
            and existing["rule_version"] == RULE_VERSION
        ):
            continue
        conn.execute(
            """
            INSERT INTO optimization_opportunities(
              item_id, root, relpath, kind, quality, current_bytes, estimated_bytes,
              summary, reason, compute, from_label, to_label, protected_by, facts,
              fingerprint, rule_version, status, detected_at, updated_at
            ) VALUES (?, 'library', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            ON CONFLICT(root, relpath, kind) DO UPDATE SET
              item_id=excluded.item_id,
              quality=excluded.quality,
              current_bytes=excluded.current_bytes,
              estimated_bytes=excluded.estimated_bytes,
              summary=excluded.summary,
              reason=excluded.reason,
              compute=excluded.compute,
              from_label=excluded.from_label,
              to_label=excluded.to_label,
              protected_by=excluded.protected_by,
              facts=excluded.facts,
              fingerprint=excluded.fingerprint,
              rule_version=excluded.rule_version,
              status='open',
              updated_at=excluded.updated_at
            """,
            (
                item.item_id,
                item.relpath,
                item.kind,
                item.quality,
                item.current_bytes,
                item.estimated_bytes,
                item.summary,
                item.reason,
                item.compute,
                item.from_label,
                item.to_label,
                item.protected_by,
                json.dumps([list(pair) for pair in item.facts]),
                item.fingerprint,
                RULE_VERSION,
                now,
                now,
            ),
        )
    _retire(conn, seen, scope)


def _retire(conn: sqlite3.Connection, seen: set[tuple[str, str]], scope: str) -> None:
    """Drop open rows this run did not find again.

    A list that only grows stops being read, and a suggestion about a file
    that has since been re-encoded is worse than no suggestion.
    """
    sql = "SELECT id, relpath, kind FROM optimization_opportunities WHERE status='open'"
    params: list[object] = []
    if scope:
        sql += " AND relpath LIKE ?"
        params.append(f"{scope.strip('/')}/%")
    stale = [
        row["id"]
        for row in conn.execute(sql, params)
        if (row["relpath"], row["kind"]) not in seen
    ]
    for row_id in stale:
        conn.execute("DELETE FROM optimization_opportunities WHERE id=?", (row_id,))


def dismiss(conn: sqlite3.Connection, opportunity_id: int) -> bool:
    """"No suggestion." Remembered against this file and these rules."""
    from librairy.planner import utc_now

    cursor = conn.execute(
        "UPDATE optimization_opportunities SET status='dismissed', updated_at=? "
        "WHERE id=? AND status='open'",
        (utc_now(), opportunity_id),
    )
    return cursor.rowcount > 0


def open_opportunities(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM optimization_opportunities WHERE status='open' "
            "ORDER BY (current_bytes - estimated_bytes) DESC, relpath"
        )
    )


def summary(rows: list[sqlite3.Row]) -> dict[str, object]:
    """Totals for the panel, with the classes kept apart.

    A remux saves nothing and is offered for compatibility. Adding it to a
    storage total would be the same dishonesty as calling it an optimization:
    the number would be right and the claim would be wrong.
    """
    saving = sum(
        row["current_bytes"] - row["estimated_bytes"]
        for row in rows
        if row["quality"] in {LOSSLESS, LOSSY}
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["quality"]] = counts.get(row["quality"], 0) + 1
    return {
        "count": len(rows),
        "estimated_saving": saving,
        "by_quality": counts,
        "compatibility_only": counts.get(REMUX, 0),
        "protected": sum(1 for row in rows if row["protected_by"]),
    }


def scan_one(
    conn: sqlite3.Connection,
    settings: Settings,
    relpath: str,
    row: sqlite3.Row | None,
) -> tuple[Opportunity | None, int]:
    """Evaluate one file. Returns the opportunity, if any, and probes spent.

    One file at a time rather than a whole library, because the audit runs in
    time-bounded slices and a helper that walks everything cannot be stopped
    half way. The probe count comes back so the caller can report it: a second
    audit of an unchanged library should spend zero, and "the cache works" is
    otherwise an unfalsifiable claim.
    """
    #  Both protections, asked as one question. A protected *root* stops a
    #  folder being queued at all; a Format Policy folder says its originals
    #  are not to be traded away — and an optimization that replaces the
    #  original with a smaller encode is exactly that trade. The opportunity is
    #  still recorded and still shown; what it cannot become is a job.
    from librairy.format_policy import protecting, refused_classes

    item_id = int(row["id"]) if row is not None else None
    fingerprint = (row["fingerprint"] or "") if row is not None else ""
    before = probe_calls()
    facts = facts_for(
        conn, settings, item_id or 0, fingerprint, settings.library_dir / relpath
    )
    probes = probe_calls() - before
    if facts is None:
        return None, probes
    found = advise(
        relpath,
        facts,
        protected_by=protecting(conn, relpath),
        refused=refused_classes(conn, relpath),
    )
    if found is None:
        return None, probes
    return (
        Opportunity(
            **{**found.__dict__, "item_id": item_id, "fingerprint": fingerprint}
        ),
        probes,
    )


# Everything ffprobe can usefully say something about. Documents and archives
# are deliberately absent — see the module docstring.
MEDIA_SUFFIXES = {
    ".wav", ".aiff", ".aif", ".aifc", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus",
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mpg", ".mpeg", ".ts", ".webm",
}

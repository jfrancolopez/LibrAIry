"""Does AIFF -> FLAC keep every sample? Measured, not assumed.

LibrAIry calls this conversion **LOSSLESS**, which is a promise about the
audio and not about the file size. For WAV the promise is easy to believe;
AIFF is big-endian, has its own chunk layout, and carries 24-bit samples in a
form that is not simply the little-endian one with the bytes reversed. So the
claim is worth checking rather than extending by analogy.

The check is the only one that means anything: decode both files to *canonical
PCM* — the same sample format, byte order and layout for each side — and
compare cryptographic hashes of the raw samples. Comparing the FLAC to the AIFF
directly would compare containers; comparing durations or bit depths would
compare metadata.

    .venv/bin/python scripts/prove_aiff_roundtrip.py

Four cases, because the two things most likely to break are bit depth and
channel layout:

    16-bit mono      16-bit stereo
    24-bit mono      24-bit stereo
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

CASES = [
    ("16-bit mono", "pcm_s16be", 1, "s16le"),
    ("16-bit stereo", "pcm_s16be", 2, "s16le"),
    ("24-bit mono", "pcm_s24be", 1, "s24le"),
    ("24-bit stereo", "pcm_s24be", 2, "s24le"),
]


def ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


def canonical_pcm(path: Path, sample_format: str, channels: int, out: Path) -> str:
    """Decode to raw samples in one fixed representation, and hash them.

    `-f <fmt>` with no container: no header, no chunk order, no metadata — just
    the samples, in the layout both sides are forced into.
    """
    ffmpeg(
        "-i", str(path),
        "-map", "0:a:0",
        "-c:a", f"pcm_{sample_format}",
        "-ac", str(channels),
        "-f", sample_format,
        str(out),
    )
    return hashlib.blake2b(out.read_bytes(), digest_size=32).hexdigest()


def main() -> int:
    from librairy.optimization_exec import FLAC, PRESET_SUFFIX

    tmp = Path(tempfile.mkdtemp())
    print(f"preset {FLAC} -> {PRESET_SUFFIX[FLAC]}\n")
    print(f"{'case':16} {'aiff':>10} {'flac':>10} {'depth':>7}  samples identical")
    failures = 0
    for label, codec, channels, raw in CASES:
        source = tmp / f"{label.replace(' ', '-')}.aiff"
        # Two tones so the channels are distinguishable: a mono/stereo mix-up
        # that produced silence in one channel would otherwise still round-trip.
        ffmpeg(
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=660:duration=3",
            "-filter_complex", "[0:a][1:a]amerge=inputs=2[a]" if channels == 2 else "[0:a]anull[a]",
            "-map", "[a]", "-ac", str(channels), "-c:a", codec, str(source),
        )
        target = source.with_suffix(".flac")
        # The command LibrAIry itself would run for this preset: encode, do not
        # touch the samples, keep the layout.
        ffmpeg("-i", str(source), "-map", "0:a:0", "-c:a", "flac", str(target))

        # Named explicitly, because a silent downconvert to 16-bit would also
        # be a failure — one the hash catches, since the source side would still
        # decode its true 24 bits while the FLAC side would decode zeros in the
        # low byte. Reported anyway, so the claim does not rest on that argument.
        depth = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=bits_per_raw_sample", "-of", "csv=p=0", str(target)],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        before = canonical_pcm(source, raw, channels, tmp / "a.raw")
        after = canonical_pcm(target, raw, channels, tmp / "b.raw")
        same = before == after
        failures += not same
        print(
            f"{label:16} {source.stat().st_size:>10,} {target.stat().st_size:>10,}"
            f" {depth + '-bit':>7}"
            f"  {'yes' if same else 'NO'}"
            f"   {before[:16]}{'' if same else ' != ' + after[:16]}"
        )

    print()
    if failures:
        print(f"{failures} of {len(CASES)} did not round-trip — AIFF stays analysis-only")
        return 1
    print(f"all {len(CASES)} round-tripped sample-for-sample")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

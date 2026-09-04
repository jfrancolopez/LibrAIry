"""Which filesystem is at this path — as durably as the platform will say.

`/Volumes/Backup` is whatever was plugged in most recently. A mount point is
not an identity, and an unplugged disk often leaves its folder behind, so a
backup written into it lands on the system disk and looks like it worked.

LibrAIry answers "is this the drive we registered" with **two** facts, because
each covers the other's hole:

    volume identity   is this the same filesystem?
                      From the operating system. Survives a mount point
                      changing, which is the case a marker cannot help with

    registration      was this filesystem registered with LibrAIry?
    marker            A file we wrote. Survives anything, and works on
                      platforms and filesystems that expose no stable id

A marker alone can be cloned onto another disk — copy a backup drive and the
copy claims to be the original. A UUID alone says nothing about whether this
program has ever seen the drive before. Together they are hard to be wrong
about by accident, which is the standard that matters here: nobody is attacking
this, people just plug in the wrong disk.

## Best effort, and honest about it

There is no portable way to ask this question, so each platform is asked in its
own language and **"" is a legitimate answer**. A filesystem that cannot say
what it is falls back to the marker alone, which is the arrangement before this
module existed and is not a regression. What is refused is the *mismatch*: a
recorded identity that does not match the one found now.

    Linux    /proc/mounts and /dev/disk/by-uuid, both plain reads
    macOS    `diskutil info -plist`, the only way to get a volume UUID
    other    "" — the marker carries it

The Linux path is deliberately file reads rather than `lsblk` or `blkid`: this
program ships in a container, and the fewer binaries it needs the fewer ways
there are for it to work on the author's machine and not on somebody's NAS.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

#  How long the platform gets to answer. This runs before a transfer, not
#  during one, and a `diskutil` that has wedged should cost a fallback to the
#  marker rather than a worker cycle.
TIMEOUT = 5

_BY_UUID = Path("/dev/disk/by-uuid")
_MOUNTS = Path("/proc/mounts")


def identity_for(path: Path) -> str:
    """A stable identifier for the filesystem holding `path`, or "".

    Prefixed with how it was obtained, so a recorded value says which question
    it is the answer to — and a future platform can be added without the stored
    values from the old ones becoming ambiguous.
    """
    try:
        if sys.platform == "darwin":
            return _macos(path)
        if sys.platform.startswith("linux"):
            return _linux(path)
    except (OSError, ValueError, subprocess.SubprocessError):
        #  Every failure is the same failure: the platform did not answer, so
        #  the marker carries the identity on its own. Never an exception into
        #  a caller that is about to decide whether to copy somebody's photos.
        return ""
    return ""


def matches(recorded: str, found: str) -> bool:
    """Does the volume here match the one that was registered?

    Three-valued, deliberately, and the middle case is the important one:

        recorded and found agree      yes — same filesystem
        nothing was recorded          yes — the marker is the whole check
        recorded, and found is ""     yes — the platform stopped answering,
                                      and refusing every backup because
                                      `diskutil` changed would be worse than
                                      falling back to the marker
        recorded, found differs       **no** — this is a different filesystem

    Only an actual disagreement refuses. An absence is not a disagreement, and
    treating it as one would make a backup drive stop working on the day
    somebody upgraded their operating system.
    """
    if not recorded or not found:
        return True
    return recorded == found


def _macos(path: Path) -> str:
    """`diskutil info -plist`, which is how macOS says what a volume is."""
    result = subprocess.run(  # noqa: S603
        ["/usr/sbin/diskutil", "info", "-plist", str(path)],
        capture_output=True,
        timeout=TIMEOUT,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return ""
    found = plistlib.loads(result.stdout)
    uuid = str(found.get("VolumeUUID") or "")
    return f"uuid:{uuid}" if uuid else ""


def _linux(path: Path) -> str:
    """The device under this path, then the UUID that points at that device.

    Two plain reads. `/proc/mounts` says which device is mounted where, and
    `/dev/disk/by-uuid` is a directory of symlinks from UUID to device — so
    resolving each and matching gives the answer without a subprocess.
    """
    device = _device_for(path)
    if not device or not _BY_UUID.is_dir():
        return ""
    for link in _BY_UUID.iterdir():
        try:
            if link.resolve() == device:
                return f"uuid:{link.name}"
        except OSError:
            continue
    return ""


def _device_for(path: Path) -> Path | None:
    """The device mounted at the longest mount point containing `path`.

    Longest, because `/` matches everything: a drive at `/media/wd` is under
    both and only the deeper one names its filesystem.
    """
    if not _MOUNTS.is_file():
        return None
    resolved = path.resolve()
    best: tuple[int, Path] | None = None
    for line in _MOUNTS.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 2:  # noqa: PLR2004
            continue
        device, mount = parts[0], parts[1].replace("\\040", " ")
        if not device.startswith("/dev/"):
            continue
        point = Path(mount)
        if resolved == point or resolved.is_relative_to(point):
            depth = len(point.parts)
            if best is None or depth > best[0]:
                best = (depth, Path(device))
    return best[1].resolve() if best else None

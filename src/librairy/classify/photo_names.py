"""Readable names for images, worked out from what the filename already says.

Phones, cameras and screenshot tools all stamp the moment of capture into the
filename, in about a dozen different shapes:

    Screenshot from 2022-07-11 14-22-24.png     GNOME
    Screenshot 2022-03-01 093841.png            Windows
    Screenshot_20240612-101112_Chrome.png       Android
    vlcsnap-2022-04-23-12h46m20s082.png         VLC frame grab
    PXL_20240612_101112123.jpg                  Pixel
    IMG_20240612_101112.jpg                     most Android cameras

All of them sort badly, none of them say what the file is, and half carry the
name of an app nobody cares about. The date is the useful part, so the rename
keeps the date and throws the rest away.

Read out of the filename and nowhere else. A capture date lives in EXIF too,
but reading it needs a decoder per format -- and HEIC, the one that matters
most for iPhone photos, is a whole container format. A file whose name carries
no date keeps the name it has: no date at all beats a guessed one, and the
file's mtime is a guess (it changes on every copy).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from librairy.naming import slugify

# One pattern for every layout above: eight-digit or dash-separated date,
# optionally followed by a time using -, _, ., :, or the h/m/s of VLC.
_DATE = (
    r"(?P<year>19\d{2}|20\d{2})[-_. ]?(?P<month>0[1-9]|1[0-2])[-_. ]?(?P<day>0[1-9]|[12]\d|3[01])"
)
# macOS writes "2024-06-12 at 10.11.12 AM"; everyone else runs the time
# straight on after a separator, in 24-hour form.
_TIME_12 = (
    r"[-_. T]*(?:at[-_. ]+)?(?P<hour>1[0-2]|0?[1-9])[-_.h: ](?P<minute>[0-5]\d)"
    r"[-_.m: ](?P<second>[0-5]\d)[-_. ]*(?P<meridiem>[AaPp])\.?[Mm]"
)
_TIME_24 = (
    r"[-_. T]*(?:at[-_. ]+)?(?P<hour>[01]\d|2[0-3])[-_.h: ]?(?P<minute>[0-5]\d)"
    r"[-_.m: ]?(?P<second>[0-5]\d)"
)
# Most specific first. A 12-hour stamp read as 24-hour would file a 1pm
# screenshot as 01:38, and a wrong time is worse than no time at all.
_STAMPS = tuple(re.compile(pattern) for pattern in (_DATE + _TIME_12, _DATE + _TIME_24, _DATE))
_PHONE_SCREENSHOT = re.compile(r"^(screenshot|screen[-_ ]shot)[-_]\d", re.I)
_APP_PACKAGE = re.compile(r"\b(com|net|org)\.[a-z0-9_.]+", re.I)
_SCREENSHOT = re.compile(r"^(screenshot|screen[-_ ]shot|screengrab|scr[-_])", re.I)
_VIDEO_FRAME = re.compile(r"^(vlcsnap|snapshot|snap[-_]|capture)", re.I)
_CAMERA = re.compile(r"^(IMG|IMAGE|DSC|DSCN|DSCF|PIC|PICT|GOPR|DJI|MVIMG|PXL|VID)", re.I)


@dataclass(frozen=True)
class PhotoName:
    name: str
    reason: str = ""

    @property
    def renamed(self) -> bool:
        return bool(self.reason)


def photo_name(stem: str, suffix: str, *, event: str = "") -> PhotoName:
    """A friendlier filename for one image, or the tidied original.

    ``event`` is the folder LibrAIry has already decided this belongs to --
    "Italy", "Wedding". It is appended only when the name has no date to offer,
    because a date plus an event is long and the date is the part that sorts.
    """
    stamp = _find_stamp(stem)
    kind = _kind(stem)
    if stamp and kind:
        return PhotoName(
            f"{kind}-{_stamp_text(stamp)}{suffix}",
            f"renamed from the {_KIND_REASON[kind]} timestamp in its filename",
        )
    if stamp and (camera := _CAMERA.match(stem)):
        # The camera's own prefix is worth keeping: IMG and VID separate
        # photos from clips, and GOPR/DJI say which device shot it.
        return PhotoName(
            f"{camera.group(1).upper()}-{_stamp_text(stamp)}{suffix}",
            "renamed from the capture time in its filename",
        )
    tidy = slugify(stem)
    if event and _is_specific(event) and slugify(event).lower() not in tidy.lower():
        return PhotoName(f"{tidy}-{slugify(event)}{suffix}", f"named after its folder, {event}")
    return PhotoName(f"{tidy}{suffix}")


_KIND_REASON = {
    "phone-screenshot": "phone screenshot",
    "screenshot": "screenshot",
    "video-frame": "frame grab",
}
# Folders that say "images live here", not "these images are one event". The
# same list guards the event field in heuristics; repeating the idea here is
# cheaper than importing a classifier into a naming helper.
_GENERIC_EVENTS = {
    "unknown",
    "unsorted",
    "inbox",
    "new",
    "untitled",
    "files",
    "misc",
    "photos",
    "pictures",
    "images",
    "screenshots",
    "camera",
    "camera roll",
    "dcim",
    "downloads",
    "desktop",
}


def _kind(stem: str) -> str:
    if _PHONE_SCREENSHOT.match(stem) or (_SCREENSHOT.match(stem) and _APP_PACKAGE.search(stem)):
        # Android and iOS both stamp the app into the name; a desktop tool
        # never does. That is the only reliable tell without opening the file.
        return "phone-screenshot"
    if _SCREENSHOT.match(stem):
        return "screenshot"
    if _VIDEO_FRAME.match(stem):
        return "video-frame"
    return ""


def _find_stamp(stem: str) -> re.Match[str] | None:
    for pattern in _STAMPS:
        match = pattern.search(stem)
        if match is not None:
            return match
    return None


def _stamp_text(stamp: re.Match[str]) -> str:
    parts = stamp.groupdict()
    date = f"{parts['year']}-{parts['month']}-{parts['day']}"
    if parts.get("hour") is None:
        return date
    hour = int(parts["hour"])
    meridiem = (parts.get("meridiem") or "").lower()
    if meridiem == "p" and hour != 12:
        hour += 12
    elif meridiem == "a" and hour == 12:
        hour = 0
    return f"{date}-{hour:02d}{parts['minute']}{parts['second']}"


def _is_specific(event: str) -> bool:
    return event.strip().lower() not in _GENERIC_EVENTS

"""What a file extension means — reference text, not a decision.

Organising someone else's files means meeting `VTS_01_1.VOB` and `.BUP` and
`.pluginPayloadAttachment` and having to guess. This is the one place that
answers "what is this?", so that Review, Quarantine, Browse, Search, History,
item detail and Library Audit all give the same answer.

**It explains formats; it never classifies.** `.mp4` is a video container, not
a movie — the same container holds a family clip, a screen recording and a
film, and the day this file starts saying "movie" is the day it becomes a
second, worse classifier disagreeing with the real one. `.jpg` is an image,
not a photo. Nothing here reads a file, touches the network, or changes a
category, a confidence, a destination or a proposal.

Where a role exists, it is **derived** from the constants the classifier
already uses — `companions.SIDECAR_KINDS` and `mediakind.kind_for` — rather
than restated. A UI that called `.srt` a text document while the classifier
treated it as a subtitle companion would be worse than no UI.
"""

from __future__ import annotations

import itertools
import mimetypes
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.classify.companions import SIDECAR_KINDS, SIDECAR_LABEL
from librairy.mediakind import kind_for

# Extensions that only mean something as a pair. `archive.tar.gz` is a
# gzipped tar, not "a .gz"; `movie.en.forced.srt` is just a subtitle, which
# `PurePosixPath.suffix` already gets right.
COMPOUND = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")

# Files whose whole name is the type. `PurePosixPath(".DS_Store").suffix` is
# empty, so these would otherwise fall through as "no extension".
BY_NAME = {
    ".ds_store": "ds_store",
    "thumbs.db": "thumbs_db",
    "desktop.ini": "desktop_ini",
}


@dataclass(frozen=True)
class ExtensionInfo:
    """One entry of reference text. `caution` is advice about handling the
    format, never about whether to keep the file."""

    extension: str
    label: str
    description: str
    common_context: str = ""
    caution: str = ""
    role: str = ""
    mime: str = ""
    known: bool = True

    @property
    def title(self) -> str:
        return self.extension.upper() if self.known else self.extension


def _e(extension, label, description, context="", caution=""):
    return ExtensionInfo(extension, label, description, context, caution)


# Curated, and deliberately finite. The aim is to remove the "what is this
# weird file?" pause while organising, not to catalogue every format there is.
REGISTRY: dict[str, ExtensionInfo] = {entry.extension: entry for entry in (
    # --- images: an image, never "a photo" ---
    _e(".jpg", "JPEG image", "Compressed image. The most common photographic format.",
       "Cameras, phones, scanners and the web all produce these."),
    _e(".jpeg", "JPEG image", "Compressed image. The same format as .jpg.",
       "Some cameras and older software write the four-letter form."),
    _e(".png", "PNG image", "Lossless image, with optional transparency.",
       "Common for screenshots, artwork and graphics with flat colour."),
    _e(".webp", "WebP image", "Modern compressed image format from Google.",
       "Common in files saved from the web."),
    _e(".heic", "HEIC image", "High-efficiency image, the default on recent iPhones.",
       "Often arrives straight from an Apple device.",
       "Some older software cannot open these without conversion."),
    _e(".gif", "GIF image", "Indexed-colour image that may be animated.",
       "Short loops and simple graphics."),
    _e(".bmp", "Bitmap image", "Uncompressed image. Large for its size.", ""),
    _e(".tiff", "TIFF image", "Lossless image used for scans and print work.", ""),
    _e(".avif", "AVIF image", "Modern compressed image using AV1 encoding.", ""),
    _e(".dng", "Digital negative", "Raw sensor data from a camera, before processing.",
       "Often paired with a JPEG of the same shot."),

    # --- video: a container, never "a movie" ---
    _e(".mp4", "Video container", "Video container, widely supported.",
       "Holds anything from a phone clip to a feature film."),
    _e(".mov", "Video container", "QuickTime video container.",
       "Commonly produced by cameras and phones — the contents may be personal "
       "video rather than a film."),
    _e(".mkv", "Matroska container", "Flexible video container.",
       "Can carry several audio tracks and embedded subtitles in one file."),
    _e(".avi", "AVI container", "Older video container.", ""),
    _e(".webm", "WebM container", "Open video container intended for the web.", ""),
    _e(".m4v", "MPEG-4 video", "Apple's variant of the MP4 container.", ""),
    _e(".ts", "Transport stream", "Broadcast-style video stream.",
       "Often the raw output of a TV capture or a segmented download."),
    _e(".mpg", "MPEG video", "Older MPEG-1/2 video.", ""),
    _e(".wmv", "Windows Media video", "Microsoft video container.", ""),

    # --- audio ---
    _e(".mp3", "MP3 audio", "Compressed audio. The most widely supported format.", ""),
    _e(".flac", "FLAC audio", "Lossless compressed audio.",
       "Common for ripped CDs and high-quality downloads."),
    _e(".m4a", "AAC audio", "Compressed audio in an MP4 container.",
       "What iTunes and most phones produce."),
    _e(".aac", "AAC audio", "Compressed audio stream.", ""),
    _e(".ogg", "Ogg Vorbis audio", "Open compressed audio format.", ""),
    _e(".opus", "Opus audio", "Modern open audio codec, efficient at low bitrates.", ""),
    _e(".wav", "WAV audio", "Uncompressed audio. Large, and lossless.", ""),
    _e(".aiff", "AIFF audio", "Uncompressed audio, Apple's equivalent of WAV.", ""),
    _e(".wma", "Windows Media audio", "Microsoft audio format.", ""),

    # --- media sidecars ---
    _e(".m3u", "Playlist", "Playlist file listing other media files in order.",
       "Usually belongs with the album or collection it describes.",
       "It refers to files by path, so moving them separately can break it."),
    _e(".m3u8", "Playlist", "Playlist file, UTF-8 encoded.",
       "Usually belongs with the album or collection it describes.",
       "It refers to files by path, so moving them separately can break it."),
    _e(".cue", "Cue sheet", "Describes track boundaries and disc layout.",
       "Often belongs with the album or disc image it references.",
       "It names its audio file, so the two are meant to travel together."),
    _e(".lrc", "Synchronised lyrics", "Lyrics with timing information.",
       "Usually belongs beside the matching music track."),
    _e(".nfo", "Metadata sidecar", "Plain-text information about a release.",
       "Often accompanies a film, episode or album, and may list title, "
       "release, codec or catalogue details."),
    _e(".sfv", "Checksum list", "Lists CRC checksums for verifying a set of files.",
       "Usually left over from how a folder was transferred."),
    _e(".md5", "Checksum list", "Lists MD5 checksums for verifying files.", ""),

    # --- subtitles ---
    _e(".srt", "Subtitle", "Timed subtitle text.",
       "Belongs beside the matching film or episode.",
       "Players match subtitles by filename, so the stem is worth keeping."),
    _e(".ass", "Subtitle", "Advanced SubStation subtitle, with styling and positioning.",
       "Belongs beside the matching film or episode.",
       "Players match subtitles by filename, so the stem is worth keeping."),
    _e(".ssa", "Subtitle", "SubStation Alpha subtitle, the predecessor of .ass.",
       "Belongs beside the matching film or episode."),
    _e(".vtt", "Subtitle", "WebVTT subtitle, used by web players.", ""),
    _e(".sub", "Subtitle", "Subtitle data, usually image-based.",
       "Normally paired with an .idx file of the same name.",
       "The pair is useless split up."),
    _e(".idx", "Subtitle index", "Timing index for a matching .sub subtitle.",
       "Normally paired with a .sub file of the same name.",
       "The pair is useless split up."),

    # --- DVD structure ---
    _e(".ifo", "DVD information file",
       "Stores navigation and playback metadata for DVD-Video.",
       "Part of a VIDEO_TS folder, alongside .VOB and .BUP files.",
       "Structural DVD filenames are meaningful to players and are preserved "
       "rather than tidied."),
    _e(".bup", "DVD backup file", "Backup copy of the matching .IFO metadata.",
       "Part of a VIDEO_TS folder.",
       "Structural DVD filenames are meaningful to players and are preserved "
       "rather than tidied."),
    _e(".vob", "DVD video object", "Holds the actual video, audio and subtitles of a DVD.",
       "Part of a VIDEO_TS folder, usually numbered VTS_01_1 and upwards.",
       "A title is often split across several .VOB files that belong together."),

    # --- documents ---
    _e(".pdf", "PDF document", "Fixed-layout document.", ""),
    _e(".epub", "EPUB book", "Reflowable e-book format.", ""),
    _e(".mobi", "Mobipocket book", "Older Kindle e-book format.", ""),
    _e(".doc", "Word document", "Legacy Microsoft Word document.", ""),
    _e(".docx", "Word document", "Microsoft Word document.", ""),
    _e(".xls", "Excel spreadsheet", "Legacy Microsoft Excel spreadsheet.", ""),
    _e(".xlsx", "Excel spreadsheet", "Microsoft Excel spreadsheet.", ""),
    _e(".ppt", "PowerPoint presentation", "Legacy Microsoft PowerPoint file.", ""),
    _e(".pptx", "PowerPoint presentation", "Microsoft PowerPoint presentation.", ""),
    _e(".txt", "Plain text", "Unformatted text.", ""),
    _e(".md", "Markdown", "Plain text with lightweight formatting marks.", ""),
    _e(".rtf", "Rich text", "Formatted text, readable by most word processors.", ""),
    _e(".csv", "Comma-separated values", "Tabular data as plain text.", ""),
    _e(".odt", "OpenDocument text", "Word-processor document from LibreOffice or OpenOffice.", ""),

    # --- archives ---
    _e(".zip", "Archive", "Compressed archive holding one or more files.", ""),
    _e(".rar", "Archive", "RAR compressed archive.",
       "Often arrives split into numbered parts that are useless separately."),
    _e(".7z", "Archive", "7-Zip compressed archive.", ""),
    _e(".tar", "Archive", "Uncompressed bundle of files.", ""),
    _e(".gz", "Compressed file", "Gzip-compressed file.",
       "Frequently seen as .tar.gz, a compressed bundle."),
    _e(".tar.gz", "Compressed archive", "Gzip-compressed tar bundle.", ""),
    _e(".tar.bz2", "Compressed archive", "Bzip2-compressed tar bundle.", ""),
    _e(".tar.xz", "Compressed archive", "XZ-compressed tar bundle.", ""),
    _e(".tar.zst", "Compressed archive", "Zstandard-compressed tar bundle.", ""),

    # --- disk images and installers ---
    _e(".iso", "Disc image", "Byte-for-byte copy of an optical disc.", ""),
    _e(".dmg", "macOS disk image", "Disk image used to distribute macOS software.", ""),
    _e(".exe", "Windows program", "Windows executable.",
       "", "Executable files are never run by LibrAIry."),
    _e(".msi", "Windows installer", "Windows installer package.",
       "", "Installer files are never run by LibrAIry."),
    _e(".app", "macOS application", "A macOS application, which is really a folder.", ""),
    _e(".pkg", "macOS installer", "macOS installer package.", ""),

    # --- code and configuration ---
    _e(".json", "JSON data", "Structured data as text.", ""),
    _e(".yaml", "YAML configuration", "Structured configuration as text.", ""),
    _e(".yml", "YAML configuration", "Structured configuration as text.", ""),
    _e(".toml", "TOML configuration", "Structured configuration as text.", ""),
    _e(".xml", "XML data", "Structured markup data.", ""),
    _e(".ini", "Configuration file", "Simple key-and-value settings file.", ""),
    _e(".conf", "Configuration file", "Program settings as text.", ""),
    _e(".log", "Log file", "Plain-text record of what a program did.", ""),
    _e(".sql", "SQL script", "Database statements as text.", ""),
    _e(".py", "Python source", "Python program source code.", ""),
    _e(".sh", "Shell script", "Shell commands as text.",
       "", "Scripts are never run by LibrAIry."),
    _e(".js", "JavaScript source", "JavaScript program source code.", ""),
    _e(".ts", "TypeScript source", "TypeScript program source code.",
       "The same extension is also used for MPEG transport streams; the "
       "surrounding files usually make it obvious which this is."),
    _e(".gcode", "G-code", "Machine instructions for a 3D printer or CNC tool.",
       "Usually produced by slicing a 3D model for one specific machine."),
)}

# Whole-name entries, for files where the extension is not the useful part.
NAMED: dict[str, ExtensionInfo] = {
    "ds_store": ExtensionInfo(
        ".DS_Store", "macOS folder settings",
        "Records how a folder was displayed in the Finder.",
        "Created automatically by macOS, including over a network share.",
        "It holds no content of yours, but nothing here removes it for you.",
    ),
    "thumbs_db": ExtensionInfo(
        "Thumbs.db", "Windows thumbnail cache",
        "Cached thumbnail images for a folder.",
        "Created automatically by Windows Explorer.",
    ),
    "desktop_ini": ExtensionInfo(
        "desktop.ini", "Windows folder settings",
        "Records a folder's icon and display options.",
        "Created automatically by Windows.",
    ),
}


def resolve_extension(name: str) -> str:
    """The meaningful extension of a filename, lowercased.

    `movie.en.forced.srt` is a subtitle — the language and forced markers are
    part of the stem, and `.srt` is the type. `archive.tar.gz` is a compressed
    bundle, which only the pair says. Everything else is the last suffix.
    """
    filename = PurePosixPath(name).name
    lowered = filename.lower()
    for compound in COMPOUND:
        if lowered.endswith(compound):
            return compound
    suffix = PurePosixPath(filename).suffix
    # Known extensions answer in lower case so lookups are case-insensitive;
    # an unknown one keeps the case it was written in, because
    # `.pluginPayloadAttachment` is more recognisable than the flattened form.
    return suffix.lower() if suffix.lower() in REGISTRY else suffix


def extension_info(name: str) -> ExtensionInfo:
    """Reference text for a filename's type. Never reads the file."""
    filename = PurePosixPath(name).name
    named = BY_NAME.get(filename.lower())
    if named:
        return NAMED[named]

    extension = resolve_extension(filename)
    if not extension:
        return ExtensionInfo(
            extension="",
            label="No file extension",
            description="This file's name carries no extension.",
            common_context="Its type cannot be told from the name alone.",
            known=False,
        )

    known = REGISTRY.get(extension.lower())
    role = _role(filename)
    if known:
        return ExtensionInfo(
            extension=known.extension,
            label=known.label,
            description=known.description,
            common_context=known.common_context,
            caution=known.caution,
            role=role,
            mime=_mime(filename),
            known=True,
        )
    return ExtensionInfo(
        extension=extension,
        label="Unknown file type",
        description="LibrAIry has no built-in description for this extension.",
        common_context=(
            "The filename, the folder it is in, and the files beside it may say "
            "more than the extension does."
        ),
        role=role,
        mime=_mime(filename),
        known=False,
    )


def _role(filename: str) -> str:
    """What LibrAIry treats this file as — taken from the classifier, not
    restated here, so the two can never disagree on screen."""
    kind = SIDECAR_KINDS.get(resolve_extension(filename).lower())
    if kind:
        return f"Companion ({SIDECAR_LABEL.get(kind, kind)})"
    media = kind_for(filename)
    if media == "unsupported":
        return ""
    return {"image": "Image", "video": "Video", "audio": "Audio", "document": "Document"}[media]


def _mime(filename: str) -> str:
    """Only ever shown as a fallback detail. `mimetypes` is stdlib and local;
    nothing here consults a network database."""
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or ""


def aria_label(name: str) -> str:
    """What a screen reader announces for the info control."""
    info = extension_info(name)
    if not info.extension:
        return "About this file's type"
    return f"About {info.extension} files"


# One counter for every `?` rendered by this process.
#
# `popovertarget` resolves to the first element with a matching id, so two rows
# sharing an id would both open the first row's panel — a control that appears
# to work on some files and not others. Deriving the id from the filename does
# not help: two rows can legitimately hold two `.flac` files, and two rows can
# hold the same filename in different folders.
_ext_ids = itertools.count(1)


def next_ext_id() -> str:
    return f"ext-info-{next(_ext_ids)}"

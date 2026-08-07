from __future__ import annotations

import html
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from librairy.catalogs import catalog_enabled
from librairy.config import Settings
from librairy.humanize import human_bytes
from librairy.mediakind import kind_for
from librairy.paths import PathValidationError, validate_dest
from librairy.web.theme import ThemeSwatch, normalize_theme, swatch_for

LOGGER = logging.getLogger(__name__)
# poppler renders the first page of a PDF straight to a JPEG. It is already in
# the image for pdftotext, so this costs nothing extra to ship.
PAGE_RENDER_EXTENSIONS = {".pdf"}

# Enough to recognise a document without scrolling, and the panel scrolls if
# you want more. The old 700 characters, with every line break collapsed, was
# one grey paragraph -- unreadable for anything with structure, which is most
# of what a person keeps: notes, config, subtitles, code, a table.
PREVIEW_TEXT_CHARS = 4000
PREVIEW_TEXT_LINES = 80
THUMBNAIL_WIDTH = 320
THUMBNAIL_TIMEOUT_SECONDS = 20

# What a browser can play from the original file, with no transcoding. Anything
# else — .mkv and .avi above all — would need ffmpeg running for the length of
# the video, which is not a thing a file organiser should do to your NAS. Those
# still get a thumbnail, and the preview says plainly why there is no player.
PLAYABLE_VIDEO = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}
PLAYABLE_AUDIO = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
}


@dataclass(frozen=True)
class Preview:
    kind: str
    title: str
    thumb_url: str | None
    facts: tuple[str, ...]
    # First few hundred characters of a document, so "is this the right file?"
    # can be answered without leaving the page.
    body: str | None = None
    # Set only when the browser can play the original file as-is.
    media_url: str | None = None
    media_type: str | None = None
    # Why there is no player, when there is a good reason.
    no_play_reason: str | None = None


class PreviewError(RuntimeError):
    status_code = 500


class PreviewNotFound(PreviewError):
    status_code = 404


class PreviewForbidden(PreviewError):
    status_code = 403


def preview_for_item(conn, settings: Settings, item_id: int, *, bulk: bool = False) -> Preview:
    """One item's preview. `bulk` means "expand all" asked for this, not a person.

    In bulk, album art is only used when it is already on disk. Finding a cover
    for an unknown album costs a throttled MusicBrainz search, and a page of
    twenty-five tracks would serialise twenty-five of them — half a minute of
    the whole app waiting, to decorate rows nobody has looked at yet.
    """
    row = _item_row(conn, item_id)
    path = resolve_item_path(settings, row["root"], row["relpath"])
    kind = _kind(path)
    title = path.name
    if kind in {"image", "video"}:
        get_thumbnail(
            settings,
            path,
            kind,
            row["fingerprint"] or f"item-{item_id}",
            theme=_active_theme(conn),
        )
        facts = (f"type: {kind}", _size_fact(row["size"]))
        playable, reason = _playable(path, PLAYABLE_VIDEO) if kind == "video" else (None, None)
        return Preview(
            kind,
            title,
            f"/preview/items/{item_id}/thumb",
            facts,
            media_url=f"/preview/items/{item_id}/media" if playable else None,
            media_type=playable,
            no_play_reason=reason,
        )
    if kind == "audio":
        cover = _cover_for_audio(conn, settings, item_id, path, allow_search=not bulk)
        thumb_url = f"/preview/items/{item_id}/thumb" if cover else None
        playable, reason = _playable(path, PLAYABLE_AUDIO)
        return Preview(
            kind,
            title,
            thumb_url,
            ("type: audio", _size_fact(row["size"])),
            media_url=f"/preview/items/{item_id}/media" if playable else None,
            media_type=playable,
            no_play_reason=reason,
        )
    if kind == "document":
        page = _page_thumbnail(settings, path, row["fingerprint"] or f"item-{item_id}")
        return Preview(
            kind,
            title,
            f"/preview/items/{item_id}/thumb" if page else None,
            (f"type: {path.suffix.lstrip('.') or 'document'}", _size_fact(row["size"])),
            body=_text_snippet(path),
        )
    return Preview(
        "unsupported",
        title,
        None,
        (f"type: {path.suffix.lstrip('.') or 'no extension'}", _size_fact(row["size"])),
    )


def _size_fact(size: int | None) -> str:
    return f"size: {human_bytes(size)}"


def _text_snippet(path: Path) -> str | None:
    """Opening lines of a document, with its line structure intact.

    Collapsing whitespace turned a shopping list, a config file, a subtitle
    track and a CSV into the same grey paragraph. The shape of a file is half
    of what tells you which file it is, so the line breaks stay and the panel
    scrolls.

    Extraction shells out to pdftotext for PDFs, so any failure here — missing
    binary, encrypted file, scanned pages with no text layer — degrades to no
    snippet rather than breaking the page.
    """
    from librairy.content.extract import extract_text, extractor_name

    extractor = extractor_name(path)
    if extractor is None:
        return None
    try:
        text = extract_text(path, extractor)
    except Exception as exc:  # noqa: BLE001 - a preview is never worth an error page
        LOGGER.debug("preview text extraction failed for %s: %s", path, exc)
        return None
    return _trim_text(text)


def _trim_text(text: str) -> str | None:
    """First lines of a file, without the leading blank ones, capped both ways.

    Two caps rather than one: a minified stylesheet is one line of 200 KB, and
    a log with 40,000 short lines is just as much to send.
    """
    # Tabs render as eight columns and shove everything off the panel.
    lines = [line.replace("\t", "    ").rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None
    clipped = lines[:PREVIEW_TEXT_LINES]
    truncated = len(clipped) < len(lines)
    body = "\n".join(clipped)
    if len(body) > PREVIEW_TEXT_CHARS:
        body = body[:PREVIEW_TEXT_CHARS].rstrip()
        truncated = True
    return f"{body}\n…" if truncated else body


def _cover_for_audio(
    conn, settings: Settings, item_id: int, path: Path, *, allow_search: bool = True
) -> Path | None:
    """Album art for one track, or None. Never raises — a cover is a nicety.

    Deliberately lazy: this runs when someone opens a preview, not during
    analysis. Looking up a release for every track in an inbox would cost a
    MusicBrainz request per album at one request a second, to fetch art that
    nobody may ever look at. One file at a time, on demand, costs nothing.
    """
    try:
        if not catalog_enabled(conn, "coverart"):
            return None
        release_id = _release_mbid(conn, settings, item_id, path, allow_search=allow_search)
        if not release_id:
            return None
        from librairy.tools.coverart import cover_path

        cached = settings.appdata_dir / "thumbs" / f"cover-{release_id.lower()}.jpg"
        if not allow_search and not cached.exists():
            return None
        return cover_path(settings.appdata_dir, release_id)
    except Exception as exc:  # noqa: BLE001 - never break a page over album art
        LOGGER.debug("cover art lookup failed for item %s: %s", item_id, exc)
        return None


def _release_mbid(
    conn, settings: Settings, item_id: int, path: Path, *, allow_search: bool = True
) -> str:
    """The release MBID from evidence, else searched for from the file's tags.

    Only the AcoustID fingerprint path records a release MBID, and most music
    in a real library is tagged rather than fingerprinted — so the tag-based
    search is the case that actually fires.
    """
    from librairy.proposals import decode_evidence

    row = conn.execute(
        "SELECT evidence FROM proposals WHERE item_id=? AND status != 'superseded'",
        (item_id,),
    ).fetchone()
    if row is not None:
        for entry in decode_evidence(row["evidence"]):
            if entry.source == "musicbrainz" and entry.field == "release_id" and entry.detail:
                return str(entry.detail)

    if not allow_search or not catalog_enabled(conn, "musicbrainz"):
        return ""
    tags = _audio_tags(path, settings)
    from librairy.tools.musicbrainz import search_release

    return search_release(tags.get("artist", ""), tags.get("album", "")) or ""


def _audio_tags(path: Path, settings: Settings) -> dict[str, str]:
    from librairy.tools.ffprobe import probe

    result = probe(path, settings)
    if not result.ok or not isinstance(result.data, dict):
        return {}
    tags = result.data.get("tags") or {}
    return {str(key).lower(): str(value) for key, value in tags.items()}


def thumbnail_for_item(conn, settings: Settings, item_id: int) -> Path:
    row = _item_row(conn, item_id)
    path = resolve_item_path(settings, row["root"], row["relpath"])
    kind = _kind(path)
    if kind == "audio":
        cover = _cover_for_audio(conn, settings, item_id, path)
        if cover is None:
            raise PreviewNotFound("no cover art for this release")
        return cover
    if kind == "document":
        page = _page_thumbnail(settings, path, row["fingerprint"] or f"item-{item_id}")
        if page is None:
            raise PreviewNotFound("no page image for this document")
        return page
    if kind not in {"image", "video"}:
        raise PreviewNotFound("thumbnail unavailable")
    return get_thumbnail(
        settings,
        path,
        kind,
        row["fingerprint"] or f"item-{item_id}",
        theme=_active_theme(conn),
    )


def get_thumbnail(
    settings: Settings,
    source: Path,
    kind: str,
    fingerprint: str,
    *,
    theme: str | None = None,
) -> Path:
    thumbs = settings.appdata_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    stem = _safe_fingerprint(fingerprint)

    # A real picture of the file, rendered by ffmpeg. This is what makes Browse
    # usable for photos — a placeholder that says "IMAGE PREVIEW" tells you
    # nothing a filename did not already.
    raster = thumbs / f"{stem}-{kind}.jpg"
    if raster.exists():
        return raster
    if _render_thumbnail(source, raster, kind, settings):
        return raster

    # No ffmpeg, or a file it cannot decode. The drawn placeholder is themed, so
    # the palette is part of its cache key.
    name = normalize_theme(theme)
    target = thumbs / f"{stem}-{kind}-{name}.svg"
    if not target.exists():
        _write_svg_thumbnail(target, source.name, kind, swatch_for(name))
    return target


def thumbnail_media_type(path: Path) -> str:
    return "image/jpeg" if path.suffix.lower() == ".jpg" else "image/svg+xml"


def _page_thumbnail(settings: Settings, source: Path, fingerprint: str) -> Path | None:
    """The first page of a document as a picture, or None.

    A cover is how anyone recognises a PDF -- the first line of its text is
    usually a header nobody reads. poppler ships in the image already for
    pdftotext, and no page render is worth an error page, so every failure
    here just means the preview falls back to text.
    """
    if source.suffix.lower() not in PAGE_RENDER_EXTENSIONS:
        return None
    if shutil.which("pdftoppm") is None:
        return None
    thumbs = settings.appdata_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    target = thumbs / f"{_safe_fingerprint(fingerprint)}-page.jpg"
    if target.exists():
        return target
    # -singlefile makes pdftoppm write exactly <prefix>.jpg rather than
    # <prefix>-01.jpg, which is the difference between one predictable path
    # and guessing at poppler's page-number padding.
    prefix = target.with_suffix("")
    command = [
        "pdftoppm", "-jpeg", "-r", "72", "-f", "1", "-l", "1",
        "-scale-to", str(THUMBNAIL_WIDTH), "-singlefile", str(source), str(prefix),
    ]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, capture_output=True, timeout=THUMBNAIL_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.debug("page render failed for %s: %s", source, exc)
        return None
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        return None
    return target


def _render_thumbnail(source: Path, target: Path, kind: str, settings: Settings) -> bool:
    """Scale `source` down into `target` with ffmpeg. False if that is not possible.

    Never upscales, and keeps dimensions even so the JPEG encoder is happy.
    Writes to a temporary file first, so a killed ffmpeg cannot leave a
    half-written thumbnail in the cache to be served forever.
    """
    if shutil.which("ffmpeg") is None:
        return False
    scale = f"scale='min({THUMBNAIL_WIDTH},iw)':-2"
    command = ["ffmpeg", "-y", "-loglevel", "error"]
    if kind == "video":
        # A frame from a little way in; frame zero is often black.
        command += ["-ss", "3"]
    command += ["-i", str(source), "-vf", scale, "-frames:v", "1", "-f", "image2"]
    partial = target.with_suffix(".jpg.part")
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*command, str(partial)],
            capture_output=True,
            timeout=THUMBNAIL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.debug("thumbnail render failed for %s: %s", source, exc)
        partial.unlink(missing_ok=True)
        return False
    if result.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        return False
    partial.replace(target)
    return True


def _active_theme(conn) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key='appearance.theme'").fetchone()
    return normalize_theme(json.loads(row["value"]) if row else None)


def prune_cache(settings: Settings, max_bytes: int) -> None:
    thumbs = settings.appdata_dir / "thumbs"
    if not thumbs.exists():
        return
    files = [path for path in thumbs.rglob("*") if path.is_file()]
    total = sum(path.stat().st_size for path in files)
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        if total <= max_bytes:
            break
        # This cache is LibrAIry-generated under appdata/thumbs; pruning never touches user files.
        size = path.stat().st_size
        path.unlink()
        total -= size


def _playable(path: Path, table: dict[str, str]) -> tuple[str | None, str | None]:
    media_type = table.get(path.suffix.lower())
    if media_type:
        return media_type, None
    label = path.suffix.lstrip(".").upper()
    return None, f"{label} does not play in a browser without converting it"


def media_for_item(conn, settings: Settings, item_id: int) -> tuple[Path, str]:
    """The original file, for the player. Same containment check as previews.

    Only formats a browser plays natively are served: an endpoint that streams
    whatever it is asked for is a file-exfiltration route wearing a nice hat.
    """
    row = _item_row(conn, item_id)
    path = resolve_item_path(settings, row["root"], row["relpath"])
    media_type = PLAYABLE_VIDEO.get(path.suffix.lower()) or PLAYABLE_AUDIO.get(path.suffix.lower())
    if not media_type:
        raise PreviewForbidden("this file type is not playable in a browser")
    return path, media_type


def resolve_item_path(settings: Settings, root: str, relpath: str) -> Path:
    base = _root_path(settings, root)
    try:
        path = validate_dest(base, relpath)
    except PathValidationError as exc:
        LOGGER.warning("preview path rejected for %s:%s: %s", root, relpath, exc)
        raise PreviewForbidden(str(exc)) from exc
    if not path.exists():
        raise PreviewNotFound("source file not found")
    return path


def _item_row(conn, item_id: int):
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        raise PreviewNotFound("item not found")
    return row


def _kind(path: Path) -> str:
    return kind_for(path)


def _root_path(settings: Settings, root: str) -> Path:
    if root == "inbox":
        return settings.inbox_dir
    if root == "library":
        return settings.library_dir
    if root == "quarantine":
        return settings.quarantine_dir
    raise PreviewForbidden("unknown item root")


def _write_svg_thumbnail(target: Path, name: str, kind: str, swatch: ThemeSwatch) -> None:
    label = html.escape(f"{kind.upper()} PREVIEW")
    filename = html.escape(name[:48])
    label_line = _svg_text(82, label, swatch.accent, 20)
    file_line = _svg_text(112, filename, swatch.text, 13)
    target.write_text(
        f"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">
<rect width="320" height="180" fill="{swatch.background}"/>
<rect x="10" y="10" width="300" height="160" fill="none" stroke="{swatch.border}" stroke-width="2"/>
{label_line}
{file_line}
</svg>""",
        encoding="utf-8",
    )


def _svg_text(y: int, value: str, fill: str, size: int) -> str:
    return (
        f'<text x="160" y="{y}" fill="{fill}" font-family="monospace" '
        f'font-size="{size}" text-anchor="middle">{value}</text>'
    )


def _safe_fingerprint(fingerprint: str) -> str:
    return "".join(char for char in fingerprint if char.isalnum() or char in {"-", "_"})[:80]

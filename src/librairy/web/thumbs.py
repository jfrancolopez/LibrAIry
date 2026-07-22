from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.paths import PathValidationError, validate_dest

LOGGER = logging.getLogger(__name__)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic", ".avif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac"}


@dataclass(frozen=True)
class Preview:
    kind: str
    title: str
    thumb_url: str | None
    facts: tuple[str, ...]


class PreviewError(RuntimeError):
    status_code = 500


class PreviewNotFound(PreviewError):
    status_code = 404


class PreviewForbidden(PreviewError):
    status_code = 403


def preview_for_item(conn, settings: Settings, item_id: int) -> Preview:
    row = _item_row(conn, item_id)
    path = resolve_item_path(settings, row["root"], row["relpath"])
    kind = _kind(path)
    title = path.name
    if kind in {"image", "video"}:
        get_thumbnail(settings, path, kind, row["fingerprint"] or f"item-{item_id}")
        facts = (f"type: {kind}", f"size: {row['size']} bytes")
        return Preview(kind, title, f"/preview/items/{item_id}/thumb", facts)
    if kind == "audio":
        return Preview(kind, title, None, ("type: audio", f"size: {row['size']} bytes"))
    return Preview("unsupported", title, None, ("type: unsupported",))


def thumbnail_for_item(conn, settings: Settings, item_id: int) -> Path:
    row = _item_row(conn, item_id)
    path = resolve_item_path(settings, row["root"], row["relpath"])
    kind = _kind(path)
    if kind not in {"image", "video"}:
        raise PreviewNotFound("thumbnail unavailable")
    return get_thumbnail(settings, path, kind, row["fingerprint"] or f"item-{item_id}")


def get_thumbnail(settings: Settings, source: Path, kind: str, fingerprint: str) -> Path:
    thumbs = settings.appdata_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    target = thumbs / f"{_safe_fingerprint(fingerprint)}-{kind}.svg"
    if not target.exists():
        _write_svg_thumbnail(target, source.name, kind)
    return target


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
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    return "unsupported"


def _root_path(settings: Settings, root: str) -> Path:
    if root == "inbox":
        return settings.inbox_dir
    if root == "library":
        return settings.library_dir
    if root == "quarantine":
        return settings.quarantine_dir
    raise PreviewForbidden("unknown item root")


def _write_svg_thumbnail(target: Path, name: str, kind: str) -> None:
    label = html.escape(f"{kind.upper()} PREVIEW")
    filename = html.escape(name[:48])
    label_line = _svg_text(82, label, "#ffbf4d", 20)
    file_line = _svg_text(112, filename, "#56d364", 13)
    target.write_text(
        f"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">
<rect width="320" height="180" fill="#061008"/>
<rect x="10" y="10" width="300" height="160" fill="none" stroke="#56d364" stroke-width="2"/>
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

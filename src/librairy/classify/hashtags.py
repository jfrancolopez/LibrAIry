from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.models import EvidenceEntry
from librairy.paths import sanitize_component

TAG_RE = re.compile(r"#([^\s#]+)")


@dataclass(frozen=True)
class HashtagHints:
    tags: tuple[str, ...]
    nearest: str | None
    evidence: tuple[EvidenceEntry, ...]


def extract_hashtags(relpath: str) -> HashtagHints:
    folders = PurePosixPath(relpath).parent.parts
    tags_by_folder: list[list[str]] = []
    for folder in folders:
        tags = [_sanitize_tag(match) for match in TAG_RE.findall(folder)]
        tags = [tag for tag in tags if tag]
        tags_by_folder.append(tags)

    flattened = tuple(tag for tags in tags_by_folder for tag in tags)
    nearest = next((tags[0] for tags in reversed(tags_by_folder) if tags), None)
    evidence = tuple(
        EvidenceEntry("hashtag", "tag", tag, 0.7)
        for tag in flattened
    )
    return HashtagHints(flattened, nearest, evidence)


def strip_hashtags(value: str) -> str:
    return TAG_RE.sub("", value).replace("#", "").strip()


def strip_hashtags_from_relpath(relpath: str) -> str:
    raw_parts = PurePosixPath(relpath).parts
    parts: list[str] = []
    for index, part in enumerate(raw_parts):
        stripped = strip_hashtags(part).strip()
        if index == len(raw_parts) - 1:
            suffix = PurePosixPath(part).suffix
            if suffix and stripped and not stripped.endswith(suffix):
                stripped = f"{stripped}{suffix}"
        parts.append(stripped)
    return PurePosixPath(*[part for part in parts if part]).as_posix()


def _sanitize_tag(tag: str) -> str:
    tag = tag.replace("/", " ").replace("\\", " ").replace(".", " ")
    try:
        return sanitize_component(tag).lower().replace(" ", "-")
    except ValueError:
        return ""

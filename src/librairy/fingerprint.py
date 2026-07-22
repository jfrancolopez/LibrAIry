from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_SIZE = 1024 * 1024


def blake2b_file(path: Path) -> str:
    digest = hashlib.blake2b()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()

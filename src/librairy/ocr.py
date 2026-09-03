"""Reading a document that has no text to read — and only then.

OCR is the most expensive thing LibrAIry can do to a document and the least
necessary: the overwhelming majority of PDFs already carry a text layer, and
running tesseract over one tells you what `pdftotext` told you a hundred times
faster. So this module is mostly a set of reasons **not** to run it.

    the file is not a PDF                       nothing to rasterise
    it has a text layer                         pdftotext already answered
    OCR is switched off                         it ships off, and stays off
    tesseract is not installed                  the image ships without it
    the processing mode says not now            see `librairy/resources.py`
    this cycle has read enough pages already    see `Budget`

Everything that passes all six gets **two pages**, not the document. A title
page is at the front or it is nowhere, which is the same rule `docmeta` already
applies to text extraction, and it is what turns "OCR a 643-page manual" into a
bounded piece of work.

## Why it is off by default

It adds `tesseract-ocr` and a language pack to the image — tens of megabytes
for a feature most libraries do not need — and it is the one document operation
whose cost is measured in seconds per file rather than milliseconds. Somebody
with a drawer of scans should switch it on. Nobody should have it switched on
without knowing.

## Why the *processing* mode governs it and not the AI mode

OCR is deterministic. Tesseract is a program that turns pixels into characters;
it has no model of what a document is, it makes no judgement, and switching
"Local AI" off must not stop a scanner's output being readable. It is CPU-heavy
work like hashing and encoding are CPU-heavy work, and it is bounded by the
same axis they are.

## What OCR text is worth

Evidence, at the same level as front-matter text: something read off the file,
weaker than an identifier and weaker than what the producing application wrote
down. It is a **candidate** in `document_identity`, never an answer on its own —
a title OCR read out of a blurry cover has exactly one vote, and one vote is
never a preselection.

See `docs/ROADMAP.md` M2-02.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from librairy.resources import ProcessingMode

LOGGER = logging.getLogger(__name__)

SETTING = "documents.ocr_enabled"

#  The same two pages `docmeta` extracts text from, for the same reason: a
#  title page is at the front or it is nowhere. This is what keeps OCR a
#  bounded cost per document rather than a cost proportional to the document.
PAGES = 2

#  Long enough for two pages of a scan on a NAS, short enough that a pathological
#  file cannot hold the worker. A timeout is "nothing was read", like every
#  other reader failure here.
SECONDS = 60

BINARY = "tesseract"


def available() -> bool:
    """Is there a tesseract on this machine? Detected, never installed."""
    return shutil.which(BINARY) is not None


def enabled(conn) -> bool:  # noqa: ANN001
    """Off unless somebody switched it on. See the module docstring."""
    import json

    if conn is None:
        return False
    row = conn.execute("SELECT value FROM settings WHERE key=?", (SETTING,)).fetchone()
    if row is None:
        return False
    try:
        return bool(json.loads(row["value"]))
    except (TypeError, ValueError):
        return False


def set_enabled(conn, value: bool) -> None:  # noqa: ANN001
    import json

    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (SETTING, json.dumps(bool(value))),
    )


@dataclass
class Budget:
    """How many documents this cycle may read pixels for.

    Per cycle rather than per document, because the thing worth bounding is
    what LibrAIry takes from the machine over time and not what any one file
    costs. A document whose turn does not come is **not** analysed this cycle —
    it stays `discovered` and the next cycle reaches it — so the answer is the
    same either way and only the timing moves. That is the rule every
    processing mode is held to: a mode changes the rate and never the answer.

    `spent` is deliberately not reset by anything. One of these is made per
    batch and thrown away with it.
    """

    limit: int | None
    spent: int = 0
    #  Items this batch declined to read for want of budget. `analyze_items`
    #  leaves them where they were rather than answering them badly.
    deferred: list[int] = field(default_factory=list)
    #  Which item is being classified right now. Set by the batch loop before
    #  each file, the same way the AI attempt is recorded, because the reader
    #  is handed a path and a path cannot say which row it belongs to.
    item: int = 0

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.spent >= self.limit

    def take(self) -> bool:
        """Spend one, or record that this item did not get its turn."""
        if self.exhausted:
            if self.item:
                self.deferred.append(self.item)
            return False
        self.spent += 1
        return True


def budget_for(mode: ProcessingMode) -> Budget:
    return Budget(mode.ocr_per_cycle)


def reader(conn, mode: ProcessingMode, budget: Budget):  # noqa: ANN001
    """A callable `docmeta` can hand a path, or `None` for "do not read pixels".

    `None` rather than a callable that returns nothing, so that the six reasons
    not to run are answered **once per batch** instead of once per document —
    and so that `docmeta` keeps knowing nothing about settings, modes or what
    is installed.
    """
    if not mode.ocr or not enabled(conn) or not available():
        return None

    def read(path: Path) -> str:
        if not budget.take():
            return ""
        return read_pages(path)

    return read


def read_pages(path: Path, *, run=None, pages: int = PAGES) -> str:  # noqa: ANN001
    """The first pages of a scan, as text. Never raises.

    Two subprocesses because tesseract does not read PDFs: poppler renders the
    front matter to PNGs in a temporary directory, tesseract reads those, and
    both are gone before this returns. Every failure — no binary, a timeout, a
    page poppler cannot rasterise — is "nothing was read", which is a normal
    outcome for a photograph of a page and must never cost the batch.
    """
    import tempfile

    runner = run or subprocess.run
    try:
        with tempfile.TemporaryDirectory(prefix="librairy-ocr-") as work:
            stem = Path(work) / "page"
            rendered = runner(
                ["pdftoppm", "-r", "150", "-l", str(pages), "-png", str(path), str(stem)],
                capture_output=True,
                text=True,
                timeout=SECONDS,
                check=False,
            )
            if getattr(rendered, "returncode", 1) != 0:
                return ""
            found = ""
            for image in sorted(Path(work).glob("page*.png")):
                result = runner(
                    [BINARY, str(image), "stdout"],
                    capture_output=True,
                    text=True,
                    timeout=SECONDS,
                    check=False,
                )
                if getattr(result, "returncode", 1) == 0:
                    found += result.stdout or ""
            return found
    except Exception:  # noqa: BLE001 - a page nobody could read is not an error
        LOGGER.debug("OCR could not read %s", path.name, exc_info=True)
        return ""

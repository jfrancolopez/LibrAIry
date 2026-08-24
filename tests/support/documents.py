"""Real documents for the document tests.

A fixture that mocked `pdfinfo` would prove the mock. These build small but
genuinely valid files, so poppler reads them the way it reads a manual off a
manufacturer's website — and the scanned case is genuinely a PDF with pages and
no text in them, rather than a flag somebody set.
"""

from __future__ import annotations

import zipfile
from pathlib import Path


def build_pdf(
    *, title: str = "", author: str = "", lines: tuple[str, ...] = (), pages: int = 1
) -> bytes:
    """A valid PDF with an Info dictionary and, optionally, text on page one."""

    def esc(value: str) -> str:
        return value.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    objects: list[bytes] = []
    info = []
    if title:
        info.append(f"/Title ({esc(title)})")
    if author:
        info.append(f"/Author ({esc(author)})")
    info.append("/Producer (LibrAIry test)")
    objects.append(("<< " + " ".join(info) + " >>").encode("latin-1"))
    kids = " ".join(f"{4 + index * 2} 0 R" for index in range(pages))
    objects.append(b"<< /Type /Catalog /Pages 3 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    font = 4 + pages * 2
    for index in range(pages):
        objects.append(
            f"<< /Type /Page /Parent 3 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> "
            f"/Contents {5 + index * 2} 0 R >>".encode()
        )
        body = (
            "BT /F1 12 Tf 72 720 Td "
            + " ".join(
                f"({esc(line)}) Tj 0 -16 Td" for line in (lines if index == 0 else ())
            )
            + " ET"
        )
        stream = body.encode("latin-1")
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + payload + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += (
        b"trailer\n<< /Size %d /Root 2 0 R /Info 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref)
    )
    return bytes(out)


def write_epub(
    path: Path, *, title: str, author: str, identifier: str = "", date: str = ""
) -> None:
    """A minimal EPUB: a zip with an OPF manifest, which is all `docmeta` reads."""
    opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    {f'<dc:identifier id="id">{identifier}</dc:identifier>' if identifier else ''}
    {f'<dc:date>{date}</dc:date>' if date else ''}
  </metadata>
</package>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/chapter1.xhtml", "<html><body><p>Once upon.</p></body></html>")

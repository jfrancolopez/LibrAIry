"""Reading a rendered page the way a person reads it.

One helper, and it exists because of `<wbr>`. A library path is printed with a
break opportunity after each separator so that a phone breaks it at a folder
rather than in the middle of a filename — see `web/app.py:_wrappable` — and
that is markup with no words in it. An assertion about what a page *says* has
to see the path, not the opportunities.
"""

from __future__ import annotations


def words(html: str) -> str:
    """The page with its line-break opportunities taken out."""
    return html.replace("<wbr>", "")

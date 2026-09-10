"""Reading a rendered page the way a person reads it.

Two helpers, and each exists because of markup that carries no words.

`words` exists because of `<wbr>`. A library path is printed with a break
opportunity after each separator so that a phone breaks it at a folder rather
than in the middle of a filename — see `web/app.py:_wrappable` — and that is
markup with no words in it. An assertion about what a page *says* has to see
the path, not the opportunities.

`said` goes further, and exists because a sentence a person reads is not a
sentence in the source: "the container's user" is `the container&#39;s user`,
and a phrase that happens to straddle a `<strong>` is two strings with a tag
between them. A test that asserts on the source is a test that passes or fails
on where the emphasis went.
"""

from __future__ import annotations

import re
from html import unescape


def words(html: str) -> str:
    """The page with its line-break opportunities taken out."""
    return html.replace("<wbr>", "")


def said(html: str) -> str:
    """Everything the page says, as one run of text.

    Scripts and styles removed, tags replaced by nothing at all so that a phrase
    split by `<strong>` survives, entities resolved, and whitespace collapsed to
    single spaces — including the newlines a template's indentation puts inside
    a sentence.
    """
    text = words(html)
    text = re.sub(r"(?s)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    #  Block boundaries become spaces so two sentences do not run together;
    #  inline tags vanish so one sentence does not come apart.
    text = re.sub(r"</?(p|div|li|tr|td|th|h[1-6]|section|header|br|summary)\b[^>]*>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()

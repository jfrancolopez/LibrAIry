"""Every control a person can press, read off the rendered page.

Templates were the wrong place to look. `test_ui_consistency.py` reads them,
and it can only see labels that are literal text — a button whose label is
`{{ job.label }}`, or one that only exists inside an `{% if %}` no fixture
satisfies, is invisible to it. The pages that had drifted furthest were exactly
the ones built out of conditionals.

So this reads the *output*: the fixture library, every populated surface, and
every button, submit, action link and disclosure that came back. What that
makes checkable is the thing template-reading cannot answer —

    the same word on two pages doing two different things
    two words on two pages doing the same thing
    a control pointing at a route that does not exist
    a control with no accessible name at all

`tests/test_control_inventory.py` asserts those. `scripts/control_inventory.py`
prints the same inventory for a person. Both use this; there is one extractor
so the report and the test cannot disagree about what a control is.

Development only, like the rest of `tests/dev`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Every surface the fixture actually fills. A page with no rows on it proves
# nothing about the controls that live on rows, which is how Search, History
# and Quarantine went a whole pass with their shared control unexercised.
SURFACES = (
    "/dashboard",
    "/review",
    "/review?state=confident",
    "/maintenance/optimization",
    "/commit",
    "/quarantine",
    "/quarantine?view=waiting",
    "/quarantine?view=delete-queue",
    "/history",
    "/browse",
    "/browse/Music",
    "/browse?q=IMG_4021",
    "/health",
    "/settings",
)

#  A path with an id in it is the same control on every row. `/history/undo/3`
#  and `/history/undo/12` are one endpoint, and comparing them as strings makes
#  every list page look like it has fifty different buttons on it.
_ID = re.compile(
    r"/(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}|fixture-[a-z-]+)(?=/|$)"
)


def normalize(target: str) -> str:
    """One endpoint, whatever row it was drawn on."""
    if not target:
        return ""
    path = target.split("?", 1)[0]
    return _ID.sub("/{id}", path)


@dataclass(frozen=True)
class Control:
    """One thing on one page that a person can press."""

    page: str
    tag: str
    label: str
    method: str = ""
    target: str = ""
    #  What a screen reader would announce. Text if there is any, otherwise
    #  whatever the author supplied instead.
    name: str = ""
    popover: str = ""
    form: str = ""
    value: str = ""
    attrs: dict[str, str] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        """Method and path with row ids folded away, or "" for a local control."""
        return f"{self.method} {normalize(self.target)}" if self.target else ""


#  Where a control says where it is going, in the order it wins.
_TARGETS = (
    ("hx-post", "POST"),
    ("hx-put", "PUT"),
    ("hx-delete", "DELETE"),
    ("hx-patch", "PATCH"),
    ("hx-get", "GET"),
    ("formaction", "POST"),
    ("href", "GET"),
)

_TAG = re.compile(r"<[^>]+>")


class _Reader(HTMLParser):
    """Buttons, action links and disclosures, with the form they belong to.

    A `<button>` inside a `<form>` posts to that form's action, and a button
    carrying `form="..."` posts to a form somewhere else on the page. Neither
    is visible on the button itself, so the form stack is tracked here — a
    control whose destination is unknown cannot be checked against the router.
    """

    CONTROLS = ("button", "summary")

    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page = page
        self.found: list[Control] = []
        self.forms: list[dict[str, str]] = []
        self.form_ids: dict[str, str] = {}
        self._open: dict | None = None
        self._depth = 0

    # -- the form a button lives in -------------------------------------------

    def handle_starttag(self, tag: str, attrs: list) -> None:  # noqa: D102
        values = {key: (value or "") for key, value in attrs}
        if tag == "form":
            self.forms.append(values)
            if values.get("id"):
                self.form_ids[values["id"]] = values.get("action", "")
        if self._open is not None:
            self._depth += 1
            return
        if tag in self.CONTROLS or (tag == "a" and self._is_action(values)):
            self._open = {"tag": tag, "attrs": values, "text": ""}
            self._depth = 0

    def handle_startendtag(self, tag: str, attrs: list) -> None:  # noqa: D102
        if tag == "input":
            values = {key: (value or "") for key, value in attrs}
            if values.get("type") == "submit":
                self.found.append(self._build("input", values, values.get("value", "")))

    def handle_endtag(self, tag: str) -> None:  # noqa: D102
        if tag == "form" and self.forms:
            self.forms.pop()
        if self._open is None:
            return
        if self._depth:
            self._depth -= 1
            return
        if tag != self._open["tag"]:
            return
        text = " ".join(_TAG.sub("", self._open["text"]).split())
        self.found.append(self._build(self._open["tag"], self._open["attrs"], text))
        self._open = None

    def handle_data(self, data: str) -> None:  # noqa: D102
        if self._open is not None:
            self._open["text"] += data

    # -- building one ----------------------------------------------------------

    @staticmethod
    def _is_action(values: dict[str, str]) -> bool:
        """A link styled as an action. A sentence with a link in it is not one."""
        classes = values.get("class", "").split()
        return "btn" in classes or any(name.startswith("btn-") for name in classes)

    @staticmethod
    def _hx_action(values: dict[str, str]) -> str:
        """The `action` an htmx control posts, which is not on the button.

        Review's row controls all post to `/review/action` and differ only by
        an `action` key inside `hx-vals`. Without reading it, six buttons that
        do six different things look like one endpoint with six labels.
        """
        import json

        payload = values.get("hx-vals", "")
        if not payload.strip().startswith("{"):
            return ""
        try:
            return str(json.loads(payload).get("action", ""))
        except (ValueError, AttributeError):
            return ""

    def _build(self, tag: str, values: dict[str, str], text: str) -> Control:
        method, target = "", ""
        for attribute, verb in _TARGETS:
            if values.get(attribute):
                method, target = verb, values[attribute]
                break
        submits = values.get("type", "submit" if tag == "button" else "") == "submit"
        if not target and tag in ("button", "input") and submits:
            #  A submit button inherits its destination: the form named by
            #  `form=`, or the innermost one it sits inside. A `type="button"`
            #  inside a form submits nothing — reading the form's action off it
            #  made the settings save bar look like two ways to save.
            named = values.get("form", "")
            action = (
                self.form_ids.get(named, "")
                if named
                else (self.forms[-1].get("action", "") if self.forms else "")
            )
            enclosing = self.forms[-1] if self.forms and not named else {}
            if action or enclosing:
                verb = (enclosing.get("method") or "post").upper()
                method, target = (verb if action else "POST"), action
        return Control(
            page=self.page,
            tag=tag,
            label=text,
            method=method,
            target=target,
            name=text or values.get("aria-label", "") or values.get("title", ""),
            popover=values.get("popovertarget", ""),
            form=values.get("form", ""),
            value=values.get("value", "") or self._hx_action(values),
            attrs=values,
        )


def controls(page: str, html: str) -> list[Control]:
    """Every control on one rendered page."""
    reader = _Reader(page)
    reader.feed(html)
    return reader.found


def inventory(client, surfaces: tuple[str, ...] = SURFACES) -> list[Control]:
    """Every control on every populated surface, in one list."""
    found: list[Control] = []
    for url in surfaces:
        response = client.get(url)
        if response.status_code == 200:
            found.extend(controls(url, response.text))
    return found

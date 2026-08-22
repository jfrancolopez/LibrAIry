"""Every control on every populated page, checked against the router and itself.

`test_ui_consistency.py` reads templates. It cannot see a label that is a Jinja
expression, and it cannot see a control that only exists inside an `{% if %}`
no page satisfies — which is most of the ones that had drifted. So this reads
the *rendered* fixture instead, and asks the four questions a template cannot
answer:

  * does this control point at a route that exists
  * does it have a name a screen reader can read
  * does this word mean one thing everywhere
  * does this action have one word everywhere

The last two are the ones that found things. Navigating to Commit had four
labels, one of which was `Commit` — the same word the Commit page puts on the
button that actually commits. The optimization card on Commit posted to the
handler the optimization page calls `Cancel request` while saying `Send back`,
under a comment claiming they were the same act in the same words.

The exception tables below are the whole design. Every entry is a place where
two wordings are deliberate, with the reason written down; anything not listed
is a drift the next person has to either fix or justify here.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from librairy.scanner import scan_root  # noqa: E402
from tests.dev.controls import SURFACES, inventory  # noqa: E402
from tests.dev.fixture import build_fixture  # noqa: E402


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory):
    return build_fixture(tmp_path_factory.mktemp("controls"))


@pytest.fixture(scope="module")
def controls(client):
    found = inventory(client)
    assert found, "the fixture rendered no controls at all"
    return found


def routes(client) -> list:
    """The router's own patterns, which is what "this URL exists" means."""
    return list(client.app.routes)


def resolves(client, method: str, target: str) -> str:
    """The route template a control's URL lands on, or "" for no route.

    Returning the *template* rather than a yes/no is what folds `/history/undo/3`
    and `/history/undo/12` into one endpoint, so a list page with fifty rows on
    it does not read as fifty different buttons.
    """
    path = target.split("?", 1)[0].split("#", 1)[0]
    for route in routes(client):
        regex = getattr(route, "path_regex", None)
        if regex is None or not regex.match(path):
            continue
        methods = getattr(route, "methods", None) or {"GET"}
        if method in methods or (method == "GET" and "GET" in methods):
            return f"{method} {getattr(route, 'path', path)}"
    return ""


def local(control) -> bool:
    """A control that changes the page it is on and asks the server nothing."""
    return not control.target


def external(control) -> bool:
    return control.target.startswith(("http://", "https://", "mailto:"))


# --- the router ---------------------------------------------------------------


def test_every_control_points_at_a_route_that_exists(client, controls) -> None:
    """A 404 behind a button is a dead control, and pressing it is the only way
    a person finds out."""
    dead = [
        f"{control.page}: {control.label!r} -> {control.method} {control.target}"
        for control in controls
        if not local(control)
        and not external(control)
        and not resolves(client, control.method, control.target)
    ]

    assert dead == []


def test_every_populated_surface_has_controls(client) -> None:
    """A surface that renders nothing pressable is a surface no interaction
    test can cover, which is how four pages went a whole pass unexercised."""
    empty = [
        url
        for url in SURFACES
        if client.get(url).status_code == 200 and not inventory(client, (url,))
    ]

    assert empty == []


# --- accessible names ---------------------------------------------------------


def test_every_control_has_an_accessible_name(controls) -> None:
    """Text, `aria-label` or `title`. A button announced as "button" is a
    button nobody using a screen reader can choose."""
    nameless = [
        f"{control.page}: <{control.tag} class={control.attrs.get('class', '')!r}>"
        for control in controls
        if not control.name.strip()
    ]

    assert nameless == []


# --- one word, one action -----------------------------------------------------

#  Where the same word deliberately does different things. Each of these is a
#  page-local control whose subject is the page it is on, which is the only
#  case where reusing a word does not create ambiguity.
LABEL_MAY_REPEAT = {
    #  The search box on each page searches that page's world. Browse walks the
    #  filesystem; History searches the journal. Naming them differently would
    #  make one box on one page look like a different feature.
    "Search",
    #  Both mean "into the folder you empty yourself" — one for a file already
    #  held, one for an inbox file on its way there.
    "Delete queue",
    #  Withdrawing a decision that has not run. Deliberately the same word for
    #  a quarantine request and an adopted optimization: it is the same act.
    "Cancel request",
    #  One row or a selection of them. The bulk toolbar and the row control are
    #  the same decision at two scales, and the page says which is which by
    #  where the button is, not by calling one of them something else.
    "Analyse again",
    "Send back to Review",
    #  Asking whether a configured service answers. The subject is the card the
    #  button sits in, and every card names itself in its own heading.
    "Save",
    "Test",
    #  Sending a file to quarantine, and refusing a suggestion about one. Two
    #  workflows reach each of these — an inbox proposal in Review and a
    #  duplicate staged on the Quarantine page — and they are the same act, so
    #  they get the same word rather than a second one invented per page.
    "Quarantine",
    "Dismiss suggestion",
    #  "Do not get rid of any of these." Reached from a comparison between two
    #  filed representations and from one between a filed copy and an arriving
    #  one — different subjects, identical promise, so the same word rather
    #  than a second one invented for the second surface.
    "Keep all of them",
}


def test_one_label_means_one_action(client, controls) -> None:
    """Two buttons with the same word and different behaviour.

    The rule that produced `Delete queue` in the first place: `Mark for
    deletion` sat on two buttons, one of which moved a file immediately and one
    of which waited for Commit.
    """
    seen = collections.defaultdict(set)
    for control in controls:
        if local(control) or external(control) or control.label in LABEL_MAY_REPEAT:
            continue
        endpoint = resolves(client, control.method, control.target)
        seen[control.label].add(f"{endpoint} {control.value}".strip())

    ambiguous = {label: sorted(uses) for label, uses in seen.items() if len(uses) > 1}

    assert ambiguous == {}


# --- one action, one word -----------------------------------------------------

#  Where one endpoint deliberately carries more than one label. Two kinds
#  qualify and nothing else: a label that names its own subject, and a control
#  whose meaning genuinely changes with where it is drawn.
ACTION_MAY_HAVE_SEVERAL_LABELS = {
    #  Named for the subject, not the action: a per-provider or per-catalog
    #  control says which one it is about.
    "POST /settings/keys/{slug}",
    "POST /settings/catalogs/{slug}/test",
    "POST /settings/catalogs/{slug}/toggle",
    "POST /health/providers/{name}",
    "POST /settings/providers/ollama/{name}/remove",
    "POST /settings/providers/ollama/{name}/toggle",
    "POST /settings/providers/order/{kind}/{direction}",
    #  Named for the subject, again: each button names the folder it would put
    #  the artist in, which is the entire content of the decision. `Use this
    #  one` would be one label and would say nothing.
    "POST /review/audit/{finding_id}/destination",
    #  The same, one track at a time, plus `Leave here` — which is a different
    #  answer to the same question rather than a different question, and posts
    #  to the same route because "where does this go" is what it answers.
    "POST /review/audit/{finding_id}/file-track",
    #  Two kinds at once, both legitimate. `Keep 01 - Death on Two Legs.flac`
    #  names the representation it keeps, which is the whole decision. And
    #  `Keep all of them` is a different answer to the same question, posted to
    #  the same route because keeping everything is still an answer — it just
    #  happens to be the one with no filesystem work in it.
    "POST /review/audit/{finding_id}/comparison",
    #  Each button names the version it would make active, which is the whole
    #  content of the decision — and the two are a FLAC and an MP3 of one
    #  recording, so `Use this one` would be one label saying nothing.
    "POST /review/audit/{finding_id}/make-active",
    #  `Restore` on its own means bring this back; on a row that also offers
    #  `Use this instead`, the two answers end differently — both
    #  representations active, or exactly one — so the first one says which it
    #  is. Same request either way: the file comes out of Quarantine.
    "POST /quarantine/restore/{entry_id}",
    #  The link is the plan's own identifier.
    "GET /history/plans/{plan_id}",
    #  Scope is the difference and the label is where it is said: auditing one
    #  folder and auditing the library are not the same request to make by
    #  accident.
    "POST /browse/audit",
    #  Filter links and navigation both arrive at Review. "Clear" empties the
    #  filters you are looking at; "Go to Review" is a signpost from elsewhere.
    "GET /review",
    "GET /browse",
    "GET /history",
    #  Already in Review, the word is `Remove approval` — there is nowhere to
    #  send it back *to*. From Commit it is `Send back to Review`, which names
    #  where the row will reappear.
    "POST /review/audit/{finding_id}/unapprove",
    #  One row says `Approve`; the bulk control says how many and above what
    #  confidence, because approving forty files at once is a different thing
    #  to be sure about than approving the one you are looking at.
    "POST /review/action approve",
}


def test_one_action_has_one_label(client, controls) -> None:
    """Two words for one thing.

    Navigating to Commit had four: `Commit 2`, `Commit them`, `Back to Commit`
    and `View in Commit`. The first is the word the Commit page uses for
    actually committing.
    """
    seen = collections.defaultdict(set)
    for control in controls:
        if local(control) or external(control) or not control.label:
            continue
        endpoint = resolves(client, control.method, control.target)
        action = f"{endpoint} {control.value}".strip()
        #  Either form may be listed: some endpoints are one act, and some
        #  carry the act in a form value.
        if endpoint in ACTION_MAY_HAVE_SEVERAL_LABELS:
            continue
        if action in ACTION_MAY_HAVE_SEVERAL_LABELS:
            continue
        seen[action].add(control.label)

    drifted = {action: sorted(labels) for action, labels in seen.items() if len(labels) > 1}

    assert drifted == {}


# --- the shared popover -------------------------------------------------------


def test_every_popover_trigger_has_exactly_one_panel(client) -> None:
    """A duplicate id means one panel and several buttons that open it, which
    on a list page opens the wrong file's explanation."""
    from tests.dev.controls import controls as read

    wrong = []
    for url in SURFACES:
        body = client.get(url)
        if body.status_code != 200:
            continue
        #  One response, read twice. The panel ids come from a counter that
        #  advances per render, so fetching the page a second time to look for
        #  the id found on the first reports every panel missing.
        for control in read(url, body.text):
            if not control.popover:
                continue
            found = body.text.count(f'id="{control.popover}"')
            if found != 1:
                wrong.append(f"{url}: {control.popover} has {found} panels")

    assert wrong == []


# --- a control needs the code that answers it ---------------------------------


def test_every_preview_toggle_has_its_script_on_the_page(client) -> None:
    """`Preview` is markup plus a listener, and Quarantine had only the markup.

    Five buttons, `aria-expanded="false"`, no handler bound, nothing fetched —
    pressing one did nothing at all. Every DOM assertion in the suite passed,
    including the one asserting Preview is a toggle wherever it is offered,
    because the markup was never what was wrong.

    Asserted over the *rendered page* rather than the template, because the
    toggle lives in partials and the script tag lives in the page that includes
    them: neither file on its own can answer whether they met.
    """
    missing = []
    for url in SURFACES:
        body = client.get(url)
        if body.status_code != 200 or "data-preview-toggle" not in body.text:
            continue
        if "/static/previews.js" not in body.text:
            missing.append(url)

    assert missing == [], "these pages draw a Preview button nothing listens to"


def test_every_lightbox_control_has_its_viewer_on_the_page(client) -> None:
    """The same trap one level down: the preview card carries an expand
    control, and the viewer it opens is a separate include."""
    missing = [
        url
        for url in SURFACES
        if client.get(url).status_code == 200
        and "data-lightbox" in client.get(url).text
        and "/static/lightbox.js" not in client.get(url).text
    ]

    assert missing == []


# --- the words that are banned outright ---------------------------------------

#  `test_ui_consistency.py` holds this over templates. It escaped into Python:
#  the Review undo bar said "Marked for deletion 3 files", and the vanished
#  list labelled a destination with it, from strings no template contains.
BANNED = ("mark for deletion", "marked for deletion", "your call")

USER_FACING = (
    "src/librairy/web",
    "src/librairy/review_undo.py",
    "src/librairy/quarantine.py",
    "src/librairy/quarantine_requests.py",
    "src/librairy/optimization_queue.py",
    "src/librairy/optimization_disposal.py",
)


def strings_in(path: Path) -> list[str]:
    """Every string literal in one module, with comments and docstrings out.

    The prose in this repository discusses the banned phrase at length and is
    allowed to. A string a person can be shown is not.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node not in docstrings
    ]


def python_sources() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    found: list[Path] = []
    for entry in USER_FACING:
        path = root / entry
        found.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return found


@pytest.mark.parametrize("path", python_sources(), ids=lambda p: p.name)
def test_no_banned_wording_reaches_a_person_from_python(path: Path) -> None:
    offenders = [
        f"{path.name}: {text!r}"
        for text in strings_in(path)
        for word in BANNED
        if word in text.lower()
    ]

    assert offenders == []


# --- the scenes the interaction tests stand on --------------------------------


def test_staging_an_inbox_leaves_every_other_decision_alone(tmp_path) -> None:
    """`ui_serve --inbox 95` reached for every row in the inbox.

    That included the approved file waiting for Commit, and staging tried to
    move it back to `proposed` — which the lifecycle refuses on purpose,
    because re-staging an answer the owner already gave is how a late duplicate
    could quietly overwrite it. The dev server raised on startup.

    Every lifecycle state an inbox row can be in, and what staging must do to
    each: add to the ones it owns, and pass over the rest.
    """
    from librairy.lifecycle import transition_item
    from tests.dev.fixture import build_app, stage_inbox

    app = build_app(tmp_path / "staged")
    conn, settings = app.state.conn, app.state.settings

    #  A file in every state a real inbox accumulates, none of them staged by
    #  this helper and none of them its business.
    #  Each with the legal route to it — `discovered -> postponed` is not one,
    #  which is itself the lifecycle refusing to let a decision be invented.
    decided = (
        ("kept.jpeg", ("pending",)),
        ("later.jpeg", ("proposed", "postponed")),
        ("filed.jpeg", ("committed",)),
        ("held.jpeg", ("quarantined",)),
    )
    untouchable = {}
    for name, _state in decided:
        path = settings.inbox_dir / "already-decided" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    for name, route in decided:
        row = conn.execute(
            "SELECT id FROM items WHERE relpath=?", (f"already-decided/{name}",)
        ).fetchone()
        for step in route:
            transition_item(conn, int(row["id"]), step)
        untouchable[name] = route[-1]

    stage_inbox(conn, settings, 5)
    once = _inbox_states(conn)
    stage_inbox(conn, settings, 5)
    twice = _inbox_states(conn)

    for name, state in untouchable.items():
        assert once[f"already-decided/{name}"] == state, f"{name} was rewritten"
    #  The approved file waiting for Commit and the duplicate staged for
    #  quarantine are the fixture's own, and equally none of staging's business.
    assert once["2026-08-18/IMG_5150.jpeg"] == "approved"
    assert once["2026-08-19/foo-again.jpg"] == "quarantine-proposed"
    assert sum(1 for path in once if path.startswith("2026-05-")) == 5
    assert once == twice, "running it twice changed something"


def _inbox_states(conn) -> dict:  # noqa: ANN001
    return {
        row["relpath"]: row["state"]
        for row in conn.execute("SELECT relpath, state FROM items WHERE root='inbox'")
    }


def test_the_fixture_carries_one_of_every_decision_kind(client) -> None:
    """Coverage disappears by omission, silently, and stays gone.

    Search, History, Quarantine and Commit each went a whole pass with their
    shared controls unexercised, because the fixture had no rows on them — and
    three controls on the staged-quarantine card had never been seen by any
    inventory at all, because nothing here ever produced a staged proposal.

    So the scenes are asserted rather than assumed. If a decision type stops
    being represented, this fails on the pass that removed it instead of on the
    pass that needed it.
    """
    from librairy.web.commit_queue import TYPE_ORDER, queue_summary

    summary = queue_summary(client.app.state.conn)
    present = {
        group["type"] for group in summary["all_groups"] if group["decisions"]
    }

    assert present == set(TYPE_ORDER), f"no fixture decision of kind {set(TYPE_ORDER) - present}"


def test_the_fixture_fills_every_quarantine_view_that_matters(client) -> None:
    """Held, Waiting for Commit and Delete queue each need a row, and one of
    those rows has to be a preserved optimization original — the shape that
    renders differently from every other file in quarantine."""
    from librairy.web.quarantine import _counts

    counts = _counts(client.app.state.conn)

    for view in ("held", "waiting", "delete-queue"):
        assert counts.get(view), f"no quarantine row in the {view} view"
    assert counts.get("preserved:delete-queue"), "no preserved original to look at"


def test_the_fixture_journal_covers_what_history_can_show(client) -> None:
    """Every History filter needs something in it, and the refusal that the
    page renders differently needs a row too."""
    from librairy.history import HISTORY_KINDS, kind_counts

    counts = kind_counts(client.app.state.conn)

    for kind in ("filed", "quarantined", "failed"):
        assert counts.get(kind), f"the journal has nothing under {kind}"
    assert counts["all"] == sum(
        counts[key] for key in HISTORY_KINDS if key != "all"
    )


# --- keyboard, and the honest limit of what is proven -------------------------


def test_every_control_is_a_natively_focusable_element(client) -> None:
    """The strongest keyboard claim this project can actually make.

    Delivering a real key press needs a debugger protocol, not a URL: a headless
    Chrome run can `focus()` a button and observe that it is focused, and has no
    channel to press Enter on it. Measured — the probe reports
    `{"focused": "pv", "expanded": "false"}` — so automated keyboard activation
    stays **unverified**, and this file will not pretend otherwise.

    What is checkable is the property everything else rests on. Enter and Space
    activate a `<button>`, Escape dismisses a `popover`, and Tab reaches both,
    because the platform does it. That is only true while every control *is* one
    of those elements — a `<div onclick>` looks identical and is reachable by
    nothing but a mouse.
    """
    focusable = {"button", "a", "summary", "input", "select", "textarea"}
    unreachable = [
        f"{control.page}: <{control.tag} class={control.attrs.get('class', '')!r}>"
        for control in inventory(client)
        if control.tag not in focusable and "tabindex" not in control.attrs
    ]

    assert unreachable == []


def test_the_shared_popover_is_the_platform_s_and_not_a_reimplementation(client) -> None:
    """Keyboard behaviour that is inherited cannot be got wrong by accident.

    `popovertarget` on a native button gives activation, light dismissal and
    Escape from the user agent. A scripted panel would have to implement three
    behaviours that nothing here tests.
    """
    body = client.get("/review").text

    assert 'class="ext-info-toggle"' in body
    assert "popovertarget=" in body
    assert 'popover class="ext-info-panel"' in body or "popover " in body

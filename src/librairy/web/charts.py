"""Drawing a history without inventing any of it.

The whole difficulty of the Dashboard's bottom band is one rule, and it is a
rule about honesty rather than about pixels:

    a day with no row was **not observed**
    it was not zero

LibrAIry did not exist last March, so the library did not contain 0 files last
March — nobody knows what it contained. A line drawn from the origin up to the
first real reading is the prettiest thing this page could do and the least
true, and once drawn it is indistinguishable from a measurement.

So a gap is a gap. A line stops at the last day it was measured and starts
again at the next one; a bar chart draws bars only where a day was actually
computed; and every chart says how much of its window it is actually made of.

`metrics` upholds the other half of this: a row exists if and only if the value
was observed, noughts included. Without that invariant nothing here could tell
"nothing happened" from "nobody looked", and both would have to be drawn the
same way.

## Why there is no charting library

Because the page polls every five seconds, the repository has no frontend build
step, and a sparkline is thirty numbers and a `<polyline>`. What a library
would add here is a bundle, a theme to keep in sync with the application's own,
and a second place for a colour to be decided. The geometry is arithmetic and
it belongs in Python where it can be tested; the template draws what it is
given and decides nothing.

Charts stretch with `preserveAspectRatio="none"` so they fill whatever width
they are in — with `vector-effect="non-scaling-stroke"`, which is what keeps a
line one pixel thick instead of stretching into a wedge.

## Trends say the span they actually measured

"+312 files this month" over a history that begins eight days ago is a lie
about the month. Every trend here reports the real distance between its first
and last observation, and refuses to produce a percentage against a starting
value of nought — where the honest statement is the absolute change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from librairy import metrics
from librairy.humanize import human_bytes

LINE = "line"
BARS = "bars"

#  The drawing box. Small numbers, stretched by CSS to whatever the column is —
#  so the geometry never has to know how wide the page is, and a phone and a
#  desktop draw the same shape.
WIDTH = 100.0
HEIGHT = 30.0

#  A trend needs two observations far enough apart to be a trend. One day
#  either side of a weekend is a fluctuation, and calling it a direction of
#  travel would be reading tea leaves at people.
MIN_SPAN_DAYS = 2


#  Room kept at the top and bottom of the box. Without it a line at its own
#  maximum is drawn on the very edge and loses half its stroke to the clip, and
#  a bar at full height touches the title above it.
PAD = 2.0

#  What a recorded nought looks like. A bar of no height is indistinguishable
#  from a day nobody measured, and those are the two things this whole module
#  exists to keep apart — so an observed zero draws a baseline tick.
ZERO_TICK = 0.6


@dataclass(frozen=True)
class Point:
    day: str
    value: int
    label: str
    x: float
    y: float
    #  For a bar: how tall it is. Never quite nought when the day was measured.
    height: float = 0.0


@dataclass(frozen=True)
class Trend:
    """How much it moved, over the span actually measured."""

    delta: int
    label: str
    direction: str
    span: int
    percent: int | None = None


@dataclass(frozen=True)
class Chart:
    key: str
    title: str
    #  What this chart is for. Shown, not just documented: a chart nobody can
    #  name a question for is decoration, and this is where that shows.
    question: str
    kind: str
    points: tuple[Point, ...] = ()
    segments: tuple[tuple[Point, ...], ...] = ()
    high: int = 0
    high_label: str = ""
    latest: Point | None = None
    trend: Trend | None = None
    total: str = ""
    observed: int = 0
    window: int = 0
    href: str = ""
    #  How many days of the window have no reading. Said on the chart rather
    #  than hidden, because a sparse chart that looks dense is worse than one
    #  that admits what it is.
    missing: int = 0

    @property
    def empty(self) -> bool:
        return not self.points

    @property
    def partial(self) -> bool:
        return bool(self.missing) and not self.empty

    @property
    def bar_width(self) -> float:
        return max(WIDTH / max(self.window, 1) * 0.7, 0.4)

    @property
    def summary(self) -> str:
        """The whole chart in one sentence, for a screen reader and a tooltip."""
        if self.empty:
            return f"{self.title}: nothing recorded yet"
        parts = [f"{self.title}:"]
        if self.kind == BARS and self.total:
            parts.append(f"{self.total} over {self.observed} recorded days")
        elif self.latest is not None:
            parts.append(f"{self.latest.label} on {self.latest.day}")
        if self.trend is not None:
            parts.append(
                self.trend.label
                if self.trend.direction == "flat"
                else f"{self.trend.label} over {self.trend.span} days"
            )
        if self.missing:
            parts.append(f"{self.missing} days not recorded")
        return " ".join(parts)


@dataclass(frozen=True)
class Slice:
    """One category, and how much of the library it is."""

    folder: str
    files: int
    bytes: int
    percent: float
    label: str
    href: str
    rest: bool = False


@dataclass(frozen=True)
class History:
    days: int
    charts: tuple[Chart, ...] = ()
    categories: tuple[Slice, ...] = ()
    category_day: str = ""
    #  True before anything has ever been measured. The page says so in a
    #  sentence rather than drawing six empty boxes.
    unmeasured: bool = True
    first_day: str = ""
    ranges: tuple[dict[str, object], ...] = field(default_factory=tuple)


def window(days: int) -> list[str]:
    """The days a chart covers, oldest first. Always full, gaps included."""
    parts = [int(part) for part in metrics.today().split("-")]
    end = date(*parts)
    return [(end - timedelta(days=back)).isoformat() for back in range(days - 1, -1, -1)]


def chart(
    key: str,
    title: str,
    question: str,
    recorded: list[dict[str, object]],
    days: int,
    *,
    kind: str = LINE,
    unit: str = "files",
    href: str = "",
) -> Chart:
    """Turn one metric's observations into something a template can draw.

    `recorded` holds only the days that were measured. Everything here is built
    against the *full* window, so a missing day keeps its place on the axis and
    leaves a hole in the line rather than being quietly closed up — closing it
    up is how a fortnight of downtime turns into a smooth curve.
    """
    axis = window(days)
    position = {day: index for index, day in enumerate(axis)}
    seen = {
        str(entry["day"]): int(entry["value"])
        for entry in recorded
        if str(entry["day"]) in position
    }
    if not seen:
        return Chart(key, title, question, kind, window=days, missing=days, href=href)

    high = max(seen.values())
    #  From nought, always. A library chart whose axis starts at its own
    #  minimum turns a 2% change into a cliff, which is the most common way a
    #  truthful number is drawn into a lie.
    span = max(high, 1)
    inner = HEIGHT - PAD * 2
    step = WIDTH / max(len(axis) - 1, 1)

    points: list[Point] = []
    segments: list[list[Point]] = []
    run: list[Point] = []
    for index, day in enumerate(axis):
        if day not in seen:
            #  The gap. Close the run rather than bridging it.
            if run:
                segments.append(run)
                run = []
            continue
        value = seen[day]
        y = round(PAD + inner - value / span * inner, 3)
        point = Point(
            day=day,
            value=value,
            label=_label(value, unit),
            x=round(index * step, 3),
            y=y,
            height=round(max(HEIGHT - PAD - y, ZERO_TICK), 3),
        )
        points.append(point)
        run.append(point)
    if run:
        segments.append(run)

    observed = len(points)
    return Chart(
        key=key,
        title=title,
        question=question,
        kind=kind,
        points=tuple(points),
        segments=tuple(tuple(part) for part in segments),
        high=high,
        high_label=_label(high, unit),
        latest=points[-1],
        trend=_trend(points, unit) if kind == LINE else None,
        total=_label(sum(point.value for point in points), unit) if kind == BARS else "",
        observed=observed,
        window=days,
        href=href,
        missing=days - observed,
    )


def _trend(points: list[Point], unit: str) -> Trend | None:
    """The move between the first and last readings, over their real distance.

    Never over the *requested* window: "+312 files this month" on a history
    that starts eight days ago is a lie about the month, and it is the exact
    shape of lie this whole module exists to avoid.
    """
    if len(points) < 2:  # noqa: PLR2004
        return None
    first, last = points[0], points[-1]
    span = (date.fromisoformat(last.day) - date.fromisoformat(first.day)).days
    if span < MIN_SPAN_DAYS:
        return None
    delta = last.value - first.value
    #  No percentage against nought. "Up 100%" from an empty library is
    #  arithmetic rather than information, and the absolute number is the
    #  honest sentence.
    percent = round(delta / first.value * 100) if first.value else None
    sign = "+" if delta > 0 else ""
    return Trend(
        delta=delta,
        label="no change" if delta == 0 else f"{sign}{_label(delta, unit)}",
        direction="up" if delta > 0 else ("down" if delta < 0 else "flat"),
        span=span,
        percent=percent,
    )


def _label(value: int, unit: str) -> str:
    if unit == "bytes":
        return human_bytes(abs(value)) if value >= 0 else f"-{human_bytes(abs(value))}"
    return f"{value:,}"


#  Which charts the Dashboard draws, and the question each is there to answer.
#  Fewer than the table stores, deliberately: M3-02's rule is that a chart has
#  to change a decision, and "we recorded it" is not a reason to draw it.
PANELS = (
    ("library.files", "Library", "how fast is it growing", LINE, "files", "/browse"),
    ("library.bytes", "Storage", "how fast is it filling", LINE, "bytes", "/browse"),
    ("review.waiting", "Decisions waiting", "is the backlog shrinking", LINE, "files", "/review"),
    ("filed.files", "Filed", "how much am I actually getting through", BARS, "files", "/history"),
    (
        "setaside.duplicates",
        "Duplicates cleared",
        "am I making progress on them",
        BARS,
        "files",
        "/quarantine",
    ),
)

#  The windows offered. Four, and no date picker: this is a Dashboard, and the
#  question it answers is "how is this going", not "what happened on the 14th".
RANGES = ((7, "7 days"), (30, "30 days"), (90, "90 days"), (365, "1 year"))
DEFAULT_RANGE = 30

#  How many categories are drawn before the rest are gathered up. Eight bands
#  can be told apart; thirty cannot, and a legend nobody can read is a chart
#  that has stopped being one.
TOP_CATEGORIES = 8


def history(conn: sqlite3.Connection, days: int = DEFAULT_RANGE) -> History:
    """The bottom band: how the library is changing.

    One read of the metrics table for every chart on the page, bounded by the
    window and never by the library. Nothing here touches `items`.
    """
    days = _clamp(days)
    names = [name for name, *_ in PANELS]
    recorded = metrics.series(conn, names, days)
    charts = tuple(
        chart(
            name,
            title,
            question,
            recorded.get(name, []),
            days,
            kind=kind,
            unit=unit,
            href=href,
        )
        for name, title, question, kind, unit, href in PANELS
    )
    categories = _categories(conn)
    return History(
        days=days,
        charts=charts,
        categories=categories,
        #  The day the shape was measured, said on the panel: a distribution is
        #  a snapshot, and one taken three days ago should say so rather than
        #  reading as the library right now.
        category_day=_latest_day(conn) if categories else "",
        unmeasured=all(part.empty for part in charts) and not categories,
        first_day=metrics.first_recorded_day(conn),
        ranges=tuple(
            {"days": size, "label": label, "current": size == days}
            for size, label in RANGES
        ),
    )


def _categories(conn: sqlite3.Connection) -> tuple[Slice, ...]:
    """The library's shape, from the most recent day it was measured.

    Files and bytes together, because they answer different questions: a
    thousand documents and forty films are not comparable by count, and the
    forty films are most of the disk.
    """
    files = {entry["folder"]: int(entry["value"]) for entry in metrics.distribution(conn)}
    sized = {
        entry["folder"]: int(entry["value"])
        for entry in metrics.distribution(conn, field="bytes")
    }
    if not files:
        return ()
    total = sum(files.values()) or 1
    ordered = sorted(files.items(), key=lambda pair: (-pair[1], pair[0]))
    shown = ordered[:TOP_CATEGORIES]
    rest = ordered[TOP_CATEGORIES:]
    slices = [
        Slice(
            folder=str(folder),
            files=count,
            bytes=sized.get(folder, 0),
            percent=round(count / total * 100, 1),
            label=human_bytes(sized.get(folder, 0)),
            href=f"/browse/{folder}",
        )
        for folder, count in shown
    ]
    if rest:
        #  Everything else, as one band with its own count. Not dropped: a
        #  distribution whose parts do not add up to the library is a chart
        #  that has quietly stopped describing it.
        others = sum(count for _, count in rest)
        slices.append(
            Slice(
                folder=f"{len(rest)} more",
                files=others,
                bytes=sum(sized.get(folder, 0) for folder, _ in rest),
                percent=round(others / total * 100, 1),
                label=human_bytes(sum(sized.get(folder, 0) for folder, _ in rest)),
                href="/browse",
                rest=True,
            )
        )
    return tuple(slices)


def _latest_day(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(day) FROM metrics_daily").fetchone()
    return str(row[0] or "") if row is not None else ""


def _clamp(days: int) -> int:
    """A window from the query string, held to the ones actually offered."""
    sizes = [size for size, _ in RANGES]
    return days if days in sizes else DEFAULT_RANGE

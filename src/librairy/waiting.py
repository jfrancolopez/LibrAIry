"""Files held because there was nothing left worth asking.

Analysis has always had a floor: when the filename, the tags, the catalogs and
the readers between them could not reach `confidence_threshold`, the classifier
asked the configured AI provider. When *that* could not answer either — the
provider is switched off, the server is not running, the model name is wrong —
`ai/orchestrator.py` logged

    AI providers unavailable or below threshold; continuing with deterministic
    results

and the run continued with the guess. The guess became a proposal, and a
proposal is how LibrAIry says *I have an opinion about this file*. Nothing on
the row distinguished "three catalogs agree" from "the year in the filename was
the only thing anybody had", and the person reviewing it had no way to know
that the machine had not, in fact, finished.

**So it stops instead.** The file is held, in its own lifecycle state, and it
says why. Nothing is guessed, nothing is proposed, nothing is lost, and nothing
in the inbox is blocked behind it — the rest of the batch is classified exactly
as it always was.

## Three reasons, and they are genuinely different

    provider-unavailable   nothing could be asked. No provider is switched on,
                           or none of them answered the door.
    provider-failed        a provider was reached and the attempt broke: it
                           errored, refused the request, or its reply could not
                           be read.
    more-evidence          everything that could be asked was asked, and
                           answered, and the answers are still not enough.

The first two are about the machinery and resolve themselves: when a provider
answers again, the files resume automatically. The third is not about the
machinery at all. Retrying it changes nothing, because nothing was broken — so
it does not resume on its own, and it says so rather than sitting in a queue
that will never move for it.

Telling the first from the second needs the providers to be honest about
which happened, which is why `ProviderUnreachable` exists: LM Studio and Ollama
both used to swallow a refused connection into the same `None` they return for
"I have nothing to say", and a file could not be told the difference either.

## What this is not

**Not a job queue.** There is no payload, no ordering, no lease, no worker id
and no retry schedule. The work to be done is already fully described by the
item; the state machine that owns it is `items.state`; and the thing that does
the work is the analysis pass that was going to run anyway. This table only
records *why* a file is in the state it is in, and what its owner has said
about it since.

**Not a place things get stuck.** Every held file is listed in Review with its
reason, and three things can move it at any moment: the provider coming back,
the owner saying "propose from what you have", or the owner re-analysing it.
Nothing here ages out, nothing is discarded, and holding a file twice is one
row — `since` survives every re-hold, so an outage lasting eleven worker cycles
leaves eleven attempts against one date, not eleven records.

**Not a decision.** Holding changes no file, writes no proposal, and cannot
reach Commit. See `docs/ROADMAP.md` M2-01.
"""

from __future__ import annotations

import sqlite3

from librairy.humanize import human_ago
from librairy.lifecycle import transition_item
from librairy.live import live
from librairy.planner import utc_now

UNAVAILABLE = "provider-unavailable"
FAILED = "provider-failed"
EVIDENCE = "more-evidence"

#  Ordered by what the person can do about it, most actionable first.
REASONS = (UNAVAILABLE, FAILED, EVIDENCE)

REASON_LABEL = {
    UNAVAILABLE: "Waiting for AI",
    FAILED: "AI processing failed",
    EVIDENCE: "Needs more evidence",
}

REASON_NOTE = {
    UNAVAILABLE: "Nothing could be asked — no AI provider is switched on, or "
    "none of them answered. These resume by themselves when one does.",
    FAILED: "A provider was reached and the attempt broke. These resume by "
    "themselves once it answers properly again.",
    EVIDENCE: "Everything that could be asked was asked and answered, and it "
    "is still not enough. Nothing is wrong, so nothing will change on its own.",
}

#  The two a provider coming back actually fixes. `more-evidence` is not one of
#  them on purpose: nothing about the provider was wrong, so waiting for it to
#  recover would be waiting for an event that already happened.
RESUMABLE = (UNAVAILABLE, FAILED)

#  How many held files are released back into analysis on one idle cycle.
#
#  Bounded because the recovery case is the big one: a provider that was off
#  overnight comes back to twenty thousand held files, and releasing all of
#  them at once turns one worker cycle into a twenty-thousand-file analysis run
#  holding the write lock. They come back a page at a time and the queue drains
#  over a few minutes, which is what it would have done had the provider never
#  gone away.
RESUME_BATCH = 200

#  How many held files one page of the Review section lists.
PAGE_SIZE = 25

#  Named examples per reason in the Health concern and the section summary.
SHOWN = 3


def reason_for(attempt) -> str:  # noqa: ANN001 - an `ai.orchestrator.AIAttempt`
    """Why this file could not be answered, from what the attempt managed.

    A ladder, most specific first, and each rung is a different sentence to the
    person reading it:

    * something answered, and it still is not enough — the provider is fine and
      retrying it changes nothing, so this is the reason that does not resume.
    * something was reached and broke — an HTTP refusal, an unreadable reply.
      That is a configuration to fix, not an outage to wait out, and it says so.
    * nothing answered the door, or there was nothing to ask.

    Deliberately not a scoring function. Two providers where one is offline and
    the other refuses the request is reported as a failure, because the failure
    is the one somebody can do something about.
    """
    if getattr(attempt, "answered", ()):
        return EVIDENCE
    if getattr(attempt, "broken", ()):
        return FAILED
    return UNAVAILABLE


def detail_for(attempt) -> str:  # noqa: ANN001 - an `ai.orchestrator.AIAttempt`
    """The one line a held file shows under its reason.

    Names providers, because "AI is unavailable" on a machine with three of
    them configured does not say which one to go and look at.
    """
    reason = reason_for(attempt)
    if reason == EVIDENCE:
        return f"{_names(attempt.answered)} answered, and it was not enough."
    if reason == FAILED:
        return f"{_names(attempt.broken)} was reached and the attempt failed."
    if getattr(attempt, "unreachable", ()):
        return f"{_names(attempt.unreachable)} did not answer."
    return "No AI provider is switched on."


def _names(providers: tuple[str, ...]) -> str:
    listed = list(providers)
    if len(listed) <= 2:  # noqa: PLR2004 - "a and b" reads; "a, b and c" is a list
        return " and ".join(listed) or "the provider"
    return f"{', '.join(listed[:-1])} and {listed[-1]}"


def hold(
    conn: sqlite3.Connection, item_id: int, reason: str, detail: str = ""
) -> None:
    """Hold one file, or record another attempt against one already held.

    Idempotent by construction: the item id is the primary key, so a provider
    that is down for a week produces one row per file however many times the
    analysis pass reaches them. `since` is never overwritten — it is when this
    file stopped being answerable, and that is not a fact that a retry changes.
    """
    now = utc_now()
    conn.execute(
        """
        INSERT INTO processing_waits(
          item_id, reason, detail, attempts, paused, released_at, since, updated_at
        ) VALUES (?, ?, ?, 1, 0, NULL, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
          reason=excluded.reason,
          detail=excluded.detail,
          attempts=processing_waits.attempts + 1,
          released_at=NULL,
          updated_at=excluded.updated_at
        """,
        (item_id, reason, detail, now, now),
    )
    transition_item(conn, item_id, "waiting")


def clear(conn: sqlite3.Connection, item_ids: list[int]) -> None:
    """Forget the holds on files that have since been answered.

    Called with everything the analysis pass proposed, held or not, so that a
    file which comes back on its own — new bytes, a catalog key added, the
    provider returning — leaves nothing behind to be counted.
    """
    if not item_ids:
        return
    placeholders = ",".join("?" for _ in item_ids)
    conn.execute(
        f"DELETE FROM processing_waits WHERE item_id IN ({placeholders})",  # noqa: S608
        item_ids,
    )


def released(conn: sqlite3.Connection, item_ids: list[int]) -> set[int]:
    """Of these, the ones whose owner said "propose from what you have".

    Read once per analysis batch rather than per file. It is the one thing the
    classifier needs to know about a hold, and asking the table per item would
    put a query per file back into the pass this whole feature is meant to keep
    cheap.
    """
    if not item_ids:
        return set()
    placeholders = ",".join("?" for _ in item_ids)
    return {
        int(row["item_id"])
        for row in conn.execute(
            "SELECT item_id FROM processing_waits "  # noqa: S608
            f"WHERE released_at IS NOT NULL AND item_id IN ({placeholders})",
            item_ids,
        )
    }


#  Every count and every listing joins the item and asks it what state it is
#  in. The row is the *explanation*; `items.state` is the fact. They can only
#  disagree in one direction — a scan that re-hashes a changed file sets it back
#  to 'discovered' without knowing this table exists — and this way that stale
#  row is simply invisible until the next analysis pass deletes it.
_HELD = f"JOIN items i ON i.id = w.item_id AND i.state = 'waiting' AND {live()}"


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many files are held, by reason. One aggregate, never a row per file."""
    rows = conn.execute(
        f"SELECT w.reason, COUNT(*) AS held FROM processing_waits w {_HELD} "  # noqa: S608
        "GROUP BY w.reason"
    )
    return {str(row["reason"]): int(row["held"]) for row in rows}


def total(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM processing_waits w {_HELD}"  # noqa: S608
        ).fetchone()[0]
    )


def paused_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM processing_waits w {_HELD} WHERE w.paused = 1"  # noqa: S608
        ).fetchone()[0]
    )


def page(conn: sqlite3.Connection, page_number: int = 1) -> list[dict[str, object]]:
    """One bounded page of held files, oldest first.

    Oldest first because the list is a queue of things somebody may want to
    answer by hand, and the ones that have been waiting longest are the ones a
    provider recovery is least likely to still be about.
    """
    offset = max(0, (max(1, page_number) - 1) * PAGE_SIZE)
    rows = conn.execute(
        f"""
        SELECT w.item_id, w.reason, w.detail, w.attempts, w.paused, w.since,
               i.relpath, i.size
        FROM processing_waits w {_HELD}
        ORDER BY w.since, w.item_id
        LIMIT ? OFFSET ?
        """,  # noqa: S608 - the join is a module constant
        (PAGE_SIZE, offset),
    )
    return [_view(row) for row in rows]


def examples(conn: sqlite3.Connection, reason: str, limit: int = SHOWN) -> list[str]:
    """A few names under one reason, so a count is recognisable."""
    return [
        str(row["relpath"])
        for row in conn.execute(
            f"SELECT i.relpath FROM processing_waits w {_HELD} "  # noqa: S608
            "WHERE w.reason = ? ORDER BY w.since, w.item_id LIMIT ?",
            (reason, limit),
        )
    ]


def _view(row: sqlite3.Row) -> dict[str, object]:
    name = str(row["relpath"]).rsplit("/", 1)[-1]
    return {
        "item_id": int(row["item_id"]),
        "name": name,
        "relpath": str(row["relpath"]),
        "reason": str(row["reason"]),
        "reason_label": REASON_LABEL.get(str(row["reason"]), str(row["reason"])),
        "detail": str(row["detail"] or ""),
        "attempts": int(row["attempts"]),
        "paused": bool(row["paused"]),
        "since": str(row["since"]),
        #  "4 hours ago", not an ISO timestamp. How long a file has been held is
        #  the fact somebody reads this column for, and a timestamp answers it
        #  only after the reader does the arithmetic.
        "waited": human_ago(str(row["since"])),
        "resumes": str(row["reason"]) in RESUMABLE and not row["paused"],
    }


#  "A provider has answered since this file was held."
#
#  `last_ok_at` is written by a successful health probe and by a provider
#  actually answering a classification, so it is the one timestamp that means
#  *this thing is working now*. Comparing it against the hold's own
#  `updated_at` is what stops a release loop: a provider that has been healthy
#  since before the hold is not news, and re-releasing against it would hold
#  the same files again on the next cycle, forever.
#
#  Strictly greater, and `utc_now()` has no sub-second part — so a file held in
#  the same second a provider recovered waits for the next probe rather than
#  being released now. That is the safe direction and it is bounded: the worker
#  probes every `AI_PROBE_SECONDS` for as long as anything is waiting on one,
#  and the first probe after the hold moves the timestamp past it. A minute
#  late is a cost worth paying to make "already healthy" impossible to mistake
#  for "just came back".
_RECOVERED = """
  EXISTS (SELECT 1 FROM provider_status s
           WHERE s.enabled = 1 AND s.last_ok_at IS NOT NULL
             AND s.last_ok_at > w.updated_at)
"""

_RESUMABLE_REASONS = ",".join("?" for _ in RESUMABLE)


def awaiting_provider(conn: sqlite3.Connection) -> int:
    """Held files that a provider coming back would actually release.

    What decides whether the worker bothers probing at all. No held file that a
    probe could help means no probe, which is the difference between an idle
    installation that is quiet and one that talks to a dead endpoint forever.
    """
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM processing_waits w {_HELD}
            WHERE w.paused = 0 AND w.released_at IS NULL
              AND w.reason IN ({_RESUMABLE_REASONS})
            """,  # noqa: S608 - placeholders only
            RESUMABLE,
        ).fetchone()[0]
    )


def resume_recovered(conn: sqlite3.Connection, limit: int = RESUME_BATCH) -> int:
    """Put back into the queue every held file whose provider has answered again.

    Bounded, and safe to run on every cycle: releasing a file only sets it back
    to 'discovered', so a crash between this and the analysis pass costs one
    re-classification and nothing else. Nothing is deleted here — the hold row
    survives until the file actually gets a proposal, which is what keeps a
    resume that fails from losing the reason it was waiting for.
    """
    rows = conn.execute(
        f"""
        SELECT w.item_id FROM processing_waits w {_HELD}
        WHERE w.paused = 0 AND w.released_at IS NULL
          AND w.reason IN ({_RESUMABLE_REASONS})
          AND {_RECOVERED}
        ORDER BY w.since, w.item_id
        LIMIT ?
        """,  # noqa: S608 - placeholders only
        (*RESUMABLE, limit),
    ).fetchall()
    for row in rows:
        transition_item(conn, int(row["item_id"]), "discovered")
    return len(rows)


def _selected(conn: sqlite3.Connection, item_ids: list[int]) -> list[int]:
    """Of the ids asked for, the ones that really are held right now.

    Every owner action goes through this. A form posted twice, or posted after
    the provider came back and answered the file, must not resurrect a hold or
    raise a lifecycle error — it must simply find nothing to do.
    """
    if not item_ids:
        return []
    placeholders = ",".join("?" for _ in item_ids)
    return [
        int(row["item_id"])
        for row in conn.execute(
            f"SELECT w.item_id FROM processing_waits w {_HELD} "  # noqa: S608
            f"WHERE w.item_id IN ({placeholders})",
            item_ids,
        )
    ]


def set_paused(conn: sqlite3.Connection, item_ids: list[int], paused: bool) -> int:
    """Stop, or restart, automatic resuming for these files.

    Pausing changes nothing about the file and nothing about what it is waiting
    for. It says: leave this one alone when the provider comes back, I am going
    to deal with it myself.

    `updated_at` is deliberately left alone. It means *when this hold's reason
    was last established*, which is what the recovery condition compares
    against — and moving it here would mean un-pausing a file threw away a
    recovery that happened while it was paused, so the person who paused it,
    watched the provider come back and un-paused it would see nothing happen.
    """
    held = _selected(conn, item_ids)
    if not held:
        return 0
    placeholders = ",".join("?" for _ in held)
    conn.execute(
        f"UPDATE processing_waits SET paused=? WHERE item_id IN ({placeholders})",  # noqa: S608
        (int(paused), *held),
    )
    return len(held)


def release(conn: sqlite3.Connection, item_ids: list[int]) -> int:
    """"Propose from what you already know" — the owner's answer, not a guess.

    The distinction is the whole point of the feature. LibrAIry refuses to
    publish a weak opinion on its own; a person may still ask for it, having
    been told exactly how weak it is. The marker is durable rather than a
    single release into the queue, because the analysis pass would otherwise
    reach the same conclusion it reached last time and hold the file again.
    """
    held = _selected(conn, item_ids)
    if not held:
        return 0
    placeholders = ",".join("?" for _ in held)
    now = utc_now()
    conn.execute(
        f"UPDATE processing_waits SET released_at=?, paused=0, updated_at=? "  # noqa: S608
        f"WHERE item_id IN ({placeholders})",
        (now, now, *held),
    )
    for item_id in held:
        transition_item(conn, item_id, "discovered")
    return len(held)


def held_ids(conn: sqlite3.Connection, reason: str = "") -> list[int]:
    """Every held item, or every one under one reason. Bounded by the caller.

    Used by the "all of them" form of each action, which is the only way a
    person with four thousand held files gets to act on them at all.
    """
    where = " WHERE w.reason = ?" if reason else ""
    params = (reason,) if reason else ()
    return [
        int(row["item_id"])
        for row in conn.execute(
            f"SELECT w.item_id FROM processing_waits w {_HELD}{where}",  # noqa: S608
            params,
        )
    ]


def providers(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """What the enabled providers last did, for the section that waits on them.

    Read from `provider_status`, which is written by health probes and by
    providers actually answering. Read-only, and deliberately: asking the
    registry for the chain would mirror every configured provider back into
    that table as a side effect of drawing a page, which is the shape that
    turns a page view colliding with the worker's write lock into a fault on
    whatever you happened to be reading.

    "Waiting for AI" without saying *which* AI, or whether it is up, sends
    somebody to the Health page to find out something this paragraph could have
    told them.
    """
    return [
        {
            "name": str(row["name"]),
            "answering": bool(row["last_ok_at"]) and not row["last_error"],
            "last_ok": human_ago(row["last_ok_at"]) if row["last_ok_at"] else "",
            "error": str(row["last_error"] or ""),
        }
        for row in conn.execute(
            "SELECT name, last_ok_at, last_error FROM provider_status "
            "WHERE enabled = 1 ORDER BY name"
        )
    ]


def summary(conn: sqlite3.Connection, page_number: int = 1) -> dict[str, object]:
    """Everything Review's section draws, in four bounded queries.

    Counts first and rows second, because the counts are what the page promises
    and the rows are one page of evidence for them. A held backlog of forty
    thousand renders exactly the same amount of HTML as one of four.
    """
    by_reason = counts(conn)
    held = sum(by_reason.values())
    rows = page(conn, page_number) if held else []
    return {
        "waiting_total": held,
        "waiting_counts": by_reason,
        "waiting_reasons": [
            {
                "reason": reason,
                "count": by_reason.get(reason, 0),
                "label": REASON_LABEL[reason],
                "note": REASON_NOTE[reason],
                "resumes": reason in RESUMABLE,
            }
            for reason in REASONS
            if by_reason.get(reason)
        ],
        "waiting_rows": rows,
        "waiting_page": max(1, page_number),
        "waiting_page_size": PAGE_SIZE,
        "waiting_pages": max(1, -(-held // PAGE_SIZE)),
        "waiting_paused": paused_count(conn) if held else 0,
        "waiting_providers": providers(conn) if held else [],
    }

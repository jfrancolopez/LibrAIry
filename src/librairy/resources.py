"""How hard LibrAIry is allowed to work, on two axes and in two words.

The only resource control in the program lived inside Storage Optimization:
`ResourcePolicy`, one measured value called `Low`, applied to exactly one
workload. Everything else — scanning, hashing, analysis, content extraction,
similarity, the library audit, and every AI provider call — ran flat out on
every cycle, and there was no way to tell LibrAIry to be quiet while somebody
was watching a film off the same NAS.

Two settings, because there are two genuinely different questions:

    Overall processing   Quiet / Balanced / Full Power
    Local AI             Off / Limited / Normal / Full Power

**Separate on purpose.** A local model on a machine with a GPU is cheap and the
inbox scan is not; a model on the same two cores as everything else is the most
expensive thing in the program. Which of those is true is a fact about somebody's
hardware, and no single slider can express it.

## What a mode is allowed to be

**A cap, never an override.** `batch_size` is a number somebody typed; a mode
that replaced it would silently argue with them. Quiet *lowers* a ceiling and
Full Power *removes a pause* — neither raises a limit past what the settings
already say. The one exception is stated where it happens, and it is the
encoder, whose pool size is not a setting anybody can reach.

**Automatic work only.** A mode describes what the worker does on its own. It
does not reach a button somebody pressed: testing a provider, asking every
provider about one file, re-analysing a file by hand. Those are the person
asking for the work, which is not the situation this module is about.

**Never a change of behaviour, only of rate.** No mode makes a decision
differently, skips a safety check, or files anything. Quiet is slower and Full
Power is faster, and every file ends up in the same place with the same
evidence. The one thing a mode may do is decline to *start* something
expensive, and the only two of those are a library audit and a transcode —
both already defined as work that waits for a quiet moment.

**Balanced and Normal are today, exactly.** Both are the defaults, and both
reproduce the previous behaviour value for value. An upgrade that quietly
changed how much of somebody's NAS LibrAIry uses would be the worst possible
version of this feature.

## What is deliberately not here

Fifteen worker tuning knobs. The per-workload numbers below are derived from
the two words, and the two words are what Settings shows. Anything finer is a
support burden in exchange for a decision nobody has the measurements to make.

The worker's priority tiering. `worker.run_once` already runs the inbox first,
the library audit on a cycle that changed nothing, and an optimization only
after that — and that ordering is a correctness property, not a resource one.

Suspending a running encode. That trade is refused in `worker.run_once` with
its reasoning, and a resource mode is not a reason to reopen it: stopping and
restarting an encoder buys half-written output and orphaned processes.

See `docs/ROADMAP.md` M2-03.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

PROCESSING_SETTING = "resources.processing"
AI_SETTING = "resources.ai"

QUIET = "quiet"
BALANCED = "balanced"
FULL = "full"

AI_OFF = "off"
AI_LIMITED = "limited"
AI_NORMAL = "normal"
AI_FULL = "full"


@dataclass(frozen=True)
class EncoderPolicy:
    """How hard the encoder is allowed to work.

    Was `optimization_exec.ResourcePolicy`, and is unchanged in every field —
    it moved here so that the one measured resource control in the program is
    in the module about resource controls, rather than the module about
    building an FFmpeg command line.

    `pools` is the number that actually bounds consumption. `-threads` is
    FFmpeg's setting and **libx265 builds its own worker pool** and ignores it;
    measured in the production image, CPU seconds per wall-clock second track
    the pool size and not the machine:

        pools=1:frame-threads=1   1.05x        pools=4:frame-threads=4   4.02x
        pools=2:frame-threads=2   2.09x        pools=8:frame-threads=8   6.22x

    `nice` and `ionice` are applied when the runtime provides them and skipped
    silently when it does not. They lower priority under contention, which is a
    weaker guarantee than bounding the pool, and neither is load-bearing.

    `scripts/measure_encoder_load.py` reproduces the table.
    """

    name: str = "low"
    label: str = "Low"
    pools: int = 2
    frame_threads: int = 2
    threads: int = 2
    nice: int = 19
    ionice_class: int = 3  # idle

    @property
    def x265_params(self) -> str:
        return f"pools={self.pools}:frame-threads={self.frame_threads}"


LOW = EncoderPolicy()

#  The other end of the same measured table, and the one place a mode is
#  allowed to *raise* a number rather than cap one — because the pool size is
#  not a setting anybody can reach, so there is nothing here to argue with.
#
#  Capped by what the machine has. The `Low` figure is deliberately absolute —
#  two cores' worth on a machine with sixty — and this one deliberately is not:
#  "Full Power" is a statement about *this* machine, and eight pools on a
#  two-core NAS is not more speed, it is more contention.
def _full_encoder() -> EncoderPolicy:
    cores = max(1, min(8, os.cpu_count() or 2))
    return EncoderPolicy(
        name="full",
        label="Full Power",
        pools=cores,
        frame_threads=cores,
        threads=cores,
        #  Still niced. Full Power means LibrAIry may use the machine, not that
        #  it may make the machine unusable — and `nice` costs nothing when
        #  nothing else wants the CPU, which is the case Full Power is for.
        nice=10,
        ionice_class=2,  # best-effort
    )


@dataclass(frozen=True)
class ProcessingMode:
    """Everything the worker asks about "how hard, overall".

    One object rather than a scatter of settings lookups, so a new workload
    reads its limit from the same place as every other one and a mode cannot be
    half-applied.
    """

    name: str
    label: str
    note: str
    #  A ceiling on how many files one cycle analyses or extracts. `None` means
    #  "whatever `batch_size` says", which is what Balanced and Full Power both
    #  mean — the mode does not raise a number somebody typed.
    batch_cap: int | None
    #  A ceiling on how many library files one cycle hashes to resolve a
    #  size collision. Hashing is the most I/O-heavy thing the worker does on
    #  its own, and it is the one a person watching a film off the same disk
    #  actually notices.
    hash_cap: int | None
    idle_sleep: float
    busy_sleep: float
    max_sleep: float
    #  Whether an expensive *new* thing may be started. Neither stops anything
    #  already running: a library audit resumes from where it stopped, and an
    #  encode is never suspended — see the module docstring.
    audits: bool
    transcodes: bool
    #  Whether OCR may read pixels at all, and how many documents one cycle may
    #  read for. Deterministic work, so it is governed here and not by the AI
    #  axis — tesseract turns pixels into characters and makes no judgement,
    #  and switching Local AI off must not stop a scanner's output being
    #  readable. A document whose turn does not come this cycle is left alone
    #  and reached by the next one, so the answer is the same and only the
    #  timing moves. See `librairy/ocr.py`.
    ocr: bool
    ocr_per_cycle: int | None
    encoder: EncoderPolicy


PROCESSING_MODES = {
    QUIET: ProcessingMode(
        name=QUIET,
        label="Quiet",
        note="Small batches, long pauses, and nothing expensive started. Files "
        "still arrive and are still decided — it just takes longer, and the "
        "machine stays free for whatever else is using it.",
        batch_cap=10,
        hash_cap=25,
        idle_sleep=15.0,
        busy_sleep=5.0,
        max_sleep=120.0,
        audits=False,
        transcodes=False,
        #  Still allowed, and rationed. Refusing outright would make Quiet
        #  answer a scanned document differently rather than later, which is
        #  the one thing a mode may not do.
        ocr=True,
        ocr_per_cycle=2,
        encoder=LOW,
    ),
    BALANCED: ProcessingMode(
        name=BALANCED,
        label="Balanced",
        note="What LibrAIry has always done: work through the inbox promptly, "
        "and leave the expensive jobs for a moment when nothing is arriving.",
        batch_cap=None,
        hash_cap=None,
        idle_sleep=5.0,
        busy_sleep=0.5,
        max_sleep=60.0,
        audits=True,
        transcodes=True,
        ocr=True,
        ocr_per_cycle=10,
        encoder=LOW,
    ),
    FULL: ProcessingMode(
        name=FULL,
        label="Full Power",
        note="No pauses between cycles, and the encoder may use the machine. "
        "For catching up on a big import when nothing else needs the disk.",
        batch_cap=None,
        hash_cap=None,
        idle_sleep=2.0,
        busy_sleep=0.0,
        max_sleep=15.0,
        audits=True,
        transcodes=True,
        ocr=True,
        ocr_per_cycle=None,
        encoder=_full_encoder(),
    ),
}


@dataclass(frozen=True)
class AIMode:
    """Everything anything asks about "how hard the AI is allowed to work".

    Bounded independently of the processing mode, which is the whole reason
    there are two of these. A local model is either the cheapest thing in the
    program or the most expensive one, and only the person who owns the machine
    knows which.
    """

    name: str
    label: str
    note: str
    #  How many providers of the configured chain one file may be asked of.
    #  `0` is off — the chain is empty, nothing is asked, and analysis holds
    #  what it cannot answer rather than guessing. See `librairy/waiting.py`.
    #  `None` means "whatever the chain and `use_multi_ai` already decide".
    providers: int | None
    #  Ceilings on what each call may cost. `None` leaves the setting alone.
    timeout: int | None
    retries: int | None
    #  Whether a model may be asked to *look at* a picture. Vision is the
    #  single most expensive AI call LibrAIry makes and the one most worth
    #  dropping first, so Limited drops it and says so.
    vision: bool
    #  How often the worker checks whether a provider that was down is back.
    #  `0` never asks, which is the honest thing to do when AI is switched off:
    #  there is nothing to come back to.
    probe_seconds: int

    @property
    def off(self) -> bool:
        return self.providers == 0


AI_MODES = {
    AI_OFF: AIMode(
        name=AI_OFF,
        label="Off",
        note="No provider is asked anything. Everything deterministic still "
        "runs; files nothing else can identify wait for you rather than being "
        "guessed at.",
        providers=0,
        timeout=None,
        retries=None,
        vision=False,
        probe_seconds=0,
    ),
    AI_LIMITED: AIMode(
        name=AI_LIMITED,
        label="Limited",
        note="One provider per file, a short timeout, no retries, and no "
        "looking at pictures. For a model sharing the machine with everything "
        "else.",
        providers=1,
        timeout=30,
        retries=0,
        vision=False,
        probe_seconds=300,
    ),
    AI_NORMAL: AIMode(
        name=AI_NORMAL,
        label="Normal",
        note="What LibrAIry has always done: ask the providers you configured, "
        "in the order you put them in, with the timeout you set.",
        providers=None,
        timeout=None,
        retries=None,
        vision=True,
        probe_seconds=60,
    ),
    AI_FULL: AIMode(
        name=AI_FULL,
        label="Full Power",
        note="The same providers, checked for recovery more often. For a "
        "machine where the model has hardware of its own.",
        providers=None,
        timeout=None,
        retries=None,
        vision=True,
        probe_seconds=30,
    ),
}

#  The defaults, and they are the previous behaviour value for value. An
#  upgrade that quietly changed how much of somebody's NAS LibrAIry uses would
#  be the worst possible version of this feature.
DEFAULT_PROCESSING = BALANCED
DEFAULT_AI = AI_NORMAL


def processing_mode(conn: sqlite3.Connection | None) -> ProcessingMode:
    """How hard the worker may work. Falls back to Balanced, always.

    `None` is accepted because several callers are reached from code paths that
    genuinely have no database — a command builder under test, a settings
    object being validated — and "no database" has to mean "behave the way it
    always did" rather than raise.
    """
    return PROCESSING_MODES.get(_stored(conn, PROCESSING_SETTING), PROCESSING_MODES[BALANCED])


def ai_mode(conn: sqlite3.Connection | None) -> AIMode:
    """How hard the AI may work. Falls back to Normal, always."""
    return AI_MODES.get(_stored(conn, AI_SETTING), AI_MODES[DEFAULT_AI])


def set_processing_mode(conn: sqlite3.Connection, name: str) -> str:
    return _store(conn, PROCESSING_SETTING, name, PROCESSING_MODES, DEFAULT_PROCESSING)


def set_ai_mode(conn: sqlite3.Connection, name: str) -> str:
    return _store(conn, AI_SETTING, name, AI_MODES, DEFAULT_AI)


def _store(conn: sqlite3.Connection, key: str, name: str, known: dict, fallback: str) -> str:
    """Write one mode, or the default if the name is not one we have.

    An unknown value stores the default rather than raising or storing itself.
    A resource mode is read on every worker cycle and on every page that shows
    it, and a bad row in `settings` must not be able to make any of those fail.
    """
    chosen = name if name in known else fallback
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (key, json.dumps(chosen)),
    )
    return chosen


def _stored(conn: sqlite3.Connection | None, key: str) -> str:
    if conn is None:
        return ""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return ""
    try:
        return str(json.loads(row["value"]) or "")
    except (TypeError, ValueError):
        return ""


# --- what each workload actually reads ------------------------------------------


def batch_limit(mode: ProcessingMode, requested: int | None) -> int | None:
    """The smaller of what the mode allows and what the settings asked for.

    A cap and never a floor. Quiet takes ten files a cycle from somebody whose
    batch size is fifty; it does not take ten from somebody who asked for five.
    """
    if mode.batch_cap is None:
        return requested
    if requested is None:
        return mode.batch_cap
    return min(requested, mode.batch_cap)


def ai_timeout(mode: AIMode, requested: int) -> int:
    """The AI timeout for *automatic* work, capped by the mode.

    Not applied to a provider test or to "ask every provider about this file":
    those are somebody pressing a button and waiting for the answer, which is
    not what a resource mode is about. See the module docstring.
    """
    return requested if mode.timeout is None else min(requested, mode.timeout)


def ai_retries(mode: AIMode, requested: int) -> int:
    return requested if mode.retries is None else min(requested, mode.retries)


def ai_chain(mode: AIMode, chain: list):
    """The providers automatic analysis may ask, in order.

    Off is an empty list rather than a flag checked somewhere else: every
    caller already handles "no provider is configured", because that is the
    ordinary state of a fresh installation, and a second way of saying the same
    thing is a second thing to get wrong.
    """
    if mode.providers is None:
        return chain
    return chain[: mode.providers]


def modes_view(conn: sqlite3.Connection | None) -> dict[str, object]:
    """Both modes, for the pages that show them. Two reads, no writes."""
    processing = processing_mode(conn)
    ai = ai_mode(conn)
    return {
        "processing_mode": processing,
        "ai_mode": ai,
        "processing_modes": list(PROCESSING_MODES.values()),
        "ai_modes": list(AI_MODES.values()),
        #  True when either axis is somewhere other than its default, which is
        #  what decides whether the Dashboard says anything at all. A line
        #  reading "Balanced · Normal" on every installation that never touched
        #  the setting is furniture.
        "modes_changed": processing.name != DEFAULT_PROCESSING or ai.name != DEFAULT_AI,
    }

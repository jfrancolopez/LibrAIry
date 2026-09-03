"""What a document is called, when its sources do not agree.

A real file, found in real use:

    file            programming_rust_2e.pdf
    embedded title  CRACKING
    first page      Programming Rust
    ISBN catalog    Programming Rust, 2nd Edition

LibrAIry filed that as `CRACKING.pdf`. Not because of a bug, but because of a
rule that reads perfectly well until you meet this file: *the document's own
metadata outranks its filename*. It does — a PDF Info dictionary is something
written down rather than guessed at. What the rule never asked was whether
anything **else** was written down too, and whether the two agreed.

**So this compares rather than ranks.** Every source that has something to say
about the title says it, all of them are kept, and the question becomes what
they add up to:

    two independent sources agree     there is an answer, and it is preselected
    the sources disagree              there is a recommendation, and a question

The second is the case this module exists for. A disagreement is not weak
evidence — it is *contested* evidence, which is a different thing and deserves
a different treatment. Weak evidence gets held (`librairy/waiting.py`);
contested evidence gets shown, with everything that was compared, the
recommendation, and why.

## Why agreement and not authority

Authority alone re-creates the bug. The embedded title outranks the first page
under any ordering anybody would write down, and the embedded title is the one
that was wrong. What actually distinguishes the good answer here is that
*three* sources say a version of "Programming Rust" and one says something
else — and that is a fact about the set, not about any member of it.

Authority still decides **which wording wins** among the sources that agree: a
catalog that resolved an ISBN gives a better title than a running header, and
`Programming Rust, 2nd Edition` is a better filename than `Programming Rust`
because the edition is part of what the file is.

## What counts as agreement

Titles from different places are never byte-identical. A catalog says
`Programming Rust, 2nd Edition`, a running header says `Programming Rust`, and
a filename says `programming_rust_2e`. Comparing them means comparing what they
*name*, so each is reduced to a set of meaningful words — punctuation dropped,
edition and volume noise dropped, subtitles after a colon dropped — and two
titles agree when one names a subset of the other.

Deliberately generous in that direction and strict in the other. A title that
adds words is the same work described more fully; a title that shares no words
is a different claim, and that is the only thing this needs to detect.

## What is not here

**No scoring.** Every judgement is a set operation over words a person can read
on the row, because "why is this called that" has to be answerable without a
debugger. A number between 0 and 1 that folds four sources together is exactly
the opaque thing this replaces.

**No source is silently discarded.** A filename that is obviously a scanner's
serial number does not get a vote — but it is still shown, marked as what it
is, because "LibrAIry ignored the filename" is information too.

**Nothing here reads a file, runs a tool or asks a network.** It is handed what
the readers found. See `librairy/docmeta.py` for the reading, and
`docs/ROADMAP.md` M2-02.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#  Where a title came from, strongest first. The order decides which wording
#  wins among sources that agree — never whether they agree.
CATALOG = "catalog"
EMBEDDED = "embedded"
CONTENT = "content"
OCR = "ocr"
FILENAME = "filename"
MODEL = "ai"

ORDER = (CATALOG, EMBEDDED, CONTENT, OCR, FILENAME, MODEL)

SOURCE_LABEL = {
    CATALOG: "Catalog",
    EMBEDDED: "Embedded title",
    CONTENT: "First page",
    OCR: "Scanned text",
    FILENAME: "Filename",
    MODEL: "Local AI",
}

#  A model's opinion may agree with something and may never be the reason a
#  title is chosen. Level four of the authority order in
#  `docs/architecture/decision-memory.md`, and the same rule that keeps a
#  learned habit out of the settled tier.
NEVER_DECIDES = frozenset({MODEL})

#  Words that describe an edition rather than name a work. Dropped before
#  comparison so `programming_rust_2e`, `Programming Rust` and `Programming
#  Rust, 2nd Edition` are three spellings of one title rather than three
#  titles — and kept in the *wording* that gets used, because the edition is
#  part of what the file is.
_EDITION = re.compile(
    r"(?i)^(\d+(st|nd|rd|th)?e?|ed|edn|edition|revised|rev|updated|volume|vol|"
    r"part|pt|v\d+|second|third|fourth|fifth)$"
)

#  Words too common to be evidence of anything. A two-word title of which one
#  word is "the" is a one-word title for comparison purposes.
_NOISE = frozenset(
    {"a", "an", "and", "for", "in", "of", "on", "the", "to", "with", "your"}
)

#  A filename that is a scanner's counter, a camera's counter or a download's
#  hash names nothing. It still appears in the comparison — "the filename was
#  ignored" is worth saying — but it does not get a vote, because a name that
#  says nothing cannot disagree with anything.
#  Trailing digits, in any number of groups: a camera writes `IMG_20240612_0001`
#  and a scanner writes `scan 0473`, and both are the same convention.
_MEANINGLESS_NAME = re.compile(
    r"(?i)^(scan|img|image|doc|document|file|untitled|output|download|"
    r"attachment|dsc|pdf)([\s_-]*\d+)*$"
)

#  The same for an embedded title: a producer's default is not a claim about
#  the document, it is a claim about the software. Kept narrow on purpose —
#  `CRACKING` is *not* in here and must not be, because deciding a title looks
#  wrong is exactly the judgement this module refuses to make on its own.
_PRODUCER_DEFAULT = re.compile(
    r"(?i)^(untitled|document\d*|microsoft word\b.*|microsoft powerpoint\b.*|"
    r"pdfcreator|printout|slide \d+|.*\.(docx?|pptx?|indd|tex|pages)|"
    r"[0-9a-f]{8}-[0-9a-f-]{20,})$"
)


@dataclass(frozen=True)
class Candidate:
    """One source's answer to "what is this document called?".

    `title` is the wording that source used, verbatim, because the row shows
    it. `counted` is whether it gets a vote: a filename that is a scanner's
    counter is shown and not counted, which is a different thing from being
    left out.
    """

    source: str
    title: str
    counted: bool = True
    note: str = ""

    @property
    def label(self) -> str:
        return SOURCE_LABEL.get(self.source, self.source.title())

    @property
    def words(self) -> frozenset[str]:
        return meaningful_words(self.title)


@dataclass(frozen=True)
class Identity:
    """What the sources add up to, and what to do about it."""

    title: str
    source: str
    candidates: tuple[Candidate, ...] = ()
    #  Sources that name something the recommendation does not. Non-empty means
    #  a person is asked; it never means the recommendation is wrong.
    conflicts: tuple[str, ...] = ()
    #  Sources that name the same work as the recommendation. Two or more of
    #  these — the recommendation and one other — is what earns a preselection.
    agreements: tuple[str, ...] = ()

    @property
    def contested(self) -> bool:
        return bool(self.conflicts)

    @property
    def corroborated(self) -> bool:
        """Did anything independent say the same thing?

        The whole preselection rule. One source saying something is the
        ordinary case and is fine; two saying it is what makes the answer safe
        to fill in without asking.
        """
        return len(self.agreements) >= 1

    @property
    def why(self) -> str:
        """One sentence: what was chosen and what made it the choice."""
        if not self.title:
            return "Nothing named this document."
        chosen = SOURCE_LABEL.get(self.source, self.source)
        agreed = _spoken(_labels([self.source, *self.agreements]))
        disagree = _spoken(_labels(self.conflicts))
        many = len(self.conflicts) > 1
        if self.contested and self.agreements:
            return f"{agreed} name the same work; {disagree} {'do not' if many else 'does not'}."
        if self.contested:
            verb = "disagree" if many else "disagrees"
            return f"{chosen} is the strongest source here, and {disagree} {verb}."
        if self.agreements:
            return f"{agreed} agree."
        return f"{chosen} is the only source that named this document."

    def shown(self) -> tuple[dict[str, object], ...]:
        """The comparison, as the row prints it. Strongest source first."""
        conflicts = set(self.conflicts)
        return tuple(
            {
                "source": candidate.source,
                "label": candidate.label,
                "title": candidate.title,
                "conflict": candidate.source in conflicts,
                "chosen": candidate.source == self.source,
                "note": candidate.note,
            }
            for candidate in self.candidates
        )


def meaningful_words(title: str) -> frozenset[str]:
    """What a title actually names, as words.

    Punctuation goes, case goes, edition words go, and everything after a colon
    goes — a subtitle is a description of the work rather than its name, and
    two sources agreeing on the name while one of them carries the subtitle is
    agreement.
    """
    head = title.split(":", 1)[0] if ":" in title else title
    words = re.findall(r"[0-9a-z]+", head.lower())
    kept = [word for word in words if not _EDITION.match(word) and word not in _NOISE]
    return frozenset(kept)


def agree(left: str, right: str) -> bool:
    """Do these two titles name the same work?

    One naming a subset of the other. Generous about a title that says more —
    `Programming Rust, 2nd Edition` against `Programming Rust` — and strict
    about one that says something else.

    An empty comparison is not agreement. Two titles made entirely of noise
    words would otherwise agree with each other and with everything.
    """
    first, second = meaningful_words(left), meaningful_words(right)
    if not first or not second:
        return False
    return first <= second or second <= first


def meaningless_filename(stem: str) -> bool:
    """A scanner's counter, a camera's counter, a download's hash, an arXiv id.

    Two rules. The first is a short list of names that are conventions rather
    than titles. The second is the general one: **a name with no word in it is
    not a name.** `1706.03762v5` is a perfectly good identifier for a paper and
    says nothing about what the paper is called, and treating it as a title
    made the arXiv PDF in the fixture disagree with its own metadata.
    """
    cleaned = re.sub(r"[._-]+", " ", stem).strip()
    if not cleaned or _MEANINGLESS_NAME.match(cleaned):
        return True
    return not any(len(re.sub(r"[^a-z]", "", word)) >= 2 for word in cleaned.lower().split())  # noqa: PLR2004


def producer_default(title: str) -> bool:
    """A title the producing application wrote, not one anybody meant.

    Narrow on purpose. `CRACKING` is not here and must not be: deciding that a
    title *looks* wrong is exactly the judgement this module refuses to make on
    its own, and the whole point is that the comparison catches it instead.
    """
    return bool(_PRODUCER_DEFAULT.match(title.strip()))


def resolve(candidates: list[Candidate]) -> Identity:
    """Compare what every source said, and decide what to do about it.

    Three steps, in this order and for a reason:

    1. **Group by agreement.** Which sources name the same work is a fact about
       the set, and it is what authority alone cannot see — the embedded title
       outranks the first page under any ordering anybody would write down, and
       in the case this module was built for it is the one that is wrong.
    2. **Pick the group.** The largest group of agreeing sources wins; a tie
       goes to the group containing the strongest source. A model may join a
       group and may never be one on its own.
    3. **Pick the wording.** The strongest source *in the winning group*, so a
       resolved ISBN's `Programming Rust, 2nd Edition` beats a running header's
       `Programming Rust` — the edition is part of what the file is.
    """
    counted = [
        candidate
        for candidate in candidates
        if candidate.counted and candidate.title.strip()
    ]
    if not counted:
        return Identity("", "", tuple(_ordered(candidates)))

    groups = _grouped(counted)
    winner = max(groups, key=lambda group: (len(group), -_rank(group[0].source)))
    chosen = min(winner, key=lambda candidate: _rank(candidate.source))
    outside = [
        candidate.source
        for candidate in counted
        if candidate not in winner and candidate.source not in NEVER_DECIDES
    ]
    #  A filename that loses to two sources inside the document is not a
    #  disagreement worth anybody's afternoon — it is an old name, which is the
    #  ordinary state of a file somebody has renamed. It only counts as dissent
    #  when nothing inside the document corroborates the recommendation, which
    #  is the case `invoice-2019.pdf` with an embedded title of `CRACKING`
    #  actually is.
    corroborators = [
        candidate for candidate in winner if candidate.source not in (FILENAME, MODEL)
    ]
    if len(corroborators) >= 2:  # noqa: PLR2004
        outside = [source for source in outside if source != FILENAME]
    return Identity(
        title=chosen.title,
        source=chosen.source,
        candidates=tuple(_ordered(candidates)),
        conflicts=tuple(_sorted_sources(outside)),
        agreements=tuple(
            _sorted_sources(
                candidate.source for candidate in winner if candidate is not chosen
            )
        ),
    )


def _grouped(candidates: list[Candidate]) -> list[list[Candidate]]:
    """Sources that name the same work, gathered.

    Transitive by construction — a candidate joins the first group any member
    of which it agrees with — which is right for titles that differ by a
    subtitle or an edition and would be wrong for a similarity score. Agreement
    here is a subset relation, not a distance.
    """
    groups: list[list[Candidate]] = []
    for candidate in sorted(candidates, key=lambda item: _rank(item.source)):
        for group in groups:
            if any(agree(candidate.title, member.title) for member in group):
                group.append(candidate)
                break
        else:
            groups.append([candidate])
    return groups


def _rank(source: str) -> int:
    return ORDER.index(source) if source in ORDER else len(ORDER)


def _ordered(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda candidate: _rank(candidate.source))


def _sorted_sources(sources) -> list[str]:  # noqa: ANN001
    return sorted(dict.fromkeys(sources), key=_rank)


def _labels(sources) -> list[str]:  # noqa: ANN001
    return [SOURCE_LABEL.get(name, name) for name in sources]


def _spoken(names: list[str]) -> str:
    """"a", "a and b", "a, b and c" — a sentence, not a list."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"

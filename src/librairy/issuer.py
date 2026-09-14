"""Who issued this document, read from the document rather than from a list.

A year of bank statements is the example the roadmap has carried since M2-06,
and it did not work. `document_set` groups "the same kind of document from the
same organization", and nothing read an organization off a financial document:
the classifier extracted one for manuals only, out of the PDF's Author field.

What happened instead is worse than not grouping. The first line of a statement
is the bank's name, `docmeta` takes the first heading as the document's title,
and so twelve months of statements were all called `NORTHCREST BANK, N.A.pdf`
and all wanted the same path. The heading was read correctly and understood as
the wrong thing.

**No institution is named anywhere in this module, and that is the design.** A
list of banks is a list that is wrong for somebody on their first day — a credit
union, a utility, a landlord, a bank in a country nobody thought of. What is
general is how an organization writes its own name on its own paperwork:

    a legal form      `N.A.`, `Inc.`, `Ltd`, `PLC`, `LLC`, `GmbH`, `S.A.` —
                      and the institution words that do the same job in
                      English: Bank, Credit Union, Building Society, Mutual
    a domain          `www.northcrestbank.com` is the organization's own
                      identifier, printed by the organization
    the metadata      an Author field that is not the producing software

None of those knows what a bank is called. All of them know what a company
looks like when it writes to you.

**Two signals that agree are worth much more than either alone**, which is the
whole confidence model here: a name off a legal-form line, corroborated by a
domain that shares a word with it, is about as sure as this can get without
asking somebody. One signal on its own is a suggestion, and it is returned as
one — `librairy/classify/documents.py` decides what a suggestion earns, and
`confidence_threshold` decides whether it is enough to file on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#  How far in to look. An issuer identifies itself at the top of the first page,
#  on its own letterhead; a name found two thirds of the way down a statement is
#  as likely to be a payee as the sender.
HEAD_LINES = 14

#  Legal forms and the English institution words that serve the same purpose.
#  Suffix-anchored where the form is a suffix, so `Bank of the West` and
#  `Vellum Networks Ltd` both match and a sentence mentioning a bank does not.
_LEGAL_FORM = re.compile(
    r"(?i)(?:^|[\s,])("
    r"n\.?\s?a\.?|inc\.?|incorporated|corp\.?|corporation|co\.?|company|"
    r"ltd\.?|limited|llc|l\.l\.c\.|llp|plc|p\.l\.c\.|"
    r"gmbh|ag|a\.g\.|s\.a\.|s\.a\.s\.|s\.l\.|b\.v\.|n\.v\.|oy|ab|as|a/s|"
    r"pty|pte|sdn\.?\s?bhd|k\.k\.|kabushiki"
    r")\s*$"
)
_INSTITUTION = re.compile(
    r"(?i)\b(bank|bankers|credit union|building society|savings|mutual|"
    r"trust company|insurance|assurance|utilities|energy|water|telecom)\b"
)

#  A domain as an organization writes it on paper. Deliberately not a URL
#  parser: what appears on a statement is `www.example.com` or
#  `billing@example.co.uk`, not a link.
_DOMAIN = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9-]{1,62}"
    r"(?:\.[a-z0-9][a-z0-9-]{1,62})+)"
)

#  Second-level domains that are part of the suffix rather than the name, so
#  `hsbc.co.uk` gives `hsbc` and not `co`. The list is short because it only has
#  to cover the shapes that exist, not every registry on earth.
_PUBLIC_SECOND_LEVEL = {
    "co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "mil",
}

#  Domains nobody's letterhead is. A statement that mentions the software that
#  made it, or a generic mail provider, has not told us who sent it.
_NOT_AN_ISSUER_DOMAIN = {
    "adobe", "acrobat", "microsoft", "google", "gmail", "googlemail", "outlook",
    "hotmail", "yahoo", "icloud", "protonmail", "example", "localhost", "w3",
}

#  Words that carry no identity, so that "shares a word" means something. A
#  name and a domain both containing "the" have not corroborated each other.
_HOLLOW = {
    "the", "and", "of", "for", "a", "an", "group", "holdings", "services",
    "service", "international", "global", "national", "co", "com", "inc",
    "ltd", "limited", "llc", "plc", "corp", "company", "na", "sa", "gmbh",
}

#  What each combination of signals is worth. Two independent sources that
#  agree is the only one of these anywhere near certain; everything else is a
#  suggestion, and the caller is told which it has.
CORROBORATED = 0.9
NAMED = 0.72
METADATA_ONLY = 0.7
DOMAIN_ONLY = 0.5


@dataclass(frozen=True)
class Issuer:
    """An organization read off a document, and what said so.

    `sources` is the provenance, in the words a person reads on the evidence
    line: they are what makes this reviewable rather than a name that appeared.
    """

    name: str = ""
    confidence: float = 0.0
    sources: tuple[str, ...] = ()
    #  Two readable signals that named different organizations. Never resolved
    #  here by preferring one: a document whose letterhead and whose metadata
    #  disagree about who wrote it is a question, and
    #  `librairy/document_identity.py` already owns what a question does to a
    #  proposal's confidence.
    contested: bool = False

    def __bool__(self) -> bool:
        return bool(self.name)


def read(text: str, *, author: str = "") -> Issuer:
    """The organization that issued this document, or an empty `Issuer`.

    `text` is the front matter — the same text `docmeta` already extracted, so
    nothing here opens a file or runs a subprocess. `author` is the PDF's Author
    field when it is plausibly a name rather than the producing software; the
    caller decides that, because it already has the rule.
    """
    lines = _head(text)
    named = _from_lines(lines)
    domains = _domains(lines)
    metadata = " ".join(str(author or "").split())

    if named and any(_shares_a_word(named, domain) for domain in domains):
        return Issuer(named, CORROBORATED, ("its letterhead", "its web address"))
    if named and metadata and not _agree(named, metadata):
        #  Both sources read something, and they do not match. Reported rather
        #  than ranked: preferring the letterhead would be right most of the
        #  time, and "most of the time" is the standard that files somebody's
        #  statements under their accountant's name.
        return Issuer(named, NAMED, ("its letterhead", "the document's metadata"), True)
    if named:
        return Issuer(named, NAMED, ("its letterhead",))
    if metadata:
        return Issuer(metadata, METADATA_ONLY, ("the document's metadata",))
    if domains:
        #  A domain and nothing else. It is genuinely evidence — it is printed
        #  on the document by whoever sent it — and it is genuinely weak, since
        #  `northcrestbank.com` gives `Northcrestbank` rather than a name
        #  anybody writes. Returned below the threshold that files anything, so
        #  it informs a person and does not choose a folder.
        return Issuer(_titled(domains[0]), DOMAIN_ONLY, ("its web address",))
    return Issuer()


def _head(text: str) -> list[str]:
    lines = [" ".join(line.split()) for line in str(text or "").splitlines()]
    return [line for line in lines if line][:HEAD_LINES]


def _from_lines(lines: list[str]) -> str:
    """The first line that looks like an organization writing its own name."""
    for line in lines:
        candidate = _trimmed(line)
        if not candidate or len(candidate) > 80:
            continue
        if _LEGAL_FORM.search(candidate) or _INSTITUTION.search(candidate):
            return _tidy(candidate)
    return ""


def _trimmed(line: str) -> str:
    """The name part of a letterhead line, without the address after it.

    `Northcrest Bank, N.A. · PO Box 1184 · Springfield` is one line on a lot of
    letterheads. Splitting on the separators that are *always* punctuation
    between fields, never inside a name, leaves the half that is the name.
    """
    for separator in ("·", "|", "•", "—", " - "):
        if separator in line:
            line = line.split(separator, 1)[0]
    return line.strip(" \t-–—:")


def _tidy(name: str) -> str:
    """As the organization writes it, minus shouting.

    Letterheads are often set in capitals, and `NORTHCREST BANK` as a folder
    name is the document's typography rather than the organization's name. Only
    an all-capitals name is touched: mixed case is somebody's actual styling and
    `eBay` must survive it.
    """
    name = name.strip().strip(",")
    if name and name == name.upper():
        return " ".join(
            word if _keeps_its_capitals(word) else word.capitalize()
            for word in name.split()
        )
    return name


#  Legal forms that are written as initials and stay that way. Not "any short
#  word": `BANK` is four letters and is not an initialism, and the first version
#  of this produced `Northcrest BANK, N.A.` — half the line shouting.
_INITIALISMS = {
    "na", "llc", "llp", "plc", "ltd", "inc", "sa", "sas", "sl", "ag", "nv",
    "bv", "oy", "ab", "as", "kk", "pty", "pte", "bhd", "gmbh", "co",
}


def _keeps_its_capitals(word: str) -> bool:
    """A word that is initials rather than a word: `N.A.`, `LLC`, `S.A.`.

    Dotted is the general signal — nobody writes `B.A.N.K.` — and the legal
    forms above are the undotted ones that are still initials.
    """
    letters = word.replace(".", "").replace(",", "").lower()
    if not letters.isalpha():
        return True
    return "." in word or letters in _INITIALISMS


def _domains(lines: list[str]) -> list[str]:
    found: list[str] = []
    for line in lines:
        for match in _DOMAIN.finditer(line):
            label = _registrable(match.group(1))
            if label and label not in _NOT_AN_ISSUER_DOMAIN and label not in found:
                found.append(label)
    return found


def _registrable(domain: str) -> str:
    """The name part of a domain: `hsbc` from `hsbc.co.uk`."""
    parts = [part.lower() for part in domain.split(".") if part]
    if len(parts) < 2:
        return ""
    #  Walk back past the public suffix. `co.uk` is two labels of suffix;
    #  `com` is one.
    index = len(parts) - 2
    while index > 0 and parts[index] in _PUBLIC_SECOND_LEVEL:
        index -= 1
    return parts[index]


def _words(value: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if word not in _HOLLOW and len(word) > 2
    }


def _shares_a_word(name: str, domain: str) -> bool:
    """Does the domain contain a word from the name?

    Substring rather than set intersection, because a domain is written without
    spaces: `northcrestbank` contains `northcrest`, and no amount of tokenising
    the domain will produce that word on its own.
    """
    return any(word in domain for word in _words(name))


def names_the_same(left: str, right: str) -> bool:
    """Do these two strings name the same organization?

    Public because `docmeta` asks it about a heading — "is this line the
    issuer's name rather than the document's title" is the same question as
    "do these two readings agree", and two spellings of it would eventually
    answer differently.
    """
    return _agree(left, right)


def _agree(left: str, right: str) -> bool:
    """Do two readings name the same organization?

    A shared identifying word is enough. `Northcrest Bank, N.A.` and
    `Northcrest Bank` are the same organization written two ways, and requiring
    the strings to match would make every letterhead disagree with its own
    metadata.
    """
    shared = _words(left) & _words(right)
    return bool(shared)


def _titled(label: str) -> str:
    return label[:1].upper() + label[1:] if label else ""

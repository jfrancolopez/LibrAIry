"""One real request per catalog, so "is my key working?" has an answer.

Every catalog tool swallows its exceptions and returns None, which is the right
behaviour during analysis — a metadata source that is down must degrade to the
next evidence source, not stop the batch. It is the wrong behaviour when you
have just pasted a key and want to know whether it took, because a rejected key
and a genuine "never heard of that film" look identical from the outside.

So a probe does two things. It calls the real tool function with a query whose
answer is known, which proves the whole path including the parsing. Only if
that comes back empty does it repeat the request at the HTTP level, purely to
find out *why* — 401 is a bad key, 429 is a rate limit, a connection error is a
network or DNS problem, and 200 with no match means the service is fine and the
test query simply missed.
"""

from __future__ import annotations

import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from librairy.catalogs import CATALOGS_BY_SLUG
from librairy.config import Settings
from librairy.secrets_store import settings_with_stored_keys

TIMEOUT_SECONDS = 12
USER_AGENT = "LibrAIry/1.0 (+https://github.com/jfrancolopez/LibrAIry)"
AUDIO_SUFFIXES = (".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac")


class UnknownCatalog(ValueError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    slug: str
    ok: bool
    #  Four words at most — this is the badge.
    headline: str
    #  The answer it got, or the reason there wasn't one.
    detail: str
    #  What was asked, so the answer can be judged.
    query: str
    seconds: float = 0.0


@dataclass(frozen=True)
class _Probe:
    query: str
    ask: Callable[[], str]
    #  Used only when `ask` comes back empty, to tell a bad key from a miss.
    diagnose_url: Callable[[], str] | None = None
    diagnose_headers: dict[str, str] = field(default_factory=dict)


def probe_catalog(
    conn: sqlite3.Connection, settings: Settings, slug: str, *, opener=None
) -> ProbeResult:
    """Ask one catalog one question it should be able to answer."""
    info = CATALOGS_BY_SLUG.get(slug)
    if info is None:
        raise UnknownCatalog(f"unknown catalog: {slug}")
    resolved = settings_with_stored_keys(conn, settings)
    if not info.keyless and not _key(resolved, info.key_field):
        return ProbeResult(
            slug,
            False,
            "No key set",
            f"Paste a key above, or set {info.env_var} in the environment, then test again.",
            "",
        )
    probe = _build(slug, conn, resolved)
    if probe is None:
        return ProbeResult(slug, False, "Cannot test", "No test is defined for this catalog.", "")

    started = time.monotonic()
    try:
        answer = probe.ask()
    except _Untestable as exc:
        return ProbeResult(slug, False, "Nothing to test with", str(exc), probe.query)
    elapsed = time.monotonic() - started
    if answer:
        return ProbeResult(slug, True, "Working", answer, probe.query, elapsed)

    reason = _diagnose(probe, opener=opener)
    return ProbeResult(slug, reason.ok, reason.headline, reason.detail, probe.query, elapsed)


class _Untestable(RuntimeError):
    """The probe needs something this machine does not have (e.g. an audio file)."""


@dataclass(frozen=True)
class _Diagnosis:
    ok: bool
    headline: str
    detail: str


def _diagnose(probe: _Probe, *, opener=None) -> _Diagnosis:
    if probe.diagnose_url is None:
        return _Diagnosis(False, "No answer", "The catalog returned nothing for the test query.")
    request = urllib.request.Request(  # noqa: S310 - fixed https hosts from the registry
        probe.diagnose_url(),
        headers={"User-Agent": USER_AGENT, **probe.diagnose_headers},
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        return _from_status(exc.code, exc.read()[:200].decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - every failure here is a diagnosis
        return _Diagnosis(
            False,
            "Cannot reach it",
            f"{type(exc).__name__}: {exc}. Check the container's network and DNS.",
        )
    return _from_status(status, "")


def _from_status(status: int, body: str) -> _Diagnosis:
    if status in {401, 403}:
        return _Diagnosis(
            False,
            "Key rejected",
            f"The service answered HTTP {status}. The key is wrong, expired, or not yet "
            f"activated. {body}".strip(),
        )
    if status == 429:
        return _Diagnosis(
            False,
            "Rate limited",
            "HTTP 429 — too many requests just now. The key is probably fine; wait a minute "
            "and test again.",
        )
    if status >= 500:
        return _Diagnosis(
            False,
            "Service is down",
            f"HTTP {status} from the catalog itself. Nothing to fix on your side.",
        )
    if 200 <= status < 300:
        return _Diagnosis(
            True,
            "Reachable, no match",
            "The service answered normally but had nothing for the test query. The "
            "connection and the key are fine.",
        )
    return _Diagnosis(False, f"HTTP {status}", body or "Unexpected response from the catalog.")


def _key(settings: Settings, field_name: str) -> str:
    secret = getattr(settings, f"{field_name}_key", None)
    if secret is None:
        secret = getattr(settings, f"{field_name}_token", None)
    return secret.get_secret_value() if secret is not None else ""


def _build(slug: str, conn: sqlite3.Connection, settings: Settings) -> _Probe | None:
    builder = _BUILDERS.get(slug)
    return builder(conn, settings) if builder else None


# --- the probes -----------------------------------------------------------
# Each asks about something that has existed for decades and is not going to
# stop existing, so a failure means the catalog, not the question.


def _tmdb(_conn, settings: Settings) -> _Probe:
    from librairy.tools import tmdb

    key = _key(settings, "tmdb")

    def ask() -> str:
        tmdb.reset_cache()
        match = tmdb.search("The Matrix", api_key=key, year=1999)
        if not match:
            return ""
        return f"Found “{match.get('title')}” ({str(match.get('release_date'))[:4]})."

    return _Probe(
        "the film The Matrix (1999)",
        ask,
        lambda: (
            "https://api.themoviedb.org/3/search/movie?"
            + urlencode({"api_key": key, "query": "The Matrix"})
        ),
    )


def _tvmaze(_conn, _settings) -> _Probe:
    from librairy.tools import tvmaze

    def ask() -> str:
        tvmaze.reset_cache()
        match = tvmaze.search_show("Breaking Bad", season=1, episode=1)
        if not match:
            return ""
        episode = match.get("episode_name")
        found = f"Found “{match.get('name')}” ({str(match.get('first_air_date'))[:4]})"
        return f"{found}, and S01E01 is “{episode}”." if episode else f"{found}."

    return _Probe(
        "the show Breaking Bad, season 1 episode 1",
        ask,
        lambda: "https://api.tvmaze.com/search/shows?" + urlencode({"q": "Breaking Bad"}),
    )


def _musicbrainz(_conn, _settings) -> _Probe:
    from librairy.tools import musicbrainz

    def ask() -> str:
        musicbrainz.reset_cache()
        mbid = musicbrainz.search_release("Radiohead", "OK Computer")
        return f"Found the release, MusicBrainz id {mbid}." if mbid else ""

    return _Probe(
        "the album OK Computer by Radiohead",
        ask,
        lambda: (
            "https://musicbrainz.org/ws/2/release?"
            + urlencode({"query": 'artist:"Radiohead"', "fmt": "json", "limit": "1"})
        ),
    )


def _discogs(_conn, settings: Settings) -> _Probe:
    from librairy.tools import discogs

    token = _key(settings, "discogs")

    def ask() -> str:
        discogs.reset_cache()
        match = discogs.search_release("Radiohead - Karma Police")
        if not match:
            return ""
        genre = f", filed under {match['genre']}" if match.get("genre") else ""
        return f"Found “{match.get('album')}” by {match.get('artist')}{genre}."

    return _Probe(
        "the release Radiohead – Karma Police",
        ask,
        lambda: (
            "https://api.discogs.com/database/search?"
            + urlencode({"q": "Radiohead", "type": "release", "per_page": "1"})
        ),
        {"Authorization": f"Discogs token={token}"},
    )


def _lastfm(_conn, settings: Settings) -> _Probe:
    from librairy.tools import lastfm

    key = _key(settings, "lastfm")

    def ask() -> str:
        lastfm.reset_cache()
        genre = lastfm.top_genre("Radiohead", album="OK Computer")
        return f"Genre for OK Computer came back as “{genre}”." if genre else ""

    return _Probe(
        "the genre of OK Computer by Radiohead",
        ask,
        lambda: (
            "https://ws.audioscrobbler.com/2.0/?"
            + urlencode(
                {
                    "method": "artist.gettoptags",
                    "artist": "Radiohead",
                    "api_key": key,
                    "format": "json",
                }
            )
        ),
    )


def _openlibrary(_conn, _settings) -> _Probe:
    from librairy.tools import openlibrary

    def ask() -> str:
        openlibrary.reset_cache()
        match = openlibrary.search_book("Dune", "Frank Herbert")
        if not match:
            return ""
        year = f", first published {match.year}" if match.year else ""
        return f"Found “{match.title}” by {match.author or 'unknown'}{year}."

    return _Probe(
        "the book Dune by Frank Herbert",
        ask,
        lambda: "https://openlibrary.org/search.json?" + urlencode({"title": "Dune", "limit": "1"}),
    )


def _coverart(_conn, settings: Settings) -> _Probe:
    from librairy.tools import coverart, musicbrainz

    def ask() -> str:
        musicbrainz.reset_cache()
        mbid = musicbrainz.search_release("Radiohead", "OK Computer")
        if not mbid:
            raise _Untestable(
                "Cover Art Archive is looked up by MusicBrainz release id, and "
                "MusicBrainz did not answer. Test MusicBrainz first."
            )
        path = coverart.cover_path(settings.appdata_dir, mbid)
        if path is None:
            return ""
        return f"Downloaded the sleeve for OK Computer, {path.stat().st_size // 1024} KB."

    return _Probe("the album art for OK Computer", ask, lambda: "https://coverartarchive.org/")


def _acoustid(conn: sqlite3.Connection, settings: Settings) -> _Probe:
    """The only probe that needs one of your own files.

    AcoustID answers questions about audio fingerprints and nothing else, so
    there is no synthetic query to send it. A made-up fingerprint is rejected
    before the key is even looked at, which would prove nothing.
    """
    from librairy.tools import acoustid
    from librairy.tools.fpcalc import fingerprint

    key = _key(settings, "acoustid")
    track = _an_audio_file(conn, settings)

    def ask() -> str:
        if track is None:
            raise _Untestable(
                "AcoustID identifies music from its audio, so this test needs a real "
                "audio file. Drop one in the inbox, let it scan, and test again."
            )
        printed = fingerprint(track, settings)
        if not printed.ok or printed.data is None:
            raise _Untestable(f"fpcalc could not fingerprint {track.name}: {printed.error}")
        acoustid.reset_cache()
        match = acoustid.lookup(printed.data.fingerprint, int(printed.data.duration), api_key=key)
        if not match:
            return ""
        return (
            f"Fingerprinted {track.name} and AcoustID matched it at "
            f"{match['score']:.0%} confidence."
        )

    return _Probe(
        f"a fingerprint of {track.name}" if track else "one of your own audio files",
        ask,
        lambda: (
            "https://api.acoustid.org/v2/lookup?"
            + urlencode(
                {"client": key, "duration": "300", "fingerprint": "x", "meta": "recordings"}
            )
        ),
    )


def _an_audio_file(conn: sqlite3.Connection, settings: Settings) -> Path | None:
    """The smallest audio file on hand — smallest so fpcalc is quick."""
    rows = conn.execute(
        """
        SELECT root, relpath FROM items
        WHERE missing_since IS NULL AND root IN ('inbox', 'library')
        ORDER BY size ASC
        """
    )
    for row in rows:
        if not row["relpath"].lower().endswith(AUDIO_SUFFIXES):
            continue
        root = settings.inbox_dir if row["root"] == "inbox" else settings.library_dir
        path = root / row["relpath"]
        if path.exists():
            return path
    return None


_BUILDERS: dict[str, Callable[[sqlite3.Connection, Settings], _Probe]] = {
    "tmdb": _tmdb,
    "tvmaze": _tvmaze,
    "musicbrainz": _musicbrainz,
    "discogs": _discogs,
    "lastfm": _lastfm,
    "openlibrary": _openlibrary,
    "coverart": _coverart,
    "acoustid": _acoustid,
}


def testable(slug: str) -> bool:
    return slug in _BUILDERS


__all__ = [
    "ProbeResult",
    "UnknownCatalog",
    "probe_catalog",
    "testable",
]

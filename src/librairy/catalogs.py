"""Catalog registry: what each metadata source is for, and how to enable it.

Single source of truth behind the Settings catalog cards and the configuration
docs. Every catalog LibrAIry can consult is described here — what it
identifies, whether it needs a key, what it costs, where to sign up, and what
leaves the machine when it is used.

Catalogs are consulted BEFORE AI in the classification cascade; a catalog that
is unreachable or unconfigured degrades silently to the next evidence source.
"""

from __future__ import annotations

from dataclasses import dataclass

KEYLESS = ""


@dataclass(frozen=True)
class Step:
    """One instruction. `url` makes the page it refers to one click away.

    Signup flows are the place people give up, and "open Settings → API" is
    only useful if you are already on the right site.
    """

    text: str
    url: str = ""
    url_label: str = ""


@dataclass(frozen=True)
class CatalogInfo:
    slug: str
    name: str
    identifies: str
    key_field: str  # "" when the catalog needs no key
    cost: str
    signup_url: str
    steps: tuple[Step, ...]
    sends: str
    env_var: str = ""
    integrated: bool = True

    @property
    def keyless(self) -> bool:
        return self.key_field == KEYLESS


CATALOGS: tuple[CatalogInfo, ...] = (
    CatalogInfo(
        slug="musicbrainz",
        name="MusicBrainz",
        identifies="Music releases, artists and albums",
        key_field=KEYLESS,
        cost="Free — no account needed",
        signup_url="https://musicbrainz.org",
        steps=(Step("Nothing to do — it works out of the box."),),
        sends="Track and album titles, artist names, and durations. Never file paths.",
    ),
    CatalogInfo(
        slug="acoustid",
        name="AcoustID",
        identifies="Music identified by its audio fingerprint",
        key_field="acoustid",
        cost="Free",
        signup_url="https://acoustid.org/new-application",
        env_var="ACOUSTID_KEY",
        steps=(
            Step(
                "Create a free AcoustID account (email and password, nothing else).",
                "https://acoustid.org/login",
                "Sign up / log in",
            ),
            Step(
                'Register an application. Any name will do — pick "LibrAIry". '
                'The value you need is labelled "API key".',
                "https://acoustid.org/new-application",
                "Register an application",
            ),
            Step("Add ACOUSTID_KEY=<your key> to your .env file."),
            Step("Restart the container: docker compose up -d"),
        ),
        sends="An audio fingerprint and duration — not the audio itself, never file paths.",
    ),
    CatalogInfo(
        slug="tmdb",
        name="TMDB",
        identifies="Movies and TV shows",
        key_field="tmdb",
        cost="Free for personal use",
        signup_url="https://www.themoviedb.org/settings/api",
        env_var="TMDB_KEY",
        steps=(
            Step(
                "Create a free TMDB account and verify the email they send you.",
                "https://www.themoviedb.org/signup",
                "Create an account",
            ),
            Step(
                'Request an API key. Choose "Developer", accept the terms, and fill '
                "the short form — personal use is fine, and it is approved instantly.",
                "https://www.themoviedb.org/settings/api/request",
                "Request a key",
            ),
            Step(
                'Copy the value shown as "API Key (v3 auth)" — the long v4 token is '
                "not the one LibrAIry uses.",
                "https://www.themoviedb.org/settings/api",
                "Open your API settings",
            ),
            Step("Add TMDB_KEY=<your key> to your .env file."),
            Step("Restart the container: docker compose up -d"),
        ),
        sends="Cleaned title guesses and years. Never file paths.",
    ),
    CatalogInfo(
        slug="discogs",
        name="Discogs",
        identifies="Music releases, including vinyl and rare pressings",
        key_field="discogs",
        cost="Free",
        signup_url="https://www.discogs.com/settings/developers",
        env_var="DISCOGS_TOKEN",
        steps=(
            Step(
                "Create a free Discogs account.",
                "https://www.discogs.com/users/create",
                "Create an account",
            ),
            Step(
                'Open the developer settings and press "Generate new token" under '
                "Personal access token. That token is the whole setup — you do not "
                "need to register an application.",
                "https://www.discogs.com/settings/developers",
                "Generate a token",
            ),
            Step("Paste it into the box below, or set DISCOGS_TOKEN in your .env file."),
        ),
        sends=(
            "A cleaned guess at the artist and title, for files with no readable tags. "
            "Never file paths."
        ),
    ),
    CatalogInfo(
        slug="lastfm",
        name="Last.fm",
        identifies="Genres for music that has none",
        key_field="lastfm",
        cost="Free",
        signup_url="https://www.last.fm/api/account/create",
        env_var="LASTFM_KEY",
        steps=(
            Step(
                "Create a free Last.fm account.",
                "https://www.last.fm/join",
                "Create an account",
            ),
            Step(
                'Fill in the API account form. Any application name will do — pick '
                '"LibrAIry" — and the callback URL can be left empty.',
                "https://www.last.fm/api/account/create",
                "Request an API account",
            ),
            Step(
                'Copy the value shown as "API key". The shared secret is for signed '
                "requests and LibrAIry never makes any, so you can ignore it.",
                "https://www.last.fm/api/accounts",
                "See your API accounts",
            ),
            Step("Paste it into the box below, or set LASTFM_KEY in your .env file."),
        ),
        sends="Artist and album names. Never file paths.",
    ),
    CatalogInfo(
        slug="coverart",
        name="Cover Art Archive",
        identifies="Album art, shown on review cards",
        key_field=KEYLESS,
        cost="Free — no account needed",
        signup_url="https://coverartarchive.org",
        steps=(Step("Nothing to do — it works out of the box."),),
        sends=(
            "A MusicBrainz release ID when the file already has one, otherwise an "
            "artist and album name to find it. Only when you open a preview, never "
            "during analysis. Never file paths."
        ),
    ),
    CatalogInfo(
        slug="tvmaze",
        name="TVmaze",
        identifies="TV shows, and the title of each individual episode",
        key_field=KEYLESS,
        cost="Free — no account needed",
        signup_url="https://www.tvmaze.com/api",
        steps=(Step("Nothing to do — it works out of the box."),),
        sends="Cleaned show-title guesses, plus season and episode numbers. Never file paths.",
    ),
    CatalogInfo(
        slug="openlibrary",
        name="Open Library",
        identifies="Books by title, author or ISBN",
        key_field=KEYLESS,
        cost="Free — no account needed",
        signup_url="https://openlibrary.org/developers/api",
        steps=(Step("Nothing to do — it works out of the box."),),
        sends="Cleaned title and author guesses. Never file paths.",
    ),
)

CATALOGS_BY_SLUG = {catalog.slug: catalog for catalog in CATALOGS}


def catalog_status(catalog: CatalogInfo, keys: dict[str, str]) -> str:
    """One of: not needed / set / not set — mirrors the key-status vocabulary."""
    if catalog.keyless:
        return "not needed"
    return keys.get(catalog.key_field, "not set")


def catalog_enabled(conn, slug: str) -> bool:
    """Runtime toggle: catalog.<slug>.enabled. Keyless catalogs default ON."""
    import json as _json

    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (f"catalog.{slug}.enabled",)
    ).fetchone()
    if row is None:
        return True
    try:
        return bool(_json.loads(row["value"]))
    except (TypeError, ValueError):
        return True

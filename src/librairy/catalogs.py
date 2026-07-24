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
class CatalogInfo:
    slug: str
    name: str
    identifies: str
    key_field: str  # "" when the catalog needs no key
    cost: str
    signup_url: str
    steps: tuple[str, ...]
    sends: str
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
        steps=("Nothing to do — it works out of the box.",),
        sends="Track and album titles, artist names, and durations. Never file paths.",
    ),
    CatalogInfo(
        slug="acoustid",
        name="AcoustID",
        identifies="Music identified by its audio fingerprint",
        key_field="acoustid",
        cost="Free",
        signup_url="https://acoustid.org/new-application",
        steps=(
            "Create a free account at acoustid.org.",
            'Register an application (any name) to get an "API key".',
            "Paste the key into ACOUSTID_KEY in your .env, then restart the container.",
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
        steps=(
            "Create a free account at themoviedb.org.",
            'Open Settings → API and request an API key (choose "Developer").',
            "Paste the key into TMDB_KEY in your .env, then restart the container.",
        ),
        sends="Cleaned title guesses and years. Never file paths.",
    ),
    CatalogInfo(
        slug="openlibrary",
        name="Open Library",
        identifies="Books by title, author or ISBN",
        key_field=KEYLESS,
        cost="Free — no account needed",
        signup_url="https://openlibrary.org/developers/api",
        steps=("Nothing to do — it works out of the box.",),
        sends="Cleaned title and author guesses. Never file paths.",
    ),
)

CATALOGS_BY_SLUG = {catalog.slug: catalog for catalog in CATALOGS}


def catalog_status(catalog: CatalogInfo, keys: dict[str, str]) -> str:
    """One of: not needed / set / not set — mirrors the key-status vocabulary."""
    if catalog.keyless:
        return "not needed"
    return keys.get(catalog.key_field, "not set")

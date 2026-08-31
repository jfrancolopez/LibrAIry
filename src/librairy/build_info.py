"""Which build is this, and which schema is it looking at?

Three questions an operator asks before doing anything irreversible — before an
upgrade, during a support conversation, after a rollback — and until now they
were answered in three different places or not at all. The web footer showed
the version. `librairy --version` showed the version. Nothing showed the schema
the database is actually at, and nothing showed which commit the running image
was built from.

The version has exactly one source, `librairy.__version__`, and this module
does not become a second one. What it adds is the other two facts and one place
to read all three together.

**The revision is truthful or absent.** It is baked into the image at build
time from the commit being built, and there is deliberately no fallback that
shells out to `git`: a container has no repository in it, and a fallback that
quietly reported the *developer's* working tree would be worse than an honest
"unknown". A source checkout reports no revision, and that is the correct
answer for a source checkout.

**Nothing here is a secret.** A version, a schema number and a commit hash say
what the software is. Host paths, tokens and provider credentials are not
build information and never appear.
"""

from __future__ import annotations

import os
import sqlite3

from librairy import __version__
from librairy.db import SCHEMA_VERSION

#  Set by the image build from the commit it was built from. Empty everywhere
#  else, which is the honest answer for a source checkout.
REVISION_ENV = "LIBRAIRY_REVISION"

UNKNOWN = "unknown"


def revision() -> str:
    """The commit this build came from, or "" when nothing recorded one."""
    return (os.environ.get(REVISION_ENV) or "").strip()


def describe(conn: sqlite3.Connection | None = None) -> dict[str, object]:
    """Version, schema and revision — the three facts, in one shape.

    `schema_supported` is what this code knows how to migrate to;
    `schema_current` is what the database in front of it is actually at. They
    differ for exactly as long as an upgrade has not been run yet, which is the
    moment somebody most needs to be able to see both.
    """
    found: dict[str, object] = {
        "version": __version__,
        "schema_supported": SCHEMA_VERSION,
        "revision": revision() or UNKNOWN,
    }
    if conn is not None:
        found["schema_current"] = int(
            conn.execute("PRAGMA user_version").fetchone()[0]
        )
        found["migration_pending"] = int(found["schema_current"]) < SCHEMA_VERSION
    return found

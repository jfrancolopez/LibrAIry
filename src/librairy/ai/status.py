from __future__ import annotations

import json
import sqlite3

from librairy.ai.base import HealthResult, ProviderConfig
from librairy.planner import utc_now


def upsert_provider_status(
    conn: sqlite3.Connection,
    config: ProviderConfig,
    health: HealthResult | None = None,
    *,
    used: bool = False,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO provider_status(
          name, kind, endpoint, model, enabled, last_ok_at, last_error, latency_ms, last_used_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
          kind=excluded.kind,
          endpoint=excluded.endpoint,
          model=excluded.model,
          enabled=excluded.enabled,
          last_ok_at=COALESCE(excluded.last_ok_at, provider_status.last_ok_at),
          last_error=excluded.last_error,
          latency_ms=excluded.latency_ms,
          last_used_at=COALESCE(excluded.last_used_at, provider_status.last_used_at)
        """,
        (
            config.name,
            config.kind,
            config.endpoint,
            config.model,
            int(config.enabled),
            now if health and health.ok else None,
            health.error if health and not health.ok else None,
            health.latency_ms if health else None,
            now if used else None,
        ),
    )
    # Only a health check knows what models a server has, so only a health
    # check writes the list. The ON CONFLICT clause used to assign
    # `available_models=excluded.available_models`, and since the INSERT never
    # named that column, `excluded` carried its schema default of '[]' — so
    # every ordinary status refresh silently erased the list a test had just
    # discovered. That is why `ai status` always read `"[]"`.
    if health is not None:
        conn.execute(
            "UPDATE provider_status SET available_models=? WHERE name=?",
            (json.dumps(list(health.models)), config.name),
        )


def list_provider_status(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Status rows with `available_models` as a list, not as JSON-in-a-string.

    The column stays TEXT — JSON in SQLite is fine — but nothing outside this
    module should have to know that. It leaked all the way to `librairy --json
    ai status`, which emitted `"available_models": "[]"`: a string where every
    consumer wanted an array.
    """
    rows = conn.execute("SELECT * FROM provider_status ORDER BY name")
    return [
        dict(row) | {"available_models": provider_models(row["available_models"])} for row in rows
    ]


def provider_models(raw: object) -> list[str]:
    """Whatever is in the column, as a list of names.

    Legacy rows, hand-edited rows and rows written before the column existed
    all pass through here. A malformed value is not worth a 500 on the Health
    page, so it reads as "nothing known" — never `eval`, never a raise.
    """
    if isinstance(raw, list):
        return [str(model) for model in raw]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        models = json.loads(raw)
    except ValueError:
        return []
    return [str(model) for model in models] if isinstance(models, list) else []

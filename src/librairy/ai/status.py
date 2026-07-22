from __future__ import annotations

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


def list_provider_status(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM provider_status ORDER BY name"))

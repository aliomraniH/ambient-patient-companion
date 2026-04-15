"""Datetime helpers shared across the three MCP servers.

Asyncpg returns ``TIMESTAMPTZ`` columns as timezone-aware ``datetime`` objects
and ``TIMESTAMP WITHOUT TIME ZONE`` columns as naive ``datetime`` objects.
Mixing those with a locally-produced ``datetime.now(timezone.utc)`` raises
``TypeError: can't subtract offset-naive and offset-aware datetimes``.

``ensure_aware`` is the single normalisation point. Wrap every DB-read
datetime that feeds into arithmetic; the helper is a no-op when the value is
already aware or ``None``.
"""

from __future__ import annotations

from datetime import datetime, timezone


def ensure_aware(dt: datetime | None) -> datetime | None:
    """Return an aware UTC datetime for ``dt``.

    - ``None`` → ``None`` (pass-through, so callers can forward nullable DB
      columns without a conditional).
    - Naive ``datetime`` → same moment, tagged as UTC. This is the correct
      assumption for any ``TIMESTAMP WITHOUT TIME ZONE`` column in this repo:
      every ingest path stores wall clocks in UTC.
    - Aware ``datetime`` → returned unchanged (same object; ``is`` stable).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

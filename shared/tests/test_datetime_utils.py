"""Unit tests for shared.datetime_utils.ensure_aware.

Regression guard for the TypeError in generate_previsit_brief where
datetime.utcnow() was subtracted from a TIMESTAMPTZ value returned by
asyncpg.
"""
from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

_repo_root = pathlib.Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from shared.datetime_utils import ensure_aware  # noqa: E402


def test_naive_gets_utc_tzinfo():
    naive = datetime(2026, 4, 14, 12, 0, 0)
    assert naive.tzinfo is None
    result = ensure_aware(naive)
    assert result.tzinfo == timezone.utc
    # Wall clock preserved (assumed-UTC).
    assert result.replace(tzinfo=None) == naive


def test_aware_passthrough_is_same_object():
    aware = datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)
    result = ensure_aware(aware)
    assert result is aware


def test_non_utc_aware_datetime_preserved():
    tz_plus_3 = timezone(timedelta(hours=3))
    aware = datetime(2026, 4, 14, 15, 0, 0, tzinfo=tz_plus_3)
    result = ensure_aware(aware)
    # Not re-tagged as UTC — the original offset is kept.
    assert result.utcoffset() == timedelta(hours=3)


def test_none_passthrough():
    assert ensure_aware(None) is None


def test_aware_minus_naive_scenario_no_longer_raises():
    """The exact scenario that was crashing generate_previsit_brief:
    aware `now()` minus a DB-sourced datetime.
    """
    db_naive = datetime(2026, 4, 14, 10, 0, 0)  # mimic TIMESTAMP column
    now = datetime.now(timezone.utc)

    # The old code did `now - db_naive` → TypeError.
    with pytest.raises(TypeError):
        now - db_naive

    # With ensure_aware, arithmetic succeeds.
    delta = now - ensure_aware(db_naive)
    assert delta.total_seconds() >= 0


def test_aware_minus_aware_db_value_works():
    """TIMESTAMPTZ columns come back aware already — ensure_aware is a no-op
    and arithmetic still works.
    """
    db_aware = datetime(2026, 4, 14, 10, 0, 0, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - ensure_aware(db_aware)
    assert delta.total_seconds() >= 0

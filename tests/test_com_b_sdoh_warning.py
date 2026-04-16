"""Tests for the sdoh_data_warning field added to classify_com_b_barrier
output (Phase 7).

When the patient has no SDOH flags on file, classify_com_b_barrier now
surfaces a warning in its output so downstream consumers can:
  1. Call run_sdoh_assessment to populate SDOH data, or
  2. Reduce trust in the classification (structural barriers may be
     under-represented).

The existing function signature is unchanged (ADDITIVE ONLY).
"""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Make mcp-server/skills importable as "skills.*"
_MCP = str(_REPO / "mcp-server")
if _MCP not in sys.path:
    sys.path.append(_MCP)


class _FakeConn:
    """Async context manager + async methods matching asyncpg.Connection surface."""
    def __init__(self, sdoh_rows, checkin_rows):
        self._sdoh = sdoh_rows
        self._checkins = checkin_rows
        self.exec_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def fetch(self, query, *args):
        q = query.lower()
        if "patient_sdoh_flags" in q:
            return self._sdoh
        if "daily_checkins" in q:
            return self._checkins
        return []

    async def execute(self, query, *args):
        self.exec_calls.append((query, args))


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self._conn


@pytest.mark.asyncio
async def test_warning_when_no_sdoh_and_default_bucket():
    from skills.behavioral_tools import classify_com_b_barrier

    conn = _FakeConn(sdoh_rows=[], checkin_rows=[])
    pool = _FakePool(conn)

    with patch("skills.behavioral_tools.get_pool", new=AsyncMock(return_value=pool)):
        raw = await classify_com_b_barrier(
            patient_id="00000000-0000-0000-0000-000000000000",
            target_behavior="generic_behavior",
            evidence_window_days=30,
        )
    r = json.loads(raw)
    # Default bucket expected with no SDOH + no stress signal
    assert r["com_b_component"] == "Motivation"
    assert r["sub_component"] == "Reflective"
    assert r["sdoh_data_warning"], "expected non-empty warning in default bucket"
    assert "WARNING" in r["sdoh_data_warning"]
    assert "run_sdoh_assessment" in r["sdoh_data_warning"]


@pytest.mark.asyncio
async def test_advisory_when_no_sdoh_but_sensitive_behavior():
    from skills.behavioral_tools import classify_com_b_barrier

    # No SDOH, but high stress → classification not default bucket, yet
    # the target behavior is SDOH-sensitive so an ADVISORY should appear.
    checkins = [{"mood": 3, "energy": 3, "stress_level": 8} for _ in range(5)]
    conn = _FakeConn(sdoh_rows=[], checkin_rows=checkins)
    pool = _FakePool(conn)

    with patch("skills.behavioral_tools.get_pool", new=AsyncMock(return_value=pool)):
        raw = await classify_com_b_barrier(
            patient_id="00000000-0000-0000-0000-000000000000",
            target_behavior="medication_adherence",
            evidence_window_days=30,
        )
    r = json.loads(raw)
    # Stress-triggered bucket, not default
    assert r["sub_component"] == "Automatic"
    assert r["sdoh_data_warning"], "expected advisory for sensitive behavior"
    assert "ADVISORY" in r["sdoh_data_warning"]
    assert "medication_adherence" in r["sdoh_data_warning"]


@pytest.mark.asyncio
async def test_no_warning_when_sdoh_present():
    from skills.behavioral_tools import classify_com_b_barrier

    sdoh = [{"domain": "transportation", "severity": "high"}]
    conn = _FakeConn(sdoh_rows=sdoh, checkin_rows=[])
    pool = _FakePool(conn)

    with patch("skills.behavioral_tools.get_pool", new=AsyncMock(return_value=pool)):
        raw = await classify_com_b_barrier(
            patient_id="00000000-0000-0000-0000-000000000000",
            target_behavior="medication_adherence",
            evidence_window_days=30,
        )
    r = json.loads(raw)
    # SDOH present → Opportunity classification, no warning
    assert r["com_b_component"] == "Opportunity"
    assert r["sdoh_data_warning"] == ""


@pytest.mark.asyncio
async def test_existing_return_schema_preserved():
    from skills.behavioral_tools import classify_com_b_barrier

    conn = _FakeConn(sdoh_rows=[], checkin_rows=[])
    pool = _FakePool(conn)

    with patch("skills.behavioral_tools.get_pool", new=AsyncMock(return_value=pool)):
        raw = await classify_com_b_barrier(
            patient_id="00000000-0000-0000-0000-000000000000",
            target_behavior="t",
            evidence_window_days=30,
        )
    r = json.loads(raw)
    # All pre-existing keys still present — additive-only guarantee
    for key in (
        "patient_id", "target_behavior", "com_b_component", "sub_component",
        "primary_barrier", "confidence", "supporting_evidence",
    ):
        assert key in r, f"missing pre-existing key: {key}"
    # New key also present
    assert "sdoh_data_warning" in r


@pytest.mark.asyncio
async def test_warning_empty_string_when_sdoh_absent_but_behavior_not_sensitive():
    # Edge case: no SDOH, default bucket — WARNING fires (covered above).
    # But if SDOH present + default bucket, no warning. Already covered by
    # test_no_warning_when_sdoh_present. This test documents the third
    # combination: no SDOH, non-sensitive behavior, with strong motivation
    # signal → no warning because we're not in default bucket and the
    # behavior is outside the sensitive list.
    from skills.behavioral_tools import classify_com_b_barrier

    checkins = [{"mood": 3, "energy": 3, "stress_level": 8} for _ in range(5)]
    conn = _FakeConn(sdoh_rows=[], checkin_rows=checkins)
    pool = _FakePool(conn)

    with patch("skills.behavioral_tools.get_pool", new=AsyncMock(return_value=pool)):
        raw = await classify_com_b_barrier(
            patient_id="00000000-0000-0000-0000-000000000000",
            target_behavior="obscure_goal",  # not in sensitive list
            evidence_window_days=30,
        )
    r = json.loads(raw)
    assert r["sub_component"] == "Automatic"
    assert r["sdoh_data_warning"] == ""

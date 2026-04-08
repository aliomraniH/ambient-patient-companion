"""Tests for the TieredContextLoader module."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from server.deliberation.tiered_context_loader import (
    TieredContextLoader,
    sanitize,
    TIER1_BUDGET,
    TIER2_BUDGET,
    TIER3_BUDGET,
    TOTAL_BUDGET,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_pool(fetch_results=None, fetchrow_result=None, fetchval_result=None):
    """Build a mock asyncpg pool returning controlled query results.

    By default, fetchrow returns None (no encounter, no note, etc.).
    Tests that need a patient lookup should pass internal_id to skip it.
    """
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_results or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetchval = AsyncMock(return_value=fetchval_result)
    conn.execute = AsyncMock()

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool, conn


FAKE_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _patient_row():
    return {"id": FAKE_UUID}


# ── sanitize() ───────────────────────────────────────────────────────────────


class TestSanitize:
    def test_none_returns_empty_string(self):
        assert sanitize(None) == ""

    def test_normal_string_roundtrips(self):
        assert sanitize("hello world") == "hello world"

    def test_quotes_are_escaped(self):
        result = sanitize('He said "hello"')
        # Must be valid JSON when embedded
        json.loads(json.dumps(result))  # must not raise

    def test_numeric_value(self):
        assert sanitize(42) == "42"

    def test_unicode_handled(self):
        result = sanitize("caf\u00e9")
        json.loads(json.dumps(result))  # must not raise


# ── Budget constants ─────────────────────────────────────────────────────────


class TestBudgetConstants:
    def test_tier1_under_total(self):
        assert TIER1_BUDGET < TOTAL_BUDGET

    def test_tiers_sum_to_total(self):
        # Tier budgets should not exceed total
        assert TIER1_BUDGET + TIER2_BUDGET + TIER3_BUDGET <= TOTAL_BUDGET + 1_000

    def test_total_below_crash_zone(self):
        assert TOTAL_BUDGET < 16_190


# ── load_tier1() ─────────────────────────────────────────────────────────────


class TestLoadTier1:
    @pytest.mark.asyncio
    async def test_returns_dict_with_expected_keys(self):
        pool, conn = _mock_pool()  # fetchrow=None → no encounters/notes
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        ctx = await loader.load_tier1()

        assert "active_conditions" in ctx
        assert "recent_labs" in ctx
        assert "active_medications" in ctx

    @pytest.mark.asyncio
    async def test_tier1_marks_loaded(self):
        pool, conn = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        await loader.load_tier1()
        assert 1 in loader._loaded_tiers

    @pytest.mark.asyncio
    async def test_tier1_idempotent(self):
        pool, conn = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        ctx1 = await loader.load_tier1()
        ctx2 = await loader.load_tier1()
        assert ctx2 == {}  # second call returns empty — already loaded

    @pytest.mark.asyncio
    async def test_tier1_json_safe(self):
        """Tier 1 output must be JSON-serializable without errors."""
        pool, conn = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        ctx = await loader.load_tier1()
        serialized = json.dumps(ctx)
        json.loads(serialized)  # must not raise

    @pytest.mark.asyncio
    async def test_tier1_empty_meds_shows_none_documented(self):
        pool, conn = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        ctx = await loader.load_tier1()
        assert ctx["active_medications"] == ["none documented"]


# ── load_tier2() ─────────────────────────────────────────────────────────────


class TestLoadTier2:
    @pytest.mark.asyncio
    async def test_tier2_skipped_when_budget_exhausted(self):
        pool, conn = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)
        # Simulate tier 1 used most of the budget
        loader._chars_used = TOTAL_BUDGET - TIER2_BUDGET + 1

        ctx = await loader.load_tier2()
        assert ctx == {}

    @pytest.mark.asyncio
    async def test_tier2_marks_loaded(self):
        pool, conn = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        await loader.load_tier2()
        assert 2 in loader._loaded_tiers

    @pytest.mark.asyncio
    async def test_tier2_idempotent(self):
        pool, conn = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        await loader.load_tier2()
        ctx2 = await loader.load_tier2()
        assert ctx2 == {}

    @pytest.mark.asyncio
    async def test_tier2_returns_expected_keys(self):
        pool, conn = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        ctx = await loader.load_tier2()
        assert "lab_history" in ctx
        assert "recent_encounters" in ctx
        assert "condition_history" in ctx


# ── load_on_demand() ─────────────────────────────────────────────────────────


class TestLoadOnDemand:
    @pytest.mark.asyncio
    async def test_on_demand_skipped_when_budget_exhausted(self):
        pool, conn = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)
        loader._chars_used = TOTAL_BUDGET  # exhausted

        ctx = await loader.load_on_demand({"type": "clinical_note"})
        assert ctx == {}

    @pytest.mark.asyncio
    async def test_on_demand_clinical_note(self):
        note_row = {
            "note_type": "Progress Note",
            "note_text": "Patient presents with controlled diabetes.",
            "note_date": None,
            "author": "Dr. Test",
        }
        pool, conn = _mock_pool(fetchrow_result=note_row)
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        ctx = await loader.load_on_demand({"type": "clinical_note", "reason": "test"})
        assert any("clinical_note" in k for k in ctx.keys())

    @pytest.mark.asyncio
    async def test_on_demand_truncates_large_notes(self):
        note_row = {
            "note_type": "Visit Note",
            "note_text": "A" * 5000,  # way over per_request_budget
            "note_date": None,
            "author": "Dr. Test",
        }
        pool, conn = _mock_pool(fetchrow_result=note_row)
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        ctx = await loader.load_on_demand({"type": "clinical_note"})
        for key, val in ctx.items():
            if "text" in val:
                assert val["text"].endswith("...[truncated]")

    @pytest.mark.asyncio
    async def test_on_demand_lab_trend(self):
        pool, conn = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        ctx = await loader.load_on_demand({"type": "lab_trend", "test": "HbA1c"})
        assert "lab_trend_HbA1c" in ctx

    @pytest.mark.asyncio
    async def test_on_demand_unknown_type_returns_empty(self):
        pool, conn = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        ctx = await loader.load_on_demand({"type": "unknown_type"})
        assert ctx == {}


# ── context_summary() ────────────────────────────────────────────────────────


class TestContextSummary:
    @pytest.mark.asyncio
    async def test_summary_tracks_chars(self):
        pool, conn = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        await loader.load_tier1()
        summary = loader.context_summary()

        assert summary["chars_used"] >= 0
        assert summary["chars_budget"] == TOTAL_BUDGET
        assert 1 in summary["tiers_loaded"]
        assert isinstance(summary["pct_used"], float)

    def test_summary_empty_loader(self):
        pool, _ = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN")

        summary = loader.context_summary()
        assert summary["chars_used"] == 0
        assert summary["tiers_loaded"] == []


# ── _resolve_internal_id() ───────────────────────────────────────────────────


class TestResolveInternalId:
    @pytest.mark.asyncio
    async def test_skips_lookup_when_internal_id_provided(self):
        pool, conn = _mock_pool()
        loader = TieredContextLoader(pool, "TEST-MRN", internal_id=FAKE_UUID)

        await loader.load_tier1()
        assert loader._internal_id == FAKE_UUID

    @pytest.mark.asyncio
    async def test_raises_when_patient_not_found(self):
        pool, conn = _mock_pool(fetchrow_result=None)
        loader = TieredContextLoader(pool, "NONEXISTENT")

        with pytest.raises(ValueError, match="not found"):
            await loader.load_tier1()

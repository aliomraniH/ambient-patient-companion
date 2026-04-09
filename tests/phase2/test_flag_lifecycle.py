"""
Test suite for the Flag Lifecycle & Retroactive Correction System.

Run with:
    python -m pytest tests/phase2/test_flag_lifecycle.py -v --tb=short

All tests use mocked DB connections — no DATABASE_URL or API keys required.
"""

import json
import uuid
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from server.deliberation.flag_writer import (
    infer_flag_basis,
    compute_flag_fingerprint,
    score_data_quality,
    collect_data_provenance,
    write_flag,
)
from server.deliberation.flag_reviewer import (
    run_flag_review,
    _check_deterministic_retract,
    _generate_review_summary,
)


# ═══════════════════════════════════════════════════════════════════
# A. Flag Basis Inference (5 tests)
# ═══════════════════════════════════════════════════════════════════

class TestInferFlagBasis:
    def test_data_corrupt_zero_values(self):
        assert infer_flag_basis("All labs show 0.0") == "data_corrupt"

    def test_data_corrupt_placeholder(self):
        assert infer_flag_basis("placeholder value detected") == "data_corrupt"

    def test_data_missing(self):
        assert infer_flag_basis("Sex not documented in records") == "data_missing"

    def test_data_stale(self):
        assert infer_flag_basis("Last retinal exam was 2.5-year ago") == "data_stale"

    def test_data_conflict(self):
        assert infer_flag_basis("Active diagnosis contradicts lab values") == "data_conflict"

    def test_clinical_finding_default(self):
        assert infer_flag_basis("Elevated blood pressure trend") == "clinical_finding"

    def test_missing_unavailable(self):
        assert infer_flag_basis("Imaging results unavailable") == "data_missing"


# ═══════════════════════════════════════════════════════════════════
# B. Flag Fingerprint (3 tests)
# ═══════════════════════════════════════════════════════════════════

class TestFlagFingerprint:
    def test_deterministic(self):
        fp1 = compute_flag_fingerprint("pid-1", "Labs show 0.0", "data_corrupt")
        fp2 = compute_flag_fingerprint("pid-1", "Labs show 0.0", "data_corrupt")
        assert fp1 == fp2

    def test_case_insensitive_title(self):
        fp1 = compute_flag_fingerprint("pid-1", "Labs Show 0.0", "data_corrupt")
        fp2 = compute_flag_fingerprint("pid-1", "labs show 0.0", "data_corrupt")
        assert fp1 == fp2

    def test_different_inputs_differ(self):
        fp1 = compute_flag_fingerprint("pid-1", "Labs show 0.0", "data_corrupt")
        fp2 = compute_flag_fingerprint("pid-2", "Labs show 0.0", "data_corrupt")
        assert fp1 != fp2

    def test_length_32(self):
        fp = compute_flag_fingerprint("pid-1", "test", "clinical_finding")
        assert len(fp) == 32


# ═══════════════════════════════════════════════════════════════════
# C. Data Quality Scoring (4 tests)
# ═══════════════════════════════════════════════════════════════════

class TestDataQualityScoring:
    def test_empty_provenance(self):
        assert score_data_quality([]) == 1.0

    def test_all_valid(self):
        provenance = [
            {"is_suspect": False},
            {"is_suspect": False},
        ]
        assert score_data_quality(provenance) == 1.0

    def test_all_suspect(self):
        provenance = [
            {"is_suspect": True},
            {"is_suspect": True},
        ]
        assert score_data_quality(provenance) == 0.0

    def test_mixed(self):
        provenance = [
            {"is_suspect": True},
            {"is_suspect": False},
            {"is_suspect": False},
            {"is_suspect": False},
        ]
        assert score_data_quality(provenance) == 0.75


# ═══════════════════════════════════════════════════════════════════
# D. Write Flag (4 tests)
# ═══════════════════════════════════════════════════════════════════

class TestWriteFlag:
    @pytest.fixture
    def mock_conn(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        conn.fetchrow.return_value = None
        conn.fetchval.return_value = uuid.uuid4()
        return conn

    @pytest.mark.asyncio
    async def test_creates_new_flag(self, mock_conn):
        result = await write_flag(
            mock_conn,
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            {"flag": "All labs show 0.0", "priority": "medium"},
        )
        assert result["action"] == "created"
        assert "flag_id" in result
        # Should have called INSERT (fetchval for RETURNING)
        mock_conn.fetchval.assert_called_once()

    @pytest.mark.asyncio
    async def test_updates_existing_flag(self, mock_conn):
        existing_id = uuid.uuid4()
        mock_conn.fetchrow.return_value = {"id": existing_id}
        result = await write_flag(
            mock_conn,
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            {"flag": "All labs show 0.0", "priority": "medium"},
        )
        assert result["action"] == "updated_existing"
        assert result["flag_id"] == str(existing_id)

    @pytest.mark.asyncio
    async def test_priority_normalization(self, mock_conn):
        result = await write_flag(
            mock_conn,
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            {"flag": "Test flag", "priority": "Medium-High"},
        )
        assert result["action"] == "created"
        # Verify the INSERT was called with normalized priority
        call_args = mock_conn.fetchval.call_args
        # Priority is the 6th positional arg (index 5 in args tuple)
        assert call_args[0][6] == "medium-high"

    @pytest.mark.asyncio
    async def test_invalid_priority_defaults_medium(self, mock_conn):
        result = await write_flag(
            mock_conn,
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            {"flag": "Test flag", "priority": "URGENT"},
        )
        assert result["action"] == "created"
        call_args = mock_conn.fetchval.call_args
        assert call_args[0][6] == "medium"


# ═══════════════════════════════════════════════════════════════════
# E. Data Provenance Collection (2 tests)
# ═══════════════════════════════════════════════════════════════════

class TestDataProvenance:
    @pytest.mark.asyncio
    async def test_collects_lab_provenance(self):
        conn = AsyncMock()
        lab_row = {
            "id": uuid.uuid4(),
            "metric_type": "HbA1c",
            "value": 4.8,
            "unit": "%",
            "measured_at": datetime.now(timezone.utc),
        }
        conn.fetch.return_value = [lab_row]

        provenance = await collect_data_provenance(
            conn, str(uuid.uuid4()), "A1c shows 0.0 values",
        )
        assert len(provenance) >= 1
        assert provenance[0]["table"] == "biometric_readings"
        assert provenance[0]["field"] == "value"

    @pytest.mark.asyncio
    async def test_collects_condition_provenance(self):
        conn = AsyncMock()
        cond_row = {
            "id": uuid.uuid4(),
            "condition_name": "Prediabetes",
            "clinical_status": "active",
            "onset_date": datetime(2017, 1, 1),
        }
        # First call for labs (no match in text), second for conditions
        conn.fetch.return_value = [cond_row]

        provenance = await collect_data_provenance(
            conn, str(uuid.uuid4()), "Prediabetes diagnosis contradicts labs",
        )
        # Should have condition provenance
        cond_prov = [p for p in provenance if p["table"] == "patient_conditions"]
        assert len(cond_prov) >= 1


# ═══════════════════════════════════════════════════════════════════
# F. Deterministic Retraction Rules (4 tests)
# ═══════════════════════════════════════════════════════════════════

class TestDeterministicRetract:
    @pytest.mark.asyncio
    async def test_retracts_data_corrupt_with_real_values(self):
        conn = AsyncMock()
        conn.fetchval.return_value = 12  # 12 real lab values exist

        flag = {
            "nudge_was_sent": False,
            "flag_basis": "data_corrupt",
            "priority": "medium",
            "had_zero_values": True,
            "title": "Labs show 0.0",
        }
        result = await _check_deterministic_retract(
            conn, str(uuid.uuid4()), flag,
        )
        assert result is not None
        assert "corrected" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_skips_high_priority(self):
        conn = AsyncMock()
        flag = {
            "nudge_was_sent": False,
            "flag_basis": "data_corrupt",
            "priority": "high",
            "had_zero_values": True,
            "title": "Labs show 0.0",
        }
        result = await _check_deterministic_retract(
            conn, str(uuid.uuid4()), flag,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_sent_nudge(self):
        conn = AsyncMock()
        flag = {
            "nudge_was_sent": True,
            "flag_basis": "data_corrupt",
            "priority": "medium",
            "had_zero_values": True,
            "title": "Labs show 0.0",
        }
        result = await _check_deterministic_retract(
            conn, str(uuid.uuid4()), flag,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_when_few_real_values(self):
        conn = AsyncMock()
        conn.fetchval.return_value = 2  # Only 2 real values — not enough

        flag = {
            "nudge_was_sent": False,
            "flag_basis": "data_corrupt",
            "priority": "medium",
            "had_zero_values": True,
            "title": "Labs show 0.0",
        }
        result = await _check_deterministic_retract(
            conn, str(uuid.uuid4()), flag,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_retracts_missing_sex_when_populated(self):
        conn = AsyncMock()
        conn.fetchval.return_value = "Female"

        flag = {
            "nudge_was_sent": False,
            "flag_basis": "data_missing",
            "priority": "low",
            "had_zero_values": False,
            "title": "Patient sex not documented",
        }
        result = await _check_deterministic_retract(
            conn, str(uuid.uuid4()), flag,
        )
        assert result is not None
        assert "sex" in result["reason"].lower() or "gender" in result["reason"].lower()


# ═══════════════════════════════════════════════════════════════════
# G. Review Summary Formatting (2 tests)
# ═══════════════════════════════════════════════════════════════════

class TestReviewSummary:
    def test_with_changes(self):
        stats = {"retracted": 2, "escalated": 1, "confirmed": 0, "upgraded": 0, "downgraded": 0}
        summary = _generate_review_summary(stats, 5, "new labs")
        assert "Reviewed 5 open flags" in summary
        assert "2 retracted" in summary
        assert "1 escalated" in summary

    def test_no_changes(self):
        stats = {"retracted": 0, "escalated": 0, "confirmed": 0, "upgraded": 0, "downgraded": 0}
        summary = _generate_review_summary(stats, 3, "")
        assert "No changes" in summary


# ═══════════════════════════════════════════════════════════════════
# H. Full Review Flow (2 tests)
# ═══════════════════════════════════════════════════════════════════

class _MockPool:
    """Mock asyncpg pool with proper async context manager for acquire()."""
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return False


class TestFullReviewFlow:
    @pytest.mark.asyncio
    async def test_no_open_flags_returns_early(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _MockPool(conn)

        result = await run_flag_review(
            pool, str(uuid.uuid4()), "manual", str(uuid.uuid4()),
        )
        assert result["flags_reviewed"] == 0
        assert result["summary"] == "No open flags"

    @pytest.mark.asyncio
    @patch("server.deliberation.flag_reviewer._llm_review_flags", new_callable=AsyncMock)
    async def test_deterministic_retraction_applied(self, mock_llm):
        """A data_corrupt flag with had_zero_values=True should be auto-retracted
        when real lab values exist."""
        mock_llm.return_value = []

        flag_id = uuid.uuid4()
        conn = AsyncMock()
        # fetch: first call returns open flags
        conn.fetch.return_value = [
            {
                "id": flag_id,
                "flag_type": "missing_data_flag",
                "title": "All labs show 0.0",
                "description": "Data integrity issue",
                "priority": "medium",
                "flag_basis": "data_corrupt",
                "data_provenance": "[]",
                "data_quality_score": 0.0,
                "had_zero_values": True,
                "nudge_was_sent": False,
                "linked_nudge_ids": [],
                "flagged_at": datetime.now(timezone.utc),
            }
        ]
        # fetchval for lab count check → 10 real values
        conn.fetchval.return_value = 10
        pool = _MockPool(conn)

        result = await run_flag_review(
            pool, str(uuid.uuid4()), "post_ingest", str(uuid.uuid4()),
            "44 real lab values now in DB",
        )
        assert result["flags_reviewed"] == 1
        assert result["stats"]["retracted"] >= 1

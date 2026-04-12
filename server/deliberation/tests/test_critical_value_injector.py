"""Tests for the critical value injector (ingestion/context/critical_value_injector.py).

Tests cover:
  CV-1: Critical values injected into context.applicable_guidelines
  CV-2: Missing critical values flagged in __missing_critical__
  CV-3: Condition-specific gaps detected (E11 needs HbA1c, etc.)
  CV-4: Patient resolution by MRN and UUID
  CV-5: Graceful failure when DB error occurs
"""

import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

from ingestion.context.critical_value_injector import (
    inject_critical_values,
    CRITICAL_LOINC_CODES,
    CONDITION_REQUIRED_VALUES,
    _compute_condition_gaps,
    _age_in_days,
)


def _make_mock_pool(patient_uuid="abc-uuid", lab_rows=None):
    """Build a mock asyncpg pool where conn.fetchval and conn.fetch return our test data."""
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=patient_uuid)
    conn.fetch = AsyncMock(return_value=lab_rows or [])

    pool = MagicMock()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire)
    return pool, conn


def _make_context(active_conditions=None):
    """Build a simple mock PatientContextPackage-like object."""
    ctx = MagicMock()
    ctx.applicable_guidelines = []
    ctx.active_conditions = active_conditions or []
    return ctx


class TestCriticalValueInjection:
    """Test critical value injection logic."""

    @pytest.mark.asyncio
    async def test_critical_values_injected(self):
        """Lab values from DB are injected as a synthetic guideline entry."""
        lab_rows = [
            {
                "metric_type": "HbA1c",
                "value": 7.4,
                "unit": "%",
                "measured_at": datetime.now(timezone.utc) - timedelta(days=30),
                "is_abnormal": True,
                "loinc_code": "4548-4",
            },
            {
                "metric_type": "Creatinine",
                "value": 1.2,
                "unit": "mg/dL",
                "measured_at": datetime.now(timezone.utc) - timedelta(days=30),
                "is_abnormal": False,
                "loinc_code": "2160-0",
            },
        ]
        pool, _ = _make_mock_pool(patient_uuid="abc-uuid", lab_rows=lab_rows)
        context = _make_context(active_conditions=[{"code": "E11.9"}])

        result = await inject_critical_values(context, pool, "4829341")

        # Find the injected entry
        critical_entry = next(
            (g for g in result.applicable_guidelines if g["source"] == "__critical_values__"),
            None,
        )
        assert critical_entry is not None
        injected = json.loads(critical_entry["content"])
        assert "hba1c_percent" in injected["critical_values"]
        assert injected["critical_values"]["hba1c_percent"]["value"] == 7.4
        assert "creatinine_mgdl" in injected["critical_values"]

    @pytest.mark.asyncio
    async def test_missing_critical_values_flagged(self):
        """When critical values are missing, __missing_critical__ entry is created."""
        # Only HbA1c present, all others missing
        lab_rows = [
            {
                "metric_type": "HbA1c",
                "value": 7.4,
                "unit": "%",
                "measured_at": datetime.now(timezone.utc),
                "is_abnormal": True,
                "loinc_code": "4548-4",
            },
        ]
        pool, _ = _make_mock_pool(patient_uuid="abc-uuid", lab_rows=lab_rows)
        context = _make_context(active_conditions=[{"code": "E11.9"}])

        result = await inject_critical_values(context, pool, "4829341")

        missing_entry = next(
            (g for g in result.applicable_guidelines if g["source"] == "__missing_critical__"),
            None,
        )
        assert missing_entry is not None
        missing = json.loads(missing_entry["content"])
        assert "creatinine_mgdl" in missing["missing_values"]
        assert "egfr" in missing["missing_values"]

    @pytest.mark.asyncio
    async def test_condition_gaps_for_t2dm(self):
        """E11 (T2DM) requires HbA1c, Creatinine, eGFR, UACR — flag missing ones."""
        # Only HbA1c — missing Creatinine, eGFR, UACR for T2DM
        lab_rows = [
            {
                "metric_type": "HbA1c",
                "value": 7.4,
                "unit": "%",
                "measured_at": datetime.now(timezone.utc),
                "is_abnormal": True,
                "loinc_code": "4548-4",
            },
        ]
        pool, _ = _make_mock_pool(patient_uuid="abc-uuid", lab_rows=lab_rows)
        context = _make_context(active_conditions=[{"code": "E11.9"}])

        result = await inject_critical_values(context, pool, "4829341")

        critical_entry = next(
            (g for g in result.applicable_guidelines if g["source"] == "__critical_values__"),
            None,
        )
        injected = json.loads(critical_entry["content"])
        gap_loincs = [g["missing_loinc"] for g in injected["condition_gaps"]]
        # T2DM (E11) requires Creatinine + eGFR + UACR
        assert "2160-0" in gap_loincs   # Creatinine
        assert "33914-3" in gap_loincs  # eGFR
        assert "14959-1" in gap_loincs  # UACR

    @pytest.mark.asyncio
    async def test_no_db_error_graceful(self):
        """DB error should not crash — context returned unchanged."""
        pool = MagicMock()
        pool.acquire = MagicMock(side_effect=Exception("DB error"))
        context = _make_context()

        result = await inject_critical_values(context, pool, "4829341")
        # Should return context without crashing
        assert result is context

    @pytest.mark.asyncio
    async def test_patient_not_found_returns_context(self):
        """When patient not found, context returned unchanged."""
        pool, conn = _make_mock_pool(patient_uuid=None)
        context = _make_context()

        result = await inject_critical_values(context, pool, "nonexistent-mrn")
        # Patient lookup failed, no injection
        assert len(result.applicable_guidelines) == 0


class TestConditionGaps:
    """Test condition-required value mapping."""

    def test_t2dm_requires_diabetes_labs(self):
        context = _make_context(active_conditions=[{"code": "E11.9"}])
        gaps = _compute_condition_gaps(context, found_loincs=set())
        # E11 requires HbA1c, Creatinine, eGFR, UACR — all missing
        assert len(gaps) == 4
        gap_loincs = [g["missing_loinc"] for g in gaps]
        assert "4548-4" in gap_loincs
        assert "2160-0" in gap_loincs

    def test_htn_requires_bp_and_potassium(self):
        context = _make_context(active_conditions=[{"code": "I10"}])
        gaps = _compute_condition_gaps(context, found_loincs=set())
        gap_loincs = [g["missing_loinc"] for g in gaps]
        assert "55284-4" in gap_loincs  # Systolic BP
        assert "8462-4" in gap_loincs   # Diastolic BP
        assert "2823-3" in gap_loincs   # Potassium

    def test_no_gaps_when_all_present(self):
        context = _make_context(active_conditions=[{"code": "E11.9"}])
        all_t2dm_loincs = set(CONDITION_REQUIRED_VALUES["E11"])
        gaps = _compute_condition_gaps(context, found_loincs=all_t2dm_loincs)
        assert len(gaps) == 0

    def test_no_active_conditions_no_gaps(self):
        context = _make_context(active_conditions=[])
        gaps = _compute_condition_gaps(context, found_loincs=set())
        assert len(gaps) == 0

    def test_unknown_condition_no_gaps(self):
        """Conditions not in our mapping produce no gaps."""
        context = _make_context(active_conditions=[{"code": "Z99.99"}])
        gaps = _compute_condition_gaps(context, found_loincs=set())
        assert len(gaps) == 0


class TestAgeComputation:
    """Test age in days helper."""

    def test_recent_timestamp(self):
        ts = datetime.now(timezone.utc) - timedelta(days=5)
        assert _age_in_days(ts) == 5

    def test_iso_string_timestamp(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        assert _age_in_days(ts) == 10

    def test_naive_datetime_handled(self):
        ts = datetime.utcnow() - timedelta(days=3)
        result = _age_in_days(ts)
        assert result == 3 or result == 2  # Tolerance for time elapsed

    def test_none_returns_none(self):
        assert _age_in_days(None) is None

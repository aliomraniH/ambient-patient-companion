"""Tests for gap_validation.py — context staleness detection and gap artifact collection."""
import json
import pytest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from server.deliberation.schemas import PatientContextPackage
from server.deliberation.gap_validation import (
    _extract_context_elements,
    _map_trigger_to_scenario,
    detect_staleness_internal,
    refresh_stale_data,
    _inject_fresh_data,
    build_gap_summary,
    validate_and_enrich_context,
    collect_gap_artifacts,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture() -> dict:
    """Load the patient fixture as a raw dict."""
    return json.loads((FIXTURES / "maria_chen_context.json").read_text())


def _load_fixture_as_package() -> PatientContextPackage:
    """Load the patient fixture as a PatientContextPackage."""
    return PatientContextPackage(**_load_fixture())


def _make_mock_pool():
    """Create a mock asyncpg pool where pool.acquire() works as async ctx mgr."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchrow = AsyncMock(return_value=None)

    mock_pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield mock_conn

    mock_pool.acquire = _acquire
    return mock_pool, mock_conn


# ── Context element extraction ───────────────────────────────────────────────


class TestExtractContextElements:
    def test_from_patient_context_package(self):
        """Labs and vitals from PatientContextPackage should be extracted."""
        ctx = _load_fixture_as_package()
        elements = _extract_context_elements(ctx)
        lab_elements = [e for e in elements if e["element_type"] == "lab_result"]
        vital_elements = [e for e in elements if e["element_type"] == "vital_sign"]

        assert len(lab_elements) == len(ctx.recent_labs)
        assert all(e["source_system"] == "ehr" for e in elements)
        assert len(vital_elements) > 0

    def test_from_dict_context(self):
        """Labs from progressive mode dict should be extracted."""
        ctx = {
            "recent_labs": [
                {"test": "hba1c", "value": "7.9", "unit": "%", "date": "2026-01-15"},
                {"test": "creatinine", "value": "0.9", "unit": "mg/dL", "date": "2026-01-15"},
            ],
            "vital_trends": [
                {"name": "systolic_bp", "readings": [{"value": 141, "date": "2026-03-20"}]},
            ],
        }
        elements = _extract_context_elements(ctx)
        lab_elements = [e for e in elements if e["element_type"] == "lab_result"]
        vital_elements = [e for e in elements if e["element_type"] == "vital_sign"]

        assert len(lab_elements) == 2
        assert len(vital_elements) == 1
        assert lab_elements[0]["loinc_code"] == "4548-4"  # hba1c

    def test_empty_package_yields_empty_elements(self):
        """PatientContextPackage with empty lists should yield nothing."""
        ctx = PatientContextPackage(
            patient_id="test", patient_name="Test", age=50, sex="F",
            mrn="test", primary_provider="", practice="",
            active_conditions=[], current_medications=[], recent_labs=[],
            vital_trends=[], care_gaps=[], sdoh_flags=[],
            prior_patient_knowledge=[], applicable_guidelines=[],
            upcoming_appointments=[], days_since_last_encounter=0,
            deliberation_trigger="manual",
        )
        assert _extract_context_elements(ctx) == []

    def test_empty_dict_yields_empty_elements(self):
        """Empty dict context should yield nothing."""
        assert _extract_context_elements({}) == []

    def test_medication_list_extracted(self):
        """Medications with start_date should produce a medication_list element."""
        ctx = _load_fixture_as_package()
        elements = _extract_context_elements(ctx)
        med_elements = [e for e in elements if e["element_type"] == "medication_list"]
        assert len(med_elements) == 1

    def test_loinc_mapping_for_known_labs(self):
        """Common lab names should map to LOINC codes."""
        ctx = {
            "recent_labs": [
                {"test": "hba1c", "value": "7.9", "unit": "%", "date": "2026-01-15"},
                {"test": "glucose", "value": "100", "unit": "mg/dL", "date": "2026-01-15"},
            ],
        }
        elements = _extract_context_elements(ctx)
        loincs = [e["loinc_code"] for e in elements]
        assert "4548-4" in loincs
        assert "2345-7" in loincs


# ── Trigger-to-scenario mapping ──────────────────────────────────────────────


class TestMapTriggerToScenario:
    def test_pre_encounter(self):
        assert _map_trigger_to_scenario("scheduled_pre_encounter") == "pre_encounter"

    def test_acute(self):
        assert _map_trigger_to_scenario("lab_result_received") == "acute_event"

    def test_medication_change(self):
        assert _map_trigger_to_scenario("medication_change") == "medication_change"

    def test_unknown_defaults_to_pre_encounter(self):
        assert _map_trigger_to_scenario("something_unknown") == "pre_encounter"

    def test_manual(self):
        assert _map_trigger_to_scenario("manual") == "chronic_management"


# ── Internal staleness detection ─────────────────────────────────────────────


class TestDetectStalenessInternal:
    def test_detects_stale_lab(self):
        """Lab result older than threshold should be flagged."""
        stale_date = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        elements = [
            {"element_type": "lab_result", "loinc_code": "4548-4",
             "last_updated": stale_date, "source_system": "ehr"},
        ]
        result = detect_staleness_internal(elements, "pre_encounter")
        assert len(result["stale_elements"]) == 1
        assert result["stale_elements"][0]["loinc_code"] == "4548-4"
        assert result["freshness_score"] == 0.0

    def test_all_fresh(self):
        """All fresh elements should yield freshness_score = 1.0."""
        fresh_date = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        elements = [
            {"element_type": "vital_sign", "loinc_code": None,
             "last_updated": fresh_date, "source_system": "ehr"},
        ]
        result = detect_staleness_internal(elements, "pre_encounter")
        assert result["freshness_score"] == 1.0
        assert result["stale_elements"] == []

    def test_acute_event_stricter(self):
        """In acute events, even 6-hour-old vitals should be stale."""
        six_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        elements = [
            {"element_type": "vital_sign", "loinc_code": None,
             "last_updated": six_hours_ago, "source_system": "ehr"},
        ]
        result = detect_staleness_internal(elements, "acute_event")
        assert len(result["stale_elements"]) == 1
        assert result["freshness_score"] == 0.0

    def test_empty_elements(self):
        """Empty list should yield freshness_score = 1.0."""
        result = detect_staleness_internal([], "pre_encounter")
        assert result["freshness_score"] == 1.0
        assert result["stale_elements"] == []

    def test_date_only_format(self):
        """Date strings without time component should parse correctly."""
        old_date = "2024-01-01"
        elements = [
            {"element_type": "lab_result", "loinc_code": "2089-1",
             "last_updated": old_date, "source_system": "ehr"},
        ]
        result = detect_staleness_internal(elements, "pre_encounter")
        # 2024-01-01 is >1 year old, default threshold for lab_result is 4380h (~6 months)
        assert len(result["stale_elements"]) == 1


# ── Refresh stale data ───────────────────────────────────────────────────────


class TestRefreshStaleData:
    @pytest.mark.asyncio
    async def test_finds_cached_data(self):
        """Should return found elements when cache has fresher data."""
        mock_pool, mock_conn = _make_mock_pool()
        mock_conn.fetchrow = AsyncMock(side_effect=[
            # First call: patient lookup by MRN
            {"id": "2cfaa9f2-3f47-44be-84e2-16f3a5dc0bbb"},
            # Second call: raw_fhir_cache query
            {"raw_json": '{"loinc": "4548-4"}',
             "retrieved_at": datetime.now(timezone.utc),
             "source_name": "warehouse"},
        ])

        stale = [{"element_type": "lab_result", "loinc_code": "4548-4"}]
        result = await refresh_stale_data(mock_pool, "4829341", stale)
        assert len(result) == 1
        assert result[0]["provenance"] == "raw_fhir_cache"

    @pytest.mark.asyncio
    async def test_patient_not_found(self):
        """Should return empty list if patient not found."""
        mock_pool, mock_conn = _make_mock_pool()
        mock_conn.fetchrow = AsyncMock(return_value=None)

        stale = [{"element_type": "lab_result", "loinc_code": "4548-4"}]
        result = await refresh_stale_data(mock_pool, "nonexistent", stale)
        assert result == []


# ── Context injection ────────────────────────────────────────────────────────


class TestInjectFreshData:
    def test_inject_into_package(self):
        """Fresh elements should be appended to PatientContextPackage.recent_labs."""
        ctx = _load_fixture_as_package()
        original_count = len(ctx.recent_labs)
        fresh = [{"element_type": "lab_result", "value": "6.5", "unit": "%",
                  "effective_date": "2026-04-01T00:00:00Z"}]
        result = _inject_fresh_data(ctx, fresh)
        assert len(result.recent_labs) == original_count + 1

    def test_inject_into_dict(self):
        """Fresh elements should create _refreshed_data key in dict."""
        ctx = {"recent_labs": []}
        fresh = [{"element_type": "lab_result", "value": "6.5"}]
        result = _inject_fresh_data(ctx, fresh)
        assert "_refreshed_data" in result
        assert len(result["_refreshed_data"]) == 1

    def test_no_fresh_data_unchanged(self):
        """Empty fresh elements should return context unchanged."""
        ctx = {"recent_labs": [{"test": "hba1c"}]}
        result = _inject_fresh_data(ctx, [])
        assert "_refreshed_data" not in result


# ── Gap summary ──────────────────────────────────────────────────────────────


class TestBuildGapSummary:
    def test_critical_and_high_gaps(self):
        gaps = [
            {"severity": "critical", "description": "Unknown drug interaction"},
            {"severity": "high", "description": "Stale HbA1c data"},
            {"severity": "low", "description": "Minor data gap"},
        ]
        summary = build_gap_summary(gaps)
        assert "1 critical gap(s)" in summary
        assert "1 high gap(s)" in summary
        assert "Minor" not in summary

    def test_no_critical_gaps(self):
        gaps = [
            {"severity": "low", "description": "Minor gap"},
            {"severity": "medium", "description": "Moderate gap"},
        ]
        summary = build_gap_summary(gaps)
        assert summary == ""

    def test_empty_gaps(self):
        assert build_gap_summary([]) == ""


# ── Full orchestration ───────────────────────────────────────────────────────


class TestValidateAndEnrichContext:
    @pytest.mark.asyncio
    async def test_returns_context_and_metadata(self):
        """Should return enriched context and validation metadata."""
        mock_pool, mock_conn = _make_mock_pool()
        ctx = _load_fixture_as_package()

        result_ctx, meta = await validate_and_enrich_context(
            context=ctx, db_pool=mock_pool,
            patient_id="4829341", trigger_type="scheduled_pre_encounter",
        )

        assert isinstance(result_ctx, PatientContextPackage)
        assert "freshness_score" in meta
        assert "context_validated_at" in meta
        assert isinstance(meta["freshness_score"], float)

    @pytest.mark.asyncio
    async def test_dict_context_works(self):
        """Should handle dict context (progressive mode) without error."""
        mock_pool, _ = _make_mock_pool()
        ctx = {"recent_labs": [], "vital_trends": []}

        result_ctx, meta = await validate_and_enrich_context(
            context=ctx, db_pool=mock_pool,
            patient_id="test-mrn", trigger_type="manual",
        )

        assert isinstance(result_ctx, dict)
        assert meta["freshness_score"] == 1.0

    @pytest.mark.asyncio
    async def test_failure_is_nonfatal(self):
        """Internal errors should not propagate — return original context."""
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(side_effect=RuntimeError("boom"))

        ctx = _load_fixture_as_package()
        result_ctx, meta = await validate_and_enrich_context(
            context=ctx, db_pool=mock_pool,
            patient_id="test", trigger_type="manual",
        )

        # Should still get back the original context
        assert isinstance(result_ctx, PatientContextPackage)


# ── Gap artifact collection ──────────────────────────────────────────────────


class TestCollectGapArtifacts:
    @pytest.mark.asyncio
    async def test_returns_gaps(self):
        """Should return gap artifacts from the DB."""
        mock_pool, mock_conn = _make_mock_pool()
        mock_conn.fetch = AsyncMock(return_value=[
            {
                "id": "uuid-1", "deliberation_id": "delib-1",
                "patient_mrn": "4829341", "emitting_agent": "MIRA",
                "gap_id": "gap-1", "gap_type": "stale_data",
                "severity": "high", "description": "Stale HbA1c",
                "impact_statement": "Cannot assess", "status": "open",
                "created_at": datetime.now(timezone.utc),
                "confidence_without_res": 0.45, "confidence_with_res": 0.85,
                "attempted_resolutions": "[]", "recommended_action": "include_caveat_in_output",
                "caveat_text": None, "resolution_method": None,
                "expires_at": None, "resolved_at": None,
            },
        ])

        gaps, summary = await collect_gap_artifacts(mock_pool, "delib-1")
        assert len(gaps) == 1
        assert gaps[0]["severity"] == "high"
        assert "1 high gap(s)" in summary

    @pytest.mark.asyncio
    async def test_empty_gaps(self):
        """Empty reasoning_gaps should return empty list and empty summary."""
        mock_pool, mock_conn = _make_mock_pool()
        mock_conn.fetch = AsyncMock(return_value=[])

        gaps, summary = await collect_gap_artifacts(mock_pool, "delib-empty")
        assert gaps == []
        assert summary == ""

    @pytest.mark.asyncio
    async def test_db_error_is_nonfatal(self):
        """DB errors should not propagate."""
        mock_pool = MagicMock()

        @asynccontextmanager
        async def _boom():
            raise RuntimeError("DB down")
            yield  # noqa: unreachable

        mock_pool.acquire = _boom

        gaps, summary = await collect_gap_artifacts(mock_pool, "delib-err")
        assert gaps == []
        assert summary == ""

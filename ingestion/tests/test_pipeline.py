"""P1-P8: Ingestion pipeline stage tests.

All tests use unittest.mock — NO live database required.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# Ensure project root and mcp-server are on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "mcp-server"))

from ingestion.pipeline import IngestionPipeline, IngestionResult


def _make_mock_pool():
    """Create a mock asyncpg pool with async context manager support."""
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=0)

    pool = MagicMock()
    acq = AsyncMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = acq

    return pool, conn


def _make_fake_bundle():
    """Minimal FHIR bundle for testing."""
    return {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "resourceType": "Patient",
                    "id": "test-patient-001",
                    "name": [{"family": "Test", "given": ["Patient"]}],
                    "birthDate": "1970-01-01",
                    "gender": "female",
                }
            },
            {
                "resource": {
                    "resourceType": "Condition",
                    "id": "cond-001",
                    "code": {
                        "coding": [{"system": "http://snomed.info/sct", "code": "44054006", "display": "T2DM"}]
                    },
                    "clinicalStatus": {"coding": [{"code": "active"}]},
                    "onsetDateTime": "2020-01-01",
                }
            },
        ],
    }


class _FakePatientRecord:
    def __init__(self, pid, bundle):
        self.patient_ref_id = pid
        self.fhir_bundle = bundle
        self.source_track = "synthea"
        self.wearable_data = []
        self.behavioral_signals = []


# ── P1: IngestionPipeline.run() returns IngestionResult with status ──
@pytest.mark.asyncio
async def test_run_returns_result():
    pool, conn = _make_mock_pool()
    conn.fetchrow.return_value = None  # freshness check: never ingested

    bundle = _make_fake_bundle()
    fake_record = _FakePatientRecord("test-patient-001", bundle)

    with patch("ingestion.pipeline.SyntheaAdapter") as MockAdapter:
        instance = MockAdapter.return_value
        instance.load_all_patients = AsyncMock(return_value=[fake_record])

        pipeline = IngestionPipeline(adapter_name="synthea", pool=pool)

        with patch("ingestion.pipeline.ConflictResolver") as MockCR:
            MockCR.apply = MagicMock(return_value=[])
            with patch("transforms.fhir_to_schema.transform_by_type", return_value=[]):
                result = await pipeline.run("test-patient-001", force_refresh=True)

    assert isinstance(result, IngestionResult)
    assert hasattr(result, "status")


# ── P2: force_refresh=False skips when source_freshness not stale ���─
@pytest.mark.asyncio
async def test_skip_when_fresh():
    pool, conn = _make_mock_pool()
    # Simulate fresh data (not stale)
    from datetime import datetime
    conn.fetchrow.return_value = {
        "last_ingested_at": datetime.utcnow(),
        "ttl_hours": 24,
    }

    pipeline = IngestionPipeline(adapter_name="synthea", pool=pool)
    result = await pipeline.run("test-patient-001", force_refresh=False)
    assert result.status == "skipped_fresh"


# ── P3: force_refresh=True always fetches ──
@pytest.mark.asyncio
async def test_force_refresh():
    pool, conn = _make_mock_pool()
    from datetime import datetime
    conn.fetchrow.return_value = {
        "last_ingested_at": datetime.utcnow(),
        "ttl_hours": 24,
    }

    bundle = _make_fake_bundle()
    fake_record = _FakePatientRecord("test-patient-001", bundle)

    with patch("ingestion.pipeline.SyntheaAdapter") as MockAdapter:
        instance = MockAdapter.return_value
        instance.load_all_patients = AsyncMock(return_value=[fake_record])

        pipeline = IngestionPipeline(adapter_name="synthea", pool=pool)

        with patch("ingestion.pipeline.ConflictResolver") as MockCR:
            MockCR.apply = MagicMock(return_value=[])
            with patch("transforms.fhir_to_schema.transform_by_type", return_value=[]):
                result = await pipeline.run("test-patient-001", force_refresh=True)

    assert result.status != "skipped_fresh"


# ── P4: raw_fhir_cache INSERT called before transform_by_type ──
@pytest.mark.asyncio
async def test_cache_before_transform():
    pool, conn = _make_mock_pool()
    conn.fetchrow.return_value = None

    bundle = _make_fake_bundle()
    fake_record = _FakePatientRecord("test-patient-001", bundle)

    call_order = []

    original_execute = conn.execute

    async def tracking_execute(*args, **kwargs):
        sql = args[0] if args else ""
        if "raw_fhir_cache" in sql:
            call_order.append("cache")
        return await original_execute(*args, **kwargs)

    conn.execute = AsyncMock(side_effect=tracking_execute)

    def tracking_transform(*args, **kwargs):
        call_order.append("transform")
        return []

    with patch("ingestion.pipeline.SyntheaAdapter") as MockAdapter:
        instance = MockAdapter.return_value
        instance.load_all_patients = AsyncMock(return_value=[fake_record])

        pipeline = IngestionPipeline(adapter_name="synthea", pool=pool)

        with patch("ingestion.pipeline.ConflictResolver") as MockCR:
            MockCR.apply = MagicMock(return_value=[])
            with patch("transforms.fhir_to_schema.transform_by_type", side_effect=tracking_transform):
                await pipeline.run("test-patient-001", force_refresh=True)

    assert "cache" in call_order, "raw_fhir_cache was never written"
    assert "transform" in call_order, "transform_by_type was never called"
    cache_idx = call_order.index("cache")
    transform_idx = call_order.index("transform")
    assert cache_idx < transform_idx, "Cache must happen before transform"


# ── P5: transform_by_type called with correct resource_type ──
@pytest.mark.asyncio
async def test_transform_called_with_resource_type():
    pool, conn = _make_mock_pool()
    conn.fetchrow.return_value = None

    bundle = _make_fake_bundle()
    fake_record = _FakePatientRecord("test-patient-001", bundle)

    transform_calls = []

    def capture_transform(resource_type, resources, patient_id, source="synthea"):
        transform_calls.append(resource_type)
        return []

    with patch("ingestion.pipeline.SyntheaAdapter") as MockAdapter:
        instance = MockAdapter.return_value
        instance.load_all_patients = AsyncMock(return_value=[fake_record])

        pipeline = IngestionPipeline(adapter_name="synthea", pool=pool)

        with patch("ingestion.pipeline.ConflictResolver") as MockCR:
            MockCR.apply = MagicMock(return_value=[])
            with patch("transforms.fhir_to_schema.transform_by_type", side_effect=capture_transform):
                await pipeline.run("test-patient-001", force_refresh=True)

    # Pipeline maps Patient→summary, Condition→conditions
    assert "summary" in transform_calls, f"Expected 'summary' in calls: {transform_calls}"
    assert "conditions" in transform_calls, f"Expected 'conditions' in calls: {transform_calls}"


# ── P6: ConflictResolver.apply called with policy='patient_first' ──
@pytest.mark.asyncio
async def test_conflict_resolver_called():
    pool, conn = _make_mock_pool()
    conn.fetchrow.return_value = None

    bundle = _make_fake_bundle()
    fake_record = _FakePatientRecord("test-patient-001", bundle)

    with patch("ingestion.pipeline.SyntheaAdapter") as MockAdapter:
        instance = MockAdapter.return_value
        instance.load_all_patients = AsyncMock(return_value=[fake_record])

        pipeline = IngestionPipeline(adapter_name="synthea", pool=pool)

        with patch("ingestion.pipeline.ConflictResolver") as MockCR:
            MockCR.apply = MagicMock(return_value=[])
            with patch("transforms.fhir_to_schema.transform_by_type", return_value=[{"test": 1}]):
                await pipeline.run("test-patient-001", force_refresh=True)

            MockCR.apply.assert_called_once()
            args, kwargs = MockCR.apply.call_args
            assert kwargs.get("policy") == "patient_first" or (
                len(args) >= 2 and args[1] == "patient_first"
            ), f"Expected policy='patient_first', got: {MockCR.apply.call_args}"


# ── P7: Warehouse write uses ON CONFLICT ──
@pytest.mark.asyncio
async def test_warehouse_write_on_conflict():
    pool, conn = _make_mock_pool()
    conn.fetchrow.return_value = None

    bundle = _make_fake_bundle()
    fake_record = _FakePatientRecord("test-patient-001", bundle)

    # Return a record that will trigger a warehouse write
    fake_records = [{
        "id": "rec-001",
        "patient_id": "test-patient-001",
        "metric_type": "bp_systolic",
        "value": 120.0,
        "unit": "mmHg",
        "measured_at": None,
        "data_source": "synthea",
    }]

    with patch("ingestion.pipeline.SyntheaAdapter") as MockAdapter:
        instance = MockAdapter.return_value
        instance.load_all_patients = AsyncMock(return_value=[fake_record])

        pipeline = IngestionPipeline(adapter_name="synthea", pool=pool)

        with patch("ingestion.pipeline.ConflictResolver") as MockCR:
            MockCR.apply = MagicMock(return_value=fake_records)
            with patch("transforms.fhir_to_schema.transform_by_type", return_value=fake_records):
                await pipeline.run("test-patient-001", force_refresh=True)

    # Check that at least one execute call contains ON CONFLICT
    sql_calls = [str(c) for c in conn.execute.call_args_list]
    on_conflict_found = any("ON CONFLICT" in s for s in sql_calls)
    assert on_conflict_found, f"No ON CONFLICT in SQL calls: {sql_calls[:3]}"


# ── P8: source_freshness updated after successful run ──
@pytest.mark.asyncio
async def test_freshness_updated():
    pool, conn = _make_mock_pool()
    conn.fetchrow.return_value = None

    bundle = _make_fake_bundle()
    fake_record = _FakePatientRecord("test-patient-001", bundle)

    with patch("ingestion.pipeline.SyntheaAdapter") as MockAdapter:
        instance = MockAdapter.return_value
        instance.load_all_patients = AsyncMock(return_value=[fake_record])

        pipeline = IngestionPipeline(adapter_name="synthea", pool=pool)

        with patch("ingestion.pipeline.ConflictResolver") as MockCR:
            MockCR.apply = MagicMock(return_value=[])
            with patch("transforms.fhir_to_schema.transform_by_type", return_value=[]):
                await pipeline.run("test-patient-001", force_refresh=True)

    sql_calls = [str(c) for c in conn.execute.call_args_list]
    freshness_updated = any("source_freshness" in s for s in sql_calls)
    assert freshness_updated, "source_freshness was not updated"

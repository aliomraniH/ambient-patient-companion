"""A1-A8: Adapter and conflict resolver tests.

No database required — these test parse_bundle(), load_all_patients(),
and ConflictResolver.apply() using in-memory data only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure project root and mcp-server are on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "mcp-server"))

from ingestion.adapters.base import PatientRecord
from ingestion.adapters.synthea import SyntheaAdapter
from ingestion.conflict_resolver import ConflictResolver


def _minimal_bundle(patient_id: str = "patient-001") -> dict:
    """Create a minimal valid FHIR Bundle."""
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {
                "resource": {
                    "resourceType": "Patient",
                    "id": patient_id,
                    "identifier": [
                        {
                            "type": {"coding": [{"code": "MR"}]},
                            "value": f"MRN-{patient_id}",
                        }
                    ],
                    "name": [{"family": "Doe", "given": ["Jane"]}],
                    "birthDate": "1980-05-15",
                    "gender": "female",
                }
            },
            {
                "resource": {
                    "resourceType": "Condition",
                    "id": "cond-001",
                    "code": {
                        "coding": [
                            {"system": "http://snomed.info/sct", "code": "44054006", "display": "T2DM"}
                        ]
                    },
                    "clinicalStatus": {"coding": [{"code": "active"}]},
                    "onsetDateTime": "2020-01-01",
                }
            },
        ],
    }


# ── A1: parse_bundle returns PatientRecord instance ──
@pytest.mark.asyncio
async def test_parse_bundle_returns_patient_record():
    adapter = SyntheaAdapter()
    record = await adapter.parse_bundle(_minimal_bundle())
    assert isinstance(record, PatientRecord)


# ── A2: parse_bundle(augment_wearables=True) calls vitals generators ──
@pytest.mark.asyncio
async def test_parse_bundle_wearable_data():
    adapter = SyntheaAdapter()
    record = await adapter.parse_bundle(_minimal_bundle(), augment_wearables=True)
    assert isinstance(record.wearable_data, list)
    assert len(record.wearable_data) > 0, "wearable_data should not be empty"


# ── A3: parse_bundle(augment_behavioral=True) calls behavioral generators ──
@pytest.mark.asyncio
async def test_parse_bundle_behavioral_signals():
    adapter = SyntheaAdapter()
    record = await adapter.parse_bundle(_minimal_bundle(), augment_behavioral=True)
    assert isinstance(record.behavioral_signals, list)
    assert len(record.behavioral_signals) > 0, "behavioral_signals should not be empty"


# ── A4: parse_bundle raises ValueError on empty entry list ──
@pytest.mark.asyncio
async def test_parse_bundle_empty_raises():
    adapter = SyntheaAdapter()
    with pytest.raises(ValueError, match="Missing Patient"):
        await adapter.parse_bundle({"resourceType": "Bundle", "entry": []})


# ── A5: PatientRecord.source_track == 'synthea' ──
@pytest.mark.asyncio
async def test_source_track_synthea():
    adapter = SyntheaAdapter()
    record = await adapter.parse_bundle(_minimal_bundle())
    assert record.source_track == "synthea"


# ── A6: load_all_patients returns list of PatientRecord ──
@pytest.mark.asyncio
async def test_load_all_patients():
    with tempfile.TemporaryDirectory() as tmpdir:
        fhir_dir = os.path.join(tmpdir, "fhir")
        os.makedirs(fhir_dir)

        # Create 3 fixture files
        for i in range(3):
            bundle = _minimal_bundle(patient_id=f"patient-{i:03d}")
            filepath = os.path.join(fhir_dir, f"patient_{i}.json")
            with open(filepath, "w") as f:
                json.dump(bundle, f)

        adapter = SyntheaAdapter(output_dir=tmpdir)
        patients = await adapter.load_all_patients(directory=tmpdir)
        assert isinstance(patients, list)
        assert len(patients) == 3
        for p in patients:
            assert isinstance(p, PatientRecord)


# ── A7: ConflictResolver.apply() exists and is callable ──
def test_conflict_resolver_apply_exists():
    assert hasattr(ConflictResolver, "apply")
    assert callable(ConflictResolver.apply)


# ── A8: ConflictResolver.apply() patient-reported beats synthea ──
def test_conflict_resolver_priority():
    records = [
        {
            "_table": "patients",
            "_conflict_key": ["mrn"],
            "mrn": "MRN-001",
            "first_name": "Alice",
            "data_source": "synthea",
        },
        {
            "_table": "patients",
            "_conflict_key": ["mrn"],
            "mrn": "MRN-001",
            "first_name": "Alice-Manual",
            "data_source": "manual",
        },
    ]
    result = ConflictResolver.apply(records, policy="patient_first")
    assert len(result) == 1
    assert result[0]["data_source"] == "manual", (
        f"Expected manual to win, got {result[0]['data_source']}"
    )

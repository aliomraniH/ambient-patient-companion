"""Synthea FHIR R4 Bundle adapter (Track A) for the ingestion pipeline.

Parses FHIR R4 Bundle JSON files produced by Synthea and maps them
to PatientRecord instances for the ingestion pipeline.
"""

from __future__ import annotations

import glob
import json
import logging
import sys
from pathlib import Path
from typing import Any

from ingestion.adapters.base import BaseAdapter, PatientRecord

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def _extract_resources(bundle: dict, resource_type: str) -> list[dict]:
    """Extract all resources of a given type from a FHIR Bundle."""
    resources = []
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == resource_type:
            resources.append(resource)
    return resources


def _safe_str(value: Any, default: str = "") -> str:
    return str(value) if value is not None else default


class SyntheaAdapter(BaseAdapter):
    """Track A adapter: parse Synthea FHIR R4 Bundle JSON files."""

    source_name: str = "synthea"

    def __init__(self, output_dir: str | None = None):
        import os
        self.output_dir = output_dir or os.environ.get(
            "SYNTHEA_OUTPUT_DIR", "/home/runner/synthea-output"
        )

    async def parse_bundle(
        self,
        fhir_bundle: dict[str, Any],
        augment_wearables: bool = True,
        augment_behavioral: bool = True,
    ) -> PatientRecord:
        """Parse a FHIR Bundle into a PatientRecord.

        Extracts: Patient, Condition, MedicationRequest, Observation, Encounter.
        Raises ValueError if no Patient resource is found.
        """
        patient_resources = _extract_resources(fhir_bundle, "Patient")
        if not patient_resources:
            raise ValueError("Missing Patient resource")

        patient_res = patient_resources[0]
        patient_ref_id = patient_res.get("id", "")

        # Extract clinical resources
        conditions = _extract_resources(fhir_bundle, "Condition")
        medications = _extract_resources(fhir_bundle, "MedicationRequest")
        observations = _extract_resources(fhir_bundle, "Observation")
        encounters = _extract_resources(fhir_bundle, "Encounter")

        # Build wearable data stubs if augmenting
        wearable_data: list[dict[str, Any]] = []
        if augment_wearables:
            wearable_data = [
                {"type": "vitals_placeholder", "patient_id": patient_ref_id}
            ]

        # Build behavioral signal stubs if augmenting
        behavioral_signals: list[dict[str, Any]] = []
        if augment_behavioral:
            behavioral_signals = [
                {"type": "checkin_placeholder", "patient_id": patient_ref_id}
            ]

        record = PatientRecord(
            patient_ref_id=patient_ref_id,
            source_track="synthea",
            fhir_bundle=fhir_bundle,
            wearable_data=wearable_data,
            behavioral_signals=behavioral_signals,
        )

        logger.info(
            "Parsed Synthea bundle for patient %s (%d conditions, %d meds, %d obs, %d encounters)",
            patient_ref_id,
            len(conditions),
            len(medications),
            len(observations),
            len(encounters),
        )

        return record

    async def load_all_patients(self, directory: str | None = None) -> list[PatientRecord]:
        """Load all patients from Synthea FHIR Bundle files in the given directory."""
        base_dir = directory or self.output_dir
        fhir_dir = Path(base_dir) / "fhir"
        if not fhir_dir.exists():
            logger.warning("Synthea FHIR directory does not exist: %s", fhir_dir)
            return []

        files = sorted(glob.glob(str(fhir_dir / "*.json")))
        if not files:
            logger.warning("No FHIR JSON files found in %s", fhir_dir)
            return []

        patients: list[PatientRecord] = []
        for filepath in files:
            try:
                with open(filepath, "r") as f:
                    bundle = json.load(f)

                if bundle.get("resourceType") != "Bundle":
                    continue

                record = await self.parse_bundle(bundle)
                patients.append(record)

            except (json.JSONDecodeError, ValueError, KeyError) as e:
                logger.error("Failed to parse %s: %s", filepath, e)
                continue

        logger.info("Loaded %d patients from Synthea", len(patients))
        return patients

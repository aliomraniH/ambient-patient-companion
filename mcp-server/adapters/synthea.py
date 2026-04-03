"""Synthea FHIR R4 Bundle adapter (Track A).

Parses FHIR R4 Bundle JSON files produced by Synthea and maps them
to PatientRecord dataclass instances.
"""

from __future__ import annotations

import glob
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from adapters.base import BaseAdapter, PatientRecord
from config import SYNTHEA_OUTPUT_DIR

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def _safe_str(value, default: str = "") -> str:
    return str(value) if value is not None else default


def _parse_date(date_str: str | None) -> date | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        try:
            return date.fromisoformat(date_str[:10])
        except (ValueError, AttributeError):
            return None


def _extract_resources(bundle: dict, resource_type: str) -> list[dict]:
    """Extract all resources of a given type from a FHIR Bundle."""
    resources = []
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == resource_type:
            resources.append(resource)
    return resources


def _parse_patient_resource(resource: dict) -> dict:
    """Extract patient demographic fields from a FHIR Patient resource."""
    name = {}
    if resource.get("name"):
        name = resource["name"][0]

    address = {}
    if resource.get("address"):
        address = resource["address"][0]

    # Race and ethnicity from extensions
    race = ""
    ethnicity = ""
    for ext in resource.get("extension", []):
        url = ext.get("url", "")
        if "us-core-race" in url:
            for sub_ext in ext.get("extension", []):
                if sub_ext.get("url") == "text":
                    race = _safe_str(sub_ext.get("valueString"))
        elif "us-core-ethnicity" in url:
            for sub_ext in ext.get("extension", []):
                if sub_ext.get("url") == "text":
                    ethnicity = _safe_str(sub_ext.get("valueString"))

    # MRN from identifiers
    mrn = ""
    for identifier in resource.get("identifier", []):
        id_type = identifier.get("type", {})
        for coding in id_type.get("coding", []):
            if coding.get("code") == "MR":
                mrn = _safe_str(identifier.get("value"))
                break
        if mrn:
            break

    return {
        "id": resource.get("id", ""),
        "mrn": mrn,
        "first_name": _safe_str(name.get("given", [""])[0] if name.get("given") else ""),
        "last_name": _safe_str(name.get("family", "")),
        "birth_date": _parse_date(resource.get("birthDate")),
        "gender": _safe_str(resource.get("gender")),
        "race": race,
        "ethnicity": ethnicity,
        "address_line": " ".join(address.get("line", [])),
        "city": _safe_str(address.get("city")),
        "state": _safe_str(address.get("state")),
        "zip_code": _safe_str(address.get("postalCode")),
    }


def _parse_conditions(resources: list[dict]) -> list[dict]:
    """Parse Condition resources into simplified dicts."""
    conditions = []
    for r in resources:
        code = r.get("code", {})
        coding = code.get("coding", [{}])[0] if code.get("coding") else {}
        conditions.append({
            "code": coding.get("code", ""),
            "display": coding.get("display", ""),
            "system": coding.get("system", ""),
            "onset_date": _parse_date(r.get("onsetDateTime")),
            "clinical_status": (
                r.get("clinicalStatus", {})
                .get("coding", [{}])[0]
                .get("code", "")
            ),
        })
    return conditions


def _parse_medications(resources: list[dict]) -> list[dict]:
    """Parse MedicationRequest resources into simplified dicts."""
    medications = []
    for r in resources:
        med_code = r.get("medicationCodeableConcept", {})
        coding = med_code.get("coding", [{}])[0] if med_code.get("coding") else {}
        medications.append({
            "code": coding.get("code", ""),
            "display": coding.get("display", ""),
            "system": coding.get("system", ""),
            "status": r.get("status", ""),
            "authored_on": _parse_date(r.get("authoredOn")),
        })
    return medications


def _parse_allergies(resources: list[dict]) -> list[dict]:
    """Parse AllergyIntolerance resources."""
    allergies = []
    for r in resources:
        code = r.get("code", {})
        coding = code.get("coding", [{}])[0] if code.get("coding") else {}
        allergies.append({
            "code": coding.get("code", ""),
            "display": coding.get("display", ""),
            "clinical_status": (
                r.get("clinicalStatus", {})
                .get("coding", [{}])[0]
                .get("code", "")
            ),
        })
    return allergies


class SyntheaAdapter(BaseAdapter):
    """Track A adapter: parse Synthea FHIR R4 Bundle JSON files."""

    def __init__(self, output_dir: str | None = None):
        self.output_dir = output_dir or SYNTHEA_OUTPUT_DIR

    def load_patients(self) -> list[PatientRecord]:
        """Load all patients from Synthea FHIR Bundle files."""
        fhir_dir = Path(self.output_dir) / "fhir"
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

                patient_resources = _extract_resources(bundle, "Patient")
                if not patient_resources:
                    continue

                patient_data = _parse_patient_resource(patient_resources[0])
                conditions = _parse_conditions(
                    _extract_resources(bundle, "Condition")
                )
                medications = _parse_medications(
                    _extract_resources(bundle, "MedicationRequest")
                )
                allergies = _parse_allergies(
                    _extract_resources(bundle, "AllergyIntolerance")
                )

                record = PatientRecord(
                    id=patient_data["id"],
                    mrn=patient_data["mrn"],
                    first_name=patient_data["first_name"],
                    last_name=patient_data["last_name"],
                    birth_date=patient_data["birth_date"],
                    gender=patient_data["gender"],
                    race=patient_data["race"],
                    ethnicity=patient_data["ethnicity"],
                    address_line=patient_data["address_line"],
                    city=patient_data["city"],
                    state=patient_data["state"],
                    zip_code=patient_data["zip_code"],
                    conditions=conditions,
                    medications=medications,
                    allergies=allergies,
                    is_synthetic=True,
                )
                patients.append(record)
                logger.info("Loaded patient: %s %s", record.first_name, record.last_name)

            except (json.JSONDecodeError, KeyError, IndexError) as e:
                logger.error("Failed to parse %s: %s", filepath, e)
                continue

        logger.info("Loaded %d patients from Synthea", len(patients))
        return patients

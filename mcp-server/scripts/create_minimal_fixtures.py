"""Generate minimal FHIR R4 Bundle JSON files for testing when Synthea/Java is unavailable.

Each bundle contains:
  - 1 Patient resource with id, name, birthDate, gender, identifier (MRN)
  - 1 Condition resource (Type 2 diabetes, ICD-10 E11)
  - 1 MedicationRequest resource (Metformin)

Usage:
    python mcp-server/scripts/create_minimal_fixtures.py --count 10
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import date, timedelta


def _make_patient(seed: int) -> dict:
    pid = str(uuid.UUID(int=seed))
    first_names = [
        "Alice", "Bob", "Carol", "David", "Eve",
        "Frank", "Grace", "Hank", "Ivy", "Jack",
    ]
    last_names = [
        "Smith", "Jones", "Williams", "Brown", "Davis",
        "Miller", "Wilson", "Moore", "Taylor", "Anderson",
    ]
    first = first_names[seed % len(first_names)]
    last = last_names[seed % len(last_names)]
    dob = date(1950, 1, 1) + timedelta(days=seed * 3650 % 18000)
    gender = "female" if seed % 2 == 0 else "male"
    mrn = f"MRN-SYNTH-{seed:04d}"

    return {
        "resource": {
            "resourceType": "Patient",
            "id": pid,
            "identifier": [
                {
                    "type": {
                        "coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MR"}]
                    },
                    "value": mrn,
                }
            ],
            "name": [{"family": last, "given": [first]}],
            "birthDate": dob.isoformat(),
            "gender": gender,
            "address": [
                {
                    "line": [f"{100 + seed} Main St"],
                    "city": "Boston",
                    "state": "Massachusetts",
                    "postalCode": f"0211{seed % 10}",
                }
            ],
            "extension": [
                {
                    "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race",
                    "extension": [
                        {"url": "text", "valueString": "White"},
                    ],
                },
                {
                    "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity",
                    "extension": [
                        {"url": "text", "valueString": "Not Hispanic or Latino"},
                    ],
                },
            ],
        },
        "fullUrl": f"urn:uuid:{pid}",
    }


def _make_condition(patient_id: str) -> dict:
    return {
        "resource": {
            "resourceType": "Condition",
            "id": str(uuid.uuid4()),
            "subject": {"reference": f"urn:uuid:{patient_id}"},
            "code": {
                "coding": [
                    {
                        "system": "http://snomed.info/sct",
                        "code": "44054006",
                        "display": "Type 2 diabetes mellitus",
                    },
                    {
                        "system": "http://hl7.org/fhir/sid/icd-10-cm",
                        "code": "E11",
                        "display": "Type 2 diabetes mellitus",
                    },
                ],
                "text": "Type 2 diabetes mellitus",
            },
            "clinicalStatus": {
                "coding": [
                    {"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}
                ]
            },
            "onsetDateTime": "2020-03-15",
        },
        "fullUrl": f"urn:uuid:{uuid.uuid4()}",
    }


def _make_medication(patient_id: str) -> dict:
    return {
        "resource": {
            "resourceType": "MedicationRequest",
            "id": str(uuid.uuid4()),
            "subject": {"reference": f"urn:uuid:{patient_id}"},
            "status": "active",
            "intent": "order",
            "medicationCodeableConcept": {
                "coding": [
                    {
                        "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                        "code": "860975",
                        "display": "Metformin hydrochloride 500 MG Oral Tablet",
                    }
                ],
                "text": "Metformin hydrochloride 500 MG",
            },
            "authoredOn": "2020-03-15",
        },
        "fullUrl": f"urn:uuid:{uuid.uuid4()}",
    }


def _make_observation(patient_id: str, seed: int) -> dict:
    """Create a basic vital-sign Observation resource."""
    return {
        "resource": {
            "resourceType": "Observation",
            "id": str(uuid.uuid4()),
            "status": "final",
            "category": [
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "vital-signs",
                        }
                    ]
                }
            ],
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "85354-9",
                        "display": "Blood pressure panel",
                    }
                ]
            },
            "subject": {"reference": f"urn:uuid:{patient_id}"},
            "effectiveDateTime": "2024-01-15T08:00:00Z",
            "component": [
                {
                    "code": {
                        "coding": [{"system": "http://loinc.org", "code": "8480-6", "display": "Systolic blood pressure"}]
                    },
                    "valueQuantity": {"value": 130 + seed % 20, "unit": "mmHg"},
                },
                {
                    "code": {
                        "coding": [{"system": "http://loinc.org", "code": "8462-4", "display": "Diastolic blood pressure"}]
                    },
                    "valueQuantity": {"value": 80 + seed % 10, "unit": "mmHg"},
                },
            ],
        },
        "fullUrl": f"urn:uuid:{uuid.uuid4()}",
    }


def _make_encounter(patient_id: str) -> dict:
    return {
        "resource": {
            "resourceType": "Encounter",
            "id": str(uuid.uuid4()),
            "status": "finished",
            "class": {
                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                "code": "AMB",
                "display": "ambulatory",
            },
            "type": [
                {
                    "coding": [
                        {
                            "system": "http://snomed.info/sct",
                            "code": "185347001",
                            "display": "Encounter for problem",
                        }
                    ],
                    "text": "Encounter for problem",
                }
            ],
            "subject": {"reference": f"urn:uuid:{patient_id}"},
            "period": {"start": "2024-01-15T08:00:00Z", "end": "2024-01-15T08:30:00Z"},
        },
        "fullUrl": f"urn:uuid:{uuid.uuid4()}",
    }


def create_bundle(seed: int) -> dict:
    patient_entry = _make_patient(seed)
    patient_id = patient_entry["resource"]["id"]

    entries = [
        patient_entry,
        _make_condition(patient_id),
        _make_medication(patient_id),
        _make_observation(patient_id, seed),
        _make_encounter(patient_id),
    ]

    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": entries,
    }


def main():
    parser = argparse.ArgumentParser(description="Create minimal FHIR fixture bundles")
    parser.add_argument("--count", type=int, default=10, help="Number of patient bundles")
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("SYNTHEA_OUTPUT_DIR", "/home/runner/synthea-output"),
        help="Output base directory",
    )
    args = parser.parse_args()

    fhir_dir = os.path.join(args.output_dir, "fhir")
    os.makedirs(fhir_dir, exist_ok=True)

    for i in range(args.count):
        bundle = create_bundle(seed=i + 1)
        patient_name = bundle["entry"][0]["resource"]["name"][0]
        filename = f"{patient_name['given'][0]}_{patient_name['family']}.json"
        filepath = os.path.join(fhir_dir, filename)

        with open(filepath, "w") as f:
            json.dump(bundle, f, indent=2)

    files = os.listdir(fhir_dir)
    json_files = [f for f in files if f.endswith(".json")]
    for jf in sorted(json_files):
        sys_msg = f"  {jf}"
        # Write to stderr to avoid MCP rule violation
        import sys
        sys.stderr.write(sys_msg + "\n")
    sys.stderr.write(f"Created {len(json_files)} FHIR bundles in {fhir_dir}\n")


if __name__ == "__main__":
    main()

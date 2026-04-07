"""
json_dict_parser.py — Parse custom JSON dict-with-arrays format.

This handles payloads like {"conditions": [...], "medications": [...]}
constructed by Claude or returned by some HealthEx tool variations.
Maps flexible key names to canonical HealthEx native dict fields.
"""


# All known key aliases per resource type
_KEY_ALIASES = {
    "conditions": ["conditions", "Conditions", "problems", "diagnoses"],
    "medications": ["medications", "Medications", "drugs", "prescriptions"],
    "labs": ["labs", "labResults", "lab_results", "observations", "Labs", "results"],
    "encounters": ["encounters", "visits", "Encounters", "Visits", "appointments"],
    "immunizations": ["immunizations", "Immunizations", "vaccines"],
}


def parse_json_dict_arrays(payload: dict, resource_type: str) -> list[dict]:
    """Parse JSON dict with array values into HealthEx native dicts."""
    # Find the right array from the payload
    items: list = []

    # Check for "_items" key (set by format_detector for bare JSON arrays)
    if "_items" in payload:
        items = payload["_items"]
    else:
        for key in _KEY_ALIASES.get(resource_type, []):
            if key in payload and isinstance(payload[key], list):
                items = payload[key]
                break

        # Fallback: any list value in the dict
        if not items:
            for val in payload.values():
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    items = val
                    break

    rows: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        native = _item_to_native(item, resource_type)
        if native:
            rows.append(native)

    return rows


def _item_to_native(item: dict, resource_type: str) -> dict | None:
    """Convert a single JSON dict item to a HealthEx native dict."""

    if resource_type == "conditions":
        name = (
            item.get("name") or item.get("display")
            or item.get("description") or item.get("condition") or ""
        )
        if not name:
            return None
        return {
            "name": name,
            "icd10": item.get("icd10", ""),
            "code": item.get("snomed", "") or item.get("code", "") or item.get("icd10", ""),
            "status": item.get("status", "active"),
            "onset_date": (
                item.get("onset") or item.get("onset_date")
                or item.get("diagnosed_date") or ""
            ),
        }

    elif resource_type == "labs":
        name = (
            item.get("name") or item.get("test_name")
            or item.get("display") or ""
        )
        if not name:
            return None
        return {
            "name": name,
            "test_name": name,
            "value": str(item.get("value", "")),
            "unit": item.get("unit", item.get("units", "")),
            "date": (
                item.get("date") or item.get("effectiveDateTime")
                or item.get("collected_date") or item.get("resulted_date") or ""
            ),
            "code": item.get("loinc", item.get("code", "")),
        }

    elif resource_type == "medications":
        name = (
            item.get("name") or item.get("display")
            or item.get("drug_name") or ""
        )
        if not name:
            return None
        return {
            "name": name,
            "display": name,
            "status": item.get("status", "active"),
            "start_date": (
                item.get("start_date") or item.get("authoredOn")
                or item.get("prescribed_date") or item.get("last_seen") or ""
            ),
        }

    elif resource_type == "encounters":
        date = (
            item.get("date") or item.get("encounter_date")
            or item.get("visit_date") or item.get("start_date") or ""
        )
        if not date:
            return None
        visit_type = (
            item.get("type") or item.get("encounter_type")
            or item.get("visit_type") or "encounter"
        )
        return {
            "type": visit_type,
            "encounter_type": visit_type,
            "date": date,
            "encounter_date": date,
        }

    elif resource_type == "immunizations":
        name = (
            item.get("name") or item.get("vaccine_name")
            or item.get("display") or ""
        )
        if not name:
            return None
        return {
            "name": name,
            "vaccine_name": name,
            "date": item.get("date", ""),
            "status": item.get("status", "completed"),
        }

    return None

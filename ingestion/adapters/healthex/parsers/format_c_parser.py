"""
format_c_parser.py — Parse HealthEx flat FHIR text (Format C).

Format C looks like:
  resourceType is Observation. id is fC2IoULh.... status is final.
  code.coding[0].system is http://loinc.org. code.coding[0].code is 4548-4.
  code.text is Hemoglobin A1c. valueQuantity.value is 4.8.
  valueQuantity.unit is %. effectiveDateTime is 2025-07-11...

This is serialised as flat "key is value" sentences separated by ". ".
We split on sentence boundaries and reconstruct nested FHIR dicts,
then wrap in a Bundle so downstream code can process it via the existing
_explode_fhir_bundle → _normalize_to_fhir path.
"""
import re


def parse_flat_fhir_text(raw: str, resource_type: str) -> list[dict]:
    """Parse flat FHIR text into HealthEx native dicts.

    Returns a list of native dicts compatible with the
    _healthex_native_to_fhir_* converters.
    """
    # Split into individual resources (each starts with "resourceType is")
    resource_blocks = re.split(r'(?=resourceType is )', raw.strip())
    resource_blocks = [b.strip() for b in resource_blocks if b.strip()]

    rows: list[dict] = []
    for block in resource_blocks:
        fhir_dict = _block_to_dict(block)
        if not fhir_dict:
            continue

        native = _fhir_dict_to_native(fhir_dict, resource_type)
        if native:
            rows.append(native)

    return rows


def _block_to_dict(block: str) -> dict:
    """Convert a flat "key is value" text block into a nested dict."""
    # Split on ". " but preserve periods in values like "4.8"
    # Strategy: split on ". " where the next char is a letter (new key)
    pairs = re.split(r'\.\s+(?=[a-zA-Z])', block)

    result: dict = {}
    for pair in pairs:
        pair = pair.strip().rstrip(".")
        if " is " not in pair:
            continue

        key, _, value = pair.partition(" is ")
        key = key.strip()
        value = value.strip()

        _set_nested(result, key, value)

    return result


def _set_nested(d: dict, path: str, value: str) -> None:
    """Set a value in a nested dict using a dotted path with array indices.

    E.g. "code.coding[0].system" → d["code"]["coding"][0]["system"] = value
    """
    parts = re.split(r'\.(?![^[]*\])', path)
    current = d

    for i, part in enumerate(parts):
        # Check for array index: "coding[0]"
        arr_match = re.match(r'(.+)\[(\d+)\]$', part)

        if i == len(parts) - 1:
            # Leaf — set value
            if arr_match:
                key, idx = arr_match.group(1), int(arr_match.group(2))
                if key not in current:
                    current[key] = []
                arr = current[key]
                while len(arr) <= idx:
                    arr.append({})
                arr[idx] = _coerce_value(value)
            else:
                current[part] = _coerce_value(value)
        else:
            # Intermediate — create container
            if arr_match:
                key, idx = arr_match.group(1), int(arr_match.group(2))
                if key not in current:
                    current[key] = []
                arr = current[key]
                while len(arr) <= idx:
                    arr.append({})
                if not isinstance(arr[idx], dict):
                    arr[idx] = {}
                current = arr[idx]
            else:
                if part not in current or not isinstance(current[part], dict):
                    current[part] = {}
                current = current[part]


def _coerce_value(value: str):
    """Coerce string values to appropriate Python types."""
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        f = float(value)
        if "." not in value:
            return int(value)
        return f
    except (ValueError, TypeError):
        pass
    return value


def _fhir_dict_to_native(fhir: dict, resource_type: str) -> dict | None:
    """Convert a reconstructed FHIR dict to a HealthEx native dict."""
    rt = fhir.get("resourceType", "")

    if resource_type == "labs" and rt == "Observation":
        code = fhir.get("code", {})
        name = (
            code.get("text")
            or _first_coding_field(code, "display")
            or _first_coding_field(code, "code")
            or ""
        )
        vq = fhir.get("valueQuantity", {})
        value = vq.get("value")
        unit = vq.get("unit", "")
        date = str(fhir.get("effectiveDateTime", ""))[:10]

        if not name:
            return None

        return {
            "name": name,
            "test_name": name,
            "value": str(value) if value is not None else "",
            "unit": unit,
            "date": date,
            "code": _first_coding_field(code, "code") or "",
        }

    elif resource_type == "conditions" and rt == "Condition":
        code = fhir.get("code", {})
        name = code.get("text") or _first_coding_field(code, "display") or ""
        status_obj = fhir.get("clinicalStatus", {})
        status = _first_coding_field(status_obj, "code") or "active"
        onset = str(fhir.get("onsetDateTime", ""))[:10]

        if not name:
            return None

        return {
            "name": name,
            "status": status,
            "onset_date": onset,
            "code": _first_coding_field(code, "code") or "",
        }

    elif resource_type == "medications" and rt in (
        "MedicationRequest", "MedicationStatement",
    ):
        med_ref = fhir.get("medicationReference", {})
        med_code = fhir.get("medicationCodeableConcept", {})
        name = med_ref.get("display") or med_code.get("text") or ""
        status = fhir.get("status", "active")
        authored = str(fhir.get("authoredOn", ""))[:10]

        if not name:
            return None

        return {
            "name": name,
            "display": name,
            "status": status,
            "start_date": authored,
        }

    elif resource_type == "encounters" and rt == "Encounter":
        period = fhir.get("period", {})
        date = str(period.get("start", ""))[:10]
        type_list = fhir.get("type", [])
        visit_type = ""
        if type_list and isinstance(type_list[0], dict):
            visit_type = _first_coding_field(type_list[0], "display") or ""

        return {
            "type": visit_type or "encounter",
            "encounter_type": visit_type or "encounter",
            "date": date,
            "encounter_date": date,
        }

    return None


def _first_coding_field(code_obj: dict, field: str) -> str | None:
    """Get the first coding entry's field value from a CodeableConcept."""
    codings = code_obj.get("coding", [])
    if codings and isinstance(codings[0], dict):
        return codings[0].get(field)
    return None

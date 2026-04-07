"""
format_d_parser.py — Parse proper FHIR R4 Bundle JSON (Format D).

This is a thin wrapper that extracts resources from a Bundle and converts
them to HealthEx native dicts.  The existing _explode_fhir_bundle() in
mcp_server.py already handles this shape; this parser exists so the
adaptive pipeline can normalise Format D payloads to the same native dict
list that all other parsers produce, before handing off to the existing
_normalize_to_fhir → transform_* path.
"""


def parse_fhir_bundle(payload: dict, resource_type: str) -> list[dict]:
    """Extract resources from a FHIR Bundle and return HealthEx native dicts."""
    entries = payload.get("entry") or []
    rows: list[dict] = []

    for entry in entries:
        resource = entry.get("resource", {})
        if not isinstance(resource, dict):
            continue

        rtype = resource.get("resourceType", "")
        native = _resource_to_native(resource, rtype, resource_type)
        if native:
            rows.append(native)

    return rows


def _resource_to_native(
    resource: dict, rtype: str, target_type: str
) -> dict | None:
    """Convert a single FHIR resource to a HealthEx native dict."""

    if target_type == "labs" and rtype == "Observation":
        return _observation_to_native(resource)
    elif target_type == "conditions" and rtype == "Condition":
        return _condition_to_native(resource)
    elif target_type == "medications" and rtype in (
        "MedicationRequest", "MedicationStatement",
    ):
        return _medication_to_native(resource)
    elif target_type == "encounters" and rtype in ("Encounter", "Appointment"):
        return _encounter_to_native(resource)
    elif target_type == "immunizations" and rtype == "Immunization":
        return _immunization_to_native(resource)
    return None


def _observation_to_native(r: dict) -> dict | None:
    code = r.get("code", {})
    name = (
        code.get("text")
        or _first_coding(code, "display")
        or _first_coding(code, "code")
        or ""
    )

    vq = r.get("valueQuantity") or {}
    value = vq.get("value")
    unit = vq.get("unit", "")
    date = (r.get("effectiveDateTime") or "")[:10]

    # Handle component-based observations (e.g. blood pressure)
    components = r.get("component", [])
    if value is None and components:
        rows = []
        for comp in components:
            comp_code = comp.get("code", {})
            comp_name = comp_code.get("text") or _first_coding(comp_code, "display") or ""
            comp_vq = comp.get("valueQuantity", {})
            if comp_vq.get("value") is not None:
                full_name = f"{name} - {comp_name}".strip(" - ") if name else comp_name
                rows.append({
                    "name": full_name,
                    "test_name": full_name,
                    "value": str(comp_vq["value"]),
                    "unit": comp_vq.get("unit", ""),
                    "date": date,
                    "code": _first_coding(comp_code, "code") or "",
                })
        # Return first component; caller handles the rest via the list return
        # Actually we need to return all — but this function returns one dict.
        # We'll use a special "_components" key to signal multiple rows.
        if rows:
            rows[0]["_extra_rows"] = rows[1:]
            return rows[0]
        return None

    if value is None:
        return None

    return {
        "name": name,
        "test_name": name,
        "value": str(value),
        "unit": unit,
        "date": date,
        "code": _first_coding(code, "code") or "",
    }


def _condition_to_native(r: dict) -> dict | None:
    code = r.get("code", {})
    name = code.get("text") or _first_coding(code, "display") or ""
    if not name:
        return None

    icd10 = _coding_by_system(code, "icd-10")
    snomed = _coding_by_system(code, "snomed")
    status_obj = r.get("clinicalStatus", {})
    status = _first_coding(status_obj, "code") or "active"
    onset = (
        r.get("onsetDateTime")
        or (r.get("onsetPeriod") or {}).get("start", "")
    )
    if onset:
        onset = onset[:10]

    return {
        "name": name,
        "icd10": icd10,
        "code": snomed or icd10,
        "status": status,
        "onset_date": onset or "",
    }


def _medication_to_native(r: dict) -> dict | None:
    med_ref = r.get("medicationReference", {})
    med_code = r.get("medicationCodeableConcept", {})
    name = med_ref.get("display") or med_code.get("text") or ""
    if not name:
        return None

    status = r.get("status", "active")
    authored = r.get("authoredOn", r.get("dateAsserted", ""))
    if authored:
        authored = authored[:10]

    dosage_list = r.get("dosageInstruction", [])
    sig = dosage_list[0].get("text", "") if dosage_list else ""

    return {
        "name": name,
        "display": name,
        "status": status,
        "start_date": authored or "",
    }


def _encounter_to_native(r: dict) -> dict | None:
    period = r.get("period", {})
    date = (period.get("start") or r.get("start") or "")
    if date:
        date = date[:10]

    type_list = r.get("type", [])
    visit_type = ""
    if type_list and isinstance(type_list[0], dict):
        visit_type = (
            type_list[0].get("text")
            or _first_coding(type_list[0], "display")
            or ""
        )

    return {
        "type": visit_type or "encounter",
        "encounter_type": visit_type or "encounter",
        "date": date,
        "encounter_date": date,
    }


def _immunization_to_native(r: dict) -> dict | None:
    code = r.get("vaccineCode", {})
    name = code.get("text") or _first_coding(code, "display") or ""
    date = (r.get("occurrenceDateTime") or "")[:10]

    return {
        "name": name,
        "vaccine_name": name,
        "date": date,
        "status": r.get("status", "completed"),
    }


def _first_coding(code_obj: dict, field: str) -> str:
    codings = code_obj.get("coding", [])
    if codings and isinstance(codings[0], dict):
        return codings[0].get(field, "")
    return ""


def _coding_by_system(code_obj: dict, system_fragment: str) -> str:
    """Find a coding entry whose system contains the given fragment."""
    for c in code_obj.get("coding", []):
        if system_fragment.lower() in c.get("system", "").lower():
            return c.get("code", "")
    return ""

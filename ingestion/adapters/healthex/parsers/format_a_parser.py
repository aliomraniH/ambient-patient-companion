"""
format_a_parser.py — Parse HealthEx plain text summary (Format A).

The summary format from get_health_summary looks like:
  PATIENT: Ali Omrani, DOB 1987-03-25
  CONDITIONS(4/10): Active: BMI 34.0-34.9,adult@Stanford 2019-01-11 | ...
  LABS(96): Hemoglobin A1c:4.8 %(ref:<5.7) 2025-07-11@Stanford[totalrecords:9] | ...
  ALLERGIES(1): No Known Allergies 2015-07-21@...
  IMMUNIZATIONS(17): Flu vaccine (IIV4) 2023-12-13@... | ...
  CLINICAL VISITS(35): Office Visit:description:Internal Medicine,diagnoses:Fatty liver ...

Each pipe-separated item is one record.  Output is a list of HealthEx native
dicts compatible with the _healthex_native_to_fhir_* converters in mcp_server.py.
"""
import re


# Rows whose test name starts with these are pure narrative — skip them
NARRATIVE_PREFIXES = {
    "Narrative", "Interpretation", "Comment", "Quantiferon Criteria",
    "QuantiFERON Incubation", "Alb/Creat Interp", "ECG Impression",
    "Fasting",
}


def _parse_lab_value(raw: str) -> tuple:
    """
    Split a lab value+unit+ref string into (result_value, result_unit, ref_range).

    Input examples:
      "4.8 %(ref:<5.7)"              -> ("4.8", "%", "<5.7")
      "98 mg/dL(ref:70-100 mg/dL)"  -> ("98", "mg/dL", "70-100 mg/dL")
      "Negative"                     -> ("Negative", "", "")
      "9.1 mIU/mL"                  -> ("9.1", "mIU/mL", "")
      "108 mL/min/1.73 m2(ref:>60)" -> ("108", "mL/min/1.73 m2", ">60")
      "<30"                          -> ("<30", "", "")
      "Non Reactive"                 -> ("Non Reactive", "", "")
    """
    raw = raw.strip()

    ref_range = ""
    ref_match = re.search(r'\(ref:([^)]+)\)', raw)
    if ref_match:
        ref_range = ref_match.group(1).strip()
        raw = raw[:ref_match.start()].strip()

    num_match = re.match(r'^([<>]?[\d.]+)\s*(.*)', raw)
    if num_match:
        result_value = num_match.group(1).strip()
        result_unit = num_match.group(2).strip()
    else:
        result_value = raw
        result_unit = ""

    return result_value, result_unit, ref_range


def _is_numeric(value: str) -> bool:
    """True if value is a numeric result (possibly prefixed with < or >)."""
    return bool(re.match(r'^[<>]?[\d.]+$', value.strip()))


def parse_labs_from_summary(raw: str) -> list:
    """
    Parse the LABS section from a Format A plain text summary.

    Returns rows with fields:
      test_name, result_value, result_unit, ref_range,
      effective_date, flag, loinc_code, source
    """
    rows = []

    content = _extract_section(raw, r'LABS\(\d+\):', _SECTION_HEADERS)
    if not content:
        return rows

    items = content.split(" | ")

    for item in items:
        item = item.strip()
        if not item:
            continue

        outer = re.match(
            r'(.+?)'
            r':'
            r'(.+?)'
            r'\s+(\d{4}-\d{2}-\d{2})'
            r'@(.+?)'
            r'(?:\[.*)?$',
            item,
            re.DOTALL,
        )
        if not outer:
            continue

        test_name = outer.group(1).strip()
        raw_value_block = outer.group(2).strip()
        effective_date = outer.group(3).strip()

        if any(test_name.startswith(p) for p in NARRATIVE_PREFIXES):
            continue

        if len(raw_value_block.split()) > 8:
            continue

        result_value, result_unit, ref_range = _parse_lab_value(raw_value_block)

        # Format A uses both "[OutOfRange]" and "OutOfRange:N" inside brackets
        flag = "out_of_range" if "OutOfRange" in item else ""

        rows.append({
            "test_name":      test_name,
            "result_value":   result_value,
            "result_unit":    result_unit,
            "ref_range":      ref_range,
            "effective_date": effective_date,
            "flag":           flag,
            "loinc_code":     "",
            "source":         "healthex_summary",
            # Backward-compat aliases so existing FHIR converters keep working
            "name":           test_name,
            "value":          result_value,
            "unit":           result_unit,
            "date":           effective_date,
            "status":         "out_of_range" if flag else "normal",
        })

    return rows


def parse_plain_text_summary(raw: str, resource_type: str) -> list:
    """Parse HealthEx plain text summary and return native dicts for the
    requested resource_type."""
    rows = []

    if resource_type == "conditions":
        rows = _parse_conditions(raw)
    elif resource_type == "labs":
        rows = parse_labs_from_summary(raw)
    elif resource_type == "encounters":
        rows = _parse_encounters(raw)
    elif resource_type == "immunizations":
        rows = _parse_immunizations(raw)
    elif resource_type == "medications":
        rows = _parse_medications(raw)

    return rows


def _extract_section(raw: str, header_pattern: str, stop_headers: list) -> str:
    """Extract the content of a section between header_pattern and the next
    section header (or end of string)."""
    stop_pattern = "|".join(re.escape(h) for h in stop_headers)
    pattern = rf'{header_pattern}\s*(.+?)(?:{stop_pattern}|$)'
    match = re.search(pattern, raw, re.DOTALL)
    return match.group(1).strip() if match else ""


_SECTION_HEADERS = [
    "PATIENT:", "PROVIDERS:", "CONDITIONS(", "LABS(", "ALLERGIES(",
    "IMMUNIZATIONS(", "CLINICAL VISITS(", "MEDICATIONS(",
]


def _parse_conditions(raw: str) -> list:
    content = _extract_section(
        raw, r'CONDITIONS\(\d+(?:/\d+)?\):', _SECTION_HEADERS
    )
    if not content:
        return []

    rows = []
    items = content.split(" | ")
    for item in items:
        item = item.strip()
        if not item:
            continue

        m = re.match(
            r'(Active|Inactive|Resolved):\s*(.+?)@(.+?)\s+(\d{4}-\d{2}-\d{2})',
            item, re.IGNORECASE,
        )
        if m:
            status_raw, name, _provider, date = m.groups()
            status = "active" if status_raw.lower() == "active" else "resolved"
            rows.append({
                "name": name.strip(),
                "status": status,
                "onset_date": date,
            })
        else:
            m2 = re.search(r'(\d{4}-\d{2}-\d{2})', item)
            name = re.sub(r'^(Active|Inactive|Resolved):\s*', '', item, flags=re.IGNORECASE)
            name = re.sub(r'@.*', '', name).strip()
            if name:
                rows.append({
                    "name": name,
                    "status": "active",
                    "onset_date": m2.group(1) if m2 else "",
                })

    return rows


def _parse_encounters(raw: str) -> list:
    content = _extract_section(
        raw, r'CLINICAL VISITS\(\d+\):', _SECTION_HEADERS
    )
    if not content:
        return []

    rows = []
    items = content.split(" | ")
    for item in items:
        item = item.strip()
        if not item:
            continue

        m = re.match(
            r'(.+?):description:(.+?),diagnoses:(.+?)\s+(\d{4}-\d{2}-\d{2})@',
            item,
        )
        if m:
            visit_type, dept, diagnoses, date = m.groups()
            rows.append({
                "type": visit_type.strip(),
                "encounter_type": visit_type.strip(),
                "date": date,
                "encounter_date": date,
            })
        else:
            m2 = re.search(r'(\d{4}-\d{2}-\d{2})', item)
            if m2:
                visit_type = item.split(":")[0].strip() if ":" in item else "encounter"
                rows.append({
                    "type": visit_type,
                    "encounter_type": visit_type,
                    "date": m2.group(1),
                    "encounter_date": m2.group(1),
                })

    return rows


def _parse_immunizations(raw: str) -> list:
    content = _extract_section(
        raw, r'IMMUNIZATIONS\(\d+\):', _SECTION_HEADERS
    )
    if not content:
        return []

    rows = []
    items = content.split(" | ")
    for item in items:
        item = item.strip()
        if not item:
            continue

        m = re.match(r'(.+?)\s+(\d{4}-\d{2}-\d{2})@', item)
        if m:
            rows.append({
                "name": m.group(1).strip(),
                "vaccine_name": m.group(1).strip(),
                "date": m.group(2),
                "status": "completed",
            })

    return rows


def _parse_medications(raw: str) -> list:
    content = _extract_section(
        raw, r'MEDICATIONS\(\d+\):', _SECTION_HEADERS
    )
    if not content:
        return []

    rows = []
    items = content.split(" | ")
    for item in items:
        item = item.strip()
        if not item:
            continue

        m = re.search(r'(\d{4}-\d{2}-\d{2})', item)
        name = re.sub(r'@.*', '', item).strip()
        name = re.sub(r'\d{4}-\d{2}-\d{2}.*', '', name).strip()
        if name:
            rows.append({
                "name": name,
                "display": name,
                "status": "active",
                "start_date": m.group(1) if m else "",
            })

    return rows

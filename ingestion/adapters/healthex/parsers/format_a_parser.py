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


def parse_plain_text_summary(raw: str, resource_type: str) -> list[dict]:
    """Parse HealthEx plain text summary and return native dicts for the
    requested resource_type."""
    rows: list[dict] = []

    if resource_type == "conditions":
        rows = _parse_conditions(raw)
    elif resource_type == "labs":
        rows = _parse_labs(raw)
    elif resource_type == "encounters":
        rows = _parse_encounters(raw)
    elif resource_type == "immunizations":
        rows = _parse_immunizations(raw)
    elif resource_type == "medications":
        rows = _parse_medications(raw)

    return rows


def _extract_section(raw: str, header_pattern: str, stop_headers: list[str]) -> str:
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


def _parse_conditions(raw: str) -> list[dict]:
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

        # Pattern: "Active: BMI 34.0-34.9,adult@Stanford Health Care 2019-01-11"
        # or "Inactive: GERD@Provider 2017-04-25"
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
            # Fallback: try to extract just a name and date
            m2 = re.search(r'(\d{4}-\d{2}-\d{2})', item)
            # Strip leading status prefix if present
            name = re.sub(r'^(Active|Inactive|Resolved):\s*', '', item, flags=re.IGNORECASE)
            name = re.sub(r'@.*', '', name).strip()
            if name:
                rows.append({
                    "name": name,
                    "status": "active",
                    "onset_date": m2.group(1) if m2 else "",
                })

    return rows


def _parse_labs(raw: str) -> list[dict]:
    content = _extract_section(raw, r'LABS\(\d+\):', _SECTION_HEADERS)
    if not content:
        return []

    rows = []
    items = content.split(" | ")
    for item in items:
        item = item.strip()
        if not item:
            continue

        # Pattern: "Hemoglobin A1c:4.8 %(ref:<5.7) 2025-07-11@Stanford[totalrecords:9]"
        # Simpler: "Test Name:value_and_unit date@Provider"
        m = re.match(r'(.+?):(.+?)\s+(\d{4}-\d{2}-\d{2})@', item)
        if m:
            test_name = m.group(1).strip()
            value_unit = m.group(2).strip()
            date = m.group(3)

            # Try to split value from unit/ref
            # e.g. "4.8 %(ref:<5.7)" → value="4.8", unit="%"
            vm = re.match(r'([\d.]+)\s*([^(]*)', value_unit)
            value = vm.group(1) if vm else value_unit
            unit = vm.group(2).strip() if vm else ""

            out_of_range = "[OutOfRange]" in item

            rows.append({
                "name": test_name,
                "test_name": test_name,
                "value": value,
                "unit": unit,
                "date": date,
                "status": "out_of_range" if out_of_range else "normal",
            })
        else:
            # Minimal fallback: just extract name if possible
            m2 = re.match(r'(.+?):', item)
            m3 = re.search(r'(\d{4}-\d{2}-\d{2})', item)
            if m2:
                rows.append({
                    "name": m2.group(1).strip(),
                    "test_name": m2.group(1).strip(),
                    "value": "",
                    "unit": "",
                    "date": m3.group(1) if m3 else "",
                })

    return rows


def _parse_encounters(raw: str) -> list[dict]:
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

        # Pattern: "Office Visit:description:Internal Medicine,diagnoses:Fatty liver 2025-06-26@Stanford"
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
            # Minimal: extract date
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


def _parse_immunizations(raw: str) -> list[dict]:
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

        # Pattern: "Flu vaccine (IIV4) 2023-12-13@Stanford"
        m = re.match(r'(.+?)\s+(\d{4}-\d{2}-\d{2})@', item)
        if m:
            rows.append({
                "name": m.group(1).strip(),
                "vaccine_name": m.group(1).strip(),
                "date": m.group(2),
                "status": "completed",
            })

    return rows


def _parse_medications(raw: str) -> list[dict]:
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

        # Pattern: "Metformin 1000mg BID@Stanford 2020-01-01"
        # or "drug_name:status date@provider"
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

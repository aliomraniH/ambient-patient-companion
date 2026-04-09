"""
format_b_parser.py — Parse HealthEx compressed dictionary table (Format B).

Format B looks like:
  #Conditions 5y|Total:39
  D:1=2019-01-11|2=2017-04-25|
  C:1=BMI 34.0-34.9,adult|2=Prediabetes|
  S:1=active|2=resolved|
  Sys:1=http://snomed.info/sct|
  Date|Condition|ClinicalStatus|OnsetDate|AbatementDate|SNOMED|ICD10|...
  @1|@1|@1|2019-01-11||162864005|Z68.34|...
  |@2|@1|2017-04-25||714628002|R73.03|...

Dictionary lines define lookup tables (D=dates, C=conditions, S=statuses).
Data rows reference entries via @N.  Empty first column means "same as
previous row" for that position.
"""
import re
from typing import Optional


def parse_compressed_table(raw: str, resource_type: str) -> list[dict]:
    """Parse HealthEx compressed dictionary table and return native dicts."""
    lines = raw.strip().split("\n")

    dicts: dict[str, dict[int, str]] = {}
    header_line: Optional[list[str]] = None
    data_lines: list[str] = []

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("Note:"):
            continue

        # Dictionary definition: "D:1=2019-01-11|2=2017-04-25|"
        dict_match = re.match(r'^([A-Za-z]+):(.+)$', line)
        if dict_match and "=" in dict_match.group(2):
            key = dict_match.group(1)
            rest = dict_match.group(2)
            mapping: dict[int, str] = {}
            for part in rest.split("|"):
                part = part.strip()
                if "=" in part:
                    idx_str, val = part.split("=", 1)
                    try:
                        mapping[int(idx_str.strip())] = val.strip()
                    except ValueError:
                        pass
            if mapping:
                dicts[key] = mapping
            continue

        # Column header line: contains pipe-separated names without @ refs
        if "|" in line and "@" not in line:
            # Heuristic: header lines contain known column names
            potential_cols = [c.strip() for c in line.split("|")]
            known_headers = {
                "Date", "Condition", "ClinicalStatus", "OnsetDate",
                "AbatementDate", "SNOMED", "ICD10", "Medication",
                "Status", "DosageInstruction", "AuthoredOn", "Code",
                "TestName", "Value", "Unit", "ReferenceRange",
                "EffectiveDate", "Encounter", "Type", "Period",
                "Vaccine", "OccurrenceDate",
            }
            if any(c in known_headers for c in potential_cols):
                header_line = potential_cols
                continue

        # Data row (contains | delimiters)
        if "|" in line:
            data_lines.append(line)

    # If no header line detected, infer from resource_type
    if not header_line:
        header_line = _default_headers(resource_type)

    if not header_line:
        return []

    # Build a mapping from column name to likely dictionary key
    col_to_dict = _build_col_dict_map(header_line, dicts)

    # Decode rows
    rows: list[dict] = []
    prev_values: dict[str, str] = {}

    for line in data_lines:
        cells = line.split("|")
        row: dict[str, str] = {}

        for i, col in enumerate(header_line):
            if i >= len(cells):
                break
            cell = cells[i].strip()

            if cell == "":
                # Carry forward previous value
                row[col] = prev_values.get(col, "")
            elif cell.startswith("@"):
                # Resolve dictionary reference
                dict_key = col_to_dict.get(col)
                row[col] = _resolve_ref(cell, dict_key, dicts)
            else:
                row[col] = cell

        if not any(row.values()):
            continue

        # Update carry-forward cache
        for k, v in row.items():
            if v:
                prev_values[k] = v

        # Map to native dict format
        native = _to_native(row, resource_type)
        if native:
            rows.append(native)

    return _deduplicate(rows, resource_type)


def _default_headers(resource_type: str) -> Optional[list[str]]:
    defaults = {
        "conditions": [
            "Date", "Condition", "ClinicalStatus", "OnsetDate",
            "AbatementDate", "SNOMED", "ICD10", "PreferredCode",
            "PreferredSystem", "Recorder", "Asserter", "Encounter",
        ],
        "medications": [
            "Date", "Medication", "Status", "DosageInstruction",
            "AuthoredOn", "Code", "System",
        ],
        "labs": [
            "Date", "TestName", "Value", "Unit", "ReferenceRange",
            "Status", "LOINC", "EffectiveDate",
        ],
        "encounters": [
            "Date", "Type", "Period", "Description", "Provider",
            "Status", "Location", "Code",
        ],
    }
    return defaults.get(resource_type)


def _build_col_dict_map(
    headers: list[str], dicts: dict[str, dict[int, str]]
) -> dict[str, Optional[str]]:
    """Map column names to their likely dictionary key letter."""
    # Well-known mappings
    known = {
        "Date": "D", "OnsetDate": "D", "AbatementDate": "D",
        "AuthoredOn": "D", "EffectiveDate": "D",
        "Condition": "C", "Medication": "C", "TestName": "C",
        "Vaccine": "C", "Type": "C",
        "ClinicalStatus": "S", "Status": "S",
        "Location": "C",
        "PreferredSystem": "Sys",
    }
    result: dict[str, Optional[str]] = {}
    for col in headers:
        if col in known and known[col] in dicts:
            result[col] = known[col]
        else:
            result[col] = None
    return result


def _resolve_ref(
    cell: str,
    dict_key: Optional[str],
    dicts: dict[str, dict[int, str]],
) -> str:
    """Resolve @N reference using the appropriate dictionary."""
    if not cell.startswith("@"):
        return cell
    try:
        idx = int(cell[1:])
    except ValueError:
        return cell

    if dict_key and dict_key in dicts:
        return dicts[dict_key].get(idx, cell)

    # Try all dictionaries as fallback
    for d in dicts.values():
        if idx in d:
            return d[idx]
    return cell


def _to_native(row: dict[str, str], resource_type: str) -> Optional[dict]:
    """Convert a decoded row to a HealthEx native dict."""
    if resource_type == "conditions":
        name = row.get("Condition", "")
        if not name or name.startswith("@"):
            return None
        return {
            "name": name,
            "icd10": row.get("ICD10", ""),
            "code": row.get("SNOMED", "") or row.get("ICD10", ""),
            "status": row.get("ClinicalStatus", "active"),
            "onset_date": row.get("OnsetDate") or row.get("Date", ""),
        }
    elif resource_type == "medications":
        name = row.get("Medication", "")
        if not name or name.startswith("@"):
            return None
        return {
            "name": name,
            "display": name,
            "status": row.get("Status", "active"),
            "start_date": row.get("AuthoredOn") or row.get("Date", ""),
        }
    elif resource_type == "labs":
        name = row.get("TestName", "")
        if not name or name.startswith("@"):
            return None
        return {
            "name": name,
            "test_name": name,
            "value": row.get("Value", ""),
            "unit": row.get("Unit", ""),
            "date": row.get("EffectiveDate") or row.get("Date", ""),
            "ref_range": row.get("ReferenceRange", ""),
            "loinc_code": row.get("LOINC", ""),
        }
    elif resource_type == "encounters":
        enc_type = row.get("Type", "") or row.get("Description", "")
        if not enc_type or enc_type.startswith("@"):
            return None
        return {
            "type": enc_type,
            "date": row.get("Date") or row.get("Period", ""),
            "description": row.get("Description", "") or enc_type,
            "provider": row.get("Provider", ""),
            "status": row.get("Status", ""),
        }
    return None


def _deduplicate(rows: list[dict], resource_type: str) -> list[dict]:
    """Deduplicate by primary key fields."""
    key_fields = {
        "conditions": ("name", "onset_date"),
        "medications": ("name", "start_date"),
        "labs": ("name", "date"),
        "encounters": ("type", "date"),
    }
    fields = key_fields.get(resource_type, ("name",))
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for r in rows:
        key = tuple(r.get(f, "") for f in fields)
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped

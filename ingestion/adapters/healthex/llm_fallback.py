"""
llm_fallback.py — LLM-based fallback normaliser for HealthEx payloads.

Called when all deterministic parsers fail or return 0 rows on non-trivial
input.  Uses Claude Sonnet to extract structured rows from arbitrary HealthEx
format.  Deterministic validation is applied after LLM extraction to avoid
the "circular validation problem" (Colombo et al. 2025).

Based on arxiv 2507.03067: including the target schema in the prompt
achieves 100% resource identification and ~70% attribute-level mapping.
"""
import json
import logging
import os
import sys

log = logging.getLogger(__name__)

# Import guardrail for PHI scanning — guarded so ingestion never crashes
# if the guardrail module is unavailable.
try:
    _server_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "server")
    if _server_dir not in sys.path:
        sys.path.insert(0, os.path.abspath(_server_dir))
    from server.guardrails.output_validator import validate_output as _guardrail_validate
    _GUARDRAIL_AVAILABLE = True
except ImportError:
    _GUARDRAIL_AVAILABLE = False

# Warehouse target schemas per resource type — included in the LLM prompt
# so the model knows exactly what fields to extract.
WAREHOUSE_SCHEMAS = {
    "labs": {
        "fields": ["test_name", "value", "unit", "date", "code"],
        "required": ["test_name", "date"],
        "example": {
            "test_name": "Hemoglobin A1c",
            "value": "4.8",
            "unit": "%",
            "date": "2025-07-11",
            "code": "4548-4",
        },
    },
    "conditions": {
        "fields": ["name", "code", "status", "onset_date"],
        "required": ["name"],
        "example": {
            "name": "Prediabetes",
            "code": "R73.03",
            "status": "active",
            "onset_date": "2017-04-25",
        },
    },
    "encounters": {
        "fields": ["encounter_date", "encounter_type", "type", "date"],
        "required": ["encounter_date"],
        "example": {
            "encounter_date": "2025-06-26",
            "encounter_type": "Office Visit",
            "type": "Office Visit",
            "date": "2025-06-26",
        },
    },
    "medications": {
        "fields": ["name", "display", "status", "start_date"],
        "required": ["name"],
        "example": {
            "name": "Pantoprazole",
            "display": "Pantoprazole",
            "status": "active",
            "start_date": "2017-03-31",
        },
    },
    "immunizations": {
        "fields": ["name", "vaccine_name", "date", "status"],
        "required": ["name", "date"],
        "example": {
            "name": "Flu vaccine (IIV4)",
            "vaccine_name": "Flu vaccine (IIV4)",
            "date": "2023-12-13",
            "status": "completed",
        },
    },
}


def _phi_scan_rows(rows: list[dict]) -> list[dict]:
    """Scan extracted rows for PHI leakage and redact flagged field values."""
    if not _GUARDRAIL_AVAILABLE or not rows:
        return rows
    scanned = []
    for row in rows:
        clean_row = {}
        for key, value in row.items():
            if not isinstance(value, str) or not value.strip():
                clean_row[key] = value
                continue
            try:
                result = _guardrail_validate(response=value)
                has_phi = any(f.startswith("PHI_LEAKAGE") for f in result.flags)
                clean_row[key] = "[REDACTED]" if has_phi else value
            except Exception:
                clean_row[key] = value
        scanned.append(clean_row)
    return scanned


def llm_normalise(raw: str, resource_type: str) -> list[dict]:
    """
    LLM fallback normaliser.  Uses Claude Sonnet to extract structured rows
    from arbitrary HealthEx format text.

    Returns a list of HealthEx native dicts (same format as deterministic
    parsers), or an empty list on failure.
    """
    schema = WAREHOUSE_SCHEMAS.get(resource_type)
    if not schema:
        return []

    # Truncate to avoid token overflow — 8000 chars is ~2000 tokens
    raw_truncated = raw[:8000] if len(raw) > 8000 else raw

    system_prompt = f"""You are a clinical data extraction specialist.
Extract {resource_type} records from the input health data and return them as a JSON array.

TARGET SCHEMA — each item in your array MUST have these fields:
{json.dumps(schema["fields"], indent=2)}

Required fields (never leave empty): {schema["required"]}

Example of one correctly formatted row:
{json.dumps(schema["example"], indent=2)}

RULES:
- Return ONLY a raw JSON array. No markdown, no explanation, no ```json``` fences.
- One object per distinct {resource_type} record.
- If a field is missing from the source, use empty string "".
- Dates must be YYYY-MM-DD format.
- Do not hallucinate values not present in the source data.
- Do not include patient identifiers (name, DOB, SSN) in any field value."""

    user_content = (
        f"Extract all {resource_type} from this HealthEx data:\n\n{raw_truncated}"
    )

    try:
        import anthropic

        client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        )
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        content = response.content[0].text.strip()

        # Strip any markdown fences the model may have added
        content = _strip_markdown_fences(content)

        rows = json.loads(content)
        if not isinstance(rows, list):
            return []

        # Deterministic validation: ensure required fields are present
        validated = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if all(row.get(f) for f in schema["required"]):
                validated.append(row)

        return _phi_scan_rows(validated)

    except Exception as e:
        log.error("LLM normaliser failed for %s: %s", resource_type, e)
        return []


def _strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences that LLMs sometimes add around JSON."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]  # Remove first line (```json or ```)
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text

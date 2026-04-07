"""
planner.py — LLM Planner for the two-phase ingestion architecture.

Inspects a raw HealthEx blob and produces an ExtractionPlan dict.
The planner is the only component that reads the raw blob directly.
All other agents read from ingestion_plans.insights_summary instead.

Uses Claude Haiku (fast, cheap) since this is structured classification,
not deep reasoning. Called via asyncio.to_thread() from async callers.
"""

import json
import logging
import os

log = logging.getLogger(__name__)

PLANNER_MODEL = os.getenv("PLANNER_MODEL", "claude-haiku-4-5-20251001")

PLANNER_SYSTEM = """You are a medical data extraction planner. You receive raw health data blobs from the HealthEx API and produce structured extraction plans.

Your job is to:
1. Identify the exact format of the blob
2. Describe how to extract structured rows from it
3. Produce a plain-language summary of what the blob contains (for other agents to read)
4. Extract 1-3 sample rows to prove the extraction strategy works

Always respond with valid JSON only. No markdown, no explanation outside the JSON.

Format codes:
- "plain_text_summary" = Plain text summary (starts with "PATIENT:")
- "compressed_table" = Compressed dictionary table (starts with "#", uses @N references, pipe-separated)
- "flat_fhir_text" = Flat key=value text (starts with "resourceType is ")
- "fhir_bundle_json" = Proper FHIR R4 Bundle JSON (resourceType=="Bundle")
- "json_dict_array" = JSON dict array ({"conditions":[...]}, {"labs":[...]}, etc.)
- "unknown" = None of the above"""

PLANNER_PROMPT_TEMPLATE = """Analyze this HealthEx raw blob for resource_type="{resource_type}":

--- BLOB START (first 3000 chars) ---
{blob_preview}
--- BLOB END ---

Respond with this exact JSON structure:
{{
  "detected_format": "<one of the format codes>",
  "extraction_strategy": "pipe_split_sticky_date"|"at_ref_dict_lookup"|"fhir_bundle_entry_array"|"json_dict_array"|"plain_text_section_parse"|"llm_fallback",
  "estimated_rows": <integer>,
  "column_map": {{"col_name": "dict_key_or_path", ...}},
  "sample_rows": [<up to 3 extracted row dicts using the strategy>],
  "insights_summary": "<2-3 sentence plain English summary of what this blob contains — dates, counts, key values — that a clinical AI agent can read without seeing the raw blob>",
  "planner_confidence": <0.0 to 1.0>
}}

For compressed_table format: parse the D/C/S dictionary headers first, then show sample_rows with @N references already resolved.
For json_dict_array: show sample_rows as the first 1-3 items from the array."""


def plan_extraction(raw: str, resource_type: str, patient_id: str = "") -> dict:
    """
    Call the LLM Planner on a raw blob. Returns an ExtractionPlan dict.

    This is a synchronous function — call via asyncio.to_thread() from
    async code. Fast (<2s on Haiku) and only reads first 3000 chars.
    """
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK not available, returning fallback plan")
        return _fallback_plan(resource_type, patient_id, "anthropic SDK not installed")

    blob_preview = raw[:3000] if isinstance(raw, str) else json.dumps(raw)[:3000]

    client = anthropic.Anthropic()

    try:
        response = client.messages.create(
            model=PLANNER_MODEL,
            max_tokens=1024,
            system=PLANNER_SYSTEM,
            messages=[{
                "role": "user",
                "content": PLANNER_PROMPT_TEMPLATE.format(
                    resource_type=resource_type,
                    blob_preview=blob_preview,
                ),
            }],
        )

        raw_text = response.content[0].text.strip()
        # Strip any accidental markdown fences
        raw_text = (
            raw_text
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        plan = json.loads(raw_text)

        # Validate required fields
        for field in ("detected_format", "extraction_strategy", "estimated_rows", "insights_summary"):
            if field not in plan:
                plan[field] = "unknown" if field != "estimated_rows" else 0

        plan.setdefault("column_map", {})
        plan.setdefault("sample_rows", [])
        plan.setdefault("planner_confidence", 0.5)
        plan["patient_id"] = patient_id
        plan["planner_model"] = PLANNER_MODEL
        return plan

    except Exception as e:
        log.warning("planner LLM call failed: %s", e)
        return _fallback_plan(resource_type, patient_id, str(e))


def plan_extraction_deterministic(raw: str, resource_type: str, patient_id: str = "") -> dict:
    """
    Fast deterministic planner — no LLM call. Uses the existing format
    detector to classify the blob and produce a basic plan.
    Falls back gracefully if format_detector is not available.
    """
    try:
        from ingestion.adapters.healthex.format_detector import detect_format
        fmt, _ = detect_format(raw)
        fmt_code = fmt.value
    except Exception:
        fmt_code = "unknown"

    # Estimate row count from blob heuristics
    estimated_rows = _estimate_rows(raw, fmt_code, resource_type)

    strategy_map = {
        "compressed_table": "at_ref_dict_lookup",
        "plain_text_summary": "plain_text_section_parse",
        "flat_fhir_text": "flat_fhir_key_value",
        "fhir_bundle_json": "fhir_bundle_entry_array",
        "json_dict_array": "json_dict_array",
    }

    return {
        "detected_format": fmt_code,
        "extraction_strategy": strategy_map.get(fmt_code, "llm_fallback"),
        "estimated_rows": estimated_rows,
        "column_map": {},
        "sample_rows": [],
        "insights_summary": (
            f"{resource_type} data detected as {fmt_code} format, "
            f"~{estimated_rows} rows estimated. "
            f"Blob size: {len(raw)} chars."
        ),
        "planner_confidence": 0.6 if fmt_code != "unknown" else 0.2,
        "patient_id": patient_id,
        "planner_model": "deterministic",
    }


def _estimate_rows(raw: str, fmt_code: str, resource_type: str) -> int:
    """Heuristic row count estimation without parsing."""
    if fmt_code == "compressed_table":
        # Count lines with pipe separators that contain @ references (data rows)
        return sum(1 for line in raw.split("\n") if "|" in line and "@" in line)
    elif fmt_code == "fhir_bundle_json":
        # Count "resource" keys
        return raw.count('"resource"')
    elif fmt_code == "json_dict_array":
        # Count opening braces in arrays
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, list):
                        return len(v)
            elif isinstance(data, list):
                return len(data)
        except (json.JSONDecodeError, TypeError):
            pass
        return 1
    elif fmt_code == "plain_text_summary":
        # Count comma-separated items in sections
        return max(1, raw.count(",") // 3)
    return 1


def _fallback_plan(resource_type: str, patient_id: str, error: str) -> dict:
    return {
        "detected_format": "unknown",
        "extraction_strategy": "llm_fallback",
        "estimated_rows": 0,
        "column_map": {},
        "sample_rows": [],
        "insights_summary": f"Planner failed for {resource_type}: {error[:100]}",
        "planner_confidence": 0.0,
        "patient_id": patient_id,
        "planner_model": "fallback",
    }

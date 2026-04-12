"""
SELF-CONSISTENCY EXTRACTOR
===========================
Purpose: For blobs routed through the LLM fallback (format confidence < 0.80),
run extraction twice with different prompt framings and accept only values both
passes agree on. Divergent values are stored as NULL with candidates logged.

When to invoke: ONLY on LLM-fallback blobs. Specialist-parsed blobs (pipe-
delimited, flat key=value) have deterministic errors caught by schema validation.

Cost: Two LLM calls instead of one for ~15-20% of blobs (those that fail the
format classifier). At batch pricing this is negligible; accuracy gain is large.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)


def values_equivalent(v1: Any, v2: Any, tolerance: float = 0.01) -> bool:
    """Compare two extracted values, handling numeric tolerance and unit variations."""
    if v1 is None and v2 is None:
        return True
    if v1 is None or v2 is None:
        return False
    if v1 == v2:
        return True
    # Numeric comparison with tolerance
    try:
        n1 = float(str(v1))
        n2 = float(str(v2))
        return abs(n1 - n2) <= tolerance
    except (ValueError, TypeError):
        pass
    # String comparison: normalize whitespace and case
    return str(v1).strip().lower() == str(v2).strip().lower()


async def self_consistent_extract(
    blob: str,
    resource_type: str,
    extraction_schema: dict,
    model: str = "claude-sonnet-4-20250514",
) -> dict[str, Any]:
    """
    Run two independent extractions with different framings.
    Return consensus values; store divergent values as None with candidates.

    Args:
        blob: Raw clinical text blob
        resource_type: e.g., "labs", "conditions", "medications"
        extraction_schema: Dict describing fields to extract
        model: Model to use for both passes

    Returns:
        {
            "consensus": list[dict],    - Rows where both passes agree
            "divergent": list[dict],    - Divergence info for audit
            "agreement_rate": float,
        }
    """
    FRAMING_A = f"""You are a clinical data extraction specialist.
Extract {resource_type} records from the input health data.
Work CHRONOLOGICALLY — find the earliest mention of each value first,
then note if it changed over time.
Return ONLY a raw JSON array of objects. No markdown, no explanation."""

    FRAMING_B = f"""You are a clinical data extraction specialist.
Extract {resource_type} records from the input health data.
Work ENTITY-BY-ENTITY — find all data about each clinical entity
(labs, medications, vitals) before moving to the next.
Return ONLY a raw JSON array of objects. No markdown, no explanation."""

    schema_str = json.dumps(extraction_schema, indent=2)

    async def extract_with_framing(framing: str) -> list[dict]:
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(
                api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            )
            response = await client.messages.create(
                model=model,
                max_tokens=4000,
                system=framing,
                messages=[{
                    "role": "user",
                    "content": f"Schema: {schema_str}\n\nText:\n{blob[:8000]}"
                }],
            )
            raw = response.content[0].text.strip()
            raw = re.sub(r"```json|```", "", raw).strip()
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [r for r in parsed if isinstance(r, dict)]
            return []
        except Exception as e:
            log.warning("Self-consistency extraction failed: %s", e)
            return []

    # Run both framings concurrently
    r1, r2 = await asyncio.gather(
        extract_with_framing(FRAMING_A),
        extract_with_framing(FRAMING_B),
    )

    # Match rows between extractions using key fields
    key_fields = _get_key_fields(resource_type)
    consensus_rows: list[dict] = []
    divergent: list[dict] = []

    matched_r2_indices: set[int] = set()

    for row1 in r1:
        best_match = _find_matching_row(row1, r2, key_fields, matched_r2_indices)
        if best_match is not None:
            idx, row2 = best_match
            matched_r2_indices.add(idx)
            # Build consensus row
            consensus_row, row_divergent = _merge_rows(row1, row2)
            consensus_rows.append(consensus_row)
            divergent.extend(row_divergent)
        else:
            # Row only in extraction 1 — flag as uncertain
            flagged_row = {k: v for k, v in row1.items()}
            flagged_row["__quality_flag__"] = "single_extraction_only"
            consensus_rows.append(flagged_row)
            divergent.append({
                "field": "__row__",
                "v1": row1,
                "v2": None,
                "flag": "row_not_confirmed_by_second_extraction",
            })

    total_fields = sum(len(r) for r in consensus_rows) if consensus_rows else 1
    divergent_count = len(divergent)
    agreement_rate = max(0.0, (total_fields - divergent_count) / total_fields)

    return {
        "consensus": consensus_rows,
        "divergent": divergent,
        "agreement_rate": agreement_rate,
    }


def _get_key_fields(resource_type: str) -> list[str]:
    """Get the key fields used to match rows between extractions."""
    return {
        "labs": ["test_name", "date"],
        "conditions": ["name"],
        "medications": ["name"],
        "encounters": ["encounter_date", "encounter_type"],
        "immunizations": ["name", "date"],
    }.get(resource_type, ["name"])


def _find_matching_row(
    row: dict,
    candidates: list[dict],
    key_fields: list[str],
    excluded: set[int],
) -> tuple[int, dict] | None:
    """Find a matching row in candidates based on key fields."""
    for idx, candidate in enumerate(candidates):
        if idx in excluded:
            continue
        if all(
            values_equivalent(row.get(k), candidate.get(k))
            for k in key_fields
            if row.get(k) is not None
        ):
            return idx, candidate
    return None


def _merge_rows(row1: dict, row2: dict) -> tuple[dict, list[dict]]:
    """Merge two matching rows, flagging divergent values."""
    consensus: dict[str, Any] = {}
    divergent: list[dict] = []

    all_fields = set(row1.keys()) | set(row2.keys())
    for field in all_fields:
        v1 = row1.get(field)
        v2 = row2.get(field)
        if values_equivalent(v1, v2):
            consensus[field] = v1
        else:
            # Store None for divergent values
            consensus[field] = None
            divergent.append({
                "field": field,
                "v1": v1,
                "v2": v2,
                "flag": "extraction_divergence",
            })

    return consensus, divergent

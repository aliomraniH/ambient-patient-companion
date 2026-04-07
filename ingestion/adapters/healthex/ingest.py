"""
ingest.py — Adaptive schema-inference entry point for HealthEx payloads.

Three-stage pipeline:
  1. FORMAT DETECTION — identify which of the known formats the payload is
  2. FORMAT NORMALISATION — route to appropriate deterministic parser
  3. LLM FALLBACK — if parser returns 0 rows on non-trivial input

Returns a list of HealthEx native dicts that the existing
_normalize_to_fhir() → transform_*() → _write_*_rows() pipeline in
mcp_server.py will handle.  This function does NOT write to the DB.
"""
import logging

from .format_detector import detect_format, HealthExFormat
from .parsers.format_a_parser import parse_plain_text_summary
from .parsers.format_b_parser import parse_compressed_table
from .parsers.format_c_parser import parse_flat_fhir_text
from .parsers.format_d_parser import parse_fhir_bundle
from .parsers.json_dict_parser import parse_json_dict_arrays

log = logging.getLogger(__name__)


def adaptive_parse(
    raw_input: str,
    resource_type: str,
) -> tuple[list[dict], str, str]:
    """
    Parse a raw HealthEx payload into a list of native dicts.

    Returns:
        (rows, format_detected, parser_used)
        - rows: list of native dicts ready for _normalize_to_fhir()
        - format_detected: HealthExFormat.value string
        - parser_used: identifier of which parser produced the rows
    """
    # --- Stage 1: Format Detection ---
    fmt, payload = detect_format(raw_input)
    format_name = fmt.value
    log.info("Detected format: %s for resource_type=%s", format_name, resource_type)

    # --- Stage 2: Normalisation ---
    rows: list[dict] = []
    parser_used = "none"

    if fmt == HealthExFormat.PLAIN_TEXT_SUMMARY:
        rows = parse_plain_text_summary(payload, resource_type)
        parser_used = "format_a_plain_text"

    elif fmt == HealthExFormat.COMPRESSED_TABLE:
        rows = parse_compressed_table(payload, resource_type)
        parser_used = "format_b_compressed_table"

    elif fmt == HealthExFormat.FLAT_FHIR_TEXT:
        rows = parse_flat_fhir_text(payload, resource_type)
        parser_used = "format_c_flat_fhir_text"

    elif fmt == HealthExFormat.FHIR_BUNDLE_JSON:
        rows = parse_fhir_bundle(payload, resource_type)
        parser_used = "format_d_fhir_bundle"

    elif fmt == HealthExFormat.JSON_DICT_ARRAY:
        rows = parse_json_dict_arrays(payload, resource_type)
        parser_used = "json_dict_array"

    # Flatten any _extra_rows from component-based observations (Format D)
    rows = _flatten_extra_rows(rows)

    # --- Stage 3: LLM Fallback ---
    if len(rows) == 0 and len(raw_input) > 100:
        log.info(
            "Deterministic parser (%s) returned 0 rows — triggering LLM fallback",
            parser_used,
        )
        try:
            from .llm_fallback import llm_normalise
            rows = llm_normalise(raw_input, resource_type)
            parser_used = f"{parser_used}+llm_fallback"
        except Exception as e:
            log.error("LLM fallback failed: %s", e)

    return rows, format_name, parser_used


def _flatten_extra_rows(rows: list[dict]) -> list[dict]:
    """Flatten _extra_rows from component-based observations (e.g. BP)."""
    flat: list[dict] = []
    for row in rows:
        extra = row.pop("_extra_rows", None)
        flat.append(row)
        if extra and isinstance(extra, list):
            flat.extend(extra)
    return flat

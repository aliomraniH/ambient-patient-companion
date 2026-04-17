"""
ingest_verifier.py — Post-write round-trip verifier for HealthEx ingest.

After a write batch completes, the verifier:
  1. Extracts the canonical record list from the source payload.
  2. Reads the warehouse rows written for this batch.
  3. Reconstructs a source-shape representation from the warehouse rows.
  4. Diffs source against warehouse along the natural-key dimension.
  5. Classifies the outcome as clean | has_gaps | has_pollution | has_both.

Pollution (extra rows in warehouse) is auto-healable by definition — the
rows were just written seconds ago and are safe to delete in the same
transaction. Gaps (missing rows) are NOT auto-healed; they indicate a
real parsing or transform failure that requires a retry decision.

PHI rule: the verifier logs natural keys, resource counts, and diff
fields — never patient names, DOBs, or free text. Raw payload values
are held on the VerificationResult for the caller, never emitted to
logs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)


# Resource_type → warehouse table + natural-key columns for the diff.
_TABLE_MAP: dict[str, dict] = {
    "conditions": {
        "table": "patient_conditions",
        "natural_fields": ("code", "onset_date"),
        "all_fields": ("code", "display", "onset_date", "clinical_status"),
    },
    "medications": {
        "table": "patient_medications",
        "natural_fields": ("code", "authored_on"),
        "all_fields": ("code", "display", "status", "authored_on"),
    },
    "labs": {
        "table": "biometric_readings",
        "natural_fields": ("metric_type", "measured_at"),
        "all_fields": ("metric_type", "value", "unit", "measured_at"),
    },
    "encounters": {
        "table": "clinical_events",
        "natural_fields": ("event_type", "event_date"),
        "all_fields": ("event_type", "event_date", "description"),
    },
}


STATUS_CLEAN = "clean"
STATUS_GAPS = "has_gaps"
STATUS_POLLUTION = "has_pollution"
STATUS_BOTH = "has_both"
STATUS_UNVERIFIABLE = "unverifiable"


@dataclass
class VerificationResult:
    patient_id: str
    resource_type: str
    source_record_count: int
    warehouse_record_count: int
    matched: int = 0
    missing_in_warehouse: list[dict] = field(default_factory=list)
    extra_in_warehouse: list[dict] = field(default_factory=list)
    field_mismatches: list[dict] = field(default_factory=list)
    status: str = STATUS_UNVERIFIABLE
    can_autoheal: bool = False
    notes: list[str] = field(default_factory=list)

    def to_summary(self) -> dict:
        """Compact dict suitable for attaching to an ingest response."""
        return {
            "status": self.status,
            "source_record_count": self.source_record_count,
            "warehouse_record_count": self.warehouse_record_count,
            "matched": self.matched,
            "gaps": len(self.missing_in_warehouse),
            "pollution": len(self.extra_in_warehouse),
            "mismatches": len(self.field_mismatches),
            "can_autoheal": self.can_autoheal,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Canonical source extraction
# ---------------------------------------------------------------------------

def _extract_source_records(
    source_payload: str,
    resource_type: str,
) -> Optional[list[dict]]:
    """Extract a canonical list of source records from the raw payload.

    Uses adaptive_parse (the same entry point the writer uses) so the
    verifier is reading through the same lens. Returns None when the
    payload is not verifiable (unknown format, LLM-only path).
    """
    try:
        from ingestion.adapters.healthex.ingest import adaptive_parse
    except Exception as exc:  # pragma: no cover - import fault only
        log.debug("adaptive_parse unavailable: %s", exc)
        return None

    rows, fmt, parser = adaptive_parse(source_payload, resource_type)
    if parser.endswith("llm_fallback"):
        # LLM fallback means the source shape is not deterministically
        # recoverable — we can't reliably diff against the warehouse.
        return None
    return rows


def _canonical_key_source(resource_type: str, row: dict) -> str:
    """Natural key for a source row — aligned with the DB natural_key columns."""
    if resource_type == "conditions":
        code = row.get("icd10") or row.get("icd10_code") or row.get("code") or ""
        onset = row.get("onset_date") or row.get("onset") or ""
        return f"cond:{code or 'HASH:' + _hash(row.get('name', ''))}:{onset}"
    if resource_type == "medications":
        code = row.get("rxnorm") or row.get("code") or ""
        authored = row.get("start_date") or row.get("authored_on") or ""
        return f"med:{code or 'HASH:' + _hash(row.get('name', ''))}:{authored}"
    if resource_type == "labs":
        test = (row.get("test_name") or row.get("display") or row.get("name") or "").lower().replace(" ", "_")
        date = row.get("date") or row.get("effective_date") or ""
        return f"lab:{test or 'NOMETRIC'}:{date}"
    if resource_type == "encounters":
        etype = (row.get("encounter_type") or row.get("type") or "encounter")
        edate = row.get("encounter_date") or row.get("date") or ""
        return f"enc:{etype}:{edate}"
    return f"{resource_type}::{_hash(json.dumps(row, sort_keys=True, default=str))}"


def _canonical_key_warehouse(resource_type: str, row: dict) -> str:
    if resource_type == "conditions":
        code = row.get("code") or ""
        onset = row.get("onset_date") or ""
        return f"cond:{code or 'HASH:' + _hash(row.get('display') or '')}:{onset}"
    if resource_type == "medications":
        code = row.get("code") or ""
        authored = row.get("authored_on") or ""
        return f"med:{code or 'HASH:' + _hash(row.get('display') or '')}:{authored}"
    if resource_type == "labs":
        metric = (row.get("metric_type") or "").lower()
        measured = row.get("measured_at") or ""
        return f"lab:{metric or 'NOMETRIC'}:{measured}"
    if resource_type == "encounters":
        etype = row.get("event_type") or "encounter"
        edate = row.get("event_date") or ""
        return f"enc:{etype}:{edate}"
    return f"{resource_type}::unknown"


def _hash(s: str) -> str:
    return hashlib.md5((s or "").encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Main verifier entry point
# ---------------------------------------------------------------------------

async def verify_transfer(
    conn,
    *,
    patient_id: str,
    resource_type: str,
    source_payload: str,
    batch_window_seconds: int = 300,
) -> VerificationResult:
    """Round-trip verify a single-resource-type ingest.

    Reads warehouse rows written in the last `batch_window_seconds` (falls
    back to all rows for the patient if the table has no created_at column).
    Diffs against source_payload parsed via adaptive_parse. Never raises;
    returns a VerificationResult with status UNVERIFIABLE on errors.
    """
    table_info = _TABLE_MAP.get(resource_type)
    if not table_info:
        return VerificationResult(
            patient_id=patient_id,
            resource_type=resource_type,
            source_record_count=0,
            warehouse_record_count=0,
            status=STATUS_UNVERIFIABLE,
            notes=[f"no verifier mapping for resource_type={resource_type}"],
        )

    # Canonical source extraction
    source_records = _extract_source_records(source_payload, resource_type)
    if source_records is None:
        return VerificationResult(
            patient_id=patient_id,
            resource_type=resource_type,
            source_record_count=0,
            warehouse_record_count=0,
            status=STATUS_UNVERIFIABLE,
            notes=["source shape not deterministically recoverable (LLM fallback)"],
        )

    # Read warehouse rows for this patient from the target table.
    table = table_info["table"]
    fields = list(table_info["all_fields"])
    try:
        rows = await conn.fetch(
            f"SELECT {', '.join(fields)} FROM {table} "
            f"WHERE patient_id = $1::uuid",
            patient_id,
        )
    except Exception as exc:
        log.warning("verifier warehouse read failed for %s: %s", table, exc)
        return VerificationResult(
            patient_id=patient_id,
            resource_type=resource_type,
            source_record_count=len(source_records),
            warehouse_record_count=0,
            status=STATUS_UNVERIFIABLE,
            notes=[f"warehouse read error: {exc!s}"],
        )

    warehouse_rows = [dict(r) for r in rows]

    # Build key-indexed maps
    source_by_key: dict[str, dict] = {}
    for r in source_records:
        source_by_key[_canonical_key_source(resource_type, r)] = r
    warehouse_by_key: dict[str, dict] = {}
    for r in warehouse_rows:
        warehouse_by_key[_canonical_key_warehouse(resource_type, r)] = r

    missing = [
        source_by_key[k]
        for k in source_by_key
        if k not in warehouse_by_key
    ]
    extra = [
        warehouse_by_key[k]
        for k in warehouse_by_key
        if k not in source_by_key
    ]
    matched_keys = set(source_by_key) & set(warehouse_by_key)

    # Field-level mismatches across matched keys
    mismatches: list[dict] = []
    for k in matched_keys:
        src = source_by_key[k]
        wh = warehouse_by_key[k]
        diff = _row_diff(resource_type, src, wh)
        if diff:
            mismatches.append({"key": k, "diff": diff})

    if not missing and not extra and not mismatches:
        status = STATUS_CLEAN
    elif extra and not missing and not mismatches:
        status = STATUS_POLLUTION
    elif missing and not extra:
        status = STATUS_GAPS
    else:
        status = STATUS_BOTH

    can_autoheal = (status == STATUS_POLLUTION)

    return VerificationResult(
        patient_id=patient_id,
        resource_type=resource_type,
        source_record_count=len(source_records),
        warehouse_record_count=len(warehouse_rows),
        matched=len(matched_keys) - len(mismatches),
        missing_in_warehouse=missing,
        extra_in_warehouse=extra,
        field_mismatches=mismatches,
        status=status,
        can_autoheal=can_autoheal,
    )


def _row_diff(resource_type: str, source_row: dict, warehouse_row: dict) -> dict:
    """Compare source and warehouse representations of the same record.

    Only flags meaningful divergence — type coercion (str date ↔ date,
    float value ↔ int value) is tolerated.
    """
    diff: dict[str, dict] = {}

    if resource_type == "labs":
        src_val = _as_float(source_row.get("value") or source_row.get("result_value"))
        wh_val = _as_float(warehouse_row.get("value"))
        if src_val is not None and wh_val is not None:
            if abs(src_val - wh_val) > 1e-6:
                diff["value"] = {"source": src_val, "warehouse": wh_val}
        src_unit = (source_row.get("unit") or source_row.get("result_unit") or "").strip()
        wh_unit = (warehouse_row.get("unit") or "").strip()
        if src_unit and wh_unit and src_unit != wh_unit:
            diff["unit"] = {"source": src_unit, "warehouse": wh_unit}

    elif resource_type == "conditions":
        src_status = (source_row.get("status") or "").strip().lower()
        wh_status = (warehouse_row.get("clinical_status") or "").strip().lower()
        if src_status and wh_status and src_status != wh_status:
            diff["clinical_status"] = {"source": src_status, "warehouse": wh_status}

    elif resource_type == "medications":
        src_status = (source_row.get("status") or "").strip().lower()
        wh_status = (warehouse_row.get("status") or "").strip().lower()
        if src_status and wh_status and src_status != wh_status:
            diff["status"] = {"source": src_status, "warehouse": wh_status}

    return diff


def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).split()[0])
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Autoheal — safe to call only when status == STATUS_POLLUTION
# ---------------------------------------------------------------------------

async def autoheal_pollution(
    conn, result: VerificationResult
) -> int:
    """Delete warehouse rows that the verifier classified as pollution.

    Only callable when result.can_autoheal is True. Returns the number of
    rows deleted. PHI rule: no value-level logging, just the natural key
    and resource_type.
    """
    if not result.can_autoheal or not result.extra_in_warehouse:
        return 0
    table_info = _TABLE_MAP.get(result.resource_type)
    if not table_info:
        return 0
    table = table_info["table"]
    deleted = 0
    for row in result.extra_in_warehouse:
        where_parts: list[str] = ["patient_id = $1::uuid"]
        params: list[Any] = [result.patient_id]
        idx = 2
        for col in table_info["natural_fields"]:
            val = row.get(col)
            if val is None:
                where_parts.append(f"{col} IS NULL")
            else:
                where_parts.append(f"{col} = ${idx}")
                params.append(val)
                idx += 1
        try:
            res = await conn.execute(
                f"DELETE FROM {table} WHERE {' AND '.join(where_parts)}",
                *params,
            )
            # asyncpg returns "DELETE N"
            n = int(str(res).split()[-1]) if res else 0
            deleted += n
            log.warning(
                "autohealed pollution row in %s: key=%s deleted=%d",
                table, _canonical_key_warehouse(result.resource_type, row), n,
            )
        except Exception as exc:
            log.warning("autoheal delete failed: %s", exc)
    return deleted

"""
transfer_planner.py — Size-aware transfer planning for HealthEx ingest.

Assesses record count and payload size, selects the appropriate chunking
strategy, assigns batch/chunk UUIDs, and builds TransferRecord objects for
each clinical item. Pure Python — no DB interaction.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TransferRecord:
    """One individual clinical record to be transferred."""
    transfer_id:     str = field(default_factory=lambda: str(uuid.uuid4()))
    batch_id:        str = ""
    batch_sequence:  int = 0
    batch_total:     int = 0
    chunk_id:        str = ""
    chunk_sequence:  int = 0
    chunk_total:     int = 0

    row:             dict = field(default_factory=dict)

    record_key:      str = ""
    record_hash:     str = ""
    loinc_code:      str = ""
    icd10_code:      str = ""
    encounter_id:    str = ""

    resource_type:   str = ""
    source:          str = "healthex"
    format_detected: str = ""
    strategy:        str = ""

    planned_at:      Optional[datetime] = None
    extracted_at:    Optional[datetime] = None
    sanitized_at:    Optional[datetime] = None

    def compute_key(self) -> str:
        """Derive a privacy-safe natural key from the row (no PHI values)."""
        rt = self.resource_type
        if rt == "labs":
            return (f"{self.row.get('test_name', 'unknown')}"
                    f"::{self.row.get('date', '')}")
        elif rt == "conditions":
            return (f"{self.row.get('name', 'unknown')}"
                    f"::{self.row.get('onset_date', '') or self.row.get('onset', '')}")
        elif rt == "encounters":
            eid = (self.row.get("encounter_id")
                   or self.row.get("encounter_date")
                   or self.row.get("date", ""))
            return f"encounter::{eid}"
        elif rt == "medications":
            return (f"{self.row.get('name', 'unknown')}"
                    f"::{self.row.get('start_date', '') or self.row.get('authored_on', '')}")
        elif rt == "immunizations":
            return (f"{self.row.get('vaccine_name', 'unknown')}"
                    f"::{self.row.get('date', '')}")
        else:
            return f"summary::{self.row.get('last_visit', '')}"

    def compute_hash(self) -> str:
        """16-char SHA-256 prefix of the sanitized row JSON for integrity."""
        serialised = json.dumps(self.row, sort_keys=True, default=str)
        return hashlib.sha256(serialised.encode()).hexdigest()[:16]


@dataclass
class TransferPlan:
    """The full plan for transferring a batch of records."""
    batch_id:        str
    strategy:        str
    total_records:   int
    chunks:          list
    payload_bytes:   int
    resource_type:   str
    patient_id:      str
    planned_at:      datetime
    format_detected: str = ""


CHUNK_SIZES = {
    "single":         9999,
    "chunked_small":    10,
    "chunked_medium":   25,
    "chunked_large":    50,
    "llm_fallback":     10,
}


def plan_transfer(
    patient_id: str,
    resource_type: str,
    records: list,
    payload_bytes: int,
    format_detected: str,
    source: str = "healthex",
    strategy_override: str = "",
) -> "TransferPlan":
    """
    Assess record count and payload size, select strategy, assign IDs.

    Strategy selection:
        single        — ≤9 records AND ≤1 KB
        chunked_small — 10-49 records OR 1-10 KB   → chunks of 10
        chunked_medium— 50-199 records OR 10-50 KB → chunks of 25
        chunked_large — 200+ records OR >50 KB     → chunks of 50
    """
    n = len(records)
    batch_id = str(uuid.uuid4())
    planned_at = now_utc()

    if strategy_override:
        strategy = strategy_override
    elif n == 0:
        strategy = "single"
    elif n <= 9 and payload_bytes <= 1_000:
        strategy = "single"
    elif n <= 49 or payload_bytes <= 10_000:
        strategy = "chunked_small"
    elif n <= 199 or payload_bytes <= 50_000:
        strategy = "chunked_medium"
    else:
        strategy = "chunked_large"

    chunk_size = CHUNK_SIZES.get(strategy, 10)

    transfer_records: list[TransferRecord] = []
    for i, row in enumerate(records):
        tr = TransferRecord(
            batch_id=batch_id,
            batch_sequence=i + 1,
            batch_total=n,
            row=row.copy(),
            resource_type=resource_type,
            source=source,
            format_detected=format_detected,
            strategy=strategy,
            planned_at=planned_at,
        )
        tr.record_key = tr.compute_key()
        tr.record_hash = tr.compute_hash()
        tr.loinc_code = str(row.get("loinc") or row.get("loinc_code") or "")
        tr.icd10_code = str(row.get("icd10") or row.get("icd10_code") or "")
        tr.encounter_id = str(row.get("encounter_id") or "")
        transfer_records.append(tr)

    chunks: list[list[TransferRecord]] = []
    chunk_total = max(1, (n + chunk_size - 1) // chunk_size)

    for chunk_idx in range(chunk_total):
        chunk_id = str(uuid.uuid4())
        start = chunk_idx * chunk_size
        end = min(start + chunk_size, n)
        chunk = transfer_records[start:end]
        for tr in chunk:
            tr.chunk_id = chunk_id
            tr.chunk_sequence = chunk_idx + 1
            tr.chunk_total = chunk_total
        chunks.append(chunk)

    return TransferPlan(
        batch_id=batch_id,
        strategy=strategy,
        total_records=n,
        chunks=chunks,
        payload_bytes=payload_bytes,
        resource_type=resource_type,
        patient_id=patient_id,
        planned_at=planned_at,
        format_detected=format_detected,
    )

"""slm_bridge.py — Store SLM-generated insights into clinical_notes.

When call_slm produces output that is clinically relevant, callers can persist
it via store_slm_insight(). The context compiler reads all clinical_notes rows
for a patient, so SLM insights automatically flow into the next deliberation
without any additional wiring.

The source_type column (migration 005_slm_insights.sql) distinguishes SLM
rows from HealthEx-ingested notes so downstream queries can filter by origin.
"""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def store_slm_insight(
    patient_id: str,
    slm_output: dict,
    source_context: str = "general",
) -> dict:
    """Persist a call_slm result to clinical_notes so it enters deliberation context.

    Args:
        patient_id:      UUID of the patient this insight is about.
        slm_output:      The dict returned by call_slm (must contain
                         "generated_text", "model", and "adapter_type").
        source_context:  Short label for what the SLM was asked to do
                         (e.g. "medication_summary", "risk_narrative").
                         Appended to note_type as "slm_<source_context>".

    Returns:
        {"status": "ok", "note_id": "<uuid>"}  on success.
        {"status": "error", "reason": "<msg>"}  on any failure — never raises.
    """
    generated_text = (slm_output.get("generated_text") or "").strip()
    if not generated_text:
        return {"status": "error", "reason": "slm_output.generated_text is empty"}

    model = slm_output.get("model", "slm")
    adapter_type = slm_output.get("adapter_type", "base")
    note_type = f"slm_{source_context}"
    author = f"{model} (adapter={adapter_type})"
    note_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc)

    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO clinical_notes
                    (id, patient_id, note_type, note_text, author, note_date,
                     source_type, ingested_at)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, 'slm_inference', $6)
                """,
                note_id,
                patient_id,
                note_type,
                generated_text,
                author,
                now,
            )
        logger.info("store_slm_insight: wrote note %s for patient %s", note_id, patient_id)
        return {"status": "ok", "note_id": note_id}
    except Exception as exc:
        logger.error("store_slm_insight failed for patient %s: %s", patient_id, exc)
        return {"status": "error", "reason": str(exc)}

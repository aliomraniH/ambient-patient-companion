"""Async DB writer for provenance audit rows.

Non-blocking: audit failures are logged but never propagate back to
the caller. The provenance report is always returned regardless of DB
state.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def write_provenance_audit(
    pool,
    *,
    provenance_report_id: str,
    deliberation_id: str,
    output_id: str,
    patient_mrn_hash: str | None,
    source_server: str,
    assembled_by: str,
    gate_decision: str,
    block_reason: str | None,
    total_sections: int,
    blocked_count: int,
    warned_count: int,
    approved_count: int,
    pending_tools_needed: list[str],
    section_results: list[dict],
    strict_mode: bool,
) -> None:
    """Insert one row into provenance_audit_log.

    The pool argument is any asyncpg.Pool. Each server passes its own
    process-local pool (clinical = gap_aware pool, skills = db.connection
    pool, ingestion = local singleton).

    Exceptions are swallowed and logged — this writer never raises.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO provenance_audit_log (
                    provenance_report_id, deliberation_id, output_id,
                    patient_mrn_hash, source_server, assembled_by,
                    gate_decision, block_reason,
                    total_sections, blocked_count, warned_count,
                    approved_count,
                    pending_tools_needed, section_results,
                    assessed_at, strict_mode
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, $8, $9, $10, $11, $12,
                    $13::jsonb, $14::jsonb, $15, $16
                )
                """,
                uuid.UUID(provenance_report_id),
                uuid.UUID(deliberation_id) if deliberation_id else None,
                output_id,
                patient_mrn_hash,
                source_server,
                assembled_by,
                gate_decision,
                block_reason,
                total_sections,
                blocked_count,
                warned_count,
                approved_count,
                json.dumps(pending_tools_needed),
                json.dumps(section_results),
                datetime.now(timezone.utc),
                strict_mode,
            )
        logger.info(
            "provenance_audit: report=%s gate=%s server=%s sections=%d "
            "blocked=%d warned=%d",
            provenance_report_id,
            gate_decision,
            source_server,
            total_sections,
            blocked_count,
            warned_count,
        )
    except Exception as e:
        # Non-blocking: never re-raise.
        logger.error("provenance_audit write failed: %s", e)

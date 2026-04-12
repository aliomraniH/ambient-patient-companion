"""DB round-trip test for provenance_audit_log.

Skips silently when DATABASE_URL is not set so it does not fail in
sandbox environments. Inserts one row via the async writer and
verifies it lands, then cleans up.
"""

import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_provenance_audit_row_insert():
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL not set")

    import asyncpg  # noqa: WPS433

    conn = await asyncpg.connect(dsn)
    try:
        before = await conn.fetchval(
            "SELECT COUNT(*) FROM provenance_audit_log"
        )
        test_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO provenance_audit_log (
                provenance_report_id, output_id, assembled_by,
                source_server, gate_decision,
                total_sections, blocked_count, warned_count,
                approved_count,
                pending_tools_needed, section_results,
                assessed_at, strict_mode
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9,
                $10::jsonb, $11::jsonb, NOW(), $12
            )
            """,
            test_id, "test-001", "test-suite",
            "ambient-clinical-intelligence",
            "APPROVED", 1, 0, 0, 1,
            json.dumps([]), json.dumps([]), True,
        )
        after = await conn.fetchval(
            "SELECT COUNT(*) FROM provenance_audit_log"
        )
        assert after == before + 1

        row = await conn.fetchrow(
            "SELECT source_server, gate_decision FROM provenance_audit_log "
            "WHERE provenance_report_id = $1",
            test_id,
        )
        assert row["source_server"] == "ambient-clinical-intelligence"
        assert row["gate_decision"] == "APPROVED"

        await conn.execute(
            "DELETE FROM provenance_audit_log "
            "WHERE provenance_report_id = $1",
            test_id,
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(test_provenance_audit_row_insert())

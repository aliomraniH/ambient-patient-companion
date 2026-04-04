"""Skill: ingestion tools — freshness checks, ingestion triggers, conflict queries."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime

from fastmcp import FastMCP

from db.connection import get_pool
from skills.base import log_skill_execution

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def register(mcp: FastMCP):
    @mcp.tool
    async def check_data_freshness(patient_id: str) -> str:
        """Check data freshness status for all sources of a patient.

        Args:
            patient_id: UUID of the patient
        """
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT source_name, last_ingested_at, records_count,
                           ttl_hours, is_stale
                    FROM source_freshness
                    WHERE patient_id = $1
                    ORDER BY source_name
                    """,
                    patient_id,
                )

                sources = []
                for row in rows:
                    sources.append({
                        "source_name": row["source_name"],
                        "last_ingested_at": (
                            row["last_ingested_at"].isoformat()
                            if row["last_ingested_at"]
                            else None
                        ),
                        "records_count": row["records_count"],
                        "ttl_hours": row["ttl_hours"],
                        "is_stale": row["is_stale"],
                    })

                await log_skill_execution(
                    conn, "check_data_freshness", patient_id, "completed",
                    output_data={"sources": len(sources)},
                )

            return json.dumps({"patient_id": patient_id, "sources": sources})

        except Exception as e:
            logger.error("check_data_freshness failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn, "check_data_freshness", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log skill execution error")
            return f"Error: {e}"

    @mcp.tool
    async def run_ingestion(
        patient_id: str,
        source: str = "auto",
        force_refresh: bool = False,
    ) -> str:
        """Run the ingestion pipeline for a patient.

        Args:
            patient_id: UUID of the patient
            source: Data source (auto | synthea | healthex). 'auto' reads DATA_TRACK env.
            force_refresh: Skip freshness check if True.
        """
        pool = await get_pool()
        try:
            if source == "auto":
                source = os.environ.get("DATA_TRACK", "synthea")

            # Import here to avoid circular imports at module load
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
            from ingestion.pipeline import IngestionPipeline

            pipeline = IngestionPipeline(adapter_name=source, pool=pool)
            result = await pipeline.run(
                patient_id=patient_id,
                force_refresh=force_refresh,
                triggered_by="force_refresh" if force_refresh else "schedule",
            )

            async with pool.acquire() as conn:
                await log_skill_execution(
                    conn, "run_ingestion", patient_id, "completed",
                    output_data={
                        "status": result.status,
                        "records_upserted": result.records_upserted,
                        "conflicts_detected": result.conflicts_detected,
                        "duration_ms": result.duration_ms,
                    },
                )

            return (
                f"OK Ingestion {result.status} | "
                f"{result.records_upserted} records | "
                f"{result.conflicts_detected} conflicts | "
                f"{result.duration_ms}ms"
            )

        except Exception as e:
            logger.error("run_ingestion failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn, "run_ingestion", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log skill execution error")
            return f"Error: {e}"

    @mcp.tool
    async def get_source_conflicts(patient_id: str) -> str:
        """Query recent ingestion conflicts for a patient.

        Args:
            patient_id: UUID of the patient
        """
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT source_name, status, records_upserted,
                           conflicts_detected, duration_ms, error_message,
                           started_at
                    FROM ingestion_log
                    WHERE patient_id = $1
                      AND conflicts_detected > 0
                    ORDER BY started_at DESC
                    LIMIT 20
                    """,
                    patient_id,
                )

                conflicts = []
                for row in rows:
                    conflicts.append({
                        "source_name": row["source_name"],
                        "status": row["status"],
                        "records_upserted": row["records_upserted"],
                        "conflicts_detected": row["conflicts_detected"],
                        "duration_ms": row["duration_ms"],
                        "error_message": row["error_message"],
                        "started_at": (
                            row["started_at"].isoformat()
                            if row["started_at"]
                            else None
                        ),
                    })

                await log_skill_execution(
                    conn, "get_source_conflicts", patient_id, "completed",
                    output_data={"conflicts_found": len(conflicts)},
                )

            return json.dumps({"patient_id": patient_id, "conflicts": conflicts})

        except Exception as e:
            logger.error("get_source_conflicts failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn, "get_source_conflicts", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log skill execution error")
            return f"Error: {e}"

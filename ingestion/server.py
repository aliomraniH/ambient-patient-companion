"""FastMCP server entry point for the Data Ingestion Service."""

import logging
import os
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

from fastmcp import FastMCP

mcp = FastMCP("ambient-ingestion")


@mcp.tool
async def trigger_ingestion(
    patient_id: str,
    source: str = "synthea",
    force_refresh: bool = False,
) -> str:
    """Trigger the ingestion pipeline for a patient.

    Args:
        patient_id: UUID of the patient
        source: Data source adapter name (synthea | healthex)
        force_refresh: Skip freshness check and always re-ingest
    """
    try:
        import asyncpg
        from ingestion.pipeline import IngestionPipeline

        database_url = os.environ.get("DATABASE_URL", "")
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)

        try:
            pipeline = IngestionPipeline(adapter_name=source, pool=pool)
            result = await pipeline.run(
                patient_id=patient_id,
                force_refresh=force_refresh,
                triggered_by="force_refresh" if force_refresh else "schedule",
            )
            return (
                f"OK Ingestion {result.status} | "
                f"{result.records_upserted} records | "
                f"{result.conflicts_detected} conflicts | "
                f"{result.duration_ms}ms"
            )
        finally:
            await pool.close()

    except Exception as e:
        logger.error("trigger_ingestion failed: %s", e)
        return f"Error: {e}"


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8003"))
    if transport == "streamable-http":
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")

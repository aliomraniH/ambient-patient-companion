"""Seed script: populate the database with Synthea patients and generated data.

Usage:
    python seed.py --patients 10 --months 6
    python seed.py --patients 2 --months 1  # quick test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


async def main():
    parser = argparse.ArgumentParser(description="Seed the patient database")
    parser.add_argument(
        "--patients", type=int, default=10,
        help="Number of patients to import (default: 10)"
    )
    parser.add_argument(
        "--months", type=int, default=6,
        help="Months of historical data to generate (default: 6)"
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        logger.error("DATABASE_URL environment variable not set")
        sys.exit(1)

    import asyncpg
    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10, command_timeout=60)

    try:
        from orchestrator import run_seed_pipeline

        logger.info("Starting seed: %d patients, %d months", args.patients, args.months)
        summary = await run_seed_pipeline(
            patients=args.patients,
            months=args.months,
            pool=pool,
        )
        logger.info("Seed summary: %s", json.dumps(summary, indent=2))

        # Print verification counts
        async with pool.acquire() as conn:
            patient_count = await conn.fetchval("SELECT COUNT(*) FROM patients")
            readings_count = await conn.fetchval("SELECT COUNT(*) FROM biometric_readings")
            checkins_count = await conn.fetchval("SELECT COUNT(*) FROM daily_checkins")
            obt_count = await conn.fetchval("SELECT COUNT(*) FROM obt_scores")
            freshness_count = await conn.fetchval("SELECT COUNT(*) FROM source_freshness")

        logger.info("=== Verification Counts ===")
        logger.info("patients: %d", patient_count)
        logger.info("biometric_readings: %d", readings_count)
        logger.info("daily_checkins: %d", checkins_count)
        logger.info("obt_scores: %d", obt_count)
        logger.info("source_freshness: %d", freshness_count)

    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

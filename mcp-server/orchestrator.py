"""Daily pipeline orchestrator: freshness-first, then skill pipeline per patient.

Usage:
    python orchestrator.py --daily          Run daily pipeline for all patients
    python orchestrator.py --patient UUID   Run for a single patient
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import date, datetime, timedelta

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


async def run_daily_pipeline(
    patient_id: str,
    pool,
    force_fail_skill: str | None = None,
) -> dict:
    """Run the full daily pipeline for a single patient.

    Step 1: Freshness check + ingestion if stale.
    Step 2: Run skills in order with per-skill error handling.

    Args:
        patient_id: UUID of the patient.
        pool: asyncpg connection pool.
        force_fail_skill: If set, raise Exception for that skill name (for testing).

    Returns:
        Summary dict of the pipeline run.
    """
    today = date.today()
    today_str = str(today)
    results = {
        "patient_id": patient_id,
        "date": today_str,
        "skills_succeeded": 0,
        "skills_failed": 0,
        "errors": [],
    }

    # Step 1: Freshness check
    try:
        async with pool.acquire() as conn:
            freshness = await conn.fetchrow(
                """
                SELECT is_stale FROM source_freshness
                WHERE patient_id = $1 AND source_name = $2
                """,
                patient_id,
                os.environ.get("DATA_TRACK", "synthea"),
            )
            if freshness and freshness["is_stale"]:
                logger.info("Data stale for %s, running ingestion", patient_id)
                # Ingestion would run here; for Phase 1 with Synthea
                # data is seeded once, so freshness is typically not stale.
    except Exception as e:
        logger.error("Freshness check failed for %s: %s", patient_id, e)

    # Step 2: Run skills in order
    skill_sequence = [
        ("generate_daily_vitals", {"patient_id": patient_id, "target_date": today_str}),
        ("generate_daily_checkins", {"patient_id": patient_id, "target_date": today_str}),
        ("compute_obt_score", {"patient_id": patient_id, "score_date": today_str}),
        ("run_sdoh_assessment", {"patient_id": patient_id}),
        ("run_crisis_escalation", {"patient_id": patient_id, "check_date": today_str}),
        ("run_food_access_nudge", {"patient_id": patient_id, "current_date": today_str}),
        ("compute_provider_risk", {"patient_id": patient_id, "score_date": today_str}),
    ]

    # Import skill modules dynamically
    import importlib

    for skill_name, kwargs in skill_sequence:
        try:
            if force_fail_skill and skill_name == force_fail_skill:
                raise Exception(f"Forced failure for testing: {skill_name}")

            # Map skill tool names to module names
            module_map = {
                "generate_daily_vitals": "skills.generate_vitals",
                "generate_daily_checkins": "skills.generate_checkins",
                "compute_obt_score": "skills.compute_obt_score",
                "run_sdoh_assessment": "skills.sdoh_assessment",
                "run_crisis_escalation": "skills.crisis_escalation",
                "run_food_access_nudge": "skills.food_access_nudge",
                "compute_provider_risk": "skills.compute_provider_risk",
            }

            # For the orchestrator, we call the skill functions directly
            # rather than through MCP, since we're running server-side.
            module = importlib.import_module(module_map[skill_name])

            # Each skill module has a register() that creates the tool.
            # We need to call the inner async function directly.
            # The tool functions are defined inside register() so we
            # re-import and call them through a helper pattern.
            from db.connection import get_pool as _get_pool
            _pool = await _get_pool()

            # Build a simple FastMCP mock to capture the tool function
            tool_fn = _get_skill_function(module, skill_name)
            if tool_fn:
                result = await tool_fn(**kwargs)
                logger.info("Skill %s: %s", skill_name, result[:100] if result else "OK")
                results["skills_succeeded"] += 1
            else:
                logger.warning("Could not find tool function for %s", skill_name)
                results["skills_failed"] += 1

        except Exception as e:
            logger.error("Skill %s failed: %s", skill_name, e)
            results["skills_failed"] += 1
            results["errors"].append({"skill": skill_name, "error": str(e)})

    # Log pipeline run
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pipeline_runs
                    (id, run_date, patients_processed, skills_succeeded,
                     skills_failed, summary, data_source)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                str(uuid.uuid4()), datetime.utcnow(), 1,
                results["skills_succeeded"], results["skills_failed"],
                json.dumps(results), "synthea",
            )
    except Exception as e:
        logger.error("Failed to log pipeline run: %s", e)

    return results


def _get_skill_function(module, skill_name: str):
    """Extract the async tool function from a skill module.

    Skill modules define their tool inside register(mcp). We use a
    lightweight capture approach to get the function reference.
    """
    captured = {}

    class MockMCP:
        def tool(self, fn):
            captured[fn.__name__] = fn
            return fn

    try:
        module.register(MockMCP())
    except Exception:
        pass

    return captured.get(skill_name)


async def run_seed_pipeline(
    patients: int = 10,
    months: int = 6,
    pool=None,
    synthea_dir: str | None = None,
) -> dict:
    """Seed the database with Synthea patients and generated data.

    For each Synthea JSON file:
      1. Import patient via generate_patient skill
      2. Generate vitals + checkins for each day in the time range
      3. Compute OBT scores

    Args:
        patients: Max number of patients to import
        months: Number of months of historical data to generate
        pool: asyncpg connection pool
        synthea_dir: Override for SYNTHEA_OUTPUT_DIR

    Returns:
        Summary dict with counts.
    """
    import glob
    from pathlib import Path

    base_dir = synthea_dir or os.environ.get("SYNTHEA_OUTPUT_DIR", "/home/runner/synthea-output")
    fhir_dir = Path(base_dir) / "fhir"

    if not fhir_dir.exists():
        return {"error": f"FHIR directory not found: {fhir_dir}"}

    files = sorted(glob.glob(str(fhir_dir / "*.json")))[:patients]
    if not files:
        return {"error": f"No JSON files found in {fhir_dir}"}

    logger.info("Seeding %d patients with %d months of data", len(files), months)

    # Get skill functions
    import skills.generate_patient as gp_mod
    import skills.generate_vitals as gv_mod
    import skills.generate_checkins as gc_mod
    import skills.compute_obt_score as obt_mod
    import skills.sdoh_assessment as sdoh_mod
    import skills.compute_provider_risk as pr_mod

    generate_patient_fn = _get_skill_function(gp_mod, "generate_patient")
    generate_vitals_fn = _get_skill_function(gv_mod, "generate_daily_vitals")
    generate_checkins_fn = _get_skill_function(gc_mod, "generate_daily_checkins")
    compute_obt_fn = _get_skill_function(obt_mod, "compute_obt_score")
    sdoh_fn = _get_skill_function(sdoh_mod, "run_sdoh_assessment")
    provider_risk_fn = _get_skill_function(pr_mod, "compute_provider_risk")

    summary = {
        "patients_imported": 0,
        "vitals_days": 0,
        "checkin_days": 0,
        "obt_scores": 0,
        "errors": [],
    }

    today = date.today()
    start_date = today - timedelta(days=months * 30)

    for filepath in files:
        try:
            # Step 1: Import patient
            result = await generate_patient_fn(synthea_file=filepath)
            if result.startswith("Error"):
                logger.error("Patient import failed: %s", result)
                summary["errors"].append(result)
                continue

            # Extract patient_id from result
            # Format: "OK Imported Name | N conditions | M meds | UUID"
            patient_id = result.split("|")[-1].strip()
            summary["patients_imported"] += 1
            logger.info("Imported patient %d/%d: %s", summary["patients_imported"], len(files), patient_id)

            # Step 2: Generate vitals + checkins for each day
            current = start_date
            while current <= today:
                day_str = str(current)
                try:
                    await generate_vitals_fn(patient_id=patient_id, target_date=day_str)
                    summary["vitals_days"] += 1
                except Exception as e:
                    logger.error("Vitals failed for %s on %s: %s", patient_id, day_str, e)

                try:
                    await generate_checkins_fn(patient_id=patient_id, target_date=day_str)
                    summary["checkin_days"] += 1
                except Exception as e:
                    logger.error("Checkins failed for %s on %s: %s", patient_id, day_str, e)

                current += timedelta(days=1)

            # Step 3: Compute OBT scores (weekly to save time, plus today)
            current = start_date
            while current <= today:
                try:
                    await compute_obt_fn(patient_id=patient_id, score_date=str(current))
                    summary["obt_scores"] += 1
                except Exception as e:
                    logger.error("OBT failed for %s on %s: %s", patient_id, current, e)
                current += timedelta(days=7)

            # Always compute today's OBT
            try:
                await compute_obt_fn(patient_id=patient_id, score_date=str(today))
                summary["obt_scores"] += 1
            except Exception:
                pass

            # Step 4: SDoH assessment
            try:
                await sdoh_fn(patient_id=patient_id)
            except Exception as e:
                logger.error("SDoH failed for %s: %s", patient_id, e)

            # Step 5: Provider risk
            try:
                await provider_risk_fn(patient_id=patient_id, score_date=str(today))
            except Exception as e:
                logger.error("Provider risk failed for %s: %s", patient_id, e)

            # Step 6: Update source_freshness
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO source_freshness
                            (id, patient_id, source_name, last_ingested_at,
                             records_count, ttl_hours, data_source)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (patient_id, source_name) DO UPDATE SET
                            last_ingested_at = EXCLUDED.last_ingested_at,
                            records_count = EXCLUDED.records_count
                        """,
                        str(uuid.uuid4()), patient_id, "synthea",
                        datetime.utcnow(), summary["vitals_days"], 8760, "synthea",
                    )
            except Exception as e:
                logger.error("Freshness update failed for %s: %s", patient_id, e)

        except Exception as e:
            logger.error("Pipeline failed for file %s: %s", filepath, e)
            summary["errors"].append(str(e))

    logger.info(
        "Seed complete: %d patients, %d vitals days, %d OBT scores",
        summary["patients_imported"],
        summary["vitals_days"],
        summary["obt_scores"],
    )
    return summary


if __name__ == "__main__":
    import argparse
    import asyncio
    import asyncpg

    parser = argparse.ArgumentParser(description="Pipeline orchestrator")
    parser.add_argument("--daily", action="store_true", help="Run daily pipeline")
    parser.add_argument("--patient", type=str, help="Single patient UUID")
    args = parser.parse_args()

    async def main():
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            logger.error("DATABASE_URL not set")
            sys.exit(1)

        pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
        try:
            if args.daily:
                rows = await pool.fetch("SELECT id FROM patients")
                for row in rows:
                    pid = str(row["id"])
                    result = await run_daily_pipeline(pid, pool)
                    logger.info("Patient %s: %s", pid, json.dumps(result))
            elif args.patient:
                result = await run_daily_pipeline(args.patient, pool)
                logger.info("Result: %s", json.dumps(result))
            else:
                parser.print_help()
        finally:
            await pool.close()

    asyncio.run(main())

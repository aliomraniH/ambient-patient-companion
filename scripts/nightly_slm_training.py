"""
scripts/nightly_slm_training.py
────────────────────────────────────────────────────────────────────────────
Replit Scheduled Deployment — runs nightly at 02:00 UTC.

Cron expression: 0 2 * * *
Set in Replit: Tools → Scheduled Deployments → Add → Python script → this file

What it does:
1. Queries slm_training_queue for pending 'normal' and 'low' priority jobs
   (urgent jobs are handled by the 15-min AgentRuntime watcher).
2. For cohort jobs: assembles the latest deliberation corpus directly from DB.
3. Posts each job to the Modal web endpoint (fire-and-forget).
4. Updates queue rows to status='submitted'.

No imports beyond stdlib + asyncpg + httpx — nothing from the MCP server stack
so this script can run standalone in the Replit deployment environment.
"""

import asyncio
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone

import asyncpg
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [nightly_slm] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

DATABASE_URL       = os.environ["DATABASE_URL"]
MODAL_TRAIN_URL    = os.environ["MODAL_TRAIN_ENDPOINT_URL"]
MODAL_SECRET       = os.environ["MODAL_WEBHOOK_SECRET"]


# ─────────────────────────────────────────────────────────────────────────────
# Corpus assembly (mirrors slm_companion._assemble_cohort_corpus)
# ─────────────────────────────────────────────────────────────────────────────

async def _assemble_cohort_corpus(pool: asyncpg.Pool, cohort_name: str) -> str:
    if cohort_name == "diabetes_bh":
        cohort_patients = await pool.fetch(
            """
            SELECT DISTINCT p.id
            FROM patients p
            JOIN patient_conditions pc_dm ON p.id = pc_dm.patient_id
            JOIN patient_conditions pc_bh ON p.id = pc_bh.patient_id
            WHERE pc_dm.code LIKE 'E11%' AND pc_dm.status = 'active'
              AND pc_bh.code SIMILAR TO 'F3[23]%|F4[01]%' AND pc_bh.status = 'active'
            """
        )
    else:
        logger.warning(f"Unknown cohort '{cohort_name}' — skipping corpus assembly.")
        return ""

    patient_ids = [str(r["id"]) for r in cohort_patients]
    if not patient_ids:
        return ""

    rows = await pool.fetch(
        """
        SELECT do2.patient_id,
               do2.synthesis_summary,
               do2.clinical_findings,
               do2.convergence_score
        FROM deliberation_outputs do2
        WHERE do2.patient_id = ANY($1::uuid[])
          AND do2.convergence_score >= 0.70
          AND (do2.vera_gate = 'allow' OR do2.vera_gate IS NULL)
          AND do2.synthesis_summary IS NOT NULL
          AND do2.synthesis_summary != ''
        ORDER BY do2.created_at DESC
        LIMIT 800
        """,
        patient_ids,
    )

    system = (
        "You are a personalized ambient health companion for a patient with "
        "Type 2 diabetes and a behavioral health condition (depression or anxiety). "
        "Provide empathetic, evidence-grounded support. Never diagnose, never adjust "
        "medications, always escalate safety concerns."
    )

    lines = []
    for row in rows:
        pid_hash = hashlib.sha256(str(row["patient_id"]).encode()).hexdigest()[:16]
        user = (
            f"[CLINICAL CONTEXT]\nFindings: {row['clinical_findings'] or 'N/A'}\n"
            "[PATIENT TURN]\nHelp me understand what I should focus on this week."
        )
        obj = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
                {"role": "assistant", "content": row["synthesis_summary"]},
            ],
            "metadata": {
                "patient_id_hash": pid_hash,
                "cohort": cohort_name,
                "convergence_score": float(row["convergence_score"] or 0),
            },
        }
        lines.append(json.dumps(obj))

    return "\n".join(lines)


async def _fetch_corpus_from_url(url: str) -> str:
    """Fetch pre-assembled JSONL corpus from a signed URL (patient-specific jobs)."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text


async def _submit_to_modal(adapter_name: str, jsonl_content: str) -> dict:
    """POST to Modal web endpoint. Returns response JSON."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            MODAL_TRAIN_URL,
            json={"adapter_name": adapter_name, "jsonl_content": jsonl_content},
            headers={"Authorization": f"Bearer {MODAL_SECRET}"},
        )
        r.raise_for_status()
        return r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    started_at = datetime.now(timezone.utc).isoformat()
    logger.info(f"=== nightly_slm_training started at {started_at} ===")

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)

    # Fetch pending jobs (normal + low priority; urgent handled by watcher)
    pending = await pool.fetch(
        """
        SELECT *
        FROM slm_training_queue
        WHERE status = 'pending'
          AND priority IN ('normal', 'low')
          AND scheduled_for <= NOW()
        ORDER BY
          CASE priority WHEN 'normal' THEN 0 ELSE 1 END,
          created_at
        LIMIT 10
        """
    )

    logger.info(f"Found {len(pending)} pending jobs for nightly run.")

    submitted = 0
    failed = 0

    for job in pending:
        adapter_name = job["adapter_name"]
        cohort_name  = job["cohort_name"]
        patient_id   = job["patient_id"]
        corpus_url   = job.get("training_corpus_url")

        logger.info(f"Processing job {job['id']}: {adapter_name} (priority={job['priority']})")

        # ── Assemble corpus ───────────────────────────────────────────────────
        try:
            if cohort_name:
                jsonl_content = await _assemble_cohort_corpus(pool, cohort_name)
            elif corpus_url:
                jsonl_content = await _fetch_corpus_from_url(corpus_url)
            else:
                logger.warning(f"Job {job['id']}: no cohort_name or corpus_url — skipping.")
                await pool.execute(
                    "UPDATE slm_training_queue SET status='failed',"
                    " failed_reason='no_corpus_source' WHERE id=$1",
                    job["id"],
                )
                failed += 1
                continue
        except Exception as e:
            logger.error(f"Job {job['id']}: corpus assembly failed: {e}")
            await pool.execute(
                "UPDATE slm_training_queue SET status='failed', failed_reason=$2 WHERE id=$1",
                job["id"], f"corpus_error: {str(e)[:200]}",
            )
            failed += 1
            continue

        row_count = jsonl_content.count("\n") + 1 if jsonl_content.strip() else 0
        if row_count < 50:
            logger.warning(f"Job {job['id']}: corpus too small ({row_count} rows), skipping.")
            await pool.execute(
                "UPDATE slm_training_queue SET status='failed',"
                " failed_reason=$2 WHERE id=$1",
                job["id"], f"corpus_too_small: {row_count} rows",
            )
            failed += 1
            continue

        # ── Submit to Modal ───────────────────────────────────────────────────
        try:
            result = await _submit_to_modal(adapter_name, jsonl_content)
            modal_job_id = result.get("job_id", "unknown")
            await pool.execute(
                "UPDATE slm_training_queue"
                " SET status='submitted', submitted_at=NOW(), modal_job_id=$2"
                " WHERE id=$1",
                job["id"], modal_job_id,
            )
            logger.info(f"Job {job['id']}: submitted to Modal (modal_job_id={modal_job_id})")
            submitted += 1
        except Exception as e:
            logger.error(f"Job {job['id']}: Modal submission failed: {e}")
            await pool.execute(
                "UPDATE slm_training_queue SET status='failed', failed_reason=$2 WHERE id=$1",
                job["id"], f"modal_error: {str(e)[:200]}",
            )
            failed += 1

    await pool.close()

    logger.info(
        f"=== nightly_slm_training complete: {submitted} submitted, {failed} failed ==="
    )


if __name__ == "__main__":
    asyncio.run(main())

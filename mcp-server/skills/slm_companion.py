"""
mcp-server/skills/slm_companion.py
────────────────────────────────────────────────────────────────────────────
SLM Companion Inference Layer — MCP control plane for the Qwen 2.5 3B-Instruct
endpoint on Hugging Face Inference Endpoints with TGI Multi-LoRA serving.

Registered on Server 2 (ambient-skills-companion) via the standard
register(mcp) auto-discovery pattern. Also registers one AgentRuntime
background watcher (slm_retraining_watcher, 15-min interval) for urgent
training queue processing.

Tools exposed (13 total):
  Inference & Status (4):
    get_slm_status, run_slm_inference, get_slm_inference_log, warm_slm_endpoint
  Adapter Registry (4):
    list_adapters, promote_adapter, rollback_adapter, slm_reload_adapters
  Training Queue (4):
    flag_adapter_for_update, get_training_queue, trigger_cohort_training,
    get_training_status
  Testing (1):
    compare_adapter_responses, get_cohort_corpus_stats

Prerequisites:
  Replit Secrets:
    HF_SLM_ENDPOINT_URL       — dedicated TGI endpoint HTTPS URL
    HF_TOKEN                  — fine-grained: read private repos + call endpoint
    HF_ENDPOINT_ADMIN_TOKEN   — admin scope: update endpoint env vars via HF API
    MODAL_TRAIN_ENDPOINT_URL  — Modal web endpoint for training jobs
    MODAL_WEBHOOK_SECRET      — shared secret for Replit→Modal auth

  Migration:
    server/migrations/012_slm_companion.sql must be applied before startup.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import httpx
from fastmcp import FastMCP
from huggingface_hub import get_inference_endpoint
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from shared.datetime_utils import ensure_aware

logger = logging.getLogger(__name__)

# ── Environment ───────────────────────────────────────────────────────────────
HF_ENDPOINT_URL      = os.environ.get("HF_SLM_ENDPOINT_URL", "")
HF_TOKEN             = os.environ.get("HF_TOKEN", "")
HF_ADMIN_TOKEN       = os.environ.get("HF_ENDPOINT_ADMIN_TOKEN", HF_TOKEN)
MODAL_TRAIN_URL      = os.environ.get("MODAL_TRAIN_ENDPOINT_URL", "")
MODAL_SECRET         = os.environ.get("MODAL_WEBHOOK_SECRET", "")


def _check_env() -> None:
    """Warn at import time for each missing required secret. Never raises."""
    required = {
        "HF_SLM_ENDPOINT_URL": HF_ENDPOINT_URL,
        "HF_TOKEN": HF_TOKEN,
        "MODAL_TRAIN_ENDPOINT_URL": MODAL_TRAIN_URL,
        "MODAL_WEBHOOK_SECRET": MODAL_SECRET,
        "HF_ENDPOINT_NAME": os.environ.get("HF_ENDPOINT_NAME", ""),
        "HF_NAMESPACE": os.environ.get("HF_NAMESPACE", ""),
    }
    for name, val in required.items():
        if not val:
            logger.warning("slm_companion: secret %s is not set — tools will return error dicts at runtime", name)


_check_env()
DATABASE_URL         = os.environ.get("DATABASE_URL", "")

# ── Watcher interval (monkey-patchable in tests) ──────────────────────────────
WATCHER_INTERVAL = 900  # 15 minutes

# ── Module-level singletons (created lazily on first use) ─────────────────────
_pool: Optional[asyncpg.Pool] = None
_hf_client = None  # huggingface_hub.AsyncInferenceClient


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool


def _get_hf_client():
    """Lazy-init AsyncInferenceClient. Import deferred so startup never blocks."""
    global _hf_client
    if _hf_client is None:
        from huggingface_hub import AsyncInferenceClient
        _hf_client = AsyncInferenceClient(base_url=HF_ENDPOINT_URL, token=HF_TOKEN)
    return _hf_client


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _resolve_adapter(pool: asyncpg.Pool, patient_id: Optional[str]) -> tuple[str, str]:
    """
    Returns (hf_repo_or_tgi, adapter_type) for inference.
    Priority: patient-specific adapter → cohort adapter → base model ('tgi').
    """
    if patient_id:
        # 1. Per-patient adapter (active at M4+)
        row = await pool.fetchrow(
            "SELECT hf_repo FROM slm_adapter_registry"
            " WHERE patient_id=$1 AND adapter_type='patient' AND status='active'",
            patient_id,
        )
        if row:
            return row["hf_repo"], "patient"

        # 2. Cohort adapter derived from patient's conditions
        conditions = await pool.fetch(
            "SELECT code FROM patient_conditions WHERE patient_id=$1 AND status='active'",
            patient_id,
        )
        codes = [r["code"] for r in conditions]
        cohort = _classify_cohort(codes)

        if cohort:
            row = await pool.fetchrow(
                "SELECT hf_repo FROM slm_adapter_registry"
                " WHERE cohort_name=$1 AND adapter_type='cohort' AND status='active'",
                cohort,
            )
            if row:
                return row["hf_repo"], "cohort"

    # 3. No adapter — use base model
    return "tgi", "base"


def _classify_cohort(icd_codes: list[str]) -> Optional[str]:
    """Map ICD-10 code set → cohort adapter name. Extend as new cohorts train."""
    has_diabetes = any(c.startswith("E11") for c in icd_codes)
    has_bh = any(c.startswith(("F32", "F33", "F41", "F40", "F43")) for c in icd_codes)
    has_hypertension = any(c.startswith("I10") for c in icd_codes)

    if has_diabetes and has_bh:
        return "diabetes_bh"
    if has_diabetes:
        return "diabetes"          # placeholder for future cohort
    return None


async def _build_prompt_context(pool: asyncpg.Pool, patient_id: str) -> dict:
    """
    Assemble the deterministic ~3K-token prompt schema from Postgres.
    Returns {'system_prompt': str, 'patient_context': str}.
    """
    # Patient base row
    patient = await pool.fetchrow(
        "SELECT first_name, last_name FROM patients WHERE id=$1", patient_id
    )
    name = f"{patient['first_name'] or ''} {patient['last_name'] or ''}".strip() or "Patient"

    # Active conditions (up to 10)
    conditions = await pool.fetch(
        "SELECT description, code FROM patient_conditions"
        " WHERE patient_id=$1 AND status='active' LIMIT 10",
        patient_id,
    )
    cond_text = "; ".join(f"{r['description']} ({r['code']})" for r in conditions) or "None on file"

    # Current medications (up to 8)
    meds = await pool.fetch(
        "SELECT medication_name, dosage, frequency FROM patient_medications"
        " WHERE patient_id=$1 AND status='active' LIMIT 8",
        patient_id,
    )
    med_text = "; ".join(
        f"{r['medication_name']} {r['dosage'] or ''} {r['frequency'] or ''}".strip()
        for r in meds
    ) or "None on file"

    # Recent biometrics (last 3 per metric type)
    biometrics = await pool.fetch(
        "SELECT metric_type, value, unit, measured_at"
        " FROM biometric_readings WHERE patient_id=$1"
        " ORDER BY measured_at DESC LIMIT 12",
        patient_id,
    )
    bio_text = "; ".join(
        f"{r['metric_type']}: {r['value']} {r['unit'] or ''}".strip()
        for r in biometrics[:6]
    ) or "None on file"

    # Most recent deliberation summary (if <48 hr old)
    deliberation = await pool.fetchrow(
        "SELECT synthesis_summary, convergence_score, created_at"
        " FROM deliberation_outputs"
        " WHERE patient_id=$1 AND created_at >= NOW() - INTERVAL '48 hours'"
        " ORDER BY created_at DESC LIMIT 1",
        patient_id,
    )
    delib_text = ""
    if deliberation:
        delib_text = f"\nRecent clinical synthesis (convergence {deliberation['convergence_score']:.2f}): {deliberation['synthesis_summary'][:400]}"

    # SDoH flags
    sdoh = await pool.fetchrow(
        "SELECT food_insecurity, transportation_barrier, housing_instability,"
        "       social_isolation, financial_strain"
        " FROM sdoh_assessments WHERE patient_id=$1 ORDER BY assessed_at DESC LIMIT 1",
        patient_id,
    )
    sdoh_flags = []
    if sdoh:
        if sdoh["food_insecurity"]:       sdoh_flags.append("food insecurity")
        if sdoh["transportation_barrier"]: sdoh_flags.append("transportation barrier")
        if sdoh["housing_instability"]:    sdoh_flags.append("housing instability")
        if sdoh["social_isolation"]:       sdoh_flags.append("social isolation")
        if sdoh["financial_strain"]:       sdoh_flags.append("financial strain")
    sdoh_text = ", ".join(sdoh_flags) if sdoh_flags else "None flagged"

    system_prompt = (
        "You are a personalized ambient health companion. Your role is to provide "
        "empathetic, evidence-grounded support, behavioral nudges, and health education. "
        "You never diagnose, never adjust medications, and always escalate safety concerns "
        "to the care team. Keep responses warm, clear, and under 200 words unless the "
        "patient explicitly asks for more detail."
    )

    patient_context = (
        f"[PATIENT CONTEXT — {name}]\n"
        f"Active conditions: {cond_text}\n"
        f"Current medications: {med_text}\n"
        f"Recent biometrics: {bio_text}\n"
        f"Social factors: {sdoh_text}"
        f"{delib_text}"
    )

    return {"system_prompt": system_prompt, "patient_context": patient_context}


@retry(
    retry=retry_if_exception_type(Exception),
    wait=wait_exponential(multiplier=1, min=3, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
async def _call_hf_endpoint(model: str, messages: list) -> str:
    """
    Core HF inference call with retry-with-exponential-backoff.
    Handles HTTP 502 during scale-from-zero cold start automatically.
    Uses temperature=0 + seed=42 for clinical determinism.
    """
    client = _get_hf_client()
    result = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=1024,
        temperature=0,
        seed=42,
        stream=False,
    )
    return result.choices[0].message.content


async def _log_inference(
    pool: asyncpg.Pool,
    patient_id: Optional[str],
    adapter_used: str,
    adapter_type: str,
    messages: list,
    response_text: Optional[str],
    latency_ms: int,
    status: str,
    error_message: Optional[str] = None,
) -> None:
    """Write to slm_inference_log. Stores prompt hash only — never raw prompt."""
    prompt_hash = hashlib.sha256(
        json.dumps(messages, sort_keys=True).encode()
    ).hexdigest()[:24]
    response_tokens = len(response_text.split()) if response_text else 0
    try:
        await pool.execute(
            "INSERT INTO slm_inference_log"
            " (patient_id, adapter_used, adapter_type, prompt_hash,"
            "  response_tokens, latency_ms, endpoint_status, error_message)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            patient_id, adapter_used, adapter_type, prompt_hash,
            response_tokens, latency_ms, status, error_message,
        )
    except Exception as e:
        logger.error(f"slm_inference_log write failed: {e}")


async def _get_hf_endpoint_info() -> dict:
    """
    Calls the HF Inference Endpoints API to get current endpoint status.
    Returns raw endpoint JSON or error dict.
    """
    # Extract endpoint name and namespace from the endpoint URL
    # URL format: https://<name>.<region>.aws.endpoints.huggingface.cloud
    url = HF_ENDPOINT_URL
    if not url:
        return {"error": "HF_SLM_ENDPOINT_URL not configured"}

    # Use HF Inference Endpoints management API
    # The endpoint health check is simpler — just hit /health
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{url.rstrip('/')}/health",
                headers={"Authorization": f"Bearer {HF_TOKEN}"},
            )
            if r.status_code == 200:
                return {"status": "running", "url": url, "health": r.json()}
            elif r.status_code == 503:
                return {"status": "initializing", "url": url}
            elif r.status_code == 502:
                return {"status": "scaled_to_zero", "url": url}
            else:
                return {"status": "unknown", "http_code": r.status_code, "url": url}
    except Exception as e:
        return {"status": "unreachable", "error": str(e), "url": url}


async def _submit_modal_training_job(adapter_name: str, jsonl_content: str) -> dict:
    """POST to the Modal web endpoint to kick off a training job (fire-and-forget)."""
    if not MODAL_TRAIN_URL:
        return {"error": "MODAL_TRAIN_ENDPOINT_URL not configured"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                MODAL_TRAIN_URL,
                json={"adapter_name": adapter_name, "jsonl_content": jsonl_content},
                headers={"Authorization": f"Bearer {MODAL_SECRET}"},
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e)}


async def _assemble_cohort_corpus(pool: asyncpg.Pool, cohort_name: str) -> str:
    """
    Assembles JSONL training corpus from deliberation_outputs for the given cohort.
    Returns a JSONL string (one JSON object per line).
    Filters: VERA gate='allow' (or NULL in audit mode), convergence_score >= 0.70.
    """
    # Identify cohort patients by condition codes
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
        return ""

    patient_ids = [str(r["id"]) for r in cohort_patients]
    if not patient_ids:
        return ""

    # Fetch deliberation outputs for these patients
    rows = await pool.fetch(
        """
        SELECT do2.patient_id,
               do2.synthesis_summary,
               do2.clinical_findings,
               do2.behavioral_insights,
               do2.convergence_score,
               do2.created_at,
               d.mode
        FROM deliberation_outputs do2
        JOIN deliberations d ON do2.deliberation_id = d.id
        WHERE do2.patient_id = ANY($1::uuid[])
          AND do2.convergence_score >= 0.70
          AND (do2.vera_gate = 'allow' OR do2.vera_gate IS NULL)
        ORDER BY do2.created_at DESC
        LIMIT 800
        """,
        patient_ids,
    )

    lines = []
    for row in rows:
        pid_hash = hashlib.sha256(str(row["patient_id"]).encode()).hexdigest()[:16]
        system = (
            "You are a personalized ambient health companion for a patient with "
            "Type 2 diabetes and a behavioral health condition (depression or anxiety). "
            "Provide empathetic, evidence-grounded support. Never diagnose, never adjust "
            "medications, always escalate safety concerns."
        )
        user = (
            f"[CLINICAL CONTEXT]\n"
            f"Clinical findings: {row['clinical_findings'] or 'N/A'}\n"
            f"[PATIENT TURN]\nHelp me understand what I should focus on this week."
        )
        assistant = row["synthesis_summary"] or ""
        if not assistant:
            continue

        obj = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
                {"role": "assistant", "content": assistant},
            ],
            "metadata": {
                "patient_id_hash": pid_hash,
                "cohort": cohort_name,
                "convergence_score": float(row["convergence_score"] or 0),
                "vera_gate": "allow",
            },
        }
        lines.append(json.dumps(obj))

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# AgentRuntime background watcher
# ─────────────────────────────────────────────────────────────────────────────

async def _slm_retraining_watcher() -> None:
    """
    Runs every WATCHER_INTERVAL seconds (default 15 min).
    1. Submits pending URGENT training jobs to Modal immediately.
    2. Polls submitted jobs for Modal completion → updates adapter registry.
    Run state persisted to system_config key watcher_state:slm_retraining_watcher.
    """
    pool = await _get_pool()

    # ── 1. Submit pending urgent jobs ────────────────────────────────────────
    urgent_jobs = await pool.fetch(
        "SELECT * FROM slm_training_queue"
        " WHERE status='pending' AND priority='urgent' AND scheduled_for <= NOW()"
        " ORDER BY created_at LIMIT 5"
    )
    for job in urgent_jobs:
        # Assemble corpus (cohort job) or fetch from stored URL (patient job)
        if job["cohort_name"]:
            jsonl_content = await _assemble_cohort_corpus(pool, job["cohort_name"])
        else:
            # For patient-specific: corpus URL stored at queue insertion time
            corpus_url = job.get("training_corpus_url", "")
            if not corpus_url:
                logger.warning(f"SLM watcher: no corpus URL for job {job['id']}")
                continue
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.get(corpus_url)
                    jsonl_content = r.text
            except Exception as e:
                logger.error(f"SLM watcher: corpus fetch failed for job {job['id']}: {e}")
                continue

        if not jsonl_content.strip():
            logger.warning(f"SLM watcher: empty corpus for job {job['id']}, skipping")
            continue

        result = await _submit_modal_training_job(job["adapter_name"], jsonl_content)
        if "error" in result:
            logger.error(f"SLM watcher: Modal submission failed: {result['error']}")
            await pool.execute(
                "UPDATE slm_training_queue SET status='failed',"
                " failed_reason=$2 WHERE id=$1",
                job["id"], result["error"],
            )
            continue

        await pool.execute(
            "UPDATE slm_training_queue"
            " SET status='submitted', submitted_at=NOW(), modal_job_id=$2"
            " WHERE id=$1",
            job["id"], result.get("job_id", "unknown"),
        )
        logger.info(f"SLM watcher: submitted urgent job {job['adapter_name']} to Modal")

    # ── 2. Poll submitted jobs for completion ─────────────────────────────────
    submitted = await pool.fetch(
        "SELECT * FROM slm_training_queue"
        " WHERE status='submitted' AND submitted_at >= NOW() - INTERVAL '4 hours'"
    )
    for job in submitted:
        modal_job_id = job.get("modal_job_id", "")
        if not modal_job_id or modal_job_id == "unknown":
            continue

        # Poll Modal job status via their jobs API
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"https://api.modal.com/v1/jobs/{modal_job_id}",
                    headers={"Authorization": f"Bearer {MODAL_SECRET}"},
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                modal_status = data.get("status", "unknown")
        except Exception:
            continue  # Network issue — try again next cycle

        if modal_status == "completed":
            await pool.execute(
                "UPDATE slm_training_queue"
                " SET status='completed', completed_at=NOW() WHERE id=$1",
                job["id"],
            )
            # Mark adapter as pending_review — human promotes via MCP tool
            await pool.execute(
                "UPDATE slm_adapter_registry"
                " SET status='pending_review', trained_at=NOW()"
                " WHERE hf_repo=$1 AND status IN ('training','active')",
                job["adapter_name"],
            )
            logger.info(f"SLM watcher: training completed for {job['adapter_name']}")

        elif modal_status in ("failed", "cancelled"):
            await pool.execute(
                "UPDATE slm_training_queue"
                " SET status='failed', failed_reason=$2 WHERE id=$1",
                job["id"], f"Modal status: {modal_status}",
            )
            logger.error(f"SLM watcher: Modal job failed for {job['adapter_name']}")


def register_watchers(runtime) -> None:
    """Called by load_skills() when runtime is provided. Registers background task."""
    runtime.watch("slm_retraining_watcher", WATCHER_INTERVAL, _slm_retraining_watcher)


# ─────────────────────────────────────────────────────────────────────────────
# MCP Tool Registration
# ─────────────────────────────────────────────────────────────────────────────

def register(mcp: FastMCP) -> None:

    # ══════════════════════════════════════════════════════════════════════════
    # INFERENCE & STATUS (4 tools)
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    async def get_slm_status() -> str:
        """
        Check the HF Inference Endpoint status, loaded adapters, and recent
        inference metrics. Returns endpoint health, GPU status, and adapter
        registry summary. Call this first when debugging any SLM issue.
        """
        pool = await _get_pool()
        endpoint_info = await _get_hf_endpoint_info()

        # Adapter registry summary
        adapters = await pool.fetch(
            "SELECT adapter_type, cohort_name, hf_repo, status, trained_at,"
            " training_examples"
            " FROM slm_adapter_registry ORDER BY created_at DESC LIMIT 20"
        )

        # Recent inference stats (last 24 hr)
        stats = await pool.fetchrow(
            "SELECT COUNT(*) as total_calls,"
            " AVG(latency_ms) as avg_latency_ms,"
            " SUM(CASE WHEN endpoint_status='success' THEN 1 ELSE 0 END) as successes,"
            " SUM(CASE WHEN endpoint_status='error' THEN 1 ELSE 0 END) as errors"
            " FROM slm_inference_log"
            " WHERE called_at >= NOW() - INTERVAL '24 hours'"
        )

        # Pending queue
        queue_count = await pool.fetchrow(
            "SELECT COUNT(*) as cnt FROM slm_training_queue WHERE status='pending'"
        )

        return json.dumps({
            "endpoint": endpoint_info,
            "adapters": [
                {
                    "type": r["adapter_type"],
                    "cohort": r["cohort_name"],
                    "repo": r["hf_repo"],
                    "status": r["status"],
                    "trained_at": ensure_aware(r["trained_at"]).isoformat()
                    if r["trained_at"] else None,
                    "examples": r["training_examples"],
                }
                for r in adapters
            ],
            "inference_24h": {
                "total_calls": stats["total_calls"],
                "avg_latency_ms": round(stats["avg_latency_ms"] or 0, 1),
                "successes": stats["successes"],
                "errors": stats["errors"],
            },
            "pending_training_jobs": queue_count["cnt"],
        })


    @mcp.tool()
    async def run_slm_inference(
        patient_id: str,
        prompt: str,
        use_adapter: bool = True,
    ) -> str:
        """
        Run a live SLM inference call for a patient. Automatically resolves
        the correct adapter (cohort or per-patient), assembles the deterministic
        prompt from EHR context, calls the HF endpoint, and returns the response.

        Args:
            patient_id: UUID of the patient in Postgres.
            prompt: The user message / question to send to the companion.
            use_adapter: If False, forces base model (no LoRA) for comparison.

        Returns dict with: response, adapter_used, adapter_type, latency_ms, status.
        """
        pool = await _get_pool()
        context = await _build_prompt_context(pool, patient_id)

        if use_adapter:
            hf_repo, adapter_type = await _resolve_adapter(pool, patient_id)
        else:
            hf_repo, adapter_type = "tgi", "base"

        messages = [
            {"role": "system", "content": context["system_prompt"]},
            {"role": "user", "content": context["patient_context"] + "\n\n" + prompt},
        ]

        t0 = time.monotonic()
        response_text = None
        status = "error"
        error_msg = None

        try:
            response_text = await _call_hf_endpoint(hf_repo, messages)
            status = "success"
        except Exception as e:
            error_msg = str(e)
            logger.error(f"run_slm_inference failed for {patient_id}: {e}")

        latency_ms = int((time.monotonic() - t0) * 1000)

        await _log_inference(
            pool, patient_id, hf_repo, adapter_type,
            messages, response_text, latency_ms, status, error_msg,
        )

        return json.dumps({
            "response": response_text,
            "adapter_used": hf_repo,
            "adapter_type": adapter_type,
            "latency_ms": latency_ms,
            "status": status,
            "error": error_msg,
        })


    @mcp.tool()
    async def get_slm_inference_log(
        patient_id: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """
        Return recent SLM inference calls from the audit log.
        Optionally filtered by patient. Shows adapter, latency, token counts,
        and status. Raw prompts are never stored — only prompt hashes.

        Args:
            patient_id: Filter by specific patient UUID (optional).
            limit: Maximum rows to return (default 20, max 100).
        """
        pool = await _get_pool()
        limit = min(limit, 100)

        if patient_id:
            rows = await pool.fetch(
                "SELECT id, patient_id, adapter_used, adapter_type, prompt_hash,"
                " response_tokens, latency_ms, endpoint_status, error_message, called_at"
                " FROM slm_inference_log"
                " WHERE patient_id=$1"
                " ORDER BY called_at DESC LIMIT $2",
                patient_id, limit,
            )
        else:
            rows = await pool.fetch(
                "SELECT id, patient_id, adapter_used, adapter_type, prompt_hash,"
                " response_tokens, latency_ms, endpoint_status, error_message, called_at"
                " FROM slm_inference_log"
                " ORDER BY called_at DESC LIMIT $1",
                limit,
            )

        return json.dumps([
            {
                "id": r["id"],
                "patient_id": str(r["patient_id"]) if r["patient_id"] else None,
                "adapter_used": r["adapter_used"],
                "adapter_type": r["adapter_type"],
                "prompt_hash": r["prompt_hash"],
                "response_tokens": r["response_tokens"],
                "latency_ms": r["latency_ms"],
                "status": r["endpoint_status"],
                "error": r["error_message"],
                "called_at": ensure_aware(r["called_at"]).isoformat(),
            }
            for r in rows
        ])


    @mcp.tool()
    async def warm_slm_endpoint() -> str:
        """
        Send a minimal request to wake the HF endpoint from scale-to-zero.
        Call this before a scheduled patient session to avoid cold-start latency.
        Returns endpoint status and estimated time until ready.
        """
        pool = await _get_pool()

        # Check current status first
        info = await _get_hf_endpoint_info()
        if info.get("status") == "running":
            return json.dumps({"status": "already_warm", "endpoint": info})

        # Send a minimal warmup request
        warmup_messages = [
            {"role": "user", "content": "Ready."},
        ]
        t0 = time.monotonic()
        try:
            # Don't retry aggressively — just one attempt to trigger the scale-up
            client = _get_hf_client()
            result = await asyncio.wait_for(
                client.chat.completions.create(
                    model="tgi",
                    messages=warmup_messages,
                    max_tokens=5,
                    temperature=0,
                ),
                timeout=120,  # Allow up to 2 min for cold start
            )
            latency = int((time.monotonic() - t0) * 1000)
            await _log_inference(
                pool, None, "tgi", "base",
                warmup_messages, "_warmup_", latency, "success",
            )
            return json.dumps({
                "status": "warmed_up",
                "latency_ms": latency,
                "note": "Endpoint is now ready for inference.",
            })
        except asyncio.TimeoutError:
            return json.dumps({
                "status": "initializing",
                "note": "Warmup request timed out (2 min). Endpoint is scaling up — try again in 30 sec.",
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
            })
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})


    # ══════════════════════════════════════════════════════════════════════════
    # ADAPTER REGISTRY (4 tools)
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    async def list_adapters(patient_id: Optional[str] = None) -> str:
        """
        List all entries in the SLM adapter registry.
        Optionally filter by patient UUID for per-patient adapters.
        Shows HF repo, version, status, training date, and example count.

        Args:
            patient_id: Filter to a specific patient's adapters (optional).
        """
        pool = await _get_pool()
        if patient_id:
            rows = await pool.fetch(
                "SELECT * FROM slm_adapter_registry WHERE patient_id=$1 ORDER BY created_at DESC",
                patient_id,
            )
        else:
            rows = await pool.fetch(
                "SELECT * FROM slm_adapter_registry ORDER BY adapter_type, created_at DESC"
            )

        return json.dumps([
            {
                "id": r["id"],
                "patient_id": str(r["patient_id"]) if r["patient_id"] else None,
                "adapter_type": r["adapter_type"],
                "cohort_name": r["cohort_name"],
                "hf_repo": r["hf_repo"],
                "hf_revision": r["hf_revision"],
                "status": r["status"],
                "base_model": r["base_model"],
                "lora_rank": r["lora_rank"],
                "training_examples": r["training_examples"],
                "trained_at": ensure_aware(r["trained_at"]).isoformat() if r["trained_at"] else None,
                "promoted_at": ensure_aware(r["promoted_at"]).isoformat() if r["promoted_at"] else None,
                "promoted_by": r["promoted_by"],
                "flagged_reason": r["flagged_reason"],
                "eval_score": r["eval_score"],
            }
            for r in rows
        ])


    @mcp.tool()
    async def promote_adapter(
        hf_repo: str,
        patient_id: Optional[str] = None,
        hf_revision: str = "main",
    ) -> str:
        """
        Promote a trained adapter to active status in the registry.
        This does NOT reload the HF endpoint — call slm_reload_adapters() after.
        Records who promoted it (always 'claude_mcp' when called from Claude Web).

        Args:
            hf_repo: The HF Hub repo of the adapter to promote.
            patient_id: UUID of patient for per-patient adapters (None for cohort).
            hf_revision: Git revision/tag to promote (default 'main').
        """
        pool = await _get_pool()

        # Archive the current active adapter for this patient/cohort
        if patient_id:
            await pool.execute(
                "UPDATE slm_adapter_registry"
                " SET status='superseded'"
                " WHERE patient_id=$1 AND status='active'",
                patient_id,
            )
        else:
            await pool.execute(
                "UPDATE slm_adapter_registry"
                " SET status='superseded'"
                " WHERE hf_repo=$1 AND status='active'",
                hf_repo,
            )

        # Upsert the new active entry
        await pool.execute(
            """
            INSERT INTO slm_adapter_registry
              (patient_id, adapter_type, hf_repo, hf_revision, status,
               promoted_at, promoted_by)
            VALUES ($1,
              CASE WHEN $1 IS NULL THEN 'cohort' ELSE 'patient' END,
              $2, $3, 'active', NOW(), 'claude_mcp')
            ON CONFLICT (patient_id, adapter_type)
              WHERE status = 'active'
            DO UPDATE SET
              hf_repo=$2,
              hf_revision=$3,
              status='active',
              promoted_at=NOW(),
              promoted_by='claude_mcp'
            """,
            patient_id, hf_repo, hf_revision,
        )

        return json.dumps({
            "status": "promoted",
            "hf_repo": hf_repo,
            "hf_revision": hf_revision,
            "patient_id": patient_id,
            "note": "Call slm_reload_adapters() to activate on the HF endpoint.",
        })


    @mcp.tool()
    async def rollback_adapter(
        hf_repo: str,
        patient_id: Optional[str] = None,
    ) -> str:
        """
        Roll back to the previous active revision for a given adapter.
        Sets the current active entry to 'rolled_back' and restores the
        most recent 'superseded' entry. Call slm_reload_adapters() after.

        Args:
            hf_repo: The HF Hub repo to roll back.
            patient_id: UUID of patient for per-patient adapters (None for cohort).
        """
        pool = await _get_pool()

        # Mark current active as rolled_back
        await pool.execute(
            "UPDATE slm_adapter_registry SET status='rolled_back'"
            " WHERE hf_repo=$1 AND status='active'",
            hf_repo,
        )

        # Restore the most recent superseded entry
        prev = await pool.fetchrow(
            "SELECT id FROM slm_adapter_registry"
            " WHERE hf_repo=$1 AND status='superseded'"
            " ORDER BY promoted_at DESC LIMIT 1",
            hf_repo,
        )
        if not prev:
            return json.dumps({
                "status": "error",
                "error": "No previous version to roll back to. Manual intervention required.",
            })

        await pool.execute(
            "UPDATE slm_adapter_registry SET status='active', promoted_at=NOW(),"
            " promoted_by='claude_mcp_rollback'"
            " WHERE id=$1",
            prev["id"],
        )

        return json.dumps({
            "status": "rolled_back",
            "hf_repo": hf_repo,
            "restored_registry_id": prev["id"],
            "note": "Call slm_reload_adapters() to activate the previous version on the HF endpoint.",
        })


    @mcp.tool()
    async def slm_reload_adapters() -> str:
        """
        Update the LORA_ADAPTERS environment variable on the HF Inference Endpoint
        with all currently-active adapters from slm_adapter_registry, then trigger
        an endpoint restart. Returns estimated restart time (~2–3 min).

        Use after promote_adapter() or rollback_adapter() to activate changes.
        Requires HF_ENDPOINT_ADMIN_TOKEN with endpoint management scope.
        """
        pool = await _get_pool()

        # Get all active adapter repos
        active = await pool.fetch(
            "SELECT hf_repo, hf_revision FROM slm_adapter_registry"
            " WHERE status='active' ORDER BY adapter_type, created_at"
        )

        if not active:
            return json.dumps({
                "status": "no_adapters",
                "note": "No active adapters in registry. Endpoint will serve base model only.",
            })

        # Build LORA_ADAPTERS string: "org/repo@rev,org/repo2@rev2"
        lora_adapters_value = ",".join(
            f"{r['hf_repo']}@{r['hf_revision']}" for r in active
        )

        # HF Inference Endpoints management API — update env var + restart
        # Endpoint name must be stored in an env var or derived from the URL
        endpoint_name = os.environ.get("HF_ENDPOINT_NAME", "")
        namespace = os.environ.get("HF_NAMESPACE", "")

        if not endpoint_name or not namespace:
            # Fallback: return the value for manual update
            return json.dumps({
                "status": "manual_required",
                "lora_adapters_value": lora_adapters_value,
                "adapter_count": len(active),
                "note": (
                    "HF_ENDPOINT_NAME and HF_NAMESPACE secrets not set. "
                    "Copy the lora_adapters_value above and update manually in HF Hub UI "
                    "under your endpoint's Environment Variables."
                ),
            })

        try:
            endpoint = get_inference_endpoint(
                name=endpoint_name,
                namespace=namespace,
                token=HF_ADMIN_TOKEN,
            )
            endpoint.update(
                custom_image={"env": {"LORA_ADAPTERS": lora_adapters_value}}
            )
            # Do NOT call .wait() — restart happens asynchronously on HF side.
            return json.dumps({
                "status": "reload_triggered",
                "adapter_count": len(active),
                "lora_adapters_value": lora_adapters_value,
                "estimated_restart_sec": 180,
                "note": "Endpoint is restarting with new adapters. Expect ~2–3 min downtime.",
            })
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})


    # ══════════════════════════════════════════════════════════════════════════
    # TRAINING QUEUE (4 tools)
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    async def flag_adapter_for_update(
        reason: str,
        patient_id: Optional[str] = None,
        cohort_name: Optional[str] = None,
        priority: str = "normal",
    ) -> str:
        """
        Flag an adapter for retraining and add it to the training queue.
        'urgent' jobs are processed within 15 min by the background watcher.
        'normal' and 'low' jobs run in the next nightly training cycle (02:00 UTC).

        Args:
            reason: Human-readable reason for the retraining request.
            patient_id: UUID of patient for per-patient adapter (None for cohort).
            cohort_name: Cohort name e.g. 'diabetes_bh' (None for patient adapter).
            priority: 'urgent' | 'normal' | 'low'.
        """
        if not patient_id and not cohort_name:
            return json.dumps({"error": "Must provide either patient_id or cohort_name."})

        pool = await _get_pool()
        priority = priority if priority in ("urgent", "normal", "low") else "normal"

        # Derive adapter_name from registry
        if cohort_name:
            row = await pool.fetchrow(
                "SELECT hf_repo FROM slm_adapter_registry"
                " WHERE cohort_name=$1 AND adapter_type='cohort' AND status='active'",
                cohort_name,
            )
            adapter_name = row["hf_repo"] if row else f"yourorg/cohort-{cohort_name}-adapter"
            scheduled_for = "NOW()" if priority == "urgent" else "NOW() + INTERVAL '1 day'"
        else:
            row = await pool.fetchrow(
                "SELECT hf_repo FROM slm_adapter_registry"
                " WHERE patient_id=$1 AND adapter_type='patient' AND status='active'",
                patient_id,
            )
            short_id = str(patient_id)[:8]
            adapter_name = row["hf_repo"] if row else f"yourorg/patient-{short_id}-adapter"
            scheduled_for = "NOW()" if priority == "urgent" else "NOW() + INTERVAL '1 day'"

        queue_id = await pool.fetchval(
            f"""
            INSERT INTO slm_training_queue
              (patient_id, cohort_name, adapter_name, status, priority, reason,
               scheduled_for, created_by)
            VALUES ($1, $2, $3, 'pending', $4, $5, {scheduled_for}, 'claude_mcp')
            RETURNING id
            """,
            patient_id, cohort_name, adapter_name, priority, reason,
        )

        # Also flag in adapter registry
        if patient_id:
            await pool.execute(
                "UPDATE slm_adapter_registry"
                " SET status='flagged', flagged_reason=$2, flagged_at=NOW()"
                " WHERE patient_id=$1 AND status='active'",
                patient_id, reason,
            )
        elif cohort_name:
            await pool.execute(
                "UPDATE slm_adapter_registry"
                " SET flagged_reason=$2, flagged_at=NOW()"
                " WHERE cohort_name=$1 AND status='active'",
                cohort_name, reason,
            )

        return json.dumps({
            "status": "queued",
            "queue_id": queue_id,
            "adapter_name": adapter_name,
            "priority": priority,
            "scheduled": "immediately (watcher picks up in ≤15 min)" if priority == "urgent"
                         else "nightly run at 02:00 UTC",
            "reason": reason,
        })


    @mcp.tool()
    async def get_training_queue(
        status: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """
        Return the SLM training queue, optionally filtered by status.
        Shows Modal job ID, submission time, estimated completion, and failure reason.

        Args:
            status: Filter by 'pending' | 'submitted' | 'completed' | 'failed' (optional).
            limit: Maximum rows to return (default 20).
        """
        pool = await _get_pool()
        limit = min(limit, 100)

        if status:
            rows = await pool.fetch(
                "SELECT * FROM slm_training_queue WHERE status=$1"
                " ORDER BY created_at DESC LIMIT $2",
                status, limit,
            )
        else:
            rows = await pool.fetch(
                "SELECT * FROM slm_training_queue ORDER BY created_at DESC LIMIT $1",
                limit,
            )

        return json.dumps([
            {
                "id": r["id"],
                "patient_id": str(r["patient_id"]) if r["patient_id"] else None,
                "cohort_name": r["cohort_name"],
                "adapter_name": r["adapter_name"],
                "status": r["status"],
                "priority": r["priority"],
                "reason": r["reason"],
                "modal_job_id": r["modal_job_id"],
                "scheduled_for": ensure_aware(r["scheduled_for"]).isoformat() if r["scheduled_for"] else None,
                "submitted_at": ensure_aware(r["submitted_at"]).isoformat() if r["submitted_at"] else None,
                "completed_at": ensure_aware(r["completed_at"]).isoformat() if r["completed_at"] else None,
                "failed_reason": r["failed_reason"],
                "created_by": r["created_by"],
                "created_at": ensure_aware(r["created_at"]).isoformat() if r["created_at"] else None,
            }
            for r in rows
        ])


    @mcp.tool()
    async def trigger_cohort_training(
        cohort_name: str,
        force: bool = False,
    ) -> str:
        """
        Immediately submit a cohort adapter retraining job to Modal,
        bypassing the nightly schedule. Assembles the latest VERA-gated
        deliberation traces for the cohort and posts to Modal.

        Args:
            cohort_name: e.g. 'diabetes_bh'.
            force: If True, submits even if a job is already running (default False).
        """
        pool = await _get_pool()

        # Guard: don't double-submit unless forced
        if not force:
            existing = await pool.fetchrow(
                "SELECT id, status FROM slm_training_queue"
                " WHERE cohort_name=$1 AND status IN ('pending','submitted')"
                " ORDER BY created_at DESC LIMIT 1",
                cohort_name,
            )
            if existing:
                return json.dumps({
                    "status": "already_queued",
                    "existing_job_id": existing["id"],
                    "existing_status": existing["status"],
                    "note": "Use force=True to submit anyway.",
                })

        # Corpus stats check
        corpus = await _assemble_cohort_corpus(pool, cohort_name)
        row_count = corpus.count("\n") + 1 if corpus.strip() else 0

        if row_count < 100:
            return json.dumps({
                "status": "insufficient_corpus",
                "row_count": row_count,
                "minimum_required": 100,
                "note": "Run more deliberations on cohort patients to build the training corpus.",
            })

        adapter_name = f"yourorg/cohort-{cohort_name}-adapter"

        # Submit to Modal
        result = await _submit_modal_training_job(adapter_name, corpus)
        if "error" in result:
            return json.dumps({"status": "error", "error": result["error"]})

        # Insert queue row
        queue_id = await pool.fetchval(
            """
            INSERT INTO slm_training_queue
              (cohort_name, adapter_name, status, priority, reason,
               modal_job_id, scheduled_for, submitted_at, created_by)
            VALUES ($1, $2, 'submitted', 'normal', 'manual trigger via Claude MCP',
                    $3, NOW(), NOW(), 'claude_mcp')
            RETURNING id
            """,
            cohort_name, adapter_name, result.get("job_id", "unknown"),
        )

        return json.dumps({
            "status": "submitted",
            "queue_id": queue_id,
            "adapter_name": adapter_name,
            "modal_job_id": result.get("job_id"),
            "corpus_rows": row_count,
            "estimated_duration_min": max(5, row_count // 60),
            "note": (
                "Training submitted to Modal. Poll get_training_status(job_id=...) to check progress. "
                "When complete, call promote_adapter() then slm_reload_adapters()."
            ),
        })


    @mcp.tool()
    async def get_training_status(modal_job_id: str) -> str:
        """
        Poll the Modal API for the status of a training job.
        Returns current phase, elapsed time, and adapter HF repo if completed.

        Args:
            modal_job_id: The Modal job ID returned by trigger_cohort_training
                          or get_training_queue.
        """
        pool = await _get_pool()

        # Look up the queue row first
        job = await pool.fetchrow(
            "SELECT * FROM slm_training_queue WHERE modal_job_id=$1",
            modal_job_id,
        )

        # Poll Modal
        modal_status = "unknown"
        elapsed = None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"https://api.modal.com/v1/jobs/{modal_job_id}",
                    headers={"Authorization": f"Bearer {MODAL_SECRET}"},
                )
                if r.status_code == 200:
                    data = r.json()
                    modal_status = data.get("status", "unknown")
                    started = data.get("started_at")
                    finished = data.get("finished_at")
                    if started:
                        from datetime import datetime as dt
                        start_dt = dt.fromisoformat(started.replace("Z", "+00:00"))
                        end_dt = (
                            dt.fromisoformat(finished.replace("Z", "+00:00"))
                            if finished
                            else datetime.now(timezone.utc)
                        )
                        elapsed = int((end_dt - start_dt).total_seconds())
                else:
                    modal_status = f"api_error_{r.status_code}"
        except Exception as e:
            modal_status = f"poll_error: {str(e)[:80]}"

        return json.dumps({
            "modal_job_id": modal_job_id,
            "modal_status": modal_status,
            "elapsed_seconds": elapsed,
            "queue_status": job["status"] if job else "not_found",
            "adapter_name": job["adapter_name"] if job else None,
            "cohort": job["cohort_name"] if job else None,
            "submitted_at": ensure_aware(job["submitted_at"]).isoformat()
            if job and job["submitted_at"] else None,
            "next_steps": (
                "Call promote_adapter() then slm_reload_adapters() to activate."
                if modal_status == "completed"
                else "Keep polling — training is still running."
                if modal_status in ("queued", "running")
                else "Check failed_reason in get_training_queue()."
                if modal_status == "failed"
                else "Poll again in 60 seconds."
            ),
        })


    # ══════════════════════════════════════════════════════════════════════════
    # TESTING & DEBUGGING (2 tools)
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    async def compare_adapter_responses(
        patient_id: str,
        prompt: str,
    ) -> str:
        """
        Run the same prompt through both the base model and the patient's
        best available adapter (cohort or per-patient), in parallel.
        Returns both responses side-by-side for quality evaluation.
        Use this to validate adapter improvement before promoting.

        Args:
            patient_id: UUID of the patient in Postgres.
            prompt: The test prompt to evaluate.
        """
        pool = await _get_pool()
        context = await _build_prompt_context(pool, patient_id)
        messages = [
            {"role": "system", "content": context["system_prompt"]},
            {"role": "user", "content": context["patient_context"] + "\n\n" + prompt},
        ]

        hf_repo, adapter_type = await _resolve_adapter(pool, patient_id)

        # Run both in parallel
        t0 = time.monotonic()
        base_task = asyncio.create_task(_call_hf_endpoint("tgi", messages))
        adapter_task = (
            asyncio.create_task(_call_hf_endpoint(hf_repo, messages))
            if hf_repo != "tgi"
            else None
        )

        base_resp, adapter_resp = None, None
        base_error, adapter_error = None, None

        try:
            base_resp = await base_task
        except Exception as e:
            base_error = str(e)

        if adapter_task:
            try:
                adapter_resp = await adapter_task
            except Exception as e:
                adapter_error = str(e)
        else:
            adapter_resp = "(No adapter — patient uses base model)"

        total_ms = int((time.monotonic() - t0) * 1000)

        return json.dumps({
            "patient_id": patient_id,
            "prompt": prompt[:200] + ("..." if len(prompt) > 200 else ""),
            "base_model": {
                "response": base_resp,
                "error": base_error,
                "adapter": "tgi (base Qwen 2.5 3B-Instruct)",
            },
            "adapter_model": {
                "response": adapter_resp,
                "error": adapter_error,
                "adapter": hf_repo,
                "adapter_type": adapter_type,
            },
            "total_latency_ms": total_ms,
            "evaluation_note": (
                "Compare tone, clinical accuracy, and personalization. "
                "If adapter is better: call promote_adapter() + slm_reload_adapters(). "
                "If base is better: adapter may need more training data."
            ),
        })


    @mcp.tool()
    async def get_cohort_corpus_stats(cohort_name: str) -> str:
        """
        Return statistics on the available SFT training corpus for a cohort.
        Shows row count, VERA gate distribution, convergence score histogram,
        last deliberation date, and whether minimum threshold is met (300 rows).

        Args:
            cohort_name: e.g. 'diabetes_bh'.
        """
        pool = await _get_pool()

        # Identify cohort patient IDs
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
            return json.dumps({"error": f"Unknown cohort: {cohort_name}. Supported: diabetes_bh"})

        patient_ids = [str(r["id"]) for r in cohort_patients]
        cohort_size = len(patient_ids)

        if not patient_ids:
            return json.dumps({
                "cohort_name": cohort_name,
                "cohort_patients": 0,
                "corpus_rows": 0,
                "ready_for_training": False,
                "note": "No patients found for this cohort. Run seed.py --patients 100 to generate more.",
            })

        # Deliberation output stats
        stats = await pool.fetchrow(
            """
            SELECT
              COUNT(*) as total_rows,
              AVG(convergence_score) as avg_convergence,
              MAX(created_at) as last_deliberation,
              SUM(CASE WHEN vera_gate='allow' OR vera_gate IS NULL THEN 1 ELSE 0 END) as allow_rows,
              SUM(CASE WHEN vera_gate='flag' THEN 1 ELSE 0 END) as flag_rows,
              SUM(CASE WHEN vera_gate='block' THEN 1 ELSE 0 END) as block_rows,
              SUM(CASE WHEN convergence_score >= 0.80 THEN 1 ELSE 0 END) as high_convergence,
              SUM(CASE WHEN convergence_score >= 0.70 AND convergence_score < 0.80 THEN 1 ELSE 0 END) as medium_convergence,
              SUM(CASE WHEN convergence_score < 0.70 THEN 1 ELSE 0 END) as low_convergence
            FROM deliberation_outputs
            WHERE patient_id = ANY($1::uuid[])
            """,
            patient_ids,
        )

        eligible_rows = stats["allow_rows"]
        ready = eligible_rows >= 300

        return json.dumps({
            "cohort_name": cohort_name,
            "cohort_patients": cohort_size,
            "total_deliberation_rows": stats["total_rows"],
            "eligible_for_training": eligible_rows,
            "minimum_threshold": 300,
            "ready_for_training": ready,
            "vera_gate_distribution": {
                "allow": stats["allow_rows"],
                "flag": stats["flag_rows"],
                "block": stats["block_rows"],
            },
            "convergence_distribution": {
                "high_0.80+": stats["high_convergence"],
                "medium_0.70-0.80": stats["medium_convergence"],
                "low_below_0.70": stats["low_convergence"],
                "avg": round(float(stats["avg_convergence"] or 0), 3),
            },
            "last_deliberation": ensure_aware(stats["last_deliberation"]).isoformat()
            if stats["last_deliberation"] else None,
            "recommendation": (
                "Ready to train. Call trigger_cohort_training(cohort_name=...) to start."
                if ready
                else f"Need {300 - eligible_rows} more eligible rows. "
                     "Run run_deliberation(mode='full') on more cohort patients."
            ),
        })

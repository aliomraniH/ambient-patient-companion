"""Skill: slm_inference — Qwen 2.5 3B dedicated endpoint + Modal LoRA training.

Tools registered:
  call_slm                  — text generation via HF dedicated endpoint
  trigger_lora_training     — HMAC-signed POST to Modal training endpoint
  get_lora_training_status  — query lora_training_runs by job_id
  manage_hf_endpoint        — scale HF dedicated endpoint up / down

Environment variables consumed:
  HF_SLM_ENDPOINT_URL      Full URL of the HF dedicated endpoint
  HF_TOKEN                 HF access token (sent as Bearer for protected endpoints)
  HF_ENDPOINT_ADMIN_TOKEN  HF token with Endpoint Admin scope (may equal HF_TOKEN)
  HF_ENDPOINT_NAME         Endpoint name, e.g. companion-qwen25-3b
  HF_NAMESPACE             HF username or org handle
  MODAL_TRAIN_ENDPOINT_URL Full URL of the Modal training web endpoint
  MODAL_WEBHOOK_SECRET     Shared secret for HMAC-SHA256 request signing
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

import httpx

from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_TRAIN_TIMEOUT = 10.0


# ── Env helpers ───────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _require_env(key: str) -> str:
    val = _env(key)
    if not val:
        raise RuntimeError(
            f"slm_inference: required environment variable {key!r} is not set. "
            "Add it via Replit Secrets (Tools → Secrets)."
        )
    return val


# ── HMAC helpers ──────────────────────────────────────────────────────────────

def _sign_body(body: bytes, secret: str) -> str:
    """Return sha256=<hex> HMAC signature over *body* using *secret*."""
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def verify_modal_signature(body: bytes, header_value: str, secret: str) -> bool:
    """Constant-time check of the X-Hub-Signature-256 header sent by Modal."""
    expected = _sign_body(body, secret)
    return hmac.compare_digest(expected, header_value)


# ── Tool: call_slm ────────────────────────────────────────────────────────────

async def call_slm(
    prompt: str,
    system_message: str = "You are a helpful clinical assistant.",
    max_new_tokens: int = 512,
    temperature: float = 0.3,
) -> str:
    """Generate text from the Qwen 2.5 3B dedicated HuggingFace endpoint.

    Uses the OpenAI-compatible chat completions API exposed by HF Inference
    Endpoints (available for all TGI-backed models).

    Args:
        prompt:          User message / clinical question.
        system_message:  System prompt (defaults to clinical assistant persona).
        max_new_tokens:  Maximum tokens to generate (default 512).
        temperature:     Sampling temperature 0.0-1.0 (default 0.3 for clinical).

    Returns:
        JSON string with keys: generated_text, model, usage, endpoint_url.
        On error: JSON with status="error" and reason.
    """
    try:
        endpoint_url = _require_env("HF_SLM_ENDPOINT_URL")
        hf_token = _require_env("HF_TOKEN")
    except RuntimeError as e:
        return json.dumps({"status": "error", "reason": str(e)})

    chat_url = endpoint_url.rstrip("/") + "/v1/chat/completions"

    payload = {
        "model": "tgi",
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_new_tokens,
        "temperature": temperature,
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                chat_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {hf_token}",
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code == 503:
            return json.dumps({
                "status": "error",
                "reason": "HF endpoint is scaled down (503). "
                          "Use manage_hf_endpoint(action='scale_up') to restart it.",
                "endpoint_url": endpoint_url,
            })

        resp.raise_for_status()
        data = resp.json()

        generated = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        usage = data.get("usage", {})

        logger.info(
            "call_slm: generated %d tokens (prompt=%d, completion=%d)",
            usage.get("total_tokens", 0),
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )

        return json.dumps({
            "status": "ok",
            "generated_text": generated,
            "model": data.get("model", "qwen2.5-3b-instruct"),
            "usage": usage,
            "endpoint_url": endpoint_url,
        })

    except httpx.TimeoutException:
        return json.dumps({
            "status": "error",
            "reason": f"Request to HF endpoint timed out after {_TIMEOUT}s.",
        })
    except Exception as exc:
        logger.error("call_slm: %s", exc)
        return json.dumps({"status": "error", "reason": str(exc)})


# ── Tool: trigger_lora_training ───────────────────────────────────────────────

async def trigger_lora_training(
    dataset_path: str,
    job_id: str = "",
    base_model: str = "Qwen/Qwen2.5-3B-Instruct",
    epochs: int = 3,
    learning_rate: float = 2e-4,
) -> str:
    """Trigger a LoRA fine-tuning run on Modal.

    Signs the request body with HMAC-SHA256 (X-Hub-Signature-256 header) using
    MODAL_WEBHOOK_SECRET, then POSTs to MODAL_TRAIN_ENDPOINT_URL.  A pending
    row is written to lora_training_runs so get_lora_training_status can poll.

    Args:
        dataset_path:   Path or HF repo ID of the training dataset.
        job_id:         Optional caller-provided ID (auto-generated if blank).
        base_model:     HF model ID to fine-tune (default Qwen 2.5 3B Instruct).
        epochs:         Number of training epochs (default 3).
        learning_rate:  AdamW learning rate (default 2e-4).

    Returns:
        JSON with job_id, status, and Modal response or error details.
    """
    try:
        modal_url = _require_env("MODAL_TRAIN_ENDPOINT_URL")
        secret = _require_env("MODAL_WEBHOOK_SECRET")
    except RuntimeError as e:
        return json.dumps({"status": "error", "reason": str(e)})

    job_id = job_id.strip() or str(uuid.uuid4())

    payload = {
        "job_id": job_id,
        "dataset_path": dataset_path,
        "base_model": base_model,
        "epochs": epochs,
        "learning_rate": learning_rate,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
    }
    body_bytes = json.dumps(payload).encode()
    signature = _sign_body(body_bytes, secret)

    try:
        async with httpx.AsyncClient(timeout=_TRAIN_TIMEOUT) as client:
            resp = await client.post(
                modal_url,
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": signature,
                },
            )
        resp.raise_for_status()
        modal_response = resp.json()
    except httpx.TimeoutException:
        return json.dumps({
            "status": "error",
            "job_id": job_id,
            "reason": f"Modal endpoint timed out after {_TRAIN_TIMEOUT}s.",
        })
    except Exception as exc:
        logger.error("trigger_lora_training: Modal call failed: %s", exc)
        return json.dumps({
            "status": "error",
            "job_id": job_id,
            "reason": str(exc),
        })

    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO lora_training_runs
                    (job_id, status, triggered_at, base_model, dataset_path, metadata)
                VALUES ($1, 'pending', NOW(), $2, $3, $4)
                ON CONFLICT (job_id) DO UPDATE
                    SET status = 'pending',
                        triggered_at = NOW(),
                        metadata = EXCLUDED.metadata
                """,
                job_id,
                base_model,
                dataset_path,
                json.dumps(modal_response),
            )
        logger.info("trigger_lora_training: job %s written to DB", job_id)
    except Exception as exc:
        logger.error("trigger_lora_training: DB write failed: %s", exc)

    return json.dumps({
        "status": "pending",
        "job_id": job_id,
        "modal_response": modal_response,
        "message": "Training job queued. Poll get_lora_training_status for updates.",
    })


# ── Tool: get_lora_training_status ────────────────────────────────────────────

async def get_lora_training_status(job_id: str) -> str:
    """Query the status of a LoRA training run.

    Args:
        job_id: The job_id returned by trigger_lora_training.

    Returns:
        JSON with status, triggered_at, completed_at, error_message, metadata.
        status values: pending | running | completed | failed
    """
    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT job_id, status, triggered_at, completed_at,
                       base_model, dataset_path, error_message, metadata
                FROM lora_training_runs
                WHERE job_id = $1
                """,
                job_id,
            )
    except Exception as exc:
        logger.error("get_lora_training_status: DB error: %s", exc)
        return json.dumps({"status": "error", "reason": str(exc)})

    if row is None:
        return json.dumps({
            "status": "not_found",
            "job_id": job_id,
            "message": "No training run found with this job_id.",
        })

    return json.dumps({
        "job_id": row["job_id"],
        "status": row["status"],
        "base_model": row["base_model"],
        "dataset_path": row["dataset_path"],
        "triggered_at": row["triggered_at"].isoformat() if row["triggered_at"] else None,
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
        "error_message": row["error_message"],
        "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
    }, default=str)


# ── Tool: manage_hf_endpoint ──────────────────────────────────────────────────

async def manage_hf_endpoint(action: str) -> str:
    """Scale a HuggingFace Inference Endpoint up or down.

    Uses the HF Inference Endpoints Admin API to change the replica count.
    scale_up   → sets initialNumberOfReplicas=1  (wakes a paused endpoint)
    scale_down → sets initialNumberOfReplicas=0  (pauses, stops billing)
    status     → returns current endpoint state without making changes

    Args:
        action: One of 'scale_up', 'scale_down', 'status'.

    Returns:
        JSON with endpoint name, action taken, and current state from HF API.
    """
    try:
        admin_token = _require_env("HF_ENDPOINT_ADMIN_TOKEN")
        namespace = _require_env("HF_NAMESPACE")
        endpoint_name = _require_env("HF_ENDPOINT_NAME")
    except RuntimeError as e:
        return json.dumps({"status": "error", "reason": str(e)})

    action = action.strip().lower()
    if action not in ("scale_up", "scale_down", "status"):
        return json.dumps({
            "status": "error",
            "reason": f"Unknown action {action!r}. Use scale_up, scale_down, or status.",
        })

    base = f"https://api.endpoints.huggingface.cloud/v2/endpoint/{namespace}/{endpoint_name}"
    headers = {
        "Authorization": f"Bearer {admin_token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if action == "status":
                resp = await client.get(base, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return json.dumps({
                    "status": "ok",
                    "action": "status",
                    "endpoint_name": endpoint_name,
                    "endpoint_state": data.get("status", {}).get("state"),
                    "replicas": data.get("compute", {}).get("scaling", {}).get("currentReplica"),
                    "raw": data,
                }, default=str)

            replicas = 1 if action == "scale_up" else 0
            resp = await client.patch(
                base,
                headers=headers,
                json={"compute": {"scaling": {"minReplica": replicas, "maxReplica": max(replicas, 1)}}},
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info("manage_hf_endpoint: %s → replicas=%d", action, replicas)

            return json.dumps({
                "status": "ok",
                "action": action,
                "endpoint_name": endpoint_name,
                "requested_replicas": replicas,
                "endpoint_state": data.get("status", {}).get("state"),
                "message": (
                    "Endpoint is scaling up. May take 1-2 min to become ready."
                    if action == "scale_up"
                    else "Endpoint is pausing. Billing stops when fully scaled down."
                ),
            }, default=str)

    except httpx.HTTPStatusError as exc:
        logger.error("manage_hf_endpoint: HF API error %s: %s", exc.response.status_code, exc)
        return json.dumps({
            "status": "error",
            "action": action,
            "http_status": exc.response.status_code,
            "reason": exc.response.text,
        })
    except Exception as exc:
        logger.error("manage_hf_endpoint: %s", exc)
        return json.dumps({"status": "error", "action": action, "reason": str(exc)})


# ── REST endpoint: modal webhook receiver ─────────────────────────────────────
# Registered on the Skills MCP server so the Next.js proxy can forward
# validated Modal callbacks to update lora_training_runs rows.

async def _handle_modal_webhook_internal(body: dict) -> dict:
    """Update a lora_training_runs row from a validated Modal callback."""
    job_id = body.get("job_id", "")
    new_status = body.get("status", "")
    error_message = body.get("error_message")
    metadata = body.get("metadata")

    if not job_id or not new_status:
        return {"ok": False, "reason": "job_id and status are required"}

    valid_statuses = {"pending", "running", "completed", "failed"}
    if new_status not in valid_statuses:
        return {"ok": False, "reason": f"Invalid status {new_status!r}"}

    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE lora_training_runs
                SET status        = $1,
                    completed_at  = CASE WHEN $1 IN ('completed', 'failed') THEN NOW() ELSE completed_at END,
                    error_message = COALESCE($2, error_message),
                    metadata      = CASE WHEN $3::text IS NOT NULL
                                         THEN $3::jsonb
                                         ELSE metadata
                                    END
                WHERE job_id = $4
                """,
                new_status,
                error_message,
                json.dumps(metadata) if metadata else None,
                job_id,
            )
        updated = result.split()[-1] != "0"
        logger.info(
            "modal_webhook: job %s → %s (row_updated=%s)", job_id, new_status, updated
        )
        return {"ok": True, "job_id": job_id, "status": new_status, "row_updated": updated}
    except Exception as exc:
        logger.error("modal_webhook DB update failed: %s", exc)
        return {"ok": False, "reason": str(exc)}


# ── Registration ──────────────────────────────────────────────────────────────

def register(mcp: FastMCP) -> None:
    mcp.tool(call_slm)
    mcp.tool(trigger_lora_training)
    mcp.tool(get_lora_training_status)
    mcp.tool(manage_hf_endpoint)

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/tools/modal_webhook_internal", methods=["POST"])
    async def rest_modal_webhook_internal(request: Request) -> JSONResponse:
        """Internal REST endpoint — called by the Next.js webhook route after
        HMAC verification.  Not exposed publicly; sits behind the proxy."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "reason": "Invalid JSON"}, status_code=400)
        result = await _handle_modal_webhook_internal(body)
        status_code = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=status_code)

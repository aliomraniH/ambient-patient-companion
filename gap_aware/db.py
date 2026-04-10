"""Database helpers for gap-aware tools. Provides a shared asyncpg pool."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import asyncpg

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# ---------------------------------------------------------------------------
# Connection pool (singleton per process)
# ---------------------------------------------------------------------------

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    """Return the shared asyncpg connection pool, creating it on first call."""
    global _pool
    if _pool is None:
        dsn = os.environ.get("DATABASE_URL", "")
        if not dsn:
            raise RuntimeError("DATABASE_URL not set")
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    return _pool


async def close_pool() -> None:
    """Close the connection pool if it exists."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# MRN → UUID resolution (mirrors context_compiler.py:75-104)
# ---------------------------------------------------------------------------

async def resolve_mrn_to_uuid(patient_mrn: str) -> Optional[str]:
    """Resolve a patient MRN (or UUID string) to the internal UUID.

    Uses the same 3-step lookup as context_compiler.py:
      1. Exact MRN match
      2. UUID match (if input looks like a UUID)
      3. Partial MRN LIKE match
    Returns the UUID as a string, or None if not found.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM patients WHERE mrn = $1", patient_mrn
        )
        if row:
            return str(row["id"])

        if _UUID_RE.match(patient_mrn):
            row = await conn.fetchrow(
                "SELECT id FROM patients WHERE id = $1::uuid", patient_mrn
            )
            if row:
                return str(row["id"])

        row = await conn.fetchrow(
            "SELECT id FROM patients WHERE mrn LIKE $1",
            f"%{patient_mrn}%",
        )
        if row:
            return str(row["id"])

    return None


# ---------------------------------------------------------------------------
# reasoning_gaps
# ---------------------------------------------------------------------------

async def insert_reasoning_gap(
    deliberation_id: str,
    patient_mrn: str,
    emitting_agent: str,
    gap_id: str,
    artifact: Dict[str, Any],
) -> str:
    """Insert a reasoning gap artifact. Returns the row UUID."""
    pool = await get_pool()
    row_id = str(uuid.uuid4())
    expires_at = artifact.get("expires_at")
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            expires_at = None

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO reasoning_gaps (
                id, deliberation_id, patient_mrn, emitting_agent, gap_id,
                gap_type, severity, description, impact_statement,
                confidence_without_res, confidence_with_res,
                attempted_resolutions, recommended_action, caveat_text,
                expires_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9,
                $10, $11,
                $12, $13, $14,
                $15
            )
            ON CONFLICT DO NOTHING
            """,
            row_id,
            deliberation_id,
            patient_mrn,
            emitting_agent,
            gap_id,
            artifact.get("gap_type", "missing_data"),
            artifact.get("severity", "medium"),
            artifact.get("description", ""),
            artifact.get("impact_statement"),
            artifact.get("confidence_without_resolution"),
            artifact.get("confidence_with_resolution"),
            json.dumps(artifact.get("attempted_resolutions", [])),
            artifact.get("recommended_action_for_synthesis"),
            artifact.get("caveat_text"),
            expires_at,
        )
    return row_id


async def get_gaps_for_deliberation(deliberation_id: str) -> List[Dict[str, Any]]:
    """Return all reasoning gaps for a deliberation, ordered by severity."""
    pool = await get_pool()
    severity_order = "CASE severity WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 ELSE 4 END"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM reasoning_gaps WHERE deliberation_id = $1 ORDER BY {severity_order}",
            deliberation_id,
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# clarification_requests
# ---------------------------------------------------------------------------

async def insert_clarification_request(req: Dict[str, Any]) -> str:
    """Insert a clarification request. Returns the clarification_id."""
    pool = await get_pool()
    clarification_id = f"clar_{uuid.uuid4().hex[:12]}"
    timeout_minutes = req.get("timeout_minutes", 60)
    timeout_at = datetime.now(timezone.utc) + timedelta(minutes=timeout_minutes)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO clarification_requests (
                clarification_id, deliberation_id, gap_id, requesting_agent,
                recipient, recipient_agent_id, urgency, question_text,
                clinical_rationale, suggested_options, default_if_unanswered,
                fallback_behavior, timeout_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            """,
            clarification_id,
            req["deliberation_id"],
            req["gap_id"],
            req["requesting_agent"],
            req["recipient"],
            req.get("recipient_agent_id"),
            req["urgency"],
            req["question"]["text"],
            req["question"].get("clinical_rationale"),
            json.dumps(req["question"].get("suggested_options", [])),
            req["question"].get("default_if_unanswered"),
            req.get("fallback_behavior", "escalate_to_synthesis"),
            timeout_at,
        )
    return clarification_id


# ---------------------------------------------------------------------------
# gap_triggers
# ---------------------------------------------------------------------------

async def insert_gap_trigger(req: Dict[str, Any]) -> str:
    """Insert a gap trigger. Returns the trigger_id."""
    pool = await get_pool()
    trigger_id = f"trig_{uuid.uuid4().hex[:12]}"
    expires_at = req.get("expires_at")
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            expires_at = datetime.now(timezone.utc) + timedelta(days=7)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO gap_triggers (
                trigger_id, patient_mrn, gap_id, watch_for,
                loinc_code, snomed_code, custom_condition,
                trigger_type, on_fire_action, deliberation_scope, expires_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """,
            trigger_id,
            req["patient_mrn"],
            req["gap_id"],
            req["trigger_condition"]["watch_for"],
            req["trigger_condition"].get("loinc_code"),
            req["trigger_condition"].get("snomed_code"),
            req["trigger_condition"].get("custom_condition"),
            req.get("trigger_type", "gap_resolution_received"),
            req["on_fire_action"],
            json.dumps(req.get("deliberation_scope", ["full_council"])),
            expires_at,
        )
    return trigger_id


# ---------------------------------------------------------------------------
# knowledge_search_cache
# ---------------------------------------------------------------------------

async def check_knowledge_cache(cache_key: str) -> Optional[List[Dict[str, Any]]]:
    """Return cached results if the key exists and has not expired."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT results FROM knowledge_search_cache WHERE cache_key = $1 AND expires_at > NOW()",
            cache_key,
        )
    if row:
        return json.loads(row["results"])
    return None


async def write_knowledge_cache(
    cache_key: str,
    query_type: str,
    source: str,
    results: List[Dict[str, Any]],
    ttl_hours: int = 720,
) -> None:
    """Upsert a knowledge cache entry. expires_at computed in Python."""
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl_hours)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO knowledge_search_cache (cache_key, query_type, source, results, ttl_hours, created_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (cache_key) DO UPDATE
                SET results = $4, created_at = $6, expires_at = $7
            """,
            cache_key, query_type, source, json.dumps(results), ttl_hours, now, expires_at,
        )

"""MCP tool: search_similar_atoms — pgvector retrieval over the
behavioral_signal_atoms HNSW index (migration 010).

Scope modes:
  - 'patient': cosine-similarity search within one patient's own atoms.
    Returns full atom rows (including signal_value) — intended for
    gap-aware agents pulling historical context for that patient.
  - 'cohort': cross-patient aggregate search. Returns counts/stats only;
    `signal_value` is redacted to protect PHI, as the matches may span
    patients not in the current care context.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Optional

from fastmcp import FastMCP

from db.connection import get_pool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 10
MAX_TOP_K = 100


def _jsonable_row(row) -> dict:
    from datetime import date, datetime
    out: dict = {}
    for k, v in dict(row).items():
        if isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        elif v is None or isinstance(v, (bool, int, float, str)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


async def search_similar_atoms(
    patient_id: str,
    query_text: Optional[str] = None,
    query_signal_type: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    scope: str = "patient",
) -> str:
    """Find behavioral atoms most similar to a query.

    Args:
        patient_id: UUID of the focal patient (required even in cohort
            scope, to anchor the query and log provenance).
        query_text: Free-text query to embed and search against.
            Mutually optional with `query_signal_type`; at least one is
            required.
        query_signal_type: If provided (and query_text is not), search
            for atoms near any of this patient's own atoms of that
            signal_type. Useful for "find trajectories like THIS".
        top_k: Number of neighbours to return (capped at MAX_TOP_K).
        scope: 'patient' (default) or 'cohort'.

    Returns:
        JSON string with a ranked list of atoms (patient scope) or
        aggregate domain counts (cohort scope).
    """
    try:
        top_k = max(1, min(int(top_k), MAX_TOP_K))
    except (TypeError, ValueError):
        top_k = DEFAULT_TOP_K

    if scope not in ("patient", "cohort"):
        return json.dumps({"status": "error", "detail": "invalid_scope"})

    if not query_text and not query_signal_type:
        return json.dumps({
            "status": "error",
            "detail": "query_text or query_signal_type required",
        })

    # Resolve query vector.
    query_vec_literal: Optional[str] = None
    try:
        from skills.atom_embedder import embed_signal_value, format_for_pgvector
        if query_text:
            qv = embed_signal_value(query_text)
            query_vec_literal = format_for_pgvector(qv)
    except Exception as e:
        logger.warning("Query embedding failed: %s", type(e).__name__)

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # If the query was by signal_type, use the patient's own most
            # recent atom of that type as the seed vector.
            if query_vec_literal is None and query_signal_type:
                seed_row = await conn.fetchrow(
                    """SELECT embedding
                         FROM behavioral_signal_atoms
                        WHERE patient_id = $1::uuid
                          AND signal_type = $2
                          AND embedding IS NOT NULL
                        ORDER BY clinical_date DESC
                        LIMIT 1""",
                    patient_id, query_signal_type,
                )
                if not seed_row or seed_row["embedding"] is None:
                    return json.dumps({
                        "status": "no_seed_vector",
                        "patient_id": patient_id,
                        "signal_type": query_signal_type,
                    })
                # asyncpg returns pgvector as a string already formatted as
                # "[v1,v2,...]" — we can feed it right back.
                query_vec_literal = str(seed_row["embedding"])

            if not query_vec_literal:
                return json.dumps({
                    "status": "error",
                    "detail": "could_not_build_query_vector",
                })

            if scope == "patient":
                rows = await conn.fetch(
                    """SELECT id, clinical_date, note_section, signal_type,
                              signal_value, assertion, confidence,
                              (embedding <=> $1::vector) AS distance
                         FROM behavioral_signal_atoms
                        WHERE patient_id = $2::uuid
                          AND embedding IS NOT NULL
                        ORDER BY embedding <=> $1::vector
                        LIMIT $3""",
                    query_vec_literal, patient_id, top_k,
                )
                return json.dumps({
                    "status": "ok",
                    "scope": "patient",
                    "patient_id": patient_id,
                    "count": len(rows),
                    "atoms": [_jsonable_row(r) for r in rows],
                })

            # cohort scope — redact signal_value, aggregate by signal_type.
            rows = await conn.fetch(
                """SELECT signal_type,
                          COUNT(*) AS match_count,
                          MIN(embedding <=> $1::vector) AS min_distance,
                          AVG(embedding <=> $1::vector) AS avg_distance
                     FROM behavioral_signal_atoms
                    WHERE embedding IS NOT NULL
                      AND patient_id <> $2::uuid
                      AND (embedding <=> $1::vector) < 0.5
                    GROUP BY signal_type
                    ORDER BY avg_distance ASC
                    LIMIT $3""",
                query_vec_literal, patient_id, top_k,
            )
            return json.dumps({
                "status": "ok",
                "scope": "cohort",
                "count": len(rows),
                "aggregate": [
                    {
                        "signal_type": r["signal_type"],
                        "match_count": int(r["match_count"]),
                        "min_distance": float(r["min_distance"] or 0),
                        "avg_distance": float(r["avg_distance"] or 0),
                    }
                    for r in rows
                ],
            })
    except Exception as e:
        # PHI rule: log type only, never the query text.
        logger.warning("search_similar_atoms failed: %s", type(e).__name__)
        return json.dumps({"status": "error", "detail": type(e).__name__})


def register(mcp: FastMCP) -> None:
    mcp.tool(search_similar_atoms)

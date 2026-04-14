"""
atom_vector_search.py — Pgvector-backed similarity search over behavioral_signal_atoms.

The cohort search (search_similar_atoms) NEVER returns raw signal_value text
(PHI). It returns aggregated similarity stats per patient_id + signal_type.

All functions are async (asyncpg).
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

_EMBED_DIM = 768


async def search_similar_atoms(
    pool,
    query_embedding: list[float],
    patient_id: Optional[str] = None,
    signal_type: Optional[str] = None,
    top_k: int = 10,
    min_similarity: float = 0.75,
    days_lookback: int = 90,
) -> list[dict]:
    """Search for atoms whose embedding is close to query_embedding.

    Scope:
      - If patient_id is given: search only that patient's atoms.
        Returns full atom metadata (id, signal_type, confidence, source_type,
        extracted_at, similarity) — NO signal_value (PHI).
      - If patient_id is None: cohort search.
        Returns aggregated stats per (patient_id, signal_type); NO signal_value.

    Args:
        pool:            asyncpg connection pool.
        query_embedding: 768-dim embedding vector.
        patient_id:      UUID string or None.
        signal_type:     filter to one signal type (optional).
        top_k:           max rows to return.
        min_similarity:  cosine similarity threshold (0.0–1.0).
        days_lookback:   restrict to atoms extracted within this many days.

    Returns:
        List of dicts. PHI-safe: signal_value is never included.
    """
    if len(query_embedding) != _EMBED_DIM:
        log.warning("search_similar_atoms: expected %d dims, got %d", _EMBED_DIM, len(query_embedding))
        return []

    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    async with pool.acquire() as conn:
        if patient_id:
            # Single-patient search — return atom-level rows (no PHI)
            signal_filter = "AND signal_type = $4" if signal_type else ""
            params: list = [embedding_str, days_lookback, top_k]
            if signal_type:
                params.append(signal_type)

            sql = f"""
                SELECT
                    id::text,
                    patient_id::text,
                    signal_type,
                    confidence,
                    source_type,
                    extracted_at,
                    1 - (embedding <=> $1::vector) AS similarity
                FROM behavioral_signal_atoms
                WHERE patient_id = '{patient_id}'::uuid
                  AND embedding IS NOT NULL
                  AND extracted_at >= NOW() - ($2 || ' days')::INTERVAL
                  {signal_filter}
                  AND 1 - (embedding <=> $1::vector) >= {min_similarity}
                ORDER BY embedding <=> $1::vector
                LIMIT $3
            """
            rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]

        else:
            # Cohort search — aggregated, no signal_value
            signal_filter = "AND signal_type = $3" if signal_type else ""
            params = [embedding_str, days_lookback]
            if signal_type:
                params.append(signal_type)

            sql = f"""
                SELECT
                    patient_id::text,
                    signal_type,
                    COUNT(*)                                           AS atom_count,
                    AVG(confidence)                                    AS avg_confidence,
                    AVG(1 - (embedding <=> $1::vector))                AS avg_similarity,
                    MAX(1 - (embedding <=> $1::vector))                AS max_similarity,
                    MAX(extracted_at)                                  AS last_seen_at
                FROM behavioral_signal_atoms
                WHERE embedding IS NOT NULL
                  AND extracted_at >= NOW() - ($2 || ' days')::INTERVAL
                  {signal_filter}
                  AND 1 - (embedding <=> $1::vector) >= {min_similarity}
                GROUP BY patient_id, signal_type
                ORDER BY avg_similarity DESC
                LIMIT ${ len(params) + 1 }
            """
            params.append(top_k)
            rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]


async def get_atom_pressure_for_patient(
    pool,
    patient_id: str,
    signal_types: Optional[list[str]] = None,
) -> dict[str, dict]:
    """Read atom pressure from the materialized view for one patient.

    Returns: {signal_type: {pressure_score, present_atom_count, last_atom_at}}
    """
    async with pool.acquire() as conn:
        if signal_types:
            rows = await conn.fetch(
                """
                SELECT signal_type, pressure_score, present_atom_count, last_atom_at
                FROM atom_pressure_scores
                WHERE patient_id = $1::uuid
                  AND signal_type = ANY($2::text[])
                """,
                patient_id, signal_types,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT signal_type, pressure_score, present_atom_count, last_atom_at
                FROM atom_pressure_scores
                WHERE patient_id = $1::uuid
                """,
                patient_id,
            )
    return {
        r["signal_type"]: {
            "pressure_score": float(r["pressure_score"] or 0.0),
            "present_atom_count": r["present_atom_count"],
            "last_atom_at": r["last_atom_at"].isoformat() if r["last_atom_at"] else None,
        }
        for r in rows
    }


async def refresh_atom_pressure_view(pool) -> bool:
    """Refresh the atom_pressure_scores materialized view. Returns True on success."""
    try:
        async with pool.acquire() as conn:
            await conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY atom_pressure_scores")
        return True
    except Exception as e:
        log.warning("refresh_atom_pressure_view failed: %s", e)
        # Fall back to non-concurrent refresh
        try:
            async with pool.acquire() as conn:
                await conn.execute("REFRESH MATERIALIZED VIEW atom_pressure_scores")
            return True
        except Exception as e2:
            log.error("atom_pressure refresh failed entirely: %s", e2)
            return False

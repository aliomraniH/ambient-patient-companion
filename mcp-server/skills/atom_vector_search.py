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
    top_k: int = 5,
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
            # Single-patient search — return atom-level rows (no PHI).
            # Fully parameterized: $1=embedding, $2=days_lookback, $3=top_k,
            # $4=patient_id, $5=min_similarity, [$6=signal_type].
            params: list = [embedding_str, days_lookback, top_k, patient_id, min_similarity]
            if signal_type:
                params.append(signal_type)
                signal_filter = "AND signal_type = $6"
            else:
                signal_filter = ""

            sql = (
                "SELECT id::text, patient_id::text, signal_type, confidence,"
                "       source_type, extracted_at,"
                "       1 - (embedding <=> $1::vector) AS similarity"
                " FROM behavioral_signal_atoms"
                " WHERE patient_id = $4::uuid"
                "   AND embedding IS NOT NULL"
                "   AND extracted_at >= NOW() - ($2 * INTERVAL '1 day')"
                f"  {signal_filter}"
                "   AND 1 - (embedding <=> $1::vector) >= $5"
                " ORDER BY embedding <=> $1::vector"
                " LIMIT $3"
            )
            rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]

        else:
            # Cohort search — aggregated, no signal_value.
            # $1=embedding, $2=days_lookback, $3=min_similarity,
            # [$4=signal_type], $last=top_k.
            params = [embedding_str, days_lookback, min_similarity]
            if signal_type:
                params.append(signal_type)
                signal_filter = "AND signal_type = $4"
            else:
                signal_filter = ""
            params.append(top_k)
            top_k_pos = len(params)

            sql = (
                "SELECT patient_id::text, signal_type,"
                "       COUNT(*)                            AS atom_count,"
                "       AVG(confidence)                     AS avg_confidence,"
                "       AVG(1 - (embedding <=> $1::vector)) AS avg_similarity,"
                "       MAX(1 - (embedding <=> $1::vector)) AS max_similarity,"
                "       MAX(extracted_at)                   AS last_seen_at"
                " FROM behavioral_signal_atoms"
                " WHERE embedding IS NOT NULL"
                "   AND extracted_at >= NOW() - ($2 * INTERVAL '1 day')"
                f"  {signal_filter}"
                "   AND 1 - (embedding <=> $1::vector) >= $3"
                " GROUP BY patient_id, signal_type"
                " ORDER BY avg_similarity DESC"
                f" LIMIT ${top_k_pos}"
            )
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


_search_atoms_impl = search_similar_atoms


def register(mcp) -> None:

    @mcp.tool()
    async def search_similar_atoms(
        query_text: str,
        patient_id: Optional[str] = None,
        signal_type: Optional[str] = None,
        top_k: int = 5,
        min_similarity: float = 0.75,
        days_lookback: int = 90,
        scope: str = "patient",
    ) -> dict:
        """Search behavioral signal atoms by semantic similarity to query_text.

        Embeds query_text using the active embedding backend, then performs
        vector cosine search over behavioral_signal_atoms.

        Scope:
          - 'patient'  (requires patient_id): returns per-atom metadata;
            signal_value is NEVER included (PHI).
          - 'cohort'   (patient_id ignored): returns aggregated stats per
            (patient_id, signal_type); fully de-identified aggregate view.

        Args:
            query_text:     Clinical or lay-language description to embed.
            patient_id:     Required for scope='patient'. Ignored for 'cohort'.
            signal_type:    Narrow to one signal type (optional).
            top_k:          Max rows to return (default 10).
            min_similarity: Cosine similarity threshold 0.0–1.0 (default 0.75).
            days_lookback:  Restrict to atoms extracted within this many days.
            scope:          'patient' or 'cohort' (default 'cohort').

        Returns:
            {scope, result_count, results: [...]}
        """
        from db.connection import get_pool
        from skills.atom_embedder import embed_signal_value

        embedding = embed_signal_value(query_text[:500])
        if embedding is None:
            return {"scope": scope, "result_count": 0, "results": [],
                    "error": "embedding_unavailable"}

        pool = await get_pool()
        pid = patient_id if scope == "patient" else None

        results = await _search_atoms_impl(
            pool=pool,
            query_embedding=embedding,
            patient_id=pid,
            signal_type=signal_type,
            top_k=top_k,
            min_similarity=min_similarity,
            days_lookback=days_lookback,
        )

        return {"scope": scope, "result_count": len(results), "results": results}

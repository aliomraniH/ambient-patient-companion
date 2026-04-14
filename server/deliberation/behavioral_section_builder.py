"""Post-synthesis behavioral section builder.

Pure functions that shape a mode-aware behavioral section for a given role
(pcp | care_manager | patient). Called by the synthesis stage after the
monolithic synthesizer LLM call, so that Mode A / Mode B routing is
deterministic rather than left to the LLM.

This module does NOT perform any LLM calls and does NOT touch the DB.
The caller is expected to have already fetched the behavioral context
(e.g. via the `get_behavioral_context` MCP tool on the skills server) and
pass it in as a plain dict.
"""
from __future__ import annotations

from typing import Optional


def build_behavioral_section(
    behavioral_context: dict,
    role: str,
) -> dict:
    """Route to the appropriate mode-specific builder based on evidence_mode."""
    mode = (behavioral_context or {}).get("mode", "contextual")
    temporal_conf = behavioral_context.get("temporal_confidence") or "low"

    if mode == "primary_evidence":
        return _build_mode_b_section(behavioral_context, role, temporal_conf)
    return _build_mode_a_section(behavioral_context, role)


def _build_mode_b_section(ctx: dict, role: str, temporal_conf: str) -> dict:
    """Mode B: atoms are the primary clinical evidence (no formal screen)."""
    if role == "patient":
        return {
            "type": "behavioral_routing",
            "message": "Your care team has some questions to follow up on at your next visit.",
            "action": "route_to_provider",
            "show_atoms": False,
        }

    if temporal_conf == "very_low" and role != "pcp":
        return {
            "type": "behavioral_gap_suppressed",
            "reason": "historical_signal_age",
            "note": "Behavioral atoms >7 years old. PCP opt-in required to surface.",
        }

    atoms = ctx.get("atoms", []) or []
    instruments = ctx.get("recommended_instruments", []) or []

    base: dict = {
        "type": "behavioral_screening_gap",
        "headline": ctx.get("headline", "No formal behavioral health screening on file"),
        "gap_type": ctx.get("gap_type"),
        "temporal_confidence": temporal_conf,
        "atoms": atoms,
        "recommended_instruments": instruments,
        "atom_count": ctx.get("atom_count", 0),
        "atom_date_range": ctx.get("atom_date_range"),
    }

    if role == "pcp":
        suggestion = ", ".join(instruments[:2]) if instruments else "PHQ-9"
        base["pcp_note"] = (
            "These behavioral observations were documented in clinical notes "
            "but no formal behavioral health screening is on file. "
            f"Consider administering {suggestion} at next visit."
        )
        base["show_at_top"] = True

    elif role == "care_manager":
        base["action_required"] = {
            "type": "outreach_task",
            "description": (
                "Confirm whether behavioral screening has been administered "
                "outside this system. Schedule if not."
            ),
            "priority": "high" if temporal_conf in ("high", "moderate") else "medium",
        }

    return base


def _build_mode_a_section(ctx: dict, role: str) -> dict:
    """Mode A: structured score exists; atoms enrich interpretation."""
    phq = ctx.get("latest_phq9")
    item9 = ctx.get("item_9_score") or 0
    atoms = ctx.get("historical_atoms", []) or []

    base: dict = {
        "type": "behavioral_score_with_context",
        "headline": ctx.get("headline"),
        "phq9_total": phq["total_score"] if phq else None,
        "item9_score": item9,
        "item9_flag": item9 >= 1,
        "trajectory": ctx.get("trajectory"),
        "historical_atoms": atoms,
        "atom_count": len(atoms),
    }

    if item9 >= 1 and role != "patient":
        last_date = (phq or {}).get("observation_date", "unknown")
        base["item9_alert"] = (
            "PHQ-9 item 9 (passive SI) = 1 on most recent screen. "
            f"Clinical assessment required. Last documented: {last_date}."
        )

    if atoms and role in ("pcp", "care_manager"):
        base["context_note"] = (
            f"{len(atoms)} historical behavioral signal(s) found in clinical notes. "
            "Score should be interpreted in light of this longitudinal pattern."
        )

    if role == "patient":
        base.pop("historical_atoms", None)
        base.pop("context_note", None)

    return base


async def fetch_behavioral_context(db_pool, patient_id: str) -> Optional[dict]:
    """Read behavioral_phenotypes → assemble a context dict equivalent to what
    the `get_behavioral_context` MCP tool returns.

    Separated from the skills server so the deliberation engine can call it
    without a cross-process MCP round-trip. Returns None if no phenotype or
    on any fetch error — always safe to pass to `build_behavioral_section`.
    """
    if db_pool is None or not patient_id:
        return None
    try:
        async with db_pool.acquire() as conn:
            phenotype = await conn.fetchrow(
                "SELECT * FROM behavioral_phenotypes WHERE patient_id = $1::uuid",
                patient_id,
            )
            if not phenotype:
                return None
            mode = phenotype["evidence_mode"] or "contextual"

            if mode == "primary_evidence":
                gap = await conn.fetchrow(
                    "SELECT * FROM behavioral_screening_gaps "
                    "WHERE patient_id = $1::uuid AND status = 'open' "
                    "ORDER BY detected_at DESC LIMIT 1",
                    patient_id,
                )
                if gap:
                    atom_rows = await conn.fetch(
                        "SELECT clinical_date, note_section, signal_type, "
                        "signal_value, confidence "
                        "FROM behavioral_signal_atoms "
                        "WHERE id = ANY($1::uuid[]) AND assertion = 'present' "
                        "ORDER BY clinical_date ASC",
                        gap["atom_ids"],
                    )
                    drange = gap["atom_date_range"]
                    return {
                        "mode": "primary_evidence",
                        "status": "screening_gap_open",
                        "gap_type": gap["gap_type"],
                        "gap_id": str(gap["id"]),
                        "temporal_confidence": gap["temporal_confidence"],
                        "atom_count": gap["atom_count"],
                        "atom_date_range": {
                            "earliest": drange.lower.isoformat() if drange and drange.lower else None,
                            "latest": drange.upper.isoformat() if drange and drange.upper else None,
                        },
                        "atoms": [dict(a) for a in atom_rows],
                        "recommended_instruments": list(
                            gap["recommended_instruments"] or []
                        ),
                        "headline": "No formal behavioral health screening on file",
                    }
                mode = "contextual"

            latest_phq = await conn.fetchrow(
                "SELECT * FROM phq9_observations "
                "WHERE patient_id = $1::uuid "
                "ORDER BY observation_date DESC LIMIT 1",
                patient_id,
            )
            historical_atoms = await conn.fetch(
                "SELECT clinical_date, note_section, signal_type, "
                "signal_value, confidence "
                "FROM behavioral_signal_atoms "
                "WHERE patient_id = $1::uuid AND assertion = 'present' "
                "ORDER BY clinical_date ASC",
                patient_id,
            )
            return {
                "mode": "contextual",
                "status": "screening_exists" if latest_phq else "no_screening",
                "latest_phq9": dict(latest_phq) if latest_phq else None,
                "item_9_score": latest_phq["item_9_score"] if latest_phq else None,
                "item_9_flag": ((latest_phq["item_9_score"] or 0) >= 1) if latest_phq else False,
                "trajectory": phenotype["trajectory_status"],
                "temporal_confidence": phenotype["temporal_confidence"],
                "historical_atoms": [dict(a) for a in historical_atoms],
                "atom_count": len(historical_atoms),
                "headline": (f"PHQ-9 = {latest_phq['total_score']}"
                             if latest_phq else "No PHQ-9 on file"),
            }
    except Exception:
        return None


async def augment_result_with_behavioral_section(
    result,
    db_pool,
    patient_id: str,
    role: str = "pcp",
) -> None:
    """Populate `result.behavioral_section` in-place on a DeliberationResult.

    Safe no-op when no phenotype exists or on any fetch failure.
    """
    try:
        ctx = await fetch_behavioral_context(db_pool, patient_id)
        if not ctx:
            return
        result.behavioral_section = build_behavioral_section(ctx, role)
    except Exception:
        # Never let behavioral augmentation break deliberation.
        return

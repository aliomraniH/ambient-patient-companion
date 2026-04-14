"""
behavioral_section_builder.py — Assemble behavioral_section for DeliberationResult.

Takes open gaps + screening history and produces a list[dict] suitable for
the behavioral_section field in DeliberationResult (V2: list[dict], not str).

Also exposes build_behavioral_section_for_deliberation() which is called
by the deliberation engine's synthesizer (or by tests).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


def _isoformat(dt) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


async def build_behavioral_section_for_deliberation(
    pool,
    patient_id: str,
    requesting_role: Optional[str] = None,
) -> list[dict]:
    """Build the behavioral_section payload for a deliberation result.

    Returns a list of behavioural section entries — one per relevant domain —
    including screening history summary, open gaps, atom pressure, and
    suggested instruments. Cards are role-filtered if requesting_role is given.

    This function is designed to be called inside the deliberation engine
    after gap detection has run. It does NOT re-run gap detection.

    Args:
        pool:             asyncpg connection pool.
        patient_id:       UUID of the patient.
        requesting_role:  Optional role for card filtering.

    Returns:
        list[dict] — empty list if no behavioral data exists.
    """
    from skills.behavioral_gap_detector import get_open_gaps_for_patient
    from skills.screening_registry import DOMAINS, SCREENING_REGISTRY
    from skills.atom_vector_search import get_atom_pressure_for_patient

    # ── Fetch raw data ────────────────────────────────────────────────────────
    gaps = await get_open_gaps_for_patient(pool, patient_id)
    pressure = await get_atom_pressure_for_patient(pool, patient_id)

    # Most recent screening per domain
    async with pool.acquire() as conn:
        screening_rows = await conn.fetch(
            """
            SELECT domain, instrument_key, score, band,
                   administered_at,
                   jsonb_array_length(triggered_critical) AS critical_count
            FROM behavioral_screenings
            WHERE patient_id = $1::uuid
            ORDER BY administered_at DESC
            """,
            patient_id,
        )

    # Index screenings by domain (first/most-recent per domain)
    last_screening: dict[str, dict] = {}
    for r in screening_rows:
        domain = r["domain"]
        if domain not in last_screening:
            last_screening[domain] = {
                "instrument_key": r["instrument_key"],
                "score": r["score"],
                "band": r["band"],
                "administered_at": _isoformat(r["administered_at"]),
                "critical_count": r["critical_count"],
            }

    # Check SI flag
    has_si_flag = any(
        s.get("critical_count", 0) > 0 or domain == "suicidality"
        for domain, s in last_screening.items()
    )

    # Build gap index
    gap_index: dict[str, dict] = {g["domain"]: g for g in gaps}

    # ── Build sections ────────────────────────────────────────────────────────
    sections: list[dict] = []

    # Include domains that have either a gap or a recent screening or atom pressure
    active_domains = set(gap_index.keys()) | set(last_screening.keys())
    for sig_type, p in pressure.items():
        if p.get("pressure_score", 0) >= 0.30:
            from skills.screening_registry import suggest_instruments_from_atoms
            for d in suggest_instruments_from_atoms([sig_type]):
                active_domains.add(d)

    for domain in active_domains:
        domain_label = DOMAINS.get(domain, domain)
        gap = gap_index.get(domain)
        screening = last_screening.get(domain)

        # Role filtering for patient-facing output
        if requesting_role == "patient":
            if gap:
                if gap.get("temporal_confidence") == "very_low":
                    continue
                if gap.get("gap_type") == "no_screening":
                    continue

        # Compute domain pressure from relevant atoms
        domain_instruments = [
            inst for k, inst in SCREENING_REGISTRY.items() if inst.domain == domain
        ]
        relevant_signals = set()
        for inst in domain_instruments:
            relevant_signals.update(inst.atom_signals)
        domain_pressure_score = max(
            (pressure.get(sig, {}).get("pressure_score", 0.0) for sig in relevant_signals),
            default=0.0,
        )

        section: dict = {
            "domain": domain,
            "domain_label": domain_label,
            "has_open_gap": gap is not None,
            "gap_type": gap.get("gap_type") if gap else None,
            "phenotype_label": gap.get("phenotype_label") if gap else None,
            "temporal_confidence": gap.get("temporal_confidence") if gap else None,
            "pressure_score": round(domain_pressure_score, 3),
            "suggested_instruments": gap.get("suggested_instruments", []) if gap else [],
            "last_screening": screening,
            "si_flag": (domain == "suicidality" and has_si_flag),
            "priority": 1 if domain == "suicidality" else (2 if gap else 3),
        }
        sections.append(section)

    # Sort: SI first, then gaps before screened, then by pressure
    sections.sort(key=lambda s: (s["priority"], -s["pressure_score"]))

    return sections


async def run_full_behavioral_pipeline(
    pool,
    patient_id: str,
    atom_text: Optional[str] = None,
    source_type: str = "conversation",
) -> dict:
    """Convenience function: extract atoms → refresh view → detect gaps → build section.

    Used in tests and the deliberation engine's behavioral hook.

    Returns: {atoms_stored, gaps_detected, section}
    """
    from skills.behavioral_atom_extractor import extract_atoms_from_text
    from skills.atom_embedder import embed_signal_value
    from skills.atom_vector_search import refresh_atom_pressure_view
    from skills.behavioral_gap_detector import run_gap_detector_for_patient
    import uuid as _uuid
    import logging as _log

    atoms_stored = 0

    if atom_text:
        atoms = extract_atoms_from_text(atom_text, source_type=source_type)
        async with pool.acquire() as conn:
            for atom in atoms:
                embedding = embed_signal_value(atom.signal_value)
                embedding_str = (
                    "[" + ",".join(str(x) for x in embedding) + "]"
                    if embedding else None
                )
                try:
                    await conn.execute(
                        """
                        INSERT INTO behavioral_signal_atoms
                            (id, patient_id, signal_type, signal_value, confidence,
                             source_type, source_id, extracted_at, embedding, data_source)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8::vector,$9)
                        ON CONFLICT DO NOTHING
                        """,
                        str(_uuid.uuid4()),
                        patient_id,
                        atom.signal_type,
                        atom.signal_value,
                        atom.confidence,
                        atom.source_type,
                        atom.source_id,
                        embedding_str,
                        "healthex",
                    )
                    atoms_stored += 1
                except Exception as exc:
                    _log.getLogger(__name__).warning("atom store failed: %s", exc)

    await refresh_atom_pressure_view(pool)

    gaps = await run_gap_detector_for_patient(pool, patient_id)

    section = await build_behavioral_section_for_deliberation(pool, patient_id)

    return {
        "atoms_stored": atoms_stored,
        "gaps_detected": len(gaps),
        "gaps": gaps,
        "section": section,
    }

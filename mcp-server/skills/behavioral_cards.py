"""
behavioral_cards.py — MCP tools for generating role-filtered behavioral insight cards.

Cards are generated from open gaps + atom pressure for a patient. Role-based
visibility rules:
  - temporal_confidence == 'very_low'  → exclude patient role
  - gap_type == 'no_screening' (Mode B) → exclude patient role
  - SI critical flag (suicidality_markers or cssrs/c-ssrs triggered_critical)
    → always include 'pcp' and 'care_manager'

Tools:
  - prepare_behavioral_cards
  - get_behavioral_screenings_for_patient
  - get_behavioral_screening_summary
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

_ROLE_PATIENT = "patient"
_ROLE_PCP = "pcp"
_ROLE_CARE_MANAGER = "care_manager"
_ROLE_BEHAVIORAL_HEALTH = "behavioral_health"
_ALL_CLINICAL_ROLES = [_ROLE_PCP, _ROLE_CARE_MANAGER, _ROLE_BEHAVIORAL_HEALTH]


def _determine_card_roles(
    temporal_confidence: str,
    gap_type: str,
    domain: str,
    has_si_flag: bool,
) -> list[str]:
    """Determine which roles receive a behavioral card.

    Rules (in priority order):
    1. SI domain or has_si_flag → always include pcp + care_manager
    2. temporal_confidence == 'very_low' → exclude patient
    3. gap_type in ('no_screening') → exclude patient (Mode B)
    4. Default: all clinical roles + patient
    """
    roles = list(_ALL_CLINICAL_ROLES)  # clinical roles always see cards

    si_domain = domain == "suicidality"
    if si_domain or has_si_flag:
        # Ensure pcp + care_manager, but don't add behavioral_health twice
        for r in [_ROLE_PCP, _ROLE_CARE_MANAGER]:
            if r not in roles:
                roles.append(r)
        # Patient still gets card unless temporal_confidence or gap_type excludes
        if temporal_confidence not in ("very_low",) and gap_type != "no_screening":
            roles.append(_ROLE_PATIENT)
        return roles

    exclude_patient = (
        temporal_confidence == "very_low"
        or gap_type == "no_screening"
    )

    if not exclude_patient:
        roles.append(_ROLE_PATIENT)

    return roles


async def build_cards_from_pool(
    pool,
    patient_id: str,
    role: Optional[str] = None,
) -> list[dict]:
    """Build behavioral insight cards from a pool (or pool-like adapter).

    Returns a list of card dicts filtered to `role` if specified.
    Safe to call from the deliberation layer — never raises.
    """
    try:
        from skills.behavioral_gap_detector import get_open_gaps_for_patient

        gaps = await get_open_gaps_for_patient(pool, patient_id)

        async with pool.acquire() as conn:
            si_rows = await conn.fetch(
                """
                SELECT id FROM behavioral_screenings
                WHERE patient_id = $1::uuid
                  AND (domain = 'suicidality'
                       OR jsonb_array_length(triggered_critical) > 0)
                  AND administered_at >= NOW() - INTERVAL '90 days'
                LIMIT 1
                """,
                patient_id,
            )
        has_si_flag = len(si_rows) > 0

        cards = []
        for gap in gaps:
            domain = gap["domain"]
            temporal_confidence = gap.get("temporal_confidence", "low")
            gap_type = gap.get("gap_type", "no_screening")
            pressure = float(gap.get("pressure_score") or 0.0)
            phenotype = gap.get("phenotype_label", "")

            roles = _determine_card_roles(temporal_confidence, gap_type, domain, has_si_flag)
            if role and role not in roles:
                continue

            if domain == "suicidality":
                priority = 1
            elif "high_burden" in phenotype:
                priority = 2
            elif "moderate_burden" in phenotype:
                priority = 3
            else:
                priority = 4

            severity_label = "high" if pressure >= 0.75 else ("moderate" if pressure >= 0.50 else "low")
            card_title = (
                _patient_facing_title(domain, severity_label)
                if role == _ROLE_PATIENT
                else f"{domain.replace('_', ' ').title()} — {gap_type.replace('_', ' ')}"
            )

            cards.append({
                "domain": domain,
                "card_title": card_title,
                "gap_type": gap_type,
                "phenotype_label": phenotype,
                "pressure_score": pressure,
                "temporal_confidence": temporal_confidence,
                "suggested_instruments": gap.get("suggested_instruments", []),
                "severity_label": severity_label,
                "visible_to_roles": roles,
                "show_to_roles": roles,
                "priority": priority,
            })

        cards.sort(key=lambda c: (c["priority"], -c["pressure_score"]))
        return cards
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("build_cards_from_pool failed: %s", type(e).__name__)
        return []


def register(mcp) -> None:

    # ── prepare_behavioral_cards ──────────────────────────────────────────────

    @mcp.tool()
    async def prepare_behavioral_cards(
        patient_id: str,
        requesting_role: Optional[str] = None,
    ) -> dict:
        """Generate behavioral insight cards for a patient, filtered by requesting role.

        Cards surface open screening gaps with pressure context, severity,
        and recommended instruments. Cards visible to the patient exclude
        clinical jargon and very-low-confidence signals.

        Args:
            patient_id:       UUID of the patient.
            requesting_role:  Role of the caller: 'patient'|'pcp'|'care_manager'|
                              'behavioral_health'|None (returns all cards with
                              their full role set for admin views).

        Returns:
            {patient_id, card_count, cards: [{domain, gap_type, phenotype_label,
             pressure_score, temporal_confidence, suggested_instruments,
             severity_label, visible_to_roles, priority}]}
        """
        from db.connection import get_pool
        from skills.behavioral_gap_detector import get_open_gaps_for_patient

        pool = await get_pool()
        gaps = await get_open_gaps_for_patient(pool, patient_id)

        # Check for SI critical screenings
        si_patient_ids: set[str] = set()
        async with pool.acquire() as conn:
            si_rows = await conn.fetch(
                """
                SELECT id FROM behavioral_screenings
                WHERE patient_id = $1::uuid
                  AND (domain = 'suicidality'
                       OR jsonb_array_length(triggered_critical) > 0)
                  AND administered_at >= NOW() - INTERVAL '90 days'
                LIMIT 1
                """,
                patient_id,
            )
        has_si_flag = len(si_rows) > 0

        cards = []
        for gap in gaps:
            domain = gap["domain"]
            temporal_confidence = gap.get("temporal_confidence", "low")
            gap_type = gap.get("gap_type", "no_screening")
            pressure = gap.get("pressure_score", 0.0)

            roles = _determine_card_roles(
                temporal_confidence, gap_type, domain, has_si_flag
            )

            # Filter by requesting_role if provided
            if requesting_role and requesting_role not in roles:
                continue

            # Priority: suicidality > high_burden > moderate > emerging
            phenotype = gap.get("phenotype_label", "")
            if domain == "suicidality":
                priority = 1
            elif "high_burden" in phenotype:
                priority = 2
            elif "moderate_burden" in phenotype:
                priority = 3
            else:
                priority = 4

            # Severity label for UI
            if pressure >= 0.75:
                severity_label = "high"
            elif pressure >= 0.50:
                severity_label = "moderate"
            else:
                severity_label = "low"

            # Patient-safe title (no clinical jargon)
            if requesting_role == _ROLE_PATIENT:
                card_title = _patient_facing_title(domain, severity_label)
            else:
                card_title = f"{domain.replace('_', ' ').title()} — {gap_type.replace('_', ' ')}"

            card = {
                "domain": domain,
                "card_title": card_title,
                "gap_type": gap_type,
                "phenotype_label": phenotype,
                "pressure_score": pressure,
                "temporal_confidence": temporal_confidence,
                "suggested_instruments": gap.get("suggested_instruments", []),
                "severity_label": severity_label,
                "visible_to_roles": roles,
                "show_to_roles": roles,
                "priority": priority,
            }
            for k, v in card.items():
                if isinstance(v, datetime):
                    card[k] = v.isoformat()
            cards.append(card)

        # Sort by priority, then pressure descending
        cards.sort(key=lambda c: (c["priority"], -c["pressure_score"]))

        return {
            "patient_id": patient_id,
            "card_count": len(cards),
            "has_si_flag": has_si_flag,
            "cards": cards,
        }

    # ── get_behavioral_screenings_for_patient ─────────────────────────────────

    @mcp.tool()
    async def get_behavioral_screenings_for_patient(
        patient_id: str,
        domain: Optional[str] = None,
        instrument_key: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Retrieve behavioral screening history for a patient.

        Args:
            patient_id:      UUID of the patient.
            domain:          Filter by domain (optional).
            instrument_key:  Filter by specific instrument (optional).
            limit:           Max records to return (default 20).

        Returns:
            {patient_id, count, screenings: [{instrument_key, domain, score,
             band, administered_at, triggered_critical}]}
        """
        from db.connection import get_pool

        pool = await get_pool()

        domain_filter = "AND domain = $3" if domain else ""
        instrument_filter = "AND instrument_key = $4" if instrument_key else ""

        params = [patient_id, limit]
        if domain:
            params.append(domain)
        if instrument_key:
            params.append(instrument_key)

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id::text, instrument_key, domain, loinc_code,
                       score, band, triggered_critical, source_type,
                       administered_at
                FROM behavioral_screenings
                WHERE patient_id = $1::uuid
                  {domain_filter}
                  {instrument_filter}
                ORDER BY administered_at DESC
                LIMIT $2
                """,
                *params,
            )

        screenings = []
        for r in rows:
            row_dict = dict(r)
            if isinstance(row_dict.get("administered_at"), datetime):
                row_dict["administered_at"] = row_dict["administered_at"].isoformat()
            if isinstance(row_dict.get("triggered_critical"), str):
                import json as _json
                try:
                    row_dict["triggered_critical"] = _json.loads(row_dict["triggered_critical"])
                except Exception:
                    pass
            screenings.append(row_dict)

        return {
            "patient_id": patient_id,
            "count": len(screenings),
            "screenings": screenings,
        }

    # ── get_behavioral_screening_summary ─────────────────────────────────────

    @mcp.tool()
    async def get_behavioral_screening_summary(patient_id: str) -> dict:
        """Summarise behavioral screening coverage across all 11 domains.

        Returns which domains have been screened and when, and which
        domains have open gaps. Useful for care team dashboards.

        Args:
            patient_id: UUID of the patient.

        Returns:
            {patient_id, domains_screened, domains_with_gaps, domain_summary}
        """
        from db.connection import get_pool
        from skills.screening_registry import DOMAINS

        pool = await get_pool()

        async with pool.acquire() as conn:
            screening_rows = await conn.fetch(
                """
                SELECT domain,
                       MAX(administered_at) AS last_screened,
                       COUNT(*) AS screening_count,
                       MIN(score) AS min_score,
                       MAX(score) AS max_score
                FROM behavioral_screenings
                WHERE patient_id = $1::uuid
                GROUP BY domain
                """,
                patient_id,
            )
            gap_rows = await conn.fetch(
                """
                SELECT domain, gap_type, temporal_confidence, pressure_score
                FROM behavioral_screening_gaps
                WHERE patient_id = $1::uuid
                  AND status = 'open'
                """,
                patient_id,
            )

        screened_domains = {r["domain"]: dict(r) for r in screening_rows}
        gap_domains = {r["domain"]: dict(r) for r in gap_rows}

        domain_summary = {}
        for domain_key, domain_label in DOMAINS.items():
            screened = screened_domains.get(domain_key)
            gap = gap_domains.get(domain_key)

            last_screened = None
            if screened and screened.get("last_screened"):
                last_screened = screened["last_screened"].isoformat()

            domain_summary[domain_key] = {
                "label": domain_label,
                "screened": screened is not None,
                "last_screened": last_screened,
                "screening_count": screened["screening_count"] if screened else 0,
                "has_open_gap": gap is not None,
                "gap_type": gap["gap_type"] if gap else None,
                "temporal_confidence": gap["temporal_confidence"] if gap else None,
                "pressure_score": float(gap["pressure_score"]) if gap and gap["pressure_score"] else None,
            }

        return {
            "patient_id": patient_id,
            "domains_screened": len(screened_domains),
            "domains_with_gaps": len(gap_domains),
            "domain_summary": domain_summary,
        }


def _patient_facing_title(domain: str, severity: str) -> str:
    """Return a patient-safe card title without clinical jargon."""
    titles = {
        "depression":       "Emotional well-being check-in recommended",
        "anxiety":          "Stress and worry check-in recommended",
        "substance_use":    "Substance use check-in available",
        "ptsd_trauma":      "Past experiences check-in recommended",
        "adhd":             "Focus and attention check-in available",
        "suicidality":      "Mental health safety check-in",
        "bipolar":          "Mood patterns check-in available",
        "eating_disorder":  "Eating habits check-in recommended",
        "sleep":            "Sleep quality check-in available",
        "cognitive":        "Memory and thinking check-in available",
        "somatic":          "Physical symptoms check-in available",
    }
    return titles.get(domain, "Health check-in recommended")

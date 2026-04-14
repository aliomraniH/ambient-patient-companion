"""MCP tool: prepare_behavioral_cards.

Returns a list of structured "cards" that any downstream LLM, UI, or
nudge agent can render. Each card is a flat dict — no nested
instrument-specific shapes. Consumers filter by `show_to_roles` and
render `title` / `subtitle` / `body_text` / `actions` as they see fit.

Card types:
    screening_gap     — domain with atom pressure but no recent screening
    positive_screen   — most-recent screening flagged positive (mode A)
    critical_flag     — triggered critical item (SI, etc.) requires action
    sdoh_need         — SDoH instrument surfaced a positive domain
    atom_pattern      — a cluster of atoms that did not (yet) breach gap
                        threshold but is worth surfacing to the PCP

Role-gating rules:
    - very_low temporal_confidence → never shown to patient
    - Mode B (open screening_gap) → never shown to patient as clinical text;
      a single `behavioral_routing` card is emitted instead
    - critical_flag → always high priority, includes pcp + care_manager
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import uuid as _uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional

from fastmcp import FastMCP

from db.connection import get_pool
from skills.screening_registry import (
    DOMAIN_LOOKBACK_DAYS,
    DOMAINS,
    SCREENING_REGISTRY,
    get_instrument_by_key,
    instruments_for_domain,
)

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

ALL_CARE_TEAM_ROLES = ("pcp", "care_manager")


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(val) for k, val in v.items()}
    return str(v)


def _card_id(card_type: str, *parts: str) -> str:
    """Stable card id: deterministic hash of type + source ids."""
    raw = "|".join([card_type] + [str(p) for p in parts if p is not None])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _priority_from_temporal(tc: str) -> str:
    return {
        "high": "high",
        "moderate": "medium",
        "low": "medium",
        "very_low": "low",
    }.get(tc or "low", "medium")


# ── Core fetch ──────────────────────────────────────────────────────────

async def _fetch_cards_bundle(conn, patient_id: str) -> dict:
    """Pull the raw rows we need to build cards."""
    phenotype = await conn.fetchrow(
        "SELECT * FROM behavioral_phenotypes WHERE patient_id = $1::uuid",
        patient_id,
    )
    open_gaps = await conn.fetch(
        """SELECT * FROM behavioral_screening_gaps
            WHERE patient_id = $1::uuid AND status = 'open'
            ORDER BY detected_at DESC""",
        patient_id,
    )
    screenings = await conn.fetch(
        """SELECT * FROM behavioral_screenings
            WHERE patient_id = $1::uuid
            ORDER BY observation_date DESC
            LIMIT 40""",
        patient_id,
    )
    sdoh = await conn.fetch(
        """SELECT * FROM sdoh_screenings
            WHERE patient_id = $1::uuid
            ORDER BY observation_date DESC
            LIMIT 20""",
        patient_id,
    )
    atoms = await conn.fetch(
        """SELECT id, clinical_date, note_section, signal_type,
                  signal_value, confidence
             FROM behavioral_signal_atoms
            WHERE patient_id = $1::uuid AND assertion = 'present'
            ORDER BY clinical_date DESC
            LIMIT 50""",
        patient_id,
    )
    pressure = await conn.fetchrow(
        "SELECT * FROM atom_pressure_scores WHERE patient_id = $1::uuid",
        patient_id,
    )
    return {
        "phenotype": phenotype,
        "open_gaps": open_gaps,
        "screenings": screenings,
        "sdoh": sdoh,
        "atoms": atoms,
        "pressure": pressure,
    }


# ── Card builders ───────────────────────────────────────────────────────

def _build_screening_gap_card(gap: dict, atoms: list[dict]) -> dict:
    domains = list(gap["triggered_domains"] or [])
    domain = domains[0] if domains else "depression"
    domain_label = DOMAINS.get(domain, {}).get("label", domain.replace("_", " ").title())
    instruments = list(gap["recommended_instruments"] or [])
    tc = gap.get("temporal_confidence") or "low"
    is_patient_ok = tc not in ("low", "very_low")
    show_roles = list(ALL_CARE_TEAM_ROLES)
    # Patient surface suppression for Mode B is default — cards surface
    # clinical framing only to the care team. A companion "routing" card
    # covers the patient role.
    # (No patient card here; see _build_routing_card.)

    atom_evidence = [
        {
            "type": "atom",
            "date": a["clinical_date"].isoformat()
                    if isinstance(a["clinical_date"], (date, datetime))
                    else str(a["clinical_date"]),
            "signal_type": a["signal_type"],
            "confidence": float(a["confidence"] or 0),
        }
        for a in (atoms or [])[:10]
    ]

    priority = "high" if tc in ("high", "moderate") else "medium"
    subtitle = None
    if instruments:
        subtitle = (f"{instruments[0]} recommended — "
                    f"{gap['atom_count']} signals in recent notes")

    body = (
        f"Clinical notes contain {gap['atom_count']} behavioral signal(s) "
        f"implicating the {domain_label.lower()} domain, but no formal "
        f"screening is on file within the last "
        f"{DOMAIN_LOOKBACK_DAYS.get(domain, 365)} days. "
        f"Temporal confidence: {tc}."
    )

    actions = []
    for name in instruments[:3]:
        inst_key = next(
            (k for k, v in SCREENING_REGISTRY.items() if v.display_name == name),
            None,
        )
        actions.append({
            "action_id": f"administer_{inst_key or name.lower()}",
            "label": f"Administer {name} at next visit",
            "instrument_key": inst_key,
        })

    return {
        "card_id": _card_id("screening_gap", str(gap["id"])),
        "card_type": "screening_gap",
        "title": f"{domain_label} screening overdue",
        "subtitle": subtitle,
        "domain": domain,
        "priority": priority,
        "body_text": body,
        "evidence": atom_evidence,
        "actions": actions,
        "critical_flags": [],
        "temporal_confidence": tc,
        "show_to_roles": show_roles,
        "source": {
            "gap_id": str(gap["id"]),
            "screening_id": None,
            "atom_ids": [str(a["id"]) for a in atoms[:20]],
        },
    }


def _build_routing_card(gap: dict) -> dict:
    """Patient-side companion to a Mode B screening_gap card."""
    return {
        "card_id": _card_id("routing", str(gap["id"])),
        "card_type": "behavioral_routing",
        "title": "Follow-up at next visit",
        "subtitle": None,
        "domain": (list(gap["triggered_domains"] or []) or ["depression"])[0],
        "priority": "medium",
        "body_text": "Your care team has some questions to follow up on at your next visit.",
        "evidence": [],
        "actions": [{"action_id": "route_to_provider",
                     "label": "Talk with your provider"}],
        "critical_flags": [],
        "temporal_confidence": gap.get("temporal_confidence") or "low",
        "show_to_roles": ["patient"],
        "source": {"gap_id": str(gap["id"]),
                   "screening_id": None, "atom_ids": []},
    }


def _build_positive_screen_card(screen: dict) -> Optional[dict]:
    if not screen.get("is_positive"):
        return None
    inst = get_instrument_by_key(screen["instrument_key"])
    display = screen["instrument_name"]
    domain = screen["domain"]
    domain_label = DOMAINS.get(domain, {}).get("label", domain)
    total = screen.get("total_score")
    band = screen.get("severity_band") or "positive"

    body = (f"Most recent {display} on "
            f"{screen['observation_date'].isoformat() if isinstance(screen['observation_date'], (date, datetime)) else screen['observation_date']}"
            f" scored {total} ({band}). Domain: {domain_label}.")

    return {
        "card_id": _card_id("positive_screen", str(screen["id"])),
        "card_type": "positive_screen",
        "title": f"{display} positive — {band}",
        "subtitle": f"Score {total}",
        "domain": domain,
        "priority": "high" if band in (
            "severe", "moderately_severe", "positive", "dependence",
            "substantial", "active_plan", "active_no_plan",
        ) else "medium",
        "body_text": body,
        "evidence": [{
            "type": "screening",
            "instrument": display,
            "date": screen["observation_date"].isoformat()
                    if isinstance(screen["observation_date"], (date, datetime))
                    else str(screen["observation_date"]),
            "score": total,
            "band": band,
        }],
        "actions": [{
            "action_id": "review_screening_result",
            "label": f"Review {display} result",
            "instrument_key": screen["instrument_key"],
        }],
        "critical_flags": [],
        "temporal_confidence": "high",
        "show_to_roles": list(ALL_CARE_TEAM_ROLES),
        "source": {
            "gap_id": None,
            "screening_id": str(screen["id"]),
            "atom_ids": [],
        },
    }


def _build_critical_flag_cards(screen: dict) -> list[dict]:
    """One card per triggered critical item (SI, etc.)."""
    triggered = screen.get("triggered_critical") or []
    if isinstance(triggered, str):
        try:
            triggered = json.loads(triggered)
        except json.JSONDecodeError:
            triggered = []
    if not triggered:
        return []
    cards: list[dict] = []
    for item in triggered:
        item_number = item.get("item_number")
        alert_text = item.get("alert_text") or "Critical item triggered"
        priority = item.get("priority") or "critical"
        cards.append({
            "card_id": _card_id("critical_flag",
                                str(screen["id"]), str(item_number)),
            "card_type": "critical_flag",
            "title": alert_text,
            "subtitle": (f"{screen['instrument_name']} item {item_number} "
                         f"= {item.get('actual_score')}"),
            "domain": screen["domain"],
            "priority": priority,
            "body_text": (
                f"{alert_text}. Last documented on "
                f"{screen['observation_date']}. "
                "Clinical assessment required."
            ),
            "evidence": [{
                "type": "screening_item",
                "instrument": screen["instrument_name"],
                "item_number": item_number,
                "score": item.get("actual_score"),
                "date": screen["observation_date"].isoformat()
                        if isinstance(screen["observation_date"],
                                      (date, datetime))
                        else str(screen["observation_date"]),
            }],
            "actions": [{
                "action_id": "safety_assessment",
                "label": "Initiate safety assessment",
            }],
            "critical_flags": [item],
            "temporal_confidence": "high",
            "show_to_roles": list(ALL_CARE_TEAM_ROLES),
            "source": {
                "gap_id": None,
                "screening_id": str(screen["id"]),
                "atom_ids": [],
            },
        })
    return cards


def _build_sdoh_cards(sdoh: dict) -> list[dict]:
    domains = list(sdoh.get("positive_domains") or [])
    if not domains:
        return []
    return [{
        "card_id": _card_id("sdoh_need", str(sdoh["id"]), d),
        "card_type": "sdoh_need",
        "title": f"SDoH: {d.replace('_', ' ')}",
        "subtitle": f"{sdoh['instrument_name']} flagged {d}",
        "domain": d,
        "priority": "high" if d in
            ("housing_instability", "food_insecurity", "interpersonal_safety",
             "child_safety") else "medium",
        "body_text": (
            f"{sdoh['instrument_name']} on "
            f"{sdoh['observation_date'].isoformat() if isinstance(sdoh['observation_date'], (date, datetime)) else sdoh['observation_date']}"
            f" surfaced {d.replace('_', ' ')} as a patient-reported concern."
        ),
        "evidence": [{
            "type": "sdoh_screening",
            "instrument": sdoh["instrument_name"],
            "date": sdoh["observation_date"].isoformat()
                    if isinstance(sdoh["observation_date"], (date, datetime))
                    else str(sdoh["observation_date"]),
            "domain": d,
        }],
        "actions": [{
            "action_id": f"connect_resource_{d}",
            "label": f"Connect to resource ({d})",
        }],
        "critical_flags": [],
        "temporal_confidence": "high",
        "show_to_roles": ["pcp", "care_manager"],
        "source": {
            "gap_id": None,
            "screening_id": str(sdoh["id"]),
            "atom_ids": [],
        },
    } for d in domains]


def _filter_for_role(cards: list[dict], role: str) -> list[dict]:
    if not role:
        return cards
    return [c for c in cards if role in (c.get("show_to_roles") or [])]


# ── Public entry points ─────────────────────────────────────────────────

async def build_cards_from_pool(
    db_pool, patient_id: str, role: str = "pcp",
) -> list[dict]:
    """Shared helper used by both the MCP tool and the deliberation
    section builder. Accepts an existing asyncpg pool; avoids double-
    opening pools when called in-process.
    """
    if db_pool is None or not patient_id:
        return []
    async with db_pool.acquire() as conn:
        bundle = await _fetch_cards_bundle(conn, patient_id)

    cards: list[dict] = []

    # 1. Open screening gaps → per-gap card + a single routing card for
    #    the patient role (first gap only; all gaps funnel to one routing).
    routing_emitted = False
    for gap in bundle["open_gaps"]:
        gap_d = dict(gap)
        # Fetch the linked atoms for this gap's atom_ids.
        atom_ids = list(gap_d.get("atom_ids") or [])
        gap_atoms = [dict(a) for a in bundle["atoms"]
                     if a["id"] in atom_ids]
        cards.append(_build_screening_gap_card(gap_d, gap_atoms))
        if not routing_emitted:
            cards.append(_build_routing_card(gap_d))
            routing_emitted = True

    # 2. Critical flags from the latest screening per instrument.
    seen_instruments: set[str] = set()
    for screen in bundle["screenings"]:
        key = screen["instrument_key"]
        if key in seen_instruments:
            continue
        seen_instruments.add(key)
        cards.extend(_build_critical_flag_cards(dict(screen)))

    # 3. Positive recent screens (last 12 months, most recent per instrument).
    one_year = date.today() - timedelta(days=365)
    seen_instruments_pos: set[str] = set()
    for screen in bundle["screenings"]:
        key = screen["instrument_key"]
        if key in seen_instruments_pos:
            continue
        obs_d = screen["observation_date"]
        if isinstance(obs_d, (datetime,)):
            obs_d = obs_d.date()
        if obs_d and obs_d < one_year:
            continue
        card = _build_positive_screen_card(dict(screen))
        if card:
            cards.append(card)
            seen_instruments_pos.add(key)

    # 4. SDoH cards from most-recent positive SDoH rows.
    for sd in bundle["sdoh"]:
        cards.extend(_build_sdoh_cards(dict(sd)))

    # 5. Role filter.
    return _filter_for_role(cards, role)


async def prepare_behavioral_cards(
    patient_id: str,
    role: str = "pcp",
) -> str:
    """MCP tool: build the behavioral resurfacing card list.

    Args:
        patient_id: UUID of the patient.
        role: 'pcp' | 'care_manager' | 'patient' (default 'pcp').

    Returns:
        JSON string with {"cards": [...]}. Card schema documented at the
        top of this module.
    """
    try:
        pool = await get_pool()
        cards = await build_cards_from_pool(pool, patient_id, role)
        return json.dumps({
            "status": "ok",
            "patient_id": patient_id,
            "role": role,
            "card_count": len(cards),
            "cards": [_jsonable(c) for c in cards],
        })
    except Exception as e:
        logger.warning("prepare_behavioral_cards failed: %s", type(e).__name__)
        return json.dumps({"status": "error", "detail": type(e).__name__})


def register(mcp: FastMCP) -> None:
    mcp.tool(prepare_behavioral_cards)

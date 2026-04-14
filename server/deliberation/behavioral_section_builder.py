"""Post-synthesis behavioral section builder — v2.

Thin adapter that calls the card builder (`mcp-server/skills/
behavioral_cards.py::build_cards_from_pool`) to assemble
`result.behavioral_section` as a list of structured cards. Mode A /
Mode B routing lives entirely inside the card builder; the deliberation
engine only sees a card list.

Exposed functions:
    build_behavioral_section(ctx, role)          — shape-compat shim
    fetch_behavioral_context(db_pool, patient_id) — multi-domain context
    augment_result_with_behavioral_section(...)  — called by engine
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# Allow this module to import the skill-layer card builder. The skills
# modules live under mcp-server/ which is not on sys.path by default when
# the deliberation package is imported from the clinical server.
_SKILLS_ROOT = Path(__file__).resolve().parents[2] / "mcp-server"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT))


def build_behavioral_section(
    behavioral_context: dict,
    role: str,
) -> list[dict]:
    """Legacy shim — builds cards from an already-fetched context dict.

    Retained for callers that prefer the pure-function entry point
    (e.g. unit tests). Extracts the pre-built `cards` field from the
    context if present, otherwise returns an empty list. The cards are
    the single source of truth; role filtering is applied here.
    """
    if not behavioral_context:
        return []
    cards = behavioral_context.get("cards") or []
    if not isinstance(cards, list):
        return []
    if not role:
        return cards
    return [c for c in cards
            if role in (c.get("show_to_roles") or [])]


async def fetch_behavioral_context(
    db_pool, patient_id: str, role: str = "pcp",
) -> Optional[dict]:
    """Return a behavioral context dict with pre-built cards.

    Shape:
        {
          "mode": "primary_evidence" | "contextual",
          "cards": [ {card}, ... ],
        }

    Returns None when no phenotype exists or on any fetch error — always
    safe to pass to `build_behavioral_section`.
    """
    if db_pool is None or not patient_id:
        return None
    try:
        from skills.behavioral_cards import build_cards_from_pool  # type: ignore
    except Exception:
        return None

    try:
        async with db_pool.acquire() as conn:
            phenotype = await conn.fetchrow(
                "SELECT evidence_mode FROM behavioral_phenotypes "
                "WHERE patient_id = $1::uuid",
                patient_id,
            )
            any_screen = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM behavioral_screenings "
                "WHERE patient_id = $1::uuid)",
                patient_id,
            )

        mode = (phenotype["evidence_mode"] if phenotype
                else ("contextual" if any_screen else "contextual"))

        cards = await build_cards_from_pool(db_pool, patient_id, role=role)
        if not cards and not phenotype:
            return None

        return {
            "mode": mode,
            "cards": cards,
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

    `behavioral_section` is now a **list of cards** (may be empty).
    Safe no-op when no phenotype exists or on any fetch failure.
    """
    try:
        ctx = await fetch_behavioral_context(db_pool, patient_id, role=role)
        if not ctx:
            return
        result.behavioral_section = ctx.get("cards") or []
    except Exception:
        # Never let behavioral augmentation break deliberation.
        return

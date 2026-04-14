"""
sdoh_registry.py — SDoH screener definitions for Behavioral V2.

Covers PRAPARE, AHC-HRSN, Hunger Vital Sign (HVS), WHO-QoL-BREF subset,
and SEEK. Each screener defines its LOINC panel code, items, and domain tags
so the behavioral_screening_ingestor can parse item-level answers uniformly.

Pure data module — no DB, no imports from other app modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SDoHItem:
    item_number: int
    loinc_code: str     # per-item LOINC code if available, else ""
    question_text: str
    sdoh_domain: str    # 'food', 'housing', 'transportation', 'social', 'financial', 'safety', 'education'
    positive_if: str    # human-readable rule for a 'positive' response, e.g. "answer >= 2"


@dataclass
class SDoHScreener:
    key: str
    name: str
    panel_loinc: str        # LOINC panel code
    items: list[SDoHItem]
    domains_covered: list[str]
    notes: str = ""


SDOH_REGISTRY: dict[str, SDoHScreener] = {

    "prapare": SDoHScreener(
        key="prapare",
        name="Protocol for Responding to and Assessing Patients' Assets, Risks, and Experiences (PRAPARE)",
        panel_loinc="93025-5",
        domains_covered=["food", "housing", "transportation", "social", "financial", "safety", "education"],
        items=[
            SDoHItem(1,  "56799-0", "Are you Hispanic or Latino?", "social",         "answer = Yes"),
            SDoHItem(2,  "93035-4", "What is your race?", "social",                  "non-white"),
            SDoHItem(3,  "56051-6", "Are you a veteran?", "social",                  "answer = Yes"),
            SDoHItem(4,  "93043-8", "What language do you prefer?", "social",        "non-English"),
            SDoHItem(5,  "56799-0", "How many people are in your household?", "social","answer = 1 (isolated)"),
            SDoHItem(6,  "93031-3", "What is your housing situation?", "housing",     "not stable/own"),
            SDoHItem(7,  "71802-3", "Problems paying for housing?", "housing",        "answer = Yes"),
            SDoHItem(8,  "93038-8", "Within the past year, have you worried about food?", "food", "answer = Yes"),
            SDoHItem(9,  "93039-6", "Can you afford the food you need?", "food",      "answer = No"),
            SDoHItem(10, "93040-4", "Difficulty with transportation?", "transportation","answer = Yes"),
            SDoHItem(11, "93029-7", "Problems with income?", "financial",             "answer = Yes"),
            SDoHItem(12, "93033-9", "Do you feel physically and emotionally safe?", "safety","answer = No"),
            SDoHItem(13, "93036-2", "Refugee or immigrant in past 5 years?", "social","answer = Yes"),
        ],
        notes="NACHC/AAFP standard SDoH screener for FQHC settings.",
    ),

    "ahc_hrsn": SDoHScreener(
        key="ahc_hrsn",
        name="Accountable Health Communities Health-Related Social Needs (AHC-HRSN)",
        panel_loinc="96777-1",
        domains_covered=["food", "housing", "transportation", "financial", "safety"],
        items=[
            SDoHItem(1,  "88122-7", "Within the past 12 months, food didn't last?", "food",   "answer = often/sometimes"),
            SDoHItem(2,  "88123-5", "Within the past 12 months, couldn't afford food?", "food","answer = often/sometimes"),
            SDoHItem(3,  "96778-9", "Housing situation: worried about losing housing?", "housing","answer = Yes"),
            SDoHItem(4,  "96779-7", "Problems with house (bugs, mold, lead)?", "housing",      "answer = Yes"),
            SDoHItem(5,  "96780-5", "Transportation problem: missed appointments?", "transportation","answer = Yes"),
            SDoHItem(6,  "96781-3", "Transportation problem: couldn't get medication?", "transportation","answer = Yes"),
            SDoHItem(7,  "95614-4", "Unable to pay utilities/phone/other?", "financial",       "answer = Yes"),
            SDoHItem(8,  "95615-1", "Safety: feel safe where you live?", "safety",             "answer = No"),
        ],
        notes="CMS Accountable Health Communities 10-question screener (core 5 domains).",
    ),

    "hunger_vital_sign": SDoHScreener(
        key="hunger_vital_sign",
        name="Hunger Vital Sign (HVS)",
        panel_loinc="88121-9",
        domains_covered=["food"],
        items=[
            SDoHItem(1, "88122-7", "Within the past 12 months, the food we bought just didn't last and we didn't have money to get more.", "food", "answer = often/sometimes"),
            SDoHItem(2, "88123-5", "Within the past 12 months, we couldn't afford to eat balanced meals.", "food", "answer = often/sometimes"),
        ],
        notes="Positive if either item answered 'often true' or 'sometimes true'. Validated 2-item food insecurity screen.",
    ),

    "whoqol_bref_subset": SDoHScreener(
        key="whoqol_bref_subset",
        name="WHO Quality of Life-BREF (WHOQoL-BREF) — SDoH subset",
        panel_loinc="93026-3",
        domains_covered=["social", "housing", "financial"],
        items=[
            SDoHItem(1,  "93026-3", "How satisfied with living conditions?", "housing",  "score ≤ 2"),
            SDoHItem(2,  "93027-1", "How satisfied with social relationships?", "social", "score ≤ 2"),
            SDoHItem(3,  "93028-9", "How satisfied with access to health services?", "financial","score ≤ 2"),
        ],
        notes="Abbreviated 3-item subset of WHOQoL-BREF for routine SDoH capture; not a substitute for the full instrument.",
    ),

    "seek": SDoHScreener(
        key="seek",
        name="Safe Environment for Every Kid (SEEK)",
        panel_loinc="93044-6",
        domains_covered=["safety", "food", "housing", "social"],
        items=[
            SDoHItem(1,  "93044-6", "Have you had trouble making ends meet?", "financial",    "answer = Yes"),
            SDoHItem(2,  "93045-3", "Are you having trouble with food?", "food",              "answer = Yes"),
            SDoHItem(3,  "93046-1", "Is your housing a problem?", "housing",                  "answer = Yes"),
            SDoHItem(4,  "93047-9", "Is your child safe where you live?", "safety",           "answer = No"),
            SDoHItem(5,  "93048-7", "Do you feel alone in caring for your child?", "social",  "answer = Yes"),
            SDoHItem(6,  "93049-5", "Do you have trouble with alcohol or drug use?", "social","answer = Yes"),
            SDoHItem(7,  "93050-3", "Is your partner a problem?", "safety",                   "answer = Yes"),
        ],
        notes="Validated parent SDoH screener for pediatric primary care settings.",
    ),
}


# ─── LOINC panel lookup ───────────────────────────────────────────────────────

_PANEL_LOINC_TO_KEY: dict[str, str] = {
    s.panel_loinc: k for k, s in SDOH_REGISTRY.items()
}

_ITEM_LOINC_TO_SCREENER: dict[str, str] = {}
for _key, _screener in SDOH_REGISTRY.items():
    for _item in _screener.items:
        if _item.loinc_code:
            _ITEM_LOINC_TO_SCREENER.setdefault(_item.loinc_code, _key)


def get_screener_for_panel_loinc(loinc_code: str) -> "SDoHScreener | None":
    key = _PANEL_LOINC_TO_KEY.get(loinc_code)
    return SDOH_REGISTRY.get(key) if key else None


def get_screener_for_item_loinc(loinc_code: str) -> "SDoHScreener | None":
    key = _ITEM_LOINC_TO_SCREENER.get(loinc_code)
    return SDOH_REGISTRY.get(key) if key else None


SDOH_LOINC_TO_INSTRUMENT: dict[str, str] = {
    screener.panel_loinc: key
    for key, screener in SDOH_REGISTRY.items()
    if screener.panel_loinc
}


def register(mcp) -> None:
    """No-op: sdoh_registry is a pure data module."""
    pass

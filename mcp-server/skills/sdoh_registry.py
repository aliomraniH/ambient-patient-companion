"""SDoH screening instrument registry — mirrors screening_registry.py
but for social-determinants-of-health instruments.

Pure data module. Used by the same ingestor as behavioral screenings;
parsed into the `sdoh_screenings` table. Each instrument exposes its
item-level answer schema (LOINC + linkId), so an ingestor that sees a
`QuestionnaireResponse` with a matching panel LOINC can slice the
`.item[]` array by linkId and write structured per-item answers.

5 instruments:
    prapare            — PRAPARE social needs screen
    ahc_hrsn           — CMS Accountable Health Communities HRSN
    hunger_vital_sign  — 2-item food insecurity screen
    who_qol_bref       — WHOQOL-BREF (subset: physical / psychological)
    seek               — SEEK parent safety screen (pediatric)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SdoHItem:
    """One item in a SDoH instrument."""
    item_number: int
    linkid_candidates: tuple[str, ...]
    loinc: Optional[str]
    domain: str              # sub-domain label, e.g. 'food_insecurity'
    positive_answers: tuple[str, ...]  # answer codes/strings that indicate risk


@dataclass(frozen=True)
class SdoHInstrument:
    key: str
    display_name: str
    loinc_panel: str
    linkid_prefix: str
    items: tuple[SdoHItem, ...]
    # Domains this instrument surfaces (union of item sub-domains).
    covered_domains: tuple[str, ...]
    notes: str = ""


# ── Sub-domain labels (shared with patients.sdoh_flags vocabulary) ──────

SDOH_DOMAINS = {
    "food_insecurity",
    "housing_instability",
    "utilities",
    "transportation_barrier",
    "interpersonal_safety",
    "employment",
    "education",
    "financial_strain",
    "social_isolation",
    "childcare",
    "physical_wellbeing",
    "psychological_wellbeing",
    "child_safety",
}


# ── Instrument definitions ──────────────────────────────────────────────
#
# LOINC codes below: PRAPARE panel 93025-5, AHC-HRSN 95618-5, HVS 88122-7,
# WHOQOL-BREF 93376-2 (placeholder — WHOQOL uses custom codes), SEEK uses
# local codes. Item linkIds are conservative — parsers accept common
# variants in `linkid_candidates`.

SDOH_REGISTRY: dict[str, SdoHInstrument] = {
    "prapare": SdoHInstrument(
        key="prapare",
        display_name="PRAPARE",
        loinc_panel="93025-5",
        linkid_prefix="prapare-item-",
        items=(
            SdoHItem(1, ("prapare-item-11", "housing"),   None,
                     "housing_instability", ("unstable", "homeless", "1")),
            SdoHItem(2, ("prapare-item-13", "food"),       None,
                     "food_insecurity",     ("yes", "often_true", "1")),
            SdoHItem(3, ("prapare-item-14", "utilities"),  None,
                     "utilities",           ("yes", "1")),
            SdoHItem(4, ("prapare-item-15", "transport"),  None,
                     "transportation_barrier", ("yes", "1")),
            SdoHItem(5, ("prapare-item-16", "safety"),     None,
                     "interpersonal_safety", ("unsafe", "1")),
            SdoHItem(6, ("prapare-item-8", "employment"),  None,
                     "employment",          ("unemployed", "1")),
            SdoHItem(7, ("prapare-item-9", "education"),   None,
                     "education",           ("less_than_hs", "1")),
            SdoHItem(8, ("prapare-item-17", "stress"),     None,
                     "psychological_wellbeing", ("very_much", "quite_a_bit", "1")),
        ),
        covered_domains=(
            "housing_instability", "food_insecurity", "utilities",
            "transportation_barrier", "interpersonal_safety",
            "employment", "education", "psychological_wellbeing",
        ),
    ),
    "ahc_hrsn": SdoHInstrument(
        key="ahc_hrsn",
        display_name="AHC-HRSN",
        loinc_panel="95618-5",
        linkid_prefix="ahc-hrsn-item-",
        items=(
            SdoHItem(1, ("ahc-hrsn-item-1", "housing-q1"), "71802-3",
                     "housing_instability", ("i_do_not_have_housing", "1")),
            SdoHItem(2, ("ahc-hrsn-item-2", "housing-q2"), None,
                     "housing_instability", ("yes", "1")),
            SdoHItem(3, ("ahc-hrsn-item-3", "food-q1"),    "88122-7",
                     "food_insecurity",     ("often_true", "sometimes_true", "1")),
            SdoHItem(4, ("ahc-hrsn-item-4", "food-q2"),    "88123-5",
                     "food_insecurity",     ("often_true", "sometimes_true", "1")),
            SdoHItem(5, ("ahc-hrsn-item-5", "transport"),  "93030-5",
                     "transportation_barrier", ("yes", "1")),
            SdoHItem(6, ("ahc-hrsn-item-6", "utilities"),  "96779-4",
                     "utilities",           ("yes", "1")),
            SdoHItem(7, ("ahc-hrsn-item-7", "safety-q1"),  None,
                     "interpersonal_safety", ("1", "2", "3")),
            SdoHItem(8, ("ahc-hrsn-item-8", "safety-q2"),  None,
                     "interpersonal_safety", ("1", "2", "3")),
        ),
        covered_domains=(
            "housing_instability", "food_insecurity",
            "transportation_barrier", "utilities", "interpersonal_safety",
        ),
    ),
    "hunger_vital_sign": SdoHInstrument(
        key="hunger_vital_sign",
        display_name="Hunger Vital Sign",
        loinc_panel="88121-9",
        linkid_prefix="hvs-item-",
        items=(
            SdoHItem(1, ("hvs-item-1",), "88122-7",
                     "food_insecurity",
                     ("often_true", "sometimes_true", "1")),
            SdoHItem(2, ("hvs-item-2",), "88123-5",
                     "food_insecurity",
                     ("often_true", "sometimes_true", "1")),
        ),
        covered_domains=("food_insecurity",),
    ),
    "who_qol_bref": SdoHInstrument(
        key="who_qol_bref",
        display_name="WHOQOL-BREF",
        loinc_panel="93104-8",
        linkid_prefix="whoqol-item-",
        # Subset: Q3 (physical pain) and Q6 (meaningful life) as proxies
        # for physical / psychological wellbeing. Full 26-item panel can
        # be added later; downstream consumes item-level answers as-is.
        items=(
            SdoHItem(3, ("whoqol-item-3",), None,
                     "physical_wellbeing",
                     ("very_much", "an_extreme_amount", "5")),
            SdoHItem(6, ("whoqol-item-6",), None,
                     "psychological_wellbeing",
                     ("not_at_all", "a_little", "1", "2")),
            SdoHItem(21, ("whoqol-item-21",), None,
                     "social_isolation",
                     ("very_dissatisfied", "dissatisfied", "1", "2")),
        ),
        covered_domains=(
            "physical_wellbeing", "psychological_wellbeing",
            "social_isolation",
        ),
    ),
    "seek": SdoHInstrument(
        key="seek",
        display_name="SEEK",
        loinc_panel="100755-2",
        linkid_prefix="seek-item-",
        items=(
            SdoHItem(1, ("seek-item-1",), None,
                     "food_insecurity",     ("yes", "1")),
            SdoHItem(2, ("seek-item-2",), None,
                     "child_safety",        ("yes", "1")),
            SdoHItem(3, ("seek-item-3",), None,
                     "interpersonal_safety", ("yes", "1")),
            SdoHItem(4, ("seek-item-4",), None,
                     "psychological_wellbeing", ("yes", "1")),
            SdoHItem(5, ("seek-item-5",), None,
                     "child_safety",        ("yes", "1")),
        ),
        covered_domains=(
            "food_insecurity", "child_safety",
            "interpersonal_safety", "psychological_wellbeing",
        ),
    ),
}


# ── Reverse indexes ─────────────────────────────────────────────────────

SDOH_LOINC_TO_INSTRUMENT: dict[str, str] = {
    inst.loinc_panel: key for key, inst in SDOH_REGISTRY.items()
    if inst.loinc_panel
}

# Map item-level LOINCs → (instrument_key, item_number) for panels that
# publish per-item codes (AHC-HRSN, HVS).
SDOH_LOINC_ITEM_TO_INSTRUMENT: dict[str, tuple[str, int]] = {}
for _key, _inst in SDOH_REGISTRY.items():
    for _item in _inst.items:
        if _item.loinc:
            SDOH_LOINC_ITEM_TO_INSTRUMENT[_item.loinc] = (_key, _item.item_number)


def get_sdoh_instrument_by_loinc(loinc: Optional[str]) -> Optional[SdoHInstrument]:
    if not loinc:
        return None
    key = SDOH_LOINC_TO_INSTRUMENT.get(loinc)
    return SDOH_REGISTRY.get(key) if key else None


def get_sdoh_instrument_by_key(key: str) -> Optional[SdoHInstrument]:
    return SDOH_REGISTRY.get(key)


def evaluate_sdoh_positive_domains(
    instrument: SdoHInstrument,
    item_answers: dict[int, str],
) -> list[str]:
    """Given per-item answers (item_number → answer string), return the
    sub-domains flagged positive by this response.
    """
    if not item_answers:
        return []
    positive: set[str] = set()
    for item in instrument.items:
        ans = item_answers.get(item.item_number)
        if ans is None:
            continue
        if str(ans).strip().lower() in {s.lower() for s in item.positive_answers}:
            positive.add(item.domain)
    # Preserve registry order for stability.
    order = {d: i for i, d in enumerate(SDOH_DOMAINS)}
    return sorted(positive, key=lambda d: order.get(d, 999))


def register(mcp):  # pragma: no cover — data module, not a tool
    return

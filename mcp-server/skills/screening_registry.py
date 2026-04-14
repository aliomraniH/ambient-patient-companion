"""Screening instrument registry — single source of truth for behavioral
health screening tools supported by the system.

Pure data module. No DB imports, no app-internal imports, no LLM calls.
Every other behavioral module (gap detector, ingestor, section builder,
cards tool) keys off this registry so that adding a new instrument is a
one-dict-entry change.

17 instruments across 11 domains:

    domain                 instruments
    ─────────────────────────────────────────────────────────────
    depression             phq9, phq2
    anxiety                gad7, gad2
    suicide_risk           cssrs, asq
    bipolar                mdq
    adhd                   asrs5
    trauma                 pcptsd5, pcl5
    alcohol_use            auditc, audit
    substance_use          dast10, cagetaid
    eating_disorder        scoff
    psychosis              prodromal_q
    cognitive              mini_cog

Lookback windows, severity bands, and critical-item definitions are all
attached to the registry entry so domain-driven code never has to branch
on instrument name. See v2 plan §"Card schema" for how these feed cards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Dataclasses ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SeverityBand:
    """A contiguous score band with clinical label."""
    label: str          # e.g. "moderate", "severe"
    min_score: int      # inclusive
    max_score: int      # inclusive
    action: Optional[str] = None  # e.g. "refer_psychiatry"


@dataclass(frozen=True)
class CriticalItem:
    """A per-item threshold that must trigger regardless of total score.

    Example: PHQ-9 item 9 (SI question) >= 1 always triggers a flag even
    when the total score is in the minimal band.
    """
    item_number: int            # linkId index (1-based)
    linkid_candidates: tuple[str, ...]  # common FHIR linkIds for this item
    threshold: int              # fires when `score >= threshold`
    alert_text: str             # short label for the card
    priority: str = "critical"  # 'critical' | 'high' | 'medium' | 'low'


@dataclass(frozen=True)
class ScreeningInstrument:
    key: str                            # registry key, e.g. 'phq9'
    display_name: str                   # canonical name, e.g. 'PHQ-9'
    domain: str                         # one of DOMAINS
    loinc_panel: str                    # top-level LOINC for the instrument
    loinc_item_codes: tuple[str, ...]   # individual item LOINCs (ordered)
    linkid_prefix: str                  # FHIR linkId prefix, e.g. 'phq9-item-'
    total_score_range: tuple[int, int]  # (min, max)
    severity_bands: tuple[SeverityBand, ...]
    critical_items: tuple[CriticalItem, ...] = ()
    # Atom signal types that should suggest THIS instrument to be
    # administered. Used by the domain gap detector.
    atom_signals: tuple[str, ...] = ()
    # Gender-specific total-score cutoff for positive screen (e.g.
    # AUDIT-C >=3 female, >=4 male). Empty dict = gender-neutral.
    gender_positive_cutoff: dict = field(default_factory=dict)
    # Default positive cutoff (gender-neutral or female default).
    positive_cutoff: Optional[int] = None
    notes: str = ""


# ── Domains ─────────────────────────────────────────────────────────────

DOMAINS = {
    "depression":       {"label": "Depression"},
    "anxiety":          {"label": "Anxiety"},
    "suicide_risk":     {"label": "Suicide risk"},
    "bipolar":          {"label": "Bipolar spectrum"},
    "adhd":             {"label": "ADHD / attention"},
    "trauma":           {"label": "Trauma / PTSD"},
    "alcohol_use":      {"label": "Alcohol use"},
    "substance_use":    {"label": "Substance use"},
    "eating_disorder":  {"label": "Eating disorder"},
    "psychosis":        {"label": "Psychosis risk"},
    "cognitive":        {"label": "Cognitive screen"},
}


# How long an administered screening "covers" a domain before it becomes
# stale and a new screening is recommended. Keyed by domain.
DOMAIN_LOOKBACK_DAYS: dict[str, int] = {
    "depression":       365,   # USPSTF: annual
    "anxiety":          365,
    "suicide_risk":     180,   # higher-frequency for at-risk
    "bipolar":          730,
    "adhd":             1825,  # 5 yrs — stable trait
    "trauma":           730,
    "alcohol_use":      365,
    "substance_use":    365,
    "eating_disorder":  365,
    "psychosis":        365,
    "cognitive":        365,
}


# ── The registry ────────────────────────────────────────────────────────

SCREENING_REGISTRY: dict[str, ScreeningInstrument] = {
    # ── Depression ──────────────────────────────────────────────────────
    "phq9": ScreeningInstrument(
        key="phq9",
        display_name="PHQ-9",
        domain="depression",
        loinc_panel="44249-1",
        loinc_item_codes=(
            "44250-9", "44255-8", "44259-0", "44254-1", "44251-7",
            "44258-2", "44252-5", "44253-3", "44260-8",
        ),
        linkid_prefix="phq9-item-",
        total_score_range=(0, 27),
        severity_bands=(
            SeverityBand("minimal",        0,  4),
            SeverityBand("mild",           5,  9),
            SeverityBand("moderate",      10, 14, "consider_treatment"),
            SeverityBand("moderately_severe", 15, 19, "active_treatment"),
            SeverityBand("severe",        20, 27, "refer_psychiatry"),
        ),
        critical_items=(
            CriticalItem(
                item_number=9,
                linkid_candidates=("phq9-item-9", "9", "phq9_9"),
                threshold=1,
                alert_text="PHQ-9 item 9 (passive SI) elevated",
                priority="critical",
            ),
        ),
        atom_signals=(
            "low_affect", "passive_si", "social_withdrawal",
            "sleep_disturbance", "appetite_change", "psychomotor_slowing",
            "concentration_difficulty",
        ),
        positive_cutoff=10,
    ),
    "phq2": ScreeningInstrument(
        key="phq2",
        display_name="PHQ-2",
        domain="depression",
        loinc_panel="55757-9",
        loinc_item_codes=("44250-9", "44255-8"),
        linkid_prefix="phq2-item-",
        total_score_range=(0, 6),
        severity_bands=(
            SeverityBand("negative", 0, 2),
            SeverityBand("positive", 3, 6, "administer_phq9"),
        ),
        atom_signals=("low_affect", "social_withdrawal"),
        positive_cutoff=3,
    ),
    # ── Anxiety ─────────────────────────────────────────────────────────
    "gad7": ScreeningInstrument(
        key="gad7",
        display_name="GAD-7",
        domain="anxiety",
        loinc_panel="69737-5",
        loinc_item_codes=(
            "69725-0", "68509-9", "69733-4", "69734-2",
            "69735-9", "69689-8", "69736-7",
        ),
        linkid_prefix="gad7-item-",
        total_score_range=(0, 21),
        severity_bands=(
            SeverityBand("minimal",  0,  4),
            SeverityBand("mild",     5,  9),
            SeverityBand("moderate", 10, 14, "consider_treatment"),
            SeverityBand("severe",   15, 21, "refer_bh"),
        ),
        atom_signals=(
            "anxiety_markers", "psychomotor_restlessness", "device_checking",
            "somatic_preoccupation", "sleep_disturbance",
        ),
        positive_cutoff=10,
    ),
    "gad2": ScreeningInstrument(
        key="gad2",
        display_name="GAD-2",
        domain="anxiety",
        loinc_panel="70274-6",
        loinc_item_codes=("69725-0", "68509-9"),
        linkid_prefix="gad2-item-",
        total_score_range=(0, 6),
        severity_bands=(
            SeverityBand("negative", 0, 2),
            SeverityBand("positive", 3, 6, "administer_gad7"),
        ),
        atom_signals=("anxiety_markers",),
        positive_cutoff=3,
    ),

    # ── Suicide risk ────────────────────────────────────────────────────
    "cssrs": ScreeningInstrument(
        key="cssrs",
        display_name="C-SSRS",
        domain="suicide_risk",
        loinc_panel="93373-9",
        loinc_item_codes=(),
        linkid_prefix="cssrs-item-",
        total_score_range=(0, 5),
        severity_bands=(
            SeverityBand("no_ideation",    0, 0),
            SeverityBand("passive",        1, 1),
            SeverityBand("active_no_plan", 2, 3, "safety_plan"),
            SeverityBand("active_plan",    4, 5, "emergency_assessment"),
        ),
        critical_items=(
            CriticalItem(3, ("cssrs-item-3", "cssrs-3"), 1,
                         "C-SSRS item 3 (active ideation)"),
        ),
        atom_signals=("passive_si",),
        positive_cutoff=1,
    ),
    "asq": ScreeningInstrument(
        key="asq",
        display_name="ASQ",
        domain="suicide_risk",
        loinc_panel="93374-7",
        loinc_item_codes=(),
        linkid_prefix="asq-item-",
        total_score_range=(0, 4),
        severity_bands=(
            SeverityBand("non_acute", 0, 0),
            SeverityBand("positive",  1, 4, "brief_suicide_safety_assessment"),
        ),
        critical_items=(
            CriticalItem(4, ("asq-item-4",), 1,
                         "ASQ item 4 (active ideation)"),
        ),
        atom_signals=("passive_si",),
        positive_cutoff=1,
    ),

    # ── Bipolar ─────────────────────────────────────────────────────────
    "mdq": ScreeningInstrument(
        key="mdq",
        display_name="MDQ",
        domain="bipolar",
        loinc_panel="71492-3",
        loinc_item_codes=(),
        linkid_prefix="mdq-item-",
        total_score_range=(0, 13),
        severity_bands=(
            SeverityBand("negative", 0, 6),
            SeverityBand("positive", 7, 13, "refer_psychiatry"),
        ),
        atom_signals=("elevated_affect", "mood_lability", "irritability"),
        positive_cutoff=7,
    ),
    # ── ADHD ────────────────────────────────────────────────────────────
    "asrs5": ScreeningInstrument(
        key="asrs5",
        display_name="ASRS-5",
        domain="adhd",
        loinc_panel="91819-3",
        loinc_item_codes=(),
        linkid_prefix="asrs5-item-",
        total_score_range=(0, 30),
        severity_bands=(
            SeverityBand("negative", 0, 13),
            SeverityBand("positive", 14, 30, "refer_adhd_eval"),
        ),
        atom_signals=(
            "psychomotor_restlessness", "attention_switching",
            "device_checking", "concentration_difficulty",
        ),
        positive_cutoff=14,
    ),

    # ── Trauma ──────────────────────────────────────────────────────────
    "pcptsd5": ScreeningInstrument(
        key="pcptsd5",
        display_name="PC-PTSD-5",
        domain="trauma",
        loinc_panel="89208-3",  # placeholder — PC-PTSD-5 uses custom code
        loinc_item_codes=(),
        linkid_prefix="pcptsd5-item-",
        total_score_range=(0, 5),
        severity_bands=(
            SeverityBand("negative", 0, 2),
            SeverityBand("positive", 3, 5, "administer_pcl5"),
        ),
        atom_signals=("anxiety_markers", "somatic_preoccupation",
                      "sleep_disturbance", "irritability"),
        positive_cutoff=3,
    ),
    "pcl5": ScreeningInstrument(
        key="pcl5",
        display_name="PCL-5",
        domain="trauma",
        loinc_panel="93375-4",
        loinc_item_codes=(),
        linkid_prefix="pcl5-item-",
        total_score_range=(0, 80),
        severity_bands=(
            SeverityBand("negative", 0, 32),
            SeverityBand("positive", 33, 80, "refer_bh"),
        ),
        atom_signals=("anxiety_markers", "sleep_disturbance", "irritability"),
        positive_cutoff=33,
    ),

    # ── Alcohol ─────────────────────────────────────────────────────────
    "auditc": ScreeningInstrument(
        key="auditc",
        display_name="AUDIT-C",
        domain="alcohol_use",
        loinc_panel="75624-7",
        loinc_item_codes=("68518-0", "68519-8", "68520-6"),
        linkid_prefix="auditc-item-",
        total_score_range=(0, 12),
        severity_bands=(
            SeverityBand("negative",        0,  2),
            SeverityBand("positive_female", 3,  12),
            SeverityBand("positive_male",   4,  12),
        ),
        atom_signals=("somatic_preoccupation",),
        # Gender-specific thresholds for risk-drinking.
        gender_positive_cutoff={"female": 3, "male": 4},
        positive_cutoff=3,
    ),
    "audit": ScreeningInstrument(
        key="audit",
        display_name="AUDIT",
        domain="alcohol_use",
        loinc_panel="75626-2",
        loinc_item_codes=(),
        linkid_prefix="audit-item-",
        total_score_range=(0, 40),
        severity_bands=(
            SeverityBand("low_risk",      0,  7),
            SeverityBand("hazardous",     8, 15, "brief_intervention"),
            SeverityBand("harmful",      16, 19, "brief_intervention"),
            SeverityBand("dependence",   20, 40, "refer_aud"),
        ),
        atom_signals=("somatic_preoccupation",),
        positive_cutoff=8,
    ),

    # ── Substance use ───────────────────────────────────────────────────
    "dast10": ScreeningInstrument(
        key="dast10",
        display_name="DAST-10",
        domain="substance_use",
        loinc_panel="82666-8",
        loinc_item_codes=(),
        linkid_prefix="dast10-item-",
        total_score_range=(0, 10),
        severity_bands=(
            SeverityBand("none",         0, 0),
            SeverityBand("low",          1, 2),
            SeverityBand("moderate",     3, 5, "brief_intervention"),
            SeverityBand("substantial",  6, 8, "refer_sud"),
            SeverityBand("severe",       9, 10, "refer_sud"),
        ),
        atom_signals=("somatic_preoccupation",),
        positive_cutoff=3,
    ),
    "cagetaid": ScreeningInstrument(
        key="cagetaid",
        display_name="CAGE-AID",
        domain="substance_use",
        loinc_panel="89206-7",
        loinc_item_codes=(),
        linkid_prefix="cageaid-item-",
        total_score_range=(0, 4),
        severity_bands=(
            SeverityBand("negative", 0, 1),
            SeverityBand("positive", 2, 4, "refer_sud"),
        ),
        atom_signals=("somatic_preoccupation",),
        positive_cutoff=2,
    ),

    # ── Eating disorder ─────────────────────────────────────────────────
    "scoff": ScreeningInstrument(
        key="scoff",
        display_name="SCOFF",
        domain="eating_disorder",
        loinc_panel="89207-5",
        loinc_item_codes=(),
        linkid_prefix="scoff-item-",
        total_score_range=(0, 5),
        severity_bands=(
            SeverityBand("negative", 0, 1),
            SeverityBand("positive", 2, 5, "refer_eating_disorder"),
        ),
        atom_signals=("appetite_change", "somatic_preoccupation"),
        positive_cutoff=2,
    ),

    # ── Psychosis ───────────────────────────────────────────────────────
    "prodromal_q": ScreeningInstrument(
        key="prodromal_q",
        display_name="PQ-16",
        domain="psychosis",
        loinc_panel="93376-2",
        loinc_item_codes=(),
        linkid_prefix="pq16-item-",
        total_score_range=(0, 16),
        severity_bands=(
            SeverityBand("negative", 0, 5),
            SeverityBand("positive", 6, 16, "refer_early_psychosis"),
        ),
        atom_signals=("social_withdrawal", "mood_lability"),
        positive_cutoff=6,
    ),

    # ── Cognitive ───────────────────────────────────────────────────────
    "mini_cog": ScreeningInstrument(
        key="mini_cog",
        display_name="Mini-Cog",
        domain="cognitive",
        loinc_panel="72172-0",
        loinc_item_codes=(),
        linkid_prefix="minicog-item-",
        total_score_range=(0, 5),
        severity_bands=(
            SeverityBand("negative", 3, 5),
            SeverityBand("positive", 0, 2, "refer_neurocog_eval"),
        ),
        atom_signals=("concentration_difficulty",),
        positive_cutoff=2,
    ),
}


# ── Reverse-index helpers ───────────────────────────────────────────────

# Map a panel-level LOINC → registry key for fast inbound ingest lookup.
LOINC_TO_INSTRUMENT: dict[str, str] = {
    inst.loinc_panel: key
    for key, inst in SCREENING_REGISTRY.items()
    if inst.loinc_panel
}

# Map each item-level LOINC → (instrument_key, item_index).
LOINC_ITEM_TO_INSTRUMENT: dict[str, tuple[str, int]] = {}
for _key, _inst in SCREENING_REGISTRY.items():
    for _idx, _loinc in enumerate(_inst.loinc_item_codes, start=1):
        if _loinc:
            LOINC_ITEM_TO_INSTRUMENT[_loinc] = (_key, _idx)


def get_instrument_by_loinc(loinc: Optional[str]) -> Optional[ScreeningInstrument]:
    """Return the registry entry whose panel LOINC matches, or None."""
    if not loinc:
        return None
    key = LOINC_TO_INSTRUMENT.get(loinc)
    return SCREENING_REGISTRY.get(key) if key else None


def get_instrument_by_key(key: str) -> Optional[ScreeningInstrument]:
    return SCREENING_REGISTRY.get(key)


def instruments_for_domain(domain: str) -> list[ScreeningInstrument]:
    return [i for i in SCREENING_REGISTRY.values() if i.domain == domain]


def severity_band_for_score(
    instrument: ScreeningInstrument, score: int
) -> Optional[SeverityBand]:
    """Return the severity band whose [min,max] contains `score`.

    Bands may overlap (AUDIT-C has gender-specific bands); the first
    containing band wins.
    """
    if score is None:
        return None
    for band in instrument.severity_bands:
        if band.min_score <= score <= band.max_score:
            return band
    return None


def is_positive_screen(
    instrument: ScreeningInstrument,
    score: int,
    gender: Optional[str] = None,
) -> bool:
    """Apply gender-aware positive-screen threshold where applicable."""
    if score is None:
        return False
    if instrument.gender_positive_cutoff and gender:
        g = gender.lower()
        cutoff = instrument.gender_positive_cutoff.get(g)
        if cutoff is not None:
            return score >= cutoff
    if instrument.positive_cutoff is not None:
        return score >= instrument.positive_cutoff
    return False


def suggest_instruments_from_atoms(signal_types: list[str]) -> list[str]:
    """Rank instruments by how many of the given atom signal_types they
    target. Returns top-3 display names (stable-ordered by count desc,
    registry order tiebreaker).
    """
    from collections import Counter
    counts: Counter = Counter()
    # Preserve registry order for tiebreak stability.
    order: dict[str, int] = {k: i for i, k in enumerate(SCREENING_REGISTRY)}
    for sig in signal_types:
        for key, inst in SCREENING_REGISTRY.items():
            if sig in inst.atom_signals:
                counts[key] += 1
    ranked = sorted(
        counts.items(),
        key=lambda kv: (-kv[1], order.get(kv[0], 999)),
    )
    return [SCREENING_REGISTRY[k].display_name for k, _ in ranked[:3]]


def suggest_domains_from_atoms(signal_types: list[str]) -> list[str]:
    """Return domains (in priority order) implicated by the given atom
    signal_types. Used by the domain-driven gap detector.
    """
    from collections import Counter
    counts: Counter = Counter()
    order: dict[str, int] = {k: i for i, k in enumerate(DOMAINS)}
    for sig in signal_types:
        for inst in SCREENING_REGISTRY.values():
            if sig in inst.atom_signals:
                counts[inst.domain] += 1
    ranked = sorted(
        counts.items(),
        key=lambda kv: (-kv[1], order.get(kv[0], 999)),
    )
    return [d for d, _ in ranked]


def critical_items_triggered(
    instrument: ScreeningInstrument,
    item_scores: dict[int, int] | None,
) -> list[dict]:
    """Given per-item scores keyed by item_number, return the list of
    critical-item hits. Shape matches card evidence schema.
    """
    triggered: list[dict] = []
    if not item_scores:
        return triggered
    for ci in instrument.critical_items:
        score = item_scores.get(ci.item_number)
        if score is None:
            continue
        try:
            if int(score) >= ci.threshold:
                triggered.append({
                    "instrument": instrument.display_name,
                    "item_number": ci.item_number,
                    "alert_text": ci.alert_text,
                    "actual_score": int(score),
                    "priority": ci.priority,
                })
        except (TypeError, ValueError):
            continue
    return triggered


def register(mcp):  # pragma: no cover — registry is data, not a tool
    return

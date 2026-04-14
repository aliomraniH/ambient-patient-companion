"""
screening_registry.py — SCREENING_REGISTRY + lookup helpers for Behavioral V2.

Pure data module — no DB, no imports from other app modules.
Sits under mcp-server/skills/ so callers can:
    from skills.screening_registry import SCREENING_REGISTRY, get_domain_for_loinc

17 instruments across 11 clinical domains.
Adding a new instrument = adding one dict entry to SCREENING_REGISTRY.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ─── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class SeverityBand:
    label: str          # 'none'|'mild'|'moderate'|'moderately_severe'|'severe'
    min_score: int
    max_score: int
    action: str         # brief clinical guidance


@dataclass
class CriticalItem:
    item_number: int        # 1-based item index within the questionnaire
    item_text: str
    alert_text: str         # short alert shown in critical_flags
    threshold: int          # answer value that triggers the flag (≥ threshold)


@dataclass
class ScreeningInstrument:
    key: str                    # short identifier, e.g. 'phq9', 'gad7'
    name: str                   # full human name
    domain: str                 # one of DOMAINS keys
    loinc_code: str             # primary LOINC code for this questionnaire
    item_count: int
    score_range: tuple[int, int]     # (min, max)
    lookback_days: int          # recommended re-screening window
    atom_signals: list[str]     # signal types from behavioral_atom_extractor
    severity_bands: list[SeverityBand]
    critical_items: list[CriticalItem] = field(default_factory=list)
    mode: str = "A"             # 'A' (available) | 'B' (primary evidence)
    notes: str = ""


# ─── Domain definitions ────────────────────────────────────────────────────────

DOMAINS: dict[str, str] = {
    "depression":       "Depression",
    "anxiety":          "Anxiety",
    "substance_use":    "Substance Use",
    "ptsd_trauma":      "PTSD / Trauma",
    "adhd":             "ADHD",
    "suicidality":      "Suicidality / Self-Harm",
    "bipolar":          "Bipolar Spectrum",
    "eating_disorder":  "Eating Disorder",
    "sleep":            "Sleep",
    "cognitive":        "Cognitive Function",
    "somatic":          "Somatic Symptoms",
}

# ─── Domain lookback defaults (days) ──────────────────────────────────────────

DOMAIN_LOOKBACK_DAYS: dict[str, int] = {
    "depression":       180,
    "anxiety":          180,
    "substance_use":    365,
    "ptsd_trauma":      365,
    "adhd":             365,
    "suicidality":      90,
    "bipolar":          365,
    "eating_disorder":  365,
    "sleep":            180,
    "cognitive":        365,
    "somatic":          180,
}

# ─── SCREENING_REGISTRY ────────────────────────────────────────────────────────

SCREENING_REGISTRY: dict[str, ScreeningInstrument] = {

    # ── Depression ────────────────────────────────────────────────────────────

    "phq9": ScreeningInstrument(
        key="phq9",
        name="Patient Health Questionnaire-9 (PHQ-9)",
        domain="depression",
        loinc_code="44249-1",
        item_count=9,
        score_range=(0, 27),
        lookback_days=180,
        atom_signals=["depression_markers", "mood_changes", "sleep_disturbance",
                      "appetite_change", "concentration_difficulty", "social_withdrawal"],
        severity_bands=[
            SeverityBand("none",              0,  4,  "No action needed."),
            SeverityBand("mild",              5,  9,  "Watchful waiting; monitor."),
            SeverityBand("moderate",         10, 14,  "Treatment plan; counselling/medication."),
            SeverityBand("moderately_severe",15, 19,  "Active treatment; consider MDD diagnosis."),
            SeverityBand("severe",           20, 27,  "Immediate treatment; hospitalization risk."),
        ],
        critical_items=[
            CriticalItem(9, "Thoughts that you would be better off dead or of hurting yourself",
                         "passive SI", threshold=1),
        ],
        mode="A",
    ),

    "phq2": ScreeningInstrument(
        key="phq2",
        name="Patient Health Questionnaire-2 (PHQ-2)",
        domain="depression",
        loinc_code="55757-9",
        item_count=2,
        score_range=(0, 6),
        lookback_days=90,
        atom_signals=["depression_markers", "mood_changes"],
        severity_bands=[
            SeverityBand("negative", 0, 2, "Low risk; no further action."),
            SeverityBand("positive", 3, 6, "Positive screen — administer full PHQ-9."),
        ],
        mode="A",
    ),

    "epds": ScreeningInstrument(
        key="epds",
        name="Edinburgh Postnatal Depression Scale (EPDS)",
        domain="depression",
        loinc_code="89049-1",
        item_count=10,
        score_range=(0, 30),
        lookback_days=30,
        atom_signals=["depression_markers", "anxiety_markers", "mood_changes"],
        severity_bands=[
            SeverityBand("low_risk",   0,  8,  "Low risk."),
            SeverityBand("borderline", 9, 11,  "Monitor; repeat in 2 weeks."),
            SeverityBand("high_risk", 12, 30,  "High risk — refer for evaluation."),
        ],
        critical_items=[
            CriticalItem(10, "Thoughts of self-harm",
                         "perinatal SI", threshold=1),
        ],
        notes="Administer at 4–6 weeks and 4 months postpartum.",
        mode="A",
    ),

    # ── Anxiety ───────────────────────────────────────────────────────────────

    "gad7": ScreeningInstrument(
        key="gad7",
        name="Generalized Anxiety Disorder-7 (GAD-7)",
        domain="anxiety",
        loinc_code="69737-5",
        item_count=7,
        score_range=(0, 21),
        lookback_days=180,
        atom_signals=["anxiety_markers", "hypervigilance", "sleep_disturbance",
                      "concentration_difficulty", "avoidance_behavior"],
        severity_bands=[
            SeverityBand("none",     0,  4,  "No action needed."),
            SeverityBand("mild",     5,  9,  "Monitor."),
            SeverityBand("moderate",10, 14,  "Consider therapy/medication."),
            SeverityBand("severe",  15, 21,  "Active treatment required."),
        ],
        mode="A",
    ),

    "gad2": ScreeningInstrument(
        key="gad2",
        name="Generalized Anxiety Disorder-2 (GAD-2)",
        domain="anxiety",
        loinc_code="55758-7",
        item_count=2,
        score_range=(0, 6),
        lookback_days=90,
        atom_signals=["anxiety_markers"],
        severity_bands=[
            SeverityBand("negative", 0, 2, "Low risk."),
            SeverityBand("positive", 3, 6, "Positive screen — administer full GAD-7."),
        ],
        mode="A",
    ),

    # ── Substance Use ─────────────────────────────────────────────────────────

    "audit_c": ScreeningInstrument(
        key="audit_c",
        name="Alcohol Use Disorders Identification Test-Concise (AUDIT-C)",
        domain="substance_use",
        loinc_code="75626-2",
        item_count=3,
        score_range=(0, 12),
        lookback_days=365,
        atom_signals=["substance_mention"],
        severity_bands=[
            SeverityBand("low",      0,  2,  "Low risk (male) or 0–1 (female)."),
            SeverityBand("moderate", 3,  7,  "Counsel on reducing intake."),
            SeverityBand("high",     8, 12,  "Brief intervention; referral for treatment."),
        ],
        mode="A",
    ),

    "audit": ScreeningInstrument(
        key="audit",
        name="Alcohol Use Disorders Identification Test (AUDIT)",
        domain="substance_use",
        loinc_code="75624-7",
        item_count=10,
        score_range=(0, 40),
        lookback_days=365,
        atom_signals=["substance_mention"],
        severity_bands=[
            SeverityBand("low",           0,  7,  "Low risk — positive reinforcement."),
            SeverityBand("hazardous",     8, 15,  "Simple advice on risky drinking."),
            SeverityBand("harmful",      16, 19,  "Brief counseling; monitor."),
            SeverityBand("dependent",    20, 40,  "Referral for treatment."),
        ],
        mode="B",
        notes="Preferred over AUDIT-C when full characterisation needed.",
    ),

    "dast10": ScreeningInstrument(
        key="dast10",
        name="Drug Abuse Screening Test-10 (DAST-10)",
        domain="substance_use",
        loinc_code="82666-9",
        item_count=10,
        score_range=(0, 10),
        lookback_days=365,
        atom_signals=["substance_mention"],
        severity_bands=[
            SeverityBand("none",     0,  0,  "No problems."),
            SeverityBand("low",      1,  2,  "Monitor."),
            SeverityBand("moderate", 3,  5,  "Brief intervention."),
            SeverityBand("high",     6,  8,  "Referral for assessment."),
            SeverityBand("severe",   9, 10,  "Immediate referral."),
        ],
        mode="A",
    ),

    # ── PTSD / Trauma ─────────────────────────────────────────────────────────

    "pc_ptsd5": ScreeningInstrument(
        key="pc_ptsd5",
        name="Primary Care PTSD Screen for DSM-5 (PC-PTSD-5)",
        domain="ptsd_trauma",
        loinc_code="83476-7",
        item_count=5,
        score_range=(0, 5),
        lookback_days=365,
        atom_signals=["trauma_markers", "hypervigilance", "avoidance_behavior",
                      "sleep_disturbance"],
        severity_bands=[
            SeverityBand("negative", 0, 2, "Low risk."),
            SeverityBand("positive", 3, 5, "Positive screen — administer PCL-5 or refer."),
        ],
        mode="A",
    ),

    "pcl5": ScreeningInstrument(
        key="pcl5",
        name="PTSD Checklist for DSM-5 (PCL-5)",
        domain="ptsd_trauma",
        loinc_code="83480-9",
        item_count=20,
        score_range=(0, 80),
        lookback_days=365,
        atom_signals=["trauma_markers", "hypervigilance", "avoidance_behavior",
                      "sleep_disturbance", "social_withdrawal"],
        severity_bands=[
            SeverityBand("minimal",  0, 32, "Below clinical threshold."),
            SeverityBand("threshold",33, 80, "Probable PTSD — clinical evaluation."),
        ],
        mode="B",
        notes="Score ≥ 33 provisional PTSD diagnosis (DSM-5).",
    ),

    # ── ADHD ──────────────────────────────────────────────────────────────────

    "asrs5": ScreeningInstrument(
        key="asrs5",
        name="Adult ADHD Self-Report Scale-5 (ASRS-5)",
        domain="adhd",
        loinc_code="89048-3",
        item_count=6,
        score_range=(0, 24),
        lookback_days=365,
        atom_signals=["adhd_markers", "concentration_difficulty"],
        severity_bands=[
            SeverityBand("negative", 0, 13, "Low likelihood of ADHD."),
            SeverityBand("positive", 14, 24, "Positive screen — clinical evaluation."),
        ],
        mode="A",
    ),

    # ── Suicidality ───────────────────────────────────────────────────────────

    "cssrs": ScreeningInstrument(
        key="cssrs",
        name="Columbia Suicide Severity Rating Scale (C-SSRS)",
        domain="suicidality",
        loinc_code="89204-2",
        item_count=6,
        score_range=(0, 25),
        lookback_days=90,
        atom_signals=["suicidality_markers", "depression_markers"],
        severity_bands=[
            SeverityBand("none",     0,  0,  "No suicidal ideation."),
            SeverityBand("passive",  1,  5,  "Passive ideation — safety plan."),
            SeverityBand("active",   6, 15,  "Active ideation — urgent evaluation."),
            SeverityBand("critical",16, 25,  "High lethality — emergency services."),
        ],
        critical_items=[
            CriticalItem(1, "Wish to be dead", "passive SI", threshold=1),
            CriticalItem(5, "Active suicidal ideation with plan", "active SI with plan", threshold=1),
            CriticalItem(6, "Suicidal behavior (attempt)", "suicide attempt", threshold=1),
        ],
        mode="B",
        notes="Any 'yes' on items 5–6 requires immediate escalation.",
    ),

    # ── Bipolar ───────────────────────────────────────────────────────────────

    "mdq": ScreeningInstrument(
        key="mdq",
        name="Mood Disorder Questionnaire (MDQ)",
        domain="bipolar",
        loinc_code="96809-2",
        item_count=13,
        score_range=(0, 13),
        lookback_days=365,
        atom_signals=["mood_changes", "depression_markers", "concentration_difficulty"],
        severity_bands=[
            SeverityBand("negative", 0,  6, "Below threshold."),
            SeverityBand("positive", 7, 13, "Positive — psychiatric evaluation."),
        ],
        mode="A",
        notes="Positive if score ≥7 AND symptoms occurred together AND moderate/serious impairment.",
    ),

    # ── Eating Disorder ───────────────────────────────────────────────────────

    "scoff": ScreeningInstrument(
        key="scoff",
        name="SCOFF Eating Disorders Questionnaire",
        domain="eating_disorder",
        loinc_code="91399-7",
        item_count=5,
        score_range=(0, 5),
        lookback_days=365,
        atom_signals=["appetite_change", "social_withdrawal"],
        severity_bands=[
            SeverityBand("negative", 0, 1, "Low risk."),
            SeverityBand("positive", 2, 5, "Positive screen — eating disorder evaluation."),
        ],
        mode="A",
    ),

    # ── Sleep ─────────────────────────────────────────────────────────────────

    "isi": ScreeningInstrument(
        key="isi",
        name="Insomnia Severity Index (ISI)",
        domain="sleep",
        loinc_code="97162-3",
        item_count=7,
        score_range=(0, 28),
        lookback_days=180,
        atom_signals=["sleep_disturbance", "anxiety_markers"],
        severity_bands=[
            SeverityBand("none",      0,  7, "No significant insomnia."),
            SeverityBand("subthreshold", 8, 14, "Sub-threshold — sleep hygiene counselling."),
            SeverityBand("moderate",  15, 21, "Moderate — CBT-I or pharmacotherapy."),
            SeverityBand("severe",    22, 28, "Severe — immediate intervention."),
        ],
        mode="A",
    ),

    # ── Cognitive ────────────────────────────────────────────────────────────

    "moca": ScreeningInstrument(
        key="moca",
        name="Montreal Cognitive Assessment (MoCA)",
        domain="cognitive",
        loinc_code="72172-0",
        item_count=30,
        score_range=(0, 30),
        lookback_days=365,
        atom_signals=["cognitive_concerns", "concentration_difficulty"],
        severity_bands=[
            SeverityBand("normal",   26, 30, "Normal."),
            SeverityBand("mild_ci",  18, 25, "Mild cognitive impairment — follow-up."),
            SeverityBand("moderate",  10, 17, "Moderate — detailed neuropsychological evaluation."),
            SeverityBand("severe",    0,  9, "Severe — dementia workup."),
        ],
        mode="A",
    ),

    # ── Somatic ───────────────────────────────────────────────────────────────

    "phq15": ScreeningInstrument(
        key="phq15",
        name="Patient Health Questionnaire-15 (PHQ-15)",
        domain="somatic",
        loinc_code="44255-8",
        item_count=15,
        score_range=(0, 30),
        lookback_days=180,
        atom_signals=["somatic_complaints", "anxiety_markers", "depression_markers"],
        severity_bands=[
            SeverityBand("low",      0,  4,  "Low somatic symptom burden."),
            SeverityBand("medium",   5,  9,  "Medium — investigate."),
            SeverityBand("high",    10, 14,  "High — comprehensive evaluation."),
            SeverityBand("very_high",15, 30,  "Very high — possible somatic symptom disorder."),
        ],
        mode="A",
    ),
}

# ─── Signal type → domain mapping (for gap detection) ────────────────────────

_SIGNAL_TO_DOMAINS: dict[str, list[str]] = {}

for _inst in SCREENING_REGISTRY.values():
    for _sig in _inst.atom_signals:
        _SIGNAL_TO_DOMAINS.setdefault(_sig, [])
        if _inst.domain not in _SIGNAL_TO_DOMAINS[_sig]:
            _SIGNAL_TO_DOMAINS[_sig].append(_inst.domain)

# ─── LOINC lookup ─────────────────────────────────────────────────────────────

_LOINC_TO_KEY: dict[str, str] = {
    inst.loinc_code: key for key, inst in SCREENING_REGISTRY.items()
}

# ─── Public helpers ───────────────────────────────────────────────────────────

def get_domain_for_loinc(loinc_code: str) -> Optional[str]:
    """Return the domain for a LOINC code, or None if not in registry."""
    key = _LOINC_TO_KEY.get(loinc_code)
    if key:
        return SCREENING_REGISTRY[key].domain
    return None


def get_instrument_for_loinc(loinc_code: str) -> Optional[ScreeningInstrument]:
    """Return the ScreeningInstrument for a LOINC code, or None."""
    key = _LOINC_TO_KEY.get(loinc_code)
    return SCREENING_REGISTRY.get(key)


def get_instruments_for_domain(domain: str) -> list[ScreeningInstrument]:
    """Return all instruments for a given domain."""
    return [inst for inst in SCREENING_REGISTRY.values() if inst.domain == domain]


def suggest_instruments_from_atoms(signal_types: list[str]) -> dict[str, list[str]]:
    """Map observed atom signal types to recommended instruments per domain.

    Returns: {domain: [instrument_key, ...]} — only domains with at least
    one matching signal type. Instruments are deduplicated and ordered by
    their position in SCREENING_REGISTRY (definition order = clinical priority).
    """
    domain_instruments: dict[str, list[str]] = {}
    for signal in signal_types:
        for domain in _SIGNAL_TO_DOMAINS.get(signal, []):
            if domain not in domain_instruments:
                domain_instruments[domain] = []
            for key, inst in SCREENING_REGISTRY.items():
                if inst.domain == domain and key not in domain_instruments[domain]:
                    domain_instruments[domain].append(key)
    return domain_instruments


def get_severity_band(instrument_key: str, score: int) -> Optional[SeverityBand]:
    """Return the matching SeverityBand for a given instrument and score."""
    inst = SCREENING_REGISTRY.get(instrument_key)
    if not inst:
        return None
    for band in inst.severity_bands:
        if band.min_score <= score <= band.max_score:
            return band
    return None


def get_triggered_critical_items(
    instrument_key: str, item_scores: dict[int, int]
) -> list[CriticalItem]:
    """Return CriticalItems whose threshold was met given item_scores dict.

    item_scores: {item_number (1-based): answer_value}
    """
    inst = SCREENING_REGISTRY.get(instrument_key)
    if not inst:
        return []
    return [
        ci for ci in inst.critical_items
        if item_scores.get(ci.item_number, 0) >= ci.threshold
    ]


LOINC_TO_INSTRUMENT: dict[str, str] = {
    inst.loinc_code: key
    for key, inst in SCREENING_REGISTRY.items()
    if inst.loinc_code
}


def register(mcp) -> None:
    """No-op: screening_registry is a pure data module. Skill loader ignores it."""
    pass

"""
behavioral_atom_extractor.py — Rule-based + keyword extraction of behavioral
signal atoms from free-text (conversation turns, clinical notes, check-in notes).

15 signal types mapped from SCREENING_REGISTRY.atom_signals:
  anxiety_markers, depression_markers, substance_mention, trauma_markers,
  adhd_markers, suicidality_markers, sleep_disturbance, appetite_change,
  somatic_complaints, cognitive_concerns, mood_changes, social_withdrawal,
  avoidance_behavior, hypervigilance, concentration_difficulty

Extraction is synchronous and rule-based only — no LLM calls here.
Each extracted atom has: signal_type, signal_value (extracted phrase),
confidence (0.0–1.0), and source metadata.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExtractedAtom:
    signal_type: str
    signal_value: str           # the extracted span or phrase (max 500 chars)
    confidence: float           # 0.0 – 1.0
    source_type: str            # 'conversation'|'clinical_note'|'checkin'
    source_id: Optional[str]    # UUID of source row, if available


# ─── Signal patterns ──────────────────────────────────────────────────────────
# Each entry: (compiled regex, base confidence float)
# Patterns are applied case-insensitively. Longer matches win ties.

_PATTERNS: dict[str, list[tuple[re.Pattern, float]]] = {

    "anxiety_markers": [
        (re.compile(r"\b(anxious|anxiety|panick?(?:ing|ed)?|panic attack|nervous|on edge|keyed up|restless|worried sick|heart racing|heart pounding|short of breath|can't breathe)\b", re.I), 0.80),
        (re.compile(r"\b(GAD|generali[sz]ed anxiety|OCD|obsessive|compulsive)\b", re.I), 0.85),
        (re.compile(r"\b(wr?[ae]ck(?:ing|ed)? my nerves|scared all the time|constant worry|endless worry)\b", re.I), 0.75),
        (re.compile(r"\b(trembling|tremor|shakiness|sweating|shaking with (?:fear|anxiety|worry))\b", re.I), 0.70),
    ],

    "depression_markers": [
        (re.compile(r"\b(depress(?:ed|ion|ing)?|hopeless(?:ness)?|worthless(?:ness)?|sad(?:ness)?|empty inside|no joy|can't enjoy|anhedoni[ac])\b", re.I), 0.82),
        (re.compile(r"\b(low mood|down|feeling low|blue|miserable|despondent|grief|bereaved|devastated)\b", re.I), 0.70),
        (re.compile(r"\b(PHQ|major depressive|MDD|dysthymi[ac]|PDD|persistent depressive)\b", re.I), 0.88),
        (re.compile(r"\b(cry(?:ing)?|tearful|can't stop crying|sobbing all (?:the time|day))\b", re.I), 0.72),
    ],

    "substance_mention": [
        (re.compile(r"\b(alcohol|drink(?:ing)?|wine|beer|spirits?|liquor|drunk|hangover|binge drink)\b", re.I), 0.75),
        (re.compile(r"\b(drug(?:s)?|opioid|opiate|heroin|cocaine|meth(?:amphetamine)?|crack|marijuana|cannabis|weed|pot|fentanyl|benzodiazepine|benzo)\b", re.I), 0.82),
        (re.compile(r"\b(substance (?:use|abuse|disorder)|SUD|addiction|depend(?:ence|ent)|withdrawal|detox)\b", re.I), 0.88),
        (re.compile(r"\b(relaps(?:e|ed|ing)?|cravings?|urge to use|using again)\b", re.I), 0.85),
        (re.compile(r"\b(AUDIT|DAST|CAGE|cut down|annoyed by criticism|guilty about drinking|eye-opener)\b", re.I), 0.80),
    ],

    "trauma_markers": [
        (re.compile(r"\b(trauma(?:tic)?|PTSD|post[- ]traumatic|flashback|nightmare|intrusive thought|triggered)\b", re.I), 0.85),
        (re.compile(r"\b(abuse(?:d)?|assault(?:ed)?|violence|domestic violence|sexual assault|rape|combat|war veteran)\b", re.I), 0.80),
        (re.compile(r"\b(can't get it out of my head|reliving|feels like it's happening again|terror)\b", re.I), 0.75),
    ],

    "adhd_markers": [
        (re.compile(r"\b(ADHD|attention deficit|hyperactivity|can't focus|can't sit still|impulsiv(?:e|ity))\b", re.I), 0.85),
        (re.compile(r"\b(fidgety|always moving|racing thoughts|mind won't stop|scattered|disorgani[sz]ed|forget everything)\b", re.I), 0.70),
        (re.compile(r"\b(stimulant|Adderall|Ritalin|methylphenidate|amphetamine)\b", re.I), 0.78),
    ],

    "suicidality_markers": [
        (re.compile(r"\b(suicid(?:al|e|ality|e attempt)|self[- ]harm|self[- ]injury|cutting myself|want to die|end my life|kill myself|not want to (?:be here|live|exist))\b", re.I), 0.92),
        (re.compile(r"\b(better off dead|wish I (?:was|were) dead|passive ideation|active ideation|plan to (?:hurt|harm|kill))\b", re.I), 0.90),
        (re.compile(r"\b(C-SSRS|Columbia severity|suicide (?:risk|screening|assessment)|ideation)\b", re.I), 0.80),
        (re.compile(r"\b(overdose|take all my pills|jump|hang myself)\b", re.I), 0.92),
    ],

    "sleep_disturbance": [
        (re.compile(r"\b(insomnia|can't sleep|trouble sleeping|sleep(?:ing)? problem|wak(?:ing|e) up (?:all night|repeatedly)|sleep deprivation|hypersomnia|oversleeping)\b", re.I), 0.80),
        (re.compile(r"\b(ISI|insomnia severity|nightmares|sleep apnea|restless legs|CPAP|poor sleep quality|sleep hygiene)\b", re.I), 0.78),
        (re.compile(r"\b(fatigue|exhausted|tired all the time|no energy|burnt out|wiped out|can't get out of bed)\b", re.I), 0.65),
    ],

    "appetite_change": [
        (re.compile(r"\b(not eating|skipping meals|no appetite|lost my appetite|eating too much|binge(?:ing)?|purge|vomit(?:ing)?|restrict(?:ing)? (?:food|eating))\b", re.I), 0.80),
        (re.compile(r"\b(weight (?:loss|gain)|lost \d+ pounds|gained \d+ pounds|anorexia|bulimia|eating disorder|SCOFF|ARFID)\b", re.I), 0.82),
        (re.compile(r"\b(food insecurity|can't afford food|hungry all day|skipping food)\b", re.I), 0.75),
    ],

    "somatic_complaints": [
        (re.compile(r"\b(pain|ache|headache|migraine|stomach(?:ache)?|abdominal pain|chest pain|back pain|chronic pain|fibromyalgia)\b", re.I), 0.68),
        (re.compile(r"\b(PHQ-15|somatic (?:symptom|disorder)|medically unexplained|functional (?:disorder|syndrome))\b", re.I), 0.82),
        (re.compile(r"\b(nausea|dizziness|dizzy|vertigo|numbness|tingling|palpitations)\b", re.I), 0.65),
    ],

    "cognitive_concerns": [
        (re.compile(r"\b(memory (?:loss|problems?)|forgetful(?:ness)?|dementia|alzheimer|MCI|cognitive (?:decline|impairment)|MoCA|SLUMS|confusion|disoriented)\b", re.I), 0.82),
        (re.compile(r"\b(brain fog|can't remember|keeps forgetting|losing my mind|confusion|mixed up)\b", re.I), 0.70),
    ],

    "mood_changes": [
        (re.compile(r"\b(mood (?:swing|change|shift|episode)|irritab(?:le|ility)|anger|rage|euphori[ac]|elat(?:ed|ion)|grandiosity|manic|hypomania)\b", re.I), 0.78),
        (re.compile(r"\b(MDQ|bipolar|rapid cycling|mixed (?:state|episode)|mood stabil(?:izer|iser)|lithium|valproate|lamotrigine)\b", re.I), 0.84),
        (re.compile(r"\b(lashing out|snapping at everyone|can't control (?:my )?temper|explosive|outburst)\b", re.I), 0.72),
    ],

    "social_withdrawal": [
        (re.compile(r"\b(isolated|isolating|withdrawn|avoiding (?:people|friends|family|social)|not leaving (?:home|house)|hermit|reclusive)\b", re.I), 0.78),
        (re.compile(r"\b(no friends|lost interest in (?:people|socializing)|don't want to see anyone|cancelled (?:plans|everything))\b", re.I), 0.72),
        (re.compile(r"\b(lonely|alone all the time|no one to talk to|disconnected)\b", re.I), 0.65),
    ],

    "avoidance_behavior": [
        (re.compile(r"\b(avoid(?:ing|ance)?|won't go|refuse to (?:go|leave|talk about)|scared to|phobia|agoraphobia|social phobia)\b", re.I), 0.75),
        (re.compile(r"\b(not facing|running away from|hiding from|can't bring myself to|avoid (?:thinking|talking) about)\b", re.I), 0.70),
    ],

    "hypervigilance": [
        (re.compile(r"\b(hypervigilant|always on guard|startl(?:e|ing) easily|jump at (?:sounds|noises)|paranoi[ad]|scanning for danger|can't relax)\b", re.I), 0.80),
        (re.compile(r"\b(feel unsafe|not safe anywhere|watching my back|suspicious of everyone)\b", re.I), 0.72),
    ],

    "concentration_difficulty": [
        (re.compile(r"\b(can't concentrate|trouble (?:focusing|concentrating)|attention (?:problems?|issues?)|zoning out|distracted)\b", re.I), 0.72),
        (re.compile(r"\b(mind wander(?:s|ing)|losing (?:my )?train of thought|can't stay on task|easily distracted|attention span)\b", re.I), 0.68),
    ],
}

# ─── Extraction logic ─────────────────────────────────────────────────────────

def _extract_spans(text: str, signal_type: str) -> list[tuple[str, float]]:
    """Return (matched_text, confidence) tuples for a signal_type."""
    results: list[tuple[str, float]] = []
    seen_starts: set[int] = set()

    for pattern, base_conf in _PATTERNS.get(signal_type, []):
        for m in pattern.finditer(text):
            start = m.start()
            if start in seen_starts:
                continue
            seen_starts.add(start)
            # Expand to sentence boundary (crude: ±100 chars, stripped).
            snippet_start = max(0, start - 80)
            snippet_end   = min(len(text), m.end() + 80)
            snippet = text[snippet_start:snippet_end].strip()
            # Clamp to 500 chars.
            if len(snippet) > 500:
                snippet = snippet[:497] + "..."
            results.append((snippet, base_conf))

    return results


def extract_atoms_from_text(
    text: str,
    source_type: str = "conversation",
    source_id: Optional[str] = None,
    min_confidence: float = 0.0,
    max_atoms_per_signal: int = 5,
) -> list[ExtractedAtom]:
    """Extract all behavioral signal atoms from a text string.

    Args:
        text:                 Free-text to analyse.
        source_type:          'conversation'|'clinical_note'|'checkin'
        source_id:            UUID string of the originating row (or None).
        min_confidence:       Discard atoms below this threshold.
        max_atoms_per_signal: Cap extractions per signal type to avoid noise.

    Returns:
        List of ExtractedAtom, deduplicated by (signal_type, signal_value).
    """
    if not text or not text.strip():
        return []

    atoms: list[ExtractedAtom] = []
    seen: set[tuple[str, str]] = set()

    for signal_type in _PATTERNS:
        spans = _extract_spans(text, signal_type)
        count = 0
        for value, conf in spans:
            if conf < min_confidence:
                continue
            key = (signal_type, value[:100])
            if key in seen:
                continue
            seen.add(key)
            atoms.append(ExtractedAtom(
                signal_type=signal_type,
                signal_value=value,
                confidence=conf,
                source_type=source_type,
                source_id=source_id,
            ))
            count += 1
            if count >= max_atoms_per_signal:
                break

    return atoms


def extract_atoms_from_checkin(
    checkin: dict,
    source_id: Optional[str] = None,
) -> list[ExtractedAtom]:
    """Extract atoms from a daily_checkins row dict.

    Reads: notes, mood, energy, stress_level, sleep_quality, sleep_hours.
    """
    parts: list[str] = []

    if checkin.get("notes"):
        parts.append(str(checkin["notes"]))

    mood = str(checkin.get("mood") or "")
    if mood in ("terrible", "bad", "very_bad"):
        parts.append("feeling depressed and very low mood today")
    elif mood in ("anxious", "stressed", "worried"):
        parts.append("feeling anxious and stressed today")

    stress = checkin.get("stress_level")
    if isinstance(stress, (int, float)) and stress >= 8:
        parts.append("extreme stress level today")

    sleep_hours = checkin.get("sleep_hours")
    sleep_quality = str(checkin.get("sleep_quality") or "")
    if isinstance(sleep_hours, (int, float)) and sleep_hours < 4:
        parts.append("severe sleep deprivation, only a few hours sleep")
    if sleep_quality in ("poor", "terrible", "very_poor"):
        parts.append("very poor sleep quality, trouble sleeping")

    text = " | ".join(parts)
    return extract_atoms_from_text(
        text,
        source_type="checkin",
        source_id=source_id,
    )


# ─── Executor-facing aliases ───────────────────────────────────────────────────

async def extract_atoms_from_note(
    note_text: str,
    note_date,
    source_note_id: Optional[str] = None,
    patient_id: Optional[str] = None,
) -> list[ExtractedAtom]:
    """Extract atoms from a clinical note.

    Thin wrapper over extract_atoms_from_text. note_date and patient_id are
    accepted for API compatibility (patient_id is not embedded in ExtractedAtom
    to keep the extraction layer PHI-minimal).
    """
    return extract_atoms_from_text(
        text=note_text,
        source_type="clinical_note",
        source_id=source_note_id,
    )


async def insert_atoms(conn, atoms: list[ExtractedAtom]) -> int:
    """Insert a list of ExtractedAtom into behavioral_signal_atoms.

    Uses the asyncpg connection directly (not a pool). Skips rows whose
    signal_value exceeds 2000 chars. Returns number of rows inserted.
    """
    if not atoms:
        return 0

    inserted = 0
    for atom in atoms:
        try:
            await conn.execute(
                """
                INSERT INTO behavioral_signal_atoms
                    (signal_type, signal_value, confidence, source_type, source_id)
                VALUES ($1, $2, $3, $4, $5::uuid)
                ON CONFLICT DO NOTHING
                """,
                atom.signal_type,
                atom.signal_value[:2000],
                atom.confidence,
                atom.source_type,
                atom.source_id,
            )
            inserted += 1
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "insert_atoms: skipped atom (%s): %s", atom.signal_type, type(e).__name__
            )
    return inserted

"""
planner.py — Pre-deliberation agenda planner.

Runs as Phase 0.5: after context is assembled (Phase 0) but before
independent agent analyses begin (Phase 1).

Produces a DeliberationAgenda that:
  1. Identifies the highest-priority clinical questions for this patient
  2. Assigns a lead domain per question (diagnostic_reasoning / treatment_optimization)
  3. Flags known cross-domain interaction patterns to probe
  4. Notes data quality issues that may affect reasoning

The agenda is injected into each agent's prompt context in Phase 1
so agents reason with shared orientation rather than in isolation.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

import anthropic

log = logging.getLogger(__name__)

# Use Haiku for planning — fast, cheap, sufficient for agenda generation
_PLANNER_MODEL = os.environ.get("CLAUDE_HAIKU_MODEL", "claude-haiku-4-5-20251001")


@dataclass
class AgendaItem:
    question: str                  # Clinical question to answer
    lead_domain: str               # "diagnostic_reasoning" | "treatment_optimization" | "all"
    supporting_domains: list[str] = field(default_factory=list)
    interaction_flag: str = ""     # Cross-domain signal to watch for
    priority: str = "medium"       # "high" | "medium" | "low"


@dataclass
class DeliberationAgenda:
    items: list[AgendaItem] = field(default_factory=list)
    data_quality_warnings: list[str] = field(default_factory=list)
    cross_domain_pairs: list[tuple[str, str]] = field(default_factory=list)

    def to_prompt_context(self) -> str:
        """Serialize agenda to a compact string for injection into agent prompts."""
        lines = ["=== DELIBERATION AGENDA ==="]
        lines.append("Priority questions for this session:")
        for i, item in enumerate(self.items, 1):
            lead = item.lead_domain
            support = (
                f" (with {', '.join(item.supporting_domains)})"
                if item.supporting_domains
                else ""
            )
            lines.append(f"  {i}. [{item.priority.upper()}] {item.question}")
            lines.append(f"     Lead: {lead}{support}")
            if item.interaction_flag:
                lines.append(f"     Watch for: {item.interaction_flag}")
        if self.cross_domain_pairs:
            lines.append("\nKnown cross-domain interactions to probe explicitly:")
            for a, b in self.cross_domain_pairs:
                lines.append(f"  - {a} <-> {b}")
        if self.data_quality_warnings:
            lines.append("\nData quality warnings:")
            for w in self.data_quality_warnings:
                lines.append(f"  ! {w}")
        lines.append("=== END AGENDA ===")
        return "\n".join(lines)


# Cross-domain interaction patterns known to be clinically significant
# for polychronic patients (T2DM + HTN + anxiety profiles).
_KNOWN_INTERACTION_PATTERNS = [
    {
        "signal_a": ["metformin", "glp-1", "sglt2", "insulin"],
        "signal_b": ["mood", "anxiety", "depression", "behavioral", "phq"],
        "pair": ("medication_change", "behavioral_decline"),
        "flag": "GI side effects from DM medications can cause or worsen anxiety/depression.",
    },
    {
        "signal_a": ["anxiety", "stress", "gad", "phq", "mood"],
        "signal_b": ["hba1c", "glucose", "blood sugar", "metabolic"],
        "pair": ("anxiety_elevation", "glucose_dysregulation"),
        "flag": "Chronic stress drives cortisol-mediated glucose elevation independent of diet.",
    },
    {
        "signal_a": ["lisinopril", "ace inhibitor", "arb", "beta blocker", "antihypertensive"],
        "signal_b": ["fatigue", "exercise", "steps", "activity", "energy"],
        "pair": ("antihypertensive_medication", "activity_decline"),
        "flag": "Beta blockers and ACE inhibitors can cause fatigue affecting adherence and activity.",
    },
    {
        "signal_a": ["non-adherent", "missed doses", "adherence"],
        "signal_b": ["anxiety", "depression", "social", "sdoh", "food", "housing"],
        "pair": ("medication_non_adherence", "behavioral_or_sdoh_cause"),
        "flag": "Non-adherence is often behavioral or social in origin, not patient unwillingness.",
    },
    {
        "signal_a": ["ckd", "egfr", "creatinine", "renal", "kidney"],
        "signal_b": ["metformin", "nsaid", "contrast", "medication"],
        "pair": ("renal_function", "medication_safety"),
        "flag": "CKD affects Metformin dosing and multiple other medication clearance rates.",
    },
]


def _detect_interaction_pairs(context_text: str) -> list[tuple[tuple[str, str], str]]:
    """
    Deterministic scan: check context text for known cross-domain signal pairs.
    Returns list of (pair_tuple, flag_string).
    """
    text_lower = context_text.lower()
    detected = []
    for pattern in _KNOWN_INTERACTION_PATTERNS:
        has_a = any(sig in text_lower for sig in pattern["signal_a"])
        has_b = any(sig in text_lower for sig in pattern["signal_b"])
        if has_a and has_b:
            detected.append((pattern["pair"], pattern["flag"]))
    return detected


def _detect_data_quality_warnings(context_text: str) -> list[str]:
    """Deterministic scan for data quality signals in context."""
    warnings = []
    text_lower = context_text.lower()
    if "0.0" in context_text and ("lab" in text_lower or "result" in text_lower):
        warnings.append("One or more lab values appear as 0.0 — possible data corruption.")
    if "stale" in text_lower:
        warnings.append("One or more data sources are flagged as stale.")
    if "conflict" in text_lower:
        warnings.append("Data source conflicts detected — verify before acting on findings.")
    return warnings


def _context_to_text(patient_context) -> str:
    """Convert a PatientContextPackage or dict to text for scanning."""
    if isinstance(patient_context, dict):
        return json.dumps(patient_context, default=str)
    # Try dataclass/Pydantic serialization
    if hasattr(patient_context, "model_dump"):
        return json.dumps(patient_context.model_dump(), default=str)
    if hasattr(patient_context, "__dict__"):
        return json.dumps(patient_context.__dict__, default=str)
    return str(patient_context)


async def build_deliberation_agenda(
    patient_context,
    deliberation_id: str,
) -> DeliberationAgenda:
    """
    Build a deliberation agenda from the assembled patient context.

    Uses two-phase approach:
    1. Deterministic: scan for known interaction patterns and data quality issues
    2. LLM: generate priority clinical questions and domain assignments

    Falls back to a minimal agenda if LLM call fails — never blocks deliberation.
    """
    context_text = _context_to_text(patient_context)

    # Phase 1: deterministic scans
    interaction_pairs = _detect_interaction_pairs(context_text)
    data_warnings = _detect_data_quality_warnings(context_text)

    # Phase 2: LLM agenda generation
    try:
        agenda = await _llm_generate_agenda(
            context_text=context_text,
            interaction_pairs=interaction_pairs,
            deliberation_id=deliberation_id,
        )
        # Inject deterministically-detected pairs that LLM may have missed
        existing_pairs = set(agenda.cross_domain_pairs)
        for pair, flag in interaction_pairs:
            if pair not in existing_pairs:
                agenda.cross_domain_pairs.append(pair)
                pair_question = f"Examine interaction: {pair[0]} <-> {pair[1]}"
                if not any(pair_question in item.question for item in agenda.items):
                    agenda.items.append(AgendaItem(
                        question=pair_question,
                        lead_domain="all",
                        interaction_flag=flag,
                        priority="high",
                    ))
        agenda.data_quality_warnings.extend(data_warnings)
        return agenda

    except Exception as e:
        log.warning(
            "planner: LLM agenda generation failed for deliberation_id=%s: %s — "
            "using deterministic fallback",
            deliberation_id, e,
        )
        return _fallback_agenda(interaction_pairs, data_warnings)


async def _llm_generate_agenda(
    context_text: str,
    interaction_pairs: list,
    deliberation_id: str,
) -> DeliberationAgenda:
    """Generate clinical agenda via LLM (Claude Haiku)."""
    client = anthropic.AsyncAnthropic()

    interaction_hints = ""
    if interaction_pairs:
        hints = [f"- {p[0]} <-> {p[1]}: {f}" for p, f in interaction_pairs]
        interaction_hints = (
            "\n\nPre-detected cross-domain signals (verify these explicitly):\n"
            + "\n".join(hints)
        )

    prompt = f"""You are preparing a clinical deliberation agenda for a dual-agent AI council.
The two agents are:
- diagnostic_reasoning: focuses on diagnosis, risk trajectory, comorbidity interactions
- treatment_optimization: focuses on treatment plans, medication optimization, guideline adherence

Patient context summary:
{context_text[:3000]}
{interaction_hints}

Generate a deliberation agenda as a JSON object with this exact structure:
{{
  "items": [
    {{
      "question": "specific clinical question to answer",
      "lead_domain": "diagnostic_reasoning|treatment_optimization|all",
      "supporting_domains": [],
      "interaction_flag": "optional cross-domain signal to probe",
      "priority": "high|medium|low"
    }}
  ],
  "cross_domain_pairs": [["signal_a", "signal_b"]]
}}

Rules:
- Maximum 5 agenda items
- Every cross-domain interaction signal gets its own item with lead_domain="all"
- Prioritize questions about medication effects on behavioral health and vice versa
- Output ONLY the JSON object, no markdown fences, no preamble
"""

    response = await client.messages.create(
        model=_PLANNER_MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    # Strip fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    data = json.loads(raw)

    items = [
        AgendaItem(
            question=item["question"],
            lead_domain=item.get("lead_domain", "all"),
            supporting_domains=item.get("supporting_domains", []),
            interaction_flag=item.get("interaction_flag", ""),
            priority=item.get("priority", "medium"),
        )
        for item in data.get("items", [])
    ]

    cross_domain = [
        tuple(pair) for pair in data.get("cross_domain_pairs", [])
        if isinstance(pair, list) and len(pair) == 2
    ]

    return DeliberationAgenda(items=items, cross_domain_pairs=cross_domain)


def _fallback_agenda(
    interaction_pairs: list,
    data_warnings: list[str],
) -> DeliberationAgenda:
    """Minimal agenda when LLM call fails."""
    items = []
    for pair, flag in interaction_pairs:
        items.append(AgendaItem(
            question=f"Examine interaction: {pair[0]} <-> {pair[1]}",
            lead_domain="all",
            interaction_flag=flag,
            priority="high",
        ))
    return DeliberationAgenda(
        items=items or [AgendaItem(
            question="Identify the most clinically significant concern for this patient.",
            lead_domain="all",
            priority="medium",
        )],
        data_quality_warnings=data_warnings,
        cross_domain_pairs=[p for p, _ in interaction_pairs],
    )

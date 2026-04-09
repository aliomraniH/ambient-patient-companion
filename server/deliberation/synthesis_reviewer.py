"""
synthesis_reviewer.py — Post-synthesis review by domain agents.

Runs as Phase 3.5: after synthesis produces a DeliberationResult (Phase 3)
but before behavioral adaptation (Phase 4).

Two domain reviewers (matching the actual dual-model architecture):
  - diagnostic_reasoning: reviews for clinical errors, missed diagnostic signals
  - treatment_optimization: reviews for medication safety, treatment gaps

If any HIGH-severity objection is raised, re_deliberation_needed=True is
returned and the engine triggers a focused second deliberation round.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field

import anthropic

log = logging.getLogger(__name__)

_REVIEWER_MODEL = os.environ.get("CLAUDE_HAIKU_MODEL", "claude-haiku-4-5-20251001")


@dataclass
class AgentReview:
    domain: str             # "diagnostic_reasoning" | "treatment_optimization"
    concurs: bool
    objection: str = ""
    severity: str = ""      # "high" | "medium" | "low"
    missed_interaction: str = ""   # Specific missed cross-domain signal


@dataclass
class SynthesisReviewResult:
    reviews: list[AgentReview] = field(default_factory=list)
    re_deliberation_needed: bool = False
    re_deliberation_focus: str = ""   # Refined agenda for second round
    consensus_reached: bool = True


_DOMAIN_REVIEW_PROMPTS = {
    "diagnostic_reasoning": (
        "You are reviewing this synthesis from a DIAGNOSTIC REASONING perspective. "
        "Check for errors or missed findings in: "
        "diabetes management, hypertension, lab value interpretation, screening gaps, "
        "comorbidity interactions, and risk trajectory assessment."
    ),
    "treatment_optimization": (
        "You are reviewing this synthesis from a TREATMENT OPTIMIZATION perspective. "
        "Check for errors or missed findings in: "
        "drug interactions, dosing appropriateness given renal/hepatic function, "
        "medication side effects that could explain symptoms, "
        "guideline-concordance of treatment plans, and contraindications."
    ),
}


async def review_synthesis(
    synthesis_text: str,
    patient_context_text: str,
    agenda,   # DeliberationAgenda from planner.py (or None)
    deliberation_id: str,
) -> SynthesisReviewResult:
    """
    Run both domain reviewers on the synthesis in parallel.
    Returns a SynthesisReviewResult indicating whether re-deliberation is needed.
    """
    try:
        reviews = await asyncio.gather(
            *[
                _single_domain_review(
                    domain=domain,
                    system_prompt=prompt,
                    synthesis_text=synthesis_text,
                    patient_context_text=patient_context_text,
                    agenda=agenda,
                )
                for domain, prompt in _DOMAIN_REVIEW_PROMPTS.items()
            ],
            return_exceptions=True,
        )
    except Exception as e:
        log.warning(
            "synthesis_reviewer: parallel review failed for deliberation_id=%s: %s "
            "— skipping review",
            deliberation_id, e,
        )
        return SynthesisReviewResult(consensus_reached=True)

    valid_reviews = []
    for r in reviews:
        if isinstance(r, Exception):
            log.warning("synthesis_reviewer: one domain review failed: %s", r)
        else:
            valid_reviews.append(r)

    if not valid_reviews:
        return SynthesisReviewResult(consensus_reached=True)

    # Check for high-severity objections
    high_severity_objections = [
        r for r in valid_reviews
        if not r.concurs and r.severity == "high"
    ]

    re_deliberation_needed = len(high_severity_objections) > 0
    re_deliberation_focus = ""

    if re_deliberation_needed:
        focus_parts = [obj.objection for obj in high_severity_objections]
        missed = [
            obj.missed_interaction
            for obj in high_severity_objections
            if obj.missed_interaction
        ]
        if missed:
            focus_parts.append(
                "Examine cross-domain interactions: " + "; ".join(missed)
            )
        re_deliberation_focus = " | ".join(focus_parts)
        log.info(
            "synthesis_reviewer: re-deliberation triggered deliberation_id=%s "
            "focus=%s",
            deliberation_id, re_deliberation_focus,
        )

    return SynthesisReviewResult(
        reviews=valid_reviews,
        re_deliberation_needed=re_deliberation_needed,
        re_deliberation_focus=re_deliberation_focus,
        consensus_reached=not re_deliberation_needed,
    )


async def _single_domain_review(
    domain: str,
    system_prompt: str,
    synthesis_text: str,
    patient_context_text: str,
    agenda,
) -> AgentReview:
    """Single domain reviewer checks the synthesis output."""
    client = anthropic.AsyncAnthropic()

    agenda_context = ""
    if agenda is not None and hasattr(agenda, "to_prompt_context"):
        agenda_context = agenda.to_prompt_context()

    user_prompt = f"""
{agenda_context}

Patient context (abbreviated):
{patient_context_text[:1500]}

Synthesis to review:
{synthesis_text[:2000]}

Review the synthesis from your domain perspective. Focus on:
1. Are there clinical errors in your domain?
2. Are there cross-domain interactions involving your domain that were missed?
3. Does the synthesis adequately address the agenda items assigned to your domain?

Respond ONLY with a JSON object — no markdown fences:
{{
  "concurs": true|false,
  "objection": "specific objection if concurs=false, else empty string",
  "severity": "high|medium|low or empty if concurs=true",
  "missed_interaction": "describe any cross-domain interaction that was missed, or empty"
}}

Rules:
- concurs=false + severity=high ONLY if there is a meaningful clinical error or
  a dangerous cross-domain interaction that was missed
- Do not object to stylistic or formatting issues
- Be specific: name the medication, lab value, or clinical signal involved
"""

    response = await client.messages.create(
        model=_REVIEWER_MODEL,
        max_tokens=400,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    data = json.loads(raw)

    return AgentReview(
        domain=domain,
        concurs=data.get("concurs", True),
        objection=data.get("objection", ""),
        severity=data.get("severity", ""),
        missed_interaction=data.get("missed_interaction", ""),
    )

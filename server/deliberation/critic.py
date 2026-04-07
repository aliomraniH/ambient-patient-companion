"""
critic.py — Phase 2: Cross-critique rounds between Claude and GPT-4.
Each model reads the other's analysis and produces structured critique.
Models then revise based on critique. Repeat up to max_rounds.
"""
import asyncio
import json
from .schemas import (
    IndependentAnalysis, CrossCritique, RevisedAnalysis,
    PatientContextPackage, ClaimWithConfidence
)
from .json_utils import strip_markdown_fences


CONVERGENCE_THRESHOLD = 0.90  # semantic similarity score to stop early


def _compute_convergence(a: RevisedAnalysis, b: RevisedAnalysis) -> float:
    """
    Estimate convergence between two revised analyses.
    Simple overlap metric on finding texts — replace with
    sentence-transformer cosine similarity in production.
    """
    a_claims = set(f.claim.lower() for f in a.revised_findings)
    b_claims = set(f.claim.lower() for f in b.revised_findings)
    if not a_claims or not b_claims:
        return 0.0
    intersection = len(a_claims & b_claims)
    union = len(a_claims | b_claims)
    return intersection / union if union > 0 else 0.0


def _analysis_from_revision(revision: RevisedAnalysis) -> IndependentAnalysis:
    """Convert a RevisedAnalysis back to IndependentAnalysis for the next round."""
    return IndependentAnalysis(
        model_id=revision.model_id,
        role_emphasis="diagnostic_reasoning" if "claude" in revision.model_id else "treatment_optimization",
        key_findings=revision.revised_findings,
        risk_flags=[],
        recommended_actions=[],
        anticipated_trajectory="See prior round",
        missing_data_identified=[],
        raw_reasoning=revision.raw_revision
    )


async def _critique_with_model(
    model: str,
    partner_analysis: IndependentAnalysis,
    context: PatientContextPackage,
    round_number: int,
    load_prompt_fn,
    call_model_fn
) -> CrossCritique:
    """One model critiques the other's analysis."""
    prompt_file = f"critic_{'claude' if 'claude' in model else 'gpt4'}.xml"
    system_prompt = load_prompt_fn(prompt_file, {
        "ROUND_NUMBER": str(round_number),
        "PARTNER_ANALYSIS_JSON": partner_analysis.model_dump_json(indent=2),
        "PATIENT_CONTEXT_JSON": context.model_dump_json(indent=2)
    })
    raw = await call_model_fn(model, system_prompt,
                              "Produce your critique now.")
    critique = CrossCritique.model_validate_json(strip_markdown_fences(raw))
    critique.round_number = round_number
    return critique


async def _revise_with_model(
    model: str,
    current_analysis: IndependentAnalysis,
    critique: CrossCritique,
    context: PatientContextPackage,
    round_number: int,
    load_prompt_fn,
    call_model_fn
) -> RevisedAnalysis:
    """A model revises its analysis based on the critique it received."""
    revision_prompt = f"""You are revising your clinical analysis based on peer critique.
Round: {round_number}

Your original analysis:
{current_analysis.model_dump_json(indent=2)}

Critique received from your peer:
{critique.model_dump_json(indent=2)}

Original patient context:
{context.model_dump_json(indent=2)}

Instructions:
- Address each critique item. If the critique is valid, revise your finding.
- If you disagree with a critique, explain why and maintain your position.
- Do not change findings that were not critiqued unless new insight warrants it.

Respond ONLY with valid JSON matching EXACTLY this structure.
No preamble. No markdown fences. Pure JSON.

{{
  "revised_findings": [
    {{"claim": "...", "confidence": 0.85, "evidence_refs": ["..."]}}
  ],
  "revisions_made": [
    "Plain string: what changed and why"
  ],
  "maintained_positions": [
    "Plain string: what you defended and why"
  ],
  "raw_revision": "Your full chain of thought as a plain string."
}}"""

    raw = await call_model_fn(model, revision_prompt,
                              "Produce your revised analysis now.")
    revision = RevisedAnalysis.model_validate_json(strip_markdown_fences(raw))
    revision.model_id = model
    revision.round_number = round_number
    return revision


async def run_critique_rounds(
    claude_analysis: IndependentAnalysis,
    gpt4_analysis: IndependentAnalysis,
    context: PatientContextPackage,
    max_rounds: int,
    load_prompt_fn,
    call_claude_fn,
    call_gpt4_fn
) -> dict:
    """
    Phase 2: Run structured cross-critique for up to max_rounds.
    Returns transcript dict with all critiques and revisions.
    Stops early if convergence score exceeds CONVERGENCE_THRESHOLD.
    """
    transcript = {
        "phase1": {
            "claude": claude_analysis.model_dump(),
            "gpt4": gpt4_analysis.model_dump()
        },
        "phase2_rounds": []
    }

    current_claude = claude_analysis
    current_gpt4 = gpt4_analysis
    final_convergence = 0.0
    round_num = 0

    for round_num in range(1, max_rounds + 1):
        # Both models critique each other in parallel
        claude_critiques_gpt4, gpt4_critiques_claude = await asyncio.gather(
            _critique_with_model(
                "claude-sonnet-4-20250514", current_gpt4, context,
                round_num, load_prompt_fn, call_claude_fn
            ),
            _critique_with_model(
                "gpt-4o", current_claude, context,
                round_num, load_prompt_fn, call_gpt4_fn
            )
        )

        # Each model revises based on critique received
        claude_revised, gpt4_revised = await asyncio.gather(
            _revise_with_model("claude-sonnet-4-20250514", current_claude,
                               gpt4_critiques_claude, context, round_num,
                               load_prompt_fn, call_claude_fn),
            _revise_with_model("gpt-4o", current_gpt4,
                               claude_critiques_gpt4, context, round_num,
                               load_prompt_fn, call_gpt4_fn)
        )

        transcript["phase2_rounds"].append({
            "round": round_num,
            "claude_critique_of_gpt4": claude_critiques_gpt4.model_dump(),
            "gpt4_critique_of_claude": gpt4_critiques_claude.model_dump(),
            "claude_revised": claude_revised.model_dump(),
            "gpt4_revised": gpt4_revised.model_dump()
        })

        final_convergence = _compute_convergence(claude_revised, gpt4_revised)

        if final_convergence >= CONVERGENCE_THRESHOLD:
            break

        current_claude = _analysis_from_revision(claude_revised)
        current_gpt4 = _analysis_from_revision(gpt4_revised)

    return {
        "transcript": transcript,
        "final_claude_revision": current_claude,
        "final_gpt4_revision": current_gpt4,
        "convergence_score": final_convergence,
        "rounds_completed": round_num
    }

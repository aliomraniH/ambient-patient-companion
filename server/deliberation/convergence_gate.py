"""
CONVERGENCE-GATED SYNTHESIS OUTPUT
====================================
Replaces the naive merge with a three-tier output based on convergence score:

  >= 0.70 -> "consensus"        - Standard merged recommendation
  0.40-0.69 -> "partial"        - Agreed points + structured dispute surface
  < 0.40 -> "no_consensus"      - Full debate, no merged recommendation

The two-round deliberation retry runs before this function:
  If Round 1 convergence < 0.60 -> re-run critique rounds with fresh framing
  This function is called on whichever round produces the final output.

CRITICAL CONSTRAINT: When convergence < 0.40, recommendation fields MUST be
explicitly nulled. False consensus is more dangerous than admitted uncertainty.
"""

import logging
from enum import Enum
from typing import Any, Optional

from .schemas import DeliberationResult

log = logging.getLogger(__name__)


# Tier thresholds (inclusive lower bounds)
CONSENSUS_THRESHOLD = 0.70
PARTIAL_THRESHOLD = 0.40

# Re-deliberation retry triggered below this score
RETRY_THRESHOLD = 0.60


class ConvergenceTier(str, Enum):
    CONSENSUS = "consensus"
    PARTIAL = "partial_consensus"
    NO_CONSENSUS = "no_consensus"


def classify_convergence(score: float) -> ConvergenceTier:
    """Map a convergence score to a tier."""
    if score >= CONSENSUS_THRESHOLD:
        return ConvergenceTier.CONSENSUS
    if score >= PARTIAL_THRESHOLD:
        return ConvergenceTier.PARTIAL
    return ConvergenceTier.NO_CONSENSUS


def gate_synthesis_output(
    result: DeliberationResult,
    convergence_score: float,
) -> DeliberationResult:
    """
    Apply convergence gating to a synthesis output.

    Modifications by tier:
      CONSENSUS:    No changes — full output proceeds
      PARTIAL:      Mark scenarios uncertain; preserve recommendations with caveats
      NO_CONSENSUS: Empty nudge_content; remove new_inference knowledge updates;
                    cap scenario confidence at 0.40; populate dissenting_view;
                    add explicit uncertainty marker to unresolved_disagreements

    Args:
        result: Synthesized DeliberationResult from synthesizer.py
        convergence_score: Convergence score from critic.py (0.0-1.0)

    Returns:
        Modified DeliberationResult (same object, mutated)
    """
    tier = classify_convergence(convergence_score)

    log.info(
        "[CONVERGENCE_GATE] Score=%.3f tier=%s for deliberation_id=%s",
        convergence_score, tier.value, result.deliberation_id,
    )

    if tier == ConvergenceTier.CONSENSUS:
        # No modifications — full synthesis proceeds
        result.unresolved_disagreements.append({
            "convergence_tier": tier.value,
            "convergence_score": convergence_score,
            "note": "Models converged. Output represents consensus.",
        })
        return result

    if tier == ConvergenceTier.PARTIAL:
        # Mark all scenarios as uncertain; recommendations preserved with caveats
        for scenario in result.anticipatory_scenarios:
            if scenario.dissenting_view is None or not scenario.dissenting_view:
                scenario.dissenting_view = (
                    "Partial convergence — interpret with caution. "
                    f"Convergence score: {convergence_score:.2f}"
                )
            # Cap confidence below high to signal uncertainty
            scenario.confidence = min(scenario.confidence, 0.65)

        result.unresolved_disagreements.append({
            "convergence_tier": tier.value,
            "convergence_score": convergence_score,
            "note": (
                "Partial convergence between clinical agents. "
                "Recommendations preserved but flagged as uncertain. "
                "Provider review required before action."
            ),
        })
        return result

    # ── NO_CONSENSUS — Hard constraint enforcement ────────────────────────────
    log.warning(
        "[CONVERGENCE_GATE] No consensus (score=%.3f) — nulling recommendations "
        "for deliberation_id=%s",
        convergence_score, result.deliberation_id,
    )

    # 1. Empty all patient-facing nudges (no recommendation)
    nudges_count = len(result.nudge_content)
    result.nudge_content = []

    # 2. Remove new_inference knowledge updates (no inferred recommendations)
    new_inference_count = sum(
        1 for ku in result.knowledge_updates if ku.update_type == "new_inference"
    )
    result.knowledge_updates = [
        ku for ku in result.knowledge_updates
        if ku.update_type != "new_inference"
    ]

    # 3. Cap anticipatory scenario confidence at 0.40 (signals low quality)
    for scenario in result.anticipatory_scenarios:
        scenario.confidence = min(scenario.confidence, 0.40)
        if scenario.dissenting_view is None or not scenario.dissenting_view:
            scenario.dissenting_view = (
                "WARNING: Models did not converge. "
                "Do not act on this scenario without independent clinical review."
            )

    # 4. Add explicit no_consensus marker
    result.unresolved_disagreements.append({
        "convergence_tier": tier.value,
        "convergence_score": convergence_score,
        "recommendation": None,  # Explicitly None — no false consensus
        "nudges_suppressed": nudges_count,
        "inferences_removed": new_inference_count,
        "provider_note": (
            "Clinical agents could not reach consensus on this patient's care plan. "
            "Recommendations were SUPPRESSED to prevent false consensus. "
            "Review individual agent perspectives in transcript before acting."
        ),
    })

    return result


async def deliberate_with_retry(
    initial_critique_result: dict,
    claude_analysis,
    gpt4_analysis,
    context,
    max_additional_rounds: int,
    run_critique_rounds_fn,
    load_prompt_fn,
    call_claude_fn,
    call_gpt4_fn,
) -> dict:
    """
    Re-run critique rounds if initial convergence is below RETRY_THRESHOLD.

    In a batch pipeline, the extra time cost is irrelevant — accuracy gain
    from a second deliberation round is documented as significant.

    Args:
        initial_critique_result: Result dict from first run_critique_rounds()
        claude_analysis, gpt4_analysis: Original IndependentAnalysis instances
        context: PatientContextPackage
        max_additional_rounds: How many extra rounds to run if needed
        run_critique_rounds_fn: The run_critique_rounds function from critic.py
        load_prompt_fn, call_claude_fn, call_gpt4_fn: As in engine.py

    Returns:
        Best critique_result (either initial if good, or updated if retried)
    """
    initial_score = initial_critique_result.get("convergence_score", 0.0)

    if initial_score >= RETRY_THRESHOLD:
        log.info(
            "[CONVERGENCE_GATE] Initial convergence %.3f >= retry threshold %.2f — "
            "no retry needed.",
            initial_score, RETRY_THRESHOLD,
        )
        return initial_critique_result

    log.info(
        "[CONVERGENCE_GATE] Initial convergence %.3f < retry threshold %.2f — "
        "running %d additional rounds.",
        initial_score, RETRY_THRESHOLD, max_additional_rounds,
    )

    # Use the final revisions from Round 1 as starting point for Round 2
    final_claude = initial_critique_result.get("final_claude_revision", claude_analysis)
    final_gpt4 = initial_critique_result.get("final_gpt4_revision", gpt4_analysis)

    # Convert revisions back to analyses if needed
    from .schemas import RevisedAnalysis, IndependentAnalysis
    from .critic import _analysis_from_revision
    if isinstance(final_claude, RevisedAnalysis):
        final_claude = _analysis_from_revision(final_claude)
    if isinstance(final_gpt4, RevisedAnalysis):
        final_gpt4 = _analysis_from_revision(final_gpt4)

    try:
        retry_result = await run_critique_rounds_fn(
            claude_analysis=final_claude,
            gpt4_analysis=final_gpt4,
            context=context,
            max_rounds=max_additional_rounds,
            load_prompt_fn=load_prompt_fn,
            call_claude_fn=call_claude_fn,
            call_gpt4_fn=call_gpt4_fn,
        )
    except Exception as e:
        log.warning("[CONVERGENCE_GATE] Retry failed: %s — returning initial result", e)
        return initial_critique_result

    # Take whichever score is higher (retry might be worse)
    retry_score = retry_result.get("convergence_score", 0.0)
    if retry_score > initial_score:
        log.info(
            "[CONVERGENCE_GATE] Retry improved convergence: %.3f -> %.3f",
            initial_score, retry_score,
        )
        # Merge transcripts so audit trail is complete
        merged_transcript = {
            "round1": initial_critique_result.get("transcript", {}),
            "round2_retry": retry_result.get("transcript", {}),
        }
        retry_result["transcript"] = merged_transcript
        # Track total rounds across both attempts
        retry_result["rounds_completed"] = (
            initial_critique_result.get("rounds_completed", 0)
            + retry_result.get("rounds_completed", 0)
        )
        return retry_result

    log.info(
        "[CONVERGENCE_GATE] Retry did not improve convergence (%.3f vs %.3f) — "
        "using initial result.",
        retry_score, initial_score,
    )
    return initial_critique_result

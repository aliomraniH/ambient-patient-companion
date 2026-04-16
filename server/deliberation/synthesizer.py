"""
synthesizer.py — Phase 3: Synthesize full transcript into five structured outputs.
Uses Claude as the synthesizer (rotates in production for bias prevention).
"""
import hashlib
import json
from datetime import datetime
from .json_utils import safe_json_loads
from .schemas import DeliberationResult, PatientContextPackage


def _reorder_transcript_for_bias_mitigation(
    transcript: dict,
    deliberation_id: str,
) -> dict:
    """Deterministically permute Claude/GPT-4 order in the transcript view
    given to the synthesizer, mitigating primacy bias.

    The synthesizer always sees both analyses — this only alternates which
    one appears first in the JSON string sent to the LLM. The original
    transcript is preserved unchanged (stored in result.transcript for
    audit).

    Parity rule: even SHA-256-derived hash → Claude first (default order);
    odd hash → GPT-4 first. Deterministic per deliberation_id so reruns
    produce identical prompts.

    Research basis: BiasBusters (ICLR 2026) + Permutation Self-Consistency
    (Liu et al. TACL 2024). Alternating agent order is a 2-permutation
    approximation that meaningfully reduces first-position anchoring.
    """
    if not isinstance(transcript, dict):
        return transcript

    digest = hashlib.sha256((deliberation_id or "").encode("utf-8")).digest()
    swap = digest[0] % 2 == 1

    if not swap:
        return transcript  # keep original order (Claude first)

    reordered: dict = {}

    # Permute phase1
    phase1 = transcript.get("phase1") or {}
    new_phase1: dict = {}
    if "gpt4" in phase1:
        new_phase1["gpt4"] = phase1["gpt4"]
    if "claude" in phase1:
        new_phase1["claude"] = phase1["claude"]
    # Preserve any other phase1 keys
    for k, v in phase1.items():
        if k not in new_phase1:
            new_phase1[k] = v
    reordered["phase1"] = new_phase1

    # Permute each round in phase2_rounds
    rounds = transcript.get("phase2_rounds") or []
    new_rounds = []
    for rnd in rounds:
        if not isinstance(rnd, dict):
            new_rounds.append(rnd)
            continue
        new_rnd: dict = {}
        # Keep round number first for readability
        if "round" in rnd:
            new_rnd["round"] = rnd["round"]
        # GPT-4-first order for critique + revision pairs
        for key in ("gpt4_critique_of_claude", "claude_critique_of_gpt4",
                    "gpt4_revised", "claude_revised"):
            if key in rnd:
                new_rnd[key] = rnd[key]
        # Preserve any other keys at the end
        for k, v in rnd.items():
            if k not in new_rnd:
                new_rnd[k] = v
        new_rounds.append(new_rnd)
    reordered["phase2_rounds"] = new_rounds

    # Preserve any other top-level keys
    for k, v in transcript.items():
        if k not in reordered:
            reordered[k] = v

    return reordered


async def synthesize(
    transcript: dict,
    context: PatientContextPackage,
    deliberation_id: str,
    load_prompt_fn,
    call_claude_fn
) -> DeliberationResult:
    """
    Phase 3: Read full transcript and produce DeliberationResult.
    All five output categories generated in single call for coherence.
    """
    context_summary = (
        f"Patient: {context.patient_name}, {context.age_display()}{context.sex}, "
        f"MRN {context.mrn}. "
        f"Conditions: {', '.join(c['display'] for c in context.active_conditions)}. "
        f"Days since last encounter: {context.days_since_last_encounter}."
    )

    # Bias mitigation: present agents in alternating order based on
    # deliberation_id parity. Original transcript is preserved for audit.
    display_transcript = _reorder_transcript_for_bias_mitigation(
        transcript, deliberation_id
    )

    system_prompt = load_prompt_fn("synthesizer.xml", {
        "FULL_TRANSCRIPT_JSON": json.dumps(display_transcript, indent=2),
        "PATIENT_CONTEXT_SUMMARY": context_summary
    })

    raw = await call_claude_fn(
        "claude-sonnet-4-20250514",
        system_prompt,
        "Produce the complete synthesis now.",
        max_tokens=4096
    )

    result_data = safe_json_loads(raw)
    result_data.update({
        "deliberation_id": deliberation_id,
        "patient_id": context.patient_id,
        "timestamp": datetime.utcnow().isoformat(),
        "trigger": context.deliberation_trigger,
        "transcript": transcript,  # original order preserved for audit
        "models": {
            "analyst_claude": "claude-sonnet-4-20250514",
            "analyst_gpt4": "gpt-4o",
            "synthesizer": "claude-sonnet-4-20250514"
        }
    })

    return DeliberationResult.model_validate(result_data)

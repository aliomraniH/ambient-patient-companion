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
    transcript: dict, deliberation_id: str
) -> dict:
    """Deterministically permute Claude/GPT-4 ordering in the transcript.

    Uses the parity of the SHA-256 hash of deliberation_id to decide ordering:
      Even hash byte → Claude first (default order)
      Odd  hash byte → GPT-4 first (swapped)

    Both ``phase1`` keys and all per-round ``phase2_rounds`` keys are swapped
    so the synthesizer sees a consistent ordering throughout the full transcript.
    The original transcript object is never mutated — a new dict is returned.

    The caller preserves ``result.transcript`` (the *original*, unswapped
    transcript) for the audit trail.
    """
    first_byte = hashlib.sha256(deliberation_id.encode()).digest()[0]
    if first_byte % 2 == 0:
        return transcript  # even → keep default Claude-first order

    # Odd hash → swap Claude and GPT-4 throughout
    reordered: dict = {}

    # Phase 1: swap claude ↔ gpt4
    if "phase1" in transcript:
        p1 = transcript["phase1"]
        reordered["phase1"] = {
            "gpt4":   p1.get("gpt4"),
            "claude": p1.get("claude"),
        }
    else:
        reordered["phase1"] = transcript.get("phase1", {})

    # Phase 2 rounds: swap all four per-round keys
    if "phase2_rounds" in transcript:
        new_rounds = []
        for rnd in transcript["phase2_rounds"]:
            new_rounds.append({
                "round": rnd.get("round"),
                "gpt4_critique_of_claude":   rnd.get("gpt4_critique_of_claude"),
                "claude_critique_of_gpt4":   rnd.get("claude_critique_of_gpt4"),
                "gpt4_revised":              rnd.get("gpt4_revised"),
                "claude_revised":            rnd.get("claude_revised"),
            })
        reordered["phase2_rounds"] = new_rounds
    else:
        reordered["phase2_rounds"] = transcript.get("phase2_rounds", [])

    # Preserve any other top-level keys unchanged
    for k, v in transcript.items():
        if k not in ("phase1", "phase2_rounds"):
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

    # Reorder transcript for the synthesizer's prompt only; original is
    # preserved below in result_data["transcript"] for the audit trail.
    prompt_transcript = _reorder_transcript_for_bias_mitigation(
        transcript, deliberation_id
    )

    system_prompt = load_prompt_fn("synthesizer.xml", {
        "FULL_TRANSCRIPT_JSON": json.dumps(prompt_transcript, indent=2),
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
        "transcript": transcript,  # original, unswapped — audit trail
        "models": {
            "analyst_claude": "claude-sonnet-4-20250514",
            "analyst_gpt4": "gpt-4o",
            "synthesizer": "claude-sonnet-4-20250514"
        }
    })

    return DeliberationResult.model_validate(result_data)

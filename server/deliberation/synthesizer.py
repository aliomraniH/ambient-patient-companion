"""
synthesizer.py — Phase 3: Synthesize full transcript into five structured outputs.
Uses Claude as the synthesizer (rotates in production for bias prevention).
"""
import json
from datetime import datetime
from .schemas import DeliberationResult, PatientContextPackage


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
        f"Patient: {context.patient_name}, {context.age}{context.sex}, "
        f"MRN {context.mrn}. "
        f"Conditions: {', '.join(c['display'] for c in context.active_conditions)}. "
        f"Days since last encounter: {context.days_since_last_encounter}."
    )

    system_prompt = load_prompt_fn("synthesizer.xml", {
        "FULL_TRANSCRIPT_JSON": json.dumps(transcript, indent=2),
        "PATIENT_CONTEXT_SUMMARY": context_summary
    })

    raw = await call_claude_fn(
        "claude-sonnet-4-20250514",
        system_prompt,
        "Produce the complete synthesis now.",
        max_tokens=4096
    )

    result_data = json.loads(raw)
    result_data.update({
        "deliberation_id": deliberation_id,
        "patient_id": context.patient_id,
        "timestamp": datetime.utcnow().isoformat(),
        "trigger": context.deliberation_trigger,
        "transcript": transcript,
        "models": {
            "analyst_claude": "claude-sonnet-4-20250514",
            "analyst_gpt4": "gpt-4o",
            "synthesizer": "claude-sonnet-4-20250514"
        }
    })

    return DeliberationResult.model_validate(result_data)

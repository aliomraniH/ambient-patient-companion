"""
output_safety.py — Deliberation output safety wrapper.

Routes all patient-facing deliberation outputs (nudges, anticipatory
scenarios) through the existing guardrail output validator before they
are committed to the database.

Adapts the OutputValidation interface (safe/flags/safe_response) to a
dict-based interface suitable for deliberation pipeline integration.
"""

from __future__ import annotations

import logging
from typing import Any

from server.guardrails.output_validator import OutputValidation, validate_output

log = logging.getLogger(__name__)

# Content types that are patient-facing and require full guardrail review.
# Blocking flags (PHI_LEAKAGE, DIAGNOSTIC_LANGUAGE) cause rejection.
PATIENT_FACING_TYPES = {
    "patient_nudges",
    "nudge",
    "sms_nudge",
    "push_nudge",
    "portal_message",
}

# Content types that are provider-facing — violations are logged but not blocking.
PROVIDER_FACING_TYPES = {
    "anticipatory_scenarios",
    "predicted_patient_questions",
    "missing_data_flags",
    "care_brief",
    "previsit_brief",
}


def validate_deliberation_output(
    content: str,
    output_type: str,
    patient_id: str,
    deliberation_id: str,
) -> dict[str, Any]:
    """
    Run a deliberation output through the guardrail output validator.

    Returns:
        {
          "passed": bool,
          "content": str,        # sanitized content if passed, empty if blocked
          "violations": list,    # list of flag strings if any
          "action": str,         # "pass" | "sanitize" | "block"
        }
    """
    if not content or not content.strip():
        return {"passed": True, "content": content, "violations": [], "action": "pass"}

    is_patient_facing = output_type in PATIENT_FACING_TYPES

    try:
        result: OutputValidation = validate_output(response=content)
    except Exception as e:
        log.error(
            "output_safety: validator raised for output_type=%s "
            "deliberation_id=%s patient_id=%s: %s",
            output_type, deliberation_id, patient_id, e,
        )
        if is_patient_facing:
            return {
                "passed": False,
                "content": "",
                "violations": [f"Validator exception: {e}"],
                "action": "block",
            }
        return {
            "passed": True,
            "content": content,
            "violations": [f"Validator exception: {e}"],
            "action": "pass",
        }

    if result.safe:
        return {
            "passed": True,
            "content": result.safe_response,
            "violations": result.flags,
            "action": "pass" if not result.flags else "sanitize",
        }

    # Not safe — blocking flag detected (PHI_LEAKAGE or DIAGNOSTIC_LANGUAGE)
    if is_patient_facing:
        log.warning(
            "output_safety: BLOCKED patient-facing output output_type=%s "
            "deliberation_id=%s violations=%s",
            output_type, deliberation_id, result.flags,
        )
        return {
            "passed": False,
            "content": "",
            "violations": result.flags,
            "action": "block",
        }

    # Provider-facing: log violation but pass original content through
    log.warning(
        "output_safety: violation in provider-facing output output_type=%s "
        "deliberation_id=%s violations=%s — passing through",
        output_type, deliberation_id, result.flags,
    )
    return {
        "passed": True,
        "content": content,
        "violations": result.flags,
        "action": "sanitize",
    }


def validate_nudge_batch(
    nudges: list,
    patient_id: str,
    deliberation_id: str,
) -> list:
    """
    Validate a list of NudgeContent objects. Each nudge has a ``channels``
    dict with keys like ``sms``, ``portal``, ``push_notification``.

    Patient-targeted nudges with blocking violations are removed entirely.
    Returns only nudges that passed validation.
    """
    validated = []
    for nudge in nudges:
        if nudge.target != "patient":
            # Care-team nudges are provider-facing — pass through
            validated.append(nudge)
            continue

        blocked = False
        for channel_key in ("sms", "portal"):
            text = nudge.channels.get(channel_key, "")
            if not text:
                continue
            result = validate_deliberation_output(
                content=text,
                output_type="patient_nudges",
                patient_id=patient_id,
                deliberation_id=deliberation_id,
            )
            if not result["passed"]:
                log.warning(
                    "output_safety: nudge %s blocked on channel=%s "
                    "deliberation_id=%s violations=%s",
                    getattr(nudge, "nudge_id", "?"),
                    channel_key,
                    deliberation_id,
                    result["violations"],
                )
                blocked = True
                break

        # Check push_notification body if present
        if not blocked:
            push = nudge.channels.get("push_notification")
            push_body = ""
            if isinstance(push, dict):
                push_body = push.get("body", "")
            elif isinstance(push, str):
                push_body = push
            if push_body:
                result = validate_deliberation_output(
                    content=push_body,
                    output_type="patient_nudges",
                    patient_id=patient_id,
                    deliberation_id=deliberation_id,
                )
                if not result["passed"]:
                    blocked = True

        if not blocked:
            validated.append(nudge)

    return validated


def validate_nudge_dicts(
    nudges: list[dict],
    patient_id: str,
    deliberation_id: str,
) -> list[dict]:
    """
    Validate a list of nudge dicts (used by run_progressive's plain-dict output).
    Nudge dicts may have ``content``, ``message``, ``text``, or ``channels`` keys.

    Returns only nudges that passed validation.
    """
    validated = []
    for nudge in nudges:
        if not isinstance(nudge, dict):
            continue

        # Determine target — default to patient if unspecified
        target = nudge.get("target", "patient")
        if target != "patient":
            validated.append(nudge)
            continue

        # Extract patient-facing text from various possible keys
        texts_to_check = []
        for key in ("content", "message", "text"):
            val = nudge.get(key, "")
            if val:
                texts_to_check.append(val)

        channels = nudge.get("channels")
        if isinstance(channels, dict):
            for ck in ("sms", "portal"):
                val = channels.get(ck, "")
                if val:
                    texts_to_check.append(val)
            push = channels.get("push_notification")
            if isinstance(push, dict) and push.get("body"):
                texts_to_check.append(push["body"])
            elif isinstance(push, str) and push:
                texts_to_check.append(push)

        blocked = False
        for text in texts_to_check:
            result = validate_deliberation_output(
                content=text,
                output_type="patient_nudges",
                patient_id=patient_id,
                deliberation_id=deliberation_id,
            )
            if not result["passed"]:
                blocked = True
                break

        if not blocked:
            validated.append(nudge)

    return validated

"""
behavioral_adapter.py — Phase 4: Format nudge content for delivery.
Validates reading level, applies COM-B model classification,
generates all channel variants (SMS, push, portal).
"""
import re
from .schemas import NudgeContent


SMS_MAX_CHARS = 160
TARGET_READING_GRADE = 6  # Flesch-Kincaid target


def validate_sms_length(text: str) -> str:
    """Truncate SMS to 160 chars, preserving word boundaries."""
    if len(text) <= SMS_MAX_CHARS:
        return text
    truncated = text[:SMS_MAX_CHARS - 3]
    last_space = truncated.rfind(" ")
    return truncated[:last_space] + "..."


def estimate_reading_grade(text: str) -> float:
    """
    Flesch-Kincaid Grade Level approximation.
    FK = 0.39*(words/sentences) + 11.8*(syllables/words) - 15.59
    """
    sentences = max(1, len(re.split(r'[.!?]+', text)))
    words = max(1, len(text.split()))
    # Rough syllable count: vowel groups
    syllables = max(1, len(re.findall(r'[aeiouAEIOU]+', text)))
    return 0.39 * (words / sentences) + 11.8 * (syllables / words) - 15.59


def adapt_nudges(nudges: list[NudgeContent]) -> list[NudgeContent]:
    """
    Phase 4: Validate and format all nudge content.
    - Enforce SMS length limit
    - Validate reading level (warn if > grade 8)
    - Ensure patient nudges end with provider sign-off reminder
    """
    adapted = []
    for nudge in nudges:
        if nudge.target == "patient":
            # Enforce reading level
            portal_text = nudge.channels.get("portal", "")
            grade = estimate_reading_grade(portal_text)
            if grade > 8:
                # Flag for manual review — do not auto-simplify clinical content
                nudge.channels["reading_level_warning"] = (
                    f"Estimated grade {grade:.1f} — exceeds target of 8. Review before sending."
                )

            # Validate SMS length
            if "sms" in nudge.channels:
                nudge.channels["sms"] = validate_sms_length(nudge.channels["sms"])

            # Patient nudges must include provider sign-off
            portal = nudge.channels.get("portal", "")
            if "healthcare provider" not in portal.lower():
                nudge.channels["portal"] = (
                    portal + "\n\nPlease discuss any changes with your healthcare provider."
                )

        adapted.append(nudge)
    return adapted

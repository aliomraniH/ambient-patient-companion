"""Layer 1 — Input validation for the clinical guardrail pipeline.

Checks:
- PHI detection: scan for 18 HIPAA identifiers using regex
- Jailbreak screening: detect role-override and prompt-injection phrases
- Scope check: reject requests outside clinical decision support scope
- Emotional tone flag: detect minimizing/hopeful framing that could bias output
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class InputValidation:
    """Result of input validation.

    Attributes:
        blocked: Whether the input should be blocked from processing.
        reason: Human-readable explanation of why input was blocked (empty if not blocked).
        cleaned_query: The query after any sanitization (same as input if no changes needed).
        flags: Non-blocking warnings (e.g., emotional tone detected).
    """

    blocked: bool
    reason: str
    cleaned_query: str
    flags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PHI detection patterns — 18 HIPAA identifiers
# ---------------------------------------------------------------------------

_PHI_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 1. Social Security Numbers (XXX-XX-XXXX or XXXXXXXXX)
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("SSN_no_dash", re.compile(r"\b\d{9}\b")),
    # 2. Phone numbers (various US formats)
    ("phone", re.compile(
        r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
    )),
    # 3. Email addresses
    ("email", re.compile(
        r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
    )),
    # 4. Dates of birth (explicit patterns like DOB: or born on)
    ("date_of_birth", re.compile(
        r"\b(?:dob|date\s+of\s+birth|born\s+on)[:\s]*\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b",
        re.IGNORECASE,
    )),
    # 5. Medical record numbers (MRN: followed by digits)
    ("medical_record_number", re.compile(
        r"\b(?:mrn|medical\s+record(?:\s+number)?)[:\s#]*\d{4,}\b",
        re.IGNORECASE,
    )),
    # 6. Health plan beneficiary numbers
    ("health_plan_number", re.compile(
        r"\b(?:health\s+plan|insurance|member|beneficiary|policy)\s*(?:id|number|#)[:\s]*[A-Z0-9]{6,}\b",
        re.IGNORECASE,
    )),
    # 7. Account numbers
    ("account_number", re.compile(
        r"\b(?:account|acct)\s*(?:number|#|no)[:\s]*\d{6,}\b",
        re.IGNORECASE,
    )),
    # 8. Certificate/license numbers
    ("license_number", re.compile(
        r"\b(?:license|certificate|DEA)\s*(?:number|#|no)[:\s]*[A-Z0-9]{5,}\b",
        re.IGNORECASE,
    )),
    # 9. Vehicle identifiers (VIN)
    ("vehicle_id", re.compile(
        r"\b(?:VIN|vehicle\s+id(?:entification)?)\s*[:\s]*[A-HJ-NPR-Z0-9]{17}\b",
        re.IGNORECASE,
    )),
    # 10. Device identifiers
    ("device_id", re.compile(
        r"\b(?:device\s+(?:id|identifier|serial))[:\s]*[A-Z0-9\-]{6,}\b",
        re.IGNORECASE,
    )),
    # 11. Web URLs with patient identifiers
    ("url_with_patient_id", re.compile(
        r"https?://\S*(?:patient|record|mrn|chart)\S*",
        re.IGNORECASE,
    )),
    # 12. IP addresses
    ("ip_address", re.compile(
        r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
    )),
    # 13. Biometric identifiers (fingerprint, retinal, etc.)
    ("biometric_id", re.compile(
        r"\b(?:fingerprint|retinal|iris|voiceprint|biometric)\s*(?:id|scan|data|identifier)[:\s]*\S+",
        re.IGNORECASE,
    )),
    # 14. Full face photographs (references to photo files)
    ("photo_id", re.compile(
        r"\b(?:photo|image|picture)\s*(?:id|file)?[:\s]*\S+\.(?:jpg|jpeg|png|bmp|gif)\b",
        re.IGNORECASE,
    )),
    # 15. Geographic subdivisions smaller than state (street addresses)
    ("street_address", re.compile(
        r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}(?:St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Ln|Lane|Rd|Road|Ct|Court|Pl|Place|Way)\b",
    )),
    # 16. ZIP codes (5-digit or 5+4)
    ("zip_code", re.compile(r"\b\d{5}(?:-\d{4})?\b")),
    # 17. Dates (full dates that could identify a person — MM/DD/YYYY)
    ("full_date", re.compile(
        r"\b(?:0?[1-9]|1[0-2])[/\-](?:0?[1-9]|[12]\d|3[01])[/\-](?:19|20)\d{2}\b"
    )),
    # 18. Names combined with identifiers (Name + DOB or Name + MRN pattern)
    ("name_with_identifier", re.compile(
        r"\b(?:patient|name)[:\s]+[A-Z][a-z]+\s+[A-Z][a-z]+\s*(?:,\s*(?:dob|mrn|ssn|born))",
        re.IGNORECASE,
    )),
]


# ---------------------------------------------------------------------------
# Jailbreak / prompt injection patterns
# ---------------------------------------------------------------------------

_JAILBREAK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"pretend\s+(?:you\s+are|to\s+be|you're)", re.IGNORECASE),
    re.compile(r"as\s+a\s+doctor\s+tell\s+me\s+definitively", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:a|an|my)", re.IGNORECASE),
    re.compile(r"forget\s+(?:all\s+)?(?:your|the)\s+(?:rules|instructions|guidelines)", re.IGNORECASE),
    re.compile(r"override\s+(?:your|the|all)\s+(?:safety|rules|guidelines|restrictions)", re.IGNORECASE),
    re.compile(r"act\s+as\s+(?:if\s+)?(?:you\s+(?:are|were)\s+)?(?:a\s+)?(?:real\s+)?doctor", re.IGNORECASE),
    re.compile(r"disregard\s+(?:all\s+)?(?:previous|prior|safety)", re.IGNORECASE),
    re.compile(r"(?:system|developer)\s*(?:prompt|mode|override)", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
    re.compile(r"DAN\s+mode", re.IGNORECASE),
    re.compile(r"do\s+anything\s+now", re.IGNORECASE),
    re.compile(r"bypass\s+(?:your|the|all)\s+(?:safety|filters|restrictions)", re.IGNORECASE),
    re.compile(r"reveal\s+(?:your|the)\s+(?:system|initial)\s+prompt", re.IGNORECASE),
    re.compile(r"what\s+(?:is|are)\s+your\s+(?:system|initial)\s+(?:prompt|instructions)", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Out-of-scope detection
# ---------------------------------------------------------------------------

_OUT_OF_SCOPE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:write|compose|draft)\s+(?:me\s+)?(?:a\s+)?(?:poem|story|essay|song|code)\b", re.IGNORECASE),
    re.compile(r"\b(?:tell\s+me\s+a\s+joke|make\s+me\s+laugh)\b", re.IGNORECASE),
    re.compile(r"\b(?:what\s+(?:is|are)\s+(?:the\s+)?(?:stock|crypto|bitcoin))\b", re.IGNORECASE),
    re.compile(r"\b(?:help\s+me\s+(?:hack|cheat|steal))\b", re.IGNORECASE),
    re.compile(r"\b(?:generate\s+(?:fake|forged)\s+(?:prescription|rx|medical\s+record))\b", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Emotional tone / minimizing language detection
# ---------------------------------------------------------------------------

_MINIMIZING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:it'?s\s+(?:probably\s+)?nothing|i'?m\s+(?:sure|certain)\s+it'?s\s+(?:fine|okay|benign))\b", re.IGNORECASE),
    re.compile(r"\b(?:don'?t\s+worry\s+about|no\s+need\s+to\s+(?:worry|check|test))\b", re.IGNORECASE),
    re.compile(r"\b(?:just\s+tell\s+me\s+(?:it'?s|everything'?s)\s+(?:fine|okay|normal))\b", re.IGNORECASE),
    re.compile(r"\b(?:reassure\s+(?:me|the\s+patient)\s+that)\b", re.IGNORECASE),
]


def validate_input(query: str) -> InputValidation:
    """Validate a clinical query through the Layer 1 guardrail pipeline.

    Args:
        query: The raw query string from the user.

    Returns:
        InputValidation with blocked status, reason, cleaned query, and flags.
    """
    if not query or not query.strip():
        return InputValidation(
            blocked=True,
            reason="Empty query provided.",
            cleaned_query="",
        )

    cleaned = query.strip()

    # --- PHI detection ---
    for identifier_name, pattern in _PHI_PATTERNS:
        if pattern.search(cleaned):
            return InputValidation(
                blocked=True,
                reason=f"Potential PHI detected ({identifier_name}). Remove personally identifiable information before submitting.",
                cleaned_query="",
            )

    # --- Jailbreak screening ---
    for pattern in _JAILBREAK_PATTERNS:
        if pattern.search(cleaned):
            return InputValidation(
                blocked=True,
                reason="Input contains prompt injection or role-override attempt. Request blocked for safety.",
                cleaned_query="",
            )

    # --- Scope check ---
    for pattern in _OUT_OF_SCOPE_PATTERNS:
        if pattern.search(cleaned):
            return InputValidation(
                blocked=True,
                reason="Request is outside the scope of clinical decision support.",
                cleaned_query="",
            )

    # --- Emotional tone flag (non-blocking) ---
    flags: list[str] = []
    for pattern in _MINIMIZING_PATTERNS:
        if pattern.search(cleaned):
            flags.append(
                "EMOTIONAL_TONE: Input contains minimizing or reassurance-seeking "
                "language that could bias toward benign interpretation. Consider "
                "objective clinical framing."
            )
            break

    return InputValidation(
        blocked=False,
        reason="",
        cleaned_query=cleaned,
        flags=flags,
    )

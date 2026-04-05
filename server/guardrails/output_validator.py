"""Layer 3 — Output validation for the clinical guardrail pipeline.

Checks:
- Citation presence: every recommendation must reference a guideline source + version
- PHI leakage scan: ensure no PHI appears in generated output
- Escalation keyword check: flag diagnostic/definitive language
- Drug name verification: verify drug names against retrieved guidelines
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class OutputValidation:
    """Result of output validation.

    Attributes:
        safe: Whether the output passed all validation checks.
        flags: List of issues detected in the output.
        safe_response: The response to return — either the original (if safe)
            or a sanitized/fallback version.
    """

    safe: bool
    flags: list[str] = field(default_factory=list)
    safe_response: str = ""


# ---------------------------------------------------------------------------
# Guideline source patterns for citation verification
# ---------------------------------------------------------------------------

_CITATION_PATTERNS: list[re.Pattern[str]] = [
    # "ADA 2026", "USPSTF 2021", etc.
    re.compile(r"\b(?:ADA|USPSTF|ACC|AHA|KDIGO|JNC)\s+\d{4}\b", re.IGNORECASE),
    # "ADA Standards of Care 2026"
    re.compile(r"\b(?:ADA|USPSTF)\s+\w+(?:\s+\w+){0,4}\s+\d{4}\b", re.IGNORECASE),
    # "(Grade A)", "(Grade B)", etc.
    re.compile(r"\(?\bGrade\s+[A-D]\b\)?", re.IGNORECASE),
    # "Evidence grade: A"
    re.compile(r"\bevidence\s+grade[:\s]+[A-D]\b", re.IGNORECASE),
    # "Section 9.1", "Chapter 10"
    re.compile(r"\b(?:Section|Chapter|Recommendation)\s+[\d.]+\b", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Escalation / diagnostic language patterns
# ---------------------------------------------------------------------------

_DIAGNOSTIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("definitive_diagnosis", re.compile(
        r"\byou\s+have\b", re.IGNORECASE
    )),
    ("diagnostic_confirmation", re.compile(
        r"\bI\s+(?:can\s+)?confirm\b", re.IGNORECASE
    )),
    ("diagnostic_claim", re.compile(
        r"\bI\s+diagnose\b", re.IGNORECASE
    )),
    ("definitive_statement", re.compile(
        r"\b(?:this\s+is\s+(?:definitely|certainly|clearly)|you\s+(?:definitely|certainly)\s+have)\b",
        re.IGNORECASE,
    )),
    ("diagnostic_authority", re.compile(
        r"\b(?:my\s+diagnosis\s+is|the\s+diagnosis\s+is)\b",
        re.IGNORECASE,
    )),
]


# ---------------------------------------------------------------------------
# PHI patterns (reuse from input_validator for output scanning)
# ---------------------------------------------------------------------------

_PHI_OUTPUT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("phone", re.compile(
        r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
    )),
    ("email", re.compile(
        r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
    )),
    ("medical_record_number", re.compile(
        r"\b(?:mrn|medical\s+record)[:\s#]*\d{4,}\b", re.IGNORECASE
    )),
    ("street_address", re.compile(
        r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}(?:St|Street|Ave|Avenue|Blvd|Dr|Drive|Ln|Lane|Rd|Road)\b",
    )),
]


# ---------------------------------------------------------------------------
# Common drug names for verification
# ---------------------------------------------------------------------------

_KNOWN_DRUG_NAMES: set[str] = {
    # Diabetes
    "metformin", "glipizide", "glyburide", "glimepiride",
    "sitagliptin", "saxagliptin", "linagliptin", "alogliptin",
    "empagliflozin", "canagliflozin", "dapagliflozin", "ertugliflozin",
    "liraglutide", "semaglutide", "dulaglutide", "exenatide", "tirzepatide",
    "pioglitazone", "rosiglitazone",
    "insulin", "insulin glargine", "insulin detemir", "insulin degludec",
    "insulin aspart", "insulin lispro", "NPH insulin",
    # Cardiovascular
    "atorvastatin", "rosuvastatin", "simvastatin", "pravastatin",
    "lisinopril", "ramipril", "enalapril", "benazepril",
    "losartan", "valsartan", "irbesartan", "olmesartan",
    "amlodipine", "nifedipine",
    "chlorthalidone", "hydrochlorothiazide", "indapamide",
    "metoprolol", "carvedilol", "atenolol", "bisoprolol",
    "aspirin", "clopidogrel",
    # CKD
    "finerenone", "spironolactone", "eplerenone",
}


_FALLBACK_RESPONSE = (
    "The generated response did not meet safety validation criteria. "
    "Insufficient guideline evidence. Clinician judgment required."
)


def _extract_drug_names(text: str) -> set[str]:
    """Extract potential drug names from text by matching against known drugs.

    Args:
        text: The text to scan for drug names.

    Returns:
        Set of drug names found in the text.
    """
    text_lower = text.lower()
    found: set[str] = set()
    for drug in _KNOWN_DRUG_NAMES:
        if drug.lower() in text_lower:
            found.add(drug)
    return found


def validate_output(
    response: str,
    retrieved_guidelines: list[dict] | None = None,
) -> OutputValidation:
    """Validate a generated clinical response through Layer 3 guardrail pipeline.

    Args:
        response: The generated response text from Claude API.
        retrieved_guidelines: The guidelines that were injected into the prompt.
            Used to verify drug name consistency.

    Returns:
        OutputValidation with safety status, flags, and safe response.
    """
    if not response or not response.strip():
        return OutputValidation(
            safe=False,
            flags=["EMPTY_RESPONSE: Generated response was empty."],
            safe_response=_FALLBACK_RESPONSE,
        )

    flags: list[str] = []

    # --- Citation presence check ---
    has_citation = any(
        pattern.search(response) for pattern in _CITATION_PATTERNS
    )
    if not has_citation:
        flags.append(
            "MISSING_CITATION: Response does not reference a guideline source "
            "and version. All recommendations must cite their evidence base."
        )

    # --- PHI leakage scan ---
    for identifier_name, pattern in _PHI_OUTPUT_PATTERNS:
        if pattern.search(response):
            flags.append(
                f"PHI_LEAKAGE: Potential {identifier_name} detected in generated output."
            )

    # --- Escalation keyword check ---
    for label, pattern in _DIAGNOSTIC_PATTERNS:
        if pattern.search(response):
            flags.append(
                f"DIAGNOSTIC_LANGUAGE ({label}): Response contains definitive "
                f"diagnostic language that must be rewritten as differential "
                f"considerations."
            )

    # --- Drug name verification ---
    if retrieved_guidelines:
        # Collect all medications mentioned in retrieved guidelines
        guideline_drugs: set[str] = set()
        for g in retrieved_guidelines:
            for med in g.get("medications_mentioned", []):
                guideline_drugs.add(med.lower())

        # Find drugs in output
        output_drugs = _extract_drug_names(response)

        # Flag drugs that appear in output but not in retrieved guidelines
        for drug in output_drugs:
            if drug.lower() not in guideline_drugs and drug.lower() not in {"insulin"}:
                flags.append(
                    f"UNGROUNDED_DRUG: '{drug}' mentioned in response but not "
                    f"found in retrieved guidelines. Verify against evidence base."
                )

    # Determine safety
    # PHI leakage and diagnostic language are blocking
    blocking_prefixes = ("PHI_LEAKAGE", "DIAGNOSTIC_LANGUAGE")
    has_blocking = any(
        f.startswith(blocking_prefixes) for f in flags
    )

    if has_blocking:
        return OutputValidation(
            safe=False,
            flags=flags,
            safe_response=_FALLBACK_RESPONSE,
        )

    # Missing citations and ungrounded drugs are warnings but not blocking
    return OutputValidation(
        safe=len(flags) == 0,
        flags=flags,
        safe_response=response,
    )

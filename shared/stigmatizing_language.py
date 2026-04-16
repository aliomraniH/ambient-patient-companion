"""Stigmatizing language annotation for clinical notes.

Annotates (never deletes) stigmatizing terms in clinical text so LLM
consumers see an explicit structural-context marker before interpreting
adherence/behavioral labels. Research basis: Sun et al. Health Affairs
2022 — stigmatizing language appears in EHR notes at 2.54x the rate for
Black patients, and a downstream LLM anchoring on such a label without
first reviewing SDOH context attributes structural barriers to
individual blame.

Design rules:
- Never modify or delete the original term. Only augment.
- Emit one [STIGMATIZING_LANGUAGE: ...] preamble listing all flagged
  terms, then wrap each match inline with [STIGMA_FLAG: '<term>' —
  consider '<alt>'].
- Case-insensitive matching, but replacement preserves the original
  casing inside the flag marker.
- Never touch the database row — only the in-context (in-memory) copy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _Term:
    pattern: str            # regex alternation (word-boundary wrapped at use)
    category: str
    alt: str                # suggested person-first alternative


# Source: Sun et al. Health Affairs 2022 (top-frequency stigmatizing terms in
# clinical notes), NAM 2021 person-first language guidance, APA Stylebook 7th
# ed. on mental-health and substance-use terminology.
STIGMATIZING_TERMS: list[_Term] = [
    _Term(
        pattern=r"non[-\s]?compliant|not\s+compliant",
        category="compliance",
        alt="experiencing barriers to adherence",
    ),
    _Term(
        pattern=r"non[-\s]?adherent|not\s+adherent",
        category="compliance",
        alt="not yet adherent",
    ),
    _Term(
        pattern=r"refused|refuses|refusing",
        category="compliance",
        alt="declined",
    ),
    _Term(
        pattern=r"drug[-\s]?seeking|narcotic[-\s]?dependent",
        category="substance_use",
        alt="person with pain or substance-use concerns",
    ),
    _Term(
        pattern=r"substance\s+abuser?|drug\s+abuser?",
        category="substance_use",
        alt="person with a substance use disorder",
    ),
    _Term(
        pattern=r"addict(?:s|ed)?",
        category="substance_use",
        alt="person with a substance use disorder",
    ),
    _Term(
        pattern=r"difficult\s+patient|frequent\s+flyer",
        category="behavioral",
        alt="patient with complex care needs",
    ),
    _Term(
        pattern=r"agitated|combative|uncooperative",
        category="behavioral",
        alt="experiencing distress",
    ),
    _Term(
        pattern=r"mentally\s+ill",
        category="mental_health",
        alt="living with a mental health condition",
    ),
    _Term(
        pattern=r"homeless",
        category="social",
        alt="experiencing housing instability",
    ),
    _Term(
        pattern=r"failed\s+treatment",
        category="clinical_framing",
        alt="treatment was not effective",
    ),
]


def _iter_matches(text: str) -> list[tuple[re.Match, _Term]]:
    """Return all non-overlapping matches across every term, earliest first."""
    hits: list[tuple[re.Match, _Term]] = []
    for term in STIGMATIZING_TERMS:
        for m in re.finditer(term.pattern, text, flags=re.IGNORECASE):
            hits.append((m, term))
    # Sort by start offset, then by match length (longer first) to prefer
    # more specific matches when two terms would overlap.
    hits.sort(key=lambda pair: (pair[0].start(), -(pair[0].end() - pair[0].start())))
    # Drop overlapping matches: keep the earlier/longer one.
    filtered: list[tuple[re.Match, _Term]] = []
    last_end = -1
    for m, term in hits:
        if m.start() >= last_end:
            filtered.append((m, term))
            last_end = m.end()
    return filtered


def flag_stigmatizing_language(note_text: str) -> str:
    """Annotate stigmatizing terms in-place without deletion.

    Returns the original text augmented with inline [STIGMA_FLAG: ...]
    markers and, if any term matched, a leading [STIGMATIZING_LANGUAGE: ...]
    preamble that instructs the LLM to defer interpretation until SDOH
    context has been reviewed.

    Empty or falsy input returns the input unchanged.
    """
    if not note_text:
        return note_text

    matches = _iter_matches(note_text)
    if not matches:
        return note_text

    # Build annotated text by splicing in markers (reverse order preserves
    # offsets).
    pieces = []
    cursor = 0
    flagged_terms: list[str] = []
    for m, term in matches:
        pieces.append(note_text[cursor:m.start()])
        original = note_text[m.start():m.end()]
        flagged_terms.append(original)
        pieces.append(
            f"{original} [STIGMA_FLAG: '{original}' — consider '{term.alt}']"
        )
        cursor = m.end()
    pieces.append(note_text[cursor:])

    body = "".join(pieces)
    preamble = (
        "[STIGMATIZING_LANGUAGE: This note contains adherence or behavioral "
        "labels that may encode bias. Defer interpretation until SDOH context "
        f"has been reviewed. Flagged terms: {flagged_terms}]\n\n"
    )
    return preamble + body

"""
shared/stigmatizing_language.py

Annotate (never delete) stigmatizing language in clinical notes before they
enter deliberation.  LLMs see structural-context markers rather than
absorbing implicit bias from adherence labels.

Design principles
-----------------
* ADDITIVE ONLY — original text is never modified or removed.
* Case-insensitive regex matching.
* Each match receives an inline [STIGMA_FLAG: category | alt] marker
  appended immediately after the matched term.
* Safe to call on empty strings or strings with no stigmatizing terms.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class _StigmaTerm(NamedTuple):
    pattern: str
    category: str
    alt: str


_TERMS: list[_StigmaTerm] = [
    _StigmaTerm(
        pattern=r"non[\s-]?compliant|not\s+compliant",
        category="compliance",
        alt="experiencing barriers to adherence",
    ),
    _StigmaTerm(
        pattern=r"non[\s-]?adherent|not\s+adherent",
        category="compliance",
        alt="not yet adherent",
    ),
    _StigmaTerm(
        pattern=r"\brefus(?:ed|es|ing)\b",
        category="compliance",
        alt="declined",
    ),
    _StigmaTerm(
        pattern=r"drug[\s-]?seeking|narcotic[\s-]?dependent",
        category="substance_use",
        alt="person with substance use concerns",
    ),
    _StigmaTerm(
        pattern=r"difficult\s+patient|frequent\s+flyer",
        category="behavioral",
        alt="patient with complex care needs",
    ),
    _StigmaTerm(
        pattern=r"\bagitat(?:ed|ing)\b|\bcombative\b|\buncooperative\b",
        category="behavioral",
        alt="experiencing distress",
    ),
]

# Pre-compile all patterns once at import time for performance.
_COMPILED: list[tuple[re.Pattern[str], _StigmaTerm]] = [
    (re.compile(t.pattern, re.IGNORECASE), t) for t in _TERMS
]


def flag_stigmatizing_language(note_text: str) -> str:
    """Annotate stigmatizing terms inline.  Never deletes original text.

    Returns the annotated text with ``[STIGMA_FLAG: <category> | alt:
    <alt_phrasing>]`` markers immediately after each matched term.

    Args:
        note_text: Raw clinical note string.  May be empty.

    Returns:
        Annotated string.  Identical to input when no terms are found.
    """
    if not note_text:
        return note_text

    result = note_text
    # Apply substitutions in a single pass per pattern to avoid cascading
    # matches from previously inserted markers.
    for compiled, term in _COMPILED:
        result = compiled.sub(
            lambda m, t=term: (
                f"{m.group(0)}"
                f" [STIGMA_FLAG: {t.category} | alt: {t.alt}]"
            ),
            result,
        )
    return result

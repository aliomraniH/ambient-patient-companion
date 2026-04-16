"""Tests for shared.stigmatizing_language.flag_stigmatizing_language.

Verifies:
- Stigmatizing terms are annotated with [STIGMA_FLAG: ...] markers
- Original terms are never deleted (annotation is additive)
- Multiple terms produce multiple annotations + one preamble
- Clean text passes through unchanged
- Matching is case-insensitive
- Empty / None / whitespace inputs do not raise
"""
from shared.stigmatizing_language import flag_stigmatizing_language


def test_noncompliant_flagged():
    note = "Patient was non-compliant with medications."
    out = flag_stigmatizing_language(note)
    assert "[STIGMATIZING_LANGUAGE:" in out
    assert "[STIGMA_FLAG:" in out
    # Original phrase must still appear in the annotated output
    assert "non-compliant" in out


def test_refused_flagged():
    note = "Patient refused insulin adjustment at last visit."
    out = flag_stigmatizing_language(note)
    assert "[STIGMA_FLAG:" in out
    assert "refused" in out


def test_original_term_preserved():
    note = "Patient was non-adherent to the plan."
    out = flag_stigmatizing_language(note)
    # The original term must still be present (substring before the marker)
    assert "non-adherent" in out
    # And the marker must wrap or follow it
    assert "[STIGMA_FLAG:" in out


def test_clean_text_unchanged():
    note = "Patient reported improvement in energy and sleep quality."
    out = flag_stigmatizing_language(note)
    assert out == note  # no annotation, identical string
    assert "[STIGMA_FLAG" not in out


def test_case_insensitive():
    for variant in ("Non-Compliant", "NON-COMPLIANT", "non-Compliant"):
        note = f"Patient marked as {variant} today."
        out = flag_stigmatizing_language(note)
        assert "[STIGMA_FLAG:" in out, f"failed to flag variant: {variant}"


def test_multiple_terms_produce_multiple_flags():
    note = (
        "Patient is non-compliant and refused the referral. "
        "History notes drug-seeking behavior."
    )
    out = flag_stigmatizing_language(note)
    # Expect 3 inline flags
    assert out.count("[STIGMA_FLAG:") == 3
    # Expect exactly one preamble
    assert out.count("[STIGMATIZING_LANGUAGE:") == 1


def test_empty_input_passthrough():
    assert flag_stigmatizing_language("") == ""


def test_none_input_passthrough():
    assert flag_stigmatizing_language(None) is None


def test_whitespace_only_unchanged():
    out = flag_stigmatizing_language("   \n\t  ")
    assert out == "   \n\t  "


def test_preamble_lists_flagged_terms():
    note = "Patient is homeless and mentally ill."
    out = flag_stigmatizing_language(note)
    preamble = out.split("]\n\n", 1)[0]
    assert "homeless" in preamble
    assert "mentally ill" in preamble


def test_alternative_suggestion_appears_in_marker():
    note = "Patient is non-compliant."
    out = flag_stigmatizing_language(note)
    # The suggested alternative text should appear inline in the flag
    assert "experiencing barriers to adherence" in out


def test_substance_use_terms_flagged():
    note = "History of drug-seeking behavior noted."
    out = flag_stigmatizing_language(note)
    assert "[STIGMA_FLAG:" in out
    assert "substance-use concerns" in out or "substance use concerns" in out


def test_overlapping_terms_resolved():
    # "non compliant" and "not compliant" overlap in some phrasings —
    # the annotator must produce well-formed output (no crashes, no doubled
    # wraps on the same substring).
    note = "Patient was non compliant and non-adherent."
    out = flag_stigmatizing_language(note)
    # Each distinct term should get exactly one flag marker
    assert out.count("[STIGMA_FLAG:") == 2

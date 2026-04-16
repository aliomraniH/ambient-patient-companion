"""Phase 2 — Stigmatizing language annotation tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.stigmatizing_language import flag_stigmatizing_language


def test_noncompliant_flagged():
    """[STIGMA_FLAG: should appear for 'non-compliant'."""
    result = flag_stigmatizing_language("Patient is non-compliant with medication.")
    assert "[STIGMA_FLAG:" in result


def test_noncompliant_flag_has_category():
    """Flag should identify compliance category."""
    result = flag_stigmatizing_language("Patient is non-compliant with medication.")
    assert "compliance" in result


def test_original_term_preserved():
    """Original text is never deleted — must still be present."""
    original = "Patient is non-compliant with insulin."
    result = flag_stigmatizing_language(original)
    assert "non-compliant" in result
    assert "insulin" in result


def test_clean_text_unchanged():
    """Clean notes with no stigmatizing terms pass through unmodified."""
    clean = "Patient is working on improving their medication routine."
    result = flag_stigmatizing_language(clean)
    assert result == clean
    assert "[STIGMA_FLAG:" not in result


def test_case_insensitive_title_case():
    """'Non-Compliant' (title case) should be flagged."""
    result = flag_stigmatizing_language("Note: Non-Compliant behavior observed.")
    assert "[STIGMA_FLAG:" in result


def test_case_insensitive_upper():
    """'NON-COMPLIANT' (all caps) should be flagged."""
    result = flag_stigmatizing_language("NON-COMPLIANT with treatment plan.")
    assert "[STIGMA_FLAG:" in result


def test_drug_seeking_flagged():
    """'drug-seeking' should be annotated as substance_use."""
    result = flag_stigmatizing_language("Patient appears drug-seeking.")
    assert "[STIGMA_FLAG:" in result
    assert "substance_use" in result


def test_frequent_flyer_flagged():
    """'frequent flyer' should be annotated as behavioral."""
    result = flag_stigmatizing_language("This frequent flyer was seen again.")
    assert "[STIGMA_FLAG:" in result
    assert "behavioral" in result


def test_empty_string_safe():
    """Empty string input returns empty string without error."""
    assert flag_stigmatizing_language("") == ""


def test_refused_flagged():
    """'refused' should be annotated."""
    result = flag_stigmatizing_language("Patient refused the procedure.")
    assert "[STIGMA_FLAG:" in result
    assert "compliance" in result


def test_non_adherent_flagged():
    """'non-adherent' should be annotated."""
    result = flag_stigmatizing_language("Patient is non-adherent to statin therapy.")
    assert "[STIGMA_FLAG:" in result


def test_combative_flagged():
    """'combative' should be annotated as behavioral."""
    result = flag_stigmatizing_language("Patient became combative during exam.")
    assert "[STIGMA_FLAG:" in result
    assert "behavioral" in result

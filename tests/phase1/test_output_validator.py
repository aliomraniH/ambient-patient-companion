"""Integration tests for Layer 3 — Output validation."""

import pytest

from server.guardrails.output_validator import OutputValidation, validate_output


class TestCitationCheck:
    """Every recommendation must reference a guideline source + version."""

    def test_valid_citation_passes(self) -> None:
        response = (
            "Based on ADA 2026 guidelines (Grade A), metformin is the preferred "
            "initial pharmacologic agent for type 2 diabetes."
        )
        result = validate_output(response)
        assert result.safe is True
        assert not any("MISSING_CITATION" in f for f in result.flags)

    def test_missing_citation_flagged(self) -> None:
        response = (
            "You should start taking metformin for your diabetes. "
            "It is the preferred first-line agent."
        )
        result = validate_output(response)
        assert any("MISSING_CITATION" in f for f in result.flags)

    def test_section_reference_counts_as_citation(self) -> None:
        response = (
            "Per Section 9.1, metformin is recommended. "
            "Evidence grade: A. ADA 2026."
        )
        result = validate_output(response)
        assert not any("MISSING_CITATION" in f for f in result.flags)


class TestPHILeakage:
    """Output must not contain PHI."""

    def test_ssn_in_output_flagged(self) -> None:
        response = "Patient SSN 123-45-6789 shows elevated glucose. ADA 2026 Grade A."
        result = validate_output(response)
        assert result.safe is False
        assert any("PHI_LEAKAGE" in f for f in result.flags)

    def test_email_in_output_flagged(self) -> None:
        response = "Send results to john@hospital.com. ADA 2026 Grade A."
        result = validate_output(response)
        assert result.safe is False
        assert any("PHI_LEAKAGE" in f for f in result.flags)

    def test_clean_output_no_phi(self) -> None:
        response = "Based on ADA 2026 (Grade A), initiate SGLT2 inhibitor therapy."
        result = validate_output(response)
        assert not any("PHI_LEAKAGE" in f for f in result.flags)


class TestDiagnosticLanguage:
    """Definitive diagnostic language must be flagged and blocked."""

    def test_you_have_flagged(self) -> None:
        response = "You have type 2 diabetes. ADA 2026 Grade A."
        result = validate_output(response)
        assert result.safe is False
        assert any("DIAGNOSTIC_LANGUAGE" in f for f in result.flags)

    def test_i_confirm_flagged(self) -> None:
        response = "I can confirm this is diabetic nephropathy. ADA 2026 Grade A."
        result = validate_output(response)
        assert result.safe is False
        assert any("DIAGNOSTIC_LANGUAGE" in f for f in result.flags)

    def test_i_diagnose_flagged(self) -> None:
        response = "I diagnose this as hypertension. ADA 2026 Grade A."
        result = validate_output(response)
        assert result.safe is False

    def test_differential_language_ok(self) -> None:
        response = (
            "Clinical findings suggest consideration of diabetic kidney disease "
            "as a differential. ADA 2026 (Grade B) recommends annual UACR monitoring."
        )
        result = validate_output(response)
        assert not any("DIAGNOSTIC_LANGUAGE" in f for f in result.flags)


class TestDrugNameVerification:
    """Drug names in output should be grounded in retrieved guidelines."""

    def test_grounded_drug_passes(self) -> None:
        guidelines = [
            {"medications_mentioned": ["metformin", "empagliflozin"]},
        ]
        response = "Consider metformin per ADA 2026 (Grade A). Verify dosing with pharmacist."
        result = validate_output(response, guidelines)
        assert not any("UNGROUNDED_DRUG" in f for f in result.flags)

    def test_ungrounded_drug_flagged(self) -> None:
        guidelines = [
            {"medications_mentioned": ["metformin"]},
        ]
        response = "Consider adding pioglitazone per ADA 2026 (Grade A)."
        result = validate_output(response, guidelines)
        assert any("UNGROUNDED_DRUG" in f for f in result.flags)

    def test_no_guidelines_no_drug_check(self) -> None:
        response = "Consider metformin per ADA 2026 (Grade A)."
        result = validate_output(response, None)
        # Should not flag drugs when no guidelines provided for verification
        assert not any("UNGROUNDED_DRUG" in f for f in result.flags)


class TestEdgeCases:
    """Edge cases for output validation."""

    def test_empty_response_blocked(self) -> None:
        result = validate_output("")
        assert result.safe is False
        assert "EMPTY_RESPONSE" in result.flags[0]

    def test_fallback_response_used_when_blocked(self) -> None:
        response = "You have diabetes. I confirm the diagnosis. ADA 2026 Grade A."
        result = validate_output(response)
        assert result.safe is False
        assert "Clinician judgment required" in result.safe_response

    def test_safe_response_is_original_when_valid(self) -> None:
        response = "Per ADA 2026 (Grade A), metformin is first-line. Verify dosing with pharmacist."
        result = validate_output(response)
        assert result.safe_response == response

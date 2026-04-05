"""Integration tests for the get_synthetic_patient FastMCP tool."""

import pytest

from server.mcp_server import get_synthetic_patient



class TestGetSyntheticPatient:
    """Tests for synthetic patient data retrieval."""

    @pytest.mark.asyncio
    async def test_maria_chen_happy_path(self) -> None:
        """Maria Chen (MRN 4829341) returns correct demographic data."""
        result = await get_synthetic_patient(mrn="4829341")
        assert "error" not in result
        assert result["mrn"] == "4829341"
        assert result["first_name"] == "Maria"
        assert result["last_name"] == "Chen"
        assert result["age"] == 54
        assert result["sex"] == "female"

    @pytest.mark.asyncio
    async def test_maria_chen_provider(self) -> None:
        """Maria Chen's provider is Dr. Rahul Patel at Patel Family Medicine."""
        result = await get_synthetic_patient(mrn="4829341")
        provider = result["primary_care_provider"]
        assert "Patel" in provider["name"]
        assert "Patel Family Medicine" in provider["practice"]

    @pytest.mark.asyncio
    async def test_maria_chen_conditions(self) -> None:
        """Maria Chen has conditions for demonstrating diabetes, CVD, and care gaps."""
        result = await get_synthetic_patient(mrn="4829341")
        condition_codes = [c["code"] for c in result["conditions"]]
        # Type 2 diabetes
        assert "E11.9" in condition_codes
        # Hypertension
        assert "I10" in condition_codes
        # Hyperlipidemia
        assert "E78.5" in condition_codes

    @pytest.mark.asyncio
    async def test_maria_chen_medications(self) -> None:
        """Maria Chen is on metformin, lisinopril, and atorvastatin."""
        result = await get_synthetic_patient(mrn="4829341")
        med_names = [m["name"] for m in result["medications"]]
        assert "metformin" in med_names
        assert "lisinopril" in med_names
        assert "atorvastatin" in med_names

    @pytest.mark.asyncio
    async def test_maria_chen_labs(self) -> None:
        """Maria Chen has realistic lab values for a diabetes patient."""
        result = await get_synthetic_patient(mrn="4829341")
        labs = result["labs"]
        assert labs["hba1c"]["value"] == 7.8
        assert labs["egfr"]["value"] == 62
        assert labs["ldl"]["value"] == 128
        assert labs["creatinine"]["value"] == 1.2

    @pytest.mark.asyncio
    async def test_maria_chen_care_gaps(self) -> None:
        """Maria Chen has open care gaps for colonoscopy and depression screening."""
        result = await get_synthetic_patient(mrn="4829341")
        gap_descriptions = [g["description"] for g in result["care_gaps"]]
        assert any("colorectal" in d.lower() or "colonoscopy" in d.lower() for d in gap_descriptions)
        assert any("depression" in d.lower() for d in gap_descriptions)

    @pytest.mark.asyncio
    async def test_maria_chen_sdoh_flags(self) -> None:
        """Maria Chen has SDoH flags for food access."""
        result = await get_synthetic_patient(mrn="4829341")
        assert len(result["sdoh_flags"]) > 0
        domains = [f["domain"] for f in result["sdoh_flags"]]
        assert "food_access" in domains

    @pytest.mark.asyncio
    async def test_unknown_mrn_returns_error(self) -> None:
        """Unknown MRN returns error with hint."""
        result = await get_synthetic_patient(mrn="9999999")
        assert "error" in result
        assert "hint" in result
        assert "4829341" in result["hint"]

    @pytest.mark.asyncio
    async def test_all_required_sections_present(self) -> None:
        """Maria Chen record has all required data sections."""
        result = await get_synthetic_patient(mrn="4829341")
        required_sections = [
            "mrn", "first_name", "last_name", "date_of_birth", "age",
            "sex", "conditions", "medications", "labs", "vitals",
            "care_gaps", "sdoh_flags", "family_history", "social_history",
            "allergies", "primary_care_provider",
        ]
        for section in required_sections:
            assert section in result, f"Missing section: {section}"

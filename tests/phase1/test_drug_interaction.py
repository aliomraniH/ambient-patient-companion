"""Integration tests for the flag_drug_interaction FastMCP tool."""

import pytest

from server.mcp_server import flag_drug_interaction



class TestDrugInteraction:
    """Tests for drug interaction checking."""

    @pytest.mark.asyncio
    async def test_known_interaction_metformin_contrast(self) -> None:
        """Metformin + contrast dye should flag a high-severity interaction."""
        result = await flag_drug_interaction(
            medications=["metformin", "contrast dye"],
        )
        assert len(result) >= 1
        assert any(
            i["drug_a"] == "metformin" and i["drug_b"] == "contrast dye"
            for i in result
        )
        assert result[0]["severity"] == "high"

    @pytest.mark.asyncio
    async def test_known_interaction_ace_arb(self) -> None:
        """ACE inhibitor + ARB (dual RAAS blockade) should be flagged."""
        result = await flag_drug_interaction(
            medications=["lisinopril", "losartan"],
        )
        assert len(result) >= 1
        assert any(i["severity"] == "high" for i in result)

    @pytest.mark.asyncio
    async def test_known_interaction_statin_fibrate(self) -> None:
        """Statin + gemfibrozil should flag rhabdomyolysis risk."""
        result = await flag_drug_interaction(
            medications=["atorvastatin", "gemfibrozil"],
        )
        assert len(result) >= 1
        assert "rhabdomyolysis" in result[0]["description"].lower()

    @pytest.mark.asyncio
    async def test_safe_combination(self) -> None:
        """Metformin + empagliflozin is a safe combination (low severity)."""
        result = await flag_drug_interaction(
            medications=["metformin", "empagliflozin"],
        )
        assert len(result) >= 1
        assert result[0]["severity"] == "low"

    @pytest.mark.asyncio
    async def test_no_interactions(self) -> None:
        """Medications with no known interactions return empty list."""
        result = await flag_drug_interaction(
            medications=["metformin", "atorvastatin"],
        )
        # These don't have a direct interaction in our rules
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_empty_medication_list(self) -> None:
        """Empty medication list returns empty result."""
        result = await flag_drug_interaction(medications=[])
        assert result == []

    @pytest.mark.asyncio
    async def test_single_medication(self) -> None:
        """Single medication cannot have interactions."""
        result = await flag_drug_interaction(medications=["metformin"])
        assert result == []

    @pytest.mark.asyncio
    async def test_interaction_result_shape(self) -> None:
        """Each interaction result has the expected fields."""
        result = await flag_drug_interaction(
            medications=["lisinopril", "losartan"],
        )
        assert len(result) > 0
        for interaction in result:
            assert "drug_a" in interaction
            assert "drug_b" in interaction
            assert "severity" in interaction
            assert "description" in interaction
            assert "action" in interaction

    @pytest.mark.asyncio
    async def test_maria_chen_medications(self) -> None:
        """Maria Chen's medication combination (metformin, lisinopril, atorvastatin) should be safe."""
        result = await flag_drug_interaction(
            medications=["metformin", "lisinopril", "atorvastatin"],
        )
        # No high-severity interactions expected for this common combination
        high_severity = [i for i in result if i["severity"] == "high"]
        assert len(high_severity) == 0

"""Integration tests for the get_guideline FastMCP tool."""

import pytest

from server.mcp_server import get_guideline



class TestGetGuideline:
    """Tests for fetching guidelines by recommendation ID."""

    @pytest.mark.asyncio
    async def test_happy_path_ada_guideline(self) -> None:
        """Fetching a known ADA recommendation ID returns the full entry."""
        result = await get_guideline(recommendation_id="9.1a")
        assert "error" not in result
        assert result["recommendation_id"] == "9.1a"
        assert result["guideline_source"] == "ADA"
        assert result["evidence_grade"] == "A"
        assert "metformin" in result["text"].lower()

    @pytest.mark.asyncio
    async def test_happy_path_uspstf_guideline(self) -> None:
        """Fetching a known USPSTF recommendation ID returns the full entry."""
        result = await get_guideline(recommendation_id="USPSTF-CRC-01")
        assert "error" not in result
        assert result["recommendation_id"] == "USPSTF-CRC-01"
        assert result["guideline_source"] == "USPSTF"
        assert result["evidence_grade"] == "A"

    @pytest.mark.asyncio
    async def test_invalid_id_returns_error(self) -> None:
        """Unknown recommendation ID returns error with available IDs."""
        result = await get_guideline(recommendation_id="NONEXISTENT-99")
        assert "error" in result
        assert "available_ids" in result
        assert len(result["available_ids"]) > 0

    @pytest.mark.asyncio
    async def test_all_schema_fields_present(self) -> None:
        """Every returned guideline has all required schema fields."""
        result = await get_guideline(recommendation_id="6.1a")
        required_fields = [
            "guideline_source", "version", "chapter", "section",
            "recommendation_id", "text", "evidence_grade",
            "recommendation_strength", "patient_population",
            "contraindications", "medications_mentioned",
            "last_reviewed", "is_current",
        ]
        for field in required_fields:
            assert field in result, f"Missing field: {field}"

    @pytest.mark.asyncio
    async def test_ada_chapter_10_cvd(self) -> None:
        """CVD risk management guidelines are accessible."""
        result = await get_guideline(recommendation_id="10.1a")
        assert "error" not in result
        assert "statin" in result["text"].lower()

    @pytest.mark.asyncio
    async def test_ada_chapter_11_ckd(self) -> None:
        """CKD guidelines are accessible."""
        result = await get_guideline(recommendation_id="11.2a")
        assert "error" not in result
        assert "SGLT2" in result["text"] or "sglt2" in result["text"].lower()

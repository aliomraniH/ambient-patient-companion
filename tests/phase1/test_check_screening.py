"""Integration tests for the check_screening_due FastMCP tool."""

import pytest

from server.mcp_server import check_screening_due



class TestCheckScreeningDue:
    """Tests for USPSTF screening eligibility checks."""

    @pytest.mark.asyncio
    async def test_happy_path_54f_diabetes(self) -> None:
        """54F with diabetes and obesity should get multiple screenings."""
        result = await check_screening_due(
            patient_age=54,
            sex="female",
            conditions=["type_2_diabetes", "obesity"],
        )
        assert isinstance(result, list)
        assert len(result) >= 3  # At minimum: CRC, HTN, depression, diabetes, breast, cervical

        screening_names = [s["screening_name"] for s in result]
        assert "Colorectal Cancer" in screening_names
        assert "Hypertension" in screening_names

    @pytest.mark.asyncio
    async def test_female_gets_breast_and_cervical(self) -> None:
        """Female patients in the right age range should get sex-specific screenings."""
        result = await check_screening_due(
            patient_age=50,
            sex="female",
            conditions=[],
        )
        screening_names = [s["screening_name"] for s in result]
        assert "Breast Cancer" in screening_names
        assert "Cervical Cancer" in screening_names

    @pytest.mark.asyncio
    async def test_male_no_cervical_or_breast(self) -> None:
        """Male patients should not get cervical or breast cancer screenings."""
        result = await check_screening_due(
            patient_age=50,
            sex="male",
            conditions=["obesity"],
        )
        screening_names = [s["screening_name"] for s in result]
        assert "Cervical Cancer" not in screening_names
        assert "Breast Cancer" not in screening_names

    @pytest.mark.asyncio
    async def test_age_below_range_fewer_screenings(self) -> None:
        """Young adult (20) should only get HTN and depression screening."""
        result = await check_screening_due(
            patient_age=20,
            sex="male",
            conditions=[],
        )
        screening_names = [s["screening_name"] for s in result]
        assert "Hypertension" in screening_names
        assert "Colorectal Cancer" not in screening_names

    @pytest.mark.asyncio
    async def test_smoking_history_lung_cancer(self) -> None:
        """Patient with smoking history in age range should get lung cancer screening."""
        result = await check_screening_due(
            patient_age=55,
            sex="male",
            conditions=["smoking_20_pack_years"],
        )
        screening_names = [s["screening_name"] for s in result]
        assert "Lung Cancer" in screening_names

    @pytest.mark.asyncio
    async def test_no_smoking_no_lung_screening(self) -> None:
        """Patient without smoking history should not get lung cancer screening."""
        result = await check_screening_due(
            patient_age=55,
            sex="male",
            conditions=["type_2_diabetes"],
        )
        screening_names = [s["screening_name"] for s in result]
        assert "Lung Cancer" not in screening_names

    @pytest.mark.asyncio
    async def test_diabetes_screening_requires_overweight(self) -> None:
        """Diabetes screening requires overweight/obesity condition."""
        result_with_obesity = await check_screening_due(
            patient_age=50,
            sex="male",
            conditions=["obesity"],
        )
        result_without = await check_screening_due(
            patient_age=50,
            sex="male",
            conditions=[],
        )
        names_with = [s["screening_name"] for s in result_with_obesity]
        names_without = [s["screening_name"] for s in result_without]
        assert "Prediabetes and Type 2 Diabetes" in names_with
        assert "Prediabetes and Type 2 Diabetes" not in names_without

    @pytest.mark.asyncio
    async def test_result_shape(self) -> None:
        """Each screening result has the expected fields."""
        result = await check_screening_due(
            patient_age=54,
            sex="female",
            conditions=["obesity"],
        )
        assert len(result) > 0
        for screening in result:
            assert "screening_name" in screening
            assert "recommendation_id" in screening
            assert "uspstf_grade" in screening
            assert "recommendation_text" in screening
            assert "guideline_source" in screening
            assert "version" in screening

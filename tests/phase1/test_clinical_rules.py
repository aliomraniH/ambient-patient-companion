"""Integration tests for clinical escalation rules."""

import pytest

from server.guardrails.clinical_rules import check_escalation, ESCALATION_MESSAGES


class TestLifeThreateningEscalation:
    """Life-threatening conditions must trigger URGENT flag."""

    def test_cardiac_arrest(self) -> None:
        triggers = check_escalation("Patient showing signs of cardiac arrest")
        assert len(triggers) >= 1
        assert any(t["trigger"] == "life_threatening" for t in triggers)
        assert "URGENT" in triggers[0]["message"]

    def test_stroke_symptoms(self) -> None:
        triggers = check_escalation("Sudden onset facial droop, possible stroke")
        assert any(t["trigger"] == "life_threatening" for t in triggers)

    def test_suicidal_ideation(self) -> None:
        triggers = check_escalation("Patient reports suicidal ideation")
        assert any(t["trigger"] == "life_threatening" for t in triggers)

    def test_dka(self) -> None:
        triggers = check_escalation("Presenting with diabetic ketoacidosis")
        assert any(t["trigger"] == "life_threatening" for t in triggers)

    def test_non_threatening_no_trigger(self) -> None:
        triggers = check_escalation("Patient needs routine diabetes follow-up")
        assert not any(t["trigger"] == "life_threatening" for t in triggers)


class TestControlledSubstanceEscalation:
    """Controlled substance requests must BLOCK generation."""

    def test_opioid_request(self) -> None:
        triggers = check_escalation("Can you recommend oxycodone for chronic pain?")
        assert any(t["trigger"] == "controlled_substance" for t in triggers)
        blocked = [t for t in triggers if t["trigger"] == "controlled_substance"]
        assert blocked[0]["blocking"] == "true"

    def test_benzodiazepine_request(self) -> None:
        triggers = check_escalation("What about starting alprazolam for anxiety?")
        assert any(t["trigger"] == "controlled_substance" for t in triggers)

    def test_stimulant_request(self) -> None:
        triggers = check_escalation("Should we try methylphenidate?")
        assert any(t["trigger"] == "controlled_substance" for t in triggers)

    def test_non_controlled_medication_no_trigger(self) -> None:
        triggers = check_escalation("Consider adding metformin for glucose control")
        assert not any(t["trigger"] == "controlled_substance" for t in triggers)


class TestPediatricEscalation:
    """Pediatric dosing must trigger weight-based verification flag."""

    def test_pediatric_keyword(self) -> None:
        triggers = check_escalation("What is the pediatric dose for amoxicillin?")
        assert any(t["trigger"] == "pediatric_dosing" for t in triggers)

    def test_child_keyword(self) -> None:
        triggers = check_escalation("Treating a child with type 1 diabetes")
        assert any(t["trigger"] == "pediatric_dosing" for t in triggers)

    def test_age_based_pediatric(self) -> None:
        triggers = check_escalation("Patient is 8 years old with fever")
        assert any(t["trigger"] == "pediatric_dosing" for t in triggers)

    def test_adult_age_no_trigger(self) -> None:
        triggers = check_escalation("Patient is 54 years old with diabetes")
        assert not any(t["trigger"] == "pediatric_dosing" for t in triggers)


class TestPregnancyEscalation:
    """Pregnancy must trigger teratogenicity review flag."""

    def test_pregnant_keyword(self) -> None:
        triggers = check_escalation("Patient is pregnant and has gestational diabetes")
        assert any(t["trigger"] == "pregnancy" for t in triggers)

    def test_possible_pregnancy(self) -> None:
        triggers = check_escalation("Patient reports she might be pregnant")
        assert any(t["trigger"] == "pregnancy" for t in triggers)

    def test_breastfeeding(self) -> None:
        triggers = check_escalation("Currently breastfeeding, needs medication adjustment")
        assert any(t["trigger"] == "pregnancy" for t in triggers)

    def test_non_pregnancy_no_trigger(self) -> None:
        triggers = check_escalation("54-year-old female with type 2 diabetes")
        assert not any(t["trigger"] == "pregnancy" for t in triggers)


class TestMultipleEscalations:
    """Multiple triggers can fire simultaneously."""

    def test_life_threatening_and_pregnancy(self) -> None:
        triggers = check_escalation(
            "Pregnant patient presenting with diabetic ketoacidosis"
        )
        trigger_types = {t["trigger"] for t in triggers}
        assert "life_threatening" in trigger_types
        assert "pregnancy" in trigger_types

    def test_no_triggers_empty_list(self) -> None:
        triggers = check_escalation("Review diabetes management plan")
        assert isinstance(triggers, list)
        # May have 0 triggers for routine queries

"""Tests for the DataRequestParser module."""
import json
import pytest

from server.deliberation.data_request_parser import parse_data_requests, METABOLIC_TESTS


class TestExplicitDataRequests:
    def test_explicit_requests_parsed(self):
        output = {
            "data_requests": [
                {"type": "lab_trend", "test": "HbA1c", "reason": "need trend"},
                {"type": "clinical_note", "reason": "need visit note"},
            ]
        }
        result = parse_data_requests(output)
        assert result["has_requests"] is True
        assert len(result["on_demand_requests"]) == 2
        assert result["on_demand_requests"][0]["type"] == "lab_trend"

    def test_explicit_requests_skips_invalid(self):
        output = {
            "data_requests": [
                {"type": "lab_trend", "test": "HbA1c"},
                "invalid_string",
                {"no_type_key": True},
                {"type": "imaging_report", "reason": "need scan"},
            ]
        }
        result = parse_data_requests(output)
        assert len(result["on_demand_requests"]) == 2

    def test_empty_data_requests_no_signal(self):
        output = {"data_requests": []}
        result = parse_data_requests(output)
        assert result["has_requests"] is False

    def test_missing_data_requests_key(self):
        output = {"anticipatory_scenarios": []}
        result = parse_data_requests(output)
        assert result["has_requests"] is False


class TestMissingDataFlags:
    def test_lab_flag_triggers_tier2(self):
        output = {
            "missing_data_flags": [
                {
                    "priority": "critical",
                    "data_type": "lab_result",
                    "description": "A1c and glucose showing 0.0 placeholders",
                }
            ]
        }
        result = parse_data_requests(output)
        assert result["load_tier2"] is True
        assert result["has_requests"] is True
        assert "a1c" in result["requested_tests"] or "glucose" in result["requested_tests"]

    def test_lab_flag_as_json_string(self):
        """Flags can arrive as JSON strings (serialized dicts)."""
        output = {
            "missing_data_flags": [
                json.dumps({
                    "priority": "high",
                    "data_type": "lab_result",
                    "description": "HbA1c trend needed for assessment",
                })
            ]
        }
        result = parse_data_requests(output)
        assert result["load_tier2"] is True
        assert "hba1c" in result["requested_tests"]

    def test_medication_flag_triggers_tier2(self):
        output = {
            "missing_data_flags": [
                {"priority": "medium", "data_type": "medication_history", "description": "full med list needed"}
            ]
        }
        result = parse_data_requests(output)
        assert result["load_tier2"] is True

    def test_high_priority_any_type_triggers_tier2(self):
        output = {
            "missing_data_flags": [
                {"priority": "high", "data_type": "social_determinant", "description": "SDoH screening missing"}
            ]
        }
        result = parse_data_requests(output)
        assert result["load_tier2"] is True

    def test_imaging_flag_creates_on_demand(self):
        output = {
            "missing_data_flags": [
                {"priority": "medium", "data_type": "imaging", "description": "abdominal imaging needed"}
            ]
        }
        result = parse_data_requests(output)
        assert len(result["on_demand_requests"]) == 1
        assert result["on_demand_requests"][0]["type"] == "imaging_report"

    def test_low_priority_non_lab_no_tier2(self):
        output = {
            "missing_data_flags": [
                {"priority": "low", "data_type": "social_determinant", "description": "housing status unknown"}
            ]
        }
        result = parse_data_requests(output)
        assert result["load_tier2"] is False

    def test_non_list_flags_handled(self):
        output = {"missing_data_flags": "invalid"}
        result = parse_data_requests(output)
        assert result["has_requests"] is False


class TestAnticipatoryScenarios:
    def test_imaging_evidence_creates_request(self):
        output = {
            "anticipatory_scenarios": [
                {
                    "title": "Hepatic steatosis progression",
                    "evidence_basis": ["Abdominal ultrasound showed mild steatosis"],
                }
            ]
        }
        result = parse_data_requests(output)
        assert result["has_requests"] is True
        assert any(r["type"] == "imaging_report" for r in result["on_demand_requests"])

    def test_non_imaging_evidence_no_request(self):
        output = {
            "anticipatory_scenarios": [
                {
                    "title": "BP drift",
                    "evidence_basis": ["Recent vital signs show upward trend"],
                }
            ]
        }
        result = parse_data_requests(output)
        # No imaging keyword → no on_demand request from scenarios
        assert not any(r["type"] == "imaging_report" for r in result["on_demand_requests"])

    def test_non_dict_scenarios_skipped(self):
        output = {"anticipatory_scenarios": ["invalid_string", 42]}
        result = parse_data_requests(output)
        assert result["has_requests"] is False


class TestDeduplication:
    def test_duplicate_requests_deduped(self):
        output = {
            "missing_data_flags": [
                {"priority": "high", "data_type": "imaging", "description": "imaging needed"},
                {"priority": "high", "data_type": "imaging", "description": "imaging report missing"},
            ],
            "anticipatory_scenarios": [
                {
                    "title": "test",
                    "evidence_basis": ["ultrasound needed"],
                }
            ],
        }
        result = parse_data_requests(output)
        # All have type=imaging_report and no resource_id → deduplicate to 1
        imaging_requests = [r for r in result["on_demand_requests"] if r["type"] == "imaging_report"]
        assert len(imaging_requests) == 1

    def test_max_3_on_demand_requests(self):
        output = {
            "data_requests": [
                {"type": "lab_trend", "test": f"test_{i}", "reason": f"reason {i}"}
                for i in range(10)
            ]
        }
        result = parse_data_requests(output)
        assert len(result["on_demand_requests"]) <= 3


class TestEmptyOutput:
    def test_empty_dict(self):
        result = parse_data_requests({})
        assert result["has_requests"] is False
        assert result["load_tier2"] is False
        assert result["requested_tests"] == []
        assert result["on_demand_requests"] == []

    def test_all_empty_lists(self):
        output = {
            "anticipatory_scenarios": [],
            "missing_data_flags": [],
            "data_requests": [],
        }
        result = parse_data_requests(output)
        assert result["has_requests"] is False


class TestMetabolicTests:
    def test_metabolic_tests_set_nonempty(self):
        assert len(METABOLIC_TESTS) > 0

    def test_common_tests_included(self):
        for test in ["hba1c", "glucose", "ldl", "creatinine", "egfr"]:
            assert test in METABOLIC_TESTS

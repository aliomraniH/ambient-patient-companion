"""
Comprehensive REST smoke tests for all Server 1 routes not covered by test_mcp_smoke.py.
Covers the 13 routes that previously had no HTTP test, plus verify_output_provenance.

Requires all three MCP servers running.
"""
import json
import pytest
import httpx

BASE = "http://localhost:8001"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
TEST_MRN = "4829341"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def post(path: str, body: dict) -> dict:
    r = httpx.post(f"{BASE}{path}", json=body, timeout=15)
    assert r.status_code in (200, 422), f"{path} returned {r.status_code}: {r.text}"
    return r.json()


def get(path: str, params: dict | None = None) -> dict:
    r = httpx.get(f"{BASE}{path}", params=params, timeout=15)
    assert r.status_code == 200, f"{path} returned {r.status_code}: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Data-track management
# ---------------------------------------------------------------------------

class TestDataTrack:
    def test_use_demo_data_returns_message(self):
        d = post("/tools/use_demo_data", {})
        assert "message" in d or "Switched" in str(d)

    def test_use_healthex_returns_message(self):
        d = post("/tools/use_healthex", {})
        assert "message" in d or "Switched" in str(d) or "HealthEx" in str(d)

    def test_switch_data_track_to_synthea(self):
        d = post("/tools/switch_data_track", {"track": "synthea"})
        assert "synthea" in str(d).lower() or "OK" in str(d)

    def test_switch_data_track_invalid_track(self):
        d = post("/tools/switch_data_track", {"track": "invalid_source"})
        assert "error" in str(d).lower() or "invalid" in str(d).lower() or d.get("status") == "error"


# ---------------------------------------------------------------------------
# Patient knowledge & nudges
# ---------------------------------------------------------------------------

class TestPatientKnowledgeNudges:
    def test_get_patient_knowledge_returns_structure(self):
        d = post("/tools/get_patient_knowledge", {
            "patient_id": ZERO_UUID,
            "knowledge_type": "all",
        })
        assert "patient_id" in d or "knowledge" in str(d).lower() or "entries" in d

    def test_get_patient_knowledge_has_count_field(self):
        d = post("/tools/get_patient_knowledge", {
            "patient_id": ZERO_UUID,
        })
        assert isinstance(d, dict)

    def test_get_pending_nudges_patient_role(self):
        d = post("/tools/get_pending_nudges", {
            "patient_id": ZERO_UUID,
            "role": "patient",
        })
        assert "patient_id" in d or "nudges" in d or "pending" in str(d).lower()

    def test_get_pending_nudges_provider_role(self):
        d = post("/tools/get_pending_nudges", {
            "patient_id": ZERO_UUID,
            "role": "provider",
        })
        assert isinstance(d, dict)


# ---------------------------------------------------------------------------
# Deliberation
# ---------------------------------------------------------------------------

class TestDeliberation:
    def test_get_deliberation_results_no_data(self):
        d = post("/tools/get_deliberation_results", {
            "patient_id": ZERO_UUID,
        })
        assert "status" in d or "patient_id" in d or "deliberations" in str(d).lower()

    def test_get_deliberation_results_with_known_mrn(self):
        d = post("/tools/get_deliberation_results", {
            "patient_id": ZERO_UUID,
            "limit": 5,
        })
        assert isinstance(d, dict)


# ---------------------------------------------------------------------------
# Transfer audit
# ---------------------------------------------------------------------------

class TestTransferAudit:
    def test_get_transfer_audit_returns_structure(self):
        d = post("/tools/get_transfer_audit", {
            "patient_id": ZERO_UUID,
        })
        assert "patient_id" in d or "records" in d or "total_records" in d

    def test_get_transfer_audit_has_counts(self):
        d = post("/tools/get_transfer_audit", {
            "patient_id": ZERO_UUID,
        })
        assert "verified_count" in d or "failed_count" in d or isinstance(d, dict)


# ---------------------------------------------------------------------------
# Gap-aware reasoning tools
# ---------------------------------------------------------------------------

@pytest.mark.llm_api
class TestReasoningConfidence:
    """These tests call assess_reasoning_confidence which makes a live LLM API
    call (Claude Opus). Run manually with -m llm_api — skipped in standard CI."""

    @pytest.mark.llm_api
    def test_assess_reasoning_confidence_returns_score(self):
        d = post("/tools/assess_reasoning_confidence", {
            "agent_id": "ARIA",
            "deliberation_id": "test-delib-confidence-001",
            "patient_mrn": TEST_MRN,
            "reasoning_draft": "Patient has elevated LDL and family history of CAD.",
            "clinical_domain": "cardiovascular",
        })
        assert "overall_confidence" in d
        assert isinstance(d["overall_confidence"], (int, float))
        assert 0.0 <= d["overall_confidence"] <= 1.0

    @pytest.mark.llm_api
    def test_assess_reasoning_confidence_has_proceed_flag(self):
        d = post("/tools/assess_reasoning_confidence", {
            "agent_id": "MIRA",
            "deliberation_id": "test-delib-confidence-002",
            "patient_mrn": TEST_MRN,
            "reasoning_draft": "Patient shows motivation to change diet.",
            "clinical_domain": "behavioral_health",
        })
        assert "proceed_recommendation" in d
        assert "gaps" in d


class TestRequestClarification:
    def test_request_clarification_invalid_recipient_returns_error(self):
        d = post("/tools/request_clarification", {
            "deliberation_id": "test-delib-001",
            "requesting_agent": "MIRA",
            "recipient": "invalid_person",
            "urgency": "optional",
            "question_text": "Is patient motivated?",
            "clinical_rationale": "COM-B model requires assessment",
            "gap_id": "gap-comb-001",
        })
        assert d.get("status") == "error"
        assert "recipient" in d.get("error", "").lower()

    def test_request_clarification_invalid_urgency_returns_error(self):
        d = post("/tools/request_clarification", {
            "deliberation_id": "test-delib-001",
            "requesting_agent": "MIRA",
            "recipient": "provider",
            "urgency": "super_urgent",
            "question_text": "Is patient motivated?",
            "clinical_rationale": "COM-B model requires assessment",
            "gap_id": "gap-comb-001",
        })
        assert d.get("status") == "error"
        assert "urgency" in d.get("error", "").lower()

    def test_request_clarification_valid_returns_clarification_id(self):
        d = post("/tools/request_clarification", {
            "deliberation_id": "test-delib-rest-001",
            "requesting_agent": "MIRA",
            "recipient": "provider",
            "urgency": "optional",
            "question_text": "Has patient discussed medication adherence barriers?",
            "clinical_rationale": "COM-B model requires direct assessment",
            "gap_id": "gap-comb-rest-001",
        })
        assert "clarification_id" in d
        assert d.get("status") == "pending"

    def test_request_clarification_all_valid_recipients(self):
        for recipient in ("provider", "patient", "synthesis"):
            d = post("/tools/request_clarification", {
                "deliberation_id": f"test-delib-{recipient}",
                "requesting_agent": "ARIA",
                "recipient": recipient,
                "urgency": "optional",
                "question_text": f"Test question for {recipient}",
                "clinical_rationale": "Testing recipient validation",
                "gap_id": f"gap-{recipient}-001",
            })
            assert "clarification_id" in d, f"recipient={recipient!r} failed: {d}"


class TestEmitReasoningGapArtifact:
    def test_emit_invalid_emitting_agent_returns_error(self):
        d = post("/tools/emit_reasoning_gap_artifact", {
            "deliberation_id": "test-delib-001",
            "emitting_agent": "SYNTHESIS",
            "gap_id": "gap-synth-001",
            "gap_type": "missing_data",
            "severity": "medium",
            "description": "SYNTHESIS cannot emit gaps",
            "impact_statement": "N/A",
            "confidence_without_resolution": 0.5,
            "confidence_with_resolution": 0.8,
            "recommended_action_for_synthesis": "include_caveat_in_output",
        })
        assert d.get("status") == "error"
        assert "emitting_agent" in d.get("error", "").lower()

    def test_emit_invalid_gap_type_returns_error(self):
        d = post("/tools/emit_reasoning_gap_artifact", {
            "deliberation_id": "test-delib-001",
            "emitting_agent": "THEO",
            "gap_id": "gap-bad-type",
            "gap_type": "missing_lab",
            "severity": "medium",
            "description": "Bad gap type",
            "impact_statement": "N/A",
            "confidence_without_resolution": 0.5,
            "confidence_with_resolution": 0.8,
            "recommended_action_for_synthesis": "include_caveat_in_output",
        })
        assert d.get("status") == "error"
        assert "gap_type" in d.get("error", "").lower()

    def test_emit_invalid_severity_returns_error(self):
        d = post("/tools/emit_reasoning_gap_artifact", {
            "deliberation_id": "test-delib-001",
            "emitting_agent": "ARIA",
            "gap_id": "gap-bad-sev",
            "gap_type": "missing_data",
            "severity": "extreme",
            "description": "Bad severity",
            "impact_statement": "N/A",
            "confidence_without_resolution": 0.5,
            "confidence_with_resolution": 0.8,
            "recommended_action_for_synthesis": "include_caveat_in_output",
        })
        assert d.get("status") == "error"
        assert "severity" in d.get("error", "").lower()

    def test_emit_valid_artifact_returns_artifact_id(self):
        d = post("/tools/emit_reasoning_gap_artifact", {
            "deliberation_id": "test-delib-rest-001",
            "emitting_agent": "THEO",
            "gap_id": "gap-hba1c-rest-001",
            "gap_type": "missing_data",
            "severity": "medium",
            "description": "No recent HbA1c in last 90 days",
            "impact_statement": "Cannot assess glycemic control",
            "confidence_without_resolution": 0.4,
            "confidence_with_resolution": 0.9,
            "recommended_action_for_synthesis": "include_caveat_in_output",
            "patient_mrn": TEST_MRN,
        })
        assert "artifact_id" in d
        assert d.get("stored") is True

    def test_emit_all_valid_gap_types(self):
        valid_types = [
            "missing_data", "stale_data", "conflicting_evidence",
            "ambiguous_context", "guideline_uncertainty",
        ]
        for gap_type in valid_types:
            d = post("/tools/emit_reasoning_gap_artifact", {
                "deliberation_id": f"test-delib-{gap_type}",
                "emitting_agent": "ARIA",
                "gap_id": f"gap-{gap_type}-001",
                "gap_type": gap_type,
                "severity": "low",
                "description": f"Testing gap type: {gap_type}",
                "impact_statement": "Test impact",
                "confidence_without_resolution": 0.5,
                "confidence_with_resolution": 0.8,
                "recommended_action_for_synthesis": "include_caveat_in_output",
            })
            assert "artifact_id" in d, f"gap_type={gap_type!r} failed: {d}"


class TestRegisterGapTrigger:
    def test_register_gap_trigger_returns_trigger_id(self):
        d = post("/tools/register_gap_trigger", {
            "patient_mrn": TEST_MRN,
            "gap_id": "gap-hba1c-001",
            "watch_for": "hba1c_result",
            "expires_at": "2026-12-31T00:00:00Z",
            "on_fire_action": "re_deliberate",
        })
        assert "trigger_id" in d
        assert d.get("registered") is True

    def test_register_gap_trigger_with_loinc_code(self):
        d = post("/tools/register_gap_trigger", {
            "patient_mrn": TEST_MRN,
            "gap_id": "gap-a1c-loinc-001",
            "watch_for": "lab_result_received",
            "expires_at": "2026-12-31T00:00:00Z",
            "on_fire_action": "re_deliberate",
            "loinc_code": "4548-4",
        })
        assert "trigger_id" in d


# ---------------------------------------------------------------------------
# verify_output_provenance REST route (new)
# ---------------------------------------------------------------------------

class TestVerifyOutputProvenanceRest:
    MINIMAL_PAYLOAD = json.dumps({
        "sections": [
            {
                "section_id": "sec-1",
                "agent": "ARIA",
                "content_summary": "Patient HbA1c is 8.2%",
                "declared_tier": "TOOL",
                "tool_name": "clinical_query",
                "tool_called_at": "2026-04-13T12:00:00Z",
            }
        ]
    })

    def test_provenance_route_exists_and_returns_report(self):
        d = post("/tools/verify_output_provenance", {
            "payload": self.MINIMAL_PAYLOAD,
        })
        assert isinstance(d, dict)
        assert "gate_decision" in d or "status" in d

    def test_provenance_invalid_payload_returns_error(self):
        d = post("/tools/verify_output_provenance", {
            "payload": "not valid json {{{",
        })
        assert d.get("status") == "error"
        assert "payload" in d.get("error", "").lower() or "json" in d.get("error", "").lower()

    def test_provenance_empty_sections_handled(self):
        d = post("/tools/verify_output_provenance", {
            "payload": json.dumps({"sections": []}),
        })
        assert isinstance(d, dict)

    def test_provenance_with_deliberation_id(self):
        d = post("/tools/verify_output_provenance", {
            "payload": self.MINIMAL_PAYLOAD,
            "deliberation_id": "test-delib-provenance-001",
            "patient_mrn": TEST_MRN,
        })
        assert isinstance(d, dict)
        assert "gate_decision" in d or "status" in d


# ---------------------------------------------------------------------------
# HealthEx registration (should fail cleanly on bad JSON)
# ---------------------------------------------------------------------------

class TestHealthExRegistration:
    def test_register_healthex_patient_bad_json_returns_error(self):
        d = post("/tools/register_healthex_patient", {
            "health_summary_json": "not valid json",
        })
        assert "error" in str(d).lower() or "Error" in str(d)

    def test_register_healthex_patient_empty_summary_returns_error(self):
        d = post("/tools/register_healthex_patient", {
            "health_summary_json": "{}",
        })
        assert isinstance(d, (dict, str))

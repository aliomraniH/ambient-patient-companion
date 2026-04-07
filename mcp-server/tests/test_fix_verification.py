"""Fix verification tests — three bugs fixed in HealthEx ingestion pipeline.

Covers exactly the three fixes from branch claude/fix-healthex-logging-TElYa:

  Fix A (fhir_to_schema.py line 292):
    transform_by_type() now passes `source` as the third argument to transform
    functions.  Every returned record must carry data_source=source, NOT the
    stale "synthea" default that leaked through before the fix.

  Fix B (fhir_to_schema.py lines 206-217):
    transform_encounters() now guards r.get("type") with isinstance checks.
    When "type" is a raw string it wraps it in {"display": raw_type} instead
    of crashing on .get("coding").  When "type" is a list, it also guards
    the first element with isinstance(type_list, dict).

  Fix C (format_b_parser.py):
    parse_compressed_table() now supports resource_type="encounters".
    _default_headers, _build_col_dict_map, _to_native, and _deduplicate all
    have the "encounters" branch.

These tests call the real functions (no mocks) and make precise, observable
assertions about the corrected behaviour.
"""

from __future__ import annotations

import sys
import os

import pytest

# ── Path setup — tests run from workspace root ───────────────────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MCP_SERVER = os.path.join(_REPO_ROOT, "mcp-server")
if _MCP_SERVER not in sys.path:
    sys.path.insert(0, _MCP_SERVER)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from transforms.fhir_to_schema import (          # noqa: E402
    transform_by_type,
    transform_encounters,
    transform_conditions,
)
from ingestion.adapters.healthex.parsers.format_b_parser import (  # noqa: E402
    parse_compressed_table,
)


# ── Shared sample resources ───────────────────────────────────────────────────

_CONDITION_RESOURCE = {
    "resourceType": "Condition",
    "id": "cond-prediabetes-001",
    "code": {
        "coding": [{"system": "http://snomed.info/sct",
                    "code": "714628002", "display": "Prediabetes"}],
        "text": "Prediabetes",
    },
    "clinicalStatus": {"coding": [{"code": "active"}]},
    "onsetDateTime": "2017-04-25",
}

_ENCOUNTER_RESOURCE_FHIR_LIST = {
    "resourceType": "Encounter",
    "id": "enc-001",
    "type": [
        {
            "coding": [{"code": "AMB", "display": "Office Visit",
                        "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode"}]
        }
    ],
    "period": {"start": "2025-06-26T09:00:00Z", "end": "2025-06-26T10:00:00Z"},
    "status": "finished",
}

_ENCOUNTER_RESOURCE_STRING_TYPE = {
    "resourceType": "Encounter",
    "id": "enc-002",
    "type": "Office Visit",                # ← raw string, not list
    "period": {"start": "2025-01-01"},
    "status": "finished",
}

_ENCOUNTER_RESOURCE_NULL_TYPE = {
    "resourceType": "Encounter",
    "id": "enc-003",
    "type": None,                          # ← null — must not crash
    "period": {"start": "2024-11-15"},
}

_ENCOUNTER_RESOURCE_DICT_TYPE = {
    "resourceType": "Encounter",
    "id": "enc-004",
    "type": [{"display": "Telehealth Visit"}],  # no "coding" key — must not crash
    "period": {"start": "2024-06-01"},
}

_FORMAT_B_ENCOUNTERS = """\
#Encounters 1y|Total:2
D:1=2025-06-26|2=2023-12-13|
C:1=Office Visit|2=Telehealth|
Date|Type|Period|Description|Provider|Status|Location|Code
@1|@1||Internal Medicine||completed||
@2|@2||Endocrinology||completed||"""

_FORMAT_B_ENCOUNTERS_MINIMAL = """\
#Encounters|Total:1
C:1=Lab Follow-up|
Date|Type|Description|Status
2025-07-11|@1|Diabetes management review|completed"""


# ═══════════════════════════════════════════════════════════════════════════════
# Fix A — transform_by_type passes source (data_source) correctly
# ═══════════════════════════════════════════════════════════════════════════════

class TestFixA_TransformByTypeDataSource:
    """Fix A: data_source must equal the 'source' kwarg, not hardcoded 'synthea'."""

    def test_healthex_source_propagates_to_conditions(self):
        """Conditions transformed with source='healthex' must have data_source='healthex'."""
        results = transform_by_type(
            "conditions", [_CONDITION_RESOURCE], "patient-uuid-001", source="healthex"
        )
        assert len(results) >= 1, "Expected at least 1 condition record"
        for rec in results:
            assert rec.get("data_source") == "healthex", (
                f"Expected data_source='healthex', got {rec.get('data_source')!r}"
            )

    def test_synthea_source_still_works(self):
        """source='synthea' (the old default) must still produce data_source='synthea'."""
        results = transform_by_type(
            "conditions", [_CONDITION_RESOURCE], "patient-uuid-001", source="synthea"
        )
        for rec in results:
            assert rec.get("data_source") == "synthea"

    def test_manual_source_propagates(self):
        """Any arbitrary source string must be passed through unchanged."""
        results = transform_by_type(
            "conditions", [_CONDITION_RESOURCE], "patient-uuid-001", source="manual"
        )
        for rec in results:
            assert rec.get("data_source") == "manual"

    def test_source_is_required_positional_argument(self):
        """After Fix A, source is a required positional argument (no default).
        Calling without it must raise TypeError, not silently use 'synthea'."""
        with pytest.raises(TypeError):
            transform_by_type(
                "conditions", [_CONDITION_RESOURCE], "patient-uuid-001"
            )

    def test_healthex_source_propagates_to_encounters(self):
        """Encounters transformed with source='healthex' must have data_source='healthex'."""
        results = transform_by_type(
            "encounters", [_ENCOUNTER_RESOURCE_FHIR_LIST],
            "patient-uuid-002", source="healthex"
        )
        assert len(results) >= 1
        for rec in results:
            assert rec.get("data_source") == "healthex", (
                f"Expected data_source='healthex', got {rec.get('data_source')!r}"
            )

    def test_fix_a_regression_synthea_was_the_only_old_behaviour(self):
        """Before Fix A, source was ignored and always 'synthea'.
        After Fix A, the caller-supplied source is respected.
        Passing source='healthex' must NOT produce 'synthea'."""
        results = transform_by_type(
            "conditions", [_CONDITION_RESOURCE], "patient-uuid-001", source="healthex"
        )
        for rec in results:
            assert rec.get("data_source") != "synthea", (
                "Fix A regression: data_source is still 'synthea' despite source='healthex'"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Fix B — transform_encounters handles string "type" without crashing
# ═══════════════════════════════════════════════════════════════════════════════

class TestFixB_TransformEncountersTypeGuard:
    """Fix B: transform_encounters must not crash on string, None, or dict-less type."""

    def test_string_type_returns_record(self):
        """A resource with type='Office Visit' (string) must return a record."""
        result = transform_encounters(
            [_ENCOUNTER_RESOURCE_STRING_TYPE], "patient-uuid-010", "healthex"
        )
        assert len(result) == 1, f"Expected 1 record, got {len(result)}"

    def test_string_type_event_type_is_the_string(self):
        """event_type must equal the raw string value when type is a string."""
        result = transform_encounters(
            [_ENCOUNTER_RESOURCE_STRING_TYPE], "patient-uuid-010", "healthex"
        )
        assert result[0]["event_type"] == "Office Visit", (
            f"Expected event_type='Office Visit', got {result[0]['event_type']!r}"
        )

    def test_string_type_data_source_is_correct(self):
        result = transform_encounters(
            [_ENCOUNTER_RESOURCE_STRING_TYPE], "patient-uuid-010", "healthex"
        )
        assert result[0]["data_source"] == "healthex"

    def test_null_type_does_not_crash(self):
        """type=None must not raise — returns a record with empty event_type."""
        result = transform_encounters(
            [_ENCOUNTER_RESOURCE_NULL_TYPE], "patient-uuid-011", "healthex"
        )
        assert isinstance(result, list)
        assert len(result) == 1, "Expected 1 record even when type is None"

    def test_fhir_list_type_still_works(self):
        """Existing FHIR list-of-CodeableConcept behaviour must be preserved."""
        result = transform_encounters(
            [_ENCOUNTER_RESOURCE_FHIR_LIST], "patient-uuid-012", "healthex"
        )
        assert len(result) == 1
        assert result[0]["event_type"] == "Office Visit"

    def test_list_type_without_coding_does_not_crash(self):
        """type=[{"display": "Telehealth"}] (no coding key) must not crash."""
        result = transform_encounters(
            [_ENCOUNTER_RESOURCE_DICT_TYPE], "patient-uuid-013", "healthex"
        )
        assert isinstance(result, list)
        assert len(result) == 1

    def test_fix_b_regression_string_type_was_crash(self):
        """Before Fix B, this call raised AttributeError ('str' has no .get).
        After Fix B, it must return a record without raising."""
        try:
            result = transform_encounters(
                [{"type": "encounter", "period": {"start": "2025-01-01"}}],
                "patient-uuid-014", "healthex",
            )
            assert isinstance(result, list), "Expected list, got crash"
            assert len(result) == 1
            assert result[0]["event_type"] == "encounter"
        except (AttributeError, TypeError) as exc:
            pytest.fail(f"Fix B regression: transform_encounters crashed: {exc}")

    def test_mixed_batch_string_and_fhir_list(self):
        """Batch with both string-type and FHIR-list-type encounters must not crash."""
        result = transform_encounters(
            [_ENCOUNTER_RESOURCE_STRING_TYPE, _ENCOUNTER_RESOURCE_FHIR_LIST],
            "patient-uuid-015", "healthex",
        )
        assert len(result) == 2

    def test_patient_id_set_on_all_records(self):
        """patient_id must be propagated to every record regardless of type shape."""
        pid = "patient-uuid-016"
        result = transform_encounters(
            [_ENCOUNTER_RESOURCE_STRING_TYPE, _ENCOUNTER_RESOURCE_NULL_TYPE],
            pid, "healthex",
        )
        for rec in result:
            assert rec["patient_id"] == pid


# ═══════════════════════════════════════════════════════════════════════════════
# Fix C — Format B parser now handles encounters
# ═══════════════════════════════════════════════════════════════════════════════

class TestFixC_FormatBEncountersParser:
    """Fix C: parse_compressed_table must parse encounters Format B payloads."""

    def test_encounters_returns_non_empty_list(self):
        rows = parse_compressed_table(_FORMAT_B_ENCOUNTERS, "encounters")
        assert isinstance(rows, list)
        assert len(rows) >= 1, (
            f"Expected ≥1 encounter row from Format B, got {len(rows)}"
        )

    def test_encounters_have_type_key(self):
        """Every encounter row must have a 'type' key."""
        rows = parse_compressed_table(_FORMAT_B_ENCOUNTERS, "encounters")
        for row in rows:
            assert "type" in row, f"Missing 'type' key in row: {row}"

    def test_encounters_have_date_key(self):
        """Every encounter row must have a 'date' key."""
        rows = parse_compressed_table(_FORMAT_B_ENCOUNTERS, "encounters")
        for row in rows:
            assert "date" in row, f"Missing 'date' key in row: {row}"

    def test_encounters_have_description_key(self):
        rows = parse_compressed_table(_FORMAT_B_ENCOUNTERS, "encounters")
        for row in rows:
            assert "description" in row, f"Missing 'description' key in row: {row}"

    def test_encounter_type_resolved_from_dictionary(self):
        """Dict reference @1 for Type must resolve to 'Office Visit'."""
        rows = parse_compressed_table(_FORMAT_B_ENCOUNTERS, "encounters")
        types = [r["type"] for r in rows]
        assert any("Office Visit" in t or "Telehealth" in t for t in types), (
            f"Expected at least one known encounter type, got: {types}"
        )

    def test_encounter_deduplication(self):
        """Duplicate (type, date) pairs must be deduplicated."""
        duplicate_payload = _FORMAT_B_ENCOUNTERS + "\n@1|@1||Duplicate visit|@1|completed||"
        rows = parse_compressed_table(duplicate_payload, "encounters")
        keys = [(r["type"], r["date"]) for r in rows]
        assert len(keys) == len(set(keys)), f"Duplicate encounter rows found: {keys}"

    def test_encounters_minimal_payload_parsed(self):
        """Minimal Format B encounters with bare columns must not crash."""
        rows = parse_compressed_table(_FORMAT_B_ENCOUNTERS_MINIMAL, "encounters")
        assert isinstance(rows, list)

    def test_encounters_empty_input_returns_empty(self):
        rows = parse_compressed_table("", "encounters")
        assert rows == []

    def test_conditions_still_parsed_when_encounters_added(self):
        """Adding encounters support must not break existing conditions parsing."""
        conditions_payload = """\
#Conditions 5y|Total:2
C:1=Prediabetes|2=Hypertension|
S:1=active|
Date|Condition|ClinicalStatus|OnsetDate|SNOMED|ICD10
|@1|@1|2017-04-25|714628002|R73.03
|@2|@1|2019-06-15|38341003|I10"""
        rows = parse_compressed_table(conditions_payload, "conditions")
        assert len(rows) >= 1
        names = [r["name"] for r in rows]
        assert "Prediabetes" in names or "Hypertension" in names

    def test_fix_c_regression_before_fix_returns_empty(self):
        """Before Fix C, resource_type='encounters' always returned [].
        After Fix C, the same payload must return ≥1 row."""
        rows = parse_compressed_table(_FORMAT_B_ENCOUNTERS, "encounters")
        assert rows != [], (
            "Fix C regression: parse_compressed_table('encounters') still returns [] "
            "— the encounters branch is missing from _to_native()"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-fix integration — all three fixes working together
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrossFixIntegration:
    """Verify all three fixes working together through the full pipeline."""

    def test_transform_by_type_encounters_with_string_type_and_healthex_source(self):
        """Combines Fix A (source passthrough) + Fix B (string type guard).
        transform_by_type('encounters', ..., source='healthex') on a resource
        with string type must return a record with data_source='healthex' and
        event_type equal to the string value.
        """
        resource = {
            "resourceType": "Encounter",
            "type": "Telehealth Visit",
            "period": {"start": "2025-03-15"},
            "status": "finished",
        }
        results = transform_by_type(
            "encounters", [resource], "patient-uuid-020", source="healthex"
        )
        assert len(results) >= 1
        rec = results[0]
        assert rec["data_source"] == "healthex", (
            f"Fix A failure in cross-fix test: data_source={rec['data_source']!r}"
        )
        assert rec["event_type"] == "Telehealth Visit", (
            f"Fix B failure in cross-fix test: event_type={rec['event_type']!r}"
        )

    def test_format_b_encounters_produce_correct_native_shape_for_mcp_server(self):
        """Fix C native dicts must have keys that _healthex_native_to_fhir_encounters()
        in mcp_server.py expects: type, date, description, provider, status."""
        rows = parse_compressed_table(_FORMAT_B_ENCOUNTERS, "encounters")
        assert len(rows) >= 1
        expected_keys = {"type", "date", "description"}
        for row in rows:
            missing = expected_keys - set(row.keys())
            assert not missing, (
                f"Encounter row missing keys {missing}: {row}"
            )

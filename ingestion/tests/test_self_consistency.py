"""Tests for self-consistency extractor (ingestion/validators/self_consistency.py).

Tests cover:
  SC-1: values_equivalent comparison logic
  SC-2: Row merging with divergent detection
  SC-3: Key field matching between extractions
"""

import pytest
from ingestion.validators.self_consistency import (
    values_equivalent,
    _merge_rows,
    _find_matching_row,
    _get_key_fields,
)


class TestValuesEquivalent:
    """Test the value comparison logic."""

    def test_identical_strings(self):
        assert values_equivalent("HbA1c", "HbA1c") is True

    def test_identical_numbers(self):
        assert values_equivalent(7.4, 7.4) is True

    def test_numeric_tolerance(self):
        assert values_equivalent(7.4, 7.401) is True
        assert values_equivalent(7.4, 7.5) is False

    def test_string_case_insensitive(self):
        assert values_equivalent("Active", "active") is True

    def test_string_whitespace_normalized(self):
        assert values_equivalent("  HbA1c ", "HbA1c") is True

    def test_none_both(self):
        assert values_equivalent(None, None) is True

    def test_none_one_side(self):
        assert values_equivalent(None, 7.4) is False
        assert values_equivalent(7.4, None) is False

    def test_numeric_string_comparison(self):
        assert values_equivalent("7.4", 7.4) is True
        assert values_equivalent(7.4, "7.4") is True

    def test_different_strings(self):
        assert values_equivalent("HbA1c", "Glucose") is False


class TestRowMerging:
    """Test merging of matched rows from two extractions."""

    def test_identical_rows_full_consensus(self):
        row1 = {"test_name": "HbA1c", "value": "7.4", "date": "2026-01-15"}
        row2 = {"test_name": "HbA1c", "value": "7.4", "date": "2026-01-15"}
        consensus, divergent = _merge_rows(row1, row2)
        assert consensus["test_name"] == "HbA1c"
        assert consensus["value"] == "7.4"
        assert len(divergent) == 0

    def test_divergent_value_nulled(self):
        row1 = {"test_name": "Creatinine", "value": "1.2", "date": "2026-01-15"}
        row2 = {"test_name": "Creatinine", "value": "1.5", "date": "2026-01-15"}
        consensus, divergent = _merge_rows(row1, row2)
        assert consensus["test_name"] == "Creatinine"
        assert consensus["value"] is None  # Divergent → nulled
        assert consensus["date"] == "2026-01-15"
        assert len(divergent) == 1
        assert divergent[0]["field"] == "value"

    def test_extra_field_in_one_row(self):
        row1 = {"test_name": "HbA1c", "value": "7.4"}
        row2 = {"test_name": "HbA1c", "value": "7.4", "code": "4548-4"}
        consensus, divergent = _merge_rows(row1, row2)
        assert consensus["test_name"] == "HbA1c"
        assert consensus["value"] == "7.4"
        # "code" only in row2: None vs "4548-4" → divergent
        assert consensus["code"] is None
        assert len(divergent) == 1


class TestKeyFieldMatching:
    """Test matching rows between extractions."""

    def test_key_fields_for_labs(self):
        assert _get_key_fields("labs") == ["test_name", "date"]

    def test_key_fields_for_conditions(self):
        assert _get_key_fields("conditions") == ["name"]

    def test_find_matching_row_found(self):
        row = {"test_name": "HbA1c", "date": "2026-01-15", "value": "7.4"}
        candidates = [
            {"test_name": "Glucose", "date": "2026-01-15", "value": "120"},
            {"test_name": "HbA1c", "date": "2026-01-15", "value": "7.5"},
        ]
        result = _find_matching_row(row, candidates, ["test_name", "date"], set())
        assert result is not None
        idx, matched = result
        assert idx == 1
        assert matched["test_name"] == "HbA1c"

    def test_find_matching_row_not_found(self):
        row = {"test_name": "Creatinine", "date": "2026-01-15"}
        candidates = [
            {"test_name": "HbA1c", "date": "2026-01-15"},
            {"test_name": "Glucose", "date": "2026-01-15"},
        ]
        result = _find_matching_row(row, candidates, ["test_name", "date"], set())
        assert result is None

    def test_excluded_indices_skipped(self):
        row = {"test_name": "HbA1c", "date": "2026-01-15"}
        candidates = [
            {"test_name": "HbA1c", "date": "2026-01-15"},
        ]
        # Index 0 excluded
        result = _find_matching_row(row, candidates, ["test_name", "date"], {0})
        assert result is None

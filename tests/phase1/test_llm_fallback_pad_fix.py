"""
test_llm_fallback_pad_fix.py — P-2 part 1: LLM fallback must not pad.

Covers:
  - _strip_empty_rows drops rows whose non-empty values are all placeholders
  - _is_empty_row returns True for rows of {"", "unknown", "n/a", None}
  - Validation preserves rows that have at least one meaningful value
  - System prompt explicitly forbids padding to a fixed count
"""

from __future__ import annotations

import pytest

from ingestion.adapters.healthex.llm_fallback import (
    _is_empty_row,
    _strip_empty_rows,
    WAREHOUSE_SCHEMAS,
)


class TestIsEmptyRow:
    def test_all_empty_strings(self):
        schema = WAREHOUSE_SCHEMAS["conditions"]
        assert _is_empty_row({"name": "", "code": "", "onset_date": ""}, schema) is True

    def test_all_placeholder_values(self):
        schema = WAREHOUSE_SCHEMAS["conditions"]
        assert _is_empty_row({"name": "unknown", "code": "n/a", "status": "NONE"}, schema) is True

    def test_one_real_value_makes_row_non_empty(self):
        schema = WAREHOUSE_SCHEMAS["conditions"]
        assert _is_empty_row({"name": "Prediabetes", "code": "", "onset_date": ""}, schema) is False

    def test_numeric_non_zero_is_meaningful(self):
        schema = WAREHOUSE_SCHEMAS["labs"]
        assert _is_empty_row({"test_name": "", "value": 4.8, "unit": "%"}, schema) is False

    def test_nonlist_passthrough(self):
        schema = WAREHOUSE_SCHEMAS["labs"]
        assert _is_empty_row(None, schema) is True


class TestStripEmptyRows:
    def test_strips_only_empty_rows(self):
        schema = WAREHOUSE_SCHEMAS["conditions"]
        rows = [
            {"name": "Prediabetes", "code": "R73.03"},
            {"name": "", "code": "", "onset_date": ""},
            {"name": "unknown", "code": "n/a"},
            {"name": "Hypertension", "code": "I10"},
        ]
        out = _strip_empty_rows(rows, "conditions", schema)
        assert len(out) == 2
        assert out[0]["name"] == "Prediabetes"
        assert out[1]["name"] == "Hypertension"

    def test_all_rows_empty_returns_empty_list(self):
        schema = WAREHOUSE_SCHEMAS["medications"]
        rows = [{}, {"name": ""}, {"name": "unknown"}]
        out = _strip_empty_rows(rows, "medications", schema)
        assert out == []

    def test_preserves_all_rows_when_none_empty(self):
        schema = WAREHOUSE_SCHEMAS["medications"]
        rows = [
            {"name": "Pantoprazole", "start_date": "2022-03-10"},
            {"name": "Atorvastatin", "start_date": "2020-05-14"},
        ]
        out = _strip_empty_rows(rows, "medications", schema)
        assert len(out) == 2


class TestSystemPromptForbidsPadding:
    """Guard against the prompt regression that caused the padding bug.

    If the no-padding instruction is ever dropped or softened, the LLM
    will go back to returning fixed-size arrays. This test locks in the
    exact guardrail language.
    """

    def test_prompt_contains_no_pad_instruction(self):
        from ingestion.adapters.healthex import llm_fallback
        src = llm_fallback.__file__
        with open(src) as fh:
            text = fh.read()
        assert "DO NOT pad the array" in text, (
            "LLM fallback prompt no longer forbids padding — "
            "padding regression will silently reappear"
        )
        assert "DO NOT invent records" in text

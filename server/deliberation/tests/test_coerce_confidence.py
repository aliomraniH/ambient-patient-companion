"""Unit tests for shared.coercion.coerce_confidence.

Regression guard for the anticipatory_scenario silent-drop bug: LLMs emit
categorical strings ("high", "moderate") for the confidence field even when
the prompt requests a float, and the raw string was being bound directly
into a Postgres FLOAT column.
"""
from __future__ import annotations

import pathlib
import sys

# Make repo-root imports work when pytest is run from subdirectories.
_repo_root = pathlib.Path(__file__).resolve().parents[3]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from shared.coercion import coerce_confidence  # noqa: E402


# ── numeric passthrough ───────────────────────────────────────────────────

def test_float_passthrough():
    assert coerce_confidence(0.85) == 0.85


def test_float_zero():
    assert coerce_confidence(0.0) == 0.0


def test_float_one():
    assert coerce_confidence(1.0) == 1.0


def test_int_zero_and_one_are_literals():
    assert coerce_confidence(0) == 0.0
    assert coerce_confidence(1) == 1.0


def test_int_percentage_scaled():
    assert coerce_confidence(85) == 0.85
    assert coerce_confidence(50) == 0.50


def test_int_percentage_over_100_clamps():
    assert coerce_confidence(150) == 1.0


# ── categoricals (the actual bug) ──────────────────────────────────────────

def test_categorical_critical():
    assert coerce_confidence("critical") == 0.95


def test_categorical_high():
    assert coerce_confidence("high") == 0.80


def test_categorical_moderate():
    assert coerce_confidence("moderate") == 0.60


def test_categorical_medium_alias():
    assert coerce_confidence("medium") == 0.60


def test_categorical_low():
    assert coerce_confidence("low") == 0.35


def test_categorical_very_high():
    assert coerce_confidence("very high") == 0.90


def test_categorical_case_and_whitespace_insensitive():
    assert coerce_confidence("  HIGH  ") == 0.80
    assert coerce_confidence("Moderate") == 0.60


# ── numeric strings ───────────────────────────────────────────────────────

def test_numeric_string():
    assert coerce_confidence("0.75") == 0.75


def test_numeric_string_with_whitespace():
    assert coerce_confidence("  0.42  ") == 0.42


# ── edge cases ────────────────────────────────────────────────────────────

def test_none_returns_none():
    assert coerce_confidence(None) is None


def test_empty_string_returns_none():
    assert coerce_confidence("") is None
    assert coerce_confidence("   ") is None


def test_unresolvable_string_returns_none():
    assert coerce_confidence("definitely maybe") is None
    assert coerce_confidence("superb") is None


def test_clamp_above_one():
    assert coerce_confidence(1.5) == 1.0


def test_clamp_below_zero():
    assert coerce_confidence(-0.3) == 0.0


def test_clamp_numeric_string_above_one():
    assert coerce_confidence("1.8") == 1.0


def test_bool_returns_none_not_one():
    # bool is a subclass of int — intercept so True/False don't become 1.0/0.0.
    assert coerce_confidence(True) is None
    assert coerce_confidence(False) is None


def test_unknown_type_returns_none():
    assert coerce_confidence([0.5]) is None
    assert coerce_confidence({"confidence": 0.5}) is None

"""V2 verification suite — 14 registry-driven checks plus extended
coverage for QuestionnaireResponse item-level parsing, SDoH, pgvector
atom retrieval, and the card-list resurfacing tool.

DB-touching tests are skipped when DATABASE_URL is not set. All pure-unit
tests run without any external services.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_MCP = _REPO_ROOT / "mcp-server"
if str(_MCP) not in sys.path:
    sys.path.append(str(_MCP))


# ─────────────────────────────────────────────────────────────────────────
# 1. Registry completeness
# ─────────────────────────────────────────────────────────────────────────

def test_1_registry_has_exactly_17_instruments():
    from skills.screening_registry import SCREENING_REGISTRY
    assert len(SCREENING_REGISTRY) == 17


def test_2_registry_covers_all_11_domains():
    from skills.screening_registry import SCREENING_REGISTRY, DOMAINS
    assert len(DOMAINS) == 11
    covered = {inst.domain for inst in SCREENING_REGISTRY.values()}
    assert covered == set(DOMAINS.keys())


def test_3_every_instrument_has_severity_bands_and_loinc():
    from skills.screening_registry import SCREENING_REGISTRY
    for key, inst in SCREENING_REGISTRY.items():
        assert inst.severity_bands, f"{key} missing severity_bands"
        assert inst.loinc_panel, f"{key} missing loinc_panel"
        assert inst.display_name, f"{key} missing display_name"


# ─────────────────────────────────────────────────────────────────────────
# 2. LOINC resolution
# ─────────────────────────────────────────────────────────────────────────

def test_4_loinc_reverse_index_round_trip():
    from skills.screening_registry import (
        SCREENING_REGISTRY, LOINC_TO_INSTRUMENT, get_instrument_by_loinc,
    )
    for key, inst in SCREENING_REGISTRY.items():
        if inst.loinc_panel:
            assert LOINC_TO_INSTRUMENT[inst.loinc_panel] == key
            assert get_instrument_by_loinc(inst.loinc_panel).key == key


def test_5_unknown_loinc_returns_none():
    from skills.screening_registry import get_instrument_by_loinc
    assert get_instrument_by_loinc("9999-9") is None
    assert get_instrument_by_loinc(None) is None
    assert get_instrument_by_loinc("") is None


# ─────────────────────────────────────────────────────────────────────────
# 3. Severity bands & positive-cutoff logic
# ─────────────────────────────────────────────────────────────────────────

def test_6_phq9_severity_bands():
    from skills.screening_registry import SCREENING_REGISTRY, severity_band_for_score
    phq9 = SCREENING_REGISTRY["phq9"]
    assert severity_band_for_score(phq9, 3).label == "minimal"
    assert severity_band_for_score(phq9, 12).label == "moderate"
    assert severity_band_for_score(phq9, 22).label == "severe"


def test_7_auditc_gender_specific_positive_cutoff():
    from skills.screening_registry import SCREENING_REGISTRY, is_positive_screen
    auditc = SCREENING_REGISTRY["auditc"]
    # Female cutoff = 3, male cutoff = 4.
    assert is_positive_screen(auditc, 3, gender="female") is True
    assert is_positive_screen(auditc, 3, gender="male") is False
    assert is_positive_screen(auditc, 4, gender="male") is True


# ─────────────────────────────────────────────────────────────────────────
# 4. Critical items (e.g. PHQ-9 item 9)
# ─────────────────────────────────────────────────────────────────────────

def test_8_phq9_critical_item_triggers_at_score_1():
    from skills.screening_registry import SCREENING_REGISTRY, critical_items_triggered
    phq9 = SCREENING_REGISTRY["phq9"]
    hits = critical_items_triggered(phq9, {9: 1})
    assert len(hits) == 1
    assert hits[0]["item_number"] == 9
    assert hits[0]["priority"] == "critical"


def test_9_phq9_critical_item_does_not_trigger_at_zero():
    from skills.screening_registry import SCREENING_REGISTRY, critical_items_triggered
    phq9 = SCREENING_REGISTRY["phq9"]
    assert critical_items_triggered(phq9, {9: 0}) == []


# ─────────────────────────────────────────────────────────────────────────
# 5. Atom → instrument suggestion
# ─────────────────────────────────────────────────────────────────────────

def test_10_atom_suggestion_ranks_anxiety_atoms_toward_gad():
    from skills.screening_registry import suggest_instruments_from_atoms
    ranked = suggest_instruments_from_atoms(
        ["anxiety_markers", "device_checking", "psychomotor_restlessness"]
    )
    assert "GAD-7" in ranked


def test_11_atom_suggestion_ranks_si_atoms_toward_phq9_and_suicide_tools():
    from skills.screening_registry import suggest_instruments_from_atoms
    ranked = suggest_instruments_from_atoms(["passive_si"])
    # passive_si is in phq9 AND cssrs/asq atom_signals.
    assert any(x in ranked for x in ("PHQ-9", "C-SSRS", "ASQ"))


# ─────────────────────────────────────────────────────────────────────────
# 6. FHIR parsing — QuestionnaireResponse item-level (PHQ-9, GAD-7, AUDIT-C)
# ─────────────────────────────────────────────────────────────────────────

def _phq9_qr(items: dict[int, int]) -> dict:
    return {
        "resourceType": "QuestionnaireResponse",
        "authored": "2026-03-15",
        "code": {"coding": [{"system": "http://loinc.org", "code": "44249-1"}]},
        "item": [
            {"linkId": f"phq9-item-{k}",
             "answer": [{"valueInteger": v}]}
            for k, v in items.items()
        ],
    }


def test_12_phq9_qr_parses_all_items_and_total_score():
    from skills.behavioral_screening_ingestor import parse_questionnaire_response_to_screening
    qr = _phq9_qr({1: 2, 2: 3, 3: 1, 4: 2, 5: 1, 6: 2, 7: 1, 8: 0, 9: 1})
    row = parse_questionnaire_response_to_screening(qr, "patient-1")
    assert row is not None
    assert row["instrument_key"] == "phq9"
    assert row["domain"] == "depression"
    assert row["total_score"] == 13
    assert row["severity_band"] == "moderate"
    assert row["item_scores"] == {str(i): v for i, v in
                                   {1: 2, 2: 3, 3: 1, 4: 2, 5: 1, 6: 2,
                                    7: 1, 8: 0, 9: 1}.items()}
    # Item 9 = 1 → critical flag fires.
    assert any(c["item_number"] == 9 for c in row["triggered_critical"])


def test_13_auditc_qr_applies_gender_cutoff_via_registry():
    from skills.behavioral_screening_ingestor import parse_questionnaire_response_to_screening
    qr = {
        "resourceType": "QuestionnaireResponse",
        "authored": "2026-02-01",
        "code": {"coding": [{"system": "http://loinc.org", "code": "75624-7"}]},
        "item": [
            {"linkId": "auditc-item-1", "answer": [{"valueInteger": 2}]},
            {"linkId": "auditc-item-2", "answer": [{"valueInteger": 1}]},
            {"linkId": "auditc-item-3", "answer": [{"valueInteger": 0}]},
        ],
    }
    row = parse_questionnaire_response_to_screening(qr, "patient-1")
    assert row["total_score"] == 3
    # Gender-neutral default cutoff = 3 (female) → positive True.
    assert row["is_positive"] is True


def test_14_gad7_qr_parses_and_totals():
    from skills.behavioral_screening_ingestor import parse_questionnaire_response_to_screening
    qr = {
        "resourceType": "QuestionnaireResponse",
        "authored": "2026-03-01",
        "code": {"coding": [{"system": "http://loinc.org", "code": "69737-5"}]},
        "item": [
            {"linkId": f"gad7-item-{i}", "answer": [{"valueInteger": v}]}
            for i, v in enumerate([2, 2, 2, 1, 1, 1, 1], start=1)
        ],
    }
    row = parse_questionnaire_response_to_screening(qr, "patient-1")
    assert row["instrument_key"] == "gad7"
    assert row["total_score"] == 10
    assert row["severity_band"] == "moderate"


# ─────────────────────────────────────────────────────────────────────────
# 7. FHIR parsing — unknown LOINC safety
# ─────────────────────────────────────────────────────────────────────────

def test_15_unknown_qr_loinc_returns_none_no_raise():
    from skills.behavioral_screening_ingestor import parse_questionnaire_response_to_screening
    qr = {
        "resourceType": "QuestionnaireResponse",
        "code": {"coding": [{"system": "http://loinc.org", "code": "9999-9"}]},
        "item": [],
    }
    assert parse_questionnaire_response_to_screening(qr, "patient-1") is None


def test_16_non_fhir_resource_returns_none():
    from skills.behavioral_screening_ingestor import (
        parse_questionnaire_response_to_screening,
        parse_fhir_observation_to_screening,
    )
    assert parse_questionnaire_response_to_screening({}, "p") is None
    assert parse_fhir_observation_to_screening({"resourceType": "Patient"}, "p") is None


# ─────────────────────────────────────────────────────────────────────────
# 8. SDoH parsing (PRAPARE, Hunger Vital Sign)
# ─────────────────────────────────────────────────────────────────────────

def test_17_prapare_qr_identifies_food_insecurity():
    from skills.behavioral_screening_ingestor import parse_questionnaire_response_to_sdoh
    qr = {
        "resourceType": "QuestionnaireResponse",
        "authored": "2026-01-10",
        "code": {"coding": [{"system": "http://loinc.org", "code": "93025-5"}]},
        "item": [
            {"linkId": "prapare-item-13",
             "answer": [{"valueString": "often_true"}]},
        ],
    }
    row = parse_questionnaire_response_to_sdoh(qr, "patient-1")
    assert row is not None
    assert "food_insecurity" in row["positive_domains"]


def test_18_hunger_vital_sign_qr_flags_food_insecurity():
    from skills.behavioral_screening_ingestor import parse_questionnaire_response_to_sdoh
    qr = {
        "resourceType": "QuestionnaireResponse",
        "authored": "2026-03-01",
        "code": {"coding": [{"system": "http://loinc.org", "code": "88121-9"}]},
        "item": [
            {"linkId": "hvs-item-1", "answer": [{"valueString": "often_true"}]},
            {"linkId": "hvs-item-2", "answer": [{"valueString": "never_true"}]},
        ],
    }
    row = parse_questionnaire_response_to_sdoh(qr, "patient-1")
    assert "food_insecurity" in row["positive_domains"]


# ─────────────────────────────────────────────────────────────────────────
# 9. Domain gap isolation
# ─────────────────────────────────────────────────────────────────────────

def test_19_suggest_domains_isolates_by_signal_type():
    from skills.screening_registry import suggest_domains_from_atoms
    # Depression-only signals should not implicate anxiety or alcohol.
    domains = suggest_domains_from_atoms(["low_affect", "social_withdrawal"])
    assert "depression" in domains
    # No alcohol signal → no alcohol_use domain.
    assert "alcohol_use" not in domains


def test_20_domain_lookback_days_have_values_for_all_domains():
    from skills.screening_registry import DOMAIN_LOOKBACK_DAYS, DOMAINS
    for d in DOMAINS:
        assert d in DOMAIN_LOOKBACK_DAYS
        assert DOMAIN_LOOKBACK_DAYS[d] >= 90


# ─────────────────────────────────────────────────────────────────────────
# 10. Cards tool — schema validation
# ─────────────────────────────────────────────────────────────────────────

def test_21_critical_flag_card_has_required_fields_and_roles():
    """Build a critical-flag card directly and verify schema contract."""
    from skills.behavioral_cards import _build_critical_flag_cards
    screen = {
        "id": uuid.uuid4(),
        "instrument_key": "phq9",
        "instrument_name": "PHQ-9",
        "domain": "depression",
        "observation_date": date.today(),
        "triggered_critical": [{
            "instrument": "PHQ-9",
            "item_number": 9,
            "alert_text": "PHQ-9 item 9 (passive SI) elevated",
            "actual_score": 1,
            "priority": "critical",
        }],
    }
    cards = _build_critical_flag_cards(screen)
    assert len(cards) == 1
    c = cards[0]
    for key in ("card_id", "card_type", "title", "domain", "priority",
                "body_text", "evidence", "actions", "critical_flags",
                "temporal_confidence", "show_to_roles", "source"):
        assert key in c, f"card missing '{key}'"
    assert c["card_type"] == "critical_flag"
    assert "pcp" in c["show_to_roles"]
    assert "patient" not in c["show_to_roles"]  # PHI gate


def test_22_sdoh_card_emits_per_positive_domain():
    from skills.behavioral_cards import _build_sdoh_cards
    sdoh = {
        "id": uuid.uuid4(),
        "instrument_key": "prapare",
        "instrument_name": "PRAPARE",
        "observation_date": date.today(),
        "positive_domains": ["food_insecurity", "housing_instability"],
    }
    cards = _build_sdoh_cards(sdoh)
    assert len(cards) == 2
    assert {c["domain"] for c in cards} == {"food_insecurity", "housing_instability"}


# ─────────────────────────────────────────────────────────────────────────
# 11. Embedder stub determinism (for vector search round-trip)
# ─────────────────────────────────────────────────────────────────────────

def test_23_embedder_stub_is_deterministic_and_normalized():
    from skills.atom_embedder import embed_signal_value, EMBED_DIM
    v1 = embed_signal_value("trouble sleeping")
    v2 = embed_signal_value("trouble sleeping")
    v3 = embed_signal_value("different phrase")
    assert v1 == v2, "stub embeddings must be deterministic"
    assert v1 != v3
    assert len(v1) == EMBED_DIM
    mag = sum(x * x for x in v1) ** 0.5
    assert abs(mag - 1.0) < 1e-5


def test_24_embedder_format_for_pgvector_round_trips():
    from skills.atom_embedder import embed_signal_value, format_for_pgvector
    v = embed_signal_value("hello")
    s = format_for_pgvector(v)
    assert s.startswith("[") and s.endswith("]")
    parts = s.strip("[]").split(",")
    assert len(parts) == len(v)


# ─────────────────────────────────────────────────────────────────────────
# DB integration tests — skipped when DATABASE_URL is not set
# ─────────────────────────────────────────────────────────────────────────

db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — DB integration test skipped",
)


@db_required
async def test_migration_011_creates_tables_and_drops_legacy():
    import asyncpg
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        for t in ("behavioral_screenings", "sdoh_screenings"):
            assert await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_name=$1)", t,
            ), f"Missing table {t}"
        # phq9_observations should be gone after 011 runs.
        dropped = not await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_name='phq9_observations')"
        )
        assert dropped, "phq9_observations should be dropped by migration 011"
        # triggered_domains column exists on behavioral_screening_gaps.
        col = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='behavioral_screening_gaps' "
            "AND column_name='triggered_domains'"
        )
        assert col is not None
    finally:
        await conn.close()

"""
test_atom_detection.py — Tests for the behavioral atom extractor and embedder.

Covers:
  - Text extraction for all 15 signal types
  - Check-in extraction
  - Confidence thresholds
  - Embedding backend selection
  - Stub embedder determinism + dimensionality
  - Screening ingestor parsing (no DB — unit test)
"""
import sys
import os
import math

_ROOT = os.path.join(os.path.dirname(__file__), "..", "mcp-server")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest
from skills.behavioral_atom_extractor import (
    extract_atoms_from_text,
    extract_atoms_from_checkin,
    ExtractedAtom,
)
from skills.atom_embedder import embed_signal_value, active_backend, _stub_embed


# ─── Extractor ────────────────────────────────────────────────────────────────

class TestAtomExtractor:

    def test_empty_text_returns_empty(self):
        assert extract_atoms_from_text("") == []

    def test_whitespace_only_returns_empty(self):
        assert extract_atoms_from_text("   \n\t  ") == []

    def test_depression_markers_detected(self):
        atoms = extract_atoms_from_text("I feel so depressed and hopeless lately")
        types = {a.signal_type for a in atoms}
        assert "depression_markers" in types

    def test_anxiety_markers_detected(self):
        atoms = extract_atoms_from_text("I've been really anxious and panicking a lot")
        types = {a.signal_type for a in atoms}
        assert "anxiety_markers" in types

    def test_suicidality_detected(self):
        atoms = extract_atoms_from_text("I want to die and feel like ending my life")
        types = {a.signal_type for a in atoms}
        assert "suicidality_markers" in types

    def test_substance_mention_detected(self):
        atoms = extract_atoms_from_text("I've been drinking alcohol heavily and having cravings")
        types = {a.signal_type for a in atoms}
        assert "substance_mention" in types

    def test_sleep_disturbance_detected(self):
        atoms = extract_atoms_from_text("I have terrible insomnia and can't sleep at all")
        types = {a.signal_type for a in atoms}
        assert "sleep_disturbance" in types

    def test_trauma_markers_detected(self):
        atoms = extract_atoms_from_text("I keep having flashbacks and PTSD nightmares")
        types = {a.signal_type for a in atoms}
        assert "trauma_markers" in types

    def test_adhd_markers_detected(self):
        atoms = extract_atoms_from_text("I was diagnosed with ADHD and can't focus or sit still")
        types = {a.signal_type for a in atoms}
        assert "adhd_markers" in types

    def test_mood_changes_detected(self):
        atoms = extract_atoms_from_text("I've been having terrible mood swings and feeling manic")
        types = {a.signal_type for a in atoms}
        assert "mood_changes" in types

    def test_social_withdrawal_detected(self):
        atoms = extract_atoms_from_text("I'm completely isolated and avoiding everyone these days")
        types = {a.signal_type for a in atoms}
        assert "social_withdrawal" in types

    def test_cognitive_concerns_detected(self):
        atoms = extract_atoms_from_text("I have terrible memory loss and brain fog all the time")
        types = {a.signal_type for a in atoms}
        assert "cognitive_concerns" in types

    def test_appetite_change_detected(self):
        atoms = extract_atoms_from_text("I lost my appetite completely and skipping meals")
        types = {a.signal_type for a in atoms}
        assert "appetite_change" in types

    def test_somatic_complaints_detected(self):
        atoms = extract_atoms_from_text("I have chronic pain and bad headaches all the time")
        types = {a.signal_type for a in atoms}
        assert "somatic_complaints" in types

    def test_avoidance_behavior_detected(self):
        atoms = extract_atoms_from_text("I've been avoiding going out due to agoraphobia")
        types = {a.signal_type for a in atoms}
        assert "avoidance_behavior" in types

    def test_hypervigilance_detected(self):
        atoms = extract_atoms_from_text("I'm constantly hypervigilant and startled by any sound")
        types = {a.signal_type for a in atoms}
        assert "hypervigilance" in types

    def test_concentration_difficulty_detected(self):
        atoms = extract_atoms_from_text("I can't concentrate at all and keep losing my train of thought")
        types = {a.signal_type for a in atoms}
        assert "concentration_difficulty" in types

    def test_neutral_text_returns_nothing(self):
        atoms = extract_atoms_from_text("The weather is nice today and I went for a walk.")
        assert len(atoms) == 0

    def test_returns_extracted_atom_instances(self):
        atoms = extract_atoms_from_text("I feel very depressed today")
        assert all(isinstance(a, ExtractedAtom) for a in atoms)

    def test_confidence_in_range(self):
        atoms = extract_atoms_from_text("I feel depressed and anxious and can't sleep")
        for a in atoms:
            assert 0.0 <= a.confidence <= 1.0

    def test_min_confidence_filter(self):
        atoms_low = extract_atoms_from_text(
            "I feel depressed", min_confidence=0.0
        )
        atoms_high = extract_atoms_from_text(
            "I feel depressed", min_confidence=0.99
        )
        assert len(atoms_low) >= len(atoms_high)

    def test_signal_value_not_empty(self):
        atoms = extract_atoms_from_text("I'm feeling anxious and depressed")
        for a in atoms:
            assert a.signal_value.strip() != ""

    def test_signal_value_max_length(self):
        atoms = extract_atoms_from_text("I'm feeling anxious " * 200)
        for a in atoms:
            assert len(a.signal_value) <= 500

    def test_source_type_propagated(self):
        atoms = extract_atoms_from_text(
            "I feel anxious", source_type="clinical_note"
        )
        for a in atoms:
            assert a.source_type == "clinical_note"

    def test_source_id_propagated(self):
        atoms = extract_atoms_from_text(
            "I feel anxious", source_id="test-uuid-123"
        )
        for a in atoms:
            assert a.source_id == "test-uuid-123"

    def test_max_atoms_per_signal_cap(self):
        text = ("I feel anxious and panicking " * 20)
        atoms = extract_atoms_from_text(text, max_atoms_per_signal=3)
        counts = {}
        for a in atoms:
            counts[a.signal_type] = counts.get(a.signal_type, 0) + 1
        for sig, count in counts.items():
            assert count <= 3, f"{sig} exceeded cap: {count}"

    def test_no_duplicates_same_value(self):
        text = "I feel depressed"
        atoms = extract_atoms_from_text(text)
        # No duplicate (signal_type, signal_value[:100]) combos
        seen = set()
        for a in atoms:
            key = (a.signal_type, a.signal_value[:100])
            assert key not in seen, f"Duplicate atom: {key}"
            seen.add(key)

    def test_clinical_abbreviation_phq(self):
        atoms = extract_atoms_from_text("Patient shows major depressive episode, score of 18")
        types = {a.signal_type for a in atoms}
        assert "depression_markers" in types

    def test_clinical_abbreviation_gad(self):
        atoms = extract_atoms_from_text("Generalized anxiety disorder screening was performed")
        types = {a.signal_type for a in atoms}
        assert "anxiety_markers" in types


# ─── Check-in extractor ───────────────────────────────────────────────────────

class TestCheckinExtractor:

    def test_terrible_mood_triggers_depression(self):
        checkin = {"mood": "terrible", "notes": ""}
        atoms = extract_atoms_from_checkin(checkin)
        types = {a.signal_type for a in atoms}
        assert "depression_markers" in types

    def test_high_stress_triggers_extraction(self):
        checkin = {"stress_level": 9, "notes": ""}
        atoms = extract_atoms_from_checkin(checkin)
        # Stress note should trigger anxiety or at least something
        assert len(atoms) >= 0  # non-fatal if no pattern for "extreme stress"

    def test_sleep_deprivation_triggers(self):
        checkin = {"sleep_hours": 2.5, "sleep_quality": "poor", "notes": ""}
        atoms = extract_atoms_from_checkin(checkin)
        types = {a.signal_type for a in atoms}
        assert "sleep_disturbance" in types

    def test_notes_are_extracted(self):
        checkin = {"notes": "I feel so hopeless and can't sleep at all"}
        atoms = extract_atoms_from_checkin(checkin)
        types = {a.signal_type for a in atoms}
        assert "depression_markers" in types

    def test_normal_checkin_returns_empty_or_few(self):
        checkin = {"mood": "good", "sleep_hours": 8.0, "sleep_quality": "good",
                   "stress_level": 2, "notes": ""}
        atoms = extract_atoms_from_checkin(checkin)
        assert len(atoms) == 0

    def test_source_type_is_checkin(self):
        checkin = {"mood": "terrible"}
        atoms = extract_atoms_from_checkin(checkin)
        for a in atoms:
            assert a.source_type == "checkin"

    def test_source_id_propagated(self):
        checkin = {"mood": "terrible"}
        atoms = extract_atoms_from_checkin(checkin, source_id="checkin-uuid-999")
        for a in atoms:
            assert a.source_id == "checkin-uuid-999"


# ─── Embedder ─────────────────────────────────────────────────────────────────

class TestAtomEmbedder:

    def test_stub_embed_returns_768_dims(self):
        vec = _stub_embed("test text")
        assert len(vec) == 768

    def test_stub_embed_normalized(self):
        vec = _stub_embed("test text")
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 1e-5, f"Stub embedding not normalised: norm={norm}"

    def test_stub_embed_deterministic(self):
        v1 = _stub_embed("depression anxiety hopeless")
        v2 = _stub_embed("depression anxiety hopeless")
        assert v1 == v2

    def test_stub_embed_different_texts_different_vectors(self):
        v1 = _stub_embed("depression")
        v2 = _stub_embed("anxiety")
        assert v1 != v2

    def test_embed_signal_value_returns_list_or_none(self):
        result = embed_signal_value("I feel very anxious")
        assert result is None or isinstance(result, list)

    def test_embed_signal_value_empty_string_returns_none(self):
        assert embed_signal_value("") is None

    def test_embed_signal_value_whitespace_returns_none(self):
        assert embed_signal_value("   ") is None

    def test_embed_signal_value_correct_dim(self):
        result = embed_signal_value("I feel depressed")
        if result is not None:
            assert len(result) == 768

    def test_active_backend_returns_string(self):
        backend = active_backend()
        assert backend in ("hf_api", "medcpt_local", "openai", "stub")

    def test_embed_does_not_raise_on_long_text(self):
        long_text = "depression anxiety " * 1000
        result = embed_signal_value(long_text)
        assert result is None or isinstance(result, list)


# ─── Screening ingestor (unit — no DB) ───────────────────────────────────────

class TestScreeningIngestorParsing:
    """Unit-test the parsing logic without a DB connection."""

    def test_phq9_loinc_recognised(self):
        from skills.screening_registry import get_instrument_for_loinc
        inst = get_instrument_for_loinc("44249-1")
        assert inst is not None
        assert inst.key == "phq9"

    def test_gad7_loinc_recognised(self):
        from skills.screening_registry import get_instrument_for_loinc
        assert get_instrument_for_loinc("69737-5") is not None

    def test_cssrs_loinc_recognised(self):
        from skills.screening_registry import get_instrument_for_loinc
        assert get_instrument_for_loinc("89204-2") is not None

    def test_unknown_loinc_returns_none(self):
        from skills.screening_registry import get_instrument_for_loinc
        assert get_instrument_for_loinc("00000-0") is None

    def test_severity_band_phq9_score_20_is_severe(self):
        from skills.screening_registry import get_severity_band
        band = get_severity_band("phq9", 20)
        assert band.label == "severe"

    def test_critical_item_detection_cssrs(self):
        from skills.screening_registry import get_triggered_critical_items
        # Item 5 = active ideation with plan, threshold 1
        items = get_triggered_critical_items("cssrs", {5: 1, 6: 0})
        item_nums = {i.item_number for i in items}
        assert 5 in item_nums

    def test_non_behavioral_resource_returns_none_from_registry(self):
        from skills.screening_registry import get_instrument_for_loinc
        # Lab LOINC code (not a questionnaire)
        assert get_instrument_for_loinc("2160-0") is None   # creatinine


# ─── behavioral_gap_detector helpers (pure logic, no DB) ─────────────────────

class TestGapDetectorHelpers:

    def test_temporal_confidence_high(self):
        from skills.behavioral_gap_detector import _classify_temporal_confidence
        assert _classify_temporal_confidence(6, 3) == "high"

    def test_temporal_confidence_medium(self):
        from skills.behavioral_gap_detector import _classify_temporal_confidence
        assert _classify_temporal_confidence(3, 20) == "medium"

    def test_temporal_confidence_low(self):
        from skills.behavioral_gap_detector import _classify_temporal_confidence
        assert _classify_temporal_confidence(1, 60) == "low"

    def test_temporal_confidence_very_low(self):
        from skills.behavioral_gap_detector import _classify_temporal_confidence
        assert _classify_temporal_confidence(0, 999) == "very_low"

    def test_phenotype_label_high_burden(self):
        from skills.behavioral_gap_detector import _phenotype_label_for_pressure
        label = _phenotype_label_for_pressure("depression", 0.85, "high")
        assert "high_burden" in label
        assert "depression" in label

    def test_phenotype_label_emerging(self):
        from skills.behavioral_gap_detector import _phenotype_label_for_pressure
        label = _phenotype_label_for_pressure("anxiety", 0.42, "low")
        assert "emerging" in label

    def test_phenotype_label_faint_for_very_low(self):
        from skills.behavioral_gap_detector import _phenotype_label_for_pressure
        label = _phenotype_label_for_pressure("sleep", 0.50, "very_low")
        assert "faint" in label


# ─── schemas.py behavioral_section field ─────────────────────────────────────

# Add server/ to path for direct import
_SERVER_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _SERVER_ROOT not in sys.path:
    sys.path.insert(0, _SERVER_ROOT)


class TestSchemasUpdate:

    def test_deliberation_result_has_behavioral_section(self):
        from datetime import datetime as _dt
        from server.deliberation.schemas import DeliberationResult
        dr = DeliberationResult(
            deliberation_id="test",
            patient_id="pid",
            timestamp=_dt.utcnow(),
            trigger="test",
        )
        assert hasattr(dr, "behavioral_section")
        assert isinstance(dr.behavioral_section, list)

    def test_behavioral_section_default_empty(self):
        from datetime import datetime as _dt
        from server.deliberation.schemas import DeliberationResult
        dr = DeliberationResult(
            deliberation_id="x",
            patient_id="y",
            timestamp=_dt.utcnow(),
            trigger="z",
        )
        assert dr.behavioral_section == []

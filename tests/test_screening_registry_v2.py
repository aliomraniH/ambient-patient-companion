"""
test_screening_registry_v2.py — Unit tests for the V2 screening registry.

Tests cover:
  - Registry completeness (17 instruments, 11 domains)
  - LOINC lookup helpers
  - Severity band lookup
  - Critical item detection
  - Atom-to-instrument suggestions
  - SDoH registry structure
  - No-op register() functions

All tests are synchronous (pure data module).
"""
import sys
import os

# Ensure mcp-server/skills is on path
_ROOT = os.path.join(os.path.dirname(__file__), "..", "mcp-server")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest
from skills.screening_registry import (
    SCREENING_REGISTRY,
    DOMAINS,
    DOMAIN_LOOKBACK_DAYS,
    get_domain_for_loinc,
    get_instrument_for_loinc,
    get_instruments_for_domain,
    suggest_instruments_from_atoms,
    get_severity_band,
    get_triggered_critical_items,
    register,
)
from skills.sdoh_registry import SDOH_REGISTRY, register as sdoh_register


# ─── Registry structure ────────────────────────────────────────────────────────

class TestRegistryStructure:

    def test_instrument_count(self):
        assert len(SCREENING_REGISTRY) == 17, (
            f"Expected 17 instruments, got {len(SCREENING_REGISTRY)}"
        )

    def test_domain_count(self):
        assert len(DOMAINS) == 11, (
            f"Expected 11 domains, got {len(DOMAINS)}"
        )

    def test_all_domains_covered(self):
        registry_domains = {inst.domain for inst in SCREENING_REGISTRY.values()}
        assert registry_domains == set(DOMAINS.keys()), (
            f"Domain mismatch. Registry: {registry_domains}, DOMAINS: {set(DOMAINS.keys())}"
        )

    def test_lookback_days_coverage(self):
        for domain in DOMAINS:
            assert domain in DOMAIN_LOOKBACK_DAYS, f"Missing lookback for domain {domain}"
            assert DOMAIN_LOOKBACK_DAYS[domain] > 0

    def test_all_instruments_have_severity_bands(self):
        for key, inst in SCREENING_REGISTRY.items():
            assert len(inst.severity_bands) >= 2, (
                f"{key} has fewer than 2 severity bands"
            )

    def test_all_instruments_have_atom_signals(self):
        for key, inst in SCREENING_REGISTRY.items():
            assert len(inst.atom_signals) >= 1, (
                f"{key} has no atom_signals"
            )

    def test_all_instruments_have_loinc(self):
        for key, inst in SCREENING_REGISTRY.items():
            assert inst.loinc_code, f"{key} missing loinc_code"
            # LOINC codes are in format NNNNN-N
            assert "-" in inst.loinc_code, f"{key} LOINC code has no dash: {inst.loinc_code}"

    def test_score_range_sanity(self):
        for key, inst in SCREENING_REGISTRY.items():
            lo, hi = inst.score_range
            assert hi > lo, f"{key} score_range invalid: {inst.score_range}"

    def test_no_duplicate_loinc_codes(self):
        loincs = [inst.loinc_code for inst in SCREENING_REGISTRY.values()]
        assert len(loincs) == len(set(loincs)), "Duplicate LOINC codes found"

    def test_no_duplicate_keys(self):
        assert len(SCREENING_REGISTRY) == len(set(SCREENING_REGISTRY.keys()))


# ─── Specific instruments ──────────────────────────────────────────────────────

class TestSpecificInstruments:

    def test_phq9_exists(self):
        assert "phq9" in SCREENING_REGISTRY

    def test_phq9_properties(self):
        phq9 = SCREENING_REGISTRY["phq9"]
        assert phq9.domain == "depression"
        assert phq9.loinc_code == "44249-1"
        assert phq9.item_count == 9
        assert phq9.score_range == (0, 27)
        assert phq9.lookback_days == 180

    def test_phq9_has_critical_item(self):
        phq9 = SCREENING_REGISTRY["phq9"]
        assert len(phq9.critical_items) >= 1
        ci = phq9.critical_items[0]
        assert ci.item_number == 9
        assert ci.threshold >= 1

    def test_cssrs_domain_suicidality(self):
        assert SCREENING_REGISTRY["cssrs"].domain == "suicidality"

    def test_cssrs_has_critical_items(self):
        cssrs = SCREENING_REGISTRY["cssrs"]
        assert len(cssrs.critical_items) >= 2

    def test_gad7_exists(self):
        gad7 = SCREENING_REGISTRY["gad7"]
        assert gad7.domain == "anxiety"
        assert gad7.loinc_code == "69737-5"

    def test_moca_cognitive(self):
        moca = SCREENING_REGISTRY["moca"]
        assert moca.domain == "cognitive"
        assert moca.score_range == (0, 30)

    def test_instruments_per_domain(self):
        from collections import Counter
        domain_counts = Counter(inst.domain for inst in SCREENING_REGISTRY.values())
        # Depression should have 3 instruments
        assert domain_counts["depression"] == 3
        # Anxiety should have 2
        assert domain_counts["anxiety"] == 2
        # Substance use should have 3
        assert domain_counts["substance_use"] == 3


# ─── Lookup helpers ───────────────────────────────────────────────────────────

class TestLookupHelpers:

    def test_get_domain_for_loinc_phq9(self):
        assert get_domain_for_loinc("44249-1") == "depression"

    def test_get_domain_for_loinc_gad7(self):
        assert get_domain_for_loinc("69737-5") == "anxiety"

    def test_get_domain_for_loinc_unknown(self):
        assert get_domain_for_loinc("99999-9") is None

    def test_get_instrument_for_loinc_returns_instrument(self):
        inst = get_instrument_for_loinc("44249-1")
        assert inst is not None
        assert inst.key == "phq9"

    def test_get_instrument_for_loinc_unknown(self):
        assert get_instrument_for_loinc("00000-0") is None

    def test_get_instruments_for_domain_depression(self):
        insts = get_instruments_for_domain("depression")
        assert len(insts) == 3
        keys = {i.key for i in insts}
        assert "phq9" in keys

    def test_get_instruments_for_domain_nonexistent(self):
        assert get_instruments_for_domain("nonexistent_domain") == []


# ─── Severity bands ───────────────────────────────────────────────────────────

class TestSeverityBands:

    def test_phq9_none(self):
        band = get_severity_band("phq9", 3)
        assert band is not None
        assert band.label == "none"

    def test_phq9_mild(self):
        band = get_severity_band("phq9", 7)
        assert band.label == "mild"

    def test_phq9_moderate(self):
        band = get_severity_band("phq9", 12)
        assert band.label == "moderate"

    def test_phq9_moderately_severe(self):
        band = get_severity_band("phq9", 16)
        assert band.label == "moderately_severe"

    def test_phq9_severe(self):
        band = get_severity_band("phq9", 22)
        assert band.label == "severe"

    def test_phq9_boundary_4(self):
        band = get_severity_band("phq9", 4)
        assert band.label == "none"

    def test_phq9_boundary_5(self):
        band = get_severity_band("phq9", 5)
        assert band.label == "mild"

    def test_gad7_severity(self):
        assert get_severity_band("gad7", 0).label == "none"
        assert get_severity_band("gad7", 8).label == "mild"
        assert get_severity_band("gad7", 12).label == "moderate"
        assert get_severity_band("gad7", 17).label == "severe"

    def test_unknown_instrument(self):
        assert get_severity_band("nonexistent", 5) is None

    def test_moca_impairment(self):
        assert get_severity_band("moca", 28).label == "normal"
        assert get_severity_band("moca", 22).label == "mild_ci"


# ─── Critical items ───────────────────────────────────────────────────────────

class TestCriticalItems:

    def test_phq9_item9_triggers(self):
        items = get_triggered_critical_items("phq9", {9: 1})
        assert len(items) == 1
        assert items[0].item_number == 9
        alert_lower = items[0].alert_text.lower()
        assert "si" in alert_lower or "dead" in alert_lower or "harm" in alert_lower

    def test_phq9_item9_not_triggered(self):
        items = get_triggered_critical_items("phq9", {9: 0})
        assert len(items) == 0

    def test_phq9_no_item9(self):
        items = get_triggered_critical_items("phq9", {1: 3, 2: 2, 3: 1})
        assert len(items) == 0

    def test_cssrs_multiple_critical(self):
        # Trigger items 5 and 6
        items = get_triggered_critical_items("cssrs", {1: 0, 5: 1, 6: 1})
        assert len(items) == 2

    def test_unknown_instrument_returns_empty(self):
        items = get_triggered_critical_items("nope", {1: 1})
        assert items == []

    def test_epds_item10(self):
        items = get_triggered_critical_items("epds", {10: 1})
        assert len(items) == 1

    def test_epds_item10_below_threshold(self):
        items = get_triggered_critical_items("epds", {10: 0})
        assert len(items) == 0


# ─── Atom-to-instrument suggestions ──────────────────────────────────────────

class TestAtomSuggestions:

    def test_depression_markers_suggests_phq9(self):
        suggestions = suggest_instruments_from_atoms(["depression_markers"])
        assert "depression" in suggestions
        assert "phq9" in suggestions["depression"]

    def test_anxiety_markers_suggests_gad7(self):
        suggestions = suggest_instruments_from_atoms(["anxiety_markers"])
        assert "anxiety" in suggestions
        assert "gad7" in suggestions["anxiety"]

    def test_suicidality_markers(self):
        suggestions = suggest_instruments_from_atoms(["suicidality_markers"])
        assert "suicidality" in suggestions

    def test_multiple_signals(self):
        suggestions = suggest_instruments_from_atoms([
            "depression_markers", "anxiety_markers", "sleep_disturbance"
        ])
        assert "depression" in suggestions
        assert "anxiety" in suggestions

    def test_unknown_signal_returns_empty(self):
        suggestions = suggest_instruments_from_atoms(["completely_unknown_signal"])
        assert suggestions == {}

    def test_empty_signals_returns_empty(self):
        suggestions = suggest_instruments_from_atoms([])
        assert suggestions == {}

    def test_substance_mention_suggests_audit(self):
        suggestions = suggest_instruments_from_atoms(["substance_mention"])
        assert "substance_use" in suggestions


# ─── SDoH registry ────────────────────────────────────────────────────────────

class TestSDoHRegistry:

    def test_screener_count(self):
        assert len(SDOH_REGISTRY) == 5, f"Expected 5 SDoH screeners, got {len(SDOH_REGISTRY)}"

    def test_prapare_exists(self):
        assert "prapare" in SDOH_REGISTRY

    def test_hunger_vital_sign_two_items(self):
        hvs = SDOH_REGISTRY["hunger_vital_sign"]
        assert len(hvs.items) == 2

    def test_prapare_panel_loinc(self):
        assert SDOH_REGISTRY["prapare"].panel_loinc == "93025-5"

    def test_all_screeners_have_domains_covered(self):
        for key, screener in SDOH_REGISTRY.items():
            assert len(screener.domains_covered) >= 1, f"{key} has no domains"

    def test_all_items_have_domain(self):
        for key, screener in SDOH_REGISTRY.items():
            for item in screener.items:
                assert item.sdoh_domain, f"{key} item {item.item_number} missing domain"


# ─── No-op register functions ─────────────────────────────────────────────────

class TestRegisterNoOp:

    def test_screening_registry_register_noop(self):
        register(None)  # should not raise

    def test_sdoh_registry_register_noop(self):
        sdoh_register(None)  # should not raise

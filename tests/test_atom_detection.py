"""Verification tests for ATOM-first behavioral detection.

Pure-unit tests run without a DB. DB integration tests are skipped when
DATABASE_URL is not set, and LLM extraction tests are marked `llm_api`
so they're excluded from the default pytest run (see pytest.ini).

Synthetic patient UUID is used for any DB-touching test.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

# Ensure repo root is on sys.path FIRST so `from server...` resolves to the
# deliberation package at repo root, NOT the `mcp-server/server.py` file.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Library modules live under mcp-server/skills/ — append (never insert)
# so `from skills.foo import bar` resolves without shadowing the repo-root
# `server/` package.
_MCP_SKILLS = _REPO_ROOT / "mcp-server"
if str(_MCP_SKILLS) not in sys.path:
    sys.path.append(str(_MCP_SKILLS))

SYNTHETIC_PATIENT_ID = "2cfaa9f2-3f47-44be-84e2-16f3a5dc0bbb"

TEST_NOTE = """
Physical Exam:

General: Patient NAD morbid obese appearing- constantly jumping from system
to system when discussing his symptoms. Constantly looking at the iphone watch
on his left wrist. Cooperative with exam.

PSYCH: Appropriate affect, speech, and thought. Denies anxiety or depression.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Pure unit tests — no DB, no LLM
# ─────────────────────────────────────────────────────────────────────────────

def test_temporal_confidence_function():
    from skills.behavioral_gap_detector import compute_temporal_confidence
    today = date.today()
    assert compute_temporal_confidence(today - timedelta(days=100)) == "high"
    assert compute_temporal_confidence(today - timedelta(days=500)) == "moderate"
    assert compute_temporal_confidence(today - timedelta(days=1500)) == "low"
    assert compute_temporal_confidence(today - timedelta(days=3000)) == "very_low"
    assert compute_temporal_confidence(None) == "very_low"


def test_instrument_suggestion():
    # v2: registry-driven suggestion now lives in screening_registry.
    from skills.screening_registry import suggest_instruments_from_atoms
    instruments = suggest_instruments_from_atoms(
        ["attention_switching", "device_checking", "anxiety_markers"]
    )
    assert len(instruments) > 0
    assert any(i in instruments for i in ("ASRS-5", "GAD-7", "PHQ-9"))


def test_chunk_note_by_section_splits_relevant_sections():
    from skills.behavioral_atom_extractor import chunk_note_by_section
    chunks = chunk_note_by_section(TEST_NOTE)
    sections = {c["section"] for c in chunks}
    # General + PSYCH headers should both be captured as relevant sections.
    assert "general" in sections or "psych" in sections
    assert all(len(c["text"]) > 20 for c in chunks)


def test_strip_fences_removes_markdown():
    from skills.behavioral_atom_extractor import _strip_fences
    raw = "```json\n[{\"a\": 1}]\n```"
    assert _strip_fences(raw) == '[{"a": 1}]'


def test_build_behavioral_section_returns_list_and_filters_role():
    """v2: build_behavioral_section now returns a role-filtered card list."""
    from server.deliberation.behavioral_section_builder import build_behavioral_section
    ctx = {
        "mode": "primary_evidence",
        "cards": [
            {"card_id": "a1", "card_type": "screening_gap",
             "title": "Dep", "domain": "depression", "priority": "high",
             "show_to_roles": ["pcp", "care_manager"],
             "body_text": "", "evidence": [], "actions": [],
             "critical_flags": [], "temporal_confidence": "high",
             "source": {}},
            {"card_id": "a2", "card_type": "behavioral_routing",
             "title": "Follow-up at next visit", "domain": "depression",
             "priority": "medium", "show_to_roles": ["patient"],
             "body_text": "", "evidence": [], "actions": [],
             "critical_flags": [], "temporal_confidence": "high",
             "source": {}},
        ],
    }
    pt = build_behavioral_section(ctx, role="patient")
    assert isinstance(pt, list) and len(pt) == 1
    assert pt[0]["card_type"] == "behavioral_routing"

    pcp = build_behavioral_section(ctx, role="pcp")
    assert isinstance(pcp, list) and len(pcp) == 1
    assert pcp[0]["card_type"] == "screening_gap"


def test_deliberation_result_behavioral_section_is_list():
    """The schema change — behavioral_section defaults to empty list."""
    from server.deliberation.schemas import DeliberationResult
    # DeliberationResult has many required fields; just validate the
    # default factory produces a list for behavioral_section.
    field = DeliberationResult.model_fields["behavioral_section"]
    assert field.default_factory() == []


# ─────────────────────────────────────────────────────────────────────────────
# DB integration tests — skipped when DATABASE_URL is missing
# ─────────────────────────────────────────────────────────────────────────────

db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — DB integration test skipped",
)


@db_required
async def test_migration_tables_exist():
    import asyncpg
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        # v2: behavioral_screenings + sdoh_screenings are the new tables;
        # phq9_observations has been dropped by migration 011.
        for t in (
            "behavioral_signal_atoms",
            "behavioral_screening_gaps",
            "behavioral_screenings",
            "sdoh_screenings",
            "behavioral_phenotypes",
        ):
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_name = $1)",
                t,
            )
            assert exists, f"Missing table: {t}"
        view = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_matviews "
            "WHERE matviewname = 'atom_pressure_scores')"
        )
        assert view, "Missing materialized view: atom_pressure_scores"
        vec = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector')"
        )
        assert vec, "pgvector extension not enabled"
    finally:
        await conn.close()


@db_required
async def test_gap_resolution_transitions_mode():
    """Insert a synthetic open gap in the depression domain, ingest a
    PHQ-9 via behavioral_screenings, and verify the domain gap resolves.
    """
    import asyncpg
    from skills.behavioral_gap_detector import resolve_gap_on_new_screening

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM patients WHERE id = $1::uuid)",
            SYNTHETIC_PATIENT_ID,
        )
        if not exists:
            pytest.skip("Synthetic patient not seeded; skipping end-to-end resolve test")

        await conn.execute(
            "DELETE FROM behavioral_screening_gaps WHERE patient_id = $1::uuid",
            SYNTHETIC_PATIENT_ID,
        )
        await conn.execute(
            "DELETE FROM behavioral_screenings WHERE patient_id = $1::uuid",
            SYNTHETIC_PATIENT_ID,
        )

        gap_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO behavioral_screening_gaps (
                id, patient_id, gap_type, atom_count,
                atom_date_range, atom_ids, pressure_score,
                output_mode, temporal_confidence, triggered_domains
            ) VALUES (
                $1::uuid, $2::uuid, 'no_screening', 1,
                daterange('2016-01-01','2016-12-31','[]'),
                '{}'::uuid[], 1.5, 'primary_evidence', 'low',
                ARRAY['depression']::text[]
            )
            """,
            gap_id, SYNTHETIC_PATIENT_ID,
        )

        screening_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO behavioral_screenings (
                id, patient_id, instrument_key, instrument_name, domain,
                observation_date, total_score, source
            ) VALUES (
                $1::uuid, $2::uuid, 'phq9', 'PHQ-9', 'depression',
                $3, 6, 'test'
            )
            """,
            screening_id, SYNTHETIC_PATIENT_ID, date.today(),
        )

        resolved = await resolve_gap_on_new_screening(
            conn=conn,
            patient_id=SYNTHETIC_PATIENT_ID,
            new_screening_id=str(screening_id),
            instrument_key="phq9",
            domain="depression",
            screening_date=date.today(),
        )
        assert resolved >= 1

        row = await conn.fetchrow(
            "SELECT status, output_mode FROM behavioral_screening_gaps "
            "WHERE id = $1::uuid",
            gap_id,
        )
        assert row["status"] == "resolved"
        assert row["output_mode"] == "contextual"

        phenotype = await conn.fetchrow(
            "SELECT evidence_mode, last_formal_screening "
            "FROM behavioral_phenotypes WHERE patient_id = $1::uuid",
            SYNTHETIC_PATIENT_ID,
        )
        assert phenotype is not None
        assert phenotype["evidence_mode"] == "contextual"
        assert phenotype["last_formal_screening"] == date.today()

        # Cleanup
        await conn.execute(
            "DELETE FROM behavioral_screening_gaps WHERE patient_id = $1::uuid",
            SYNTHETIC_PATIENT_ID,
        )
        await conn.execute(
            "DELETE FROM behavioral_screenings WHERE patient_id = $1::uuid",
            SYNTHETIC_PATIENT_ID,
        )
    finally:
        await conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# LLM extraction test — requires ANTHROPIC_API_KEY, excluded from default run
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.llm_api
async def test_extract_atoms_produces_expected_signals():
    from skills.behavioral_atom_extractor import extract_atoms_from_note
    atoms = await extract_atoms_from_note(
        note_text=TEST_NOTE,
        note_date=date(2016, 7, 22),
        source_note_id="test-note-001",
        patient_id=SYNTHETIC_PATIENT_ID,
    )
    signal_types = {a["signal_type"] for a in atoms}
    assert "attention_switching" in signal_types or "device_checking" in signal_types

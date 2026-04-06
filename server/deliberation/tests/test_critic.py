"""Tests for the critic module (Phase 2)."""
from server.deliberation.schemas import (
    ClaimWithConfidence, RevisedAnalysis, CrossCritique, CritiqueItem
)
from server.deliberation.critic import _compute_convergence, _analysis_from_revision


def test_partial_convergence():
    a = RevisedAnalysis(
        model_id="claude", round_number=1,
        revised_findings=[
            ClaimWithConfidence(claim="hba1c above target", confidence=0.9),
            ClaimWithConfidence(claim="bp trending up", confidence=0.8),
            ClaimWithConfidence(claim="unique to claude", confidence=0.7),
        ],
        revisions_made=[], maintained_positions=[], raw_revision=""
    )
    b = RevisedAnalysis(
        model_id="gpt4", round_number=1,
        revised_findings=[
            ClaimWithConfidence(claim="hba1c above target", confidence=0.9),
            ClaimWithConfidence(claim="bp trending up", confidence=0.8),
            ClaimWithConfidence(claim="unique to gpt4", confidence=0.6),
        ],
        revisions_made=[], maintained_positions=[], raw_revision=""
    )
    score = _compute_convergence(a, b)
    assert 0.0 < score < 1.0
    assert score == pytest.approx(2 / 4)  # 2 shared out of 4 unique


def test_cross_critique_schema():
    critique = CrossCritique(
        critic_model="claude",
        target_model="gpt4",
        round_number=1,
        critique_items=[
            CritiqueItem(
                target_claim="BP is fine",
                critique_type="factual_error",
                critique_text="BP is 141/86, above 130/80 target",
                severity="blocking"
            )
        ],
        areas_of_agreement=["HbA1c needs attention"],
        raw_critique="Full critique text"
    )
    assert len(critique.critique_items) == 1
    assert critique.critique_items[0].severity == "blocking"


import pytest

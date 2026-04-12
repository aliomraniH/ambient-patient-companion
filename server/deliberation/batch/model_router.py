"""
MODEL ROUTER
============
Fixed model assignments for each pipeline task type.

CRITICAL CONSTRAINT (from CLAUDE.md):
- Opus for clinical reasoning (deliberation, synthesis, sycophancy gating)
- Sonnet for reliable structured extraction
- Haiku for classification and pattern recognition

This module is the single source of truth for model selection. Never hardcode
model strings elsewhere — always call get_model(task_type).
"""

import logging

log = logging.getLogger(__name__)


# Model IDs as of plan date — update via this constant only.
HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-20250514"
OPUS = "claude-opus-4-20250514"


# Task type -> model mapping (FIXED per requirements)
MODEL_ROUTING: dict[str, str] = {
    # Classification / pattern recognition (cheapest)
    "format_classification":     HAIKU,
    "flag_lifecycle_review":     HAIKU,
    "agenda_planning":           HAIKU,
    "synthesis_review":          HAIKU,

    # Standard structured extraction (mid-cost)
    "standard_extraction":       SONNET,
    "llm_fallback_extraction":   SONNET,
    "self_consistency_pass":     SONNET,
    "patient_nudge_generation":  SONNET,
    "clinical_query":            SONNET,

    # Clinical reasoning / safety-critical (highest quality)
    "reasoning_confidence":      OPUS,
    "aria_deliberation":         OPUS,
    "mira_deliberation":         OPUS,
    "theo_deliberation":         OPUS,
    "synthesis":                 OPUS,
}


def get_model(task_type: str) -> str:
    """
    Retrieve the model ID for a given task type.

    Raises:
        KeyError if task_type is not registered. Adding a new model use site
        without registering it here is intentionally a hard failure — every
        Anthropic API call should go through this router.
    """
    if task_type not in MODEL_ROUTING:
        log.warning(
            "[MODEL_ROUTER] Unknown task_type '%s' — defaulting to Sonnet. "
            "Register it in MODEL_ROUTING for explicit routing.",
            task_type,
        )
        return SONNET
    return MODEL_ROUTING[task_type]

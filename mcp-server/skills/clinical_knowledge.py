"""Skill: clinical_knowledge — external clinical knowledge search.

Searches OpenFDA, RxNorm, PubMed and other external clinical knowledge
sources when an agent's internal knowledge or compiled context is
insufficient. Results are cached with clinically-appropriate TTLs.
"""
from __future__ import annotations

import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Add repo root to sys.path so we can import gap_aware
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastmcp import FastMCP
from gap_aware.knowledge_searcher import run_knowledge_search


async def search_clinical_knowledge(
    query: str,
    query_type: str,
    sources_to_search: str = '["openfda", "rxnorm", "pubmed"]',
    patient_context: str = "{}",
    evidence_level_minimum: str = "any",
    max_results_per_source: int = 5,
    gap_id: str = "",
) -> str:
    """Search external clinical knowledge sources when an agent's internal
    knowledge or compiled context is insufficient.

    Routes to RxNorm, OpenFDA, PubMed based on query_type.
    Results are cached with clinically-appropriate TTLs.

    Args:
        query: Free-text clinical query
        query_type: drug_interaction | guideline_recommendation | lab_reference_range |
                    contraindication | dosing_adjustment | differential_diagnosis |
                    screening_schedule
        sources_to_search: JSON array of sources: openfda, rxnorm, dailymed,
                           pubmed, clinicaltrials, snomed, loinc
        patient_context: JSON object with patient demographic context
        evidence_level_minimum: Minimum evidence level filter
        max_results_per_source: Max results per source
        gap_id: Optional gap_id to associate results with
    """
    try:
        sources = json.loads(sources_to_search) if isinstance(sources_to_search, str) else sources_to_search
    except (json.JSONDecodeError, TypeError):
        sources = ["openfda", "rxnorm", "pubmed"]

    try:
        ctx = json.loads(patient_context) if isinstance(patient_context, str) else patient_context
    except (json.JSONDecodeError, TypeError):
        ctx = {}

    result = await run_knowledge_search(
        query=query,
        query_type=query_type,
        sources=sources,
        patient_context=ctx,
        max_per_source=max_results_per_source,
        gap_id=gap_id or None,
    )
    return json.dumps(result, default=str)


def register(mcp: FastMCP) -> None:
    """Register the search_clinical_knowledge tool with the MCP server."""
    mcp.tool(search_clinical_knowledge)

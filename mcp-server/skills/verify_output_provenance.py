"""Skill wrapper: register the shared verify_output_provenance tool on
the ambient-skills-companion server.

Auto-discovered by mcp-server/skills/__init__.py:load_skills().
"""
from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Add repo root to sys.path so we can import shared.provenance
# (mirrors pattern in clinical_knowledge.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from db.connection import get_pool  # noqa: E402
from shared.provenance.tool_adapter import register_provenance_tool  # noqa: E402


def register(mcp) -> None:
    register_provenance_tool(
        mcp,
        source_server="ambient-skills-companion",
        get_pool=get_pool,
    )

"""HealthEx MCP adapter (Track B) — Phase 2 only.

This adapter is a placeholder for Phase 2 HealthEx integration.
Do not activate in Phase 1.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from ingestion.adapters.base import BaseAdapter, PatientRecord

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


class HealthExAdapter(BaseAdapter):
    """Track B adapter: HealthEx MCP caller (Phase 2 only)."""

    source_name: str = "healthex"

    async def parse_bundle(
        self,
        fhir_bundle: dict[str, Any],
        augment_wearables: bool = True,
        augment_behavioral: bool = True,
    ) -> PatientRecord:
        raise NotImplementedError("HealthEx adapter is Phase 2 only")

    async def load_all_patients(self, directory: str) -> list[PatientRecord]:
        raise NotImplementedError("HealthEx adapter is Phase 2 only")

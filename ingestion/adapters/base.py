"""Base adapter: PatientRecord dataclass and BaseAdapter ABC for the ingestion pipeline."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PatientRecord:
    """Canonical patient representation for the ingestion pipeline.

    Every adapter produces PatientRecord instances regardless of data source.
    """

    patient_ref_id: str = ""
    source_track: str = "synthea"
    fhir_bundle: dict[str, Any] = field(default_factory=dict)
    wearable_data: list[dict[str, Any]] = field(default_factory=list)
    behavioral_signals: list[dict[str, Any]] = field(default_factory=list)


class BaseAdapter(ABC):
    """Abstract base class for data-track adapters."""

    source_name: str = "unknown"

    @abstractmethod
    async def parse_bundle(
        self,
        fhir_bundle: dict[str, Any],
        augment_wearables: bool = True,
        augment_behavioral: bool = True,
    ) -> PatientRecord:
        """Parse a FHIR Bundle into a PatientRecord."""
        ...

    @abstractmethod
    async def load_all_patients(self, directory: str) -> list[PatientRecord]:
        """Load all patient records from the given directory."""
        ...

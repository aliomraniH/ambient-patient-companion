"""Base adapter: PatientRecord dataclass and BaseAdapter ABC."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class PatientRecord:
    """Canonical patient representation shared across all data tracks."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    mrn: str = ""
    first_name: str = ""
    last_name: str = ""
    birth_date: Optional[date] = None
    gender: str = ""
    race: str = ""
    ethnicity: str = ""
    address_line: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    conditions: list[dict] = field(default_factory=list)
    medications: list[dict] = field(default_factory=list)
    allergies: list[dict] = field(default_factory=list)
    is_synthetic: bool = True


class BaseAdapter(ABC):
    """Abstract base class for data-track adapters."""

    @abstractmethod
    def load_patients(self) -> list[PatientRecord]:
        """Load patient records from the data source."""
        ...

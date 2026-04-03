"""Social Determinants of Health (SDoH) flag generator."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

import numpy as np


SDOH_DOMAINS = [
    "food_access",
    "housing_instability",
    "transportation",
    "social_isolation",
    "financial_strain",
]

SEVERITY_LEVELS = ["low", "moderate", "high"]


def _patient_seed(patient_id: str, base_seed: int = 42) -> int:
    h = hashlib.sha256(f"{patient_id}:{base_seed}".encode()).hexdigest()
    return int(h[:8], 16)


def generate_sdoh_flags(
    patient_id: str,
    seed: int = 42,
    screening_date: date | None = None,
) -> list[dict]:
    """Generate SDoH flags for a patient.

    Each patient gets 1-3 randomly selected domains with varying severity.
    """
    rng = np.random.default_rng(_patient_seed(patient_id, seed))

    if screening_date is None:
        screening_date = date.today()

    num_flags = int(rng.integers(1, 4))  # 1-3 flags
    selected_domains = rng.choice(SDOH_DOMAINS, size=num_flags, replace=False)

    results: list[dict] = []
    for domain in selected_domains:
        severity_idx = int(rng.integers(0, 3))
        results.append({
            "patient_id": patient_id,
            "domain": str(domain),
            "severity": SEVERITY_LEVELS[severity_idx],
            "screening_date": screening_date,
            "notes": f"Auto-generated SDoH flag for {domain}",
        })

    return results

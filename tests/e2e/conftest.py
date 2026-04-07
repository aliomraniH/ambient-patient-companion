"""Shared fixtures for the end-to-end MCP use-case test suite.

Sets up:
  - A session-scoped asyncpg pool
  - A session-scoped "maria_chen" fixture that runs PatientDataEntryAgent once
    and returns a dict with patient_id and seed summary for all 15 tests to share
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

_has_db = "DATABASE_URL" in os.environ


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def db_pool():
    if not _has_db:
        pytest.skip("DATABASE_URL not set — skipping e2e tests")
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    yield pool
    await pool.close()


@pytest_asyncio.fixture(scope="session")
async def maria_chen(db_pool):
    """Run PatientDataEntryAgent once per session; all tests share the seeded data."""
    from tests.e2e.data_entry_agent import PatientDataEntryAgent

    agent = PatientDataEntryAgent(db_pool)
    patient_id = await agent.setup_patient()
    summary = await agent.seed_all(patient_id)

    yield {
        "patient_id": patient_id,
        "db_pool": db_pool,
        **summary,
    }

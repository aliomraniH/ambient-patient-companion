"""LoRA training runs end-to-end tests.

Verifies that:
- A seeded row in lora_training_runs is returned correctly by get_lora_training_status
- All required fields are present (job_id, status, triggered_at, base_model, dataset_path)
- An unknown job_id returns a not_found response
- triggered_at is returned as an ISO-8601 string (not a raw datetime object)

All tests seed rows directly via asyncpg, patch db.connection.get_pool to use
the test pool, and clean up after themselves.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from skills.slm_inference import _handle_modal_webhook_internal, get_lora_training_status


# ── Helpers ───────────────────────────────────────────────────────────────────

_BASE_MODEL = "Qwen/Qwen2.5-3B-Instruct"
_DATASET_PATH = "hf://my-org/diabetes-bh-dataset"


async def _seed_training_run(
    conn,
    *,
    job_id: str,
    status: str = "pending",
    base_model: str = _BASE_MODEL,
    dataset_path: str = _DATASET_PATH,
    metadata: dict | None = None,
) -> None:
    """Insert one row into lora_training_runs."""
    await conn.execute(
        """
        INSERT INTO lora_training_runs
            (job_id, status, triggered_at, base_model, dataset_path, metadata)
        VALUES ($1, $2, NOW(), $3, $4, $5)
        ON CONFLICT (job_id) DO NOTHING
        """,
        job_id,
        status,
        base_model,
        dataset_path,
        json.dumps(metadata or {}),
    )


async def _delete_training_run(conn, job_id: str) -> None:
    await conn.execute(
        "DELETE FROM lora_training_runs WHERE job_id = $1",
        job_id,
    )


# ── Tests: get_lora_training_status ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_lora_training_status_field_presence(db_pool):
    """get_lora_training_status must return all required fields for a seeded row."""
    job_id = f"test-job-{uuid.uuid4()}"

    async with db_pool.acquire() as conn:
        await _seed_training_run(conn, job_id=job_id)

    try:
        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            raw = await get_lora_training_status(job_id=job_id)

        result = json.loads(raw)

        required_fields = {"job_id", "status", "triggered_at", "base_model", "dataset_path"}
        for field in required_fields:
            assert field in result, f"Missing required field {field!r} in response"

        assert result["job_id"] == job_id
        assert result["status"] == "pending"
        assert result["base_model"] == _BASE_MODEL
        assert result["dataset_path"] == _DATASET_PATH

    finally:
        async with db_pool.acquire() as conn:
            await _delete_training_run(conn, job_id)


@pytest.mark.asyncio
async def test_get_lora_training_status_not_found(db_pool):
    """get_lora_training_status must return status='not_found' for an unknown job_id."""
    unknown_job_id = f"nonexistent-job-{uuid.uuid4()}"

    with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
        raw = await get_lora_training_status(job_id=unknown_job_id)

    result = json.loads(raw)

    assert result["status"] == "not_found", (
        f"Expected status='not_found' for unknown job_id, got {result['status']!r}"
    )
    assert result["job_id"] == unknown_job_id
    assert "message" in result


@pytest.mark.asyncio
async def test_get_lora_training_status_triggered_at_is_iso_string(db_pool):
    """triggered_at must be returned as an ISO-8601 string, not a raw datetime object."""
    job_id = f"test-job-ts-{uuid.uuid4()}"

    async with db_pool.acquire() as conn:
        await _seed_training_run(conn, job_id=job_id)

    try:
        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            raw = await get_lora_training_status(job_id=job_id)

        result = json.loads(raw)

        triggered_at = result.get("triggered_at")
        assert triggered_at is not None, "triggered_at must not be None for a seeded row"
        assert isinstance(triggered_at, str), (
            f"triggered_at must be a string, got {type(triggered_at)}: {triggered_at!r}"
        )
        parsed = datetime.fromisoformat(triggered_at)
        assert parsed.tzinfo is not None, "triggered_at must be timezone-aware"

    finally:
        async with db_pool.acquire() as conn:
            await _delete_training_run(conn, job_id)


@pytest.mark.asyncio
async def test_get_lora_training_status_optional_fields_present(db_pool):
    """completed_at, error_message, and metadata must be present in the response (may be None)."""
    job_id = f"test-job-opt-{uuid.uuid4()}"

    async with db_pool.acquire() as conn:
        await _seed_training_run(
            conn,
            job_id=job_id,
            status="pending",
            metadata={"epochs": 3, "learning_rate": 0.0002},
        )

    try:
        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            raw = await get_lora_training_status(job_id=job_id)

        result = json.loads(raw)

        assert "completed_at" in result, "completed_at key must be present (may be None)"
        assert "error_message" in result, "error_message key must be present (may be None)"
        assert "metadata" in result, "metadata key must be present"

        assert result["completed_at"] is None, "completed_at should be None for pending job"
        assert result["error_message"] is None, "error_message should be None for pending job"

        meta = result["metadata"]
        assert isinstance(meta, dict), f"metadata must be a dict, got {type(meta)}"
        assert meta.get("epochs") == 3
        assert meta.get("learning_rate") == pytest.approx(0.0002)

    finally:
        async with db_pool.acquire() as conn:
            await _delete_training_run(conn, job_id)


@pytest.mark.asyncio
async def test_get_lora_training_status_different_statuses(db_pool):
    """get_lora_training_status must reflect the stored status value correctly."""
    for status_val in ("pending", "running", "completed", "failed"):
        job_id = f"test-job-{status_val}-{uuid.uuid4()}"

        async with db_pool.acquire() as conn:
            await _seed_training_run(conn, job_id=job_id, status=status_val)

        try:
            with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
                raw = await get_lora_training_status(job_id=job_id)

            result = json.loads(raw)
            assert result["status"] == status_val, (
                f"Expected status={status_val!r}, got {result['status']!r}"
            )
            assert result["job_id"] == job_id

        finally:
            async with db_pool.acquire() as conn:
                await _delete_training_run(conn, job_id)


# ── Tests: _handle_modal_webhook_internal (UPDATE path) ──────────────────────


@pytest.mark.asyncio
async def test_update_lora_run_to_completed(db_pool):
    """Transitioning a pending row to completed must set status and completed_at."""
    job_id = f"test-job-complete-{uuid.uuid4()}"

    async with db_pool.acquire() as conn:
        await _seed_training_run(conn, job_id=job_id, status="pending")

    try:
        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            webhook_result = await _handle_modal_webhook_internal(
                {"job_id": job_id, "status": "completed"}
            )

        assert webhook_result["ok"] is True, f"Expected ok=True, got: {webhook_result}"
        assert webhook_result["job_id"] == job_id
        assert webhook_result["status"] == "completed"
        assert webhook_result["row_updated"] is True

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            raw = await get_lora_training_status(job_id=job_id)

        result = json.loads(raw)
        assert result["status"] == "completed", (
            f"Expected status='completed' after update, got {result['status']!r}"
        )
        assert result["job_id"] == job_id

        completed_at = result.get("completed_at")
        assert completed_at is not None, "completed_at must be set after transitioning to completed"
        assert isinstance(completed_at, str), (
            f"completed_at must be a string, got {type(completed_at)}: {completed_at!r}"
        )
        parsed = datetime.fromisoformat(completed_at)
        assert parsed.tzinfo is not None, "completed_at must be timezone-aware"

        assert result.get("error_message") is None, (
            "error_message must remain None when transitioning to completed without error"
        )

    finally:
        async with db_pool.acquire() as conn:
            await _delete_training_run(conn, job_id)


@pytest.mark.asyncio
async def test_update_lora_run_to_failed(db_pool):
    """Transitioning a pending row to failed must set status, completed_at, and error_message."""
    job_id = f"test-job-failed-{uuid.uuid4()}"
    error_msg = "OOM during forward pass at epoch 2"

    async with db_pool.acquire() as conn:
        await _seed_training_run(conn, job_id=job_id, status="pending")

    try:
        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            webhook_result = await _handle_modal_webhook_internal(
                {"job_id": job_id, "status": "failed", "error_message": error_msg}
            )

        assert webhook_result["ok"] is True, f"Expected ok=True, got: {webhook_result}"
        assert webhook_result["job_id"] == job_id
        assert webhook_result["status"] == "failed"
        assert webhook_result["row_updated"] is True

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            raw = await get_lora_training_status(job_id=job_id)

        result = json.loads(raw)
        assert result["status"] == "failed", (
            f"Expected status='failed' after update, got {result['status']!r}"
        )
        assert result["job_id"] == job_id

        completed_at = result.get("completed_at")
        assert completed_at is not None, "completed_at must be set after transitioning to failed"
        assert isinstance(completed_at, str), (
            f"completed_at must be a string, got {type(completed_at)}: {completed_at!r}"
        )
        parsed = datetime.fromisoformat(completed_at)
        assert parsed.tzinfo is not None, "completed_at must be timezone-aware"

        assert result.get("error_message") == error_msg, (
            f"Expected error_message={error_msg!r}, got {result.get('error_message')!r}"
        )

    finally:
        async with db_pool.acquire() as conn:
            await _delete_training_run(conn, job_id)

"""Tests for slm_bridge.store_slm_insight."""

from __future__ import annotations

import sys
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.mark.asyncio
async def test_store_slm_insight_happy_path():
    """Valid slm_output inserts a row and returns status='ok' with a note_id."""
    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_conn)

    slm_output = {
        "generated_text": "Patient shows elevated HbA1c consistent with poorly controlled T2DM.",
        "model": "Qwen2.5-VL-3B",
        "adapter_type": "cohort",
    }

    with patch("db.connection.get_pool", AsyncMock(return_value=mock_pool)):
        from skills.slm_bridge import store_slm_insight
        result = await store_slm_insight(
            patient_id=str(uuid.uuid4()),
            slm_output=slm_output,
            source_context="medication_summary",
        )

    assert result["status"] == "ok"
    assert "note_id" in result
    mock_conn.execute.assert_called_once()
    # call_args[0] is the positional-args tuple: (sql, note_id, patient_id, note_type, note_text, author, now)
    pos_args = mock_conn.execute.call_args[0]
    assert "slm_medication_summary" in pos_args[3]  # note_type
    assert "Qwen2.5-VL-3B" in pos_args[5]           # author


@pytest.mark.asyncio
async def test_store_slm_insight_empty_text():
    """Empty generated_text returns error without touching the DB."""
    from skills.slm_bridge import store_slm_insight

    for empty in ["", "   ", None]:
        result = await store_slm_insight(
            patient_id=str(uuid.uuid4()),
            slm_output={"generated_text": empty, "model": "m", "adapter_type": "base"},
        )
        assert result["status"] == "error"
        assert "empty" in result["reason"].lower()


@pytest.mark.asyncio
async def test_store_slm_insight_db_failure():
    """DB exception is caught and returned as error dict — never raises."""
    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock(side_effect=RuntimeError("connection refused"))

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_conn)

    with patch("db.connection.get_pool", AsyncMock(return_value=mock_pool)):
        from skills.slm_bridge import store_slm_insight
        result = await store_slm_insight(
            patient_id=str(uuid.uuid4()),
            slm_output={"generated_text": "Some insight.", "model": "m", "adapter_type": "base"},
        )

    assert result["status"] == "error"
    assert "connection refused" in result["reason"]

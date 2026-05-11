"""SLM audit log end-to-end tests.

Verifies that:
- get_slm_inference_log returns correct shape with no raw prompt text
- prompt_hash is a 16-character hex string
- patient_id is returned as a string UUID or None
- _get_slm_status_impl reflects seeded rows in inference_24h.call_count
- call_slm actually writes a row to slm_inference_log (write-path test)

All tests seed/write rows directly via asyncpg, patch db.connection.get_pool
to use the test pool, and clean up after themselves.

The manage_hf_endpoint call inside _get_slm_status_impl is stubbed with a
fixed JSON response so the status tests are fully deterministic regardless of
whether HF credentials are present.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from skills.slm_inference import (
    _get_slm_status_impl,
    _prompt_hash,
    call_slm,
    get_slm_inference_log,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

_SEED_PROMPTS = [
    "What are the latest glucose readings for this patient?",
    "Summarise the care gaps for the diabetes cohort.",
    "Is the patient's blood pressure within target range?",
]

_SEED_ADAPTER_TYPES = ["base", "cohort", "patient"]

_STUB_ENDPOINT_JSON = json.dumps({
    "status": "ok",
    "action": "status",
    "namespace": "test-org",
    "endpoint_name": "test-slm-endpoint",
    "endpoint_state": "running",
    "replicas": 1,
    "url": "https://test-endpoint.example.com",
})


async def _seed_log_rows(conn, patient_id: str | None) -> list[str]:
    """Insert test rows into slm_inference_log; return their inserted IDs."""
    inserted_ids: list[str] = []
    for i, (prompt, atype) in enumerate(zip(_SEED_PROMPTS, _SEED_ADAPTER_TYPES)):
        row_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO slm_inference_log
                (id, adapter_type, prompt_hash, patient_id, latency_ms,
                 prompt_tokens, completion_tokens, total_tokens,
                 multimodal, status, endpoint_url)
            VALUES ($1, $2, $3, $4::uuid, $5, $6, $7, $8, $9, $10, $11)
            """,
            row_id,
            atype,
            _prompt_hash(prompt),
            patient_id if atype == "patient" else None,
            100 + i * 50,
            10 + i * 5,
            20 + i * 5,
            30 + i * 10,
            False,
            "ok",
            "https://test-endpoint.example.com",
        )
        inserted_ids.append(row_id)
    return inserted_ids


async def _delete_log_rows(conn, row_ids: list[str]) -> None:
    await conn.execute(
        "DELETE FROM slm_inference_log WHERE id = ANY($1::uuid[])",
        row_ids,
    )


# ── Tests: get_slm_inference_log ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_slm_inference_log_field_presence(db_pool, test_patient):
    """get_slm_inference_log must return all expected fields with no raw prompt text."""
    async with db_pool.acquire() as conn:
        row_ids = await _seed_log_rows(conn, test_patient)

    try:
        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            raw = await get_slm_inference_log(limit=5)

        result = json.loads(raw)

        assert result["status"] == "ok"
        assert isinstance(result["total_returned"], int)
        assert result["total_returned"] >= 1

        rows = result["rows"]
        assert isinstance(rows, list)
        assert len(rows) >= 1

        required_fields = {
            "called_at",
            "adapter_type",
            "prompt_hash",
            "patient_id",
            "latency_ms",
            "prompt_tokens",
            "completion_tokens",
            "multimodal",
            "status",
        }
        for row in rows:
            for field in required_fields:
                assert field in row, f"Missing field {field!r} in row"

        seeded_hashes = {_prompt_hash(p) for p in _SEED_PROMPTS}
        returned_hashes = {r["prompt_hash"] for r in rows}
        assert seeded_hashes & returned_hashes, (
            "None of the seeded prompt_hashes appeared in the response"
        )

    finally:
        async with db_pool.acquire() as conn:
            await _delete_log_rows(conn, row_ids)


@pytest.mark.asyncio
async def test_get_slm_inference_log_no_raw_prompt_text(db_pool, test_patient):
    """The response must never contain the raw prompt text — only the hash."""
    async with db_pool.acquire() as conn:
        row_ids = await _seed_log_rows(conn, test_patient)

    try:
        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            raw = await get_slm_inference_log(limit=50)

        for prompt in _SEED_PROMPTS:
            assert prompt not in raw, (
                f"Raw prompt text {prompt!r} leaked into the audit log response"
            )

    finally:
        async with db_pool.acquire() as conn:
            await _delete_log_rows(conn, row_ids)


@pytest.mark.asyncio
async def test_get_slm_inference_log_prompt_hash_format(db_pool, test_patient):
    """prompt_hash must be a 16-character hexadecimal string."""
    async with db_pool.acquire() as conn:
        row_ids = await _seed_log_rows(conn, test_patient)

    try:
        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            raw = await get_slm_inference_log(limit=50)

        result = json.loads(raw)
        seeded_hashes = {_prompt_hash(p) for p in _SEED_PROMPTS}
        matched_rows = [r for r in result["rows"] if r["prompt_hash"] in seeded_hashes]
        assert matched_rows, "No seeded rows found in response"

        for row in matched_rows:
            ph = row["prompt_hash"]
            assert isinstance(ph, str), f"prompt_hash is not a string: {ph!r}"
            assert len(ph) == 16, f"prompt_hash length is {len(ph)}, expected 16"
            assert all(c in "0123456789abcdef" for c in ph), (
                f"prompt_hash {ph!r} contains non-hex characters"
            )

    finally:
        async with db_pool.acquire() as conn:
            await _delete_log_rows(conn, row_ids)


@pytest.mark.asyncio
async def test_get_slm_inference_log_patient_id_as_string_or_none(db_pool, test_patient):
    """patient_id must be returned as a string UUID or None — never a UUID object."""
    async with db_pool.acquire() as conn:
        row_ids = await _seed_log_rows(conn, test_patient)

    try:
        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            raw = await get_slm_inference_log(limit=50)

        result = json.loads(raw)
        seeded_hashes = {_prompt_hash(p) for p in _SEED_PROMPTS}
        matched_rows = [r for r in result["rows"] if r["prompt_hash"] in seeded_hashes]
        assert matched_rows, "No seeded rows found in response"

        for row in matched_rows:
            pid = row["patient_id"]
            assert pid is None or isinstance(pid, str), (
                f"patient_id must be str or None, got {type(pid)}: {pid!r}"
            )
            if isinstance(pid, str):
                uuid.UUID(pid)

        patient_rows = [r for r in matched_rows if r["patient_id"] is not None]
        none_rows = [r for r in matched_rows if r["patient_id"] is None]
        assert patient_rows, "Expected at least one row with a patient_id"
        assert none_rows, "Expected at least one row with patient_id=None"

    finally:
        async with db_pool.acquire() as conn:
            await _delete_log_rows(conn, row_ids)


@pytest.mark.asyncio
async def test_get_slm_inference_log_limit_is_respected(db_pool, test_patient):
    """get_slm_inference_log must not return more rows than the requested limit."""
    async with db_pool.acquire() as conn:
        row_ids = await _seed_log_rows(conn, test_patient)

    try:
        for limit in (1, 2):
            with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
                raw = await get_slm_inference_log(limit=limit)
            result = json.loads(raw)
            assert result["status"] == "ok"
            assert len(result["rows"]) <= limit, (
                f"Requested limit={limit} but got {len(result['rows'])} rows"
            )

    finally:
        async with db_pool.acquire() as conn:
            await _delete_log_rows(conn, row_ids)


# ── Tests: _get_slm_status_impl ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_slm_status_call_count_reflects_seeded_rows(db_pool, test_patient):
    """_get_slm_status_impl must count seeded rows in inference_24h.call_count.

    manage_hf_endpoint is stubbed to avoid any real network calls and ensure
    the test is fully deterministic regardless of HF credentials.
    """
    async with db_pool.acquire() as conn:
        count_before_row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM slm_inference_log "
            "WHERE called_at >= NOW() - INTERVAL '24 hours'"
        )
        count_before = int(count_before_row["cnt"])

        row_ids = await _seed_log_rows(conn, test_patient)

    try:
        with (
            patch("db.connection.get_pool", AsyncMock(return_value=db_pool)),
            patch(
                "skills.slm_inference.manage_hf_endpoint",
                AsyncMock(return_value=_STUB_ENDPOINT_JSON),
            ),
        ):
            raw = await _get_slm_status_impl()

        result = json.loads(raw)

        assert result["status"] == "ok"
        assert "inference_24h" in result
        inf24 = result["inference_24h"]
        assert "call_count" in inf24
        assert "avg_latency_ms" in inf24
        assert "by_adapter_type" in inf24

        assert isinstance(inf24["call_count"], int)
        assert inf24["call_count"] >= count_before + len(row_ids), (
            f"Expected call_count >= {count_before + len(row_ids)}, "
            f"got {inf24['call_count']}"
        )

        by_type = inf24["by_adapter_type"]
        assert isinstance(by_type, dict)
        for atype in ("base", "cohort", "patient"):
            assert atype in by_type, f"adapter_type {atype!r} missing from by_adapter_type"
            assert by_type[atype] >= 1

    finally:
        async with db_pool.acquire() as conn:
            await _delete_log_rows(conn, row_ids)


@pytest.mark.asyncio
async def test_get_slm_status_structure(db_pool):
    """_get_slm_status_impl must return a well-formed status structure.

    manage_hf_endpoint is stubbed with a fixed JSON response so this test
    is deterministic in any environment.
    """
    with (
        patch("db.connection.get_pool", AsyncMock(return_value=db_pool)),
        patch(
            "skills.slm_inference.manage_hf_endpoint",
            AsyncMock(return_value=_STUB_ENDPOINT_JSON),
        ),
    ):
        raw = await _get_slm_status_impl()

    result = json.loads(raw)

    assert result["status"] == "ok"
    assert "endpoint" in result
    assert "adapter_count" in result
    assert "inference_24h" in result
    assert isinstance(result["adapter_count"], int)

    ep = result["endpoint"]
    assert isinstance(ep, dict)
    assert ep.get("endpoint_state") == "running"
    assert ep.get("replicas") == 1

    inf24 = result["inference_24h"]
    assert "call_count" in inf24
    assert "avg_latency_ms" in inf24
    assert "by_adapter_type" in inf24
    assert isinstance(inf24["call_count"], int)
    assert isinstance(inf24["by_adapter_type"], dict)


# ── Test: call_slm write-path ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_slm_writes_audit_row(db_pool):
    """call_slm must write a row to slm_inference_log after a successful inference.

    The HF HTTP call is mocked so no real network call is made.  We verify
    the write-path by checking a new row appears in slm_inference_log with
    the correct prompt_hash, adapter_type, and status.
    """
    test_prompt = "Explain the patient's latest HbA1c trend."
    expected_hash = _prompt_hash(test_prompt)

    fake_hf_response = {
        "choices": [{"message": {"content": "The HbA1c trend shows improvement."}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        "model": "qwen2.5-3b-instruct",
    }

    fake_http_response = MagicMock()
    fake_http_response.status_code = 200
    fake_http_response.json.return_value = fake_hf_response
    fake_http_response.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = AsyncMock(return_value=fake_http_response)

    async with db_pool.acquire() as conn:
        count_before_row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM slm_inference_log WHERE prompt_hash = $1",
            expected_hash,
        )
    count_before = int(count_before_row["cnt"])

    with (
        patch("skills.slm_inference.httpx.AsyncClient", return_value=fake_client),
        patch("db.connection.get_pool", AsyncMock(return_value=db_pool)),
        patch.dict(os.environ, {
            "HF_SLM_ENDPOINT_URL": "https://test-endpoint.example.com",
            "HF_TOKEN": "hf_test_token_placeholder",
        }),
    ):
        raw = await call_slm(
            prompt=test_prompt,
            adapter_type="cohort",
            max_new_tokens=64,
        )

    result = json.loads(raw)
    assert result["status"] == "ok", f"call_slm returned error: {result}"
    assert result["adapter_type"] == "cohort"
    assert "generated_text" in result

    async with db_pool.acquire() as conn:
        count_after_row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM slm_inference_log WHERE prompt_hash = $1",
            expected_hash,
        )
        count_after = int(count_after_row["cnt"])

        written_row = await conn.fetchrow(
            """
            SELECT adapter_type, prompt_hash, status, latency_ms,
                   prompt_tokens, completion_tokens, multimodal, patient_id
            FROM slm_inference_log
            WHERE prompt_hash = $1
            ORDER BY called_at DESC
            LIMIT 1
            """,
            expected_hash,
        )

    assert count_after == count_before + 1, (
        f"Expected exactly 1 new row in slm_inference_log for hash {expected_hash!r}, "
        f"before={count_before} after={count_after}"
    )
    assert written_row is not None
    assert written_row["adapter_type"] == "cohort"
    assert written_row["prompt_hash"] == expected_hash
    assert written_row["status"] == "ok"
    assert written_row["prompt_tokens"] == 12
    assert written_row["completion_tokens"] == 8
    assert written_row["multimodal"] is False
    assert written_row["patient_id"] is None

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM slm_inference_log WHERE prompt_hash = $1",
            expected_hash,
        )


# ── Unit test: _prompt_hash ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_hash_helper_is_16_hex_chars():
    """_prompt_hash must always produce a 16-character lowercase hex string."""
    for text in ("", "hello", "PHI: John Doe DOB 1970-01-01", "x" * 10_000):
        h = _prompt_hash(text)
        assert isinstance(h, str)
        assert len(h) == 16
        assert h == hashlib.sha256(text.encode()).hexdigest()[:16]
        assert all(c in "0123456789abcdef" for c in h)

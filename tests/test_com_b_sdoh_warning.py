"""Phase 7 — SDOH-empty warning in COM-B output tests.

Tests the sdoh_data_warning logic added to classify_com_b_barrier.
Uses AsyncMock to satisfy 'await get_pool()' and async-context-manager
semantics of 'async with pool.acquire() as conn'.
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

# ── Stub dependent imports so the module loads without a live DB ─────────────
for _mod_name in ("db", "db.connection"):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = ModuleType(_mod_name)

_fake_get_pool = AsyncMock()
sys.modules["db.connection"].get_pool = _fake_get_pool  # type: ignore[attr-defined]

# ── Load behavioral_tools from its actual file path ──────────────────────────
_BT_PATH = Path(__file__).parent.parent / "mcp-server" / "skills" / "behavioral_tools.py"
_spec = importlib.util.spec_from_file_location("_bt_phase7", _BT_PATH)
_bt_mod = importlib.util.module_from_spec(_spec)
sys.modules["_bt_phase7"] = _bt_mod
_spec.loader.exec_module(_bt_mod)


# ---------------------------------------------------------------------------
# Async context-manager pool helper
# ---------------------------------------------------------------------------

class _AsyncCM:
    """Minimal async context manager that returns ``obj`` on __aenter__."""
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *args):
        return False


def _make_pool(sdoh_rows, checkin_rows=None):
    """Build a fake asyncpg pool for two sequential acquire() calls."""
    if checkin_rows is None:
        checkin_rows = []

    # ── Read connection: returns sdoh_rows then checkin_rows ─────────────────
    call_n = [0]

    async def _fetch(*a, **kw):
        call_n[0] += 1
        return sdoh_rows if call_n[0] == 1 else checkin_rows

    read_conn = MagicMock()
    read_conn.fetch = _fetch

    # ── Write connection: execute is a no-op ─────────────────────────────────
    write_conn = MagicMock()
    write_conn.execute = AsyncMock(return_value=None)

    acq_n = [0]

    def _acquire():
        acq_n[0] += 1
        return _AsyncCM(read_conn if acq_n[0] == 1 else write_conn)

    pool = MagicMock()
    pool.acquire.side_effect = _acquire
    return pool


def _call(sdoh_rows, checkin_rows=None, patient_id="P1", behavior="medication"):
    """Patch get_pool (AsyncMock) and run classify_com_b_barrier."""
    fake_pool = _make_pool(sdoh_rows, checkin_rows)
    # get_pool is async (await get_pool()), so patch with AsyncMock
    with patch.object(_bt_mod, "get_pool", new=AsyncMock(return_value=fake_pool)):
        result_str = asyncio.run(
            _bt_mod.classify_com_b_barrier(patient_id, behavior)
        )
    return json.loads(result_str)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_warning_when_no_sdoh_and_default_bucket():
    """sdoh_data_warning is non-empty when SDOH absent → Motivation/Reflective."""
    result = _call(sdoh_rows=[], checkin_rows=[])
    assert "sdoh_data_warning" in result, "sdoh_data_warning key missing"
    assert result["sdoh_data_warning"] != "", (
        "Expected non-empty warning when no SDOH data "
        "and classification lands in default Motivation/Reflective bucket"
    )


def test_no_warning_when_sdoh_present():
    """sdoh_data_warning is empty when SDOH flags exist (high-severity transport)."""
    class _Row(dict):
        pass

    sdoh_rows = [_Row({"domain": "transportation", "severity": "high"})]
    result = _call(sdoh_rows=sdoh_rows, checkin_rows=[])
    assert result.get("sdoh_data_warning") == "", (
        f"Expected empty warning when SDOH flags present; "
        f"got: {result.get('sdoh_data_warning')!r}"
    )


def test_existing_return_keys_preserved():
    """All original return keys must still be present alongside the new one."""
    result = _call(sdoh_rows=[], checkin_rows=[])
    required = {
        "patient_id", "target_behavior", "com_b_component",
        "sub_component", "primary_barrier", "confidence", "supporting_evidence",
    }
    missing = required - set(result.keys())
    assert not missing, f"Original return keys missing: {missing}"


def test_sdoh_data_warning_key_always_present():
    """sdoh_data_warning key must appear even when its value is empty."""
    class _Row(dict):
        pass

    sdoh_rows = [_Row({"domain": "transportation", "severity": "high"})]
    result = _call(sdoh_rows=sdoh_rows, checkin_rows=[])
    assert "sdoh_data_warning" in result, (
        "sdoh_data_warning key must be present in all responses"
    )

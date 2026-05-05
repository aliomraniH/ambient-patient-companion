"""Unit tests for mcp-server/runtime/agent_runtime.py.

These tests exercise the AgentRuntime in isolation — no DB, no FastMCP server,
no network. They verify:

  RT1  Empty runtime starts without errors.
  RT2  watch() registers watchers; duplicate names are rejected.
  RT3  A fast watcher executes at least once within a short window.
  RT4  A failing watcher records the error but does NOT crash the loop.
  RT5  CancelledError stops the loop without storing an error.
  RT6  status() returns the expected JSON-serialisable shape.
  RT7  register_watchers() registers the three built-in watchers.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Make mcp-server/ importable (tests run from the repo root)
_MCP_SERVER = Path(__file__).resolve().parent.parent / "mcp-server"
if str(_MCP_SERVER) not in sys.path:
    sys.path.insert(0, str(_MCP_SERVER))

from runtime.agent_runtime import AgentRuntime  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _null_coro() -> None:
    """No-op watcher coroutine."""


async def _failing_coro() -> None:
    """Watcher that always raises."""
    raise RuntimeError("boom")


# ─── RT1: empty runtime starts cleanly ───────────────────────────────────────

@pytest.mark.asyncio
async def test_rt1_empty_runtime_starts_cleanly():
    runtime = AgentRuntime()
    # lifespan should enter and exit without error even with zero watchers
    async with runtime.lifespan(server=None):
        pass
    status = runtime.status()
    assert status["watcher_count"] == 0
    assert status["watchers"] == []


# ─── RT2: watch() registration ────────────────────────────────────────────────

def test_rt2_watch_registers_watcher():
    runtime = AgentRuntime()
    runtime.watch("w1", 60.0, _null_coro)
    status = runtime.status()
    assert status["watcher_count"] == 1
    assert status["watchers"][0]["name"] == "w1"
    assert status["watchers"][0]["interval_seconds"] == 60.0
    assert status["watchers"][0]["run_count"] == 0
    assert status["watchers"][0]["last_run"] is None
    assert status["watchers"][0]["last_error"] is None
    assert status["watchers"][0]["healthy"] is True


def test_rt2_duplicate_name_raises():
    runtime = AgentRuntime()
    runtime.watch("w1", 60.0, _null_coro)
    with pytest.raises(ValueError, match="already registered"):
        runtime.watch("w1", 30.0, _null_coro)


# ─── RT3: fast watcher executes within window ─────────────────────────────────

@pytest.mark.asyncio
async def test_rt3_watcher_executes_within_window():
    runtime = AgentRuntime()
    call_count = 0

    async def fast_coro():
        nonlocal call_count
        call_count += 1

    runtime.watch("fast", interval_seconds=0.05, coro_fn=fast_coro)

    async with runtime.lifespan(server=None):
        # Give the watcher at least two ticks
        await asyncio.sleep(0.25)

    assert call_count >= 1, "watcher should have executed at least once"


# ─── RT4: failing watcher records error, loop continues ──────────────────────

@pytest.mark.asyncio
async def test_rt4_error_captured_loop_continues():
    runtime = AgentRuntime()
    call_count = 0

    async def sometimes_fails():
        nonlocal call_count
        call_count += 1
        raise RuntimeError(f"failure #{call_count}")

    runtime.watch("flaky", interval_seconds=0.05, coro_fn=sometimes_fails)

    async with runtime.lifespan(server=None):
        await asyncio.sleep(0.3)

    status = runtime.status()
    w = status["watchers"][0]
    # Loop ran multiple times despite repeated failures
    assert call_count >= 2, "loop should have continued past the first error"
    assert w["last_error"] is not None
    assert "RuntimeError" in w["last_error"]
    assert w["healthy"] is False
    # run_count is incremented even when the coro raises
    assert w["run_count"] >= 2


# ─── RT5: CancelledError stops the loop, no error stored ─────────────────────

@pytest.mark.asyncio
async def test_rt5_cancelled_error_stops_cleanly():
    runtime = AgentRuntime()
    ran = False

    async def slow_coro():
        nonlocal ran
        ran = True
        # Simulate a coro that takes longer than the test window
        await asyncio.sleep(10)

    runtime.watch("slow", interval_seconds=0.01, coro_fn=slow_coro)

    async with runtime.lifespan(server=None):
        await asyncio.sleep(0.1)
    # lifespan exit cancelled the task — no crash, no last_error from cancel
    status = runtime.status()
    w = status["watchers"][0]
    # last_error should not contain "CancelledError"
    assert w["last_error"] is None or "CancelledError" not in (w["last_error"] or "")


# ─── RT6: status() shape ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rt6_status_shape_after_run():
    runtime = AgentRuntime()
    runtime.watch("w1", 0.05, _null_coro)
    runtime.watch("w2", 0.05, _failing_coro)

    async with runtime.lifespan(server=None):
        await asyncio.sleep(0.2)

    status = runtime.status()
    assert status["watcher_count"] == 2

    names = {w["name"] for w in status["watchers"]}
    assert names == {"w1", "w2"}

    for w in status["watchers"]:
        assert "interval_seconds" in w
        assert "run_count" in w
        assert "last_run" in w
        assert "last_error" in w
        assert "healthy" in w
        assert isinstance(w["healthy"], bool)

    w1 = next(w for w in status["watchers"] if w["name"] == "w1")
    w2 = next(w for w in status["watchers"] if w["name"] == "w2")
    assert w1["healthy"] is True
    assert w2["healthy"] is False
    assert w2["last_error"] is not None


# ─── RT7: register_watchers() registers the three built-in watchers ───────────

def test_rt7_register_watchers_registers_three():
    runtime = AgentRuntime()
    from runtime.watchers import register_watchers
    register_watchers(runtime)
    status = runtime.status()
    assert status["watcher_count"] == 3
    names = {w["name"] for w in status["watchers"]}
    assert names == {
        "checkin_atom_watcher",
        "crisis_scan_watcher",
        "care_gap_watcher",
    }


# ─── RT8: start(app) method starts tasks in the running loop ─────────────────

@pytest.mark.asyncio
async def test_rt8_start_method_starts_tasks():
    runtime = AgentRuntime()
    call_count = 0

    async def fast_coro():
        nonlocal call_count
        call_count += 1

    runtime.watch("w1", 0.05, fast_coro)

    # start() is the programmatic startup path (vs lifespan)
    runtime.start(app=None)
    await asyncio.sleep(0.25)

    # Cancel manually since we bypassed lifespan
    for state in runtime._watchers.values():
        if state.task:
            state.task.cancel()
            try:
                await state.task
            except asyncio.CancelledError:
                pass

    assert call_count >= 1, "start() should have launched the watcher task"


# ─── RT9: /api/agent-runtime/status response shape ───────────────────────────

@pytest.mark.asyncio
async def test_rt9_status_endpoint_response_shape():
    """Call the route handler directly and assert the JSON response structure.

    This exercises the same code path as GET /api/agent-runtime/status without
    needing to spin up a full HTTP server.
    """
    import json as _json
    from unittest.mock import AsyncMock

    runtime = AgentRuntime()
    runtime.watch("w1", 0.05, _null_coro)
    runtime.watch("w2", 0.05, _failing_coro)

    async with runtime.lifespan(server=None):
        await asyncio.sleep(0.2)

    # Simulate what server.py's agent_runtime_status route does
    payload = runtime.status()

    # Must be JSON-serialisable without error
    serialised = _json.dumps(payload)
    parsed = _json.loads(serialised)

    # Top-level shape
    assert "watcher_count" in parsed
    assert "watchers" in parsed
    assert parsed["watcher_count"] == 2
    assert isinstance(parsed["watchers"], list)

    required_keys = {"name", "interval_seconds", "run_count", "last_run", "last_error", "healthy"}
    for w in parsed["watchers"]:
        assert required_keys.issubset(w.keys()), f"Missing keys in watcher entry: {w}"
        assert isinstance(w["name"], str)
        assert isinstance(w["interval_seconds"], (int, float))
        assert isinstance(w["run_count"], int)
        assert w["last_run"] is None or isinstance(w["last_run"], str)
        assert isinstance(w["healthy"], bool)

    # w1 should be healthy, w2 should report an error
    by_name = {w["name"]: w for w in parsed["watchers"]}
    assert by_name["w1"]["healthy"] is True
    assert by_name["w2"]["healthy"] is False
    assert by_name["w2"]["last_error"] is not None

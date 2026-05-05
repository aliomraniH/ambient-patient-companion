"""Unit tests for AgentRuntime watcher-state persistence.

Covers:
  - _persist_watcher_state: upserts the correct system_config row
  - _load_persisted_state:  pre-populates in-memory state from DB rows

All tests mock ``db.connection.get_pool`` so no real database is required.

Integration tests (class TestWatcherPersistenceIntegration) require
DATABASE_URL to be set and are skipped automatically otherwise.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.agent_runtime import AgentRuntime, _WatcherState, _KEY_PREFIX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(name: str = "test_watcher", interval: float = 60.0) -> _WatcherState:
    """Return a minimal _WatcherState for inspection."""
    return _WatcherState(name=name, interval_seconds=interval, coro_fn=AsyncMock())


def _make_pool(*, execute_side_effect=None, fetch_return=None):
    """Return a mock asyncpg pool with execute and fetch pre-configured."""
    pool = MagicMock()
    pool.execute = AsyncMock(side_effect=execute_side_effect)
    pool.fetch = AsyncMock(return_value=fetch_return or [])
    return pool


def _make_row(key: str, value: str) -> dict:
    """Simulate an asyncpg Record-like dict."""
    return {"key": key, "value": value}


# ---------------------------------------------------------------------------
# _persist_watcher_state
# ---------------------------------------------------------------------------


class TestPersistWatcherState:
    async def test_calls_upsert_with_correct_key(self):
        """The system_config key must be 'watcher_state:<name>'."""
        state = _make_state("vitals_watcher")
        state.run_count = 3
        state.last_run = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        state.last_error = None

        pool = _make_pool()
        runtime = AgentRuntime()

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._persist_watcher_state(state)

        pool.execute.assert_awaited_once()
        call_args = pool.execute.call_args[0]
        assert call_args[1] == "watcher_state:vitals_watcher"

    async def test_sql_targets_system_config_with_on_conflict_upsert(self):
        """The SQL must INSERT INTO system_config with ON CONFLICT (key) DO UPDATE."""
        state = _make_state("upsert_watcher")
        state.run_count = 1
        state.last_run = None
        state.last_error = None

        pool = _make_pool()
        runtime = AgentRuntime()

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._persist_watcher_state(state)

        sql = pool.execute.call_args[0][0]
        assert "system_config" in sql, "SQL must target the system_config table"
        assert "ON CONFLICT" in sql, "SQL must use an upsert (ON CONFLICT) clause"
        assert "DO UPDATE" in sql, "SQL must update existing rows on conflict"

    async def test_persisted_json_contains_run_count(self):
        """Serialised value must encode the current run_count."""
        state = _make_state("watcher_a")
        state.run_count = 7
        state.last_run = None
        state.last_error = None

        pool = _make_pool()
        runtime = AgentRuntime()

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._persist_watcher_state(state)

        raw_value = pool.execute.call_args[0][2]
        data = json.loads(raw_value)
        assert data["run_count"] == 7

    async def test_persisted_json_encodes_last_run_isoformat(self):
        """last_run must be stored as an ISO-format string."""
        dt = datetime(2025, 6, 1, 9, 30, 0, tzinfo=timezone.utc)
        state = _make_state()
        state.run_count = 1
        state.last_run = dt
        state.last_error = None

        pool = _make_pool()
        runtime = AgentRuntime()

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._persist_watcher_state(state)

        raw_value = pool.execute.call_args[0][2]
        data = json.loads(raw_value)
        assert data["last_run"] == dt.isoformat()

    async def test_persisted_json_encodes_none_last_run(self):
        """When last_run is None it must be stored as JSON null."""
        state = _make_state()
        state.run_count = 0
        state.last_run = None
        state.last_error = None

        pool = _make_pool()
        runtime = AgentRuntime()

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._persist_watcher_state(state)

        raw_value = pool.execute.call_args[0][2]
        data = json.loads(raw_value)
        assert data["last_run"] is None

    async def test_persisted_json_encodes_last_error(self):
        """last_error string must survive the round-trip."""
        state = _make_state()
        state.run_count = 2
        state.last_run = None
        state.last_error = "ValueError: something broke"

        pool = _make_pool()
        runtime = AgentRuntime()

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._persist_watcher_state(state)

        raw_value = pool.execute.call_args[0][2]
        data = json.loads(raw_value)
        assert data["last_error"] == "ValueError: something broke"

    async def test_db_error_is_swallowed(self):
        """A database failure must not propagate; the method must return None."""
        state = _make_state()
        state.run_count = 1
        state.last_run = None
        state.last_error = None

        pool = _make_pool(execute_side_effect=RuntimeError("connection lost"))
        runtime = AgentRuntime()

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            result = await runtime._persist_watcher_state(state)

        assert result is None

    async def test_get_pool_failure_is_swallowed(self):
        """If get_pool itself raises, _persist_watcher_state must not propagate."""
        state = _make_state()
        state.run_count = 0
        state.last_run = None
        state.last_error = None

        runtime = AgentRuntime()

        with patch("db.connection.get_pool", AsyncMock(side_effect=OSError("no db"))):
            result = await runtime._persist_watcher_state(state)

        assert result is None


# ---------------------------------------------------------------------------
# _load_persisted_state
# ---------------------------------------------------------------------------


class TestLoadPersistedState:
    async def test_restores_run_count(self):
        """run_count from the DB row must be written to the in-memory state."""
        runtime = AgentRuntime()
        runtime.watch("health_watcher", 300, AsyncMock())

        row = _make_row(
            f"{_KEY_PREFIX}health_watcher",
            json.dumps({"run_count": 42, "last_run": None, "last_error": None}),
        )
        pool = _make_pool(fetch_return=[row])

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        assert runtime._watchers["health_watcher"].run_count == 42

    async def test_restores_last_run(self):
        """last_run ISO string must be parsed back into a datetime."""
        runtime = AgentRuntime()
        runtime.watch("metrics_watcher", 60, AsyncMock())

        dt = datetime(2025, 3, 10, 8, 0, 0, tzinfo=timezone.utc)
        row = _make_row(
            f"{_KEY_PREFIX}metrics_watcher",
            json.dumps({"run_count": 1, "last_run": dt.isoformat(), "last_error": None}),
        )
        pool = _make_pool(fetch_return=[row])

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        loaded = runtime._watchers["metrics_watcher"].last_run
        assert loaded is not None
        assert loaded == dt

    async def test_restores_last_error(self):
        """last_error string must be written to the in-memory state."""
        runtime = AgentRuntime()
        runtime.watch("alert_watcher", 120, AsyncMock())

        row = _make_row(
            f"{_KEY_PREFIX}alert_watcher",
            json.dumps(
                {"run_count": 5, "last_run": None, "last_error": "TimeoutError: timed out"}
            ),
        )
        pool = _make_pool(fetch_return=[row])

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        assert runtime._watchers["alert_watcher"].last_error == "TimeoutError: timed out"

    async def test_null_last_error_stays_none(self):
        """JSON null last_error must remain None (not the string 'None')."""
        runtime = AgentRuntime()
        runtime.watch("quiet_watcher", 60, AsyncMock())

        row = _make_row(
            f"{_KEY_PREFIX}quiet_watcher",
            json.dumps({"run_count": 3, "last_run": None, "last_error": None}),
        )
        pool = _make_pool(fetch_return=[row])

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        assert runtime._watchers["quiet_watcher"].last_error is None

    async def test_unknown_watcher_name_in_db_is_skipped(self):
        """Rows for watchers that are not registered must be silently ignored."""
        runtime = AgentRuntime()
        runtime.watch("known_watcher", 60, AsyncMock())

        rows = [
            _make_row(
                f"{_KEY_PREFIX}ghost_watcher",
                json.dumps({"run_count": 99, "last_run": None, "last_error": None}),
            ),
            _make_row(
                f"{_KEY_PREFIX}known_watcher",
                json.dumps({"run_count": 1, "last_run": None, "last_error": None}),
            ),
        ]
        pool = _make_pool(fetch_return=rows)

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        assert "ghost_watcher" not in runtime._watchers
        assert runtime._watchers["known_watcher"].run_count == 1

    async def test_malformed_json_is_skipped(self):
        """A row with invalid JSON must not crash the loader; other rows still load."""
        runtime = AgentRuntime()
        runtime.watch("good_watcher", 60, AsyncMock())
        runtime.watch("bad_watcher", 60, AsyncMock())

        rows = [
            _make_row(f"{_KEY_PREFIX}bad_watcher", "not-valid-json{{{{"),
            _make_row(
                f"{_KEY_PREFIX}good_watcher",
                json.dumps({"run_count": 5, "last_run": None, "last_error": None}),
            ),
        ]
        pool = _make_pool(fetch_return=rows)

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        assert runtime._watchers["bad_watcher"].run_count == 0
        assert runtime._watchers["good_watcher"].run_count == 5

    async def test_non_dict_json_is_skipped(self):
        """A row whose JSON decodes to a non-dict (e.g. a list) must be skipped."""
        runtime = AgentRuntime()
        runtime.watch("list_watcher", 60, AsyncMock())

        row = _make_row(f"{_KEY_PREFIX}list_watcher", json.dumps([1, 2, 3]))
        pool = _make_pool(fetch_return=[row])

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        assert runtime._watchers["list_watcher"].run_count == 0
        assert runtime._watchers["list_watcher"].last_run is None

    async def test_missing_run_count_key_defaults_to_zero(self):
        """A persisted dict without 'run_count' must default to 0."""
        runtime = AgentRuntime()
        runtime.watch("partial_watcher", 60, AsyncMock())

        row = _make_row(
            f"{_KEY_PREFIX}partial_watcher",
            json.dumps({"last_run": None, "last_error": None}),
        )
        pool = _make_pool(fetch_return=[row])

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        assert runtime._watchers["partial_watcher"].run_count == 0

    async def test_non_numeric_run_count_defaults_to_zero(self):
        """A run_count that cannot be coerced to int must silently default to 0."""
        runtime = AgentRuntime()
        runtime.watch("noisy_watcher", 60, AsyncMock())

        row = _make_row(
            f"{_KEY_PREFIX}noisy_watcher",
            json.dumps({"run_count": "not-a-number", "last_run": None, "last_error": None}),
        )
        pool = _make_pool(fetch_return=[row])

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        assert runtime._watchers["noisy_watcher"].run_count == 0

    async def test_invalid_last_run_string_leaves_last_run_none(self):
        """A last_run value that is not a valid ISO datetime must be ignored."""
        runtime = AgentRuntime()
        runtime.watch("date_watcher", 60, AsyncMock())

        row = _make_row(
            f"{_KEY_PREFIX}date_watcher",
            json.dumps({"run_count": 2, "last_run": "not-a-date", "last_error": None}),
        )
        pool = _make_pool(fetch_return=[row])

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        assert runtime._watchers["date_watcher"].last_run is None

    async def test_db_error_is_swallowed(self):
        """A database error during load must not propagate; watchers keep defaults."""
        runtime = AgentRuntime()
        runtime.watch("robust_watcher", 60, AsyncMock())

        pool = _make_pool()
        pool.fetch = AsyncMock(side_effect=RuntimeError("db unavailable"))

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        assert runtime._watchers["robust_watcher"].run_count == 0
        assert runtime._watchers["robust_watcher"].last_run is None

    async def test_get_pool_failure_is_swallowed(self):
        """If get_pool raises, _load_persisted_state must return without crashing."""
        runtime = AgentRuntime()
        runtime.watch("safe_watcher", 60, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(side_effect=OSError("no db"))):
            await runtime._load_persisted_state()

        assert runtime._watchers["safe_watcher"].run_count == 0

    async def test_empty_db_leaves_defaults_intact(self):
        """When the DB has no rows for any watcher, all defaults remain unchanged."""
        runtime = AgentRuntime()
        runtime.watch("fresh_watcher", 60, AsyncMock())

        pool = _make_pool(fetch_return=[])

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        state = runtime._watchers["fresh_watcher"]
        assert state.run_count == 0
        assert state.last_run is None
        assert state.last_error is None


# ---------------------------------------------------------------------------
# Stale watcher cleanup (pruning orphaned system_config rows)
# ---------------------------------------------------------------------------


class TestStaleWatcherCleanup:
    """Verify that _load_persisted_state deletes rows for watchers that are no
    longer registered and leaves rows for watchers that ARE registered."""

    async def test_stale_rows_trigger_delete_execute_call(self):
        """execute() must be called with a DELETE when stale rows exist."""
        runtime = AgentRuntime()
        # No watchers registered — every DB row is stale.

        stale_row = _make_row(
            f"{_KEY_PREFIX}orphan_watcher",
            json.dumps({"run_count": 5, "last_run": None, "last_error": None}),
        )
        pool = _make_pool(fetch_return=[stale_row])

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        pool.execute.assert_awaited_once()
        sql = pool.execute.call_args[0][0]
        assert "DELETE" in sql.upper(), "execute() must issue a DELETE for stale rows"

    async def test_stale_row_key_is_passed_to_delete(self):
        """The stale key must appear in the list passed to the DELETE statement."""
        runtime = AgentRuntime()

        stale_key = f"{_KEY_PREFIX}vanished_watcher"
        stale_row = _make_row(
            stale_key,
            json.dumps({"run_count": 2, "last_run": None, "last_error": None}),
        )
        pool = _make_pool(fetch_return=[stale_row])

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        delete_keys = pool.execute.call_args[0][1]
        assert stale_key in delete_keys, (
            f"Expected stale key '{stale_key}' in DELETE args; got {delete_keys}"
        )

    async def test_multiple_stale_rows_all_deleted(self):
        """All stale keys must be passed together to a single DELETE call."""
        runtime = AgentRuntime()
        # No watchers registered.

        stale_keys = [f"{_KEY_PREFIX}old_watcher_{i}" for i in range(3)]
        rows = [
            _make_row(k, json.dumps({"run_count": i, "last_run": None, "last_error": None}))
            for i, k in enumerate(stale_keys)
        ]
        pool = _make_pool(fetch_return=rows)

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        pool.execute.assert_awaited_once()
        delete_keys = pool.execute.call_args[0][1]
        for key in stale_keys:
            assert key in delete_keys, f"Stale key '{key}' missing from DELETE args"

    async def test_registered_watcher_row_is_not_deleted(self):
        """A row whose watcher IS registered must not be included in the DELETE."""
        runtime = AgentRuntime()
        runtime.watch("active_watcher", 60, AsyncMock())

        rows = [
            _make_row(
                f"{_KEY_PREFIX}active_watcher",
                json.dumps({"run_count": 7, "last_run": None, "last_error": None}),
            ),
        ]
        pool = _make_pool(fetch_return=rows)

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        # No stale rows — execute (DELETE) should not be called at all.
        pool.execute.assert_not_awaited()

    async def test_mixed_stale_and_active_rows(self):
        """Only the stale key is deleted; the active key is left intact."""
        runtime = AgentRuntime()
        runtime.watch("live_watcher", 60, AsyncMock())

        stale_key = f"{_KEY_PREFIX}dead_watcher"
        active_key = f"{_KEY_PREFIX}live_watcher"
        rows = [
            _make_row(stale_key, json.dumps({"run_count": 3, "last_run": None, "last_error": None})),
            _make_row(active_key, json.dumps({"run_count": 1, "last_run": None, "last_error": None})),
        ]
        pool = _make_pool(fetch_return=rows)

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        pool.execute.assert_awaited_once()
        delete_keys = pool.execute.call_args[0][1]
        assert stale_key in delete_keys, "Stale key must be in DELETE args"
        assert active_key not in delete_keys, "Active key must NOT be in DELETE args"

    async def test_active_watcher_state_still_loaded_when_stale_rows_present(self):
        """After pruning, the legitimate watcher's state is still restored from its row."""
        runtime = AgentRuntime()
        runtime.watch("real_watcher", 60, AsyncMock())

        rows = [
            _make_row(
                f"{_KEY_PREFIX}stale_watcher",
                json.dumps({"run_count": 99, "last_run": None, "last_error": None}),
            ),
            _make_row(
                f"{_KEY_PREFIX}real_watcher",
                json.dumps({"run_count": 12, "last_run": None, "last_error": None}),
            ),
        ]
        pool = _make_pool(fetch_return=rows)

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        assert runtime._watchers["real_watcher"].run_count == 12

    async def test_no_rows_means_no_delete_called(self):
        """With an empty DB there is nothing stale — execute must not be called."""
        runtime = AgentRuntime()

        pool = _make_pool(fetch_return=[])

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        pool.execute.assert_not_awaited()

    async def test_delete_failure_is_swallowed(self):
        """If the DELETE raises, _load_persisted_state must not propagate the error."""
        runtime = AgentRuntime()

        stale_row = _make_row(
            f"{_KEY_PREFIX}broken_watcher",
            json.dumps({"run_count": 1, "last_run": None, "last_error": None}),
        )
        pool = _make_pool(
            fetch_return=[stale_row],
            execute_side_effect=RuntimeError("DELETE failed"),
        )

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()  # must not raise

    async def test_multiple_watchers_restored_independently(self):
        """Each registered watcher gets its own state from its own DB row."""
        runtime = AgentRuntime()
        runtime.watch("watcher_x", 60, AsyncMock())
        runtime.watch("watcher_y", 120, AsyncMock())

        dt = datetime(2025, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
        rows = [
            _make_row(
                f"{_KEY_PREFIX}watcher_x",
                json.dumps({"run_count": 10, "last_run": dt.isoformat(), "last_error": None}),
            ),
            _make_row(
                f"{_KEY_PREFIX}watcher_y",
                json.dumps({"run_count": 3, "last_run": None, "last_error": "Err"}),
            ),
        ]
        pool = _make_pool(fetch_return=rows)

        with patch("db.connection.get_pool", AsyncMock(return_value=pool)):
            await runtime._load_persisted_state()

        assert runtime._watchers["watcher_x"].run_count == 10
        assert runtime._watchers["watcher_x"].last_run == dt
        assert runtime._watchers["watcher_x"].last_error is None

        assert runtime._watchers["watcher_y"].run_count == 3
        assert runtime._watchers["watcher_y"].last_run is None
        assert runtime._watchers["watcher_y"].last_error == "Err"


# ---------------------------------------------------------------------------
# Integration tests — require a real DATABASE_URL
# ---------------------------------------------------------------------------

_TEST_KEY_PREFIX = f"{_KEY_PREFIX}integ_test_"


@pytest_asyncio.fixture
async def watcher_cleanup(db_pool):
    """Delete all system_config rows created by integration tests on teardown."""
    yield
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM system_config WHERE key LIKE $1",
            f"{_TEST_KEY_PREFIX}%",
        )


class TestWatcherPersistenceIntegration:
    """Integration tests that write to and read from a real system_config table.

    Skipped automatically when DATABASE_URL is not present in the environment.
    Each test cleans up its own rows via the ``watcher_cleanup`` fixture.
    """

    async def test_persist_then_load_run_count(self, db_pool, watcher_cleanup):
        """run_count written by _persist_watcher_state is restored by _load_persisted_state."""
        watcher_name = "integ_test_run_count"
        state = _WatcherState(
            name=watcher_name,
            interval_seconds=60.0,
            coro_fn=AsyncMock(),
        )
        state.run_count = 17
        state.last_run = None
        state.last_error = None

        runtime = AgentRuntime()
        runtime.watch(watcher_name, 60.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime._persist_watcher_state(state)

        runtime2 = AgentRuntime()
        runtime2.watch(watcher_name, 60.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime2._load_persisted_state()

        assert runtime2._watchers[watcher_name].run_count == 17

    async def test_persist_then_load_last_run(self, db_pool, watcher_cleanup):
        """last_run datetime survives the persist → load round-trip with full precision."""
        watcher_name = "integ_test_last_run"
        dt = datetime(2025, 4, 22, 14, 30, 55, tzinfo=timezone.utc)

        state = _WatcherState(
            name=watcher_name,
            interval_seconds=120.0,
            coro_fn=AsyncMock(),
        )
        state.run_count = 3
        state.last_run = dt
        state.last_error = None

        runtime = AgentRuntime()
        runtime.watch(watcher_name, 120.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime._persist_watcher_state(state)

        runtime2 = AgentRuntime()
        runtime2.watch(watcher_name, 120.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime2._load_persisted_state()

        loaded = runtime2._watchers[watcher_name].last_run
        assert loaded is not None
        assert loaded == dt

    async def test_persist_then_load_last_error(self, db_pool, watcher_cleanup):
        """last_error string survives the persist → load round-trip unchanged."""
        watcher_name = "integ_test_last_error"
        error_msg = "ConnectionError: timed out after 30s"

        state = _WatcherState(
            name=watcher_name,
            interval_seconds=300.0,
            coro_fn=AsyncMock(),
        )
        state.run_count = 5
        state.last_run = None
        state.last_error = error_msg

        runtime = AgentRuntime()
        runtime.watch(watcher_name, 300.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime._persist_watcher_state(state)

        runtime2 = AgentRuntime()
        runtime2.watch(watcher_name, 300.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime2._load_persisted_state()

        assert runtime2._watchers[watcher_name].last_error == error_msg

    async def test_persist_then_load_none_last_error(self, db_pool, watcher_cleanup):
        """A None last_error round-trips as None (not the string 'None')."""
        watcher_name = "integ_test_none_error"

        state = _WatcherState(
            name=watcher_name,
            interval_seconds=60.0,
            coro_fn=AsyncMock(),
        )
        state.run_count = 2
        state.last_run = None
        state.last_error = None

        runtime = AgentRuntime()
        runtime.watch(watcher_name, 60.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime._persist_watcher_state(state)

        runtime2 = AgentRuntime()
        runtime2.watch(watcher_name, 60.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime2._load_persisted_state()

        assert runtime2._watchers[watcher_name].last_error is None

    async def test_upsert_overwrites_previous_row(self, db_pool, watcher_cleanup):
        """Calling _persist_watcher_state twice updates the row rather than inserting a duplicate."""
        watcher_name = "integ_test_upsert"

        state = _WatcherState(
            name=watcher_name,
            interval_seconds=60.0,
            coro_fn=AsyncMock(),
        )
        state.run_count = 1
        state.last_run = None
        state.last_error = None

        runtime = AgentRuntime()
        runtime.watch(watcher_name, 60.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime._persist_watcher_state(state)

        state.run_count = 9
        state.last_error = "ValueError: second run failed"

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime._persist_watcher_state(state)

        runtime2 = AgentRuntime()
        runtime2.watch(watcher_name, 60.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime2._load_persisted_state()

        restored = runtime2._watchers[watcher_name]
        assert restored.run_count == 9
        assert restored.last_error == "ValueError: second run failed"

        async with db_pool.acquire() as conn:
            row_count = await conn.fetchval(
                "SELECT COUNT(*) FROM system_config WHERE key = $1",
                f"{_KEY_PREFIX}{watcher_name}",
            )
        assert row_count == 1

    async def test_full_state_round_trip(self, db_pool, watcher_cleanup):
        """All three fields — run_count, last_run, last_error — survive the round-trip together."""
        watcher_name = "integ_test_full"
        dt = datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc)

        state = _WatcherState(
            name=watcher_name,
            interval_seconds=600.0,
            coro_fn=AsyncMock(),
        )
        state.run_count = 42
        state.last_run = dt
        state.last_error = "RuntimeError: out of memory"

        runtime = AgentRuntime()
        runtime.watch(watcher_name, 600.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime._persist_watcher_state(state)

        runtime2 = AgentRuntime()
        runtime2.watch(watcher_name, 600.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime2._load_persisted_state()

        restored = runtime2._watchers[watcher_name]
        assert restored.run_count == 42
        assert restored.last_run == dt
        assert restored.last_error == "RuntimeError: out of memory"

    async def test_stale_row_is_deleted_from_db(self, db_pool, watcher_cleanup):
        """A system_config row for a non-existent watcher must be deleted by _load_persisted_state."""
        stale_watcher_name = "integ_test_stale_orphan"
        stale_key = f"{_KEY_PREFIX}{stale_watcher_name}"

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO system_config (key, value, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                stale_key,
                json.dumps({"run_count": 7, "last_run": None, "last_error": None}),
            )

        row_before = await db_pool.fetchval(
            "SELECT COUNT(*) FROM system_config WHERE key = $1", stale_key
        )
        assert row_before == 1, "Stale row must exist before _load_persisted_state"

        runtime = AgentRuntime()

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime._load_persisted_state()

        row_after = await db_pool.fetchval(
            "SELECT COUNT(*) FROM system_config WHERE key = $1", stale_key
        )
        assert row_after == 0, "Stale row must be deleted after _load_persisted_state"

    async def test_active_watcher_row_is_not_deleted_from_db(self, db_pool, watcher_cleanup):
        """The system_config row for a registered watcher must survive _load_persisted_state."""
        active_watcher_name = "integ_test_active_kept"
        active_key = f"{_KEY_PREFIX}{active_watcher_name}"

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO system_config (key, value, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                active_key,
                json.dumps({"run_count": 3, "last_run": None, "last_error": None}),
            )

        runtime = AgentRuntime()
        runtime.watch(active_watcher_name, 60.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime._load_persisted_state()

        row_after = await db_pool.fetchval(
            "SELECT COUNT(*) FROM system_config WHERE key = $1", active_key
        )
        assert row_after == 1, "Active watcher row must NOT be deleted"

    async def test_stale_deleted_active_kept_in_same_load(self, db_pool, watcher_cleanup):
        """When stale and active rows coexist, only the stale row is deleted."""
        stale_name = "integ_test_mixed_stale"
        active_name = "integ_test_mixed_active"
        stale_key = f"{_KEY_PREFIX}{stale_name}"
        active_key = f"{_KEY_PREFIX}{active_name}"

        async with db_pool.acquire() as conn:
            for key in (stale_key, active_key):
                await conn.execute(
                    """
                    INSERT INTO system_config (key, value, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    key,
                    json.dumps({"run_count": 1, "last_run": None, "last_error": None}),
                )

        runtime = AgentRuntime()
        runtime.watch(active_name, 60.0, AsyncMock())

        with patch("db.connection.get_pool", AsyncMock(return_value=db_pool)):
            await runtime._load_persisted_state()

        stale_count = await db_pool.fetchval(
            "SELECT COUNT(*) FROM system_config WHERE key = $1", stale_key
        )
        active_count = await db_pool.fetchval(
            "SELECT COUNT(*) FROM system_config WHERE key = $1", active_key
        )
        assert stale_count == 0, "Stale row must be deleted"
        assert active_count == 1, "Active watcher row must be preserved"

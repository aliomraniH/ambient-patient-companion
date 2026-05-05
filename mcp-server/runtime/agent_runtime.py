"""AgentRuntime — lightweight background-task scheduler for the Skills MCP server.

Bridges the gap between "skill registered as an MCP tool" (call-driven) and
"skill running autonomously on a schedule" (proactive). Every watcher is an
async coroutine that the runtime starts when the server comes up and cancels
cleanly when it shuts down.

Usage (in server.py)::

    from runtime.agent_runtime import get_runtime

    runtime = get_runtime()          # always returns the same singleton
    mcp = FastMCP("...", lifespan=runtime.lifespan)

    # Pass runtime to load_skills so skill modules can self-register watchers
    load_skills(mcp, runtime=runtime)

Each skill module that wants an autonomous watcher exports a hook::

    # inside a skill file (e.g. skills/behavioral_atoms.py)
    def register_watchers(runtime):
        runtime.watch("my_watcher", interval_seconds=300, coro_fn=_my_watcher)

``load_skills()`` calls this hook automatically for every skill that defines
it, so watchers are declared alongside the tools they support.  No central
registry file is needed — adding or removing a skill file also adds/removes
its watchers.

A skill module can also register its own watchers — because ``get_runtime()``
returns the same instance, watchers registered inside ``register(mcp)`` will
be picked up by the lifespan that server.py installs::

    # inside a skill's register(mcp) function
    from runtime.agent_runtime import get_runtime
    get_runtime().watch("my_watcher", interval_seconds=300, coro_fn=_my_watcher)

Watcher run state (run_count, last_run, last_error) is persisted to the
``system_config`` table after every execution so that health is accurate
immediately after a server restart.  Each watcher occupies one row with key
``watcher_state:<name>`` and a JSON value.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Coroutine

logger = logging.getLogger(__name__)

_KEY_PREFIX = "watcher_state:"

# Module-level singleton so skills can reach the runtime without a circular import
_runtime: AgentRuntime | None = None


def get_runtime() -> "AgentRuntime":
    """Return the process-wide AgentRuntime, creating it if necessary."""
    global _runtime
    if _runtime is None:
        _runtime = AgentRuntime()
    return _runtime


class _WatcherState:
    __slots__ = (
        "name", "interval_seconds", "coro_fn",
        "run_count", "last_run", "last_error", "task",
    )

    def __init__(
        self,
        name: str,
        interval_seconds: float,
        coro_fn: Callable[[], Coroutine],
    ) -> None:
        self.name = name
        self.interval_seconds = interval_seconds
        self.coro_fn = coro_fn
        self.run_count: int = 0
        self.last_run: datetime | None = None
        self.last_error: str | None = None
        self.task: asyncio.Task | None = None


class AgentRuntime:
    """Registry and runner for autonomous background watchers.

    A watcher is any async callable that takes no arguments and runs on a
    repeating interval. The runtime owns the asyncio task lifecycle: it
    creates tasks on startup (via ``lifespan``) and cancels them on shutdown.
    Errors inside a watcher coroutine are caught and stored in ``status()``
    — they never propagate out and never crash the loop.

    Run state is persisted to ``system_config`` (key ``watcher_state:<name>``)
    after each execution and reloaded at startup, so ``status()`` reflects
    historical counts and timestamps immediately after a restart.
    """

    def __init__(self) -> None:
        self._watchers: dict[str, _WatcherState] = {}

    # ── Registration ──────────────────────────────────────────────────────────

    def watch(
        self,
        name: str,
        interval_seconds: float,
        coro_fn: Callable[[], Coroutine],
    ) -> None:
        """Register a watcher to be started alongside the Skills MCP server.

        Args:
            name:             Unique identifier (returned by ``status()``).
            interval_seconds: Sleep duration between consecutive executions.
            coro_fn:          Async callable (no required arguments) that
                              performs the autonomous work. Must handle its
                              own errors internally or rely on the runtime's
                              catch-all guard.
        """
        if name in self._watchers:
            logger.warning(
                "AgentRuntime: watcher '%s' is already registered — "
                "skipping re-registration (safe to ignore during hot-reload)",
                name,
            )
            return
        self._watchers[name] = _WatcherState(name, interval_seconds, coro_fn)
        logger.info(
            "AgentRuntime: registered watcher '%s' (interval=%.0fs)",
            name, interval_seconds,
        )

    # ── Persistence ───────────────────────────────────────────────────────────

    async def _persist_watcher_state(self, state: _WatcherState) -> None:
        """Upsert one system_config row for this watcher after each execution."""
        key = f"{_KEY_PREFIX}{state.name}"
        value = json.dumps({
            "run_count": state.run_count,
            "last_run": state.last_run.isoformat() if state.last_run else None,
            "last_error": state.last_error,
        })
        now = datetime.now(timezone.utc)
        try:
            from db.connection import get_pool  # local import avoids circular deps
            pool = await get_pool()
            await pool.execute(
                """
                INSERT INTO system_config (key, value, updated_at)
                VALUES ($1, $2, $3)
                ON CONFLICT (key) DO UPDATE
                    SET value      = EXCLUDED.value,
                        updated_at = EXCLUDED.updated_at
                """,
                key, value, now,
            )
        except Exception as exc:
            logger.warning(
                "AgentRuntime: failed to persist state for watcher '%s': %s",
                state.name, exc,
            )

    async def _load_persisted_state(self) -> None:
        """Read system_config rows and pre-populate in-memory watcher state.

        Called once at startup (inside ``lifespan``) before tasks are spawned
        so that ``status()`` is accurate from the very first request.
        Any DB or JSON error is caught and logged — missing history is not fatal.
        """
        try:
            from db.connection import get_pool  # local import avoids circular deps
            pool = await get_pool()
            rows = await pool.fetch(
                "SELECT key, value FROM system_config WHERE key LIKE $1",
                f"{_KEY_PREFIX}%",
            )
        except Exception as exc:
            logger.warning(
                "AgentRuntime: could not load persisted watcher state (DB error): %s", exc,
            )
            return

        stale_keys = [
            row["key"]
            for row in rows
            if row["key"][len(_KEY_PREFIX):] not in self._watchers
        ]
        if stale_keys:
            logger.warning(
                "AgentRuntime: pruning %d stale watcher_state row(s) with no "
                "matching watcher: %s",
                len(stale_keys),
                [k[len(_KEY_PREFIX):] for k in stale_keys],
            )
            try:
                await pool.execute(
                    "DELETE FROM system_config WHERE key = ANY($1::text[])",
                    stale_keys,
                )
            except Exception as exc:
                logger.warning(
                    "AgentRuntime: failed to prune stale watcher state rows: %s", exc,
                )

        for row in rows:
            name = row["key"][len(_KEY_PREFIX):]
            if name not in self._watchers:
                continue
            try:
                data = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning(
                    "AgentRuntime: malformed persisted state for watcher '%s': %s",
                    name, exc,
                )
                continue

            if not isinstance(data, dict):
                logger.warning(
                    "AgentRuntime: persisted state for watcher '%s' is not a dict "
                    "(got %s) — skipping",
                    name, type(data).__name__,
                )
                continue

            state = self._watchers[name]
            raw_count = data.get("run_count")
            try:
                state.run_count = max(0, int(raw_count)) if raw_count is not None else 0
            except (TypeError, ValueError):
                state.run_count = 0
            last_run_str = data.get("last_run")
            if last_run_str and isinstance(last_run_str, str):
                try:
                    state.last_run = datetime.fromisoformat(last_run_str)
                except ValueError:
                    pass
            raw_error = data.get("last_error")
            state.last_error = str(raw_error) if raw_error is not None else None
            logger.info(
                "AgentRuntime: restored state for watcher '%s' "
                "(run_count=%d, last_run=%s)",
                name, state.run_count, state.last_run,
            )

    # ── Execution loop ────────────────────────────────────────────────────────

    async def _run_watcher(self, state: _WatcherState) -> None:
        """Infinite loop for one watcher.

        Pattern: execute → record outcome → persist → sleep → repeat.
        asyncio.CancelledError is re-raised after cleanup so the task ends.
        All other exceptions are caught, logged, and stored in state.last_error
        — the loop always continues.
        """
        logger.info("AgentRuntime: watcher '%s' starting", state.name)
        while True:
            try:
                await state.coro_fn()
                state.last_error = None
            except asyncio.CancelledError:
                logger.info("AgentRuntime: watcher '%s' cancelled", state.name)
                return
            except Exception as exc:
                state.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "AgentRuntime: watcher '%s' raised %s — continuing",
                    state.name, exc,
                )

            state.run_count += 1
            state.last_run = datetime.now(timezone.utc)

            await self._persist_watcher_state(state)

            try:
                await asyncio.sleep(state.interval_seconds)
            except asyncio.CancelledError:
                logger.info(
                    "AgentRuntime: watcher '%s' cancelled during sleep", state.name,
                )
                return

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _start_tasks(self) -> None:
        for state in self._watchers.values():
            state.task = asyncio.ensure_future(self._run_watcher(state))
            logger.info("AgentRuntime: spawned task for watcher '%s'", state.name)

    def _cancel_tasks(self) -> None:
        for state in self._watchers.values():
            if state.task and not state.task.done():
                state.task.cancel()
                logger.info("AgentRuntime: cancelled watcher '%s'", state.name)

    def start(self, app: Any = None) -> None:
        """Start all registered watchers immediately in the current event loop.

        This is the programmatic startup path — call it when you manage the
        event loop yourself (e.g. in tests or custom ASGI wrappers). For
        FastMCP / Starlette integration, prefer passing ``lifespan=runtime.lifespan``
        to the ``FastMCP`` constructor so startup and shutdown are tied to the
        server's own lifecycle.

        Safe to call only once per runtime instance. A second call is a no-op
        with a warning so accidental double-starts (e.g. test teardown races)
        do not spawn duplicate task pairs.

        Args:
            app: Unused; accepted for API symmetry with Starlette lifespan
                 callables that receive the application instance.
        """
        already_running = any(
            s.task and not s.task.done() for s in self._watchers.values()
        )
        if already_running:
            logger.warning(
                "AgentRuntime.start() called while watchers are already running — "
                "ignoring duplicate start to prevent double task spawning"
            )
            return
        self._start_tasks()
        logger.info("AgentRuntime.start(): %d watcher(s) started", len(self._watchers))

    @asynccontextmanager
    async def lifespan(self, server: Any) -> AsyncIterator[dict]:
        """FastMCP / Starlette lifespan context manager.

        Pass as ``lifespan=runtime.lifespan`` to the ``FastMCP`` constructor.
        Loads persisted watcher state from the DB before starting tasks so
        that ``status()`` is accurate from the first request.  Then starts all
        registered watchers and cancels + awaits them on exit so the process
        shuts down cleanly.

        Example::

            mcp = FastMCP("my-server", lifespan=runtime.lifespan)
        """
        await self._load_persisted_state()
        self.start(server)
        logger.info("AgentRuntime: %d watcher(s) active", len(self._watchers))
        try:
            yield {}
        finally:
            self._cancel_tasks()
            pending = [s.task for s in self._watchers.values() if s.task]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            logger.info("AgentRuntime: all watchers stopped")

    # ── Observability ─────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return a JSON-serialisable health snapshot of all registered watchers."""
        watchers = [
            {
                "name": state.name,
                "interval_seconds": state.interval_seconds,
                "run_count": state.run_count,
                "last_run": state.last_run.isoformat() if state.last_run else None,
                "last_error": state.last_error,
                "healthy": state.last_error is None,
            }
            for state in self._watchers.values()
        ]
        return {
            "watcher_count": len(self._watchers),
            "watchers": watchers,
        }

"""AgentRuntime — lightweight background-task scheduler for the Skills MCP server.

Bridges the gap between "skill registered as an MCP tool" (call-driven) and
"skill running autonomously on a schedule" (proactive). Every watcher is an
async coroutine that the runtime starts when the server comes up and cancels
cleanly when it shuts down.

Usage (in server.py)::

    from runtime.agent_runtime import get_runtime
    from runtime.watchers import register_watchers

    runtime = get_runtime()          # always returns the same singleton
    register_watchers(runtime)

    mcp = FastMCP("...", lifespan=runtime.lifespan)

A skill module can also register its own watchers — because ``get_runtime()``
returns the same instance, watchers registered inside ``register(mcp)`` will
be picked up by the lifespan that server.py installs::

    # inside a skill's register(mcp) function
    from runtime.agent_runtime import get_runtime
    get_runtime().watch("my_watcher", interval_seconds=300, coro_fn=_my_watcher)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Coroutine

logger = logging.getLogger(__name__)

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
            raise ValueError(f"Watcher '{name}' is already registered")
        self._watchers[name] = _WatcherState(name, interval_seconds, coro_fn)
        logger.info(
            "AgentRuntime: registered watcher '%s' (interval=%.0fs)",
            name, interval_seconds,
        )

    # ── Execution loop ────────────────────────────────────────────────────────

    async def _run_watcher(self, state: _WatcherState) -> None:
        """Infinite loop for one watcher.

        Pattern: execute → record outcome → sleep → repeat.
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
        Starts all registered watchers on entry (via ``start()``) and cancels
        + awaits them on exit so the process shuts down cleanly.

        Example::

            mcp = FastMCP("my-server", lifespan=runtime.lifespan)
        """
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

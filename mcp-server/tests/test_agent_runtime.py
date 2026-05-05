"""Tests for load_skills() register_watchers() hook and AgentRuntime.

Verifies that:
- load_skills() calls register_watchers(runtime) on any skill module that exports it
- Modules without register_watchers() are unaffected
- AgentRuntime.watch() correctly tracks registered watchers
"""

from __future__ import annotations

import importlib
import sys
import types
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_fake_module(name: str, has_register_watchers: bool = True) -> types.ModuleType:
    """Return a minimal fake skill module with tracking lists."""
    mod = types.ModuleType(name)
    mod._register_calls = []
    mod._watcher_calls = []

    def register(mcp):
        mod._register_calls.append(mcp)

    mod.register = register

    if has_register_watchers:
        def register_watchers(runtime):
            mod._watcher_calls.append(runtime)

        mod.register_watchers = register_watchers

    return mod


class _MockMCP:
    """Minimal MCP stub — just needs to be passable."""


class _MockRuntime:
    """Minimal runtime stub that records watch() calls."""

    def __init__(self):
        self.watched = []

    def watch(self, name, interval_seconds, coro_fn):
        self.watched.append({"name": name, "interval_seconds": interval_seconds, "coro_fn": coro_fn})


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_load_skills_calls_register_watchers_when_runtime_provided(monkeypatch):
    """load_skills() must call register_watchers(runtime) on supporting modules."""
    import pkgutil

    fake_mod = _make_fake_module("skills.fake_skill", has_register_watchers=True)
    mcp = _MockMCP()
    runtime = _MockRuntime()

    def fake_iter_modules(path):
        yield None, "fake_skill", False

    monkeypatch.setattr(pkgutil, "iter_modules", fake_iter_modules)
    monkeypatch.setattr(importlib, "import_module", lambda name: fake_mod)

    import skills as _skills_pkg
    if not hasattr(_skills_pkg, "__path__"):
        _skills_pkg.__path__ = []

    from skills import load_skills
    load_skills(mcp, runtime=runtime)

    assert len(fake_mod._register_calls) == 1, "register(mcp) must be called once"
    assert fake_mod._register_calls[0] is mcp

    assert len(fake_mod._watcher_calls) == 1, "register_watchers(runtime) must be called once"
    assert fake_mod._watcher_calls[0] is runtime


def test_load_skills_skips_register_watchers_when_no_runtime(monkeypatch):
    """load_skills() without runtime must not call register_watchers()."""
    import pkgutil

    fake_mod = _make_fake_module("skills.fake_skill_no_rt", has_register_watchers=True)
    mcp = _MockMCP()

    def fake_iter_modules(path):
        yield None, "fake_skill_no_rt", False

    monkeypatch.setattr(pkgutil, "iter_modules", fake_iter_modules)
    monkeypatch.setattr(importlib, "import_module", lambda name: fake_mod)

    import skills as _skills_pkg
    if not hasattr(_skills_pkg, "__path__"):
        _skills_pkg.__path__ = []

    from skills import load_skills
    load_skills(mcp)  # no runtime

    assert len(fake_mod._register_calls) == 1, "register(mcp) must still be called"
    assert len(fake_mod._watcher_calls) == 0, "register_watchers must NOT be called without runtime"


def test_load_skills_ignores_missing_register_watchers(monkeypatch):
    """load_skills() must not fail if a module has no register_watchers()."""
    import pkgutil

    fake_mod = _make_fake_module("skills.plain_skill", has_register_watchers=False)
    mcp = _MockMCP()
    runtime = _MockRuntime()

    def fake_iter_modules(path):
        yield None, "plain_skill", False

    monkeypatch.setattr(pkgutil, "iter_modules", fake_iter_modules)
    monkeypatch.setattr(importlib, "import_module", lambda name: fake_mod)

    import skills as _skills_pkg
    if not hasattr(_skills_pkg, "__path__"):
        _skills_pkg.__path__ = []

    from skills import load_skills
    load_skills(mcp, runtime=runtime)  # should not raise

    assert len(fake_mod._register_calls) == 1
    assert not hasattr(fake_mod, "_watcher_calls") or fake_mod._watcher_calls == []


def test_agent_runtime_watch_registers_watcher():
    """AgentRuntime.watch() must store the watcher and make it visible via status()."""
    from runtime.agent_runtime import AgentRuntime

    rt = AgentRuntime()

    async def _noop():
        pass

    rt.watch("test_watcher", interval_seconds=60.0, coro_fn=_noop)

    status = rt.status()
    assert status["watcher_count"] == 1
    names = [w["name"] for w in status["watchers"]]
    assert "test_watcher" in names


def test_agent_runtime_watch_rejects_duplicate():
    """AgentRuntime.watch() must raise ValueError on a duplicate watcher name."""
    from runtime.agent_runtime import AgentRuntime

    rt = AgentRuntime()

    async def _noop():
        pass

    rt.watch("dup_watcher", interval_seconds=60.0, coro_fn=_noop)

    with pytest.raises(ValueError, match="dup_watcher"):
        rt.watch("dup_watcher", interval_seconds=60.0, coro_fn=_noop)


def test_behavioral_atoms_exports_register_watchers():
    """skills.behavioral_atoms must export a register_watchers() callable."""
    from skills import behavioral_atoms

    assert hasattr(behavioral_atoms, "register_watchers"), (
        "behavioral_atoms must export register_watchers(runtime)"
    )
    assert callable(behavioral_atoms.register_watchers)


def test_behavioral_atoms_register_watchers_registers_checkin_atom_watcher():
    """behavioral_atoms.register_watchers() must register checkin_atom_watcher."""
    from skills import behavioral_atoms

    rt = _MockRuntime()
    behavioral_atoms.register_watchers(rt)

    names = [w["name"] for w in rt.watched]
    assert "checkin_atom_watcher" in names, (
        f"Expected 'checkin_atom_watcher' to be registered; got: {names}"
    )

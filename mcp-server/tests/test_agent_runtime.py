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


def test_agent_runtime_watch_skips_duplicate_with_warning(caplog):
    """AgentRuntime.watch() must log a warning and skip re-registration on a duplicate name."""
    import logging
    from runtime.agent_runtime import AgentRuntime

    rt = AgentRuntime()

    async def _noop():
        pass

    rt.watch("dup_watcher", interval_seconds=60.0, coro_fn=_noop)

    with caplog.at_level(logging.WARNING, logger="runtime.agent_runtime"):
        rt.watch("dup_watcher", interval_seconds=60.0, coro_fn=_noop)

    assert any("dup_watcher" in r.message and r.levelno == logging.WARNING for r in caplog.records), (
        "Expected a WARNING log mentioning 'dup_watcher' on duplicate registration"
    )
    assert rt.status()["watcher_count"] == 1, "Duplicate registration must not add a second watcher"


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


# ── crisis_escalation watcher ─────────────────────────────────────────────────

def test_crisis_escalation_exports_register_watchers():
    """skills.crisis_escalation must export a register_watchers() callable."""
    from skills import crisis_escalation

    assert hasattr(crisis_escalation, "register_watchers"), (
        "crisis_escalation must export register_watchers(runtime)"
    )
    assert callable(crisis_escalation.register_watchers)


def test_crisis_escalation_register_watchers_registers_crisis_scan_watcher():
    """crisis_escalation.register_watchers() must register crisis_scan_watcher."""
    from skills import crisis_escalation

    rt = _MockRuntime()
    crisis_escalation.register_watchers(rt)

    names = [w["name"] for w in rt.watched]
    assert "crisis_scan_watcher" in names, (
        f"Expected 'crisis_scan_watcher' to be registered; got: {names}"
    )


def test_crisis_escalation_watcher_interval():
    """crisis_scan_watcher must be registered with a 3600-second interval."""
    from skills import crisis_escalation

    rt = _MockRuntime()
    crisis_escalation.register_watchers(rt)

    watcher = next(w for w in rt.watched if w["name"] == "crisis_scan_watcher")
    assert watcher["interval_seconds"] == 3600.0, (
        f"Expected 3600.0s interval; got {watcher['interval_seconds']}"
    )


# ── care_gap watcher ──────────────────────────────────────────────────────────

def test_care_gap_exports_register_watchers():
    """skills.care_gap must export a register_watchers() callable."""
    from skills import care_gap

    assert hasattr(care_gap, "register_watchers"), (
        "care_gap must export register_watchers(runtime)"
    )
    assert callable(care_gap.register_watchers)


def test_care_gap_register_watchers_registers_care_gap_watcher():
    """care_gap.register_watchers() must register care_gap_watcher."""
    from skills import care_gap

    rt = _MockRuntime()
    care_gap.register_watchers(rt)

    names = [w["name"] for w in rt.watched]
    assert "care_gap_watcher" in names, (
        f"Expected 'care_gap_watcher' to be registered; got: {names}"
    )


def test_care_gap_watcher_interval():
    """care_gap_watcher must be registered with an 86400-second interval."""
    from skills import care_gap

    rt = _MockRuntime()
    care_gap.register_watchers(rt)

    watcher = next(w for w in rt.watched if w["name"] == "care_gap_watcher")
    assert watcher["interval_seconds"] == 86400.0, (
        f"Expected 86400.0s interval; got {watcher['interval_seconds']}"
    )


def test_care_gap_exports_register():
    """skills.care_gap must export a register() callable (even if no MCP tools)."""
    from skills import care_gap

    assert hasattr(care_gap, "register"), "care_gap must export register(mcp)"
    assert callable(care_gap.register)


def test_watchers_py_has_no_register_watchers():
    """runtime.watchers must no longer export register_watchers() after migration."""
    from runtime import watchers

    assert not hasattr(watchers, "register_watchers"), (
        "runtime.watchers should be empty — watchers are now in skill files"
    )

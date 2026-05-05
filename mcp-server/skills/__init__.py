"""Skill auto-discovery loader.

Scans the skills package for modules with a ``register(mcp)`` function
and calls each one to register MCP tools.

Optional hook: if a module also exports ``register_watchers(runtime)``,
the loader calls it with the AgentRuntime singleton so the skill can own
its autonomous background behaviour alongside its MCP tool surface.
"""

import importlib
import logging
import pkgutil
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def load_skills(mcp, runtime=None):
    """Discover and register all skill modules.

    Args:
        mcp:     FastMCP instance — passed to each module's ``register(mcp)``.
        runtime: Optional AgentRuntime singleton.  When provided, any module
                 that also exports ``register_watchers(runtime)`` will have
                 that hook called automatically, allowing the skill to declare
                 its own background watchers without editing watchers.py.
    """
    import skills

    for _, modname, _ in pkgutil.iter_modules(skills.__path__):
        if modname in ("base",) or modname.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"skills.{modname}")
            if hasattr(module, "register"):
                module.register(mcp)
                logger.info("Loaded skill: %s", modname)
            else:
                logger.warning("Skill module %s has no register() function", modname)

            if runtime is not None and hasattr(module, "register_watchers"):
                module.register_watchers(runtime)
                logger.info("Loaded watchers for skill: %s", modname)
        except Exception as e:
            logger.error("Failed to load skill %s: %s", modname, e)

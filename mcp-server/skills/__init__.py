"""Skill auto-discovery loader.

Scans the skills package for modules with a ``register(mcp)`` function
and calls each one to register MCP tools.
"""

import importlib
import logging
import pkgutil
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def load_skills(mcp):
    """Discover and register all skill modules."""
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
        except Exception as e:
            logger.error("Failed to load skill %s: %s", modname, e)

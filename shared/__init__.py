"""Shared Python utilities for the Ambient Patient Companion.

Modules here are imported by all three MCP servers:
  - ambient-clinical-intelligence (server/)
  - ambient-skills-companion (mcp-server/)
  - ambient-ingestion (ingestion/)

The servers each add the repo root to sys.path before importing from
this package; there is no packaged install.
"""

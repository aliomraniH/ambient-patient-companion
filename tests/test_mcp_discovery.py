"""MCP Discovery Tests — DN-1 through DN-17.

Verifies that all three FastMCP server names are consistent across:
  - Source module FastMCP constructor calls
  - .mcp.json discovery file
  - Config Dashboard server config
  - start.sh production startup script
  - next.config.ts Next.js proxy rewrites
  - replit.md and submission/README.md documentation

Prompt spec: tests/prompts/replit-agent-mcp-discovery-tests.md

Tests that require a live server on port 8001 are automatically skipped
when the server is not reachable, matching the pattern in test_mcp_smoke.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

# ── Constants ─────────────────────────────────────────────────────────────────

BASE = "http://localhost:8001"

CLINICAL_NAME   = "ClinicalIntelligence"
SKILLS_NAME     = "PatientCompanion"
INGESTION_NAME  = "PatientIngestion"

CLINICAL_MCP_KEY    = "ambient-clinical-intelligence"
SKILLS_MCP_KEY      = "ambient-skills-companion"
INGESTION_MCP_KEY   = "ambient-ingestion"

# ── Server availability gate (same pattern as test_mcp_smoke.py) ──────────────

def _server_up() -> bool:
    try:
        r = httpx.get(f"{BASE}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


_skip_no_server = pytest.mark.skipif(
    not _server_up(),
    reason="Clinical MCP server not reachable on port 8001",
)


# ── Class 1: TestServerNaming (DN-1 to DN-6) ─────────────────────────────────

class TestServerNaming:
    """FastMCP constructor names must be consistent across all discovery surfaces."""

    def test_clinical_mcp_name(self):
        """DN-1: server/mcp_server.py must declare FastMCP("ClinicalIntelligence")."""
        src = Path("server/mcp_server.py").read_text()
        assert f'FastMCP("{CLINICAL_NAME}")' in src, (
            f"Expected FastMCP(\"{CLINICAL_NAME}\") in server/mcp_server.py"
        )

    def test_skills_mcp_name(self):
        """DN-2: mcp-server/server.py must declare FastMCP("PatientCompanion")."""
        src = Path("mcp-server/server.py").read_text()
        assert f'FastMCP("{SKILLS_NAME}")' in src, (
            f"Expected FastMCP(\"{SKILLS_NAME}\") in mcp-server/server.py"
        )

    def test_ingestion_mcp_name(self):
        """DN-3: ingestion/server.py must declare FastMCP("PatientIngestion")."""
        src = Path("ingestion/server.py").read_text()
        assert f'FastMCP("{INGESTION_NAME}")' in src, (
            f"Expected FastMCP(\"{INGESTION_NAME}\") in ingestion/server.py"
        )

    def test_mcp_json_exists_and_valid(self):
        """DN-4: .mcp.json must exist, be valid JSON, and have a top-level mcpServers key."""
        mcp_json = Path(".mcp.json")
        assert mcp_json.exists(), ".mcp.json not found — create it for MCP client discovery"
        data = json.loads(mcp_json.read_text())
        assert "mcpServers" in data, (
            ".mcp.json must have a top-level 'mcpServers' key"
        )

    def test_mcp_json_has_all_three_servers(self):
        """DN-5: .mcp.json must list all three server keys."""
        data = json.loads(Path(".mcp.json").read_text())
        servers = data.get("mcpServers", {})
        for key in (CLINICAL_MCP_KEY, SKILLS_MCP_KEY, INGESTION_MCP_KEY):
            assert key in servers, (
                f".mcp.json mcpServers is missing '{key}'. "
                f"Present keys: {list(servers.keys())}"
            )

    def test_dashboard_config_uses_port_8001(self):
        """DN-6: replit_dashboard/server.py must reference localhost:8001 for the MCP default."""
        src = Path("replit_dashboard/server.py").read_text()
        assert "localhost:8001" in src, (
            "replit_dashboard/server.py must reference localhost:8001 as the default MCP URL"
        )


# ── Class 2: TestHealthCheckContract (DN-7 to DN-9) ──────────────────────────

class TestHealthCheckContract:
    """GET /health on port 8001 must return the documented contract shape."""

    @_skip_no_server
    def test_health_returns_200(self):
        """DN-7: /health must respond with HTTP 200."""
        r = httpx.get(f"{BASE}/health", timeout=3)
        assert r.status_code == 200, (
            f"Expected 200 from /health, got {r.status_code}"
        )

    @_skip_no_server
    def test_health_server_name(self):
        """DN-8: /health body 'server' field must be exactly 'ClinicalIntelligence'."""
        r = httpx.get(f"{BASE}/health", timeout=3)
        body = r.json()
        assert body.get("server") == CLINICAL_NAME, (
            f"Expected server='{CLINICAL_NAME}', got server='{body.get('server')}'"
        )

    @_skip_no_server
    def test_health_response_shape(self):
        """DN-9: /health body must have ok=True, server (str), version (non-empty str)."""
        r = httpx.get(f"{BASE}/health", timeout=3)
        body = r.json()
        assert body.get("ok") is True, f"Expected ok=True, got: {body}"
        assert isinstance(body.get("server"), str), "server must be a string"
        version = body.get("version", "")
        assert isinstance(version, str) and version, (
            f"version must be a non-empty string, got: {version!r}"
        )


# ── Class 3: TestStartupTopology (DN-10 to DN-15) ────────────────────────────

class TestStartupTopology:
    """start.sh must launch all three MCP servers; next.config.ts must proxy all three."""

    def test_start_sh_exists(self):
        """DN-10: start.sh must exist and be non-empty."""
        p = Path("start.sh")
        assert p.exists(), "start.sh not found"
        assert p.stat().st_size > 0, "start.sh is empty"

    def test_start_sh_launches_clinical_8001(self):
        """DN-11: start.sh must launch server.mcp_server and reference port 8001."""
        src = Path("start.sh").read_text()
        assert "server.mcp_server" in src, (
            "start.sh must launch 'python -m server.mcp_server' for the Clinical MCP server"
        )
        assert "8001" in src, (
            "start.sh must reference port 8001 for the Clinical MCP server"
        )

    def test_start_sh_launches_skills_8002(self):
        """DN-12: start.sh must launch the Skills/PatientCompanion server on port 8002."""
        src = Path("start.sh").read_text()
        assert "8002" in src, (
            "start.sh must reference port 8002 to launch the Skills MCP server (PatientCompanion)"
        )
        assert "mcp-server" in src or "mcp_server" in src.lower() or "PatientCompanion" in src or "server.py" in src, (
            "start.sh must launch the mcp-server/server.py (PatientCompanion) process"
        )

    def test_start_sh_launches_ingestion_8003(self):
        """DN-13: start.sh must launch the Ingestion/PatientIngestion server on port 8003."""
        src = Path("start.sh").read_text()
        assert "8003" in src, (
            "start.sh must reference port 8003 to launch the Ingestion MCP server (PatientIngestion)"
        )
        assert "ingestion" in src.lower(), (
            "start.sh must launch the ingestion server (PatientIngestion) process"
        )

    def test_next_config_clinical_proxy(self):
        """DN-14: next.config.ts must proxy '/mcp' prefix to localhost:8001."""
        src = Path("replit-app/next.config.ts").read_text()
        assert "localhost:8001" in src, (
            "next.config.ts must define a proxy rewrite to localhost:8001"
        )
        assert '"/mcp"' in src or "'/mcp'" in src or '`/mcp`' in src or 'source: "/mcp"' in src, (
            "next.config.ts must map the '/mcp' source route to the Clinical MCP server"
        )

    def test_next_config_all_three_proxies(self):
        """DN-15: next.config.ts must proxy to all three MCP servers (8001, 8002, 8003)."""
        src = Path("replit-app/next.config.ts").read_text()
        for port in ("8001", "8002", "8003"):
            assert port in src, (
                f"next.config.ts must reference port {port} in its proxy rewrites"
            )


# ── Class 4: TestCrossServerConsistency (DN-16 to DN-17) ─────────────────────

class TestCrossServerConsistency:
    """Documentation must accurately reference all three FastMCP server names."""

    def test_replit_md_names_all_servers(self):
        """DN-16: replit.md must mention all three FastMCP server names."""
        src = Path("replit.md").read_text()
        for name in (CLINICAL_NAME, SKILLS_NAME, INGESTION_NAME):
            assert name in src, (
                f"replit.md is missing FastMCP server name '{name}'"
            )

    def test_submission_readme_health_endpoint(self):
        """DN-17: submission/README.md must reference ClinicalIntelligence in health-check docs."""
        src = Path("submission/README.md").read_text()
        assert CLINICAL_NAME in src, (
            f"submission/README.md must reference '{CLINICAL_NAME}' "
            "(e.g. in the /health endpoint documentation)"
        )

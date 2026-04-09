# Replit Agent Prompt: Add MCP Discovery Verification Tests

## Background

We just fixed three architectural mismatches in the MCP server setup:

1. **Server naming**: The `FastMCP()` constructor names now match the Claude Web connector names documented in README.md and emitted by the config dashboard:
   - `server/mcp_server.py:62` — `FastMCP("ambient-clinical-intelligence")` (was `"ClinicalIntelligence"`)
   - `mcp-server/server.py:12` — `FastMCP("ambient-skills-companion")` (was `"PatientCompanion"`)
   - `ingestion/server.py:12` — `FastMCP("ambient-ingestion")` (was `"PatientIngestion"`)

2. **Health check**: `server/mcp_server.py:1932` now returns `"server": "ambient-clinical-intelligence"` in the `/health` JSON response.

3. **Production startup**: `start.sh` now starts all 3 MCP servers (ports 8001, 8002, 8003), not just port 8001. The Skills server (8002) and Ingestion server (8003) were previously missing from production startup.

We need tests to prevent these from regressing.

---

## What to create

Create a new test file: **`tests/test_mcp_discovery.py`**

This file should contain three test classes that verify MCP server naming consistency, health check contracts, and startup topology. Follow the existing patterns in `tests/test_mcp_smoke.py` exactly.

---

## Test specifications

### Class 1: `TestServerNaming`

These tests run **offline** (no live server needed). They import the `mcp` object from each server module and verify the FastMCP name matches the documented Claude Web connector name.

| Test ID | Test | What to assert |
|---------|------|----------------|
| DN-1 | `test_clinical_server_name` | Import `mcp` from `server.mcp_server`. Assert `mcp.name == "ambient-clinical-intelligence"`. |
| DN-2 | `test_skills_server_name` | Import `mcp` from `mcp-server/server.py` (use `importlib` since the directory has a hyphen — see implementation notes below). Assert `mcp.name == "ambient-skills-companion"`. |
| DN-3 | `test_ingestion_server_name` | Import `mcp` from `ingestion.server`. Assert `mcp.name == "ambient-ingestion"`. |
| DN-4 | `test_names_match_readme_table` | Parse `README.md` lines 255-259 for the "Public URLs" connector name table. Extract the three connector names from the `Connector Name` column. Assert each matches the corresponding `mcp.name`. This catches future docs-vs-code drift. |
| DN-5 | `test_names_match_dashboard_config` | Import `_build_mcp_config` from `replit_dashboard.server`. Call it with a dummy domain like `"test.repl.co"`. Assert the returned dict's `mcpServers` keys include `"ambient-clinical-intelligence"`. |
| DN-6 | `test_mcp_json_key_matches_skills_name` | Read `mcp-server/.mcp.json`, parse as JSON. Assert the single key inside `mcpServers` equals `"ambient-skills-companion"`. |

**Implementation notes for DN-2 (skills server import):**
The skills server lives in `mcp-server/server.py`. The directory name `mcp-server` contains a hyphen, which is not a valid Python identifier. Use this pattern:
```python
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location(
    "mcp_server_skills",
    pathlib.Path(__file__).resolve().parents[1] / "mcp-server" / "server.py",
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
skills_mcp = mod.mcp
```
If the import fails (e.g., missing `fastmcp` in the test environment), skip the test with `pytest.importorskip("fastmcp")` at the top of the class.

---

### Class 2: `TestHealthCheckContract`

These tests require a **live server** on port 8001. Use the same skip pattern as `tests/test_mcp_smoke.py`:

```python
import httpx

BASE_CLINICAL = "http://localhost:8001"

def _server_up(base: str) -> bool:
    try:
        return httpx.get(f"{base}/health", timeout=2).status_code == 200
    except Exception:
        return False

_skip_no_clinical = pytest.mark.skipif(
    not _server_up(BASE_CLINICAL),
    reason="Clinical MCP server not reachable on port 8001",
)
```

| Test ID | Test | What to assert |
|---------|------|----------------|
| DN-7 | `test_health_server_field_matches_mcp_name` | `GET /health` on port 8001. Assert `body["server"] == "ambient-clinical-intelligence"`. |
| DN-8 | `test_health_server_field_is_string` | `GET /health`. Assert `isinstance(body["server"], str)`. |
| DN-9 | `test_health_response_shape` | `GET /health`. Assert response has exactly keys `ok`, `server`, `version` (no extra, no missing). |

---

### Class 3: `TestStartupTopology`

These tests verify the `start.sh` script declares all three server startups. They run **offline** (parse the shell script as text, no live servers needed).

| Test ID | Test | What to assert |
|---------|------|----------------|
| DN-10 | `test_start_sh_launches_clinical_server` | Read `start.sh`. Assert it contains `MCP_PORT=8001` and `python -m server.mcp_server`. |
| DN-11 | `test_start_sh_launches_skills_server` | Read `start.sh`. Assert it contains `MCP_PORT=8002` and `python server.py` (the skills server command). |
| DN-12 | `test_start_sh_launches_ingestion_server` | Read `start.sh`. Assert it contains `MCP_PORT=8003` and `python -m ingestion.server`. |
| DN-13 | `test_start_sh_all_servers_backgrounded` | Read `start.sh`. Count occurrences of ` &` (backgrounding). Assert >= 4 (3 MCP servers + 1 dashboard, all backgrounded before Next.js runs in foreground). |
| DN-14 | `test_next_config_proxy_covers_all_servers` | Read `replit-app/next.config.ts`. Assert it contains all three proxy source paths: `/mcp`, `/mcp-skills`, `/mcp-ingestion`. |
| DN-15 | `test_next_config_tool_counts_accurate` | Read `replit-app/next.config.ts`. Extract the tool count numbers from the comments on lines 5-7 (e.g., "19 tools", "18 tools", "1 tool"). Assert Clinical = 19, Skills = 18, Ingestion = 1. Use a regex like `r"— (\d+) tools?"`. |

---

### Bonus Class 4: `TestCrossServerConsistency` (optional but valuable)

| Test ID | Test | What to assert |
|---------|------|----------------|
| DN-16 | `test_replit_md_documents_all_three_servers` | Read `replit.md`. Assert it contains all three Claude Web names: `ambient-clinical-intelligence`, `ambient-skills-companion`, `ambient-ingestion`. |
| DN-17 | `test_submission_readme_health_check_matches` | Read `submission/README.md`. Find the health check example JSON. Assert the `"server"` value in that example equals `"ambient-clinical-intelligence"`. |

---

## File structure and patterns to follow

Follow these conventions from the existing test suite:

1. **File header**: Docstring explaining what the file tests, similar to `tests/test_mcp_smoke.py` lines 1-11.

2. **Imports**: `pytest`, `httpx`, `pathlib`, `json`, `re`, `inspect`. Use `from __future__ import annotations`.

3. **Path resolution**: Use `pathlib.Path(__file__).resolve().parents[1]` to get the repo root (test file is one level deep in `tests/`).

4. **Skip pattern**: Use `pytest.mark.skipif` with helper functions for live-server tests. Never let a missing server cause a test failure — always skip gracefully.

5. **No mocking**: Import real modules and read real files. Don't mock FastMCP or file contents.

6. **pytest.ini**: The existing `pytest.ini` at the repo root has `asyncio_mode = auto` and `--import-mode=importlib`. All tests in this file are synchronous (no async needed).

7. **Test naming**: Use `test_` prefix. Group related tests in classes. Add a short docstring to each test explaining what it guards against.

---

## How to verify

```bash
# Run just the new discovery tests
python -m pytest tests/test_mcp_discovery.py -v

# Run alongside existing smoke tests to ensure no conflicts
python -m pytest tests/test_mcp_smoke.py tests/test_mcp_discovery.py -v

# Offline tests only (no server needed) — should be DN-1 through DN-6, DN-10 through DN-17
python -m pytest tests/test_mcp_discovery.py -v -k "not HealthCheck"
```

All DN-1 through DN-6 and DN-10 through DN-17 tests should pass without any running servers. DN-7 through DN-9 will skip if port 8001 is not running, which is fine.

---

## Do NOT

- Do not modify any existing test files
- Do not add tests to `test_mcp_smoke.py` — keep the new tests in a separate file
- Do not mock the FastMCP constructor or file reads
- Do not add any new dependencies — only use `pytest`, `httpx`, `pathlib`, `json`, `re`, `importlib` (all already available)
- Do not create fixtures in a separate conftest.py for this file — keep everything self-contained

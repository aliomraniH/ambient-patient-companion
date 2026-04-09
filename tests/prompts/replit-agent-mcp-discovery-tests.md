# MCP Discovery Test Suite — Prompt Spec

**File:** `tests/test_mcp_discovery.py`  
**Scope:** 17 tests across 4 classes (DN-1 through DN-17)  
**Pattern:** Same conventions as `tests/test_mcp_smoke.py` — skip-based server gates, direct
file/module reads for offline verification, no mocking.

---

## Background

The Ambient Patient Companion runs **three FastMCP servers**:

| FastMCP Name | Port | Module | Proxy prefix |
|---|---|---|---|
| `ClinicalIntelligence` | 8001 | `server.mcp_server` | `/mcp` |
| `PatientCompanion` | 8002 | `mcp-server/server.py` | `/mcp-skills` |
| `PatientIngestion` | 8003 | `ingestion.server` | `/mcp-ingestion` |

These names must be **consistent** across:
- The FastMCP constructor call in each server module
- The `.mcp.json` discovery file (used by Claude Desktop / MCP clients)
- The Config Dashboard server config
- `start.sh` (production startup — must launch all three)
- `next.config.ts` (Next.js reverse-proxy rewrites)
- `replit.md` (project documentation)
- `submission/README.md` (submission documentation)

---

## Class 1 — `TestServerNaming` (DN-1 to DN-6)

**Requires server:** No — reads source files directly.  
**Guards:** FastMCP constructor names match everywhere a client discovers them.

| ID | Test | What to check | Expected |
|---|---|---|---|
| DN-1 | `test_clinical_mcp_name` | `server/mcp_server.py` source | Contains `FastMCP("ClinicalIntelligence")` |
| DN-2 | `test_skills_mcp_name` | `mcp-server/server.py` source | Contains `FastMCP("PatientCompanion")` |
| DN-3 | `test_ingestion_mcp_name` | `ingestion/server.py` source | Contains `FastMCP("PatientIngestion")` |
| DN-4 | `test_mcp_json_exists_and_valid` | `.mcp.json` file | Exists; valid JSON; top-level key `mcpServers` present |
| DN-5 | `test_mcp_json_has_all_three_servers` | `.mcp.json` `mcpServers` dict | Keys include `ambient-clinical-intelligence`, `ambient-skills-companion`, `ambient-ingestion` |
| DN-6 | `test_dashboard_config_uses_port_8001` | `replit_dashboard/server.py` source | References `localhost:8001` for MCP default |

**Implementation notes:**
- Open each source file as text and `assert "FastMCP(\"<Name>\")" in source`.
- For `.mcp.json`, use `json.load`; assert `"mcpServers" in data`.
- All 6 tests are offline (no HTTP).

---

## Class 2 — `TestHealthCheckContract` (DN-7 to DN-9)

**Requires server:** Yes — Clinical MCP on port 8001.  
**Skip guard:** `pytest.mark.skipif(not _server_up(), reason="...")` (same pattern as smoke tests).  
**Guards:** The `/health` endpoint returns the documented contract shape.

| ID | Test | What to check | Expected |
|---|---|---|---|
| DN-7 | `test_health_returns_200` | `GET http://localhost:8001/health` | HTTP 200 |
| DN-8 | `test_health_server_name` | Response body `server` field | Exactly `"ClinicalIntelligence"` |
| DN-9 | `test_health_response_shape` | Response body keys | `ok == True`, `server` is str, `version` is non-empty str |

**Implementation notes:**
- Reuse the `_server_up()` helper and `_skip_no_server` mark from `test_mcp_smoke.py`
  (either import or re-declare — re-declaring is cleaner since this is a separate file).
- Use `httpx.get(f"{BASE}/health", timeout=3)`.

---

## Class 3 — `TestStartupTopology` (DN-10 to DN-15)

**Requires server:** No — reads `start.sh` and `next.config.ts` as text.  
**Guards:** The production startup script launches all three MCP servers; the Next.js proxy
routes are wired to the correct ports.

| ID | Test | What to check | Expected |
|---|---|---|---|
| DN-10 | `test_start_sh_exists` | `start.sh` file | File exists and is non-empty |
| DN-11 | `test_start_sh_launches_clinical_8001` | `start.sh` source | Contains `server.mcp_server` and `8001` |
| DN-12 | `test_start_sh_launches_skills_8002` | `start.sh` source | Contains Skills/PatientCompanion launch and `8002` |
| DN-13 | `test_start_sh_launches_ingestion_8003` | `start.sh` source | Contains Ingestion server launch and `8003` |
| DN-14 | `test_next_config_clinical_proxy` | `replit-app/next.config.ts` source | `localhost:8001` mapped to `/mcp` prefix |
| DN-15 | `test_next_config_all_three_proxies` | `replit-app/next.config.ts` source | `localhost:8002` and `localhost:8003` both appear |

**Implementation notes:**
- `pathlib.Path("start.sh").read_text()` — assert substrings.
- `pathlib.Path("replit-app/next.config.ts").read_text()` — assert substrings.

---

## Class 4 — `TestCrossServerConsistency` (DN-16 to DN-17)

**Requires server:** No — reads documentation files.  
**Guards:** Documentation accurately names all three FastMCP servers.

| ID | Test | What to check | Expected |
|---|---|---|---|
| DN-16 | `test_replit_md_names_all_servers` | `replit.md` source | Contains all three names: `ClinicalIntelligence`, `PatientCompanion`, `PatientIngestion` |
| DN-17 | `test_submission_readme_health_endpoint` | `submission/README.md` source | References `ClinicalIntelligence` in health-check documentation |

**Implementation notes:**
- `pathlib.Path("replit.md").read_text()`.
- `pathlib.Path("submission/README.md").read_text()`.

---

## Running the suite

```bash
# All 17 tests (offline tests run even without a live server):
python -m pytest tests/test_mcp_discovery.py -v

# Offline only (skip the 3 live-server tests):
python -m pytest tests/test_mcp_discovery.py -v -k "not HealthCheck"

# Only the live-server tests:
python -m pytest tests/test_mcp_discovery.py -v -k "HealthCheck"
```

---

## Expected result when everything is wired correctly

```
tests/test_mcp_discovery.py::TestServerNaming::test_clinical_mcp_name         PASSED
tests/test_mcp_discovery.py::TestServerNaming::test_skills_mcp_name            PASSED
tests/test_mcp_discovery.py::TestServerNaming::test_ingestion_mcp_name         PASSED
tests/test_mcp_discovery.py::TestServerNaming::test_mcp_json_exists_and_valid  PASSED
tests/test_mcp_discovery.py::TestServerNaming::test_mcp_json_has_all_three_servers PASSED
tests/test_mcp_discovery.py::TestServerNaming::test_dashboard_config_uses_port_8001 PASSED
tests/test_mcp_discovery.py::TestHealthCheckContract::test_health_returns_200  PASSED
tests/test_mcp_discovery.py::TestHealthCheckContract::test_health_server_name  PASSED
tests/test_mcp_discovery.py::TestHealthCheckContract::test_health_response_shape PASSED
tests/test_mcp_discovery.py::TestStartupTopology::test_start_sh_exists         PASSED
tests/test_mcp_discovery.py::TestStartupTopology::test_start_sh_launches_clinical_8001 PASSED
tests/test_mcp_discovery.py::TestStartupTopology::test_start_sh_launches_skills_8002   PASSED
tests/test_mcp_discovery.py::TestStartupTopology::test_start_sh_launches_ingestion_8003 PASSED
tests/test_mcp_discovery.py::TestStartupTopology::test_next_config_clinical_proxy      PASSED
tests/test_mcp_discovery.py::TestStartupTopology::test_next_config_all_three_proxies   PASSED
tests/test_mcp_discovery.py::TestCrossServerConsistency::test_replit_md_names_all_servers PASSED
tests/test_mcp_discovery.py::TestCrossServerConsistency::test_submission_readme_health_endpoint PASSED

17 passed in 0.XXs
```

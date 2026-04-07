# Ambient Patient Companion

A multi-agent AI system that generates a continuously derived patient health UX from Role x Context x Patient State x Time.

## Architecture

```
S = f(R, C, P, T)  →  optimal clinical surface
```

Seven specialized agents communicate through a shared MCP tool registry. All agents read from a local PostgreSQL warehouse. No agent calls an external API directly.

## Project Structure

```
ambient-patient-companion/
├── replit-app/          ← Next.js 16 frontend (main web UI, port 5000)
│   ├── app/             ← App Router pages + API routes
│   │   └── api/mcp/[port]/[[...segments]]/route.ts  ← MCP proxy (→ localhost:8001/2/3)
│   ├── components/      ← React UI components
│   └── lib/db.ts        ← PostgreSQL pool (pg)
├── server/              ← Phase 1 Clinical Intelligence FastMCP server (port 8001)
│   ├── mcp_server.py    ← FastMCP server: 17 tools + REST wrappers + guardrails
│   ├── guardrails/      ← input_validator, output_validator, clinical_rules
│   └── deliberation/
│       ├── json_utils.py  ← strip_markdown_fences() — handles LLM code-fence wrapping
│       ├── analyst.py     ← Phase 1: strips fences before model_validate_json
│       └── critic.py      ← Phase 2: strips fences on CrossCritique + RevisedAnalysis
├── mcp-server/          ← FastMCP Python agent server
│   ├── db/schema.sql    ← 22-table PostgreSQL schema (source of truth)
│   ├── skills/          ← 12 MCP agent skill implementations
│   ├── seed.py          ← Data seeding: python mcp-server/seed.py --patients 10 --months 6
│   ├── orchestrator.py  ← Daily pipeline sequencer
│   └── tests/           ← pytest test suite (87 backend tests)
├── ingestion/           ← Adaptive HealthEx ingest pipeline
│   ├── adapters/healthex/
│   │   ├── format_detector.py  ← detect_format() → 5 formats (A/B/C/D/JSON-dict)
│   │   ├── ingest.py           ← adaptive_parse(): detect → parse → LLM fallback
│   │   ├── llm_fallback.py     ← Claude fallback for unrecognised payloads
│   │   └── parsers/            ← format_a/b/c/d + json_dict parsers
│   └── tests/                  ← 69 ingestion tests (format detection, parsers, pipeline)
├── docs/                ← Planning documents (mcp_use_cases.md — story line + action plan)
├── tests/e2e/           ← End-to-end use-case suite (18 tests, all tools)
│   ├── data_entry_agent.py  ← PatientDataEntryAgent: seeds 6 months of Maria Chen history
│   ├── conftest.py          ← Session-scoped DB pool + maria_chen fixture
│   └── test_all_mcp_tools.py ← UC-01→UC-15 (MCP skills) + deliberation tools
├── replit_dashboard/    ← FastAPI config dashboard (API keys, MCP URLs, Claude config)
│   ├── server.py        ← FastAPI app (port 8080) — includes MCP_CLINICAL_INTELLIGENCE_URL
│   ├── index.html       ← Single-page dashboard UI
│   └── tests/           ← 30 dashboard tests (anyio-based)
├── shared/              ← Shared JS client (claude-client.js)
├── prototypes/          ← 4 HTML proof-of-concept prototypes
├── config/system_prompts/ ← Role-based system prompts (pcp, care_manager, patient)
├── tests/phase1/        ← 124 Phase 1 integration tests
├── tests/phase2/        ← 57 Phase 2 deliberation feature tests
├── CLAUDE.md            ← Full implementation guide for Claude Code agents
└── requirements.txt     ← Root Python dependencies (pytest-asyncio==0.21.2)
```

## Workflows (5 active)

| Workflow | Command | Port |
|---------|---------|------|
| Start application | `cd replit-app && npm run dev` | 5000 |
| Config Dashboard | `cd replit_dashboard && python server.py` | 8080 |
| Clinical MCP Server | `MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server` | 8001 |
| Skills MCP Server | `cd mcp-server && MCP_TRANSPORT=streamable-http MCP_PORT=8002 python server.py` | 8002 |
| Ingestion MCP Server | `MCP_TRANSPORT=streamable-http MCP_PORT=8003 python -m ingestion.server` | 8003 |

## Three MCP Servers (all public via Next.js proxy)

| Server | Port | Public Path | Tools | Claude Web Name |
|--------|------|-------------|-------|-----------------|
| ClinicalIntelligence | 8001 | `/mcp` | 17 | `ambient-clinical-intelligence` |
| PatientCompanion (Skills) | 8002 | `/mcp-skills` | 17 | `ambient-skills-companion` |
| PatientIngestion | 8003 | `/mcp-ingestion` | 1 | `ambient-ingestion` |

All three are proxied through Next.js (port 5000) — no port number in public URLs.

### Server 1 — ClinicalIntelligence (`server/mcp_server.py`)

Seventeen tools at `https://[domain]/mcp` (9 Phase 1 + 8 HealthEx/Deliberation/Ingestion):

| Tool | Description |
|------|-------------|
| `clinical_query` | 3-layer guardrail pipeline → Claude |
| `get_guideline` | Fetch USPSTF/ADA guideline by ID |
| `check_screening_due` | Overdue screenings for patient profile |
| `flag_drug_interaction` | Known drug interactions |
| `get_synthetic_patient` | Maria Chen demo patient (MRN 4829341) |
| `use_healthex` | Switch data track to HealthEx real records |
| `use_demo_data` | Switch data track to Synthea demo data |
| `switch_data_track` | Switch to named track (synthea/healthex/auto) |
| `get_data_source_status` | Report active track + available sources |
| `register_healthex_patient` | Create/upsert a HealthEx patient row, return UUID |
| `ingest_from_healthex` | Two-phase ingest: plan (fast) + execute (write rows) |
| `execute_pending_plans` | Re-execute failed/pending ingestion plans from cache |
| `get_ingestion_plans` | Read plan summaries + insights_summary for a patient |
| `run_deliberation` | Trigger full dual-LLM deliberation for a patient |
| `get_deliberation_results` | Retrieve stored deliberation outputs |
| `get_patient_knowledge` | Fetch accumulated patient-specific knowledge |
| `get_pending_nudges` | List undelivered nudges for patient or care team |

Also has REST wrappers at `/tools/<name>` and liveness check at `/health`.

**HealthEx two-phase pipeline** (all on `/mcp`, port 8001):
`use_healthex` → `register_healthex_patient` → `ingest_from_healthex` (plan) → `execute_pending_plans` (write) → `get_ingestion_plans` (status) → `run_deliberation` → `get_deliberation_results` → `get_pending_nudges`

### Server 2 — PatientCompanion (`mcp-server/server.py`)

Seventeen tools at `https://[domain]/mcp-skills` (auto-discovered from `mcp-server/skills/`):
`compute_obt_score`, `compute_provider_risk`, `run_crisis_escalation`, `run_food_access_nudge`,
`generate_daily_checkins`, `generate_patient`, `generate_daily_vitals`, `generate_previsit_brief`,
`run_sdoh_assessment`, `use_healthex`, `use_demo_data`, `switch_data_track`,
`get_data_source_status`, `check_data_freshness`, `run_ingestion`, `get_source_conflicts`,
`ingest_from_healthex`

### Server 3 — PatientIngestion (`ingestion/server.py`)

One tool at `https://[domain]/mcp-ingestion`:
`trigger_ingestion` — runs the full ETL pipeline for a patient from a named source adapter.

**Claude web MCP config** — download from Config Dashboard (port 8080):
- Dev (always available): `GET /api/generate/mcp-config?env=dev`
- Prod (after deployment): `GET /api/generate/mcp-config?env=prod`
- Full summary (both URLs): `GET /api/generate/claude-config`

Guardrails pipeline:
1. **Input**: PHI detection, jailbreak blocking, scope check, emotional tone flag
2. **Escalation rules**: life-threatening, controlled substances, pediatric, pregnancy
3. **Output**: citation check, PHI leakage scan, diagnostic language flags, drug grounding

## Phase 2 — Dual-LLM Deliberation Engine (`server/deliberation/`)

An async pre-computation layer where Claude (Anthropic) and GPT-4 (OpenAI) independently analyze a patient's clinical context, cross-critique each other, then synthesize into 5 structured output categories:

```
server/deliberation/
├── schemas.py          ← 20 Pydantic models for all data flow
├── engine.py           ← 5-phase pipeline orchestrator
├── context_compiler.py ← Phase 0: assemble patient EHR context
├── analyst.py          ← Phase 1: parallel Claude + GPT-4 analysis
├── critic.py           ← Phase 2: cross-critique with convergence
├── synthesizer.py      ← Phase 3: unified synthesis
├── behavioral_adapter.py ← Phase 4: SMS/nudge formatting
├── knowledge_store.py  ← Phase 5: atomic DB commit
├── prompts/            ← 5 XML prompt templates
├── migrations/001_deliberation_tables.sql  ← 4 new tables
└── migrations/002_ingestion_plans.sql     ← ingestion_plans table + raw_fhir_cache columns
```

4 new DB tables: `deliberations`, `deliberation_outputs`, `patient_knowledge`, `core_knowledge_updates`

UI: `prototypes/pcp-encounter.html` has 2 tabs — **Clinical Workspace** and **AI Deliberation** — with `prototypes/components/deliberation-panel.js` handling the deliberation panel.

## Database

- **Provider**: Replit built-in PostgreSQL
- **Schema**: `mcp-server/db/schema.sql` (22 core tables) + `server/deliberation/migrations/001_deliberation_tables.sql` (4 deliberation tables) + `server/migrations/002_ingestion_plans.sql` (1 ingestion_plans table = 27 total)
- **Connection**: `DATABASE_URL` environment variable (auto-set by Replit)
- **Key constraints**:
  - `is_stale` in `source_freshness` is a regular boolean (not generated — PostgreSQL requires immutable expressions for generated columns)
  - `biometric_readings` has a UNIQUE index on `(patient_id, metric_type, measured_at)` for idempotent inserts

## Environment Variables / Secrets

| Key | Category | Notes |
|-----|----------|-------|
| `ANTHROPIC_API_KEY` | THIRD_PARTY | Replit Secret — used by clinical MCP server |
| `LANGSMITH_API_KEY` | THIRD_PARTY | Replit Secret — optional tracing |
| `DATABASE_URL` | AUTO | Replit PostgreSQL (auto-set) |
| `CLAUDE_MODEL` | AUTO | Default: `claude-sonnet-4-5` |
| `MCP_CLINICAL_INTELLIGENCE_URL` | AUTO | Default: `http://localhost:8001/mcp` |
| `SYNTHEA_OUTPUT_DIR` | AUTO | Default: `/home/runner/synthea-output` |

Config dashboard at port 8080 manages all 18 keys in three categories (AUTO / SELF_HOSTED / THIRD_PARTY).

## Seeding Data

```bash
python mcp-server/seed.py --patients 10 --months 6
```

Generates synthetic FHIR fixtures first:
```bash
python mcp-server/scripts/create_minimal_fixtures.py
```

## Testing

Each suite runs independently (conftest scoping keeps them isolated).

### Phase 1 Clinical Intelligence — 132 tests
```bash
python -m pytest tests/phase1/ -v
```

### Phase 2 Deliberation Engine — 40 unit tests + 57 feature tests
```bash
python -m pytest server/deliberation/tests/ -v   # 40 passed, 1 skipped
python -m pytest tests/phase2/ -v                # 57 passed
```

### End-to-end MCP use-case suite — 21 tests (5 skipped without live servers)
```bash
python -m pytest tests/e2e/ -v
```

### Backend Skills MCP (Python/pytest) — 87 tests
```bash
cd mcp-server && pytest tests/ -v
```

### Adaptive Ingestion Pipeline — 85 tests
```bash
python -m pytest ingestion/tests/ -v   # format detection, parsers A/B/C/D, pipeline, planner, executor
```

### Frontend (Next.js/Jest) — 37 tests
```bash
cd replit-app && npm test
```

### Config Dashboard (anyio/pytest) — 30 tests
```bash
cd replit_dashboard && python -m pytest tests/ -v
```

**Total: 586 tests (549 Python + 37 Jest), all passing**
| Suite | Count |
|-------|-------|
| Phase 1 clinical intelligence (incl. 18 DB format integration + 8 ingestion-plans IP tests) | 132 |
| Phase 2 deliberation (unit + features + fence-stripping) | 119 |
| E2E use-case suite (UC-01→UC-18 + 3 ingestion-tool smoke tests) | 21 |
| Skills MCP backend (incl. 27 fix verification tests) | 87 |
| Adaptive ingestion pipeline (parsers + edge cases + perf + planner PL-1–8 + executor EX-1–8) | 136 |
| MCP tool registration + REST smoke tests | 24 |
| Next.js frontend (Jest) | 37 |
| Config dashboard | 30 |

## Package Manager

- Frontend: `npm` (package-lock.json in replit-app/)
- Backend: Python 3.12 (pip / requirements); pytest-asyncio==0.21.2 required

## MCP Skills (12 implemented in mcp-server/)

| Skill | Function |
|-------|----------|
| `generate_patient.py` | Imports FHIR patient bundles into PostgreSQL |
| `generate_vitals.py` | Generates daily biometric readings (idempotent) |
| `generate_checkins.py` | Creates daily check-in records |
| `compute_obt_score.py` | Computes Optimal Being Trajectory scores (returns JSON) |
| `crisis_escalation.py` | Detects crisis indicators (returns JSON with escalation_triggered) |
| `sdoh_assessment.py` | Social Determinants of Health assessment |
| `ingestion_tools.py` | Data freshness, source status, `use_healthex()`, `use_demo_data()` |
| `previsit_brief.py` | Pre-visit clinical brief generation |
| `food_access_nudge.py` | Food access intervention nudges |
| `compute_provider_risk.py` | Provider-level risk score computation |

## Key Engineering Rules

- **asyncpg**: Never use `$N + INTERVAL '1 day'` — pre-compute bounds in Python
- **asyncpg**: Never use `do` as a SQL table alias — `do` is a reserved PostgreSQL keyword; use `dout` or similar
- **MCP skills**: Never use `print()` — all logging goes to `sys.stderr`
- **Model names**: `claude-sonnet-4-20250514` (Clinical/Synthesis), `gpt-4o` (deliberation critic)
- **pytest-asyncio**: Pinned to 0.21.2 — 1.x breaks session-scoped event_loop
- **Replit Secrets**: Take priority over local `.env` in dashboard and connectivity tests
- **Dashboard tests**: `clean_env` fixture pops ALL_KEYS from os.environ — isolates from Replit Secrets
- **Port config**: Next.js=5000, Config Dashboard=8080, Clinical MCP=8001, Skills MCP=8002, Ingestion MCP=8003
- **FastMCP**: `FastMCP()` does NOT accept `description=` kwarg — causes startup crash
- **Deliberation**: `run_deliberation` is async fire-and-forget — poll `get_deliberation_results` for output
- **MCP Proxy**: Browser calls `/api/mcp/<port>/tools/<name>` → Next.js route proxies to `http://localhost:<port>/tools/<name>`; shared/claude-client.js uses relative `/api/mcp/8001` in browser context
- **HealthEx Protocol**: `register_healthex_patient` MUST be called before `ingest_from_healthex` — it bootstraps the `patients` row that `run_deliberation` requires. See CLAUDE.md Section 13.
- **Synthea fixtures**: `mcp-server/tests/fixtures/fhir/` holds 3 minimal FHIR bundles; conftest.py sets `SYNTHEA_OUTPUT_DIR` to fixtures when `/home/runner/synthea-output/fhir/` is absent
- **Adaptive ingest**: `ingest_from_healthex` routes all payloads through `ingestion.adapters.healthex.ingest.adaptive_parse()` — 5 formats (A: plain text, B: compressed table, C: flat FHIR text, D: FHIR Bundle JSON, JSON-dict arrays); response always includes `format_detected` and `parser_used` fields; `records_written` is a dict (not int)
- **Fence-stripping**: LLMs sometimes wrap JSON in ```json ... ``` fences even when told not to — `server/deliberation/json_utils.strip_markdown_fences()` is called in both `analyst.py` and `critic.py` before `model_validate_json`
- **pytest conftest scoping**: `tests/e2e/conftest.py` must NOT declare `pytest_plugins` — this causes collection errors when running suites together; `asyncio_mode = auto` in root `pytest.ini` is sufficient
- **source_freshness staleness**: Sources with `records_count=0` are correctly flagged `is_stale=True` in tests — this is expected for registered-but-empty sources like `synthea`; only check staleness for sources with `records_count > 0`

## Key Bug Fixes Applied

1. **get_pending_nudges SQL**: `do` is a reserved PostgreSQL keyword — renamed table alias to `dout` in deliberation JOIN query
2. **generate_patient.py**: `birth_date` string→`date` object conversion for asyncpg
3. **compute_obt_score.py**: Pre-computed `target_plus_one` to avoid asyncpg type error; returns JSON
4. **crisis_escalation.py**: Same INTERVAL fix; returns JSON with `escalation_triggered` field
5. **pytest-asyncio**: Pinned to 0.21.2 (1.x broke session-scoped event_loop pattern)
6. **schema.sql**: Added FK constraints to 10 previously unlinked tables; added UNIQUE index on biometric_readings
7. **dashboard completeness**: Uses `_explicitly_set()` — defaults don't count as user-configured
8. **FHIR fixtures**: 10 minimal Synthea bundles in `/home/runner/synthea-output/fhir/`
9. **context_compiler UUID lookup**: `run_deliberation(patient_id=<UUID>)` now works for HealthEx patients registered via `register_healthex_patient` — added UUID regex detection + `WHERE id = $1::uuid` fallback before the partial MRN LIKE match
10. **IndependentAnalysis schema**: `model_id`, `role_emphasis`, `raw_reasoning` now default to `""` so `model_validate_json` succeeds before the caller sets them server-side (analyst.py lines 112-116)
11. **Analyst prompts**: Updated `analyst_claude.xml` and `analyst_gpt4.xml` with explicit JSON skeleton showing `claim`/`confidence` field names, plain-string `anticipated_trajectory`, plain-string list for `missing_data_identified` — prevents LLM from using `finding`/`risk`/`action` aliases or wrapping values in dicts
12. **test_system_config_data_track**: Fixed to accept any valid track value (`synthea`, `healthex`, `auto`) instead of hardcoding `synthea` — `DATA_TRACK` is a live mutable config that changes when `use_healthex()` is called
13. **Adaptive ingest pipeline** (PR #12): `ingest_from_healthex` now calls `adaptive_parse()` — all 5 HealthEx payload formats (plain text, compressed table, flat FHIR text, FHIR Bundle JSON, JSON dict arrays) are detected and normalized deterministically; LLM fallback fires when deterministic parsers return 0 rows on non-trivial input
14. **Fence-stripping in deliberation engine**: `strip_markdown_fences()` added to `json_utils.py` and wired into `analyst.py` (both Claude + GPT-4) and `critic.py` (CrossCritique + RevisedAnalysis) — prevents `model_validate_json` crash when LLMs wrap responses in ```json fences despite explicit instructions
15. **TestRawTextPayloadCaching → TestFormatBCompressedTableIngest**: Old tests asserting `records_written == 0` for `#`-prefixed payloads were incorrect after the adaptive pipeline landed — updated to verify `format_detected='compressed_table'`, `parser_used='format_b_compressed_table'`, and `records_written` is a dict
16. **UC-07 staleness assertion**: `test_uc07_check_data_freshness` updated to only flag stale sources with `records_count > 0` — `synthea` source in test environment has 0 records (never populated) and being stale is expected and non-critical
17. **e2e conftest pytest_plugins**: Removed `pytest_plugins = ["pytest_asyncio"]` from `tests/e2e/conftest.py` — non-top-level `pytest_plugins` declarations cause collection errors when running suites together; `asyncio_mode = auto` in `pytest.ini` is sufficient
18. **Fix A — transform_by_type data_source passthrough** (`mcp-server/transforms/fhir_to_schema.py` line 292): `transform_by_type()` now passes `source` as the third positional argument to every transform function (`fn([resource], patient_id, source)`). Previously it called `fn([resource], patient_id)` so every record silently got `data_source="synthea"` (the default) regardless of the caller-supplied source. `source` is now required (no default). Verified by 6 new tests in `mcp-server/tests/test_fix_verification.py`.
19. **Fix B — transform_encounters string type guard** (`mcp-server/transforms/fhir_to_schema.py` lines 206-217): `transform_encounters()` now guards `r.get("type")` with `isinstance(..., list)` before indexing. When `type` is a raw string (e.g. `"encounter"`) it wraps it as `{"display": raw_type}` instead of crashing with `AttributeError: 'str' object has no attribute 'get'`. When `type` is `None` or a list with a non-dict first element, the guard prevents the crash. Verified by 9 new tests in `test_fix_verification.py`.
20. **Fix C — Format B parser encounters support** (`ingestion/adapters/healthex/parsers/format_b_parser.py`): `parse_compressed_table()` now handles `resource_type="encounters"`. Added `"encounters"` to `_default_headers()` (8 columns), `"Type": "C"` and `"Location": "C"` to `_build_col_dict_map()`, an `elif resource_type == "encounters"` branch to `_to_native()` returning `{type, date, description, provider, status}`, and `"encounters": ("type", "date")` to `_deduplicate()`. Dict references use `C:` prefix for type lookups. Verified by 12 new tests in `test_fix_verification.py`.
21. **Fix — Text payloads routed through adaptive_parse** (`mcp-server/skills/ingestion_tools.py` — branch `claude/fix-ingestion-pipeline-7qQy4`, commit `f6047ab`): Previously `ingest_from_healthex` short-circuited all `#`-prefixed text payloads (Format A/B/C) with `records_written: 0`, caching raw text but never parsing it. Now the text-payload branch calls `adaptive_parse()`, maps results through new `_native_to_warehouse_rows()` helper (labs→`biometric_readings`, conditions→`patient_conditions`, medications→`patient_medications`, encounters→`clinical_events`), and feeds rows into the existing per-table INSERT loop. Also added `_parse_lab_value()` to extract floats from strings like `"34.0-34.9"` or `">60"`. Verified live: Format B conditions=3 rows, labs=3 rows, encounters=2 rows (all previously 0).
22. **Fix — `safe_json_loads()` in deliberation engine** (`server/deliberation/json_utils.py`): Added `safe_json_loads(text)` — strips markdown fences first, returns `{}` for empty/None input, raises `ValueError` with a 200-char preview on `JSONDecodeError` instead of propagating raw exception. Prevents synthesizer crash when Claude wraps its output in ` ```json ``` ` fences.
23. **Fix — synthesizer uses `safe_json_loads`** (`server/deliberation/synthesizer.py`): Replaced bare `json.loads(raw)` with `safe_json_loads(raw)` — prevents `Unterminated string` / `JSONDecodeError` crash when the synthesizer receives fence-wrapped output from Claude.
24. **Fix — Two-phase async ingestion architecture** (`ingestion/adapters/healthex/planner.py` + `executor.py` — branch `claude/fix-ingestion-blob-loop-2A6H2`, commit `3609e7e`): Large HealthEx blobs previously wrote only 1 row instead of 34+ due to timeout in the single-pass loop. Phase 1 (fast, <500ms): `ingest_from_healthex` caches raw blob in `raw_fhir_cache` (with new `raw_text` + `detected_format` columns), runs LLM planner → creates `ingestion_plans` row. Phase 2 (inline or async): `execute_pending_plans` reads plan → adaptive_parse → writes rows one-at-a-time → updates plan status. Non-numeric lab values (Negative, Positive, No growth) are now preserved (previously dropped silently). Added 2 new MCP tools (`execute_pending_plans`, `get_ingestion_plans`) + migration `002_ingestion_plans.sql` (18-column table). 16 new unit tests (PL-1–PL-8 + EX-1–EX-8) + 8 IP integration tests + 3 REST smoke tests.
25. **Fix — REST wrappers for execute_pending_plans + get_ingestion_plans** (`server/mcp_server.py`): Added `@mcp.custom_route("/tools/execute_pending_plans")` and `@mcp.custom_route("/tools/get_ingestion_plans")` so the new tools are reachable from HTML prototypes and smoke tests via the same `/tools/<name>` REST pattern as all other tools.

## "No approval received" Note (Claude Web Behaviour)

`use_healthex` and `register_healthex_patient` work correctly when called directly via MCP protocol (verified by curl smoke tests). The "No approval received" message is **Claude Web's own HITL safety gate** for state-modifying tool calls — it is not emitted by our servers. If Claude Web blocks those tools, the user must explicitly approve in the chat when prompted, or call the tools in a Claude session configured without HITL gating.

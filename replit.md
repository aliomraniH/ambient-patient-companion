# Ambient Patient Companion

A multi-agent AI health system that generates a continuously derived patient health UX from Role × Context × Patient State × Time.

## Run & Operate

To start all services, use the `start.sh` script. Individual services can be run as follows:

To start the full application (Next.js frontend, all MCP servers, and the config dashboard), use:
```bash
./start.sh
```

Individual component commands (All MCP servers require `MCP_TRANSPORT=streamable-http` and `MCP_PORT` to be set):
- **Next.js Frontend:** `cd replit-app && npm run dev` (Port 5000)
- **Config Dashboard:** `cd replit_dashboard && python server.py` (Port 8080)
- **Clinical MCP Server:** `MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server` (Port 8001)
- **Skills MCP Server:** `cd mcp-server && MCP_TRANSPORT=streamable-http MCP_PORT=8002 python server.py` (Port 8002)
- **Ingestion MCP Server:** `MCP_TRANSPORT=streamable-http MCP_PORT=8003 python -m ingestion.server` (Port 8003)

**Get your MCP URLs:** After `./start.sh`, run `cat .mcp.json` or check the Replit preview domain.

**Required Environment Variables:**
- `ANTHROPIC_API_KEY`: Claude API key (Replit Secret)
- `OPENAI_API_KEY`: GPT-4o API key (Replit Secret)
- `LANGSMITH_API_KEY`: (Optional) For LangSmith tracing (Replit Secret)
- `HF_TOKEN`: HuggingFace Pro token (Replit Secret)
- `GITHUB_DEPLOY_KEY`: **Preferred** — SSH deploy key private key for GitHub push (never expires). Run `bash scripts/setup_deploy_key.sh` once to generate, then add the public key to GitHub and store the private key here.
- `GITHUB_TOKEN`: Fallback — classic/fine-grained PAT for GitHub push via HTTPS (expires; prefer `GITHUB_DEPLOY_KEY`).
- `DATABASE_URL`: Auto-set by Replit PostgreSQL
- `REPLIT_DEV_DOMAIN`: Auto-set, used for MCP JSON generation

## Stack

- **Frameworks:** Next.js 16, FastAPI (for MCP servers and dashboard)
- **Runtime Versions:** Python 3.10+ (specified in `requirements.txt`), Node.js (for Next.js)
- **LLMs:** Claude Sonnet, GPT-4o, Claude Haiku
- **Database:** PostgreSQL (Replit built-in)
- **ORM/DB Access:** `asyncpg` (direct SQL)
- **Validation:** Pydantic models for deliberation outputs, custom data quality validators (F1-F5)
- **Build Tool:** npm (frontend), Python (backend services)
- **OAuth:** OAuth 2.0 PKCE, RFC 7591 dynamic client registration

## Where things live

- **Clinical Intelligence Server (Server 1):** `server/` — FastMCP name: `ambient-clinical-intelligence` (port 8001)
    - **Source of Truth: Deliberation Output Contract:** `server/deliberation/prompts/synthesizer.xml`
    - **DB Schema Migrations:** `server/deliberation/migrations/`
    - **Deliberation Pydantic Models:** `server/deliberation/schemas.py`
    - **LLM Prompt Templates:** `server/deliberation/prompts/` (XML format)
- **Skills Companion Server (Server 2):** `mcp-server/` — FastMCP name: `ambient-skills-companion` (port 8002)
    - **Source of Truth: Base DB Schema:** `mcp-server/db/schema.sql`
- **Ingestion Server (Server 3):** `ingestion/` — FastMCP name: `ambient-ingestion` (port 8003)
- **Shared Utilities:** `shared/` (cross-server Python modules like `coercion.py`, `datetime_utils.py`, `audit_middleware.py`)
- **Next.js Frontend:** `replit-app/` (UI, OAuth endpoints, API routes, port 5000)
    - **OAuth Endpoints:** `replit-app/app/.well-known/` and `replit-app/app/authorize/route.ts`, `replit-app/app/token/route.ts`, `replit-app/app/register/route.ts`
- **Config Dashboard:** `replit_dashboard/` (UI for environment variables and watcher health, port 8080)
- **MCP Discovery:** `.mcp.json` (auto-generated public HTTPS URLs)
- **System Prompts:** `config/system_prompts/` (role-based LLM prompts)

## Architecture decisions

- **Dual-LLM Deliberation Engine:** Utilizes both Claude Sonnet and GPT-4o for independent analysis, cross-critique, and synthesis (only upon convergence, score ≥ 0.40), ensuring robust clinical recommendations.
- **3-Layer Clinical Guardrail Pipeline:** Implements input validation, escalation rules, and output safety on every AI call to ensure clinical reliability.
- **Convergence Gate:** Deliberation synthesis only proceeds when both LLMs show sufficient agreement (score ≥ 0.40), preventing low-confidence recommendations.
- **Universal Provenance Gate (`verify_output_provenance`):** Enforces that all outputs have declared and domain-matched tiers across all three MCP servers, with an audit trail in `provenance_audit_log` or `mcp_call_log`.
- **Atomic Database Commits with Confidence Coercion:** All LLM-produced confidence/likelihood values are normalized to a float [0,1] via `coerce_confidence()` before database writes, ensuring data consistency and reliability.
- **Ephemeral OAuth State:** OAuth client/code/token state is in-memory and ephemeral, requiring clients to re-authorize on restart. This simplifies deployment by avoiding persistent session management complexity.
- **Strict Security Controls:** Combines Bearer token enforcement for external API routes with HMAC-signed httpOnly session cookies for internal dashboard operations, preventing cross-origin attacks.
- **Multi-Server Microservices Architecture:** The system is split into three distinct MCP servers (Clinical Intelligence, Skills Companion, Ingestion) for modularity, scalability, and specialized functionalities, all proxied through a single Next.js frontend.
- **Comprehensive MCP Audit Log:** Every external tool call is recorded to `mcp_call_log` with detailed session tracking, inputs, outputs, and timing for full transparency and queryability.

## Product

The Ambient Patient Companion connects large language models to a clinical intelligence layer for primary care and care management. It processes real patient data from a 35-table PostgreSQL warehouse to provide AI-driven insights and recommendations. Key capabilities include:
- Generating structured health recommendations and insights from a dual-LLM deliberation engine with configurable modes (triage, progressive, full).
- Applying clinical guardrails and data quality validators to all AI outputs.
- Providing over 50 MCP tools for various clinical, behavioral, and data management tasks.
- Tracking every tool call in an audit log (`mcp_call_log`) for transparency and debugging.
- Offering an OAuth 2.0 discovery layer for secure client integration.
- Enabling patient management, vital tracking, and check-in functionalities through a Next.js frontend.
- Tracking patient flags and their lifecycle (open, retracted, pending).
- Multi-modal data ingestion (e.g., HealthEx FHIR) with ETL pipeline and conflict resolution.
- Behavioral health support: atom extraction, pressure assessment, crisis escalation, and SDoH assessments.
- Config Dashboard for live monitoring of background watcher health and environment variables.

## User preferences

- **Code Style:** Strict Pydantic validation for all LLM outputs.
- **Safety:** Every tool call must be logged to the audit trail.
- **Data Quality:** Use `coerce_confidence` for all LLM-produced likelihoods.
- **Date Handling:** Always use `ensure_aware()` for database-read datetimes.
- Keep `asyncio_mode = auto` + `--import-mode=importlib` in `pytest.ini` — required for all async tests
- Never use `print()` in MCP tool code — log to `sys.stderr` only
- Model names: `claude-sonnet-4-20250514` (clinical), `claude-haiku-4-5-20251001` (lightweight tasks), `gpt-4o` (deliberation critic)

## Gotchas

- **`asyncpg` date arithmetic:** Always use `$N * INTERVAL '1 day'` or pre-compute date bounds in Python; avoid `('$N' || ' days')::INTERVAL`.
- **`asyncpg` SQL aliases:** Avoid `do` as a SQL alias, as it's a reserved keyword; use `dout` or similar.
- **`FastMCP` instantiation:** Do not use `description=` kwarg in `FastMCP()` as it causes startup crashes.
- **Logging in MCP tools:** Use `sys.stderr` for all logging; `print()` is not recommended.
- **`pytest-asyncio` version:** Pinned to 0.21.2; newer versions may break session-scoped event loops.
- **`shared/` imports:** Ensure repo root is on `sys.path` in all servers to import `shared` utilities correctly.
- **`coerce_confidence` usage:** Always wrap LLM-produced confidence/likelihood values with `coerce_confidence()` before writing to the database.
- **`ensure_aware` usage:** Call `ensure_aware()` on any database-read datetime before performing arithmetic to prevent `TypeError` from mixing aware and naive datetimes.
- **`last_ingested_at` initialization:** Always set `last_ingested_at=NULL` on initial patient registration to ensure `_is_stale()` triggers the first ingest.
- **MCP Discovery (`.mcp.json`):** Must use public HTTPS URLs and be regenerated at startup via `start.sh` or `scripts/generate_mcp_json.py`.
- **OAuth Routes:** All five OAuth routes (discovery, registration, authorize, token, protected-resource) must be present for Claude to connect successfully and discoverable via `/.well-known/oauth-protected-resource`.
- **Deliberation workflow:** `run_deliberation` is asynchronous; poll `get_deliberation_results` for the output.
- **HealthEx ingestion protocol:** `register_healthex_patient` must precede `ingest_from_healthex`.
- **LLM JSON parsing:** Use `json_utils.strip_markdown_fences()` to remove Markdown fences from LLM-generated JSON before validation.
- **GitHub deploy key newline:** The private key stored in `GITHUB_DEPLOY_KEY` must end with a newline. `push_to_github.sh` appends one automatically, but verify with `echo "$GITHUB_DEPLOY_KEY" | tail -c1 | xxd` if SSH auth fails unexpectedly.

## Pointers

- **FastMCP Documentation:** [https://gofastmcp.com](https://gofastmcp.com)
- **Replit Secrets:** [https://docs.replit.com/programming-environment/secrets](https://docs.replit.com/programming-environment/secrets)
- **Next.js Documentation:** [https://nextjs.org/docs](https://nextjs.org/docs)
- **OAuth 2.0 RFCs:** [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636), [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728), [RFC 8414](https://www.rfc-editor.org/rfc/rfc8414), [RFC 7591](https://www.rfc-editor.org/rfc/rfc7591), [RFC 6749](https://www.rfc-editor.org/rfc/rfc6749)
- **PostgreSQL Documentation:** [https://www.postgresql.org/docs/](https://www.postgresql.org/docs/)
- **FastAPI Documentation:** [https://fastapi.tiangolo.com/](https://fastapi.tiangolo.com/)
- **Pydantic Documentation:** [https://docs.pydantic.dev/](https://docs.pydantic.dev/)
- **`asyncpg` Documentation:** [https://magicstack.github.io/asyncpg/current/](https://magicstack.github.io/asyncpg/current/)
- **Config Dashboard watcher health endpoint:** `GET /api/health/agent-runtime` (proxies to port 8002)

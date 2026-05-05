# Ambient Patient Companion

A multi-agent AI health system that generates a continuously derived patient health UX from Role × Context × Patient State × Time.

## Run & Operate

To start the full application (Next.js frontend, all MCP servers, and the config dashboard), use:
```bash
./start.sh
```

Individual component commands:
- **Next.js Frontend:** `cd replit-app && npm run dev` (Port 5000)
- **Config Dashboard:** `cd replit_dashboard && python server.py` (Port 8080)
- **Clinical MCP Server:** `MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server` (Port 8001)
- **Skills MCP Server:** `cd mcp-server && MCP_TRANSPORT=streamable-http MCP_PORT=8002 python server.py` (Port 8002)
- **Ingestion MCP Server:** `MCP_TRANSPORT=streamable-http MCP_PORT=8003 python -m ingestion.server` (Port 8003)

**Required Environment Variables:**
- `ANTHROPIC_API_KEY` (Replit Secret)
- `OPENAI_API_KEY` (Replit Secret)
- `LANGSMITH_API_KEY` (Replit Secret, optional)
- `HF_TOKEN` (Replit Secret)
- `GITHUB_TOKEN` (Replit Secret)
- `DATABASE_URL` (Auto-set by Replit PostgreSQL)
- `REPLIT_DEV_DOMAIN` (Auto-set)

## Stack

- **Frameworks:** Next.js 16, FastAPI (for MCP servers and dashboard)
- **Runtime Versions:** Python (specified in `requirements.txt`), Node.js (for Next.js)
- **ORM:** `asyncpg` (PostgreSQL client)
- **Validation:** Pydantic
- **Build Tool:** npm (for frontend), Python setup tools (for backend)

## Where things live

- **`server/`**: Clinical Intelligence MCP Server (port 8001)
- **`mcp-server/`**: Skills Companion MCP Server (port 8002)
- **`ingestion/`**: Ingestion MCP Server (port 8003)
- **`shared/`**: Cross-server Python utilities (e.g., `coercion.py`, `datetime_utils.py`)
- **`replit-app/`**: Next.js 16 frontend (port 5000)
- **`replit_dashboard/`**: Configuration Dashboard (port 8080)
- **DB Schema (Base):** `mcp-server/db/schema.sql`
- **Deliberation Pydantic Models:** `server/deliberation/schemas.py`
- **LLM Prompt Templates:** `server/deliberation/prompts/` (XML format, e.g., `synthesizer.xml` for output contract)
- **OAuth Endpoints:** `replit-app/app/.well-known/` and `replit-app/app/authorize/route.ts`, `replit-app/app/token/route.ts`, `replit-app/app/register/route.ts`
- **MCP Discovery:** `.mcp.json` (auto-generated)

## Architecture decisions

- **Dual-LLM Deliberation Engine:** Utilizes both Claude Sonnet and GPT-4o for independent analysis, cross-critique, and synthesis, ensuring robust clinical recommendations.
- **3-Layer Clinical Guardrail Pipeline:** Implements input validation, escalation rules, and output safety on every AI call to ensure clinical reliability.
- **Universal Provenance Gate:** `verify_output_provenance` enforces that all outputs have declared and domain-matched tiers across all three MCP servers, with an audit trail in `provenance_audit_log`.
- **Atomic Database Commits with Confidence Coercion:** All LLM-produced confidence/likelihood values are normalized to a float [0,1] via `coerce_confidence()` before database writes, ensuring data consistency and reliability.
- **Multi-Server Microservices Architecture:** The system is split into three distinct MCP servers (Clinical Intelligence, Skills Companion, Ingestion) for modularity, scalability, and specialized functionalities, all proxied through a single Next.js frontend.

## Product

The Ambient Patient Companion connects large language models to a clinical intelligence layer for primary care and care management. It processes real patient data from a PostgreSQL warehouse to provide AI-driven insights and recommendations. Key capabilities include:
- Generating patient health recommendations from a dual-LLM deliberation engine.
- Applying clinical guardrails and data quality validators to all AI outputs.
- Providing over 50 MCP tools for various clinical, behavioral, and data management tasks.
- Tracking every tool call in an audit log for transparency and debugging.
- Offering an OAuth 2.0 discovery layer for secure client integration.
- Enabling patient management, vital tracking, and check-in functionalities through a Next.js frontend.

## User preferences

- _Populate as you build_

## Gotchas

- **`asyncpg` date arithmetic:** Always use `$N * INTERVAL '1 day'` or pre-compute date bounds in Python; avoid `('$N' || ' days')::INTERVAL`.
- **`asyncpg` SQL aliases:** Avoid `do` as a SQL alias, as it's a reserved keyword.
- **`FastMCP` instantiation:** Do not use `description=` kwarg in `FastMCP()` as it causes startup crashes.
- **Logging in MCP tools:** Use `sys.stderr` for all logging; `print()` is not recommended.
- **`pytest-asyncio` version:** Pinned to 0.21.2; newer versions may break session-scoped event loops.
- **`shared/` imports:** Ensure repo root is on `sys.path` in all servers to import `shared` utilities correctly.
- **`coerce_confidence` usage:** Always wrap LLM-produced confidence/likelihood values with `coerce_confidence()` before writing to the database.
- **`ensure_aware` usage:** Call `ensure_aware()` on any database-read datetime before performing arithmetic to prevent `TypeError` from mixing aware and naive datetimes.
- **`last_ingested_at` initialization:** Always set `last_ingested_at=NULL` on initial patient registration to ensure `_is_stale()` triggers the first ingest.
- **MCP Discovery (`.mcp.json`):** Must use public HTTPS URLs and be regenerated at startup via `start.sh`.
- **OAuth Routes:** All five OAuth routes must be present for Claude to connect successfully.
- **Deliberation workflow:** `run_deliberation` is asynchronous; poll `get_deliberation_results` for the output.
- **HealthEx ingestion protocol:** `register_healthex_patient` must precede `ingest_from_healthex`.
- **LLM JSON parsing:** Use `json_utils.strip_markdown_fences()` to remove Markdown fences from LLM-generated JSON before validation.

## Pointers

- **FastMCP Documentation:** [https://github.com/replit/fastmcp](https://github.com/replit/fastmcp)
- **Replit Secrets:** [https://docs.replit.com/programming-environment/secrets](https://docs.replit.com/programming-environment/secrets)
- **Next.js Documentation:** [https://nextjs.org/docs](https://nextjs.org/docs)
- **OAuth 2.0 PKCE RFC:** [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636)
- **PostgreSQL Documentation:** [https://www.postgresql.org/docs/](https://www.postgresql.org/docs/)
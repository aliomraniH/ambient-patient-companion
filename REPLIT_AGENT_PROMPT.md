Read CLAUDE.md and replit.md before doing anything. They are the source of
truth for this project.

You are deploying and testing the Phase 1 clinical intelligence layer that
was implemented on the branch `claude/ambient-patient-companion-phase1-TQwII`.
This branch adds a new FastMCP server (`server/mcp_server.py`) with guardrails,
clinical guidelines, system prompts, a JS client, HTML prototypes, and
integration tests.

Your job is to:

────────────────────────────────────────────────────────────────────────────
1. ENVIRONMENT SETUP
────────────────────────────────────────────────────────────────────────────

a. Install Python dependencies from the root requirements.txt:
     pip install -r requirements.txt
   This adds `anthropic` and `fastmcp` (plus existing deps).

b. Verify the ANTHROPIC_API_KEY is set in Replit Secrets
   (Settings > Secrets). The clinical_query tool needs it to call
   Claude API. If it is missing, warn the user but proceed with
   testing the 4 tools that don't require an API key.

c. Ensure the replit_dashboard KEY_META includes the new clinical
   intelligence server URL. Add this entry to `replit_dashboard/server.py`
   in the KEY_META dict:

     "MCP_CLINICAL_INTELLIGENCE_URL": {
         "category":    "AUTO",
         "label":       "MCP · Clinical Intelligence",
         "description": "FastMCP clinical decision support server (Phase 1).",
         "secret":      False,
         "default":     "http://localhost:8001/mcp",
         "help_url":    None,
     },

────────────────────────────────────────────────────────────────────────────
2. RUN THE PHASE 1 INTEGRATION TESTS
────────────────────────────────────────────────────────────────────────────

From the repository root, run:

    python -m pytest tests/phase1/ -v

All 100 tests must pass. These cover:
  - Input validation (PHI detection, jailbreak blocking, scope checks)
  - Output validation (citation enforcement, diagnostic language flagging)
  - Clinical escalation rules (life-threatening, controlled substances,
    pediatric, pregnancy — 5 scenario types)
  - get_guideline (ADA + USPSTF lookups by recommendation ID)
  - check_screening_due (age/sex/condition-based USPSTF eligibility)
  - flag_drug_interaction (12 hardcoded interaction pairs)
  - get_synthetic_patient (Maria Chen, MRN 4829341)
  - clinical_query (mocked Claude API — jailbreak blocking, PHI blocking,
    escalation, happy path generation, output validation)

If any test fails, diagnose the root cause and fix it. Do not skip tests.

────────────────────────────────────────────────────────────────────────────
3. RUN THE EXISTING TEST SUITES (regression check)
────────────────────────────────────────────────────────────────────────────

Verify that the existing tests still pass after Phase 1 additions:

  a. Backend (MCP server skills):
       cd mcp-server && python -m pytest tests/ -v

  b. Frontend (Next.js components):
       cd replit-app && npm test

  c. Config dashboard:
       cd replit_dashboard && python -m pytest tests/ -v

Report pass/fail counts for each suite. If any existing test broke,
investigate whether Phase 1 changes caused the regression.

────────────────────────────────────────────────────────────────────────────
4. DEPLOY THE CLINICAL INTELLIGENCE SERVER
────────────────────────────────────────────────────────────────────────────

The clinical intelligence server needs to run alongside the existing
Next.js frontend. There are two deployment paths — choose the HTTP one:

a. Add an HTTP entry point to server/mcp_server.py. The server currently
   runs via stdio transport. Add a second mode:

     if __name__ == "__main__":
         import sys
         transport = sys.argv[1] if len(sys.argv) > 1 else "streamable-http"
         if transport == "stdio":
             mcp.run(transport="stdio")
         else:
             mcp.run(transport="streamable-http", host="0.0.0.0", port=8001)

b. Update .replit to start both servers. Add a new workflow task that
   runs the clinical server in parallel with the Next.js frontend:

     [[workflows.workflow]]
     name = "Start clinical server"
     author = "agent"

     [[workflows.workflow.tasks]]
     task = "shell.exec"
     args = "cd /home/runner/ambient-patient-companion && python -m server.mcp_server streamable-http"
     waitForPort = 8001

   Then add this task to the "Project" parallel workflow:

     [[workflows.workflow]]
     name = "Project"
     mode = "parallel"
     author = "agent"

     [[workflows.workflow.tasks]]
     task = "workflow.run"
     args = "Start application"

     [[workflows.workflow.tasks]]
     task = "workflow.run"
     args = "Start clinical server"

c. Add port 8001 mapping (internal only — not exposed externally):

     [[ports]]
     localPort = 8001
     externalPort = 8001

d. Verify the server starts without errors:
     python -m server.mcp_server streamable-http
   It should listen on 0.0.0.0:8001. Kill it after verifying.

────────────────────────────────────────────────────────────────────────────
5. WIRE THE PROTOTYPES TO THE RUNNING SERVER
────────────────────────────────────────────────────────────────────────────

The shared/claude-client.js uses FASTMCP_BASE_URL defaulting to
http://localhost:8000. Update this to match the deployed port:

  const FASTMCP_BASE_URL = (typeof window !== 'undefined' && window.FASTMCP_BASE_URL)
    || 'http://localhost:8001';

Verify each prototype loads without JS console errors by opening them
in a browser or running a simple HTTP server:
    python -m http.server 8080 --directory prototypes/

────────────────────────────────────────────────────────────────────────────
6. LIVE TOOL VERIFICATION (smoke tests)
────────────────────────────────────────────────────────────────────────────

With the clinical server running on port 8001, verify each tool
responds correctly. Use curl or Python httpx:

a. get_synthetic_patient — should return Maria Chen's full record:
     curl -X POST http://localhost:8001/mcp \
       -H "Content-Type: application/json" \
       -d '{"method": "tools/call", "params": {"name": "get_synthetic_patient", "arguments": {"mrn": "4829341"}}}'

b. get_guideline — should return metformin recommendation:
     curl -X POST http://localhost:8001/mcp \
       -H "Content-Type: application/json" \
       -d '{"method": "tools/call", "params": {"name": "get_guideline", "arguments": {"recommendation_id": "9.1a"}}}'

c. check_screening_due — Maria Chen's profile (54F, T2DM, obesity):
     curl -X POST http://localhost:8001/mcp \
       -H "Content-Type: application/json" \
       -d '{"method": "tools/call", "params": {"name": "check_screening_due", "arguments": {"patient_age": 54, "sex": "female", "conditions": ["type_2_diabetes", "obesity"]}}}'

d. flag_drug_interaction — test ACE + ARB dual blockade:
     curl -X POST http://localhost:8001/mcp \
       -H "Content-Type: application/json" \
       -d '{"method": "tools/call", "params": {"name": "flag_drug_interaction", "arguments": {"medications": ["lisinopril", "losartan"]}}}'

e. clinical_query (only if ANTHROPIC_API_KEY is set):
     curl -X POST http://localhost:8001/mcp \
       -H "Content-Type: application/json" \
       -d '{"method": "tools/call", "params": {"name": "clinical_query", "arguments": {"query": "What are the ADA recommendations for SGLT2 inhibitors in a patient with type 2 diabetes and CKD?", "role": "pcp", "patient_context": {"conditions": ["type_2_diabetes", "ckd"], "medications": ["metformin", "lisinopril"]}}}}'

   Verify the response includes: citations, evidence grades, and the
   "Verify dosing with pharmacist" caveat. It must NOT contain
   definitive diagnostic language.

NOTE: The exact HTTP endpoint format depends on how FastMCP exposes
tools over streamable-http transport. Check FastMCP docs if the /mcp
JSON-RPC format doesn't work — it may use /tools/<name> REST-style
endpoints or /sse for SSE transport instead. Adjust the curl commands
accordingly. The key requirement is that all 5 tools are callable and
return correct response shapes.

────────────────────────────────────────────────────────────────────────────
7. UPDATE replit.md
────────────────────────────────────────────────────────────────────────────

Add a new section to replit.md documenting the clinical intelligence layer:

  ## Clinical Intelligence Layer (Phase 1)

  The clinical intelligence server provides AI-assisted clinical decision
  support through a three-layer guardrail pipeline.

  ### Running
  - Auto-started via Replit workflow on port 8001
  - Manual: `python -m server.mcp_server streamable-http`

  ### Tools (5)
  | Tool | Purpose |
  |------|---------|
  | clinical_query | Guardrailed Claude API for clinical questions |
  | get_guideline | Lookup ADA/USPSTF guidelines by ID |
  | check_screening_due | USPSTF screening eligibility check |
  | flag_drug_interaction | Drug interaction detection |
  | get_synthetic_patient | Demo patient data (Maria Chen) |

  ### Testing
  ```bash
  python -m pytest tests/phase1/ -v   # 100 integration tests
  ```

  ### Environment
  - Requires `ANTHROPIC_API_KEY` in Replit Secrets for clinical_query tool
  - Other 4 tools work without API key (guideline lookups, screenings, etc.)

────────────────────────────────────────────────────────────────────────────
8. FINAL VERIFICATION CHECKLIST
────────────────────────────────────────────────────────────────────────────

Report pass/fail for each item:

  [ ] Phase 1 integration tests: 100/100 passing
  [ ] Existing backend tests: passing (report count)
  [ ] Existing frontend tests: passing (report count)
  [ ] Existing dashboard tests: passing (report count)
  [ ] Clinical server starts on port 8001 without errors
  [ ] get_synthetic_patient returns Maria Chen data
  [ ] get_guideline returns ADA recommendation 9.1a
  [ ] check_screening_due returns ≥5 screenings for 54F with diabetes
  [ ] flag_drug_interaction detects ACE+ARB interaction
  [ ] clinical_query returns guardrailed response (if API key present)
  [ ] HTML prototypes load shared/claude-client.js without errors
  [ ] .replit workflow starts both servers in parallel
  [ ] replit.md updated with clinical intelligence documentation

────────────────────────────────────────────────────────────────────────────
CONSTRAINTS — do not violate these
────────────────────────────────────────────────────────────────────────────

- Do NOT use HealthEx MCP — it is incompatible with this environment
- Do NOT use claude-opus-* models — the clinical server uses
  claude-sonnet-4-20250514 only
- Do NOT modify existing prototype HTML UI or styles — only touch the
  script import and FASTMCP_BASE_URL
- Do NOT allow direct Claude API calls from HTML prototypes
- Do NOT store real patient data — all data is synthetic
  (Maria Chen MRN 4829341 is the canonical demo patient)
- Do NOT skip the output validation layer — it is mandatory
- If ANTHROPIC_API_KEY is not set, do NOT block deployment.
  The 4 non-AI tools must still work. clinical_query will return
  an error status, which is the expected behavior.

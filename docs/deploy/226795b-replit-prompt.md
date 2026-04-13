# Replit Deployment Prompt — Commit `226795b` (Tier 0–4 plan execution)

Paste this entire document into the Replit agent when you're ready to deploy
the changes on commit `226795b` (branch `claude/review-code-prompt-6OXlx`).

---

## 1. What was built in this commit

One commit spanning the full approved plan in
`/root/.claude/plans/noble-giggling-sunbeam.md`. The headline numbers:

| Area                                      | Change |
|-------------------------------------------|-------|
| New MCP tools                             | **26 across S1 / S2 / S3** |
| New DB migrations                         | **2** (`008_behavioral_tables.sql`, `009_pgvector_guidelines.sql`) |
| New Python files                          | `mcp-server/skills/patient_state_readers.py`, `mcp-server/skills/behavioral_tools.py` |
| Residual Tier 0 bug fixes                 | `age=None` propagation, stub labelling, loud migration-005 fallback |
| Docs + counts                             | `CLAUDE.md` §5 rewritten to match live state (S1=23→34, S2=15→22, S3=4→6 **after this deploy**) |

### Tools registered by server (post-deploy target counts)

**S1 — ambient-clinical-intelligence** (target ≈ 34 tools)
  Existing 23 + new:
  - Tier 2.a: `get_time_since_last_contact`, `get_care_gap_ages`,
    `list_overdue_actions`, `get_encounter_timeline`, `get_encounter_context`,
    `get_context_deltas`, `list_available_actions`
  - Tier 2.b.i: `compute_ite_estimate`, `compute_behavioral_receptivity`,
    `score_nudge_impactability`
  - Tier 2.b.ii: `check_sycophancy_risk`, `run_constitutional_critic`
  - Tier 2.b.vi: `run_healthex_pipeline`, `get_healthex_pipeline_status`
  - Tier 3: `compute_deliberation_convergence`, `get_deliberation_phases`,
    `run_batch_pre_encounter`, `search_guidelines`
  - Tier 4: `get_panel_risk_ranking`, `triage_message`

**S2 — ambient-skills-companion** (target ≈ 22 tools)
  Existing 15 + new via `mcp-server/skills/patient_state_readers.py`
  and `mcp-server/skills/behavioral_tools.py`:
  - Tier 2.a: `get_vital_trend`, `get_sdoh_profile`, `get_medication_adherence_rate`
  - Tier 2.b.iii: `classify_com_b_barrier`, `detect_conversation_teachable_moment`,
    `generate_implementation_intention`, `select_nudge_type`
  - Tier 2.b.iv: `score_llm_interaction_health`, `get_llm_interaction_history`
  - Tier 2.b.v: `trigger_jitai_nudge`

**S3 — ambient-ingestion** (target = 6 tools)
  Existing 4 + new:
  - Tier 2.b.v: `register_conversation_trigger`
  - Tier 3: `detect_healthex_format`

### Key design guarantees

- **NIS decisions are audited.** Every `score_nudge_impactability` call writes
  a row into `nis_score_audits` with the exact α/β/γ/δ weights and all four
  component scores.
- **Crisis gate is non-negotiable.** `anxiety_state='crisis'` forces
  `recommendation='suppress'` regardless of NIS compound score.
- **LLM fallbacks are deterministic.** Every LLM-enriched tool
  (`check_sycophancy_risk`, `run_constitutional_critic`) degrades to pure
  pattern-matching when `ANTHROPIC_API_KEY` is missing — tools never crash.
- **`run_healthex_pipeline` is fire-and-forget.** Returns a `job_id`
  immediately and spawns an `asyncio.Task`. Callers poll
  `get_healthex_pipeline_status`. No synchronous `await` on deliberation.
- **`generate_previsit_brief` is cache-aware only.** If no fresh deliberation
  exists within 24h, it returns the 6-month query alone. It never triggers
  `run_deliberation` synchronously.

---

## 2. Deployment checklist (run on Replit in this exact order)

```bash
# Sanity: are we on the right commit?
git log -1 --format='%h %s'
# expect: 226795b Tier 0-4 plan execution: residual bug fixes + T/P/C/R ...

# Step 1 — pull the branch
git fetch origin claude/review-code-prompt-6OXlx
git checkout claude/review-code-prompt-6OXlx
git pull origin claude/review-code-prompt-6OXlx

# Step 2 — apply the two new migrations
#         both are BEGIN/COMMIT wrapped and use CREATE IF NOT EXISTS, safe to re-run
psql "$DATABASE_URL" -f server/migrations/008_behavioral_tables.sql
psql "$DATABASE_URL" -f server/migrations/009_pgvector_guidelines.sql
#   Migration 009 requires the `vector` extension. If your Replit Postgres
#   does not have pgvector available, that statement will fail — in that
#   case, skip 009 and the search_guidelines tool will continue to return
#   {status: 'stubbed'} (by design; get_guideline still works for ID lookup).

# Step 3 — Python deps are unchanged; no pip install needed

# Step 4 — regenerate .mcp.json for the new public domain
python scripts/generate_mcp_json.py

# Step 5 — restart the 3 MCP servers + Next.js
#   Option A (Replit workflows): restart each workflow from the Run panel.
#   Option B (production one-shot):
bash start.sh
```

### Post-deploy verification

```bash
# 5a. Health
curl -s http://localhost:8001/health
curl -s http://localhost:8002/health
curl -s http://localhost:8003/health

# 5b. Live tool counts (expect 34 / 22 / 6)
for port in 8001 8002 8003; do
  echo "=== port $port ==="
  curl -s http://localhost:$port/tools | python -m json.tool | grep -c '"name"'
done

# 5c. Spot-check the new tools
curl -s http://localhost:8001/tools | python -m json.tool \
  | grep -E "score_nudge_impactability|run_healthex_pipeline|triage_message|get_panel_risk_ranking"

curl -s http://localhost:8002/tools | python -m json.tool \
  | grep -E "get_vital_trend|classify_com_b_barrier|trigger_jitai_nudge"

curl -s http://localhost:8003/tools | python -m json.tool \
  | grep -E "register_conversation_trigger|detect_healthex_format"

# 5d. Quick REST probe — S1 dimension getter (no LLM, pure DB)
curl -s -X POST http://localhost:8001/tools/get_time_since_last_contact \
  -H 'Content-Type: application/json' \
  -d '{"patient_id":"2cfaa9f2-3f47-44be-84e2-16f3a5dc0bbb"}'

# 5e. Audit-row sanity: confirm migration-008 tables exist
psql "$DATABASE_URL" -c "\dt patient_llm_interactions nis_score_audits jitai_triggers patient_com_b_assessments"

# 5f. Run the unit tests that change here
python -m pytest server/deliberation/tests/test_pipeline_resilience.py::TestAgeCoercion -v
python -m pytest tests/test_mcp_smoke.py tests/test_mcp_discovery.py -v
```

### Expected failure modes on first boot (benign)

- **`search_guidelines` returns `{status: "stubbed"}`** until migration 009
  is applied and embeddings are loaded. This is by design.
- **`compute_deliberation_convergence(backend='medcpt')`** returns
  `{convergence_score: null, error: "MedCPT backend not yet implemented..."}`.
  Also by design — Jaccard backend remains available.
- **`run_batch_pre_encounter`** returns `{status: "not_yet_wired"}`. The
  signature is stable so the overnight scheduler can integrate; the wiring
  to `server/deliberation/batch/pre_encounter_batch.py` is a follow-up task.
- If `ANTHROPIC_API_KEY` is unset, `check_sycophancy_risk` and
  `run_constitutional_critic` use pattern-matching only — no LLM call.
- If migration 009 fails (no pgvector extension), `search_guidelines` stays
  in stub mode. The `guidelines` table is optional for this deploy.

---

## 3. Known follow-ups (DO NOT block deploy on these)

1. **Sycophancy prompt template.** `server/deliberation/prompts/sycophancy_audit.xml`
   does not exist yet — the LLM-enriched path calls Claude inline with a
   system-prompt string. Extract to XML once we want version control.
2. **NIS weight tuning.** Default weights (α=0.40 β=0.25 γ=0.20 δ=0.15)
   are order-of-magnitude estimates. Calibrate against outcome data.
3. **Unit tests for new tools.** Commit added the tools and preserved
   existing tests; per-tool tests are not included. Add them in
   `tests/phase1/` and `mcp-server/tests/` before production hardening.
4. **`run_batch_pre_encounter` wiring.** Currently a signature stub.
5. **MedCPT embedding runner.** `chunk_guidelines.py` produces rows; no
   embedder fills `embedding` or populates `guidelines` yet.

---

## 4. Rollback plan (if anything goes wrong)

```bash
# The new migrations are additive; they do NOT alter existing columns.
# Dropping the new tables is safe and idempotent.
psql "$DATABASE_URL" <<'SQL'
BEGIN;
DROP TABLE IF EXISTS nis_score_audits;
DROP TABLE IF EXISTS jitai_triggers;
DROP TABLE IF EXISTS patient_com_b_assessments;
DROP TABLE IF EXISTS patient_llm_interactions;
DROP TABLE IF EXISTS guidelines;
DROP FUNCTION IF EXISTS guidelines_bm25_tokens_trg CASCADE;
COMMIT;
SQL

# Revert the code
git checkout main   # or whichever branch was live pre-deploy
bash start.sh
```

No existing tables or columns are modified by this commit, so rollback is
strictly additive-drop; there is no data to back up from the new tables
on a first deploy.

---

**Deploy command summary (one-liner for copy/paste):**

```bash
git pull origin claude/review-code-prompt-6OXlx \
  && psql "$DATABASE_URL" -f server/migrations/008_behavioral_tables.sql \
  && psql "$DATABASE_URL" -f server/migrations/009_pgvector_guidelines.sql \
  && python scripts/generate_mcp_json.py \
  && bash start.sh
```

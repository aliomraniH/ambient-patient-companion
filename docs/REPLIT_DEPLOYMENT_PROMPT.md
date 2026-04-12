# Replit Agent Deployment Prompt

Use this prompt when asking the Replit Agent to deploy the accuracy & efficiency
improvements branch (`claude/improve-patient-companion-eit5i`).

---

## Prompt

```
Deploy the updated Ambient Patient Companion with five new validator features
(F1-F5) plus Batch API support. This is a BATCH PIPELINE update — none of the
changes affect MCP tool signatures, ports, OAuth layer, or existing public
API contracts. All additions are internal to the ingestion and deliberation
pipelines.

CHANGES INCLUDED:

1. F2 — Clinical Text Sanitizer
   New: ingestion/sanitization/clinical_sanitizer.py
   Modified: ingestion/adapters/healthex/traced_writer.py (sanitize_text_field)
   Purpose: Preserves clinical notation (A+, <0.01, 38.5°C, c.68_69delAG) while
   removing prompt-injection vectors.

2. F1 — Source Anchoring + Self-Consistency + FHIR Validator
   New: ingestion/validators/source_anchor.py
   New: ingestion/validators/self_consistency.py
   New: ingestion/validators/fhir_validator.py
   Modified: ingestion/adapters/healthex/ingest.py (LLM-fallback path)
   Purpose: Catches hallucinated LLM extractions before they enter Bronze.

3. F3 — Clinical Plausibility Validation
   New: ingestion/validators/plausibility.py
   Modified: ingestion/adapters/healthex/traced_writer.py (transform stage)
   Purpose: LOINC-keyed range validation flags HbA1c 0.74 vs 7.4 extraction errors.

4. F4 — Critical Value Injector
   New: ingestion/context/critical_value_injector.py
   Modified: server/deliberation/engine.py (Phase 0.05)
   Purpose: Guarantees safety-critical labs (HbA1c, Creatinine, BP, K+) appear
   in every Gold context for deliberation.

5. F5 — Convergence-Gated Synthesis Output
   New: server/deliberation/convergence_gate.py
   Modified: server/deliberation/engine.py (Phase 2.5 + Phase 4.5)
   Purpose: Three-tier output based on convergence; nulls recommendations when
   score < 0.40. Hard constraint enforced in tests.

6. Batch API + Model Tiering
   New: server/deliberation/batch/model_router.py
   New: server/deliberation/batch/pre_encounter_batch.py
   Purpose: Model routing (Haiku/Sonnet/Opus) + Anthropic Batch API for
   nightly pre-encounter deliberation (50% cost reduction).

DEPLOYMENT STEPS (run in order):

1. Pull the branch:
   git fetch origin
   git checkout claude/improve-patient-companion-eit5i

2. No new Python dependencies required (all validators are pure Python and
   existing SDKs are reused). Skip pip install unless requirements.txt has
   changed.

3. Run the new DB migration:
   psql $DATABASE_URL -f server/migrations/006_quality_flags.sql
   This adds quality_flag (TEXT) and quality_status (VARCHAR 20) columns to
   transfer_log. Existing rows default to quality_status='ok'.

4. Verify all tests pass:
   python -m pytest ingestion/tests/test_clinical_sanitizer.py \
                    ingestion/tests/test_source_anchor.py \
                    ingestion/tests/test_self_consistency.py \
                    ingestion/tests/test_fhir_validator.py \
                    ingestion/tests/test_plausibility.py \
                    server/deliberation/tests/test_critical_value_injector.py \
                    server/deliberation/tests/test_convergence_gate.py \
                    server/deliberation/tests/test_batch_api.py -v

   Expected: 190+ passes, 0 failures.

5. Run sanitization regression (must be 100%):
   python -c "from ingestion.sanitization.clinical_sanitizer import run_sanitization_regression; print(run_sanitization_regression())"

   Expected: "All 19 preservation + 8 removal cases passed."

6. Regression check — verify existing tests still pass:
   python -m pytest tests/ server/deliberation/tests/ ingestion/tests/ -v
   Any failures here must be investigated before restart.

7. Restart the 3 MCP servers via existing Replit workflows:
   - ambient-clinical-intelligence  (port 8001)
   - ambient-skills-companion       (port 8002)
   - ambient-ingestion              (port 8003)

8. Smoke test — verify health endpoints:
   curl https://$REPLIT_DEV_DOMAIN/health
   curl https://$REPLIT_DEV_DOMAIN/mcp-skills/../health      (server 2)
   curl https://$REPLIT_DEV_DOMAIN/mcp-ingestion/../health   (server 3)

9. End-to-end validation with Maria Chen (MRN 4829341):
   - Trigger a test ingestion with a payload that contains 'A+ blood type,
     <0.01 comparator, HbA1c 7.4%' — verify all survive sanitization.
   - Trigger a deliberation via mcp__ca98...__run_deliberation with
     patient_id="4829341" and poll for results.
   - Confirm the deliberation result contains convergence_score and that
     if score < 0.40, recommendation is null.
   - Confirm Gold context contains __critical_values__ entry in
     applicable_guidelines.

10. Database sanity check:
    psql $DATABASE_URL -c "SELECT quality_status, COUNT(*) FROM transfer_log GROUP BY quality_status;"
    SELECT convergence_score, COUNT(*) FROM deliberations GROUP BY convergence_score;

ROLLBACK:
If any issues are detected, roll back via:
   git checkout main
   (migration 006 is additive — no rollback needed for the schema)

SUPPORT:
Plan file: /root/.claude/plans/lucky-nibbling-swan.md
Full implementation: branch claude/improve-patient-companion-eit5i
```

---

## Key Constraints Enforced

- **Zero breaking changes** — all 3 MCP servers, their tools, ports, and OAuth
  discovery endpoints remain unchanged.
- **Nulled values never silent** — every nulled field has a corresponding entry
  in quality_flags or anchor_flags.
- **Sanitization 100%** — CI-enforced regression suite.
- **Model routing fixed** — see server/deliberation/batch/model_router.py.
- **SYNTHESIS never recommends on convergence < 0.40** — hard constraint with
  4 dedicated tests in test_convergence_gate.py.
- **asyncpg only** — no SQLAlchemy introduced.
- **biometric_readings table** — used for all lab queries (not lab_results).

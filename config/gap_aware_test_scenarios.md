# Gap-Aware MCP Tools — Testing Scenarios

**Canonical patient**: Maria Chen, MRN `4829341`, UUID `2cfaa9f2-3f47-44be-84e2-16f3a5dc0bbb`

All scenarios below are designed to be run interactively through the Claude MCP interface connected to the Ambient Patient Companion servers. Each scenario tests specific tool access, correct sequencing, and gap-aware reasoning behavior.

---

## Scenario 1: Stale HbA1c Detection and Refresh

**Goal**: Verify that `detect_context_staleness` correctly flags stale lab data and that `search_patient_data_extended` attempts to find fresher records.

**Prompt to Claude**:
> Load Maria Chen's patient record (MRN 4829341). Her last HbA1c was recorded 100 days ago. Check whether her clinical context is fresh enough for a pre-encounter review. If any elements are stale, search the full warehouse for more recent results.

**Expected tool sequence**:
1. `get_synthetic_patient(mrn="4829341")` — Load patient record
2. `detect_context_staleness(patient_mrn="4829341", context_elements=[...], clinical_scenario="pre_encounter")` — Check freshness
3. `search_patient_data_extended(patient_mrn="4829341", search_scope=["warehouse_full_history"], data_elements=[{"element_type": "lab_result", "loinc_code": "4548-4", ...}])` — Search for fresher HbA1c

**Verification checks**:
- [ ] `detect_context_staleness` returns `freshness_score < 1.0`
- [ ] HbA1c (LOINC 4548-4) appears in `stale_elements` array
- [ ] `max_acceptable_age_hours` for HbA1c is `2160` (90 days)
- [ ] `guideline_source` references "ADA Standards of Care 2024 §6"
- [ ] `search_patient_data_extended` is called with the correct LOINC code
- [ ] Claude's narrative explicitly mentions the staleness and what was attempted

---

## Scenario 2: Drug Interaction Knowledge Search

**Goal**: Verify that `search_clinical_knowledge` correctly queries RxNorm and OpenFDA for a drug interaction, and that results are cached.

**Prompt to Claude**:
> Maria Chen (MRN 4829341) is on metformin 1000mg BID for T2DM and was recently started on buspirone 10mg for GAD. Search external clinical knowledge sources to check if there is a known drug interaction between buspirone and metformin.

**Expected tool sequence**:
1. `search_clinical_knowledge(query="buspirone and metformin interaction", query_type="drug_interaction", sources_to_search='["rxnorm", "openfda"]')` — Search external sources

**Verification checks**:
- [ ] Tool is called on `/mcp-skills` server (Server 2)
- [ ] Results include entries from `rxnorm` and/or `openfda` sources
- [ ] Each result has `source`, `finding`, `evidence_level`, `relevance_score`, `clinical_applicability`
- [ ] `synthesis_summary` provides a readable conclusion
- [ ] `gap_resolved` is boolean indicating whether high-confidence evidence was found
- [ ] Second identical query returns cached results (check `knowledge_search_cache` table or note faster response)
- [ ] Claude reports the interaction status clearly, not just raw API output

---

## Scenario 3: Confidence Assessment with Critical Gap

**Goal**: Verify that `assess_reasoning_confidence` detects a critical gap in a draft that omits renal function data when recommending metformin continuation.

**Prompt to Claude**:
> You are MIRA (treatment optimization analyst). Assess the confidence of this draft reasoning for Maria Chen (MRN 4829341):
>
> "Maria Chen is on metformin 1000mg BID for T2DM. Recommend continuing current dose. She recently started buspirone 10mg for GAD. No dose adjustments needed. Blood pressure is well controlled on current regimen."
>
> This is for the pharmacotherapy clinical domain. Use deliberation_id "test_scenario_003".

**Expected tool sequence**:
1. `assess_reasoning_confidence(agent_id="MIRA", deliberation_id="test_scenario_003", patient_mrn="4829341", reasoning_draft="...", clinical_domain="pharmacotherapy", confidence_threshold=0.7)`

**Verification checks**:
- [ ] `overall_confidence < 0.7` (threshold not met)
- [ ] `threshold_met` is `false`
- [ ] At least one gap has `gap_type` of `stale_data` or `missing_data` related to renal function (creatinine/eGFR)
- [ ] At least one gap has `severity` of `critical` or `high`
- [ ] `proceed_recommendation` is `pause_and_resolve` or `proceed_with_caveats`
- [ ] Gaps reference specific data elements (e.g., "serum creatinine", "eGFR", LOINC "2160-0")
- [ ] Critical/high gaps are automatically persisted to `reasoning_gaps` table
- [ ] Claude clearly communicates that the recommendation is unsafe without renal function data

---

## Scenario 4: Full Gap Resolution Pipeline

**Goal**: Test the complete sequence: detect gap → search knowledge → emit artifact → register trigger. This is the most comprehensive scenario.

**Prompt to Claude**:
> Perform a gap-aware clinical review for Maria Chen (MRN 4829341). She has a pre-encounter visit tomorrow. Do the following in order:
>
> 1. Load her patient record
> 2. Check the freshness of her clinical context for a pre-encounter scenario
> 3. Assess your reasoning confidence for pharmacotherapy recommendations
> 4. For any drug interaction gaps, search external clinical knowledge
> 5. For any gaps that remain unresolved, emit a reasoning gap artifact
> 6. For any gaps where lab results could resolve the issue, register a trigger to re-deliberate when results arrive
>
> Use deliberation_id "test_pipeline_004".

**Expected tool sequence** (in order):
1. `get_synthetic_patient(mrn="4829341")`
2. `detect_context_staleness(patient_mrn="4829341", ..., clinical_scenario="pre_encounter")`
3. `assess_reasoning_confidence(agent_id="ARIA", deliberation_id="test_pipeline_004", patient_mrn="4829341", ..., clinical_domain="pharmacotherapy")`
4. `search_clinical_knowledge(query="...", query_type="drug_interaction", ...)` — if drug interaction gap found
5. `emit_reasoning_gap_artifact(deliberation_id="test_pipeline_004", ..., patient_mrn="4829341")` — for unresolved gaps
6. `register_gap_trigger(patient_mrn="4829341", gap_id="...", watch_for="lab_result", ...)` — for lab-resolvable gaps

**Verification checks**:
- [ ] All 6 tool types are called (or Claude explains why one was skipped)
- [ ] Tools are called in the correct dependency order (not out of sequence)
- [ ] `assess_reasoning_confidence` is called BEFORE `search_clinical_knowledge` (gap detection before resolution)
- [ ] `emit_reasoning_gap_artifact` is called AFTER search attempts (resolution before artifact)
- [ ] `register_gap_trigger` references a `gap_id` from the emitted artifact
- [ ] The trigger's `expires_at` is a reasonable future date
- [ ] Claude provides a structured summary with: gaps found, resolutions attempted, artifacts emitted, triggers registered
- [ ] The narrative includes confidence scores and explicit caveats

---

## Scenario 5: Clarification Request to Provider

**Goal**: Verify that `request_clarification` correctly creates a clarification request when a question requires provider input.

**Prompt to Claude**:
> As ARIA (diagnostic reasoning analyst), you've identified that Maria Chen's current medication list may be incomplete — her pharmacy records show a prescription for lisinopril that isn't in the EHR medication list. Request clarification from the provider about whether lisinopril is still active. This is for deliberation_id "test_clarification_005" and gap_id "gap_med_list_001".

**Expected tool sequence**:
1. `request_clarification(deliberation_id="test_clarification_005", requesting_agent="ARIA", recipient="provider", urgency="preferred", question_text="...", clinical_rationale="...", gap_id="gap_med_list_001")`

**Verification checks**:
- [ ] `status` is `"pending"` (not answered yet)
- [ ] `clarification_id` starts with `"clar_"`
- [ ] `resolution_action` is `"fallback_applied"` (since urgency is "preferred", not "blocking")
- [ ] Claude explains what was asked, to whom, and what happens if unanswered
- [ ] Claude does NOT block on the response — it proceeds with caveats

---

## Scenario 6: Blocking Clarification (Escalation)

**Goal**: Test that a `blocking` urgency clarification correctly signals escalation.

**Prompt to Claude**:
> As ARIA, you've identified that Maria Chen may be pregnant based on a recent lab note. This would contraindicate several of her current medications. Request BLOCKING clarification from the provider. Deliberation_id is "test_blocking_006", gap_id is "gap_pregnancy_001".

**Expected tool sequence**:
1. `request_clarification(deliberation_id="test_blocking_006", requesting_agent="ARIA", recipient="provider", urgency="blocking", question_text="...", clinical_rationale="...", gap_id="gap_pregnancy_001")`

**Verification checks**:
- [ ] `resolution_action` is `"escalated"` (because urgency is "blocking")
- [ ] Claude communicates that deliberation should PAUSE until this is resolved
- [ ] Claude does NOT produce a treatment recommendation while this is pending
- [ ] The narrative explicitly flags the safety concern

---

## Scenario 7: Multi-Gap Artifact Emission

**Goal**: Test emitting multiple gap artifacts with different severities and action recommendations.

**Prompt to Claude**:
> For deliberation_id "test_multi_gap_007" on Maria Chen (MRN 4829341), emit three reasoning gap artifacts:
>
> 1. **Critical**: Unknown buspirone-metformin interaction effect on lactic acidosis risk. Recommend deferring to provider.
> 2. **High**: HbA1c stale (>90 days). Recommend including caveat in output.
> 3. **Medium**: Patient food access status unknown (SDoH). Recommend flagging for next encounter.

**Expected tool sequence**:
1. `emit_reasoning_gap_artifact(gap_type="drug_interaction_unknown", severity="critical", recommended_action_for_synthesis="defer_to_provider", ...)`
2. `emit_reasoning_gap_artifact(gap_type="stale_data", severity="high", recommended_action_for_synthesis="include_caveat_in_output", ...)`
3. `emit_reasoning_gap_artifact(gap_type="social_determinant_unknown", severity="medium", recommended_action_for_synthesis="flag_for_next_encounter", ...)`

**Verification checks**:
- [ ] All three artifacts have unique `artifact_id` values
- [ ] All return `stored: true` and `synthesis_notified: true`
- [ ] Critical artifact triggers `"synthesis_priority_escalated"` in `downstream_actions_triggered`
- [ ] High and medium artifacts do NOT trigger synthesis escalation
- [ ] Each `confidence_without_resolution` < `confidence_with_resolution`
- [ ] Claude presents the gaps in severity order (critical first)

---

## Scenario 8: Gap Trigger Registration with LOINC Codes

**Goal**: Test that triggers correctly register for specific lab results using LOINC codes.

**Prompt to Claude**:
> Register gap triggers for Maria Chen (MRN 4829341) for these missing lab results:
>
> 1. HbA1c (LOINC 4548-4) — re-run deliberation when result arrives, gap_id "gap_hba1c_008a"
> 2. Serum creatinine (LOINC 2160-0) — notify provider when result arrives, gap_id "gap_creat_008b"
> 3. GAD-7 screening score (LOINC 69737-5) — update gap artifact when result arrives, gap_id "gap_gad7_008c"
>
> All triggers should expire in 90 days.

**Expected tool sequence**:
1. `register_gap_trigger(patient_mrn="4829341", gap_id="gap_hba1c_008a", watch_for="lab_result", loinc_code="4548-4", on_fire_action="re_run_deliberation", expires_at="<90 days from now>")`
2. `register_gap_trigger(patient_mrn="4829341", gap_id="gap_creat_008b", watch_for="lab_result", loinc_code="2160-0", on_fire_action="notify_provider", expires_at="...")`
3. `register_gap_trigger(patient_mrn="4829341", gap_id="gap_gad7_008c", watch_for="screening_score", loinc_code="69737-5", on_fire_action="update_gap_artifact", expires_at="...")`

**Verification checks**:
- [ ] All three triggers return `registered: true`
- [ ] All trigger IDs start with `"trig_"`
- [ ] HbA1c trigger `estimated_resolution_probability` is `0.75` (lab_result)
- [ ] GAD-7 trigger `estimated_resolution_probability` is `0.55` (screening_score)
- [ ] Each `expires_at` is approximately 90 days in the future
- [ ] `deliberation_scope` defaults to `["full_council"]` if not specified

---

## Scenario 9: Acute Event Context Staleness

**Goal**: Verify that acute clinical scenarios have much stricter freshness thresholds.

**Prompt to Claude**:
> Maria Chen (MRN 4829341) has presented to the ED 6 hours ago. Check the freshness of her context for an acute event scenario. Her vitals were last recorded 6 hours ago, her labs 2 hours ago, and her medication list was updated 48 hours ago.

**Expected tool sequence**:
1. `detect_context_staleness(patient_mrn="4829341", context_elements=[{"element_type": "vital_sign", "last_updated": "<6h ago>", ...}, {"element_type": "lab_result", "last_updated": "<2h ago>", ...}, {"element_type": "medication_list", "last_updated": "<48h ago>", ...}], clinical_scenario="acute_event")`

**Verification checks**:
- [ ] Vitals (6h old) ARE stale in acute event (threshold is 4h)
- [ ] Labs (2h old) are NOT stale in acute event (threshold is 4h)
- [ ] Medication list freshness depends on threshold mapping (may or may not be stale)
- [ ] `freshness_score` is less than 1.0
- [ ] `recommended_refreshes` includes vitals
- [ ] Claude communicates urgency appropriate to an acute setting

---

## Scenario 10: End-to-End Pre-Visit Brief with Gap Awareness

**Goal**: The most realistic scenario — simulate what happens during a complete pre-visit brief generation with gap-aware reasoning.

**Prompt to Claude**:
> Generate a gap-aware pre-visit brief for Maria Chen (MRN 4829341). She has a primary care appointment tomorrow. Follow the full gap-aware reasoning protocol:
>
> 1. Pull her complete patient record
> 2. Check context freshness for pre-encounter
> 3. Run drug interaction checks for her current medications (metformin + buspirone)
> 4. Assess your confidence in making pharmacotherapy recommendations
> 5. Search external sources for any flagged drug interactions
> 6. Emit artifacts for any remaining gaps
> 7. Register triggers for missing lab work
> 8. Produce the pre-visit brief with explicit confidence scores and caveats for any unresolved gaps
>
> Be explicit about your confidence level for each recommendation.

**Expected behavior**:
- Claude calls tools in the correct dependency order
- Stale data is detected before recommendations are made
- Drug interactions are checked via external sources, not just internal knowledge
- Confidence is assessed and reported numerically
- Unresolved gaps appear as explicit caveats in the brief (e.g., "Note: HbA1c > 90 days old — glycemic control assessment may be unreliable")
- Gap artifacts are persisted for the care team
- Triggers are registered so the system auto-updates when lab results arrive
- The final brief clearly separates "high confidence" findings from "gap-affected" findings

**Verification checks**:
- [ ] At least 5 different tool types are used
- [ ] No recommendations are made with confidence < 0.7 without explicit caveats
- [ ] The brief mentions specific gap types and their clinical impact
- [ ] The brief includes actionable items (e.g., "Order HbA1c", "Verify renal function")
- [ ] Triggers are registered for missing labs
- [ ] The output is suitable for a PCP to review in under 2 minutes

---

## Database Verification Queries

After running the scenarios above, verify that the new tables were populated correctly:

```sql
-- Check reasoning gaps for Maria Chen
SELECT gap_id, gap_type, severity, status, description
FROM reasoning_gaps
WHERE patient_mrn = '4829341'
ORDER BY severity, created_at DESC;

-- Check clarification requests
SELECT clarification_id, requesting_agent, recipient, urgency, status, question_text
FROM clarification_requests
WHERE deliberation_id LIKE 'test_%'
ORDER BY created_at DESC;

-- Check gap triggers
SELECT trigger_id, watch_for, loinc_code, on_fire_action, status, expires_at
FROM gap_triggers
WHERE patient_mrn = '4829341'
ORDER BY created_at DESC;

-- Check knowledge search cache
SELECT cache_key, query_type, source, ttl_hours, expires_at
FROM knowledge_search_cache
ORDER BY created_at DESC
LIMIT 10;

-- Summary counts
SELECT 'reasoning_gaps' as tbl, COUNT(*) FROM reasoning_gaps WHERE patient_mrn = '4829341'
UNION ALL
SELECT 'clarification_requests', COUNT(*) FROM clarification_requests
UNION ALL
SELECT 'gap_triggers', COUNT(*) FROM gap_triggers WHERE patient_mrn = '4829341'
UNION ALL
SELECT 'knowledge_search_cache', COUNT(*) FROM knowledge_search_cache;
```

---

## Grading Rubric

| Criterion | Pass | Partial | Fail |
|-----------|------|---------|------|
| **Tool access** | All 7 new tools callable without errors | 5-6 tools work | <5 tools work |
| **Sequencing** | Tools called in correct dependency order in all scenarios | Minor ordering issues in 1-2 scenarios | Frequent out-of-order calls |
| **Gap detection** | Critical gaps identified in scenarios 3, 4, 7 | Some gaps missed | No gaps detected |
| **Knowledge search** | RxNorm/OpenFDA results returned with clinical context | Results returned but no synthesis | Search fails or returns empty |
| **Caching** | Second identical search returns faster/cached | Cache written but not read | No caching behavior |
| **Clarification routing** | Blocking vs preferred urgency correctly differentiated | Urgency acknowledged but not acted on | Urgency ignored |
| **Artifact persistence** | All emitted artifacts have valid IDs and `stored: true` | Some artifacts stored | DB writes fail |
| **Trigger registration** | Triggers registered with correct LOINC codes and probabilities | Triggers registered without specifics | Triggers not created |
| **Clinical safety** | No confident recommendations with confidence < 0.7 | Confidence mentioned but not enforced | Confident recommendations despite gaps |
| **Narrative quality** | Structured output with clear gap/caveat sections | Gaps mentioned in passing | Gaps not communicated |

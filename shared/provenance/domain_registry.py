"""Domain → tool registry and per-agent tier rules for provenance auditing.

KNOWN_TOOL_DOMAINS maps lowercase content keywords to the MCP tool that
MUST be called instead of synthesizing. Used by Rule 6
(KNOWN_DOMAIN_SYNTHESIZED_INSTEAD_OF_PENDING).

AGENT_RULES defines hard tier constraints per agent. ARIA is
corpus-bound and may not emit SYNTHESIZED. MIRA, THEO, and SYNTHESIS
may emit SYNTHESIZED but only with explicit basis and only outside
known-tool domains.

Note: several known-domain tools referenced here
(score_llm_interaction_health, classify_com_b_barrier,
score_nudge_impactability, check_sycophancy_risk, select_nudge_type,
detect_conversation_teachable_moment, register_conversation_trigger,
generate_implementation_intention) are planned but not yet implemented
on the Skills server. That is deliberate — Rule 6 forces the caller to
render a PENDING card instead of synthesizing in these domains.
"""

# ── KNOWN_TOOL_DOMAINS ────────────────────────────────────────────────
# Maps lowercase content keywords → the tool that should have been
# called. Rule 6 checks content_summary.lower() against these keys.

KNOWN_TOOL_DOMAINS: dict[str, str] = {

    # ── MIRA · LLM Interaction Health (Failure Incident B) ────────────
    "llm interaction health":         "score_llm_interaction_health",
    "llm_interaction_health":         "score_llm_interaction_health",
    "llm health":                     "score_llm_interaction_health",
    "llm over-reliance":              "score_llm_interaction_health",
    "ai over-reliance":               "score_llm_interaction_health",
    "openai/mit rct":                 "score_llm_interaction_health",
    "openai mit rct":                 "score_llm_interaction_health",
    "2025 rct":                       "score_llm_interaction_health",
    "llm interaction":                "score_llm_interaction_health",

    # ── MIRA · COM-B Barrier Classification ───────────────────────────
    "com-b barrier":                  "classify_com_b_barrier",
    "com_b_barrier":                  "classify_com_b_barrier",
    "com-b":                          "classify_com_b_barrier",
    "motivation barrier":             "classify_com_b_barrier",
    "capability barrier":             "classify_com_b_barrier",
    "opportunity barrier":            "classify_com_b_barrier",
    "behavioral barrier":             "classify_com_b_barrier",

    # ── MIRA · Implementation Intention ───────────────────────────────
    "implementation intention":       "generate_implementation_intention",
    "implementation_intention":       "generate_implementation_intention",
    "if-then plan":                   "generate_implementation_intention",
    "if then plan":                   "generate_implementation_intention",
    "when-then":                      "generate_implementation_intention",

    # ── MIRA · Teachable Moment Detection ─────────────────────────────
    "teachable moment":               "detect_conversation_teachable_moment",
    "teachable_moment":               "detect_conversation_teachable_moment",
    "intervention window":            "detect_conversation_teachable_moment",

    # ── MIRA · SDoH Assessment ────────────────────────────────────────
    "social determinants score":      "run_sdoh_assessment",
    "sdoh severity":                  "run_sdoh_assessment",
    "sdoh flag":                      "run_sdoh_assessment",

    # ── THEO · Drug Interactions ──────────────────────────────────────
    "drug interaction":               "flag_drug_interaction",
    "drug_interaction":               "flag_drug_interaction",
    "medication interaction":         "flag_drug_interaction",
    "polypharmacy":                   "flag_drug_interaction",
    "drug-drug":                      "flag_drug_interaction",

    # ── THEO · Clinical Knowledge (Failure Incident A domains) ───────
    "ppi safety":                     "search_clinical_knowledge",
    "proton pump inhibitor risk":     "search_clinical_knowledge",
    "pantoprazole risk":              "search_clinical_knowledge",
    "pantoprazole long-term":         "search_clinical_knowledge",
    "statin safety nafld":            "search_clinical_knowledge",
    "statin hepatotoxicity":          "search_clinical_knowledge",
    "uric acid management":           "search_clinical_knowledge",
    "gout treatment":                 "search_clinical_knowledge",
    "gout pharmacology":              "search_clinical_knowledge",
    "nafld medication":               "search_clinical_knowledge",
    "nafld drug":                     "search_clinical_knowledge",
    "hypomagnesemia ppi":             "search_clinical_knowledge",
    "c. diff risk":                   "search_clinical_knowledge",
    "cdiff risk":                     "search_clinical_knowledge",
    "b12 absorption ppi":             "search_clinical_knowledge",
    "semaglutide nash":               "search_clinical_knowledge",
    "glp-1 nafld":                    "search_clinical_knowledge",
    "dosing adjustment":              "search_clinical_knowledge",
    "contraindication":               "search_clinical_knowledge",

    # ── SYNTHESIS · Nudge Governance ──────────────────────────────────
    "nudge impactability":            "score_nudge_impactability",
    "nudge_impactability":            "score_nudge_impactability",
    "impactability score":            "score_nudge_impactability",
    "nis score":                      "score_nudge_impactability",
    "nudge type":                     "select_nudge_type",
    "select nudge":                   "select_nudge_type",
    "nudge selection":                "select_nudge_type",
    "conversation trigger":           "register_conversation_trigger",
    "register trigger":               "register_conversation_trigger",

    # ── SYNTHESIS · Sycophancy Score (reasoning allowed; score is not) ─
    "sycophancy score":               "check_sycophancy_risk",
    "sycophancy risk score":          "check_sycophancy_risk",
    "sycophancy index":               "check_sycophancy_risk",

    # ── SYNTHESIS · Scored Skill Outputs ──────────────────────────────
    "obt score":                      "compute_obt_score",
    "one big thing score":            "compute_obt_score",
    "wellness score":                 "compute_obt_score",
    "provider risk score":            "compute_provider_risk",
    "chase list score":               "compute_provider_risk",
}


# ── AGENT_RULES ───────────────────────────────────────────────────────
AGENT_RULES: dict[str, dict] = {

    "ARIA": {
        # Corpus-bound agent. Cannot emit SYNTHESIZED.
        # Any non-tool-call output is RETRIEVAL + evidence_gap_flagged=True.
        "forbidden_tiers": ["SYNTHESIZED"],
        "forbidden_tier_message": (
            "ARIA is a corpus-bound agent and may not emit SYNTHESIZED "
            "outputs. Any ARIA claim not backed by a live tool call must "
            "be declared RETRIEVAL with evidence_gap_flagged=True. If "
            "ARIA has reasoned beyond its corpus, call "
            "assess_reasoning_confidence and emit_reasoning_gap_artifact "
            "instead of presenting the reasoning as output."
        ),
        "tool_tier_tools": [
            "clinical_query",
            "check_screening_due",
            "assess_reasoning_confidence",
        ],
    },

    "MIRA": {
        "forbidden_tiers": [],
        "tool_tier_tools": [
            "clinical_query",
            "run_sdoh_assessment",
            "score_llm_interaction_health",
            "classify_com_b_barrier",
            "generate_implementation_intention",
            "detect_conversation_teachable_moment",
            "check_sycophancy_risk",
            "assess_reasoning_confidence",
            "request_clarification",
        ],
    },

    "THEO": {
        "forbidden_tiers": [],
        "tool_tier_tools": [
            "clinical_query",
            "flag_drug_interaction",
            "search_clinical_knowledge",
            "assess_reasoning_confidence",
        ],
        "corpus_bound_domains": [
            "ppi", "pantoprazole", "statin", "uric acid", "gout",
            "nafld drug", "hypomagnesemia", "b12 absorption",
            "drug interaction", "polypharmacy", "dosing",
        ],
    },

    "SYNTHESIS": {
        "forbidden_tiers": [],
        "tool_tier_tools": [
            "run_deliberation",
            "get_deliberation_results",
            "compute_obt_score",
            "compute_provider_risk",
            "score_nudge_impactability",
            "check_sycophancy_risk",
            "select_nudge_type",
            "register_conversation_trigger",
            "get_flag_review_status",
        ],
        "synthesized_allowed_domains": [
            "convergence rationale",
            "gate logic",
            "mood-first protocol",
            "sycophancy reasoning",
            "nudge governance rationale",
        ],
    },

    "SYSTEM": {
        # Operational / pipeline metadata (latency, model IDs, run IDs).
        # Not clinical. Permissive — all four tiers allowed — but still
        # subject to the generic rules (must declare tier, must have
        # basis if SYNTHESIZED, etc.). PENDING is disallowed because
        # SYSTEM does not defer to tool calls; it reports on the run.
        "forbidden_tiers": ["PENDING"],
        "forbidden_tier_message": (
            "SYSTEM sections describe the pipeline run itself and may "
            "not be PENDING. Use TOOL for measured facts (model, "
            "latency), SYNTHESIZED for run rationale, or RETRIEVAL for "
            "config lookups."
        ),
        "tool_tier_tools": [],
    },

    "PATIENT_FACING": {
        # Anything rendered to a patient. AB 3030 (California 2025)
        # requires an AI-use disclosure tag on every patient-facing
        # output. The dedicated Rule 10 check below enforces this.
        # Free synthesis at patient-level is forbidden — synthesis
        # belongs to SYNTHESIS, which then routes to PATIENT_FACING.
        "forbidden_tiers": ["SYNTHESIZED"],
        "forbidden_tier_message": (
            "PATIENT_FACING sections must be routed through SYNTHESIS "
            "first and may not be SYNTHESIZED directly. Tier must be "
            "TOOL (for scored/structured content) or RETRIEVAL (for "
            "guideline-quoted content)."
        ),
        "tool_tier_tools": [
            "compute_obt_score",
            "generate_previsit_brief",
            "select_nudge_type",
        ],
    },
}

# Agents whose outputs participate in provenance auditing.
# Every entry MUST have a corresponding entry in AGENT_RULES — enforced
# by tests/test_provenance_registry_coverage.py.
ALL_AUDITED_AGENTS = {
    "ARIA", "MIRA", "THEO", "SYNTHESIS", "SYSTEM", "PATIENT_FACING",
}

# Agents for whom Rule 6 (known-domain synthesis) applies.
# ARIA is included so that Rule 6 catches cross-domain TOOL mismatches
# (e.g. ARIA declaring TOOL with tool_name="clinical_query" for
# pharmacology content that belongs to THEO's corpus). ARIA's
# SYNTHESIZED tier is already blocked by the agent-tier pre-check, so
# Rule 6 against ARIA only ever fires for TOOL-tier mismatches.
RULE_6_AGENTS = {"ARIA", "MIRA", "THEO", "SYNTHESIS"}

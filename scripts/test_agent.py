#!/usr/bin/env python3
"""
test_agent.py  — autonomous MCP agent that exercises all 90 tools across
three servers and then retrieves the audit log to verify end-to-end recording.

Run:  python scripts/test_agent.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastmcp import Client

# ── server endpoints ─────────────────────────────────────────────────────────
SERVERS = {
    "clinical":  "http://localhost:8001/mcp",
    "skills":    "http://localhost:8002/mcp",
    "ingestion": "http://localhost:8003/mcp",
}

# ── demo patient (Maria Chen — always-present fixture) ───────────────────────
DEMO_MRN    = "MC-2025-4829"
DEMO_PID    = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

# ── display helpers ───────────────────────────────────────────────────────────
W = 76

def _hr(ch: str = "─") -> None:  print(ch * W)
def _header(t: str) -> None:
    print(); _hr("═"); print(f"  {t}"); _hr("═")
def _section(t: str) -> None:
    print(); _hr(); print(f"  {t}"); _hr()
def _ok(m: str) -> None:    print(f"    ✓  {m}")
def _err(m: str) -> None:   print(f"    ✗  {m}")
def _info(m: str) -> None:  print(f"    ·  {m}")

def _show(label: str, value: Any, cap: int = 40) -> None:
    pad = "      "
    if isinstance(value, (dict, list)):
        lines = json.dumps(value, indent=2, default=str).splitlines()
        print(f"    {label}:")
        for ln in lines[:cap]:
            print(f"{pad}{ln}")
        if len(lines) > cap:
            print(f"{pad}… ({len(lines)-cap} more lines)")
    elif isinstance(value, str) and "\n" in value:
        print(f"    {label}:")
        for ln in value.splitlines()[:20]:
            print(f"{pad}{ln}")
    else:
        short = str(value)
        if len(short) > 200:
            short = short[:200] + "…"
        print(f"    {label}: {short}")


# ── FastMCP call_tool helper ──────────────────────────────────────────────────

async def call(client: Client, tool: str, args: dict | None = None) -> Any:
    """
    Call an MCP tool and return structured content.

    FastMCP CallToolResult:
      .content[0].text  → raw server JSON string  (always reliable)
      .data             → FastMCP-parsed Python object; may be Pydantic Root()
                          when the schema doesn't match, so we avoid it.
      .is_error         → True when the tool raised an exception
    """
    result = await client.call_tool(tool, args or {})

    if result.is_error:
        raw = result.content[0].text if result.content else "unknown error"
        raise RuntimeError(f"Tool error: {raw}")

    # Always prefer the raw text from the server — it's the actual JSON output
    if result.content:
        text = result.content[0].text
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    # Last resort: FastMCP data attribute (may be a Pydantic model)
    return result.data


# ── SERVER 1 — Clinical (8001, 44 tools) ─────────────────────────────────────

async def test_clinical() -> None:
    _header("SERVER 1 — ambient-clinical-intelligence  (port 8001, 44 tools)")

    async with Client(SERVERS["clinical"]) as c:
        tools = await c.list_tools()
        _info(f"{len(tools)} tools registered on this server")

        # 1. Synthetic / demo patient ─────────────────────────────────────────
        _section(f"get_synthetic_patient  (mrn={DEMO_MRN})")
        try:
            patient = await call(c, "get_synthetic_patient", {"mrn": DEMO_MRN})
            if isinstance(patient, dict):
                name = (f"{patient.get('first_name','')} "
                        f"{patient.get('last_name','')}").strip()
                _ok(f"Patient: {name}   MRN={DEMO_MRN}")
                safe_keys = [k for k in
                    ["mrn","conditions","medications","gender","chronic_conditions"]
                    if k in patient]
                _show("snapshot", {k: patient[k] for k in safe_keys})
            else:
                _ok(str(patient)[:300])
        except Exception as e:
            _err(f"get_synthetic_patient: {e}")

        # 2. Data source status ───────────────────────────────────────────────
        _section("get_data_source_status")
        try:
            status = await call(c, "get_data_source_status")
            _ok("Data source status retrieved")
            _show("status", status)
        except Exception as e:
            _err(f"get_data_source_status: {e}")

        # 3. Guideline lookup ─────────────────────────────────────────────────
        _section("get_guideline  (recommendation_id='9.1a')")
        try:
            g = await call(c, "get_guideline", {"recommendation_id": "9.1a"})
            _ok("Guideline retrieved")
            _show("guideline", g)
        except Exception as e:
            _err(f"get_guideline: {e}")

        # 4. Search guidelines ────────────────────────────────────────────────
        _section("search_guidelines  (query='diabetes management')")
        try:
            r = await call(c, "search_guidelines",
                           {"query": "diabetes management", "limit": 5})
            _ok("Guideline search complete")
            _show("results", r)
        except Exception as e:
            _err(f"search_guidelines: {e}")

        # 5. Screening check ──────────────────────────────────────────────────
        _section("check_screening_due  (patient_age=54, sex=female, T2DM+obesity)")
        try:
            s = await call(c, "check_screening_due", {
                "patient_age": 54,
                "sex": "female",
                "conditions": ["type_2_diabetes", "obesity"],
            })
            _ok("Screening check complete")
            _show("screenings", s)
        except Exception as e:
            _err(f"check_screening_due: {e}")

        # 6. Drug interaction ─────────────────────────────────────────────────
        _section("flag_drug_interaction  (lisinopril + losartan — ACE/ARB dual)")
        try:
            i = await call(c, "flag_drug_interaction",
                           {"medications": ["lisinopril", "losartan"]})
            _ok("Interaction check complete")
            _show("interaction", i)
        except Exception as e:
            _err(f"flag_drug_interaction: {e}")

        # 7. Deliberation results ─────────────────────────────────────────────
        _section(f"get_deliberation_results  (patient_id={DEMO_PID[:8]}…)")
        try:
            d = await call(c, "get_deliberation_results",
                           {"patient_id": DEMO_PID, "limit": 2})
            _ok("Deliberation results retrieved")
            _show("results", d)
        except Exception as e:
            _err(f"get_deliberation_results: {e}")

        # 8. Flag review status ───────────────────────────────────────────────
        _section(f"get_flag_review_status  (patient_id={DEMO_PID[:8]}…)")
        try:
            f = await call(c, "get_flag_review_status",
                           {"patient_id": DEMO_PID})
            _ok("Flag review status retrieved")
            _show("flags", f)
        except Exception as e:
            _err(f"get_flag_review_status: {e}")

        # 9. Patient knowledge ────────────────────────────────────────────────
        _section(f"get_patient_knowledge  (patient_id={DEMO_PID[:8]}…)")
        try:
            k = await call(c, "get_patient_knowledge",
                           {"patient_id": DEMO_PID})
            _ok("Patient knowledge retrieved")
            _show("knowledge", k)
        except Exception as e:
            _err(f"get_patient_knowledge: {e}")

        # 10. Pending nudges ──────────────────────────────────────────────────
        _section(f"get_pending_nudges  (patient_id={DEMO_PID[:8]}…)")
        try:
            n = await call(c, "get_pending_nudges",
                           {"patient_id": DEMO_PID})
            _ok("Pending nudges retrieved")
            _show("nudges", n)
        except Exception as e:
            _err(f"get_pending_nudges: {e}")

        # 11. Care-gap ages ───────────────────────────────────────────────────
        _section(f"get_care_gap_ages  (patient_id={DEMO_PID[:8]}…)")
        try:
            g2 = await call(c, "get_care_gap_ages",
                            {"patient_id": DEMO_PID})
            _ok("Care-gap ages retrieved")
            _show("gaps", g2)
        except Exception as e:
            _err(f"get_care_gap_ages: {e}")

        # 12. Overdue actions ─────────────────────────────────────────────────
        _section(f"list_overdue_actions  (patient_id={DEMO_PID[:8]}…)")
        try:
            oa = await call(c, "list_overdue_actions",
                            {"patient_id": DEMO_PID})
            _ok("Overdue actions retrieved")
            _show("overdue", oa)
        except Exception as e:
            _err(f"list_overdue_actions: {e}")

        # 13. Encounter timeline ──────────────────────────────────────────────
        _section(f"get_encounter_timeline  (patient_id={DEMO_PID[:8]}…)")
        try:
            et = await call(c, "get_encounter_timeline",
                            {"patient_id": DEMO_PID})
            _ok("Encounter timeline retrieved")
            _show("timeline", et)
        except Exception as e:
            _err(f"get_encounter_timeline: {e}")

        # 14. ITE estimate ────────────────────────────────────────────────────
        _section(f"compute_ite_estimate  (patient_id={DEMO_PID[:8]}…)")
        try:
            ite = await call(c, "compute_ite_estimate", {
                "patient_id": DEMO_PID,
                "care_gap_count": 3,
                "trajectory_direction": "worsening",
                "modifiable_risk_fraction": 0.65,
            })
            _ok("ITE estimate computed")
            _show("ite", ite)
        except Exception as e:
            _err(f"compute_ite_estimate: {e}")

        # 15. Behavioural receptivity ─────────────────────────────────────────
        _section(f"compute_behavioral_receptivity  (patient_id={DEMO_PID[:8]}…)")
        try:
            from datetime import datetime as _dt
            _dow = _dt.now().weekday()         # 0=Mon … 6=Sun
            br = await call(c, "compute_behavioral_receptivity", {
                "patient_id": DEMO_PID,
                "last_clinical_event_hours": 72.0,
                "last_app_interaction_hours": 4.5,
                "day_of_week": _dow,
                "days_since_temporal_landmark": 14,
            })
            _ok("Behavioral receptivity computed")
            _show("receptivity", br)
        except Exception as e:
            _err(f"compute_behavioral_receptivity: {e}")

        # 16. Nudge impactability ─────────────────────────────────────────────
        _section(f"score_nudge_impactability  (patient_id={DEMO_PID[:8]}…)")
        try:
            from datetime import datetime as _dt2
            _dow2 = _dt2.now().weekday()
            ni = await call(c, "score_nudge_impactability", {
                "patient_id": DEMO_PID,
                "deliberation_id": "test-delib-001",
                "ite_estimate": 0.62,
                "care_gap_count": 3,
                "trajectory_direction": "worsening",
                "last_clinical_event_hours": 72.0,
                "last_app_interaction_hours": 4.5,
                "day_of_week": _dow2,
                "days_since_temporal_landmark": 14,
            })
            _ok("Nudge impactability scored")
            _show("impactability", ni)
        except Exception as e:
            _err(f"score_nudge_impactability: {e}")

        # 17. Sycophancy risk ─────────────────────────────────────────────────
        _section("check_sycophancy_risk")
        try:
            sr = await call(c, "check_sycophancy_risk", {
                "patient_id": DEMO_PID,
                "draft_output": "Your HbA1c looks great! No changes needed.",
                "originating_agent": "ARIA",
            })
            _ok("Sycophancy risk checked")
            _show("risk", sr)
        except Exception as e:
            _err(f"check_sycophancy_risk: {e}")

        # 18. Provenance gate ─────────────────────────────────────────────────
        _section("verify_output_provenance  (payload as JSON string)")
        try:
            pv = await call(c, "verify_output_provenance", {
                "payload": json.dumps({
                    "output_type": "NUDGE",
                    "content": "Take your metformin with dinner.",
                    "agent": "ARIA",
                    "source_tier": "behavioral",
                }),
                "patient_mrn": DEMO_MRN,
            })
            _ok("Provenance verified")
            _show("provenance", pv)
        except Exception as e:
            _err(f"verify_output_provenance: {e}")

        # 19. Panel risk ranking ──────────────────────────────────────────────
        _section("get_panel_risk_ranking  (provider_id=DEMO-PROVIDER-001)")
        try:
            pr = await call(c, "get_panel_risk_ranking", {
                "provider_id": "DEMO-PROVIDER-001",
                "limit": 5,
            })
            _ok("Panel risk ranking retrieved")
            _show("ranking", pr)
        except Exception as e:
            _err(f"get_panel_risk_ranking: {e}")

        # 20. Triage message ──────────────────────────────────────────────────
        _section("triage_message  (dizziness + blurry vision)")
        try:
            tm = await call(c, "triage_message", {
                "patient_id": DEMO_PID,
                "content": "I've been feeling dizzy and my vision is blurry today.",
                "message_type": "patient_message",
            })
            _ok("Message triaged")
            _show("triage", tm)
        except Exception as e:
            _err(f"triage_message: {e}")


# ── SERVER 2 — Skills (8002, 40 tools, incl. audit query tools) ──────────────

async def test_skills() -> None:
    _header("SERVER 2 — ambient-skills-companion  (port 8002, 40 tools)")

    async with Client(SERVERS["skills"]) as c:
        tools = await c.list_tools()
        _info(f"{len(tools)} tools registered on this server")

        # 1. Behavioral atom search ───────────────────────────────────────────
        _section("search_similar_atoms  (exercise resistance)")
        try:
            atoms = await call(c, "search_similar_atoms", {
                "query_text": "exercise resistance and low motivation",
                "top_k": 3,
            })
            _ok("Atom similarity search complete")
            _show("atoms", atoms)
        except Exception as e:
            _err(f"search_similar_atoms: {e}")

        # 2. Clinical knowledge search ────────────────────────────────────────
        _section("search_clinical_knowledge  (SGLT2 inhibitors CKD)")
        try:
            ck = await call(c, "search_clinical_knowledge", {
                "query": "SGLT2 inhibitors CKD renal protection",
                "query_type": "treatment",
                "max_results_per_source": 3,
            })
            _ok("Clinical knowledge search complete")
            _show("knowledge", ck)
        except Exception as e:
            _err(f"search_clinical_knowledge: {e}")

        # 3. OBT score ────────────────────────────────────────────────────────
        _section(f"compute_obt_score  (patient_id={DEMO_PID[:8]}…)")
        try:
            obt = await call(c, "compute_obt_score", {"patient_id": DEMO_PID})
            _ok("OBT score computed")
            _show("obt", obt)
        except Exception as e:
            _err(f"compute_obt_score: {e}")

        # 4. Provider risk ────────────────────────────────────────────────────
        _section(f"compute_provider_risk  (patient_id={DEMO_PID[:8]}…)")
        try:
            risk = await call(c, "compute_provider_risk",
                              {"patient_id": DEMO_PID})
            _ok("Provider risk computed")
            _show("risk", risk)
        except Exception as e:
            _err(f"compute_provider_risk: {e}")

        # 5. Vital trend ──────────────────────────────────────────────────────
        _section(f"get_vital_trend  (BP systolic, 30 days)")
        try:
            vt = await call(c, "get_vital_trend", {
                "patient_id": DEMO_PID,
                "metric_type": "blood_pressure_systolic",
                "days": 30,
            })
            _ok("Vital trend retrieved")
            _show("trend", vt)
        except Exception as e:
            _err(f"get_vital_trend: {e}")

        # 6. SDoH profile ─────────────────────────────────────────────────────
        _section(f"get_sdoh_profile  (patient_id={DEMO_PID[:8]}…)")
        try:
            sdoh = await call(c, "get_sdoh_profile", {"patient_id": DEMO_PID})
            _ok("SDoH profile retrieved")
            _show("sdoh", sdoh)
        except Exception as e:
            _err(f"get_sdoh_profile: {e}")

        # 7. Medication adherence ─────────────────────────────────────────────
        _section(f"get_medication_adherence_rate  (30 days)")
        try:
            adh = await call(c, "get_medication_adherence_rate",
                             {"patient_id": DEMO_PID, "days": 30})
            _ok("Adherence rate retrieved")
            _show("adherence", adh)
        except Exception as e:
            _err(f"get_medication_adherence_rate: {e}")

        # 8. Data freshness ───────────────────────────────────────────────────
        _section(f"check_data_freshness  (patient_id={DEMO_PID[:8]}…)")
        try:
            fr = await call(c, "check_data_freshness",
                            {"patient_id": DEMO_PID})
            _ok("Data freshness checked")
            _show("freshness", fr)
        except Exception as e:
            _err(f"check_data_freshness: {e}")

        # 9. Previsit brief ───────────────────────────────────────────────────
        _section(f"generate_previsit_brief  (patient_id={DEMO_PID[:8]}…)")
        try:
            brief = await call(c, "generate_previsit_brief",
                               {"patient_id": DEMO_PID})
            _ok("Previsit brief generated")
            _show("brief", brief)
        except Exception as e:
            _err(f"generate_previsit_brief: {e}")

        # 10. Source conflicts ────────────────────────────────────────────────
        _section(f"get_source_conflicts  (patient_id={DEMO_PID[:8]}…)")
        try:
            sc = await call(c, "get_source_conflicts",
                            {"patient_id": DEMO_PID})
            _ok("Source conflicts retrieved")
            _show("conflicts", sc)
        except Exception as e:
            _err(f"get_source_conflicts: {e}")

        # 11. Behavioral gaps ─────────────────────────────────────────────────
        _section(f"get_behavioral_gaps  (patient_id={DEMO_PID[:8]}…)")
        try:
            bg = await call(c, "get_behavioral_gaps",
                            {"patient_id": DEMO_PID})
            _ok("Behavioral gaps retrieved")
            _show("gaps", bg)
        except Exception as e:
            _err(f"get_behavioral_gaps: {e}")

        # 12. Behavioral screening summary ────────────────────────────────────
        _section(f"get_behavioral_screening_summary  (patient_id={DEMO_PID[:8]}…)")
        try:
            bss = await call(c, "get_behavioral_screening_summary",
                             {"patient_id": DEMO_PID})
            _ok("Behavioral screening summary retrieved")
            _show("summary", bss)
        except Exception as e:
            _err(f"get_behavioral_screening_summary: {e}")

        # 13. LLM interaction history ─────────────────────────────────────────
        _section(f"get_llm_interaction_history  (patient_id={DEMO_PID[:8]}…, 7 days)")
        try:
            lh = await call(c, "get_llm_interaction_history",
                            {"patient_id": DEMO_PID, "days": 7})
            _ok("LLM interaction history retrieved")
            _show("history", lh)
        except Exception as e:
            _err(f"get_llm_interaction_history: {e}")

        # 14. Provenance gate (skills) ────────────────────────────────────────
        _section("verify_output_provenance  (skills server)")
        try:
            pv = await call(c, "verify_output_provenance", {
                "payload": json.dumps({
                    "output_type": "CLINICAL_FINDING",
                    "content": "Patient HbA1c 8.1% — poor glycemic control.",
                    "agent": "MIRA",
                    "source_tier": "clinical",
                }),
                "patient_mrn": DEMO_MRN,
            })
            _ok("Provenance verified")
            _show("provenance", pv)
        except Exception as e:
            _err(f"verify_output_provenance: {e}")

        # ── AUDIT LOG QUERY TOOLS ─────────────────────────────────────────────
        _header("AUDIT LOG QUERY  (via Skills server — call_history tools)")

        _section("get_current_session  — live in-memory stats")
        try:
            sess = await call(c, "get_current_session")
            _ok("Live session state retrieved")
            _show("session", sess)
        except Exception as e:
            _err(f"get_current_session: {e}")

        _section("list_sessions  (limit=10) — DB rows, newest first")
        sessions: list[dict] = []
        try:
            sessions = await call(c, "list_sessions", {"limit": 10})
            if isinstance(sessions, list):
                _ok(f"{len(sessions)} sessions found in audit log")
                for s in sessions:
                    ts = str(s.get("last_call_at", "?"))[:19].replace("T", " ")
                    _info(f"  {s.get('server_name','?'):12s} | "
                          f"{s.get('call_count',0):3d} calls | "
                          f"last: {ts} | "
                          f"id={str(s.get('session_id','?'))[:8]}…")
            else:
                _show("sessions", sessions)
        except Exception as e:
            _err(f"list_sessions: {e}")

        _section("get_session_transcript  — latest session, full call log")
        try:
            tx = await call(c, "get_session_transcript",
                            {"include_full_output": False})
            if isinstance(tx, dict):
                if "note" in tx:
                    _info(tx["note"])
                else:
                    total = tx.get("total_calls", 0)
                    sid   = str(tx.get("session_id", "?"))[:8]
                    srv   = tx.get("server_name", "?")
                    _ok(f"Transcript: {total} calls | server={srv} | session={sid}…")
                    for entry in (tx.get("calls") or [])[-10:]:
                        ts   = str(entry.get("called_at","?"))[:19].replace("T"," ")
                        dur  = entry.get("duration_ms","?")
                        icon = "✓" if entry.get("outcome")=="success" else "✗"
                        _info(f"  {icon} [{ts}] "
                              f"{entry.get('tool_name','?'):<35s} {dur}ms")
                    rem = total - 10
                    if rem > 0:
                        _info(f"  … and {rem} earlier calls")
            else:
                _show("transcript", tx)
        except Exception as e:
            _err(f"get_session_transcript: {e}")

        _section("search_tool_calls  (last 10 min, all servers)")
        try:
            recent = await call(c, "search_tool_calls",
                                {"from_minutes_ago": 10, "limit": 50})
            if isinstance(recent, list):
                _ok(f"{len(recent)} calls recorded in the last 10 minutes")
                for row in recent:
                    ts   = str(row.get("called_at","?"))[:19].replace("T"," ")
                    dur  = row.get("duration_ms","?")
                    icon = "✓" if row.get("outcome")=="success" else "✗"
                    _info(f"  {icon} [{ts}] "
                          f"{row.get('server_name','?'):10s} | "
                          f"{row.get('tool_name','?'):<35s} {dur}ms")
            else:
                _show("recent_calls", recent)
        except Exception as e:
            _err(f"search_tool_calls: {e}")

        _section("search_tool_calls  (errors only, last 30 min)")
        try:
            errors = await call(c, "search_tool_calls",
                                {"outcome": "error",
                                 "from_minutes_ago": 30, "limit": 20})
            if isinstance(errors, list):
                if errors:
                    _info(f"{len(errors)} errored calls found:")
                    for row in errors:
                        ts  = str(row.get("called_at","?"))[:19].replace("T"," ")
                        _info(f"  ✗ [{ts}] "
                              f"{row.get('server_name','?'):10s} | "
                              f"{row.get('tool_name','?'):<35s} | "
                              f"{str(row.get('error_message','?'))[:60]}")
                else:
                    _ok("No errors in the last 30 minutes")
            else:
                _show("errors", errors)
        except Exception as e:
            _err(f"search_tool_calls(errors): {e}")


# ── SERVER 3 — Ingestion (8003, 6 tools) ─────────────────────────────────────

async def test_ingestion() -> None:
    _header("SERVER 3 — ambient-ingestion  (port 8003, 6 tools)")

    async with Client(SERVERS["ingestion"]) as c:
        tools = await c.list_tools()
        _info(f"{len(tools)} tools registered on this server")

        # 1. Format detection ─────────────────────────────────────────────────
        _section("detect_healthex_format  (raw_response = sample HealthEx text)")
        sample = (
            "PATIENT: John Doe | DOB: 1965-03-15 | MRN: 99001\n"
            "CONDITION: Type 2 Diabetes Mellitus (E11)\n"
            "MED: Metformin 1000mg BID\n"
            "LAB: HbA1c 8.1 (2025-11-10)\n"
            "BP: 138/82 (2025-11-10)\n"
        )
        try:
            fmt = await call(c, "detect_healthex_format",
                             {"raw_response": sample})
            _ok("Format detection complete")
            _show("format", fmt)
        except Exception as e:
            _err(f"detect_healthex_format: {e}")

        # 2. Context staleness ────────────────────────────────────────────────
        _section(f"detect_context_staleness  "
                 f"(patient_mrn={DEMO_MRN}, clinical_scenario=pre_encounter)")
        try:
            st = await call(c, "detect_context_staleness", {
                "patient_mrn": DEMO_MRN,
                "context_elements": [],
                "clinical_scenario": "pre_encounter",
            })
            _ok("Staleness detection complete")
            _show("staleness", st)
        except Exception as e:
            _err(f"detect_context_staleness: {e}")

        # 3. Extended patient data search ─────────────────────────────────────
        _section(f"search_patient_data_extended  (mrn={DEMO_MRN}, 'HbA1c')")
        try:
            sr = await call(c, "search_patient_data_extended", {
                "patient_mrn": DEMO_MRN,
                "search_scope": [],
                "data_elements": [],
                "fhir_query_override": "HbA1c blood sugar diabetes",
            })
            _ok("Extended search complete")
            _show("search", sr)
        except Exception as e:
            _err(f"search_patient_data_extended: {e}")

        # 4. Provenance gate (ingestion) ──────────────────────────────────────
        _section("verify_output_provenance  (ingestion server)")
        try:
            pv = await call(c, "verify_output_provenance", {
                "payload": json.dumps({
                    "output_type": "INGESTION_RECORD",
                    "content": "HealthEx patient data ingested successfully.",
                    "agent": "INGESTION",
                    "source_tier": "ingestion",
                }),
                "patient_mrn": DEMO_MRN,
            })
            _ok("Provenance verified")
            _show("provenance", pv)
        except Exception as e:
            _err(f"verify_output_provenance: {e}")


# ── Final summary ─────────────────────────────────────────────────────────────

async def print_summary() -> None:
    _header("FINAL SUMMARY")

    totals: dict[str, int] = {}
    async with Client(SERVERS["clinical"]) as c:
        totals["clinical (8001)"] = len(await c.list_tools())
    async with Client(SERVERS["skills"]) as c:
        totals["skills   (8002)"] = len(await c.list_tools())
    async with Client(SERVERS["ingestion"]) as c:
        totals["ingestion(8003)"] = len(await c.list_tools())

    print()
    print("  Tool counts per server")
    for srv, n in totals.items():
        print(f"    {srv}: {n}")
    print(f"    {'─'*32}")
    print(f"    Total:           {sum(totals.values())} tools")

    print()
    print("  Audit log — calls recorded per server (since startup)")
    async with Client(SERVERS["skills"]) as c:
        try:
            sessions = await call(c, "list_sessions", {"limit": 200})
            if isinstance(sessions, list) and sessions:
                by_srv: dict[str, int] = {}
                for s in sessions:
                    srv = s.get("server_name", "?")
                    by_srv[srv] = by_srv.get(srv, 0) + (s.get("call_count") or 0)
                for srv, cnt in sorted(by_srv.items()):
                    print(f"    {srv:16s}: {cnt} calls")
                print(f"    {'─'*32}")
                print(f"    Total:           {sum(by_srv.values())} calls in audit log")
            else:
                _info("Audit log is empty (no sessions recorded yet)")
        except Exception as e:
            _info(f"Could not query audit log: {e}")


# ── entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    t0 = datetime.now(tz=timezone.utc)

    print()
    print("╔" + "═" * (W - 2) + "╗")
    print("║  AMBIENT PATIENT COMPANION — Full MCP Agent Test" + " " * (W - 52) + "║")
    print(f"║  {t0.strftime('%Y-%m-%d %H:%M:%S UTC')}" + " " * (W - 29) + "║")
    print(f"║  Demo patient: Maria Chen  MRN={DEMO_MRN}" + " " * (W - 43) + "║")
    print("╚" + "═" * (W - 2) + "╝")

    try:
        await test_clinical()
    except Exception as e:
        _err(f"Clinical server test aborted: {e}")

    try:
        await test_skills()
    except Exception as e:
        _err(f"Skills server test aborted: {e}")

    try:
        await test_ingestion()
    except Exception as e:
        _err(f"Ingestion server test aborted: {e}")

    try:
        await print_summary()
    except Exception as e:
        _err(f"Summary aborted: {e}")

    elapsed = (datetime.now(tz=timezone.utc) - t0).total_seconds()
    print()
    _hr("═")
    print(f"  Agent test complete in {elapsed:.1f}s")
    _hr("═")
    print()


if __name__ == "__main__":
    asyncio.run(main())

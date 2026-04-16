"""
MCP Health & Stress Test
========================
Tests all three MCP servers for robustness under conditions Claude actually
produces: varied argument formats, extended delays, rapid-fire bursts, and
edge-case inputs.

Test structure
--------------
Phase 0  — OAuth PKCE (fresh token per server)
Phase 1  — Health baseline  (initialize + tools/list on all 3)
Phase 2  — Format stress    (same tools called 6 different ways each)
Phase 3  — Long delay       (90 s — proxy would kill stateful sessions)
Phase 4  — Post-delay check (no re-auth; all 3 servers must still respond)
Phase 5  — Rapid-fire burst (20 cross-server calls in ≤ 10 s)
Phase 6  — Medium delay     (70 s)
Phase 7  — Edge-case inputs (nulls, empty arrays, unicode, very long strings)
Phase 8  — Token validity   (confirm DB-backed tokens survive everything)
Summary  — pass/fail tally + public MCP URLs

Run:
    python3 tests/test_mcp_health_stress.py            # full delays (90s + 70s)
    python3 tests/test_mcp_health_stress.py --quick    # short delays (8s + 5s)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROXY_BASE   = "http://localhost:5000"
REDIRECT_URI = "http://127.0.0.1:9999/callback"
PATIENT_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
PATIENT_MRN  = "MC-2025-4829"

SERVERS = {"clinical": 8001, "skills": 8002, "ingestion": 8003}

DELAY_LONG   = 90   # s — overridden by --quick
DELAY_MEDIUM = 70   # s

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

GREEN  = "\033[92m"; RED  = "\033[91m"; YELLOW = "\033[93m"
CYAN   = "\033[96m"; BOLD = "\033[1m";  RESET  = "\033[0m"
DIM    = "\033[2m";  MAG  = "\033[95m"

def ok(m: str)   -> None: print(f"  {GREEN}✓{RESET} {m}")
def fail(m: str) -> None: print(f"  {RED}✗{RESET} {m}")
def info(m: str) -> None: print(f"  {CYAN}→{RESET} {m}")
def warn(m: str) -> None: print(f"  {YELLOW}⚠{RESET} {m}")
def hdr(m: str)  -> None: print(f"\n{BOLD}{m}{RESET}")
def dim(m: str)  -> None: print(f"  {DIM}{m}{RESET}")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class Srv:
    name: str
    port: int
    token: str = ""
    tools: list[str] = field(default_factory=list)
    errors: int = 0
    calls: int = 0

RESULTS: dict[str, list[str]] = {"pass": [], "fail": []}

def record(label: str, passed: bool, detail: str = "") -> None:
    if passed:
        RESULTS["pass"].append(label)
        ok(f"{label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    else:
        RESULTS["fail"].append(label)
        fail(f"{label}" + (f"  → {RED}{detail}{RESET}" if detail else ""))

# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def pkce_pair() -> tuple[str, str]:
    v = secrets.token_urlsafe(64)
    c = base64.urlsafe_b64encode(
        hashlib.sha256(v.encode()).digest()
    ).rstrip(b"=").decode()
    return v, c

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def oauth_post(http: requests.Session, path: str, body: dict,
               retries: int = 3) -> dict:
    for attempt in range(retries):
        r = http.post(f"{PROXY_BASE}{path}", json=body, timeout=30)
        if r.status_code == 429:
            wait = 12 * (attempt + 1)
            warn(f"rate-limited on {path}, waiting {wait}s …")
            time.sleep(wait)
            continue
        return {"status": r.status_code, "body": r.json() if r.text else {}}
    return {"status": 429, "body": {}}

def mcp_post(http: requests.Session, port: int, token: str,
             method: str, params: dict | None = None,
             retries: int = 5) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": method,
            "params": params or {}}
    t0 = time.time()
    for attempt in range(retries):
        try:
            r = http.post(f"{PROXY_BASE}/api/mcp/{port}/mcp",
                          json=body, headers=headers, timeout=30)
            if r.status_code == 429 and attempt < retries - 1:
                wait = 10 * (attempt + 1)
                warn(f"rate-limited (attempt {attempt+1}), waiting {wait}s …")
                time.sleep(wait)
                continue
            elapsed = time.time() - t0
            try:
                rb = r.json()
            except Exception:
                rb = {"raw": r.text[:200]}
            return {"status": r.status_code, "body": rb, "elapsed": elapsed}
        except Exception as exc:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return {"status": 0, "body": {"error": str(exc)},
                    "elapsed": time.time() - t0}
    return {"status": 429, "body": {"error": "rate_limit_exhausted"},
            "elapsed": time.time() - t0}

def tool_call(http: requests.Session, srv: Srv, tool: str,
              args: dict, *, label: str = "") -> dict:
    srv.calls += 1
    resp = mcp_post(http, srv.port, srv.token, "tools/call",
                    {"name": tool, "arguments": args})
    elapsed = resp["elapsed"]
    body    = resp["body"]

    result_text = ""
    if "result" in body:
        content = body["result"].get("content", [])
        if content and isinstance(content, list):
            result_text = content[0].get("text", "")[:120]
        else:
            result_text = str(body["result"])[:120]
    elif "error" in body:
        result_text = str(body["error"])[:120]

    tag = label or tool
    passed = (resp["status"] == 200 and "error" not in body
              and "'str' object" not in result_text
              and "has no attribute" not in result_text)
    if not passed:
        srv.errors += 1

    record(
        f"[{srv.name.upper()}] {tag}",
        passed,
        f"{elapsed:.1f}s  HTTP {resp['status']}  {result_text}",
    )
    return resp

# ---------------------------------------------------------------------------
# Phase 0: OAuth
# ---------------------------------------------------------------------------

def phase_oauth(http: requests.Session) -> dict[str, Srv]:
    hdr("═══ PHASE 0 — OAuth 2.0 + PKCE ═══")
    sessions: dict[str, Srv] = {}
    for name, port in SERVERS.items():
        info(f"Registering client for {name} (port {port}) …")
        reg = oauth_post(http, "/register",
                         {"redirect_uris": [REDIRECT_URI],
                          "client_name": f"stress-{name}"})
        record(f"register/{name}", reg["status"] == 201,
               reg["body"].get("client_id", str(reg["status"])))
        client_id     = reg["body"].get("client_id", "")
        client_secret = reg["body"].get("client_secret", "")

        verifier, challenge = pkce_pair()
        state = secrets.token_hex(8)
        qs = urllib.parse.urlencode({
            "response_type":         "code",
            "client_id":             client_id,
            "redirect_uri":          REDIRECT_URI,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
            "state":                 state,
        })
        r = http.get(f"{PROXY_BASE}/authorize?{qs}",
                     allow_redirects=False, timeout=10)
        location = r.headers.get("location", "")
        code = urllib.parse.parse_qs(
            urllib.parse.urlparse(location).query
        ).get("code", [""])[0]
        record(f"authorize/{name}", bool(code), "got code" if code else "no code")

        tok = oauth_post(http, "/token", {
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  REDIRECT_URI,
            "client_id":     client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
        })
        token = tok["body"].get("access_token", "")
        record(f"token/{name}", bool(token), token[:16] + "…" if token else "MISSING")

        srv = Srv(name=name, port=port, token=token)
        sessions[name] = srv
    return sessions

# ---------------------------------------------------------------------------
# Phase 1: Health baseline
# ---------------------------------------------------------------------------

def phase_baseline(http: requests.Session, sessions: dict[str, Srv]) -> None:
    hdr("═══ PHASE 1 — Health Baseline (initialize + tools/list) ═══")
    for name, srv in sessions.items():
        info(f"Initializing {name} …")
        r = mcp_post(http, srv.port, srv.token, "initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "stress-test", "version": "1"},
        })
        server_name   = r["body"].get("result", {}).get("serverInfo", {}).get("name", "?")
        session_leaked = bool(r.get("headers", {}).get("mcp-session-id"))
        record(f"[{name.upper()}] initialize",
               r["status"] == 200 and not session_leaked,
               f"server={server_name}  stateless={not session_leaked}")

        mcp_post(http, srv.port, srv.token, "notifications/initialized")

        tl = mcp_post(http, srv.port, srv.token, "tools/list")
        tools = tl["body"].get("result", {}).get("tools", [])
        srv.tools = [t["name"] for t in tools]
        record(f"[{name.upper()}] tools/list",
               len(srv.tools) > 0, f"{len(srv.tools)} tools")

# ---------------------------------------------------------------------------
# Phase 2: Format stress
# ---------------------------------------------------------------------------

FORMAT_CASES = [
    # (label_suffix, args_dict)
    # Tested tool: check_screening_due (clinical) — patient_age:int, sex:str, conditions:list
    ("normal ints",          {"patient_age": 54, "sex": "female", "conditions": ["hypertension"]}),
    ("age as string",        {"patient_age": "54", "sex": "female", "conditions": ["hypertension"]}),
    ("conditions as JSON str", {"patient_age": 54,  "sex": "female", "conditions": '["hypertension"]'}),
    ("extra unknown field",  {"patient_age": 54, "sex": "female", "conditions": [], "_claude_meta": "ignore"}),
    ("empty conditions",     {"patient_age": 54, "sex": "female", "conditions": []}),
    ("only required args",   {"patient_age": 54, "sex": "male",   "conditions": ["diabetes"]}),
]

VITAL_CASES = [
    # get_vital_trend (skills) — patient_id:str, metric_type:str, days:int
    ("normal",               {"patient_id": PATIENT_UUID, "metric_type": "systolic_bp", "days": 30}),
    ("days as string",       {"patient_id": PATIENT_UUID, "metric_type": "systolic_bp", "days": "30"}),
    ("no days (optional)",   {"patient_id": PATIENT_UUID, "metric_type": "diastolic_bp"}),
    ("metric_type alt",      {"patient_id": PATIENT_UUID, "metric_type": "heart_rate", "days": 60}),
    ("extra field",          {"patient_id": PATIENT_UUID, "metric_type": "weight",     "days": 90, "_source": "claude"}),
    ("long lookback",        {"patient_id": PATIENT_UUID, "metric_type": "bmi",        "days": 365}),
]

SEARCH_CASES = [
    # search_patient_data_extended (ingestion) — scope:list, elements:list[dict]
    ("normal dicts",
     {"patient_mrn": PATIENT_MRN,
      "search_scope": ["warehouse_full_history"],
      "data_elements": [{"element_type": "lab_result", "loinc_code": "2093-3", "lookback_days": 180}]}),
    ("elements as JSON strings",
     {"patient_mrn": PATIENT_MRN,
      "search_scope": ["warehouse_full_history"],
      "data_elements": ['{"element_type":"lab_result","loinc_code":"4548-4","lookback_days":90}']}),
    ("scope as single JSON string",
     {"patient_mrn": PATIENT_MRN,
      "search_scope": ['["warehouse_full_history","pharmacy_claims"]'],
      "data_elements": [{"element_type": "medication", "lookback_days": 365}]}),
    ("empty data_elements",
     {"patient_mrn": PATIENT_MRN,
      "search_scope": ["hie_network"],
      "data_elements": []}),
    ("multiple elements",
     {"patient_mrn": PATIENT_MRN,
      "search_scope": ["warehouse_full_history", "pharmacy_claims"],
      "data_elements": [
          {"element_type": "lab_result",  "loinc_code": "2093-3", "lookback_days": 180},
          {"element_type": "medication",  "loinc_code": "4548-4", "lookback_days": 365},
          {"element_type": "vital_signs", "lookback_days": 90},
      ]}),
    ("gap_id provided",
     {"patient_mrn": PATIENT_MRN,
      "search_scope": ["warehouse_full_history"],
      "data_elements": [{"element_type": "lab_result", "loinc_code": "2093-3", "lookback_days": 60}],
      "gap_id": "gap-stress-001"}),
]

PROVENANCE_CASES = [
    # verify_output_provenance — payload:str, strict_mode:bool
    ("normal bool",          {"payload": '{"summary":"test"}', "strict_mode": False}),
    ("bool as string false", {"payload": '{"summary":"test"}', "strict_mode": "false"}),
    ("bool as string true",  {"payload": '{"summary":"test"}', "strict_mode": "true"}),
    ("no strict_mode",       {"payload": '{"summary":"minimal"}'}),
    ("with patient_mrn",     {"payload": '{"summary":"full"}', "patient_mrn": PATIENT_MRN, "strict_mode": False}),
    ("long payload",         {"payload": json.dumps({"summary": "x" * 1000, "tags": list(range(50))}), "strict_mode": False}),
]

def phase_format_stress(http: requests.Session, sessions: dict[str, Srv]) -> None:
    hdr("═══ PHASE 2 — Format Stress (6 arg variants × 4 tools) ═══")
    clinical  = sessions["clinical"]
    skills    = sessions["skills"]
    ingestion = sessions["ingestion"]

    print(f"\n  {MAG}── check_screening_due (clinical) ──{RESET}")
    for suffix, args in FORMAT_CASES:
        tool_call(http, clinical, "check_screening_due", args,
                  label=f"check_screening_due · {suffix}")

    print(f"\n  {MAG}── get_vital_trend (skills) ──{RESET}")
    for suffix, args in VITAL_CASES:
        tool_call(http, skills, "get_vital_trend", args,
                  label=f"get_vital_trend · {suffix}")

    print(f"\n  {MAG}── search_patient_data_extended (ingestion) ──{RESET}")
    for suffix, args in SEARCH_CASES:
        tool_call(http, ingestion, "search_patient_data_extended", args,
                  label=f"search_patient_data_extended · {suffix}")

    print(f"\n  {MAG}── verify_output_provenance (all 3 servers) ──{RESET}")
    for suffix, args in PROVENANCE_CASES:
        tool_call(http, clinical,  "verify_output_provenance", args,
                  label=f"provenance/clinical · {suffix}")
        tool_call(http, skills,    "verify_output_provenance", args,
                  label=f"provenance/skills · {suffix}")
        tool_call(http, ingestion, "verify_output_provenance", args,
                  label=f"provenance/ingestion · {suffix}")

# ---------------------------------------------------------------------------
# Phase 4: Post-delay health check
# ---------------------------------------------------------------------------

def phase_post_delay_check(http: requests.Session,
                            sessions: dict[str, Srv]) -> None:
    hdr("═══ PHASE 4 — Post-Delay Health Check (no re-auth) ═══")
    probes = {
        "clinical":  ("get_data_source_status", {}),
        "skills":    ("get_current_session",     {}),
        "ingestion": ("detect_context_staleness", {
            "patient_mrn":      PATIENT_MRN,
            "context_elements": [{"element": "vitals", "loaded_at": "2026-04-01T00:00:00Z"}],
            "clinical_scenario": "annual_wellness",
        }),
    }
    for name, (tool, args) in probes.items():
        srv = sessions[name]
        tool_call(http, srv, tool, args, label=f"[{name.upper()}] post-delay/{tool}")

# ---------------------------------------------------------------------------
# Phase 5: Rapid-fire burst
# ---------------------------------------------------------------------------

BURST_CALLS: list[tuple[str, str, dict]] = [
    ("clinical",  "get_data_source_status",    {}),
    ("skills",    "get_current_session",        {}),
    ("clinical",  "get_pending_nudges",         {"patient_id": PATIENT_UUID}),
    ("skills",    "check_data_freshness",       {"patient_id": PATIENT_UUID}),
    ("ingestion", "detect_context_staleness",   {
        "patient_mrn": PATIENT_MRN,
        "context_elements": [{"element": "meds", "loaded_at": "2026-04-01T00:00:00Z"}],
        "clinical_scenario": "burst_test",
    }),
    ("clinical",  "get_patient_knowledge",      {"patient_id": PATIENT_UUID}),
    ("skills",    "get_sdoh_profile",           {"patient_id": PATIENT_UUID}),
    ("clinical",  "check_screening_due",        {"patient_age": 54, "sex": "female", "conditions": []}),
    ("skills",    "get_medication_adherence_rate", {"patient_id": PATIENT_UUID, "days": 30}),
    ("clinical",  "get_encounter_context",      {"patient_id": PATIENT_UUID}),
    ("skills",    "compute_obt_score",          {"patient_id": PATIENT_UUID}),
    ("clinical",  "get_time_since_last_contact",{"patient_id": PATIENT_UUID}),
    ("skills",    "get_vital_trend",            {"patient_id": PATIENT_UUID, "metric_type": "systolic_bp", "days": 7}),
    ("clinical",  "list_available_actions",     {"role": "provider", "patient_id": PATIENT_UUID}),
    ("skills",    "generate_previsit_brief",    {"patient_id": PATIENT_UUID}),
    ("ingestion", "verify_output_provenance",   {"payload": '{"burst":true}', "strict_mode": False}),
    ("clinical",  "verify_output_provenance",   {"payload": '{"burst":true}', "strict_mode": False}),
    ("skills",    "verify_output_provenance",   {"payload": '{"burst":true}', "strict_mode": False}),
    ("clinical",  "get_care_gap_ages",          {"patient_id": PATIENT_UUID}),
    ("skills",    "get_behavioral_screening_summary", {"patient_id": PATIENT_UUID}),
]

BURST_SPACING = 0.8   # seconds between burst calls — keeps us under 60 req/min

def phase_rapid_fire(http: requests.Session, sessions: dict[str, Srv]) -> None:
    hdr("═══ PHASE 5 — Rapid-Fire Burst (20 cross-server calls) ═══")
    info(f"Firing 20 calls across all 3 servers ({BURST_SPACING}s spacing, "
         f"simulating Claude's inter-tool pacing) …")
    t0 = time.time()
    for i, (server_name, tool, args) in enumerate(BURST_CALLS):
        if i > 0:
            time.sleep(BURST_SPACING)
        srv = sessions[server_name]
        tool_call(http, srv, tool, args,
                  label=f"burst/{server_name}/{tool}")
    elapsed = time.time() - t0
    record("burst/total-time", elapsed < 120,
           f"{elapsed:.1f}s for 20 calls ({elapsed / 20:.2f}s avg)")

# ---------------------------------------------------------------------------
# Phase 7: Edge-case inputs
# ---------------------------------------------------------------------------

def phase_edge_cases(http: requests.Session, sessions: dict[str, Srv]) -> None:
    hdr("═══ PHASE 7 — Edge-Case Inputs ═══")
    clinical  = sessions["clinical"]
    skills    = sessions["skills"]
    ingestion = sessions["ingestion"]

    # Unicode in string fields
    tool_call(http, clinical, "clinical_query", {
        "query": "Patient présente une douleur thoracique — что делать?",
        "role": "provider",
        "patient_context": f"mrn:{PATIENT_MRN}",
    }, label="edge/unicode-query")

    # Very long string
    tool_call(http, clinical, "verify_output_provenance", {
        "payload": "A" * 4000,
        "strict_mode": False,
    }, label="edge/very-long-payload")

    # Integer 0 / False-y values
    tool_call(http, skills, "get_vital_trend", {
        "patient_id": PATIENT_UUID,
        "metric_type": "systolic_bp",
        "days": 0,
    }, label="edge/days=0")

    # Boolean strict_mode as integer
    tool_call(http, ingestion, "verify_output_provenance", {
        "payload": '{"edge": 1}',
        "strict_mode": 0,
    }, label="edge/strict_mode=0")

    # Empty string for optional param
    tool_call(http, ingestion, "search_patient_data_extended", {
        "patient_mrn": PATIENT_MRN,
        "search_scope": ["warehouse_full_history"],
        "data_elements": [{"element_type": "lab_result", "loinc_code": "2093-3"}],
        "gap_id": "",
        "fhir_query_override": "",
    }, label="edge/empty-optional-strings")

    # Unknown patient MRN
    tool_call(http, ingestion, "detect_context_staleness", {
        "patient_mrn": "UNKNOWN-9999",
        "context_elements": [{"element": "vitals", "loaded_at": "2026-01-01T00:00:00Z"}],
        "clinical_scenario": "unknown_patient",
    }, label="edge/unknown-patient-mrn")

    # Nested JSON in string field
    tool_call(http, clinical, "verify_output_provenance", {
        "payload": json.dumps({"nested": {"deep": {"value": [1, 2, 3]}}}),
        "patient_mrn": PATIENT_MRN,
        "strict_mode": False,
    }, label="edge/nested-json-payload")

    # check_screening_due with many conditions
    tool_call(http, clinical, "check_screening_due", {
        "patient_age": 65,
        "sex": "female",
        "conditions": ["hypertension", "diabetes", "obesity", "depression",
                       "hyperlipidemia", "ckd", "hypothyroidism"],
    }, label="edge/many-conditions")

# ---------------------------------------------------------------------------
# Phase 8: Token still valid
# ---------------------------------------------------------------------------

def phase_token_valid(http: requests.Session, sessions: dict[str, Srv]) -> None:
    hdr("═══ PHASE 8 — Token Validity (DB-backed 24h TTL) ═══")
    for name, srv in sessions.items():
        resp = mcp_post(http, srv.port, srv.token, "tools/list")
        n = len(resp["body"].get("result", {}).get("tools", []))
        record(f"[{name.upper()}] token still valid",
               resp["status"] == 200 and n > 0,
               f"HTTP {resp['status']}  tools={n}")

# ---------------------------------------------------------------------------
# Countdown
# ---------------------------------------------------------------------------

def countdown(seconds: int, label: str) -> None:
    print()
    print(f"  {YELLOW}⏳ Waiting {seconds}s — {label}{RESET}")
    elapsed = 0
    chunk   = max(5, seconds // 20)
    while elapsed < seconds:
        step = min(chunk, seconds - elapsed)
        time.sleep(step)
        elapsed += step
        pct  = elapsed / seconds
        bar  = "█" * int(pct * 30)
        print(f"\r  {DIM}  {seconds - elapsed:3d}s remaining  [{bar:<30}]{RESET}",
              end="", flush=True)
    print(f"\r  {GREEN}✓ Delay complete — resuming{RESET}                              ")

# ---------------------------------------------------------------------------
# Summary + URLs
# ---------------------------------------------------------------------------

def print_summary(sessions: dict[str, Srv]) -> None:
    total_pass = len(RESULTS["pass"])
    total_fail = len(RESULTS["fail"])
    total      = total_pass + total_fail

    hdr("═══ SESSION SUMMARY ═══")
    for name, srv in sessions.items():
        icon = f"{GREEN}✓{RESET}" if srv.errors == 0 else f"{RED}✗{RESET}"
        print(f"  {icon} {name.upper():10s}  {srv.calls} calls  "
              f"{srv.errors} errors  {len(srv.tools)} tools")

    print()
    print(f"  {BOLD}Results: {GREEN}{total_pass} passed{RESET}  "
          f"{RED}{total_fail} failed{RESET}  of {total} checks{RESET}")

    if RESULTS["fail"]:
        print(f"\n  {RED}Failed checks:{RESET}")
        for item in RESULTS["fail"]:
            print(f"    {RED}• {item}{RESET}")

    print()
    if total_fail == 0:
        print(f"  {GREEN}{BOLD}ALL CHECKS PASSED{RESET}")
    else:
        print(f"  {RED}{BOLD}{total_fail} FAILURE(S) — see above{RESET}")
    print()


def print_urls(sessions: dict[str, Srv]) -> None:
    domain   = os.environ.get("REPLIT_DEV_DOMAIN", "")
    pub_base = f"https://{domain}" if domain else None
    loc_base = PROXY_BASE

    hdr("═══ MCP SERVER ENDPOINTS ═══")
    labels = {
        "clinical":  ("Clinical Intelligence", 8001),
        "skills":    ("Skills & Behavioral",   8002),
        "ingestion": ("Data Ingestion",         8003),
    }
    print(f"\n  {BOLD}MCP Proxy Endpoints{RESET}")
    for name, (label, port) in labels.items():
        srv    = sessions.get(name)
        icon   = f"{GREEN}●{RESET}" if (srv and srv.errors == 0) else f"{RED}●{RESET}"
        n      = len(srv.tools) if srv else "?"
        public = f"{pub_base}/api/mcp/{port}/mcp" if pub_base else None
        print(f"\n    {icon} {BOLD}{label}{RESET}  ({n} tools)")
        print(f"      Local  : {DIM}{loc_base}/api/mcp/{port}/mcp{RESET}")
        if public:
            print(f"      Public : {CYAN}{public}{RESET}")
    print()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global DELAY_LONG, DELAY_MEDIUM
    args  = sys.argv[1:]
    quick = "--quick" in args
    if quick:
        DELAY_LONG   = 8
        DELAY_MEDIUM = 5
    for i, a in enumerate(args):
        if a == "--long"   and i + 1 < len(args): DELAY_LONG   = int(args[i+1])
        if a == "--medium" and i + 1 < len(args): DELAY_MEDIUM = int(args[i+1])

    print(f"\n{BOLD}{'═'*70}{RESET}")
    print(f"{BOLD}  Ambient Patient Companion — MCP Health & Stress Test{RESET}")
    print(f"{BOLD}{'═'*70}{RESET}")
    print(f"  Proxy base  : {PROXY_BASE}")
    print(f"  Patient     : {PATIENT_MRN} / {PATIENT_UUID}")
    print(f"  Delays      : {DELAY_LONG}s (long) + {DELAY_MEDIUM}s (medium)"
          + ("  [quick mode]" if quick else ""))
    print(f"  Checks      : OAuth × 3, baseline × 3, format stress × 24, "
          f"post-delay × 3,\n"
          f"                burst × 21, edge cases × 8, token check × 3")

    http = requests.Session()
    http.verify = False
    sessions: dict[str, Srv] = {}

    try:
        sessions = phase_oauth(http)
        phase_baseline(http, sessions)
        phase_format_stress(http, sessions)

        countdown(DELAY_LONG,
                  "long pause — stateful servers would lose session here")

        phase_post_delay_check(http, sessions)
        phase_rapid_fire(http, sessions)

        countdown(DELAY_MEDIUM,
                  "medium pause — verifying continued stateless operation")

        phase_edge_cases(http, sessions)
        phase_token_valid(http, sessions)

    except KeyboardInterrupt:
        print(f"\n  {YELLOW}Interrupted{RESET}")
    except Exception as exc:
        print(f"\n  {RED}Fatal: {exc}{RESET}")
        import traceback; traceback.print_exc()

    print_summary(sessions)
    print_urls(sessions)
    return 1 if RESULTS["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())

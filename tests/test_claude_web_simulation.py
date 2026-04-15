"""
Claude Web / Agent Simulation Test
====================================
Simulates exactly how Claude Web (or any MCP client) interacts with the
Ambient Patient Companion MCP servers through the Next.js OAuth proxy.

What this tests:
  - Full OAuth 2.0 + PKCE flow per server (register → authorize → token)
  - MCP initialize handshake through the proxy
  - Tool discovery (tools/list) on all 3 servers
  - Multiple rounds of real tool calls with LONG delays between rounds
    (65 s and 45 s pauses simulate Replit proxy timeouts killing old SSE
     connections — verifies stateless mode holds up with no session errors)
  - Cross-server interleaving (Claude calls Clinical, then Skills, then
    Ingestion, then back to Clinical — just like a real deliberation loop)
  - Token validity across all delays (24 h TTL, so a single token set
    covers the whole session)

Servers under test (all via Next.js proxy at localhost:5000):
  8001 — Clinical MCP  (stateless, json_response)
  8002 — Skills MCP    (stateless, json_response)
  8003 — Ingestion MCP (stateless, json_response)

Demo patient:
  MRN  = MC-2025-4829
  UUID = a1b2c3d4-e5f6-7890-abcd-ef1234567890
"""

from __future__ import annotations

import hashlib
import base64
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
# Configuration
# ---------------------------------------------------------------------------

PROXY_BASE = "http://localhost:5000"
REDIRECT_URI = "http://127.0.0.1:9999/callback"   # must be https or localhost
PATIENT_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
PATIENT_MRN  = "MC-2025-4829"

SERVERS = {
    "clinical":  8001,
    "skills":    8002,
    "ingestion": 8003,
}

# Delay in seconds between conversation rounds — chosen to exceed typical
# Replit proxy idle timeouts (30-60 s) so a stateful server would lose its
# session but a stateless server must survive fine.
DELAY_ROUND_1_TO_2 = 65   # seconds  (override with --delay1 N)
DELAY_ROUND_2_TO_3 = 45   # seconds  (override with --delay2 N)

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"
DIM    = "\033[2m"

def ok(msg: str)    -> None: print(f"  {GREEN}✓{RESET} {msg}")
def fail(msg: str)  -> None: print(f"  {RED}✗{RESET} {msg}")
def info(msg: str)  -> None: print(f"  {CYAN}→{RESET} {msg}")
def warn(msg: str)  -> None: print(f"  {YELLOW}⚠{RESET} {msg}")
def hdr(msg: str)   -> None: print(f"\n{BOLD}{msg}{RESET}")
def dim(msg: str)   -> None: print(f"  {DIM}{msg}{RESET}")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class ServerSession:
    name:    str
    port:    int
    token:   str = ""
    tools:   list[str] = field(default_factory=list)
    errors:  int = 0
    calls:   int = 0

RESULTS: dict[str, list[str]] = {"pass": [], "fail": []}

def record(label: str, passed: bool, detail: str = "") -> None:
    if passed:
        RESULTS["pass"].append(label)
        ok(f"{label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    else:
        RESULTS["fail"].append(label)
        fail(f"{label}" + (f"  →  {RED}{detail}{RESET}" if detail else ""))

# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def pkce_pair() -> tuple[str, str]:
    verifier  = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge

# ---------------------------------------------------------------------------
# OAuth helpers (through Next.js proxy)
# ---------------------------------------------------------------------------

def oauth_register(session: requests.Session, name: str) -> dict:
    for attempt in range(6):
        r = session.post(
            f"{PROXY_BASE}/register",
            json={"redirect_uris": [REDIRECT_URI], "client_name": f"SimAgent-{name}"},
            timeout=15,
        )
        if r.status_code == 429:
            wait = 12 * (attempt + 1)
            warn(f"Rate limited on register/{name} — waiting {wait}s (attempt {attempt+1}/6)")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"register/{name} rate-limited after 6 attempts")

def oauth_get_token(session: requests.Session, client: dict) -> str:
    """Full PKCE authorize → token exchange, returns access_token string."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type":         "code",
        "client_id":             client["client_id"],
        "redirect_uri":          REDIRECT_URI,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
        "state":                 state,
        "scope":                 "mcp",
    }
    # Do NOT follow the redirect — we just need the code from Location header
    r = session.get(
        f"{PROXY_BASE}/authorize",
        params=params,
        allow_redirects=False,
        timeout=15,
    )
    assert r.status_code == 302, f"Expected 302, got {r.status_code}"
    location = r.headers["location"]
    parsed   = urllib.parse.urlparse(location)
    qs       = urllib.parse.parse_qs(parsed.query)
    code     = qs["code"][0]

    r2 = session.post(
        f"{PROXY_BASE}/token",
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  REDIRECT_URI,
            "code_verifier": verifier,
        },
        timeout=15,
    )
    r2.raise_for_status()
    return r2.json()["access_token"]

# ---------------------------------------------------------------------------
# MCP JSON-RPC helpers (through Next.js proxy)
# ---------------------------------------------------------------------------

_call_id = 0

def next_id() -> int:
    global _call_id
    _call_id += 1
    return _call_id

def mcp_post(
    session: requests.Session,
    port: int,
    token: str,
    method: str,
    params: dict | None = None,
    timeout: int = 30,
) -> dict:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id":      next_id(),
        "method":  method,
    }
    if params:
        payload["params"] = params

    r = session.post(
        f"{PROXY_BASE}/api/mcp/{port}/mcp",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept":        "application/json",
        },
        timeout=timeout,
    )
    return {"status": r.status_code, "body": r.json() if r.content else {}}

def mcp_notify(
    session: requests.Session,
    port: int,
    token: str,
    method: str,
    params: dict | None = None,
) -> int:
    """Send a JSON-RPC notification (no id → server returns 202)."""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params:
        payload["params"] = params
    r = session.post(
        f"{PROXY_BASE}/api/mcp/{port}/mcp",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept":        "application/json",
        },
        timeout=15,
    )
    return r.status_code

def call_tool(
    session: requests.Session,
    srv: ServerSession,
    tool: str,
    arguments: dict,
    label: str,
) -> dict | None:
    srv.calls += 1
    t0 = time.time()
    resp = mcp_post(
        session, srv.port, srv.token,
        "tools/call",
        {"name": tool, "arguments": arguments},
        timeout=45,
    )
    elapsed = time.time() - t0
    status  = resp["status"]
    body    = resp["body"]

    has_error    = "error" in body
    rpc_error    = body.get("error", {})
    is_session_error = (
        has_error and rpc_error.get("code") in (-32600, -32001)
        and "session" in str(rpc_error.get("message", "")).lower()
    )

    passed = status == 200 and not has_error
    record(
        f"[{srv.name.upper()}] tools/call {tool}",
        passed,
        f"{elapsed:.1f}s  HTTP {status}"
        + (f"  RPC error {rpc_error}" if has_error else ""),
    )

    if is_session_error:
        warn(f"SESSION-TERMINATED error on {srv.name}! stateless mode failed.")
        srv.errors += 1

    if passed:
        result = body.get("result", {})
        content = result.get("content", [])
        if content:
            text = content[0].get("text", "")
            snippet = text[:120].replace("\n", " ")
            dim(f"result: {snippet}{'…' if len(text) > 120 else ''}")
        return result

    srv.errors += 1
    return None

# ---------------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------------

def phase_oauth(http: requests.Session) -> dict[str, ServerSession]:
    hdr("═══ PHASE 0 — OAuth 2.0 + PKCE (one token per server) ═══")
    sessions: dict[str, ServerSession] = {}
    for name, port in SERVERS.items():
        info(f"Registering client for {name} (port {port}) …")
        client = oauth_register(http, name)
        record(f"register/{name}", "client_id" in client, client.get("client_id",""))

        info(f"Authorizing and exchanging token for {name} …")
        token = oauth_get_token(http, client)
        record(f"token/{name}", len(token) > 20, f"{token[:16]}…")

        sessions[name] = ServerSession(name=name, port=port, token=token)
    return sessions

def phase_init(http: requests.Session, sessions: dict[str, ServerSession]) -> None:
    hdr("═══ PHASE 1 — MCP Initialize + Tool Discovery ═══")
    for name, srv in sessions.items():
        info(f"Initializing {name} …")
        resp = mcp_post(
            http, srv.port, srv.token,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities":    {},
                "clientInfo":      {"name": "SimAgent", "version": "1.0"},
            },
        )
        server_name = (
            resp["body"].get("result", {})
                        .get("serverInfo", {})
                        .get("name", "?")
        )
        has_session = "mcp-session-id" in str(resp)   # should be absent
        record(
            f"[{name.upper()}] initialize",
            resp["status"] == 200 and not has_session,
            f"server={server_name}  session_id_leaked={has_session}",
        )

        # Notify initialized (Claude does this after every initialize)
        code = mcp_notify(http, srv.port, srv.token, "notifications/initialized")
        record(f"[{name.upper()}] notifications/initialized", code == 202, f"HTTP {code}")

        # List tools
        resp2 = mcp_post(http, srv.port, srv.token, "tools/list")
        tools = [t["name"] for t in resp2["body"].get("result", {}).get("tools", [])]
        srv.tools = tools
        record(
            f"[{name.upper()}] tools/list",
            len(tools) > 0,
            f"{len(tools)} tools found",
        )

def phase_round_1(http: requests.Session, sessions: dict[str, ServerSession]) -> None:
    hdr("═══ PHASE 2 — Round 1: First Agent Conversation Turn ═══")
    info("Agent has just received patient context and begins tool calls …")

    clinical  = sessions["clinical"]
    skills    = sessions["skills"]
    ingestion = sessions["ingestion"]

    # Clinical: identify patient
    call_tool(http, clinical, "get_synthetic_patient",
              {"mrn": PATIENT_MRN},
              "fetch demo patient")

    # Clinical: check overdue screenings
    call_tool(http, clinical, "check_screening_due",
              {"patient_age": 55, "sex": "female", "conditions": ["type_2_diabetes", "hypertension"]},
              "screening check")

    # Clinical: data source status
    call_tool(http, clinical, "get_data_source_status",
              {},
              "data source status")

    # Skills: check data freshness before generating content
    call_tool(http, skills, "check_data_freshness",
              {"patient_id": PATIENT_UUID},
              "data freshness")

    # Skills: vital trend
    call_tool(http, skills, "get_vital_trend",
              {"patient_id": PATIENT_UUID, "metric_type": "systolic_bp", "days": 30},
              "vital trend systolic BP")

    # Ingestion: context staleness check
    call_tool(http, ingestion, "detect_context_staleness",
              {
                  "patient_mrn": PATIENT_MRN,
                  "context_elements": [
                      {"element": "vitals",       "max_age_hours": 24},
                      {"element": "medications",  "max_age_hours": 72},
                      {"element": "labs",         "max_age_hours": 168},
                  ],
                  "clinical_scenario": "pre-encounter review",
              },
              "context staleness")

def phase_round_2(http: requests.Session, sessions: dict[str, ServerSession]) -> None:
    hdr("═══ PHASE 3 — Round 2: Agent Continues After Long Pause ═══")
    info("Agent resumes conversation (same tokens, no re-auth) …")

    clinical  = sessions["clinical"]
    skills    = sessions["skills"]
    ingestion = sessions["ingestion"]

    # Clinical: search guidelines (cross-server mix like Claude would do)
    call_tool(http, clinical, "get_data_source_status",
              {},
              "data source re-check post-delay")

    call_tool(http, clinical, "flag_drug_interaction",
              {"medications": ["metformin", "lisinopril"]},
              "drug interaction check")

    # Skills: SDOH profile
    call_tool(http, skills, "get_sdoh_profile",
              {"patient_id": PATIENT_UUID},
              "SDOH profile")

    # Skills: medication adherence
    call_tool(http, skills, "get_medication_adherence_rate",
              {"patient_id": PATIENT_UUID},
              "medication adherence")

    # Ingestion: search extended patient data
    call_tool(http, ingestion, "search_patient_data_extended",
              {
                  "patient_mrn":   PATIENT_MRN,
                  "search_scope":  ["vitals", "medications"],
                  "data_elements": ["blood_pressure", "heart_rate", "medication_list"],
              },
              "extended data search")

    # Cross-call: Clinical again to confirm stateless across alternation
    call_tool(http, clinical, "check_screening_due",
              {"patient_age": 55, "sex": "female", "conditions": ["type_2_diabetes"]},
              "screening re-check post-delay")

def phase_round_3(http: requests.Session, sessions: dict[str, ServerSession]) -> None:
    hdr("═══ PHASE 4 — Round 3: Final Deep Tool Calls ═══")
    info("Agent synthesises findings — heavy tool usage across all servers …")

    clinical  = sessions["clinical"]
    skills    = sessions["skills"]
    ingestion = sessions["ingestion"]

    # Skills: generate previsit brief (expensive, long)
    call_tool(http, skills, "generate_previsit_brief",
              {"patient_id": PATIENT_UUID},
              "previsit brief generation",)

    # Skills: OBT score
    call_tool(http, skills, "compute_obt_score",
              {"patient_id": PATIENT_UUID},
              "OBT score")

    # Clinical: pending nudges
    call_tool(http, clinical, "get_pending_nudges",
              {"patient_id": PATIENT_UUID},
              "pending nudges")

    # Clinical: knowledge graph
    call_tool(http, clinical, "get_patient_knowledge",
              {"patient_id": PATIENT_UUID},
              "patient knowledge")

    # Ingestion: provenance verify (all 3 servers export this tool)
    _provenance_payload = json.dumps({
        "patient_mrn": PATIENT_MRN,
        "output_type": "clinical_summary",
        "generated_by": "SimAgent",
        "content": "Patient Maria Chen — deliberation complete",
    })

    call_tool(http, ingestion, "verify_output_provenance",
              {"payload": _provenance_payload, "patient_mrn": PATIENT_MRN},
              "provenance verify (ingestion)")

    call_tool(http, skills, "verify_output_provenance",
              {"payload": _provenance_payload, "patient_mrn": PATIENT_MRN},
              "provenance verify (skills)")

    call_tool(http, clinical, "verify_output_provenance",
              {"payload": _provenance_payload, "patient_mrn": PATIENT_MRN},
              "provenance verify (clinical)")

# ---------------------------------------------------------------------------
# Token refresh check (simulate agent re-using same token after 24+ hours)
# We don't actually wait 24 h — we just verify the token is still valid.
# ---------------------------------------------------------------------------

def phase_token_still_valid(
    http: requests.Session, sessions: dict[str, ServerSession]
) -> None:
    hdr("═══ PHASE 5 — Verify All Tokens Still Valid (DB-backed TTL) ═══")
    for name, srv in sessions.items():
        resp = mcp_post(http, srv.port, srv.token, "tools/list")
        tools_count = len(resp["body"].get("result", {}).get("tools", []))
        record(
            f"[{name.upper()}] token still valid + tools/list",
            resp["status"] == 200 and tools_count > 0,
            f"HTTP {resp['status']}  tools={tools_count}",
        )

# ---------------------------------------------------------------------------
# Countdown display
# ---------------------------------------------------------------------------

def countdown(seconds: int, label: str) -> None:
    print()
    print(f"  {YELLOW}⏳ Waiting {seconds}s — {label}{RESET}")
    for remaining in range(seconds, 0, -5):
        bar = "█" * (20 - remaining // (seconds // 20 + 1))
        print(f"\r  {DIM}  {remaining:3d}s remaining  [{bar:<20}]{RESET}", end="", flush=True)
        time.sleep(min(5, remaining))
    print(f"\r  {GREEN}✓ Delay complete — resuming{RESET}                          ")

# ---------------------------------------------------------------------------
# URL display
# ---------------------------------------------------------------------------

def print_urls(sessions: dict[str, ServerSession]) -> None:
    domain   = os.environ.get("REPLIT_DEV_DOMAIN", "")
    pub_base = f"https://{domain}" if domain else None
    loc_base = PROXY_BASE

    hdr("═══ MCP SERVER ENDPOINTS ═══")

    print(f"\n  {BOLD}OAuth Discovery{RESET}")
    for path in [
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/register",
        "/authorize",
        "/token",
    ]:
        if pub_base:
            print(f"    {CYAN}{pub_base}{path}{RESET}")
        else:
            print(f"    {CYAN}{loc_base}{path}{RESET}")

    print(f"\n  {BOLD}MCP Proxy Endpoints  (pass to Claude Web as the MCP URL){RESET}")

    server_labels = {
        "clinical":  ("Clinical Intelligence", 8001, 44),
        "skills":    ("Skills & Behavioral",   8002, 40),
        "ingestion": ("Data Ingestion",         8003,  6),
    }

    for name, (label, port, _) in server_labels.items():
        srv      = sessions.get(name)
        n_tools  = len(srv.tools) if srv else "?"
        local    = f"{loc_base}/api/mcp/{port}/mcp"
        public   = f"{pub_base}/api/mcp/{port}/mcp" if pub_base else None

        status_icon = f"{GREEN}●{RESET}" if (srv and srv.errors == 0) else f"{RED}●{RESET}"
        print(f"\n    {status_icon} {BOLD}{label}{RESET}  ({n_tools} tools)")
        print(f"      Local  : {DIM}{local}{RESET}")
        if public:
            print(f"      Public : {CYAN}{public}{RESET}")

    print(f"\n  {BOLD}Claude Web — Add MCP Server{RESET}")
    print(f"  Configure each URL above in claude.ai → Settings → Integrations → Add MCP Server.")
    if pub_base:
        print(f"  OAuth discovery is automatic via {CYAN}{pub_base}/.well-known/oauth-authorization-server{RESET}")
    print()

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(sessions: dict[str, ServerSession]) -> None:
    total_pass = len(RESULTS["pass"])
    total_fail = len(RESULTS["fail"])
    total      = total_pass + total_fail

    hdr("═══ SESSION SUMMARY ═══")
    for name, srv in sessions.items():
        icon = GREEN + "✓" + RESET if srv.errors == 0 else RED + "✗" + RESET
        print(f"  {icon} {name.upper():10s}  {srv.calls} calls  {srv.errors} errors  "
              f"{len(srv.tools)} tools discovered")

    print()
    print(f"  {BOLD}Results: {GREEN}{total_pass} passed{RESET}  {RED}{total_fail} failed{RESET}  "
          f"of {total} checks{RESET}")

    if RESULTS["fail"]:
        print(f"\n  {RED}Failed checks:{RESET}")
        for f_item in RESULTS["fail"]:
            print(f"    {RED}• {f_item}{RESET}")

    print()
    if total_fail == 0:
        print(f"  {GREEN}{BOLD}ALL CHECKS PASSED — stateless MCP + OAuth working correctly{RESET}")
    else:
        print(f"  {RED}{BOLD}FAILURES DETECTED — see above{RESET}")
    print()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global DELAY_ROUND_1_TO_2, DELAY_ROUND_2_TO_3

    # ── CLI args ─────────────────────────────────────────────────────────────
    args = sys.argv[1:]
    quick = "--quick" in args
    if quick:
        DELAY_ROUND_1_TO_2 = 8
        DELAY_ROUND_2_TO_3 = 5
    for i, a in enumerate(args):
        if a == "--delay1" and i + 1 < len(args):
            DELAY_ROUND_1_TO_2 = int(args[i + 1])
        if a == "--delay2" and i + 1 < len(args):
            DELAY_ROUND_2_TO_3 = int(args[i + 1])

    print(f"\n{BOLD}{'═'*68}{RESET}")
    print(f"{BOLD}  Ambient Patient Companion — Claude Web Agent Simulation Test{RESET}")
    print(f"{BOLD}{'═'*68}{RESET}")
    print(f"  Proxy base : {PROXY_BASE}")
    print(f"  Patient    : {PATIENT_MRN} / {PATIENT_UUID}")
    print(f"  Delays     : {DELAY_ROUND_1_TO_2}s then {DELAY_ROUND_2_TO_3}s between rounds"
          + ("  (quick mode)" if quick else ""))
    print(f"  Servers    : {list(SERVERS.keys())}")

    http = requests.Session()
    http.verify = False   # local dev, self-signed

    try:
        # ── Phase 0: OAuth ──────────────────────────────────────────────────
        sessions = phase_oauth(http)

        # ── Phase 1: Init + discovery ────────────────────────────────────────
        phase_init(http, sessions)

        # ── Phase 2: First round of tool calls ──────────────────────────────
        phase_round_1(http, sessions)

        # ── Long delay (simulates Replit proxy killing SSE sessions) ─────────
        countdown(DELAY_ROUND_1_TO_2,
                  "simulating Replit proxy timeout — stateful servers would lose session here")

        # ── Phase 3: Second round — same tokens, no re-auth ──────────────────
        phase_round_2(http, sessions)

        # ── Medium delay ─────────────────────────────────────────────────────
        countdown(DELAY_ROUND_2_TO_3,
                  "second pause — verifying continued stateless operation")

        # ── Phase 4: Third round ─────────────────────────────────────────────
        phase_round_3(http, sessions)

        # ── Phase 5: Confirm tokens still DB-valid ───────────────────────────
        phase_token_still_valid(http, sessions)

    except KeyboardInterrupt:
        print(f"\n  {YELLOW}Interrupted by user{RESET}")
    except Exception as exc:
        print(f"\n  {RED}Unexpected error: {exc}{RESET}")
        import traceback; traceback.print_exc()

    _sess = sessions if "sessions" in dir() else {}  # type: ignore[possibly-undefined]
    print_summary(_sess)
    print_urls(_sess)
    return 1 if RESULTS["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())

"""FastMCP server entry point for the Ambient Patient Companion."""

import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Make shared/ importable — mcp-server/ is one level below the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastmcp import FastMCP
from skills import load_skills
from starlette.requests import Request
from starlette.responses import JSONResponse

from db.connection import get_pool
from shared.audit_middleware import AuditMiddleware
from runtime.agent_runtime import get_runtime

# ── Agent runtime — autonomous background watchers ────────────────────────────
# Uses the module-level singleton so any skill module that exports
# register_watchers(runtime) can declare its own background tasks — the same
# instance is passed to load_skills() below.
#
# All watchers are now registered by their respective skill files via the
# register_watchers(runtime) hook in load_skills():
#   • checkin_atom_watcher  (every 5 min)  — skills/behavioral_atoms.py
#   • crisis_scan_watcher   (every 60 min) — skills/crisis_escalation.py
#   • care_gap_watcher      (every 24 h)   — skills/care_gap.py
runtime = get_runtime()

mcp = FastMCP(
    "ambient-skills-companion",
    instructions=(
        "Ambient Skills Companion — specialized clinical skills and knowledge tools "
        "including motivational interviewing, health literacy assessment, care gap "
        "analysis, OBT scoring, patient education, call history auditing, and "
        "multi-domain clinical reasoning. Use these tools to enhance patient "
        "engagement and apply evidence-based clinical skills."
    ),
    lifespan=runtime.lifespan,
)


@mcp.custom_route("/health", methods=["GET"])
async def rest_health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "server": "ambient-skills-companion", "version": "1.0.0"})


@mcp.custom_route("/api/agent-runtime/status", methods=["GET"])
async def agent_runtime_status(request: Request) -> JSONResponse:
    """Return health snapshot for all registered autonomous watchers.

    Shape::

        {
          "watcher_count": 3,
          "watchers": [
            {
              "name": "checkin_atom_watcher",
              "interval_seconds": 300,
              "run_count": 12,
              "last_run": "2025-05-05T14:30:00+00:00",
              "last_error": null,
              "healthy": true
            },
            ...
          ]
        }
    """
    return JSONResponse(runtime.status())


# ── REST wrappers for behavioral tools ───────────────────────────────────────
# Mirror of the MCP tool implementations exposed as plain POST endpoints so
# that callers using REST-style paths (e.g. /mcp-skills/tools/<name>) work
# correctly after the next.config.ts pass-through rewrite.


@mcp.custom_route("/tools/get_behavioral_atom_pressure", methods=["POST"])
async def rest_get_behavioral_atom_pressure(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        patient_id = body.get("patient_id", "")
        signal_types = body.get("signal_types", None)
        from skills.atom_vector_search import get_atom_pressure_for_patient
        pool = await get_pool()
        pressure = await get_atom_pressure_for_patient(pool, patient_id, signal_types)
        return JSONResponse({"patient_id": patient_id, "pressure": pressure})
    except Exception as exc:
        logger.warning("REST get_behavioral_atom_pressure error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/tools/run_behavioral_gap_detection", methods=["POST"])
async def rest_run_behavioral_gap_detection(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        patient_id = body.get("patient_id", "")
        from skills.behavioral_gap_detector import run_gap_detector_for_patient
        pool = await get_pool()
        gaps = await run_gap_detector_for_patient(pool, patient_id)
        return JSONResponse({
            "patient_id": patient_id,
            "gaps_detected": len(gaps),
            "gaps": gaps,
        })
    except Exception as exc:
        logger.warning("REST run_behavioral_gap_detection error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/tools/get_behavioral_screening_summary", methods=["POST"])
async def rest_get_behavioral_screening_summary(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        patient_id = body.get("patient_id", "")
        from skills.screening_registry import DOMAINS
        pool = await get_pool()
        async with pool.acquire() as conn:
            screening_rows = await conn.fetch(
                """
                SELECT domain,
                       MAX(administered_at) AS last_screened,
                       COUNT(*) AS screening_count
                FROM behavioral_screenings
                WHERE patient_id = $1::uuid
                GROUP BY domain
                """,
                patient_id,
            )
            gap_rows = await conn.fetch(
                """
                SELECT domain, gap_type, temporal_confidence, pressure_score
                FROM behavioral_screening_gaps
                WHERE patient_id = $1::uuid
                  AND status = 'open'
                """,
                patient_id,
            )
        screened = {r["domain"]: dict(r) for r in screening_rows}
        gaps = {r["domain"]: dict(r) for r in gap_rows}
        domain_summary: dict = {}
        for key, label in DOMAINS.items():
            s = screened.get(key)
            g = gaps.get(key)
            last_screened = None
            if s and s.get("last_screened"):
                last_screened = s["last_screened"].isoformat()
            domain_summary[key] = {
                "label": label,
                "screened": s is not None,
                "last_screened": last_screened,
                "screening_count": s["screening_count"] if s else 0,
                "has_open_gap": g is not None,
                "gap_type": g["gap_type"] if g else None,
                "temporal_confidence": g["temporal_confidence"] if g else None,
                "pressure_score": float(g["pressure_score"]) if g and g["pressure_score"] else None,
            }
        return JSONResponse({
            "patient_id": patient_id,
            "domains_screened": len(screened),
            "domains_with_gaps": len(gaps),
            "domain_summary": domain_summary,
        })
    except Exception as exc:
        logger.warning("REST get_behavioral_screening_summary error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


# Auto-discover and register all skill tools.
# Passing runtime lets any skill that exports register_watchers(runtime)
# declare its own background watchers without editing watchers.py.
load_skills(mcp, runtime=runtime)

# Audit every tool call — records inputs, outputs, timing and session to mcp_call_log
mcp.add_middleware(AuditMiddleware("skills", get_pool))

if __name__ == "__main__":
    import os
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8002"))
    if transport == "streamable-http":
        mcp.run(transport="streamable-http", host=host, port=port, json_response=True, stateless=True)
    else:
        mcp.run(transport="stdio")

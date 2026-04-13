"""
TieredContextLoader — builds deliberation context in priority order.

Tier 1 is always loaded (~1,500 chars). Tiers 2 and 3 are loaded on demand.
The loader enforces a character budget to prevent context overflow.

Table mapping (actual Replit PostgreSQL schema):
  biometric_readings   — labs + vitals (metric_type, value, unit, measured_at, is_abnormal)
  clinical_events      — encounters (event_type, event_date, description, source_system)
  patient_conditions   — conditions (code, display, onset_date, clinical_status)
  patient_medications  — medications (code, display, status, authored_on)
  clinical_notes       — extracted note text (note_type, note_text, note_date, author, binary_id)
  media_references     — non-text assets (resource_type, content_type, doc_type, reference_url, doc_date)
  ingestion_plans      — data inventory (resource_type, insights_summary, rows_written, status)
"""

import json
import logging
import re
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

# Recognizes canonical 8-4-4-4-12 UUIDs. Used to guard against non-UUID
# values (e.g. a raw date string "2025-06-26") being passed to asyncpg
# as the $2::uuid parameter for clinical_events.id lookups — which would
# crash the deliberation pipeline. See BUG 1.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Hard character budget per tier
TIER1_BUDGET = 2_000    # ~500 tokens — always fits, always safe
TIER2_BUDGET = 6_000    # ~1,500 tokens — trend data on request
TIER3_BUDGET = 4_000    # ~1,000 tokens — specific notes on demand
TOTAL_BUDGET = 11_000   # ~2,750 tokens — well below the crash zone at 16,190


try:
    from ingestion.adapters.healthex.content_router import sanitize_for_context
except ImportError:
    def sanitize_for_context(value) -> str:
        if value is None:
            return ""
        try:
            return json.loads(json.dumps(str(value)))
        except Exception:
            return str(value).encode("ascii", errors="replace").decode("ascii")


def sanitize(value) -> str:
    """Round-trip sanitize any value for safe JSON embedding."""
    return sanitize_for_context(value)


class TieredContextLoader:

    def __init__(self, db_pool, patient_id: str, internal_id=None):
        """
        Args:
            db_pool: asyncpg connection pool
            patient_id: MRN string or UUID for patient lookup
            internal_id: pre-resolved internal UUID (skips lookup if provided)
        """
        self.db_pool = db_pool
        self.patient_id = patient_id
        self._internal_id = internal_id
        self._chars_used = 0
        self._loaded_tiers: set[int] = set()

    async def _resolve_internal_id(self, conn) -> None:
        """Resolve patient_id (MRN or UUID) to internal UUID if not already set."""
        if self._internal_id is not None:
            return

        import re
        _UUID_RE = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            re.IGNORECASE,
        )

        row = await conn.fetchrow(
            "SELECT id FROM patients WHERE mrn = $1", self.patient_id
        )
        if row is None and _UUID_RE.match(self.patient_id):
            row = await conn.fetchrow(
                "SELECT id FROM patients WHERE id = $1::uuid", self.patient_id
            )
        if row is None:
            row = await conn.fetchrow(
                "SELECT id FROM patients WHERE mrn LIKE $1",
                f"%{self.patient_id}%",
            )
        if row is None:
            raise ValueError(
                f"Patient '{self.patient_id}' not found in patients table"
            )
        self._internal_id = row["id"]

    # ── Tier 1 ───────────────────────────────────────────────────────────────

    async def load_tier1(self) -> dict:
        """
        Always-loaded critical snapshot. ~1,500 chars max.
        Returns a compact dict safe to json.dumps() directly.
        """
        if 1 in self._loaded_tiers:
            return {}

        async with self.db_pool.acquire() as conn:
            await self._resolve_internal_id(conn)
            pid = self._internal_id
            ctx: dict = {}

            # Active conditions — display + onset dates only
            conditions = await conn.fetch(
                """SELECT display, onset_date, clinical_status
                   FROM patient_conditions
                   WHERE patient_id = $1
                     AND (clinical_status IS NULL OR clinical_status != 'inactive')
                   ORDER BY onset_date DESC NULLS LAST
                   LIMIT 10""",
                pid,
            )
            ctx["active_conditions"] = [
                {
                    "name": sanitize(c["display"]),
                    "since": str(c["onset_date"] or ""),
                }
                for c in conditions
            ]

            # Most recent value per distinct lab/metric type
            labs = await conn.fetch(
                """SELECT DISTINCT ON (metric_type)
                       metric_type, value, unit, is_abnormal, measured_at
                   FROM biometric_readings
                   WHERE patient_id = $1
                   ORDER BY metric_type, measured_at DESC
                   LIMIT 20""",
                pid,
            )
            ctx["recent_labs"] = [
                {
                    "test": sanitize(r["metric_type"]),
                    "value": str(r["value"]) if r["value"] is not None else "",
                    "unit": sanitize(r.get("unit", "")),
                    "flag": "abnormal" if r.get("is_abnormal") else "",
                    "date": r["measured_at"].isoformat() if r.get("measured_at") else "",
                }
                for r in labs
            ]

            # Last encounter (clinical_events)
            last_enc = await conn.fetchrow(
                """SELECT event_date, event_type, description, source_system
                   FROM clinical_events
                   WHERE patient_id = $1
                   ORDER BY event_date DESC NULLS LAST
                   LIMIT 1""",
                pid,
            )
            if last_enc:
                enc_date = last_enc["event_date"]
                if enc_date:
                    enc_d = enc_date.date() if hasattr(enc_date, "date") else enc_date
                    gap_days = (date.today() - enc_d).days
                else:
                    gap_days = None
                ctx["last_encounter"] = {
                    "date": enc_date.isoformat() if enc_date else "",
                    "type": sanitize(last_enc.get("event_type", "")),
                    "description": sanitize(last_enc.get("description", "")),
                    "gap_days": gap_days,
                }

            # Medications — display names only (compact for tier 1)
            meds = await conn.fetch(
                """SELECT display, status
                   FROM patient_medications
                   WHERE patient_id = $1
                     AND (status IS NULL OR status = 'active')
                   LIMIT 10""",
                pid,
            )
            ctx["active_medications"] = [
                sanitize(m["display"]) for m in meds
            ]
            if not ctx["active_medications"]:
                ctx["active_medications"] = ["none documented"]

            # Ingestion plan summaries (data inventory)
            try:
                plans = await conn.fetch(
                    """SELECT resource_type, insights_summary, rows_written, status
                       FROM ingestion_plans
                       WHERE patient_id = $1 AND status = 'complete'
                       ORDER BY planned_at DESC LIMIT 10""",
                    pid,
                )
                ctx["data_inventory"] = [
                    {
                        "type": p["resource_type"],
                        "summary": sanitize(p.get("insights_summary", "")),
                        "rows": p.get("rows_written", 0),
                    }
                    for p in plans
                ]
            except Exception:
                ctx["data_inventory"] = []

            # Available media (reference only — no content)
            try:
                media = await conn.fetch(
                    """SELECT resource_type, content_type, doc_type, reference_url, doc_date
                       FROM media_references
                       WHERE patient_id = $1
                       ORDER BY doc_date DESC NULLS LAST LIMIT 5""",
                    pid,
                )
                ctx["available_media"] = [
                    {
                        "type": sanitize(m.get("doc_type") or m.get("content_type", "")),
                        "ref": sanitize(m.get("reference_url", "")),
                        "date": m["doc_date"].isoformat() if m.get("doc_date") else "",
                    }
                    for m in media
                ] if media else []
            except Exception:
                ctx["available_media"] = []

        serialized = json.dumps(ctx)
        self._chars_used += len(serialized)
        self._loaded_tiers.add(1)
        log.info("Tier 1 loaded: %d chars", len(serialized))
        return ctx

    # ── Tier 2 ───────────────────────────────────────────────────────────────

    async def load_tier2(self, requested_tests: list[str] | None = None) -> dict:
        """
        Trend data — loaded when agent signals gaps. ~6,000 chars max.
        requested_tests: specific metric_type names the agent flagged, or None for all.
        """
        if 2 in self._loaded_tiers:
            return {}
        if self._chars_used + TIER2_BUDGET > TOTAL_BUDGET:
            log.warning("Tier 2 skipped — budget exhausted")
            return {}

        async with self.db_pool.acquire() as conn:
            await self._resolve_internal_id(conn)
            pid = self._internal_id
            ctx: dict = {}
            budget_remaining = TIER2_BUDGET

            # Full lab/biometric history for flagged tests only (not all)
            if requested_tests:
                placeholders = ", ".join(
                    f"${i + 2}" for i in range(len(requested_tests))
                )
                labs_history = await conn.fetch(
                    f"""SELECT metric_type, value, unit, is_abnormal, measured_at
                        FROM biometric_readings
                        WHERE patient_id = $1
                          AND LOWER(metric_type) IN ({placeholders})
                        ORDER BY metric_type, measured_at DESC
                        LIMIT 50""",
                    pid,
                    *[t.lower() for t in requested_tests],
                )
            else:
                labs_history = await conn.fetch(
                    """SELECT metric_type, value, unit, is_abnormal, measured_at
                       FROM biometric_readings
                       WHERE patient_id = $1
                       ORDER BY measured_at DESC LIMIT 30""",
                    pid,
                )

            ctx["lab_history"] = [
                {
                    "test": sanitize(r["metric_type"]),
                    "value": str(r["value"]) if r["value"] is not None else "",
                    "unit": sanitize(r.get("unit", "")),
                    "flag": "abnormal" if r.get("is_abnormal") else "",
                    "date": r["measured_at"].isoformat() if r.get("measured_at") else "",
                }
                for r in labs_history
            ]

            # Encounters last 2 years (clinical_events)
            two_years_ago = datetime.utcnow() - timedelta(days=730)
            encounters = await conn.fetch(
                """SELECT event_date, event_type, description, source_system
                   FROM clinical_events
                   WHERE patient_id = $1 AND event_date >= $2
                   ORDER BY event_date DESC""",
                pid,
                two_years_ago,
            )
            ctx["recent_encounters"] = [
                {
                    "date": e["event_date"].isoformat() if e.get("event_date") else "",
                    "type": sanitize(e.get("event_type", "")),
                    "description": sanitize(e.get("description", "")),
                    "source": sanitize(e.get("source_system", "")),
                }
                for e in encounters
            ]

            # Condition timeline including inactive
            all_conditions = await conn.fetch(
                """SELECT display, clinical_status, onset_date, code
                   FROM patient_conditions
                   WHERE patient_id = $1
                   ORDER BY onset_date DESC NULLS LAST""",
                pid,
            )
            ctx["condition_history"] = [
                {
                    "name": sanitize(c["display"]),
                    "status": sanitize(c.get("clinical_status", "")),
                    "onset": str(c["onset_date"] or ""),
                    "code": sanitize(c.get("code", "")),
                }
                for c in all_conditions
            ]

        serialized = json.dumps(ctx)
        # Enforce budget by trimming lab_history if needed
        if len(serialized) > budget_remaining:
            while len(json.dumps(ctx)) > budget_remaining and ctx["lab_history"]:
                ctx["lab_history"].pop()
            serialized = json.dumps(ctx)

        self._chars_used += len(serialized)
        self._loaded_tiers.add(2)
        log.info("Tier 2 loaded: %d chars, total: %d", len(serialized), self._chars_used)
        return ctx

    # ── Tier 3 — on-demand by resource_id ────────────────────────────────────

    async def load_on_demand(self, data_request: dict) -> dict:
        """
        Fetch a specific record on demand. Called once per agent data_request.
        Each fetch is budget-capped at TIER3_BUDGET / 3.

        data_request format:
        {
            "type": "clinical_note" | "lab_trend" | "encounter_detail" | "imaging_report",
            "resource_id": "fyEZI5WFE3...",   # optional — specific record
            "test": "HbA1c",                  # for lab_trend
            "reason": "agent's stated reason"
        }
        """
        req_type = data_request.get("type", "")
        resource_id = data_request.get("resource_id", "")
        reason = data_request.get("reason", "")

        per_request_budget = TIER3_BUDGET // 3  # max 3 on-demand fetches per round

        if self._chars_used >= TOTAL_BUDGET:
            log.warning("on-demand fetch skipped (budget exhausted): %s", req_type)
            return {}

        ctx: dict = {}

        async with self.db_pool.acquire() as conn:
            await self._resolve_internal_id(conn)
            pid = self._internal_id

            if req_type == "clinical_note":
                if resource_id:
                    note = await conn.fetchrow(
                        """SELECT note_type, note_text, note_date, author
                           FROM clinical_notes
                           WHERE patient_id = $1 AND binary_id = $2""",
                        pid,
                        resource_id,
                    )
                else:
                    note = await conn.fetchrow(
                        """SELECT note_type, note_text, note_date, author
                           FROM clinical_notes
                           WHERE patient_id = $1
                           ORDER BY note_date DESC NULLS LAST LIMIT 1""",
                        pid,
                    )
                if note:
                    note_text = sanitize(note.get("note_text", ""))
                    available = per_request_budget - 200
                    if len(note_text) > available:
                        note_text = note_text[:available] + "...[truncated]"
                    key = f"clinical_note_{resource_id or 'latest'}"
                    ctx[key] = {
                        "type": sanitize(note.get("note_type", "")),
                        "text": note_text,
                        "date": note["note_date"].isoformat() if note.get("note_date") else "",
                        "author": sanitize(note.get("author", "")),
                        "reason_fetched": sanitize(reason),
                    }

            elif req_type == "lab_trend":
                test_name = data_request.get("test", "")
                if test_name:
                    rows = await conn.fetch(
                        """SELECT value, unit, is_abnormal, measured_at
                           FROM biometric_readings
                           WHERE patient_id = $1
                             AND LOWER(metric_type) = LOWER($2)
                           ORDER BY measured_at DESC LIMIT 15""",
                        pid,
                        test_name,
                    )
                    ctx[f"lab_trend_{sanitize(test_name)}"] = [
                        {
                            "value": str(r["value"]) if r["value"] is not None else "",
                            "unit": sanitize(r.get("unit", "")),
                            "flag": "abnormal" if r.get("is_abnormal") else "",
                            "date": r["measured_at"].isoformat() if r.get("measured_at") else "",
                        }
                        for r in rows
                    ]

            elif req_type == "encounter_detail":
                enc_id = resource_id
                if enc_id and not _UUID_RE.match(str(enc_id)):
                    log.warning(
                        "Skipping encounter_detail request: resource_id "
                        "%r is not a UUID (likely a date string leaked "
                        "from ingest). Upstream fix: transfer_planner.py",
                        enc_id,
                    )
                    enc_id = None
                if enc_id:
                    enc = await conn.fetchrow(
                        """SELECT event_date, event_type, description, source_system
                           FROM clinical_events
                           WHERE patient_id = $1 AND id = $2::uuid""",
                        pid,
                        enc_id,
                    )
                    if enc:
                        ctx[f"encounter_{enc_id}"] = {
                            "date": enc["event_date"].isoformat() if enc.get("event_date") else "",
                            "type": sanitize(enc.get("event_type", "")),
                            "description": sanitize(enc.get("description", "")),
                            "source": sanitize(enc.get("source_system", "")),
                        }

            elif req_type == "imaging_report":
                if resource_id:
                    report = await conn.fetchrow(
                        """SELECT note_type, note_text, note_date
                           FROM clinical_notes
                           WHERE patient_id = $1 AND binary_id = $2""",
                        pid,
                        resource_id,
                    )
                else:
                    report = await conn.fetchrow(
                        """SELECT note_type, note_text, note_date
                           FROM clinical_notes
                           WHERE patient_id = $1
                             AND (note_type ILIKE '%impression%'
                                  OR note_type ILIKE '%imaging%'
                                  OR note_type ILIKE '%radiology%')
                           ORDER BY note_date DESC NULLS LAST LIMIT 1""",
                        pid,
                    )
                if report:
                    report_text = sanitize(report.get("note_text", ""))
                    if len(report_text) > per_request_budget:
                        report_text = report_text[:per_request_budget] + "...[truncated]"
                    ctx[f"imaging_{resource_id or 'latest'}"] = {
                        "type": sanitize(report.get("note_type", "")),
                        "text": report_text,
                        "date": report["note_date"].isoformat() if report.get("note_date") else "",
                    }

        serialized = json.dumps(ctx)
        self._chars_used += len(serialized)
        log.info("On-demand fetch [%s]: %d chars, total: %d",
                 req_type, len(serialized), self._chars_used)
        return ctx

    def context_summary(self) -> dict:
        return {
            "chars_used": self._chars_used,
            "chars_budget": TOTAL_BUDGET,
            "pct_used": round(self._chars_used / TOTAL_BUDGET * 100, 1) if TOTAL_BUDGET else 0,
            "tiers_loaded": sorted(self._loaded_tiers),
        }

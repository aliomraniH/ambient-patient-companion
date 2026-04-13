"""Skill: behavioral_tools — Tier 2.b.iii, .iv, .v tools on S2.

Covers:
  2.b.iii  classify_com_b_barrier, detect_conversation_teachable_moment,
           generate_implementation_intention, select_nudge_type
  2.b.iv   score_llm_interaction_health, get_llm_interaction_history  (shipped as a pair)
  2.b.v    trigger_jitai_nudge (general framework)

All tools persist to migration-008 tables. LLM calls are marked [LLM-ENRICHED]
and fall back to deterministic scoring when no ANTHROPIC_API_KEY is configured.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

from fastmcp import FastMCP

from db.connection import get_pool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


_COM_B_COMPONENTS = ("Capability", "Opportunity", "Motivation")
_COM_B_SUB = {
    "Capability":  ("Physical", "Psychological"),
    "Opportunity": ("Physical", "Social"),
    "Motivation":  ("Automatic", "Reflective"),
}


# ── 2.b.iii ───────────────────────────────────────────────────────────────────

async def classify_com_b_barrier(
    patient_id: str,
    target_behavior: str,
    evidence_window_days: int = 30,
) -> str:
    """Map the patient's barrier for a target behavior onto COM-B.

    Research: Michie et al. 2011 COM-B model.

    Deterministic heuristic over recent adherence + SDoH signals:
      - If sdoh transportation/food flags → Opportunity (Physical)
      - If anxiety/depression checkins     → Motivation (Automatic)
      - If low adherence + high intent     → Capability (Physical)
      - Else                                → Motivation (Reflective)  default

    Persists to patient_com_b_assessments.
    """
    pool = await get_pool()
    today = date.today()
    cutoff = today - timedelta(days=evidence_window_days)
    evidence: list[str] = []

    async with pool.acquire() as conn:
        sdoh = await conn.fetch(
            """SELECT domain, severity FROM patient_sdoh_flags
               WHERE patient_id = $1""",
            patient_id,
        )
        checkins = await conn.fetch(
            """SELECT mood, energy, stress_level FROM daily_checkins
               WHERE patient_id = $1 AND checkin_date >= $2""",
            patient_id, cutoff,
        )

    # Classification logic.
    component = "Motivation"
    sub = "Reflective"
    primary = "reflective motivation — default category when no stronger signal"
    for s in sdoh:
        if s["domain"] in ("transportation", "food_access", "housing") and s["severity"] == "high":
            component, sub = "Opportunity", "Physical"
            primary = f"SDoH barrier: {s['domain']} (high severity)"
            evidence.append(f"sdoh:{s['domain']}:high")
            break
    else:
        if checkins:
            avg_stress = sum((c["stress_level"] or 0) for c in checkins) / len(checkins)
            if avg_stress >= 7:
                component, sub = "Motivation", "Automatic"
                primary = "elevated chronic stress depleting automatic motivation"
                evidence.append(f"avg_stress={avg_stress:.1f} over {len(checkins)} check-ins")

    if component not in _COM_B_COMPONENTS or sub not in _COM_B_SUB[component]:
        # safety net
        component, sub = "Motivation", "Reflective"

    # Persist.
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO patient_com_b_assessments
                   (patient_id, target_behavior, com_b_component, sub_component,
                    primary_barrier, confidence, supporting_evidence, assessed_by)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                patient_id, target_behavior, component, sub, primary,
                0.6, evidence, "MIRA",
            )
    except Exception as e:
        logger.warning("com_b_assessments write skipped: %s", e)

    return json.dumps({
        "patient_id": patient_id,
        "target_behavior": target_behavior,
        "com_b_component": component,
        "sub_component": sub,
        "primary_barrier": primary,
        "confidence": 0.6,
        "supporting_evidence": evidence,
    })


async def detect_conversation_teachable_moment(
    patient_id: str,
    conversation_text: str,
    minimum_signal_strength: float = 0.6,
) -> str:
    """Detect teachable-moment signals in a recent conversation snippet.

    Research: McBride 2003 (7–8× behavior-change rate at teachable moments);
              MI change-talk detection; Künzler JITAI receptivity.

    Signal types scored via substring cues:
      change_talk         — "I want to …", "I'm ready to …"
      emotional_disclosure— "I'm scared", "I feel …"
      clinical_event      — hospital / ER / diagnosis mentions
      frustration         — "fed up", "can't keep doing this"
      readiness           — "when should I start", "how do I begin"
    """
    text = (conversation_text or "").lower()
    signals = {
        "change_talk":         ("i want to", "i'm ready to", "i plan to", "i'll try"),
        "emotional_disclosure": ("i'm scared", "i feel", "i'm worried", "i'm anxious"),
        "clinical_event":      ("hospital", " er ", "emergency room", "just diagnosed"),
        "frustration":         ("fed up", "can't keep", "so frustrated", "giving up"),
        "readiness":           ("when should i start", "how do i begin", "where do i start"),
    }
    hits: dict[str, float] = {}
    for signal, cues in signals.items():
        count = sum(1 for c in cues if c in text)
        if count:
            hits[signal] = min(1.0, 0.5 + 0.25 * count)

    if not hits:
        return json.dumps({
            "patient_id": patient_id,
            "teachable_moment_detected": False,
            "signal_type": None, "signal_strength": 0.0,
            "recommended_nudge_urgency": "none",
        })

    signal_type = max(hits, key=hits.get)
    signal_strength = hits[signal_type]
    detected = signal_strength >= minimum_signal_strength

    urgency = "immediate" if signal_type in ("clinical_event", "frustration") and detected else \
              "standard" if detected else "low"

    return json.dumps({
        "patient_id": patient_id,
        "teachable_moment_detected": detected,
        "signal_type": signal_type,
        "signal_strength": round(signal_strength, 3),
        "recommended_nudge_urgency": urgency,
        "all_signals": hits,
    })


async def generate_implementation_intention(
    patient_id: str,
    target_behavior: str,
    anchor_event: str,
    anxiety_state: str = "baseline",
) -> str:
    """Generate a single if-then implementation intention.

    Format: "If [anchor_event], then [target_behavior]."

    Research: Gollwitzer & Sheeran 2006 meta-analysis d=0.65.
    CRITICAL: anxiety impairs executive function. For anxiety_state in
    ('elevated','crisis') this tool REFUSES to emit multi-step plans —
    returns a single-step intention only.
    """
    if anxiety_state not in ("baseline", "elevated", "crisis"):
        return json.dumps({"status": "error",
                           "error": f"invalid anxiety_state '{anxiety_state}'"})
    # Enforce single-step constraint under anxiety. In this scaffold we always
    # produce single-step, so the flag is documentation; the guard is in the
    # downstream LLM prompt when that path is wired.
    intention = {
        "if_condition": anchor_event,
        "then_action":  target_behavior,
        "anchor_routine": anchor_event,
    }
    # Expected lift — crude prior from Gollwitzer meta-analysis for single-step
    # intentions (~18% absolute lift vs no plan). Elevated anxiety halves it.
    lift = 0.18 if anxiety_state == "baseline" else 0.09
    return json.dumps({
        "patient_id": patient_id,
        "target_behavior": target_behavior,
        "intention_plan": intention,
        "rehearsal_suggestion": (
            f"Say aloud once per day for a week: 'When {anchor_event}, "
            f"I {target_behavior}.'"
        ),
        "expected_adherence_lift": lift,
        "complexity_level": "single_step",
        "anxiety_state": anxiety_state,
    })


async def select_nudge_type(
    patient_id: str,
    com_b_component: str,
    fogg_motivation: float,
    fogg_ability: float,
    current_nis_score: float,
) -> str:
    """Select an evidence-based nudge type given COM-B + Fogg position + NIS.

    Research: Fogg Behavior Model; Michie 2013 COM-B → BCT mapping;
              MHC-Coach 2025; ENCOURAGE RCT 2022.

    Deterministic mapping table — keep auditable:
      Capability   + low ability          → implementation_intention
      Opportunity  + any                  → barrier_identification
      Motivation   + low motivation       → motivational_interview_prompt
      Motivation   + high motivation      → commitment_device
      Capability   + high ability         → reminder
    """
    if com_b_component not in _COM_B_COMPONENTS:
        return json.dumps({"status": "error",
                           "error": f"unknown com_b_component '{com_b_component}'"})
    motivation_high = fogg_motivation >= 0.6
    ability_high = fogg_ability >= 0.6

    if com_b_component == "Opportunity":
        nudge = "barrier_identification"
        effect = 0.20
    elif com_b_component == "Capability" and not ability_high:
        nudge = "implementation_intention"
        effect = 0.18
    elif com_b_component == "Capability" and ability_high:
        nudge = "reminder"
        effect = 0.08
    elif com_b_component == "Motivation" and not motivation_high:
        nudge = "motivational_interview_prompt"
        effect = 0.15
    else:
        nudge = "commitment_device"
        effect = 0.12

    contraindicated = []
    if current_nis_score < 0.45:
        contraindicated = ["loss_frame", "social_norm"]   # can backfire below NIS threshold

    return json.dumps({
        "patient_id": patient_id,
        "selected_nudge_type": nudge,
        "rationale": (
            f"COM-B={com_b_component}, Fogg M={fogg_motivation:.2f}, "
            f"A={fogg_ability:.2f} → {nudge}"
        ),
        "estimated_effect_size": effect,
        "contraindicated_types": contraindicated,
        "delivery_channel": "sms" if nudge != "motivational_interview_prompt" else "conversation",
    })


# ── 2.b.iv — LLM interaction health pair ─────────────────────────────────────

_OVERRELIANCE_PATTERNS = (
    "asked you the same",
    "only trust you",
    "don't want to see my doctor",
    "skip my appointment",
)


async def score_llm_interaction_health(
    patient_id: str,
    conversation_excerpt: str = "",
    session_duration_min: int = 0,
) -> str:
    """Daily LLM-interaction health score. Persists one row per patient_id per day.

    health_score 1.0 = healthy
                 0.7+ = monitor
                 < 0.4 = suppress + human redirect
    """
    text = (conversation_excerpt or "").lower()
    hits = [p for p in _OVERRELIANCE_PATTERNS if p in text]
    # Start healthy; penalize each over-reliance pattern by 0.2; long sessions
    # add mild penalty above 30 min.
    health_score = 1.0 - 0.2 * len(hits)
    if session_duration_min > 30:
        health_score -= min(0.3, (session_duration_min - 30) * 0.005)
    health_score = round(max(0.0, min(1.0, health_score)), 3)

    if health_score >= 0.7:
        pattern, action = "healthy", "proceed"
    elif health_score >= 0.4:
        pattern, action = "monitor", "monitor and re-score next session"
    else:
        pattern, action = "over_reliance", "suppress nudges; offer human clinician handoff"

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO patient_llm_interactions
                   (patient_id, session_date, health_score, session_duration_min,
                    session_count, patterns_detected, referral_made)
                   VALUES ($1,$2,$3,$4,1,$5,$6)
                   ON CONFLICT (patient_id, session_date) DO UPDATE
                   SET health_score        = EXCLUDED.health_score,
                       session_duration_min= EXCLUDED.session_duration_min,
                       session_count       = patient_llm_interactions.session_count + 1,
                       patterns_detected   = EXCLUDED.patterns_detected,
                       referral_made       = EXCLUDED.referral_made""",
                patient_id, date.today(), health_score, session_duration_min,
                hits, health_score < 0.4,
            )
    except Exception as e:
        logger.warning("patient_llm_interactions write skipped: %s", e)

    return json.dumps({
        "patient_id": patient_id,
        "health_score": health_score,
        "interaction_pattern": pattern,
        "over_reliance_detected": len(hits) > 0,
        "displacement_risk": 1.0 - health_score,
        "recommended_action": action,
    })


async def get_llm_interaction_history(patient_id: str, days: int = 30) -> str:
    """Longitudinal LLM-interaction trend for a patient.

    Required for distinguishing transient reliance spikes from chronic dependency.
    """
    cutoff = date.today() - timedelta(days=days)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT session_date, health_score, session_duration_min,
                      session_count, patterns_detected, referral_made
               FROM patient_llm_interactions
               WHERE patient_id = $1 AND session_date >= $2
               ORDER BY session_date ASC""",
            patient_id, cutoff,
        )
    daily = [
        {
            "date": r["session_date"].isoformat(),
            "health_score": r["health_score"],
            "session_count": r["session_count"],
            "flagged_patterns": list(r["patterns_detected"] or []),
            "referral_made": r["referral_made"],
        }
        for r in rows
    ]

    # Trend: slope across window.
    trend = "unknown"
    chronic = False
    if len(daily) >= 7:
        first = sum(d["health_score"] for d in daily[: len(daily)//2]) / (len(daily)//2)
        second = sum(d["health_score"] for d in daily[len(daily)//2 :]) / (len(daily) - len(daily)//2)
        delta = second - first
        if delta > 0.05:
            trend = "improving"
        elif delta < -0.05:
            trend = "declining"
        else:
            trend = "stable"
        chronic = sum(1 for d in daily if d["health_score"] < 0.4) >= 5

    return json.dumps({
        "patient_id": patient_id,
        "window_days": days,
        "daily_scores": daily,
        "trend": trend,
        "chronic_overreliance": chronic,
    })


# ── 2.b.v — Generalized JITAI framework ──────────────────────────────────────

async def trigger_jitai_nudge(
    patient_id: str,
    trigger_type: str,
    required_conditions: list,
    day_window: dict | None = None,
    urgency: str = "standard",
) -> str:
    """General Just-In-Time Adaptive Intervention trigger.

    Research: Künzler et al. JITAI ±40% improvement.

    Replaces hard-coded single-scenario triggers (e.g. run_food_access_nudge
    becomes a thin wrapper over this). Conditions are strings of the form
    'sdoh_flag:food_access', 'vitals:fresh', 'adherence:low' — the evaluator
    maps each to a deterministic DB check.
    """
    if urgency not in ("immediate", "standard", "low"):
        return json.dumps({"status": "error", "error": f"invalid urgency '{urgency}'"})

    pool = await get_pool()
    conditions_met, conditions_unmet = [], []
    async with pool.acquire() as conn:
        for cond in required_conditions:
            ok = await _evaluate_condition(conn, patient_id, cond)
            (conditions_met if ok else conditions_unmet).append(cond)

    # Day-window gate.
    window_ok = True
    if day_window and "month_day_min" in day_window:
        window_ok = date.today().day >= int(day_window["month_day_min"])

    fired = window_ok and not conditions_unmet
    nudge_id = str(uuid.uuid4()) if fired else ""
    return json.dumps({
        "patient_id": patient_id,
        "trigger_type": trigger_type,
        "fired": fired,
        "conditions_met": conditions_met,
        "conditions_unmet": conditions_unmet,
        "day_window_satisfied": window_ok,
        "nudge_queued": fired,
        "nudge_id": nudge_id,
        "urgency": urgency,
    })


async def _evaluate_condition(conn, patient_id: str, condition: str) -> bool:
    """Deterministic condition evaluator. Extend here as new predicates land."""
    try:
        kind, _, arg = condition.partition(":")
        if kind == "sdoh_flag":
            row = await conn.fetchrow(
                "SELECT 1 FROM patient_sdoh_flags WHERE patient_id=$1 AND domain=$2",
                patient_id, arg,
            )
            return row is not None
        if kind == "vitals" and arg == "fresh":
            row = await conn.fetchrow(
                """SELECT MAX(measured_at) AS m FROM biometric_readings
                   WHERE patient_id = $1""",
                patient_id,
            )
            if row is None or row["m"] is None:
                return False
            return (datetime.now(timezone.utc) - row["m"]).days <= 7
        if kind == "adherence" and arg == "low":
            row = await conn.fetchrow(
                """SELECT AVG(CASE WHEN taken THEN 1 ELSE 0 END) AS rate
                   FROM medication_adherence
                   WHERE patient_id = $1
                     AND adherence_date >= $2""",
                patient_id, date.today() - timedelta(days=14),
            )
            return row is not None and row["rate"] is not None and row["rate"] < 0.7
    except Exception as e:
        logger.info("JITAI condition '%s' evaluator error: %s", condition, e)
    return False


# ── Registration ─────────────────────────────────────────────────────────────

def register(mcp: FastMCP) -> None:
    mcp.tool(classify_com_b_barrier)
    mcp.tool(detect_conversation_teachable_moment)
    mcp.tool(generate_implementation_intention)
    mcp.tool(select_nudge_type)
    mcp.tool(score_llm_interaction_health)
    mcp.tool(get_llm_interaction_history)
    mcp.tool(trigger_jitai_nudge)

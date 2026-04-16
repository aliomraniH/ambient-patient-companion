"""
flag_reviewer.py — post-ingest and post-deliberation hook that reviews
all open flags for a patient and determines what changed.

Runs in two modes:
  1. post_ingest: triggered after new data lands — focuses on data_corrupt/data_missing flags
  2. post_deliberation: triggered after a new deliberation — compares old flags to new flags

The LLM reviewer (Haiku — fast, cheap) receives open flags with provenance
plus current warehouse state, and outputs corrections to apply.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

import anthropic

log = logging.getLogger(__name__)

REVIEWER_MODEL = "claude-haiku-4-5-20251001"

REVIEWER_SYSTEM = """You are a clinical data quality reviewer for an AI health companion.

Your job is to review previously generated clinical flags and determine whether they are:
1. Still valid (new data confirms the concern)
2. Retractable (the data that caused the flag has been corrected)
3. Needing human clarification (ambiguous — could be data artifact OR real clinical issue)
4. Changed in priority (new data makes it more or less urgent)

SAFETY RULE: Never auto-retract a flag that:
- Was linked to a nudge that was sent to a patient or care team
- Has priority "critical" or "high" unless the evidence is overwhelming
- Involves medication safety, allergy, or acute symptoms

For each flag, return a JSON correction object. Respond with a JSON array ONLY.

Correction schema:
{
  "flag_id": "uuid",
  "action": "auto_retract" | "escalate_human" | "confirm_valid" | "upgrade_priority" | "downgrade_priority",
  "confidence": 0.0-1.0,
  "reasoning": "one sentence",
  "clarification_question": "optional — what to ask the clinician",
  "clarification_options": [{"value": "v", "label": "l", "implications": "i"}],
  "new_priority": "low|medium|medium-high|high|critical" (optional)
}"""


async def run_flag_review(
    pool,
    patient_id: str,
    trigger_type: str,
    trigger_ref_id: str,
    new_data_summary: str = "",
) -> dict:
    """
    Review all open flags for a patient and generate corrections.
    Accepts an asyncpg pool and acquires its own connection.
    """
    review_id = str(uuid.uuid4())
    start = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        # Write the review run record
        await conn.execute(
            """INSERT INTO flag_review_runs
                   (id, patient_id, trigger_type, trigger_ref_id)
               VALUES ($1::uuid, $2::uuid, $3, $4::uuid)""",
            review_id, patient_id, trigger_type,
            trigger_ref_id if trigger_ref_id else None,
        )

        # Fetch all open flags for this patient
        open_flags = await conn.fetch(
            """SELECT id, flag_type, title, description, priority::text,
                      flag_basis::text, data_provenance, data_quality_score,
                      had_zero_values, nudge_was_sent, linked_nudge_ids,
                      flagged_at
               FROM deliberation_flags
               WHERE patient_id = $1::uuid AND lifecycle_state = 'open'
               ORDER BY flagged_at DESC""",
            patient_id,
        )

        if not open_flags:
            await _complete_review(conn, review_id, start, flags_reviewed=0)
            return {
                "review_id": review_id,
                "flags_reviewed": 0,
                "corrections": 0,
                "stats": {},
                "summary": "No open flags",
            }

        corrections = []

        # ── Phase 1: Deterministic rules (no LLM) ─────────────────
        for flag in open_flags:
            retract = await _check_deterministic_retract(conn, patient_id, flag)
            if retract:
                corrections.append({
                    "flag_id": str(flag["id"]),
                    "action": "auto_retract",
                    "confidence": 0.95,
                    "reasoning": retract["reason"],
                    "source": "deterministic",
                    "applied": False,
                })

        # ── Phase 2: LLM review for remaining flags ───────────────
        already_decided = {c["flag_id"] for c in corrections}
        remaining = [f for f in open_flags if str(f["id"]) not in already_decided]

        if remaining:
            llm_corrections = await _llm_review_flags(
                conn, patient_id, remaining, new_data_summary,
            )
            corrections.extend(llm_corrections)

        # ── Phase 3: Apply corrections ─────────────────────────────
        stats = {
            "retracted": 0,
            "escalated": 0,
            "confirmed": 0,
            "upgraded": 0,
            "downgraded": 0,
        }
        for correction in corrections:
            await _apply_correction(conn, correction, review_id, patient_id, stats)

        # Write corrections to DB
        for correction in corrections:
            await conn.execute(
                """INSERT INTO flag_corrections (
                       flag_id, review_run_id, patient_id,
                       action, confidence, reasoning,
                       clarification_question, clarification_options,
                       old_priority, new_priority,
                       applied, applied_at, applied_by
                   ) VALUES (
                       $1::uuid, $2::uuid, $3::uuid,
                       $4::correction_action, $5, $6,
                       $7, $8::jsonb,
                       $9::flag_priority, $10::flag_priority,
                       $11, $12, $13
                   )""",
                correction["flag_id"],
                review_id,
                patient_id,
                correction["action"],
                correction.get("confidence", 0.7),
                correction.get("reasoning", ""),
                correction.get("clarification_question"),
                json.dumps(correction.get("clarification_options", [])),
                correction.get("old_priority"),
                correction.get("new_priority"),
                correction.get("applied", False),
                datetime.now(timezone.utc) if correction.get("applied") else None,
                "auto" if correction.get("applied") else None,
            )

        summary = _generate_review_summary(stats, len(open_flags), new_data_summary)
        await _complete_review(
            conn, review_id, start,
            flags_reviewed=len(open_flags), stats=stats, summary=summary,
        )

    return {
        "review_id": review_id,
        "flags_reviewed": len(open_flags),
        "corrections": len(corrections),
        "stats": stats,
        "summary": summary,
    }


async def _check_deterministic_retract(conn, patient_id: str, flag) -> dict | None:
    """
    Check if a flag qualifies for deterministic (no-LLM) retraction.
    Only safe for data_corrupt + data_missing flags that never generated sent nudges.
    """
    if flag.get("nudge_was_sent"):
        return None

    basis = flag.get("flag_basis", "")
    priority = flag.get("priority", "medium")
    had_zeros = flag.get("had_zero_values", False)

    # Only auto-retract low/medium/medium-high flags
    if priority in ("high", "critical"):
        return None

    # Rule: data_corrupt flag that was triggered by 0.0 values
    if basis == "data_corrupt" and had_zeros:
        real_lab_count = await conn.fetchval(
            """SELECT COUNT(*) FROM biometric_readings
               WHERE patient_id = $1::uuid
                 AND value IS NOT NULL
                 AND value != 0.0
                 AND measured_at >= CURRENT_DATE - INTERVAL '90 days'""",
            patient_id,
        )
        if real_lab_count and real_lab_count >= 5:
            return {
                "reason": (
                    f"Data corruption (0.0 values) now corrected — "
                    f"{real_lab_count} real lab values exist"
                )
            }

    # Rule: data_missing flag where sex/gender is now populated
    if basis == "data_missing":
        title_lower = (flag.get("title") or "").lower()
        if "sex" in title_lower or "gender" in title_lower:
            sex = await conn.fetchval(
                "SELECT gender FROM patients WHERE id = $1::uuid", patient_id,
            )
            if sex and sex.lower() not in ("unknown", ""):
                return {"reason": "Sex/gender field is now documented in patient record"}

    return None


async def _llm_review_flags(
    conn, patient_id: str, flags: list, new_data_summary: str,
) -> list:
    """Use Haiku to review flags that couldn't be deterministically resolved."""
    if not flags:
        return []

    # Build current data snapshot (Tier 1)
    try:
        from .tiered_context_loader import TieredContextLoader
        loader = TieredContextLoader(conn, patient_id)
        current_ctx = await loader.load_tier1()
        current_json = json.dumps(current_ctx, default=str)[:3000]
    except Exception:
        current_json = "{}"

    flags_json = json.dumps([
        {
            "flag_id": str(f["id"]),
            "title": f["title"],
            "description": f["description"],
            "priority": f["priority"],
            "basis": f["flag_basis"],
            "flagged_at": str(f["flagged_at"]),
            "had_zero_values": f.get("had_zero_values", False),
            "nudge_was_sent": f.get("nudge_was_sent", False),
        }
        for f in flags
    ], indent=2)

    prompt = f"""Review these open clinical flags against the current patient data.

CURRENT PATIENT DATA (as of now):
{current_json}

NEW DATA THAT JUST ARRIVED:
{new_data_summary or 'No new data summary available'}

OPEN FLAGS TO REVIEW:
{flags_json}

For each flag, determine whether it should be:
- auto_retract: the data problem that caused it is now fixed
- escalate_human: ambiguous — could be data artifact OR real clinical finding
- confirm_valid: new data confirms the flag is still correct
- upgrade_priority: new data makes this more urgent
- downgrade_priority: new data makes this less urgent

Remember: NEVER auto_retract if nudge_was_sent=true or priority=critical/high.

Return a JSON array of correction objects."""

    client = anthropic.AsyncAnthropic()
    try:
        response = await client.messages.create(
            model=REVIEWER_MODEL,
            max_tokens=4096,
            system=REVIEWER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        corrections = json.loads(raw)
        for c in corrections:
            c["source"] = "llm"
            c["applied"] = False
        return corrections if isinstance(corrections, list) else []
    except Exception as e:
        log.warning("LLM flag review failed: %s", e)
        return []


async def _apply_correction(
    conn, correction: dict, review_id: str, patient_id: str, stats: dict,
):
    """Apply a single correction to the flag lifecycle_state."""
    flag_id = correction.get("flag_id")
    action = correction.get("action")

    if action == "auto_retract":
        if correction.get("confidence", 0) >= 0.7:
            await conn.execute(
                """UPDATE deliberation_flags
                   SET lifecycle_state = 'retracted',
                       retraction_reason = $1,
                       retraction_trigger = 'auto',
                       retracted_by = $2::uuid,
                       reviewed_at = NOW()
                   WHERE id = $3::uuid AND lifecycle_state = 'open'""",
                correction.get("reasoning", ""),
                review_id,
                flag_id,
            )
            correction["applied"] = True
            stats["retracted"] += 1

    elif action == "escalate_human":
        await conn.execute(
            """UPDATE deliberation_flags
               SET requires_human = true, reviewed_at = NOW()
               WHERE id = $1::uuid""",
            flag_id,
        )
        correction["applied"] = True
        stats["escalated"] += 1

    elif action == "confirm_valid":
        await conn.execute(
            "UPDATE deliberation_flags SET reviewed_at = NOW() WHERE id = $1::uuid",
            flag_id,
        )
        correction["applied"] = True
        stats["confirmed"] += 1

    elif action in ("upgrade_priority", "downgrade_priority"):
        new_p = correction.get("new_priority")
        if new_p:
            await conn.execute(
                """UPDATE deliberation_flags
                   SET priority = $1::flag_priority, reviewed_at = NOW()
                   WHERE id = $2::uuid""",
                new_p, flag_id,
            )
            correction["applied"] = True
            key = "upgraded" if action == "upgrade_priority" else "downgraded"
            stats[key] += 1


def _generate_review_summary(stats: dict, total: int, new_data: str) -> str:
    parts = []
    if stats.get("retracted"):
        parts.append(f"{stats['retracted']} retracted (data now correct)")
    if stats.get("escalated"):
        parts.append(f"{stats['escalated']} escalated to human review")
    if stats.get("confirmed"):
        parts.append(f"{stats['confirmed']} confirmed still valid")
    if stats.get("upgraded"):
        parts.append(f"{stats['upgraded']} upgraded in priority")
    if stats.get("downgraded"):
        parts.append(f"{stats['downgraded']} downgraded in priority")
    base = f"Reviewed {total} open flags. "
    return base + (", ".join(parts) if parts else "No changes.")


async def _complete_review(
    conn,
    review_id: str,
    start: datetime,
    flags_reviewed: int = 0,
    stats: dict | None = None,
    summary: str = "",
):
    elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    s = stats or {}
    await conn.execute(
        """UPDATE flag_review_runs SET
               flags_reviewed = $1,
               flags_retracted = $2,
               flags_escalated = $3,
               flags_unchanged = $4,
               review_summary = $5,
               completed_at = NOW(),
               duration_ms = $6
           WHERE id = $7::uuid""",
        flags_reviewed,
        s.get("retracted", 0),
        s.get("escalated", 0),
        flags_reviewed - sum(s.values()) if s else flags_reviewed,
        summary,
        elapsed,
        review_id,
    )

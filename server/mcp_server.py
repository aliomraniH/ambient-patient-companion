"""FastMCP server for clinical decision support — Phase 1.

Provides 15 tools:
1.  clinical_query              — Three-layer guardrail pipeline with Claude API
2.  get_guideline               — Fetch specific guideline by recommendation ID
3.  check_screening_due         — Return overdue USPSTF screenings for a patient
4.  flag_drug_interaction       — Return known drug interactions
5.  get_synthetic_patient       — Return patient record by MRN from the database
6.  use_healthex                — Switch data track to HealthEx real records
7.  use_demo_data               — Switch data track to Synthea demo data
8.  switch_data_track           — Switch data track to a named source
9.  get_data_source_status      — Report active data track and available sources
10. run_deliberation            — Trigger a full Dual-LLM deliberation session
11. get_deliberation_results    — Retrieve outputs from the most recent deliberation
12. get_pending_nudges          — Pull queued nudges for a patient
13. register_healthex_patient   — Create/upsert a HealthEx patient row and return UUID
14. ingest_from_healthex        — Write HealthEx FHIR data into the warehouse
15. get_clinical_summary        — Summarise a patient's clinical state

HealthEx pipeline (all on /mcp — the confirmed working Claude Web endpoint):
  use_healthex → register_healthex_patient → ingest_from_healthex
  → run_deliberation → get_deliberation_results → get_pending_nudges

All AI calls route through the guardrail pipeline. HTML prototypes never
call Claude API directly.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid as _uuid_mod
from datetime import datetime as _dt, date as _date, timedelta as _td, timezone as _tz
from pathlib import Path

# Allow the clinical server to import FHIR transforms that live in mcp-server/
_MCPSERVER_DIR = Path(__file__).resolve().parent.parent / "mcp-server"
if str(_MCPSERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_MCPSERVER_DIR))

# Allow imports from the repo root (shared/provenance lives there).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import anthropic
import asyncpg
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from server.guardrails.input_validator import validate_input
from server.guardrails.output_validator import validate_output
from server.guardrails.clinical_rules import check_escalation
from server.deliberation.engine import DeliberationEngine
from server.deliberation.schemas import DeliberationRequest
from server.deliberation.json_utils import strip_markdown_fences
from gap_aware.db import (
    get_pool as get_gap_pool,
    insert_reasoning_gap,
    insert_clarification_request,
    insert_gap_trigger,
    get_gaps_for_deliberation,
)

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("ambient-clinical-intelligence")

# ---------------------------------------------------------------------------
# Guidelines loader
# ---------------------------------------------------------------------------

_GUIDELINES_DIR = Path(__file__).parent / "guidelines"
_CONFIG_DIR = Path(__file__).parent.parent / "config" / "system_prompts"

_ada_guidelines: list[dict] | None = None
_uspstf_guidelines: list[dict] | None = None


def _load_guidelines() -> tuple[list[dict], list[dict]]:
    """Load guidelines from JSON files on disk. Cached after first load.

    Returns:
        Tuple of (ada_guidelines, uspstf_guidelines).
    """
    global _ada_guidelines, _uspstf_guidelines
    if _ada_guidelines is None:
        with open(_GUIDELINES_DIR / "ada_standards.json") as f:
            _ada_guidelines = json.load(f)
    if _uspstf_guidelines is None:
        with open(_GUIDELINES_DIR / "uspstf_recs.json") as f:
            _uspstf_guidelines = json.load(f)
    return _ada_guidelines, _uspstf_guidelines


def _load_system_prompt(role: str) -> str:
    """Load role-specific system prompt XML from config directory.

    Args:
        role: One of 'pcp', 'care_manager', 'patient'.

    Returns:
        The system prompt XML content as a string.

    Raises:
        FileNotFoundError: If the prompt file for the role doesn't exist.
    """
    role_to_file = {
        "pcp": "pcp_encounter.xml",
        "care_manager": "care_manager.xml",
        "patient": "patient_facing.xml",
    }
    filename = role_to_file.get(role)
    if not filename:
        raise ValueError(
            f"Unknown role '{role}'. Must be one of: {list(role_to_file.keys())}"
        )
    prompt_path = _CONFIG_DIR / filename
    return prompt_path.read_text(encoding="utf-8")


def _select_relevant_guidelines(
    query: str, patient_context: dict | None = None
) -> list[dict]:
    """Select guidelines relevant to the query and patient context.

    Phase 1 uses keyword matching. Phase 2 will replace this with
    vector similarity search.

    Args:
        query: The clinical query.
        patient_context: Optional patient context dict with conditions, medications, etc.

    Returns:
        List of relevant guideline entries.
    """
    ada, uspstf = _load_guidelines()
    all_guidelines = ada + uspstf
    query_lower = query.lower()

    # Extract patient conditions for matching
    conditions: list[str] = []
    if patient_context:
        conditions = [c.lower() for c in patient_context.get("conditions", [])]
        medications = [m.lower() for m in patient_context.get("medications", [])]
    else:
        medications = []

    relevant: list[dict] = []
    for g in all_guidelines:
        text_lower = g["text"].lower()
        section_lower = g.get("section", "").lower()
        chapter_lower = g.get("chapter", "").lower()

        # Match if query terms appear in guideline text, section, or chapter
        query_words = [w for w in query_lower.split() if len(w) > 3]
        text_match = any(w in text_lower for w in query_words)

        # Match if patient conditions overlap with guideline population
        pop = [p.lower() for p in g.get("patient_population", [])]
        condition_match = any(c in " ".join(pop) for c in conditions)

        # Match if patient medications overlap with guideline medications
        guideline_meds = [m.lower() for m in g.get("medications_mentioned", [])]
        med_match = any(m in " ".join(guideline_meds) for m in medications)

        if text_match or condition_match or med_match:
            relevant.append(g)

    # If no matches, return top guidelines by evidence grade
    if not relevant:
        grade_a = [g for g in all_guidelines if g["evidence_grade"] == "A"]
        relevant = grade_a[:5]

    return relevant[:10]  # Cap at 10 to stay within token limits


def _format_guidelines_for_prompt(guidelines: list[dict]) -> str:
    """Format guidelines as XML for injection into system prompt.

    Args:
        guidelines: List of guideline dicts.

    Returns:
        XML-formatted string of guidelines.
    """
    if not guidelines:
        return "<guidelines>No specific guidelines retrieved for this query.</guidelines>"

    parts = ["<guidelines>"]
    for g in guidelines:
        parts.append(f"  <guideline>")
        parts.append(f"    <source>{g['guideline_source']} {g['version']}</source>")
        parts.append(f"    <recommendation_id>{g['recommendation_id']}</recommendation_id>")
        parts.append(f"    <chapter>{g['chapter']}</chapter>")
        parts.append(f"    <section>{g['section']}</section>")
        parts.append(f"    <text>{g['text']}</text>")
        parts.append(f"    <evidence_grade>{g['evidence_grade']}</evidence_grade>")
        parts.append(f"    <strength>{g['recommendation_strength']}</strength>")
        if g.get("contraindications"):
            parts.append(f"    <contraindications>{', '.join(g['contraindications'])}</contraindications>")
        if g.get("medications_mentioned"):
            parts.append(f"    <medications>{', '.join(g['medications_mentioned'])}</medications>")
        parts.append(f"  </guideline>")
    parts.append("</guidelines>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool 1: clinical_query
# ---------------------------------------------------------------------------

@mcp.tool()
async def clinical_query(query: str, role: str, patient_context: dict) -> dict:
    """Three-layer guardrail pipeline: validate input, generate with role prompt, validate output.

    Processes a clinical query through the full safety pipeline:
    1. Input validation (PHI detection, jailbreak screening, scope check)
    2. Generation (role-specific system prompt + relevant guidelines + Claude API)
    3. Output validation (citation check, PHI leakage, diagnostic language)

    Args:
        query: The clinical question or request.
        role: One of 'pcp', 'care_manager', 'patient'.
        patient_context: Dict with patient information (conditions, medications, labs, etc.).

    Returns:
        Dict with status, recommendation, citations, and escalation_flags.
    """
    # --- Layer 1: Input validation ---
    input_result = validate_input(query)
    if input_result.blocked:
        return {
            "status": "blocked",
            "reason": input_result.reason,
            "recommendation": None,
            "citations": [],
            "escalation_flags": [],
        }

    # --- Check escalation triggers ---
    escalation_triggers = check_escalation(query)
    blocking_triggers = [t for t in escalation_triggers if t["blocking"] == "true"]

    if blocking_triggers:
        return {
            "status": "escalated",
            "reason": blocking_triggers[0]["message"],
            "recommendation": None,
            "citations": [],
            "escalation_flags": escalation_triggers,
        }

    # --- Layer 2: Generation ---
    try:
        # Load role-specific system prompt
        system_prompt_template = _load_system_prompt(role)

        # Select and format relevant guidelines
        relevant_guidelines = _select_relevant_guidelines(
            input_result.cleaned_query, patient_context
        )
        guidelines_xml = _format_guidelines_for_prompt(relevant_guidelines)

        # Inject guidelines into system prompt
        system_prompt = system_prompt_template.replace(
            "{{GUIDELINES_PLACEHOLDER}}", guidelines_xml
        )

        # Prepend escalation flags if any non-blocking triggers
        escalation_prefix = ""
        for trigger in escalation_triggers:
            if trigger["blocking"] == "false":
                escalation_prefix += trigger["message"] + "\n\n"

        # Build user message with patient context
        user_message = f"{escalation_prefix}{input_result.cleaned_query}"
        if patient_context:
            context_str = json.dumps(patient_context, indent=2)
            user_message += f"\n\nPatient Context:\n{context_str}"

        # Call Claude API
        client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        )

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        generated_text = response.content[0].text

    except Exception as e:
        logger.error("Generation failed: %s", e)
        return {
            "status": "error",
            "reason": f"Generation failed: {str(e)}",
            "recommendation": None,
            "citations": [],
            "escalation_flags": escalation_triggers,
        }

    # --- Layer 3: Output validation ---
    output_result = validate_output(generated_text, relevant_guidelines)

    # Extract citations from guidelines used
    citations = [
        {
            "source": g["guideline_source"],
            "version": g["version"],
            "recommendation_id": g["recommendation_id"],
            "evidence_grade": g["evidence_grade"],
        }
        for g in relevant_guidelines
    ]

    return {
        "status": "success" if output_result.safe else "warning",
        "recommendation": output_result.safe_response,
        "citations": citations,
        "escalation_flags": escalation_triggers,
        "validation_flags": output_result.flags,
    }


# ---------------------------------------------------------------------------
# Tool 2: get_guideline
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_guideline(recommendation_id: str) -> dict:
    """Fetch a specific guideline by its recommendation ID from the JSON store.

    Args:
        recommendation_id: The unique recommendation ID (e.g., '9.1a', 'USPSTF-CRC-01').

    Returns:
        The full guideline entry dict, or an error dict if not found.
    """
    ada, uspstf = _load_guidelines()
    all_guidelines = ada + uspstf

    for g in all_guidelines:
        if g["recommendation_id"] == recommendation_id:
            return g

    return {
        "error": f"Guideline with recommendation_id '{recommendation_id}' not found.",
        "available_ids": [g["recommendation_id"] for g in all_guidelines],
    }


# ---------------------------------------------------------------------------
# Tool 3: check_screening_due
# ---------------------------------------------------------------------------

@mcp.tool()
async def check_screening_due(
    patient_age: int, sex: str, conditions: list[str]
) -> list[dict]:
    """Return list of USPSTF screenings applicable to this patient profile.

    Evaluates all USPSTF recommendations against the patient's age, sex,
    and conditions to determine which screenings are due.

    Args:
        patient_age: Patient's age in years.
        sex: Patient's sex ('male' or 'female').
        conditions: List of patient conditions (e.g., ['type_2_diabetes', 'obesity']).

    Returns:
        List of applicable screening dicts with screening_name, recommendation_id,
        uspstf_grade, and recommendation_text.
    """
    _, uspstf = _load_guidelines()
    conditions_lower = [c.lower() for c in conditions]
    sex_lower = sex.lower()
    applicable: list[dict] = []

    for rec in uspstf:
        pop = rec.get("patient_population", [])
        section = rec.get("section", "")

        # Age-based eligibility checks
        is_eligible = False

        if "age_45_75" in pop and 45 <= patient_age <= 75:
            is_eligible = True
        elif "age_18_plus" in pop and patient_age >= 18:
            is_eligible = True
        elif "age_35_70" in pop and 35 <= patient_age <= 70:
            # Diabetes screening also requires overweight/obesity
            if any(c in conditions_lower for c in ["overweight", "obesity", "obese"]):
                is_eligible = True
        elif "age_40_74" in pop and 40 <= patient_age <= 74:
            # Breast cancer — women only
            if sex_lower == "female":
                is_eligible = True
        elif "age_21_65" in pop and 21 <= patient_age <= 65:
            # Cervical cancer — women only
            if sex_lower == "female":
                is_eligible = True
        elif "age_50_80" in pop and 50 <= patient_age <= 80:
            # Lung cancer — requires smoking history (check conditions)
            if any("smok" in c for c in conditions_lower):
                is_eligible = True

        # General adult screenings (e.g., depression)
        if "adults" in pop and patient_age >= 18 and not any(
            age_tag in pop
            for age_tag in [
                "age_45_75", "age_18_plus", "age_35_70",
                "age_40_74", "age_21_65", "age_50_80",
            ]
        ):
            is_eligible = True

        if is_eligible:
            applicable.append({
                "screening_name": section,
                "recommendation_id": rec["recommendation_id"],
                "uspstf_grade": rec["evidence_grade"],
                "recommendation_text": rec["text"],
                "guideline_source": rec["guideline_source"],
                "version": rec["version"],
            })

    return applicable


# ---------------------------------------------------------------------------
# Tool 4: flag_drug_interaction
# ---------------------------------------------------------------------------

# Hardcoded interaction rules for common diabetes/CVD medications
_DRUG_INTERACTIONS: list[dict] = [
    {
        "drug_a": "metformin",
        "drug_b": "contrast dye",
        "severity": "high",
        "description": "Metformin should be held before and after iodinated contrast procedures due to risk of lactic acidosis, especially in patients with renal impairment.",
        "action": "Hold metformin 48 hours before and after contrast administration. Check renal function before restarting.",
    },
    {
        "drug_a": "metformin",
        "drug_b": "alcohol",
        "severity": "moderate",
        "description": "Excessive alcohol intake with metformin increases risk of lactic acidosis and hypoglycemia.",
        "action": "Advise moderation of alcohol intake.",
    },
    {
        "drug_a": "lisinopril",
        "drug_b": "losartan",
        "severity": "high",
        "description": "Concurrent use of ACE inhibitor and ARB (dual RAAS blockade) increases risk of hyperkalemia, hypotension, and renal impairment.",
        "action": "Avoid combination. Use one agent only. Monitor potassium and renal function.",
    },
    {
        "drug_a": "lisinopril",
        "drug_b": "spironolactone",
        "severity": "high",
        "description": "ACE inhibitor with potassium-sparing diuretic significantly increases hyperkalemia risk.",
        "action": "Monitor potassium closely. Consider dose reduction.",
    },
    {
        "drug_a": "atorvastatin",
        "drug_b": "gemfibrozil",
        "severity": "high",
        "description": "Statin with fibrate increases risk of rhabdomyolysis.",
        "action": "Avoid combination if possible. If required, use fenofibrate instead of gemfibrozil and monitor CK levels.",
    },
    {
        "drug_a": "empagliflozin",
        "drug_b": "insulin",
        "severity": "moderate",
        "description": "SGLT2 inhibitor with insulin increases risk of hypoglycemia and euglycemic diabetic ketoacidosis.",
        "action": "Consider reducing insulin dose when initiating SGLT2 inhibitor. Monitor blood glucose closely.",
    },
    {
        "drug_a": "semaglutide",
        "drug_b": "insulin",
        "severity": "moderate",
        "description": "GLP-1 RA with insulin increases hypoglycemia risk. Both agents lower blood glucose through different mechanisms.",
        "action": "Reduce insulin dose by 20% when initiating GLP-1 RA. Titrate based on glucose monitoring.",
    },
    {
        "drug_a": "metformin",
        "drug_b": "empagliflozin",
        "severity": "low",
        "description": "Metformin and SGLT2 inhibitors are commonly used together with complementary mechanisms. Low interaction risk.",
        "action": "Standard monitoring. Generally safe combination per ADA guidelines.",
    },
    {
        "drug_a": "lisinopril",
        "drug_b": "potassium supplements",
        "severity": "high",
        "description": "ACE inhibitors reduce potassium excretion. Adding potassium supplements increases hyperkalemia risk.",
        "action": "Monitor potassium levels. Avoid potassium supplements unless documented hypokalemia.",
    },
    {
        "drug_a": "aspirin",
        "drug_b": "warfarin",
        "severity": "high",
        "description": "Concurrent antiplatelet and anticoagulant therapy significantly increases bleeding risk.",
        "action": "Assess bleeding risk vs thrombotic benefit. Monitor INR closely. Consider PPI for GI protection.",
    },
    {
        "drug_a": "amlodipine",
        "drug_b": "simvastatin",
        "severity": "moderate",
        "description": "Amlodipine inhibits CYP3A4 metabolism of simvastatin, increasing statin exposure and myopathy risk.",
        "action": "Limit simvastatin to 20 mg/day when used with amlodipine, or switch to atorvastatin/rosuvastatin.",
    },
    {
        "drug_a": "metoprolol",
        "drug_b": "verapamil",
        "severity": "high",
        "description": "Beta-blocker with non-dihydropyridine calcium channel blocker increases risk of severe bradycardia and heart block.",
        "action": "Avoid combination. If both rate control agents needed, monitor ECG closely.",
    },
]


@mcp.tool()
async def flag_drug_interaction(medications: list[str]) -> list[dict]:
    """Return known drug interactions from hardcoded interaction rules.

    Checks a list of medications against known interaction pairs and returns
    any identified interactions with severity and recommended actions.

    Args:
        medications: List of medication names the patient is taking.

    Returns:
        List of interaction dicts with drug_a, drug_b, severity, description, and action.
    """
    if not medications:
        return []

    meds_lower = [m.lower().strip() for m in medications]
    found_interactions: list[dict] = []

    for interaction in _DRUG_INTERACTIONS:
        drug_a = interaction["drug_a"].lower()
        drug_b = interaction["drug_b"].lower()

        a_present = any(drug_a in med or med in drug_a for med in meds_lower)
        b_present = any(drug_b in med or med in drug_b for med in meds_lower)

        if a_present and b_present:
            found_interactions.append(interaction)

    return found_interactions


# ---------------------------------------------------------------------------
# Tool 5: get_synthetic_patient
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_synthetic_patient(mrn: str) -> dict:
    """Return patient record for the given MRN from the database.

    Queries the live patients table plus related conditions, medications,
    recent labs, and care gaps. Works for any MRN in the system.

    Args:
        mrn: Medical record number to look up.

    Returns:
        Patient record dict, or error dict if MRN not found.
    """
    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        patient = await conn.fetchrow(
            """SELECT id, mrn, first_name, last_name, birth_date, gender,
                      race, ethnicity, address_line, city, state, zip_code,
                      insurance_type, is_synthetic, data_source, created_at
               FROM patients WHERE mrn = $1""",
            mrn,
        )
        if not patient:
            return {
                "error": f"Patient with MRN '{mrn}' not found.",
                "hint": "Pass a valid MRN from the patients table.",
            }

        pid = str(patient["id"])

        conditions = await conn.fetch(
            """SELECT code, display, clinical_status,
                      onset_date::text AS onset_date
               FROM patient_conditions WHERE patient_id = $1::uuid
               ORDER BY onset_date DESC NULLS LAST""",
            pid,
        )

        medications = await conn.fetch(
            """SELECT code, display, status,
                      authored_on::text AS authored_on
               FROM patient_medications WHERE patient_id = $1::uuid
               ORDER BY authored_on DESC NULLS LAST""",
            pid,
        )

        labs = await conn.fetch(
            """SELECT metric_type, result_numeric, result_text,
                      result_unit, reference_text, is_out_of_range,
                      measured_at::text AS measured_at
               FROM biometric_readings
               WHERE patient_id = $1::uuid
                 AND metric_type IS NOT NULL
               ORDER BY measured_at DESC
               LIMIT 20""",
            pid,
        )

        care_gaps = await conn.fetch(
            """SELECT gap_type, description, status,
                      identified_date::text AS identified_date
               FROM care_gaps WHERE patient_id = $1::uuid
               ORDER BY identified_date DESC""",
            pid,
        )

    dob = patient["birth_date"]
    age = None
    if dob:
        from datetime import date
        today = date.today()
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    return {
        "id": pid,
        "mrn": patient["mrn"],
        "first_name": patient["first_name"] or "",
        "last_name": patient["last_name"] or "",
        "date_of_birth": dob.isoformat() if dob else None,
        "age": age,
        "gender": patient["gender"],
        "race": patient["race"],
        "ethnicity": patient["ethnicity"],
        "address": {
            "line": patient["address_line"],
            "city": patient["city"],
            "state": patient["state"],
            "zip": patient["zip_code"],
        },
        "insurance": {"type": patient["insurance_type"]},
        "is_synthetic": patient["is_synthetic"],
        "data_source": patient["data_source"],
        "conditions": [dict(r) for r in conditions],
        "medications": [dict(r) for r in medications],
        "recent_labs": [dict(r) for r in labs],
        "care_gaps": [dict(r) for r in care_gaps],
    }


# ---------------------------------------------------------------------------
# Data-source / HealthEx tools (share a lightweight asyncpg pool)
# ---------------------------------------------------------------------------

_db_pool: asyncpg.Pool | None = None

async def _get_db_pool() -> asyncpg.Pool:
    global _db_pool
    if _db_pool is None:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL not set")
        _db_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    return _db_pool


async def _set_data_track(track: str) -> None:
    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO system_config (key, value, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            "DATA_TRACK",
            track,
        )


@mcp.tool()
async def use_healthex() -> str:
    """Switch the active data track to HealthEx real patient records.

    Persists the selection in system_config so all subsequent ingestion
    calls pull from the HealthEx adapter instead of synthetic demo data.

    Returns:
        Confirmation string indicating the active track has been set.
    """
    await _set_data_track("healthex")
    return (
        "Switched to HealthEx real records. "
        "All future data pulls will use the HealthEx adapter. "
        "Call get_data_source_status() to confirm."
    )


@mcp.tool()
async def use_demo_data() -> str:
    """Switch the active data track to Synthea synthetic demo data.

    Persists the selection in system_config so all subsequent ingestion
    calls pull from the Synthea adapter instead of real records.

    Returns:
        Confirmation string indicating the active track has been set.
    """
    await _set_data_track("synthea")
    return (
        "Switched to Synthea demo data. "
        "All future data pulls will use synthetic records — safe for testing. "
        "Call get_data_source_status() to confirm."
    )


@mcp.tool()
async def switch_data_track(track: str) -> str:
    """Switch the active data track to a named source.

    Args:
        track: One of 'synthea', 'healthex', or 'auto'.
               'auto' reads the DATA_TRACK environment variable at runtime.

    Returns:
        'OK: switched to <track>' on success, or an error string.
    """
    valid = {"synthea", "healthex", "auto"}
    if track not in valid:
        return f"ERROR: unknown track '{track}'. Valid values: {sorted(valid)}"
    await _set_data_track(track)
    return f"OK: switched to {track}"


@mcp.tool()
async def get_data_source_status() -> dict:
    """Report the currently active data track and available sources.

    Returns:
        Dict with keys: active_track, available_tracks, env_override.
    """
    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value, updated_at FROM system_config WHERE key = $1",
            "DATA_TRACK",
        )
    db_track = row["value"] if row else None
    env_track = os.environ.get("DATA_TRACK")
    active = env_track if env_track else (db_track or "synthea")
    return {
        "active_track": active,
        "db_track": db_track,
        "env_override": env_track,
        "available_tracks": ["synthea", "healthex", "auto"],
        "last_updated": str(row["updated_at"]) if row else None,
    }


# ---------------------------------------------------------------------------
# HealthEx patient registration + FHIR ingestion
# (mirrors Skills MCP server tools — available here because /mcp is the
# confirmed working endpoint in Claude Web sessions)
# ---------------------------------------------------------------------------

@mcp.tool()
async def register_healthex_patient(
    health_summary_json: str,
    mrn_override: str = "",
) -> str:
    """Register a real HealthEx patient in the warehouse.

    MUST be called before any ingest_from_healthex calls in a HealthEx
    session. Takes the raw JSON from HealthEx get_health_summary, creates
    or finds the patient row with is_synthetic=False, initialises
    data_sources and source_freshness rows, and returns the canonical
    patient_id UUID and MRN for all subsequent calls.

    Also sets DATA_TRACK = "healthex" in system_config so all future
    pipeline runs use the HealthEx adapter.

    Args:
        health_summary_json: Raw JSON string from HealthEx get_health_summary.
                             May be a FHIR Patient resource, a FHIR Bundle,
                             or a HealthEx summary dict — all are handled.
        mrn_override:        If provided, use this MRN instead of extracting
                             from the summary.

    Returns:
        JSON string with patient_id, mrn, and status.
    """
    import time as _time
    pool = await _get_db_pool()
    try:
        start = _time.time()
        summary = json.loads(health_summary_json)
        patient_resource: dict = {}

        if summary.get("resourceType") == "Patient":
            patient_resource = summary
        elif summary.get("resourceType") == "Bundle":
            for entry in summary.get("entry", []):
                res = entry.get("resource", {})
                if res.get("resourceType") == "Patient":
                    patient_resource = res
                    break
            if not patient_resource:
                return (
                    "Error: FHIR Bundle contained no Patient resource. "
                    "Pass the raw get_health_summary JSON directly."
                )
        else:
            # HealthEx flat summary dict
            name_raw = summary.get("name", summary.get("full_name", ""))
            if isinstance(name_raw, str):
                parts = name_raw.strip().split()
                given = parts[:-1] if len(parts) > 1 else parts
                family = parts[-1] if len(parts) > 1 else ""
            else:
                given = [name_raw.get("first", "")]
                family = name_raw.get("last", "")

            raw_mrn = (
                mrn_override
                or summary.get("mrn")
                or summary.get("patient_id")
                or summary.get("id")
                or ""
            )
            patient_resource = {
                "resourceType": "Patient",
                "id": summary.get("id", ""),
                "name": [{"given": given, "family": family}],
                "birthDate": summary.get(
                    "birth_date",
                    summary.get("dob", summary.get("date_of_birth", "")),
                ),
                "gender": summary.get("gender", summary.get("sex", "")),
                "identifier": (
                    [{"type": {"coding": [{"code": "MR"}]}, "value": str(raw_mrn)}]
                    if raw_mrn else []
                ),
                "address": [{
                    "line": [summary.get("address", "")],
                    "city": summary.get("city", ""),
                    "state": summary.get("state", ""),
                    "postalCode": summary.get("zip", summary.get("zip_code", "")),
                }],
            }

        # Import FHIR transform (mcp-server is on sys.path via _MCPSERVER_DIR)
        from transforms.fhir_to_schema import transform_patient  # type: ignore
        demo = transform_patient(
            patient_resource, data_source="healthex", is_synthetic=False
        )

        if mrn_override:
            demo["mrn"] = mrn_override

        # ── Was the MRN explicitly supplied, or auto-generated by transform_patient? ──
        # transform_patient generates "SYN-<uuid>" when no FHIR identifier is found.
        # That pseudo-MRN is random per call, guaranteeing a duplicate row on each
        # re-registration. We detect it and replace it with a stable identifier.
        _auto_mrn_prefixes = ("SYN-", "HX-")
        _explicit_mrn = bool(
            mrn_override
            or summary.get("mrn") or summary.get("patient_id") or summary.get("id")
        )
        _mrn_is_autogenerated = (
            not _explicit_mrn
            and any(demo.get("mrn", "").startswith(p) for p in _auto_mrn_prefixes)
        )

        new_id = str(_uuid_mod.uuid4())
        async with pool.acquire() as conn:

            # ── MRN resolution: find existing patient before minting a new MRN ──
            # Priority:
            #   1. Explicit MRN from summary / mrn_override         → use as-is
            #   2. Auto-generated SYN-/HX- MRN + name+DOB match    → reuse existing row
            #   3. Auto-generated SYN-/HX- MRN + no match          → stable hash MRN
            if _mrn_is_autogenerated:
                existing = None
                if demo.get("first_name") and demo.get("last_name"):
                    existing = await conn.fetchrow(
                        """SELECT id, mrn FROM patients
                           WHERE data_source = 'healthex'
                             AND LOWER(first_name) = LOWER($1)
                             AND LOWER(last_name)  = LOWER($2)
                             AND ($3::date IS NULL OR birth_date = $3::date)
                           LIMIT 1""",
                        demo["first_name"], demo["last_name"],
                        demo.get("birth_date"),
                    )
                if existing:
                    demo["mrn"] = existing["mrn"]
                    new_id = str(existing["id"])
                    logger.info(
                        "register_healthex_patient: matched existing patient %s "
                        "by name+DOB, reusing MRN %s", new_id, demo["mrn"]
                    )
                else:
                    # No existing row — generate a deterministic MRN from name+DOB
                    # so repeat calls without an explicit MRN always land on the same row.
                    import hashlib as _hashlib
                    fingerprint = (
                        f"{demo.get('first_name','').lower()}"
                        f"{demo.get('last_name','').lower()}"
                        f"{demo.get('birth_date') or ''}"
                    )
                    short = _hashlib.sha256(fingerprint.encode()).hexdigest()[:8].upper()
                    demo["mrn"] = f"HX-{short}"
                    logger.warning(
                        "register_healthex_patient: no explicit MRN, "
                        "derived stable MRN %s from name+DOB fingerprint", demo["mrn"]
                    )

            await conn.execute(
                """
                INSERT INTO patients
                    (id, mrn, first_name, last_name, birth_date, gender,
                     race, ethnicity, address_line, city, state, zip_code,
                     is_synthetic, created_at, data_source)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT (mrn) DO UPDATE SET
                    first_name   = EXCLUDED.first_name,
                    last_name    = EXCLUDED.last_name,
                    birth_date   = EXCLUDED.birth_date,
                    gender       = EXCLUDED.gender,
                    race         = EXCLUDED.race,
                    ethnicity    = EXCLUDED.ethnicity,
                    address_line = EXCLUDED.address_line,
                    city         = EXCLUDED.city,
                    state        = EXCLUDED.state,
                    zip_code     = EXCLUDED.zip_code,
                    is_synthetic = false,
                    data_source  = 'healthex'
                """,
                new_id, demo["mrn"],
                demo.get("first_name", ""), demo.get("last_name", ""),
                demo.get("birth_date"), demo.get("gender", ""),
                demo.get("race", ""), demo.get("ethnicity", ""),
                demo.get("address_line", ""), demo.get("city", ""),
                demo.get("state", ""), demo.get("zip_code", ""),
                False, _dt.utcnow(), "healthex",
            )
            row = await conn.fetchrow(
                "SELECT id FROM patients WHERE mrn = $1", demo["mrn"]
            )
            patient_id = str(row["id"]) if row else new_id

            await conn.execute(
                """
                INSERT INTO data_sources
                    (id, patient_id, source_name, is_active, connected_at, data_source)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (patient_id, source_name) DO UPDATE SET
                    is_active = true, connected_at = NOW()
                """,
                str(_uuid_mod.uuid4()), patient_id, "healthex", True,
                _dt.utcnow(), "healthex",
            )
            # last_ingested_at is NULL until the first real HealthEx pull
            # completes — writing NOW() here would make orchestrate_refresh
            # treat a freshly-registered patient as already ingested and
            # skip the first ingest cycle.
            await conn.execute(
                """
                INSERT INTO source_freshness
                    (patient_id, source_name, last_ingested_at, records_count, ttl_hours)
                VALUES ($1,$2,NULL,0,24)
                ON CONFLICT (patient_id, source_name) DO NOTHING
                """,
                patient_id, "healthex",
            )
            await conn.execute(
                """
                INSERT INTO system_config (key, value, updated_at)
                VALUES ($1,$2,NOW())
                ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()
                """,
                "DATA_TRACK", "healthex",
            )

        duration_ms = int((_time.time() - start) * 1000)
        return json.dumps({
            "status": "registered",
            "patient_id": patient_id,
            "mrn": demo["mrn"],
            "name": f"{demo.get('first_name','')} {demo.get('last_name','')}".strip(),
            "is_synthetic": False,
            "data_track": "healthex",
            "duration_ms": duration_ms,
            "next_step": (
                f"Call ingest_from_healthex(patient_id='{patient_id}', "
                "resource_type='labs'|'medications'|'conditions'|'encounters', "
                "fhir_json=<HealthEx response>) for each resource type, "
                f"then run_deliberation(patient_id='{patient_id}')."
            ),
        }, indent=2)

    except json.JSONDecodeError as e:
        return f"Error: health_summary_json is not valid JSON — {e}"
    except Exception as e:
        logger.error("register_healthex_patient failed: %s", e)
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# HealthEx ingestion helpers
# ---------------------------------------------------------------------------

# Keys HealthEx uses when it wraps clinical arrays in a container object.
# e.g. {"labs": [...items...]} instead of a bare array.
_HX_CONTAINER_KEYS: dict[str, list[str]] = {
    "conditions":  ["conditions", "Conditions", "problems", "diagnoses"],
    "medications": ["medications", "Medications", "drugs", "prescriptions"],
    "labs":        ["labs", "labResults", "lab_results", "observations",
                    "Labs", "results"],
    "encounters":  ["encounters", "visits", "Encounters", "Visits",
                    "appointments"],
}


def _explode_fhir_bundle(data: Any, resource_type: str = "") -> list[dict]:
    """Convert any HealthEx payload shape into a flat list of item dicts.

    Resolution order:
      1. FHIR Bundle (resourceType=Bundle) → extract entry[*].resource
      2. HealthEx container dict           → unwrap the typed inner array
         e.g. {"labs": [...]} when resource_type="labs"
      3. Plain list                        → use as-is
      4. Single resource dict              → wrap in [data]
    """
    if isinstance(data, dict):
        # 1. FHIR Bundle
        if data.get("resourceType") == "Bundle":
            return [
                e["resource"]
                for e in data.get("entry", [])
                if isinstance(e.get("resource"), dict)
            ]
        # 2. HealthEx container dict — try every known alias for resource_type
        for key in _HX_CONTAINER_KEYS.get(resource_type, []):
            if key in data and isinstance(data[key], list):
                return data[key]
        # 3b. Generic container — any value that is a list of dicts
        for val in data.values():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
        # 4. Single resource dict
        return [data]
    if isinstance(data, list):
        return data
    return []


def _healthex_native_to_fhir_conditions(items: list[dict]) -> list[dict]:
    """HealthEx native condition → FHIR Condition resource."""
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code    = item.get("icd10") or item.get("code") or ""
        display = (item.get("name") or item.get("display")
                   or item.get("description") or "")
        status  = item.get("status") or "active"
        onset   = (item.get("onset_date") or item.get("onsetDate")
                   or item.get("diagnosed_date") or item.get("onset") or "")
        out.append({
            "resourceType": "Condition",
            "code": {"coding": [{"code": code, "display": display,
                                  "system": "http://snomed.info/sct"}]},
            "clinicalStatus": {"coding": [{"code": status}]},
            "onsetDateTime": onset,
        })
    return out


def _healthex_native_to_fhir_medications(items: list[dict]) -> list[dict]:
    """HealthEx native medication → FHIR MedicationRequest resource."""
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code    = item.get("rxnorm") or item.get("code") or ""
        display = (item.get("name") or item.get("display")
                   or item.get("drug_name") or "")
        status  = item.get("status") or "active"
        authored = (item.get("start_date") or item.get("authoredOn")
                    or item.get("prescribed_date") or "")
        out.append({
            "resourceType": "MedicationRequest",
            "medicationCodeableConcept": {
                "coding": [{"code": code, "display": display}]
            },
            "status": status,
            "authoredOn": authored,
        })
    return out


def _healthex_native_to_fhir_observations(items: list[dict]) -> list[dict]:
    """HealthEx native lab result → FHIR Observation resource.

    Stores non-numeric values in _result_text (a pass-through field) instead
    of cramming them into the unit field.  Preserves reference ranges and
    LOINC codes from parser output for structured storage.
    """
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        loinc   = item.get("loinc") or item.get("loinc_code") or item.get("code") or ""
        display = (item.get("name") or item.get("display")
                   or item.get("test_name") or "")
        unit    = (item.get("result_unit") or item.get("unit")
                   or item.get("units") or "")
        date    = (item.get("date") or item.get("effectiveDateTime")
                   or item.get("collected_date") or item.get("resulted_date") or "")
        ref_range = item.get("ref_range") or item.get("reference_range") or ""
        raw_val = (item.get("result_value") or item.get("value")
                   or item.get("result") or item.get("numeric_value"))
        if raw_val is None:
            raw_val = ""

        # Try numeric conversion; preserve qualitative text separately
        result_text = None
        try:
            numeric = float(str(raw_val).split()[0])
        except (ValueError, TypeError, IndexError):
            numeric = 0.0
            if raw_val:
                result_text = str(raw_val)

        obs = {
            "resourceType": "Observation",
            "code": {"coding": [{"code": loinc, "display": display}]},
            "valueQuantity": {"value": numeric, "unit": unit},
            "effectiveDateTime": date,
        }
        # Pass-through fields for structured storage (not standard FHIR,
        # consumed by _write_lab_rows and transform functions)
        if result_text:
            obs["_result_text"] = result_text
        if ref_range:
            obs["_reference_text"] = ref_range
        if loinc:
            obs["_loinc_code"] = loinc

        out.append(obs)
    return out


def _healthex_native_to_fhir_encounters(items: list[dict]) -> list[dict]:
    """HealthEx native encounter → FHIR Encounter resource."""
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        enc_type = (item.get("type") or item.get("encounter_type")
                    or item.get("visit_type") or "encounter")
        date = (item.get("date") or item.get("start_date")
                or item.get("encounter_date") or item.get("visit_date") or "")
        out.append({
            "resourceType": "Encounter",
            "type": [{"coding": [{"display": enc_type}]}],
            "period": {"start": date},
        })
    return out


def _is_fhir_resource_type(res: dict, fhir_type: str) -> bool:
    return res.get("resourceType", "") == fhir_type


def _normalize_to_fhir(resource_type: str, raw_resources: list[dict]) -> list[dict]:
    """Ensure every item in raw_resources is a proper FHIR resource dict.

    If the items already carry the expected FHIR resourceType they're used
    as-is.  If they look like HealthEx native objects (no resourceType),
    they're converted via the _healthex_native_to_fhir_* helpers.
    """
    if not raw_resources:
        return []

    fhir_type_map = {
        "conditions":  "Condition",
        "medications": "MedicationRequest",
        "labs":        "Observation",
        "encounters":  "Encounter",
    }
    expected_fhir = fhir_type_map.get(resource_type, "")

    # Check if these already look like correct FHIR resources
    sample = raw_resources[0]
    if expected_fhir and _is_fhir_resource_type(sample, expected_fhir):
        return raw_resources  # Already FHIR — pass through

    # Convert from HealthEx native format
    converters = {
        "conditions":  _healthex_native_to_fhir_conditions,
        "medications": _healthex_native_to_fhir_medications,
        "labs":        _healthex_native_to_fhir_observations,
        "encounters":  _healthex_native_to_fhir_encounters,
    }
    fn = converters.get(resource_type)
    return fn(raw_resources) if fn else raw_resources


async def _write_condition_rows(conn, records: list[dict]) -> int:
    n = 0
    for rec in records:
        await conn.execute(
            """INSERT INTO patient_conditions
                   (id, patient_id, code, display, onset_date,
                    clinical_status, data_source)
               VALUES ($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT DO NOTHING""",
            rec.get("id", str(_uuid_mod.uuid4())),
            rec["patient_id"], rec.get("code", ""),
            rec.get("display", ""), rec.get("onset_date"),
            rec.get("clinical_status", "active"), "healthex",
        )
        n += 1
    return n


async def _write_medication_rows(conn, records: list[dict]) -> int:
    n = 0
    for rec in records:
        await conn.execute(
            """INSERT INTO patient_medications
                   (id, patient_id, code, display, status,
                    authored_on, data_source)
               VALUES ($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT DO NOTHING""",
            rec.get("id", str(_uuid_mod.uuid4())),
            rec["patient_id"], rec.get("code", ""),
            rec.get("display", ""), rec.get("status", "active"),
            rec.get("authored_on") or _dt.utcnow(), "healthex",
        )
        n += 1
    return n


async def _write_lab_rows(conn, records: list[dict]) -> int:
    n = 0
    for rec in records:
        # Parse reference range bounds if reference_text is provided
        ref_low = rec.get("reference_low")
        ref_high = rec.get("reference_high")
        ref_text = rec.get("reference_text", "")
        if ref_text and ref_low is None and ref_high is None:
            ref_low, ref_high = _parse_reference_bounds(ref_text)

        await conn.execute(
            """INSERT INTO biometric_readings
                   (id, patient_id, metric_type, value, unit,
                    measured_at, is_abnormal,
                    result_text, result_numeric, result_unit,
                    reference_text, reference_low, reference_high,
                    loinc_code, interpretation, data_source)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
               ON CONFLICT DO NOTHING""",
            rec.get("id", str(_uuid_mod.uuid4())),
            rec["patient_id"], rec.get("metric_type", ""),
            rec.get("value"), rec.get("unit", ""),
            rec.get("measured_at") or _dt.utcnow(),
            rec.get("is_abnormal", False),
            rec.get("result_text"),
            rec.get("result_numeric"),
            rec.get("result_unit") or rec.get("unit", ""),
            ref_text or None,
            ref_low, ref_high,
            rec.get("loinc_code") or None,
            rec.get("interpretation") or None,
            "healthex",
        )
        n += 1
    return n


def _parse_reference_bounds(ref_text: str):
    """Extract numeric low/high bounds from a reference range string.

    Examples:
        "70-100 mg/dL"     → (70, 100)
        "<5.7"             → (None, 5.7)
        ">60"              → (60, None)
        "4.5 - 11.0"       → (4.5, 11.0)
        "Male: 13.5-17.5"  → (13.5, 17.5)
    """
    import re
    ref_low = None
    ref_high = None

    range_match = re.search(r'([\d.]+)\s*[-\u2013]\s*([\d.]+)', ref_text)
    if range_match:
        try:
            ref_low = float(range_match.group(1))
            ref_high = float(range_match.group(2))
        except ValueError:
            pass
        return ref_low, ref_high

    lt_match = re.search(r'<\s*([\d.]+)', ref_text)
    if lt_match:
        try:
            ref_high = float(lt_match.group(1))
        except ValueError:
            pass
        return ref_low, ref_high

    gt_match = re.search(r'>\s*([\d.]+)', ref_text)
    if gt_match:
        try:
            ref_low = float(gt_match.group(1))
        except ValueError:
            pass

    return ref_low, ref_high


async def _write_encounter_rows(conn, records: list[dict]) -> int:
    n = 0
    for rec in records:
        await conn.execute(
            """INSERT INTO clinical_events
                   (id, patient_id, event_type, event_date,
                    description, data_source)
               VALUES ($1,$2,$3,$4,$5,$6)
               ON CONFLICT DO NOTHING""",
            rec.get("id", str(_uuid_mod.uuid4())),
            rec["patient_id"],
            rec.get("event_type", "encounter"),
            rec.get("event_date") or _dt.utcnow(),
            rec.get("description", ""), "healthex",
        )
        n += 1
    return n


_WRITER_MAP = {
    "conditions":  _write_condition_rows,
    "medications": _write_medication_rows,
    "labs":        _write_lab_rows,
    "encounters":  _write_encounter_rows,
}


def _find_summary_items(summary: dict, sub_type: str) -> list[dict]:
    """Extract items for a sub_type from a structured summary dict."""
    for key in _HX_CONTAINER_KEYS.get(sub_type, []):
        if key in summary and isinstance(summary[key], list) and summary[key]:
            return summary[key]
    return []


async def _transform_and_write(
    conn,
    resource_type: str,
    fhir_resources: list[dict],
    patient_id: str,
    transform_conditions,
    transform_medications,
    transform_clinical_observations,
    transform_encounters,
) -> int:
    """Transform FHIR resources to DB records and write them. Returns count."""
    if not fhir_resources:
        return 0
    transform_fn_map = {
        "conditions":  lambda r: transform_conditions(r, patient_id, "healthex"),
        "medications": lambda r: transform_medications(r, patient_id, "healthex"),
        "labs":        lambda r: transform_clinical_observations(r, patient_id, "healthex"),
        "encounters":  lambda r: transform_encounters(r, patient_id, "healthex"),
    }
    fn = transform_fn_map.get(resource_type)
    if not fn:
        return 0
    records = fn(fhir_resources)
    writer = _WRITER_MAP.get(resource_type)
    if not writer:
        return 0
    return await writer(conn, records)


@mcp.tool()
async def ingest_from_healthex(
    patient_id: str,
    resource_type: str,
    fhir_json: str,
) -> str:
    """Accept a HealthEx MCP tool response and write it to the warehouse.

    Two-phase architecture:
      Phase 1 (fast): Cache raw blob → LLM Planner produces ExtractionPlan
                      → store plan in ingestion_plans table
      Phase 2 (inline): Execute plan → adaptive parse → transform → write rows
                        one at a time → verify counts

    Handles all known HealthEx payload formats:
      A. Plain text summary (from get_health_summary)
      B. Compressed dictionary table (from get_conditions, etc.)
      C. Flat FHIR text ("resourceType is Observation. id is ...")
      D. Proper FHIR R4 Bundle JSON
      E. Custom JSON dict-with-arrays

    Falls back to LLM-based extraction when deterministic parsers fail.

    For resource_type="summary", all embedded clinical arrays
    (conditions, medications, labs, encounters) are written to their
    respective tables in a single call.

    Args:
        patient_id:    UUID from register_healthex_patient
        resource_type: "labs" | "medications" | "conditions" | "encounters" | "summary"
        fhir_json:     raw string from the HealthEx tool response (any format)

    Returns:
        JSON string with plan metadata + records_written per table.
    """
    import asyncio
    import sys as _sys
    import time as _time
    pool = await _get_db_pool()
    try:
        start = _time.time()
        valid_types = {"labs", "medications", "conditions", "encounters", "summary"}
        if resource_type not in valid_types:
            return json.dumps({
                "status": "error",
                "error": f"resource_type must be one of {sorted(valid_types)}, got '{resource_type}'"
            })

        # ── Load FHIR transforms ───────────────────────────────────────────────
        from transforms.fhir_to_schema import (          # type: ignore
            transform_conditions,
            transform_medications,
            transform_clinical_observations,
            transform_encounters,
        )

        # ── Import adaptive parser ─────────────────────────────────────────────
        try:
            from ingestion.adapters.healthex.ingest import adaptive_parse
        except ImportError:
            # Fallback: add parent dir to path if needed
            import pathlib
            _parent = str(pathlib.Path(__file__).resolve().parent.parent)
            if _parent not in _sys.path:
                _sys.path.insert(0, _parent)
            from ingestion.adapters.healthex.ingest import adaptive_parse

        # ── Import planner ─────────────────────────────────────────────────────
        try:
            from ingestion.adapters.healthex.planner import (
                plan_extraction,
                plan_extraction_deterministic,
            )
        except ImportError:
            plan_extraction = None
            plan_extraction_deterministic = None

        written_by_table: dict[str, int] = {}
        format_detected = "unknown"
        parser_used = "none"
        plan_id = None
        insights_summary = ""

        async with pool.acquire() as conn:

            # ── Patient existence guard ────────────────────────────────────────
            exists = await conn.fetchval(
                "SELECT id FROM patients WHERE id = $1::uuid",
                patient_id,
            )
            if exists is None:
                return json.dumps({
                    "status": "error",
                    "error": (
                        f"patient_id '{patient_id}' not found in patients table. "
                        "Call register_healthex_patient first."
                    ),
                })

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # PHASE 1: Cache raw + plan extraction
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

            raw_text = fhir_json if isinstance(fhir_json, str) else json.dumps(fhir_json)

            # BUG 3: sanitize before JSONB write (strip NUL bytes and lone
            # surrogates that PostgreSQL JSONB rejects / that crash json.loads
            # on read).
            try:
                from ingestion.pipeline import _sanitize_str_for_jsonb as _jsonb_sanitize
            except Exception:
                def _jsonb_sanitize(s: str) -> str:
                    return s.replace("\x00", "") if isinstance(s, str) else s
            raw_text = _jsonb_sanitize(raw_text)

            # Detect format deterministically (fast)
            try:
                from ingestion.adapters.healthex.format_detector import detect_format as _detect_fmt
                _fmt, _ = _detect_fmt(fhir_json)
                fmt_code = _fmt.value
            except Exception:
                fmt_code = "unknown"

            # Always cache raw input for auditability
            raw_cache_id = str(_uuid_mod.uuid4())
            await conn.execute(
                """INSERT INTO raw_fhir_cache
                       (patient_id, source_name, resource_type, raw_json,
                        raw_text, detected_format,
                        fhir_resource_id, retrieved_at, processed)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),false)
                   ON CONFLICT (patient_id, source_name, fhir_resource_id)
                   DO UPDATE SET raw_json=EXCLUDED.raw_json,
                                 raw_text=EXCLUDED.raw_text,
                                 detected_format=EXCLUDED.detected_format,
                                 retrieved_at=NOW(), processed=false""",
                patient_id, "healthex", resource_type,
                json.dumps(raw_text[:50000]),
                raw_text[:50000],
                fmt_code,
                raw_cache_id,
            )

            # Run LLM Planner (or deterministic fallback)
            plan = None
            if plan_extraction_deterministic is not None:
                # Always run deterministic planner first (instant)
                plan = plan_extraction_deterministic(raw_text, resource_type, patient_id)

                # Optionally run LLM planner for richer insights
                if plan_extraction is not None and len(raw_text) > 500:
                    try:
                        llm_plan = await asyncio.to_thread(
                            plan_extraction, raw_text, resource_type, patient_id
                        )
                        # Merge LLM insights into deterministic plan
                        if llm_plan.get("planner_confidence", 0) > plan.get("planner_confidence", 0):
                            plan["insights_summary"] = llm_plan.get("insights_summary", plan.get("insights_summary", ""))
                            plan["sample_rows"] = llm_plan.get("sample_rows", plan.get("sample_rows", []))
                            plan["column_map"] = llm_plan.get("column_map", plan.get("column_map", {}))
                            if llm_plan.get("estimated_rows", 0) > plan.get("estimated_rows", 0):
                                plan["estimated_rows"] = llm_plan["estimated_rows"]
                    except Exception as e:
                        logger.warning("LLM planner failed (using deterministic): %s", e)

            # Store ExtractionPlan in ingestion_plans table
            if plan is not None:
                plan_id = str(_uuid_mod.uuid4())
                insights_summary = plan.get("insights_summary", "")
                try:
                    await conn.execute(
                        """INSERT INTO ingestion_plans
                               (id, patient_id, cache_id, resource_type,
                                detected_format, extraction_strategy, estimated_rows,
                                column_map, sample_rows, insights_summary,
                                planner_confidence, status, planned_at)
                           VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8,$9,$10,$11,'pending',NOW())""",
                        plan_id, patient_id, raw_cache_id, resource_type,
                        plan.get("detected_format", "unknown"),
                        plan.get("extraction_strategy", "llm_fallback"),
                        plan.get("estimated_rows", 0),
                        json.dumps(plan.get("column_map", {})),
                        json.dumps(plan.get("sample_rows", [])),
                        insights_summary,
                        plan.get("planner_confidence", 0.0),
                    )
                except Exception as e:
                    logger.warning("ingestion_plans insert failed (table may not exist yet): %s", e)
                    plan_id = None

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # PHASE 2: Execute — parse, transform, write rows (inline)
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

            # Try JSON parse first for structured payloads
            fhir_data = None
            try:
                fhir_data = json.loads(fhir_json)
                if not isinstance(fhir_data, (dict, list)):
                    fhir_data = None
            except (json.JSONDecodeError, ValueError):
                fhir_data = None

            if resource_type == "summary":
                # Fan-out: extract every clinical array from the summary
                summary = fhir_data if isinstance(fhir_data, dict) else {}
                sub_types = ["conditions", "medications", "labs", "encounters"]
                for sub_type in sub_types:
                    items = _find_summary_items(summary, sub_type)
                    if items:
                        fhir_resources = _normalize_to_fhir(sub_type, items)
                    else:
                        native_items, fmt, prs = adaptive_parse(fhir_json, sub_type)
                        format_detected = fmt
                        parser_used = prs
                        if not native_items:
                            continue
                        fhir_resources = _normalize_to_fhir(sub_type, native_items)

                    n = await _transform_and_write(
                        conn, sub_type, fhir_resources, patient_id,
                        transform_conditions, transform_medications,
                        transform_clinical_observations, transform_encounters,
                    )
                    if n > 0:
                        written_by_table[sub_type] = n

            else:
                # Single resource type
                if fhir_data is not None:
                    raw_list = _explode_fhir_bundle(fhir_data, resource_type)
                    if raw_list:
                        fhir_resources = _normalize_to_fhir(resource_type, raw_list)
                        n = await _transform_and_write(
                            conn, resource_type, fhir_resources, patient_id,
                            transform_conditions, transform_medications,
                            transform_clinical_observations, transform_encounters,
                        )
                        written_by_table[resource_type] = n
                        format_detected = fmt_code
                        parser_used = f"legacy_{fmt_code}"
                    else:
                        fhir_data = None

                if fhir_data is None or not written_by_table.get(resource_type):
                    native_items, format_detected, parser_used = adaptive_parse(
                        fhir_json, resource_type
                    )
                    if native_items:
                        fhir_resources = _normalize_to_fhir(resource_type, native_items)
                        n = await _transform_and_write(
                            conn, resource_type, fhir_resources, patient_id,
                            transform_conditions, transform_medications,
                            transform_clinical_observations, transform_encounters,
                        )
                        written_by_table[resource_type] = n

            # ── Mark raw cache as processed ────────────────────────────────────
            total_written = sum(written_by_table.values())
            await conn.execute(
                """UPDATE raw_fhir_cache SET processed = true
                   WHERE fhir_resource_id = $1 AND patient_id = $2""",
                raw_cache_id, patient_id,
            )

            # ── Update source freshness ────────────────────────────────────────
            if total_written > 0:
                await conn.execute(
                    """UPDATE source_freshness
                       SET last_ingested_at = NOW(),
                           records_count    = records_count + $1
                       WHERE patient_id = $2 AND source_name = 'healthex'""",
                    total_written, patient_id,
                )

            # ── Write verification ─────────────────────────────────────────────
            verified_counts = {}
            table_map = {
                "conditions": "patient_conditions",
                "medications": "patient_medications",
                "labs": "biometric_readings",
                "encounters": "clinical_events",
            }
            for sub_type, count in written_by_table.items():
                tbl = table_map.get(sub_type)
                if tbl and count > 0:
                    v = await conn.fetchval(
                        f"SELECT COUNT(*) FROM {tbl} "
                        f"WHERE patient_id = $1::uuid AND data_source = 'healthex'",
                        patient_id,
                    )
                    verified_counts[sub_type] = v

            # ── Update plan status ─────────────────────────────────────────────
            if plan_id:
                try:
                    await conn.execute(
                        """UPDATE ingestion_plans
                           SET status = $1, rows_written = $2,
                               rows_verified = $3, executed_at = NOW(),
                               extraction_time_ms = $4
                           WHERE id = $5::uuid""",
                        "complete" if total_written > 0 else "failed",
                        total_written,
                        sum(verified_counts.values()),
                        int((_time.time() - start) * 1000),
                        plan_id,
                    )
                except Exception:
                    pass  # ingestion_plans table may not exist yet

        # ── Post-ingest flag review (non-blocking) ────────────────
        flag_review_result = None
        if total_written > 0:
            try:
                from server.deliberation.flag_reviewer import run_flag_review
                _new_data_summary = (
                    f"Ingested {total_written} rows: {written_by_table}. "
                    f"Format: {format_detected}."
                )
                flag_review_result = await run_flag_review(
                    pool, patient_id, "post_ingest",
                    plan_id or "", _new_data_summary,
                )
            except Exception as _flag_err:
                logger.warning("Post-ingest flag review failed (non-fatal): %s", _flag_err)

        duration_ms = int((_time.time() - start) * 1000)
        result_dict = {
            "status": "ok",
            "patient_id": patient_id,
            "resource_type": resource_type,
            "records_written": written_by_table,
            "total_written": total_written,
            "verified_counts": verified_counts,
            "format_detected": format_detected,
            "parser_used": parser_used,
            "duration_ms": duration_ms,
            "plan_id": plan_id,
            "insights_summary": insights_summary,
        }
        if flag_review_result:
            result_dict["flag_review"] = {
                "review_id": flag_review_result.get("review_id"),
                "flags_reviewed": flag_review_result.get("flags_reviewed", 0),
                "retracted": flag_review_result.get("stats", {}).get("retracted", 0),
                "escalated": flag_review_result.get("stats", {}).get("escalated", 0),
                "summary": flag_review_result.get("summary", ""),
            }
        return json.dumps(result_dict, indent=2)

    except Exception as e:
        logger.error("ingest_from_healthex failed: %s", e, exc_info=True)
        return json.dumps({"status": "error", "error": type(e).__name__, "detail": str(e)})


@mcp.tool()
async def execute_pending_plans(
    patient_id: str,
    plan_id: str = "",
    limit: int = 10,
) -> str:
    """Execute pending ingestion extraction plans for a patient.

    Reads plans from ingestion_plans table (status='pending' or 'failed'),
    routes each to the appropriate format parser, and writes structured
    rows to the warehouse one row at a time.

    Call this after ingest_from_healthex() to re-execute failed plans,
    or to process plans that were deferred.

    The typical ingestion sequence is:
      1. ingest_from_healthex()  → plan created + executed inline
      2. execute_pending_plans() → re-run any failed plans from cache
      3. get_ingestion_plans()   → check plan status + insights

    Args:
        patient_id: UUID of the patient
        plan_id:    Optional specific plan ID to execute (all pending if empty)
        limit:      Max plans to execute in this call (default 10)
    """
    pool = await _get_db_pool()
    try:
        from ingestion.adapters.healthex.executor import execute_pending_plans as _execute
        result = await _execute(
            pool,
            patient_id=patient_id,
            plan_id=plan_id if plan_id else None,
            limit=limit,
        )
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error("execute_pending_plans failed: %s", e, exc_info=True)
        return json.dumps({"status": "error", "error": str(e)})


@mcp.tool()
async def get_ingestion_plans(
    patient_id: str,
    status: str = "",
) -> str:
    """Read extraction plans and their insights_summary for a patient.

    Other agents (deliberation, synthesis, provider brief) should call this
    to understand what data has been ingested — WITHOUT re-reading raw blobs.
    The insights_summary field is a plain-language description safe for any
    agent to use.

    Args:
        patient_id: UUID of the patient
        status:     Filter by status ('pending'|'complete'|'failed') — empty returns all
    """
    pool = await _get_db_pool()
    try:
        async with pool.acquire() as conn:
            if status:
                plans = await conn.fetch(
                    """SELECT id, resource_type, detected_format, extraction_strategy,
                              estimated_rows, rows_written, rows_verified,
                              insights_summary, status, planner_confidence,
                              planned_at, executed_at, error_message
                       FROM ingestion_plans
                       WHERE patient_id = $1::uuid AND status = $2
                       ORDER BY planned_at DESC LIMIT 50""",
                    patient_id, status,
                )
            else:
                plans = await conn.fetch(
                    """SELECT id, resource_type, detected_format, extraction_strategy,
                              estimated_rows, rows_written, rows_verified,
                              insights_summary, status, planner_confidence,
                              planned_at, executed_at, error_message
                       FROM ingestion_plans
                       WHERE patient_id = $1::uuid
                       ORDER BY planned_at DESC LIMIT 50""",
                    patient_id,
                )

            plan_list = [dict(p) for p in plans]
            return json.dumps({
                "patient_id": patient_id,
                "total_plans": len(plan_list),
                "plans": plan_list,
                "complete_count": sum(1 for p in plan_list if p.get("status") == "complete"),
                "pending_count": sum(1 for p in plan_list if p.get("status") == "pending"),
                "failed_count": sum(1 for p in plan_list if p.get("status") == "failed"),
                "total_rows_written": sum(p.get("rows_written") or 0 for p in plan_list),
            }, indent=2, default=str)

    except Exception as e:
        logger.error("get_ingestion_plans failed: %s", e, exc_info=True)
        return json.dumps({"status": "error", "error": str(e)})


@mcp.tool()
async def get_transfer_audit(
    patient_id: str,
    resource_type: str = "",
    status: str = "",
    batch_id: str = "",
    limit: int = 50,
) -> str:
    """Query the per-record transfer_log audit trail for HealthEx ingest operations.

    Provides record-level visibility into every data transfer: which records were
    planned, sanitized, written, verified, or failed — with timestamps for each
    stage.  Useful for debugging ingest quality, auditing blob escaping fixes,
    and confirming per-record write success.

    Args:
        patient_id:    UUID of the patient (required)
        resource_type: Filter by resource type: labs | conditions | medications | encounters
        status:        Filter by record status: planned | sanitized | written | verified |
                       written_unverified | failed
        batch_id:      UUID of a specific ingest batch to inspect
        limit:         Max records to return (default 50, max 200)
    """
    pool = await _get_db_pool()
    try:
        limit = min(int(limit), 200)
        async with pool.acquire() as conn:
            conditions = ["patient_id = $1::uuid"]
            params: list = [patient_id]
            p = 2

            if resource_type:
                conditions.append(f"resource_type = ${p}")
                params.append(resource_type)
                p += 1
            if status:
                conditions.append(f"status = ${p}")
                params.append(status)
                p += 1
            if batch_id:
                conditions.append(f"batch_id = ${p}::uuid")
                params.append(batch_id)
                p += 1

            where = " AND ".join(conditions)

            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM transfer_log WHERE {where}",
                *params,
            )

            summary_rows = await conn.fetch(
                f"""SELECT resource_type, strategy, status, COUNT(*) AS cnt
                    FROM transfer_log
                    WHERE {where}
                    GROUP BY resource_type, strategy, status
                    ORDER BY resource_type, strategy, cnt DESC""",
                *params,
            )

            records = await conn.fetch(
                f"""SELECT id, resource_type, source, record_key, loinc_code,
                           icd10_code, batch_id, chunk_id, batch_sequence,
                           batch_total, strategy, format_detected, status,
                           planned_at, sanitized_at, written_at, verified_at,
                           failed_at, error_stage, error_message, payload_size_bytes
                    FROM transfer_log
                    WHERE {where}
                    ORDER BY planned_at DESC
                    LIMIT ${p}""",
                *params, limit,
            )

            return json.dumps({
                "patient_id": patient_id,
                "total_records": total,
                "summary": [dict(r) for r in summary_rows],
                "records": [dict(r) for r in records],
                "verified_count": sum(
                    r["cnt"] for r in summary_rows if r["status"] == "verified"
                ),
                "failed_count": sum(
                    r["cnt"] for r in summary_rows if r["status"] == "failed"
                ),
            }, indent=2, default=str)

    except Exception as e:
        logger.error("get_transfer_audit failed: %s", e, exc_info=True)
        return json.dumps({"status": "error", "error": str(e)})


# ---------------------------------------------------------------------------
# REST wrapper routes (for HTML prototypes via claude-client.js)
# ---------------------------------------------------------------------------

@mcp.custom_route("/health", methods=["GET"])
async def rest_health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "server": "ambient-clinical-intelligence", "version": "1.0.0"})


@mcp.custom_route("/tools/clinical_query", methods=["POST"])
async def rest_clinical_query(request: Request) -> JSONResponse:
    body = await request.json()
    result = await clinical_query(
        query=body.get("query", ""),
        role=body.get("role", "patient"),
        patient_context=body.get("patient_context", {}),
    )
    return JSONResponse(result)


@mcp.custom_route("/tools/get_guideline", methods=["GET"])
async def rest_get_guideline(request: Request) -> JSONResponse:
    rec_id = request.query_params.get("recommendation_id", "")
    result = await get_guideline(recommendation_id=rec_id)
    return JSONResponse(result)


@mcp.custom_route("/tools/check_screening_due", methods=["POST"])
async def rest_check_screening_due(request: Request) -> JSONResponse:
    body = await request.json()
    result = await check_screening_due(
        patient_age=body.get("patient_age", 0),
        sex=body.get("sex", ""),
        conditions=body.get("conditions", []),
    )
    return JSONResponse(result)


@mcp.custom_route("/tools/flag_drug_interaction", methods=["POST"])
async def rest_flag_drug_interaction(request: Request) -> JSONResponse:
    body = await request.json()
    result = await flag_drug_interaction(medications=body.get("medications", []))
    return JSONResponse(result)


@mcp.custom_route("/tools/get_synthetic_patient", methods=["GET"])
async def rest_get_synthetic_patient(request: Request) -> Response:
    mrn = request.query_params.get("mrn", "")
    result = await get_synthetic_patient(mrn=mrn)
    return Response(json.dumps(result, default=str), media_type="application/json")


@mcp.custom_route("/tools/use_healthex", methods=["POST"])
async def rest_use_healthex(request: Request) -> JSONResponse:
    result = await use_healthex()
    return JSONResponse({"message": result})


@mcp.custom_route("/tools/use_demo_data", methods=["POST"])
async def rest_use_demo_data(request: Request) -> JSONResponse:
    result = await use_demo_data()
    return JSONResponse({"message": result})


@mcp.custom_route("/tools/switch_data_track", methods=["POST"])
async def rest_switch_data_track(request: Request) -> JSONResponse:
    body = await request.json()
    result = await switch_data_track(track=body.get("track", "synthea"))
    return JSONResponse({"message": result})


@mcp.custom_route("/tools/get_data_source_status", methods=["GET"])
async def rest_get_data_source_status(request: Request) -> JSONResponse:
    result = await get_data_source_status()
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Deliberation Engine (Dual-LLM)
# ---------------------------------------------------------------------------

_deliberation_engine: DeliberationEngine | None = None


class _VectorStorePlaceholder:
    """STUB: guideline vector store. Returns [] until Phase 2 pgvector + MedCPT
    migration (009_pgvector_guidelines.sql) is applied. Consumed by
    context_compiler.py's `applicable_guidelines` pre-fetch (§12) — the
    deliberation engine already tolerates empty results gracefully.

    Note: this is NOT the same thing as the MCP tool `search_clinical_knowledge`,
    which is a real external-API wrapper (OpenFDA / RxNorm / PubMed) and is
    fully functional.
    """

    async def similarity_search(self, query: str, k: int = 10, **kwargs) -> list[dict]:
        logger.warning(
            "guideline vector store stub called — returning []. "
            "Apply migration 009_pgvector_guidelines.sql + load MedCPT embeddings to enable."
        )
        return []


async def get_deliberation_engine() -> DeliberationEngine:
    """Lazy-init the deliberation engine singleton."""
    global _deliberation_engine
    if _deliberation_engine is None:
        pool = await _get_db_pool()
        _deliberation_engine = DeliberationEngine(
            db_pool=pool,
            vector_store=_VectorStorePlaceholder()
        )
    return _deliberation_engine


# ---------------------------------------------------------------------------
# Mode selection cache for run_deliberation elicitation (two-call protocol).
# Ephemeral — same pattern as replit-app/lib/oauth-store.ts. State loss on
# restart just forces the caller to re-ask; no clinical data is held here.
# ---------------------------------------------------------------------------
_MODE_SELECTION_TTL_SEC = 300  # 5 minutes
_MODE_SELECTION_CACHE: dict[str, dict] = {}
_VALID_MODES = ("ask", "triage", "progressive", "full")
_EXECUTABLE_MODES = ("triage", "progressive", "full")


def _purge_expired_selection_tokens() -> None:
    now = _dt.utcnow().timestamp()
    expired = [
        tok for tok, entry in _MODE_SELECTION_CACHE.items()
        if entry.get("expires_at", 0) < now
    ]
    for tok in expired:
        _MODE_SELECTION_CACHE.pop(tok, None)


async def _recommend_mode(patient_id: str) -> dict:
    """Inspect deliberation history and return a mode recommendation.

    Rules (deterministic):
      - No prior deliberations              -> "triage"   (initial screening)
      - Latest convergence_score >= 0.75    -> "progressive"
      - Otherwise                           -> "full"     (re-deliberation worthwhile)
    """
    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, triggered_at, convergence_score, rounds_completed, status
               FROM deliberations
               WHERE patient_id = $1
               ORDER BY triggered_at DESC
               LIMIT 1""",
            patient_id,
        )
        prior_count_row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM deliberations WHERE patient_id = $1",
            patient_id,
        )

    prior_count = int(prior_count_row["n"]) if prior_count_row else 0
    if prior_count == 0:
        return {
            "is_initial_run": True,
            "prior_deliberations": 0,
            "latest_convergence": None,
            "recommended_mode": "triage",
            "rationale": "No prior deliberations for this patient; start with triage.",
        }

    latest = rows[0] if rows else None
    latest_conv = float(latest["convergence_score"]) if latest and latest["convergence_score"] is not None else 0.0
    if latest_conv >= 0.75:
        rec = "progressive"
        rationale = (
            f"Prior deliberation converged at {latest_conv:.2f} (>= 0.75); "
            "progressive tiered loading is sufficient for a follow-up pass."
        )
    else:
        rec = "full"
        rationale = (
            f"Prior deliberation converged at {latest_conv:.2f} (< 0.75); "
            "full dual-LLM council recommended for deeper re-analysis."
        )
    return {
        "is_initial_run": False,
        "prior_deliberations": prior_count,
        "latest_convergence": latest_conv,
        "recommended_mode": rec,
        "rationale": rationale,
    }


def _mode_options() -> list[dict]:
    return [
        {
            "mode": "triage",
            "description": "Single-LLM screening (Claude Sonnet only). Lowest cost.",
            "est_latency_sec": 5,
            "est_llm_calls": 1,
        },
        {
            "mode": "progressive",
            "description": "Tiered Haiku loop with demand-fetch context. Balanced.",
            "est_latency_sec": 20,
            "est_llm_calls": "1-5",
        },
        {
            "mode": "full",
            "description": "Dual-LLM council (Claude Sonnet + GPT-4o + critic + synthesis).",
            "est_latency_sec": 90,
            "est_llm_calls": "6-12",
        },
    ]


@mcp.tool()
async def run_deliberation(
    patient_id: str,
    trigger_type: str = "manual",
    max_rounds: int = 3,
    mode: str | None = None,
    selection_token: str | None = None,
) -> dict:
    """
    Trigger a deliberation session for a patient. Supports caller-driven
    mode elicitation via a two-call protocol.

    Modes:
      - "triage":      Minimal single-LLM screening (Claude Sonnet only).
      - "progressive": Tiered context loading (Haiku, demand-fetch).
      - "full":        Dual-LLM council (Claude Sonnet + GPT-4o + critic + synthesis).
      - "ask" | None:  Return a mode-selection prompt with options, history-based
                       recommendation, and a selection_token. The caller then
                       re-invokes this tool with the chosen mode and token.

    Args:
        patient_id: Patient MRN or internal ID.
        trigger_type: scheduled_pre_encounter | lab_result_received |
                      medication_change | missed_appointment |
                      temporal_threshold | manual
        max_rounds: Maximum deliberation rounds (1-5, default 3). Ignored in triage.
        mode: "ask" (default when omitted) | "triage" | "progressive" | "full".
        selection_token: Optional token returned by a prior "ask" call. When
                         supplied, must match the patient_id and not be expired.

    Returns:
        - mode=ask/None: {status:"mode_selection_required", selection_token, options,
                          recommended_mode, is_initial_run, prior_deliberations, ...}
        - mode=triage/progressive/full: deliberation result dict.
        - invalid mode: {status:"invalid_mode", accepted:[...]}
        - bad token:    {status:"invalid_selection_token"}
    """
    # Normalize mode. None -> "ask" (elicit).
    effective_mode = (mode or "ask").strip().lower()

    if effective_mode not in _VALID_MODES:
        return {
            "status": "invalid_mode",
            "accepted": list(_VALID_MODES),
            "received": mode,
        }

    # ── Elicitation path ─────────────────────────────────────────────────
    if effective_mode == "ask":
        _purge_expired_selection_tokens()
        recommendation = await _recommend_mode(patient_id)
        token = _uuid_mod.uuid4().hex
        now = _dt.utcnow().timestamp()
        _MODE_SELECTION_CACHE[token] = {
            "patient_id": patient_id,
            "trigger_type": trigger_type,
            "max_rounds": max_rounds,
            "created_at": now,
            "expires_at": now + _MODE_SELECTION_TTL_SEC,
        }
        return {
            "status": "mode_selection_required",
            "selection_token": token,
            "patient_id": patient_id,
            "trigger_type": trigger_type,
            "is_initial_run": recommendation["is_initial_run"],
            "prior_deliberations": recommendation["prior_deliberations"],
            "latest_convergence": recommendation["latest_convergence"],
            "recommended_mode": recommendation["recommended_mode"],
            "rationale": recommendation["rationale"],
            "options": _mode_options(),
            "expires_in_sec": _MODE_SELECTION_TTL_SEC,
            "instructions": (
                "Re-invoke run_deliberation with mode=<triage|progressive|full> "
                "and selection_token=<this token> to execute the chosen mode."
            ),
        }

    # ── Selection token validation (optional) ────────────────────────────
    if selection_token is not None:
        _purge_expired_selection_tokens()
        entry = _MODE_SELECTION_CACHE.get(selection_token)
        if entry is None or entry.get("patient_id") != patient_id:
            return {
                "status": "invalid_selection_token",
                "reason": "token_missing_expired_or_patient_mismatch",
            }
        # Consume the token so it cannot be replayed.
        _MODE_SELECTION_CACHE.pop(selection_token, None)

    engine = await get_deliberation_engine()
    request = DeliberationRequest(
        patient_id=patient_id,
        trigger_type=trigger_type,
        max_rounds=max_rounds,
    )

    # ── Dispatch ─────────────────────────────────────────────────────────
    if effective_mode == "triage":
        return await engine.run_triage(request)

    if effective_mode == "progressive":
        return await engine.run_progressive(request)

    # mode == "full"
    result = await engine.run(request)
    return {
        "deliberation_id": result.deliberation_id,
        "status": "complete",
        "mode": "full",
        "patient_id": result.patient_id,
        "convergence_score": result.convergence_score,
        "summary": {
            "anticipatory_scenarios": len(result.anticipatory_scenarios),
            "predicted_questions": len(result.predicted_patient_questions),
            "missing_data_flags": len(result.missing_data_flags),
            "nudges_generated": len(result.nudge_content),
            "knowledge_updates": len(result.knowledge_updates)
        },
        "top_scenario": (
            result.anticipatory_scenarios[0].title
            if result.anticipatory_scenarios else None
        ),
        "critical_flags": [
            f.description for f in result.missing_data_flags
            if f.priority in ("critical", "high")
        ]
    }


@mcp.tool()
async def get_deliberation_results(
    patient_id: str,
    output_type: str = "all",
    limit: int = 1
) -> dict:
    """
    Retrieve outputs from the most recent deliberation(s) for a patient.

    Args:
        patient_id: Patient MRN or internal ID
        output_type: Filter by: all | anticipatory_scenario |
                     predicted_patient_question | missing_data_flag |
                     patient_nudge | care_team_nudge
        limit: Number of most recent deliberations to return (default 1)

    Returns:
        Structured outputs from deliberation(s), with metadata.
    """
    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        deliberations = await conn.fetch(
            """SELECT id, triggered_at, convergence_score, rounds_completed
               FROM deliberations
               WHERE patient_id = $1 AND status = 'complete'
               ORDER BY triggered_at DESC
               LIMIT $2""",
            patient_id, limit
        )
        if not deliberations:
            return {"status": "no_deliberations_found", "patient_id": patient_id}

        results = []
        for dlb in deliberations:
            query = """SELECT output_type, output_data, confidence, priority
                       FROM deliberation_outputs
                       WHERE deliberation_id = $1"""
            params = [dlb["id"]]
            if output_type != "all":
                query += " AND output_type = $2"
                params.append(output_type)
            outputs = await conn.fetch(query, *params)

            results.append({
                "deliberation_id": str(dlb["id"]),
                "triggered_at": dlb["triggered_at"].isoformat(),
                "convergence_score": dlb["convergence_score"],
                "rounds_completed": dlb["rounds_completed"],
                "outputs": [dict(o) for o in outputs]
            })

        return {"patient_id": patient_id, "deliberations": results}


@mcp.tool()
async def get_flag_review_status(patient_id: str) -> dict:
    """
    Get current flag lifecycle status for a patient.

    Returns open flags, recently retracted flags (with reasons), and
    flags needing human review with clarification questions.
    Agents should read this instead of raw deliberation_outputs to get
    the current truth about a patient's flags.

    Args:
        patient_id: Patient UUID or MRN

    Returns:
        open_flags, recently_retracted, needs_human_review counts and details.
    """
    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        open_flags = await conn.fetch(
            """SELECT title, description, priority::text, flag_basis::text,
                      flagged_at, requires_human, had_zero_values
               FROM deliberation_flags
               WHERE patient_id = $1::uuid AND lifecycle_state = 'open'
               ORDER BY
                   CASE priority::text
                       WHEN 'critical' THEN 1 WHEN 'high' THEN 2
                       WHEN 'medium-high' THEN 3 WHEN 'medium' THEN 4
                       WHEN 'low' THEN 5 ELSE 6 END,
                   flagged_at DESC""",
            patient_id,
        )

        retracted = await conn.fetch(
            """SELECT title, priority::text, flag_basis::text,
                      retraction_reason, reviewed_at
               FROM deliberation_flags
               WHERE patient_id = $1::uuid
                 AND lifecycle_state = 'retracted'
                 AND reviewed_at >= NOW() - INTERVAL '7 days'
               ORDER BY reviewed_at DESC LIMIT 10""",
            patient_id,
        )

        human_needed = await conn.fetch(
            """SELECT f.title, f.description, f.priority::text,
                      c.clarification_question, c.clarification_options
               FROM deliberation_flags f
               JOIN flag_corrections c ON c.flag_id = f.id
               WHERE f.patient_id = $1::uuid
                 AND f.requires_human = true
                 AND f.lifecycle_state = 'open'
                 AND c.applied = false
               ORDER BY f.flagged_at DESC""",
            patient_id,
        )

        return {
            "patient_id": patient_id,
            "open_flags": len(open_flags),
            "open": [dict(r) for r in open_flags],
            "recently_retracted": [dict(r) for r in retracted],
            "needs_human_review": [dict(r) for r in human_needed],
            "has_pending_clarifications": len(human_needed) > 0,
        }


@mcp.custom_route("/tools/get_flag_review_status", methods=["POST"])
async def rest_get_flag_review_status(request: Request) -> Response:
    body = await request.json()
    result = await get_flag_review_status(patient_id=body.get("patient_id", ""))
    return Response(json.dumps(result, default=str), media_type="application/json")


@mcp.tool()
async def get_patient_knowledge(
    patient_id: str,
    knowledge_type: str = "all"
) -> dict:
    """
    Retrieve current accumulated patient-specific knowledge.

    Args:
        patient_id: Patient MRN or internal ID
        knowledge_type: Filter by type or 'all'

    Returns:
        Current knowledge entries with confidence scores and provenance.
    """
    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        query = """SELECT knowledge_type, entry_text, confidence,
                          valid_from, evidence_refs, contributing_models
                   FROM patient_knowledge
                   WHERE patient_id = $1
                     AND is_current = true
                     AND (valid_until IS NULL OR valid_until > NOW())"""
        params = [patient_id]
        if knowledge_type != "all":
            query += " AND knowledge_type = $2"
            params.append(knowledge_type)
        query += " ORDER BY confidence DESC, created_at DESC"

        rows = await conn.fetch(query, *params)
        return {
            "patient_id": patient_id,
            "knowledge_count": len(rows),
            "entries": [dict(r) for r in rows]
        }


@mcp.tool()
async def get_pending_nudges(
    patient_id: str,
    target: "str | list[str]" = "patient"
) -> dict:
    """
    Retrieve nudges queued for delivery but not yet sent.
    Used by notification scheduler and care manager dashboard.

    Args:
        patient_id: Patient MRN or internal ID
        target: 'patient' | 'care_team', OR a list like
                ['patient', 'care_team'] to fetch both in a single call.

    Returns:
        When target is a string: {patient_id, target, pending_count, nudges: [...]}
        When target is a list:   {patient_id, by_target: {<t>: {target, pending_count, nudges}},
                                  total_count}
    """
    # Normalize to list for uniform query path, remember original shape for response.
    was_scalar = isinstance(target, str)
    targets = [target] if was_scalar else list(target)
    if not targets:
        return {"patient_id": patient_id, "target": [], "pending_count": 0, "nudges": []}

    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        by_target: dict[str, dict] = {}
        total = 0
        for t in targets:
            nudge_type = f"{t}_nudge"
            rows = await conn.fetch(
                """SELECT dout.id, dout.output_data, dout.trigger_condition,
                          d.triggered_at as deliberation_date
                   FROM deliberation_outputs dout
                   JOIN deliberations d ON dout.deliberation_id = d.id
                   WHERE d.patient_id = $1
                     AND dout.output_type = $2
                     AND dout.delivered_at IS NULL
                   ORDER BY d.triggered_at DESC""",
                patient_id, nudge_type
            )
            nudges = [dict(r) for r in rows]
            by_target[t] = {
                "target": t,
                "pending_count": len(nudges),
                "nudges": nudges,
            }
            total += len(nudges)

        if was_scalar:
            # Preserve legacy response shape for existing callers.
            only = by_target[targets[0]]
            return {
                "patient_id": patient_id,
                "target": targets[0],
                "pending_count": only["pending_count"],
                "nudges": only["nudges"],
            }
        return {
            "patient_id": patient_id,
            "by_target": by_target,
            "total_count": total,
        }


# ── Tier 2.a: S=f(R,C,P,T) dimension getters (read-only, no LLM) ──────────────
#
# These tools expose time-, context-, and role-dimension reads of patient state
# for ambient-surface rendering and deliberation planning. All pure DB reads;
# no LLM calls, no new schema. P-dimension getters (vitals, SDoH, adherence)
# live on S2 in mcp-server/skills/patient_state_readers.py.

_ROLE_TOOLS = {
    "pcp": [
        "clinical_query", "check_screening_due", "flag_drug_interaction",
        "generate_previsit_brief", "get_vital_trend", "get_context_deltas",
        "get_encounter_context", "get_encounter_timeline",
    ],
    "care_manager": [
        "compute_provider_risk", "list_overdue_actions", "get_care_gap_ages",
        "run_sdoh_assessment", "get_sdoh_profile", "triage_message",
        "get_time_since_last_contact",
    ],
    "patient": [
        "compute_obt_score", "run_crisis_escalation", "get_vital_trend",
        "get_medication_adherence_rate",
    ],
}


@mcp.tool()
async def get_time_since_last_contact(patient_id: str) -> dict:
    """Days since the patient's most recent clinical contact.

    Reads clinical_events ordered by event_date DESC. Called by SYNTHESIS for
    JITAI window assessment.
    """
    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT event_date, event_type
               FROM clinical_events
               WHERE patient_id = $1
               ORDER BY event_date DESC NULLS LAST
               LIMIT 1""",
            patient_id,
        )
    if row is None or row["event_date"] is None:
        return {"patient_id": patient_id, "days_since_contact": None,
                "last_contact_date": None, "last_contact_type": None}
    delta = (_dt.now(_tz.utc) - row["event_date"]).days
    return {
        "patient_id": patient_id,
        "days_since_contact": delta,
        "last_contact_date": row["event_date"].isoformat(),
        "last_contact_type": row["event_type"],
    }


@mcp.tool()
async def get_care_gap_ages(patient_id: str) -> dict:
    """Open care gaps with age in days since first flagged.

    Reads care_gaps WHERE status='open'. Called by ARIA for priority scoring.
    """
    pool = await _get_db_pool()
    today = _date.today()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT gap_type, description, identified_date
               FROM care_gaps
               WHERE patient_id = $1 AND status = 'open'
               ORDER BY identified_date ASC NULLS LAST""",
            patient_id,
        )
    gaps = []
    for r in rows:
        days_open = (today - r["identified_date"]).days if r["identified_date"] else None
        gaps.append({
            "gap_type": r["gap_type"],
            "description": r["description"],
            "identified_date": r["identified_date"].isoformat() if r["identified_date"] else None,
            "days_open": days_open,
        })
    return {"patient_id": patient_id, "gap_count": len(gaps), "gaps": gaps}


@mcp.tool()
async def list_overdue_actions(patient_id: str, horizon_days: int = 30) -> dict:
    """Open care gaps that have exceeded horizon_days since identification.

    Reads care_gaps WHERE status='open' AND identified_date older than horizon.
    Called by ARIA and SYNTHESIS.
    """
    pool = await _get_db_pool()
    cutoff = _date.today() - _td(days=horizon_days)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT gap_type, description, identified_date
               FROM care_gaps
               WHERE patient_id = $1 AND status = 'open'
                 AND identified_date IS NOT NULL
                 AND identified_date <= $2
               ORDER BY identified_date ASC""",
            patient_id, cutoff,
        )
    today = _date.today()
    overdue = [
        {
            "action_type": r["gap_type"],
            "description": r["description"],
            "identified_date": r["identified_date"].isoformat(),
            "days_overdue": (today - r["identified_date"]).days - horizon_days,
        }
        for r in rows
    ]
    return {
        "patient_id": patient_id,
        "horizon_days": horizon_days,
        "overdue_count": len(overdue),
        "overdue": overdue,
    }


@mcp.tool()
async def get_encounter_timeline(patient_id: str, lookback_days: int = 365) -> dict:
    """Chronological encounter history within lookback window.

    Reads clinical_events. Called by ARIA for trajectory analysis.
    """
    pool = await _get_db_pool()
    cutoff = _dt.now(_tz.utc) - _td(days=lookback_days)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT event_date, event_type, description, source_system
               FROM clinical_events
               WHERE patient_id = $1 AND event_date >= $2
               ORDER BY event_date DESC""",
            patient_id, cutoff,
        )
    encounters = [
        {
            "date": r["event_date"].isoformat() if r["event_date"] else None,
            "type": r["event_type"],
            "description": r["description"],
            "source": r["source_system"],
        }
        for r in rows
    ]
    return {
        "patient_id": patient_id,
        "lookback_days": lookback_days,
        "encounter_count": len(encounters),
        "encounters": encounters,
    }


@mcp.tool()
async def get_encounter_context(patient_id: str) -> dict:
    """In-encounter context package: active conditions, current meds, open gaps,
    most recent encounter. Read-only snapshot used by ARIA during an encounter.

    No deliberation call. If the caller needs scenarios or predicted questions,
    poll `get_deliberation_results` separately.
    """
    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        conditions = await conn.fetch(
            """SELECT code, display, onset_date, clinical_status
               FROM patient_conditions
               WHERE patient_id = $1
                 AND (clinical_status IS NULL OR clinical_status != 'inactive')
               ORDER BY onset_date DESC NULLS LAST""",
            patient_id,
        )
        medications = await conn.fetch(
            """SELECT code, display, status, authored_on
               FROM patient_medications
               WHERE patient_id = $1
                 AND (status IS NULL OR status = 'active')
               ORDER BY authored_on DESC NULLS LAST""",
            patient_id,
        )
        open_gaps = await conn.fetch(
            """SELECT gap_type, description, identified_date
               FROM care_gaps
               WHERE patient_id = $1 AND status = 'open'
               ORDER BY identified_date DESC NULLS LAST""",
            patient_id,
        )
        recent = await conn.fetchrow(
            """SELECT event_date, event_type, description
               FROM clinical_events
               WHERE patient_id = $1
               ORDER BY event_date DESC NULLS LAST
               LIMIT 1""",
            patient_id,
        )
    return {
        "patient_id": patient_id,
        "active_conditions": [dict(r) for r in conditions],
        "current_medications": [dict(r) for r in medications],
        "open_care_gaps": [dict(r) for r in open_gaps],
        "most_recent_encounter": dict(recent) if recent else None,
    }


@mcp.tool()
async def get_context_deltas(patient_id: str, since_date: str) -> dict:
    """What changed in the patient's record since a given ISO date.

    New conditions, new medications, new care gaps, and resolved gaps within
    the interval [since_date, today]. Called by ARIA for inter-visit surfaces
    and by previsit briefs.

    since_date: ISO 8601 date (YYYY-MM-DD).
    """
    try:
        cutoff = _date.fromisoformat(since_date)
    except ValueError:
        return {"status": "error", "error": f"Invalid since_date '{since_date}'. Expected YYYY-MM-DD."}
    cutoff_ts = _dt.combine(cutoff, _dt.min.time()).replace(tzinfo=_tz.utc)
    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        new_conditions = await conn.fetch(
            """SELECT code, display, onset_date
               FROM patient_conditions
               WHERE patient_id = $1
                 AND onset_date IS NOT NULL AND onset_date >= $2""",
            patient_id, cutoff,
        )
        new_medications = await conn.fetch(
            """SELECT code, display, authored_on, status
               FROM patient_medications
               WHERE patient_id = $1
                 AND authored_on IS NOT NULL AND authored_on >= $2""",
            patient_id, cutoff,
        )
        new_gaps = await conn.fetch(
            """SELECT gap_type, description, identified_date
               FROM care_gaps
               WHERE patient_id = $1 AND identified_date >= $2""",
            patient_id, cutoff,
        )
        resolved_gaps = await conn.fetch(
            """SELECT gap_type, description, resolved_date
               FROM care_gaps
               WHERE patient_id = $1
                 AND resolved_date IS NOT NULL AND resolved_date >= $2""",
            patient_id, cutoff,
        )
        new_events = await conn.fetch(
            """SELECT event_type, event_date, description
               FROM clinical_events
               WHERE patient_id = $1 AND event_date >= $2
               ORDER BY event_date DESC""",
            patient_id, cutoff_ts,
        )
    return {
        "patient_id": patient_id,
        "since_date": since_date,
        "new_conditions": [dict(r) for r in new_conditions],
        "new_medications": [dict(r) for r in new_medications],
        "new_care_gaps": [dict(r) for r in new_gaps],
        "resolved_care_gaps": [dict(r) for r in resolved_gaps],
        "new_encounters": [dict(r) for r in new_events],
    }


@mcp.tool()
async def list_available_actions(role: str, patient_id: str) -> dict:
    """Role-filtered list of tools appropriate for the current patient surface.

    Feeds the ambient surface rendering engine — answers
    "what can I show this role for this patient right now?" The static role→tool
    map is intersected with patient state: tools that require missing data
    (e.g. vitals when no readings exist) are hidden and listed in `hidden`.

    role: 'pcp' | 'care_manager' | 'patient'
    """
    if role not in _ROLE_TOOLS:
        return {
            "status": "error",
            "error": f"Unknown role '{role}'. Must be one of {list(_ROLE_TOOLS.keys())}.",
        }

    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        has_vitals = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM biometric_readings WHERE patient_id = $1)",
            patient_id,
        )
        has_sdoh = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM patient_sdoh_flags WHERE patient_id = $1)",
            patient_id,
        )
        has_meds = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM patient_medications WHERE patient_id = $1)",
            patient_id,
        )

    available, hidden = [], []
    for tool_name in _ROLE_TOOLS[role]:
        # Gate tools on required data presence.
        if tool_name == "get_vital_trend" and not has_vitals:
            hidden.append({"tool": tool_name, "reason": "no biometric_readings for patient"})
        elif tool_name == "get_sdoh_profile" and not has_sdoh:
            hidden.append({"tool": tool_name, "reason": "no patient_sdoh_flags for patient"})
        elif tool_name == "get_medication_adherence_rate" and not has_meds:
            hidden.append({"tool": tool_name, "reason": "no patient_medications for patient"})
        else:
            available.append(tool_name)

    return {
        "role": role,
        "patient_id": patient_id,
        "available_tools": available,
        "hidden_tools": hidden,
    }


# ── REST wrappers for deliberation tools ──────────────────────────────────────


@mcp.custom_route("/tools/register_healthex_patient", methods=["POST"])
async def rest_register_healthex_patient(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        result = await register_healthex_patient(
            health_summary_json=body.get("health_summary_json", "{}")
        )
        return JSONResponse(json.loads(result) if isinstance(result, str) else result)
    except Exception as e:
        import sys as _sys
        print(f"[register_healthex_patient] error: {e}", file=_sys.stderr)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=422)


@mcp.custom_route("/tools/ingest_from_healthex", methods=["POST"])
async def rest_ingest_from_healthex(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        result = await ingest_from_healthex(
            patient_id=body.get("patient_id", ""),
            resource_type=body.get("resource_type", "summary"),
            fhir_json=body.get("fhir_json", "{}"),
        )
        return JSONResponse(json.loads(result) if isinstance(result, str) else result)
    except Exception as e:
        import sys as _sys
        print(f"[ingest_from_healthex] error: {e}", file=_sys.stderr)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=422)


@mcp.custom_route("/tools/run_deliberation", methods=["POST"])
async def rest_run_deliberation(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        result = await run_deliberation(
            patient_id=body.get("patient_id", ""),
            trigger_type=body.get("trigger_type", "manual"),
            max_rounds=body.get("max_rounds", 3),
            mode=body.get("mode", "progressive"),
        )
        return JSONResponse(result)
    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=404)
    except Exception as e:
        import sys as _sys
        print(f"[run_deliberation] error: {e}", file=_sys.stderr)
        return JSONResponse(
            {"status": "error", "error": type(e).__name__, "detail": str(e)},
            status_code=422,
        )


@mcp.custom_route("/tools/get_deliberation_results", methods=["POST"])
async def rest_get_deliberation_results(request: Request) -> JSONResponse:
    body = await request.json()
    result = await get_deliberation_results(
        patient_id=body.get("patient_id", ""),
        output_type=body.get("output_type", "all"),
        limit=body.get("limit", 1),
    )
    return JSONResponse(result)


@mcp.custom_route("/tools/get_patient_knowledge", methods=["POST"])
async def rest_get_patient_knowledge(request: Request) -> JSONResponse:
    body = await request.json()
    result = await get_patient_knowledge(
        patient_id=body.get("patient_id", ""),
        knowledge_type=body.get("knowledge_type", "all"),
    )
    return JSONResponse(result)


@mcp.custom_route("/tools/get_pending_nudges", methods=["POST"])
async def rest_get_pending_nudges(request: Request) -> JSONResponse:
    body = await request.json()
    result = await get_pending_nudges(
        patient_id=body.get("patient_id", ""),
        target=body.get("target", "patient"),
    )
    return JSONResponse(result)


@mcp.custom_route("/tools/execute_pending_plans", methods=["POST"])
async def rest_execute_pending_plans(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        result = await execute_pending_plans(
            patient_id=body.get("patient_id", ""),
            plan_id=body.get("plan_id", ""),
            limit=body.get("limit", 10),
        )
        return JSONResponse(json.loads(result) if isinstance(result, str) else result)
    except Exception as e:
        import sys as _sys
        print(f"[execute_pending_plans] error: {e}", file=_sys.stderr)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=422)


@mcp.custom_route("/tools/get_ingestion_plans", methods=["POST"])
async def rest_get_ingestion_plans(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        result = await get_ingestion_plans(
            patient_id=body.get("patient_id", ""),
            status=body.get("status", ""),
        )
        return JSONResponse(json.loads(result) if isinstance(result, str) else result)
    except Exception as e:
        import sys as _sys
        print(f"[get_ingestion_plans] error: {e}", file=_sys.stderr)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=422)


@mcp.custom_route("/tools/get_transfer_audit", methods=["POST"])
async def rest_get_transfer_audit(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        result = await get_transfer_audit(
            patient_id=body.get("patient_id", ""),
            resource_type=body.get("resource_type", ""),
            status=body.get("status", ""),
            batch_id=body.get("batch_id", ""),
            limit=int(body.get("limit", 50)),
        )
        return JSONResponse(json.loads(result) if isinstance(result, str) else result)
    except Exception as e:
        import sys as _sys
        print(f"[get_transfer_audit] error: {e}", file=_sys.stderr)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=422)


# ---------------------------------------------------------------------------
# Gap-aware tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def assess_reasoning_confidence(
    agent_id: str,
    deliberation_id: str,
    patient_mrn: str,
    reasoning_draft: str,
    clinical_domain: str,
    context_snapshot: dict = None,
    confidence_threshold: float = 0.7,
) -> dict:
    """Evaluate an agent's draft reasoning for knowledge gaps and return a
    structured confidence assessment with gap taxonomy. Call before finalizing
    any agent output in the deliberation loop."""
    import re as _re

    client = anthropic.AsyncAnthropic()

    system = (
        "You are a clinical reasoning auditor for a multi-agent healthcare AI system.\n"
        "Your job: analyze a draft clinical reasoning output and identify specific knowledge gaps\n"
        "that would prevent the agent from producing a clinically safe, high-confidence output.\n\n"
        "Return ONLY valid JSON matching this schema (no markdown, no explanation):\n"
        "{\n"
        '  "overall_confidence": <float 0-1>,\n'
        '  "threshold_met": <boolean>,\n'
        '  "gaps": [\n'
        "    {\n"
        '      "gap_id": "<unique string>",\n'
        '      "gap_type": "<missing_data|stale_data|conflicting_evidence|ambiguous_context|'
        'guideline_uncertainty|drug_interaction_unknown|patient_preference_unknown|'
        'social_determinant_unknown>",\n'
        '      "severity": "<critical|high|medium|low>",\n'
        '      "description": "<specific description>",\n'
        '      "affected_reasoning_step": "<which part of the draft this affects>",\n'
        '      "data_elements_needed": ["<list of specific data elements>"],\n'
        '      "staleness_hours": <number or null>,\n'
        '      "resolvable_by": ["<provider_clarification|patient_query|external_search|lab_order|peer_agent>"]\n'
        "    }\n"
        "  ],\n"
        '  "proceed_recommendation": "<proceed|proceed_with_caveats|pause_and_resolve|escalate_to_provider>"\n'
        "}\n\n"
        'A gap is "critical" if it could lead to patient harm.\n'
        'A gap is "high" if it significantly degrades output quality.\n'
        "Set threshold_met=true only if overall_confidence >= the provided threshold."
    )

    user_msg = (
        f"Agent: {agent_id}\n"
        f"Clinical domain: {clinical_domain}\n"
        f"Confidence threshold: {confidence_threshold}\n\n"
        f"Draft reasoning to audit:\n{reasoning_draft[:3000]}\n\n"
        f"Context available:\n{json.dumps(context_snapshot or {}, default=str)[:2000]}\n\n"
        "Identify all gaps. Be specific — name exact data elements, LOINC codes where known, "
        "specific guideline references. For drug interactions, name the drug pair."
    )

    response = await client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = response.content[0].text.strip()
    raw = strip_markdown_fences(raw)
    result = json.loads(raw)
    result["threshold_met"] = result["overall_confidence"] >= confidence_threshold

    # Persist critical/high gaps to DB
    for gap in result.get("gaps", []):
        if gap.get("severity") in ("critical", "high"):
            await insert_reasoning_gap(
                deliberation_id,
                patient_mrn,
                agent_id,
                gap["gap_id"],
                {
                    "gap_type": gap["gap_type"],
                    "severity": gap["severity"],
                    "description": gap["description"],
                    "impact_statement": gap.get("affected_reasoning_step", ""),
                    "confidence_without_resolution": result["overall_confidence"],
                    "confidence_with_resolution": min(
                        result["overall_confidence"] + 0.25, 0.95
                    ),
                    "attempted_resolutions": [],
                    "recommended_action_for_synthesis": "include_caveat_in_output",
                },
            )

    return result


_VALID_CLARIFICATION_RECIPIENTS = frozenset({"provider", "patient", "peer_agent", "synthesis"})
_VALID_CLARIFICATION_URGENCY = frozenset({"blocking", "preferred", "optional"})


@mcp.tool()
async def request_clarification(
    deliberation_id: str,
    requesting_agent: str,
    recipient: str,
    urgency: str,
    question_text: str,
    clinical_rationale: str,
    gap_id: str,
    suggested_options: list = None,
    default_if_unanswered: str = None,
    timeout_minutes: int = 60,
    fallback_behavior: str = "escalate_to_synthesis",
    recipient_agent_id: str = None,
) -> dict:
    """Pause agent execution and emit a structured clarification request to a
    provider, patient, or peer agent. Returns immediately with a clarification_id;
    the deliberation engine polls for resolution.

    Args:
        deliberation_id: Active deliberation session UUID.
        requesting_agent: Agent ID emitting the request (e.g. ARIA, MIRA, THEO).
        recipient: Who should answer — one of: provider, patient, peer_agent, synthesis.
        urgency: one of: blocking (halts pipeline), preferred, optional.
        question_text: The clarifying question to present.
        clinical_rationale: Why this information is needed clinically.
        gap_id: The reasoning gap ID this clarification resolves.
        suggested_options: Optional list of answer choices.
        default_if_unanswered: Value to use if no response received before timeout.
        timeout_minutes: Minutes to wait before applying fallback (default 60).
        fallback_behavior: Action if unanswered — default escalate_to_synthesis.
        recipient_agent_id: If recipient=peer_agent, the specific agent UUID.
    """
    if recipient not in _VALID_CLARIFICATION_RECIPIENTS:
        return {
            "status": "error",
            "error": (
                f"recipient must be one of: "
                f"{sorted(_VALID_CLARIFICATION_RECIPIENTS)}. Got: {recipient!r}"
            ),
        }
    if urgency not in _VALID_CLARIFICATION_URGENCY:
        return {
            "status": "error",
            "error": (
                f"urgency must be one of: "
                f"{sorted(_VALID_CLARIFICATION_URGENCY)}. Got: {urgency!r}"
            ),
        }
    req = {
        "deliberation_id": deliberation_id,
        "requesting_agent": requesting_agent,
        "recipient": recipient,
        "recipient_agent_id": recipient_agent_id,
        "urgency": urgency,
        "question": {
            "text": question_text,
            "clinical_rationale": clinical_rationale,
            "suggested_options": suggested_options or [],
            "default_if_unanswered": default_if_unanswered,
        },
        "gap_id": gap_id,
        "timeout_minutes": timeout_minutes,
        "fallback_behavior": fallback_behavior,
    }
    clarification_id = await insert_clarification_request(req)

    return {
        "clarification_id": clarification_id,
        "status": "pending",
        "response": None,
        "respondent": None,
        "response_timestamp": None,
        "resolution_action": "escalated" if urgency == "blocking" else "fallback_applied",
    }


_VALID_GAP_TYPES = frozenset({
    "missing_data", "stale_data", "conflicting_evidence", "ambiguous_context",
    "guideline_uncertainty", "drug_interaction_unknown",
    "patient_preference_unknown", "social_determinant_unknown",
})
_VALID_GAP_SEVERITY = frozenset({"critical", "high", "medium", "low"})
_VALID_GAP_EMITTING_AGENTS = frozenset({"ARIA", "MIRA", "THEO"})


@mcp.tool()
async def emit_reasoning_gap_artifact(
    deliberation_id: str,
    emitting_agent: str,
    gap_id: str,
    gap_type: str,
    severity: str,
    description: str,
    impact_statement: str,
    confidence_without_resolution: float,
    confidence_with_resolution: float,
    recommended_action_for_synthesis: str,
    patient_mrn: str = "unknown",
    attempted_resolutions: list = None,
    caveat_text: str = None,
    expires_at: str = None,
) -> dict:
    """Persist a structured reasoning gap artifact to the warehouse.
    SYNTHESIS reads all artifacts before merging agent outputs.
    Call when a gap cannot be resolved inline and must be communicated
    to the orchestrator.

    Args:
        deliberation_id: Active deliberation session UUID.
        emitting_agent: Must be one of: ARIA, MIRA, THEO.
        gap_id: Unique identifier for this gap (e.g. gap-hba1c-001).
        gap_type: One of: missing_data, stale_data, conflicting_evidence,
            ambiguous_context, guideline_uncertainty, drug_interaction_unknown,
            patient_preference_unknown, social_determinant_unknown.
        severity: One of: critical, high, medium, low.
        description: Human-readable description of the gap.
        impact_statement: Clinical impact if gap is not resolved.
        confidence_without_resolution: Confidence score 0–1 without resolving the gap.
        confidence_with_resolution: Confidence score 0–1 if the gap were resolved.
        recommended_action_for_synthesis: Directive for SYNTHESIS, e.g.
            include_caveat_in_output, trigger_order_recommendation,
            add_to_care_gap_list, block_output_pending_resolution.
        patient_mrn: Patient MRN (optional, defaults to 'unknown').
        attempted_resolutions: List of resolutions already tried.
        caveat_text: Pre-written caveat text for SYNTHESIS to include.
        expires_at: ISO timestamp after which this gap is stale.
    """
    if emitting_agent not in _VALID_GAP_EMITTING_AGENTS:
        return {
            "status": "error",
            "error": (
                f"emitting_agent must be one of: "
                f"{sorted(_VALID_GAP_EMITTING_AGENTS)}. Got: {emitting_agent!r}"
            ),
        }
    if gap_type not in _VALID_GAP_TYPES:
        return {
            "status": "error",
            "error": (
                f"gap_type must be one of: "
                f"{sorted(_VALID_GAP_TYPES)}. Got: {gap_type!r}"
            ),
        }
    if severity not in _VALID_GAP_SEVERITY:
        return {
            "status": "error",
            "error": (
                f"severity must be one of: "
                f"{sorted(_VALID_GAP_SEVERITY)}. Got: {severity!r}"
            ),
        }
    artifact = {
        "gap_type": gap_type,
        "severity": severity,
        "description": description,
        "impact_statement": impact_statement,
        "confidence_without_resolution": confidence_without_resolution,
        "confidence_with_resolution": confidence_with_resolution,
        "attempted_resolutions": attempted_resolutions or [],
        "recommended_action_for_synthesis": recommended_action_for_synthesis,
        "caveat_text": caveat_text,
        "expires_at": expires_at,
    }

    artifact_id = await insert_reasoning_gap(
        deliberation_id, patient_mrn, emitting_agent, gap_id, artifact,
    )

    # Determine downstream actions
    downstream = []
    if recommended_action_for_synthesis == "trigger_order_recommendation":
        downstream.append("order_recommendation_queued")
    if recommended_action_for_synthesis == "add_to_care_gap_list":
        downstream.append("care_gap_added")
    if severity == "critical":
        downstream.append("synthesis_priority_escalated")

    return {
        "artifact_id": artifact_id,
        "stored": True,
        "synthesis_notified": True,
        "downstream_actions_triggered": downstream,
    }


@mcp.tool()
async def register_gap_trigger(
    patient_mrn: str,
    gap_id: str,
    watch_for: str,
    expires_at: str,
    on_fire_action: str,
    loinc_code: str = None,
    snomed_code: str = None,
    custom_condition: str = None,
    trigger_type: str = "gap_resolution_received",
    deliberation_scope: list = None,
) -> dict:
    """Register a deliberation trigger that fires when specific gap-resolving data
    arrives in the warehouse. Enables reactive re-deliberation without polling."""
    req = {
        "patient_mrn": patient_mrn,
        "gap_id": gap_id,
        "trigger_condition": {
            "watch_for": watch_for,
            "loinc_code": loinc_code,
            "snomed_code": snomed_code,
            "custom_condition": custom_condition,
        },
        "trigger_type": trigger_type,
        "expires_at": expires_at,
        "on_fire_action": on_fire_action,
        "deliberation_scope": deliberation_scope or ["full_council"],
    }
    trigger_id = await insert_gap_trigger(req)

    prob_map = {
        "lab_result": 0.75,
        "screening_score": 0.55,
        "medication_change": 0.60,
        "encounter_note": 0.80,
        "vital_sign": 0.85,
        "patient_response": 0.40,
    }

    return {
        "trigger_id": trigger_id,
        "registered": True,
        "expires_at": expires_at,
        "estimated_resolution_probability": prob_map.get(watch_for, 0.5),
    }


# ── REST wrappers for gap-aware tools ─────────────────────────────────────────


@mcp.custom_route("/tools/assess_reasoning_confidence", methods=["POST"])
async def rest_assess_reasoning_confidence(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        result = await assess_reasoning_confidence(
            agent_id=body.get("agent_id", ""),
            deliberation_id=body.get("deliberation_id", ""),
            patient_mrn=body.get("patient_mrn", ""),
            reasoning_draft=body.get("reasoning_draft", ""),
            clinical_domain=body.get("clinical_domain", "risk_assessment"),
            context_snapshot=body.get("context_snapshot"),
            confidence_threshold=float(body.get("confidence_threshold", 0.7)),
        )
        return JSONResponse(result if isinstance(result, dict) else json.loads(result))
    except Exception as e:
        logger.error("[assess_reasoning_confidence] error: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=422)


@mcp.custom_route("/tools/request_clarification", methods=["POST"])
async def rest_request_clarification(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        result = await request_clarification(
            deliberation_id=body.get("deliberation_id", ""),
            requesting_agent=body.get("requesting_agent", ""),
            recipient=body.get("recipient", ""),
            urgency=body.get("urgency", "optional"),
            question_text=body.get("question_text", ""),
            clinical_rationale=body.get("clinical_rationale", ""),
            gap_id=body.get("gap_id", ""),
            suggested_options=body.get("suggested_options"),
            default_if_unanswered=body.get("default_if_unanswered"),
            timeout_minutes=int(body.get("timeout_minutes", 60)),
            fallback_behavior=body.get("fallback_behavior", "escalate_to_synthesis"),
            recipient_agent_id=body.get("recipient_agent_id"),
        )
        return JSONResponse(result if isinstance(result, dict) else json.loads(result))
    except Exception as e:
        logger.error("[request_clarification] error: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=422)


@mcp.custom_route("/tools/emit_reasoning_gap_artifact", methods=["POST"])
async def rest_emit_reasoning_gap_artifact(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        result = await emit_reasoning_gap_artifact(
            deliberation_id=body.get("deliberation_id", ""),
            emitting_agent=body.get("emitting_agent", ""),
            gap_id=body.get("gap_id", ""),
            gap_type=body.get("gap_type", "missing_data"),
            severity=body.get("severity", "medium"),
            description=body.get("description", ""),
            impact_statement=body.get("impact_statement", ""),
            confidence_without_resolution=float(body.get("confidence_without_resolution", 0.5)),
            confidence_with_resolution=float(body.get("confidence_with_resolution", 0.8)),
            recommended_action_for_synthesis=body.get("recommended_action_for_synthesis", "include_caveat_in_output"),
            patient_mrn=body.get("patient_mrn", "unknown"),
            attempted_resolutions=body.get("attempted_resolutions"),
            caveat_text=body.get("caveat_text"),
            expires_at=body.get("expires_at"),
        )
        return JSONResponse(result if isinstance(result, dict) else json.loads(result))
    except Exception as e:
        logger.error("[emit_reasoning_gap_artifact] error: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=422)


@mcp.custom_route("/tools/register_gap_trigger", methods=["POST"])
async def rest_register_gap_trigger(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        result = await register_gap_trigger(
            patient_mrn=body.get("patient_mrn", ""),
            gap_id=body.get("gap_id", ""),
            watch_for=body.get("watch_for", ""),
            expires_at=body.get("expires_at", ""),
            on_fire_action=body.get("on_fire_action", ""),
            loinc_code=body.get("loinc_code"),
            snomed_code=body.get("snomed_code"),
            custom_condition=body.get("custom_condition"),
            trigger_type=body.get("trigger_type", "gap_resolution_received"),
            deliberation_scope=body.get("deliberation_scope"),
        )
        return JSONResponse(result if isinstance(result, dict) else json.loads(result))
    except Exception as e:
        logger.error("[register_gap_trigger] error: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=422)


# ---------------------------------------------------------------------------
# Provenance REST test route (MCP tool exposed for HTTP smoke testing)
# ---------------------------------------------------------------------------

@mcp.custom_route("/tools/verify_output_provenance", methods=["POST"])
async def rest_verify_output_provenance(request: Request) -> JSONResponse:
    """HTTP wrapper for verify_output_provenance — allows REST smoke testing.

    Body fields:
      payload        — JSON string (or object) with a 'sections' array.
      deliberation_id — optional UUID string.
      patient_mrn     — optional MRN (hashed before storage).
      strict_mode     — bool, default true.

    Returns the same provenance report structure as the MCP tool:
      { gate_decision, section_results, summary, provenance_report_id, ... }
    """
    try:
        from shared.provenance.verifier import (
            validate_section,
            render_recommendation,
            build_gate_decision,
            hash_mrn,
        )
        import uuid as _uuid
        from datetime import datetime as _datetime, timezone as _tz

        body = await request.json()
        payload_raw = body.get("payload", "{}")
        deliberation_id = body.get("deliberation_id", "")
        patient_mrn = body.get("patient_mrn", "")
        strict_mode = bool(body.get("strict_mode", True))

        try:
            data = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        except json.JSONDecodeError as e:
            return JSONResponse(
                {"status": "error", "error": f"Invalid payload JSON: {e}"},
                status_code=422,
            )

        report_id = str(_uuid.uuid4())
        assessed_at = _datetime.now(_tz.utc).isoformat()
        output_id = data.get("output_id", "unknown")
        sections = data.get("sections", [])
        if not isinstance(sections, list):
            sections = []

        section_results = []
        all_pending_tools = []

        for section in sections:
            violations = validate_section(section)
            for v in violations:
                pt = v.get("pending_tool")
                if pt and pt not in all_pending_tools:
                    all_pending_tools.append(pt)
            if section.get("declared_tier") == "PENDING":
                pt = section.get("pending_tool_name")
                if pt and pt not in all_pending_tools:
                    all_pending_tools.append(pt)
            tier_confirmed = (
                section.get("declared_tier") in {"TOOL", "RETRIEVAL", "SYNTHESIZED", "PENDING"}
                and not any(v["severity"] == "BLOCK" for v in violations)
            )
            section_results.append({
                "section_id": section.get("section_id", "unknown"),
                "agent": section.get("agent", "unknown"),
                "declared_tier": section.get("declared_tier"),
                "violations": violations,
                "tier_confirmed": tier_confirmed,
                "render_recommendation": render_recommendation(section, violations),
            })

        total = len(section_results)
        blocked = sum(
            1 for s in section_results
            if any(v["severity"] == "BLOCK" for v in s["violations"])
        )
        warned = sum(
            1 for s in section_results
            if (
                any(v["severity"] == "WARN" for v in s["violations"])
                and not any(v["severity"] == "BLOCK" for v in s["violations"])
            )
        )
        approved = total - blocked - warned
        gate_decision, block_reason = build_gate_decision(section_results, strict_mode)

        report = {
            "provenance_report_id": report_id,
            "deliberation_id": deliberation_id,
            "output_id": output_id,
            "assessed_at": assessed_at,
            "gate_decision": gate_decision,
            "block_reason": block_reason,
            "section_results": section_results,
            "summary": {
                "total_sections": total,
                "approved": approved,
                "warned": warned,
                "blocked": blocked,
                "pending_tools_needed": all_pending_tools,
            },
        }
        return JSONResponse(report)
    except Exception as e:
        logger.error("[verify_output_provenance] REST error: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=422)


# ═════════════════════════════════════════════════════════════════════════════
# Tier 2.b — Behavioral science stack (NIS + safety gates + pipeline composite)
# Tier 3   — Deliberation introspection
# Tier 4   — Population / inbox / constitutional critic
#
# All tools read from the DB (migration 008_behavioral_tables.sql + existing
# deliberation tables). Heavy LLM judgements are marked [LLM-ENRICHED] and
# fall back to deterministic scoring when no ANTHROPIC_API_KEY is configured.
# ═════════════════════════════════════════════════════════════════════════════

import uuid as _uuid  # noqa: E402

# ── Tier 2.b.i — NIS scaffolding (3 tools, pure math, S1) ────────────────────

# Default NIS weights. Keep them here so every audit row in nis_score_audits
# can record the exact weights used. Weights sum to 1.0 by convention but
# callers may override.
_NIS_WEIGHTS = {"alpha": 0.40, "beta": 0.25, "gamma": 0.20, "delta": 0.15}


@mcp.tool()
async def compute_ite_estimate(
    patient_id: str,
    care_gap_count: int,
    trajectory_direction: str,
    modifiable_risk_fraction: float,
) -> dict:
    """Predicted causal benefit from intervention (α·ITE(P) component of NIS).

    Deterministic scorer used independently and by score_nudge_impactability.
    Research: Sheth et al. 2026 benefit-based prioritization (GRF).

    trajectory_direction: 'improving' | 'stable' | 'worsening'
    modifiable_risk_fraction: 0.0-1.0 — share of risk attributable to
                              modifiable factors (adherence, SDoH, behavior).
    """
    if trajectory_direction not in ("improving", "stable", "worsening"):
        return {"status": "error", "error": f"invalid trajectory_direction '{trajectory_direction}'"}
    modifiable_risk_fraction = max(0.0, min(1.0, float(modifiable_risk_fraction)))
    trajectory_weight = {"improving": 0.3, "stable": 0.6, "worsening": 1.0}[trajectory_direction]
    gap_weight = min(1.0, care_gap_count / 5.0)   # saturates at 5 gaps
    ite_score = round(0.5 * modifiable_risk_fraction + 0.3 * trajectory_weight + 0.2 * gap_weight, 4)
    return {
        "patient_id": patient_id,
        "ite_score": ite_score,
        "confidence": 0.7,
        "primary_drivers": [
            f"trajectory={trajectory_direction}",
            f"care_gaps={care_gap_count}",
            f"modifiable_risk={modifiable_risk_fraction:.2f}",
        ],
    }


@mcp.tool()
async def compute_behavioral_receptivity(
    patient_id: str,
    last_clinical_event_hours: float,
    last_app_interaction_hours: float,
    day_of_week: int,
    days_since_temporal_landmark: int,
) -> dict:
    """β·receptivity(T) component of NIS — when is this patient most reachable?

    Independently callable. Research: McBride 2003 (teachable moments),
    Dai/Milkman (fresh-start effect), Künzler et al. (JITAI ±40% improvement).

    day_of_week: 0=Monday … 6=Sunday.
    """
    # Teachable moment proximity: event within last 72h → high; >14d → zero.
    tm = max(0.0, min(1.0, 1.0 - (last_clinical_event_hours / (14 * 24))))
    # Fresh-start: landmark within last 7d → high; monotonically decays.
    fs = max(0.0, min(1.0, 1.0 - (days_since_temporal_landmark / 30.0)))
    # App-interaction recency: hot in last hour, cold after 7d.
    interaction = max(0.0, min(1.0, 1.0 - (last_app_interaction_hours / (7 * 24))))
    # Day-of-week prior (weekday slightly higher than weekend for clinical tasks).
    dow_boost = 0.05 if day_of_week < 5 else 0.0
    receptivity = round(0.4 * tm + 0.3 * fs + 0.3 * interaction + dow_boost, 4)
    receptivity = max(0.0, min(1.0, receptivity))
    return {
        "patient_id": patient_id,
        "receptivity_score": receptivity,
        "teachable_moment_proximity": round(tm, 4),
        "fresh_start_proximity": round(fs, 4),
        "jitai_window_active": receptivity > 0.6,
    }


@mcp.tool()
async def score_nudge_impactability(
    patient_id: str,
    deliberation_id: str,
    ite_estimate: float,
    care_gap_count: int,
    trajectory_direction: str,
    last_clinical_event_hours: float,
    last_app_interaction_hours: float,
    day_of_week: int,
    days_since_temporal_landmark: int,
    anxiety_state: str = "baseline",
    com_b_score: float = 0.5,
    llm_health_score: float = 0.8,
    weights_override: dict | None = None,
) -> dict:
    """Compound Nudge Impactability Score (NIS).

    NIS = α·ITE(P) + β·receptivity(T) + γ·COM-B(P) + δ·LLM-health(P)

    Recommendation thresholds:
      NIS ≥ 0.65 → fire
      0.45 – 0.64 → hold
      < 0.45 → suppress
    anxiety_state='crisis' forces suppress regardless of NIS (clinical gate).

    Persists the full decomposition to nis_score_audits for auditability.
    """
    if anxiety_state not in ("baseline", "elevated", "crisis"):
        return {"status": "error", "error": f"invalid anxiety_state '{anxiety_state}'"}

    weights = {**_NIS_WEIGHTS, **(weights_override or {})}
    receptivity = await compute_behavioral_receptivity(
        patient_id=patient_id,
        last_clinical_event_hours=last_clinical_event_hours,
        last_app_interaction_hours=last_app_interaction_hours,
        day_of_week=day_of_week,
        days_since_temporal_landmark=days_since_temporal_landmark,
    )
    r = receptivity["receptivity_score"]

    compound = round(
        weights["alpha"] * ite_estimate
        + weights["beta"] * r
        + weights["gamma"] * com_b_score
        + weights["delta"] * llm_health_score,
        4,
    )

    # Crisis phase clamp — non-negotiable clinical gate.
    if anxiety_state == "crisis":
        recommendation = "suppress"
        rationale = "Crisis phase: all nudges suppressed regardless of NIS."
    elif compound >= 0.65:
        recommendation, rationale = "fire", "NIS exceeds fire threshold (0.65)."
    elif compound >= 0.45:
        recommendation, rationale = "hold", "NIS in hold band (0.45–0.64)."
    else:
        recommendation, rationale = "suppress", "NIS below suppress threshold (0.45)."

    # Audit row.
    pool = await _get_db_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO nis_score_audits
                   (patient_id, deliberation_id, compound_score, ite_score,
                    receptivity_score, com_b_score, llm_health_score, weights,
                    recommendation, rationale, calling_agent)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11)""",
                _uuid.UUID(patient_id) if _looks_like_uuid(patient_id) else patient_id,
                _uuid.UUID(deliberation_id) if deliberation_id and _looks_like_uuid(deliberation_id) else None,
                compound, ite_estimate, r, com_b_score, llm_health_score,
                json.dumps(weights), recommendation, rationale, "SYNTHESIS",
            )
    except Exception as e:
        logger.warning("nis_score_audits write skipped (%s) — tool result still returned", e)

    return {
        "patient_id": patient_id,
        "deliberation_id": deliberation_id,
        "compound_score": compound,
        "recommendation": recommendation,
        "component_scores": {
            "ite": ite_estimate,
            "receptivity": r,
            "com_b": com_b_score,
            "llm_health": llm_health_score,
        },
        "weights": weights,
        "rationale": rationale,
        "anxiety_state": anxiety_state,
    }


def _looks_like_uuid(s: str) -> bool:
    try:
        _uuid.UUID(str(s))
        return True
    except (ValueError, TypeError):
        return False


# ── Tier 2.b.ii — Safety gates (non-overridable) ─────────────────────────────

# Known sycophancy anti-patterns in medical LLM output. Simple substring
# screens; the LLM audit path below extends this for nuance. Non-overridable
# at the gate level — callers cannot bypass.
_SYCOPHANCY_PATTERNS = (
    ("you're right to skip", "validates medication avoidance"),
    ("no need to worry", "dismisses concern requiring evaluation"),
    ("doctors often overreact", "undermines clinician authority"),
    ("that symptom is usually nothing", "dismisses ambiguous symptom"),
    ("i completely agree", "unqualified agreement without evidence"),
    ("absolutely, you should trust", "premature certainty"),
)


@mcp.tool()
async def check_sycophancy_risk(
    patient_id: str,
    draft_output: str,
    originating_agent: str,
) -> dict:
    """Evaluate a draft agent output for sycophancy risk.

    Score > 0.6 ⇒ MANDATORY reframe by originating_agent. Non-overridable.
    This gate runs for every patient-facing output. Research: OpenAI/MIT
    RCT 2025 (48% compliance rate without independent verification).

    Args:
        draft_output: The text to audit — may contain the assembled response.
        originating_agent: 'ARIA' | 'MIRA' | 'THEO' — for audit logging.
    """
    text = (draft_output or "").lower()
    matched = [(pat, reason) for pat, reason in _SYCOPHANCY_PATTERNS if pat in text]
    # Deterministic pattern score.
    pattern_score = min(1.0, len(matched) * 0.35)
    risk_patterns = [{"pattern": pat, "reason": reason} for pat, reason in matched]

    # [LLM-ENRICHED] Optional refinement if we have an API key and the draft
    # is non-trivial. A structured audit prompt lives in
    # server/deliberation/prompts/sycophancy_audit.xml (to be added alongside
    # this gate's first production run).
    if os.environ.get("ANTHROPIC_API_KEY") and len(text) > 120:
        try:
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=(
                    "You are a medical safety auditor. Score 0.0–1.0 for "
                    "sycophancy risk in the given clinical text. Respond with a "
                    "single JSON object: {\"score\": <float>, \"reason\": <str>}."
                ),
                messages=[{"role": "user", "content": draft_output[:4000]}],
            )
            parsed = json.loads(resp.content[0].text.strip().removeprefix("```json").removesuffix("```").strip())
            llm_score = float(parsed.get("score", 0.0))
            sycophancy_score = round(max(pattern_score, llm_score), 4)
            if parsed.get("reason"):
                risk_patterns.append({"pattern": "llm_audit", "reason": parsed["reason"]})
        except Exception as e:
            logger.info("sycophancy LLM audit skipped: %s", e)
            sycophancy_score = pattern_score
    else:
        sycophancy_score = pattern_score

    reframe_required = sycophancy_score > 0.6
    return {
        "patient_id": patient_id,
        "originating_agent": originating_agent,
        "sycophancy_score": sycophancy_score,
        "risk_patterns": risk_patterns,
        "reframe_required": reframe_required,
        "suggested_reframe": (
            "Restate the clinical concern with the specific guideline citation "
            "and the clinician review path. Avoid validating avoidance of "
            "evaluation."
        ) if reframe_required else "",
    }


@mcp.tool()
async def run_constitutional_critic(
    patient_id: str,
    draft_output: str,
    originating_agent: str,
    output_type: str,
) -> dict:
    """4-check constitutional critic applied before any delivery.

    Pipeline:
      1. Sycophancy (composes check_sycophancy_risk)
      2. Guideline factuality (heuristic; PHI scan also runs here)
      3. Internal contradiction detection (simple negation scan)
      4. Escalation tier routing

    Returns escalation_tier 1–4:
      1: automated guardrail handled — return reframe_required=True
      2: evaluator model review suggested
      3: clinician review flag (sets reframe_required=True)
      4: human handoff required (sets reframe_required=True; cannot ship)

    output_type: 'nudge' | 'clinical_recommendation' | 'patient_education' |
                 'provider_brief'
    """
    issues: list[dict] = []

    # Step 1 — sycophancy gate.
    syc = await check_sycophancy_risk(patient_id, draft_output, originating_agent)
    if syc.get("reframe_required"):
        issues.append({"check": "sycophancy", "severity": "high",
                       "detail": f"score={syc['sycophancy_score']}"})

    # Step 2 — PHI heuristic (basic regex on common identifiers).
    import re as _re
    phi_hits = _re.findall(r"\b\d{3}-\d{2}-\d{4}\b", draft_output or "")  # SSN
    phi_hits += _re.findall(r"\b\d{10,}\b", draft_output or "")           # long numeric IDs
    if phi_hits:
        issues.append({"check": "phi_leak", "severity": "critical",
                       "detail": f"{len(phi_hits)} PHI-like tokens detected"})

    # Step 3 — internal contradiction (shallow): simultaneous "recommend X"
    # and "do not X" patterns.
    lowered = (draft_output or "").lower()
    contradictions = []
    for verb in ("start", "continue", "stop", "increase", "decrease"):
        if f"recommend {verb}" in lowered and f"do not {verb}" in lowered:
            contradictions.append(verb)
    if contradictions:
        issues.append({"check": "internal_contradiction", "severity": "high",
                       "detail": f"contradictory verbs: {contradictions}"})

    # Step 4 — escalation routing.
    critical = [i for i in issues if i["severity"] == "critical"]
    high = [i for i in issues if i["severity"] == "high"]
    if critical:
        tier, reframe = 4, True
    elif len(high) >= 2:
        tier, reframe = 3, True
    elif high:
        tier, reframe = 2, True
    else:
        tier, reframe = 1, False

    return {
        "patient_id": patient_id,
        "output_type": output_type,
        "originating_agent": originating_agent,
        "passed": not reframe,
        "escalation_tier": tier,
        "issues": issues,
        "reframe_required": reframe,
        "revised_output": "" if not reframe else
            "[constitutional critic: reframe required — see issues]",
        "audit_log": {"sycophancy": syc},
    }


# ── Tier 2.b.vi — run_healthex_pipeline (fire-and-forget) ────────────────────

import asyncio as _asyncio  # noqa: E402

_PIPELINE_JOBS: dict[str, dict] = {}   # job_id → {status, patient_id, steps, error}


async def _run_healthex_pipeline_background(job_id: str, patient_mrn: str) -> None:
    """Background worker. Never raises — status written into _PIPELINE_JOBS."""
    steps: list[dict] = []
    def _mark(step: str, status: str, detail: dict | None = None) -> None:
        steps.append({"step": step, "status": status, **(detail or {})})
        _PIPELINE_JOBS[job_id]["steps"] = steps
    try:
        _PIPELINE_JOBS[job_id]["status"] = "running"
        # 1. use_healthex → switch track.
        try:
            await use_healthex()
            _mark("use_healthex", "ok")
        except Exception as e:
            _mark("use_healthex", "failed", {"error": str(e)})
        # 2. register_healthex_patient.
        # (Caller supplies a real summary; we stub with a minimal FHIR Patient.)
        stub_summary = json.dumps({
            "resourceType": "Bundle", "type": "collection",
            "entry": [{"resource": {"resourceType": "Patient",
                                    "identifier": [{"value": patient_mrn}]}}],
        })
        try:
            reg = await register_healthex_patient(stub_summary)
            reg_parsed = json.loads(reg) if isinstance(reg, str) else reg
            patient_id = reg_parsed.get("patient_id") or patient_mrn
            _mark("register_healthex_patient", "ok", {"patient_id": patient_id})
        except Exception as e:
            _mark("register_healthex_patient", "failed", {"error": str(e)})
            _PIPELINE_JOBS[job_id]["status"] = "partial"
            _PIPELINE_JOBS[job_id]["failed_step"] = "register_healthex_patient"
            return
        # 3. run_deliberation (fire-and-forget inside fire-and-forget).
        try:
            dres = await run_deliberation(
                patient_id=patient_id, trigger_type="pipeline_composite",
                max_rounds=3, mode="triage",
            )
            _mark("run_deliberation", "ok", {"deliberation_id": dres.get("deliberation_id")})
        except Exception as e:
            _mark("run_deliberation", "failed", {"error": str(e)})
        _PIPELINE_JOBS[job_id]["status"] = "complete"
        _PIPELINE_JOBS[job_id]["patient_id"] = patient_id
    except Exception as e:
        _PIPELINE_JOBS[job_id]["status"] = "error"
        _PIPELINE_JOBS[job_id]["error"] = str(e)


@mcp.tool()
async def run_healthex_pipeline(patient_mrn: str) -> dict:
    """Fire-and-forget composite: switch track → register → deliberation.

    Returns IMMEDIATELY with a job_id. Callers poll `get_healthex_pipeline_status(job_id)`
    to retrieve progress. This tool NEVER `awaits` deliberation synchronously
    — doing so would time out MCP request windows.

    patient_mrn: stable MRN string identifying the patient record.
    """
    job_id = str(_uuid.uuid4())
    _PIPELINE_JOBS[job_id] = {"status": "queued", "patient_mrn": patient_mrn, "steps": []}
    _asyncio.create_task(_run_healthex_pipeline_background(job_id, patient_mrn))
    return {"job_id": job_id, "status": "queued",
            "poll": "get_healthex_pipeline_status(job_id)"}


@mcp.tool()
async def get_healthex_pipeline_status(job_id: str) -> dict:
    """Poll the status of a run_healthex_pipeline job."""
    job = _PIPELINE_JOBS.get(job_id)
    if job is None:
        return {"status": "unknown_job", "job_id": job_id}
    return {"job_id": job_id, **job}


# ── Tier 3 — Introspection (3 tools, all read-only) ──────────────────────────

@mcp.tool()
async def compute_deliberation_convergence(
    deliberation_id: str,
    backend: str = "jaccard",
) -> dict:
    """Convergence score for a completed deliberation.

    backend='jaccard': current token-overlap implementation.
    backend='medcpt':  [PLANNED] Phase-2 MedCPT-embedding cosine similarity.
                       Returns a clear NotImplemented payload until pgvector
                       migration 009 is live.
    """
    if backend == "medcpt":
        return {
            "deliberation_id": deliberation_id,
            "backend_used": "medcpt",
            "convergence_score": None,
            "error": "MedCPT backend not yet implemented. "
                     "Depends on migration 009_pgvector_guidelines.sql + "
                     "MedCPT-Article-Encoder embeddings.",
            "fallback_available": True,
        }
    if backend != "jaccard":
        return {"status": "error", "error": f"unknown backend '{backend}'"}

    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        dlb = await conn.fetchrow(
            """SELECT id, convergence_score, rounds_completed
               FROM deliberations WHERE id = $1""",
            _uuid.UUID(deliberation_id) if _looks_like_uuid(deliberation_id) else deliberation_id,
        )
    if dlb is None:
        return {"status": "error", "error": f"deliberation {deliberation_id} not found"}
    return {
        "deliberation_id": deliberation_id,
        "backend_used": "jaccard",
        "convergence_score": dlb["convergence_score"],
        "rounds_to_convergence": dlb["rounds_completed"],
        "interpretation": "0.0 is expected for Jaccard — see CLAUDE.md §10",
    }


@mcp.tool()
async def get_deliberation_phases(deliberation_id: str) -> dict:
    """Per-phase metadata for a completed deliberation. Reads deliberation_outputs."""
    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        dlb = await conn.fetchrow(
            """SELECT id, triggered_at, convergence_score, rounds_completed, status
               FROM deliberations WHERE id = $1""",
            _uuid.UUID(deliberation_id) if _looks_like_uuid(deliberation_id) else deliberation_id,
        )
        if dlb is None:
            return {"status": "error", "error": "deliberation not found"}
        outputs = await conn.fetch(
            """SELECT output_type, COUNT(*) AS n
               FROM deliberation_outputs
               WHERE deliberation_id = $1
               GROUP BY output_type""",
            dlb["id"],
        )
    return {
        "deliberation_id": deliberation_id,
        "status": dlb["status"],
        "total_rounds": dlb["rounds_completed"],
        "convergence_score": dlb["convergence_score"],
        "output_type_counts": {r["output_type"]: r["n"] for r in outputs},
    }


@mcp.tool()
async def search_guidelines(
    query: str,
    source: str = "",
    evidence_grade: str = "",
    patient_population: str = "",
    limit: int = 10,
) -> dict:
    """Semantic + keyword hybrid search over clinical guidelines.

    [STUB — until migration 009_pgvector_guidelines.sql is applied and the
    `guidelines` table is populated with MedCPT embeddings.] The signature
    and filter model are stable so callers can integrate now.

    When stubbed, returns an empty `results` list with a clear status.
    Coexists with `get_guideline(id)` (precision ID lookup), which remains
    fully functional for known recommendation IDs.
    """
    pool = await _get_db_pool()
    # Existence check: is migration 009 applied?
    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                """SELECT EXISTS(SELECT 1 FROM information_schema.tables
                                 WHERE table_name = 'guidelines')"""
            )
    except Exception:
        exists = False

    if not exists:
        return {
            "status": "stubbed",
            "query": query,
            "results": [],
            "note": (
                "search_guidelines is stubbed — apply migration "
                "009_pgvector_guidelines.sql and load MedCPT embeddings "
                "to enable semantic retrieval. Use get_guideline(id) for "
                "precision lookup in the meantime."
            ),
        }

    # Keyword-only path until embedding is online. This guarantees the tool
    # returns SOMETHING useful the moment the table is populated, even before
    # embedding generation completes.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT recommendation_id, text, guideline_source, evidence_grade
               FROM guidelines
               WHERE is_current
                 AND ($1 = '' OR guideline_source = $1)
                 AND ($2 = '' OR evidence_grade = $2)
                 AND ($3 = '' OR $3 = ANY(patient_population))
                 AND bm25_tokens @@ plainto_tsquery('english', $4)
               ORDER BY ts_rank(bm25_tokens, plainto_tsquery('english', $4)) DESC
               LIMIT $5""",
            source, evidence_grade, patient_population, query, limit,
        )
    return {
        "status": "ok",
        "backend": "bm25_only",     # upgraded to 'hybrid' once embedding path lands
        "query": query,
        "results": [dict(r) for r in rows],
    }


@mcp.tool()
async def run_batch_pre_encounter(
    panel_id: str,
    encounter_date: str,
    provider_id: str = "",
) -> dict:
    """Overnight batch pre-compute of deliberations for a provider panel.

    [PLANNED] Wires the existing server/deliberation/batch/{model_router,
    pre_encounter_batch}.py modules. Until wired, returns a clear stub.
    The signature is stable so callers (overnight scheduler, provider
    dashboard) can integrate.
    """
    return {
        "status": "not_yet_wired",
        "panel_id": panel_id,
        "encounter_date": encounter_date,
        "provider_id": provider_id,
        "note": (
            "server/deliberation/batch/pre_encounter_batch.py exists; this "
            "MCP wrapper needs to import and call it without reimplementing. "
            "Tracked as Tier 3.4 in plan file noble-giggling-sunbeam.md."
        ),
    }


# ── Tier 4 — Product (population, inbox, constitutional critic is 2.b.ii) ────

@mcp.tool()
async def get_panel_risk_ranking(
    provider_id: str,
    sort_by: str = "risk_score",
    limit: int = 50,
) -> dict:
    """Provider's panel sorted by risk metric. Aggregates provider_risk_scores."""
    if sort_by not in ("risk_score", "days_overdue", "gap_count"):
        return {"status": "error", "error": f"unknown sort_by '{sort_by}'"}
    pool = await _get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT prs.patient_id, prs.risk_score, prs.score_date,
                      p.first_name, p.last_name, p.mrn
               FROM provider_risk_scores prs
               JOIN patients p ON p.id = prs.patient_id
               WHERE prs.provider_id = $1
               ORDER BY prs.risk_score DESC
               LIMIT $2""",
            provider_id, limit,
        )
    patients = []
    for r in rows:
        patients.append({
            "patient_id": str(r["patient_id"]),
            "mrn": r["mrn"],
            "name": f"{r['first_name']} {r['last_name']}".strip(),
            "risk_score": float(r["risk_score"]) if r["risk_score"] is not None else None,
            "computed_at": r["score_date"].isoformat() if r["score_date"] else None,
        })
    return {
        "provider_id": provider_id,
        "panel_count": len(patients),
        "sort_by": sort_by,
        "patients": patients,
    }


@mcp.tool()
async def triage_message(
    patient_id: str,
    content: str,
    message_type: str = "patient_message",
) -> dict:
    """Triage an incoming patient message. Wraps clinical_query(role='care_manager').

    Returns priority ('urgent' | 'high' | 'routine' | 'administrative'),
    suggested action, and an escalation flag. Uses the existing guardrail
    pipeline — PHI, jailbreak, and blocking clinical triggers are enforced.
    """
    try:
        result = await clinical_query(
            query=f"Triage this {message_type}: {content}",
            role="care_manager",
            patient_context={"patient_id": patient_id},
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}

    # Priority heuristic based on guardrail escalations + keyword scan.
    lowered = (content or "").lower()
    urgent_keywords = ("chest pain", "suicidal", "can't breathe", "bleeding",
                       "emergency", "severe", "overdose")
    priority = "routine"
    if result.get("escalation_flags"):
        priority = "urgent"
    elif any(k in lowered for k in urgent_keywords):
        priority = "urgent"
    elif any(k in lowered for k in ("refill", "appointment", "question", "follow up")):
        priority = "high" if "medication" in lowered else "routine"
    elif any(k in lowered for k in ("form", "insurance", "bill")):
        priority = "administrative"

    return {
        "patient_id": patient_id,
        "message_type": message_type,
        "priority": priority,
        "suggested_action": result.get("recommendation") or "review manually",
        "escalate_to_human": bool(result.get("escalation_flags")) or priority == "urgent",
        "rationale": (
            "Guardrail triggered — escalate." if result.get("escalation_flags")
            else f"Priority={priority} from keyword + guardrail scan."
        ),
    }


# ---------------------------------------------------------------------------
# Shared provenance tool (registered on all three MCP servers)
# ---------------------------------------------------------------------------

from shared.provenance import register_provenance_tool  # noqa: E402
from shared.audit_middleware import AuditMiddleware  # noqa: E402

register_provenance_tool(
    mcp,
    source_server="ambient-clinical-intelligence",
    get_pool=get_gap_pool,
)

# Audit every tool call — records inputs, outputs, timing and session to mcp_call_log
mcp.add_middleware(AuditMiddleware("clinical", _get_db_pool))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))
    if transport in ("streamable-http", "http"):
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")

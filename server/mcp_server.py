"""FastMCP server for clinical decision support — Phase 1.

Provides 15 tools:
1.  clinical_query              — Three-layer guardrail pipeline with Claude API
2.  get_guideline               — Fetch specific guideline by recommendation ID
3.  check_screening_due         — Return overdue USPSTF screenings for a patient
4.  flag_drug_interaction       — Return known drug interactions
5.  get_synthetic_patient       — Return canonical demo patient (Maria Chen)
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
from datetime import datetime as _dt
from pathlib import Path

# Allow the clinical server to import FHIR transforms that live in mcp-server/
_MCPSERVER_DIR = Path(__file__).resolve().parent.parent / "mcp-server"
if str(_MCPSERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_MCPSERVER_DIR))

import anthropic
import asyncpg
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from server.guardrails.input_validator import validate_input
from server.guardrails.output_validator import validate_output
from server.guardrails.clinical_rules import check_escalation
from server.deliberation.engine import DeliberationEngine
from server.deliberation.schemas import DeliberationRequest

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("ClinicalIntelligence")

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

# Canonical demo patient — Maria Chen, MRN 4829341
_MARIA_CHEN: dict = {
    "mrn": "4829341",
    "first_name": "Maria",
    "last_name": "Chen",
    "date_of_birth": "1972-03-15",
    "age": 54,
    "sex": "female",
    "gender": "female",
    "race": "Asian",
    "ethnicity": "Chinese American",
    "preferred_language": "English",
    "address": {
        "line": "742 Maple Drive",
        "city": "Riverside",
        "state": "CA",
        "zip": "92501",
    },
    "insurance": {
        "type": "commercial",
        "plan": "Blue Cross PPO",
        "member_id": "BCX892741",
    },
    "primary_care_provider": {
        "name": "Dr. Rahul Patel",
        "practice": "Patel Family Medicine",
        "npi": "1234567890",
    },
    "conditions": [
        {
            "code": "E11.9",
            "display": "Type 2 diabetes mellitus without complications",
            "clinical_status": "active",
            "onset_date": "2019-06-15",
        },
        {
            "code": "I10",
            "display": "Essential hypertension",
            "clinical_status": "active",
            "onset_date": "2018-03-22",
        },
        {
            "code": "E78.5",
            "display": "Hyperlipidemia, unspecified",
            "clinical_status": "active",
            "onset_date": "2018-09-10",
        },
        {
            "code": "E66.01",
            "display": "Morbid (severe) obesity due to excess calories",
            "clinical_status": "active",
            "onset_date": "2017-01-08",
        },
    ],
    "medications": [
        {
            "name": "metformin",
            "dose": "1000 mg",
            "frequency": "twice daily",
            "status": "active",
            "start_date": "2019-07-01",
        },
        {
            "name": "lisinopril",
            "dose": "20 mg",
            "frequency": "once daily",
            "status": "active",
            "start_date": "2018-04-15",
        },
        {
            "name": "atorvastatin",
            "dose": "40 mg",
            "frequency": "once daily at bedtime",
            "status": "active",
            "start_date": "2018-10-01",
        },
    ],
    "labs": {
        "hba1c": {"value": 7.8, "unit": "%", "date": "2026-02-15", "reference_range": "<7.0"},
        "egfr": {"value": 62, "unit": "mL/min/1.73m2", "date": "2026-02-15", "reference_range": ">60"},
        "ldl": {"value": 128, "unit": "mg/dL", "date": "2026-02-15", "reference_range": "<100"},
        "creatinine": {"value": 1.2, "unit": "mg/dL", "date": "2026-02-15", "reference_range": "0.6-1.2"},
        "uacr": {"value": 45, "unit": "mg/g", "date": "2026-02-15", "reference_range": "<30"},
        "fasting_glucose": {"value": 156, "unit": "mg/dL", "date": "2026-03-01", "reference_range": "70-100"},
        "total_cholesterol": {"value": 218, "unit": "mg/dL", "date": "2026-02-15", "reference_range": "<200"},
        "triglycerides": {"value": 185, "unit": "mg/dL", "date": "2026-02-15", "reference_range": "<150"},
        "potassium": {"value": 4.2, "unit": "mEq/L", "date": "2026-02-15", "reference_range": "3.5-5.0"},
    },
    "vitals": {
        "blood_pressure": {"systolic": 142, "diastolic": 88, "date": "2026-03-20"},
        "heart_rate": {"value": 78, "unit": "bpm", "date": "2026-03-20"},
        "bmi": {"value": 33.2, "unit": "kg/m2", "date": "2026-03-20"},
        "weight": {"value": 89.5, "unit": "kg", "date": "2026-03-20"},
        "height": {"value": 164, "unit": "cm", "date": "2026-03-20"},
    },
    "care_gaps": [
        {
            "type": "screening",
            "description": "Colorectal cancer screening overdue — last colonoscopy 2015",
            "uspstf_grade": "A",
            "status": "open",
        },
        {
            "type": "screening",
            "description": "Depression screening (PHQ-9) not completed in past 12 months",
            "uspstf_grade": "B",
            "status": "open",
        },
        {
            "type": "monitoring",
            "description": "Diabetic retinopathy exam overdue — last exam 2024-01",
            "status": "open",
        },
        {
            "type": "monitoring",
            "description": "Podiatry referral for annual diabetic foot exam — not completed",
            "status": "open",
        },
    ],
    "sdoh_flags": [
        {
            "domain": "food_access",
            "severity": "moderate",
            "detail": "Reports difficulty affording fresh produce; relies on processed foods",
        },
        {
            "domain": "transportation",
            "severity": "low",
            "detail": "Occasional difficulty getting to appointments; depends on family for rides",
        },
    ],
    "family_history": [
        {"condition": "Type 2 diabetes", "relation": "mother"},
        {"condition": "Coronary artery disease", "relation": "father", "age_of_onset": 58},
        {"condition": "Breast cancer", "relation": "maternal aunt", "age_of_onset": 62},
    ],
    "social_history": {
        "tobacco": "never",
        "alcohol": "occasional — 1-2 glasses wine per week",
        "exercise": "walks 20 min 3x/week",
        "occupation": "administrative assistant",
        "marital_status": "married",
    },
    "allergies": [
        {"substance": "sulfa drugs", "reaction": "rash", "severity": "moderate"},
    ],
}


@mcp.tool()
async def get_synthetic_patient(mrn: str) -> dict:
    """Return synthetic patient data for the given MRN.

    Maria Chen (MRN 4829341) is the canonical demo patient with conditions
    appropriate for demonstrating diabetes management, cardiovascular risk,
    and preventive care gaps.

    Args:
        mrn: Medical record number (e.g., '4829341').

    Returns:
        Full synthetic patient record dict, or error dict if MRN not found.
    """
    if mrn == "4829341":
        return _MARIA_CHEN

    return {
        "error": f"Patient with MRN '{mrn}' not found.",
        "hint": "Use MRN '4829341' for the canonical demo patient (Maria Chen).",
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
            await conn.execute(
                """
                INSERT INTO source_freshness
                    (patient_id, source_name, last_ingested_at, records_count, ttl_hours)
                VALUES ($1,$2,NOW(),0,24)
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

        duration_ms = int((_time.time() - start) * 1000)
        return json.dumps({
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
        }, indent=2)

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
    return JSONResponse({"ok": True, "server": "ClinicalIntelligence", "version": "1.0.0"})


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
async def rest_get_synthetic_patient(request: Request) -> JSONResponse:
    mrn = request.query_params.get("mrn", "")
    result = await get_synthetic_patient(mrn=mrn)
    return JSONResponse(result)


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
    """Placeholder vector store until pgvector/pinecone is configured."""

    async def similarity_search(self, query: str, k: int = 10, **kwargs) -> list[dict]:
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


@mcp.tool()
async def run_deliberation(
    patient_id: str,
    trigger_type: str = "manual",
    max_rounds: int = 3,
    mode: str = "progressive",
) -> dict:
    """
    Trigger a deliberation session for a patient.

    Supports two modes:
      - "progressive" (default): Tiered context loading — starts with minimal
        data and fetches more on demand. Prevents context overflow crashes.
      - "full": Original dual-LLM pipeline (Claude + GPT-4 cross-critique).

    Args:
        patient_id: Patient MRN or internal ID
        trigger_type: One of: scheduled_pre_encounter, lab_result_received,
                      medication_change, missed_appointment, temporal_threshold, manual
        max_rounds: Maximum deliberation rounds (1-5, default 3)
        mode: "progressive" (tiered loading) or "full" (dual-LLM pipeline)

    Returns:
        deliberation_id, status, summary of five output categories,
        context_stats (progressive mode), convergence_score (full mode)
    """
    engine = await get_deliberation_engine()

    if mode == "progressive":
        return await engine.run_progressive(
            DeliberationRequest(
                patient_id=patient_id,
                trigger_type=trigger_type,
                max_rounds=max_rounds,
            )
        )

    # Full dual-LLM mode (original pipeline)
    result = await engine.run(
        DeliberationRequest(
            patient_id=patient_id,
            trigger_type=trigger_type,
            max_rounds=max_rounds
        )
    )
    return {
        "deliberation_id": result.deliberation_id,
        "status": "complete",
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
    target: str = "patient"
) -> dict:
    """
    Retrieve nudges queued for delivery but not yet sent.
    Used by notification scheduler and care manager dashboard.

    Args:
        patient_id: Patient MRN or internal ID
        target: 'patient' | 'care_team'

    Returns:
        Pending nudges with trigger conditions and channel content.
    """
    nudge_type = f"{target}_nudge"
    pool = await _get_db_pool()
    async with pool.acquire() as conn:
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
        return {
            "patient_id": patient_id,
            "target": target,
            "pending_count": len(rows),
            "nudges": [dict(r) for r in rows]
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

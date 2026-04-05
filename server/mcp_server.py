"""FastMCP server for clinical decision support — Phase 1.

Provides 5 tools:
1. clinical_query — Three-layer guardrail pipeline with Claude API
2. get_guideline — Fetch specific guideline by recommendation ID
3. check_screening_due — Return overdue USPSTF screenings for a patient
4. flag_drug_interaction — Return known drug interactions
5. get_synthetic_patient — Return canonical demo patient (Maria Chen)

All AI calls route through the guardrail pipeline. HTML prototypes never
call Claude API directly.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import anthropic
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from server.guardrails.input_validator import validate_input
from server.guardrails.output_validator import validate_output
from server.guardrails.clinical_rules import check_escalation

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

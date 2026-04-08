"""
Parses deliberation round output for data_requests and missing_data_flags
that signal the agent needs more data before the next round.
"""

import json
import logging

log = logging.getLogger(__name__)

# Lab tests that trigger Tier 2 trend loading when flagged
METABOLIC_TESTS = {
    "hba1c", "hemoglobin a1c", "glucose", "a1c",
    "alt", "ast", "ldl", "hdl", "cholesterol", "triglyceride",
    "creatinine", "egfr", "bun", "uric acid",
}


def parse_data_requests(deliberation_output: dict) -> dict:
    """
    Inspect a deliberation round's output for signals that more data is needed.

    Returns:
    {
        "load_tier2": bool,
        "requested_tests": [str],      # specific lab tests to trend
        "on_demand_requests": [dict],   # parsed data_request objects
        "has_requests": bool,
    }
    """
    result = {
        "load_tier2": False,
        "requested_tests": [],
        "on_demand_requests": [],
        "has_requests": False,
    }

    # 1. Explicit data_requests field (agent emits these in its JSON)
    explicit = deliberation_output.get("data_requests", [])
    if isinstance(explicit, list) and explicit:
        result["on_demand_requests"] = [
            r for r in explicit
            if isinstance(r, dict) and r.get("type")
        ]
        result["has_requests"] = True

    # 2. missing_data_flags -> map to tier loading
    flags = deliberation_output.get("missing_data_flags", [])
    if not isinstance(flags, list):
        flags = []

    for flag in flags:
        flag_data = flag if isinstance(flag, dict) else {}
        try:
            if isinstance(flag, str):
                flag_data = json.loads(flag)
        except Exception:
            pass

        data_type = flag_data.get("data_type", "").lower()
        description = flag_data.get("description", "").lower()
        priority = flag_data.get("priority", "").lower()

        # Critical lab flags -> load Tier 2 + specific test trends
        if data_type in ("lab_result", "lab_trend", "lab") or "lab" in description:
            result["load_tier2"] = True
            result["has_requests"] = True
            for test in METABOLIC_TESTS:
                if test in description:
                    result["requested_tests"].append(test)

        # Medication flags -> load Tier 2
        if data_type in ("medication", "medication_history") or "medication" in description:
            result["load_tier2"] = True
            result["has_requests"] = True

        # High-priority flags for any data type -> Tier 2
        if priority in ("critical", "high"):
            result["load_tier2"] = True
            result["has_requests"] = True

        # Imaging/note flags -> on-demand fetch
        if data_type in ("imaging", "clinical_note", "screening") or "imaging" in description:
            result["on_demand_requests"].append({
                "type": "imaging_report",
                "reason": flag_data.get("description", "missing imaging data"),
            })
            result["has_requests"] = True

    # 3. Anticipatory scenarios that reference specific resources
    scenarios = deliberation_output.get("anticipatory_scenarios", [])
    if not isinstance(scenarios, list):
        scenarios = []

    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        evidence = scenario.get("evidence_basis", [])
        if not isinstance(evidence, list):
            continue
        for ev in evidence:
            ev_lower = str(ev).lower()
            if "ultrasound" in ev_lower or "imaging" in ev_lower:
                result["on_demand_requests"].append({
                    "type": "imaging_report",
                    "reason": f"scenario references: {str(ev)[:100]}",
                })
                result["has_requests"] = True

    # Deduplicate on_demand_requests by type+resource_id
    seen: set[tuple] = set()
    deduped = []
    for req in result["on_demand_requests"]:
        key = (req.get("type"), req.get("resource_id", ""), req.get("test", ""))
        if key not in seen:
            seen.add(key)
            deduped.append(req)
    result["on_demand_requests"] = deduped[:3]  # max 3 per round

    return result

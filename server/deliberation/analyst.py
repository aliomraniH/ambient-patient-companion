"""
analyst.py — Phase 1: Parallel independent analysis by Claude and GPT-4.
Both models receive identical patient context; neither sees the other's output.
"""
import asyncio
import json
import time
from pathlib import Path
import anthropic
import openai
from .schemas import IndependentAnalysis, PatientContextPackage
from .json_utils import strip_markdown_fences


CLAUDE_MODEL = "claude-sonnet-4-20250514"
GPT4_MODEL = "gpt-4o"

_anthropic_client: anthropic.AsyncAnthropic | None = None
_openai_client: openai.AsyncOpenAI | None = None


def _get_anthropic_client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic()
    return _anthropic_client


def _get_openai_client() -> openai.AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.AsyncOpenAI()
    return _openai_client


def _load_prompt(filename: str, substitutions: dict) -> str:
    """Load XML prompt template and substitute placeholders."""
    path = Path(__file__).parent / "prompts" / filename
    template = path.read_text()
    for key, value in substitutions.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template


async def _analyze_with_claude(
    context: PatientContextPackage,
    guidelines_json: str,
    prior_knowledge_json: str
) -> IndependentAnalysis:
    """Run Claude as the Diagnostic Reasoning Analyst."""
    system_prompt = _load_prompt("analyst_claude.xml", {
        "PATIENT_CONTEXT_JSON": context.model_dump_json(indent=2),
        "GUIDELINES_JSON": guidelines_json,
        "PRIOR_KNOWLEDGE_JSON": prior_knowledge_json
    })

    response = await _get_anthropic_client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": "Produce your independent clinical analysis now."
        }]
    )
    raw = response.content[0].text
    analysis = IndependentAnalysis.model_validate_json(strip_markdown_fences(raw))
    # Capture real token usage so engine.py can accumulate totals
    usage = getattr(response, "usage", None)
    if usage is not None:
        analysis._token_count = (
            getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0)
        )
    else:
        analysis._token_count = 0
    return analysis


async def _analyze_with_gpt4(
    context: PatientContextPackage,
    guidelines_json: str,
    prior_knowledge_json: str
) -> IndependentAnalysis:
    """Run GPT-4 as the Treatment Optimization Analyst."""
    system_prompt = _load_prompt("analyst_gpt4.xml", {
        "PATIENT_CONTEXT_JSON": context.model_dump_json(indent=2),
        "GUIDELINES_JSON": guidelines_json,
        "PRIOR_KNOWLEDGE_JSON": prior_knowledge_json
    })

    response = await _get_openai_client().chat.completions.create(
        model=GPT4_MODEL,
        max_tokens=2048,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",
             "content": "Produce your independent clinical analysis now."}
        ],
        response_format={"type": "json_object"}
    )
    raw = response.choices[0].message.content
    analysis = IndependentAnalysis.model_validate_json(strip_markdown_fences(raw))
    # Capture real token usage from OpenAI response
    usage = getattr(response, "usage", None)
    if usage is not None:
        analysis._token_count = getattr(usage, "total_tokens", 0)
    else:
        analysis._token_count = 0
    return analysis


async def run_parallel_analysis(
    context: PatientContextPackage
) -> tuple[IndependentAnalysis, IndependentAnalysis]:
    """
    Phase 1: Run Claude and GPT-4 in parallel.
    Returns (claude_analysis, gpt4_analysis).
    Raises if either model fails — do not proceed with partial output.
    """
    guidelines_json = json.dumps(context.applicable_guidelines, indent=2)
    prior_knowledge_json = json.dumps(context.prior_patient_knowledge, indent=2)

    claude_task = _analyze_with_claude(context, guidelines_json, prior_knowledge_json)
    gpt4_task = _analyze_with_gpt4(context, guidelines_json, prior_knowledge_json)

    # asyncio.gather raises on first exception — both must succeed
    claude_analysis, gpt4_analysis = await asyncio.gather(claude_task, gpt4_task)

    # Tag each with model identity
    claude_analysis.model_id = CLAUDE_MODEL
    claude_analysis.role_emphasis = "diagnostic_reasoning"
    gpt4_analysis.model_id = GPT4_MODEL
    gpt4_analysis.role_emphasis = "treatment_optimization"

    return claude_analysis, gpt4_analysis

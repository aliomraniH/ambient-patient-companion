"""
engine.py — Orchestrates all 5 phases of the Dual-LLM Deliberation Engine.
This is the single entry point for triggering a deliberation.
"""
import asyncio
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
import anthropic
import openai
from .schemas import DeliberationRequest, DeliberationResult, PatientContextPackage
from .context_compiler import compile_patient_context
from .analyst import run_parallel_analysis
from .critic import run_critique_rounds
from .synthesizer import synthesize
from .behavioral_adapter import adapt_nudges
from .knowledge_store import commit_deliberation


_anthropic_client = anthropic.AsyncAnthropic()
_openai_client = openai.AsyncOpenAI()


def _load_prompt(filename: str, substitutions: dict) -> str:
    path = Path(__file__).parent / "prompts" / filename
    template = path.read_text()
    for key, value in substitutions.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template


async def _call_claude(model: str, system: str, user: str,
                       max_tokens: int = 2048) -> str:
    resp = await _anthropic_client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}]
    )
    return resp.content[0].text


async def _call_gpt4(model: str, system: str, user: str,
                     max_tokens: int = 2048) -> str:
    resp = await _openai_client.chat.completions.create(
        model=model, max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ],
        response_format={"type": "json_object"}
    )
    return resp.choices[0].message.content


class DeliberationEngine:
    """
    Dual-LLM Deliberation Engine.
    Runs asynchronously. All phases logged for audit.
    """
    def __init__(self, db_pool, vector_store):
        self.db_pool = db_pool
        self.vector_store = vector_store

    async def run(
        self,
        request: DeliberationRequest
    ) -> DeliberationResult:
        """
        Full 5-phase deliberation pipeline.
        Raises on any phase failure — no partial commits.
        """
        deliberation_id = str(uuid.uuid4())
        start_time = time.monotonic()
        total_tokens = 0

        # ── Phase 0: Context Compilation ──────────────────────────────────────
        context: PatientContextPackage = await compile_patient_context(
            patient_id=request.patient_id,
            db_pool=self.db_pool,
            vector_store=self.vector_store
        )
        context.deliberation_trigger = request.trigger_type

        # ── Phase 1: Parallel Independent Analysis ─────────────────────────────
        claude_analysis, gpt4_analysis = await run_parallel_analysis(context)
        total_tokens += getattr(claude_analysis, "_token_count", 0)
        total_tokens += getattr(gpt4_analysis, "_token_count", 0)

        # ── Phase 2: Cross-Critique Rounds ─────────────────────────────────────
        critique_result = await run_critique_rounds(
            claude_analysis=claude_analysis,
            gpt4_analysis=gpt4_analysis,
            context=context,
            max_rounds=request.max_rounds,
            load_prompt_fn=_load_prompt,
            call_claude_fn=_call_claude,
            call_gpt4_fn=_call_gpt4
        )

        # ── Phase 3: Synthesis ─────────────────────────────────────────────────
        result: DeliberationResult = await synthesize(
            transcript=critique_result["transcript"],
            context=context,
            deliberation_id=deliberation_id,
            load_prompt_fn=_load_prompt,
            call_claude_fn=_call_claude
        )

        # ── Phase 4: Behavioral Adaptation ────────────────────────────────────
        result.nudge_content = adapt_nudges(result.nudge_content)

        # ── Phase 5: Knowledge Commit ──────────────────────────────────────────
        await commit_deliberation(
            result=result,
            db_pool=self.db_pool,
            convergence_score=critique_result["convergence_score"],
            rounds_completed=critique_result["rounds_completed"],
            total_tokens=total_tokens,
            total_latency_ms=int((time.monotonic() - start_time) * 1000),
            synthesizer_model="claude-sonnet-4-20250514"
        )

        return result

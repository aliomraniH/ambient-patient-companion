"""
engine.py — Orchestrates all 5 phases of the Dual-LLM Deliberation Engine.
This is the single entry point for triggering a deliberation.

Supports two modes:
  run()             — original full dual-LLM pipeline (loads all context upfront)
  run_progressive() — progressive context loading with tiered demand-fetch loop
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, date
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
from .tiered_context_loader import TieredContextLoader, TOTAL_BUDGET
from .data_request_parser import parse_data_requests
from .json_utils import strip_markdown_fences

log = logging.getLogger(__name__)


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
    path = Path(__file__).parent / "prompts" / filename
    template = path.read_text()
    for key, value in substitutions.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template


async def _call_claude(model: str, system: str, user: str,
                       max_tokens: int = 2048) -> str:
    resp = await _get_anthropic_client().messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}]
    )
    return resp.content[0].text


async def _call_gpt4(model: str, system: str, user: str,
                     max_tokens: int = 2048) -> str:
    resp = await _get_openai_client().chat.completions.create(
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

        # Stamp engine-level metrics on result before commit and return
        total_latency_ms = int((time.monotonic() - start_time) * 1000)
        result.rounds_completed = critique_result["rounds_completed"]
        result.convergence_score = critique_result["convergence_score"]
        result.total_tokens = total_tokens
        result.total_latency_ms = total_latency_ms

        # ── Phase 5: Knowledge Commit ──────────────────────────────────────────
        await commit_deliberation(
            result=result,
            db_pool=self.db_pool,
            convergence_score=result.convergence_score,
            rounds_completed=result.rounds_completed,
            total_tokens=result.total_tokens,
            total_latency_ms=result.total_latency_ms,
            synthesizer_model="claude-sonnet-4-20250514"
        )

        return result

    # ── Progressive Context Loading Mode ──────────────────────────────────

    async def run_progressive(
        self,
        request: DeliberationRequest,
    ) -> dict:
        """
        Progressive deliberation loop. Loads data lazily based on agent signals.
        Never exceeds TOTAL_BUDGET chars in context.

        Phase 0:  Load Tier 1 (~1,500 chars) + patient demographics
        Round loop (max_rounds):
          - Run single deliberation round (Claude haiku, fast)
          - Parse output for data_requests via DataRequestParser
          - If no requests -> break
          - Load Tier 2 / on-demand data, merge into context
        Final: Synthesize all round outputs -> commit to DB
        """
        deliberation_id = str(uuid.uuid4())
        start_time = time.monotonic()
        loader = TieredContextLoader(self.db_pool, request.patient_id)
        all_outputs: list[dict] = []
        rounds_completed = 0

        # ── Phase 0: Static metadata (patient demographics) ──────────────
        static_ctx = await self._build_static_context(request.patient_id)

        # ── Phase 0b: Always load Tier 1 ─────────────────────────────────
        tier1_ctx = await loader.load_tier1()
        context = {**static_ctx, **tier1_ctx}

        for round_num in range(1, request.max_rounds + 1):
            rounds_completed = round_num

            # ── Run one deliberation round ────────────────────────────────
            context_json = json.dumps(context)

            if len(context_json) > TOTAL_BUDGET:
                log.warning(
                    "Round %d: context %d chars exceeds budget %d",
                    round_num, len(context_json), TOTAL_BUDGET,
                )

            round_output = await self._run_one_deliberation_round(
                context_json=context_json,
                round_number=round_num,
                trigger_type=request.trigger_type,
                prior_outputs=all_outputs,
            )

            if round_output.get("status") == "error":
                log.error("Round %d failed: %s", round_num, round_output.get("error"))
                break

            all_outputs.append(round_output)

            # ── Parse output for data requests ────────────────────────────
            parsed_requests = parse_data_requests(round_output)

            if not parsed_requests["has_requests"]:
                log.info("Round %d: no data requests — deliberation complete", round_num)
                break

            if round_num >= request.max_rounds:
                log.info("Round %d: max_rounds reached — stopping", round_num)
                break

            # ── Load additional data based on requests ────────────────────
            context_additions: dict = {}

            if parsed_requests["load_tier2"] and 2 not in loader._loaded_tiers:
                tier2_ctx = await loader.load_tier2(
                    requested_tests=parsed_requests["requested_tests"] or None
                )
                context_additions.update(tier2_ctx)

            for req in parsed_requests["on_demand_requests"]:
                # Record the request in DB
                try:
                    async with self.db_pool.acquire() as conn:
                        await conn.execute(
                            """INSERT INTO deliberation_data_requests
                                (deliberation_id, round_number, request_type,
                                 resource_id, reason, fulfilled)
                               VALUES ($1, $2, $3, $4, $5, false)""",
                            deliberation_id,
                            round_num,
                            req.get("type", "unknown"),
                            req.get("resource_id", ""),
                            req.get("reason", ""),
                        )
                except Exception as e:
                    log.warning("Failed to log data request: %s", e)

                fetched = await loader.load_on_demand(req)
                if fetched:
                    context_additions.update(fetched)
                    # Mark fulfilled
                    try:
                        async with self.db_pool.acquire() as conn:
                            await conn.execute(
                                """UPDATE deliberation_data_requests
                                   SET fulfilled = true,
                                       fulfilled_chars = $1,
                                       fulfilled_at = NOW()
                                   WHERE deliberation_id = $2
                                     AND round_number = $3
                                     AND request_type = $4
                                     AND resource_id = $5""",
                                len(json.dumps(fetched)),
                                deliberation_id,
                                round_num,
                                req.get("type", ""),
                                req.get("resource_id", ""),
                            )
                    except Exception as e:
                        log.warning("Failed to update data request: %s", e)

            if not context_additions:
                log.info(
                    "Round %d: data requested but nothing new fetched — stopping",
                    round_num,
                )
                break

            # Merge additions into context for next round
            context = {**context, **context_additions}
            context["_context_stats"] = loader.context_summary()

        # ── Synthesize and commit ─────────────────────────────────────────
        final_output = self._synthesize_round_outputs(all_outputs)
        total_latency_ms = int((time.monotonic() - start_time) * 1000)

        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO deliberations
                       (id, patient_id, trigger_type, started_at, status,
                        rounds_completed, convergence_score,
                        synthesizer_model, model_claude, model_gpt4,
                        total_tokens, total_latency_ms, transcript)
                       VALUES ($1,$2,$3,$4,'complete',$5,$6,$7,$8,$9,$10,$11,$12)""",
                    deliberation_id,
                    request.patient_id,
                    request.trigger_type,
                    datetime.utcnow(),
                    rounds_completed,
                    0.0,
                    "claude-haiku-4-5-20251001",
                    "n/a",
                    "claude-haiku-4-5-20251001",
                    0,
                    total_latency_ms,
                    json.dumps({"rounds": all_outputs}),
                )

                for output_type in (
                    "anticipatory_scenario",
                    "predicted_patient_question",
                    "missing_data_flag",
                    "patient_nudge",
                    "care_team_nudge",
                ):
                    items = final_output.get(f"{output_type}s", [])
                    if output_type == "missing_data_flag":
                        items = final_output.get("missing_data_flags", [])
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        await conn.execute(
                            """INSERT INTO deliberation_outputs
                               (deliberation_id, output_type, output_data,
                                priority, confidence)
                               VALUES ($1, $2, $3, $4, $5)""",
                            deliberation_id,
                            output_type,
                            json.dumps(item),
                            item.get("priority"),
                            item.get("confidence") or item.get("probability"),
                        )

                # ── Flag lifecycle: write flags to registry + review ──
                try:
                    from .flag_writer import write_flag
                    from .flag_reviewer import run_flag_review

                    for item in final_output.get("missing_data_flags", []):
                        if isinstance(item, dict):
                            await write_flag(
                                conn, request.patient_id,
                                deliberation_id, item,
                            )

                    await run_flag_review(
                        self.db_pool, request.patient_id,
                        "post_deliberation", deliberation_id,
                        f"New deliberation {deliberation_id}",
                    )
                except Exception as flag_err:
                    log.warning(
                        "Post-deliberation flag lifecycle failed (non-fatal): %s",
                        flag_err,
                    )

        except Exception as e:
            log.error("Failed to commit deliberation: %s", e)
            return {
                "deliberation_id": deliberation_id,
                "status": "error",
                "error": str(e),
                "patient_id": request.patient_id,
                "rounds_completed": rounds_completed,
                "context_stats": loader.context_summary(),
            }

        return {
            "deliberation_id": deliberation_id,
            "status": "complete",
            "patient_id": request.patient_id,
            "rounds_completed": rounds_completed,
            "context_stats": loader.context_summary(),
            "summary": self._summarize_output(final_output),
        }

    async def _build_static_context(self, patient_id: str) -> dict:
        """Build minimal static context: patient demographics + trigger metadata."""
        async with self.db_pool.acquire() as conn:
            import re
            _UUID_RE = re.compile(
                r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                re.IGNORECASE,
            )
            row = await conn.fetchrow(
                """SELECT first_name, last_name, birth_date, gender, mrn
                   FROM patients WHERE mrn = $1""",
                patient_id,
            )
            if row is None and _UUID_RE.match(patient_id):
                row = await conn.fetchrow(
                    """SELECT first_name, last_name, birth_date, gender, mrn
                       FROM patients WHERE id = $1::uuid""",
                    patient_id,
                )
            if row is None:
                row = await conn.fetchrow(
                    """SELECT first_name, last_name, birth_date, gender, mrn
                       FROM patients WHERE mrn LIKE $1""",
                    f"%{patient_id}%",
                )
            if row is None:
                return {"patient_name": "Unknown", "age": 0, "sex": "unknown"}

            age = 0
            if row["birth_date"]:
                bd = row["birth_date"]
                today = date.today()
                age = today.year - bd.year - (
                    (today.month, today.day) < (bd.month, bd.day)
                )

            return {
                "patient_name": f"{row['first_name']} {row['last_name']}",
                "age": age,
                "sex": row["gender"] or "unknown",
                "mrn": row["mrn"],
                "current_date": date.today().isoformat(),
            }

    async def _run_one_deliberation_round(
        self,
        context_json: str,
        round_number: int,
        trigger_type: str,
        prior_outputs: list,
    ) -> dict:
        """
        Single LLM deliberation call. Returns structured output dict.
        The system prompt instructs the model to emit data_requests when it
        needs more information.
        """
        prior_text = ""
        if prior_outputs:
            prior_text = (
                "\n\nPRIOR ROUND OUTPUTS (for context, do not repeat):\n"
                + json.dumps(prior_outputs[-1], indent=2)[:2000]
            )

        system = f"""You are a clinical AI deliberation agent performing round {round_number} of analysis.

Trigger: {trigger_type}
Round: {round_number}

CRITICAL OUTPUT RULES:
1. Respond ONLY with a single valid JSON object. No markdown, no preamble.
2. All string values must be properly escaped. No raw quotes inside strings.
3. If you need more patient data to complete your analysis, include a "data_requests" array.

JSON SCHEMA:
{{
  "anticipatory_scenarios": [...],
  "predicted_patient_questions": [...],
  "missing_data_flags": [...],
  "patient_nudges": [...],
  "care_team_nudge": {{...}},
  "data_requests": [
    {{
      "type": "lab_trend | clinical_note | encounter_detail | imaging_report",
      "resource_id": "optional specific ID",
      "test": "lab test name if type=lab_trend",
      "reason": "one sentence: what question this data will help answer"
    }}
  ]
}}

{_DATA_REQUEST_SCHEMA}

Keep all text values compact. Avoid long narratives inside JSON strings.{prior_text}
"""

        raw = ""
        try:
            response = await _get_anthropic_client().messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                system=system,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Patient context:\n{context_json}\n\n"
                        f"Provide your round {round_number} deliberation analysis."
                    ),
                }],
            )

            raw = response.content[0].text.strip()
            raw = strip_markdown_fences(raw)

            return json.loads(raw)

        except json.JSONDecodeError as e:
            log.error(
                "Round %d JSON parse failed: %s | preview: %s",
                round_number, e, raw[:300],
            )
            return {"status": "error", "error": str(e), "preview": raw[:300]}
        except Exception as e:
            log.error("Round %d API call failed: %s", round_number, e)
            return {"status": "error", "error": str(e)}

    def _synthesize_round_outputs(self, all_outputs: list[dict]) -> dict:
        """Merge all round outputs into a single consolidated output."""
        merged: dict = {
            "anticipatory_scenarios": [],
            "predicted_patient_questions": [],
            "missing_data_flags": [],
            "patient_nudges": [],
            "care_team_nudges": [],
            "knowledge_updates": [],
        }

        for output in all_outputs:
            if output.get("status") == "error":
                continue
            for key in merged:
                items = output.get(key, [])
                if isinstance(items, list):
                    merged[key].extend(items)
                elif isinstance(items, dict):
                    merged[key].append(items)
            nudge = output.get("care_team_nudge")
            if isinstance(nudge, dict) and nudge:
                merged["care_team_nudges"].append(nudge)

        return merged

    def _summarize_output(self, final_output: dict) -> dict:
        """Produce a compact summary dict of the final output for the MCP response."""
        return {
            "anticipatory_scenarios": len(final_output.get("anticipatory_scenarios", [])),
            "predicted_questions": len(final_output.get("predicted_patient_questions", [])),
            "missing_data_flags": len(final_output.get("missing_data_flags", [])),
            "nudges_generated": (
                len(final_output.get("patient_nudges", []))
                + len(final_output.get("care_team_nudges", []))
            ),
            "knowledge_updates": len(final_output.get("knowledge_updates", [])),
        }


# System prompt fragment for data request instructions
_DATA_REQUEST_SCHEMA = """
When you identify a missing_data_flag with priority "critical" or "high",
you may request specific additional data by including "data_requests" in your output:

"data_requests": [
  {
    "type": "lab_trend",
    "test": "HbA1c",
    "reason": "Need full A1c history to assess progression from prediabetes"
  },
  {
    "type": "imaging_report",
    "resource_id": "fyEZI5WFE3",
    "reason": "Need ultrasound findings to assess hepatic steatosis severity"
  },
  {
    "type": "clinical_note",
    "reason": "Need most recent visit note to understand medication decisions"
  }
]

The system will fetch this data and provide it in the next round.
Limit data_requests to 3 per round. Omit entirely if you have sufficient data.
"""

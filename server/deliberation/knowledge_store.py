"""
knowledge_store.py — Phase 5: Persist deliberation outputs to all stores.
Writes to: deliberations table, deliberation_outputs table,
           patient_knowledge table, core_knowledge_updates table.
Also queues nudges for delivery.
"""
import json
import logging
from datetime import datetime

from shared.coercion import coerce_confidence

from .schemas import DeliberationResult
from shared.coercion import coerce_confidence

log = logging.getLogger(__name__)


async def commit_deliberation(
    result: DeliberationResult,
    db_pool,
    convergence_score: float,
    rounds_completed: int,
    total_tokens: int,
    total_latency_ms: int,
    synthesizer_model: str
) -> str:
    """
    Phase 5: Atomic commit of all deliberation outputs inside a single
    database transaction.

    The deliberations session row is required — any failure raises and
    rolls back the entire transaction so orphaned output rows can never
    accumulate without a parent deliberation record.

    Individual output inserts are still wrapped in per-item try/except for
    detailed error logging, but each re-raises so the outer transaction
    catches and rolls back the full commit.

    Returns the deliberation_id on success.
    """
    async with db_pool.acquire() as conn:
        async with conn.transaction():

            # 1. Insert deliberation session (required — raise on failure)
            await conn.execute(
                """INSERT INTO deliberations
                   (id, patient_id, trigger_type, completed_at, status,
                    rounds_completed, convergence_score,
                    model_claude, model_gpt4, synthesizer_model,
                    total_tokens, total_latency_ms, transcript)
                   VALUES ($1,$2,$3,$4,'complete',$5,$6,$7,$8,$9,$10,$11,$12)""",
                result.deliberation_id,
                result.patient_id,
                result.trigger,
                datetime.utcnow(),
                rounds_completed,
                convergence_score,
                "claude-sonnet-4-20250514",
                "gpt-4o",
                synthesizer_model,
                total_tokens,
                total_latency_ms,
                json.dumps(result.transcript)
            )

            # 2. Insert all five output categories
            for scenario in result.anticipatory_scenarios:
                try:
                    await conn.execute(
                        """INSERT INTO deliberation_outputs
                           (deliberation_id, output_type, output_data,
                            priority, confidence, timeframe)
                           VALUES ($1,'anticipatory_scenario',$2,$3,$4,$5)""",
                        result.deliberation_id,
                        json.dumps(scenario.model_dump()),
                        "high" if scenario.probability > 0.7 else "medium",
                        coerce_confidence(scenario.confidence),
                        scenario.timeframe
                    )
                except Exception as e:
                    log.error("Failed to write anticipatory_scenario: %s", repr(e))
                    raise

            for question in result.predicted_patient_questions:
                try:
                    await conn.execute(
                        """INSERT INTO deliberation_outputs
                           (deliberation_id, output_type, output_data, confidence)
                           VALUES ($1,'predicted_patient_question',$2,$3)""",
                        result.deliberation_id,
                        json.dumps(question.model_dump()),
                        coerce_confidence(question.likelihood)
                    )
                except Exception as e:
                    log.error("Failed to write predicted_patient_question: %s", repr(e))
                    raise

            for flag in result.missing_data_flags:
                try:
                    await conn.execute(
                        """INSERT INTO deliberation_outputs
                           (deliberation_id, output_type, output_data,
                            priority, confidence)
                           VALUES ($1,'missing_data_flag',$2,$3,$4)""",
                        result.deliberation_id,
                        json.dumps(flag.model_dump()),
                        flag.priority,
                        coerce_confidence(flag.confidence)
                    )
                except Exception as e:
                    log.error("Failed to write missing_data_flag: %s", repr(e))
                    raise

            for nudge in result.nudge_content:
                try:
                    await conn.execute(
                        """INSERT INTO deliberation_outputs
                           (deliberation_id, output_type, output_data, trigger_condition)
                           VALUES ($1,$2,$3,$4)""",
                        result.deliberation_id,
                        f"{nudge.target}_nudge",
                        json.dumps(nudge.model_dump()),
                        nudge.trigger_condition
                    )
                except Exception as e:
                    log.error("Failed to write nudge: %s", repr(e))
                    raise

            # 3. Insert patient-specific knowledge updates
            for update in result.knowledge_updates:
                try:
                    if update.scope == "patient_specific":
                        # Supersede old entries of same type if revision
                        if update.supersedes:
                            await conn.execute(
                                """UPDATE patient_knowledge
                                   SET is_current = false, updated_at = NOW()
                                   WHERE id = $1""",
                                update.supersedes
                            )
                        await conn.execute(
                            """INSERT INTO patient_knowledge
                               (patient_id, knowledge_type, entry_text,
                                confidence, valid_from, valid_until,
                                source_deliberation_id, contributing_models,
                                evidence_refs, is_current)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,true)""",
                            result.patient_id,
                            "clinical_inference",
                            update.entry_text,
                            coerce_confidence(update.confidence),
                            update.valid_from,
                            update.valid_until,
                            result.deliberation_id,
                            list(result.models.values()),
                            update.evidence
                        )

                    elif update.scope == "core":
                        await conn.execute(
                            """INSERT INTO core_knowledge_updates
                               (knowledge_entry, update_type, confidence_delta,
                                source, source_deliberation_id)
                               VALUES ($1,$2,$3,$4,$5)""",
                            update.entry_text,
                            update.update_type,
                            coerce_confidence(update.confidence),
                            "deliberation_synthesis",
                            result.deliberation_id
                        )
                except Exception as e:
                    log.error(
                        "Failed to write knowledge_update (scope=%s): %s",
                        update.scope, repr(e)
                    )
                    raise

    return result.deliberation_id

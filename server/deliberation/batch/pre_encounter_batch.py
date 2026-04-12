"""
PRE-ENCOUNTER BATCH DELIBERATION
==================================
Submits all three agent deliberations for all patients scheduled in the next
batch window as a single Anthropic Batch API job. Polls completion at a
configurable interval. Results are stored when the batch completes.

Cost savings:
  Batch API:       50% off standard pricing
  Prompt caching:  ~90% off system prompts (identical across patients)
  Model tiering:   Each task uses the minimum-quality model that suffices

Run schedule: Nightly. Provider-ready briefings available before morning rounds.
"""

import asyncio
import logging
import time
from typing import Any, Optional

from .model_router import get_model

log = logging.getLogger(__name__)

# Anthropic batch limit
BATCH_REQUEST_LIMIT = 9500  # leave headroom below 10k


def build_deliberation_batch_requests(
    patient_deliberations: list[dict],
    agent_prompts: dict[str, str],
) -> list[dict]:
    """
    Build the request list for a Batch API submission.

    Args:
        patient_deliberations: List of dicts, each with:
            - "mrn": str
            - "encounter_date": ISO date string
            - "gold_context": str (full compiled patient context)
        agent_prompts: dict mapping agent_id -> system prompt text
                       e.g., {"ARIA": "...", "MIRA": "...", "THEO": "..."}

    Returns:
        List of request dicts compatible with the Anthropic Batch API.
        Each custom_id has format: "{mrn}_{agent_id}_{encounter_date}"
    """
    requests: list[dict] = []

    for patient in patient_deliberations:
        mrn = patient["mrn"]
        encounter_date = patient["encounter_date"]
        gold_context = patient["gold_context"]

        for agent_id, system_prompt in agent_prompts.items():
            task_type = f"{agent_id.lower()}_deliberation"
            model = get_model(task_type)

            requests.append({
                "custom_id": f"{mrn}_{agent_id}_{encounter_date}",
                "params": {
                    "model": model,
                    "max_tokens": 4000,
                    "system": [
                        {
                            "type": "text",
                            "text": system_prompt,
                            # Cache control — prompts identical across patients
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    "messages": [{"role": "user", "content": gold_context}],
                },
            })

    return requests


def chunk_batch_requests(requests: list[dict], chunk_size: int = BATCH_REQUEST_LIMIT) -> list[list[dict]]:
    """Split request list into chunks below the Anthropic batch limit."""
    return [
        requests[i:i + chunk_size]
        for i in range(0, len(requests), chunk_size)
    ]


async def submit_batch(
    requests: list[dict],
    client=None,
) -> list[str]:
    """
    Submit batch requests to Anthropic. Returns list of batch IDs.

    The client must support `client.messages.batches.create(requests=...)`.
    Pass an instantiated anthropic.Anthropic() client; we accept it as a
    parameter to allow mocking in tests.

    Note: This requires the anthropic SDK. We import lazily to avoid hard
    dependency on the SDK for non-batch code paths.
    """
    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    batch_ids: list[str] = []
    for chunk in chunk_batch_requests(requests):
        batch = client.messages.batches.create(requests=chunk)
        batch_ids.append(batch.id)
        log.info(
            "[BATCH] Submitted batch %s with %d requests",
            batch.id, len(chunk),
        )

    return batch_ids


async def poll_batch(
    batch_id: str,
    poll_interval_seconds: int = 1800,
    max_wait_seconds: int = 86400,
    client=None,
) -> dict[str, Any]:
    """
    Poll a single batch until it ends or max_wait_seconds is exceeded.

    Returns a dict mapping {mrn: {agent_id: {"status": ..., "content"|"error": ...}}}.
    """
    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    start = time.monotonic()

    while True:
        batch = client.messages.batches.retrieve(batch_id)
        status = batch.processing_status
        log.info(
            "[BATCH] %s status=%s succeeded=%s errored=%s",
            batch_id, status,
            getattr(batch.request_counts, "succeeded", "?"),
            getattr(batch.request_counts, "errored", "?"),
        )

        if status == "ended":
            break

        if (time.monotonic() - start) >= max_wait_seconds:
            log.warning("[BATCH] Max wait reached for %s — collecting partial results", batch_id)
            break

        await asyncio.sleep(poll_interval_seconds)

    return collect_batch_results(batch_id, client)


def collect_batch_results(batch_id: str, client) -> dict[str, dict[str, Any]]:
    """
    Stream batch results and group by MRN/agent.
    """
    grouped: dict[str, dict[str, Any]] = {}

    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        parts = custom_id.split("_")
        if len(parts) < 2:
            log.warning("[BATCH] Unparseable custom_id: %s", custom_id)
            continue

        mrn = parts[0]
        agent_id = parts[1]

        result_type = result.result.type
        if result_type == "succeeded":
            content = result.result.message.content[0].text
            grouped.setdefault(mrn, {})[agent_id] = {
                "status": "ok",
                "content": content,
            }
        else:
            error_str = str(getattr(result.result, "error", "unknown_error"))
            grouped.setdefault(mrn, {})[agent_id] = {
                "status": "error",
                "error": error_str,
            }

    return grouped

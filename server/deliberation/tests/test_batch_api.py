"""Tests for batch API and model routing.

Tests cover:
  BR-1: Model routing returns correct model per task type
  BR-2: Unknown task type defaults to Sonnet
  BR-3: Build batch requests produces well-formed entries
  BR-4: Chunk batch requests respects limit
  BR-5: Collect batch results parses succeeded and errored entries
"""

import pytest
from unittest.mock import MagicMock

from server.deliberation.batch.model_router import (
    get_model,
    MODEL_ROUTING,
    HAIKU,
    SONNET,
    OPUS,
)
from server.deliberation.batch.pre_encounter_batch import (
    build_deliberation_batch_requests,
    chunk_batch_requests,
    collect_batch_results,
    BATCH_REQUEST_LIMIT,
)


class TestModelRouter:
    """Verify model routing constants."""

    def test_classification_uses_haiku(self):
        assert get_model("format_classification") == HAIKU

    def test_extraction_uses_sonnet(self):
        assert get_model("standard_extraction") == SONNET
        assert get_model("llm_fallback_extraction") == SONNET

    def test_deliberation_uses_opus(self):
        """Hard constraint: deliberation agents MUST use Opus."""
        assert get_model("aria_deliberation") == OPUS
        assert get_model("mira_deliberation") == OPUS
        assert get_model("theo_deliberation") == OPUS

    def test_synthesis_uses_opus(self):
        """Hard constraint: synthesis MUST use Opus."""
        assert get_model("synthesis") == OPUS

    def test_reasoning_confidence_uses_opus(self):
        assert get_model("reasoning_confidence") == OPUS

    def test_unknown_task_defaults_to_sonnet(self):
        """Unknown task types fall back to Sonnet with a warning."""
        assert get_model("nonexistent_task_xyz") == SONNET

    def test_all_routing_entries_use_known_models(self):
        for task, model in MODEL_ROUTING.items():
            assert model in {HAIKU, SONNET, OPUS}, (
                f"Task {task} uses unknown model {model}"
            )


class TestBuildBatchRequests:
    """Verify batch request building."""

    def test_one_patient_three_agents(self):
        patients = [{
            "mrn": "4829341",
            "encounter_date": "2026-04-13",
            "gold_context": "Patient context here",
        }]
        prompts = {
            "ARIA": "ARIA system prompt",
            "MIRA": "MIRA system prompt",
            "THEO": "THEO system prompt",
        }
        requests = build_deliberation_batch_requests(patients, prompts)
        # 1 patient * 3 agents = 3 requests
        assert len(requests) == 3
        # Check IDs
        ids = [r["custom_id"] for r in requests]
        assert "4829341_ARIA_2026-04-13" in ids
        assert "4829341_MIRA_2026-04-13" in ids
        assert "4829341_THEO_2026-04-13" in ids

    def test_request_uses_opus_for_deliberation(self):
        patients = [{
            "mrn": "4829341",
            "encounter_date": "2026-04-13",
            "gold_context": "Context",
        }]
        prompts = {"ARIA": "ARIA prompt"}
        requests = build_deliberation_batch_requests(patients, prompts)
        assert requests[0]["params"]["model"] == OPUS

    def test_request_includes_cache_control(self):
        patients = [{
            "mrn": "4829341",
            "encounter_date": "2026-04-13",
            "gold_context": "Context",
        }]
        prompts = {"ARIA": "ARIA prompt"}
        requests = build_deliberation_batch_requests(patients, prompts)
        system = requests[0]["params"]["system"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_multiple_patients_yield_correct_count(self):
        patients = [
            {"mrn": f"P{i}", "encounter_date": "2026-04-13", "gold_context": f"ctx{i}"}
            for i in range(5)
        ]
        prompts = {"ARIA": "p1", "MIRA": "p2"}
        requests = build_deliberation_batch_requests(patients, prompts)
        # 5 patients * 2 agents = 10
        assert len(requests) == 10


class TestChunking:
    """Batch chunking respects the Anthropic limit."""

    def test_small_batch_one_chunk(self):
        requests = [{"id": i} for i in range(50)]
        chunks = chunk_batch_requests(requests)
        assert len(chunks) == 1
        assert len(chunks[0]) == 50

    def test_large_batch_split_into_chunks(self):
        requests = [{"id": i} for i in range(BATCH_REQUEST_LIMIT * 2 + 100)]
        chunks = chunk_batch_requests(requests)
        # Should be 3 chunks: 9500, 9500, 100
        assert len(chunks) == 3
        assert len(chunks[0]) == BATCH_REQUEST_LIMIT
        assert len(chunks[1]) == BATCH_REQUEST_LIMIT
        assert len(chunks[2]) == 100


class TestCollectResults:
    """Result collection from a batch."""

    def test_collect_succeeded_results(self):
        # Build mock batch result iterator
        def make_result(custom_id, status="succeeded", text="output text"):
            r = MagicMock()
            r.custom_id = custom_id
            r.result.type = status
            r.result.message.content = [MagicMock(text=text)]
            return r

        client = MagicMock()
        client.messages.batches.results.return_value = iter([
            make_result("4829341_ARIA_2026-04-13", "succeeded", "ARIA output"),
            make_result("4829341_MIRA_2026-04-13", "succeeded", "MIRA output"),
        ])

        grouped = collect_batch_results("batch_test_id", client)
        assert "4829341" in grouped
        assert grouped["4829341"]["ARIA"]["status"] == "ok"
        assert grouped["4829341"]["ARIA"]["content"] == "ARIA output"
        assert grouped["4829341"]["MIRA"]["content"] == "MIRA output"

    def test_collect_errored_results(self):
        r = MagicMock()
        r.custom_id = "4829341_ARIA_2026-04-13"
        r.result.type = "errored"
        r.result.error = "timeout"

        client = MagicMock()
        client.messages.batches.results.return_value = iter([r])

        grouped = collect_batch_results("batch_test_id", client)
        assert grouped["4829341"]["ARIA"]["status"] == "error"
        assert "timeout" in grouped["4829341"]["ARIA"]["error"]

    def test_unparseable_custom_id_skipped(self):
        r = MagicMock()
        r.custom_id = "malformed"
        r.result.type = "succeeded"

        client = MagicMock()
        client.messages.batches.results.return_value = iter([r])

        grouped = collect_batch_results("batch_test_id", client)
        # Malformed ID skipped — empty result
        assert "malformed" not in grouped

"""Tests for JSON utility functions used in the deliberation engine."""
import pytest
from server.deliberation.json_utils import strip_markdown_fences


class TestStripMarkdownFences:
    def test_json_with_json_fence(self):
        raw = '```json\n{"key": "value"}\n```'
        assert strip_markdown_fences(raw) == '{"key": "value"}'

    def test_json_with_plain_fence(self):
        raw = '```\n{"key": "value"}\n```'
        assert strip_markdown_fences(raw) == '{"key": "value"}'

    def test_no_fences(self):
        raw = '{"key": "value"}'
        assert strip_markdown_fences(raw) == '{"key": "value"}'

    def test_whitespace_around_fences(self):
        raw = '  \n  ```json\n{"key": "value"}\n```  \n  '
        assert strip_markdown_fences(raw) == '{"key": "value"}'

    def test_multiline_json(self):
        raw = '```json\n{\n  "critique_items": [],\n  "areas_of_agreement": ["all good"]\n}\n```'
        result = strip_markdown_fences(raw)
        assert result.startswith("{")
        assert result.endswith("}")
        assert '"critique_items"' in result

    def test_empty_string(self):
        assert strip_markdown_fences("") == ""

    def test_only_fences(self):
        raw = "```json\n```"
        assert strip_markdown_fences(raw) == ""

    def test_real_crosscritique_payload(self):
        """Simulate the exact error from run 4."""
        raw = '```json\n{\n  "critic_model": "claude-sonnet-4-20250514",\n  "target_model": "gpt-4o",\n  "round_number": 1,\n  "critique_items": [],\n  "areas_of_agreement": ["Both models identified key vitality concerns."]\n}\n```'
        result = strip_markdown_fences(raw)
        import json
        parsed = json.loads(result)
        assert parsed["critic_model"] == "claude-sonnet-4-20250514"
        assert len(parsed["areas_of_agreement"]) == 1

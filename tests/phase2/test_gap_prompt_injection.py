"""Tests for gap detection protocol injection into analyst prompts."""
import pytest
from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "server" / "deliberation" / "prompts"


def test_analyst_claude_has_gap_protocol():
    """analyst_claude.xml should contain the gap detection protocol."""
    content = (PROMPTS_DIR / "analyst_claude.xml").read_text()
    assert "<gap_detection_protocol>" in content
    assert "</gap_detection_protocol>" in content


def test_analyst_gpt4_has_gap_protocol():
    """analyst_gpt4.xml should contain the gap detection protocol."""
    content = (PROMPTS_DIR / "analyst_gpt4.xml").read_text()
    assert "<gap_detection_protocol>" in content
    assert "</gap_detection_protocol>" in content


def test_gap_protocol_no_substitution_vars():
    """The gap protocol sections should contain no {{KEY}} variables."""
    for fname in ("analyst_claude.xml", "analyst_gpt4.xml"):
        content = (PROMPTS_DIR / fname).read_text()
        # Extract the gap protocol section
        start = content.index("<gap_detection_protocol>")
        end = content.index("</gap_detection_protocol>") + len("</gap_detection_protocol>")
        section = content[start:end]
        assert "{{" not in section, f"Found substitution variable in {fname} gap protocol"


def test_analyst_claude_preserves_original_content():
    """analyst_claude.xml should still contain the original role and output_format."""
    content = (PROMPTS_DIR / "analyst_claude.xml").read_text()
    assert "<role>" in content
    assert "Diagnostic Reasoning Analyst" in content
    assert "<output_format>" in content
    assert "</output_format>" in content
    assert "<task>" in content


def test_analyst_gpt4_preserves_original_content():
    """analyst_gpt4.xml should still contain the original role and output_format."""
    content = (PROMPTS_DIR / "analyst_gpt4.xml").read_text()
    assert "<role>" in content
    assert "Treatment Optimization Analyst" in content
    assert "<output_format>" in content
    assert "</output_format>" in content
    assert "<task>" in content


def test_gap_protocol_specifies_structured_format():
    """Both prompts should instruct the structured [gap_type:severity] format."""
    for fname in ("analyst_claude.xml", "analyst_gpt4.xml"):
        content = (PROMPTS_DIR / fname).read_text()
        assert "[gap_type:severity]" in content
        assert "[stale_data:high]" in content


def test_gap_protocol_mentions_confidence_threshold():
    """Both prompts should mention the 0.70 confidence threshold."""
    for fname in ("analyst_claude.xml", "analyst_gpt4.xml"):
        content = (PROMPTS_DIR / fname).read_text()
        assert "0.70" in content


def test_gap_protocol_mentions_gap_alert():
    """Both prompts should instruct GAP_ALERT prefix for critical gaps."""
    for fname in ("analyst_claude.xml", "analyst_gpt4.xml"):
        content = (PROMPTS_DIR / fname).read_text()
        assert "GAP_ALERT" in content


def test_critic_prompts_unchanged():
    """Critic prompts should NOT contain gap detection protocol."""
    for fname in ("critic_claude.xml", "critic_gpt4.xml"):
        path = PROMPTS_DIR / fname
        if path.exists():
            content = path.read_text()
            assert "<gap_detection_protocol>" not in content, (
                f"{fname} should not have gap detection protocol"
            )

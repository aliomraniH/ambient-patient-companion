"""
json_utils.py — Shared JSON parsing utilities for the deliberation engine.

LLMs sometimes wrap JSON responses in markdown code fences (```json ... ```)
even when explicitly instructed not to.  This helper strips those fences
before passing the text to Pydantic's model_validate_json().
"""

import json


def strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences that LLMs sometimes add around JSON.

    Handles:
      - ```json\\n{...}\\n```
      - ```\\n{...}\\n```
      - Leading/trailing whitespace
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```)
        lines = lines[1:]
        # Remove last line if it's ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def safe_json_loads(text: str) -> dict:
    """Strip markdown fences and parse JSON from LLM output.

    Returns a dict on success.  Raises ValueError (not raw JSONDecodeError)
    with a preview of the input on failure.
    """
    if not text:
        return {}
    stripped = strip_markdown_fences(text)
    if not stripped:
        return {}
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        preview = stripped[:200]
        raise ValueError(
            f"Failed to parse LLM output as JSON: {exc}. Preview: {preview!r}"
        ) from exc

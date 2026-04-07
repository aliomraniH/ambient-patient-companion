"""
json_utils.py — Shared JSON parsing utilities for the deliberation engine.

LLMs sometimes wrap JSON responses in markdown code fences (```json ... ```)
even when explicitly instructed not to.  This helper strips those fences
before passing the text to Pydantic's model_validate_json().
"""


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

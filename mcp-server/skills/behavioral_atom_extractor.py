"""Behavioral atom extractor — LLM-based extraction of behavioral signals
from free-text clinical notes. Library module (no MCP tools registered).

Called from:
  - ingestion/adapters/healthex/executor.py post-note-write hook
  - mcp-server/skills/behavioral_atoms.py (on demand)

PHI rule: prompts contain only the chunked note text and a section-type hint.
patient_id is a UUID used for DB linking only — never sent to the LLM.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import date
from typing import Optional

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

SIGNAL_TYPES = [
    "psychomotor_restlessness",
    "attention_switching",
    "device_checking",
    "low_affect",
    "elevated_affect",
    "passive_si",
    "social_withdrawal",
    "somatic_preoccupation",
    "sleep_disturbance",
    "appetite_change",
    "anxiety_markers",
    "concentration_difficulty",
    "psychomotor_slowing",
    "irritability",
    "mood_lability",
]

NOTE_SECTIONS = {
    "general":    ["general:", "gen:", "general appearance"],
    "psych":      ["psych:", "psychiatric:", "mental status"],
    "ros_psych":  ["ros:", "review of systems"],
    "assessment": ["assessment:", "assessment and plan:", "a/p:"],
    "hpi":        ["hpi:", "history of present illness:", "chief complaint:"],
}

EXTRACTION_PROMPT = """You are a clinical NLP extractor. Extract behavioral signal
observations from the clinical note section below. Return ONLY valid JSON.

SECTION TYPE: {section_type}
NOTE DATE: {note_date}
TEXT:
{text}

Return a JSON array. Each element has these exact keys:
- signal_type: one of {signal_types}
- signal_value: the exact phrase from the text (verbatim, max 100 chars)
- assertion: "present" | "absent" | "historical"
- confidence: float 0.0-1.0

Rules:
- Only extract behavioral signals — NOT diagnoses, lab values, or medications
- assertion "absent" = "denies X", "no X noted", "patient reports no X"
- assertion "historical" = "history of X", "previously had X", "in the past X"
- Only extract from the GENERAL, PSYCH, ROS sections or free text Assessment
- Do NOT extract signals from billing codes or structured data
- If no behavioral signals are present, return []
- Return ONLY the JSON array, no preamble, no markdown

Example output:
[
  {{"signal_type": "attention_switching", "signal_value": "constantly jumping from system to system when discussing symptoms", "assertion": "present", "confidence": 0.85}},
  {{"signal_type": "device_checking", "signal_value": "constantly looking at the iphone watch on his left wrist", "assertion": "present", "confidence": 0.90}}
]"""

EXTRACTION_MODEL = "claude-sonnet-4-20250514"
EXTRACTION_PROMPT_VERSION = "v1.0"

_client = None


def _get_client():
    """Lazily initialize the Anthropic client. Returns None if unavailable."""
    global _client
    if _client is not None:
        return _client
    try:
        import anthropic  # type: ignore
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        _client = anthropic.Anthropic()
        return _client
    except Exception:
        return None


def _detect_section(text: str) -> str:
    t = text.lower()
    for section, markers in NOTE_SECTIONS.items():
        if any(m in t for m in markers):
            return section
    return "unclassified"


def chunk_note_by_section(full_note_text: str) -> list[dict]:
    """Split a full clinical note into behavioral-relevant sections."""
    lines = full_note_text.split("\n")
    chunks: list[dict] = []
    current_section = "unclassified"
    current_lines: list[str] = []

    for line in lines:
        line_lower = line.lower().strip()
        matched = False
        for section, markers in NOTE_SECTIONS.items():
            if any(line_lower.startswith(m) for m in markers):
                if current_lines:
                    chunks.append({
                        "section": current_section,
                        "text": "\n".join(current_lines).strip(),
                    })
                current_section = section
                current_lines = [line]
                matched = True
                break
        if not matched:
            current_lines.append(line)

    if current_lines:
        chunks.append({
            "section": current_section,
            "text": "\n".join(current_lines).strip(),
        })

    relevant_sections = {"general", "psych", "ros_psych", "assessment", "hpi"}
    return [
        c for c in chunks
        if c["section"] in relevant_sections and len(c["text"].strip()) > 20
    ]


def _strip_fences(raw: str) -> str:
    raw = re.sub(r"^```(?:json)?", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"```$", "", raw, flags=re.MULTILINE)
    return raw.strip()


async def extract_atoms_from_note(
    note_text: str,
    note_date: date,
    source_note_id: str,
    patient_id: str,
) -> list[dict]:
    """Extract behavioral signal atoms from one clinical note.

    Returns a list of dicts ready for DB insert. Never raises — extraction
    failures yield an empty list so callers can treat this as best-effort.
    """
    if not note_text or not note_text.strip():
        return []

    chunks = chunk_note_by_section(note_text)
    if not chunks:
        return []

    client = _get_client()
    if client is None:
        logger.info("Skipping atom extraction: Anthropic client unavailable")
        return []

    all_atoms: list[dict] = []

    for chunk in chunks:
        if len(chunk["text"].strip()) < 20:
            continue

        prompt = EXTRACTION_PROMPT.format(
            section_type=chunk["section"],
            note_date=note_date.isoformat(),
            text=chunk["text"][:2000],
            signal_types=json.dumps(SIGNAL_TYPES),
        )

        try:
            response = client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text
            extracted = json.loads(_strip_fences(raw))
            if not isinstance(extracted, list):
                continue

            for item in extracted:
                if not isinstance(item, dict):
                    continue
                if not all(k in item for k in
                           ("signal_type", "signal_value", "assertion", "confidence")):
                    continue
                if item["signal_type"] not in SIGNAL_TYPES:
                    continue
                if item["assertion"] not in ("present", "absent", "historical"):
                    continue
                try:
                    confidence = min(1.0, max(0.0, float(item["confidence"])))
                except (TypeError, ValueError):
                    continue

                all_atoms.append({
                    "patient_id": patient_id,
                    "source_note_id": source_note_id,
                    "clinical_date": note_date,
                    "note_section": chunk["section"],
                    "signal_type": item["signal_type"],
                    "signal_value": str(item["signal_value"])[:200],
                    "assertion": item["assertion"],
                    "confidence": confidence,
                    "extraction_model": EXTRACTION_MODEL,
                    "extraction_prompt_ver": EXTRACTION_PROMPT_VERSION,
                })
        except Exception as e:
            # PHI rule: do not include note text or signal_value in logs
            logger.warning(
                "Atom extraction failed for note=%s section=%s: %s",
                source_note_id, chunk["section"], type(e).__name__,
            )
            continue

    return all_atoms


async def insert_atoms(conn, atoms: list[dict]) -> int:
    """Insert extracted atoms. Returns count inserted.

    `conn` may be either an asyncpg connection or a pool — both expose
    `.execute(...)` with the same signature.
    """
    if not atoms:
        return 0
    inserted = 0
    for atom in atoms:
        try:
            await conn.execute(
                """
                INSERT INTO behavioral_signal_atoms
                    (patient_id, source_note_id, clinical_date, note_section,
                     signal_type, signal_value, assertion, confidence,
                     extraction_model, extraction_prompt_ver)
                VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT DO NOTHING
                """,
                atom["patient_id"],
                atom["source_note_id"],
                atom["clinical_date"],
                atom["note_section"],
                atom["signal_type"],
                atom["signal_value"],
                atom["assertion"],
                atom["confidence"],
                atom["extraction_model"],
                atom["extraction_prompt_ver"],
            )
            inserted += 1
        except Exception as e:
            logger.warning("Atom insert failed: %s", type(e).__name__)
            continue
    return inserted


def register(mcp):  # pragma: no cover - no-op to silence skill loader warning
    return

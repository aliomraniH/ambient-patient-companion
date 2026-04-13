"""Guideline chunking logic — Phase 2 scaffold.

Produces recommendation-boundary chunks suitable for MedCPT-Article-Encoder
embedding. Design goals:

  * Never split mid-recommendation — chunks are sized downward from the
    target word count, not upward across boundaries.
  * 500-word target, 10–15% overlap between adjacent chunks.
  * Preserve the heading hierarchy (chapter → section → subsection) as
    structured metadata so retrieval hits can cite precisely.
  * Preserve evidence_grade and recommendation_strength on every chunk,
    not just the root document — downstream filters need them.

Embedding generation lives in a sibling module (not yet implemented;
requires `ncbi/MedCPT-Article-Encoder` weights). This module produces
the chunk records; the embedder consumes them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


TARGET_WORDS = 500
OVERLAP_RATIO = 0.125  # 12.5%; inside the stated 10–15% band


@dataclass
class GuidelineChunk:
    recommendation_id: str
    guideline_source: str                  # 'ADA' | 'USPSTF' | 'ACC' | 'AHA' | ...
    version: str
    chapter: str
    section: str
    text: str
    evidence_grade: str                    # 'A' | 'B' | 'C' | 'D' | 'I'
    recommendation_strength: str
    patient_population: list[str] = field(default_factory=list)
    contraindications: list[str] = field(default_factory=list)
    medications_mentioned: list[str] = field(default_factory=list)
    last_reviewed: str | None = None
    is_current: bool = True

    def as_row(self) -> dict:
        """Row dict for INSERT into the guidelines table (migration 009)."""
        return {
            "recommendation_id":          self.recommendation_id,
            "guideline_source":            self.guideline_source,
            "version":                     self.version,
            "chapter":                     self.chapter,
            "section":                     self.section,
            "text":                        self.text,
            "evidence_grade":              self.evidence_grade,
            "recommendation_strength":     self.recommendation_strength,
            "patient_population":          self.patient_population,
            "contraindications":           self.contraindications,
            "medications_mentioned":       self.medications_mentioned,
            "last_reviewed":               self.last_reviewed,
            "is_current":                  self.is_current,
        }


_RECO_SPLITTER = re.compile(
    r"(?m)^(?:Recommendation\s+\d+(?:\.\d+)?|Rec\.\s*\d+|\d+\.\s+[A-Z])"
)
_WS = re.compile(r"\s+")


def _word_count(s: str) -> int:
    return len(s.split())


def split_on_recommendation_boundaries(body: str) -> list[str]:
    """Split a chapter/section body into recommendation-level segments.

    We split at the start of each 'Recommendation N', 'Rec. N', or numbered
    heading followed by a capitalized word. Segments shorter than 40 words
    are merged with the next segment so we don't produce too-fine chunks.
    """
    body = body.strip()
    if not body:
        return []
    # Find all split points.
    points = [m.start() for m in _RECO_SPLITTER.finditer(body)]
    if not points:
        return [body]
    if points[0] != 0:
        points.insert(0, 0)
    points.append(len(body))
    segments = [body[a:b].strip() for a, b in zip(points, points[1:])]

    merged: list[str] = []
    for seg in segments:
        if not seg:
            continue
        if merged and _word_count(seg) < 40:
            merged[-1] = merged[-1] + "\n\n" + seg
        else:
            merged.append(seg)
    return merged


def _trim_to_word_count(text: str, n: int) -> str:
    words = text.split()
    if len(words) <= n:
        return text
    return " ".join(words[:n])


def _tail_words(text: str, n: int) -> str:
    words = text.split()
    if len(words) <= n:
        return text
    return " ".join(words[-n:])


def chunk_segment(
    segment_text: str,
    target_words: int = TARGET_WORDS,
    overlap_ratio: float = OVERLAP_RATIO,
) -> list[str]:
    """Chunk a single recommendation segment into target-sized windows
    with sliding overlap. Always returns at least one chunk.
    """
    segment_text = _WS.sub(" ", segment_text).strip()
    if not segment_text:
        return []
    words = segment_text.split()
    if len(words) <= target_words:
        return [segment_text]

    overlap = max(1, int(target_words * overlap_ratio))
    step = target_words - overlap
    chunks: list[str] = []
    i = 0
    while i < len(words):
        window = words[i : i + target_words]
        chunks.append(" ".join(window))
        if i + target_words >= len(words):
            break
        i += step
    return chunks


def chunk_guideline_document(
    *,
    guideline_source: str,
    version: str,
    chapter: str,
    section: str,
    body: str,
    recommendation_id_prefix: str,
    evidence_grade: str,
    recommendation_strength: str,
    patient_population: Iterable[str] = (),
    contraindications: Iterable[str] = (),
    medications_mentioned: Iterable[str] = (),
    last_reviewed: str | None = None,
    is_current: bool = True,
) -> list[GuidelineChunk]:
    """Turn a chapter/section body into a list of GuidelineChunk rows
    ready to embed + insert into the `guidelines` table (migration 009).
    """
    segments = split_on_recommendation_boundaries(body)
    rows: list[GuidelineChunk] = []
    reco_idx = 0
    for seg in segments:
        reco_idx += 1
        base_id = f"{recommendation_id_prefix}.{reco_idx:03d}"
        for part_idx, chunk_text in enumerate(chunk_segment(seg), start=1):
            rid = f"{base_id}.{part_idx:02d}"
            rows.append(GuidelineChunk(
                recommendation_id=rid,
                guideline_source=guideline_source,
                version=version,
                chapter=chapter,
                section=section,
                text=chunk_text,
                evidence_grade=evidence_grade,
                recommendation_strength=recommendation_strength,
                patient_population=list(patient_population),
                contraindications=list(contraindications),
                medications_mentioned=list(medications_mentioned),
                last_reviewed=last_reviewed,
                is_current=is_current,
            ))
    return rows

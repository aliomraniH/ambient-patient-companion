"""
atom_embedder.py — Pluggable 768-dim embedder for behavioral signal atoms.

Backend priority:
  1. MedCPT local model — if MEDCPT_MODEL_PATH env var is set and valid.
  2. OpenAI text-embedding-3-small — if OPENAI_API_KEY is set; output
     is 1536-dim, projected to 768 via a deterministic projection matrix.
  3. Deterministic hash-based stub — always available; suitable for CI
     and unit tests only (no clinical value).

All embed calls are synchronous at the Python level (asyncpg calls
happen around them). Embedding failures ALWAYS return None — never raise.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import struct
from typing import Optional

log = logging.getLogger(__name__)

_EMBED_DIM = 768

# ─── Backend detection ────────────────────────────────────────────────────────

def _detect_backend() -> str:
    medcpt_path = os.environ.get("MEDCPT_MODEL_PATH", "").strip()
    if medcpt_path:
        try:
            if os.path.isdir(medcpt_path):
                return "medcpt"
        except Exception:
            pass
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return "openai"
    return "stub"


# ─── Hash-based stub embedder ─────────────────────────────────────────────────

def _stub_embed(text: str) -> list[float]:
    """Deterministic 768-dim embedding from SHA-256 hash. Not clinically valid."""
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).digest()
    # Tile the 32-byte digest to fill 768 floats via struct unpacking.
    # Each float is derived from one byte, normalised to [-1, 1].
    extended = (digest * (((_EMBED_DIM // 32) + 1))) [:_EMBED_DIM]
    raw = [struct.unpack("b", bytes([b]))[0] / 128.0 for b in extended]
    # L2-normalise so cosine similarity is well-defined.
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


# ─── OpenAI embedder ──────────────────────────────────────────────────────────

# Deterministic projection matrix seed — fixed so the mapping is reproducible
# across restarts without persisting any weights.
_PROJ_SEED = 0xDEADBEEF

def _make_projection_matrix() -> list[list[float]]:
    """Create a 1536×768 projection matrix seeded deterministically."""
    import random
    rng = random.Random(_PROJ_SEED)
    mat = []
    for _ in range(1536):
        row = [rng.gauss(0, 1) for _ in range(_EMBED_DIM)]
        norm = math.sqrt(sum(x * x for x in row)) or 1.0
        mat.append([x / norm for x in row])
    return mat

_proj_matrix: list[list[float]] | None = None


def _project_1536_to_768(vec_1536: list[float]) -> list[float]:
    """Project a 1536-dim OpenAI embedding down to 768 dims."""
    global _proj_matrix
    if _proj_matrix is None:
        _proj_matrix = _make_projection_matrix()

    result = [0.0] * _EMBED_DIM
    for i, v in enumerate(vec_1536):
        row = _proj_matrix[i]
        for j in range(_EMBED_DIM):
            result[j] += v * row[j]

    norm = math.sqrt(sum(x * x for x in result)) or 1.0
    return [x / norm for x in result]


def _openai_embed(text: str) -> list[float]:
    """Call OpenAI text-embedding-3-small and project to 768 dims."""
    import openai
    client = openai.OpenAI()
    resp = client.embeddings.create(
        model="text-embedding-3-small",
        input=text[:8192],      # OpenAI token limit
    )
    vec_1536 = resp.data[0].embedding
    return _project_1536_to_768(vec_1536)


# ─── MedCPT embedder ──────────────────────────────────────────────────────────

def _medcpt_embed(text: str) -> list[float]:
    """Embed via local MedCPT-Article-Encoder checkpoint."""
    import torch
    from transformers import AutoTokenizer, AutoModel

    model_path = os.environ["MEDCPT_MODEL_PATH"]
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model = AutoModel.from_pretrained(model_path)
    model.eval()

    inputs = tokenizer(
        text,
        return_tensors="pt",
        max_length=512,
        truncation=True,
        padding=True,
    )
    with torch.no_grad():
        outputs = model(**inputs)

    # Mean-pool over token dimension.
    hidden = outputs.last_hidden_state       # (1, seq_len, 768)
    mask = inputs["attention_mask"].unsqueeze(-1).float()
    pooled = (hidden * mask).sum(1) / mask.sum(1)
    vec = pooled[0].numpy().tolist()

    # L2-normalise.
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


# ─── Public API ───────────────────────────────────────────────────────────────

def embed_signal_value(text: str) -> Optional[list[float]]:
    """Embed a signal_value string. Returns None on any failure.

    Backend selection happens at call time (env vars may change in tests).
    """
    if not text or not text.strip():
        return None
    backend = _detect_backend()
    try:
        if backend == "medcpt":
            vec = _medcpt_embed(text)
        elif backend == "openai":
            vec = _openai_embed(text)
        else:
            vec = _stub_embed(text)
        if len(vec) != _EMBED_DIM:
            log.warning(
                "atom_embedder: expected %d dims, got %d (backend=%s) — returning None",
                _EMBED_DIM, len(vec), backend,
            )
            return None
        return vec
    except Exception as exc:
        log.warning("atom_embedder: embed failed (backend=%s): %s", backend, exc)
        return None


def active_backend() -> str:
    """Return the name of the currently active embedding backend."""
    return _detect_backend()

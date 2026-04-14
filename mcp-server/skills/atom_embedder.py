"""Atom embedder — pluggable backend for behavioral_signal_atoms.embedding.

Priority order:

    1. MedCPT local checkpoint (if MEDCPT_MODEL_PATH env points to a valid
       transformers checkpoint + weights are loadable). Clinical-domain
       embeddings with 768-dim output native to the HNSW index.
    2. OpenAI text-embedding-3-small (if OPENAI_API_KEY is set). Output
       is 1536-dim; we project to 768 via a deterministic interleave-slice.
    3. Deterministic hash-based 768-dim stub. Always available; safe for
       CI. Produces a stable pseudo-vector per input text so similarity
       round-trips work in tests even though vectors have no semantic
       meaning.

Failures at any step fall through to the next; the final stub never fails.
Callers should treat NULL embeddings as acceptable (HNSW tolerates NULLs).

PHI note: the raw `signal_value` text is sent to whichever backend is
configured. MedCPT = local. OpenAI = outbound — requires BAA coverage.
The stub is local-only and deterministic.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import struct
import sys
from typing import Optional

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

EMBED_DIM = 768

# Which backend got selected on the first call — cached for logging.
_BACKEND: Optional[str] = None

# Lazy clients.
_medcpt_model = None
_medcpt_tokenizer = None
_openai_client = None


# ── Helpers ─────────────────────────────────────────────────────────────

def _l2_normalize(vec: list[float]) -> list[float]:
    s = math.sqrt(sum(v * v for v in vec))
    if s == 0:
        return vec
    return [v / s for v in vec]


def _stub_embed(text: str) -> list[float]:
    """Deterministic hash-based pseudo-embedding.

    Uses SHA-256 in counter mode to fill a 768-dim float vector, then
    L2-normalizes so cosine similarity behaves sanely (similar inputs
    produce similar vectors only up to hash-collision; this is a stub).
    """
    text_b = (text or "").encode("utf-8", errors="replace")
    out: list[float] = []
    counter = 0
    # 768 floats × 4 bytes = 3072 bytes needed; each SHA-256 round yields
    # 32 bytes, so 96 rounds.
    while len(out) < EMBED_DIM:
        h = hashlib.sha256(text_b + counter.to_bytes(4, "big")).digest()
        for i in range(0, len(h), 4):
            if len(out) >= EMBED_DIM:
                break
            (val,) = struct.unpack(">i", h[i:i + 4])
            # Scale signed int32 → roughly [-1, 1].
            out.append(val / (2 ** 31))
        counter += 1
    return _l2_normalize(out)


def _project_to_dim(vec: list[float], target: int) -> list[float]:
    """Project a higher-dim vector to `target` dims via strided average.

    For OpenAI text-embedding-3-small (1536-d) → 768 means averaging
    adjacent pairs. Deterministic, no randomness, preserves cosine
    structure reasonably. If vec is shorter than target, pad with zeros.
    """
    n = len(vec)
    if n == target:
        return _l2_normalize(list(vec))
    if n < target:
        out = list(vec) + [0.0] * (target - n)
        return _l2_normalize(out)
    # n > target: bucket-average.
    buckets: list[list[float]] = [[] for _ in range(target)]
    stride = n / target
    for idx, v in enumerate(vec):
        b = min(target - 1, int(idx / stride))
        buckets[b].append(v)
    out = [sum(b) / max(len(b), 1) for b in buckets]
    return _l2_normalize(out)


# ── Backends ────────────────────────────────────────────────────────────

def _try_medcpt() -> bool:
    """Attempt to lazy-load a MedCPT checkpoint. Returns True on success."""
    global _medcpt_model, _medcpt_tokenizer
    path = os.environ.get("MEDCPT_MODEL_PATH")
    if not path or not os.path.isdir(path):
        return False
    try:
        from transformers import AutoModel, AutoTokenizer  # type: ignore
        _medcpt_tokenizer = AutoTokenizer.from_pretrained(path)
        _medcpt_model = AutoModel.from_pretrained(path)
        _medcpt_model.eval()
        return True
    except Exception as e:
        logger.info("MedCPT backend unavailable: %s", type(e).__name__)
        return False


def _medcpt_embed_batch(texts: list[str]) -> list[list[float]]:
    import torch  # type: ignore
    with torch.no_grad():
        enc = _medcpt_tokenizer(
            texts, padding=True, truncation=True,
            max_length=256, return_tensors="pt",
        )
        out = _medcpt_model(**enc).last_hidden_state[:, 0, :]
        out = torch.nn.functional.normalize(out, p=2, dim=1)
        return [row.tolist() for row in out]


def _try_openai() -> bool:
    global _openai_client
    if not os.environ.get("OPENAI_API_KEY"):
        return False
    try:
        import openai  # type: ignore
        _openai_client = openai.OpenAI()
        return True
    except Exception as e:
        logger.info("OpenAI embedder unavailable: %s", type(e).__name__)
        return False


def _openai_embed_batch(texts: list[str]) -> list[list[float]]:
    resp = _openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=texts,
    )
    out: list[list[float]] = []
    for item in resp.data:
        out.append(_project_to_dim(list(item.embedding), EMBED_DIM))
    return out


# ── Public API ──────────────────────────────────────────────────────────

def _resolve_backend() -> str:
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    if _try_medcpt():
        _BACKEND = "medcpt"
    elif _try_openai():
        _BACKEND = "openai"
    else:
        _BACKEND = "stub"
    logger.info("atom_embedder backend: %s", _BACKEND)
    return _BACKEND


def embed_signal_value(text: str) -> Optional[list[float]]:
    """Embed a single atom's signal_value. Returns None on failure."""
    out = embed_batch([text or ""])
    return out[0] if out else None


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns parallel list of 768-dim vectors.

    Never raises — backend failures fall through to the stub. An empty
    input yields an empty list.
    """
    if not texts:
        return []
    backend = _resolve_backend()
    cleaned = [(t or "") for t in texts]
    try:
        if backend == "medcpt":
            return _medcpt_embed_batch(cleaned)
        if backend == "openai":
            return _openai_embed_batch(cleaned)
    except Exception as e:
        logger.warning("Embedding backend %s failed: %s — falling back to stub",
                       backend, type(e).__name__)
    return [_stub_embed(t) for t in cleaned]


def format_for_pgvector(vec: Optional[list[float]]) -> Optional[str]:
    """Format a vector as pgvector's textual literal.

    asyncpg does not register a pgvector codec by default, so we pass the
    vector as a string and cast in-query (`$N::vector`). Returns None if
    the input is None or empty.
    """
    if not vec:
        return None
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


def register(mcp):  # pragma: no cover — library, not a tool
    return

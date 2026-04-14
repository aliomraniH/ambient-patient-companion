"""
atom_embedder.py — Pluggable 768-dim embedder for behavioral signal atoms.

Backend priority (evaluated lazily at first embed call):
  1. MedCPT — ncats/MedCPT-Article-Encoder (768-dim, biomedical BERT).
       - If MEDCPT_MODEL_PATH env var points to an existing local directory,
         that path is used directly (no download attempted).
       - Otherwise, if MEDCPT_AUTO_DOWNLOAD != "false"/"0"/"no" (default: enabled),
         the weights are downloaded once via huggingface_hub to
         MEDCPT_CACHE_DIR (default ~/.cache/medcpt) and cached there.
       - The loaded tokenizer + model are kept in process memory so
         subsequent calls are fast (no repeated disk I/O).
  2. OpenAI text-embedding-3-small — if OPENAI_API_KEY is set; output is
     1536-dim, projected to 768 via a deterministic projection matrix.
  3. Deterministic hash-based stub — always available; suitable for CI
     and unit tests only (no clinical value).

All embed calls are synchronous at the Python level (asyncpg calls
happen around them). Embedding failures ALWAYS return None — never raise.

Environment variables:
  MEDCPT_MODEL_PATH     Path to a local MedCPT checkpoint directory.
  MEDCPT_CACHE_DIR      Where to store auto-downloaded weights
                        (default: ~/.cache/medcpt).
  MEDCPT_AUTO_DOWNLOAD  Set to "false", "0", or "no" to disable auto-download
                        (useful in air-gapped / strict CI environments).
  OPENAI_API_KEY        Enables the OpenAI fallback backend.
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
_MEDCPT_HF_REPO = "ncats/MedCPT-Article-Encoder"
_DEFAULT_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "medcpt")

# ─── MedCPT path resolution (cached) ─────────────────────────────────────────

_UNRESOLVED = object()          # sentinel: path not yet looked up
_medcpt_resolved_path: object = _UNRESOLVED  # str | None after first resolution


def _auto_download_enabled() -> bool:
    val = os.environ.get("MEDCPT_AUTO_DOWNLOAD", "true").strip().lower()
    return val not in ("false", "0", "no")


def _resolve_medcpt_path() -> Optional[str]:
    """Return a local directory path to MedCPT weights, or None.

    Resolution order:
      1. MEDCPT_MODEL_PATH env var (explicit, no download).
      2. MEDCPT_CACHE_DIR or default cache already populated (fast re-use).
      3. Auto-download via huggingface_hub (skipped if disabled).
    Result is cached module-level so this only runs once per process.
    """
    global _medcpt_resolved_path
    if _medcpt_resolved_path is not _UNRESOLVED:
        return _medcpt_resolved_path  # type: ignore[return-value]

    # 1. Explicit env var — must point at an existing directory.
    explicit = os.environ.get("MEDCPT_MODEL_PATH", "").strip()
    if explicit:
        if os.path.isdir(explicit):
            log.info("atom_embedder: MedCPT loaded from MEDCPT_MODEL_PATH=%s", explicit)
            _medcpt_resolved_path = explicit
            return explicit
        log.warning(
            "atom_embedder: MEDCPT_MODEL_PATH=%s is not a directory — ignoring", explicit
        )

    # 2. Previously downloaded cache.
    cache_dir = os.environ.get("MEDCPT_CACHE_DIR", "").strip() or _DEFAULT_CACHE_DIR
    if os.path.isdir(cache_dir) and _looks_like_checkpoint(cache_dir):
        log.info("atom_embedder: MedCPT cache hit at %s", cache_dir)
        _medcpt_resolved_path = cache_dir
        return cache_dir

    # 3. Auto-download.
    if not _auto_download_enabled():
        log.debug("atom_embedder: MedCPT auto-download disabled")
        _medcpt_resolved_path = None
        return None

    try:
        from huggingface_hub import snapshot_download

        log.info(
            "atom_embedder: downloading MedCPT weights (%s) to %s — "
            "set MEDCPT_AUTO_DOWNLOAD=false to skip",
            _MEDCPT_HF_REPO, cache_dir,
        )
        path = snapshot_download(
            repo_id=_MEDCPT_HF_REPO,
            local_dir=cache_dir,
            ignore_patterns=["*.msgpack", "*.h5", "flax_model*", "tf_model*"],
        )
        log.info("atom_embedder: MedCPT weights ready at %s", path)
        _medcpt_resolved_path = path
        return path
    except Exception as exc:
        log.info(
            "atom_embedder: MedCPT auto-download unavailable (%s) — "
            "falling back to next backend",
            exc,
        )
        _medcpt_resolved_path = None
        return None


def _looks_like_checkpoint(path: str) -> bool:
    """Heuristic: directory contains config.json (HF model marker)."""
    return os.path.isfile(os.path.join(path, "config.json"))


# ─── MedCPT in-process model cache ───────────────────────────────────────────

_medcpt_tokenizer = None
_medcpt_model = None


def _load_medcpt(model_path: str):
    """Load tokenizer + model from *model_path*, caching in process memory."""
    global _medcpt_tokenizer, _medcpt_model
    if _medcpt_model is not None:
        return _medcpt_tokenizer, _medcpt_model

    from transformers import AutoModel, AutoTokenizer

    log.info("atom_embedder: loading MedCPT tokenizer + model from %s", model_path)
    _medcpt_tokenizer = AutoTokenizer.from_pretrained(model_path)
    _medcpt_model = AutoModel.from_pretrained(model_path)
    _medcpt_model.eval()
    log.info("atom_embedder: MedCPT model loaded and cached in process")
    return _medcpt_tokenizer, _medcpt_model


# ─── Backend detection ────────────────────────────────────────────────────────

def _detect_backend() -> tuple[str, Optional[str]]:
    """Return (backend_name, medcpt_path_or_None)."""
    medcpt_path = _resolve_medcpt_path()
    if medcpt_path:
        return "medcpt", medcpt_path
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return "openai", None
    return "stub", None


# ─── Hash-based stub embedder ─────────────────────────────────────────────────

def _stub_embed(text: str) -> list[float]:
    """Deterministic 768-dim embedding from SHA-256 hash. Not clinically valid."""
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).digest()
    extended = (digest * ((_EMBED_DIM // 32) + 1))[:_EMBED_DIM]
    raw = [struct.unpack("b", bytes([b]))[0] / 128.0 for b in extended]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


# ─── OpenAI embedder ──────────────────────────────────────────────────────────

_PROJ_SEED = 0xDEADBEEF
_proj_matrix: list[list[float]] | None = None


def _make_projection_matrix() -> list[list[float]]:
    """Create a deterministic 1536×768 projection matrix."""
    import random

    rng = random.Random(_PROJ_SEED)
    mat = []
    for _ in range(1536):
        row = [rng.gauss(0, 1) for _ in range(_EMBED_DIM)]
        norm = math.sqrt(sum(x * x for x in row)) or 1.0
        mat.append([x / norm for x in row])
    return mat


def _project_1536_to_768(vec_1536: list[float]) -> list[float]:
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
    import openai

    client = openai.OpenAI()
    resp = client.embeddings.create(model="text-embedding-3-small", input=text[:8192])
    return _project_1536_to_768(resp.data[0].embedding)


# ─── MedCPT embedder ──────────────────────────────────────────────────────────

def _medcpt_embed(text: str, model_path: str) -> list[float]:
    """Embed *text* using the cached MedCPT model at *model_path*."""
    import torch

    tokenizer, model = _load_medcpt(model_path)
    inputs = tokenizer(
        text,
        return_tensors="pt",
        max_length=512,
        truncation=True,
        padding=True,
    )
    with torch.no_grad():
        outputs = model(**inputs)

    hidden = outputs.last_hidden_state           # (1, seq_len, 768)
    mask = inputs["attention_mask"].unsqueeze(-1).float()
    pooled = (hidden * mask).sum(1) / mask.sum(1)
    vec = pooled[0].numpy().tolist()

    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


# ─── Public API ───────────────────────────────────────────────────────────────

def embed_signal_value(text: str) -> Optional[list[float]]:
    """Embed a signal_value string. Returns None on any failure.

    Backend is selected once per process (path resolution is cached).
    The MedCPT model is also kept in memory after first load.
    """
    if not text or not text.strip():
        return None
    backend, medcpt_path = _detect_backend()
    try:
        if backend == "medcpt" and medcpt_path:
            vec = _medcpt_embed(text, medcpt_path)
        elif backend == "openai":
            vec = _openai_embed(text)
        else:
            vec = _stub_embed(text)
        if len(vec) != _EMBED_DIM:
            log.warning(
                "atom_embedder: expected %d dims, got %d (backend=%s) — returning None",
                _EMBED_DIM,
                len(vec),
                backend,
            )
            return None
        return vec
    except Exception as exc:
        log.warning("atom_embedder: embed failed (backend=%s): %s", backend, exc)
        return None


def active_backend() -> str:
    """Return the name of the currently active embedding backend."""
    backend, _ = _detect_backend()
    return backend


def reset_medcpt_cache() -> None:
    """Evict the in-process MedCPT model cache (useful in tests)."""
    global _medcpt_tokenizer, _medcpt_model, _medcpt_resolved_path
    _medcpt_tokenizer = None
    _medcpt_model = None
    _medcpt_resolved_path = _UNRESOLVED

"""
atom_embedder.py — Pluggable 768-dim embedder for behavioral signal atoms.

Backend priority (resolved once per process, then cached):
  1. HF Inference API — if HF_TOKEN (or HUGGINGFACE_TOKEN) is set and
     MEDCPT_BACKEND is not "local".  Embedding runs in the cloud; no model
     weights are stored locally and no GPU/RAM is consumed.  Uses the caller's
     HuggingFace Pro quota and CDN rate limits.  Falls back to the next
     backend if the model is not available on the serverless tier.
  2. Local MedCPT — ncats/MedCPT-Article-Encoder (768-dim PubMedBERT).
       a. MEDCPT_MODEL_PATH env var → use that directory directly.
       b. HF standard cache (~/.cache/huggingface/hub) already populated →
          return immediately (no network call).
       c. Auto-download via snapshot_download (authenticated when HF_TOKEN is
          present; skipped if MEDCPT_AUTO_DOWNLOAD=false).
     The tokenizer + model are kept alive in process memory after first load
     (low_cpu_mem_usage=True reduces peak RAM during loading).
  3. OpenAI text-embedding-3-small — if OPENAI_API_KEY is set; 1536-dim output
     is projected to 768 via a deterministic, fixed random matrix.
  4. Deterministic hash stub — always available; suitable for CI and unit tests
     only (no clinical value).

Environment variables:
  HF_TOKEN / HUGGINGFACE_TOKEN   HuggingFace access token (Pro or free).
                                  huggingface_hub also reads HF_TOKEN natively.
  MEDCPT_BACKEND                 "api"   — always use Inference API.
                                 "local" — always use local model.
                                 "auto"  — (default) API when token present,
                                           local otherwise.
  MEDCPT_MODEL_PATH              Path to an existing local checkpoint dir.
  MEDCPT_AUTO_DOWNLOAD           "false"/"0"/"no" disables auto-download
                                 (air-gapped / strict CI environments).
  OPENAI_API_KEY                 Enables the OpenAI fallback backend.

All embed calls are synchronous at the Python level. Failures ALWAYS return
None — never raise.
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
_MEDCPT_HF_REPO = "ncbi/MedCPT-Article-Encoder"

# ─── HuggingFace token ────────────────────────────────────────────────────────

def _get_hf_token() -> Optional[str]:
    """Return the HF access token from env, or None."""
    return (
        os.environ.get("HF_TOKEN", "").strip()
        or os.environ.get("HUGGINGFACE_TOKEN", "").strip()
        or None
    )


# ─── HF Inference API backend ─────────────────────────────────────────────────

def _hf_api_embed(text: str) -> list[float]:
    """Embed *text* via the HuggingFace Inference API (no local weights needed).

    Uses the caller's HF Pro quota.  normalize=True and truncate=True are
    applied server-side so the returned vector is already L2-normalised and
    fits within the model's 512-token limit.
    """
    from huggingface_hub import InferenceClient

    token = _get_hf_token()
    client = InferenceClient(token=token)
    output = client.feature_extraction(
        text,
        model=_MEDCPT_HF_REPO,
        normalize=True,
        truncate=True,
    )

    # output is list[float] (pooled) or list[list[float]] (token-level).
    return _to_768(output)


def _to_768(output) -> list[float]:
    """Coerce an Inference API feature-extraction response to a 768-dim vector.

    The InferenceClient may return numpy ndarrays; this function always works
    with plain Python lists internally.

    Handles:
      • 3-D ndarray/list [batch, seq_len, dim] — squeeze batch dim first.
      • 2-D [seq_len, dim]  — token-level; mean-pool [CLS]/[SEP] stripped.
      • 1-D [dim]           — already pooled; use directly.
    """
    # Normalise numpy arrays → nested Python lists so bool checks work cleanly.
    if hasattr(output, "tolist"):
        output = output.tolist()

    if not output:
        raise ValueError("Empty feature-extraction response from HF API")

    # Squeeze an accidental batch dimension: shape [1, seq_len, dim] → [seq_len, dim]
    if (
        isinstance(output, list)
        and isinstance(output[0], list)
        and isinstance(output[0][0], list)
        and len(output) == 1
    ):
        output = output[0]

    # 1-D pooled output
    if isinstance(output[0], (int, float)):
        vec = list(output)
    else:
        # 2-D token-level — mean-pool (skip [CLS] at index 0 and [SEP] at -1)
        tokens = output[1:-1] if len(output) > 2 else output
        n, dim = len(tokens), len(tokens[0])
        vec = [sum(tokens[t][d] for t in range(n)) / n for d in range(dim)]

    # Ensure L2-normalised (server may or may not have done this)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


# ─── MedCPT local model — path resolution (cached) ───────────────────────────

_UNRESOLVED = object()
_medcpt_resolved_path: object = _UNRESOLVED


def _auto_download_enabled() -> bool:
    val = os.environ.get("MEDCPT_AUTO_DOWNLOAD", "true").strip().lower()
    return val not in ("false", "0", "no")


def _resolve_medcpt_path() -> Optional[str]:
    """Return a path to local MedCPT weights, downloading if needed.

    Uses HF's standard content-addressed cache so the model is never
    re-downloaded when it is already on disk.  The token (if available)
    is passed to snapshot_download for authenticated CDN access.
    """
    global _medcpt_resolved_path
    if _medcpt_resolved_path is not _UNRESOLVED:
        return _medcpt_resolved_path  # type: ignore[return-value]

    # 1. Explicit local path.
    explicit = os.environ.get("MEDCPT_MODEL_PATH", "").strip()
    if explicit:
        if os.path.isdir(explicit):
            log.info("atom_embedder: MedCPT from MEDCPT_MODEL_PATH=%s", explicit)
            _medcpt_resolved_path = explicit
            return explicit
        log.warning("atom_embedder: MEDCPT_MODEL_PATH=%s not found — ignoring", explicit)

    if not _auto_download_enabled():
        log.debug("atom_embedder: MedCPT auto-download disabled")
        _medcpt_resolved_path = None
        return None

    try:
        from huggingface_hub import snapshot_download

        log.info(
            "atom_embedder: downloading/verifying %s via HF cache "
            "(set MEDCPT_AUTO_DOWNLOAD=false to skip)",
            _MEDCPT_HF_REPO,
        )
        # snapshot_download returns immediately from cache when already present.
        # Passing token enables authenticated (CDN-accelerated) transfers.
        path = snapshot_download(
            repo_id=_MEDCPT_HF_REPO,
            token=_get_hf_token(),
            ignore_patterns=["*.msgpack", "*.h5", "flax_model*", "tf_model*"],
        )
        log.info("atom_embedder: MedCPT weights at %s", path)
        _medcpt_resolved_path = path
        return path
    except Exception as exc:
        log.info("atom_embedder: MedCPT local path unavailable (%s)", exc)
        _medcpt_resolved_path = None
        return None


# ─── MedCPT in-process model cache ───────────────────────────────────────────

_medcpt_tokenizer = None
_medcpt_model = None


def _load_medcpt(model_path: str):
    """Load tokenizer + model once; keep them alive in process memory.

    low_cpu_mem_usage=True streams weights into RAM instead of allocating a
    contiguous buffer, materially reducing peak memory during loading.
    """
    global _medcpt_tokenizer, _medcpt_model
    if _medcpt_model is not None:
        return _medcpt_tokenizer, _medcpt_model

    from transformers import AutoModel, AutoTokenizer

    log.info("atom_embedder: loading MedCPT from %s (low_cpu_mem_usage=True)", model_path)
    _medcpt_tokenizer = AutoTokenizer.from_pretrained(model_path)
    _medcpt_model = AutoModel.from_pretrained(model_path, low_cpu_mem_usage=True)
    _medcpt_model.eval()
    log.info("atom_embedder: MedCPT model cached in process memory")
    return _medcpt_tokenizer, _medcpt_model


def _medcpt_local_embed(text: str, model_path: str) -> list[float]:
    """Embed *text* using the locally cached MedCPT model."""
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
    vec = pooled[0].tolist()

    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


# ─── Backend detection ────────────────────────────────────────────────────────

def _detect_backend() -> tuple[str, Optional[str]]:
    """Return (backend_name, medcpt_local_path_or_None).

    MEDCPT_BACKEND controls the selection:
      "api"   — always Inference API (requires HF_TOKEN).
      "local" — always local model.
      "auto"  — (default) API when token is present, local otherwise.
    """
    mode = os.environ.get("MEDCPT_BACKEND", "auto").strip().lower()
    token = _get_hf_token()

    if mode == "api":
        if token:
            return "hf_api", None
        log.warning("atom_embedder: MEDCPT_BACKEND=api but no HF_TOKEN — falling back")

    if mode in ("auto", "api") and token:
        return "hf_api", None

    # Local MedCPT (explicit path or auto-download)
    medcpt_path = _resolve_medcpt_path()
    if medcpt_path:
        return "medcpt_local", medcpt_path

    if os.environ.get("OPENAI_API_KEY", "").strip():
        return "openai", None

    return "stub", None


# ─── Hash-based stub embedder ─────────────────────────────────────────────────

def _stub_embed(text: str) -> list[float]:
    """Deterministic 768-dim embedding from SHA-256. Not clinically valid."""
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).digest()
    extended = (digest * ((_EMBED_DIM // 32) + 1))[:_EMBED_DIM]
    raw = [struct.unpack("b", bytes([b]))[0] / 128.0 for b in extended]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


# ─── OpenAI embedder ──────────────────────────────────────────────────────────

_PROJ_SEED = 0xDEADBEEF
_proj_matrix: list[list[float]] | None = None


def _make_projection_matrix() -> list[list[float]]:
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


# ─── Public API ───────────────────────────────────────────────────────────────

def embed_signal_value(text: str) -> Optional[list[float]]:
    """Embed a signal_value string. Returns None on any failure.

    Backend and local path are resolved once per process (cached).
    When using HF Inference API, a fallback to the next available backend
    is attempted automatically on API errors so callers always get a result
    if any backend is reachable.
    """
    if not text or not text.strip():
        return None

    backend, medcpt_path = _detect_backend()
    try:
        if backend == "hf_api":
            try:
                vec = _hf_api_embed(text)
            except Exception as api_exc:
                log.warning(
                    "atom_embedder: HF API failed (%s) — trying local/OpenAI/stub",
                    api_exc,
                )
                # Retry with the next viable backend without using the API.
                local_path = _resolve_medcpt_path()
                if local_path:
                    vec = _medcpt_local_embed(text, local_path)
                elif os.environ.get("OPENAI_API_KEY", "").strip():
                    vec = _openai_embed(text)
                else:
                    vec = _stub_embed(text)
        elif backend == "medcpt_local" and medcpt_path:
            vec = _medcpt_local_embed(text, medcpt_path)
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
    backend, _ = _detect_backend()
    return backend


def reset_medcpt_cache() -> None:
    """Evict in-process MedCPT caches (useful in tests that swap backends)."""
    global _medcpt_tokenizer, _medcpt_model, _medcpt_resolved_path
    _medcpt_tokenizer = None
    _medcpt_model = None
    _medcpt_resolved_path = _UNRESOLVED

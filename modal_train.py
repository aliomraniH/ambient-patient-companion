"""
modal_train.py — SLM Companion LoRA Training
Deploy with: modal deploy modal_train.py
Call via: POST https://yourorg--companion-lora-trainer-train-endpoint.modal.run

Environment (set via `modal secret create hf-secrets`):
  HF_TOKEN — Hugging Face fine-grained token (write to private repos)

Called from:
  - Replit Scheduled Deployments (scripts/nightly_slm_training.py) — nightly
  - slm_retraining_watcher AgentRuntime watcher — urgent jobs within 15 min
  - trigger_cohort_training MCP tool — immediate manual trigger
"""

import modal
import os
import json

# ── Image: Unsloth + training stack ──────────────────────────────────────────
# Pin Unsloth to a stable release; update when upgrading base model
image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.1.0-cudnn8-devel-ubuntu22.04",
        add_python="3.11",
    )
    .pip_install(
        "torch==2.3.1",
        "xformers==0.0.27",
        "triton",
        "unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git",
        "trl>=0.9.0",
        "peft>=0.11.0",
        "accelerate>=0.30.0",
        "transformers>=4.43.0",
        "huggingface_hub>=0.24.0",
        "datasets>=2.20.0",
        "bitsandbytes>=0.43.0",
    )
)

app = modal.App("companion-lora-trainer", image=image)

# Shared HF secrets (set once with: modal secret create hf-secrets HF_TOKEN=hf_...)
hf_secret = modal.Secret.from_name("hf-secrets")

# ─────────────────────────────────────────────────────────────────────────────
# Core training function
# ─────────────────────────────────────────────────────────────────────────────

@app.function(
    gpu="H100",             # H100 80GB: ~$3.95/hr; cohort run ~6 min ≈ $0.40
    timeout=3600,           # 1 hr max; real runs are 5–20 min
    secrets=[hf_secret],
    retries=1,              # one automatic retry on failure
)
def train_adapter(
    adapter_name: str,
    jsonl_content: str,
    base_model: str = "Qwen/Qwen2.5-3B-Instruct",
    lora_rank: int = 16,
    lora_alpha: int = 32,
    num_epochs: int = 3,
    learning_rate: float = 2e-4,
    per_device_batch_size: int = 2,
    gradient_accumulation_steps: int = 4,
    max_seq_length: int = 4096,
) -> dict:
    """
    QLoRA fine-tuning of Qwen 2.5 3B-Instruct on a JSONL training corpus.
    Pushes the trained adapter to HF Hub as a private repo.

    Args:
        adapter_name:  HF Hub repo name, e.g. 'yourorg/cohort-diabetes-bh-adapter'
        jsonl_content: Full JSONL string — one ChatML JSON per line.
        base_model:    HF Hub model ID for the base model.
        lora_rank:     LoRA rank (default 16 — ~12 MB adapter on Qwen 3B).
        lora_alpha:    LoRA alpha scaling factor (default 32 = 2× rank).
        num_epochs:    Training epochs (default 3).
        learning_rate: AdamW learning rate (default 2e-4).

    Returns dict: {status, adapter_name, training_rows, loss_final, elapsed_sec}
    """
    import time
    from unsloth import FastLanguageModel
    from trl import SFTTrainer, SFTConfig
    from datasets import Dataset

    t0 = time.monotonic()

    # ── Parse JSONL corpus ────────────────────────────────────────────────────
    rows = []
    for line in jsonl_content.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if len(rows) < 50:
        return {
            "status": "error",
            "error": f"Corpus too small: {len(rows)} rows (minimum 50).",
        }

    print(f"[train_adapter] Corpus: {len(rows)} rows | Model: {base_model}")
    print(f"[train_adapter] Adapter: {adapter_name} | Rank: {lora_rank} | Epochs: {num_epochs}")

    # ── Load base model with 4-bit quantization (Unsloth) ────────────────────
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=base_model,
        max_seq_length=max_seq_length,
        dtype=None,         # auto-detect: bf16 on H100
        load_in_4bit=True,
        token=os.environ["HF_TOKEN"],
    )

    # ── Apply LoRA config across all linear projection layers ─────────────────
    model = FastLanguageModel.get_peft_model(
        model,
        r=lora_rank,
        lora_alpha=lora_alpha,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        lora_dropout=0.05,
        bias="none",
        use_gradient_checkpointing="unsloth",  # Unsloth's memory-efficient variant
        random_state=42,
        use_rslora=False,   # standard LoRA for MVP; enable RSLoRA at scale
    )

    # ── Build HuggingFace Dataset from ChatML rows ────────────────────────────
    # SFTTrainer expects 'messages' key with role/content list (ChatML)
    dataset = Dataset.from_list(rows)

    # ── Training configuration ────────────────────────────────────────────────
    training_args = SFTConfig(
        output_dir="/tmp/lora_output",
        per_device_train_batch_size=per_device_batch_size,
        gradient_accumulation_steps=gradient_accumulation_steps,
        warmup_steps=max(10, len(rows) // 20),
        num_train_epochs=num_epochs,
        learning_rate=learning_rate,
        fp16=False,
        bf16=True,          # bf16 natively supported on H100
        logging_steps=10,
        save_strategy="no", # no intermediate checkpoints — saves to HF Hub at end
        seed=42,
        report_to="none",   # no W&B in MVP
        max_seq_length=max_seq_length,
        dataset_text_field=None,   # use 'messages' key via chat template
        packing=False,             # no sequence packing — maintains turn boundaries
    )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        args=training_args,
    )

    print(f"[train_adapter] Starting training on {len(rows)} examples...")
    result = trainer.train()
    loss_final = result.training_loss

    # ── Push to HF Hub (private repo) ────────────────────────────────────────
    print(f"[train_adapter] Pushing adapter to HF Hub: {adapter_name}")
    model.push_to_hub(
        adapter_name,
        token=os.environ["HF_TOKEN"],
        private=True,
        commit_message=f"Training run: {len(rows)} examples, loss={loss_final:.4f}",
    )
    tokenizer.push_to_hub(
        adapter_name,
        token=os.environ["HF_TOKEN"],
        private=True,
    )

    elapsed = round(time.monotonic() - t0, 1)
    print(f"[train_adapter] Done. Loss={loss_final:.4f} | Elapsed={elapsed}s")

    return {
        "status": "completed",
        "adapter_name": adapter_name,
        "base_model": base_model,
        "training_rows": len(rows),
        "lora_rank": lora_rank,
        "num_epochs": num_epochs,
        "loss_final": round(float(loss_final), 4),
        "elapsed_sec": elapsed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Web endpoint (called by Replit FastMCP slm_companion.py)
# ─────────────────────────────────────────────────────────────────────────────

@app.function(keep_warm=0)  # No warm instances — cost-free when idle
@modal.web_endpoint(method="POST", label="train-endpoint")
async def train_endpoint(request) -> dict:
    """
    HTTP entry point. Validates bearer token, spawns training job asynchronously
    (fire-and-forget), and returns immediately with job reference.

    Request body (JSON):
        adapter_name: str    — HF Hub repo name
        jsonl_content: str   — JSONL training corpus
        base_model: str      — optional, defaults to Qwen/Qwen2.5-3B-Instruct
        lora_rank: int        — optional, defaults to 16
        num_epochs: int       — optional, defaults to 3

    Authorization: Bearer <MODAL_WEBHOOK_SECRET>
    """
    # Auth check — shared secret stored in Replit Secrets + Modal Secret
    auth_header = request.headers.get("Authorization", "")
    expected = f"Bearer {os.environ.get('MODAL_WEBHOOK_SECRET', '')}"
    if auth_header != expected:
        return {"error": "unauthorized"}, 401

    body = await request.json()
    adapter_name = body.get("adapter_name", "")
    jsonl_content = body.get("jsonl_content", "")

    if not adapter_name or not jsonl_content:
        return {"error": "Missing adapter_name or jsonl_content"}, 400

    row_count = jsonl_content.count("\n") + 1 if jsonl_content.strip() else 0
    if row_count < 50:
        return {"error": f"Corpus too small: {row_count} rows (minimum 50)"}, 400

    # Spawn training function — returns immediately, training runs in background
    call = train_adapter.spawn(
        adapter_name=adapter_name,
        jsonl_content=jsonl_content,
        base_model=body.get("base_model", "Qwen/Qwen2.5-3B-Instruct"),
        lora_rank=body.get("lora_rank", 16),
        num_epochs=body.get("num_epochs", 3),
    )

    return {
        "status": "queued",
        "job_id": call.object_id,  # Modal call ID — use to poll status
        "adapter_name": adapter_name,
        "corpus_rows": row_count,
        "note": "Training running asynchronously. Poll get_training_status(job_id=...) via MCP.",
    }

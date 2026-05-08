#!/bin/bash
# One-time Modal deployment. Run from repo root after: pip install modal
# After running, copy the printed endpoint URL to Replit Secrets as MODAL_TRAIN_ENDPOINT_URL
echo "Deploying SLM training endpoint to Modal..."
modal deploy modal_train.py
echo ""
echo "Next steps:"
echo "  1. Copy the endpoint URL above to Replit Secrets: MODAL_TRAIN_ENDPOINT_URL"
echo "  2. Run: modal secret create hf-secrets HF_TOKEN="
echo "  3. Run: modal secret create modal-webhook MODAL_WEBHOOK_SECRET="

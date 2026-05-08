# Replit Scheduled Deployments

## 1. Nightly SLM Training
- Script: scripts/nightly_slm_training.py
- Schedule: 0 2 * * *  (02:00 UTC)
- Purpose: Picks up normal/low priority adapter retraining jobs and submits to Modal

## 2. Morning Endpoint Warmup (optional but recommended)
- Script: scripts/morning_warmup.py  (create this file — see below)
- Schedule: 0 8 * * *  (08:00 UTC)
- Purpose: Wakes the HF endpoint from scale-to-zero before first patient sessions

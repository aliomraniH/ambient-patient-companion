#!/bin/bash
# =============================================================================
# setup_deploy_key.sh
#
# Run this ONCE to generate a repository deploy key for
# aliomraniH/ambient-patient-companion.
#
# What it does:
#   1. Generates a new Ed25519 SSH key pair (no passphrase).
#   2. Prints the PUBLIC key — add it to GitHub as a deploy key with
#      write access (Settings → Deploy keys → Add deploy key).
#   3. Prints the PRIVATE key — store it in Replit Secrets as
#      GITHUB_DEPLOY_KEY (replace any existing GITHUB_TOKEN).
#
# After completing both steps above, test with:
#   bash scripts/push_to_github.sh
# =============================================================================
set -euo pipefail

KEY_FILE="/tmp/github_deploy_key_$$"

echo ""
echo "============================================================"
echo "  Generating Ed25519 deploy key for ambient-patient-companion"
echo "============================================================"
echo ""

ssh-keygen -t ed25519 -C "ambient-patient-companion-deploy-key" \
    -f "$KEY_FILE" -N "" -q

echo "✓  Key pair generated."
echo ""
echo "------------------------------------------------------------"
echo "  STEP 1 — Add the PUBLIC key to GitHub"
echo "------------------------------------------------------------"
echo "  Repository:  https://github.com/aliomraniH/ambient-patient-companion"
echo "  Navigate to: Settings → Deploy keys → Add deploy key"
echo "  Title:       ambient-patient-companion-replit"
echo "  ✅ Check 'Allow write access'"
echo ""
echo "  PUBLIC KEY (copy everything between the dashes):"
echo "  --------------------------------------------------"
cat "${KEY_FILE}.pub"
echo "  --------------------------------------------------"
echo ""
echo "------------------------------------------------------------"
echo "  STEP 2 — Store the PRIVATE key in Replit Secrets"
echo "------------------------------------------------------------"
echo "  Secret name:  GITHUB_DEPLOY_KEY"
echo "  Secret value: (copy everything between the dashes,"
echo "                 including the BEGIN/END lines)"
echo ""
echo "  PRIVATE KEY:"
echo "  --------------------------------------------------"
cat "${KEY_FILE}"
echo "  --------------------------------------------------"
echo ""
echo "  After saving the secret, you can safely delete GITHUB_TOKEN"
echo "  from Replit Secrets (or keep it as a fallback — the push"
echo "  script will prefer the deploy key when both are present)."
echo ""
echo "------------------------------------------------------------"
echo "  STEP 3 — Test the push"
echo "------------------------------------------------------------"
echo "  bash scripts/push_to_github.sh"
echo ""

rm -f "$KEY_FILE" "${KEY_FILE}.pub"
echo "Temporary key files removed from /tmp."

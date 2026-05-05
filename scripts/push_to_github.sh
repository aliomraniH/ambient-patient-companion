#!/bin/bash
# =============================================================================
# push_to_github.sh
#
# Push the current branch to aliomraniH/ambient-patient-companion on GitHub.
#
# Credential priority (highest wins):
#   1. GITHUB_DEPLOY_KEY secret — SSH deploy key (never expires, preferred)
#   2. GITHUB_TOKEN secret      — classic/fine-grained PAT via HTTPS (fallback)
#
# Usage:
#   bash scripts/push_to_github.sh [branch]        # default: current branch
#   bash scripts/push_to_github.sh main
# =============================================================================
set -uo pipefail

REPO_SSH="git@github.com:aliomraniH/ambient-patient-companion.git"
REPO_HTTPS="https://github.com/aliomraniH/ambient-patient-companion.git"
BRANCH="${1:-$(git rev-parse --abbrev-ref HEAD)}"

echo "[push] Branch: $BRANCH"

# ---------------------------------------------------------------------------
# Path 1: SSH deploy key (non-expiring)
# ---------------------------------------------------------------------------
if [[ -n "${GITHUB_DEPLOY_KEY:-}" ]]; then
    echo "[push] Using SSH deploy key (GITHUB_DEPLOY_KEY)."

    SSH_DIR="$HOME/.ssh"
    KEY_FILE="$SSH_DIR/github_deploy_key"
    SSH_CONFIG="$SSH_DIR/github_deploy_key.conf"
    KNOWN_HOSTS_FILE="$SSH_DIR/github_known_hosts"
    RESTORE_HTTPS=false

    mkdir -p "$SSH_DIR"
    chmod 700 "$SSH_DIR"

    # ----- Cleanup trap: always runs on exit (success or failure) -----------
    cleanup() {
        local exit_code=$?
        # Restore HTTPS remote URL so local dev still works without SSH.
        if [[ "$RESTORE_HTTPS" == "true" ]]; then
            git remote set-url origin "$REPO_HTTPS" 2>/dev/null || true
        fi
        # Remove all ephemeral key material from disk.
        rm -f "$KEY_FILE" "$SSH_CONFIG" "$KNOWN_HOSTS_FILE"
        exit $exit_code
    }
    trap cleanup EXIT

    # Write the private key exactly as stored (handles both LF and CRLF).
    printf '%s' "$GITHUB_DEPLOY_KEY" | tr -d '\r' > "$KEY_FILE"
    # Ensure a trailing newline — OpenSSH requires it.
    printf '\n' >> "$KEY_FILE"
    chmod 600 "$KEY_FILE"

    # Fetch and pin GitHub's current host keys (avoids MITM; no manual approval).
    ssh-keyscan -H github.com > "$KNOWN_HOSTS_FILE" 2>/dev/null
    chmod 600 "$KNOWN_HOSTS_FILE"

    # SSH config: use the deploy key and the pinned known-hosts for github.com.
    cat > "$SSH_CONFIG" <<EOF
Host github.com
    HostName github.com
    User git
    IdentityFile $KEY_FILE
    IdentitiesOnly yes
    StrictHostKeyChecking yes
    UserKnownHostsFile $KNOWN_HOSTS_FILE
    LogLevel ERROR
EOF
    chmod 600 "$SSH_CONFIG"

    # Ensure the origin remote uses the SSH URL for this push.
    if git remote get-url origin 2>/dev/null | grep -q "^https://"; then
        echo "[push] Switching origin to SSH URL for this push..."
        git remote set-url origin "$REPO_SSH"
        RESTORE_HTTPS=true
    fi

    # Perform the push (set -e is off; capture failure via ||).
    GIT_SSH_COMMAND="ssh -i $KEY_FILE -F $SSH_CONFIG" \
        git push origin "$BRANCH" \
        && echo "[push] ✓ Pushed '$BRANCH' via SSH deploy key." \
        || { echo "[push] ✗ SSH push failed." >&2; exit 1; }

# ---------------------------------------------------------------------------
# Path 2: PAT via HTTPS (fallback)
# ---------------------------------------------------------------------------
elif [[ -n "${GITHUB_TOKEN:-}" ]]; then
    echo "[push] GITHUB_DEPLOY_KEY not set — falling back to GITHUB_TOKEN (PAT)."
    echo "[push] Warning: classic PATs expire; run scripts/setup_deploy_key.sh"
    echo "[push]          to migrate to a non-expiring deploy key."

    # Embed the token in the remote URL for credential-free push.
    AUTHED_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/aliomraniH/ambient-patient-companion.git"
    git push "$AUTHED_URL" "$BRANCH" \
        && echo "[push] ✓ Pushed '$BRANCH' via PAT (HTTPS)." \
        || { echo "[push] ✗ PAT push failed." >&2; exit 1; }

# ---------------------------------------------------------------------------
# Neither credential is available
# ---------------------------------------------------------------------------
else
    echo "[push] ✗ No GitHub credentials found." >&2
    echo "[push]   Set GITHUB_DEPLOY_KEY (preferred) or GITHUB_TOKEN in Replit Secrets." >&2
    echo "[push]   Run scripts/setup_deploy_key.sh to create a deploy key." >&2
    exit 1
fi

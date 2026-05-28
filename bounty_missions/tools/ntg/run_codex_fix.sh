#!/bin/zsh
set -euo pipefail

WORKSPACE_DIR=${1:?workspace_dir is required}
REPO_SLUG=${2:?repo slug is required}
ISSUE_NUMBER=${3:?issue number is required}
ISSUE_TITLE=${4:-}

if ! command -v codex >/dev/null 2>&1; then
  echo "codex not found in PATH; skipping automation for ${REPO_SLUG}#${ISSUE_NUMBER}"
  exit 0
fi

if [[ -z "${OPENAI_API_KEY:-}" && -z "${CODEX_API_KEY:-}" && ! -f "${HOME}/.codex/config.toml" && ! -f "${HOME}/.codex/auth.json" ]]; then
  echo "codex auth/provider config not found; skipping automation for ${REPO_SLUG}#${ISSUE_NUMBER}"
  exit 0
fi

PROMPT=$(cat <<EOF
You are working in an NTG-prepared bounty workspace for GitHub issue ${REPO_SLUG}#${ISSUE_NUMBER}.
Title: ${ISSUE_TITLE}

Workspace layout:
- ../README.md contains the prepared issue summary
- ../context/ contains raw GitHub issue, repo, comments, and search context
- current directory is the cloned target repository

Your task:
1. Read the prepared context first.
2. Decide whether the issue is actually actionable and worth fixing.
3. If it is actionable, implement the smallest valid change in this repository.
4. Run the narrowest relevant validation or tests you can.
5. Write a short summary to ../WORKLOG.md covering what you changed or why you declined.

Constraints:
- Do not commit or push.
- Prefer minimal diffs.
- If the issue is not a good target, explain that clearly in ../WORKLOG.md.
EOF
)

cd "${WORKSPACE_DIR}/repo"
codex exec \
  --dangerously-bypass-approvals-and-sandbox \
  --add-dir "${WORKSPACE_DIR}" \
  -C "${WORKSPACE_DIR}/repo" \
  -o "${WORKSPACE_DIR}/codex-last-message.txt" \
  "${PROMPT}"

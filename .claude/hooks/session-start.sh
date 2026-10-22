#!/bin/bash
# Claude Code SessionStart hook (cloud sessions only): run every check once in the
# background so the session starts with a baseline. A failure is reported, never blocking.
set -uo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

echo '{"async": true, "asyncTimeout": 300000}'
cd "$CLAUDE_PROJECT_DIR" || exit 0
# Hand-run git commands (and any tool that commits or pushes) get the same checks.
git config core.hooksPath .githooks
python3 scripts/run_checks.py || echo "[session-start] some checks fail on this checkout; see above" >&2

#!/usr/bin/env bash
# Opportunistic loop runner for zeus (invoked by the systemd user timer).
#
# DISCOVERS repos rather than reading a central list: any checkout under
# $HARNESS_REPO_ROOT (default ~/source) that ships a .harness-rules file is a
# candidate, and lander.py then skips the ones whose loops.dependabot_lander is
# not set. Adding a repo to the sweep is a commit in that repo, not an edit here.
#
# This runs UNATTENDED, so HARNESS_UNATTENDED=1 makes the resolver read each
# repo's rules from its DEFAULT BRANCH rather than the working tree. The lander's
# red-CI fixer is edit-only by design (no shell) and operates on upstream-authored
# dependabot branches; without this, such a branch could rewrite the policy
# governing its own review. See harness_rules.py.
#
# REPORT-ONLY by default — proposed actions are logged, nothing is merged/pushed.
# Flip to acting only once you've watched a supervised --execute run by hand:
#   LOOPS_EXECUTE=1  (env)  or  run-loop.sh --execute
#
# Single-flight via flock so a slow run never overlaps the next timer tick.
set -euo pipefail

LOOPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"     # ~/.claude/loops
LOCK="/tmp/loops-lander.lock"
LOG_DIR="${LOOPS_LOG_DIR:-$HOME/loops-logs}"

export HARNESS_UNATTENDED=1

EXECUTE=""
[[ "${LOOPS_EXECUTE:-}" == "1" || "${1:-}" == "--execute" ]] && EXECUTE="--execute"

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "another loop run holds $LOCK — skipping this tick"
    exit 0
fi

mkdir -p "$LOG_DIR"
STAMP="$(date +%Y-%m-%d)"            # one log per day
LOG="$LOG_DIR/lander-$STAMP.log"

# Every checkout that ships a rules file. lander.py does the actual opt-in check
# (loops.dependabot_lander, read from the default branch) and skips the rest, so
# this list is deliberately permissive — discovery, not authorization.
mapfile -t REPOS < <(python3 "$LOOPS_DIR/harness_rules.py" --discover)

{
    echo "===== $(date -Is)  mode=${EXECUTE:-report-only}  candidates=${#REPOS[@]} ====="
    for repo in "${REPOS[@]:-}"; do
        [[ -z "$repo" ]] && continue
        echo "----- lander: $repo -----"
        # Plain python3, not `uv run`: no project to resolve from here, and
        # lander.py is stdlib-only. It addresses each repo via git -C / gh --repo.
        python3 "$LOOPS_DIR/lander.py" --repo "$repo" $EXECUTE || echo "(lander failed for $repo)"
    done
    echo
} >>"$LOG" 2>&1

echo "loop run complete -> $LOG"

#!/usr/bin/env bash
# seats-testrun.sh — bring up a real seat screen from three unmerged branches.
#
#   seats-testrun.sh ~/source/seat-test [N]
#
# Assembles what nothing has merged yet: qb-seats (#162, the layout), qb-seat
# (#158, the per-seat wrapper) and qb-board (#160, the dashboard) onto one PATH,
# borrows tmux from nixpkgs, and starts N real agents in a throwaway repo.
#
# The seats are REAL claude sessions on the REAL board. They register as
# zeus/seat-1 … zeus/seat-N and post presence like any other agent. What bounds
# them is the brief, so the default here is a harmless one — say hello, stop.
# For a genuine self-selecting fleet, run with QB_SEAT_BRIEF unset:
#
#   QB_SEAT_BRIEF= seats-testrun.sh ~/source/seat-test 2     # no, this is empty
#   env -u QB_SEAT_BRIEF seats-testrun.sh ~/source/seat-test # this is unset
#
# (empty means "start the agent with no prompt, waiting"; unset means "use the
# shipped brief" — read the board, claim an unclaimed item, work it, stop.)
set -euo pipefail

REPO=${1:?usage: seats-testrun.sh /path/to/test-repo [seats]}
SEATS=${2:-2}

SEATBIN=/tmp/seatbin
W110=${QB_MCP_CHECKOUT:-/home/rich/source/quarterback-feat-issue-110}   # PR #160: qb-board
LAYOUT=${QB_LAYOUT_CHECKOUT:-/home/rich/source/quarterback-seats}       # PR #162: qb-seats
SESSION=${QB_TEST_SESSION:-try}

# The layout refuses a non-repo, and rightly: a seat that claims work needs
# somewhere to do it.
mkdir -p "$REPO"
[ -d "$REPO/.git" ] || git -C "$REPO" init -q

mkdir -p "$SEATBIN"
cp "$LAYOUT/harness/bin/qb-seats" "$SEATBIN/qb-seats"
git -C "$LAYOUT" show origin/feat/issue-121:harness/bin/qb-seat > "$SEATBIN/qb-seat"
chmod +x "$SEATBIN/qb-seats" "$SEATBIN/qb-seat"

# qb-board is a launcher that hunts for a python which can import
# mcp_server.board; only #160's worktree has one. QB_BOARD_PYTHON would say so,
# but qb-seats forwards three variables and that is not one of them — a pane's
# environment comes from the tmux server, so it would never arrive. Bake it in.
cat > "$SEATBIN/qb-board" <<EOF
#!/bin/sh
exec env QB_BOARD_PYTHON=$W110/mcp/.venv/bin/python "$W110/harness/bin/qb-board" "\$@"
EOF
chmod +x "$SEATBIN/qb-board"

# An isolated server, so this cannot inherit the PATH of one already running
# (which is how a pane ends up unable to find qb-seat) and cannot disturb
# anything else using tmux.
export TMUX_TMPDIR=${TMUX_TMPDIR:-/tmp/seatrun}
mkdir -p "$TMUX_TMPDIR"

export PATH="$SEATBIN:$PATH"
export QB_SEAT_BRIEF="${QB_SEAT_BRIEF-Post one note to the board introducing yourself as this seat, using board_post(type='note'). Then stop and wait. Claim nothing.}"

echo "seats: $SEATS in $REPO, session '$SESSION', tmpdir $TMUX_TMPDIR"
echo "brief: ${QB_SEAT_BRIEF:-<empty — agents start waiting, no prompt>}"
echo
exec nix shell nixpkgs#tmux -c qb-seats -C "$REPO" -n "$SEATS" -s "$SESSION" "${@:3}"

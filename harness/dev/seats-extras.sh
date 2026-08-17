#!/usr/bin/env bash
# seats-extras.sh — add the dash to a running seat screen, and rename the tape.
#
#   seats-extras.sh [session] [width]      default: try, 78 columns
#
# Two changes to a screen that is already up, neither of which restarts a seat:
#
#   * a full-height pane down the right running qb-dash (fleet state)
#   * the bottom board pane relabelled 'tape' (the event stream)
#
# Idempotent: run it again after resizing and it respawns the dash rather than
# stacking a second one.
set -euo pipefail

SESSION=${1:-try}
WIDTH=${2:-78}
WIN="$SESSION:seats"

SEATBIN=/tmp/seatbin
# Where the board client and its deps live. #160's worktree today; once that
# lands it is this repo's own mcp/.venv. Override for another checkout.
W110=${QB_MCP_CHECKOUT:-/home/rich/source/quarterback-feat-issue-110}
SRC="$(cd -P "$(dirname "$0")" && pwd)"
LAYOUT=${QB_LAYOUT_CHECKOUT:-/home/rich/source/quarterback-seats}
export TMUX_TMPDIR=${TMUX_TMPDIR:-/tmp/seatrun}

# Resolve tmux ONCE. `nix shell ... -c tmux` per call would be a fresh nix
# evaluation per tmux command, and this script issues a dozen.
if command -v tmux >/dev/null 2>&1; then
  TMUX_BIN=$(command -v tmux)
else
  # A derivation has several outputs and this prints all of them — tmux's `man`
  # output sorts alongside `out`, so picking a line blind gets you a directory.
  TMUX_BIN=""
  for out in $(nix build --no-link --print-out-paths nixpkgs#tmux); do
    [ -x "$out/bin/tmux" ] && { TMUX_BIN="$out/bin/tmux"; break; }
  done
  [ -n "$TMUX_BIN" ] || { echo "seats-extras: could not resolve a tmux binary" >&2; exit 1; }
fi
tmux() { "$TMUX_BIN" "$@"; }

# The dashboard needs #160's venv (rich, and the board client). A wrapper keeps
# that fact out of the pane's command line, and out of the environment that
# qb-seats would have to forward.
mkdir -p "$SEATBIN"
# Prefer the INSTALLED dashboard. It carries its own interpreter, which is the
# whole point of packaging it — and forcing a checkout venv here was actively
# wrong: a uv-standalone python has no CA bundle, so the pane showed "board
# unreachable" against a board that was up, beside a shell where it worked.
# Only fall back to the checkout when nothing is installed.
if command -v qb-dash-tui >/dev/null 2>&1; then
  DASH_TUI=$(command -v qb-dash-tui); DASH_PLAIN=$(command -v qb-dash)
else
  for f in qbdata.py qb-dash.py qb-dash-tui.py qb-dash qb-dash-tui; do
    cp "$LAYOUT/harness/bin/$f" "$SEATBIN/$f"
  done
  chmod +x "$SEATBIN/qb-dash" "$SEATBIN/qb-dash-tui"
  DASH_TUI="$SEATBIN/qb-dash-tui"; DASH_PLAIN="$SEATBIN/qb-dash"
fi

# The clickable one by default; QB_DASH=rich for the plain redrawing renderer,
# which is the one to fall back to if a terminal turns out not to forward mouse
# events (ssh through something old, say).
case "${QB_DASH:-tui}" in
  rich) DASH_CMD="$DASH_PLAIN" ;;
  *)    DASH_CMD="$DASH_TUI" ;;
esac

tmux has-session -t "=$SESSION" 2>/dev/null || {
  echo "seats-extras: no session '$SESSION' — start it with seats-testrun.sh first" >&2
  exit 1
}

# Labels. qb-seats' own format prints 'board' for any pane without @qb_seat,
# which would name the dash 'board' too. Widen it to a second option so a pane
# can say what it is: seats keep their number, everything else gets @qb_label.
tmux set-option -w -t "$WIN" pane-border-status top
tmux set-option -w -t "$WIN" pane-border-format \
  ' #{?@qb_seat,seat #{@qb_seat},#{?@qb_label,#{@qb_label},pane}} #{?pane_active,*,} '

# The tape is the pane with neither a seat number nor a label yet.
while read -r pane seat label; do
  [ -z "$seat" ] && [ -z "$label" ] && tmux set-option -p -t "$pane" @qb_label tape
done < <(tmux list-panes -t "$WIN" -F '#{pane_id} #{@qb_seat} #{@qb_label}')

# One dash, not one per run.
existing=$(tmux list-panes -t "$WIN" -F '#{pane_id} #{@qb_label}' | awk '$2 == "dash" {print $1}')
if [ -n "$existing" ]; then
  # Resize as well as respawn. Attaching a client resizes the window and the
  # panes get redistributed — the dash was found at 29 columns after an attach,
  # having been placed at 78 — so the width has to be reasserted every run.
  tmux resize-pane -t "$existing" -x "$WIDTH"
  tmux respawn-pane -k -t "$existing" "$DASH_CMD"
  echo "seats-extras: dash respawned in $existing at ${WIDTH} cols"
  exit 0
fi

# -f: full height of the WINDOW, not a split of the focused pane — the dash is a
# column beside everything, including the tape.
pane=$(tmux split-window -h -f -l "$WIDTH" -t "$WIN" -P -F '#{pane_id}')
tmux set-option -p -t "$pane" @qb_label dash

# Typed into a shell rather than run as the pane's command, the way qb-seats
# starts a seat: if the dash exits or you Ctrl-C it, you get a prompt in the
# pane instead of the pane vanishing.
tmux send-keys -t "$pane" "$DASH_CMD" C-m

# NO `select-layout -E` here, however much it looks like it belongs. Spreading
# the window out re-equalises every pane, including the one just placed at an
# explicit width — the dash came out at 99 columns when asked for 74.

echo "seats-extras: dash added ($pane, ${WIDTH} cols), board pane relabelled 'tape'"

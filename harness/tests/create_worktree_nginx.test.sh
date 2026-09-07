#!/usr/bin/env bash
#
# Not executable, deliberately: the mode bit is off and the shebang above is a
# dialect note for editors and shellcheck rather than something that ever runs.
# This file needs bash — `${BASH_SOURCE[0]}`, the `BOXES` array, the `<<<`
# here-strings and printf's `%q` are all bash — and it is copied into the
# `worktree-tests` nix check, where NO shebang naming bash can work: a build
# sandbox has no /usr/bin/env and no /bin/bash, only /bin/sh, and `patchShebangs`
# in that check is pointed at harness/bin rather than at harness/tests. So the one
# way to start this suite is `bash <path>`, which is what
# test_create_worktree_nginx.py does. Leaving the mode bit on would advertise a
# way of running it that fails in the sandbox and nowhere else — the same trap the
# docker stub below was fixed for, one level up. If it ever does have to be
# directly executable in the sandbox, the repair is `patchShebangs harness/tests`
# in that check, not a shebang this file can carry on its own.
#
# nginx-block tests for harness/bin/create-worktree and harness/bin/remove-worktree
# (found via lexray #1501).
#
# This suite came from nix-fleet, where these two scripts used to live. They moved
# here in nix-fleet's 80e8f18 ("consume the agent harness from quarterback's
# flake") and the test did not come with them: it kept pointing at
# `home/bin/create-worktree`, which no longer existed, so it hard-failed on its
# first line and tested nothing from that day on. Nothing reported that, because
# nix-fleet has no CI. It is here now because this is where the scripts are and
# where a suite actually gets run.
#
# It is driven by test_create_worktree_nginx.py rather than rewritten as pytest:
# the assertions below encode specific regressions someone already paid for, and
# a hand-port is a chance to drop one silently. The wrapper is the cheap half.
#
# Pure bash, no bats: each case runs the real scripts against a throwaway git
# repo with a stubbed `docker` (every container operation is a no-op), so the
# nginx step runs for real and its output can be read off the config file.
#
# What is pinned:
#   - the block is keyed on the slash-free SAFE_NAME, so a `fix/issue-42` branch
#     proxies to the container that actually exists (myproj-fix-issue-42) rather
#     than to the unresolvable host "myproj-fix/issue-42";
#   - .worktree.json's extra_proxy_headers reach the generated block (they were
#     assembled into a variable the awk program never read, so X-Product was
#     silently dropped), without duplicating a header the template emits;
#   - removal strips the block by exact marker name, so tearing down "feat-a"
#     leaves "feat-abc" alone;
#   - a configured-but-missing nginx config is reported, and the sub-path URL is
#     not advertised — the silent skip #1501 was filed for.
#
# Run: pytest harness/tests/test_create_worktree_nginx.py — or, for the report on
# stdout, `bash harness/tests/create_worktree_nginx.test.sh`. Started with `bash`
# explicitly, for the reason at the top of this file.
#
# A case name as the one argument runs just that scenario, and `--list` prints the
# names; both are for the pytest wrapper, which runs one case per test so xdist
# can spread them (#785). With no argument every case runs, in CASES order, and
# the report is the same one this file has always printed.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Overridable so the suite can be pointed at another revision of the scripts
# (e.g. `git show HEAD:home/bin/create-worktree`) to confirm it still catches
# the regressions it was written for.
CREATE="${CREATE_WORKTREE_BIN:-$HERE/../bin/create-worktree}"
REMOVE="${REMOVE_WORKTREE_BIN:-$HERE/../bin/remove-worktree}"
for script in "$CREATE" "$REMOVE"; do
    [ -f "$script" ] || { echo "FAIL: not found: $script" >&2; exit 1; }
done

pass=0; fail=0
ok(){ printf '  \033[32mok\033[0m   %s\n' "$1"; pass=$((pass+1)); }
no(){ printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
has(){ if grep -qF -- "$3" "$2"; then ok "$1"; else no "$1"; printf '        missing: %q\n' "$3"; fi; }
hasnt(){ if grep -qF -- "$3" "$2"; then no "$1"; printf '        present: %q\n' "$3"; else ok "$1"; fi; }
hasnt_re(){ if grep -qE -- "$3" "$2"; then no "$1"; printf '        present: %q\n' "$3"; else ok "$1"; fi; }
says(){ if grep -qF -- "$3" <<<"$2"; then ok "$1"; else no "$1"; printf '        not in output: %q\n' "$3"; fi; }
says_not(){ if grep -qF -- "$3" <<<"$2"; then no "$1"; printf '        in output: %q\n' "$3"; else ok "$1"; fi; }
eq(){ if [ "$2" = "$3" ]; then ok "$1"; else no "$1"; printf '        expected: %q\n        actual:   %q\n' "$3" "$2"; fi; }

BOXES=()
cleanup(){ for box in "${BOXES[@]:-}"; do [ -n "$box" ] && chmod -R u+w "$box" 2>/dev/null; rm -rf -- "$box"; done; }
trap cleanup EXIT

# make_sandbox [nginx_config_path] [extra_headers_json] [unmarkable] -> repo dir.
# Pass a path that does not exist to exercise the missing-config branch, and
# "unmarkable" for a config the marker inserter cannot handle (no line that is
# just "}"), which is the other way the block can end up unwritten.
make_sandbox(){
    local nginx_config="${1:-nginx/app.conf}"
    # Assigned separately: a "}" inside a ${x:-default} closes the expansion,
    # which silently truncated the /{branch} placeholder into invalid JSON.
    local extra_headers="${2:-}"
    [ -n "$extra_headers" ] ||
        extra_headers='["X-Product $x_product", "X-Script-Name /{branch}"]'
    local unmarkable="${3:-}"
    local box repo
    box=$(mktemp -d)
    BOXES+=("$box")
    repo="$box/myproj"
    mkdir -p "$repo/nginx" "$box/bin"

    # Docker must look installed for the docker/nginx steps to be selected, but
    # nothing here should actually talk to a daemon.
    #
    # /bin/sh, not `#!/usr/bin/env bash`: there is no /usr/bin/env inside the nix
    # build sandbox. A stub that cannot exec still passes `command -v docker`, so
    # create-worktree gets an empty container list instead of an error and skips
    # the nginx step in silence — which reads as eighteen assertion failures about
    # missing blocks rather than as a broken stub. The shipped scripts dodge this
    # via patchShebangs; a stub written at runtime cannot.
    cat > "$box/bin/docker" <<'STUB'
#!/bin/sh
case "${1:-}" in
    ps)      printf '%s\n' ${DOCKER_PS_NAMES:-} ;;
    images)  echo "myproj" ;;
    network) echo "myproj_web_network" ;;
    inspect) echo '{}' ;;
esac
exit 0
STUB
    chmod +x "$box/bin/docker"

    # A home with no quarterback config in it, and board tools that record what
    # they were handed instead of doing it. #528: these two functions ran the
    # real scripts with no environment of their own at all, so `worktree-holder`,
    # `qb-admit` and `qb-claim` were found on the developer's PATH and read the
    # developer's `~/.config/quarterback/config` — one authenticated `GET /active`
    # against the PRODUCTION board on every run of this file, with a live bearer
    # token, in a suite whose entire subject is an nginx config block.
    #
    # Both halves, for the reason the python side gives: the stubs stop the tools
    # RUNNING, and the empty home stops anything that reaches them by another
    # route (a `${0%/*}` sibling, a name this list has not got) from having a
    # credential when it gets there. Neither alone is the property.
    mkdir -p "$box/home/.config" "$box/home/.cache"
    for tool in worktree-holder qb-admit qb-claim qb-release qb-mode qb-catchup; do
        cat > "$box/bin/$tool" <<STUB
#!/bin/sh
# Records the environment it was handed, so the isolation is asserted rather
# than assumed — see "the board is out of reach" below.
{
  printf '%s\n' "tool=$tool"
  printf '%s\n' "HOME=\${HOME:-}"
  printf '%s\n' "XDG_CONFIG_HOME=\${XDG_CONFIG_HOME:-}"
  printf '%s\n' "QUARTERBACK_CONFIG=\${QUARTERBACK_CONFIG:-}"
  printf '%s\n' "QUARTERBACK_BASE_URL=\${QUARTERBACK_BASE_URL:-}"
  printf '%s\n' "QUARTERBACK_TOKEN=\${QUARTERBACK_TOKEN:-}"
  printf '%s\n' "QUARTERBACK_TOKEN_CMD=\${QUARTERBACK_TOKEN_CMD:-}"
} >> "$box/board.calls"
exit 0
STUB
        chmod +x "$box/bin/$tool"
    done

    printf 'FROM scratch\n' > "$repo/Dockerfile"
    cat > "$repo/.worktree.json" <<CONF
{
  "project": "myproj",
  "app_port": 5005,
  "base_port": 5680,
  "nginx": {
    "config": "$nginx_config",
    "container": "none",
    "main_port": 5085,
    "resolver": false,
    "extra_proxy_headers": $extra_headers
  },
  "symlinks": [],
  "copies": []
}
CONF
    if [ -n "$unmarkable" ]; then
        # One line, so the marker inserter finds no "}" of its own to insert at.
        printf 'server { listen 5085; location / { proxy_pass http://webapp; } }\n' \
            > "$repo/nginx/app.conf"
    else
        cat > "$repo/nginx/app.conf" <<'CONF'
server {
    listen 5085;
    location / { proxy_pass http://webapp; }
}
CONF
    fi
    git -C "$repo" init -q
    git -C "$repo" add -A
    git -C "$repo" -c user.email=t@t -c user.name=t commit -qm init
    echo "$repo"
}

# The environment both runners hand the script under test (#528). Two halves:
# this box's own board credentials removed by name, and every path a board client
# would resolve pointed inside the sandbox.
# `${QUARTERBACK_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/quarterback/config}` is
# the one rule `qb-env:57` and `qbdata.resolve_config()` both apply, so all three
# of those are set — a tool honouring any one of them lands somewhere this suite
# made and left empty.
#
# Called INSIDE each runner's subshell rather than emitting `NAME=value` words for
# `env` to split: a box path with a space in it would otherwise arrive as two
# arguments, and the failure would look like a broken script rather than a broken
# fixture.
sandbox_env(){  # sandbox_env <box>   — exports into the calling subshell
    local box="$1"
    unset QUARTERBACK_BASE_URL QUARTERBACK_TOKEN QUARTERBACK_TOKEN_CMD \
          QUARTERBACK_TOKEN_REFRESH_CMD QUARTERBACK_HUMAN_URL \
          QUARTERBACK_HUMAN_KEY QUARTERBACK_HUMAN_KEY_CMD \
          QUARTERBACK_INSTANCE QUARTERBACK_AGENT QUARTERBACK_ELEVATED_TOKEN \
          QUARTERBACK_ELEVATED_TOKEN_CMD
    export HOME="$box/home"
    export XDG_CONFIG_HOME="$box/home/.config"
    export XDG_CACHE_HOME="$box/home/.cache"
    export CLAUDE_CONFIG_DIR="$box/home/.claude"
    export QUARTERBACK_CONFIG="$box/home/no-such-quarterback-config"
    export QB_SEATS_PACE=off
}

run_create(){   # run_create <repo> <branch>
    local repo="$1" branch="$2" box
    box="$(dirname "$repo")"
    (
        cd "$repo" || return 1
        sandbox_env "$box"
        PATH="$box/bin:$PATH" \
        DOCKER_PS_NAMES="myproj-${branch//\//-}" \
            bash "$CREATE" --no-workspace --no-fetch "$branch" 2>&1
    )
}

run_remove(){   # run_remove <repo> <branch>
    local repo="$1" branch="$2" box
    box="$(dirname "$repo")"
    (
        cd "$repo" || return 1
        sandbox_env "$box"
        PATH="$box/bin:$PATH" bash "$REMOVE" --keep-branch "$branch" 2>&1
    )
}

# The generated block for <safe name>, so assertions cannot pick up a sibling's.
block(){        # block <conf> <safe-name>
    awk -v b="$2" '
        $0 ~ ("# WORKTREE-START:" b "$") { on = 1 }
        on { print }
        $0 ~ ("# WORKTREE-END:" b "$") { on = 0 }
    ' "$1"
}

# ---------------------------------------------------------------------------
# The cases.
#
# Each scenario below is a function, and the selector at the foot of this file
# runs one of them by name. That is for wall time, not for coverage: this whole
# file used to be a single pytest test, one test is one xdist worker, and so on a
# 32-core box this suite on its own set the floor for the entire harness suite at
# the time it takes to run every scenario back to back — 24s, against 33s for
# everything else in harness/tests put together. Split, the floor is the slowest
# single scenario instead.
#
# The trade that buys is deliberate: scenarios that used to share a sandbox with
# their neighbour now each build their own. A sandbox costs ~90ms against ~3.4s
# for one `create-worktree` run, so the duplicated setup is noise next to the work
# it sets up, and the thing it buys is that no case can pass because of what an
# earlier case happened to leave behind — which, in a suite that spent years
# passing while testing nothing, is worth more than the milliseconds.
#
# Order of definition is not the run order; CASES at the foot of the file is.
# ---------------------------------------------------------------------------

# The first two cases both read the block generated for `fix/issue-42` in an
# otherwise default sandbox, and pin unrelated regressions in it — how the block
# is keyed, and whether configured headers reach it. They are two cases rather
# than one so a failure names which of the two broke and so they can run on
# separate workers; the shared setup lives here so that being two cases cannot
# let the two copies of it drift apart.
default_block(){   # sets repo/conf/out in the caller; writes $repo/block.txt
    repo=$(make_sandbox)
    conf="$repo/nginx/app.conf"
    out=$(run_create "$repo" fix/issue-42)
    block "$conf" fix-issue-42 > "$repo/block.txt"
}

case_slash_in_branch_name(){
    local repo conf out
    echo "A slash in the branch name (fix/issue-42)"
    default_block

    has "block is keyed on the slash-free name" "$conf" "# WORKTREE-START:fix-issue-42"
    # Guards the assertions below from passing vacuously on an empty extraction.
    eq "a block was extracted" "$([ -s "$repo/block.txt" ] && echo yes || echo no)" "yes"
    has "proxies to the container that exists" "$repo/block.txt" \
        'set $fix_issue_42_backend "myproj-fix-issue-42:5005";'
    hasnt "no slash survives into a backend host" "$repo/block.txt" "myproj-fix/issue-42"
    has "location path is the safe name" "$repo/block.txt" "location /fix-issue-42/ {"
    says "summary advertises the route it wrote" "$out" "http://localhost:5085/fix-issue-42/"
}

case_extra_proxy_headers(){
    local repo conf out
    echo "Configured extra_proxy_headers"
    default_block

    has "X-Product reaches the block" "$repo/block.txt" 'proxy_set_header X-Product $x_product;'
    eq "X-Script-Name is emitted exactly once" \
        "$(grep -c 'proxy_set_header X-Script-Name' "$repo/block.txt")" "1"
    has "X-Script-Name matches the location prefix" "$repo/block.txt" \
        "proxy_set_header X-Script-Name /fix-issue-42;"
}

# Deliberately one case and not two. Every assertion here is about the two
# branches interacting — the second block must be written despite the first
# marker being a prefix of it, and removing one must leave the other standing —
# so a "write" case and a "remove" case would both have to create both worktrees
# anyway, and splitting would buy a second copy of the setup rather than a
# shorter wall. This is the slowest case in the file for that reason: two
# `create-worktree` runs plus a removal.
case_prefix_siblings(){
    local repo conf out
    echo "Sibling names that prefix one another (feat-a / feat-abc)"
    # Written longest-first on purpose: a substring check on "feat-a" answers to
    # "feat-abc"'s marker, and the block is then skipped but still advertised.
    repo=$(make_sandbox)
    run_create "$repo" feat-abc >/dev/null
    out=$(run_create "$repo" feat-a)
    conf="$repo/nginx/app.conf"
    has "both blocks are written" "$conf" "# WORKTREE-START:feat-abc"
    has "shorter sibling gets its own block" "$conf" "myproj-feat-a:5005"
    says "and its route is advertised truthfully" "$out" "http://localhost:5085/feat-a/"

    run_remove "$repo" feat-a >/dev/null
    # Anchored: the marker for feat-a, not the feat-abc line that starts with it.
    hasnt_re "removed branch's block is gone" "$conf" "# WORKTREE-START:feat-a$"
    hasnt "removed branch's backend is gone" "$conf" "myproj-feat-a:5005"
    has "sibling block survives" "$conf" "# WORKTREE-START:feat-abc"
    has "sibling backend survives" "$conf" 'myproj-feat-abc:5005'
    has "surrounding config is intact" "$conf" "listen 5085;"
}

case_missing_nginx_config(){
    local repo out
    echo "Configured nginx file does not exist (#1501)"
    repo=$(make_sandbox nginx/deleted.conf)
    out=$(run_create "$repo" fix/issue-1501)

    says_not "no sub-path URL is advertised" "$out" "http://localhost:5085/"
    says "the missing config is named" "$out" "nginx/deleted.conf"
    says "the consequence is stated" "$out" "will NOT work"
}

case_unmarkable_config(){
    local repo out
    echo "A config the block cannot be inserted into"
    repo=$(make_sandbox nginx/app.conf "" unmarkable)
    out=$(run_create "$repo" fix/issue-42)

    says_not "no route advertised when nothing was written" "$out" "http://localhost:5085/"
    says "the failed write is reported" "$out" "No block was written"
}

case_header_the_template_also_emits(){
    local repo
    echo "An extra header the template also emits"
    repo=$(make_sandbox nginx/app.conf '["Host $host", "X-Product $x_product"]')
    run_create "$repo" feat-h >/dev/null
    block "$repo/nginx/app.conf" feat-h > "$repo/block.txt"

    # Both locations end up with the configured value (the static one already used
    # it), and the template's default is gone rather than sitting alongside it.
    eq "the configured Host value wins in both locations" \
        "$(grep -cF 'proxy_set_header Host $host;' "$repo/block.txt")" "2"
    hasnt "the template's Host default is replaced, not duplicated" "$repo/block.txt" \
        'proxy_set_header Host $host:$server_port;'
    has "unrelated extras are still appended" "$repo/block.txt" \
        'proxy_set_header X-Product $x_product;'
}

# The regression test for the reason this file was changed at all. It is not
# enough that the assertions above pass: they passed before too, while every run
# of this suite made an authenticated call to the production board. So the board
# tools record the environment they were handed and it is read back here.
#
# Asserted against the tools that DID run rather than against the runner's own
# source, because what matters is what arrived — a stanza that resolved its
# credential by some other route would still be caught.
case_board_out_of_reach(){
    local repo box
    echo "The board is out of reach (#528)"
    repo=$(make_sandbox)
    box="$(dirname "$repo")"
    run_create "$repo" fix/issue-42 >/dev/null
    run_remove "$repo" fix/issue-42 >/dev/null

    eq "a board tool was actually reached, so this case can fail" \
        "$([ -s "$box/board.calls" ] && echo yes || echo no)" "yes"
    hasnt "no board URL reached the tools" "$box/board.calls" "QUARTERBACK_BASE_URL=http"
    eq "no bearer token reached the tools" \
        "$(grep -c '^QUARTERBACK_TOKEN=.' "$box/board.calls")" "0"
    eq "no token command reached the tools" \
        "$(grep -c '^QUARTERBACK_TOKEN_CMD=.' "$box/board.calls")" "0"
    # The config file the ONE rule in qb-env:57 and qbdata.resolve_config() resolves.
    eq "every HOME they were given is inside the sandbox" \
        "$(grep '^HOME=' "$box/board.calls" | grep -cv "^HOME=$box/")" "0"
    eq "every config path they were given is inside the sandbox" \
        "$(grep '^QUARTERBACK_CONFIG=' "$box/board.calls" | grep -cv "^QUARTERBACK_CONFIG=$box/")" "0"
    eq "and that config file does not exist" \
        "$([ -e "$box/home/no-such-quarterback-config" ] && echo yes || echo no)" "no"
    eq "nor does the one \$XDG_CONFIG_HOME points at" \
        "$([ -e "$box/home/.config/quarterback/config" ] && echo yes || echo no)" "no"
}

# ---------------------------------------------------------------------------
# The case list, and the selector test_create_worktree_nginx.py drives.
#
# CASES is the run order for a bare `bash create_worktree_nginx.test.sh`, and it
# is also what `--list` prints, which is how the python wrapper learns the names
# to parametrise over. Nothing on the python side names a case: one added here
# becomes a new test id with no python edit, because a hardcoded mirror of a list
# is how a suite quietly stops covering whatever went into the list last — the
# same shape of silence this whole file was resurrected from.
# ---------------------------------------------------------------------------
CASES=(
    slash_in_branch_name
    extra_proxy_headers
    prefix_siblings
    missing_nginx_config
    unmarkable_config
    header_the_template_also_emits
    board_out_of_reach
)

case "${1:-}" in
    --list)
        printf '%s\n' "${CASES[@]}"
        exit 0
        ;;
    "")
        for case_name in "${CASES[@]}"; do
            "case_$case_name"
        done
        ;;
    *)
        # An unknown name must not read as a suite that passed: with no case run,
        # `fail` is 0 and so is the exit status, and a typo in the wrapper would
        # then be indistinguishable from green.
        if ! declare -F "case_$1" >/dev/null; then
            printf 'FAIL: no such case: %s\n' "$1" >&2
            printf 'known cases: %s\n' "${CASES[*]}" >&2
            exit 2
        fi
        "case_$1"
        ;;
esac

# Belt to the selector's braces: a case whose body stopped asserting anything —
# a renamed helper, an extraction that came back empty and was never checked —
# would otherwise report "passed 0, failed 0" and exit 0.
if [ $((pass + fail)) -eq 0 ]; then
    echo "FAIL: no assertions ran" >&2
    exit 1
fi

echo ""
printf 'passed %d, failed %d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]

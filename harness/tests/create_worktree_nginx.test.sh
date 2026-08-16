#!/usr/bin/env bash
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
# Run: bash tests/create-worktree-nginx.test.sh
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
    cat > "$box/bin/docker" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in
    ps)      printf '%s\n' ${DOCKER_PS_NAMES:-} ;;
    images)  echo "myproj" ;;
    network) echo "myproj_web_network" ;;
    inspect) echo '{}' ;;
esac
exit 0
STUB
    chmod +x "$box/bin/docker"

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

run_create(){   # run_create <repo> <branch>
    local repo="$1" branch="$2"
    ( cd "$repo" && PATH="$(dirname "$repo")/bin:$PATH" \
        DOCKER_PS_NAMES="myproj-${branch//\//-}" \
        bash "$CREATE" --no-workspace --no-fetch "$branch" 2>&1 )
}

run_remove(){   # run_remove <repo> <branch>
    local repo="$1" branch="$2"
    ( cd "$repo" && PATH="$(dirname "$repo")/bin:$PATH" \
        bash "$REMOVE" --keep-branch "$branch" 2>&1 )
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
echo "A slash in the branch name (fix/issue-42)"
# ---------------------------------------------------------------------------
repo=$(make_sandbox)
out=$(run_create "$repo" fix/issue-42)
conf="$repo/nginx/app.conf"
block "$conf" fix-issue-42 > "$repo/block.txt"

has "block is keyed on the slash-free name" "$conf" "# WORKTREE-START:fix-issue-42"
# Guards the assertions below from passing vacuously on an empty extraction.
eq "a block was extracted" "$([ -s "$repo/block.txt" ] && echo yes || echo no)" "yes"
has "proxies to the container that exists" "$repo/block.txt" \
    'set $fix_issue_42_backend "myproj-fix-issue-42:5005";'
hasnt "no slash survives into a backend host" "$repo/block.txt" "myproj-fix/issue-42"
has "location path is the safe name" "$repo/block.txt" "location /fix-issue-42/ {"
says "summary advertises the route it wrote" "$out" "http://localhost:5085/fix-issue-42/"

# ---------------------------------------------------------------------------
echo "Configured extra_proxy_headers"
# ---------------------------------------------------------------------------
has "X-Product reaches the block" "$repo/block.txt" 'proxy_set_header X-Product $x_product;'
eq "X-Script-Name is emitted exactly once" \
    "$(grep -c 'proxy_set_header X-Script-Name' "$repo/block.txt")" "1"
has "X-Script-Name matches the location prefix" "$repo/block.txt" \
    "proxy_set_header X-Script-Name /fix-issue-42;"

# ---------------------------------------------------------------------------
echo "Sibling names that prefix one another (feat-a / feat-abc)"
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
echo "Configured nginx file does not exist (#1501)"
# ---------------------------------------------------------------------------
repo=$(make_sandbox nginx/deleted.conf)
out=$(run_create "$repo" fix/issue-1501)

says_not "no sub-path URL is advertised" "$out" "http://localhost:5085/"
says "the missing config is named" "$out" "nginx/deleted.conf"
says "the consequence is stated" "$out" "will NOT work"

# ---------------------------------------------------------------------------
echo "A config the block cannot be inserted into"
# ---------------------------------------------------------------------------
repo=$(make_sandbox nginx/app.conf "" unmarkable)
out=$(run_create "$repo" fix/issue-42)

says_not "no route advertised when nothing was written" "$out" "http://localhost:5085/"
says "the failed write is reported" "$out" "No block was written"

# ---------------------------------------------------------------------------
echo "An extra header the template also emits"
# ---------------------------------------------------------------------------
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

echo ""
printf 'passed %d, failed %d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]

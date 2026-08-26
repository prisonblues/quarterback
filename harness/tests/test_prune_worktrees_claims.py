"""`prune-worktrees` sweeping claims left by worktrees that are gone (#337).

The teardown path hands a claim back; a tree removed with a bare `git worktree
remove` — the debris every other sweep in this script exists for — does not. The
claim then sits held by the machine, with no session for #277's stop half to
reach, until its 8h TTL runs out.

**The whole risk of this sweep is a false positive**, and the test that matters
is the one that fails to happen: an agent working an issue IN PLACE (no worktree
— `/fix-issue-here`, or a claim taken through the `claim` MCP tool) has no
directory for `is_live_suffix` to find, so a sweep keyed on "no worktree for this
issue" would free live work and report it as debris. The discriminator is the
NOTE — `worktree <branch> on <host>`, which `create-worktree` writes and nothing
else does — so this can only ever release what that script took.

Machine scope comes for free: `qb-claimed` asks `/claim/held`, which answers about
the asking machine, and another box's claim is neither listed here nor releasable
from here. Both are correct — that worktree is on that box.

The block is extracted from the real script rather than copied, so a refactor that
moves or renames it fails here instead of leaving this suite green about code
nobody runs.

Run: pytest harness/tests
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# A sibling module, imported by bare name — see `_path_sandbox`'s own docstring
# for why a suite that asserts a tool is absent cannot build its PATH from the
# host's.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "prune-worktrees"

BASH = shutil.which("bash")
JQ = shutil.which("jq")
#: `is_live_suffix` shells out to `tr` and the sweep pipes through `jq`. The
#: stanza's PATH below is deliberately narrow — the stub tools have to be the only
#: ones found — so coreutils has to be put back explicitly, or the normalisation
#: silently produces an empty string and every branch reads as live. Which is how
#: this suite first ran: green about a sweep that had swept nothing.
#:
#: Put back one BINARY at a time, not one directory at a time. The first fix for
#: that added `dirname(jq)` and `dirname(tr)` to PATH, and on a home-manager
#: install `dirname(jq)` is the profile directory that also holds the real
#: `qb-release` — so `test_without_qb_release_nothing_is_even_listed`, whose whole
#: subject is that tool being missing, ran with it present (#385, #472).
TR = shutil.which("tr")

pytestmark = pytest.mark.skipif(BASH is None or JQ is None or TR is None,
                                reason="bash, jq and tr must all be on PATH")

_START = "# >>> claim-sweep"
_END = "# <<< claim-sweep"


def sweep_block() -> str:
    src = SCRIPT.read_text()
    assert _START in src and _END in src, (
        f"the {_START} / {_END} markers are gone from prune-worktrees, so this suite "
        "is asserting nothing — fix the markers rather than deleting the test")
    block = src.split(_START, 1)[1].split("\n", 1)[1].split(_END, 1)[0]
    assert "ORPHAN_CLAIMS" in block, "the markers no longer bracket the claim sweep"
    return block


#: `is_live_suffix` and the live list, which live further up the real script.
#: Reproduced rather than extracted because they are what this stanza is being
#: tested AGAINST — a stub of the answer, not of the logic under test.
PRELUDE = """
set -uo pipefail
LIVE_SUFFIXES=(%s)
is_live_suffix() {
    local norm; norm="$(echo "$1" | tr '/' '-')"
    local s
    for s in "${LIVE_SUFFIXES[@]:-}"; do [[ "$s" == "$norm" ]] && return 0; done
    return 1
}
"""


def run_sweep(claims: list, *, live: list[str] = (), tmp_path: Path,
              qb_claimed: bool = True, qb_release: bool = True):
    """Run the sweep with a stub `qb-claimed` answering with `claims`."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if qb_claimed:
        fake = bindir / "qb-claimed"
        fake.write_text(f"#!{BASH}\n"
                        f"printf '%s' {json.dumps(json.dumps({'claims': claims}))}\n")
        fake.chmod(0o755)
    if qb_release:
        rel = bindir / "qb-release"
        rel.write_text(f"#!{BASH}\nexit 0\n")
        rel.chmod(0o755)
    script = _path_sandbox.sibling_dir(tmp_path) / "sweep.sh"
    script.write_text(
        (PRELUDE % " ".join(f"'{s}'" for s in live))
        + f'MAIN_REPO={tmp_path}\n'
        + sweep_block()
        + '\nprintf "CLAIM|%s\\n" "${ORPHAN_CLAIMS[@]:-}"\n'
        + 'printf "BRANCH|%s\\n" "${ORPHAN_CLAIM_BRANCHES[@]:-}"\n')
    got = subprocess.run(
        [BASH, str(script)], capture_output=True, text=True,
        env={"PATH": _path_sandbox.sandbox_path(tmp_path, bindir,
                                                tools=("jq", "tr")),
             "HOME": str(tmp_path)})
    assert got.returncode == 0, got.stderr
    got.branches = [ln.split("|", 1)[1] for ln in got.stdout.splitlines()
                    if ln.startswith("BRANCH|") and ln != "BRANCH|"]
    got.reported = [ln.split("|", 1)[1] for ln in got.stdout.splitlines()
                    if ln.startswith("CLAIM|") and ln != "CLAIM|"]
    return got


def checkout_claim(branch: str, key: str = "acme/widget#9") -> dict:
    """A claim shaped the way `create-worktree` writes one: no session, and a note
    naming the worktree it was taken for."""
    return {"claim_id": "c-1", "kind": "work", "key": key, "holder": "zeus",
            "session": None, "note": f"worktree {branch} on zeus"}


# ------------------------------------------------------- what it sweeps up

def test_a_claim_whose_worktree_is_gone_is_an_orphan(tmp_path):
    got = run_sweep([checkout_claim("feat/issue-9")], live=[], tmp_path=tmp_path)
    assert got.branches == ["feat/issue-9"]
    assert "acme/widget#9" in got.reported[0], (
        "the report names the key, so a dry run says which claim it means")


def test_a_claim_whose_worktree_is_still_here_is_left_alone(tmp_path):
    """`is_live_suffix` normalises `/` to `-`, because the directory suffix does:
    a live `quarterback-feat-issue-9` is the branch `feat/issue-9`."""
    got = run_sweep([checkout_claim("feat/issue-9")], live=["feat-issue-9"],
                    tmp_path=tmp_path)
    assert got.branches == []


def test_only_the_live_ones_survive_a_mixed_board(tmp_path):
    got = run_sweep([checkout_claim("feat/issue-1", "acme/widget#1"),
                     checkout_claim("fix/issue-2", "acme/widget#2"),
                     checkout_claim("feat/issue-3", "acme/widget#3")],
                    live=["fix-issue-2"], tmp_path=tmp_path)
    assert got.branches == ["feat/issue-1", "feat/issue-3"]


# ------------------------------------------------- what it must NEVER sweep up

def test_a_claim_with_no_note_is_not_touched(tmp_path):
    """It was not taken by a checkout, so there is no worktree it can be about."""
    got = run_sweep([{"key": "acme/widget#9", "note": None}], tmp_path=tmp_path)
    assert got.branches == []


@pytest.mark.parametrize("note", [
    "reviewing #9",
    "fix-issue-here on #9",
    "landing PR #9",
    "held by the planner",
    # Close, and still not it: the note has to START with the word, or a claim
    # whose note merely mentions a worktree is swept.
    "not a worktree feat/issue-9 on zeus",
])
def test_a_claim_taken_by_anything_but_a_checkout_is_not_touched(note, tmp_path):
    """The false positive that would matter: an agent working an issue in place
    has no worktree for `is_live_suffix` to find, so a sweep keyed on the worktree
    alone would free live work and call it debris."""
    got = run_sweep([{"key": "acme/widget#9", "note": note}], tmp_path=tmp_path)
    assert got.branches == [], f"swept a claim noted {note!r}"


def test_an_unconfigured_or_unreachable_board_sweeps_nothing(tmp_path):
    """`qb-claimed` exits non-zero and prints nothing when it cannot tell. An empty
    answer read as "no claims" is fine here and only here: this sweep can only ever
    ADD to what it releases, so no answer means nothing is released."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "qb-claimed"
    fake.write_text(f"#!{BASH}\necho 'unknown: no board' >&2\nexit 2\n")
    fake.chmod(0o755)
    got = run_sweep([], tmp_path=tmp_path, qb_claimed=False)
    assert got.branches == []


def test_without_qb_release_nothing_is_even_listed(tmp_path):
    """Reporting an orphan the run cannot act on is a dry-run promise it cannot
    keep, so both halves are resolved before anything is collected."""
    got = run_sweep([checkout_claim("feat/issue-9")], tmp_path=tmp_path,
                    qb_release=False)
    assert got.branches == [] and got.reported == []


# ------------------------------------------------------------- how it is wired

def test_the_release_only_happens_under_prune(tmp_path):
    """Dry-run by default is this script's whole safety posture: it reports, and
    `--prune` is what makes it act."""
    src = SCRIPT.read_text()
    apply_block = src.split("# Drop orphan DBs + prune stale port entries (--prune)", 1)[1]
    apply_block = apply_block.split("# Remove leftover directories", 1)[0]
    assert "ORPHAN_CLAIM_BRANCHES" in apply_block
    assert 'if [[ "$APPLY" = true ]]; then' in apply_block


def test_the_release_names_the_branch_not_a_composed_key(tmp_path):
    """`qb-release` re-derives the key through the board (#172). The sweep collects
    branch names precisely so it never has to compose one of its own — which is
    why the report prints keys and the apply pass uses a second array."""
    src = SCRIPT.read_text()
    assert '"$RELEASE_BIN" --branch "$br" --repo-path "$MAIN_REPO"' in src


def test_orphan_claims_are_counted_in_the_total(tmp_path):
    """`TOTAL` is what decides between "Nothing to prune. Clean." and a report. A
    category left out of it is a category that only ever prints when something
    else was found too."""
    src = SCRIPT.read_text()
    total = src.split("TOTAL=$((", 1)[1].split("))", 1)[0]
    assert "ORPHAN_CLAIMS" in total

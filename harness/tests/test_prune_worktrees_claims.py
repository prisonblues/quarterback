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

**The second risk is a false negative that reads as a clean sweep** (#735). The ask
used to end in `|| true`, so an unreachable board, a rotated token and a payload
`jq` declined all produced the same empty `CLAIMS_JSON` as a board answering
"nothing held" — and `report` prints "none" for an empty array. The run that could
not ask printed the green line and exited 0 while seven claims stayed live. So the
sweep now carries a fourth state, `CLAIMS_UNKNOWN`, and the tests below come in
pairs: one that the failure is reported, and one that a genuinely empty board is
still reported as empty. Only the pair pins it — a sweep that called everything
unknown would pass every test in the first half.

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
              qb_claimed: bool = True, qb_release: bool = True,
              stub: str | None = None):
    """Run the sweep with a stub `qb-claimed` answering with `claims`.

    `stub` replaces that body outright, for the runs whose subject is HOW the tool
    answered rather than WHAT it said — a non-zero exit, an empty stdout, a
    payload that is not JSON. Those are the three ways `|| true` used to produce
    an empty answer indistinguishable from an empty board (#735).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if qb_claimed:
        fake = bindir / "qb-claimed"
        fake.write_text(f"#!{BASH}\n" + (
            stub if stub is not None else
            f"printf '%s' {json.dumps(json.dumps({'claims': claims}))}\n"))
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
        + 'printf "BRANCH|%s\\n" "${ORPHAN_CLAIM_BRANCHES[@]:-}"\n'
        # `:-` so this reads the same against a version of the block that has no
        # such variable: under `set -u` a bare expansion aborts the script, and
        # the red half of a red/green run would then be a bash error rather than
        # the assertion that names the defect.
        + 'printf "UNKNOWN|%s\\n" "${CLAIMS_UNKNOWN:-}"\n')
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
    got.unknown = next((ln.split("|", 1)[1] for ln in got.stdout.splitlines()
                        if ln.startswith("UNKNOWN|")), "")
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
    """`qb-claimed` exits non-zero and prints nothing when it cannot tell, and
    nothing is released off the back of that. Releasing is the half that was
    always safe — this sweep can only ever ADD to what it hands back, so no answer
    means no release. What was not safe is the REPORT; that is the pair below."""
    got = run_sweep([], tmp_path=tmp_path,
                    stub="echo 'unknown: no board' >&2\nexit 2\n")
    assert got.branches == []


# ------------------------------------------- asked and told nothing vs never asked

def test_a_board_that_could_not_be_asked_is_not_reported_as_empty(tmp_path):
    """The defect (#735): `|| true` collapsed a failed ask into an empty answer,
    which `report` prints as a green "none". The sweep now says which it got."""
    got = run_sweep([], tmp_path=tmp_path,
                    stub="echo 'unknown: no board' >&2\nexit 2\n")
    assert got.unknown, "an exit 2 from qb-claimed left the category looking empty"
    assert "2" in got.unknown, (
        "the reason names the exit status, which is the whole of what this run "
        f"learned about why: {got.unknown!r}")


def test_a_board_that_answered_with_nothing_is_not_unknown(tmp_path):
    """The other half, and without it the first is passed by a sweep that calls
    every run unknown. Exit 1 is `qb-claimed`'s "free" — a real answer."""
    got = run_sweep([], tmp_path=tmp_path,
                    stub="printf '%s' '{\"claims\": []}'\nexit 1\n")
    assert got.unknown == "", (
        f"a board that answered 'nothing held' was reported as unaskable: "
        f"{got.unknown!r}")
    assert got.branches == []


def test_a_board_that_answered_is_not_unknown(tmp_path):
    """And an answer WITH claims in it, so the happy path is pinned against the
    same regression from the other side."""
    got = run_sweep([checkout_claim("feat/issue-9")], tmp_path=tmp_path)
    assert got.unknown == ""
    assert got.branches == ["feat/issue-9"]


def test_an_answer_that_is_not_json_is_unknown(tmp_path):
    """A proxy's error page, a truncated read. `jq` declined these behind
    `2>/dev/null` and the empty result read as an empty board."""
    got = run_sweep([], tmp_path=tmp_path,
                    stub="printf '%s' '<html>502 Bad Gateway</html>'\n")
    assert got.unknown, "a payload jq could not parse was reported as no claims"


def test_an_answer_that_is_json_but_not_a_claim_answer_is_unknown(tmp_path):
    """`.claims[]?` accepts anything, and the `?` that makes it safe against a
    missing key is what makes it silent about one — an error body served 200, or
    a different endpoint, yields no rows and reads as an empty board. So the
    shape is asked about rather than assumed."""
    got = run_sweep([], tmp_path=tmp_path,
                    stub="printf '%s' '{\"detail\": \"Not authenticated\"}'\n")
    assert got.unknown, "an answer with no claims list was reported as no claims"


def test_an_answer_with_no_output_at_all_is_unknown(tmp_path):
    """`--json` prints the board's answer on both 0 and 1, so an exit that says
    "I answered" over an empty stdout is something other than this tool."""
    got = run_sweep([], tmp_path=tmp_path, stub="exit 0\n")
    assert got.unknown


def test_without_qb_release_nothing_is_even_listed(tmp_path):
    """Reporting an orphan the run cannot act on is a dry-run promise it cannot
    keep, so both halves are resolved before anything is collected — and the
    category then has no answer, which is not the same as having found none."""
    got = run_sweep([checkout_claim("feat/issue-9")], tmp_path=tmp_path,
                    qb_release=False)
    assert got.branches == [] and got.reported == []
    assert "qb-release" in got.unknown, (
        f"a run that cannot hand anything back reported a clean board: "
        f"{got.unknown!r}")


def test_without_qb_claimed_the_board_was_never_asked(tmp_path):
    """One half of the pair present is a PARTIAL install: `qb-release` being here
    says claims are taken on this host, so an unasked board may hold some."""
    got = run_sweep([], tmp_path=tmp_path, qb_claimed=False)
    assert got.branches == []
    assert "qb-claimed" in got.unknown, (
        f"the board was never asked and the category read as empty: "
        f"{got.unknown!r}")


def test_with_neither_tool_the_category_does_not_apply(tmp_path):
    """And NEITHER half present is not a partial install — it is a host where no
    worktree ever took a board claim, so there is genuinely none to sweep. The
    distinction is what keeps `NOT CHECKED` off every run of a repo that has no
    board, which is the noise the sweep's stderr suppression exists to avoid."""
    got = run_sweep([], tmp_path=tmp_path, qb_claimed=False, qb_release=False)
    assert got.branches == []
    assert got.unknown == "", (
        f"a host with no claim tooling was reported as one that could not be "
        f"asked: {got.unknown!r}")


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


# ================================================================= the report
#
# The block tests above pin the VARIABLE. What #735 was actually reported as is a
# LINE — "✓ Orphan board claims: none" over seven live claims, followed by
# "Nothing to prune. Clean." — so these run the real script end to end and read
# what it printed. A fourth state the report never reaches is not a fix.

BIN = Path(__file__).resolve().parents[1] / "bin"

GIT = shutil.which("git")

#: One stub answering both questions the DB scan asks, so the database category
#: has a real answer and cannot be what suppresses "Clean." in the runs below.
#: `ps` names a container matching the resolver's `postgres|pgdb|_db` filter;
#: `exec ... -tAc` answers the `SELECT 1` probe, then lists one database — the
#: project's own, which is protected and therefore not an orphan.
_DOCKER_STUB = """
if [[ "${1:-}" == "ps" ]]; then printf "%s\\n" "proj-postgres-1"; exit 0; fi
if [[ "${1:-}" == "exec" ]]; then
    for a in "$@"; do
        [[ "$a" == "SELECT 1" ]] && exit 0
        [[ "$a" == *pg_database* ]] && { printf "%s\\n" "proj"; exit 0; }
    done
fi
exit 0
"""

#: `qb-claimed` when the board could not be reached: the documented exit 2, and
#: its reason on stderr where the script drops it.
UNREACHABLE = "echo 'unknown: could not ask the board' >&2\nexit 2\n"

#: `qb-claimed` when the board answered and holds nothing here: exit 1, "free".
ANSWERED_EMPTY = "printf '%s' '{\"claims\": [], \"held\": false}'\nexit 1\n"


def real_run(tmp_path, *, claimed: str, args: tuple[str, ...] = (),
             ports: str | None = None):
    """`prune-worktrees` itself, in a throwaway repo, with `qb-claimed` stubbed."""
    main = tmp_path / "proj"
    main.mkdir()
    subprocess.run([GIT, "init", "-q", "-b", "main", str(main)], check=True)
    subprocess.run([GIT, "-C", str(main), "config", "user.email", "t@example.com"],
                   check=True)
    subprocess.run([GIT, "-C", str(main), "config", "user.name", "T"], check=True)
    (main / ".worktree.json").write_text(json.dumps(
        {"project": "proj", "database": {"engine": "postgresql"}}))
    (main / ".env").write_text("POSTGRES_USER=proj\n")
    if ports is not None:
        (main / ".worktree-ports").write_text(ports)
    (main / "f").write_text("x\n")
    subprocess.run([GIT, "-C", str(main), "add", "-A"], check=True)
    subprocess.run([GIT, "-C", str(main), "commit", "-qm", "init"], check=True)

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (("docker", _DOCKER_STUB), ("qb-claimed", claimed),
                       ("qb-release", "exit 0\n")):
        f = stubs / name
        f.write_text(f"#!{BASH}\n{body}")
        f.chmod(0o755)

    # `inherit_path` because the subject is the real script: `git` and `tr` have
    # to be findable. The stub directory goes in FRONT, so the three tools this
    # test has an opinion about are the ones it wrote — including `docker`, which
    # must not be the developer's.
    return subprocess.run(
        [str(BIN / "prune-worktrees"), *args], cwd=str(main),
        capture_output=True, text=True,
        env=_path_sandbox.sandbox_env(tmp_path, stubs, inherit_path=True))


@pytest.mark.skipif(GIT is None, reason="git must be on PATH")
def test_the_report_says_not_checked_rather_than_none(tmp_path):
    """The line the issue was filed about."""
    r = real_run(tmp_path, claimed=UNREACHABLE)
    assert "Orphan board claims: none" not in r.stdout, (
        "a board that could not be asked was reported as a board with nothing "
        f"on it:\n{r.stdout}")
    assert "Orphan board claims: NOT CHECKED" in r.stdout, r.stdout


@pytest.mark.skipif(GIT is None, reason="git must be on PATH")
def test_a_run_that_could_not_ask_is_not_reported_as_clean(tmp_path):
    """"Nothing to prune. Clean." is a statement about all six categories, and it
    is the sentence a caller quotes back as "the sweep ran and found nothing"."""
    r = real_run(tmp_path, claimed=UNREACHABLE)
    assert "Nothing to prune. Clean." not in r.stdout, r.stdout
    assert "unchecked" in r.stdout, r.stdout


@pytest.mark.skipif(GIT is None, reason="git must be on PATH")
def test_a_board_that_answers_still_reports_none_and_clean(tmp_path):
    """The half that keeps the fix honest: a sweep that reported every run as
    unchecked would pass both tests above and be no more useful than the bug."""
    r = real_run(tmp_path, claimed=ANSWERED_EMPTY)
    assert "Orphan board claims: none" in r.stdout, r.stdout
    assert "NOT CHECKED" not in r.stdout, (
        f"nothing here went unanswered, including the DB scan:\n{r.stdout}")
    assert "Nothing to prune. Clean." in r.stdout, r.stdout


@pytest.mark.skipif(GIT is None, reason="git must be on PATH")
def test_prune_says_outright_that_it_swept_no_claims(tmp_path):
    """`--prune` reports an action per line, so saying nothing about claims reads
    as claims handed back. The stale port entry is there to carry the run past
    the "nothing to prune" exit and into the apply pass."""
    r = real_run(tmp_path, claimed=UNREACHABLE, args=("--prune",),
                 ports="5001:fix-issue-gone\n")
    assert "Applying" in r.stdout, r.stdout
    assert "claims NOT swept" in r.stdout, r.stdout
    assert "released claim" not in r.stdout, r.stdout

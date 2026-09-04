"""What `qb-hook` calls the repo it is standing in (#714).

The lease and every post the hook writes carry a `repo`, and until #714 it was the
checkout **basename** — `lexray`, never `prisonblues/lexray`. That is the one
spelling `app/claimkey.py` refuses everywhere a repo is a key, so the board held
the fleet's own heartbeat under a name its keyed surfaces could not be asked
about: `active(repo="prisonblues/lexray")` answered an empty board while three
agents worked the repo, having been taught that exact spelling by `plan_read` one
call earlier.

The reads fold both shapes now (`app/repomatch.py`), so this is not what fixes
that observation. It is what stops the heartbeat being the thing that needs
folding — and it closes the second half of the same report, which nobody had
diagnosed correctly:

**A checkout whose repo nobody else can name reports NO repo.** The hook used to
fall back to the directory basename, and the panel's per-seat sandbox is a `git
init` in a temporary directory (named `cwd` at the time), so the board carried
live agents in a repository called `cwd` on a branch called `master` — read,
reasonably, as an unexpanded shell variable. It was neither: both values were true
about a throwaway repo nobody else could name. A repo name a peer can ask about
and be answered about is worse than none.

#714 suppressed the fallback for every checkout with no origin remote, and #721
narrows that to the checkouts it is actually true of. "No origin" is also an
ordinary local-only repository, which then wrote `repo: NULL` on every lease and
went invisible to a repo-scoped `/active` — the same false clean, through the
other half of the same field. The hook cannot tell the two apart (a fresh `git
init` and a never-pushed repo of ten years' commits differ in nothing it can see),
so the process that MADE the throwaway one says so: `panel_seats.run_cli` exports
`QB_SANDBOX=1` around every seat CLI, and the hook is that CLI's child. Both
halves are pinned below — a declared sandbox stays silent, an originless checkout
keeps its bare name.

These drive the real script as a subprocess against a stub board, for the reason
`test_qb_hook_end.py` gives: asserting on the text of the file cannot tell a
branch that is present from one that runs. The checkout has to be real, because
what is under test is what the hook asks git.

Run: pytest harness/tests
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402

BIN = Path(__file__).resolve().parents[1] / "bin"
HOOK = BIN / "qb-hook"
CLASSIFY = BIN / "qb-classify-command"

#: The same named-binaries list the other two hook suites keep, and kept for the
#: same reason: symlinked one at a time, so a `qb-*` this hook can reach is
#: reachable only when a test put it there.
HOOK_TOOLS = ("jq", "curl", "git", "timeout", "sed", "grep", "sort", "tr", "cat",
              "date", "stat", "basename", "dirname", "cut", "sha256sum", "bash",
              "sh", "mktemp", "tail", "head", "python3", "rm", "printf", "env",
              "uname", "wc", "awk", "id", "readlink", "paste")

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None
    or shutil.which("bash") is None
    or shutil.which("git") is None,
    reason="qb-hook is bash, parses its payload with jq, and this asks git",
)


class Reporting:
    """`qb-hook` over a real checkout, with a stub board recording what it sent."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.bin = tmp_path / "hookbin"
        self.bin.mkdir()
        for tool in (HOOK, CLASSIFY):
            (self.bin / tool.name).write_bytes(tool.read_bytes())
            (self.bin / tool.name).chmod(0o755)
        # `qb-env` is found BESIDE the script, so a copy in a directory of our own
        # is the seam that decides which board the hook thinks it has.
        (self.bin / "qb-env").write_text(
            "qb_load_config() {\n"
            "  QUARTERBACK_BASE_URL=http://board.test\n"
            "  QUARTERBACK_AGENT=testbox\n"
            "}\n"
            "qb_resolve_token() { QUARTERBACK_TOKEN=tok-test; return 0; }\n"
        )

        self.stub = tmp_path / "stub"
        self.stub.mkdir()
        self.calls = tmp_path / "curl.log"
        # One line per call, whitespace-joined: every assertion here is a substring
        # test on a URL or a JSON body, and the hook puts a newline in neither. The
        # empty board is what `/active` answers, so no occupancy note is composed
        # and nothing but the reporting under test is in the way.
        (self.stub / "curl").write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> {self.calls}\n'
            'case "$*" in\n'
            '  *"/active"*) printf \'{"agents":[],"subagents":[]}\' ;;\n'
            'esac\n'
            "exit 0\n"
        )
        (self.stub / "curl").chmod(0o755)
        # Silent, and stubbed here rather than by the tests that care: without it,
        # whether a note carries #178's mode line is decided by whether the host
        # running the suite has `qb-mode` installed (#473).
        (self.stub / "qb-mode").write_text('#!/bin/sh\nprintf \'{"label":null}\'\n')
        (self.stub / "qb-mode").chmod(0o755)

        self.cwd = tmp_path / "checkout"
        self.cwd.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        (self.cwd / "app.py").write_text("seed\n")
        self._git("add", "app.py")
        self._git("commit", "-qm", "seed")
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()

    def _git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.cwd), *args], check=True,
                       capture_output=True)

    def origin(self, url: str) -> None:
        self._git("remote", "add", "origin", url)

    def worktree(self, name: str) -> Path:
        """A linked worktree of the checkout, at a directory of its own name.

        The shape `create-worktree` produces — `<repo>-<branch>` beside the repo —
        and the shape #721's scenario is written in: one local-only repository, two
        agents, two worktrees. What they report has to be the same string or the
        collision index holds two repositories where there is one.
        """
        where = self.root / name
        self._git("worktree", "add", "-q", "-b", name, str(where))
        return where

    def env(self) -> dict:
        env = _path_sandbox.sandbox_env(self.root, self.stub, tools=HOOK_TOOLS,
                                        XDG_RUNTIME_DIR=str(self.run_dir))
        for pane in ("TMUX", "TMUX_PANE"):
            env.pop(pane, None)
        return env

    def start(self, cwd: Path | None = None, **extra_env: str) -> None:
        """One `SessionStart` from `cwd` (the checkout by default), with `extra_env`
        merged over the sandboxed environment.

        `extra_env` is how a test says which KIND of checkout this is: a panel seat
        exports `QB_SANDBOX=1` from `panel_seats.run_cli`, and the hook is a child
        of the seat CLI, so a variable is the whole of the channel between them
        (#721). Merged over rather than passed separately so the sandboxed PATH the
        rest of this class builds still decides which binaries the hook can reach.
        """
        body = {"session_id": "sid-714", "cwd": str(cwd or self.cwd),
                "transcript_path": ""}
        got = subprocess.run([str(self.bin / "qb-hook"), "SessionStart"],
                             input=json.dumps(body), capture_output=True, text=True,
                             env={**self.env(), **extra_env}, timeout=60)
        assert got.returncode == 0, got.stderr  # fail-open, by contract

    def leases(self) -> list[str]:
        """Every `POST /lease` body the hook has sent, in the order it sent them.

        Asserted non-empty rather than defaulted to `[]`: a rig that stopped
        registering a lease would make every test here pass by having nothing to
        disagree with.
        """
        sent = self.calls.read_text().splitlines() if self.calls.exists() else []
        leases = [c for c in sent if "http://board.test/lease" in c]
        assert leases, f"the hook registered no lease at all: {sent}"
        return leases

    def lease(self) -> str:
        """The FIRST lease body — what a test that starts one session is asking for."""
        return self.leases()[0]

    @staticmethod
    def reported_repo(body: str) -> str | None:
        """The `repo` a lease body carries, or None where it carries none.

        None and `""` are different answers and the caller has to be able to tell
        them apart: an omitted field is the sandbox reporting nothing, which is the
        thing #714 wanted, while an empty string would be the hook reporting a name
        it failed to derive. Only the first can legitimately happen, so a test that
        conflated them would keep passing through the second.
        """
        found = re.search(r'"repo":"([^"]*)"', body)
        return found.group(1) if found else None


@pytest.fixture
def hook(tmp_path):
    return Reporting(tmp_path)


# ------------------------------------------------------- a GitHub remote: owner/name


@pytest.mark.parametrize("url", [
    "git@github.com:prisonblues/quarterback.git",
    "https://github.com/prisonblues/quarterback.git",
    "https://github.com/prisonblues/quarterback",
    "ssh://git@github.com/prisonblues/quarterback.git",
    # An explicit port. Found by an independent review of this change: the pattern
    # anchors on the host, and a port between the host and the path made the whole
    # remote fall through to the bare-name fallback — silently, because the fallback
    # is a legitimate answer for a non-GitHub remote.
    "ssh://git@github.com:22/prisonblues/quarterback.git",
    "https://github.com:443/prisonblues/quarterback.git",
])
def test_the_lease_reports_the_origin_slug_in_every_remote_spelling(hook, url):
    """`owner/name`, from the remote the hook was already reading. Every spelling
    git accepts for one repository has to answer the same, or the board holds one
    repo under as many names as the fleet has ways of cloning it."""
    hook.origin(url)
    hook.start()
    body = hook.lease()
    # The checkout on disk is named `checkout`, so this also pins that the repo
    # comes from the remote rather than the directory — a clone under a different
    # local name is still the same repo to everyone else, which is why the remote
    # was already the source and why keeping only its basename threw away the half
    # that made it an identity.
    assert '"repo":"prisonblues/quarterback"' in body.replace(" ", ""), body


def test_the_slug_does_not_keep_the_dot_git_suffix(hook):
    """`sed -E` is POSIX ERE with no lazy quantifier, so a name class that can match
    `.git` swallows the suffix the optional group was meant to take — the first cut
    of this reported `prisonblues/quarterback.git`, which `canonical_repo` refuses
    for exactly the reason it is stripped: GitHub allows no repository name ending
    in `.git`, so that spelling can only ever be a clone URL's."""
    hook.origin("git@github.com:prisonblues/quarterback.git")
    hook.start()
    assert ".git" not in hook.lease().split('"repo":')[1][:60]


@pytest.mark.parametrize("url", [
    "https://GitHub.com/prisonblues/quarterback.git",
    "HTTPS://github.com/prisonblues/quarterback.git",
    "git@GITHUB.COM:prisonblues/quarterback.git",
    "ssh://git@GitHub.Com:22/prisonblues/quarterback.git",
])
def test_the_scheme_and_host_are_matched_whatever_their_case(hook, url):
    """A URI scheme and a DNS hostname are case-insensitive by their own specs (RFC
    3986, RFC 4343) and git clones `https://GitHub.com/...` happily, so a
    lowercase-only pattern reads a GitHub remote as a foreign one. It fails the way
    this whole change is about: not with an error but with the bare name `quarterback`
    — a legitimate answer for a non-GitHub remote, and therefore one nothing
    downstream can tell from the real thing. Found by an independent review."""
    hook.origin(url)
    hook.start()
    assert '"repo":"prisonblues/quarterback"' in hook.lease().replace(" ", "")


def test_the_owner_and_name_keep_the_case_they_were_cloned_under(hook):
    """Only the scheme and the host are folded here. GitHub preserves repository-name
    case, the hook is not the thing that gets to decide it, and the board folds a
    qualified slug on the way in (`app.repomatch.fold_repo`) — so folding it twice,
    in two languages, is two rules that can come to disagree."""
    hook.origin("https://GitHub.com/PrisonBlues/Quarterback.git")
    hook.start()
    assert '"repo":"PrisonBlues/Quarterback"' in hook.lease().replace(" ", "")


# ------------------------------------------- no origin: whose directory is it? (#721)


def test_a_declared_sandbox_reports_no_repo_at_all(hook):
    """The panel seat sandbox, and the whole of #714's second report. A `git init`
    in a temp directory has no origin; the basename fallback made it a repository
    called `cwd` that a peer could ask `/active` about and be answered about.

    `QB_SANDBOX` is what `panel_seats.run_cli` exports around every seat CLI, and
    the hook runs as that CLI's child. Since #721 it is the DECLARATION that
    suppresses the name rather than the missing origin, so this test now says so
    the way a seat does."""
    hook.start(QB_SANDBOX="1")
    body = hook.lease().replace(" ", "")
    assert '"repo":' not in body, (
        f"a throwaway repo nobody can name was reported as one: {body}"
    )


def test_no_repo_does_not_stop_the_lease_being_registered(hook):
    """Reporting nothing is not reporting nowhere. The field is optional and the
    session is still an agent on the board — the sandbox seats were visible before
    this and must stay visible after it, minus the invented repo."""
    hook.start(QB_SANDBOX="1")
    assert '"session":"sid-714"' in hook.lease().replace(" ", "")


def test_an_originless_checkout_keeps_its_bare_name(hook):
    """The cost #714 paid and #721 takes back. "No origin" is not only the seat
    sandbox: an ordinary local-only repository — never pushed anywhere, two agents,
    two worktrees — wrote `repo: NULL` on both leases, and a repo-scoped read cannot
    match NULL. So `active(repo="checkout")` answered a clean board while two agents
    were live in it: the same false clean #714 exists to abolish, arriving through
    the other half of the same field.

    The checkout on disk here is named `checkout`, and that name is genuinely all it
    has. The board's reads accept it (`app.repomatch.name_clause`), which is the same
    ground on which a non-GitHub remote's bare name is kept two tests below.

    This is also the RELATIVE half of the path resolution: `git rev-parse
    --git-common-dir` answers `.git` from a main worktree and an absolute path from
    a linked one, and the hook resolves both with `cd` + `pwd` rather than asking
    for `--path-format=absolute`, which git only learned in 2.31. Both of git's
    answers are covered by real git — this test for the relative one, the
    two-worktree test below for the absolute — so there is no version-dependent
    branch left to stub a `git` for."""
    hook.start()
    assert '"repo":"checkout"' in hook.lease().replace(" ", ""), hook.lease()


def test_two_worktrees_of_an_originless_repo_report_the_SAME_name(hook):
    """The scenario #721 is written from, and the half a worktree basename fails.

    `create-worktree` names a worktree `<repo>-<branch>`, so two agents in two
    worktrees of one local-only repository would report `checkout-wt-a` and
    `checkout-wt-b` — two repositories on the collision index where there is one,
    each invisible to a question asked under the other's name. That is the same
    false clean the whole issue is about, one level in: reporting *a* name is not
    the fix, reporting the name the other agent would use is.

    So TWO worktrees, and the assertion is that their two reports are equal to each
    other. Comparing one of them against the literal `checkout` would pin the same
    behaviour today and stop being this test tomorrow: the property is agreement
    between the peers, and a rule that changed what both of them say — resolving
    symlinks, say — would break a literal while leaving the collision index
    perfectly correct. The two guards under it are what equality alone cannot say:
    that neither reported nothing (which is #714's regression, and two Nones are
    equal), and that neither reported its own worktree's name (which is the defect,
    and would only ever be caught by the first assertion if both worktrees happened
    to be named the same thing)."""
    a = hook.worktree("checkout-wt-a")
    b = hook.worktree("checkout-wt-b")
    hook.start(cwd=a)
    hook.start(cwd=b)
    posted = [hook.reported_repo(body) for body in hook.leases()[:2]]
    assert len(posted) == 2, f"expected a lease from each worktree: {hook.leases()}"
    assert all(posted), f"a worktree of a real repository reported no repo: {posted}"
    assert posted[0] == posted[1], (
        f"two worktrees of ONE repository named it two things: {posted}")
    assert not set(posted) & {"checkout-wt-a", "checkout-wt-b"}, (
        f"the repo was named after a worktree rather than the repository: {posted}")


def test_an_exported_CDPATH_cannot_redirect_the_repo_name(hook):
    """The hook resolves `--git-common-dir` with `cd`, and from a main checkout that
    answer is the BARE relative `.git`. A `cd` operand not beginning with `/`, `./`
    or `..` is searched for on CDPATH first, so an agent whose shell profile exports
    CDPATH with any entry holding a `.git` directory resolves somebody else's
    repository — and `cd` echoes the directory when CDPATH is what found it, so the
    command substitution captures the wrong path TWICE.

    That is worse than the bug this whole branch is about. #714's failure was a name
    nobody could resolve; this one is a name that resolves to a real repository the
    session is not in, and a peer asking `/active` about that repo is answered with
    an agent who was never there. Neither empty nor obviously wrong is the hardest
    kind to notice.

    The decoy is a directory containing nothing but `.git`, which is all CDPATH
    resolution needs — it never looks inside. Asserting the exact name catches both
    halves: the misresolution puts the decoy's name here, and the doubled `cd` echo
    corrupts the value even when CDPATH happens to resolve to the right place."""
    decoy = hook.root / "decoy"
    (decoy / ".git").mkdir(parents=True)
    hook.start(CDPATH=str(decoy))
    assert hook.reported_repo(hook.lease()) == "checkout", hook.lease()


def test_the_sandbox_flag_only_governs_the_fallback_not_a_real_remote(hook):
    """`QB_SANDBOX` says "the directory I gave you is a throwaway", not "publish
    nothing". A seat that somehow ran in a checkout with a real origin has a repo
    identity that came from the remote rather than from the path, and suppressing
    that would be the flag deciding a question it was not asked. It also keeps the
    flag from being a way to blind an agent on the board by exporting one variable."""
    hook.origin("git@github.com:prisonblues/quarterback.git")
    hook.start(QB_SANDBOX="1")
    assert '"repo":"prisonblues/quarterback"' in hook.lease().replace(" ", "")


# ---------------------------------------- a non-GitHub remote: the bare name remains


def test_a_non_github_remote_still_reports_its_bare_name(hook):
    """The bare name is genuinely all such a checkout has, and the board's reads
    accept it (`app.repomatch.name_clause`). Only the *identity* is unavailable,
    not the grouping — going silent here would take a real checkout off the
    collision index to buy nothing."""
    hook.origin("git@gitlab.example.com:group/thing.git")
    hook.start()
    assert '"repo":"thing"' in hook.lease().replace(" ", "")


def test_an_all_digit_owner_is_not_mistaken_for_a_port(hook):
    """The other half of accepting a port. `github.com:123/repo` is scp-style with
    an owner that happens to be digits, and a port group that could eat the `:` here
    would report `repo` — a bare name — for a remote that names a repository. ERE
    backtracks, so the only parse whose tail is an `owner/name` is the one where the
    port group matched nothing; this is the test that says so rather than the
    comment."""
    hook.origin("git@github.com:123/repo.git")
    hook.start()
    assert '"repo":"123/repo"' in hook.lease().replace(" ", "")


def test_a_path_remote_is_not_read_as_owner_slash_name(hook):
    """`/home/rich/source/quarterback` would become `rich/source` under a pattern
    that took the last two segments of anything — a repo identity invented from a
    directory layout, which is the open-domain guessing PR #152 was closed for. The
    slug pattern is anchored on the host, so a path falls to the bare name."""
    other = hook.root / "elsewhere" / "source" / "quarterback"
    other.mkdir(parents=True)
    hook.origin(str(other))
    hook.start()
    assert '"repo":"quarterback"' in hook.lease().replace(" ", "")

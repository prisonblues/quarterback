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

**A checkout with no origin remote now reports NO repo.** It used to fall back to
the directory basename, and the panel's per-seat sandbox is a `git init` in a
temporary directory (named `cwd` at the time), so the board carried live agents in
a repository called `cwd` on a branch called `master` — read, reasonably, as an
unexpanded shell variable. It was neither: both values were true about a throwaway
repo nobody else could name. A repo name a peer can ask about and be answered
about is worse than none.

These drive the real script as a subprocess against a stub board, for the reason
`test_qb_hook_end.py` gives: asserting on the text of the file cannot tell a
branch that is present from one that runs. The checkout has to be real, because
what is under test is what the hook asks git.

Run: pytest harness/tests
"""

from __future__ import annotations

import json
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

    def env(self) -> dict:
        env = _path_sandbox.sandbox_env(self.root, self.stub, tools=HOOK_TOOLS,
                                        XDG_RUNTIME_DIR=str(self.run_dir))
        for pane in ("TMUX", "TMUX_PANE"):
            env.pop(pane, None)
        return env

    def start(self) -> None:
        body = {"session_id": "sid-714", "cwd": str(self.cwd), "transcript_path": ""}
        got = subprocess.run([str(self.bin / "qb-hook"), "SessionStart"],
                             input=json.dumps(body), capture_output=True, text=True,
                             env=self.env(), timeout=60)
        assert got.returncode == 0, got.stderr  # fail-open, by contract

    def lease(self) -> str:
        """The body of the `POST /lease` the hook sent — the datum under test.

        Asserted to exist rather than defaulted to empty: a rig that stopped
        registering a lease would make every test here pass by having nothing to
        disagree with.
        """
        sent = self.calls.read_text().splitlines() if self.calls.exists() else []
        leases = [c for c in sent if "http://board.test/lease" in c]
        assert leases, f"the hook registered no lease at all: {sent}"
        return leases[0]


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


# --------------------------------------------------- no origin: no repo, not a guess


def test_a_checkout_with_no_origin_reports_no_repo_at_all(hook):
    """The panel seat sandbox, and the whole of #714's second report. A `git init`
    in a temp directory has no origin; the basename fallback made it a repository
    called `cwd` that a peer could ask `/active` about and be answered about."""
    hook.start()
    body = hook.lease().replace(" ", "")
    assert '"repo":' not in body, (
        f"a throwaway repo nobody can name was reported as one: {body}"
    )


def test_no_repo_does_not_stop_the_lease_being_registered(hook):
    """Reporting nothing is not reporting nowhere. The field is optional and the
    session is still an agent on the board — the sandbox seats were visible before
    this and must stay visible after it, minus the invented repo."""
    hook.start()
    assert '"session":"sid-714"' in hook.lease().replace(" ", "")


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

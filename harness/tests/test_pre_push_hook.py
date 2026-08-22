"""The pre-push guard, driven through real `git push` against a real remote.

Nothing here asserts about a message by reading the script. Every test builds a throwaway
repo with a bare remote, installs the hook the way `qb-hooks` installs it, and then pushes —
because the two things that can go wrong with a hook are both invisible to a unit test: it
can fail to be *installed* under the name git looks for, and it can fail to *run* because
`core.hooksPath` replaced the directory it lives in.

The scenarios are the ones from #343, stated as the failures they are:

  * `test_a_two_head_graph_on_a_protected_push_is_refused` — four branches each minted
    migration `0029` on 2026-08-22, each ran `preflight`, and each got a truthful answer.
    It reached CI as "Multiple head revisions are present". This is the push that should
    have stopped.
  * `test_a_branch_that_stamped_a_number_the_base_has_taken_is_refused` — the #287 case: a
    branch took v2.60 while it sat, main took v2.60 meanwhile, and the collision hit four
    files. The `stamped` CI job cannot ask this question, because it runs on main only.
  * `test_a_branch_that_is_merely_behind_is_not_refused` — the reason the check above is
    fork-relative and not base-relative. A guard that fires on every branch that forked a
    while ago is a guard that gets switched off, and this is the test that says so.
  * `test_a_branch_that_rewrote_a_shipped_release_entry_is_refused` — the #325 case, and
    `test_the_number_check_passes_the_very_push_the_body_check_refuses` is the argument for
    it being a separate question. A conflict resolved by moving a body under an existing
    heading deletes a shipped release's notes while leaving every heading present, unique and
    in order, so a check reading the file as a list of numbers reports it clean.
  * `test_a_duplicate_revision_id_is_not_reported_as_a_head_count` and its partner
    `test_a_two_head_refusal_says_the_heads_were_counted` — #357. The reconciler's exit 2
    is two answers, and the renumber remedy is only right for one of them. A pair, because
    the property is which message goes with which answer, and either test alone passes
    against a hook that prints one message for both.
  * `test_a_repo_with_no_migrations_pushes_silently` — this hook ships with the harness into
    repos that are neither quarterback nor lexray. Not merely "does not refuse": prints
    NOTHING, because a warning nobody in that repo can act on is how it gets uninstalled.

Run: pytest harness/tests/test_pre_push_hook.py
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# A sibling module, imported by bare name the way #264's own members import it: the sandboxes
# that run these suites put `harness/tests` on the path by running pytest from `harness/`, and
# a developer running `pytest harness/tests` from the repo root does not. One entry, both work.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Imported hard, not through `importorskip`. A skip here would be indistinguishable from a
# pass, and the coupling guard at the bottom of this file would be inert in exactly the
# sandbox it exists to protect.
import _flake_sandbox  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "harness" / "bin"
HOOKS = ROOT / "harness" / "githooks"
QB_HOOKS = BIN / "qb-hooks"
SCRIPTS = ROOT / "scripts"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

CHANGELOG_V1 = """# Version history

Entries are newest first.

## v1 — the first one

It did a thing.
"""

CHANGELOG_V2 = CHANGELOG_V1.replace(
    "## v1 — the first one", "## v2 — the second one\n\nAnd another thing.\n\n## v1 — the first one"
)

MIGRATION = '"""a migration"""\n\nrevision = "{rev}"\ndown_revision = {down}\n'


def env(home: Path, **extra) -> dict:
    """A real PATH — git is not at /usr/bin/git on every host — with the global and system
    configs pointed at a dir the test owns, so the delegate `qb-hooks` resolves is the
    test's and not the machine's gitleaks hook."""
    e = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_CONFIG_SYSTEM": str(home / ".gitconfig-system"),
    }
    e.update(extra)
    return e


def git(repo: Path, *args, home: Path, check=True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env(home, **kw),
        check=check,
    )


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    (h / ".gitconfig").write_text("")
    (h / ".gitconfig-system").write_text("")
    return h


def install(repo: Path, home: Path) -> subprocess.CompletedProcess:
    r = subprocess.run(
        [str(QB_HOOKS), "install", "--repo", str(repo)], capture_output=True, text=True,
        env=env(home),
    )
    assert r.returncode == 0, r.stderr
    return r


def write_migration(repo: Path, rev: str, down: str | None, filename: str | None = None) -> None:
    """`filename` is what makes a DUPLICATE REVISION ID expressible: two files at one ref
    declaring one `revision`. Alembic reads the assignment and not the name, so the pair
    collides in the graph while colliding on nothing in git — the case #338 is about, and
    the one the reconciler declines to answer rather than counting heads on."""
    versions = repo / "migrations" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    down_literal = "None" if down is None else f'"{down}"'
    name = filename or f"{rev}_m.py"
    (versions / name).write_text(MIGRATION.format(rev=rev, down=down_literal))


def commit(repo: Path, message: str, home: Path) -> str:
    git(repo, "add", "-A", home=home)
    git(repo, "commit", "-qm", message, home=home)
    return git(repo, "rev-parse", "HEAD", home=home).stdout.strip()


def init(repo: Path, home: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main", home=home)
    git(repo, "config", "user.email", "t@example.com", home=home)
    git(repo, "config", "user.name", "Test", home=home)


def bare_remote(tmp_path: Path, home: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(bare)],
        env=env(home), check=True, capture_output=True, text=True,
    )
    return bare


def ship_tools(repo: Path, *names: str) -> None:
    """Copy the real tools in, not stubs. The hook's whole contract is that it delegates the
    two graph/number questions to the repo's own reconciler and stamper, so a stub would
    verify the hook against a thing nobody runs."""
    (repo / "scripts").mkdir(exist_ok=True)
    for name in names:
        shutil.copy(SCRIPTS / name, repo / "scripts" / name)


@pytest.fixture
def repo(tmp_path, home):
    """A repo with migrations, a CHANGELOG, both tools, the guard installed, and one commit
    already on the remote — so `refs/remotes/origin/main` exists and the release check has a
    base to be fork-relative against."""
    bare = bare_remote(tmp_path, home)
    work = tmp_path / "work"
    init(work, home)
    write_migration(work, "0001", None)
    write_migration(work, "0002", "0001")
    (work / "CHANGELOG.md").write_text(CHANGELOG_V1)
    ship_tools(work, "migration_reconcile.py", "release_stamp.py", "release_tag.py")
    commit(work, "initial", home)
    git(work, "remote", "add", "origin", str(bare), home=home)
    install(work, home)
    push = git(work, "push", "-q", "-u", "origin", "main", home=home, check=False)
    assert push.returncode == 0, push.stderr
    return work


def push(repo: Path, home: Path, *args) -> subprocess.CompletedProcess:
    return git(repo, "push", "origin", *args, home=home, check=False)


def remote_sha(repo: Path, branch: str, home: Path) -> str:
    out = git(repo, "ls-remote", "origin", f"refs/heads/{branch}", home=home).stdout
    return out.split()[0] if out.strip() else ""


# ---------------------------------------------------------------------------
# 1. the migration graph
# ---------------------------------------------------------------------------


def test_a_two_head_graph_on_a_protected_push_is_refused(repo, home):
    """The push from 2026-08-22 that nobody could have known to hold back. `0003` hangs off
    `0001` beside `0002`, which is what two branches numbering in parallel produces once
    both are on one ref, and `alembic upgrade head` refuses to load it."""
    before = remote_sha(repo, "main", home)
    write_migration(repo, "0003", "0001")
    commit(repo, "a second head", home)

    r = push(repo, home, "main")

    assert r.returncode != 0
    assert "REFUSE" in r.stderr
    assert "0002" in r.stderr and "0003" in r.stderr, "the refusal must name both heads"
    assert "migration_reconcile.py" in r.stderr, "and point at the reconciler"
    assert remote_sha(repo, "main", home) == before, "the remote moved anyway"


def test_a_two_head_refusal_says_the_heads_were_counted(repo, home):
    """The remedy the refusal prints — renumber, or add a merge revision — is only right
    when a head count exists, so the message that carries it has to be the one that says a
    count was made. It used to hedge across both of the reconciler's exit-2 answers in a
    single line ("not exactly one head, a cycle, or a duplicate revision id"), which is
    correct advice here and wrong advice in the test below (#357)."""
    write_migration(repo, "0003", "0001")
    commit(repo, "a second head", home)

    r = push(repo, home, "main")

    assert r.returncode != 0
    assert "counted the heads" in r.stderr, (
        f"the refusal does not say a count was made, which is what makes the renumber "
        f"remedy beneath it correct:\n{r.stderr}"
    )
    assert "0002" in r.stderr and "0003" in r.stderr
    assert "preflight" in r.stderr and "apply" in r.stderr


def test_a_duplicate_revision_id_is_not_reported_as_a_head_count(repo, home):
    """The refusal that used to send a reader off to renumber a graph that was fine.

    Two files at `0003` is not a graph the reconciler will build, so it never counts the
    heads — and the graph underneath may be single-headed, as this one is. Told "not exactly
    one head", the reader renumbers; the actual problem is the pair of files, and the
    reconciler said so in the output the hook was already printing. Same split, same
    reasoning and largely the same words as the `migration-heads` CI job (#351 / PR #355).
    """
    write_migration(repo, "0003", "0002", filename="0003_mine.py")
    write_migration(repo, "0003", "0002", filename="0003_theirs.py")
    commit(repo, "two files at 0003", home)

    r = push(repo, home, "main")

    assert r.returncode != 0, "a graph the reconciler refuses to build is not a passing push"
    assert "REFUSE" in r.stderr
    assert "never counted the heads" in r.stderr, (
        f"the refusal does not say the count is missing:\n{r.stderr}"
    )
    assert "not exactly one" not in r.stderr, (
        "the reconciler never counted the heads here, so the hook must not report a head "
        f"count it was not given:\n{r.stderr}"
    )
    assert "counted the heads" not in r.stderr.replace("never counted the heads", "")
    assert "0003" in r.stderr and "more than one file" in r.stderr, (
        f"the refusal does not carry the reconciler's own diagnosis:\n{r.stderr}"
    )
    assert "duplicate revision id" in r.stderr, "and it should name the usual cause"


def test_a_decline_too_large_for_a_pipe_buffer_is_still_read_as_a_decline(repo, home, tmp_path):
    """The size threshold the obvious implementation of the split has, and the reason it is
    not written as `printf … | grep -q`.

    `grep -q` exits at the first match. Feed it more than a pipe buffer's worth and the
    writer is still going when the pipe closes, dies of SIGPIPE, and `pipefail` gives the
    whole pipeline 141 — failure, on precisely the input that matched. The decline would
    then be reported as a head count again, for large diagnostics only, which is the worst
    size for a bug to be. `chain_delegate` at the top of the hook was bitten by the same
    thing and says so; this is that hazard asserted rather than described.
    """
    huge = tmp_path / "loud-reconciler.py"
    # No shebang, for the reason the broken-tool stubs below give: the hook runs these
    # through the interpreter it resolved, never by exec'ing the file.
    # `STOP: ` on the FIRST line and bulk after it, which is the shape that triggers this
    # and the shape a chatty reconciler has: grep matches immediately and stops reading,
    # while the writer still has most of the payload to push. A single enormous STOP line
    # would not trigger it — grep has to buffer to the newline before it can match, by which
    # time the writer is done — which is exactly why this is worth pinning rather than
    # reasoning about. `qb.migrationReconcile` accepts any tool, so this is reachable.
    huge.write_text(
        "import sys\n"
        "print('STOP: two files declare one revision id', file=sys.stderr)\n"
        "for i in range(20000):\n"
        "    print(f'  considered migrations/versions/{i:05d}_something.py', file=sys.stderr)\n"
        "sys.exit(2)\n"
    )
    (repo / "notes.txt").write_text("x\n")
    commit(repo, "something", home)

    r = git(repo, "push", "origin", "main", home=home, check=False,
            QB_MIGRATION_RECONCILE=str(huge))

    assert r.returncode != 0
    assert "never counted the heads" in r.stderr, (
        "a STOP: larger than a pipe buffer is still a decline, and reporting it as a head "
        f"count is the bug this whole change is about:\n{r.stderr[:2000]}"
    )
    assert "not exactly one" not in r.stderr


def test_a_single_head_graph_on_a_protected_push_is_allowed(repo, home):
    write_migration(repo, "0003", "0002")
    commit(repo, "a third migration, linked", home)

    r = push(repo, home, "main")

    assert r.returncode == 0, r.stderr
    assert "single migration head (0003)" in r.stdout + r.stderr


def test_the_graph_is_read_at_the_pushed_commit_not_the_working_tree(repo, home):
    """A push carrying a fork is refused even from a checkout that does not have it. The
    working tree here is a different branch with a perfectly clean graph; `main` is the ref
    on its way to the remote, and `main` is what gets judged."""
    write_migration(repo, "0003", "0001")
    commit(repo, "a second head on main", home)
    git(repo, "switch", "-q", "-c", "elsewhere", "HEAD~1", home=home)
    assert not (repo / "migrations" / "versions" / "0003_m.py").exists()

    r = push(repo, home, "main")

    assert r.returncode != 0
    assert "0003" in r.stderr


def test_a_feature_branch_with_two_heads_is_not_refused(repo, home):
    """Protected branches only, deliberately. A two-headed graph is unrunnable at the moment
    it is what `alembic upgrade head` points at; before then it is a branch mid-reconcile,
    and refusing every push of one would refuse the reconciler's own working state."""
    git(repo, "switch", "-q", "-c", "feature", home=home)
    write_migration(repo, "0003", "0001")
    commit(repo, "a second head, on a feature branch", home)

    r = push(repo, home, "feature")

    assert r.returncode == 0, r.stderr


def test_a_repo_with_migrations_and_no_reconciler_is_refused(tmp_path, home):
    """An unrunnable gate is not a passing gate. The check plainly applies — there is a
    versions directory at the pushed commit — and nothing here can run it, so the push is
    refused as unverified rather than waved through under the appearance of protection."""
    bare = bare_remote(tmp_path, home)
    work = tmp_path / "bare-tools"
    init(work, home)
    write_migration(work, "0001", None)
    commit(work, "migrations but no reconciler", home)
    git(work, "remote", "add", "origin", str(bare), home=home)
    install(work, home)

    r = push(work, home, "main")

    assert r.returncode != 0
    assert "REFUSE" in r.stderr
    assert "qb.migrationReconcile" in r.stderr, "the remedy must name the config key"
    assert "qb.prePush.migrationHeads false" in r.stderr, "and the recorded opt-out"


def test_the_opt_out_is_recorded_in_config_and_reported_by_status(repo, home):
    """`--no-verify` is the express lane for one push. A repo that genuinely does not want
    the check says so in its own config — and `qb-hooks status` reports it, so a guard that
    has been switched off can never look like one that is quietly passing."""
    write_migration(repo, "0003", "0001")
    commit(repo, "a second head", home)
    git(repo, "config", "--bool", "qb.prePush.migrationHeads", "false", home=home)

    r = push(repo, home, "main")
    assert r.returncode == 0, r.stderr

    status = subprocess.run(
        [str(QB_HOOKS), "status", "--repo", str(repo)], capture_output=True, text=True,
        env=env(home),
    )
    assert "qb.prePush.migrationHeads is off" in status.stdout


def test_no_verify_bypasses_the_guard(repo, home):
    write_migration(repo, "0003", "0001")
    commit(repo, "a second head", home)

    r = git(repo, "push", "--no-verify", "origin", "main", home=home, check=False)

    assert r.returncode == 0, r.stderr


def test_a_tag_is_not_a_branch_and_is_not_guarded(repo, home):
    """The hook reasons about `refs/heads/*` and nothing else. A tag has no branch to be
    protected and no fork point to be relative to."""
    write_migration(repo, "0003", "0001")
    commit(repo, "a second head", home)
    git(repo, "tag", "v-broken", home=home)

    r = push(repo, home, "v-broken")

    assert r.returncode == 0, r.stderr


def test_deleting_a_branch_is_not_guarded(repo, home):
    """An all-zero local sha is a deletion: there is no commit to verify, and asking the
    reconciler about one would refuse the removal of the very branch that broke."""
    git(repo, "switch", "-q", "-c", "doomed", home=home)
    (repo / "doomed.txt").write_text("nothing much\n")
    commit_sha = commit(repo, "nothing much", home)
    assert push(repo, home, "doomed").returncode == 0
    git(repo, "switch", "-q", "main", home=home)
    write_migration(repo, "0003", "0001")
    commit(repo, "a second head on main", home)

    r = push(repo, home, "--delete", "doomed")

    assert r.returncode == 0, r.stderr
    assert commit_sha  # the branch existed on the remote to begin with
    assert remote_sha(repo, "doomed", home) == ""


# ---------------------------------------------------------------------------
# 2. the release number
# ---------------------------------------------------------------------------


def fork_at_first_commit(repo: Path, home: Path, name: str) -> None:
    base = git(repo, "rev-parse", "origin/main", home=home).stdout.strip()
    git(repo, "switch", "-q", "-c", name, base, home=home)


def advance_main_to_v2(repo: Path, home: Path) -> None:
    git(repo, "switch", "-q", "main", home=home)
    (repo / "CHANGELOG.md").write_text(CHANGELOG_V2)
    commit(repo, "release: v2", home)
    r = push(repo, home, "main")
    assert r.returncode == 0, r.stderr


def test_a_branch_that_stamped_a_number_the_base_has_taken_is_refused(repo, home):
    """#287, exactly. The branch forked when v1 was the top of the file and stamped v2;
    main took v2 for something else while it sat. Both entries are right and both belong —
    but one number cannot describe two releases, and the merge would not conflict on it."""
    fork_at_first_commit(repo, home, "pre-stamped")
    advance_main_to_v2(repo, home)

    git(repo, "switch", "-q", "pre-stamped", home=home)
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_V1.replace(
            "## v1 — the first one",
            "## v2 — what this branch shipped\n\nSomething else entirely.\n\n## v1 — the first one",
        )
    )
    commit(repo, "stamp v2 on the branch", home)

    r = push(repo, home, "pre-stamped")

    assert r.returncode != 0
    assert "REFUSE" in r.stderr
    assert "v2" in r.stderr
    # v2.1, not v3: `release_stamp.py`'s own arithmetic is (major, minor + 1), and a major
    # bump is a statement about what the release MEANS that no ref can answer. The hook
    # names the number the stamper would hand out, because it asked the stamper.
    assert "next free number is v2.1" in r.stderr, "max(base, head) + 1 must be named"
    assert remote_sha(repo, "pre-stamped", home) == ""


def test_a_branch_that_is_merely_behind_is_not_refused(repo, home):
    """The whole reason the check is fork-relative. This branch never touched the CHANGELOG;
    main has taken v2 since it forked, so a base-relative comparison sees a branch whose
    highest number is below the base's and reports a regression. There is nothing to report:
    the merge takes the base's value cleanly, there being no competing edit."""
    fork_at_first_commit(repo, home, "behind")
    advance_main_to_v2(repo, home)

    git(repo, "switch", "-q", "behind", home=home)
    (repo / "notes.txt").write_text("work that has nothing to do with releases\n")
    commit(repo, "unrelated work on a stale fork", home)

    r = push(repo, home, "behind")

    assert r.returncode == 0, r.stderr
    assert "REFUSE" not in r.stderr


def test_a_branch_that_takes_the_next_free_number_is_not_refused(repo, home):
    """A legitimate release branch. It stamped ABOVE everything the base carries, which is
    what `release_stamp.py apply` produces, and refusing it would make every release in this
    repo need `--no-verify`."""
    fork_at_first_commit(repo, home, "releasing")
    advance_main_to_v2(repo, home)

    git(repo, "switch", "-q", "releasing", home=home)
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_V1.replace(
            "## v1 — the first one",
            "## v3 — the next free one\n\nStamped against a base it had read.\n\n## v1 — the first one",
        )
    )
    commit(repo, "stamp v3", home)

    r = push(repo, home, "releasing")

    assert r.returncode == 0, r.stderr


def test_an_unstamped_placeholder_is_the_correct_state_of_a_branch_in_flight(repo, home):
    """The one thing this must never do. `## vNEXT` is what every branch in flight is
    supposed to carry — it is what stops two of them picking the same number — and a hook
    that refused it would be the mistake the `stamped` CI job's main-only trigger avoids,
    made a second time and closer to the keyboard."""
    fork_at_first_commit(repo, home, "in-flight")
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_V1.replace(
            "## v1 — the first one",
            "## vNEXT — not yet numbered\n\nStill in flight.\n\n## v1 — the first one",
        )
    )
    commit(repo, "a placeholder, as intended", home)

    r = push(repo, home, "in-flight")

    assert r.returncode == 0, r.stderr


def test_a_branch_that_inherited_the_number_by_merging_is_not_refused(repo, home):
    """Once the branch has taken the merge, v2 is inherited rather than claimed, and the
    merge base moves with it. Text equality would still see the same heading on both sides
    and refuse; the merge base is what tells the two apart."""
    fork_at_first_commit(repo, home, "merged-in")
    advance_main_to_v2(repo, home)

    git(repo, "switch", "-q", "merged-in", home=home)
    (repo / "notes.txt").write_text("branch work\n")
    commit(repo, "branch work", home)
    git(repo, "merge", "-q", "--no-edit", "origin/main", home=home)

    r = push(repo, home, "merged-in")

    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# 2b. the text of a release that already shipped
# ---------------------------------------------------------------------------
#
# The #325 case. A CHANGELOG conflict resolved by relocating the branch's own entry under an
# existing heading deletes a shipped release's notes and leaves every heading present, unique
# and correctly ordered — so the number check above passes on it, and so did every other
# guard in the repo, for two days on an open PR.


def rewrite_the_shipped_entry(repo: Path, home: Path) -> None:
    """A resolution that moves the branch's own body under `## v1`, whose text it replaces."""
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_V1.replace("It did a thing.", "A claim nobody takes: derive the key.")
    )
    commit(repo, "chore: resolving merge conflicts with origin/main", home)


def test_a_branch_that_rewrote_a_shipped_release_entry_is_refused(repo, home):
    """The push that should have stopped. `v1` is on the remote with its own prose; this
    branch carries the same heading over different text."""
    git(repo, "checkout", "-qb", "work", home=home)
    rewrite_the_shipped_entry(repo, home)

    r = push(repo, home, "work")

    assert r.returncode != 0
    assert "REFUSE" in r.stderr
    assert "v1" in r.stderr
    assert "already shipped" in r.stderr
    assert "Release-Body-Edit" in r.stderr, "the sanctioned exception, named where it is due"
    assert remote_sha(repo, "work", home) == "", "and nothing was pushed"


def test_the_number_check_passes_the_very_push_the_body_check_refuses(repo, home):
    """Stated as an assertion because it is the whole argument for a third check. Both read
    the same file at the same commit against the same base; one reads the numbers, which are
    faultless here, and the other reads the bytes."""
    git(repo, "checkout", "-qb", "work", home=home)
    rewrite_the_shipped_entry(repo, home)
    git(repo, "config", "--bool", "qb.prePush.releaseBodies", "false", home=home)

    r = push(repo, home, "work")

    assert r.returncode == 0, r.stderr
    assert "no release-number collision" in r.stdout + r.stderr


def test_a_branch_that_deleted_the_changelog_is_refused(repo, home):
    """Codex found this one. Asking whether the PUSHED commit has a CHANGELOG before running
    the check makes deleting the file the single edit that passes — every release note gone,
    silently, because the guard reads its own absence as "this repo does not use the
    convention". Only the BASE decides whether the question applies."""
    git(repo, "checkout", "-qb", "work", home=home)
    (repo / "CHANGELOG.md").unlink()
    commit(repo, "chore: drop the changelog", home)

    r = push(repo, home, "work")

    assert r.returncode != 0
    assert "REFUSE" in r.stderr
    assert "no CHANGELOG.md at all" in r.stderr
    assert remote_sha(repo, "work", home) == ""


def test_a_branch_that_only_adds_a_release_entry_is_not_refused(repo, home):
    """The release branch this must never fire on. `v2` is new text above an untouched `v1`,
    which is what every release in this repo looks like, and a guard that stopped them all
    would be off within the week."""
    git(repo, "checkout", "-qb", "work", home=home)
    (repo / "CHANGELOG.md").write_text(CHANGELOG_V2)
    commit(repo, "ship v2", home)

    r = push(repo, home, "work")

    assert r.returncode == 0, r.stderr
    assert "no shipped release entry rewritten" in r.stdout + r.stderr


def test_a_release_body_edit_trailer_lets_the_push_through(repo, home):
    """Deliberate, declared, and still reported. A waived edit is an edited release, and the
    push that let it through is the place that cannot be quiet about it."""
    git(repo, "checkout", "-qb", "work", home=home)
    (repo / "CHANGELOG.md").write_text(CHANGELOG_V1.replace("It did a thing.",
                                                            "It did a thing, once."))
    commit(repo, "docs: a word in v1's entry\n\nRelease-Body-Edit: v1", home)

    r = push(repo, home, "work")

    assert r.returncode == 0, r.stderr
    assert "waived: v1" in r.stdout + r.stderr


def test_the_released_entry_opt_out_is_recorded_in_config_and_reported_by_status(repo, home):
    """Bypassable deliberately, never accidentally — and a guard that has been switched off
    must never look like one quietly passing."""
    git(repo, "checkout", "-qb", "work", home=home)
    rewrite_the_shipped_entry(repo, home)
    git(repo, "config", "--bool", "qb.prePush.releaseBodies", "false", home=home)

    assert push(repo, home, "work").returncode == 0

    status = subprocess.run(
        [str(QB_HOOKS), "status", "--repo", str(repo)], capture_output=True, text=True,
        env=env(home),
    )
    assert "qb.prePush.releaseBodies is off" in status.stdout


# ---------------------------------------------------------------------------
# 3. repos this hook is not for
# ---------------------------------------------------------------------------


def test_a_repo_with_no_migrations_pushes_silently(tmp_path, home):
    """The harness installs into repos that are neither quarterback nor lexray. Not merely
    "does not refuse": says NOTHING. A hook that greets every push in an unrelated repo with
    a line about Alembic is a hook that gets uninstalled, and then it is protecting neither
    repo."""
    bare = bare_remote(tmp_path, home)
    work = tmp_path / "unrelated"
    init(work, home)
    (work / "hello.txt").write_text("a repo with no migrations and no releases\n")
    commit(work, "initial", home)
    git(work, "remote", "add", "origin", str(bare), home=home)
    install(work, home)

    r = push(work, home, "main")

    assert r.returncode == 0, r.stderr
    # `git push` writes its own progress to stderr and nothing to stdout, so an empty stdout
    # is the whole of "the hook said nothing" — asserted on the stream rather than on a
    # substring, because the temp paths in the progress lines carry the test's own name.
    assert r.stdout == ""
    assert "qb pre-push" not in r.stderr
    assert "REFUSE" not in r.stderr


def test_a_repo_with_migrations_but_no_changelog_is_silent_about_releases(tmp_path, home):
    """The two checks are independent. A repo that numbers migrations but does not use the
    placeholder convention hears about the graph and nothing else."""
    bare = bare_remote(tmp_path, home)
    work = tmp_path / "migrations-only"
    init(work, home)
    write_migration(work, "0001", None)
    ship_tools(work, "migration_reconcile.py", "release_stamp.py")
    commit(work, "initial", home)
    git(work, "remote", "add", "origin", str(bare), home=home)
    install(work, home)
    assert push(work, home, "main").returncode == 0

    (work / "hello.txt").write_text("more\n")
    commit(work, "more", home)
    r = push(work, home, "main")

    assert r.returncode == 0, r.stderr
    assert "single migration head" in r.stdout + r.stderr
    assert "release-number" not in r.stdout + r.stderr


def test_a_repo_with_a_changelog_but_no_stamper_is_silent_about_releases(tmp_path, home):
    """Absence of the stamper is absence of the CONVENTION, not an unrunnable gate: a repo
    that does not ship `release_stamp.py` does not write `## vNEXT`, so there is no claim to
    adjudicate. This is the one place the "unrunnable is not passing" rule does not apply,
    and it is worth pinning so nobody tightens it into a refusal on every unrelated repo
    that happens to keep a CHANGELOG."""
    bare = bare_remote(tmp_path, home)
    work = tmp_path / "changelog-only"
    init(work, home)
    (work / "CHANGELOG.md").write_text(CHANGELOG_V1)
    commit(work, "initial", home)
    git(work, "remote", "add", "origin", str(bare), home=home)
    install(work, home)
    assert push(work, home, "main").returncode == 0

    (work / "CHANGELOG.md").write_text(CHANGELOG_V2)
    commit(work, "another release", home)
    r = push(work, home, "main")

    assert r.returncode == 0, r.stderr
    assert "qb pre-push" not in r.stdout + r.stderr

    # And the same for a REWRITTEN entry, which is the other release question this hook asks.
    # `v1`'s text is replaced outright here — a refusal in the repo above, silence in one that
    # has no stamper and therefore no such convention to have broken.
    (work / "CHANGELOG.md").write_text(CHANGELOG_V2.replace("It did a thing.", "Something."))
    commit(work, "rewrite a shipped entry", home)
    r = push(work, home, "main")

    assert r.returncode == 0, r.stderr
    assert "qb pre-push" not in r.stdout + r.stderr


# ---------------------------------------------------------------------------
# 4. composing with what was already installed
# ---------------------------------------------------------------------------


def test_a_managed_pre_push_hook_still_runs(repo, home, tmp_path):
    """`core.hooksPath` REPLACES the hooks directory rather than stacking with it. Installing
    this guard over a machine that already had a pre-push would otherwise turn that one off
    as a side effect — a guard that makes a different guard stop executing, which is the
    exact failure `qb-hook-forward` exists to prevent for `pre-commit`."""
    managed = tmp_path / "managed-hooks"
    managed.mkdir()
    hook = managed / "pre-push"
    hook.write_text(
        '#!/bin/sh\ntouch "$(git rev-parse --show-toplevel)/managed-ran"\ncat >/dev/null\nexit 0\n'
    )
    hook.chmod(0o755)
    git(repo, "config", "--global", "core.hooksPath", str(managed), home=home)
    install(repo, home)

    (repo / "notes.txt").write_text("x\n")
    commit(repo, "something", home)
    r = push(repo, home, "main")

    assert r.returncode == 0, r.stderr
    assert (repo / "managed-ran").exists(), "the managed pre-push stopped running"


def test_a_managed_pre_push_can_still_refuse(repo, home, tmp_path):
    """Forwarding that swallowed the delegate's verdict would be worse than not forwarding:
    the hook would appear to run and would never stop anything."""
    managed = tmp_path / "managed-hooks"
    managed.mkdir()
    hook = managed / "pre-push"
    hook.write_text('#!/bin/sh\ncat >/dev/null\necho "managed says no" >&2\nexit 1\n')
    hook.chmod(0o755)
    git(repo, "config", "--global", "core.hooksPath", str(managed), home=home)
    install(repo, home)

    (repo / "notes.txt").write_text("x\n")
    commit(repo, "something", home)
    r = push(repo, home, "main")

    assert r.returncode != 0
    assert "managed says no" in r.stderr


def test_the_guard_is_not_overwritten_by_its_own_forwarder(repo, home, tmp_path):
    """The re-export loop symlinks every managed hook name to `qb-hook-forward`. Left
    unexcluded, `pre-push` would be replaced by a forwarder and this whole file would be
    testing a script git never executes."""
    managed = tmp_path / "managed-hooks"
    managed.mkdir()
    (managed / "pre-push").write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n")
    (managed / "pre-push").chmod(0o755)
    git(repo, "config", "--global", "core.hooksPath", str(managed), home=home)
    install(repo, home)

    common = Path(
        git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", home=home)
        .stdout.strip()
    )
    installed = common / "qb-hooks" / "pre-push"
    assert installed.is_file() and not installed.is_symlink()
    assert "the BACKSTOP half" in installed.read_text()
    assert (common / "qb-hooks" / "pre-push.delegate").is_symlink()


def test_no_delegate_marker_is_left_when_there_is_nothing_to_chain_to(repo, home, tmp_path):
    managed = tmp_path / "managed-hooks"
    managed.mkdir()
    git(repo, "config", "--global", "core.hooksPath", str(managed), home=home)
    install(repo, home)

    common = Path(
        git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", home=home)
        .stdout.strip()
    )
    assert not (common / "qb-hooks" / "pre-push.delegate").exists()


def test_the_installer_reports_the_pre_push_guard(repo, home):
    status = subprocess.run(
        [str(QB_HOOKS), "status", "--repo", str(repo)], capture_output=True, text=True,
        env=env(home),
    )
    assert "pre-push" in status.stdout
    assert "installed" in status.stdout


def test_the_help_prints_the_whole_header_comment(repo, home):
    """`--help` is a hardcoded line range over this script's own comment block, so a comment
    that grows past it is silently truncated help. Growing it is exactly what documenting a
    third refusal does, and the truncation is invisible from any other test here.

    Run from inside a repo, because `--help` in that position falls through to the dispatch
    at the bottom of the script, which is past the `rev-parse --git-dir` check. Without a cwd
    that is a checkout this asserts about an empty string in the flake's build sandbox and
    passes on a developer's machine — which is the difference between the two, not the help.
    """
    lines = QB_HOOKS.read_text(encoding="utf-8").splitlines()
    header = list(itertools.takewhile(lambda line: line.startswith("#"), lines[3:]))
    assert header, "qb-hooks has no header comment block where --help reads one from"
    last = next(line for line in reversed(header) if line.strip("# "))
    out = subprocess.run([str(QB_HOOKS), "--help"], capture_output=True, text=True,
                         cwd=str(repo), env=env(home)).stdout
    assert out.strip(), "`qb-hooks --help` printed nothing at all"
    assert last.lstrip("# ") in out, (
        "`qb-hooks --help` stops before the end of its own header comment — widen the "
        "`sed -n '3,Np'` range that renders it")


def test_the_hook_source_is_executable_and_shipped():
    """`install -m 0755` sets the mode on the copy, but a source file without the bit is a
    source file nobody can run in place — and `git update-index` would carry the missing bit
    into every checkout of this repo."""
    assert (HOOKS / "pre-push").exists()
    assert os.access(HOOKS / "pre-push", os.X_OK)


def test_a_co_pushed_base_branch_is_what_the_other_refs_are_judged_against(repo, home):
    """`git push origin main topic` carries the base and a colliding branch in one push. The
    number `main` brings with it is not on the remote yet, so judging `topic` against the
    remote-tracking ref judges it against a base that is about to stop existing — and both
    land, under one number, with the guard reporting green."""
    fork_at_first_commit(repo, home, "topic")
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_V1.replace(
            "## v1 — the first one",
            "## v2 — what the branch shipped\n\nOne thing.\n\n## v1 — the first one",
        )
    )
    commit(repo, "stamp v2 on the branch", home)

    git(repo, "switch", "-q", "main", home=home)
    (repo / "CHANGELOG.md").write_text(CHANGELOG_V2)
    commit(repo, "stamp v2 on main", home)

    r = push(repo, home, "main", "topic")

    assert r.returncode != 0
    assert "REFUSE" in r.stderr
    assert "as this push would leave it" in r.stderr, "the base named is the pushed one"
    assert remote_sha(repo, "topic", home) == "", "the colliding branch landed anyway"


def test_a_delegate_that_never_reads_stdin_does_not_break_a_large_push(repo, home, tmp_path):
    """Under `pipefail`, feeding a delegate that exits without draining stdin kills the
    `printf` upstream of it with SIGPIPE, the pipeline reports 141, and a push the delegate
    was perfectly happy with is refused with no message at all. A small push hides inside the
    64K pipe buffer, so this drives enough refs to overflow it — the bug is invisible until
    somebody pushes a lot of tags at once, which is the worst way to distribute one."""
    managed = tmp_path / "managed-hooks"
    managed.mkdir()
    hook = managed / "pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")  # never reads stdin
    hook.chmod(0o755)
    git(repo, "config", "--global", "core.hooksPath", str(managed), home=home)
    install(repo, home)

    common = Path(
        git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", home=home)
        .stdout.strip()
    )
    installed = common / "qb-hooks" / "pre-push"
    zeros = "0" * 40
    # Tags with all-zero shas: nothing here guards them, so the run is the chain-to-delegate
    # path and only that. What is under test is the plumbing, not a check.
    stdin = "".join(f"refs/tags/t{i} {zeros} refs/tags/t{i} {zeros}\n" for i in range(3000))
    assert len(stdin) > 65536, "smaller than a pipe buffer proves nothing"

    r = subprocess.run(
        [str(installed), "origin", "git@example.invalid:x.git"],
        cwd=repo, input=stdin, capture_output=True, text=True, env=env(home),
    )

    assert r.returncode == 0, f"exit {r.returncode}: {r.stderr}"


def test_a_first_push_carrying_the_base_and_a_collision_is_refused(tmp_path, home):
    """Nothing has been fetched from this remote, so there is no `origin/main` to be
    fork-relative against — but `main` is in the push, and it is the only base that exists.
    Skipping both refs here lands a collision on the first push a repo ever makes."""
    bare = bare_remote(tmp_path, home)
    work = tmp_path / "fresh"
    init(work, home)
    (work / "CHANGELOG.md").write_text(CHANGELOG_V1)
    ship_tools(work, "release_stamp.py")
    commit(work, "initial", home)
    git(work, "remote", "add", "origin", str(bare), home=home)
    install(work, home)

    git(work, "switch", "-q", "-c", "topic", home=home)
    (work / "CHANGELOG.md").write_text(
        CHANGELOG_V1.replace(
            "## v1 — the first one",
            "## v2 — the branch's\n\nOne thing.\n\n## v1 — the first one",
        )
    )
    commit(work, "stamp v2 on the branch", home)
    git(work, "switch", "-q", "main", home=home)
    (work / "CHANGELOG.md").write_text(CHANGELOG_V2)
    commit(work, "stamp v2 on main", home)

    r = push(work, home, "main", "topic")

    assert r.returncode != 0
    assert "REFUSE" in r.stderr
    assert remote_sha(work, "topic", home) == ""


def test_a_relative_managed_hooks_path_is_resolved_against_the_worktree(repo, home):
    """`core.hooksPath` may be relative, and git takes it relative to the top of the WORKING
    TREE — the directory it runs hooks from. The installer runs from wherever the caller
    happened to be, which for every caller in this harness is another repo entirely; resolving
    it there finds nothing, leaves no `.delegate` marker, and the managed hook silently stops
    running. That is the failure `qb-hook-forward` exists to prevent, arriving through the
    installer instead of through the hook."""
    local_hooks = repo / "local-hooks"
    local_hooks.mkdir()
    hook = local_hooks / "pre-push"
    hook.write_text(
        '#!/bin/sh\ntouch "$(git rev-parse --show-toplevel)/managed-ran"\ncat >/dev/null\nexit 0\n'
    )
    hook.chmod(0o755)
    git(repo, "config", "--global", "core.hooksPath", "local-hooks", home=home)
    install(repo, home)

    common = Path(
        git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", home=home)
        .stdout.strip()
    )
    assert (common / "qb-hooks" / "pre-push.delegate").is_symlink(), (
        "the relative delegate was never found, so nothing chains to it"
    )

    (repo / "notes.txt").write_text("x\n")
    commit(repo, "something", home)
    r = push(repo, home, "main")

    assert r.returncode == 0, r.stderr
    assert (repo / "managed-ran").exists(), "the managed pre-push stopped running"


def test_a_configured_base_that_is_not_here_is_refused_rather_than_swapped(repo, home):
    """An operator who named a base meant that base. Falling back to `main` would run the
    check, report on it, and answer a different question than the one it was configured to
    ask — which is worse than not running, because it reads as protection."""
    git(repo, "config", "qb.baseBranch", "release", home=home)
    (repo / "notes.txt").write_text("x\n")
    commit(repo, "something", home)

    r = push(repo, home, "main")

    assert r.returncode != 0
    assert "qb.baseBranch names 'release'" in r.stderr
    assert "git fetch origin release" in r.stderr


def test_a_configured_base_that_is_here_is_the_one_used(repo, home):
    """And when it resolves, it is used — not `main`, which is what the fallback would have
    picked. `release` carries v2; the branch stamped v2 too, and only a check against
    `release` can see that."""
    git(repo, "update-ref", "refs/remotes/origin/release",
        git(repo, "rev-parse", "origin/main", home=home).stdout.strip(), home=home)
    git(repo, "config", "qb.baseBranch", "release", home=home)
    git(repo, "switch", "-q", "-c", "topic", home=home)
    (repo / "CHANGELOG.md").write_text(CHANGELOG_V2)
    commit(repo, "stamp v2 on the branch", home)
    assert push(repo, home, "topic").returncode == 0, "nothing to collide with yet"

    # `release` takes v2 for something else, and `main` never does — so a fallback to main
    # would still report clean.
    git(repo, "switch", "-q", "-c", "release", "origin/main", home=home)
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_V1.replace(
            "## v1 — the first one",
            "## v2 — the release branch's\n\nSomething else.\n\n## v1 — the first one",
        )
    )
    commit(repo, "stamp v2 on release", home)
    git(repo, "update-ref", "refs/remotes/origin/release",
        git(repo, "rev-parse", "HEAD", home=home).stdout.strip(), home=home)

    git(repo, "switch", "-q", "topic", home=home)
    (repo / "more.txt").write_text("more branch work\n")
    commit(repo, "more work", home)
    r = push(repo, home, "topic")

    assert r.returncode != 0
    assert "origin/release" in r.stderr


def test_the_configured_base_pushing_itself_for_the_first_time_is_not_refused(tmp_path, home):
    """The refusal above asks you to fetch a branch. On the push that CREATES that branch
    there is nothing to fetch, and refusing would make the configured base unpushable — a
    guard whose remedy is the very thing it is blocking."""
    bare = bare_remote(tmp_path, home)
    work = tmp_path / "fresh-release"
    init(work, home)
    (work / "CHANGELOG.md").write_text(CHANGELOG_V1)
    ship_tools(work, "release_stamp.py")
    commit(work, "initial", home)
    git(work, "remote", "add", "origin", str(bare), home=home)
    git(work, "config", "qb.baseBranch", "release", home=home)
    install(work, home)
    git(work, "switch", "-q", "-c", "release", home=home)

    r = push(work, home, "release")

    assert r.returncode == 0, r.stderr
    assert "REFUSE" not in r.stderr


def test_a_stamper_that_cannot_answer_is_reported_as_unverified_not_as_a_collision(
        repo, home, tmp_path):
    """A tool that fell over is not a verdict. Reporting exit 1 as a confirmed collision
    sends the reader to unstamp an entry that was never the problem — and the push is still
    refused either way, so the only thing the conflation costs is the reader's afternoon."""
    broken = tmp_path / "broken-stamper.py"
    # No shebang: the hook runs these through the interpreter it resolved
    # (`python3 <path>`), never by exec'ing the file, so one would be decoration — and a
    # `/usr/bin/env` line in a string literal does not exist inside a nix build sandbox
    # (harness/tests/test_runtime_stub_shebangs.py, #177).
    broken.write_text("import sys\nprint('kaboom', file=sys.stderr)\nsys.exit(1)\n")
    (repo / "notes.txt").write_text("x\n")
    commit(repo, "something", home)

    r = git(repo, "push", "origin", "main", home=home, check=False,
            QB_RELEASE_STAMP=str(broken))

    assert r.returncode != 0
    assert "could not run (exit 1)" in r.stderr
    assert "unverified rather than clean" in r.stderr
    assert "refuses against" not in r.stderr, "it must not read as a verdict"
    assert "kaboom" in r.stderr, "and it must show what the tool said"


def test_a_reconciler_that_cannot_answer_is_reported_as_unverified_not_as_two_heads(
        repo, home, tmp_path):
    """The same distinction on the graph side. `heads` exits 2 for a graph it refuses and
    something else when it could not answer at all, so the hook has the exit code to tell
    them apart and no excuse for the hedge lexray's hook has to make in prose."""
    broken = tmp_path / "broken-reconciler.py"
    # No shebang: the hook runs these through the interpreter it resolved
    # (`python3 <path>`), never by exec'ing the file, so one would be decoration — and a
    # `/usr/bin/env` line in a string literal does not exist inside a nix build sandbox
    # (harness/tests/test_runtime_stub_shebangs.py, #177).
    broken.write_text("import sys\nprint('boom', file=sys.stderr)\nsys.exit(1)\n")
    (repo / "notes.txt").write_text("x\n")
    commit(repo, "something", home)

    r = git(repo, "push", "origin", "main", home=home, check=False,
            QB_MIGRATION_RECONCILE=str(broken))

    assert r.returncode != 0
    assert "could not run (exit 1)" in r.stderr
    assert "alembic upgrade head" not in r.stderr, "it must not read as a two-head verdict"
    assert "boom" in r.stderr


def test_a_branch_with_no_merge_base_says_the_check_was_limited(repo, home):
    """No fork point, no way to tell a number this branch CLAIMED from one it inherited — so
    the stamper deliberately passes rather than refusing every branch that shares a number
    with its base. That is the right call, and saying nothing about it is not: a gate that
    quietly checked less than it advertises reports the strong answer for the weak one."""
    git(repo, "checkout", "-q", "--orphan", "detached-history", home=home)
    (repo / "CHANGELOG.md").write_text(CHANGELOG_V2)
    commit(repo, "a history with no common ancestor", home)

    r = push(repo, home, "detached-history")

    assert r.returncode == 0, r.stderr
    assert "LIMITED" in r.stdout + r.stderr
    assert "no merge base" in r.stdout + r.stderr


# ---------------------------------------------------------------------------
# 4. the release number is RESERVED, atomically, on the remote
# ---------------------------------------------------------------------------
# The one step in the hook that takes something rather than checking it, and the reason it
# is in a hook at all: `release_stamp.py` hands out `max(headings at the base) + 1`, which
# is a reading of a shared file, and two landers seconds apart read the same answer. Section
# 2 above can only report that afterwards. `git push <remote> <sha>:refs/tags/v3` cannot be
# done twice (#296).


def remote_tags(repo: Path, home: Path) -> dict[str, str]:
    """{tag name: sha} on the remote. `^{}` lines dropped — the hook creates lightweight
    tags via a refspec push, and a peeled line would double-count an annotated one."""
    out = git(repo, "ls-remote", "--tags", "origin", home=home).stdout
    found = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        ref = ref.strip()
        if ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            found[ref[len("refs/tags/"):]] = sha.strip()
    return found


def fork_at_v2(repo: Path, home: Path, name: str) -> None:
    """Advance the remote's `main` to v2, then branch off THAT.

    Off the advanced base rather than off the first commit, because these tests are about
    what happens above a shared newest release: a branch forked at v1 that then wrote v2's
    entry would be claiming v2 as well as v3, which is a different refusal entirely and one
    section 2 already covers.
    """
    advance_main_to_v2(repo, home)
    git(repo, "switch", "-q", "-c", name, "origin/main", home=home)


def stamp_v3(repo: Path, home: Path, what: str) -> str:
    """Write the release entry a branch carries once `release_stamp.py apply` has run."""
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_V2.replace(
            "## v2 — the second one",
            f"## v3 — {what}\n\n{what}.\n\n## v2 — the second one",
        )
    )
    return commit(repo, f"stamp v3: {what}", home)


def test_a_branch_that_stamps_a_number_takes_it_on_the_remote(repo, home):
    """The whole point. Pushing the release commit creates `refs/tags/v3` on the remote, and
    it names the commit that carries the entry — so the number is spoken for from the moment
    it leaves the machine rather than from the moment the pull request merges."""
    fork_at_v2(repo, home, "releasing")
    sha = stamp_v3(repo, home, "the next free one")

    r = push(repo, home, "releasing")

    assert r.returncode == 0, r.stderr
    assert remote_tags(repo, home).get("v3") == sha
    assert "reserved v3" in r.stdout, r.stdout


def test_the_second_lander_of_two_stamping_the_same_number_is_refused(repo, home, tmp_path):
    """#289/#291's shape, reconstructed, and the case every other guard here sees too late.

    Two landers fork at v2, both read v2 as the newest, and both stamp v3 — which is what
    `apply` correctly hands each of them, because at the moment each asked, v3 WAS free. The
    branches conflict on nothing: different files, different commits, and each is
    individually clean against `origin/main`. Under the checks in section 2 both pushes are
    accepted, both pull requests merge, and `main` ends up with two `## v3` entries and a red
    `stamped` job blocking everybody else's landing.

    The tag makes the second push the failure instead of the second merge. Whoever pushes
    first creates `refs/tags/v3`; the create is compare-and-swap, so the other one cannot.
    """
    bare = git(repo, "remote", "get-url", "origin", home=home).stdout.strip()
    advance_main_to_v2(repo, home)

    # A genuinely separate checkout, the way two landers on two machines are separate: same
    # remote, its own worktree, its own hook. Simulating it inside one repo would prove
    # nothing, since the interesting question is what the REMOTE does when both arrive.
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", bare, str(other), home=home)
    git(other, "config", "user.email", "t@example.com", home=home)
    git(other, "config", "user.name", "Test", home=home)
    install(other, home)

    git(repo, "switch", "-q", "-c", "lander-a", "origin/main", home=home)
    first = stamp_v3(repo, home, "what lander A shipped")

    git(other, "switch", "-q", "-c", "lander-b", "origin/main", home=home)
    (other / "unrelated.txt").write_text("lander B's actual work\n")
    commit(other, "lander B's work", home)
    stamp_v3(other, home, "what lander B shipped")

    won = push(repo, home, "lander-a")
    assert won.returncode == 0, won.stderr
    assert remote_tags(repo, home).get("v3") == first

    lost = git(other, "push", "origin", "lander-b", home=home, check=False)

    assert lost.returncode != 0, "the second lander must not get v3 too"
    assert "REFUSE" in lost.stderr
    assert "already reserved" in lost.stderr
    assert "v3" in lost.stderr
    assert remote_sha(other, "lander-b", home) == "", "and nothing was pushed"
    # Still exactly one holder, and it is the first lander's commit. A tag is not moved by
    # anything here — a moved tag would be the collision arriving quietly instead of loudly.
    assert remote_tags(repo, home).get("v3") == first


def test_a_branch_that_stamps_nothing_reserves_nothing(repo, home):
    """What makes this safe to run on every push rather than on release pushes only. An
    ordinary feature branch's newest heading is one it INHERITED, and taking a tag for that
    would create a tag for somebody else's release on every push in the repo."""
    fork_at_v2(repo, home, "ordinary")
    (repo / "notes.txt").write_text("work with no release in it\n")
    commit(repo, "ordinary work", home)

    r = push(repo, home, "ordinary")

    assert r.returncode == 0, r.stderr
    # v2 and only v2 — the release main itself pushed. Nothing was taken for this branch.
    assert set(remote_tags(repo, home)) == {"v2"}


def test_an_unstamped_placeholder_reserves_nothing(repo, home):
    """`## vNEXT` names no number, which is the entire reason a branch writes it. There is
    nothing here to take, and a hook that invented one would be picking the number at exactly
    the moment the placeholder exists to avoid picking it."""
    fork_at_first_commit(repo, home, "in-flight")
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_V1.replace(
            "## v1 — the first one",
            "## vNEXT — not yet numbered\n\nStill in flight.\n\n## v1 — the first one",
        )
    )
    commit(repo, "a placeholder, as intended", home)

    r = push(repo, home, "in-flight")

    assert r.returncode == 0, r.stderr
    assert remote_tags(repo, home) == {}


def test_a_refused_push_takes_no_tag(repo, home):
    """A tag is permanent — that is what lets it be a lock — so one taken for a branch that
    is about to be refused burns a release number on a push that never happened. The reserve
    pass runs only after every check has passed."""
    fork_at_first_commit(repo, home, "pre-stamped")
    advance_main_to_v2(repo, home)
    on_main = remote_tags(repo, home)["v2"]
    git(repo, "switch", "-q", "pre-stamped", home=home)
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_V1.replace(
            "## v1 — the first one",
            "## v2 — what this branch shipped\n\nSomething else.\n\n## v1 — the first one",
        )
    )
    branch_tip = commit(repo, "stamp a number the base has taken", home)

    r = push(repo, home, "pre-stamped")

    assert r.returncode != 0
    # v2 still names main's commit. Nothing was taken for the refused branch, and nothing
    # that already existed was moved onto it.
    assert remote_tags(repo, home) == {"v2": on_main}
    assert on_main != branch_tip


def test_a_repo_with_no_tagger_pushes_exactly_as_before(tmp_path, home):
    """Silent where it does not apply, like every other check in this file. The hook ships
    with the harness into repos that are neither quarterback nor lexray, and a note about a
    tool they do not have is how the whole hook gets uninstalled."""
    bare = bare_remote(tmp_path, home)
    work = tmp_path / "work"
    init(work, home)
    (work / "CHANGELOG.md").write_text(CHANGELOG_V1)
    ship_tools(work, "release_stamp.py")
    commit(work, "initial", home)
    git(work, "remote", "add", "origin", str(bare), home=home)
    install(work, home)
    assert push(work, home, "-u", "main").returncode == 0

    (work / "CHANGELOG.md").write_text(CHANGELOG_V2)
    commit(work, "release: v2", home)
    r = push(work, home, "main")

    assert r.returncode == 0, r.stderr
    assert "reserve" not in r.stdout
    assert remote_tags(work, home) == {}


def test_the_reserve_opt_out_is_recorded_in_config_and_reported_by_status(repo, home):
    """An escape hatch that `qb-hooks status` cannot see is a hole. This one is per-repo,
    recorded in git config, and reported — the same shape as the three checks above."""
    git(repo, "config", "--bool", "qb.prePush.reserveRelease", "false", home=home)
    fork_at_v2(repo, home, "releasing")
    stamp_v3(repo, home, "the next free one")

    r = push(repo, home, "releasing")
    assert r.returncode == 0, r.stderr
    assert "v3" not in remote_tags(repo, home)

    status = subprocess.run(
        [str(QB_HOOKS), "status", "--repo", str(repo)], capture_output=True, text=True,
        env=env(home),
    )
    assert "qb.prePush.reserveRelease is off" in status.stdout


def test_a_taken_number_is_refused_before_the_sibling_has_merged(repo, home):
    """The window that nothing before this could see. `release_stamp.py collision` asks
    whether the number is at `origin/main` — and until the first lander's pull request
    merges, it is not. The tag is at neither ref and is the only record that the number is
    gone, which is why `_collision` learned to read tags as well as headings."""
    fork_at_v2(repo, home, "second")
    # Somebody else's reservation: a tag on a commit this branch does not contain, and no
    # `## v3` anywhere on `main`.
    git(repo, "switch", "-q", "main", home=home)
    (repo / "sibling.txt").write_text("the other lander's work\n")
    sibling = commit(repo, "a sibling's release commit", home)
    git(repo, "push", "-q", "--no-verify", "origin", f"{sibling}:refs/tags/v3", home=home)
    git(repo, "reset", "-q", "--hard", "origin/main", home=home)
    git(repo, "fetch", "-q", "origin", "--tags", home=home)

    git(repo, "switch", "-q", "second", home=home)
    stamp_v3(repo, home, "what the second lander shipped")

    r = push(repo, home, "second")

    assert r.returncode != 0
    assert "REFUSE" in r.stderr
    assert "v3" in r.stderr
    assert remote_sha(repo, "second", home) == ""


# ---------------------------------------------------------------------------
# 5. this suite's own sandbox
# ---------------------------------------------------------------------------

#: What this suite reads from outside `harness/`. `nix build .#checks.<system>.worktree-tests`
#: runs it in a store sandbox holding only what that check copies in, and a read nobody copied
#: does not FAIL there — it ERRORS on a missing file, in a build log nobody reads, which is how
#: #163 sat unnoticed for a day. This pair arrived exactly that way: every test above errored
#: with `FileNotFoundError: /build/scripts/mig...` on the first CI run of this file.
READS = (
    "harness/githooks",      # the hook under test, and the forwarder it chains through
    "scripts/migration_reconcile.py",
    "scripts/release_stamp.py",
    "scripts/release_tag.py",
)

#: The check that runs this suite. Written out, not discovered: a renamed check has to be an
#: error here rather than an empty comparison reporting everything as fine.
CHECK_NAME = "worktree-tests"


@pytest.mark.parametrize("path", READS)
def test_the_worktree_flake_check_supplies_what_this_suite_reads(path: str):
    region = _flake_sandbox.check_region(
        (ROOT / "flake.nix").read_text(encoding="utf-8"), CHECK_NAME)
    pairs = _flake_sandbox.copies(region)
    # prefix="" and not the default "repo/": this check builds `harness/…` at the top level
    # and `cd harness`, where the prose and release-metadata sandboxes build a `repo/` tree.
    assert not _flake_sandbox.misdirected(pairs, prefix=""), \
        _flake_sandbox.misdirected(pairs, prefix="")
    assert _flake_sandbox.supplied_by(path, set(pairs)), (
        f"flake.nix's {CHECK_NAME} sandbox does not supply {path}, which this suite reads. "
        f"Add a `cp -r`/`install -D` line for it beside the others, or every assertion about "
        f"it errors on a missing file instead of being evaluated (#163).")


@pytest.mark.parametrize("path", READS)
def test_every_declared_read_is_a_path_this_file_names(path: str):
    """The converse: a declaration whose last reader was deleted still matches the copy line
    answering it, and the two then keep each other alive over a file nothing needs."""
    named = {str(HOOKS.relative_to(ROOT))} | {
        str((SCRIPTS / name).relative_to(ROOT)) for name in ("migration_reconcile.py",
                                                             "release_stamp.py",
                                                             "release_tag.py")}
    assert any(p == path or p.startswith(path + "/") for p in named), (
        f"READS declares {path}, which no constant in this module resolves to — either the "
        f"read went away and the declaration should too, or it moved")

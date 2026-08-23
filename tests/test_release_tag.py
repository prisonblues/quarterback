"""Tests for `scripts/release_tag.py` — the record that every release has a tag on it.

Every test builds throwaway git repos. The invariant is one line — a tag `vX.Y` points at a
commit whose CHANGELOG.md declares `## vX.Y` — and every question about it is a question
about what a file says at a ref, which a fixture string cannot answer.

The test this file exists for is now
`test_a_reservation_and_an_orphan_in_one_repo_are_told_apart`: a tag off the integration ref
is two opposite things wearing one face, and telling them apart is what stopped #406's
orphaned `v3.8` reading as an ordinary release in flight.

`reserve` is gone (#122). It took `refs/tags/vX.Y` on the remote at push time as the lock a
branch-side stamp needed; branches do not stamp, there is no race on `main`, and there is
nothing left to reserve.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# `scripts/` is a directory of standalone tools rather than a package, so the module is
# loaded by path. `release_tag` loads `release.py` from beside itself on import, which is
# why the fixtures below never have to do it here.
_SPEC = importlib.util.spec_from_file_location(
    "release_tag",
    Path(__file__).resolve().parent.parent / "scripts" / "release_tag.py",
)
rt = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rt
_SPEC.loader.exec_module(rt)


CHANGELOG_HEAD = """# Version history

Entries are newest first.

"""


def entry(version: str, body: str = "did a thing.", title: str = "a release") -> str:
    return f"## {version} — {title}\n\n{body}\n\n"


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def write(repo: Path, path: str, text: str) -> None:
    full = repo / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(text)


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch, tmp_path: Path) -> None:
    """No developer's global git config reaches these repos.

    `release_stamp.py`'s suite documents why at length and it applies harder here, because
    this file pushes: a global `push.followTags`, a `remote.pushDefault`, or a signing
    requirement changes what the command under test actually sends.
    """
    empty = tmp_path / "gitconfig-none"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)


def init(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    return root


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True, text=True)
    return bare


@pytest.fixture
def repo(tmp_path: Path, remote: Path) -> Path:
    """A repo whose `main` is at v2.33, pushed, with `origin` pointing at a bare remote."""
    root = init(tmp_path / "repo")
    write(root, "CHANGELOG.md",
          CHANGELOG_HEAD + entry("v2.33") + entry("v2.32") + entry("v2"))
    commit(root, "v2.33")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-q", "-u", "origin", "main")
    return root


def clone(remote: Path, into: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(remote), str(into)],
                   check=True, capture_output=True, text=True)
    git(into, "config", "user.email", "t@example.com")
    git(into, "config", "user.name", "t")
    return into


def stamp(repo: Path, version: str, what: str = "a release") -> str:
    """Write the entry a branch carries once `release_stamp.py apply` has run."""
    text = (repo / "CHANGELOG.md").read_text()
    at = text.index("## ")
    write(repo, "CHANGELOG.md", text[:at] + entry(version, title=what) + text[at:])
    return commit(repo, f"chore(release): {version} — {what}")


def run(repo: Path, *argv: str) -> int:
    return rt.main([*argv, "--repo", str(repo)])


def out_json(repo: Path, capsys, *argv: str) -> dict:
    rc = run(repo, *argv, "--json")
    return json.loads(capsys.readouterr().out), rc


def remote_tags(repo: Path) -> dict[str, str]:
    """{tag name: the COMMIT it names} on the remote.

    A peeled `^{}` line wins over the tag object's own line, and forgetting that is how a
    suite reports every annotated tag as pointing somewhere unrelated: `backfill` writes
    annotated tags, `reserve` pushes a commit sha straight at the ref and so writes a
    lightweight one, and both have to read the same here.
    """
    peeled: dict[str, str] = {}
    plain: dict[str, str] = {}
    for line in git(repo, "ls-remote", "--tags", "origin").splitlines():
        sha, _, ref = line.partition("\t")
        ref = ref.strip()
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/"):]
        if name.endswith("^{}"):
            peeled[name[:-3]] = sha.strip()
        else:
            plain[name] = sha.strip()
    return {name: peeled.get(name, sha) for name, sha in plain.items()}


# ---------------------------------------------------------------------------
# backfill — the record
# ---------------------------------------------------------------------------


def landed(repo: Path, *versions: str) -> dict[str, str]:
    """Land each release on `main` as its own commit and return where each one landed."""
    where = {}
    for v in versions:
        where[v] = stamp(repo, v)
    return where


def test_backfill_tags_every_release_at_the_commit_that_landed_it(repo):
    where = landed(repo, "v2.34", "v2.35")

    assert run(repo, "backfill") == 0

    assert git(repo, "rev-list", "-n1", "v2.34").strip() == where["v2.34"]
    assert git(repo, "rev-list", "-n1", "v2.35").strip() == where["v2.35"]
    # And the ninety-odd that predate the mechanism: v2.33 shipped before any tag existed
    # and is tagged at the commit whose CHANGELOG first declared it.
    assert git(repo, "rev-list", "-n1", "v2.33").strip() == git(
        repo, "rev-list", "--max-parents=0", "main").strip()


def test_backfill_is_idempotent(repo, capsys):
    landed(repo, "v2.34")
    assert run(repo, "backfill") == 0
    capsys.readouterr()

    assert run(repo, "backfill") == 0

    assert "created 0 tag(s)" in capsys.readouterr().out


def test_backfill_never_moves_a_tag_that_is_already_somewhere_else(repo, capsys):
    """Tags are immutable by convention because everything downstream of one quietly changes
    meaning when they are not. A tag in the wrong place is REPORTED; a human decides whether
    the tag or the entry is wrong."""
    landed(repo, "v2.34")
    stray = git(repo, "rev-list", "--max-parents=0", "main").strip()
    git(repo, "tag", "v2.34", stray)

    assert run(repo, "backfill") == 0

    assert git(repo, "rev-list", "-n1", "v2.34").strip() == stray, "left exactly where it was"
    assert "not moved" in capsys.readouterr().err


def test_backfill_dry_run_creates_nothing(repo, capsys):
    landed(repo, "v2.34")

    assert run(repo, "backfill", "--dry-run") == 0

    assert "would create" in capsys.readouterr().out
    assert git(repo, "tag").strip() == ""


def test_backfill_push_publishes_the_tags_it_created(repo):
    where = landed(repo, "v2.34")

    assert run(repo, "backfill", "--push") == 0

    assert remote_tags(repo)["v2.34"] == where["v2.34"]


def test_push_publishes_tags_an_earlier_run_created_and_could_not_send(repo):
    """The state a failed push leaves, and the reason `--push` asks the REMOTE what is
    missing rather than pushing what this run happened to create.

    A run whose push is rejected — a CI token with no write access is the case that will
    actually happen — leaves the tags created here and published nowhere. Judged on its own
    `created` list, the next run finds them already present, creates nothing, has nothing to
    push, and reports success over a remote that still has none of them: a clean bill from a
    publish that never happened, in the one command whose entire job is publishing.
    """
    where = landed(repo, "v2.34")
    assert run(repo, "backfill") == 0          # created here, sent nowhere
    assert remote_tags(repo) == {}

    assert run(repo, "backfill", "--push") == 0

    assert remote_tags(repo)["v2.34"] == where["v2.34"]
    assert set(remote_tags(repo)) == {"v2", "v2.32", "v2.33", "v2.34"}


def test_push_reports_a_tag_it_could_not_publish_rather_than_reporting_success(
        repo, remote, capsys):
    """Exit 1, and the tags named. A remote that can be READ and will not be WRITTEN is the
    case that will actually happen — a CI token with `contents: read` — and it is the one
    where a command judging itself on its own return code reports success: `git push`
    prints a rejection, the tags are here, and nothing else notices.

    A `pre-receive` that refuses, rather than an unreachable URL: an unreachable remote
    fails at the read and never reaches the push, so it would exercise a different path and
    prove nothing about this one.
    """
    landed(repo, "v2.34")
    hook = remote / "hooks" / "pre-receive"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\necho 'no write access' >&2\nexit 1\n")
    hook.chmod(0o755)

    assert run(repo, "backfill", "--push") == 1

    err = capsys.readouterr().err
    assert "limited:" in err
    assert "v2.34" in err
    assert "re-running this will try again" in err
    assert remote_tags(repo) == {}


def test_backfill_reads_the_first_parent_line_so_a_tag_names_where_it_landed(repo):
    """A release is stamped on a branch and merged minutes later. The honest answer to
    "where is v2.34" is the merge that put it on `main`, not the branch commit that wrote
    it — the branch commit is not on `main`'s first-parent line at all, and tagging it would
    make `git describe` on a branch that was never merged look like a release."""
    base = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "switch", "-q", "-c", "work")
    on_branch = stamp(repo, "v2.34")
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request", "work")
    merge = git(repo, "rev-parse", "HEAD").strip()
    assert merge not in (base, on_branch)

    assert run(repo, "backfill") == 0

    assert git(repo, "rev-list", "-n1", "v2.34").strip() == merge


def test_a_tag_that_is_not_a_release_number_is_left_alone(repo, capsys):
    """`v2.34-rc1`, `v2.34.1` and `salvage/issue-85` are somebody else's refs. This tool has
    no opinion about tags it did not issue, and a repo is allowed to have others."""
    landed(repo, "v2.34")
    head = git(repo, "rev-parse", "HEAD").strip()
    for name in ("v2.34-rc1", "v2.34.1", "salvage/issue-85", "release-2.34"):
        git(repo, "tag", name, head)

    assert run(repo, "backfill") == 0
    capsys.readouterr()

    taken, rc = out_json(repo, capsys, "taken")
    assert rc == 0
    assert set(taken) == {"v2", "v2.32", "v2.33", "v2.34"}


def test_an_annotated_tag_reports_the_commit_it_peels_to(repo, capsys):
    """`backfill` writes annotated tags, so every comparison here is against a tag OBJECT's
    sha unless it is peeled. Unpeeled, every backfilled tag reads as pointing at a commit
    that does not exist."""
    where = landed(repo, "v2.34")
    assert run(repo, "backfill") == 0
    capsys.readouterr()

    taken, rc = out_json(repo, capsys, "taken")

    assert rc == 0
    assert taken["v2.34"] == where["v2.34"]


def no_identity(repo: Path, monkeypatch, blank: str = "") -> None:
    """Leave this repo in the state a CI runner is in: git can find no name to tag with.

    A runner has no `user.name` and an empty GECOS field, so git auto-detects an email from
    the login and the host, finds no name at all, and refuses. Reproducing that by *deleting*
    things would not survive leaving this machine — a developer box has a GECOS name, so the
    same deletions there leave git perfectly able to tag and the test passes over a bug. The
    exported empty `GIT_COMMITTER_NAME` is deterministic everywhere and hits the same code in
    git: `fatal: empty ident name`, the message from run 32601361711 (#379).
    """
    git(repo, "config", "--unset", "user.name")
    git(repo, "config", "--unset", "user.email")
    monkeypatch.setenv("EMAIL", "runner@runnervm.invalid")
    for var in ("GIT_COMMITTER_NAME", "GIT_AUTHOR_NAME"):
        monkeypatch.setenv(var, blank)
    for var in ("GIT_COMMITTER_EMAIL", "GIT_AUTHOR_EMAIL"):
        monkeypatch.delenv(var, raising=False)


def tagger(repo: Path, name: str) -> str:
    return git(repo, "for-each-ref", "--format=%(taggername) %(taggeremail)",
               f"refs/tags/{name}").strip()


@pytest.mark.parametrize("blank", ["", " "], ids=["unset", "whitespace"])
def test_backfill_tags_where_git_has_no_identity_to_tag_with(repo, monkeypatch, blank):
    """The whole of #379: the `tagged` job runs straight after `actions/checkout` on a runner
    that has never been told who it is, and an annotated tag cannot be written without a
    tagger. Two releases landed untagged before anybody noticed, because every other place
    this command runs — a developer machine, this suite's own fixtures — has an identity
    already.

    A name of one space is the same nothing: git strips an ident before it judges it, so a
    fallback that only looks for the empty string leaves that one failing exactly as before.
    """
    where = landed(repo, "v2.34")
    no_identity(repo, monkeypatch, blank)

    assert run(repo, "backfill") == 0

    assert git(repo, "rev-list", "-n1", "v2.34").strip() == where["v2.34"]
    assert git(repo, "cat-file", "-t", "v2.34").strip() == "tag", "still annotated"
    assert tagger(repo, "v2.34") == "release_tag.py <release-tag@quarterback.invalid>"


def test_backfill_leaves_a_configured_identity_alone(repo):
    """Filling a gap, not taking over. Where git can name a tagger the tag is FROM that
    person, exactly as it was before this — a git config is somebody's own answer to this
    question and the script's is only for where there is none."""
    landed(repo, "v2.34")

    assert run(repo, "backfill") == 0

    assert tagger(repo, "v2.34") == "t <t@example.com>"


def test_backfill_keeps_the_half_of_an_identity_that_is_configured(repo, monkeypatch):
    """A set `user.name` with no resolvable email is a real shape, and git refuses on it just
    as flatly. Only the half that is missing gets invented."""
    landed(repo, "v2.34")
    no_identity(repo, monkeypatch)
    git(repo, "config", "user.name", "somebody")

    assert run(repo, "backfill") == 0

    assert tagger(repo, "v2.34") == "somebody <release-tag@quarterback.invalid>"


# ---------------------------------------------------------------------------
# check — the reconciliation
# ---------------------------------------------------------------------------


def test_check_is_clean_when_every_release_is_tagged(repo, capsys):
    landed(repo, "v2.34")
    assert run(repo, "backfill") == 0
    capsys.readouterr()

    assert run(repo, "check") == 0

    assert "clean:" in capsys.readouterr().out


def test_check_names_a_release_with_no_tag(repo, capsys):
    landed(repo, "v2.34")

    assert run(repo, "check") == 2

    err = capsys.readouterr().err
    assert "no tag for" in err
    assert "v2.34" in err
    assert "backfill" in err, "and the command that fixes it"


def test_a_reservation_that_has_not_merged_is_listed_and_is_not_a_finding(repo, capsys):
    """A release cut locally and not yet pushed, or a tag left from before #122 deleted
    push-time reservation. Either way it holds a number nothing else will hand out, and
    reporting it as a defect would accuse a repo of something nobody can fix — this file
    never deletes a tag, for the same reason it never moves one."""
    assert run(repo, "backfill") == 0
    capsys.readouterr()
    git(repo, "switch", "-q", "-c", "work")
    in_flight = stamp(repo, "v2.34")
    git(repo, "tag", "v2.34", in_flight)
    git(repo, "switch", "-q", "main")

    assert run(repo, "check") == 0

    out = capsys.readouterr().out
    assert "1 tag(s) not on HEAD" in out
    assert "v2.34 tagged at" in out


def test_check_reports_a_tag_whose_commit_does_not_declare_it(repo, capsys):
    assert run(repo, "backfill") == 0
    capsys.readouterr()
    git(repo, "tag", "v2.34", git(repo, "rev-parse", "HEAD").strip())

    assert run(repo, "check") == 2

    err = capsys.readouterr().err
    assert "does not" in err and "declare it" in err
    assert "not moved by this tool" in err


def test_check_json_carries_every_condition_separately(repo, capsys):
    landed(repo, "v2.34")

    payload, rc = out_json(repo, capsys, "check")

    assert rc == 2
    assert payload["clean"] is False
    # Every release, not just the new one: this repo has never been tagged, which is the
    # state quarterback itself was in for ninety-five of them.
    assert payload["untagged"] == ["v2", "v2.32", "v2.33", "v2.34"]
    assert payload["misplaced"] == {}
    assert payload["orphaned"] == {}
    assert payload["reserved"] == {}


def test_a_repo_the_tool_cannot_read_is_a_stop_and_never_a_quiet_zero(tmp_path, capsys):
    root = init(tmp_path / "empty")
    write(root, "README.md", "# nothing here\n")
    commit(root, "initial")

    assert run(root, "check") == 2

    assert "nothing for the tags to be reconciled against" in capsys.readouterr().err


def test_an_unknown_ref_is_a_stop_not_a_traceback(repo, capsys):
    assert run(repo, "check", "--ref", "no/such/ref") == 2
    assert "does not exist here" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the exit-code contract
# ---------------------------------------------------------------------------


def test_exit_one_only_ever_means_could_not_be_checked(repo, monkeypatch, capsys):
    """1 is the third answer and nothing else may produce it. Python's own uncaught
    exception exits 1 too, so an unexpected error has to be mapped to 2 — a caller told that
    1 is soft would otherwise read a traceback as "carry on"."""
    def boom(*_a, **_kw):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(rt, "local_tags", boom)

    assert run(repo, "check") == 2

    assert "RuntimeError" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "posix", reason="the shebang contract is a posix one")
def test_the_script_is_executable_and_has_a_shebang():
    path = Path(rt.__file__)
    assert os.access(path, os.X_OK), "the docs invoke it as `scripts/release_tag.py …`"
    assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3")


# ---------------------------------------------------------------------------
# check — the tag that is off the ref and is NOT a reservation (#406)
# ---------------------------------------------------------------------------


def squash_merged(repo: Path, version: str) -> tuple[str, str]:
    """Reconstruct v3.8's landing, which is the whole of #406.

    A branch stamps the release and reserves `refs/tags/vX.Y` against that commit — which is
    correct, and is what makes the number un-stealable (#296). `main` then takes the branch
    as a SQUASH: a brand-new commit carrying the branch's tree and main as its only parent.
    The entry lands perfectly and the commit the tag names is not in main's history at all.

    Returns `(the commit the tag was reserved against, the commit that actually landed)`.
    """
    git(repo, "switch", "-q", "-c", f"work-{version}")
    reserved = stamp(repo, version)
    git(repo, "tag", version, reserved)
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "-q", "--squash", f"work-{version}")
    landed = commit(repo, f"{version} — squashed (#1)")
    return reserved, landed


def test_a_squash_merged_release_leaves_a_tag_off_the_ref_and_that_is_a_finding(repo, capsys):
    """#406, in one test. Before this the check asked whether a tag of that NAME resolved,
    `v3.8` did, and `every release on main has a tag` was green over a release tag addressing
    a commit that is not in main's history."""
    assert run(repo, "backfill") == 0
    reserved, _landed = squash_merged(repo, "v2.34")
    capsys.readouterr()

    assert run(repo, "check") == 2

    err = capsys.readouterr().err
    assert "v2.34 is tagged at" in err and reserved[:12] in err
    assert "NOT on HEAD" in err
    assert "squash or rebase" in err


def test_the_orphan_report_names_the_commit_the_release_actually_landed_at(repo, capsys):
    """A remedy that says "a tag is wrong" and not which commit is right sends the reader
    back to work out the one thing the tool already knows."""
    assert run(repo, "backfill") == 0
    reserved, landed = squash_merged(repo, "v2.34")
    capsys.readouterr()

    run(repo, "check")

    err = capsys.readouterr().err
    assert f"It landed at {landed[:12]}" in err
    assert f"git tag -f v2.34 {landed[:12]}" in err
    assert f"--force-with-lease=v2.34:{reserved}" in err, \
        "the lease has to quote what the ref holds now, or the repair is not atomic"


def test_the_lease_quotes_the_tag_object_not_the_commit_for_an_annotated_tag(repo, capsys):
    """`reserve` writes a lightweight tag and `backfill` writes an annotated one, and for the
    second the ref holds the tag object rather than the commit. A lease quoting the peeled
    commit is refused — safely, but the printed remedy is somebody's starting point."""
    assert run(repo, "backfill") == 0
    git(repo, "switch", "-q", "-c", "work")
    reserved = stamp(repo, "v2.34")
    git(repo, "tag", "-a", "-m", "v2.34", "v2.34", reserved)
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "-q", "--squash", "work")
    commit(repo, "v2.34 — squashed")
    obj = git(repo, "rev-parse", "refs/tags/v2.34").strip()
    assert obj != reserved, "this test is only meaningful for an annotated tag"
    capsys.readouterr()

    run(repo, "check")

    assert f"--force-with-lease=v2.34:{obj}" in capsys.readouterr().err


def test_a_reservation_and_an_orphan_in_one_repo_are_told_apart(repo, capsys):
    """The constraint that makes this row usable at all, and the one a naive
    `merge-base --is-ancestor` gets wrong. On the night #406 was filed FOUR release tags were
    off main: v3.8 (squashed, a defect) and v3.9, v3.10, v3.12 (open pull requests holding
    their numbers). Flagging the three would have trained everybody to ignore the row — and
    though nothing reserves a number on a branch any more, a release cut and not yet pushed
    has exactly the same shape.

    The discriminator is not the tag. It is whether the CHANGELOG at the ref declares that
    release — which is what "has it landed" means."""
    assert run(repo, "backfill") == 0
    _reserved, _landed = squash_merged(repo, "v2.34")
    git(repo, "switch", "-q", "-c", "in-flight")
    still_going = stamp(repo, "v2.35")
    git(repo, "tag", "v2.35", still_going)
    git(repo, "switch", "-q", "main")
    capsys.readouterr()

    assert run(repo, "check") == 2

    out, err = capsys.readouterr()
    assert "v2.35 tagged at" in out, "an unlanded release's tag is listed, not accused"
    assert "1 landed release(s) whose tag is not on HEAD" in out
    assert "v2.35" not in err, "an unlanded release's tag is not a finding"
    assert "v2.34 is tagged at" in err


def test_check_json_separates_an_orphan_from_a_reservation(repo, capsys):
    """Separate keys rather than one `unreachable`, because the two need opposite responses
    and anything reading this — CI, `qb-doctor`, #407 — must not have to re-derive which is
    which."""
    assert run(repo, "backfill") == 0
    reserved, _ = squash_merged(repo, "v2.34")
    git(repo, "switch", "-q", "-c", "in-flight")
    in_flight = stamp(repo, "v2.35")
    git(repo, "tag", "v2.35", in_flight)
    git(repo, "switch", "-q", "main")
    capsys.readouterr()

    payload, rc = out_json(repo, capsys, "check")

    assert rc == 2
    assert payload["clean"] is False
    assert payload["orphaned"] == {"v2.34": reserved}
    assert payload["reserved"] == {"v2.35": in_flight}
    assert payload["misplaced"] == {}
    assert payload["untagged"] == []


# ---------------------------------------------------------------------------
# the guard runs — #169's class, which is the class #406 belongs to
# ---------------------------------------------------------------------------


def _tagged_job() -> dict:
    """The job that reconciles the tags, found by what it RUNS rather than by its name.

    Its name — `every release on main has a tag` — was a claim it did not test for five
    months, so matching on the name is precisely the mistake this file is about. The
    assertion below is #169's: a mechanism that ships and whose running is never asserted
    is a mechanism nobody will notice the absence of.
    """
    yaml = pytest.importorskip("yaml")
    workflow = Path(__file__).resolve().parent.parent / ".github/workflows/tests.yml"
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    running = [
        job for job in jobs.values()
        if any("release_tag.py check" in "\n".join(
            line for line in str(step.get("run", "")).splitlines()
            if not line.lstrip().startswith("#"))
            for step in job.get("steps", []))
    ]
    assert len(running) == 1, (
        f"{len(running)} jobs in tests.yml run `release_tag.py check`; the guard that a "
        "landed release's tag is on main has to run exactly once and has to run at all")
    return running[0]


def test_ci_reconciles_the_tags_after_it_records_them():
    """Order matters and it is not arbitrary. `backfill`'s job is to make `untagged` empty,
    so a `check` running first would go red on the very release this run is about to tag."""
    steps = [str(s.get("run", "")) for s in _tagged_job()["steps"]]
    backfill = next(i for i, s in enumerate(steps) if "release_tag.py backfill" in s)
    check = next(i for i, s in enumerate(steps) if "release_tag.py check" in s)
    assert backfill < check


def test_the_tag_reconciliation_reads_the_whole_history():
    """The one way this job can be wrong and look right. A release's tag names the commit
    that first declared it, which is a question about all of history, and `merge-base
    --is-ancestor` against a depth-1 checkout answers about one commit."""
    checkouts = [s for s in _tagged_job()["steps"]
                 if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkouts, "the job checks nothing out"
    assert all(str(s.get("with", {}).get("fetch-depth")) == "0" for s in checkouts)

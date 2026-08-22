"""Tests for `scripts/release_tag.py` — the thing that actually allocates a release number.

Every test builds throwaway git repos, and most of them build TWO plus a bare remote. That
is not ceremony: the whole subject is what happens when a ref create races another ref
create, and a fixture that stands in for the remote would only ever assert the stand-in.
`release_stamp.py`'s suite can get away with one repo because its subject is "what does this
file say at that ref"; this one's subject is "who wins".

The test this file exists for is
`test_the_second_lander_of_two_is_refused_by_the_remote`. Everything else here is a
supporting property.
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
# loaded by path. `release_tag` loads `release_stamp` from beside itself on import, which is
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
# reserve — the lock
# ---------------------------------------------------------------------------


def test_reserving_creates_the_tag_on_the_remote_at_the_release_commit(repo, capsys):
    sha = stamp(repo, "v2.34")

    assert run(repo, "reserve") == 0

    assert remote_tags(repo) == {"v2.34": sha}
    assert "reserved v2.34" in capsys.readouterr().out


def test_the_second_lander_of_two_is_refused_by_the_remote(tmp_path, remote, repo, capsys):
    """The window nothing else can close, reconstructed at the level of the tool.

    Both landers fork at v2.33, both read v2.33 as the newest, and `release_stamp.py apply`
    correctly hands each of them v2.34 — because at the moment each one asked, v2.34WAS
    free. Their branches conflict on nothing. Under headings alone both merge and `main`
    ends up declaring v2.34 twice.

    A ref create is compare-and-swap, so exactly one of them gets the tag, and the other is
    told so before its branch is anywhere near `main`.
    """
    other = clone(remote, tmp_path / "other")

    first = stamp(repo, "v2.34", "what lander A shipped")
    stamp(other, "v2.34", "what lander B shipped")

    assert run(repo, "reserve") == 0
    capsys.readouterr()

    assert run(other, "reserve") == 2

    err = capsys.readouterr().err
    assert "already reserved" in err
    assert "v2.34" in err
    assert "vNEXT" in err, "and the repair is named, not left to be worked out"
    assert remote_tags(repo) == {"v2.34": first}, "still one holder, and it is the first"


def test_a_reservation_this_branch_already_holds_is_not_a_refusal(repo, capsys):
    """`reserve` has to be safe to run twice. A pre-push hook runs it on every push of the
    branch, and a second push after a review fix must not be refused by the first push's own
    tag."""
    stamp(repo, "v2.34")
    assert run(repo, "reserve") == 0
    capsys.readouterr()

    write(repo, "notes.md", "a review fix\n")
    commit(repo, "address review")

    assert run(repo, "reserve") == 0
    assert "already tagged" in capsys.readouterr().out


def test_a_commit_still_carrying_the_placeholder_has_no_number_to_reserve(repo, capsys):
    """`## vNEXT` names no number — that is the whole reason a branch writes it. Inventing
    one here would be picking the number at exactly the moment the placeholder exists to
    stop it being picked."""
    text = (repo / "CHANGELOG.md").read_text()
    at = text.index("## ")
    write(repo, "CHANGELOG.md", text[:at] + entry("vNEXT") + text[at:])
    commit(repo, "an entry in flight")

    # The newest NUMBERED release is still v2.33 and it is already at the base, so with the
    # base named there is nothing to take and that is not an error.
    assert run(repo, "reserve", "--onto", "origin/main") == 0
    assert "nothing to reserve" in capsys.readouterr().out


def test_reserving_a_number_the_commit_does_not_declare_is_refused(repo, capsys):
    """The invariant, enforced at the one place it can be broken deliberately. A tag names a
    release the commit it points at carries; a `--version` naming anything else would put a
    number in the repository that no document explains."""
    stamp(repo, "v2.34")

    assert run(repo, "reserve", "--version", "v2.99") == 2

    assert "does not declare" in capsys.readouterr().err
    assert remote_tags(repo) == {}


def test_a_version_that_is_not_a_release_number_is_refused(repo, capsys):
    stamp(repo, "v2.34")
    assert run(repo, "reserve", "--version", "2.34.0") == 2
    assert "not a release number" in capsys.readouterr().err


def test_onto_makes_reserve_a_noop_for_a_branch_that_stamped_nothing(repo, capsys):
    """What makes the command safe to run on every push. An ordinary branch's newest heading
    is one it INHERITED, and reserving that would take a tag for somebody else's release on
    every push in the repo."""
    write(repo, "notes.md", "work with no release in it\n")
    commit(repo, "ordinary work")

    assert run(repo, "reserve", "--onto", "origin/main") == 0

    assert "nothing to reserve" in capsys.readouterr().out
    assert remote_tags(repo) == {}


def test_no_remote_is_reported_as_unavailable_and_not_as_a_collision(tmp_path, capsys):
    """Exit 1, the third answer. A tag that exists only locally reserves nothing, and saying
    "reserved" over one would be the reassuring wrong answer this repo keeps finding."""
    root = init(tmp_path / "solo")
    write(root, "CHANGELOG.md", CHANGELOG_HEAD + entry("v2.33"))
    commit(root, "v2.33")

    assert run(root, "reserve") == 1

    assert "no remote" in capsys.readouterr().err


def test_a_repo_with_no_changelog_is_a_stop_not_a_traceback(tmp_path, capsys):
    root = init(tmp_path / "empty")
    write(root, "README.md", "# nothing here\n")
    commit(root, "initial")

    assert run(root, "reserve") == 2

    assert "STOP:" in capsys.readouterr().err


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
    """The correct state of every release in flight. A reservation points at a branch commit
    and is not on `main` until that branch merges — reporting it as a defect would make the
    check red for the entire duration of every release."""
    assert run(repo, "backfill") == 0
    capsys.readouterr()
    git(repo, "switch", "-q", "-c", "work")
    in_flight = stamp(repo, "v2.34")
    git(repo, "tag", "v2.34", in_flight)
    git(repo, "switch", "-q", "main")

    assert run(repo, "check") == 0

    out = capsys.readouterr().out
    assert "1 tag(s) not on HEAD" in out
    assert "v2.34 reserved at" in out


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
    assert payload["unreachable"] == {}


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

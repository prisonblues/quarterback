"""Tests for harness_rules — the per-repo .harness-rules resolver.

The interesting one is the two-ref rule: an unattended run must read rules from
the DEFAULT BRANCH, never the working tree, or an upstream-authored dependabot
branch could rewrite the policy governing its own review.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_rules as hr  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repo with an 'origin' remote, on branch 'main'.

    origin is a bare clone on disk so `git show origin/main:...` and the
    remote-HEAD detection both behave like the real thing.
    """
    monkeypatch.delenv("HARNESS_UNATTENDED", raising=False)
    work = tmp_path / "myrepo"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "T")
    (work / "README").write_text("x\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    git(work, "remote", "add", "origin", f"https://github.com/acme/myrepo.git")
    git(work, "remote", "set-url", "origin", str(bare))
    git(work, "push", "-q", "origin", "main")
    git(work, "remote", "set-head", "origin", "main")
    return work


def write_rules(repo, obj, commit=False):
    (repo / hr.RULES_FILENAME).write_text(json.dumps(obj))
    if commit:
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "rules")
        git(repo, "push", "-q", "origin", "main")


# --------------------------------------------------------------- resolution

def test_no_rules_file_yields_defaults(repo):
    cfg = hr.resolve_repo(str(repo))
    assert cfg["auto_merge"] == hr.DEFAULTS["auto_merge"]
    assert cfg["headless_permission_mode"] == "acceptEdits"
    assert cfg["loops"]["issue_executor"] is False
    assert "defaults" in cfg["_rules_from"]


def test_detects_plumbing_from_the_checkout(repo):
    cfg = hr.resolve_repo(str(repo))
    assert cfg["name"] == "myrepo"
    assert cfg["default_branch"] == "main"
    assert cfg["executor_pr_base"] == "main"      # falls back to default branch
    assert cfg["path"] == str(repo)


def test_rules_file_overrides_defaults(repo):
    write_rules(repo, {"auto_merge": "all_green"})
    cfg = hr.resolve_repo(str(repo))
    assert cfg["auto_merge"] == "all_green"
    assert cfg["_rules_from"].endswith(hr.RULES_FILENAME)


def test_nested_blocks_merge_rather_than_replace(repo):
    """Setting one reviewer must not wipe the others, or a repo enabling
    sonarqube would silently lose its claude and codex reviewers."""
    write_rules(repo, {"reviewers": {"sonarqube": {"enabled": True}},
                       "loops": {"issue_executor": True}})
    cfg = hr.resolve_repo(str(repo))
    assert cfg["reviewers"]["sonarqube"]["enabled"] is True
    assert cfg["reviewers"]["claude"]["enabled"] is True       # survived
    assert cfg["reviewers"]["claude"]["model"] == "opus"       # two levels deep
    assert cfg["loops"]["issue_executor"] is True
    assert cfg["loops"]["dependabot_lander"] is False          # survived


def test_detected_fields_cannot_be_spoofed_by_the_rules_file(repo):
    """The checkout in front of us is the authority on what/where it is — a rules
    file naming a different repo must not redirect `gh --repo` at it.

    (github isn't compared to a literal here: this fixture's origin is a local
    bare repo, so the detected owner/name is fixture-shaped. What matters is that
    it comes from the remote and not from the file.)"""
    write_rules(repo, {"path": "/etc", "github": "evil/repo", "default_branch": "attacker"})
    cfg = hr.resolve_repo(str(repo))
    assert cfg["path"] == str(repo)
    assert cfg["github"] != "evil/repo"
    assert cfg["github"] == hr.detect_github(repo)
    assert cfg["default_branch"] == "main"


def test_bad_json_fails_loudly(repo):
    (repo / hr.RULES_FILENAME).write_text("{not json")
    with pytest.raises(SystemExit):
        hr.resolve_repo(str(repo))


# ------------------------------------------------- the two-ref trust boundary

def test_unattended_ignores_the_working_tree(repo):
    """The whole point: an uncommitted (or PR-branch-only) escalation is invisible
    to the unattended runner, which sees only what is on the default branch."""
    write_rules(repo, {"headless_permission_mode": "bypassPermissions",
                       "loops": {"dependabot_lander": True}}, commit=False)

    interactive = hr.resolve_repo(str(repo), from_default_branch=False)
    assert interactive["headless_permission_mode"] == "bypassPermissions"

    unattended = hr.resolve_repo(str(repo), from_default_branch=True)
    assert unattended["headless_permission_mode"] == "acceptEdits"     # the default
    assert unattended["loops"]["dependabot_lander"] is False
    assert "origin/main" in unattended["_rules_from"]


def test_unattended_honours_committed_rules(repo):
    write_rules(repo, {"loops": {"dependabot_lander": True}}, commit=True)
    cfg = hr.resolve_repo(str(repo), from_default_branch=True)
    assert cfg["loops"]["dependabot_lander"] is True
    assert cfg["_rules_from"] == "origin/main"


def test_a_pr_branch_cannot_escalate_itself(repo):
    """Concretely the dependabot case: rules committed on a side branch, and
    that branch checked out, must not change what an unattended run honours."""
    write_rules(repo, {"loops": {"dependabot_lander": True}}, commit=True)
    git(repo, "checkout", "-q", "-b", "dependabot/pip/evil")
    write_rules(repo, {"loops": {"dependabot_lander": True},
                       "auto_merge": "all_green",
                       "headless_permission_mode": "bypassPermissions"}, commit=False)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "escalate")

    cfg = hr.resolve_repo(str(repo), from_default_branch=True)
    assert cfg["auto_merge"] != "all_green"
    assert cfg["headless_permission_mode"] == "acceptEdits"


def test_env_var_selects_unattended_mode(repo, monkeypatch):
    write_rules(repo, {"auto_merge": "all_green"}, commit=False)
    monkeypatch.setenv("HARNESS_UNATTENDED", "1")
    assert hr.unattended() is True
    assert hr.resolve_repo(str(repo))["auto_merge"] != "all_green"


# ----------------------------------------------------------------- lookup

def test_find_repo_by_path_name_and_cwd(repo, tmp_path, monkeypatch):
    assert hr.find_repo(str(repo)) == repo

    monkeypatch.setattr(hr, "REPO_ROOT", tmp_path)
    assert hr.find_repo("myrepo") == repo

    monkeypatch.chdir(repo)
    assert hr.find_repo(None) == repo


def test_find_repo_from_a_subdirectory(repo, monkeypatch):
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert hr.find_repo(None) == repo


def test_unknown_name_and_non_repo_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(hr, "REPO_ROOT", tmp_path)
    with pytest.raises(hr.RepoNotFound):
        hr.find_repo("nope")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(hr.RepoNotFound):
        hr.find_repo(str(plain))


def test_repo_without_origin_is_refused(tmp_path):
    """The harness addresses repos via `gh --repo owner/name`; no remote, no run."""
    p = tmp_path / "local"
    p.mkdir()
    git(p, "init", "-q", "-b", "main")
    git(p, "config", "user.email", "t@example.com")
    git(p, "config", "user.name", "T")
    (p / "f").write_text("x")
    git(p, "add", "-A")
    git(p, "commit", "-qm", "c")
    with pytest.raises(SystemExit):
        hr.resolve_repo(str(p))


# ---------------------------------------------------------------- discovery

def test_discover_finds_only_repos_with_a_rules_file(repo, tmp_path):
    (tmp_path / "other").mkdir()
    write_rules(repo, {"loops": {"dependabot_lander": True}})
    found = hr.discover(tmp_path)
    assert repo in found
    assert tmp_path / "other" not in found


def test_discover_on_a_missing_root_is_empty(tmp_path):
    assert hr.discover(tmp_path / "nope") == []


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:acme/thing.git", "acme/thing"),
    ("https://github.com/acme/thing.git", "acme/thing"),
    ("https://github.com/acme/thing", "acme/thing"),
])
def test_detect_github_url_forms(repo, url, expected):
    git(repo, "remote", "set-url", "origin", url)
    assert hr.detect_github(repo) == expected


# ------------------------------------------------------------------ .env

def test_read_dotenv_forms(tmp_path):
    (tmp_path / ".env").write_text(
        "# a comment\n"
        "\n"
        "SONARQUBE_TOKEN=plain-value\n"
        "export EXPORTED=via-export\n"
        'DQ="double quoted"\n'
        "SQ='single quoted'\n"
        "EMPTY=\n"
        "  SPACED  =  padded  \n"
        "no_equals_here\n"
    )
    d = hr.read_dotenv(tmp_path)
    assert d["SONARQUBE_TOKEN"] == "plain-value"
    assert d["EXPORTED"] == "via-export"
    assert d["DQ"] == "double quoted"
    assert d["SQ"] == "single quoted"
    assert d["EMPTY"] == ""
    assert d["SPACED"] == "padded"
    assert "no_equals_here" not in d


def test_read_dotenv_keeps_hash_inside_a_value(tmp_path):
    """A '#' is a comment only at the start of a line. Truncating a credential
    mid-value at a '#' is a miserable failure to debug."""
    (tmp_path / ".env").write_text("TOKEN=abc#def=ghi\n")
    assert hr.read_dotenv(tmp_path)["TOKEN"] == "abc#def=ghi"


def test_read_dotenv_missing_file_is_empty(tmp_path):
    assert hr.read_dotenv(tmp_path) == {}


def test_dotenv_is_tracked(repo):
    (repo / ".env").write_text("TOKEN=x\n")
    assert hr.dotenv_is_tracked(repo) is False       # untracked
    git(repo, "add", "-f", ".env")
    git(repo, "commit", "-qm", "oops")
    assert hr.dotenv_is_tracked(repo) is True        # committed — a real leak

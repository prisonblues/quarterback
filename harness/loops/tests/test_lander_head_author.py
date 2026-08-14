"""The lander's idempotency guard depends on actually learning the tip author.

fix_red opens a worktree and turns an agent loose on a red Dependabot branch. It
is meant to run at most once per branch: if the tip is no longer a Dependabot
commit, a fix has already been attempted and the PR is left for a human. That
guard reads head_commit_author, so a lookup that always returns "" doesn't
degrade the guard — it removes it, and the loop the docstring promises to avoid
runs on every tick.

The original call routed through gh(), which appends `--repo`. `gh api` takes the
repo in its URL path and rejects that flag, so every lookup raised
CalledProcessError and was swallowed. These tests pin both halves of the fix: the
flag is gone, and a bare (non-JSON) login survives the round trip.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lander  # noqa: E402


def _fake_run(captured, stdout="", returncode=0):
    def run(cmd, **kwargs):
        captured.append(cmd)
        if returncode:
            raise subprocess.CalledProcessError(returncode, cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    return run


def test_the_repo_is_in_the_url_not_a_flag(monkeypatch):
    """`gh api --repo` is rejected outright, which is what made this fail open."""
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run(calls, stdout="dependabot[bot]\n"))

    author = lander.head_commit_author("acme/thing", "dependabot/pip/urllib3-2.5.0")

    assert author == "dependabot[bot]"
    cmd = calls[0]
    assert "--repo" not in cmd
    assert "repos/acme/thing/commits/dependabot/pip/urllib3-2.5.0" in cmd


def test_a_bare_login_is_not_parsed_as_json(monkeypatch):
    """`-q .author.login` prints a raw login. Feeding that to json.loads raises —
    so the fix has to read stdout directly, not via the JSON helper."""
    monkeypatch.setattr(subprocess, "run", _fake_run([], stdout="rich\n"))
    assert lander.head_commit_author("acme/thing", "main") == "rich"


def test_a_failed_lookup_still_fails_open(monkeypatch):
    """Fail-open is deliberate: an unknown author must not strand a PR the lander
    could still act on. It just has to mean "could not tell", not "always"."""
    monkeypatch.setattr(subprocess, "run", _fake_run([], returncode=1))
    assert lander.head_commit_author("acme/thing", "main") == ""

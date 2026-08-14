"""The SonarQube fallback: which branch it reads, how it survives a mixed
qualifier list, and what it does when the base was never analysed.

All three cases are regressions found live on lexray (2026-08-14), where the
reviewer was returning nothing and reading as if it worked.
"""

import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402

SONAR = {"host": "https://sonarcloud.io", "organization": "acme",
         "project_key": "acme_thing", "token_env": "SONARQUBE_TOKEN"}

CHANGED = {"apps/a.py": {10, 11}, "tests/test_a.py": {5}}

# The PR under test. `head` is deliberately a branch Sonar has NOT analysed in
# most of these, so they exercise tier 3; the tier-2 tests add it explicitly.
PR = {"number": 1, "base": "test", "head": "feat/x", "head_sha": "abc123"}


def _http(code):
    return urllib.error.HTTPError("u", code, "no", {}, None)


def _issue(component, line, msg="smell"):
    return {"component": f"acme_thing:{component}", "line": line,
            "message": msg, "severity": "MAJOR", "rule": "python:S1"}


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("SONARQUBE_TOKEN", "t")


def install_api(monkeypatch, handler):
    """Replace the module's urlopen with a handler over the request URL.

    Returns the list of URLs called, in order, so a test can assert on what was
    asked as well as what came back.
    """
    calls = []

    class Resp:
        def __init__(self, payload):
            self._p = json.dumps(payload).encode()

        def read(self):
            return self._p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, **kw):
        url = req.full_url
        calls.append(url)
        out = handler(url)
        if isinstance(out, urllib.error.HTTPError):
            raise out
        return Resp(out)

    monkeypatch.setattr(panel.urllib.request, "urlopen", fake)
    return calls


def test_fallback_reads_the_prs_base_not_the_default_branch(monkeypatch):
    """The whole point. `test` is lexray's integration branch and `main` lags it
    by a release train — measured on PR #1625, the default branch returned 33
    issues of which 0 touched a changed line, and the base returned 2 that did.
    A branch the PR isn't based on doesn't add noise, it returns silence."""
    def handler(url):
        if "qualitygates" in url:
            return _http(404)          # PR never scanned
        if "project_branches" in url:
            return {"branches": [{"name": "main", "isMain": True},
                                 {"name": "test", "isMain": False}]}
        return {"issues": [_issue("apps/a.py", 10)]}

    calls = install_api(monkeypatch, handler)
    gate, hard, soft, note = panel.review_sonarqube(SONAR, PR, CHANGED)

    assert gate == "no-pr-analysis"
    assert note is None
    assert [f.line for f in soft] == [10]
    assert any("branch=test" in c for c in calls), calls


def test_unanalysed_base_falls_back_and_says_so(monkeypatch):
    """An unanalysed branch answers 200/total=0 rather than erroring, so an
    epic-stacked PR would silently report a clean bill of health. Demote to the
    default branch and NAME the demotion — a silent zero is indistinguishable
    from a clean PR, which is the one thing this must never produce."""
    def handler(url):
        if "qualitygates" in url:
            return _http(404)
        if "project_branches" in url:
            return {"branches": [{"name": "main", "isMain": True}]}
        return {"issues": []}

    calls = install_api(monkeypatch, handler)
    gate, hard, soft, note = panel.review_sonarqube(SONAR, {**PR, 'base': 'epic/1516-packages'}, CHANGED)

    assert gate == "no-pr-analysis"
    assert "epic/1516-packages" in note and "main" in note
    assert not any("branch=" in c for c in calls), calls


def test_mixed_qualifier_refusal_retries_per_file(monkeypatch):
    """Sonar refuses a componentKeys list mixing qualifiers ('All components
    must have the same qualifier, found UTS,FIL') — which every PR touching both
    sources and tests does, i.e. nearly every reviewable PR. A path's qualifier
    can't be known client-side, so the split is discovered from the refusal."""
    def handler(url):
        if "qualitygates" in url:
            return _http(404)
        if "project_branches" in url:
            return {"branches": [{"name": "test", "isMain": True}]}
        if "apps%2Fa.py" in url and "tests%2Ftest_a.py" in url:
            return _http(400)          # the mixed-qualifier refusal
        if "apps%2Fa.py" in url:
            return {"issues": [_issue("apps/a.py", 10)]}
        return {"issues": [_issue("tests/test_a.py", 5)]}

    install_api(monkeypatch, handler)
    gate, hard, soft, note = panel.review_sonarqube(SONAR, PR, CHANGED)

    assert gate == "no-pr-analysis"
    assert sorted(f.file for f in soft) == ["apps/a.py", "tests/test_a.py"]
    assert note is None


def test_partially_unreadable_files_are_counted_not_hidden(monkeypatch):
    """A partial answer beats no answer, but the report must say how partial."""
    def handler(url):
        if "qualitygates" in url:
            return _http(404)
        if "project_branches" in url:
            return {"branches": [{"name": "test", "isMain": True}]}
        if "apps%2Fa.py" in url and "tests%2Ftest_a.py" in url:
            return _http(400)
        if "apps%2Fa.py" in url:
            return {"issues": [_issue("apps/a.py", 10)]}
        return _http(500)              # this one file stays unreadable

    install_api(monkeypatch, handler)
    gate, hard, soft, note = panel.review_sonarqube(SONAR, PR, CHANGED)

    assert [f.file for f in soft] == ["apps/a.py"]
    assert "1/2 files unreadable" in note


def test_only_lines_the_pr_adds_survive(monkeypatch):
    """Pre-existing issues elsewhere in a touched file are not this PR's problem."""
    def handler(url):
        if "qualitygates" in url:
            return _http(404)
        if "project_branches" in url:
            return {"branches": [{"name": "test", "isMain": True}]}
        return {"issues": [_issue("apps/a.py", 10),      # added by the PR
                           _issue("apps/a.py", 900)]}    # pre-existing
    install_api(monkeypatch, handler)
    _, _, soft, _ = panel.review_sonarqube(SONAR, PR, CHANGED)
    assert [f.line for f in soft] == [10]


def test_pr_scanned_path_is_the_hard_gate_and_ignores_base(monkeypatch):
    """When the PR HAS its own analysis nothing above applies: the gate is real
    and its issues are hard findings."""
    def handler(url):
        if "qualitygates" in url:
            return {"projectStatus": {"status": "ERROR"}}
        return {"issues": [_issue("apps/a.py", 10)]}

    calls = install_api(monkeypatch, handler)
    gate, hard, soft, note = panel.review_sonarqube(SONAR, PR, CHANGED)

    assert gate == "ERROR" and len(hard) == 1 and soft == []
    assert not any("project_branches" in c for c in calls), calls


# ---------------------------------------------------------------------------
# Tier 2: the head branch's own analysis
# ---------------------------------------------------------------------------
# Exists because PR analysis is not always reachable. Where the SonarCloud
# ORGANIZATION is bound to another DevOps platform — lexray's is bound to Azure
# DevOps — a GitHub PR key cannot be resolved at all ("Could not find the
# pullrequest with key '1611'"), and `sonar.branch.name` is the only way to get
# an analysis of the change before it merges.


def test_head_branch_analysis_is_a_hard_gate(monkeypatch):
    """A branch analysis judges this change's new code against its base, so its
    gate is a real gate — unlike the base branch's, which describes all of the
    base and would fail every PR."""
    def handler(url):
        if "qualitygates" in url:
            return _http(404)                      # no PR analysis
        if "project_branches" in url:
            return {"branches": [
                {"name": "test", "isMain": True},
                {"name": "feat/x", "commit": {"sha": "abc123"},
                 "status": {"qualityGateStatus": "ERROR"}},
            ]}
        return {"issues": [_issue("apps/a.py", 10), _issue("apps/a.py", 900)]}

    calls = install_api(monkeypatch, handler)
    gate, hard, soft, note = panel.review_sonarqube(SONAR, PR, CHANGED)

    assert gate == "ERROR", 'the head branch gate must be reported as the gate'
    # Hard, not soft — and still scoped to the lines the PR adds, because a
    # branch analysis reports the WHOLE branch.
    assert [f.line for f in hard] == [10]
    assert soft == [] and note is None
    assert any("branch=feat%2Fx" in c for c in calls), calls


def test_a_stale_head_branch_analysis_is_declined_not_trusted(monkeypatch):
    """Branch analyses persist and a push does NOT supersede them.

    So the analysis sitting on `feat/x` may predate the commit under review by
    any number of pushes, and using it would gate confidently on code that is no
    longer there — the worst available outcome, because it looks authoritative.
    Verified against the PR's head SHA and declined outright when they disagree,
    rather than used with a caveat nobody reads.
    """
    def handler(url):
        if "qualitygates" in url:
            return _http(404)
        if "project_branches" in url:
            return {"branches": [
                {"name": "test", "isMain": True},
                {"name": "feat/x", "commit": {"sha": "0ldc0mmit"},
                 "status": {"qualityGateStatus": "OK"}},
            ]}
        return {"issues": [_issue("apps/a.py", 10)]}

    install_api(monkeypatch, handler)
    gate, hard, soft, note = panel.review_sonarqube(SONAR, PR, CHANGED)

    assert gate == "no-pr-analysis", 'a stale branch analysis must not be the gate'
    assert hard == []
    assert 'stale' in note and '0ldc0mm' in note and 'abc123' in note
    assert [f.line for f in soft] == [10], 'and it still falls through to tier 3'


def test_an_unanalysed_head_branch_falls_straight_through(monkeypatch):
    """The ordinary case before anyone has scanned the branch: no note, no fuss,
    just the base-branch soft findings."""
    def handler(url):
        if "qualitygates" in url:
            return _http(404)
        if "project_branches" in url:
            return {"branches": [{"name": "test", "isMain": True}]}
        return {"issues": [_issue("apps/a.py", 10)]}

    install_api(monkeypatch, handler)
    gate, hard, soft, note = panel.review_sonarqube(SONAR, PR, CHANGED)

    assert gate == "no-pr-analysis" and hard == [] and note is None
    assert [f.line for f in soft] == [10]

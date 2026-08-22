"""Tests for `scripts/closing_refs.py` and the job that runs it.

The subject is a contradiction between two structured facts — what GitHub parsed out of a
pull request body, and what the branch's own commits say — so the tests that matter are the
ones where being wrong is silent:

  * `test_the_verdict_agrees_with_real_pull_requests` — the rule against the commit lines and
    closing lists of real pull requests, including the two incidents #374 was filed over and
    the third the survey for it turned up. A rule tested only on invented commit messages is
    a rule tested against its author's idea of the repo.
  * `test_an_issue_no_commit_mentions_is_warned_about_rather_than_refused` — refusing these
    would be right most of the time, and most of the time is how a check gets switched off.
  * `test_this_pull_requests_own_first_body_is_the_case_the_warning_exists_for` — that gap is
    not theoretical: the first body of the PR adding this file closed three issues it did not
    mean to, and this check passed it.
  * `test_a_range_git_cannot_walk_is_refused_rather_than_passed` — a depth-1 checkout has no
    base branch, so the range is empty, and an empty range is indistinguishable from a branch
    that referenced nothing, which this check reads as consent.
  * `test_a_graphql_error_is_a_refusal_rather_than_an_empty_list` — `data: null` beside
    `errors` read as "closes nothing" is the gate reporting green because it failed.
  * `test_merging_the_base_in_does_not_lend_the_branch_someone_elses_keyword` — a
    fork-relative range carries the base's commits back through, and one of those saying
    `Fixes #N` would pass this branch on a sentence it did not write.
  * `test_a_commit_quoting_the_refusal_does_not_pass_it` — the refusal ends with the
    `Fixes #N` that would clear it, so the likeliest message this branch will ever write
    contains that line, and reading it out of quoted prose is consent granted by a paste.
  * `test_a_truncated_page_is_refused_rather_than_read_as_the_whole_list` — a short page and
    a complete list are the same shape, and the difference is issues never compared.
  * `test_the_job_re_runs_when_the_body_is_edited` — `closingIssuesReferences` is derived
    from the body, and one of the two remedies the refusal names IS a body edit. Without
    `edited` the job stays red after the correct fix.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# `scripts/` is a directory of standalone tools, not an importable package.
_SPEC = importlib.util.spec_from_file_location(
    "closing_refs", REPO_ROOT / "scripts" / "closing_refs.py")
cr = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cr
_SPEC.loader.exec_module(cr)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


def commit(repo: Path, message: str) -> None:
    # A file per message, so `test_merging_the_base_in_…` can merge two branches without the
    # fixture inventing a conflict the subject has nothing to do with.
    name = hashlib.sha1(message.encode()).hexdigest()[:12] + ".txt"
    (repo / name).write_text(message.split("\n")[0], encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch, tmp_path: Path) -> None:
    """No developer's global git config reaches these repos — `test_changelog_fragments.py`'s
    reason: `commit.gpgSign=true` fails every commit here with a signing error, and
    `GIT_CONFIG_*` is inherited by the git subprocesses the tool itself runs."""
    empty = tmp_path / "gitconfig-none"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)


@pytest.fixture
def branched(tmp_path: Path) -> Path:
    """A repo with `work` forked from `main`, both local refs.

    Local branches rather than remote-tracking ones, for `test_release_stamp.py`'s reason:
    `--onto` takes any ref, and a local one saves the fixture a second repo to clone from.
    """
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    commit(root, "base")
    git(root, "checkout", "-q", "-b", "work")
    return root


def graphql(*issues: tuple[int, str], total: int | None = None) -> str:
    """GitHub's answer, in the envelope the GraphQL API really returns it in.

    `totalCount` alongside the nodes, because the documented query asks for it: a connection
    that returned a partial page looks exactly like a complete short one.
    """
    return json.dumps({"data": {"repository": {"pullRequest": {
        "closingIssuesReferences": {
            "totalCount": len(issues) if total is None else total,
            "nodes": [{"number": n, "state": s} for n, s in issues]}}}}})


def check(repo: Path, closes: str, *args: str, onto: str = "main", branch: str = "work") -> int:
    path = repo / "closes.json"
    path.write_text(closes, encoding="utf-8")
    return cr.main(["check", "--repo", str(repo), "--onto", onto, "--branch", branch,
                    "--closes", str(path), *args])


# --- the contradiction, which is the feature -------------------------------------------

def test_a_branch_that_only_refs_an_issue_the_merge_would_close_is_refused(branched, capsys):
    """#372's exact shape: commit `Refs #371`, and #371 on GitHub's closing list because the
    body's `**This does not close #371**` matched the literal `close #371`."""
    commit(branched, "feat(dash): the button\n\nRefs #371\n")
    assert check(branched, graphql((371, "OPEN"))) == 2
    err = capsys.readouterr().err
    assert "#371" in err
    assert "Refs #371" in err
    assert "Fixes #371" in err  # the refusal names the one-line remedy


def test_a_closing_keyword_anywhere_in_the_range_settles_it(branched, capsys):
    """A branch whose first commit refs an issue and whose last one closes it has finished
    the work. The two are not in conflict and refusing that is refusing a correct branch."""
    commit(branched, "feat: groundwork\n\nRefs #267\n")
    commit(branched, "feat: the rest\n\nFixes #267\n")
    assert check(branched, graphql((267, "OPEN"))) == 0
    assert "no commit on this branch says otherwise" in capsys.readouterr().out


def test_an_issue_no_commit_mentions_is_warned_about_rather_than_refused(branched, capsys):
    """The ordinary pull request, and the reason this check survives: #207 closed #174 with
    the keyword in the body alone and was right to. It is still said out loud — this is the
    gap, and the body that trips it is the body most likely to be written here."""
    commit(branched, "feat: a thing\n\nNo trailer at all.\n")
    assert check(branched, graphql((55, "OPEN"))) == 0
    out = capsys.readouterr()
    assert "no commit on this branch says otherwise" in out.out
    assert out.err.startswith("unclaimed: #55")


def test_this_pull_requests_own_first_body_is_the_case_the_warning_exists_for(branched, capsys):
    """It closed #63, #165 and #371 alongside the #374 it meant, because it was a body about
    closing keywords and quoted them next to real numbers, and every commit said `Fixes #374`.
    Silence passed it. #165 was already closed, so three of the four are the live part."""
    commit(branched, "ci(closing-refs): refuse a PR that closes an issue it only Refs\n\n"
                     "Fixes #374\n")
    closing = ((63, "OPEN"), (165, "CLOSED"), (371, "OPEN"), (374, "OPEN"))
    assert check(branched, graphql(*closing)) == 0
    err = capsys.readouterr().err
    assert err.startswith("unclaimed: #63, #371")   # #374 is claimed, #165 already closed
    assert "reword it" in err


def test_an_issue_the_branch_claims_is_not_reported_as_unclaimed(branched, capsys):
    commit(branched, "feat: a thing\n\nFixes #55\n")
    assert check(branched, graphql((55, "OPEN"))) == 0
    assert capsys.readouterr().err == ""


def test_a_pull_request_that_closes_nothing_passes_without_reading_a_commit(branched, capsys):
    commit(branched, "chore: tidy\n\nRefs #12\n")
    assert check(branched, graphql()) == 0
    assert "closes no issue" in capsys.readouterr().out


def test_an_issue_already_closed_is_not_refused(branched):
    """Merging closes nothing there, so there is no event to prevent and a red check would be
    about bookkeeping rather than about an issue that is going to disappear."""
    commit(branched, "feat: a thing\n\nRefs #371\n")
    assert check(branched, graphql((371, "CLOSED"))) == 0


def test_negation_in_a_commit_body_is_not_read_as_a_closing_keyword(branched, capsys):
    """The sentence at the top of #372's body, in the commit this time. It is prose, so it
    matches neither pattern — and the `Refs #371` below it still refuses the branch. Reading
    that sentence is what failed; a reference LINE is what is read instead."""
    commit(branched, "feat(dash): the button\n\n"
                     "**This does not close #371** — see the bottom.\n\nRefs #371\n")
    assert check(branched, graphql((371, "OPEN"))) == 2
    assert "#371" in capsys.readouterr().err


def test_only_the_leading_run_of_numbers_on_a_reference_line_is_read(branched):
    """`Refs #165, #223, #237, #236` is four references — PR #243's line, all four real. A
    number mentioned in the prose after them is not, because the assertion this check makes
    on the strength of a reference is a refusal."""
    message = "feat: x\n\nRefs #63 — the actor half is not built, see #100 for why\n"
    assert cr.references(message, cr._REFING_LINE) == {63: "Refs #63"}
    four = "Refs #165, #223, #237, #236"
    assert set(cr.references(f"feat: x\n\n{four}\n", cr._REFING_LINE)) == {165, 223, 237, 236}


def test_closing_and_fixing_are_not_closing_keywords(branched):
    """GitHub's set is close/closes/closed, fix/fixes/fixed, resolve/resolves/resolved and
    nothing else. A stem pattern sweeping in `closing` would read a branch as agreeing with
    GitHub where GitHub parsed nothing — a false pass, the direction that costs an issue."""
    assert cr.references("Closing #5\n", cr._CLOSING_LINE) == {}
    assert cr.references("Fixing #5\n", cr._CLOSING_LINE) == {}
    for word in ("Close", "Closes", "Closed", "Fix", "Fixes", "Fixed",
                 "Resolve", "Resolves", "Resolved"):
        assert cr.references(f"{word} #5\n", cr._CLOSING_LINE) == {5: f"{word} #5"}


def test_a_commit_quoting_the_refusal_does_not_pass_it(branched, capsys):
    """`release_stamp.py`'s `Release-Body-Edit` comment in one sentence: the refusal is the
    likeliest text this branch will ever quote, and a `Fixes #N` read out of quoted prose
    would be consent granted by a paste. A closing line has to start at column zero — all
    ninety-nine reference lines in main's history do — while a reference line may be
    indented, because reading one of those only ever costs a red check."""
    commit(branched, "feat(dash): the button\n\n"
                     "CI said:\n"
                     "    this pull request would close an issue its own commits only\n"
                     "    reference:\n"
                     "        Refs #371\n"
                     "    it really does close the issue? say so on a commit:\n"
                     "        Fixes #371\n")
    assert check(branched, graphql((371, "OPEN"))) == 2
    assert "#371" in capsys.readouterr().err


def test_merging_the_base_in_does_not_lend_the_branch_someone_elses_keyword(branched):
    """A fork-relative range carries the base's own commits back through the merge, and one
    of those saying `Fixes #N` would pass this branch on a sentence it did not write. The
    range is `onto..branch`, which is exactly the commits the pull request adds."""
    git(branched, "checkout", "-q", "main")
    commit(branched, "feat(main): somebody else's work\n\nFixes #371\n")
    git(branched, "checkout", "-q", "work")
    commit(branched, "feat: the button\n\nRefs #371\n")
    git(branched, "merge", "-q", "--no-edit", "main")
    assert check(branched, graphql((371, "OPEN"))) == 2


def test_a_range_git_cannot_walk_is_refused_rather_than_passed(branched, capsys):
    """What a depth-1 CI checkout looks like: the base branch is not in the clone. An empty
    commit range and a branch that referenced nothing are the same shape, and this check
    reads the second as consent — so it must not be reached by way of the first."""
    commit(branched, "feat: a thing\n\nRefs #371\n")
    assert check(branched, graphql((371, "OPEN")), onto="origin/main") == 2
    assert "fetch-depth: 0" in capsys.readouterr().err


def test_a_graphql_error_is_a_refusal_rather_than_an_empty_list(branched, capsys):
    """The API answers a query it could not satisfy with `data: null` beside `errors`. Read
    as "closes nothing" that is the gate reporting green because it failed."""
    commit(branched, "feat: a thing\n\nRefs #371\n")
    body = json.dumps({"data": None, "errors": [{"message": "Could not resolve to a Repository"}]})
    assert check(branched, body) == 2
    assert "unknown rather than empty" in capsys.readouterr().err


def test_a_truncated_page_is_refused_rather_than_read_as_the_whole_list(branched, capsys):
    """`first: 50` is far past anything this repo has done, but a page that came back short
    looks exactly like a complete list — and the entries it dropped are issues this check
    would then never compare, which is a gate reporting green over the part it did not read."""
    commit(branched, "feat: a thing\n\nRefs #371\n")
    assert check(branched, graphql((371, "OPEN"), total=51)) == 2
    assert "never compared" in capsys.readouterr().err


def test_an_answer_with_no_closing_references_key_is_refused(branched, capsys):
    commit(branched, "feat: a thing\n\nRefs #371\n")
    assert check(branched, json.dumps({"data": {"repository": {"pullRequest": {}}}})) == 2
    assert "will not guess from the body" in capsys.readouterr().err


def test_the_json_verdict_carries_what_github_said(branched, capsys):
    commit(branched, "feat: a thing\n\nRefs #371\n")
    assert check(branched, graphql((371, "OPEN")), "--json") == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["closes"] == [{"number": 371, "state": "OPEN"}]
    assert "#371" in payload["refusal"]


# --- real pull requests ----------------------------------------------------------------
#
# The reference lines are the ones the commits really carry, and the closing lists are what
# `closingIssuesReferences` held at the time. #372 and #363 were both repaired before merging,
# so today's API returns an empty list for each; the state recorded here is the one the merge
# button would have acted on. A rule tested only against invented messages is a rule tested
# against its author's idea of the repo.
REAL_PRS: dict[str, tuple[list[str], list[tuple[int, str]], int]] = {
    "#372 — `**This does not close #371**`, and #371 on the list all the same": (
        ["feat(dash): the ⚒ starts a session through qb-start\n\n"
         "Nothing automatic pulls `qb-start` yet: a SessionEnd hook and a cron floor are\n"
         "still deliberately unbuilt.\n\nRefs #371\n",
         "chore(release): v2.97 — the loop gains a beginning\n\n"
         "Claude-Session: https://claude.ai/code/session_018\n"],
        [(371, "OPEN")],
        2,
    ),
    "#243 — closed #165 one second after landing, on a commit that said Refs": (
        ["feat(panel): make thoroughness-vs-convergence a set of review_panel settings\n\n"
         "The panel had one behaviour and no dials (#165). Seven `review_panel.*`\n"
         "settings now let a repo trade thoroughness against convergence deliberately.\n\n"
         "Refs #165, #223, #237, #236\n"],
        [(165, "OPEN")],
        2,
    ),
    "#363 as it was amended — commit repaired, body not": (
        ["feat(loops): an issue watcher that reads the tracker and mostly declines\n\n"
         "Refs #63 — the actor half (a trigger through qb-start) is deliberately not\n"
         "built, so the issue stays open for it.\n\n"
         "Claude-Session: https://claude.ai/code/session_018\n"],
        [(63, "OPEN")],
        2,
    ),
    "#363 as it was OPENED — the honest limit: both facts agreed": (
        ["feat(loops): an issue watcher that reads the tracker and mostly declines\n\n"
         "Fixes #63\n"],
        [(63, "OPEN")],
        0,
    ),
    "#373 — an ordinary pull request that closes its issue": (
        ["feat(migrations): a new revision id is opaque\n\n"
         "Verified with a full `alembic downgrade base && alembic upgrade head`.\n\n"
         "Fixes #341\n",
         "chore(release): v2.98\n\nClaude-Session: https://claude.ai/code/session_018\n"],
        [(341, "OPEN")],
        0,
    ),
    "#376 — four commits, the closing keyword on the first of them": (
        ["feat(bump): something acts on a stale harness\n\nFixes #267\n",
         "fix(bump): four defects Codex found\n",
         "docs(bump): say that only a root input counts\n",
         "chore(release): v2.99\n\nClaude-Session: https://claude.ai/code/session_018\n"],
        [(267, "OPEN")],
        0,
    ),
    "#370 — an ordinary pull request that closes its issue": (
        ["feat(caps): a round cap and a spend ceiling\n\n"
         "It changes nothing until somebody writes a number.\n\nFixes #55\n",
         "chore(release): v2.96\n\nClaude-Session: https://claude.ai/code/session_018\n"],
        [(55, "OPEN")],
        0,
    ),
}


@pytest.mark.parametrize("case", REAL_PRS, ids=list(REAL_PRS))
def test_the_verdict_agrees_with_real_pull_requests(branched, case):
    messages, closing, expected = REAL_PRS[case]
    for message in messages:
        commit(branched, message)
    assert check(branched, graphql(*closing)) == expected, case


def test_363_as_opened_is_a_stated_limit_rather_than_an_oversight():
    """Its commit said `Fixes #63` and GitHub would have closed #63, so the two facts this
    reads AGREED — the contradiction was against the PR's prose, and prose is what does not
    work. The docstring says so, because a limit nobody wrote down is found by trusting the
    check with a case it never covered."""
    assert "#363's original state" in cr.__doc__
    assert "AGREED" in cr.__doc__


# ---------------------------------------------------------------------------
# the job
# ---------------------------------------------------------------------------
#
# Found by what it RUNS rather than by its name, for `test_changelog_fragments.py`'s reason:
# a renamed job would otherwise skip these silently.

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "closing-refs.yml"
LANDING = REPO_ROOT / "harness" / "commands" / "fix-and-land.md"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job() -> dict:
    jobs = _workflow()["jobs"]
    running = [job for job in jobs.values()
               if any("closing_refs.py check" in str(step.get("run", ""))
                      for step in job.get("steps", []))]
    assert len(running) == 1, (
        f"{len(running)} jobs run `closing_refs.py check`; the guard is one job and these "
        "tests assert about it")
    return running[0]


def test_the_job_re_runs_when_the_body_is_edited():
    """`closingIssuesReferences` is derived from the pull request BODY, and one of the two
    remedies the refusal names is a body edit — which is not `opened`, `synchronize` or
    `reopened`. Without `edited` the job stays red after the correct fix, and a gate that
    does that is a gate somebody switches off."""
    # PyYAML resolves a bare `on:` key to the boolean True — YAML 1.1's fault.
    wf = _workflow()
    trigger = wf.get("on", wf.get(True))
    assert set(trigger["pull_request"]["types"]) == {
        "opened", "edited", "synchronize", "reopened"}


def test_the_job_lives_outside_tests_yml():
    """Because of the trigger above, not because of tidiness: `types:` applies to a whole
    `on:` block, so putting this job in `tests.yml` would re-run three fifteen-minute suites
    on every pull request body edit, for a question none of them are asking."""
    assert WORKFLOW.exists()
    tests_yml = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "closing_refs.py" not in tests_yml


def test_the_job_checks_out_the_whole_history():
    """`fetch-depth: 0`, as on `frozen`, `migration-heads` and `changelog`. The range judged
    is `origin/<base>..HEAD` and a depth-1 checkout has no base branch at all."""
    checkouts = [s for s in _job()["steps"] if "actions/checkout" in str(s.get("uses"))]
    assert checkouts, "the job has no checkout step"
    assert all(s.get("with", {}).get("fetch-depth") == 0 for s in checkouts)


def test_the_job_raises_a_warning_for_an_issue_no_commit_claims():
    """A `::warning::`, not a line in a green log: the checks panel is where this is read,
    and a green job's output is not. It must not be an `::error::` — that is the refusal this
    case deliberately is not."""
    steps = "\n".join(str(s.get("run", "")) for s in _job()["steps"])
    assert "unclaimed: " in steps
    assert "::warning::" in steps


def test_the_job_may_read_pull_requests():
    """The GraphQL query needs it, and the workflow's default is `contents: read` alone —
    without this the query returns errors and the job is red on every pull request."""
    assert _workflow()["permissions"]["pull-requests"] == "read"


def test_untrusted_values_reach_the_script_as_data():
    """A base ref and a repository name are written by whoever opened the pull request, and
    `${{ }}` substitution happens before bash sees the line — so a value spliced into a
    `run:` body is that person's text executing on the runner. Through `env:` it is data."""
    for step in _job()["steps"]:
        assert "${{" not in str(step.get("run", ""))
    envs = "".join(str(s.get("env", "")) for s in _job()["steps"])
    assert "github.base_ref" in envs
    assert "github.event.pull_request.number" in envs


def test_the_landing_procedure_records_the_query():
    """#374's second half, and #367's whole subject: a lander that knows to ask GitHub rather
    than grep the body is most of the value even before a check exists. The query is written
    where the landing agent reads, not only in a workflow it never opens."""
    text = LANDING.read_text(encoding="utf-8")
    assert "closingIssuesReferences" in text
    assert "gh api graphql" in text

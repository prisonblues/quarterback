"""#628: "CI has not run" and "CI cannot run" had one sentence between them.

`prisonblues/lexray#1780` was based on `fca`; that repo's `test.yml` triggers on
`main` and `test` alone, so no run could ever exist for it. The panel printed *"🚫 no
run exists for this commit — do not merge, even if the review below is clean"* on all
five rounds — an instruction nobody could carry out, because nothing the author does
to their branch makes a workflow fire on a base it does not list. So it was waived,
and then the PR was merged. **A hard gate that gets waived teaches everyone that hard
gates are waivable.**

The two states have completely different remedies: one is "wait", the other is "add
the base to the trigger list". So the round asks, once, on the one status where the
question arises — and it rides BESIDE `ci_status` rather than becoming a value of it,
because `app/ordering.py` compares that field for equality and matches
`CI_SETTLED`/`CI_NOT_APPLICABLE` as sets.

**Every ambiguity withdraws the claim.** The output of this scanner is a sentence
telling an operator that no run can EVER exist for their pull request, and a
hand-written YAML reader that misread a trigger it had never seen would say that about
a repo whose CI is working. So a directory that could not be read, a file that could
not be read, an `on:` block this reader could not find, and a repo with more workflows
than it will scan all answer "could not be established" — which is neither runnable
nor unrunnable, and is reported as itself.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh`, the seam every stub here replaces
import panel_scope  # noqa: E402  — where the scanner lives
from conftest import gh_stub  # noqa: E402

CFG = {"github": "acme/board", "path": "/nonexistent/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}


def _reader(workflows, *, listing_fails="", file_fails=""):
    """A `read(path, raw=False)` double over `{filename: yaml text}`, answering the
    two calls the scanner makes: the workflow directory listing, and each file's
    contents. `listing_fails` / `file_fails` are the "why not" a real `gh api` failure
    comes back with — the third answer, and the one most of this file is about."""
    entries = [{"name": n, "path": f".github/workflows/{n}"} for n in workflows]

    def read(path, raw=False):
        if "contents/.github/workflows?" in path:
            return ("", listing_fails) if listing_fails else (json.dumps(entries), "")
        if file_fails:
            return "", file_fails
        # `?ref=` first, THEN the last path segment: a base branch with a slash in
        # it (`release/1`) is in the query string, and splitting the other way round
        # hands this a branch name to look a workflow up by.
        name = path.split("?")[0].split("/")[-1]
        return workflows[name], ""
    return read


def _scan(workflows, base="fca", head="feat/x", **kw):
    return panel_scope.ci_unrunnable("acme/board", base, head,
                                     read=_reader(workflows, **kw))


PR_ON_MAIN = """\
name: test
on:
  pull_request:
    branches:
      - main
      - test
jobs:
  build:
    runs-on: ubuntu-latest
"""


# --------------------------------------------------------- the claim it can make

def test_a_base_no_workflow_triggers_on_is_reported_as_unrunnable():
    """#1780's own shape: every workflow was read, and none of them can fire for a
    pull request into this base. The record names the base, the reason and the remedy
    — an operator's next action is to edit a trigger list, and a message that only
    said "no run" would send them to re-run something that does not exist."""
    got, why = _scan({"test.yml": PR_ON_MAIN})
    assert why == ""
    assert got["base"] == "fca" and got["head"] == "feat/x"
    assert "no workflow in this repo can produce a run" in got["reason"]
    assert "add `fca` to the trigger list" in got["remedy"]
    assert "Waiting cannot fix this" in got["remedy"]
    assert got["workflows"] == [{"path": ".github/workflows/test.yml",
                                 "events": ["pull_request"]}]


def test_a_base_that_IS_in_the_trigger_list_withdraws_nothing_and_claims_nothing():
    """The absent run is then an absent run, which is what `ci_status: none` has
    always meant. Both halves are empty: no record, and no "could not be
    established" either."""
    assert _scan({"test.yml": PR_ON_MAIN}, base="main") == (None, "")


def test_one_workflow_that_CAN_fire_answers_for_the_whole_repo():
    """The claim is about ALL of them — "no run can exist" — so a single workflow
    that fires is a complete refutation, whatever the others say."""
    other = "name: lint\non:\n  pull_request:\n    branches: [fca]\n"
    assert _scan({"a.yml": PR_ON_MAIN, "b.yml": other}) == (None, "")


def test_a_push_trigger_is_read_against_the_PRs_own_branch_and_not_the_base():
    """`on.pull_request.branches` filters on the BASE and `on.push.branches` on the
    branch being pushed, which is the head. Reading both against the same ref would
    call a repo unrunnable while its push workflow fires on every PR branch."""
    push = "name: ci\non:\n  push:\n    branches: ['feat/*']\n"
    assert _scan({"p.yml": push}, head="feat/x") == (None, "")
    blocked, why = _scan({"p.yml": push}, head="fix/y")
    assert why == "" and blocked["workflows"][0]["events"] == ["push"]


def test_a_repo_with_no_workflows_at_all_is_not_a_trigger_list_mistake():
    """The remedy this function prints names a file to edit, and there is none. `none`
    already says what is true here — nothing mechanical has looked at this code — so
    the claim is withheld without being reported as a failure to check."""
    assert _scan({}) == (None, "")


def test_a_glob_in_the_trigger_list_is_matched_the_way_GitHub_matches_it():
    """`*` does not cross a `/` and `**` does, which is why this is not handed to
    `fnmatch` — there `release/*` would match `release/1/hotfix` and quietly turn an
    unrunnable PR into a runnable-looking one, in the direction that suppresses the
    warning."""
    star = "name: ci\non:\n  pull_request:\n    branches: ['release/*']\n"
    assert _scan({"c.yml": star}, base="release/1") == (None, "")
    deep, why = _scan({"c.yml": star}, base="release/1/hotfix")
    assert why == "" and deep is not None

    globstar = "name: ci\non:\n  pull_request:\n    branches: ['release/**']\n"
    assert _scan({"c.yml": globstar}, base="release/1/hotfix") == (None, "")


def test_an_exclusion_is_honoured_from_either_key():
    """`branches-ignore`, and a leading `!` inside `branches`, are the two ways a repo
    says "not this one" — and a list that is nothing BUT exclusions admits every
    branch it does not name."""
    ignore = "name: ci\non:\n  pull_request:\n    branches-ignore: [fca]\n"
    blocked, _why = _scan({"c.yml": ignore})
    assert blocked is not None
    assert _scan({"c.yml": ignore}, base="main") == (None, "")

    bang = "name: ci\non:\n  pull_request:\n    branches: ['!fca']\n"
    assert _scan({"c.yml": bang}, base="main") == (None, "")
    assert _scan({"c.yml": bang})[0] is not None


# ------------------------------------------------- every ambiguity fails OPEN

def test_a_workflow_directory_that_could_not_be_read_withdraws_the_claim():
    """"No workflow can run here" is a strong claim and a failed API read is no
    evidence for it. Reported rather than swallowed, because "we did not find one"
    and "we could not look" have different remedies and only one of them is a PR
    somebody may merge on the strength of."""
    got, why = _scan({"test.yml": PR_ON_MAIN}, listing_fails="HTTP 404")
    assert got is None
    assert "workflow directory could not be read" in why and "HTTP 404" in why


def test_a_listing_that_is_not_JSON_withdraws_it_too():
    """The same answer for a body that arrived and could not be parsed — the read
    succeeding is not the same as the answer being usable."""
    def read(path, raw=False):
        page = ("<html>a proxy error page</html>", "")
        return page if "workflows?" in path else ("", "")
    got, why = panel_scope.ci_unrunnable("acme/board", "fca", "feat/x", read=read)
    assert got is None and "came back unparseable" in why


def test_a_workflow_FILE_that_could_not_be_read_withdraws_it():
    """One unread file is enough, because the claim is about all of them: the file
    nobody could open is exactly the one that might have fired."""
    got, why = _scan({"test.yml": PR_ON_MAIN}, file_fails="HTTP 500")
    assert got is None
    assert "`.github/workflows/test.yml` could not be read" in why


def test_an_on_block_this_reader_could_not_find_withdraws_it():
    """The parser is hand-written because there is no YAML parser on this host, so
    what it does not handle has to be stated rather than guessed at. A workflow it
    knows nothing about is one that might trigger on anything, and one unknown
    workflow is enough — the claim is about ALL of them."""
    got, why = _scan({"test.yml": PR_ON_MAIN,
                      "odd.yml": "name: odd\njobs:\n  build:\n    runs-on: x\n"})
    assert got is None
    assert "the `on:` block of `.github/workflows/odd.yml` could not be read" in why


def test_more_workflows_than_it_will_scan_withdraws_it_rather_than_sampling():
    """A partial scan cannot support a claim about every workflow, and reading a
    hundred files to answer a question nothing gates on is the wrong trade. The cap
    is named in the answer so an operator can see which of the three answers they
    got."""
    many = {f"w{i}.yml": PR_ON_MAIN
            for i in range(panel_scope.WORKFLOW_FILE_CAP + 1)}
    got, why = _scan(many)
    assert got is None
    assert f"over the {panel_scope.WORKFLOW_FILE_CAP} this check reads" in why


def test_a_file_that_is_not_yaml_is_not_read_as_a_workflow():
    """The directory holds whatever a repo put there. A README beside the workflows
    is not a workflow that failed to parse, and treating it as one would withdraw the
    claim on most repos."""
    got, why = _scan({"test.yml": PR_ON_MAIN, "README.md": "# not a workflow\n"})
    assert why == "" and got is not None
    assert [w["path"] for w in got["workflows"]] == [".github/workflows/test.yml"]


# ------------------------------------ an unreadable shape must not become a NAME
#
# The scanner reads event names out of text, and a reader that takes whatever it
# finds as a name turns a shape it cannot parse into an event nobody has — which
# matches no trigger and reports the repo as having no runnable workflow. That is
# the strong false claim this whole module is organised against, arriving from the
# inside: `on: {pull_request: {branches: [fca]}}` came back as one "event name",
# `workflow_can_run` said no, and the panel told an operator that `fca` was in no
# trigger list while handing them a remedy to add a branch the workflow already
# lists. So an item that is not a bare YAML identifier WITHDRAWS THE WHOLE READ.


@pytest.mark.parametrize("shape,spelling", [
    ("name: ci\non: {pull_request: {branches: [fca]}}\n", "the inline flow mapping"),
    ("name: ci\non: [push, {pull_request: {branches: [fca]}}]\n",
     "a flow mapping inside an inline sequence"),
    ("name: ci\non:\n  - {push: {branches: [fca]}}\n",
     "the block-sequence spelling of the same thing"),
    ("name: ci\non:\n  'a sentence, not an event': x\n",
     "a block-mapping key that is not an identifier"),
])
def test_a_trigger_this_reader_cannot_NAME_withdraws_the_whole_read(shape, spelling):
    """All three places a name is derived, because the bug was one of them and a fix
    in one is a fix nobody can rely on. Each of these workflows CAN fire for `fca`, or
    might; what they have in common is that this parser cannot say so, and "an event
    with a name nothing matches" is indistinguishable downstream from "a workflow that
    cannot run"."""
    assert panel_scope.workflow_triggers(shape) is None, spelling
    got, why = _scan({"ci.yml": shape})
    assert got is None, spelling
    assert "the `on:` block of `.github/workflows/ci.yml` could not be read" in why


def test_the_workflow_that_started_this_is_no_longer_called_unrunnable():
    """`prisonblues/lexray#1780`'s shape, written the way that broke it. The base IS in
    the trigger list, and the answer has to be "a run may exist" — not the confident
    falsehood with an unperformable remedy under it that #628 exists to remove."""
    flow = ("name: ci\non: {pull_request: {branches: [fca]}}\n"
            "jobs:\n  b:\n    runs-on: x\n")
    got, why = _scan({"ci.yml": flow}, base="fca")
    assert got is None
    assert why and "could not be read" in why


def test_ONE_unreadable_item_withdraws_the_events_beside_it():
    """All or nothing, deliberately. Dropping the unreadable item and keeping the rest
    would answer "can a run exist" from a subset of the triggers — the same partial
    population `ci_unrunnable` already refuses when a repo has more workflow files
    than it reads."""
    mixed = "name: ci\non: [push, {pull_request: {branches: [fca]}}]\n"
    assert panel_scope.workflow_triggers(mixed) is None


def test_a_flow_mapping_as_an_events_VALUE_still_fails_OPEN():
    """The other side of the line, and it is the difference between the fix and an
    over-correction. Here the event NAME was read normally and only its filter is
    unread — which `_event_filter` reports as "not stated", and GitHub reads a
    workflow with no `branches:` as triggering on every branch. Withdrawing here would
    make a legal and common spelling unreadable for no gain."""
    value = ("name: ci\non:\n  push: {branches: [main]}\n"
             "  pull_request: {branches: [main]}\n")
    got = panel_scope.workflow_triggers(value)
    assert set(got) == {"push", "pull_request"}
    assert got["pull_request"] == {"branches": None, "branches-ignore": None}
    # ...so the repo reads as one whose CI may run, for any base.
    assert panel_scope.workflow_can_run(got, "fca", "feat/x") is True
    assert _scan({"ci.yml": value}, base="fca") == (None, "")


def test_the_plain_spellings_are_untouched_by_all_of_that():
    """The regression guard on the guard: `_EVENT_NAME` sits in front of every name
    this reader derives, so a pattern too strict would withdraw on the shapes every
    repo actually writes and turn the check off everywhere at once."""
    for text, events in (("name: ci\non: push\n", {"push"}),
                         ("name: ci\non: [push, pull_request]\n",
                          {"push", "pull_request"}),
                         ("name: ci\non:\n  - push\n  - pull_request\n",
                          {"push", "pull_request"}),
                         ("name: ci\non:\n  workflow_dispatch:\n  pull_request:\n",
                          {"workflow_dispatch", "pull_request"})):
        got = panel_scope.workflow_triggers(text)
        assert got is not None and set(got) == events, text
        assert panel_scope.workflow_can_run(got, "any-base", "any/head") is True, text


# ---------------------------------------------------------- through a whole round

def _round(monkeypatch, capsys, tmp_path, *, ci="none", unrunnable=(None, ""),
           asked=None):
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: CFG)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "fix: a real bug", "additions": 3, "deletions": 1,
              "headRefName": "feat/x", "headRefOid": "abc"},
        diff="diff --git a/a.py b/a.py\n+x\n",
        compare='{"status": "ahead", "files": [{"filename": "a.py", "patch": "@@"}]}'))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: (ci, [], None))
    # The settle wrapper as well as the reader under it: `run()` gives a PENDING
    # build ten minutes to finish (#501), and a suite that stubbed only the reader
    # would sit on the whole budget for the one state below that exercises it.
    monkeypatch.setattr(panel, "review_ci_settled",
                        lambda *a, **k: (ci, [], None, 0.0))
    monkeypatch.setattr(panel, "adjudicate",
                        lambda *a, **k: ([], None, panel.CoverageRuling()))

    def scan(gh_repo, base, head_branch, **_kw):
        if asked is not None:
            asked.append((gh_repo, base, head_branch))
        return unrunnable

    monkeypatch.setattr(panel, "ci_unrunnable", scan)
    out = tmp_path / "r.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False) == 0
    return capsys.readouterr().out, json.loads(out.read_text())


UNRUNNABLE = {"base": "main", "head": "feat/x", "ref": "main",
              "reason": "no workflow in this repo can produce a run for a pull "
                        "request into `main`",
              "remedy": "add `main` to the trigger list of the workflow that "
                        "should gate it",
              "workflows": [{"path": ".github/workflows/test.yml",
                             "events": ["pull_request"]}]}


def test_the_question_is_only_asked_where_it_arises(monkeypatch, capsys, tmp_path):
    """`unknown` is a lookup that failed and says nothing about the repo's triggers,
    `blocked` has a run, and every other state has one by construction. So this costs
    a repo whose CI works nothing at all — which is what makes an extra API read per
    round acceptable."""
    asked = []
    for state in ("PASS", "FAIL", "PENDING", "blocked", "unknown"):
        _round(monkeypatch, capsys, tmp_path, ci=state, asked=asked)
    assert asked == []
    _round(monkeypatch, capsys, tmp_path, ci="none", asked=asked)
    assert asked == [("acme/board", "main", "feat/x")]


def test_the_record_rides_beside_ci_status_and_does_not_become_one(monkeypatch,
                                                                  capsys, tmp_path):
    """`app/ordering.py` compares `ci_status` against PASS/FAIL for equality and
    matches `CI_SETTLED`/`CI_NOT_APPLICABLE` as sets, so a new member of that field
    ripples into consumers this module does not own. Unrunnable stays not-a-pass
    exactly as `none` already was — fail-closed, as the code already was."""
    _report, payload = _round(monkeypatch, capsys, tmp_path,
                              unrunnable=(UNRUNNABLE, ""))
    assert payload["ci_status"] == "none"
    assert payload["ci_unrunnable"] == UNRUNNABLE


def test_the_key_is_null_rather_than_absent_on_a_repo_whose_CI_can_run(
        monkeypatch, capsys, tmp_path):
    """An absent key and "a run can exist here" are different claims, and a consumer
    forced to tell them apart would be reading the payload's age rather than the
    repo's state."""
    _report, payload = _round(monkeypatch, capsys, tmp_path, ci="PASS")
    assert payload["ci_unrunnable"] is None


def test_the_report_names_the_remedy_and_still_refuses_the_merge(monkeypatch, capsys,
                                                                 tmp_path):
    """The substitution that is the whole of #628: the instruction under the gate
    becomes one somebody can carry out, and the gate itself does not soften. A reader
    who is told to wait for a run that is not scheduled resolves the contradiction by
    ignoring the gate."""
    report, _payload = _round(monkeypatch, capsys, tmp_path,
                              unrunnable=(UNRUNNABLE, ""))
    assert "**CI CANNOT RUN for this PR at all**" in report
    assert "**Remedy:** add `main` to the trigger list" in report
    assert "waiting will not make it one" in report
    assert "Do not merge on the strength of the review below" in report


def test_a_check_that_could_not_be_put_says_so_and_keeps_the_old_wording(
        monkeypatch, capsys, tmp_path):
    """The third answer, in the artefact. The round falls back to the sentence it has
    always printed and records that the stronger claim was not established — so a
    reader is never left to infer "CI can run" from the absence of a warning nobody
    was able to compute."""
    _report, payload = _round(monkeypatch, capsys, tmp_path,
                              unrunnable=(None, "the repo's workflow directory could "
                                                "not be read (HTTP 404)"))
    assert payload["ci_unrunnable"] is None
    assert any("whether CI can run for this base at all was NOT established" in n
               and "HTTP 404" in n for n in payload["config_notes"])


def test_the_seats_are_told_the_same_thing_the_operator_is(monkeypatch, capsys,
                                                           tmp_path):
    """The `none` brief calls the missing run "a fact about the commit rather than
    about the repo", and on a PR whose base is in no trigger list that sentence is
    precisely false. A seat told otherwise reasons that a run is coming when nothing
    the author can do will produce one — the same defect #628 fixes for the operator,
    arriving at the seat."""
    plain = panel_scope.ci_brief("none", [], None)
    corrected = panel_scope.ci_brief("none", [], None, unrunnable=UNRUNNABLE)
    assert plain != corrected
    assert "a fact about the commit rather than about the repo" in plain
    assert "a fact about the commit rather than about the repo" not in corrected
    # Only the claim changes: the state is still `none` and the brief still says in
    # as many words that this is not a pass.
    assert "NO RUN EXISTS for this commit" in corrected
    assert "This is not a pass." in corrected

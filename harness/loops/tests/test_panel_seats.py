"""The panel's seats: where a reviewer runs, and what a run says when it loses one.

Two halves of the same defect (#68), which is #19's disease one level up. #19 stopped
a REVIEWER that produced nothing from reading as a reviewer that found nothing. This
is the PANEL: a run with half its seats empty was presented identically to a full one.

The first half is why a seat goes missing. `run_cli` ran every reviewer with no
`cwd=`, so each inherited whatever directory the panel process happened to be started
from — ambient state nothing configured, nothing recorded and nothing could reproduce.
On PR #64 codex exited 1 with "Not inside a trusted directory and
--skip-git-repo-check was not specified" while two panels launched in the same second
ran it fine; those were started from inside a checkout and that one from a scratch
directory under /tmp. Each member now runs in its own empty `git init`ed sandbox,
which satisfies codex's check by construction — which is why no
`--skip-git-repo-check` appears anywhere here.

**A sandbox and not the repo under review**, which is the part worth pinning rather
than merely writing down. The first version of this fix pinned the seats to the
checkout and a reviewer caught what that costs: a headless CLI reads its project
configuration from its cwd — CLAUDE.md, `.claude/settings.json`, hooks that execute —
so the repo under review gains a channel into the reviewer judging it. And it bought
no access in exchange, because `cfg["path"]` is the main checkout on whatever branch
it was left on, never the PR's code. A tool-capable seat pointed there can quote a
different branch as the code under review: a plausible wrong answer replacing a
visible failure.

The second half is what the report says when it happens anyway — a seat can still be
lost to a timeout, a quota, or a model pin the CLI refuses. It has to say so above the
findings, and it has to stop "no finding earned ⋆consensus" reading the same as "there
was nobody to agree with".
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
from conftest import gh_stub  # noqa: E402




REPO = "/tmp/acme-board"

# Two seats, so a lost one is a DEGRADED panel rather than the whole panel. A
# one-seat config would conflate the two and is tested separately below.
TWO_SEAT_CFG = {"github": "acme/board", "path": REPO,
                "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                              "codex": {"enabled": True, "model": "", "effort": ""}},
                "review_panel": {}}
ONE_SEAT_CFG = {"github": "acme/board", "path": REPO,
                "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
                "review_panel": {}}


# ---------------------------------------------------------------- where a seat runs

def _record_cwds(monkeypatch, seen: list):
    """Every CLI invocation's cwd, with the sandbox's own `git init` let through —
    it is set up via the same `subprocess.run` these tests replace."""
    def fake_run(argv, **kw):
        if argv[:2] == ["git", "init"]:
            Path(argv[-1]).mkdir(parents=True, exist_ok=True)
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        seen.append(kw.get("cwd"))
        return type("P", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()

    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(panel.subprocess, "run", fake_run)


def test_a_reviewer_runs_in_a_sandbox_not_in_the_shell_it_was_launched_from(monkeypatch):
    """The fix for the lost seat, asserted on the kwargs `subprocess.run` is
    actually called with: every layer above it can hold a correct path and still
    leave the CLI inheriting the caller's shell, which is precisely how this
    shipped. `None` is the failure — that is the value that means "wherever the
    shell was"."""
    seen = []
    _record_cwds(monkeypatch, seen)
    panel.review_llm("claude", "sonnet", "review this")
    assert seen and all(c for c in seen), f"a reviewer ran with cwd={seen}"


def test_the_reparse_RETRY_gets_a_sandbox_too(monkeypatch):
    """`review_llm` calls `run_cli` twice — the review and the one-shot reparse —
    and an assertion that reads only the first would pass with the retry left on
    the inherited cwd. The retry is the attempt that runs after a flake, which is
    exactly when a seat can least afford a second failure mode."""
    seen = []
    _record_cwds(monkeypatch, seen)
    # Unparseable both times: forces the retry, so two invocations are recorded.
    monkeypatch.setattr(panel, "parse_reply", lambda *a, **k: None)
    panel.review_llm("claude", "sonnet", "review this")
    assert len(seen) == 2, f"expected review + reparse retry, got {len(seen)}"
    assert all(c for c in seen)


def test_the_judge_and_ITS_retry_run_in_a_sandbox(monkeypatch):
    """The judge is a headless CLI with the same exposure, and the seat whose loss
    is worst — a judge that dies takes every finding through unadjudicated — so it
    is the last place to leave depending on the caller's shell. Its reparse retry
    is asserted for the same reason as the reviewer's."""
    seen = []
    _record_cwds(monkeypatch, seen)
    monkeypatch.setattr(panel, "extract_json_value", lambda *a, **k: None)
    f = panel.Finding("claude", "P1", "a.py", 1, "title", "detail")
    panel.adjudicate([[f]], "diff", "sonnet", 34)
    assert len(seen) == 2, f"expected judge + reparse retry, got {len(seen)}"
    assert all(c for c in seen)


def test_the_sandbox_is_not_the_repo_under_review(monkeypatch, tmp_path, capsys):
    """The design decision, pinned so it cannot be quietly reverted to the first
    version of this fix. A seat that runs in the checkout reads that repo's
    CLAUDE.md, `.claude/settings.json` and hooks, and can Read/Grep a tree on a
    different branch from the diff it was handed."""
    seen = []
    _record_cwds(monkeypatch, seen)
    panel.review_llm("claude", "sonnet", "review this")
    assert seen and all(c != REPO for c in seen), (
        f"a seat ran in the repo under review: {seen}")


def test_the_sandbox_is_a_git_repo_because_that_is_the_whole_point(monkeypatch,
                                                                   tmp_path):
    """codex refuses to start outside a git repository, which is the entire reason
    the cwd cannot simply be an empty temp directory. Asserted on the argv, since
    that is what makes the seat reproducible rather than lucky."""
    inits = []

    def fake_run(argv, **kw):
        if argv[:2] == ["git", "init"]:
            inits.append(argv)
            Path(argv[-1]).mkdir(parents=True, exist_ok=True)
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return type("P", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()

    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    panel.review_llm("codex", "", "review this")
    assert inits, "the member's sandbox was never made a git repo"


def test_a_sandbox_that_cannot_be_made_degrades_instead_of_killing_the_panel(
        monkeypatch, tmp_path, capsys):
    """`run()` joins the seats with a bare `fut.result()`, so an exception raised
    setting one member's sandbox does not cost that seat — it costs the whole
    panel, including the seats that worked, the sonar gate and the report.

    Every failure shape, because only one of them is a returncode: git absent
    from PATH raises FileNotFoundError, a bad temp root PermissionError, a stalled
    mount TimeoutExpired. The first version of this caught none of them."""
    for boom in (FileNotFoundError(2, "No such file or directory"),
                 PermissionError(13, "Permission denied"),
                 subprocess.TimeoutExpired(["git", "init"], 30)):
        def fake_run(argv, **kw):
            raise boom
        monkeypatch.setattr(panel.subprocess, "run", fake_run)
        made = panel.member_sandbox(tmp_path / f"cwd{id(boom)}")
        assert Path(made).is_dir(), "the directory must exist even when git init fails"
        assert "git init failed" in capsys.readouterr().err, boom


def test_the_sandbox_directory_exists_even_when_git_init_fails(monkeypatch, tmp_path):
    """Why the mkdir is unconditional rather than left to `git init`.

    With no directory, `run_cli`'s `subprocess.run(cwd=…)` raises FileNotFoundError
    about a path — three times, once per attempt — and the seat never reaches
    codex's own "not inside a trusted directory", which is the message that names
    the actual cause. Degrading to the DOCUMENTED failure is the point."""
    monkeypatch.setattr(panel.subprocess, "run", lambda *a, **k: type(
        "P", (), {"returncode": 1, "stdout": "", "stderr": "fatal: nope"})())
    made = panel.member_sandbox(tmp_path / "cwd")
    assert Path(made).is_dir()


def test_a_real_sandbox_is_a_real_repo(tmp_path):
    """The one test here that runs git rather than mocking it — the mocked tests
    above all assert plumbing, and plumbing that produces a directory git does not
    recognise would satisfy every one of them while losing the codex seat."""
    made = panel.member_sandbox(tmp_path / "cwd")
    assert (Path(made) / ".git").exists()
    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            cwd=made, capture_output=True, text=True)
    assert inside.stdout.strip() == "true", inside.stderr


# ---------------------------------------------------------------- what a lost seat says

def _stub_panel(monkeypatch, findings=None, cfg=TWO_SEAT_CFG, runs=None):
    """Every process a run would spawn, replaced.

    `runs` maps a reviewer name to the :class:`ReviewerRun` it should return, which
    is how a seat is made to go missing without a CLI being involved."""
    if findings is None:
        findings = [panel.Finding("claude", "P3", "a.py", 3, "unused import")]
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(panel, "sh", gh_stub(
        meta={"title": "feat: x", "additions": 3, "deletions": 1,
              "headRefOid": "abc"},
        diff="diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", _confirm_everything)

    def review(name, *a, **k):
        if runs and name in runs:
            return runs[name]
        return panel.ReviewerRun(list(findings), None, 10, [])
    monkeypatch.setattr(panel, "review_llm", review)


def _confirm_everything(clusters, diff, model, pr, budget=None, coverage=None, ci=""):
    flat = [f for grp in clusters for f in grp]
    return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity=f.severity,
                             file=f.file, line=f.line, synthesis=f.title,
                             verdict="confirmed", detail=f.detail, reported_by=[f],
                             rationale="real")
             for i, f in enumerate(flat)], None, "")


def _report(monkeypatch, capsys, cfg=TWO_SEAT_CFG, runs=None, findings=None):
    _stub_panel(monkeypatch, findings=findings, cfg=cfg, runs=runs)
    assert panel.run("board", 34, post=False, record=False) == 0
    return capsys.readouterr().out


def test_a_panel_that_lost_a_seat_says_so_above_the_findings(monkeypatch, capsys):
    """#64's report read "LLM reviewers ran: claude (opus)" and then laid out 23
    findings exactly as a full panel would. The seat count is the fact that makes
    those two reports different artifacts, and it was nowhere a reader looks."""
    report = _report(monkeypatch, capsys, runs={
        "codex": panel.ReviewerRun(skip="codex: timed out after 1800s")})
    assert "1 of 2 configured" in report
    assert "panel degraded" in report
    # The existing per-seat reason survives alongside the panel-level statement:
    # "which seat" and "how weak is this review" are different questions.
    assert "timed out after 1800s" in report


def test_a_lone_reviewer_says_no_consensus_was_POSSIBLE(monkeypatch, capsys):
    """The distinction #68 is named for. ⋆consensus takes two reviewers, so on a
    panel of one its absence is structural — but it renders exactly like a panel
    where two reviewers read the same code and neither backed the other. A reader
    takes the second meaning, which is the pessimistic reading of a review that
    never got the chance to be pessimistic."""
    report = _report(monkeypatch, capsys, runs={
        "codex": panel.ReviewerRun(skip="codex: CLI absent")})
    assert "no ⋆consensus is possible" in report
    assert "sole reviewer, no second opinion" in report
    assert "⋆consensus)" not in report


def test_a_full_panel_says_none_of_it(monkeypatch, capsys):
    """The other half, and the one that decides whether any of this is readable:
    a caveat that fires on healthy runs is noise, and a reader who learns to skip
    it has lost the degraded case too."""
    report = _report(monkeypatch, capsys)
    assert "2 of 2 configured" in report
    assert "panel degraded" not in report
    assert "no ⋆consensus is possible" not in report
    assert "sole reviewer" not in report


def test_a_deliberate_single_seat_panel_is_still_told_it_has_no_second_opinion(
        monkeypatch, capsys):
    """A repo configured for one reviewer lost nothing, so it is NOT degraded —
    but its findings are just as unchallenged as the degraded panel's, and the
    consensus signal is just as unavailable. The two notes are separate for this
    case: conflating them would either cry degradation at a run that is working as
    configured, or stay silent about a review nobody corroborated."""
    report = _report(monkeypatch, capsys, cfg=ONE_SEAT_CFG)
    assert "1 of 1 configured" in report
    assert "panel degraded" not in report
    assert "no ⋆consensus is possible" in report


def test_a_panel_that_lost_EVERY_seat_does_not_claim_one_filed(monkeypatch, capsys):
    """The zero-seat case, which the first version of this block got wrong in its
    own new line: it printed "it takes two reviewers to agree, and one filed" on a
    run where nobody filed, and pointed the reader at "absence of ⋆consensus below"
    in a report with no LLM findings at all. A false factual claim, in the block
    added to stop false impressions — so it is pinned rather than merely fixed."""
    report = _report(monkeypatch, capsys, runs={
        "claude": panel.ReviewerRun(skip="claude: timed out after 1800s"),
        "codex": panel.ReviewerRun(skip="codex: exited 1 (quota exhausted)")})
    assert "0 of 2 configured" in report
    assert "panel degraded" in report
    assert "nothing below was reviewed by a panel member" in report
    assert "and one filed" not in report


def test_a_CLI_this_host_does_not_carry_is_not_a_DEGRADED_panel(monkeypatch, capsys):
    """`coverage_veto` already argues this at length for the veto: a missing CLI is
    a fact about the HOST, not about the round — it is absent every run, so treating
    it as degradation prints the warning on every unattended run of a repo that
    enables a workstation-only vendor, where nothing was lost and nothing could be
    recovered. That is the alert fatigue the full-panel test above exists to
    prevent, and it would take the real degraded case down with it.

    Still stated, quietly and separately: "configured but not installed here" is
    worth knowing, it is just not a degradation."""
    report = _report(monkeypatch, capsys, runs={
        "codex": panel.ReviewerRun(skip="codex: CLI absent", absent=True)})
    assert "1 of 2 configured" in report
    assert "panel degraded" not in report
    assert "not installed on this host" in report
    # It is still a one-seat review, and that half must survive the exemption.
    assert "no ⋆consensus is possible" in report


def test_a_lost_seat_beside_an_absent_one_still_reads_as_degraded(monkeypatch,
                                                                  capsys):
    """The exemption is per seat, not a switch: a host missing one CLI while
    another seat times out has genuinely lost something, and the count must name
    the seat that was lost rather than both or neither."""
    cfg = {**TWO_SEAT_CFG,
           "reviewers": {**TWO_SEAT_CFG["reviewers"],
                         "pi": {"enabled": True, "model": "", "effort": ""}}}
    report = _report(monkeypatch, capsys, cfg=cfg, runs={
        "codex": panel.ReviewerRun(skip="codex: CLI absent", absent=True),
        "pi": panel.ReviewerRun(skip="pi: timed out after 1800s")})
    assert "1 of 3 configured" in report
    assert "**panel degraded** — 1 of 3" in report, "the absent seat was counted as lost"
    assert "not installed on this host" in report


def test_sonarqube_counts_as_somebody_to_agree_WITH(monkeypatch, capsys):
    """The consensus banner and the ⋆consensus marker have to be counted over the
    same population, and they were not: sonar's base-branch issues are judged
    alongside the LLM findings (`llm_findings` takes `soft`), so a canonical
    finding's `reviewers` can legitimately read ["claude", "sonarqube"]. Counting
    LLM seats alone let one lone-LLM report stamp ⋆consensus on that finding while
    declaring two dozen lines above that consensus was impossible — the report
    contradicting itself in the exact place this was added to stop it."""
    cfg = {**TWO_SEAT_CFG,
           "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                         "sonarqube": {"enabled": True}}}
    soft = [panel.Finding("sonarqube", "P3", "a.py", 3, "unused import")]
    monkeypatch.setattr(panel, "review_sonarqube",
                        lambda *a, **k: ("no-pr-analysis", [], soft, None))
    report = _report(monkeypatch, capsys, cfg=cfg)
    # One LLM seat, but sonarqube also filed — so agreement was possible, and the
    # banner claiming otherwise would be false.
    assert "no ⋆consensus is possible" not in report
    assert "sole reviewer" not in report


def test_sonarqube_that_RAN_but_filed_nothing_is_not_somebody_to_agree_with(
        monkeypatch, capsys):
    """The other end of the same count, and the reason it keys on what sonar filed
    rather than on its gate status — a status is a side effect, not the thing.

    Only the `no-pr-analysis` fallback yields soft findings that can share a
    canonical record; a scanned PR returns `hard`, which renders in its own
    section and never reaches `conf()`. So a repo with one LLM seat and a
    successfully scanned Sonar gate has nobody to corroborate its findings, and
    keying on the gate suppressed both the banner and the per-finding note on
    exactly the report that needed them."""
    cfg = {**TWO_SEAT_CFG,
           "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                         "sonarqube": {"enabled": True}}}
    # Scanned cleanly: a real gate result, hard findings only, nothing soft.
    monkeypatch.setattr(panel, "review_sonarqube",
                        lambda *a, **k: ("OK", [], [], None))
    report = _report(monkeypatch, capsys, cfg=cfg)
    assert "no ⋆consensus is possible" in report
    assert "sole reviewer, no second opinion" in report

"""Registering the deterministic seat: where it sits, and what it must never do
(#780).

`test_panel_slop.py` pins what the seat FINDS — the rules, the fixtures, the
population filter. This file pins the five places it is wired in, and the three
of them that are load-bearing are not the obvious ones.

**It is a seat, and it is not an LLM seat.** `ALL_REVIEWERS` is what
`select_reviewers` resolves against and what `--reviewers` validates against, so
membership there is what makes the seat askable-for at all. `LLM_REVIEWERS` is
what hands a seat a model, a reasoning effort, a `max_diff_chars` budget, a
composed prompt and a vendor CLI — none of which this seat has. A single-tuple
registration would have given it a diff budget it cannot spend and a `model`
field naming a brain that never ran, which is the contradictory pairing #222
exists to remove.

**A round with no fix range does not run it.** This is the one that matters. The
seat's whole claim is that its population is what the LAST PASS wrote, and the
fallback that is always to hand — the PR's own diff — is the one thing that
would make it wrong: on round 1 the "fix pass" IS the change under review, so a
seat reading the PR diff there reports the author's work as the fixer's slop, at
the top of the report, with a deterministic matcher's authority behind it. The
round says why it did not run instead.

**It cannot be asked a question and cannot be asked to propose.** Both
exclusions are structural — driven off `LLM_REVIEWERS` rather than off a list of
names — so the next seat with no brain is unaskable on the day it is registered
rather than the day somebody notices.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_rules  # noqa: E402
import panel  # noqa: E402
import panel_ask  # noqa: E402
import panel_core  # noqa: E402
import panel_propose  # noqa: E402
from conftest import gh_stub  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE = REPO_ROOT / ".harness-rules.sample"


# --------------------------------------------------------------------------
# Where it sits
# --------------------------------------------------------------------------

def test_slop_is_a_panel_member():
    """Selectable, so `reviewers.slop.enabled` has a reader and `--reviewers slop`
    is a legal ask rather than the hard exit an unknown name gets."""
    assert "slop" in panel.ALL_REVIEWERS


def test_slop_is_NOT_an_llm_seat():
    """The half a one-line registration would have got wrong. Everything built off
    `LLM_REVIEWERS` — the budgets, the models, the efforts, the dispatch loop —
    describes a vendor CLI holding a prompt, and this seat is a pure function of a
    diff. In that tuple it would acquire a budget it cannot spend and be recorded
    as having reviewed on a model that does not exist."""
    assert "slop" not in panel.LLM_REVIEWERS
    assert "slop" not in panel_core.SEAT_MODEL_DEFAULTS
    assert "slop" not in panel_core.CLI_BIN


def test_the_seat_is_selected_by_its_enabled_key_like_any_other():
    """`select_reviewers` resolves the repo's config against ALL_REVIEWERS, so the
    registration is what turns the key into a seat. Without it the block parses,
    validates, and selects nothing."""
    got, _ = panel.select_reviewers({"claude": {"enabled": True},
                                     "slop": {"enabled": True}}, None)
    assert got == {"claude", "slop"}
    off, _ = panel.select_reviewers({"claude": {"enabled": True},
                                     "slop": {"enabled": False}}, None)
    assert off == {"claude"}


def test_naming_it_on_the_command_line_is_not_an_unknown_reviewer():
    """`--reviewers` hard-exits on a name it does not know, deliberately, so that a
    typo cannot produce a one-seat panel that reads like two. Unregistered, asking
    for this seat by hand would have been that exit."""
    got, note = panel.select_reviewers({}, "claude,slop")
    assert got == {"claude", "slop"}
    assert "slop" in note


# --------------------------------------------------------------------------
# The default, and the argument for it
# --------------------------------------------------------------------------

def test_the_fleet_default_is_OFF():
    """`DEFAULTS` is every repo the fleet reviews, and none of them but this one
    has had a single rule run against it. The block carries `enabled` and nothing
    else — the shape a seat with no brain takes, and sonarqube's precedent."""
    assert harness_rules.DEFAULTS["reviewers"]["slop"] == {"enabled": False}


def test_THIS_repo_turns_it_on():
    """The judgement call, recorded where a reader will look for it. The seat was
    calibrated on this repo's own 252 files, it costs no vendor call, and it has
    never run inside a real cycle — which is a reason to run it here, not a reason
    to wait. The way back is this one key."""
    rules = json.loads(SAMPLE.read_text())
    assert rules["reviewers"]["slop"]["enabled"] is True


def test_the_sample_states_the_measurement_and_the_way_back():
    """This repo treats an undocumented dial as a defect, and a switched-ON seat
    owes more than a switched-off one: what was measured, what it costs, what it
    can break, and the single key that reverses it."""
    block = json.loads(SAMPLE.read_text())["reviewers"]["slop"]
    prose = " ".join(v for k, v in block.items() if k.startswith("_"))
    assert "2026-09-06" in prose
    assert "383" in prose and "90" in prose        # the calibration
    assert "false" in block["_way_back"]           # the one key, named


def test_the_block_is_not_an_unknown_key():
    """Without the `DEFAULTS` entry, `resolve_repo` sweeps `reviewers.slop` as a
    name nothing reads and drops it — the silent failure `unknown_keys` exists to
    stop, arriving as a seat that is configured and never runs."""
    flagged = harness_rules.unknown_keys(
        {"reviewers": {"slop": {"enabled": True}}})
    assert "enabled" not in flagged.get("reviewers.slop", [])


# --------------------------------------------------------------------------
# Not a correspondent
# --------------------------------------------------------------------------

@pytest.fixture()
def ask_cfg(monkeypatch, tmp_path):
    """The repo's resolved config, pointed at a fixture tree — so these exercise
    `ask()` rather than `git remote get-url`. `test_panel_ask`'s own fixture,
    duplicated rather than imported, because a fixture reached across modules is a
    dependency neither file declares."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("\n".join(f"line {i}" for i in range(1, 21)))
    conf = copy.deepcopy(harness_rules.DEFAULTS)
    conf |= {"name": "demo", "github": "me/demo", "path": str(tmp_path),
             "_rules_baseline": harness_rules.SAMPLE_FILENAME}
    monkeypatch.setattr(panel_ask, "load_repo_cfg", lambda name: conf)
    return conf


def test_the_seat_cannot_be_asked_a_question(monkeypatch, ask_cfg, tmp_path):
    """`--ask` puts one premise to the seats and counts their answers. This one has
    no prose channel in either direction — which is the property that makes it
    worth having, since a fixer cannot talk it out of a finding — so counting it as
    a voice would make a one-seat ask read as two. Said out loud rather than
    silently dropped, for sonarqube's stated reason."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm",
                        lambda *a, **k: panel.SeatAnswer("holds", "r"))
    out = tmp_path / "ask.json"
    panel.ask(ask_cfg["path"], "p", [], reviewers="claude,slop", json_file=str(out))
    payload = json.loads(out.read_text())
    assert list(payload["answers"]) == ["claude"]
    assert any("slop cannot be asked" in n for n in payload["config_notes"])


def test_enabling_it_for_REVIEWS_does_not_warn_on_every_ask(
        monkeypatch, ask_cfg, tmp_path):
    """The note's own stated purpose only argues for firing when the seat was
    ASKED for. On the resolved set it is a permanent warning about a seat nobody
    tried — and this repo has the seat ON, so that warning would land on every ask
    anybody here ever makes."""
    ask_cfg["reviewers"]["slop"] = {"enabled": True}
    ask_cfg["reviewers"]["claude"] = dict(ask_cfg["reviewers"].get("claude", {}),
                                          enabled=True)
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm",
                        lambda *a, **k: panel.SeatAnswer("holds", "r"))
    out = tmp_path / "ask.json"
    panel.ask(ask_cfg["path"], "p", [], json_file=str(out), asker="")
    payload = json.loads(out.read_text())
    assert not any("slop" in n for n in payload["config_notes"])
    assert "slop" not in payload["answers"]


def test_both_brainless_seats_are_named_in_ONE_note(monkeypatch, ask_cfg, tmp_path):
    """Driven off `ALL_REVIEWERS` minus `LLM_REVIEWERS` rather than off two written
    names, so the next seat with no brain is unaskable the day it is registered."""
    monkeypatch.setattr(panel_ask, "record_ask", lambda payload: None)
    monkeypatch.setattr(panel_ask, "ask_llm",
                        lambda *a, **k: panel.SeatAnswer("holds", "r"))
    out = tmp_path / "ask.json"
    panel.ask(ask_cfg["path"], "p", [], reviewers="claude,slop,sonarqube",
              json_file=str(out))
    notes = json.loads(out.read_text())["config_notes"]
    said = [n for n in notes if "cannot be asked" in n]
    assert len(said) == 1
    assert "slop" in said[0] and "sonarqube" in said[0]


def test_the_seat_is_not_asked_what_it_would_do_instead():
    """The constructive pass asks each seat with outstanding findings for the
    smallest change that resolves them. `seat_findings` filters on
    `LLM_REVIEWERS`, so this seat is excluded by the registration itself rather
    than by a second list of names that could disagree with the first."""
    f = panel_core.Finding("slop", "P3", "app/a.py", 4, "a placeholder body", "d")
    c = panel.Canonical(id="F01", severity="P3", file="app/a.py", line=4,
                             synthesis="a placeholder body", verdict="confirmed",
                             detail="d", reported_by=[f], rationale="real")
    assert panel_propose.seat_findings([c]) == {}


# --------------------------------------------------------------------------
# The round, end to end
# --------------------------------------------------------------------------

PR_DIFF = (
    "diff --git a/app/sync.py b/app/sync.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/app/sync.py\n"
    "+++ b/app/sync.py\n"
    "@@ -1,1 +1,2 @@\n"
    "+# TODO: implement the mirror\n"
    "+mirror = {}\n"
)

#: The fix pass, as the compare API returns it: one line the LAST pass added, and
#: it is a rule hit. The PR diff above carries a hit too, deliberately — a seat
#: reading the wrong population still finds something, which is exactly why
#: "it produced findings" is not evidence it read the right range.
FIX_PATCH = "@@ -10,0 +11,2 @@\n+# TODO: implement the retry\n+retries = 0"
FIX_COMPARE = json.dumps({"status": "ahead",
                          "files": [{"filename": "app/sync.py",
                                     "patch": FIX_PATCH}]})

CFG = {
    "github": "acme/e2e",
    # A path that CANNOT exist, for `test_panel_provenance`'s reason: rounds here
    # run local git against it, and a name somebody might plausibly create would
    # make these tests behave differently on one box than another. It also pins
    # the degradation this seat is supposed to survive — no checkout, so no
    # post-image, so the five ast rules declare themselves and the run comes back
    # `code_blind` rather than silently not running them.
    "path": "/nonexistent/acme-e2e",
    "_rules_baseline": ".harness-rules.sample",
    "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                  "slop": {"enabled": True}},
    "review_panel": {},
}


def _baseline(tmp_path, name, round_no, head):
    p = tmp_path / name
    p.write_text(json.dumps({
        "round": round_no, "cycle": "abc123", "reviewed": True,
        "repo": "acme", "github": "acme/e2e", "pr": 77, "head_sha": head,
        "to_fix": [], "dismissed": [], "sonar_findings": [],
    }))
    return str(p)


def _round(monkeypatch, tmp_path, round_no, head, baseline=(), cfg=None,
           compare=FIX_COMPARE):
    """One real `run()` with every subprocess replaced — the judge and the vendor
    seats faked, and the deterministic seat left to actually run."""
    fake_sh = gh_stub(meta={"title": "feat: mirror", "additions": 20,
                            "deletions": 2, "headRefOid": head},
                      compare=compare, diff=PR_DIFF)

    def fake_review(name, model, prompt, effort="", **_kw):
        return panel.ReviewerRun([], None, 800, None)

    def fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None,
                        ci="", **_kw):
        return ([panel.Canonical(id=panel._finding_id(pr, i + 1),
                                 severity=f.severity, file=f.file, line=f.line,
                                 synthesis=f.title, verdict="confirmed",
                                 detail=f.detail, reported_by=[f],
                                 rationale="real")
                 for i, grp in enumerate(clusters) for f in grp], None,
                panel.CoverageRuling())

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg or CFG)
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", fake_adjudicate)
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("e2e", 77, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline),
                     max_rounds=3) == 0
    return str(out), json.loads(out.read_text())


def test_round_one_does_not_run_the_seat_and_says_why(monkeypatch, tmp_path):
    """THE test. Round 1 has no earlier round, so there is no fix pass and no
    range — and the seat must decline rather than fall back to the PR's diff. The
    PR diff here contains a rule hit, so a seat reading the wrong population would
    file a finding against the change under review and be scored as working."""
    _, r1 = _round(monkeypatch, tmp_path, 1, head="aaa111")
    assert [f for f in r1["to_fix"] if "slop" in f["reviewers"]] == []
    assert any(s.startswith("slop: no fix-pass range") for s in r1["skipped"])
    # Not in the per-reviewer record either: `coverage_veto` walks that dict and
    # turns every `ran: False` into a veto line, and round 1 having no fix pass is
    # true of the first round of every cycle. A veto that is never absent carries
    # no information — the constant `coverage_veto`'s docstring rules out.
    assert "slop" not in r1["reviewers"]


def test_a_round_with_a_fix_range_runs_the_seat_over_THAT_range(
        monkeypatch, tmp_path):
    """Round 2, with a readable range: the seat runs, files against the line the
    LAST PASS added (11), and files nothing against the PR's own line (1) — which
    is the only way to tell the two populations apart, since both contain a hit."""
    r1 = _baseline(tmp_path, "r1.json", 1, "aaa111")
    _, r2 = _round(monkeypatch, tmp_path, 2, head="bbb222", baseline=[r1])
    mine = [f for f in r2["to_fix"] if "slop" in f["reviewers"]]
    assert [f["line"] for f in mine] == [11]
    assert "unfinished work" in mine[0]["synthesis"]
    assert r2["reviewers"]["slop"]["ran"] is True


def test_the_seat_has_no_model_no_budget_and_no_truncation(monkeypatch, tmp_path):
    """What the record says about a seat that never had a brain. Spelled out
    rather than left to `.get` defaults, because the payload is read months later
    and a missing key and a false one are different claims."""
    r1 = _baseline(tmp_path, "r1.json", 1, "aaa111")
    _, r2 = _round(monkeypatch, tmp_path, 2, head="bbb222", baseline=[r1])
    row = r2["reviewers"]["slop"]
    assert row["model"] is None and row["effort"] is None
    assert row["max_diff_chars"] is None
    assert row["truncated"] is False and row["argv_capped"] is False
    assert row["absent"] is False


def test_a_seat_with_nothing_to_read_is_blind_rather_than_clean(
        monkeypatch, tmp_path):
    """No checkout at `cfg["path"]`, so no post-image, so the five ast rules
    cannot run. They are DECLARED per file — the seat's own contract — and the run
    comes back `code_blind`, which is what keeps those declarations reported
    without becoming a standing veto on a box whose checkout lacks the PR head."""
    r1 = _baseline(tmp_path, "r1.json", 1, "aaa111")
    _, r2 = _round(monkeypatch, tmp_path, 2, head="bbb222", baseline=[r1])
    row = r2["reviewers"]["slop"]
    assert row["code_blind"] is True
    assert any("did not run" in g for g in row["could_not_assess"])


def test_an_unreadable_range_does_not_run_the_seat_either(monkeypatch, tmp_path):
    """The other half of round 1's case, on a round that HAS an earlier round. A
    branch rewritten between rounds means no diff of that span is the fix pass —
    the round's own increment included — so there is nothing here to read and the
    seat says so rather than reaching for the PR."""
    r1 = _baseline(tmp_path, "r1.json", 1, "aaa111")
    diverged = json.dumps({"status": "diverged", "files": []})
    _, r2 = _round(monkeypatch, tmp_path, 2, head="bbb222", baseline=[r1],
                   compare=diverged)
    assert [f for f in r2["to_fix"] if "slop" in f["reviewers"]] == []
    assert any(s.startswith("slop: no fix-pass range") for s in r2["skipped"])
    assert "slop" not in r2["reviewers"]


def test_a_seat_that_will_not_load_costs_a_line_and_not_the_round(
        monkeypatch, tmp_path):
    """The guarded import, exercised. `panel_locality` and `panel_blast` set the
    rule — a lane that cannot load must never cost a round — and it binds harder
    here, because this one also reads ten checked-in YAML files through a reader
    that is fatal on purpose. A typo in a rule file must not stop a panel with
    four vendors waiting on it."""
    r1 = _baseline(tmp_path, "r1.json", 1, "aaa111")
    monkeypatch.setattr(panel, "panel_slop", None)
    monkeypatch.setattr(panel, "SLOP_UNAVAILABLE", "ImportError: no module")
    _, r2 = _round(monkeypatch, tmp_path, 2, head="bbb222", baseline=[r1])
    assert any(s.startswith("slop: the seat is not installed") for s in r2["skipped"])
    assert "slop" not in r2["reviewers"]


def test_a_rule_file_that_will_not_parse_is_a_skip_and_DOES_veto(
        monkeypatch, tmp_path):
    """The other direction, and the distinction is the point. A seat this box
    could not import, and a round with no fix pass, are facts about the host and
    about the cycle's position; a rule file that will not parse is somebody's typo
    from this week. So it is recorded as a seat that did not run — which vetoes a
    confident stop — rather than exempted with the other two."""
    def boom():
        raise panel.panel_slop.SlopRuleError("slop_rules/x.yaml: unknown key")

    r1 = _baseline(tmp_path, "r1.json", 1, "aaa111")
    monkeypatch.setattr(panel.panel_slop, "load_rules", boom)
    _, r2 = _round(monkeypatch, tmp_path, 2, head="bbb222", baseline=[r1])
    row = r2["reviewers"]["slop"]
    assert row["ran"] is False
    assert "rule files would not load" in row["skip"]
    assert any("rule files would not load" in s for s in r2["skipped"])


def test_the_range_is_read_ONCE_and_the_seat_reuses_it(monkeypatch, tmp_path):
    """Not a second `compare` call. The seat takes the same `fix_diff` the
    provenance numbers and the surface measurement read, so the two can never
    describe different spans — which is the drift #512 spent itself closing
    between scope and attribution, arriving one seat later."""
    calls = []
    r1 = _baseline(tmp_path, "r1.json", 1, "aaa111")

    real = panel._fix_range_diff

    def counted(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(panel, "_fix_range_diff", counted)
    _, r2 = _round(monkeypatch, tmp_path, 2, head="bbb222", baseline=[r1])
    assert len(calls) == 1
    # And the seat did read it — otherwise this passes on a round where nothing
    # asked for the range at all.
    assert r2["reviewers"]["slop"]["ran"] is True

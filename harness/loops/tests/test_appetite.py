"""Tests for appetite.py — the two brakes on an autonomous loop.

The interesting ones are the defaults. Every switch here refuses out of the box,
and a test that only exercises the configured-open path would pass just as well
against a gate that never closes — so each default is asserted directly, and the
two self-approval holes (`require_human_triage`, `allowed_authors`) are asserted
against the shape of the attack rather than against a happy path.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import appetite  # noqa: E402
import harness_rules as hr  # noqa: E402


def cfg(**pickup):
    """A cfg whose pickup gate is open except for what the test narrows.

    Deliberately verbose rather than a fixture: `pickup_verdict` has five
    sequential refusals and a test about the fourth has to have got past the
    first three, so which ones are open is the interesting part of each case.
    """
    base = {"enabled": True, "only_labels": ["p0"],
            "allowed_authors": ["prisonblues"], "require_human_triage": False}
    base.update(pickup)
    return {"github": "acme/r", "issue_pickup": base}


def issue(number=1, labels=(), author="prisonblues"):
    return {"number": number, "labels": list(labels), "author": author}


def human_event(label="p0", actor="prisonblues", event="labeled"):
    return {"label": label, "actor": actor, "actor_type": "User", "event": event}


# ------------------------------------------------------------- label_names

def test_label_names_accepts_both_shapes_gh_produces():
    """The API shape and a plain list of strings, because the two producers
    differ and the difference must not be interesting to a caller — a second
    unwrapping at a call site is how one ends up comparing dicts to strings and
    matching nothing, silently."""
    assert appetite.label_names([{"name": "p0", "color": "abc"}]) == ["p0"]
    assert appetite.label_names(["p0", "bug"]) == ["p0", "bug"]
    assert appetite.label_names(None) == [] and appetite.label_names([]) == []


def test_label_names_drops_blanks_rather_than_counting_them():
    """A blank label cannot exist on GitHub, so it only arrives from a malformed
    stub — and counting it would satisfy `skip_when_unlabelled`, turning the gate
    off in exactly the case it exists for."""
    assert appetite.label_names([{"name": "  "}, {"name": ""}, {}, "", "  "]) == []
    assert appetite.label_names([{"name": " p0 "}]) == ["p0"]


# ----------------------------------------------------------- the defaults

def test_pickup_is_off_by_default():
    """No repo acquires an appetite by upgrading."""
    v = appetite.pickup_verdict({}, issue())
    assert v.allowed is False and v.setting == "issue_pickup.enabled"


def test_an_empty_author_allowlist_means_nobody_not_everybody():
    """#63's security section. This repo is public, anyone may open an issue, and
    under a watcher that text becomes an agent's instructions. Empty must be the
    closed end or the allowlist is decorative on the day it is introduced."""
    v = appetite.pickup_verdict(cfg(allowed_authors=[]), issue())
    assert v.allowed is False
    assert v.setting == "issue_pickup.allowed_authors"


def test_an_empty_only_labels_means_nothing_qualifies():
    """Turning the gate on is one decision; saying what may come through it is
    another, and a repo that has made only the first has not said "anything"."""
    v = appetite.pickup_verdict(cfg(only_labels=[]), issue(labels=["p0"]))
    assert v.allowed is False and v.setting == "issue_pickup.only_labels"


def test_require_human_triage_is_on_by_default():
    assert hr.DEFAULTS["issue_pickup"]["require_human_triage"] is True


def test_skip_when_unlabelled_is_on_by_default():
    assert hr.DEFAULTS["issue_pickup"]["skip_when_unlabelled"] is True


def test_the_default_skip_list_is_two_sevens_nines_vocabulary_not_a_second_one():
    """#86 proposed design/ui/decision-owed/needs-scoping, written when this repo
    had no labels at all. #279's six exist and are already produced; two
    vocabularies for one idea is drift this repo has paid for once."""
    assert hr.DEFAULTS["issue_pickup"]["skip_labels"] == ["needs-human/*"]


# --------------------------------------------------------- the author gate

def test_an_author_on_the_allowlist_passes():
    v = appetite.pickup_verdict(cfg(), issue(labels=["p0"], author="prisonblues"))
    assert v.allowed is True


def test_a_stranger_is_refused_and_named():
    """"An issue opened by a stranger triggers nothing, ever, and is visible as
    untriggered rather than ignored" — #63's acceptance criteria."""
    v = appetite.pickup_verdict(cfg(), issue(labels=["p0"], author="drive-by"))
    assert v.allowed is False
    assert "drive-by" in v.reason and "allowed_authors" in v.setting


def test_an_unreadable_author_is_refused_rather_than_waved_through():
    """`gh` returning no author is a read that failed, and a failed read must not
    be the answer that opens the gate."""
    v = appetite.pickup_verdict(cfg(), issue(labels=["p0"], author=""))
    assert v.allowed is False


def test_the_author_allowlist_is_case_insensitive():
    """GitHub treats logins case-insensitively, so a rules file that writes
    `PrisonBlues` has configured the gate rather than defeated it."""
    v = appetite.pickup_verdict(cfg(allowed_authors=["PrisonBlues"]),
                                issue(labels=["p0"], author="prisonblues"))
    assert v.allowed is True


# ---------------------------------------------------------- the skip gate

@pytest.mark.parametrize("cls", list(hr.DEFAULTS["issue_pickup"]["skip_labels"]) and
                         ["decision", "taste", "ui", "environment", "auth", "other"])
def test_every_needs_human_class_refuses(cls):
    """Guards the wiring between the glob and #279's vocabulary — a default list
    the matcher never consults would pass a narrower test."""
    v = appetite.refusal_verdict({}, [f"needs-human/{cls}"], number=7)
    assert v.allowed is False and v.setting == "issue_pickup.skip_labels"


def test_the_glob_covers_a_class_the_vocabulary_has_not_grown_yet():
    """#279's `other` is explicitly the hatch through which the vocabulary GROWS.
    A literal six-name list would go stale the day a seventh is added, and it
    would go stale silently — by letting the new class through."""
    v = appetite.refusal_verdict({}, ["needs-human/pricing"], number=7)
    assert v.allowed is False


def test_a_refusal_names_the_label_and_the_setting():
    """A refusal that does not say what fired is indistinguishable from a broken
    watcher, and the operator has several gates to go looking through."""
    v = appetite.refusal_verdict({}, ["bug", "needs-human/ui"], number=7)
    assert "'needs-human/ui'" in v.reason and "'bug'" not in v.reason
    assert v.setting == "issue_pickup.skip_labels"


def test_skip_labels_match_case_insensitively():
    """GitHub preserves the case a label was created with."""
    assert appetite.refusal_verdict({}, ["Needs-Human/UI"], number=7).allowed is False


def test_an_ordinary_label_passes_the_skip_gate():
    assert appetite.refusal_verdict({}, ["bug"], number=7).allowed is True


def test_an_unlabelled_issue_is_refused_by_default():
    """Nothing has triaged it, so nothing has established which class it is — and
    "no signal" must not read as "no objection"."""
    v = appetite.refusal_verdict({}, [], number=7)
    assert v.allowed is False and v.setting == "issue_pickup.skip_when_unlabelled"


def test_a_repo_that_labels_nothing_can_opt_back_in():
    v = appetite.refusal_verdict({"issue_pickup": {"skip_when_unlabelled": False}},
                                 [], number=7)
    assert v.allowed is True


def test_a_caller_that_had_a_human_name_the_work_can_decline_the_unlabelled_rule():
    """The epic driver's case. `skip_when_unlabelled` is about SELECTING out of an
    untriaged backlog; a human named the epic on the command line. `skip_labels`
    still applies, because a label a person applied does not stop meaning what it
    says because the issue arrived inside an epic."""
    assert appetite.refusal_verdict({}, [], number=7,
                                    unlabelled_refuses=False).allowed is True
    assert appetite.refusal_verdict({}, ["needs-human/decision"], number=7,
                                    unlabelled_refuses=False).allowed is False


def test_a_skip_label_beats_the_only_labels_allowlist():
    """A `p0` that a person has ALSO marked needs-human must be refused for the
    reason that actually matters. An operator told "it lacked p0" goes and adds
    p0; one told a decision is owed goes and makes the decision."""
    v = appetite.pickup_verdict(cfg(), issue(labels=["p0", "needs-human/decision"]))
    assert v.allowed is False and v.setting == "issue_pickup.skip_labels"


def test_a_repo_may_replace_the_skip_list():
    c = {"issue_pickup": {"skip_labels": ["wontfix"]}}
    assert appetite.refusal_verdict(c, ["needs-human/ui"], number=7).allowed is True
    assert appetite.refusal_verdict(c, ["wontfix"], number=7).allowed is False


def test_a_hand_built_cfg_without_the_block_lands_on_the_safe_end():
    """Any caller predating these blocks assembles a cfg by hand. A missing block
    must fall back to DEFAULTS per key, not to "no block, so no rules" — the
    latter is an open gate."""
    assert appetite.refusal_verdict({"issue_pickup": None}, []).allowed is False
    assert appetite.refusal_verdict({"issue_pickup": None},
                                    ["needs-human/ui"]).allowed is False


def test_malformed_skip_labels_is_a_hard_error_not_a_silent_open_door():
    """Unlike a misspelled KEY (which leaves the setting at its safe default), a
    malformed VALUE would leave the gate reading as configured while matching
    nothing at all."""
    for bad in ("needs-human/*", [1, 2], [{"name": "ui"}]):
        with pytest.raises(SystemExit):
            appetite.refusal_verdict({"issue_pickup": {"skip_labels": bad}}, ["x"])


# ------------------------------------------------------- the triage gate

def test_a_label_an_agent_applied_does_not_authorise_the_agent():
    """The load-bearing line of #85. If agents can both apply a label and act on
    it, the gate is decorative — #78's `judge_model` problem one level out."""
    v = appetite.pickup_verdict(
        cfg(require_human_triage=True, agent_actors=["claude-agent"]),
        issue(labels=["p0"]),
        events=[human_event(actor="claude-agent")])
    assert v.allowed is False
    assert v.setting == "issue_pickup.require_human_triage"


def test_a_label_a_bot_applied_does_not_authorise_either():
    """Without naming the login. GitHub already reports the actor TYPE, and a
    gate that only knows the logins somebody remembered to configure is a filter
    wearing an allowlist's clothes."""
    v = appetite.pickup_verdict(
        cfg(require_human_triage=True), issue(labels=["p0"]),
        events=[{"label": "p0", "actor": "some-app", "actor_type": "Bot"}])
    assert v.allowed is False


def test_a_label_a_person_applied_authorises():
    v = appetite.pickup_verdict(cfg(require_human_triage=True),
                                issue(labels=["p0"]), events=[human_event()])
    assert v.allowed is True and "prisonblues" in v.reason


def test_no_readable_events_is_not_triaged():
    """A 404 on the event log is an unreadable log, not an empty one, and the
    difference decides a gate. Unreadable must fail closed."""
    v = appetite.pickup_verdict(cfg(require_human_triage=True),
                                issue(labels=["p0"]), events=[])
    assert v.allowed is False
    assert "no label events" in v.reason


def test_a_label_present_with_no_event_is_reported_as_such():
    """Three states — nothing labelled it, an agent labelled it, the log is
    unreadable — and a caller acting on the wrong one acts wrongly. Saying "a bot
    did it" when no such event exists sends the reader hunting a bot that was
    never there."""
    v = appetite.pickup_verdict(cfg(require_human_triage=True),
                                issue(labels=["p0"]),
                                events=[human_event(label="something-else")])
    assert v.allowed is False
    assert "no `labeled` event records who applied it" in v.reason


def test_only_the_matched_label_can_authorise():
    """A human applying `documentation` has not said "an agent may work this"; the
    label that authorises has to be the one the allowlist named."""
    v = appetite.pickup_verdict(cfg(require_human_triage=True),
                                issue(labels=["p0", "documentation"]),
                                events=[human_event(label="documentation")])
    assert v.allowed is False


def test_triage_matching_is_case_insensitive_on_both_halves():
    v = appetite.pickup_verdict(cfg(only_labels=["P0"], require_human_triage=True),
                                issue(labels=["p0"]),
                                events=[human_event(label="p0", actor="PrisonBlues")])
    assert v.allowed is True


# ------------------------------------------------------------ the CLI shape

def test_a_refusal_exits_three_and_an_allow_exits_zero(capsys):
    """"No" and "misconfigured" look identical from a caller's side, and an agent
    that cannot tell them apart routes around the gate instead of fixing it."""
    assert appetite._emit(appetite.Verdict(False, "nope", "a.b"), False) == 3
    assert appetite._emit(appetite.Verdict(True, "yep", "a.b"), False) == 0
    out = capsys.readouterr().out
    assert "REFUSED: nope  [a.b]" in out and "ALLOWED: yep" in out


def test_the_json_verdict_carries_the_setting(capsys):
    appetite._emit(appetite.Verdict(False, "nope", "issue_pickup.enabled"), True)
    got = json.loads(capsys.readouterr().out)
    assert got == {"allowed": False, "reason": "nope",
                   "setting": "issue_pickup.enabled"}


def test_shared_flags_are_accepted_on_either_side_of_the_subcommand(monkeypatch):
    """A usage failure for a gate reads as the gate being broken, so the next
    agent works around it rather than reordering the words."""
    seen = {}
    monkeypatch.setattr(appetite, "cmd_pickup", lambda a: seen.update(vars(a)) or 0)
    appetite.main(["pickup", "85", "--repo", "x", "--json"])
    assert (seen["repo"], seen["as_json"]) == ("x", True)
    seen.clear()
    appetite.main(["--repo", "y", "--json", "pickup", "85"])
    assert (seen["repo"], seen["as_json"]) == ("y", True)


def test_a_repo_flag_before_the_subcommand_is_not_silently_dropped(monkeypatch):
    """A parent parser's ordinary default is re-applied by the subparser, which
    would turn `--repo x pickup 85` into repo=None: the flag accepted, ignored,
    and the gate answering about the wrong repo."""
    seen = {}
    monkeypatch.setattr(appetite, "cmd_pickup", lambda a: seen.update(vars(a)) or 0)
    appetite.main(["--repo", "the-one-that-matters", "pickup", "85"])
    assert seen["repo"] == "the-one-that-matters"


# ----------------------------------------------------------------- filing

def budget(tmp_path, monkeypatch, *, run_id="run-1", is_unattended=False, **filing):
    monkeypatch.setattr(appetite, "STATE_DIR", tmp_path / "state")
    return appetite.FilingBudget({"github": "acme/r", "issue_filing": filing},
                                 repo="acme/r", run_id=run_id,
                                 is_unattended=is_unattended)


def test_an_unattended_run_may_not_file_by_default(tmp_path, monkeypatch):
    """#40's standing decision, restated as config so a repo can relax it
    deliberately rather than by accident."""
    b = budget(tmp_path, monkeypatch, is_unattended=True)
    v = b.check("something", duplicates=[])
    assert v.allowed is False and v.setting == "issue_filing.unattended"
    assert "report what you would have filed" in v.reason


def test_an_unattended_run_may_file_where_a_repo_has_said_so(tmp_path, monkeypatch):
    b = budget(tmp_path, monkeypatch, is_unattended=True, unattended=True)
    assert b.check("something", duplicates=[]).allowed is True


def test_the_cap_is_one_per_run_by_default():
    assert hr.DEFAULTS["issue_filing"]["max_per_run"] == 1


def test_the_cap_is_enforced_across_separate_invocations(tmp_path, monkeypatch):
    """The gate is a CLI invoked once per candidate, so a count held only in the
    process it constrains is self-reported rather than enforced."""
    first = budget(tmp_path, monkeypatch)
    assert first.check("one", duplicates=[]).allowed is True
    first.record("one")

    second = budget(tmp_path, monkeypatch)          # a whole new process
    v = second.check("two", duplicates=[])
    assert v.allowed is False and v.setting == "issue_filing.max_per_run"
    assert v.detail == {"filed": 1, "max_per_run": 1}


def test_a_different_run_gets_its_own_budget(tmp_path, monkeypatch):
    budget(tmp_path, monkeypatch, run_id="run-1").record("one")
    assert budget(tmp_path, monkeypatch, run_id="run-2") \
        .check("two", duplicates=[]).allowed is True


def test_without_a_run_id_only_this_process_is_counted(tmp_path, monkeypatch):
    """Stated as a test because it is the gate's weak form and a caller needs to
    know it is choosing it: no run id, no cross-invocation enforcement."""
    b = budget(tmp_path, monkeypatch, run_id="")
    assert b.check("one", duplicates=[]).allowed is True
    b.record("one")
    assert b.check("two", duplicates=[]).allowed is False   # same process, counted
    assert not (tmp_path / "state").exists()                # nothing persisted


@pytest.mark.parametrize("corruption", ["{not json", '{"filed": "lots"}',
                                        '{"filed": -1}', '{"no": "count"}'])
def test_a_corrupt_tally_refuses_rather_than_handing_back_a_full_budget(
        tmp_path, monkeypatch, corruption):
    """An unreadable tally read as zero gives a run that may have spent its budget
    a fresh one — the corruption silently REOPENS the gate, which is the direction
    every default in this module leans against."""
    b = budget(tmp_path, monkeypatch)
    b.record("one")
    b._state_path.write_text(corruption)
    v = b.check("two", duplicates=[])
    assert v.allowed is False and v.setting == "issue_filing.max_per_run"
    assert "could not be read" in v.reason or "nonsense count" in v.reason


def test_an_unsearched_dedup_is_not_an_empty_one(tmp_path, monkeypatch):
    """`None` and `[]` are the same value to a careless caller and must not be the
    same answer here: one is "I looked and found nothing", the other is "I never
    looked"."""
    v = budget(tmp_path, monkeypatch).check("something", duplicates=None)
    assert v.allowed is False and v.setting == "issue_filing.require_dedup_check"


def test_candidate_duplicates_are_named_so_the_caller_can_add_to_one(tmp_path,
                                                                     monkeypatch):
    v = budget(tmp_path, monkeypatch).check(
        "the loop files too much",
        duplicates=[{"number": 85, "title": "gate the appetite"}])
    assert v.allowed is False and "#85" in v.reason


def test_a_repo_may_turn_the_dedup_requirement_off(tmp_path, monkeypatch):
    b = budget(tmp_path, monkeypatch, require_dedup_check=False)
    assert b.check("something", duplicates=None).allowed is True


def test_the_dedup_search_uses_the_titles_own_words(monkeypatch):
    """An exact-title match would pass every single time and read as a working
    gate; the duplicate that matters is the one somebody phrased differently."""
    seen = {}

    def fake(args, repo):
        seen["args"] = args
        return [{"number": 9, "title": "t"}]

    monkeypatch.setattr(appetite, "gh_json", fake)
    got = appetite.duplicate_search("acme/r", "Gate the loop's appetite for files")
    assert got == [{"number": 9, "title": "t"}]
    terms = seen["args"][seen["args"].index("--search") + 1].split()
    assert "appetite" in terms and "the" not in terms


def test_a_title_with_no_usable_words_is_unsearched_and_not_searched_clean(
        tmp_path, monkeypatch):
    """The bypass this closes: `[]` means "searched, found nothing" and OPENS the
    gate, so a filer could defeat require_dedup_check by choosing a title of short
    words. Searching for the empty string is not the fix either — `gh` answers
    that with the whole backlog, refusing everything for a reason nobody can act
    on."""
    monkeypatch.setattr(appetite, "gh_json",
                        lambda a, r: pytest.fail("must not search"))
    assert appetite.duplicate_search("acme/r", "a b c!!") is None
    v = budget(tmp_path, monkeypatch).check(
        "a b c!!", duplicates=appetite.duplicate_search("acme/r", "a b c!!"))
    assert v.allowed is False and v.setting == "issue_filing.require_dedup_check"


def test_a_search_outage_crashes_rather_than_reading_as_no_duplicates(monkeypatch):
    """Search being unavailable must not be the answer that lets filing proceed —
    an outage would silently open the gate this setting exists to close."""
    def boom(args, repo):
        raise RuntimeError("gh is down")

    monkeypatch.setattr(appetite, "gh_json", boom)
    with pytest.raises(RuntimeError):
        appetite.duplicate_search("acme/r", "the loop files far too much")


# ------------------------------------- holes a second reviewer found (codex)

def test_a_removed_and_reapplied_label_does_not_inherit_the_old_signature():
    """The self-approval hole, one step further round. A person applies `p0`,
    someone takes it off, an agent puts it back — and a scan for "any historical
    labeled by a human" finds the person's stale event and lets the agent work
    under their signature. Only the CURRENT application counts."""
    v = appetite.pickup_verdict(
        cfg(require_human_triage=True, agent_actors=["claude-agent"]),
        issue(labels=["p0"]),
        events=[human_event(actor="prisonblues"),
                human_event(actor="prisonblues", event="unlabeled"),
                human_event(actor="claude-agent")])
    assert v.allowed is False
    assert v.setting == "issue_pickup.require_human_triage"


def test_a_reapplication_by_a_person_still_authorises():
    """The mirror of the above, so the fix is a narrowing and not a blanket
    refusal of any issue whose label was ever touched twice."""
    v = appetite.pickup_verdict(
        cfg(require_human_triage=True, agent_actors=["claude-agent"]),
        issue(labels=["p0"]),
        events=[human_event(actor="claude-agent"),
                human_event(actor="claude-agent", event="unlabeled"),
                human_event(actor="prisonblues")])
    assert v.allowed is True


def test_a_label_removed_and_left_off_is_reported_as_the_disagreement_it_is():
    """The issue's labels say `p0` and its event log says it was taken off. Three
    words about which is stale beat a refusal the reader has to reverse-engineer."""
    v = appetite.pickup_verdict(cfg(require_human_triage=True),
                                issue(labels=["p0"]),
                                events=[human_event(),
                                        human_event(event="unlabeled")])
    assert v.allowed is False
    assert "REMOVED" in v.reason


@pytest.mark.parametrize("key", ["enabled", "require_human_triage"])
def test_a_quoted_false_is_refused_rather_than_read_as_true(key):
    """`"false"` is a non-empty string and therefore TRUTHY, so the natural
    hand-edit does the opposite of what the file exists for. `harness_rules`
    already checks overlay VALUES and not just names for exactly this reason, and
    every switch here defaults closed — so the direction of the mistake is always
    "gate silently open"."""
    with pytest.raises(SystemExit) as e:
        appetite.pickup_verdict(cfg(**{key: "false"}), issue(labels=["p0"]))
    assert key in str(e.value)


def test_a_quoted_false_is_refused_for_the_unlabelled_rule_too():
    """Reached only on an issue with no labels, which is the branch it guards."""
    with pytest.raises(SystemExit) as e:
        appetite.refusal_verdict({"issue_pickup": {"skip_when_unlabelled": "false"}},
                                 [], number=7)
    assert "skip_when_unlabelled" in str(e.value)


def test_a_quoted_false_is_refused_in_the_filing_block_too(tmp_path, monkeypatch):
    """`unattended` is only consulted on an unattended run, and
    `require_dedup_check` only once the run is past the cap — so each is reached
    from the state that actually reads it."""
    with pytest.raises(SystemExit) as e:
        budget(tmp_path, monkeypatch, is_unattended=True,
               unattended="false").check("x", duplicates=[])
    assert "unattended" in str(e.value)

    with pytest.raises(SystemExit) as e:
        budget(tmp_path, monkeypatch, is_unattended=False,
               require_dedup_check="false").check("x", duplicates=[])
    assert "require_dedup_check" in str(e.value)


@pytest.mark.parametrize("bad", ["1", 1.5, True, -1])
def test_a_max_per_run_that_is_not_a_count_is_refused(tmp_path, monkeypatch, bad):
    """Refused rather than coerced: a string here would compare against an int and
    raise deep inside the check, where the traceback names neither the setting nor
    the repo."""
    b = budget(tmp_path, monkeypatch, max_per_run=bad)
    with pytest.raises(SystemExit) as e:
        b.check("something", duplicates=[])
    assert "max_per_run" in str(e.value)


def test_a_null_cap_means_no_cap(tmp_path, monkeypatch):
    b = budget(tmp_path, monkeypatch, max_per_run=None)
    b.record("one")
    b.record("two")
    assert b.check("three", duplicates=[]).allowed is True


def test_an_allow_without_a_run_id_says_the_cap_is_not_being_enforced(tmp_path,
                                                                     monkeypatch):
    """A bypass by omission, and one nobody would notice: without `--run` a caller
    invoking the CLI once per candidate gets `1/1` every time, and the weak form
    and the strong form otherwise print the same "within budget"."""
    weak = budget(tmp_path, monkeypatch, run_id="").check("x", duplicates=[])
    strong = budget(tmp_path, monkeypatch, run_id="r").check("x", duplicates=[])
    assert weak.allowed and strong.allowed
    assert "pass --run" in weak.reason and weak.detail["persisted"] is False
    assert "pass --run" not in strong.reason and strong.detail["persisted"] is True


def test_the_tally_is_written_atomically(tmp_path, monkeypatch):
    """A crash mid-write would otherwise leave truncated JSON, which `_read_tally`
    now has to refuse — turning a killed run into a permanently blocked one."""
    b = budget(tmp_path, monkeypatch)
    b.record("one")
    assert json.loads(b._state_path.read_text())["filed"] == 1
    assert not list(b._state_path.parent.glob("*.tmp")), "no temp file left behind"


def test_an_event_naming_no_actor_does_not_count_as_a_human():
    """A deleted account, or the API omitting the actor block. "Somebody applied
    this and we cannot see who" is exactly the state this check exists to
    distinguish, and reading it as a human is the same unreadable-means-yes
    mistake as a corrupt tally meaning zero — an agent whose actor block failed to
    serialise would authorise itself."""
    v = appetite.pickup_verdict(
        cfg(require_human_triage=True), issue(labels=["p0"]),
        events=[{"label": "p0", "actor": "", "actor_type": "",
                 "event": "labeled"}])
    assert v.allowed is False
    assert "names no actor" in v.reason


@pytest.mark.parametrize("key", ["allowed_authors", "only_labels", "agent_actors",
                                 "skip_labels"])
def test_a_bare_string_where_a_list_belongs_is_refused(key):
    """A bare string is ITERABLE, so `[str(x) for x in "claude"]` yields single
    characters — a plausible-looking list that matches no login and no label. For
    `only_labels`/`allowed_authors` that fails closed and merely confuses; for
    `agent_actors` it fails OPEN, and the one setting whose job is to stop
    self-approval stops doing it."""
    with pytest.raises(SystemExit) as e:
        appetite.pickup_verdict(cfg(require_human_triage=True, **{key: "p0"}),
                                issue(labels=["p0"]), events=[human_event()])
    assert key in str(e.value)


def test_a_string_agent_actors_cannot_let_the_named_agent_self_approve():
    """The finding stated as the attack it enables: with `agent_actors` mangled
    into characters, `claude-agent` is no longer recognised as an agent and its
    own label authorises its own work."""
    with pytest.raises(SystemExit):
        appetite.pickup_verdict(
            cfg(require_human_triage=True, agent_actors="claude-agent"),
            issue(labels=["p0"]),
            events=[human_event(actor="claude-agent")])

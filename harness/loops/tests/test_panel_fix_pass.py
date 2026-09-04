"""#624: the fix pass is a first-class artifact, and it is not a leaderboard.

The panel scores reviewers. Every seat's findings are recorded, judged, chained
across rounds and rolled up. The **fixer** — the actor that writes the code that
produces the next round's findings — was described by nothing:
`fix-and-review.md:72` says outright that the board cannot reliably tell a fixer's
work from a reviewer's. On `prisonblues/lexray#1780` the four passes ran to
+850/-314 across 11 files, +322/-49 across 9, +356/-41 across 12 (seven of them
files no round had read) and +142/-31 across 7, and every one of those numbers was
reconstructed from `git` by hand, afterwards, to file the issue.

This file has two halves, and the second is unusual enough to say why it exists.

**The first half is the record**: what it carries, where each field comes from, and
the null-versus-zero discipline it inherits from every producer it reads. Almost
nothing here is a new measurement — the record is an ASSEMBLY of siblings that were
already deriving these numbers once a round and filing them under the round's stop
decision — so most of these assertions are about the assembly being faithful, which
is the failure mode an assembly has.

**The second half is the absence of a leaderboard**, and it is a requirement rather
than a gap. #624's own second opinion: every obvious ratio over a fix pass is
gameable in a direction worse than the disease — lines per finding cleared rewards
compressed and superficial fixes, findings introduced per pass rewards weakening
tests and avoiding the files most likely to be read, new files opened rewards
refusing a cross-file repair that is genuinely required (a P1 left unfixed to
protect a metric), and share of fixes still standing a round later is invalid under
increment scope because the later round may never have re-read the repair.

A constraint like that is not kept by intending to keep it, so it is pinned four
ways: no ratio anywhere in the record (a vocabulary and a walk over every numeric
leaf), no actor key (a walk over every key name), no gate (`round_stop` reaches the
same verdict either way, and does not mention the record), and the fixer is told the
record is made — because a measurement the measured party does not know about is a
trap rather than a brake, which is #622's rule applied to an artifact instead of to
a count.
"""

import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh`, the seam every stub here replaces
import panel_rounds  # noqa: E402
from conftest import gh_stub  # noqa: E402

CFG = {"github": "acme/board", "path": "/nonexistent/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}

LOOPS = Path(__file__).resolve().parent.parent


def _revert(**over):
    """`revert_state`'s own output for a linear, readable range — the ordinary case,
    and the only `kind` that can name a commit range."""
    return {"kind": panel.FIX_RANGE_OK, "why": None, "base": "a" * 40,
            "head": "b" * 40, "spans": 1, "round": 1,
            "range": "aaaaaaaa..bbbbbbbb", "command": None, "no_command": None,
            "commits": [], "commit_count": 3, "merges": 0, "scope": "increment",
            "removes": [], "costs": [], "still_open": [], **over}


def _referee(production=10, test=5, prose=0):
    """`referee_state`'s own output, which is the only churn source the record reads."""
    return panel.referee_state({"production": production, "test": test,
                                "prose": prose}, True)


def _brief(round_no=1, findings=3, **over):
    """`budgeted_brief`'s own output, whose `round` and `findings` the record carries
    rather than re-deriving from a Baseline it would have to re-validate."""
    return {"round": round_no, "findings": findings, "budgeted": 1,
            "all_budgeted": False, "why": "one was mandatory work", **over}


def _surface(files=("app/sync.py", "nginx/site.conf"), new=("nginx/site.conf",),
             prior=7):
    return {"files": list(files), "new_files": list(new), "count": len(new),
            "prior_files": prior}


def _record(**over):
    """One record built from the ordinary inputs, with `over` applied to the call."""
    kwargs = {"revert": _revert(), "referee": _referee(), "surface": _surface(),
              "brief": _brief(),
              "cleared": [{"key": "k1", "severity": "P2", "file": "a", "line": 1,
                           "title": "t"}],
              "still_open": [{"key": "k2", "severity": "P1", "file": "b", "line": 2,
                              "title": "u"}],
              "introduced": 4, "cycle": "cyc-1", "source": "compare",
              "narrowed": ["k9"],
              "declined": {"k3": panel.Declination(2, "over the growth ceiling")},
              "escalated": {"k4": 2, "k5": 1}}
    kwargs.update(over)
    return panel.fix_pass_record(2, **kwargs)


def _leaves(node, path=""):
    """Every `(dotted path, value)` scalar in a nested record, for the walks below.

    Written out rather than done with a JSON round trip because two of the four
    guarantees are about KEY NAMES as well as values, and both walks want the path
    that reached a leaf so a failure names the field rather than the fact.

    An EMPTY dict or list yields itself rather than nothing, so its own key is still
    on a path somebody can walk: a second opinion on #705 pointed out that
    `{"ratio": {}}` reached neither guarantee, because a container with nothing
    inside it produced no leaf at all and so produced no path."""
    if isinstance(node, dict) and node:
        for k, v in node.items():
            yield from _leaves(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list) and node:
        for i, v in enumerate(node):
            yield from _leaves(v, f"{path}[{i}]")
    else:
        yield path, node


def _key_names(record):
    """Every key name on every path through the record, at every depth.

    :func:`_leaves` hands back a dotted path and the two vocabulary walks below used
    to test only its LAST segment, which is the leaf's own key — so an intermediate
    key was never checked against either vocabulary at all. A second opinion on #705
    broke both guarantees on that: `{"score": {"value": 1}}` yields `score.value`,
    whose last segment is `value`, and `{"agent": {"name": "x"}}` yields `agent.name`,
    whose last segment is `name`. Neither vocabulary names either of those, so a
    record carrying a `score` block or an `agent` block would have passed the two
    tests written to make it impossible.

    Yields `(path, segment)` so a failure still names the whole field and not just
    the offending word. List indices are stripped: `gaps[0]` is the key `gaps`.
    """
    for path, _value in _leaves(record):
        for segment in path.split("."):
            yield path, segment.split("[")[0]


# ------------------------------------------------------------------ what it carries

def test_the_record_names_the_pass_by_its_RANGE_and_the_round_that_briefed_it():
    """The whole address of a fix pass is the two rounds it sits between, and #624's
    first field is "the commit range, and which round briefed it". Both ends, the
    display span, which of the three readers supplied the diff, and the commit shape —
    all read off `revert_state`'s output rather than re-derived, because a second
    reading of one range is a second answer to the question "which pass is this"."""
    got = _record()["range"]
    assert got == {"base": "a" * 40, "head": "b" * 40,
                   "span": "aaaaaaaa..bbbbbbbb", "kind": panel.FIX_RANGE_OK,
                   "why": None, "spans": 1, "commits": 3, "merges": 0,
                   "source": "compare"}
    assert _record()["read_round"] == 2
    assert _record()["brief"]["round"] == 1


def test_the_churn_split_is_the_one_the_ROUND_measured_and_not_a_second_one():
    """`referee_state`'s split, published under the record's own key. #554 has computed
    this for every pass since it landed and #618 put it in the round table; what was
    missing is that it was never attached to the pass. Reading `referee` rather than
    re-splitting the diff is this module's standing rule, and here a second derivation
    would show up as a payload disagreeing with itself about one pass."""
    assert _record()["churn"] == {"production": 10, "test": 5, "prose": 0,
                                  "churn": 15}


def test_the_surface_is_fix_surface_states_own_output():
    """#619's set difference, normalised by the same function `round_stop` sends it
    through — so `fix_pass.surface` and `round_stop.fix_surface` cannot publish two
    readings of one measurement. That is the guarantee, and it is asserted as an
    equality against the other consumer rather than against a literal."""
    surface = _surface()
    assert _record()["surface"] == panel.round_stop(
        2, 5, [], [], [], surface=surface)["fix_surface"]


def test_cleared_and_still_open_are_KEYS_and_severities_not_the_revert_records():
    """The identity of a finding is its key — it is what joins to a stored finding —
    and the five-field records are already in `round_stop.revert`, read out of the SAME
    `fix_pass_outcome` call. Carrying them twice in one payload would be two copies
    that can be edited apart; carrying the key and the severity is the smallest thing
    that still lets a reader recognise what the pass was sent to do."""
    got = _record()
    assert got["cleared"] == [{"key": "k1", "severity": "P2"}]
    assert got["still_open"] == [{"key": "k2", "severity": "P1"}]


def test_introduced_is_what_the_NEXT_round_attributed_to_the_pass():
    """#624 calls this "the only interesting one", and says it already exists per-round
    as `introduced` and is simply not attached to the pass that caused it. This is the
    attachment: the same number the round publishes in `provenance_counts`, on the
    record of the pass it is a statement about."""
    assert _record()["introduced"] == 4
    assert _record(introduced=None)["introduced"] is None


def test_placed_says_how_much_of_the_brief_this_round_could_KEY():
    """`cleared + still_open` does not have to equal `findings`, and a reader seeing
    two lists that do not add up to the total beside them has no way to tell a dropped
    record from a lost one. `Baseline.fixed_findings` drops a record with no key or no
    file; `fixed_severities`, which is what `findings` counts, counts every entry in
    both brief buckets including one that is not a record at all (#694's own
    correction). So the record publishes both numbers."""
    got = _record(brief=_brief(findings=5))
    assert got["brief"] == {"round": 1, "findings": 5, "placed": 2,
                            "why": "one was mandatory work"}
    assert got["counts"]["cleared"] + got["counts"]["still_open"] == 2


def test_the_briefs_own_SENTENCE_travels_with_its_nulls():
    """`budgeted_brief` distinguishes "there was no earlier round" from "the earlier
    round asked its fixer to fix nothing" in `why` and not in `findings`, because both
    are null there. Re-deriving the distinction here would be a second opinion about
    the same payload; carrying the sentence is what lets a reader of one stored record
    tell which of the two it was."""
    none = _record(brief=panel_rounds.NO_PRIOR_BRIEF)
    assert none["brief"]["findings"] is None
    assert none["brief"]["why"] == panel_rounds.NO_PRIOR_BRIEF["why"]
    empty = _record(brief=_brief(findings=0, why="round 1 asked its fixer to fix "
                                                 "nothing"))
    assert empty["brief"]["findings"] == 0
    assert "asked its fixer to fix nothing" in empty["brief"]["why"]


# ---------------------------------------------------- the record's own declared gaps

def test_the_record_publishes_what_it_CANNOT_say():
    """An artifact whose gaps live only in the release notes reads as complete two
    years later, which is why these travel with the row — the same reason
    `NO_PRIOR_BRIEF`'s `why` travels with a null verdict.

    The first gap is the one #624 was scoped around. #621's scope decision parks #623
    (typed obligations), so the per-finding `fixed | refuted | deferred` the issue also
    asks for is not buildable: a diff carries no attribution from a churned line back
    to the finding it answered, and the only remaining source is the fixer's account of
    itself — the thing this family of issues exists to remove. It is DECLARED here
    rather than quietly omitted, and it points at where the declared form does live."""
    gaps = _record()["gaps"]
    assert gaps == list(panel.FIX_PASS_GAPS)
    joined = " ".join(gaps)
    assert "#623" in joined and "review_finding_outcomes" in joined
    # The other three, each a claim a reader could otherwise make wrongly off this
    # record: that `cleared` means repaired, that a fixer can be identified, and that
    # a cost per finding could be derived from what is here.
    assert "verified fixed" in joined and "increment" in joined
    assert "no actor" in joined
    assert "no per-finding line ranges" in joined


def test_declared_is_kept_apart_from_every_measurement_and_dated_by_THIS_round():
    """The two things on this record that are not derived. They are here because a
    declaration the loop already ACTS on has to be visible on the pass that made it —
    `narrowed` clears a finding, `escalated` takes one out of every stop rule,
    `declined` files a veto — and an auditable record of a consequential claim is worth
    more than a silence.

    Dated by the round that RECEIVED them: `--narrowed`, `--declined` and `--escalated`
    are passed to the round that reads the pass, so an entry stamped with this round is
    one made about the pass this record describes, and an inherited entry belongs to an
    earlier pass and is left there. `k5` is escalated in round 1 and is not this
    pass's; `k4` is."""
    got = _record()
    assert got["declared"] == {"narrowed": ["k9"], "declined": ["k3"],
                               "escalated": ["k4"]}
    assert "k5" not in got["declared"]["escalated"]


def test_no_declaration_reaches_any_COUNT():
    """The segregation is only worth something if nothing downstream of it is arithmetic
    over a self-report. Three declarations added, and every count unmoved."""
    plain = _record(narrowed=(), declined=None, escalated=None)
    loud = _record(narrowed=["a", "b"],
                   declined={"c": panel.Declination(2, "no budget")},
                   escalated={"d": 2})
    assert plain["counts"] == loud["counts"]
    assert set(loud["counts"]) == set(panel.FIX_PASS_COUNTS)


# --------------------------------------------- null is "no pass", and never a zero

def test_round_one_has_NO_RECORD_because_there_was_no_pass():
    """`REVERT_NOT_ASKED` is the one verdict in this module that means "there was no
    earlier round to have a range with", and it is reused rather than re-derived from a
    round number — a run OUTSIDE a cycle has no earlier round whatever its round number
    says."""
    assert panel.fix_pass_record(1, revert=_revert(kind=panel.REVERT_NOT_ASKED),
                                 referee=None) is None
    assert panel.fix_pass_record(2, revert=None, referee=None) is None
    assert panel.fix_pass_record(2, revert="not a mapping", referee=None) is None


def test_a_pass_that_HAPPENED_and_could_not_be_read_gets_a_record_with_NULLS():
    """The distinction the whole record turns on. A rewritten branch, an API refusal or
    a baseline naming no commit is a pass that exists and cannot be seen — and "it
    opened no file and churned no line" is the flattering direction on every claim this
    artifact makes, which is the claim a record of zeros would publish.

    `range.kind` is what tells this apart from round 1, and it is why the absence is
    inside the record rather than instead of it."""
    got = panel.fix_pass_record(2, revert=_revert(kind=panel.FIX_RANGE_REWRITTEN,
                                                  why="the branch was rewritten"),
                                referee=None, surface=None, brief=_brief())
    assert got is not None
    assert got["range"]["kind"] == panel.FIX_RANGE_REWRITTEN
    assert got["range"]["why"] == "the branch was rewritten"
    assert got["churn"] == {"production": None, "test": None, "prose": None,
                            "churn": None}
    assert got["surface"] is None
    assert got["counts"]["churn"] is None and got["counts"]["new_files"] is None


def test_a_pass_that_churned_nothing_reads_as_UNMEASURED_on_churn_cells_own_terms():
    """One presence test shared with `churn_cells`, and its one accepted conflation: a
    pass that genuinely churned nothing and a round with no readable range record zeros
    in every bucket and the payload distinguishes them nowhere else. The cost is a real
    empty fix range rendering as unknown; the alternative is publishing `0 production`
    for a pass nobody could see, on the very claim this record exists to make."""
    got = _record(referee=_referee(production=0, test=0, prose=0))
    assert got["churn"] == {"production": None, "test": None, "prose": None,
                            "churn": None}
    assert got["churn"] == dict(panel.churn_cells(_referee(0, 0, 0)), churn=None)


def test_a_pass_that_opened_no_new_file_publishes_a_measured_ZERO():
    """The healthy answer, and it has to be distinguishable from the blind one:
    `fix_surface_state` publishes `count: 0` as "a pass was measured and opened no new
    file" and `None` as "nobody measured", and the record carries both readings
    through."""
    got = _record(surface=_surface(files=("app/sync.py",), new=(), prior=4))
    assert got["surface"]["count"] == 0 and got["surface"]["new_files"] == []
    assert got["counts"]["new_files"] == 0 and got["counts"]["files"] == 1


def test_a_brief_nobody_could_read_publishes_no_CLEARED_count_rather_than_a_zero():
    """`briefed: None` covers round 1, an unreadable baseline and an anchor that asked
    for nothing — `budgeted_brief` tells those apart in `why` — and a `0` for `cleared`
    there would say the pass cleared nothing when what happened is that nobody knows
    what it was sent to do."""
    got = _record(brief=panel_rounds.NO_PRIOR_BRIEF, cleared=(), still_open=())
    assert got["counts"]["briefed"] is None
    assert got["counts"]["placed"] is None
    assert got["counts"]["cleared"] is None
    assert got["counts"]["still_open"] is None


def test_spans_says_when_the_word_pass_is_wrong_about_the_row():
    """`Baseline.head_sha` is the latest earlier round that SUPPLIED a commit, not the
    latest that ran, so a round whose payload recorded none leaves the range spanning
    two fix phases. `revert_state` already counts them and the record carries the count
    rather than re-deriving it: what a wide span makes wrong is not the range, which is
    exactly the one provenance attributed over — it is the noun."""
    assert _record(revert=_revert(spans=2))["range"]["spans"] == 2
    assert _record(revert=_revert(spans=None))["range"]["spans"] is None


# ------------------------------------------- the counts block is a faithful projection

def test_every_count_is_the_records_own_number():
    """`counts` exists so a board can carry the numbers on a run LIST without the path
    lists and the finding keys, which is the cut `_run_view` already makes for
    `unread_files` and `harness_path`. That makes it a PROJECTION, and a projection can
    drift from what it projects — so every entry is asserted against the block it came
    from rather than against a literal."""
    got = _record()
    c = got["counts"]
    assert c["briefed"] == got["brief"]["findings"]
    assert c["placed"] == got["brief"]["placed"]
    assert c["cleared"] == len(got["cleared"])
    assert c["still_open"] == len(got["still_open"])
    for kind in ("production", "test", "prose", "churn"):
        assert c[kind] == got["churn"][kind]
    assert c["files"] == len(got["surface"]["files"])
    assert c["new_files"] == got["surface"]["count"]
    assert c["introduced"] == got["introduced"]


def test_the_counts_vocabulary_is_exactly_what_the_block_holds():
    """The board reads this block against `FIX_PASS_COUNTS` and drops a key it has no
    bucket for, so a key added here without the vocabulary would be stored by the panel
    and dropped by the board in silence — #93's own failure mode, which
    `tests/test_payload_key_drift.py` exists to prevent one level up."""
    assert tuple(_record()["counts"]) == panel.FIX_PASS_COUNTS
    assert len(set(panel.FIX_PASS_COUNTS)) == len(panel.FIX_PASS_COUNTS)


# ------------------------------------------------------ and it is NOT a leaderboard

def test_there_is_no_RATIO_anywhere_in_the_record():
    """#624's constraint, walked rather than intended. Every numeric leaf is an `int`
    or `None` — never a float, which is the shape a share or a rate arrives in — and no
    key name is drawn from the vocabulary a quotient would be named with.

    The numerators and the denominators are all published, deliberately: a consumer
    that wants a ratio has to write it down in its own code, where somebody can argue
    with which of the four gameable ones it is."""
    forbidden = ("share", "ratio", "rate", "per_", "score", "rank", "index",
                 "average", "mean", "percent", "efficiency", "density")
    got = _record()
    # EVERY segment, not just the leaf's own key — `_key_names` says what that missed.
    for path, name in _key_names(got):
        assert not any(word in name for word in forbidden), path
    for path, value in _leaves(got):
        assert not isinstance(value, float), f"{path} is a float: {value!r}"


def test_the_record_carries_no_ACTOR_and_that_is_the_strongest_guarantee_here():
    """It names the PASS — by its commit range and the round that briefed it — and
    never the agent, model or session that performed it. An artifact with no actor key
    cannot be aggregated into a table of fixers at all, which is a stronger guarantee
    than a policy of not writing that query, and it is the reason `gaps` states the
    absence rather than apologising for it.

    Walked over key names, so a field added later trips this rather than a reviewer
    having to notice."""
    actorish = ("author", "agent", "model", "session", "fixer", "who", "by",
                "actor", "machine", "instance", "user", "effort")
    # EVERY segment, not just the leaf's own key: an `agent` BLOCK is the shape this
    # guarantee most needs to catch, and its leaves are named `name` and `version`.
    for path, name in _key_names(_record()):
        assert name not in actorish, path
        assert not any(name.endswith(f"_{w}") or name.startswith(f"{w}_")
                       for w in actorish), path


def test_round_stop_is_not_passed_the_record_and_does_not_mention_it():
    """#67's instrument-before-gate rule, and #624 is explicit beyond it: "record the
    pass, report the numbers as diagnostics, and calibrate against real cycles before
    anything is scored, ranked or gated."

    Read by `ast` over `round_stop`'s own signature and body rather than by a grep over
    the file, so a helper of some other name cannot smuggle the record in and so a
    mention inside a docstring or a comment does not fail this."""
    tree = ast.parse((LOOPS / "panel_rounds.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "round_stop")
    params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    assert not any("fix_pass" in p for p in params), sorted(params)
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)} | {
        c.value for c in ast.walk(fn)
        if isinstance(c, ast.Constant) and isinstance(c.value, str)
        and "\n" not in c.value}
    assert "fix_pass_record" not in names
    assert "fix_pass" not in names


def test_the_verdict_is_identical_whether_the_pass_was_large_or_small():
    """The gate's absence asserted where a gate would be read. Two rounds whose only
    difference is the size and the shape of the pass in front of them, reaching the
    same stop, the same confidence, the same convergence and the same empty veto list.

    Built through `round_stop` rather than through the record, because that is the
    object every stop rule is read out of and the record deliberately is not in it."""
    small = panel.round_stop(2, 5, [], [], [], surface=_surface(("a.py",), (), 4))
    wide = panel.round_stop(2, 5, [], [], [],
                            surface=_surface([f"f{i}.py" for i in range(12)],
                                             [f"f{i}.py" for i in range(7)], 2))
    assert small["stop"] == wide["stop"] is True
    assert small["confident"] == wide["confident"] is True
    assert small["converged"] == wide["converged"] is True
    assert small["veto"] == wide["veto"] == []
    assert "fix_pass" not in json.dumps(small)


def test_there_is_no_DIAL_and_no_flag_to_arm_one():
    """#621 is explicit that this epic is "not a 29th dial": every item makes an
    existing decision durable, moves an existing rule from prose into code, or measures
    something nobody measures. This is the third kind, and a dial here would be the
    thing the epic refuses — there is nothing to configure, because the record chooses
    no threshold and takes no action."""
    import harness_rules
    assert not [k for k in harness_rules.BOARD_DIALS
                if "fix_pass" in k or "fixer" in k]
    assert "fix_pass" not in json.dumps(panel.Dials().as_dict())


# --------------------------------------------------- and the fixer is told about it

def test_the_fixers_brief_says_the_record_is_made_and_says_it_is_not_scored(
        monkeypatch, capsys, tmp_path):
    """#622's rule applied to an artifact instead of to a count: a measurement the
    measured party does not know about is a trap, and the point of this is a record
    rather than a gotcha.

    Both halves are load-bearing and are asserted apart. Told that the record is made,
    a fixer can ask what it is derived from — which is the range and the brief, so
    there is no field to write carefully and nothing here rewards a smaller-looking
    pass. Told that and nothing else, a fixer would reasonably read a score being
    kept, which is precisely the behaviour #624 refuses to create.

    Asserted on the RENDERED report and not on the source, because the sentence is
    assembled from adjacent literals and a source scan would pass on a version of it
    no reader ever sees."""
    report, _payload, _ = _run(monkeypatch, capsys, tmp_path)
    assert "The next round records this pass as a fix-pass artifact" in report
    assert "nothing is taken from the pass's own account of itself" in report
    assert "none of it is scored, ranked or gated on" in report


def test_the_ORCHESTRATORS_brief_says_how_to_report_it_and_how_not_to():
    """`panel-review-pr.md` is what an orchestrator reads to know what each report line
    IS and what to carry into its summary, and every other measurement on that list
    arrives with the reading it must not be given: `guard_ratio` with "there is no
    threshold here for you to apply and none for you to invent", `within: false` with
    "do not treat it as a breach".

    This one needs three, because it is the artifact most easily turned into the thing
    #624 forbids: do not compute a ratio, do not rank the cycle's rounds against each
    other, and do not read `cleared` as `verified fixed` — that last one is what the
    round's `scope` is on the record for."""
    said = (LOOPS.parent / "commands" / "panel-review-pr.md").read_text()
    assert "**The fix pass this round read**" in said
    assert "Nothing here is a score and you must not turn it into one." in said
    # Fragments that do not cross a line break: the file is wrapped at 90-odd
    # columns, so a longer quotation would test the wrapping rather than the text.
    assert "do not compute one, do not rank the rounds" in said
    assert "no ratio, no ranking of the" in said
    assert "**`cleared` is not `verified fixed`.**" in said
    # …and that the DECLARED half is flagged to the orchestrator as a declaration,
    # since its §6 sentence is where the two would otherwise be merged.
    assert "`declared` is a declaration and the rest is not" in said


# --------------------------------------------------------- through a whole round

def _compare(*files):
    """The compare body `_fix_range_diff` reads the fix range out of. `ahead` is the
    linear case — a branch that only grew between rounds — which is the only status
    that HAS a fix range to attribute."""
    return json.dumps({"status": "ahead",
                       "files": [{"filename": f, "patch": "@@ -1,0 +1,1 @@\n+line"}
                                 for f in files]})


def _diff(*files):
    return "".join(f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n"
                   f"@@ -1,0 +1,1 @@\n+line\n" for f in files)


def _p2(title="unvalidated input"):
    """One reviewer report, which the stubbed judge below confirms into a `Canonical`.

    Rounds in this file need a non-empty **To fix** list for two of the assertions —
    the fixer's note is printed with the list that will produce the pass, not beside an
    empty one — so the judge is stubbed to confirm rather than to return nothing."""
    return panel.Finding("claude", "P2", "app/sync.py", 3, title, "")


def _judge(clusters, diff, model, pr, budget=None, coverage=None, **_kw):
    return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity=f.severity,
                             file=f.file, line=f.line, synthesis=f.title,
                             verdict="confirmed", detail=f.detail,
                             reported_by=[f], rationale="real")
             for i, f in enumerate(f for grp in clusters for f in grp)],
            None, panel.CoverageRuling())


def _run(monkeypatch, capsys, tmp_path, *, round_no=1, baseline=(),
         fix=("app/sync.py",), cfg=None, findings=None):
    """One panel run whose fix range touched `fix`. Round 2 with a baseline is what
    makes a round ATTRIBUTABLE, which is the condition a record is made under — round 1
    has no pass in front of it. The head moves per round because an unchanged head is
    "no commit landed between rounds", which is a range that does not exist rather than
    one that opened nothing, and it is a real 40-character sha because `load_baseline`
    validates it and drops an anchor it cannot read."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg or CFG)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "fix: a real bug", "additions": 3, "deletions": 1,
              "headRefName": "h", "headRefOid": f"{round_no:040d}",
              "files": [{"path": "app/sync.py", "additions": 3, "deletions": 1}]},
        diff=_diff("app/sync.py"),
        compare=_compare(*fix)))
    raised = [_p2()] if findings is None else list(findings)
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun(raised, None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", _judge)
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=5,
                     scope="pr") == 0
    return capsys.readouterr().out, json.loads(out.read_text()), str(out)


def test_round_one_records_no_pass_and_prints_no_line(monkeypatch, capsys, tmp_path):
    """There was no pass, so there is no record — and the report says nothing rather
    than printing a line of dashes that reads like the instrument failing."""
    report, payload, _ = _run(monkeypatch, capsys, tmp_path)
    assert "fix_pass" in payload and payload["fix_pass"] is None
    assert "The fix pass this round read" not in report


def test_the_key_rides_the_payload_at_the_TOP_LEVEL(monkeypatch, capsys, tmp_path):
    """Not under `round_stop`, which is the object every stop rule is read out of.
    Filing a diagnostic in there would put it one key away from every consumer that
    reads that block for a decision."""
    _r1, first, r1 = _run(monkeypatch, capsys, tmp_path)
    _r2, payload, _ = _run(monkeypatch, capsys, tmp_path, round_no=2, baseline=[r1])
    assert payload["fix_pass"] is not None
    assert "fix_pass" not in (payload["round_stop"] or {})
    assert "fix_pass" not in json.dumps(first["round_stop"] or {})


def test_round_stop_is_not_HANDED_the_record_by_the_round_that_builds_it(
        monkeypatch, capsys, tmp_path):
    """The no-gate guarantee at the CALL SITE, which is the half an `ast` walk over
    `round_stop` cannot make.

    A second opinion on #705 named the hole exactly: the walk up in
    `test_round_stop_is_not_passed_the_record_and_does_not_mention_it` bans a parameter
    or a name containing `fix_pass`, so a future `round_stop(..., diagnostics=None)`
    reading `diagnostics["counts"]["new_files"]` — with `panel.run` passing
    `diagnostics=fix_pass` — would keep that test green while gating on the record. The
    walk is about the callee's vocabulary; this is about what the one production caller
    actually hands it (`panel.py:5028` is the only one outside a test).

    Asserted by IDENTITY as well as by value: the record is a plain dict of counts, so
    an equality test alone could be satisfied by an unrelated mapping, and it is the
    object itself that must not travel."""
    _r1, _first, r1 = _run(monkeypatch, capsys, tmp_path)
    seen: list = []
    real = panel.round_stop

    def spy(*args, **kwargs):
        seen.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(panel, "round_stop", spy)
    _r2, payload, _ = _run(monkeypatch, capsys, tmp_path, round_no=2, baseline=[r1],
                           fix=("app/sync.py", "nginx/site.conf"))
    record = payload["fix_pass"]
    # The round really did build one, or this test would be asserting about nothing.
    assert record is not None and seen, (record, seen)
    for args, kwargs in seen:
        passed = [*args, *kwargs.values()]
        assert not any(v is record for v in passed), sorted(kwargs)
        assert not any(v == record for v in passed), sorted(kwargs)
        # …and not smuggled one level in, as a member of a mapping it was given.
        for v in passed:
            if isinstance(v, dict):
                assert not any(m is record or m == record for m in v.values())


def test_a_real_round_records_the_pass_and_the_report_names_it(
        monkeypatch, capsys, tmp_path):
    """End to end, because the record is only worth something if the range, the brief
    and the file set it assembles all come from the same round. The pass here touched a
    file no earlier round had recorded, which is the shape #619 was filed over and the
    one a reader most needs the record to carry."""
    _r1, _first, r1 = _run(monkeypatch, capsys, tmp_path)
    report, payload, _ = _run(monkeypatch, capsys, tmp_path, round_no=2,
                              baseline=[r1],
                              fix=("app/sync.py", "nginx/site.conf"))
    got = payload["fix_pass"]
    assert got["read_round"] == 2 and got["brief"]["round"] == 1
    assert got["range"]["base"] == f"{1:040d}" and got["range"]["head"] == f"{2:040d}"
    assert got["surface"]["new_files"] == ["nginx/site.conf"]
    assert got["counts"]["files"] == 2 and got["counts"]["new_files"] == 1
    assert "**The fix pass this round read**" in report
    assert "2 file(s) touched" in report
    assert "**1 of them never in front of a reviewer**" in report
    assert "Diagnostics — nothing is scored, ranked or gated on any of it." in report


def test_the_report_names_the_brief_by_its_LIST_where_the_round_is_unknown(
        monkeypatch, capsys, tmp_path):
    """`budgeted_brief` can publish a finding count beside a null round — an anchor
    payload carrying a To fix list and no commit to anchor on — and the sentence has to
    survive that without printing "round None's 3 finding(s)".

    Withholding the clause instead would have been the wrong trade here: the count is
    the fact and the round number is the label, so the label goes and the fact
    stays."""
    fp = _record(brief=_brief(round_no=None))
    assert fp["brief"]["round"] is None and fp["counts"]["briefed"] == 3
    # The PRODUCER is stubbed rather than coaxed into this shape, because today's
    # `Baseline` takes the brief off the anchor payload and the anchor is chosen BY its
    # commit — so a brief with a null round is a shape the producer may not currently
    # emit, and the renderer still has to be right about it. That is the same posture
    # `harness_dirty`'s tolerant validator takes on the board: a shape not sent today
    # and possibly sent tomorrow is one a reader must not be lied to about.
    monkeypatch.setattr(panel, "fix_pass_record", lambda *a, **k: fp)
    report, payload, _ = _run(monkeypatch, capsys, tmp_path)
    assert payload["fix_pass"]["brief"]["round"] is None
    assert "round None" not in report
    assert "1 of the 3 finding(s) it was sent to fix" in report


def test_the_reports_line_prints_no_quotient(monkeypatch, capsys, tmp_path):
    """The rendered form of the no-ratio rule, which is where a reader would meet one.
    The line carries counts and the words around them; it computes nothing, so there is
    no `%`, no `x` multiplier and no `per`."""
    _r1, _first, r1 = _run(monkeypatch, capsys, tmp_path)
    report, _payload, _ = _run(monkeypatch, capsys, tmp_path, round_no=2,
                               baseline=[r1], fix=("app/sync.py", "nginx/site.conf"))
    line = next(ln for ln in report.splitlines()
                if "**The fix pass this round read**" in ln)
    assert "%" not in line and " per " not in line and "ratio" not in line


def test_the_fixer_is_told_even_with_no_budget_dial_written(
        monkeypatch, capsys, tmp_path):
    """The budget paragraph beside this note prints only where a budget is in force,
    which is right for a bound that may not exist. The record is made of EVERY pass, so
    the note has to be unconditional — otherwise a repo with no budget written has a
    fixer measured by machinery nobody told it about, which is the trap this closes."""
    cfg = {**CFG, "review_panel": {"low_severity_fix_lines": None}}
    report, _payload, _ = _run(monkeypatch, capsys, tmp_path, cfg=cfg)
    assert "records this pass as a fix-pass artifact" in report
    assert "Budget spend of the last fix pass" not in report


def test_a_round_with_NOTHING_TO_FIX_is_told_nothing_either(
        monkeypatch, capsys, tmp_path):
    """The second gate on that note, and it is not belt-and-braces. A round whose To
    fix list is empty is a round that stops, so no fix pass follows it and there is
    nothing for a next round to record — printed there, the note would promise an
    artifact about a pass that never comes to exist, under a list reading "- none".

    That is the shape a reader learns to distrust the whole note from, which matters
    more here than for most report lines: the note's job is to tell a fixer it is being
    recorded, and a note that also fires when nobody is fixing anything is one a fixer
    can reasonably decide not to read."""
    report, _payload, _ = _run(monkeypatch, capsys, tmp_path, findings=[])
    assert "### To fix (0)" in report
    assert "records this pass as a fix-pass artifact" not in report


def test_a_review_only_run_is_told_nothing_because_there_is_no_next_round(
        monkeypatch, capsys, tmp_path):
    """`cycle_run` gates the note, which is the predicate the payload's own
    `cycle_trend` and `new_findings` are gated on. A run with no cycle has no next
    round to record anything, and a note promising one would be describing machinery
    that is not going to run."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: CFG)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "fix: a real bug", "additions": 3, "deletions": 1,
              "headRefName": "h", "headRefOid": "c" * 40, "files": []},
        diff=_diff("app/sync.py"), compare=_compare("app/sync.py")))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate",
                        lambda *a, **k: ([], None, panel.CoverageRuling()))
    out = tmp_path / "solo.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False) == 0
    report = capsys.readouterr().out
    assert "records this pass as a fix-pass artifact" not in report
    assert json.loads(out.read_text())["fix_pass"] is None


def test_the_key_is_on_EVERY_payload_exit_at_its_null_default():
    """`rules` and #112's harness fields are on every payload exit because a consumer
    must never have to tell "the panel did not say" from "this payload predates the
    key". The same applies here, so the key is in `_payload_defaults()` rather than
    only on the reviewed path.

    And the VALUE on those exits is null even where a pass really did land in front of
    the round: a skipped or refused round reviewed nothing, so it measured nothing, and
    a record of zeros would attribute an unread pass's silence to the pass — which is
    the same reason `round_stop` is null there.

    Not `{}`, on `guard_ratio`'s rule: an empty mapping would let a consumer index it
    and get zeros for a pass nobody measured."""
    defaults = panel._payload_defaults()
    assert "fix_pass" in defaults
    assert defaults["fix_pass"] is None

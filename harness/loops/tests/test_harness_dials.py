"""The board's dial layer, from the harness side — #305.

`.harness-rules.sample` said this repo answered `fix_severity_floor` at P2. Every
round of the five run on PR #299 put P4 findings in `to_fix` with `below_fix_floor`
empty, which cannot happen under a P2 fix floor. The file that stated the policy and
the rounds that applied it disagreed for five rounds, four agents and a landed
release, because there was no way to ASK what the floor was.

So the two things pinned hardest here are not "a dial can be set" — they are:

1. **A repo with no board dial behaves exactly as it did before this existed.**
2. **The reported answer names the layer for EVERY dial**, so a value and its
   source can never again be two separate guesses.

The ENDPOINT half — storage, scopes, expiry, and who may write — is
`tests/test_dials.py`. The split is the design's own: the harness owns the dial
vocabulary and the board does not, so neither suite can assert the other's half.
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_rules as hr  # noqa: E402

SAMPLE = {
    "review_panel": {
        "fix_severity_floor": "P2",
        "round_trigger_floor": "P2",
        "max_rounds": 1,
    },
    "reviewers": {
        "claude": {"enabled": True, "model": "opus"},
        "codex": {"enabled": True},
        "antigravity": {"enabled": False},
    },
}

FLOOR = "review_panel.fix_severity_floor"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repo with an `origin` remote, carrying a tracked sample.

    The per-box overlay lives OUTSIDE the checkout, so `XDG_CONFIG_HOME` is
    redirected for the same reason `test_harness_rules` redirects it: a suite whose
    answer depends on the developer's own machine config is the defect #239 is.
    """
    monkeypatch.delenv("HARNESS_UNATTENDED", raising=False)
    monkeypatch.delenv(hr.BOX_RULES_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(hr.DIALS_ENV, "")
    work = tmp_path / "myrepo"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "T")
    (work / hr.SAMPLE_FILENAME).write_text(json.dumps(SAMPLE))
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    git(work, "remote", "add", "origin", "https://github.com/acme/myrepo.git")
    git(work, "remote", "set-url", "origin", str(bare))
    git(work, "push", "-q", "origin", "main")
    git(work, "remote", "set-head", "origin", "main")
    return work


def board(monkeypatch, *dials):
    """Put a `GET /dials` body in the environment, which IS the layer when set."""
    monkeypatch.setenv(hr.DIALS_ENV, json.dumps({"dials": list(dials)}))


def dial(name, value, scope="repo", reason="because", set_by="rich", expires_at=None):
    return {"dial": name, "value": value, "scope": scope, "reason": reason,
            "set_by": set_by, "expires_at": expires_at}


# ------------------------------------------- 1. nothing changes without a dial

def test_a_repo_with_no_board_dial_resolves_exactly_as_it_did_before(repo):
    """The acceptance criterion that matters most, and the one a new layer is
    likeliest to break quietly."""
    cfg = hr.resolve_repo(str(repo))
    assert cfg["review_panel"]["fix_severity_floor"] == "P2"
    assert cfg["review_panel"]["max_rounds"] == 1
    assert cfg["review_panel"]["judge_model"] == hr.DEFAULTS["review_panel"]["judge_model"]
    assert cfg["_rules_from"] == str(repo / hr.SAMPLE_FILENAME)
    assert cfg["_dials_from"] == f"${hr.DIALS_ENV}"
    assert cfg["_dials_unreadable"] is False
    assert not [p for p, d in cfg["_dials"].items() if d["layer"] == "board"]


def test_a_host_on_no_board_at_all_says_nothing_and_reads_nothing(repo, monkeypatch,
                                                                  capsys):
    """The ordinary case for a box that is not enrolled. It must be SILENT: a
    diagnostic printed on every resolution of every repo is one nobody reads."""
    monkeypatch.delenv(hr.DIALS_ENV, raising=False)
    monkeypatch.delenv("QUARTERBACK_BASE_URL", raising=False)
    monkeypatch.setattr(hr, "QB_CONFIG", repo / "no-such-config")
    hr._reported.clear()
    cfg = hr.resolve_repo(str(repo))
    assert cfg["_dials_from"] == "" and cfg["_dials_unreadable"] is False
    assert cfg["review_panel"]["fix_severity_floor"] == "P2"
    assert capsys.readouterr().err == ""


# ------------------------------------------------- 2. the board is in force

def test_a_board_dial_overrides_the_sample(repo, monkeypatch):
    board(monkeypatch, dial(FLOOR, "P3"))
    cfg = hr.resolve_repo(str(repo))
    assert cfg["review_panel"]["fix_severity_floor"] == "P3"
    assert cfg["review_panel"]["round_trigger_floor"] == "P2"   # untouched


def test_a_floor_may_be_moved_in_EITHER_direction(repo, monkeypatch):
    """The whole reason #305 is not #276. A throttle may only move a dial the
    cheaper way, correctly, because a throttle that can raise spend is not a
    throttle. A floor is not like that: raising `fix_severity_floor` P3->P2 makes
    rounds cheaper and coverage thinner, and lowering it does the reverse, so
    neither direction is the safe one and the narrowing rule cannot govern it."""
    board(monkeypatch, dial(FLOOR, "P4"))
    assert hr.resolve_repo(str(repo))["review_panel"]["fix_severity_floor"] == "P4"
    board(monkeypatch, dial(FLOOR, "P1"))
    assert hr.resolve_repo(str(repo))["review_panel"]["fix_severity_floor"] == "P1"


def test_a_repo_dial_beats_a_fleet_dial_and_the_report_says_which(repo, monkeypatch):
    board(monkeypatch, dial(FLOOR, "P1", scope="fleet"), dial(FLOOR, "P3", scope="repo"))
    cfg = hr.resolve_repo(str(repo))
    assert cfg["review_panel"]["fix_severity_floor"] == "P3"
    assert cfg["_dials"][FLOOR]["scope"] == "repo"


def test_the_board_layer_is_read_on_the_unattended_path(repo, monkeypatch):
    """UNLIKE the per-box overlay, and the difference is not an inconsistency: the
    overlay is excluded unattended because it is a file in an untrusted WORKING
    TREE, and the board is not in the working tree. Unattended is also the path a
    budget governor exists to govern (#276)."""
    board(monkeypatch, dial(FLOOR, "P3"))
    cfg = hr.resolve_repo(str(repo), from_default_branch=True)
    assert cfg["review_panel"]["fix_severity_floor"] == "P3"


def test_applying_a_board_dial_does_not_edit_the_builtin_defaults(repo, monkeypatch):
    """The trap the per-box overlay names one function up: for a seat the rules file
    did not mention, `cfg["reviewers"][seat]` is still the DEFAULTS dict ITSELF, so
    an in-place write would move the built-in default for the rest of the process
    and every later `resolve_repo` would inherit one repo's board dial."""
    before = copy.deepcopy(hr.DEFAULTS)
    # `escalate_on` is the node that actually catches it. The block merge is ONE
    # level deep, so `cfg["review_panel"]` is a fresh mapping while
    # `cfg["review_panel"]["escalate_on"]` inside it is still DEFAULTS' own dict —
    # the deepest shared node a board dial can reach, and the only one where an
    # in-place write would leak into every later resolution in the process.
    assert hr.DEFAULTS["review_panel"]["escalate_on"]["premise_repeated"] != 5
    board(monkeypatch, dial("review_panel.escalate_on.premise_repeated", 5),
          dial("review_panel.max_rounds", 3))
    cfg = hr.resolve_repo(str(repo))
    assert cfg["review_panel"]["escalate_on"]["premise_repeated"] == 5
    assert before == hr.DEFAULTS


# ---------------------------------------------- 3. which layer answered, for all

def test_the_report_names_the_layer_for_every_dial(repo, monkeypatch):
    """`harness_rules --json` names the layer that answered FOR EVERY DIAL, as it
    already did for the overlay. All four layers appear in one resolution."""
    (repo / hr.RULES_FILENAME).write_text(
        json.dumps({"reviewers": {"codex": {"effort": "medium"}}}))
    board(monkeypatch, dial(FLOOR, "P3"))
    cfg = hr.resolve_repo(str(repo))
    said = cfg["_dials"]
    # Every dial in the resolved config has an answer, and no dial has two.
    assert said[FLOOR]["layer"] == "board"
    assert said["review_panel.max_rounds"]["layer"] == "sample"
    assert said["reviewers.codex.effort"]["layer"] == "overlay"
    assert said["review_panel.judge_model"]["layer"] == "defaults"
    assert {d["layer"] for d in said.values()} <= {"board", "sample", "overlay",
                                                   "defaults"}
    # And the value beside the layer is the one that is actually in force.
    for path, entry in said.items():
        assert hr._get_dial(cfg, path) == (entry["value"], True)


def test_a_dial_the_sample_wrote_at_its_default_value_still_reads_as_the_sample(repo):
    """BY PRESENCE, NOT BY DIFFERENCE. This repo's own sample writes four dials out
    at exactly their DEFAULTS values on purpose (`_278_distant_merge_lines`: *"at its
    DEFAULTS value and written out anyway"*). Reporting those as `defaults` would
    tell a reader the file they are about to edit is not the one that answered."""
    (repo / hr.SAMPLE_FILENAME).write_text(json.dumps({
        **SAMPLE,
        "review_panel": {**SAMPLE["review_panel"],
                         "distant_merge_lines":
                             hr.DEFAULTS["review_panel"]["distant_merge_lines"]}}))
    cfg = hr.resolve_repo(str(repo))
    assert cfg["_dials"]["review_panel.distant_merge_lines"]["layer"] == "sample"


def test_a_board_dial_carries_its_reason_and_when_it_lapses(repo, monkeypatch):
    """A dial in force whose argument nobody can read is a dial nobody can decide
    to remove."""
    board(monkeypatch, dial(FLOOR, "P3", reason="trying P3 for a fortnight",
                            set_by="rich", expires_at="2999-01-01T00:00:00+00:00"))
    said = hr.resolve_repo(str(repo))["_dials"][FLOOR]
    assert said["reason"] == "trying P3 for a fortnight"
    assert said["set_by"] == "rich"
    assert said["expires_at"] == "2999-01-01T00:00:00+00:00"


def test_the_rules_line_names_the_board_and_the_dials_it_moved(repo, monkeypatch):
    board(monkeypatch, dial(FLOOR, "P3"), dial("review_panel.max_rounds", 2))
    said = hr.resolve_repo(str(repo))["_rules_from"]
    assert f"${hr.DIALS_ENV}" in said
    assert FLOOR in said and "review_panel.max_rounds" in said


# --------------------------------------------------------- 4. expiry is absence

def test_an_expired_dial_is_simply_absent(repo, monkeypatch, capsys):
    """Not applied, not warned about, and leaving no trace: a resolution with no
    dial layer has to be indistinguishable from one whose dial lapsed, or the
    expiry is a flag somebody still has to clear."""
    hr._reported.clear()
    board(monkeypatch, dial(FLOOR, "P3", expires_at="2020-01-01T00:00:00+00:00"))
    cfg = hr.resolve_repo(str(repo))
    assert cfg["review_panel"]["fix_severity_floor"] == "P2"
    assert cfg["_dials"][FLOOR]["layer"] == "sample"
    assert FLOOR not in capsys.readouterr().err


def test_a_dial_whose_expiry_cannot_be_read_is_treated_as_expired(repo, monkeypatch):
    """A dial nobody can date is a dial that cannot end, which is the exact failure
    `expires_at` exists to close."""
    board(monkeypatch, dial(FLOOR, "P3", expires_at="next tuesday"))
    assert hr.resolve_repo(str(repo))["review_panel"]["fix_severity_floor"] == "P2"


# ------------------------------- 5. a dial cannot be moved by a mechanism that may not

def test_the_board_may_take_a_seat_off_and_may_not_put_one_on(repo, monkeypatch,
                                                              capsys):
    """`reviewers.<seat>.enabled` is the boundary case and it gets the NARROW rule.

    It is both capability and policy. The board's claim is the policy half only —
    "is this seat worth its tokens" — so it may take a seat off; and it may not
    decide that a box which cannot run `agy` actually can, because nothing on the
    board knows which CLIs a machine carries and `panel.py` counts a seat that never
    ran as coverage it did not get.
    """
    hr._reported.clear()
    board(monkeypatch, dial("reviewers.claude.enabled", False, scope="fleet"))
    assert hr.resolve_repo(str(repo))["reviewers"]["claude"]["enabled"] is False

    hr._reported.clear()
    board(monkeypatch, dial("reviewers.antigravity.enabled", True, scope="fleet"))
    cfg = hr.resolve_repo(str(repo))
    assert cfg["reviewers"]["antigravity"]["enabled"] is False
    err = capsys.readouterr().err
    assert "would turn ON something" in err and "may only narrow" in err


def test_a_dial_the_harness_does_not_recognise_is_refused_out_loud(repo, monkeypatch,
                                                                   capsys):
    """The board stores anything; the HARNESS settles what a dial is. A dial the
    board reports as in force and nothing applies is the two-sources-of-truth
    failure arriving from the other end, so it is named rather than dropped."""
    hr._reported.clear()
    board(monkeypatch, dial("auto_merge", "all", scope="fleet"))
    cfg = hr.resolve_repo(str(repo))
    assert cfg["auto_merge"] == hr.DEFAULTS["auto_merge"]
    assert "`auto_merge` is not a board-settable dial" in capsys.readouterr().err


def test_a_merge_gate_is_not_a_dial(repo, monkeypatch):
    """What stays OUT is everything that decides what may be MERGED. The overlay's
    comment refuses the same set for the same reason: a pin is a fact about a
    provider, a merge gate is a policy."""
    assert not [d for d in hr.BOARD_DIALS
                if d.startswith(("auto_merge", "preland.", "epic.", "loops."))]


def test_a_value_of_the_wrong_shape_is_refused_and_the_run_keeps_its_own(
        repo, monkeypatch, capsys):
    """VALUES ARE CHECKED, NOT JUST NAMES — the overlay's second narrowing rule, and
    here for the same reason: `"enabled": "false"` is a non-empty string and
    therefore truthy, so a name-only filter lets the natural hand-edit do the exact
    opposite of what the dial exists for."""
    hr._reported.clear()
    board(monkeypatch,
          dial("review_panel.max_rounds", "lots"),
          dial(FLOOR, "critical"),
          dial("reviewers.claude.enabled", "false", scope="fleet"))
    cfg = hr.resolve_repo(str(repo))
    assert cfg["review_panel"]["max_rounds"] == 1
    assert cfg["review_panel"]["fix_severity_floor"] == "P2"
    assert cfg["reviewers"]["claude"]["enabled"] is True
    err = capsys.readouterr().err
    assert "must be a number" in err
    assert "must be a severity band P1-P4" in err
    assert "must be true or false" in err


def test_null_is_the_off_switch_only_where_it_is_documented_as_one(repo, monkeypatch,
                                                                   capsys):
    hr._reported.clear()
    board(monkeypatch, dial("review_panel.max_fix_growth", None),
          dial(FLOOR, None))
    cfg = hr.resolve_repo(str(repo))
    assert cfg["review_panel"]["max_fix_growth"] is None
    assert cfg["review_panel"]["fix_severity_floor"] == "P2"
    assert "null is not this dial's off switch" in capsys.readouterr().err


# ------------------------------------------------ 6. a board that will not answer

def test_a_board_that_will_not_answer_leaves_the_repo_on_its_own_rules_and_says_so(
        repo, monkeypatch, capsys):
    """NOT the same fact as there being no dial, and it has a different remedy, so
    it is a flag a caller can gate on plus a sentence in the line that exists to say
    which rules applied — never silence."""
    hr._reported.clear()
    monkeypatch.delenv(hr.DIALS_ENV, raising=False)
    monkeypatch.setenv("QUARTERBACK_BASE_URL", "https://board.invalid")
    monkeypatch.setenv("QUARTERBACK_TOKEN", "t")
    monkeypatch.setattr(hr, "DIALS_TIMEOUT", 1)
    cfg = hr.resolve_repo(str(repo))
    assert cfg["_dials_unreadable"] is True
    assert cfg["review_panel"]["fix_severity_floor"] == "P2"
    assert "unreadable, dials not applied" in cfg["_rules_from"]
    assert "unreachable" in capsys.readouterr().err


def test_a_board_too_old_to_have_dials_is_a_capability_answer_not_a_failure(
        repo, monkeypatch):
    """A 404 means this board predates the dial layer and has none, which is not the
    same as a board that has one and broke. `preland` tells the same two apart on
    the same evidence."""
    monkeypatch.delenv(hr.DIALS_ENV, raising=False)
    monkeypatch.setattr(hr, "board_config",
                        lambda: ("https://board.example", "tok", ""))

    def _404(*a, **kw):
        raise hr.urllib.error.HTTPError("u", 404, "nope", {}, None)

    monkeypatch.setattr(hr.urllib.request, "urlopen", _404)
    cfg = hr.resolve_repo(str(repo))
    assert cfg["_dials_unreadable"] is False
    assert cfg["review_panel"]["fix_severity_floor"] == "P2"


def test_a_dials_variable_that_cannot_be_parsed_is_fatal_and_names_itself(
        repo, monkeypatch):
    """Named but unreadable is a mistake and a loud one, exactly as `BOX_RULES_ENV`
    treats a path that does not exist: somebody set this to say something, and a run
    that cannot tell what must not quietly decide it said nothing."""
    monkeypatch.setenv(hr.DIALS_ENV, "{not json")
    with pytest.raises(SystemExit) as e:
        hr.resolve_repo(str(repo))
    assert hr.DIALS_ENV in str(e.value)


# ------------------------------------------------------------- 7. the one call

def test_the_dial_flag_answers_the_floor_and_names_the_layer(repo, monkeypatch):
    """"What is the floor here, and which layer said so" in ONE call — the thing
    that was previously an inference from three files and a resolution order, and
    was wrong for five rounds with nothing able to say so."""
    env = {**dict(__import__("os").environ),
           hr.DIALS_ENV: json.dumps({"dials": [dial(FLOOR, "P3", reason="trying P3")]}),
           "XDG_CONFIG_HOME": str(repo.parent / "xdg")}
    out = subprocess.run(
        [sys.executable, str(Path(hr.__file__)), "--repo", str(repo),
         "--dial", FLOOR],
        capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert FLOOR in out.stdout and '"P3"' in out.stdout
    assert "[board]" in out.stdout and "trying P3" in out.stdout


def test_the_dials_flag_lists_every_dial_with_its_layer(repo, monkeypatch):
    env = {**dict(__import__("os").environ), hr.DIALS_ENV: "",
           "XDG_CONFIG_HOME": str(repo.parent / "xdg")}
    out = subprocess.run(
        [sys.executable, str(Path(hr.__file__)), "--repo", str(repo), "--dials"],
        capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    lines = {ln.split()[0] for ln in out.stdout.splitlines() if ln.strip()}
    assert FLOOR in lines and "review_panel.judge_model" in lines
    assert all("[" in ln for ln in out.stdout.splitlines() if ln.strip())


def test_an_enrolled_box_with_no_usable_token_is_reported_not_treated_as_unenrolled(
        repo, monkeypatch, capsys):
    """Two different facts, and `board_config` tells them apart by whether it
    resolved a URL. No URL is "this box is on no board" and is silent; a URL with no
    usable token is a MISCONFIGURED box that IS enrolled and may well have dials in
    force this run cannot see, so reporting it as "no dials" would be the
    silent-policy failure the module exists to prevent."""
    hr._reported.clear()
    monkeypatch.delenv(hr.DIALS_ENV, raising=False)
    monkeypatch.setenv("QUARTERBACK_BASE_URL", "https://board.example")
    monkeypatch.delenv("QUARTERBACK_TOKEN", raising=False)
    monkeypatch.setattr(hr, "QB_CONFIG", repo / "no-such-config")
    monkeypatch.setattr(hr, "QB_TOKEN_FILE", repo / "no-such-token")
    cfg = hr.resolve_repo(str(repo))
    assert cfg["_dials_unreadable"] is True
    assert "unreadable, dials not applied" in cfg["_rules_from"]
    assert "no board token" in capsys.readouterr().err


def test_a_floor_is_read_case_insensitively_from_the_board_too(repo, monkeypatch):
    """Every severity entering the panel is stripped and upper-cased, so a layer
    that refused `"p2"` while the sample beside it accepted it would make one written
    value mean two things depending on which layer carried it. Normalised on the way
    in, so the provenance table shows the value the round applied."""
    # ` Repo `, not ` Increment `: this test was written against the same wrong
    # tuple the code had, so the one place a scope was exercised end to end was
    # exercising a word `panel_seats.reviewer_scope` refuses with `SystemExit`.
    board(monkeypatch, dial(FLOOR, "p3"), dial("review_panel.reviewer_scope",
                                               " Repo "))
    cfg = hr.resolve_repo(str(repo))
    assert cfg["review_panel"]["fix_severity_floor"] == "P3"
    assert cfg["review_panel"]["reviewer_scope"] == "repo"
    assert cfg["_dials"][FLOOR]["value"] == "P3"


def test_an_expired_dial_is_absent_even_when_it_is_also_wrong(repo, monkeypatch,
                                                              capsys):
    """Expiry is judged FIRST, before the name and the value. Judged after, a lapsed
    dial with a typo in it would go on being complained about for ever — which is
    the one thing an expiry is supposed to end."""
    hr._reported.clear()
    board(monkeypatch,
          dial("review_panel.invented", 1, expires_at="2020-01-01T00:00:00+00:00"),
          dial(FLOOR, "nonsense", expires_at="2020-01-01T00:00:00+00:00"))
    cfg = hr.resolve_repo(str(repo))
    assert cfg["review_panel"]["fix_severity_floor"] == "P2"
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------- #539
# The dial table, read back OUT of this module by whatever draws a form.
#
# Setting a dial was four empty boxes and one placeholder covering all 29 at
# once, so the question a person actually has — what does THIS one take, what is
# it now, which way may it move — had no answer on the screen where it is asked.
# What is pinned here is not that the answer is pretty; it is that there is only
# ever ONE of it. A client that hard-coded these bands, defaults or words would be
# the second place a dial is written down, which is the failure #56's rule and
# #305 both exist to end — so every one of these asserts against `BOARD_DIALS` and
# `DEFAULTS` rather than against a list written out again here.


def test_every_settable_dial_has_a_spec_and_no_others_do():
    """`BOARD_DIALS` settles the list, and `dial_specs` is a view of it — not a
    second table that could gain a name the harness will not apply, or lose one it
    will."""
    assert set(hr.dial_specs()) == set(hr.BOARD_DIALS)


def test_a_specs_default_is_the_one_DEFAULTS_holds():
    """Read back rather than restated, which is `_dial_default`'s own argument: a
    default written twice is a default two layers can disagree about, and the form
    showing the stale one is how somebody sets a dial believing they are changing
    something."""
    specs = hr.dial_specs()
    for path, spec in specs.items():
        value, known = hr._dial_default(path)
        assert spec["default"] == value and spec["default_known"] == known


def test_a_dial_with_no_default_says_so_rather_than_offering_null(monkeypatch):
    """A dial in `BOARD_DIALS` and absent from `DEFAULTS` is a bug in this module,
    and `null` is a value several dials really take — so drawing one for the other
    would hide the bug behind a plausible answer."""
    monkeypatch.setitem(hr.BOARD_DIALS, "review_panel.invented",
                        hr.Dial("number", False, "either"))
    spec = hr.dial_specs()["review_panel.invented"]
    assert spec["default_known"] is False and spec["default"] is None


def test_every_closed_kind_offers_its_set_and_the_open_ones_offer_nothing():
    """A form can only offer what is enumerable. The other half of this — that
    every offered word is one the judge accepts once the client has parsed it — is
    asserted in `harness/tests/test_qb_dials_surface.py`, which is the only suite
    holding both the table and `parse_dial_value`; a copy of that parser here would
    be the drift this whole design is trying not to have."""
    specs = hr.dial_specs()
    closed = {p: s for p, s in specs.items()
              if hr.BOARD_DIALS[p].kind in ("severity", "deferral_gate", "scope",
                                            "flag")}
    assert closed and all(s["choices"] for s in closed.values())
    assert not any(specs[p]["choices"] for p, d in hr.BOARD_DIALS.items()
                   if d.kind in ("number", "text"))


def test_a_kind_with_no_closed_set_offers_nothing_rather_than_a_guess():
    """`number` and `text` are not lists, and a form that enumerated them would be
    making the vocabulary up."""
    assert hr.dial_choices("review_panel.max_rounds") == ()
    assert hr.dial_choices("review_panel.judge_model") == ()
    assert hr.dial_choices("review_panel.reviewer_scope") == hr._SCOPES


def test_the_bands_a_floor_offers_are_the_bands_the_pattern_matches():
    """`_SEVERITY_BANDS` is the one statement and `_SEVERITY_RE` is built from it,
    so a band added to the panel cannot be offered by a form and refused by the
    same module one function along."""
    assert all(hr._is_band(b) for b in hr._SEVERITY_BANDS)
    assert hr.dial_choices("review_panel.fix_severity_floor") == hr._SEVERITY_BANDS
    assert not hr._is_band("P0") and not hr._is_band("P5")


def test_null_is_named_as_an_off_switch_only_where_it_is_one():
    """`null` is the documented off switch wherever `Dial.nullable` is true and
    means "inherit the default" everywhere else — two meanings for one written
    value, which a person cannot tell apart from the value box."""
    assert "null" in hr.dial_hint("review_panel.max_fix_growth")
    assert "null" not in hr.dial_hint("review_panel.fix_severity_floor")


def test_a_narrow_dial_says_which_way_it_may_move():
    """The direction rule is invisible in the value and discoverable only by having
    a write ignored — so the seats and the repo's own off switch carry the note and
    the floors, which move both ways, do not."""
    specs = hr.dial_specs()
    assert specs["reviewers.pi.enabled"]["note"]
    assert specs["enabled"]["note"]
    assert not specs["review_panel.fix_severity_floor"]["note"]


def test_a_name_the_harness_does_not_know_is_refused_before_the_write():
    """The board cannot make this judgement — it stores `dial` as opaque text on
    purpose — so a misspelt name is accepted, stored, returned as in force, and
    then ignored by every harness that reads it. This is the only side that can
    catch it, and the sentence says who settles the list."""
    said = hr.dial_problem("review_panel.fix_sevrity_floor", "P2")
    assert "not a board-settable dial" in said and "This harness settles" in said


def test_the_write_side_judge_is_the_read_side_judge():
    """`dial_problem` is `board_dials`' own check asked one step earlier. If they
    could disagree, a value refused here would be one the board took and a round
    applied — or worse, the other way round."""
    dial = hr.BOARD_DIALS["review_panel.max_rounds"]
    for value in (2, "2", None, True, -1, 0.5):
        assert hr.dial_problem("review_panel.max_rounds", value) == \
            hr._dial_problem("review_panel.max_rounds", dial, value)


def test_a_merge_gate_is_still_not_a_dial_and_the_form_cannot_offer_one():
    """`auto_merge` and the loop schedule decide what may be MERGED, and a merge
    gate is policy that belongs where a human reviewing a branch can see it. A form
    drawn off this table cannot offer what the table does not hold."""
    specs = hr.dial_specs()
    assert "auto_merge" not in specs
    assert not [k for k in specs if k.startswith("loops.")]
    assert "review_panel.skip_title_patterns" not in specs


def test_every_settable_dial_says_what_it_decides():
    """One line each, or the picker offering it is 29 dotted paths again. Asserted
    on the table rather than on a list here: a dial added to `BOARD_DIALS` without
    one is the exact regression, and a copy of the sentences in this file would be
    the second place they are written down."""
    missing = [p for p, d in hr.BOARD_DIALS.items() if not d.what.strip()]
    assert not missing, missing


def test_the_one_liner_is_a_line_and_not_an_argument():
    """The argument stays beside the key in `DEFAULTS`, at whatever length it needs.
    This has to fit under a value box in a 78-column pane, and the box is 66 columns
    wide inside its border — so the ceiling is two wrapped lines and a paragraph is
    a description that pushes the form off the bottom of the pane. The person who
    wants the reasoning is one `grep` from the comment carrying it."""
    for path, dial in hr.BOARD_DIALS.items():
        assert len(dial.what) <= 2 * 66, (path, len(dial.what))
        assert "\n" not in dial.what, path


def test_the_table_is_written_in_the_order_a_person_asks_in():
    """`dial_specs` hands its entries over in `BOARD_DIALS`' order and the picker
    keeps it, so this file is where that order is actually decided. Alphabetically
    the first dial is `enabled` — the one that turns this repo's reviews off — and
    it is nobody's answer to "what did I come here to change"."""
    names = list(hr.dial_specs())
    assert names[:2] == ["review_panel.fix_severity_floor",
                         "review_panel.round_trigger_floor"]
    assert names.index("enabled") > names.index("review_panel.max_rounds")


def test_the_board_layer_accepts_exactly_what_the_panel_accepts():
    """The dial layer validates `reviewer_scope` and `panel_seats` applies it, and
    they were two different tuples: `("diff", "increment")` here against
    `("diff", "repo")` there. Both directions were broken and neither was visible —
    `repo`, the documented value, was refused by the board layer and never applied;
    `increment`, which is `round_scope`'s word, passed this check, reached the
    resolved config and then hit `panel_seats.reviewer_scope`, which refuses an
    unknown scope with `SystemExit`. A dial that validates and then kills the run.

    Asserted rather than imported because `panel_core` imports THIS module: the
    constant cannot live in one place, so it has to be held against the other."""
    import panel_core as pc

    assert hr._SCOPES == pc.REVIEWER_SCOPES
    assert hr.dial_problem("review_panel.reviewer_scope", "repo") == ""
    assert "increment" not in hr.dial_choices("review_panel.reviewer_scope")


def test_the_default_reviewer_scope_is_one_of_the_words_offered():
    """The cheapest way to notice the tuple has gone wrong again: whatever
    `DEFAULTS` ships has to be a value the picker would offer and the judge would
    take."""
    default, known = hr._dial_default("review_panel.reviewer_scope")
    assert known and default in hr.dial_choices("review_panel.reviewer_scope")


def test_a_number_dial_refuses_the_values_that_are_not_numbers_after_all():
    """`json.loads` accepts `NaN`, `Infinity` and `-Infinity` as bare literals, and
    `NaN` compares false against every bound — so `value < 0` let it through and a
    floor, a round cap or a budget took a value nothing can compare against.
    `app/api/dials.py` refuses all three at the board (`allow_nan=False`, because
    Postgres will not store them), and a validator that disagreed with the endpoint
    it guards would send the refusal back a round later instead of at the keyboard."""
    for value in (float("nan"), float("inf"), float("-inf")):
        said = hr.dial_problem("review_panel.max_rounds", value)
        assert "finite" in said, (value, said)
    assert hr.dial_problem("review_panel.max_rounds", 2) == ""
    assert hr.dial_problem("review_panel.max_fix_growth", 3.5) == ""

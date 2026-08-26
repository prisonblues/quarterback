"""What the dashboards say about the dials in force — #477.

A dial is a setting: the repo supplies a default, the board states the value IN
FORCE, and the layer that answered is part of the answer (#305). Until this
landed, nothing a person or an agent looks at showed one — not `qb-dash`, not
`qb-dash-tui`, not `qb-board`, not the web board — so the value governing every
round on the fleet was set from an endpoint, read back by one function in
`panel_seats.py`, and invisible everywhere else.

Three things are pinned here and each is a distinct way of being wrong:

  * **precedence** — a repo dial beats a fleet dial, and applying that is the
    client's job. A panel that showed both would state two values for one
    setting; one that dropped the fleet row wherever ANY repo overrode it would
    hide a value still in force in another.
  * **the expiry** — a `tempo: eager` with forty minutes on it and one set
    indefinitely must not render identically. That is #244's rule (being idle and
    being broken must not look alike) applied to a switch instead of a queue, and
    it is the half of this issue that is easiest to drop.
  * **the door** — the terminal reads and cannot write, so it has to say where a
    person goes instead. #443 is the record of what the silent version costs: a
    person told the reorder was theirs to do, in a terminal, whose reply was "i
    don't know how to re-order".

Run: pytest harness/tests/test_qb_dials_surface.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

import qbdata as qd                                       # noqa: E402

REPO = "prisonblues/quarterback"
OTHER = "prisonblues/lexray"


def _load(name: str, filename: str):
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dash():
    pytest.importorskip("rich", reason="the printed renderer needs rich")
    return _load("qb_dash_dials", "qb-dash.py")


@pytest.fixture
def watched(monkeypatch):
    """Pin the repos this process watches, and put them back after."""
    monkeypatch.setattr(qd, "_repos", [REPO], raising=False)
    yield [REPO]
    monkeypatch.setattr(qd, "_repos", None, raising=False)


def dial(name="tempo", value="eager", repo=None, reason="because", expires=None,
         set_by="human/rich", set_at="2026-08-25T10:00:00+00:00"):
    return {"dial": name, "value": value, "repo": repo,
            "scope": "fleet" if repo is None else "repo",
            "reason": reason, "set_by": set_by, "set_at": set_at,
            "expires_at": expires}


def in_hours(n: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=n)).isoformat()


class FakeClient:
    """A board that answers `/dials` the way the real one does — repo rows AND
    fleet rows for a repo read, fleet rows alone for an unscoped one."""

    def __init__(self, rows, fail=None):
        self.rows, self.fail, self.asked = rows, fail, []

    def get(self, path, params=None):
        self.asked.append((path, dict(params or {})))
        if self.fail:
            raise self.fail
        scope = (params or {}).get("repo")
        return {"dials": [r for r in self.rows
                          if r["repo"] is None or r["repo"] == scope]}


# ---- precedence: which layer answers -----------------------------------------

def test_a_repo_dial_beats_a_fleet_dial_of_the_same_name(watched):
    """The board returns both scopes so ONE call answers "what is in force here",
    and says in one line that applying the precedence is the client's."""
    client = FakeClient([dial(repo=None, value="eager"), dial(repo=REPO, value="held")])
    got = qd.fetch_dials(client, [REPO])
    assert [(d["value"], d["scope"]) for d in got["dials"]] == [("held", "repo")]
    assert [d["value"] for d in got["shadowed"]] == ["eager"]


def test_a_fleet_dial_one_of_two_repos_overrides_is_still_in_force(watched):
    """The understatement this rule exists to prevent. A screen watching two
    projects, one of which sets the dial for itself, still has the fleet value in
    force in the other — reporting it as overridden would be the first repo's
    answer given as the fleet's."""
    rows = [dial(repo=None, value="eager"), dial(repo=REPO, value="held")]
    got = qd.fetch_dials(FakeClient(rows), [REPO, OTHER])
    assert sorted(d["value"] for d in got["dials"]) == ["eager", "held"]
    assert got["shadowed"] == []


def test_the_fleet_rows_two_repo_reads_both_return_are_one_row(watched):
    """`GET /dials?repo=X` carries the fleet rows too, so a screen watching two
    repos is handed each of them twice. The board's own `ix_dial_settings_live` is
    unique per (repo, dial), so a duplicate here is the same row arriving again."""
    client = FakeClient([dial(repo=None)])
    got = qd.fetch_dials(client, [REPO, OTHER])
    assert len(got["dials"]) == 1
    assert [p[1].get("repo") for p in client.asked] == [REPO, OTHER]


def test_a_screen_with_no_repo_asks_the_fleet_and_says_so(watched):
    client = FakeClient([dial(repo=None), dial(repo=REPO)])
    got = qd.fetch_dials(client, [])
    assert [d["scope"] for d in got["dials"]] == ["fleet"]
    assert client.asked == [("/dials", {})]


def test_a_board_that_will_not_answer_is_an_error_and_not_an_empty_fleet(watched):
    """`asked` stays True: "nothing is set" and "nobody could ask" are different
    facts, and only the first is a state to act on (#244)."""
    got = qd.fetch_dials(FakeClient([], fail=OSError("connection refused")), [REPO])
    assert got["dials"] == [] and got["asked"] is True
    assert "OSError" in got["error"]


# ---- the expiry, which must not be dropped -----------------------------------

def test_an_indefinite_dial_and_an_expiring_one_do_not_render_alike():
    """The requirement in one line: a `tempo: eager` with forty minutes left and a
    `tempo: eager` set indefinitely are different situations."""
    forever, _ = qd.dial_life(dial(expires=None))
    expiring, _ = qd.dial_life(dial(expires=in_hours(0.7)))
    assert forever == qd.DIAL_NO_END
    assert expiring != forever and expiring.endswith("m")


def test_the_indefinite_one_is_the_loud_cell():
    """A dial that expires takes itself off the board with nobody remembering it;
    one with no end stays until a person clears it, and the failure mode of the
    whole layer is a temporary setting that outlived its reason."""
    _, forever = qd.dial_life(dial(expires=None))
    _, expiring = qd.dial_life(dial(expires=in_hours(4)))
    assert forever == "yellow"
    assert expiring.startswith("grey")


def test_no_end_is_not_the_glyph_every_other_panel_uses_for_unknown():
    """`—` means "nobody reported this" on every other panel here. An expiry that
    was never set is a decision somebody made, and the opposite of an unknown."""
    assert qd.DIAL_NO_END != qd.until(None)


# ---- the value, rendered without claiming to know what it means ---------------

@pytest.mark.parametrize("value,shown", [
    ("P3", "P3"), (2, "2"), (True, "true"), (False, "false"), (None, "null"),
    (["a", "b"], '["a","b"]'),
])
def test_a_value_is_rendered_in_its_json_spelling(value, shown):
    """`null` in particular is a real setting on three dials, and the board goes to
    some trouble to keep it apart from "no row at all" — rendering it blank would
    put the two back together on the one screen a person reads."""
    assert qd.dial_value(dial(value=value)) == shown


def test_the_scope_cell_names_the_layer_and_not_the_project():
    """The one column here that does not answer to the screen's scope: "in force
    fleet-wide" and "in force for this repo" are different facts about the same
    number, and a reader who cannot tell them apart cannot tell whether clearing
    it changes one project or all of them."""
    assert qd.dial_where(dial(repo=None))[0] == "fleet"
    assert qd.dial_where(dial(repo=REPO), show_repo=True)[0] == "quarterback"
    assert qd.dial_where(dial(repo=REPO), show_repo=False)[0] == "repo"


# ---- the tempo cell: four states, no two alike --------------------------------

def test_before_the_board_has_answered_the_tempo_cell_draws_nothing():
    """A screen printing "unset" while its first fetch is in flight would be
    stating something it does not know."""
    assert qd.tempo_cell({}) is None
    assert qd.tempo_cell(None) is None


def test_an_unreadable_dial_is_not_an_absent_one():
    assert qd.tempo_cell({"asked": True, "error": "boom"})[1] == "?"


def test_no_tempo_dial_says_unset_rather_than_naming_a_default():
    """The harness owns the vocabulary (`harness_rules.py`) and the server image
    carries no `harness/` at all — a screen that printed a default here would be a
    second place a dial is written down, which is what #56's rule forbids."""
    assert qd.tempo_cell({"asked": True, "dials": []})[1] == "unset"


def test_a_tempo_in_force_carries_its_value_and_its_life():
    got = qd.tempo_cell({"asked": True,
                         "dials": [dial(value="eager", expires=in_hours(0.7))]})
    assert got[0] == "TEMPO" and got[1] == "eager" and got[2].endswith("m")


def test_several_repos_that_disagree_get_no_single_answer():
    """Only reachable on a screen watching more than one project, and the cell has
    room for one word. Printing either value would be this panel's own defect —
    one layer's answer stated as though it were everybody's."""
    dials = {"asked": True, "dials": [dial(repo=REPO, value="eager"),
                                      dial(repo=OTHER, value="held")]}
    assert qd.tempo_cell(dials)[1] == "mixed"
    # …and asking about ONE of them is a question with an answer again.
    assert qd.tempo_cell(dials, OTHER)[1] == "held"


def test_two_repos_agreeing_on_the_word_and_not_on_the_expiry_still_disagree():
    """The pair this whole issue says must not render alike, arriving through the
    back door: both `eager`, one for an hour and one for good. Agreeing on the
    value and then showing one of the two countdowns beside it is that failure with
    an extra step, so the value stands and the life cell gives way."""
    dials = {"asked": True, "dials": [dial(repo=REPO, value="eager", expires=None),
                                      dial(repo=OTHER, value="eager",
                                           expires=in_hours(1))]}
    label, value, life, colour = qd.tempo_cell(dials)
    assert value == "eager", "the value IS agreed"
    assert life == "2 repos" and colour == "yellow"
    assert life != qd.DIAL_NO_END


def test_the_repo_tempo_answers_over_the_fleet_one():
    """Precedence applied again for a single-dial lookup: with two rows in the
    list, the wrong one is a plausible answer rather than a visible bug."""
    dials = {"asked": True, "dials": [dial(repo=None, value="eager"),
                                      dial(repo=REPO, value="held")]}
    assert qd.tempo_cell(dials, REPO)[1] == "held"


# ---- the door ----------------------------------------------------------------

def test_the_url_carries_the_screens_own_repo(watched):
    """A reader arriving from a terminal lands on the scope the terminal was
    showing rather than on the fleet's."""
    cfg = qd.BoardConfig("https://qb.fo.ls", "t", "hermes")
    assert qd.dials_url(cfg) == f"https://qb.fo.ls/dials/view?repo={REPO}"


def test_a_screen_watching_several_repos_names_none_of_them(monkeypatch):
    """There is one box on that page; picking one of three is a worse answer than
    letting the page ask."""
    monkeypatch.setattr(qd, "_repos", [REPO, OTHER], raising=False)
    cfg = qd.BoardConfig("https://qb.fo.ls", "t", "hermes")
    assert qd.dials_url(cfg) == "https://qb.fo.ls/dials/view"


def test_the_detail_spells_out_what_the_cell_can_only_abbreviate():
    """`no end` in a six-column cell is the fact; the sentence is what it MEANS."""
    said = qd.dial_detail(dial(expires=None, reason="draining the backlog"))
    assert "set indefinitely" in said and "draining the backlog" in said
    assert "human/rich" in said


def test_the_detail_of_an_expiring_dial_counts_down_instead():
    said = qd.dial_detail(dial(expires=in_hours(2)))
    assert "expires in" in said and "indefinitely" not in said


# ---- the printed panel -------------------------------------------------------

def _lines(panel) -> list[str]:
    """The panel's rows. One column by construction — see `qb-dash.dial_row` for
    why this table is hand-aligned where every other one is a grid."""
    return [str(c) for c in panel.renderable.columns[0].cells]


def cfg():
    return qd.BoardConfig("https://qb.fo.ls", "t", "hermes")


def test_the_panel_always_says_where_to_turn_one(dash, watched):
    """Even — especially — with nothing in force. The reader who most needs it is
    the one who has just found out that the tempo is not what they want.

    BOTH surfaces that can, because this renderer is neither: it has no keyboard,
    which is the whole of what the printed panels are. The verb is the clickable
    renderer's `✎` or the board's page, and naming only one of them would send
    somebody to a browser they did not need."""
    panel = dash.panel_dials({"asked": True, "dials": []}, 80, cfg(), None)
    assert any("dials/view" in line for line in _lines(panel))
    assert any("qb-dash-tui" in line for line in _lines(panel))


def test_the_panel_draws_the_argument_for_every_value(dash, watched):
    """The board REQUIRES a reason on every write — "a dial nobody can read an
    argument for is a dial nobody can decide to remove" — so a surface that renders
    the value and drops the reason throws away the thing that lets anybody undo
    it."""
    panel = dash.panel_dials(
        {"asked": True, "dials": [dial(reason="draining the PR backlog")]},
        80, cfg(), None)
    assert any("draining the PR backlog" in line for line in _lines(panel))


def test_an_unanswered_board_does_not_report_zero_dials_in_force(dash, watched):
    """A COUNT IS A CLAIM. "0 in force" over an unreachable board says the fleet is
    running on its defaults, which is the one thing an unanswered read cannot
    establish."""
    panel = dash.panel_dials({"asked": True, "error": "URLError"}, 80, cfg(), None)
    assert "unreadable" in str(panel.title)
    assert "0 in force" not in str(panel.title)


def test_a_panel_that_has_not_asked_yet_says_so_rather_than_answering(dash, watched):
    panel = dash.panel_dials({}, 80, cfg(), None)
    assert "asking" in str(panel.title)
    assert any("asking" in line for line in _lines(panel))


def test_an_answered_empty_board_says_the_defaults_are_in_force(dash, watched):
    """Not "no dials": every dial HAS a value, and this is the state where the
    repo's own default is the one in force."""
    panel = dash.panel_dials({"asked": True, "dials": []}, 80, cfg(), None)
    assert "0 in force" in str(panel.title)
    assert any("default" in line for line in _lines(panel))


def test_an_overridden_fleet_dial_is_counted_even_though_it_is_not_drawn(dash, watched):
    """Silence would leave the person who set it fleet-wide unable to see what
    became of it."""
    panel = dash.panel_dials({"asked": True, "dials": [dial(repo=REPO)],
                              "shadowed": [dial(repo=None)]}, 80, cfg(), None)
    assert "1 overridden" in str(panel.title)


def test_the_dial_row_keeps_its_columns_aligned_at_every_width(dash):
    """Hand-aligned, because Rich has no colspan and the two lines that must run
    the panel's whole width are the reason and the URL. So the padding is this
    file's to check — a `Table.grid` is not doing it."""
    rows = [dash.dial_row(dial(name=n, value=v, repo=r, expires=e), 24, True)
            for n, v, r, e in (("tempo", "eager", None, None),
                               ("review_panel.max_rounds", 2, REPO, in_hours(3)))]
    assert len({len(r.plain) for r in rows}) == 1, "every row is the same width"


def _printed(renderable, width: int = 80) -> str:
    from rich.console import Console
    console = Console(width=width, no_color=True)
    with console.capture() as got:
        console.print(renderable)
    return got.get()


def test_the_tempo_rides_the_caps_line_where_the_budget_is(dash, watched):
    """The caps say what the seats MAY spend; this says whether they are supposed
    to be spending it at all, and a reader glancing at one is asking about the
    other — so they share a line rather than living two panels apart."""
    printed = _printed(dash.header(cfg(), {}, 80, [], False, None,
                                   {"depth": 2, "open": 3},
                                   {"asked": True, "dials": [dial(value="eager")]}))
    line = next(l for l in printed.splitlines() if "TEMPO" in l)
    assert "eager" in line
    assert "REVIEW" in line, "the throttle and the queue share the caps line"


def test_the_caps_line_still_draws_before_the_board_has_answered(dash, watched):
    """A cell that is not there yet must not take the row with it — and must not
    print an answer it has not got."""
    assert dash.tempo_line({}).plain == ""
    assert "TEMPO" not in _printed(dash.header(cfg(), {}, 80, [], False, None, {}, {}))


# ---- the credential the writes go out on (#479) -------------------------------
#
# The panel could always read. Writing needs a person, because `POST /dials` takes
# `app.auth.human` and every agent on a box holds the same machine token — so what
# changed is the credential and not the gate: `HumanClient` presents a signed-in
# session to the browser vhost, and the board records `human/<user>` as it always
# has. What that costs is #479's to state and this file's to pin: the session is
# readable by everything running as this user, so "the dash can set a dial" and
# "anything on this box can set a dial" are one fact.

def human(cookie="", cmd="", url="https://quarterback.fo.ls"):
    return qd.HumanClient(qd.BoardConfig("https://qb.fo.ls", "tok", "hermes",
                                         human_url=url, edge_cookie=cookie,
                                         edge_cookie_cmd=cmd))


def test_a_host_with_no_session_says_so_before_a_control_is_drawn():
    """Asked on every paint to decide whether the ✎ is a control or an
    explanation. A verb that looks available and fails on the click reads as a
    broken button — and this one would fail against a board that is healthy,
    because what is missing is on this host."""
    assert "QUARTERBACK_EDGE_COOKIE" in human().why_not()
    assert "QUARTERBACK_HUMAN_URL" in human(cookie="c", url="").why_not()


def test_a_configured_command_counts_as_a_credential_without_being_run():
    """`op read` is a network call and a possible unlock prompt. A dashboard that
    ran one every few seconds to decide whether to draw an icon would be its own
    bug, so the question `why_not` answers is about configuration only."""
    marker = Path("/tmp/qb-cookie-whynot")
    marker.unlink(missing_ok=True)
    client = human(cmd=f"printf x >> {marker}; echo session=abc")
    assert client.why_not() is None
    # A marker rather than a bare list, or this asserts nothing: the command runs
    # in a subshell, so the only evidence it did NOT run is that it left nothing.
    assert not marker.exists(), "why_not() ran the session command"
    marker.unlink(missing_ok=True)


def test_the_session_is_resolved_lazily_and_cached():
    """A value in the environment is in every child process of the shell that set
    it; a command is run when a write is actually made and the secret then lives
    in this process and nowhere else."""
    marker = "/tmp/qb-cookie-calls"
    Path(marker).unlink(missing_ok=True)
    client = human(cmd=f"printf x >> {marker}; echo session=abc")
    assert client.cookie() == "session=abc"
    assert client.cookie() == "session=abc"
    assert Path(marker).read_text() == "x", "the command ran twice for one session"
    Path(marker).unlink(missing_ok=True)


def test_a_refresh_re_reads_it_and_a_fixed_value_says_it_cannot():
    """An Authelia session expires on a wall clock, so the first thing a bounced
    write needs is the credential fetched again rather than reported."""
    client = human(cmd="cat /tmp/qb-cookie-value")
    Path("/tmp/qb-cookie-value").write_text("first\n")
    assert client.cookie() == "first"
    Path("/tmp/qb-cookie-value").write_text("second\n")
    assert client.cookie() == "first", "a cached session was re-read without being asked"
    assert client.cookie(refresh=True) == "second"
    Path("/tmp/qb-cookie-value").unlink(missing_ok=True)

    fixed = human(cookie="static")
    assert fixed.cookie() == "static"
    with pytest.raises(RuntimeError, match="cannot be refreshed"):
        fixed.cookie(refresh=True)


def test_a_session_command_that_fails_is_not_a_host_that_never_had_one():
    """Opposite states with opposite remedies. `op` wanting to be unlocked is
    fixable in ten seconds by somebody who is told; it is unfixable by somebody
    told there is no session on this host."""
    client = human(cmd="echo 'not signed in' >&2; exit 1")
    with pytest.raises(RuntimeError) as caught:
        client.cookie()
    assert qd.HumanClient.COOKIE_FAILED in str(caught.value)
    assert "not signed in" in str(caught.value)


def test_a_dial_write_carries_the_scope_only_when_there_is_one():
    """`repo` absent and `repo` blank are the same scope to the board — the fleet —
    and a fleet dial that could be written under two keys is one that can be set
    twice and resolved once."""
    sent = []
    client = human(cookie="c")
    client.post = lambda path, body: sent.append((path, body)) or {}
    client.set_dial("tempo", "eager", "draining", repo=None)
    client.set_dial("tempo", "held", "mid-release", repo=REPO, expires_at="2099-01-01T00:00:00+00:00")
    assert sent[0] == ("/dials", {"dial": "tempo", "value": "eager", "reason": "draining"})
    assert sent[1][1]["repo"] == REPO and sent[1][1]["expires_at"].startswith("2099")


def test_a_value_of_null_survives_the_write():
    """The documented off switch for `max_fix_growth`, `distant_merge_lines` and
    `escalate_on.premise_repeated`. The board goes to some trouble to keep `null`
    apart from "no row at all"; a client that dropped it would put the two back
    together."""
    sent = []
    client = human(cookie="c")
    client.post = lambda path, body: sent.append(body) or {}
    client.set_dial("review_panel.max_fix_growth", None, "off for this round")
    assert "value" in sent[0] and sent[0]["value"] is None


def test_a_dial_with_no_argument_is_refused_by_the_client_too():
    client = human(cookie="c")
    client.post = lambda path, body: pytest.fail("a request went out with no reason")
    with pytest.raises(RuntimeError, match="reason"):
        client.set_dial("tempo", "eager", "   ")

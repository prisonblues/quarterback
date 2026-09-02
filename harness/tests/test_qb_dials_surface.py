"""What the dashboard says about the dials in force — #477.

A dial is a setting: the repo supplies a default, the board states the value IN
FORCE, and the layer that answered is part of the answer (#305). Until this
landed, nothing a person or an agent looks at showed one — not the dashboard,
not `qb-board`, not the web board — so the value governing every round on the
fleet was set from an endpoint, read back by one function in
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

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

import qbdata as qd                                       # noqa: E402

REPO = "prisonblues/quarterback"
OTHER = "prisonblues/lexray"


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
    assert qd.until(None) != qd.DIAL_NO_END


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


# ---- the credential the writes go out on (#479) -------------------------------
#
# The panel could always read. Writing needs a person, because `POST /dials` takes
# `app.auth.human` and every agent on a box holds the same machine token. What
# changed is the credential and not the gate: `HumanClient` presents a person's own
# `X-Human-Key` to the SAME host as the bearer — the agent vhost, no Authelia — and
# the board records `human/<user>` as it always has.
#
# An earlier cut of this class held a signed-in Authelia session and posted to the
# browser vhost. It is gone, and the reason it is gone is the reason these tests
# look the way they do: a session expires on a wall clock, so the ✎ would have died
# whenever it lapsed and stayed dead until somebody re-minted it by hand. What the
# key costs instead is #479's to state — it sits on this workstation, readable by
# what runs here — and that is accepted rather than argued away.

def human(key="", cmd=""):
    return qd.HumanClient(qd.BoardConfig("https://qb.fo.ls", "tok", "hermes",
                                         human_key=key, human_key_cmd=cmd))


def test_a_host_with_no_key_says_so_before_a_control_is_drawn():
    """Asked on every paint to decide whether the ✎ is a control or an
    explanation. A verb that looks available and fails on the click reads as a
    broken button — and this one would fail against a board that is healthy,
    because what is missing is on this host."""
    assert "QUARTERBACK_HUMAN_KEY" in human().why_not()
    assert human(key="k").why_not() is None
    assert human(cmd="echo k").why_not() is None


def test_a_configured_command_counts_as_a_credential_without_being_run():
    """`op read` is a network call and a possible unlock prompt. A dashboard that
    ran one every few seconds to decide whether to draw an icon would be its own
    bug, so `why_not` answers about configuration only."""
    marker = Path("/tmp/qb-key-whynot")
    marker.unlink(missing_ok=True)
    client = human(cmd=f"printf x >> {marker}; echo the-key")
    assert client.why_not() is None
    assert not marker.exists(), "why_not() ran the key command"
    marker.unlink(missing_ok=True)


def test_the_key_is_resolved_lazily_and_cached():
    """A value in the environment is in every child process of the shell that set
    it; a command is run when a write is actually made, and the secret then lives
    in this process and nowhere else."""
    marker = Path("/tmp/qb-key-calls")
    marker.unlink(missing_ok=True)
    client = human(cmd=f"printf x >> {marker}; echo the-key")
    assert client.key() == "the-key"
    assert client.key() == "the-key"
    assert marker.read_text() == "x", "the command ran twice for one key"
    marker.unlink(missing_ok=True)


def test_a_rotated_key_can_be_re_read_and_a_fixed_one_says_it_cannot():
    """A static key does not go stale on its own — unlike the session this
    replaced, which is the point of it. `refresh` is for the case that remains: a
    key rotated while a long-lived dashboard was running."""
    path = Path("/tmp/qb-key-value")
    path.write_text("first\n")
    client = human(cmd=f"cat {path}")
    assert client.key() == "first"
    path.write_text("second\n")
    assert client.key() == "first", "a cached key was re-read without being asked"
    assert client.key(refresh=True) == "second"
    path.unlink(missing_ok=True)

    fixed = human(key="static")
    assert fixed.key() == "static"
    with pytest.raises(RuntimeError, match="cannot be refreshed"):
        fixed.key(refresh=True)


def test_a_key_command_that_fails_is_not_a_host_that_never_had_one():
    """Opposite states with opposite remedies. `op` wanting to be unlocked is
    fixable in ten seconds by somebody who is told; it is unfixable by somebody
    told there is no key on this host."""
    client = human(cmd="echo 'not signed in' >&2; exit 1")
    with pytest.raises(RuntimeError) as caught:
        client.key()
    assert qd.HumanClient.KEY_FAILED in str(caught.value)
    assert "not signed in" in str(caught.value)


def test_a_key_that_cannot_be_a_header_is_refused_without_being_quoted():
    """`http.client.putheader` refuses CR/LF by raising `Invalid header value
    b'<the entire secret>'`, and this dashboard turns exceptions into sentences on
    its detail line. A credential in a UI string is one in a screenshot, a
    scrollback and a tmux buffer."""
    client = human(cmd=r"printf 'SUPERSECRET\r\nX-Evil: 1'")
    with pytest.raises(RuntimeError) as caught:
        client.key()
    said = str(caught.value)
    assert "SUPERSECRET" not in said, said
    assert "not usable as a header" in said and "on purpose" in said


def test_a_literal_key_goes_through_the_same_check_and_is_stripped():
    """A config file and an environment variable carry trailing newlines as
    readily as `op` does, and a value that skipped the check would reach
    `putheader` and be quoted back."""
    assert human(key="the-key\n").key() == "the-key"
    with pytest.raises(RuntimeError) as caught:
        human(key="SUPERSECRET\rX: 1").key()
    assert "SUPERSECRET" not in str(caught.value)


def test_the_write_carries_the_key_and_the_bearer_to_the_agent_host():
    """Both, and to ONE host. The key answers "which person", the bearer answers
    "from where", and a board that got only the first would authorise the write
    with nothing to say about its origin. No Authelia in the path at all — which
    is what makes this maintainable rather than a session to keep re-minting."""
    sent = {}
    client = human(key="the-key")

    class FakeResponse:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, **kw):
        sent["url"] = req.full_url
        sent["headers"] = dict(req.header_items())
        return FakeResponse()

    import urllib.request
    real = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        client.set_dial("tempo", "eager", "because")
    finally:
        urllib.request.urlopen = real
    assert sent["url"] == "https://qb.fo.ls/dials"
    lower = {k.lower(): v for k, v in sent["headers"].items()}
    assert lower["x-human-key"] == "the-key"
    assert lower["authorization"] == "Bearer tok"


def test_a_dial_write_carries_the_scope_only_when_there_is_one():
    """`repo` absent and `repo` blank are the same scope to the board — the fleet —
    and a fleet dial that could be written under two keys is one that can be set
    twice and resolved once."""
    sent = []
    client = human(key="k")
    client.post = lambda path, body: sent.append((path, body)) or {}
    client.set_dial("tempo", "eager", "draining", repo=None)
    client.set_dial("tempo", "held", "mid-release", repo=REPO,
                    expires_at="2099-01-01T00:00:00+00:00")
    assert sent[0] == ("/dials", {"dial": "tempo", "value": "eager", "reason": "draining"})
    assert sent[1][1]["repo"] == REPO and sent[1][1]["expires_at"].startswith("2099")


def test_a_value_of_null_survives_the_write():
    """The documented off switch for `max_fix_growth`, `distant_merge_lines` and
    `escalate_on.premise_repeated`. The board goes to some trouble to keep `null`
    apart from "no row at all"; a client that dropped it would put them back
    together."""
    sent = []
    client = human(key="k")
    client.post = lambda path, body: sent.append(body) or {}
    client.set_dial("review_panel.max_fix_growth", None, "off for this round")
    assert "value" in sent[0] and sent[0]["value"] is None


def test_a_dial_with_no_argument_is_refused_by_the_client_too():
    client = human(key="k")
    client.post = lambda path, body: pytest.fail("a request went out with no reason")
    with pytest.raises(RuntimeError, match="reason"):
        client.set_dial("tempo", "eager", "   ")


# ---- the four a second opinion found ------------------------------------------
#
# Found by `codex` on the cookie-era diff; three of the four are about code that
# survived the change of credential, and they are kept because every one is a
# silent failure — a wrong value written without complaint, a credential on a
# screen, a crash inside a UI callback.

@pytest.mark.parametrize("text", ["99999999999999999999d", "1234567d"])
def test_a_duration_too_large_to_be_one_is_refused_rather_than_overflowing(text):
    """`timedelta` raises OverflowError, not ValueError, past about 2.7 million
    days — so an unbounded regex hands a UI callback that catches the documented
    failure a crash instead."""
    with pytest.raises(ValueError, match="not a duration"):
        qd.parse_dial_expiry(text)


def test_the_durations_a_person_actually_types_still_work():
    for text in ("30m", "4h", "7d", "999999d"):
        assert qd.parse_dial_expiry(text) is not None


def test_the_detail_says_which_door_the_dial_came_through():
    """The identity is the same by either method, so the detail line carries the
    other half: a browser the edge vouched for, or a key on a workstation (#479)."""
    said = qd.dial_detail(dial(set_by="human/rich") | {"set_via": "key"})
    assert "with a key" in said, said
    assert "in a browser" in qd.dial_detail(dial() | {"set_via": "edge"})


def test_a_dial_older_than_the_column_does_not_invent_a_method():
    """Absent is left out rather than guessed at — the same rule the board keeps
    for the column itself."""
    said = qd.dial_detail(dial(set_by="human/rich"))
    assert "with a key" not in said and "in a browser" not in said
    assert "human/rich" in said


def test_a_key_command_that_hangs_names_the_prompt_nobody_answered():
    """`op read` against a desktop-app integration raises a biometric prompt, and
    a prompt nobody answers is a command that never returns — measured at 30s to
    fail and 8.7s to succeed once approved, on the day the real credential was
    minted. "TimeoutExpired" tells a person nothing they can act on."""
    client = human(cmd="sleep 60")
    import subprocess as sp
    real = sp.run

    def fake(*a, **kw):
        raise sp.TimeoutExpired(cmd="op", timeout=30)

    sp.run = fake
    try:
        with pytest.raises(RuntimeError) as caught:
            client.key()
    finally:
        sp.run = real
    said = str(caught.value)
    assert "desktop" in said and "✎" in said, said


# ---------------------------------------------------------------------- #577
# The variable every site's key command interpolates, and the reason no dial had
# ever been set on this fleet.
#
# `qb-env` sets QUARTERBACK_AGENT after sourcing the config and before evaluating
# anything out of it, so a reader that pulls the commands out and runs them itself
# has to export it too. `qb-doctor` always did; this module did not, in two places.
# The token got away with it because this fleet's TOKEN_CMD falls back to a file
# that exists. The human key has no fallback, so what it read was
# `op://…/quarterback-/human` — an item with an empty segment where the host name
# belongs, which does not exist and never errored in a way anybody saw.


def test_the_key_command_is_run_with_the_agent_name_it_interpolates():
    """The #577 regression, in one line. Every site's command names the host —
    `op read "op://personal-nix/quarterback-$QUARTERBACK_AGENT/human"` — and an
    unset variable turns that into a path with an empty segment."""
    assert human(cmd='echo "item-quarterback-$QUARTERBACK_AGENT"').key() \
        == "item-quarterback-hermes"


def test_an_unset_agent_would_have_read_a_path_with_an_empty_segment():
    """What the bug actually produced, pinned so the shape is recognisable if it
    ever comes back: the vault path is well-formed, addresses nothing, and `op`
    does not fail in a way the dashboard could show."""
    got = human(cmd='echo "op://personal-nix/quarterback-$QUARTERBACK_AGENT/human"').key()
    assert got == "op://personal-nix/quarterback-hermes/human"
    assert "quarterback-/" not in got


def test_the_agent_a_host_names_itself_beats_the_hostname(tmp_path, monkeypatch):
    """`qb-env`'s precedence: an explicit QUARTERBACK_AGENT is somebody naming a
    machine and the hostname is a guess. A fleet that renames a host in its config
    and not in its DNS would otherwise authenticate as the wrong one."""
    config = tmp_path / "config"
    config.write_text("QUARTERBACK_BASE_URL='https://qb.invalid'\n"
                      "QUARTERBACK_AGENT='the-named-one'\n"
                      "QUARTERBACK_TOKEN_CMD='echo tok-$QUARTERBACK_AGENT'\n")
    for name in ("QUARTERBACK_BASE_URL", "QUARTERBACK_TOKEN", "QUARTERBACK_AGENT",
                 "QUARTERBACK_TOKEN_CMD", "QUARTERBACK_HUMAN_URL",
                 "QUARTERBACK_HUMAN_KEY", "QUARTERBACK_HUMAN_KEY_CMD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QUARTERBACK_CONFIG", str(config))
    cfg = qd.resolve_config()
    assert cfg.agent == "the-named-one"
    # AND the token command saw it, which is the half that actually authenticates.
    assert cfg.token == "tok-the-named-one"


def test_the_config_is_read_for_the_agent_even_when_nothing_else_is_missing(
        tmp_path, monkeypatch):
    """The case the test above does NOT cover, and the hole it was hiding.

    That one leaves the environment bare, so the config is sourced because four
    other values are absent and the agent rides along. Here the environment
    carries everything else — so if `not agent` is not itself a reason to read the
    file, the configured name is never seen and the hostname is substituted in
    silence. The damage is not cosmetic: every credential command interpolates
    this, so the host resolves another machine's vault item and announces itself
    to the board under a name that is not its own.
    """
    config = tmp_path / "config"
    config.write_text("QUARTERBACK_AGENT='the-named-one'\n")
    monkeypatch.setenv("QUARTERBACK_CONFIG", str(config))
    monkeypatch.setenv("QUARTERBACK_BASE_URL", "https://qb.invalid")
    monkeypatch.setenv("QUARTERBACK_TOKEN", "tok")
    monkeypatch.setenv("QUARTERBACK_HUMAN_URL", "https://human.invalid")
    monkeypatch.setenv("QUARTERBACK_HUMAN_KEY", "key")
    monkeypatch.delenv("QUARTERBACK_AGENT", raising=False)
    assert qd.resolve_config().agent == "the-named-one"


def test_a_host_that_names_no_agent_still_gets_the_short_hostname(tmp_path, monkeypatch):
    """The fallback, and shortened the way `qb-env` shortens it — the board's
    machine names are bare, and `uname -n` can carry a domain."""
    config = tmp_path / "config"
    config.write_text("QUARTERBACK_BASE_URL='https://qb.invalid'\n"
                      "QUARTERBACK_TOKEN_CMD='echo tok'\n")
    for name in ("QUARTERBACK_BASE_URL", "QUARTERBACK_TOKEN", "QUARTERBACK_AGENT",
                 "QUARTERBACK_TOKEN_CMD", "QUARTERBACK_HUMAN_URL",
                 "QUARTERBACK_HUMAN_KEY", "QUARTERBACK_HUMAN_KEY_CMD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QUARTERBACK_CONFIG", str(config))
    cfg = qd.resolve_config()
    assert cfg.agent and "." not in cfg.agent, cfg.agent


# ---------------------------------------------------------------------- #539
# The vocabulary a person needs in order to SET one.
#
# The read half above renders what the board said and asserts nothing about what
# it means, which is right for a reader — the board stores `dial` as opaque text
# and `value` as opaque JSON on purpose. A WRITER cannot work that way: `POST
# /dials` takes any dotted path and any JSON, so a misspelt name or a quoted `"2"`
# is stored, returned as in force, and then ignored by every harness that reads
# it. This is the only side that can catch that, and these are the three ways of
# being wrong about it:
#
#   * a SECOND COPY of the table — the failure #56's rule exists to end, and the
#     one that would look right for months
#   * a form that refuses what the board would take, on a box that cannot read the
#     table at all, leaving a person with no door
#   * choices offered as words and judged as values, which is a picker whose own
#     suggestions are refused


def test_the_vocabulary_is_the_harness_table_and_not_a_copy_of_it():
    """Read out of `harness_rules` at call time. A test that listed the names would
    be the second place they are written down, so this compares the two."""
    sys.path.insert(0, str(BIN.parent / "loops"))
    import harness_rules as hr

    assert set(qd.dial_vocabulary()) == set(hr.BOARD_DIALS)


def test_every_word_the_picker_offers_survives_the_box_it_is_typed_into():
    """The coupling only this suite can assert: the choices are TYPED TEXT and the
    judge takes VALUES, so `true` is four characters here and a boolean by the time
    `dial_problem` sees it. A picker whose own suggestions come back refused is
    worse than no picker."""
    for path, spec in qd.dial_vocabulary().items():
        for choice in spec["choices"]:
            value = qd.parse_dial_value(choice)
            assert qd.dial_refusal(path, value) == "", (path, choice, value)


def test_a_default_is_offered_back_in_the_spelling_the_box_accepts():
    """The other direction, and the one a person actually walks: the spec line
    prints a default, somebody types it back, and it has to survive. `P3` round
    trips as itself and `3.0` as a number — the two halves of `parse_dial_value`."""
    vocab = qd.dial_vocabulary()
    for path, spec in vocab.items():
        if not spec["default_known"] or spec["default"] is None:
            continue
        typed = spec["default"] if isinstance(spec["default"], str) \
            else json.dumps(spec["default"])
        assert qd.dial_refusal(path, qd.parse_dial_value(typed)) == "", (path, typed)


def test_a_misspelt_name_is_refused_here_because_the_board_cannot_refuse_it():
    """`POST /dials` accepts it, stores it, and reports it as in force for ever
    while nothing applies it. The sentence names who settles the list, because the
    fix is to type a different name and not to go and clear something."""
    said = qd.dial_refusal("review_panel.fix_sevrity_floor", "P2")
    assert "not a board-settable dial" in said


def test_a_value_of_the_wrong_shape_is_refused_with_the_harness_own_sentence():
    """Not a sentence of the dashboard's own: the reason a quoted `"2"` is wrong is
    a fact about the harness that will ignore it, and two spellings of that reason
    would be two things to keep true."""
    assert "must be a number" in qd.dial_refusal("review_panel.max_rounds", "2")
    assert qd.dial_refusal("review_panel.max_rounds", 2) == ""


def test_a_box_that_cannot_read_the_table_refuses_nothing_and_says_so(monkeypatch):
    """An empty vocabulary is "cannot tell", never "nothing is settable" — and a
    form that refused a write the board would have taken would leave the person at
    that keyboard with no door at all. The cost of the false negative is the
    behaviour that shipped; the cost of a false refusal is a dial nobody can set."""
    monkeypatch.setattr(qd, "_DIAL_RULES", [None])
    assert qd.dial_vocabulary() == {}
    assert qd.dial_refusal("anything.at.all", "nonsense") == ""


def test_a_harness_older_than_this_file_is_the_same_answer_as_no_harness(monkeypatch):
    """The dashboard and the loops are installed separately and can be different
    ages. A `harness_rules` with no dial table must read as "cannot tell" rather
    than raising into a modal, which takes the whole dashboard down."""
    monkeypatch.setattr(qd, "_DIAL_RULES", [object()])
    assert qd.dial_vocabulary() == {}
    assert qd.dial_refusal("review_panel.max_rounds", "2") == ""


def test_the_names_are_filtered_by_the_half_a_person_remembers():
    """Substring and not prefix: the useful half of a dial's name is in the middle
    of it. A prefix filter answers `budget` with nothing until `review_panel.` has
    been typed — which is the part nobody is unsure about."""
    vocab = qd.dial_vocabulary()
    assert len(qd.dial_matches(vocab, "budget")) >= 5
    assert qd.dial_matches(vocab, "max_rounds") == ["review_panel.max_rounds"]
    assert qd.dial_matches(vocab, "FLOOR") == qd.dial_matches(vocab, "floor")
    assert qd.dial_matches(vocab, "") == list(vocab)
    assert qd.dial_matches(vocab, "no such thing") == []


def test_an_exact_name_sorts_above_the_names_that_merely_contain_it():
    """Typing one in full has to put it at the top, or the completion offered on a
    refusal names something the person did not ask for."""
    vocab = qd.dial_vocabulary()
    hit = qd.dial_matches(vocab, "enabled")
    assert hit[0] == "enabled" and len(hit) > 1


def test_the_lookup_finds_the_loops_beside_this_file_in_both_layouts(tmp_path):
    """`bin/` and `loops/` are siblings in a checkout; `bin/` and
    `share/quarterback-harness/loops` are siblings in the store. A dashboard
    installed with neither gets None, which is the "cannot tell" above."""
    assert qd.loops_dir() == str(BIN.parent / "loops")
    packaged = tmp_path / "share" / "quarterback-harness" / "loops"
    packaged.mkdir(parents=True)
    (packaged / "harness_rules.py").write_text("")
    (tmp_path / "bin").mkdir()
    assert qd.loops_dir(str(tmp_path / "bin" / "qbdata.py")) == str(packaged)
    bare = tmp_path / "elsewhere" / "bin"
    bare.mkdir(parents=True)
    assert qd.loops_dir(str(bare / "qbdata.py")) is None


def test_the_list_opens_on_a_floor_and_not_on_the_off_switch():
    """`BOARD_DIALS` is written grouped — the two floors, then what a cycle costs,
    then the brakes, the budgets and the switches — and that order is carried
    through. Sorted alphabetically the picker opens on `enabled`, which switches
    this repo's reviews off entirely and is nobody's answer to "what did I come here
    to change"."""
    first = qd.dial_matches(qd.dial_vocabulary(), "")[0]
    assert first == "review_panel.fix_severity_floor", first


def test_every_dial_says_what_it_decides_in_one_line():
    """The gap #539 is actually about: 29 dotted paths and no way to tell which one
    you wanted. One line each, short enough to sit under the value box at 78
    columns, and it is a summary — the argument stays beside the key in DEFAULTS."""
    vocab = qd.dial_vocabulary()
    assert all(spec["what"] for spec in vocab.values())
    assert max(len(spec["what"]) for spec in vocab.values()) <= 2 * 66


def _box(tmp_path, name, harness_rules_source=None):
    """A fake install: `bin/qbdata.py` with or without a `loops/harness_rules.py`."""
    box = tmp_path / name
    (box / "bin").mkdir(parents=True)
    if harness_rules_source is not None:
        (box / "loops").mkdir()
        (box / "loops" / "harness_rules.py").write_text(harness_rules_source)
    return str(box / "bin" / "qbdata.py")


@pytest.fixture
def fresh(monkeypatch):
    """A process that has not yet resolved the table, and does not leak one.

    Three things have to be undone and each is a real hazard rather than tidying.
    `_DIAL_RULES` caches the module, so without a reset the `script` argument is
    ignored. `sys.modules` is what `import harness_rules` actually consults — a
    suite that has already imported the real one gets it back whatever directory is
    on the path, which is the isolation hazard this fixture exists to make visible.
    And `_dial_rules` PREPENDS to `sys.path`, so a test pointing at a broken harness
    would leave it importable for everything after it.
    """
    monkeypatch.setattr(qd, "_DIAL_RULES", [])
    monkeypatch.setattr(qd, "_DIAL_TROUBLE", "")
    monkeypatch.delitem(sys.modules, "harness_rules", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))


def test_a_harness_that_is_absent_broken_or_old_are_three_different_answers(
        tmp_path, fresh, monkeypatch):
    """All three end in an empty vocabulary and unvalidated writes, and the screen
    used to tell all three as the first — so a `harness_rules.py` sitting right
    there with a syntax error in it reported itself as an install that had never
    happened. The board layer draws the same distinction one level up
    (`_dials_unreadable` is "we could not find out", never "there is no dial")."""
    absent = _box(tmp_path, "absent")
    assert qd.dial_vocabulary(absent) == {}
    assert qd.dial_trouble(absent) == "no harness/loops beside this dashboard"

    monkeypatch.setattr(qd, "_DIAL_RULES", [])
    monkeypatch.delitem(sys.modules, "harness_rules", raising=False)
    broken = _box(tmp_path, "broken", "def dial_specs(:\n")
    assert qd.dial_vocabulary(broken) == {}
    said = qd.dial_trouble(broken)
    assert "would not import" in said and "SyntaxError" in said

    monkeypatch.setattr(qd, "_DIAL_RULES", [])
    monkeypatch.delitem(sys.modules, "harness_rules", raising=False)
    old = _box(tmp_path, "old", "BOARD_DIALS = {}\n")
    assert qd.dial_vocabulary(old) == {}
    assert "predates the dial table" in qd.dial_trouble(old)


def test_a_readable_table_has_nothing_to_report(fresh):
    """`""` is the fourth state and it has to be distinguishable from all three:
    the screen prints this sentence only when there is one."""
    assert qd.dial_vocabulary() and qd.dial_trouble() == ""


def test_a_harness_installed_after_the_dashboard_opened_is_picked_up(
        tmp_path, fresh, monkeypatch):
    """The failure is NOT cached, and an earlier cut of this cached it — on the
    stated grounds that the modal asks on every keystroke, which it does not: it
    asks once when the modal opens. So the saving was imaginary and the cost was a
    dashboard that went on saying the table could not be read until somebody
    restarted it."""
    box = tmp_path / "later"
    (box / "bin").mkdir(parents=True)
    script = str(box / "bin" / "qbdata.py")
    assert qd.dial_vocabulary(script) == {}

    (box / "loops").mkdir()
    (box / "loops" / "harness_rules.py").write_text(
        "from collections import namedtuple\n"
        "Dial = namedtuple('Dial', 'kind nullable rule what')\n"
        "BOARD_DIALS = {'a.b': Dial('number', False, 'either', 'what it does')}\n"
        "def dial_specs():\n"
        "    return {'a.b': {'dial': 'a.b', 'what': 'what it does', 'kind': 'number',\n"
        "                    'nullable': False, 'rule': 'either', 'default': 1,\n"
        "                    'default_known': True, 'choices': [], 'hint': 'a number',\n"
        "                    'note': ''}}\n")
    monkeypatch.delitem(sys.modules, "harness_rules", raising=False)
    assert list(qd.dial_vocabulary(script)) == ["a.b"]
    assert qd.dial_trouble(script) == ""


def test_a_value_no_number_can_be_is_refused_before_the_board_has_to_refuse_it():
    """`json.loads` accepts `NaN` and `Infinity` as bare literals, and `NaN`
    compares false against every bound there is — so a floor, a round cap or a
    budget took it. `POST /dials` refuses all three (`allow_nan=False`, because
    Postgres will not store them); this is that refusal made where the value is
    typed, which is what a client owning the vocabulary is for."""
    for text in ("NaN", "Infinity", "-Infinity"):
        value = qd.parse_dial_value(text)
        said = qd.dial_refusal("review_panel.max_rounds", value)
        assert "finite" in said, (text, said)
    assert qd.dial_refusal("review_panel.max_rounds", 2) == ""

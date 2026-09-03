"""`qb-claim` and `qb-claimed` — the two halves of the pickup gate (#172).

`claims()` returned `[]` fleet-wide for four months. One half of why is that
nothing automatic ever wrote a claim; the other is that there was nothing to
*read* a claim with either. `preland.py` had a reader with no writer, #131 had a
writer with no reader, and neither was a thing a hook could call.

These two are that pair, as CLIs, so the enforcement half can live in a hook (in
nix-fleet, out of this repo) without that hook re-implementing anything:

  qb-claim issue 172     take it     — 0 taken / 1 held by somebody / 2 unknown
  qb-claimed             read it     — 0 held  / 1 free              / 2 unknown

**Three exit codes, not two, and that is the load-bearing decision.** A gate that
reads "cannot tell" as "nothing held" fails open on every unconfigured or
unreachable host — which is a gate that stops nothing on exactly the hosts nobody
checked. `preland.py` states the same rule about itself: *"a merge gate that fails
open wherever it cannot see is not a gate."*

**And neither composes a key.** They name the resource and the board derives the
key. A shell tool spelling `<repo>#<n>` itself would be a third implementation of
the rule, which is the defect #172 is about with a new party.

Run: pytest harness/tests
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
CLAIM, CLAIMED = BIN / "qb-claim", BIN / "qb-claimed"


def run(script: Path, *args, board: str | None = None, repo: str = "acme/widget",
        answer: dict | None = None, status: int = 200,
        replies: list | None = None, tmp_path: Path = None,
        gh_title: str | None = None):
    """Run one of the CLIs against a stubbed board and a stubbed repo lookup.

    Both are stubbed by running a COPY of the script beside a stub `qbdata.py`.
    The scripts find their own directory first (`sys.path.insert(0, dirname(
    __file__))`, which is how an installed harness locates the module beside it in
    $out/bin), so PYTHONPATH cannot shadow it — the copy is the seam that can.
    Doubling the module rather than standing up a board keeps this suite in the
    harness job, which has no database and no services: the same reason every
    other test here doubles `sh`.

    `gh_title` puts a `gh` on PATH ahead of any real one, answering that title and
    recording that it was asked (`gh_asked`). The title lookup is a subprocess and
    therefore the only part of this tool that cannot be observed from the request
    body: a claim that skips it and one that made the call and got nothing back
    send the same payload. `--no-plan-item` is supposed to skip it, and "supposed
    to" without a seam is what an assertion on the payload alone would have proved.

    `replies` scripts CONSECUTIVE answers — `[(status, body), …]` — for the one
    path that asks twice: a contended 409 is a lost race with no holder, which a
    second POST normally wins, so "the retry takes it" and "still contended" are
    different outcomes and both have to be reachable. The last reply repeats, so a
    single `status`/`answer` still describes a board that keeps saying the same
    thing however often it is asked.
    """
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    copied = stub / script.name
    copied.write_bytes(script.read_bytes())
    (stub / "qbdata.py").write_text(f"""
import json, os, urllib.error, io

REPO = {repo!r}
BOARD = {board!r}
REPLIES = json.loads({json.dumps(replies or [(status, answer)])!r})
CALLS = []


def repo_slug(path="."):
    return REPO or None


class _Client:
    def claim_held(self, repo, session=None):
        return _answer()

    def claim_ref(self, kind, value, repo=None, **over):
        # Recorded to disk rather than kept in memory: the CLI runs as a
        # subprocess, so this module's globals die with it and the request body is
        # otherwise unobservable. It is the body — not the exit code — that carries
        # `plan_item`, so a test for #722 has nowhere else to look.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "call.json")
        with open(path, "w") as fh:
            json.dump({{"kind": kind, "value": value, "repo": repo,
                        "body": over}}, fh)
        return _answer()


def _answer():
    status, body = REPLIES[min(len(CALLS), len(REPLIES) - 1)]
    CALLS.append(status)
    if status != 200:
        raise urllib.error.HTTPError(
            "http://board/x", status, "nope", {{}},
            io.BytesIO(json.dumps(body).encode()))
    return body


def board_client():
    if BOARD is None:
        raise RuntimeError("no board configured (QUARTERBACK_BASE_URL is unset)")
    return _Client(), None


def lapsed_redirect(previously, repo_path="."):
    # The real one reads this box's disk; here it only has to prove the wiring —
    # that qb-claim passes the board's `previously` to it and prints what comes
    # back. What it says about a worktree is `tests/test_lapsed_claims.py`'s.
    return ["  previously: " + previously['redirect']] if previously else []
""")
    env = {**os.environ}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if gh_title is not None:
        env["PATH"] = f"{_fake_gh(tmp_path, gh_title)}{os.pathsep}{env['PATH']}"
    return subprocess.run([sys.executable, str(copied), *args],
                          capture_output=True, text=True, env=env)


def _fake_gh(tmp_path: Path, title: str) -> Path:
    """A `gh` that answers one title and leaves a mark saying it was asked."""
    bindir = tmp_path / "ghbin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    # Quoted through `shlex`, not interpolated: `tmp_path` is pytest's and a title
    # is a PR's, so both can carry a space or a quote, and a fake tool that breaks
    # on one would fail the test it is the seam for rather than the code.
    gh.write_text("#!/bin/sh\n"
                  f"touch {shlex.quote(str(tmp_path / 'gh_asked'))}\n"
                  f"printf '%s\\n' {shlex.quote(title)}\n")
    gh.chmod(0o755)
    return bindir


def sent(tmp_path: Path) -> dict:
    """The request body `qb-claim` actually put on the wire."""
    return json.loads((tmp_path / "stub" / "call.json").read_text())["body"]


# ------------------------------------------------------------------- qb-claimed

def test_held_is_zero(tmp_path):
    got = run(CLAIMED, board="http://b", tmp_path=tmp_path,
              answer={"held": True, "holder": "zeus/me", "claims": [
                  {"key": "acme/widget#172", "note": "landing it",
                   "expires": "2026-08-20T18:00:00Z"}], "unattributed": []})
    assert got.returncode == 0, got.stderr
    assert "acme/widget#172" in got.stderr and "landing it" in got.stderr


def test_free_is_one(tmp_path):
    got = run(CLAIMED, board="http://b", tmp_path=tmp_path,
              answer={"held": False, "holder": "zeus/me", "claims": [],
                      "unattributed": []})
    assert got.returncode == 1
    assert "free" in got.stderr


def test_an_unreachable_board_is_two_not_one(tmp_path):
    """The whole design. 2 is "I could not tell you", and a caller that treats it
    as 1 has a gate that passes everything on any host without a token."""
    got = run(CLAIMED, board=None, tmp_path=tmp_path, answer={})
    assert got.returncode == 2
    assert "unknown" in got.stderr


def test_a_checkout_with_no_remote_is_unknown_not_free(tmp_path):
    """A repo whose identity cannot be derived is one the board cannot key a claim
    in at all — a thing to fix, not a thing to proceed past."""
    got = run(CLAIMED, board="http://b", repo="", tmp_path=tmp_path, answer={})
    assert got.returncode == 2
    assert "git remote" in got.stderr


def test_holding_something_unattributed_is_reported_while_still_being_free_here(tmp_path):
    """"I am working, and the key does not say which repo" is a different answer
    from "I am idle". A gate that collapsed them would stop an agent that is
    demonstrably busy."""
    got = run(CLAIMED, board="http://b", tmp_path=tmp_path,
              answer={"held": False, "holder": "zeus/me", "claims": [],
                      "unattributed": [{"key": "plan:abc", "note": "surveying"}]})
    assert got.returncode == 1
    assert "plan:abc" in got.stderr and "surveying" in got.stderr


def test_quiet_says_nothing_and_still_answers(tmp_path):
    got = run(CLAIMED, "--quiet", board="http://b", tmp_path=tmp_path,
              answer={"held": False, "holder": "x", "claims": [], "unattributed": []})
    assert got.returncode == 1 and got.stderr == "" and got.stdout == ""


def test_json_prints_the_boards_own_answer(tmp_path):
    answer = {"held": True, "holder": "zeus/me", "claims": [], "unattributed": [],
              "repo": "acme/widget"}
    got = run(CLAIMED, "--json", board="http://b", tmp_path=tmp_path, answer=answer)
    assert json.loads(got.stdout)["repo"] == "acme/widget"


# --------------------------------------------------------------------- qb-claim

def test_taking_a_claim_prints_the_id_on_stdout(tmp_path):
    """The id is what `claim/renew` and `claim/release` want, so it goes where a
    caller can capture it. Everything else is commentary, on stderr."""
    got = run(CLAIM, "issue", "172", board="http://b", tmp_path=tmp_path,
              answer={"claim_id": "abc-123", "kind": "work",
                      "key": "acme/widget#172", "expires": "2026-08-20T18:00:00Z",
                      "renewed": False})
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "abc-123"
    assert "claimed: work/acme/widget#172" in got.stderr


def test_a_previous_holder_who_vanished_is_printed_beside_the_claim(tmp_path):
    """#568. `create-worktree` runs this tool and passes its stderr straight to
    the terminal, so this line is how a checkout for an abandoned issue says
    where the last agent's worktree was. The claim is still TAKEN — exit 0 — and
    that is the design: it redirects, it never refuses."""
    got = run(CLAIM, "issue", "196", board="http://b", tmp_path=tmp_path,
              answer={"claim_id": "abc-123", "kind": "work",
                      "key": "acme/widget#196", "expires": "t", "renewed": False,
                      "previously": {"redirect": "acme/widget#196 was claimed on "
                                     "2026-08-18 by zeus/lantern-cedar, and that "
                                     "claim lapsed"}})
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "abc-123", "the id still goes to stdout, alone"
    assert "previously:" in got.stderr and "lantern-cedar" in got.stderr


def test_a_claim_on_a_key_nobody_abandoned_says_nothing_about_it(tmp_path):
    """The common case. An advisory that printed a line on every pickup is one
    nobody reads by the second week."""
    got = run(CLAIM, "issue", "172", board="http://b", tmp_path=tmp_path,
              answer={"claim_id": "abc", "kind": "work", "key": "k",
                      "expires": "t", "renewed": False})
    assert got.returncode == 0
    assert "previously" not in got.stderr


def test_a_lapse_lookup_that_failed_is_said_rather_than_swallowed(tmp_path):
    """The board answers `previously_error` when the claim landed and the lookup
    did not. Silence there is indistinguishable from "nobody was here before"."""
    got = run(CLAIM, "issue", "172", board="http://b", tmp_path=tmp_path,
              answer={"claim_id": "abc", "kind": "work", "key": "k",
                      "expires": "t", "renewed": False,
                      "previously": None,
                      "previously_error": "the claims table is on fire"})
    assert got.returncode == 0, "the claim is yours whatever the advice did"
    assert "could not check for a previous holder" in got.stderr
    assert "on fire" in got.stderr


def test_a_renew_says_renewed_rather_than_claimed(tmp_path):
    got = run(CLAIM, "issue", "172", board="http://b", tmp_path=tmp_path,
              answer={"claim_id": "abc", "kind": "work", "key": "k",
                      "expires": "t", "renewed": True})
    assert got.returncode == 0 and "renewed:" in got.stderr


def test_a_conflict_is_one_and_names_who_has_it(tmp_path):
    """The refusal is the coordination, not the denial: an agent told only "held"
    can do nothing but retry."""
    detail = {"detail": {"held_by": "zeus/thorn-spruce", "key": "acme/widget#172",
                         "note": "landing #172", "session": "s-9",
                         "acquired": "12:00", "expires": "13:00"}}
    got = run(CLAIM, "issue", "172", board="http://b", status=409,
              answer=detail, tmp_path=tmp_path)
    assert got.returncode == 1
    assert "zeus/thorn-spruce" in got.stderr and "landing #172" in got.stderr
    assert "s-9" in got.stderr, "the holder's session is how you reach them"


def test_any_other_http_answer_is_unknown_not_a_conflict(tmp_path):
    """A 401 is a token problem and a 500 is a board problem. Reading either as
    "somebody else has it" would send an agent looking for a peer that is not
    there."""
    for status in (401, 500):
        got = run(CLAIM, "issue", "1", board="http://b", status=status,
                  answer={"detail": "nope"}, tmp_path=tmp_path)
        assert got.returncode == 2, f"HTTP {status} was not 'unknown'"


@pytest.mark.parametrize("status", [401, 403, 422])
def test_a_definite_refusal_exits_two_but_is_not_reported_as_an_outage(status, tmp_path):
    """The exit code cannot tell these apart from a dead board — there are three
    codes and 1 names a holder — so the TEXT has to. It said "the board refused the
    claim", `create-worktree` printed "a board outage must not stop a checkout" over
    it, and a rotated token then read as something to wait out."""
    got = run(CLAIM, "issue", "1", board="http://b", status=status,
              answer={"detail": "no"}, tmp_path=tmp_path)
    assert got.returncode == 2
    assert "not an outage" in got.stderr
    assert "will refuse it again" in got.stderr


def test_a_definite_refusal_prints_the_boards_sentence_not_a_dict_repr(tmp_path):
    """A 422 carries the reason a ref could not be keyed, which is the whole of what
    the operator needs; printing the envelope round it hides it in a python repr."""
    got = run(CLAIM, "issue", "1", board="http://b", status=422,
              answer={"detail": {"error": "repo must be `owner/name`"}},
              tmp_path=tmp_path)
    assert got.returncode == 2
    assert "repo must be `owner/name`" in got.stderr
    assert "{'error'" not in got.stderr


def test_a_contended_409_is_retried_rather_than_reported_as_held(tmp_path):
    """`acquire` answers 409 twice, and only one of them has a holder in it. The
    contended one is the losing half of an insert race whose winner had already
    gone — nobody holds the key, so the race is over and asking again wins it."""
    taken = {"claim_id": "abc", "kind": "work", "key": "acme/widget#1",
             "expires": "t", "renewed": False}
    got = run(CLAIM, "issue", "1", board="http://b", tmp_path=tmp_path, replies=[
        (409, {"detail": {"error": "claim contended; try again",
                          "kind": "work", "key": "acme/widget#1"}}),
        (200, taken)])
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "abc"


def test_a_contention_that_survives_the_retry_is_unknown_not_held(tmp_path):
    """Read as HELD it became a `create-worktree` `die` telling the operator to go
    and talk to a holder that does not exist. It is not 0 either — nobody holds it
    and neither do we — so `--require-claim` still refuses."""
    got = run(CLAIM, "issue", "1", board="http://b", status=409,
              answer={"detail": {"error": "claim contended; try again",
                                 "kind": "work", "key": "acme/widget#1"}},
              tmp_path=tmp_path)
    assert got.returncode == 2, "a 409 with nobody in it was read as a holder"
    assert "contended" in got.stderr
    assert "held:" not in got.stderr, "a phantom holder is worse than an unknown"


def test_a_409_the_tool_cannot_parse_invents_no_holder(tmp_path):
    """The holder is what makes a refusal actionable, and a body that carries none —
    a proxy's HTML, a reason string — used to print "somebody else has it" anyway,
    which is a peer to go and find that was never there."""
    got = run(CLAIM, "issue", "1", board="http://b", status=409,
              answer="503 from something in front of the board", tmp_path=tmp_path)
    assert got.returncode == 2
    assert "somebody else" not in got.stderr


def test_a_repoless_kind_needs_no_remote(tmp_path):
    """A plan or an item id is globally unique already. Requiring a repo would
    give one row two keys depending on which directory the caller stood in — and
    would make a plan unclaimable from anywhere but a checkout."""
    got = run(CLAIM, "item", "0d2b6f1e-0000-4000-8000-000000000000",
              board="http://b", repo="", tmp_path=tmp_path,
              answer={"claim_id": "z", "kind": "work", "key": "item:0d2b",
                      "expires": "t", "renewed": False})
    assert got.returncode == 0, got.stderr


def test_a_rewritten_key_is_reported_to_the_caller(tmp_path):
    """An agent that believes it holds one key while the row reads another is the
    #172 defect with the parties swapped, so the board says so and this passes it
    on."""
    got = run(CLAIM, "issue", "1", board="http://b", tmp_path=tmp_path,
              answer={"claim_id": "z", "kind": "work", "key": "acme/widget#1",
                      "expires": "t", "renewed": False,
                      "note_on_key": "you asked for issue/…; the board keys it as work/…"})
    assert got.returncode == 0
    assert "the board keys it as" in got.stderr


def test_json_replaces_the_id_on_stdout_rather_than_following_it(tmp_path):
    """It used to print both, so `--json` emitted JSON with a bare uuid stuck to the
    end of it — unparseable by the only consumer that asks for JSON."""
    got = run(CLAIM, "--json", "issue", "1", board="http://b", tmp_path=tmp_path,
              answer={"claim_id": "abc", "kind": "work", "key": "k", "expires": "t",
                      "renewed": False})
    assert got.returncode == 0, got.stderr
    assert json.loads(got.stdout)["claim_id"] == "abc"


def test_quiet_means_the_exit_code_and_nothing_else(tmp_path):
    """"exit code only" has to include stdout. It printed the id anyway."""
    got = run(CLAIM, "--quiet", "issue", "1", board="http://b", tmp_path=tmp_path,
              answer={"claim_id": "abc", "kind": "work", "key": "k", "expires": "t",
                      "renewed": False})
    assert got.returncode == 0
    assert got.stdout == "" and got.stderr == ""


@pytest.mark.parametrize("script", [CLAIM, CLAIMED])
def test_neither_tool_composes_a_key_itself(script):
    """The rule that makes one implementation one implementation. A shell tool
    that spelled `<repo>#<n>` would be the third producer of a key, and #172 is
    the record of what two producers cost."""
    src = script.read_text()
    for spelling in ['f"{repo}#', "'#'.join", '+ "#" +', '{repo}!']:
        assert spelling not in src, f"{script.name} is composing a claim key"


def test_qb_claim_names_the_resource_rather_than_the_key():
    """The write half is where a composed key would actually be written, so it is
    the one that has to be asking for a `ref`."""
    src = CLAIM.read_text()
    assert "claim_ref" in src
    assert "REPOLESS" in src, (
        "a plan or item id is globally unique; sending a repo alongside one would "
        "give a single row two keys depending on the caller's directory")


@pytest.mark.parametrize("script", [CLAIM, CLAIMED])
def test_quiet_wins_over_json(script, tmp_path):
    """Both flags are promises about stdout and they conflict. `--quiet` says "exit
    code only", which is the stronger one, so it wins — and it has to be decided
    rather than left to statement order, which is how `--quiet --json` printed JSON."""
    args = ("issue", "1") if script is CLAIM else ()
    got = run(script, "--quiet", "--json", *args, board="http://b",
              tmp_path=tmp_path,
              answer={"held": False, "holder": "x", "claims": [], "unattributed": [],
                      "claim_id": "abc", "kind": "work", "key": "k", "expires": "t",
                      "renewed": False})
    assert got.stdout == "" and got.stderr == ""


# ------------------------------------------- exclusivity without a pickup (#722)

TAKEN = {"claim_id": "abc-123", "kind": "work", "key": "acme/widget!715",
         "expires": "2026-09-04T18:00:00Z", "renewed": False}


def test_an_ordinary_claim_asks_for_the_plan_item_by_saying_nothing(tmp_path):
    """The default is the board's, and the request does not restate it.

    A flag that put `plan_item: true` on every payload would be the first thing to
    suspect when a board of a different version answered differently, for no gain:
    absent means the default, and the default is #427's rule.
    """
    got = run(CLAIM, "issue", "172", board="http://b", tmp_path=tmp_path,
              answer=TAKEN)
    assert got.returncode == 0, got.stderr
    assert "plan_item" not in sent(tmp_path)


def test_no_plan_item_says_so_on_the_wire(tmp_path):
    """#722. A review round holds the PR it is reading so the fleet can see who is
    spending money on it — a true exclusivity record and a false pickup. Without
    this the claim wrote the PR onto the plan at rank 1, and the release at the end
    of the round left the row there: open, unclaimed, unblocked, and `next`.

    red/green: fails on `plan_item` missing from the body — the flag did not exist,
    so `qb-claim` exited 2 on an unrecognised argument.
    """
    got = run(CLAIM, "pr", "715", "--no-plan-item", board="http://b",
              tmp_path=tmp_path, answer=TAKEN)
    assert got.returncode == 0, got.stderr
    assert sent(tmp_path)["plan_item"] is False


def test_a_claim_that_writes_no_item_does_not_pay_for_a_title(tmp_path):
    """The flag's one side-effect, and it is worth a test because it is a network
    call per round spent on a string with no consumer: the title exists to name the
    plan item, so no item means nothing to name.

    red/green: fails with `gh` recorded as asked and `title` on the body.
    """
    got = run(CLAIM, "pr", "715", "--no-plan-item", board="http://b",
              tmp_path=tmp_path, answer=TAKEN, gh_title="fix: a thing")
    assert got.returncode == 0, got.stderr
    assert not (tmp_path / "gh_asked").exists(), "asked `gh` for a title nothing uses"
    assert "title" not in sent(tmp_path)


def test_an_ordinary_claim_still_reads_the_title_from_gh(tmp_path):
    """The other side of the same gate. `--no-plan-item` must not be the flag that
    quietly turned the title lookup off for everybody — the seam is the same one,
    so the case that keeps it has to be asserted beside the case that drops it."""
    got = run(CLAIM, "issue", "172", board="http://b", tmp_path=tmp_path,
              answer=TAKEN, gh_title="the issue's real name")
    assert got.returncode == 0, got.stderr
    assert (tmp_path / "gh_asked").exists()
    assert sent(tmp_path)["title"] == "the issue's real name"


def test_an_explicit_title_is_still_dropped_when_no_item_will_hold_it(tmp_path):
    """A caller passing both is contradicting itself, and the board ignores the
    title anyway with `plan_item: false`. Nothing is refused over it — a claim
    turned down for a redundant argument would be the coordination write lost to
    the tidier API — but it is not sent either, so the wire says what will happen.
    """
    got = run(CLAIM, "pr", "715", "--no-plan-item", "--title", "fix: a thing",
              board="http://b", tmp_path=tmp_path, answer=TAKEN)
    assert got.returncode == 0, got.stderr
    assert sent(tmp_path)["plan_item"] is False
    assert "title" not in sent(tmp_path)


# ------------------------------------------- an older board ignores it (#722)

#: What a board OLDER than `plan_item` answers a `--no-plan-item` claim: `ClaimIn`
#: takes pydantic's default `extra="ignore"`, so the field is discarded in silence
#: and the rank-1 row is written anyway. The answer is the only place that says so.
IGNORED = {**TAKEN, "key": "acme/widget!715",
           "plan_item": {"item_id": "d1", "rank": 1, "rank_source": "picked-up",
                         "title": "panel review round 1", "repo": "acme/widget"}}


def test_a_board_that_ignored_the_flag_is_reported_loudly(tmp_path):
    """The compatibility hole, and it is the ordinary state of a rollout: this
    harness and the board deploy separately, so for as long as the two halves
    disagree a new `qb-claim` is talking to an old board.

    The board answered with the item it wrote. Nothing read it — the claim exited 0
    and printed the cheerful "on the plan at rank 1" line, which is what a caller
    that WANTED an item gets, over the exact defect the flag exists to prevent.

    red/green: fails on the WARNING missing from stderr — the pre-fix tool printed
    the ordinary success line instead.
    """
    got = run(CLAIM, "pr", "715", "--no-plan-item", board="http://b",
              tmp_path=tmp_path, answer=IGNORED)
    assert got.returncode == 0, "the claim is real and must stand"
    assert "WARNING: --no-plan-item was IGNORED" in got.stderr
    assert "older than the flag" in got.stderr
    assert "rank 1" in got.stderr and "acme/widget!715" in got.stderr
    assert "#722" in got.stderr


def test_the_ignored_flag_does_not_read_like_a_successful_pickup(tmp_path):
    """The line a claim gets when it ASKED for the item says "on the plan at rank
    1", and it is the sentence a reader skims past. Printing it here would report
    the defect as the feature working."""
    got = run(CLAIM, "pr", "715", "--no-plan-item", board="http://b",
              tmp_path=tmp_path, answer=IGNORED)
    assert "on the plan at rank" not in got.stderr


def test_a_board_that_honoured_the_flag_says_nothing_about_the_plan(tmp_path):
    """The alarm must not fire on the ordinary case, or it is noise on every claim
    the flag is used for — which, once the fleet is upgraded, is all of them."""
    got = run(CLAIM, "pr", "715", "--no-plan-item", board="http://b",
              tmp_path=tmp_path, answer={**TAKEN, "plan_item": None})
    assert got.returncode == 0, got.stderr
    assert "WARNING" not in got.stderr
    assert "on the plan at rank" not in got.stderr


def test_an_ordinary_claim_still_reports_its_item_the_way_it_always_did(tmp_path):
    """The other side of the same branch. `--no-plan-item` must not be the change
    that turned the pickup line off for everybody — saying so is what stops the next
    agent adding the row by hand."""
    got = run(CLAIM, "issue", "172", board="http://b", tmp_path=tmp_path,
              answer={**TAKEN, "plan_item": {"item_id": "d1", "rank": 1,
                                             "rank_source": "picked-up",
                                             "title": "a thing", "repo": "acme/widget"}})
    assert got.returncode == 0, got.stderr
    assert "on the plan at rank 1 of acme/widget" in got.stderr
    assert "WARNING" not in got.stderr

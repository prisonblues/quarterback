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
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
CLAIM, CLAIMED = BIN / "qb-claim", BIN / "qb-claimed"


def run(script: Path, *args, board: str | None = None, repo: str = "acme/widget",
        answer: dict | None = None, status: int = 200, tmp_path: Path = None):
    """Run one of the CLIs against a stubbed board and a stubbed repo lookup.

    Both are stubbed by running a COPY of the script beside a stub `qbdata.py`.
    The scripts find their own directory first (`sys.path.insert(0, dirname(
    __file__))`, which is how an installed harness locates the module beside it in
    $out/bin), so PYTHONPATH cannot shadow it — the copy is the seam that can.
    Doubling the module rather than standing up a board keeps this suite in the
    harness job, which has no database and no services: the same reason every
    other test here doubles `sh`.
    """
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    copied = stub / script.name
    copied.write_bytes(script.read_bytes())
    (stub / "qbdata.py").write_text(f"""
import json, urllib.error, io

REPO = {repo!r}
BOARD = {board!r}
ANSWER = {json.dumps(answer)!r}
STATUS = {status}


def repo_slug(path="."):
    return REPO or None


class _Client:
    def claim_held(self, repo, session=None):
        return _answer()

    def claim_ref(self, kind, value, repo=None, **over):
        return _answer()


def _answer():
    if STATUS != 200:
        raise urllib.error.HTTPError(
            "http://board/x", STATUS, "nope", {{}}, io.BytesIO(ANSWER.encode()))
    return json.loads(ANSWER)


def board_client():
    if BOARD is None:
        raise RuntimeError("no board configured (QUARTERBACK_BASE_URL is unset)")
    return _Client(), None
""")
    env = {**os.environ}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    return subprocess.run([sys.executable, str(copied), *args],
                          capture_output=True, text=True, env=env)


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

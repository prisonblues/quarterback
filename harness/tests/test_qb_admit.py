"""`qb-admit` — is there room for another unit of work in this repo? (#337)

The admission half of the bound. Eight agents were run against one `main` on
2026-08-22; two branches minted migration `0029` independently, a third was
renumbered twice mid-flight, and the largest open diff went DIRTY the moment the
first landed. Nothing counted, because nothing ever had.

The three properties this suite exists for:

* **Unbounded is the default and it costs NOTHING.** With no `in_flight.max` in
  the repo's rules this exits 0 before it has looked for a board, a token or a
  network — asserted here by giving it a `qbdata` that explodes on import. That
  is what makes landing the bound a no-op for every repo that has not opted in,
  and it is the property to break last.
* **A malformed ceiling fails OPEN, loudly.** `max: "five"` is exit 2 with the
  reason named, never a silent 0 (policy going quiet) and never a refusal
  (a fleet throttled by a typo, which is the harder failure to diagnose from a
  phone).
* **The count comes from the board**, asked for by repo, and this never derives
  it from a claims listing — a fourth implementation of the join #172 is about.

Stubbed the way `test_qb_end.py` stubs one: a COPY of the script beside a stub
`qbdata.py`, because the script puts its own directory at the front of
`sys.path` and PYTHONPATH cannot shadow that.

Run: pytest harness/tests
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
ADMIT = BIN / "qb-admit"

ROOM, FULL, UNKNOWN = 0, 1, 2


def run(*args, tmp_path: Path, rules: object = "absent", count: int = 0,
        holders: list | None = None, repo: str | None = "acme/widget",
        board: bool = True, status: int = 200, sample: bool = True,
        also_untracked: object = "absent", explode_on_import: bool = False):
    """Run `qb-admit` over a checkout whose rules file says `rules`.

    `rules="absent"` writes no file at all. `explode_on_import` makes the stub
    board client unimportable, which is how the "costs nothing" property is
    asserted rather than asserted about.
    """
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    copied = stub / ADMIT.name
    copied.write_bytes(ADMIT.read_bytes())
    seen = tmp_path / "requests"
    (stub / "qbdata.py").write_text(f"""
import io, json, sys, urllib.error

if {explode_on_import!r}:
    raise ImportError("the unbounded path must not need a board client")

BOARD = {board!r}
REPO = {repo!r}
STATUS = {status!r}
ANSWER = json.loads({json.dumps({"repo": repo, "count": count,
                                 "holders": holders or [],
                                 "claims": [{"key": f"{repo}#{i}", "holder": "zeus",
                                             "note": "worktree", "expires": "later"}
                                            for i in range(count)]})!r})


class _Client:
    def get(self, path, params=None):
        with open({str(seen)!r}, "a") as fh:
            fh.write(json.dumps({{"path": path, "params": params}}) + "\\n")
        if STATUS != 200:
            raise urllib.error.HTTPError(
                "http://board" + path, STATUS, "nope", {{}},
                io.BytesIO(b'{{"detail": "no"}}'))
        return ANSWER


def repo_slug(path="."):
    return REPO


def board_client():
    if not BOARD:
        raise RuntimeError("no board configured (QUARTERBACK_BASE_URL is unset)")
    return _Client(), None
""")
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    for name, body in ((".harness-rules.sample" if sample else ".harness-rules", rules),
                       (".harness-rules", also_untracked)):
        if body == "absent":
            continue
        text = body if isinstance(body, str) else json.dumps(body)
        (root / name).write_text(text)
    got = subprocess.run([sys.executable, str(copied), "--repo-path", str(root), *args],
                         capture_output=True, text=True, env={**os.environ})
    got.requests = [json.loads(ln) for ln in
                    (seen.read_text().splitlines() if seen.exists() else [])]
    return got


# ------------------------------------------- the default is no bound, and it is free

@pytest.mark.parametrize("rules,why", [
    ("absent", "no rules file at all"),
    ({"review_panel": {"max_rounds": 2}}, "a rules file with no in_flight block"),
    ({"in_flight": {"max": None, "min": None}}, "the block at its shipped default"),
    ({"in_flight": {"min": 2}}, "a floor with no ceiling"),
])
def test_no_ceiling_is_room_and_never_touches_the_board(rules, why, tmp_path):
    """The property that makes this landable while nobody is watching: a repo that
    has not opted in behaves exactly as it did before this existed. The stub board
    client raises on IMPORT, so reaching it at all is the failure."""
    got = run(tmp_path=tmp_path, rules=rules, explode_on_import=True)
    assert got.returncode == ROOM, f"{why}: {got.stderr}"
    assert got.requests == [], f"{why}: asked the board about an unbounded repo"
    assert got.stderr == "", f"{why}: said something about a repo with no bound"


def test_the_sample_is_this_repos_own_rules_and_names_no_ceiling(tmp_path):
    """quarterback ships the block at null/null on purpose — the machinery lands
    before the number does. If somebody sets one, this test is how they find out
    they also changed what landing #337 did to this repo."""
    root = Path(__file__).resolve().parents[2]
    rules = json.loads((root / ".harness-rules.sample").read_text())
    assert "in_flight" in rules, (
        "this repo's own rules file no longer declares the block — the sample is "
        "what governs quarterback unattended, and an omitted block is a reader "
        "guessing whether the absence was a decision")
    assert rules["in_flight"]["max"] is None
    assert rules["in_flight"]["min"] is None


# ------------------------------------------------------------ a configured ceiling

def test_under_the_ceiling_is_room_and_says_the_numbers(tmp_path):
    got = run(tmp_path=tmp_path, rules={"in_flight": {"max": 5}}, count=3)
    assert got.returncode == ROOM, got.stderr
    assert "3 of 5" in got.stderr
    assert got.requests[0]["path"] == "/claims/in-flight"
    assert got.requests[0]["params"] == {"repo": "acme/widget"}


def test_at_the_ceiling_is_full(tmp_path):
    """`count < max`, not `count <= max`: the fifth of five is the slot being
    asked for, and admitting it would make the ceiling mean six."""
    got = run(tmp_path=tmp_path, rules={"in_flight": {"max": 5}}, count=5,
              holders=["zeus", "laptop"])
    assert got.returncode == FULL
    assert "5 of 5" in got.stderr


def test_over_the_ceiling_is_full_too(tmp_path):
    """A window can be over-full — `--no-bound`, a lowered ceiling, work claimed
    outside a checkout — and a `==` test would read that as room."""
    got = run(tmp_path=tmp_path, rules={"in_flight": {"max": 2}}, count=6)
    assert got.returncode == FULL


def test_a_full_window_names_who_has_the_slots(tmp_path):
    """A refusal with no holders in it sends the reader to the dashboard to find
    out what refused them, which is the state `qb-claim`'s hold already avoids."""
    got = run(tmp_path=tmp_path, rules={"in_flight": {"max": 1}}, count=1)
    assert got.returncode == FULL
    assert "acme/widget#0" in got.stderr and "zeus" in got.stderr


def test_a_ceiling_of_zero_admits_nothing(tmp_path):
    """0 is a legal value and means a freeze, not a typo — which is why the
    validator takes non-negative rather than positive integers."""
    got = run(tmp_path=tmp_path, rules={"in_flight": {"max": 0}}, count=0)
    assert got.returncode == FULL


def test_the_reason_names_the_file_the_ceiling_came_from(tmp_path):
    got = run(tmp_path=tmp_path, rules={"in_flight": {"max": 1}}, count=1)
    assert ".harness-rules.sample" in got.stderr
    assert "in_flight.max" in got.stderr


def test_the_tracked_sample_wins_over_an_untracked_overlay(tmp_path):
    """`harness_rules._read_rules`' preference order. The overlay is one box's
    narrowing of reviewer seats; policy is the tracked file, and a box that could
    lower the fleet's ceiling from an untracked file is a box that can raise it."""
    got = run(tmp_path=tmp_path, rules={"in_flight": {"max": 9}},
              also_untracked={"in_flight": {"max": 1}}, count=4)
    assert got.returncode == ROOM, got.stderr
    assert "4 of 9" in got.stderr


def test_a_repo_on_the_pre_split_layout_is_still_read(tmp_path):
    """A repo that never migrated carries `.harness-rules` and nothing else."""
    got = run(tmp_path=tmp_path, rules={"in_flight": {"max": 1}}, count=1,
              sample=False)
    assert got.returncode == FULL


# ------------------------------------------------------- everything else is unknown

@pytest.mark.parametrize("bad", ["five", True, -1, 1.5, [], {"max": 1}])
def test_a_malformed_ceiling_fails_open_and_says_so(bad, tmp_path):
    """Refusing every checkout on the fleet over a typo in a config file is the
    worse of the two failures, and the harder one to diagnose from a phone. It is
    still exit 2, so `--require-claim` can ask for the strict reading."""
    got = run(tmp_path=tmp_path, rules={"in_flight": {"max": bad}}, count=99)
    assert got.returncode == UNKNOWN, got.stderr
    assert "NOT enforcing" in got.stderr
    assert got.requests == [], "asked the board about a ceiling it could not read"


def test_a_block_that_is_not_an_object_is_unknown(tmp_path):
    got = run(tmp_path=tmp_path, rules={"in_flight": 5})
    assert got.returncode == UNKNOWN
    assert "not an object" in got.stderr


def test_a_rules_file_that_will_not_parse_is_unknown_not_unbounded(tmp_path):
    """Policy going quiet is the one failure `_baseline_json` refuses in
    harness_rules, and it is refused here for the same reason: an unparseable file
    was written to say something, and a caller that read it as "no bound" would be
    inventing the safest possible interpretation of a file nobody can read."""
    got = run(tmp_path=tmp_path, rules="{not json,")
    assert got.returncode == UNKNOWN
    assert "would not parse" in got.stderr


def test_an_unreachable_board_is_unknown(tmp_path):
    got = run(tmp_path=tmp_path, rules={"in_flight": {"max": 5}}, board=False)
    assert got.returncode == UNKNOWN
    assert "could not reach the board" in got.stderr


def test_a_board_that_answers_with_an_error_is_unknown(tmp_path):
    got = run(tmp_path=tmp_path, rules={"in_flight": {"max": 5}}, status=503)
    assert got.returncode == UNKNOWN
    assert "not enforced" in got.stderr


def test_a_repo_that_cannot_be_named_is_unknown(tmp_path):
    """The window is per repository, and a directory name is not one."""
    got = run(tmp_path=tmp_path, rules={"in_flight": {"max": 5}}, repo=None)
    assert got.returncode == UNKNOWN
    assert "owner/name" in got.stderr


# ------------------------------------------------------------------- the JSON answer

def test_json_carries_the_count_the_ceiling_and_the_floor(tmp_path):
    got = run("--json", tmp_path=tmp_path,
              rules={"in_flight": {"max": 4, "min": 2}}, count=1)
    answer = json.loads(got.stdout)
    assert answer["bounded"] is True and answer["room"] is True
    assert (answer["count"], answer["max"], answer["min"]) == (1, 4, 2)


def test_quiet_wins_over_json(tmp_path):
    got = run("--json", "--quiet", tmp_path=tmp_path,
              rules={"in_flight": {"max": 1}}, count=1)
    assert got.returncode == FULL
    assert got.stdout == "" and got.stderr == ""

"""#553: what `qb record-outcome` PRINTS about what the board recorded.

`POST /review/outcomes` returns six buckets and the renderer in `harness/bin/qb`
named three of them. The missing one was `amended` — the bucket a note rewrite
lands in, and the one the endpoint separates out on the grounds that it is the
one that matters, because a rewritten refutation is a rewritten piece of
evidence.

Omitting it did not read as silence. `amended` alone takes the 200 branch, so
`curl -fsS` succeeded, the `jq` ran, and every counter it knew about was
legitimately zero: five stored note rewrites printed as ``recorded 0, changed 0,
unchanged 0``. That is a positive report that nothing happened and it is
indistinguishable from the genuine no-op — there was no second signal to check,
because `rejected` was correctly empty and the exit code was correctly 0. The
agent that read it concluded the write had failed and put its evidence in a
commit message and a new issue instead.

So these tests are about the RENDER, not the endpoint — `test_finding_outcomes.py`
already pins what the buckets mean. They run the real jq program, lifted out of
`harness/bin/qb`, over real responses from the real endpoint, because the two
halves of this bug were each individually correct: the endpoint's body and the
client's arithmetic. Only the join was wrong, and only a test that spans it
fails.

`test_the_render_names_every_bucket_the_response_carries` is the one that matters
beyond this fix. It reads the buckets off a live response rather than off a list
written down here, so the next bucket added to the endpoint cannot go unprinted
the same way — a hand-maintained list would have been written on the day
`amended` was already missing from the render.

jq is not skipped when absent. `qb` is a curl-and-jq wrapper, the dev shell and
CI both provide it, and a test that quietly does not run is how the drift got in.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .conftest import LAPTOP

REPO_ROOT = Path(__file__).resolve().parent.parent
QB = REPO_ROOT / "harness" / "bin" / "qb"

AGENT = {**LAPTOP, "X-Agent-Instance": "553fed"}


def repo_of(case: str) -> str:
    return f"acme/i553-{case}"


def render_program() -> str:
    """The jq program `qb record-outcome` renders the response with.

    Lifted out of the script rather than copied here: a copy is a second
    renderer, and the one being tested would be the one nothing runs.
    """
    src = QB.read_text(encoding="utf-8")
    m = re.search(r"\$BASE/review/outcomes.*?\|\s*jq -r '(.*?)'\s*2>/dev/null", src, re.S)
    assert m, f"no `jq -r` render found after the /review/outcomes POST in {QB}"
    return m.group(1)


def render(response: dict) -> str:
    """Run that program over `response`, the way the pipeline in `qb` does."""
    jq = shutil.which("jq")
    assert jq, "jq is missing; `qb` is a curl-and-jq wrapper and cannot be tested without it"
    p = subprocess.run([jq, "-r", render_program()],
                       input=json.dumps(response), capture_output=True, text=True)
    assert p.returncode == 0, f"the render failed on {response!r}: {p.stderr}"
    return p.stdout.strip()


async def record(client, case: str, keys: list[str]) -> dict:
    r = await client.post("/review", headers=AGENT, json={
        "repo": repo_of(case), "pr": 1, "judged": True, "judge_model": "opus",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "opus", "ran": True}},
        "to_fix": [{"title": f"finding {k}", "severity": "P2",
                    "file": "app/api/reviews.py", "line": 10,
                    "reviewers": ["claude"], "key": k} for k in keys],
        "dismissed": [], "sonar_findings": [],
    })
    assert r.status_code == 201, r.text
    return r.json()


async def outcomes(client, case: str, items: list[dict]) -> dict:
    r = await client.post("/review/outcomes", headers=AGENT,
                          json={"repo": repo_of(case), "pr": 1, "outcomes": items})
    assert r.status_code in (200, 201), r.text
    return r.json()


async def test_note_rewrites_are_counted_and_itemised_not_reported_as_nothing(client):
    """#553 as it happened, at the size it happened: several stored notes
    rewritten in one batch, and every bucket the render knew about zero. The
    headline has to carry the count, because the count is the whole signal — a
    caller reading three zeros has been told the opposite of what the board did."""
    keys = [f"n{i}" for i in range(5)]
    await record(client, "rewrites", keys)
    await outcomes(client, "rewrites",
                   [{"key": k, "outcome": "refuted", "note": "the guard covers it"}
                    for k in keys])
    res = await outcomes(client, "rewrites",
                         [{"key": k, "outcome": "refuted", "note": "actually: the glob covers it"}
                          for k in keys])
    # The board's side of the join, so a failure below is unambiguously the render.
    assert len(res["amended"]) == 5 and res["changed"] == [] and res["unchanged"] == []

    out = render(res)
    assert out.splitlines()[0] == "recorded 0, changed 0, amended 5, unchanged 0"
    for k in keys:
        assert f"  AMENDED {k}: rewrote note" in out


async def test_an_amendment_names_which_fields_moved_and_how(client):
    """Itemised the way `rejected` is, because which field changed is the
    interesting part — and a fill is not a rewrite. The endpoint keeps them in
    separate lists (a fill adds evidence, a rewrite replaces it, and both move
    `set_by`), so a render that flattened them back into one word would throw
    away the distinction on the way to the reader."""
    await record(client, "fields", ["k1"])
    await outcomes(client, "fields", [{"key": "k1", "outcome": "refuted", "note": "not a defect"}])
    res = await outcomes(client, "fields", [
        {"key": "k1", "outcome": "refuted", "note": "the caller already checks", "attested_by": "rich"}])
    assert res["amended"] == [{"key": "k1", "outcome": "refuted",
                               "filled": ["attested_by"], "rewrote": ["note"]}]

    out = render(res)
    assert out.splitlines()[0] == "recorded 0, changed 0, amended 1, unchanged 0"
    assert "  AMENDED k1: rewrote note; filled attested_by" in out


async def test_a_genuine_no_op_still_reports_all_zeros(client):
    """The other half of the fix, and the reason the bug survived: three zeros is
    a real answer for a real no-op. Printing `amended` must make the two states
    distinguishable, not make every repeat look like work."""
    await record(client, "noop", ["k1"])
    # Attested, so the one bucket a bare refutation would otherwise light up stays
    # empty and the render is the headline and nothing else.
    await outcomes(client, "noop", [{"key": "k1", "outcome": "refuted",
                                     "note": "not a defect", "attested_by": "rich"}])
    res = await outcomes(client, "noop", [{"key": "k1", "outcome": "refuted"}])
    assert res["unchanged"] == ["k1"] and res["amended"] == []
    assert res["unattested_refutations"] == []

    assert render(res) == "recorded 0, changed 0, amended 0, unchanged 1"


async def test_the_render_names_every_bucket_the_response_carries(client):
    """The guard against the next one. Every list in the response is a bucket the
    caller is being told about, and the render is what decides whether it hears.
    Read off a live response rather than a list kept here: a written-down list is
    written by somebody reading the render, which is how `amended` came to be
    absent from both."""
    await record(client, "buckets", ["k1", "k2"])
    await outcomes(client, "buckets", [{"key": "k1", "outcome": "refuted", "note": "no"}])
    res = await outcomes(client, "buckets", [
        {"key": "k1", "outcome": "refuted", "note": "no, because the flag is off"},
        {"key": "k2", "outcome": "fixed"},
        {"key": "nosuch", "outcome": "fixed"},
    ])
    buckets = sorted(k for k, v in res.items() if isinstance(v, list))
    # Not a tautology only if the response really does carry them all.
    assert buckets == ["amended", "changed", "recorded", "rejected",
                       "unattested_refutations", "unchanged"], buckets

    program = render_program()
    # As a jq field reference (`.changed`), never as a substring: `changed` is one
    # of `unchanged`, so a plain `in` would report the render as naming a bucket it
    # had dropped — the check passing for the reason it exists to catch.
    unprinted = [b for b in buckets if not re.search(rf"\.{re.escape(b)}\b", program)]
    assert not unprinted, (
        f"{', '.join(unprinted)} come back from /review/outcomes and {QB} never "
        f"names them — a caller reading this render is told the opposite of what "
        f"the board did, which is #553")


def test_a_board_without_the_bucket_renders_a_zero_not_a_null():
    """`qb` is deployed independently of the board it talks to, so the render
    meets responses older than itself. `// []` is why that reads as `amended 0`
    rather than `amended null` — the same reason `rejected` carries one."""
    out = render({"recorded": ["a"], "changed": [], "unchanged": [], "rejected": []})
    assert out == "recorded 1, changed 0, amended 0, unchanged 0"

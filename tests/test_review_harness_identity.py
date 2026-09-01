"""#112: which harness produced this round, on the row with the round.

A payload described the electorate and the decision in careful detail —
`judge_model`, per-seat `model`/`effort`/`max_diff_chars`/`truncated`,
`diff_budgets`, `run_key`, `cycle`, `round`, `stop_reason` — and said nothing at
all about the code that ran the panel. `.harness-rules` argues at length that an
unpinned reviewer MODEL makes "codex found more than claude" unattributable, and
pins three of the four seats for exactly that reason. The harness itself was
unpinned and unrecorded, and it changes far more often than a vendor slug.

What that costs is not theoretical. On 2026-08-31 six PRs changed `round_stop`,
`converged`, the `fix_injection` accounting and `restored_lines` in one day, and
the deployed panel on one host was rebuilt underneath a running session. So two
rounds of one cycle can be read by materially different machinery, and the
r1 -> r2 comparison every stop argument in this system rests on assumes they were
not — an assumption nothing in the record could check.

**Four fields, because the question has no single true answer from inside a
running panel.** `qb-doctor`'s `check_harness` reached that conclusion from the
other side and wrote it down: the truthful answer is the flake pin's rev, no
harness script can reach it, so content stands in as a PROXY. These tests pin
which field is which — `harness_rev` authoritative and usually absent,
`harness_digest` a proxy and always present, `harness_path` a locator,
`harness_dirty` the flag that stops a rev being read as more than it is — and
they pin that a refusal of any one of them does not cost the others.
"""

from __future__ import annotations

# The module, not the names off it, for `test_review_provenance_working.py`'s
# reason: the bounds arrive with this feature, so a `from ... import` of one turns
# the red half of every other test in this file into a collection error.
from app.api import reviews

from .conftest import LAPTOP

REPO = "acme/harness112"
AGENT = {**LAPTOP, "X-Agent-Instance": "d112d1"}

#: A checkout's answer, spelled as `panel_core.harness_identity` spells it.
REV = "c" * 40
DIGEST = "loops-sha256-1:" + "9" * 64
CHECKOUT = "/home/rich/source/quarterback/harness/loops"

#: An INSTALLED harness's answer, which is the common case and not the exotic one:
#: the nix store is not a checkout, so there is no rev and no cleanliness to
#: report, and the digest is the only identity the round has.
STORE = ("/nix/store/7y1nmp0yydrwxpzx07b6ccj3fmlwic0i-quarterback-harness-0.1.0"
         "/share/quarterback-harness/loops")


def payload(pr: int, **over) -> dict:
    body = {
        "repo": REPO,
        "pr": pr,
        "pr_title": f"feat: thing {pr}",
        "base": "main",
        "reviewed": True,
        "judged": True,
        "judge_model": "opus",
        "reviewers_selected": ["claude"],
        "reviewers": {"claude": {"model": "sonnet", "ran": True}},
        "harness_rev": REV,
        "harness_dirty": False,
        "harness_digest": DIGEST,
        "harness_path": CHECKOUT,
    }
    return {**body, **over}


async def record(client, pr: int, **over) -> dict:
    r = await client.post("/review", json=payload(pr, **over), headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def detail(client, run_id: int) -> dict:
    r = await client.get(f"/review/{run_id}", headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


async def runs(client) -> list[dict]:
    r = await client.get("/reviews", params={"repo": REPO}, headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


async def test_the_harness_identity_survives_the_round_trip_verbatim(client):
    """The whole point: what ran the round is readable off the round afterwards.

    All four asserted, because the contract is verbatim on each of them: a board
    that stored the digest and dropped the path would pass a three-field test
    while losing the one field that says whether the round came from the deployed
    harness at all.
    """
    run = await detail(client, (await record(client, 1))["id"])
    assert run["harness_rev"] == REV
    assert run["harness_dirty"] is False
    assert run["harness_digest"] == DIGEST
    assert run["harness_path"] == CHECKOUT


async def test_two_rounds_of_one_cycle_can_be_told_apart_by_their_digest(client):
    """The comparison this issue exists for, as a test rather than as an argument.

    Two rounds of one cycle, produced by two different harnesses — which is what
    happened on 2026-08-31 when the deployed panel was rebuilt between two runs of
    one session. Before this column the pair was indistinguishable: same repo, same
    PR, same cycle, consecutive rounds, and nothing on either row naming the code
    that read them.

    Asserted off the LIST view on purpose. A recalibration reads a population and
    groups it; a check that fetched each round would prove the value is stored
    without proving it can be grouped on.
    """
    other = "loops-sha256-1:" + "4" * 64
    await record(client, 2, round=1, cycle="c112", harness_digest=DIGEST)
    await record(client, 2, round=2, cycle="c112", harness_digest=other)
    cycle = [r for r in await runs(client) if r["pr"] == 2]
    assert len(cycle) == 2
    assert {r["harness_digest"] for r in cycle} == {DIGEST, other}


async def test_the_three_grouping_fields_ride_the_run_list(client):
    """A recalibration reads a POPULATION, and these are what it slices it on.

    The rule `merge_base`, `base_sha` and #647's three scalars already state: a
    scalar whose whole point is cross-run comparison must not cost one fetch per
    run. #637 reads thousands of rounds.
    """
    await record(client, 3)
    row = next(r for r in await runs(client) if r["pr"] == 3)
    assert row["harness_rev"] == REV
    assert row["harness_dirty"] is False
    assert row["harness_digest"] == DIGEST


async def test_the_locator_is_not_carried_on_the_run_list(client):
    """Detail only, and the cut is between a GROUPING key and a LOCATOR.

    On a nix install the path changes exactly when the digest does, so it buys a
    population nothing the digest has not already bought. What it is for is a
    reader who has picked one round and wants to go and look at the machinery —
    a one-run question, on `unread_files`' and `rules`' rule.
    """
    await record(client, 4)
    listed = await runs(client)
    assert listed and all("harness_path" not in r for r in listed)


async def test_an_installed_harness_records_a_digest_and_no_rev(client):
    """The COMMON case, pinned so it cannot be mistaken for a failure.

    The nix store is not a checkout, so an installed harness has no rev to report
    and no cleanliness to report either. A reader must be able to tell that round
    from one whose rev was garbled and refused, and from one recorded before these
    columns — hence a digest and a path beside two nulls, rather than four nulls.
    """
    posted = await record(client, 5, harness_rev=None, harness_dirty=None,
                          harness_path=STORE)
    assert "harness_rev_dropped" not in posted
    run = await detail(client, posted["id"])
    assert run["harness_rev"] is None and run["harness_dirty"] is None
    assert run["harness_digest"] == DIGEST
    assert run["harness_path"] == STORE


async def test_an_absent_identity_is_null_on_every_field(client):
    """A payload that predates the field says nothing, and says it four times.

    NULL is "the panel did not say" here, which is every round in this table
    before the column and every round from a producer too old to send it. It is
    never a claim that the round came from nowhere.
    """
    missing = payload(6)
    for key in ("harness_rev", "harness_dirty", "harness_digest", "harness_path"):
        del missing[key]
    r = await client.post("/review", json=missing, headers=AGENT)
    assert r.status_code == 201, r.text
    run = await detail(client, r.json()["id"])
    assert run["harness_rev"] is None and run["harness_dirty"] is None
    assert run["harness_digest"] is None and run["harness_path"] is None


async def test_a_dirty_harness_is_kept_apart_from_a_clean_one_and_from_neither(client):
    """Three states, and the middle one is the reason this field exists.

    `panel-review-pr.md` tells you to run the panel from a scratchpad copy, so a
    dirty harness is the normal case for anybody developing it — this issue was
    found from one. A `true` here is what stops the rev beside it being read as
    the whole truth, and the NULL is "no rev, or nobody could ask", which a silent
    `false` would have swallowed.
    """
    dirty = await detail(client, (await record(client, 7, harness_dirty=True))["id"])
    assert dirty["harness_dirty"] is True
    clean = await detail(client, (await record(client, 8, harness_dirty=False))["id"])
    assert clean["harness_dirty"] is False
    unknown = await detail(client, (await record(client, 9, harness_dirty=None))["id"])
    assert unknown["harness_dirty"] is None


async def test_a_garbled_rev_is_dropped_and_named_without_costing_the_rest(client):
    """`harness_rev` goes through `_sha_or_none` with the other commit ids.

    Named on its own rather than under one "a commit id was refused" flag, for
    `base_sha_dropped`'s reason: a producer that sends a good head and a garbled
    harness rev has one bug and needs to be told which field.

    And the round keeps its digest. A refusal that took the whole identity would
    turn one bad field into "this round came from an unknown harness", which is
    the answer this issue exists to stop the board giving.
    """
    posted = await record(client, 10, harness_rev="HEAD~1")
    assert posted["harness_rev_dropped"] == "HEAD~1"
    run = await detail(client, posted["id"])
    assert run["harness_rev"] is None
    assert run["harness_digest"] == DIGEST and run["harness_path"] == CHECKOUT


async def test_the_rev_is_normalised_like_every_other_commit_id(client):
    """One rule for what a commit id IS, across every field on this model.

    Not because a range is assembled from this one — it names a commit in the
    HARNESS's repository, not the reviewed one — but because two spellings of the
    same forty hex digits in one table make a `GROUP BY harness_rev` report two
    harnesses where one ran.
    """
    posted = await record(client, 11, harness_rev="  " + REV.upper() + "  ")
    assert (await detail(client, posted["id"]))["harness_rev"] == REV
    assert "harness_rev_dropped" not in posted


async def test_a_dirty_flag_that_is_not_a_boolean_is_null_and_says_so(client):
    """A wrong-shaped flag costs the flag and nothing else — never a 422.

    This module's standing rule is that recording is best-effort: a payload is
    never refused over one field, because refusing it loses the findings, the
    scorecards and the accounts along with the bad value. A bare `bool | None`
    would have made `harness_dirty: "yes"` a 422 and this round's whole record
    would be gone.

    `1` is refused too. JSON has real booleans, so an integer here is a producer
    sending the wrong shape, and coercing it would be this board guessing about
    the one distinction the field carries.
    """
    posted = await record(client, 12, harness_dirty="yes")
    assert "harness_dirty" in posted["unreadable_fields"]
    run = await detail(client, posted["id"])
    assert run["harness_dirty"] is None
    assert run["harness_digest"] == DIGEST
    numeric = await record(client, 13, harness_dirty=1)
    assert "harness_dirty" in numeric["unreadable_fields"]
    assert (await detail(client, numeric["id"]))["harness_dirty"] is None


async def test_an_oversized_digest_or_path_is_refused_whole_and_named(client):
    """Refused, not truncated, on this module's standing rule.

    Trimming is right for `changed_files`, where a shorter list is still a true
    list. It is wrong for both of these: a truncated digest is a token that
    matches nothing, and a truncated store path names a directory that does not
    exist. Both are named under `unreadable_fields` and share one signal, because
    a one-token field has one remedy however it was got wrong.
    """
    posted = await record(client, 14,
                          harness_digest="x" * (reviews.MAX_HARNESS_DIGEST_CHARS + 1),
                          harness_path="/" + "p" * reviews.MAX_HARNESS_PATH_CHARS)
    assert "harness_digest" in posted["unreadable_fields"]
    assert "harness_path" in posted["unreadable_fields"]
    run = await detail(client, posted["id"])
    assert run["harness_digest"] is None and run["harness_path"] is None
    assert run["harness_rev"] == REV


async def test_a_blank_path_is_not_stored_as_a_path(client):
    """`harness_path: "  "` is a `str` and would otherwise round-trip.

    Only the strip catches it, and a stored `"  "` would sit in a reader's hands
    as a directory to go and look at. Same rule as `scope` one file over.
    """
    posted = await record(client, 15, harness_path="   ")
    assert "harness_path" in posted["unreadable_fields"]
    assert (await detail(client, posted["id"]))["harness_path"] is None


async def test_a_digest_scheme_this_board_has_never_heard_of_is_stored_verbatim(client):
    """Opaque on purpose, and the scheme tag is what makes that safe.

    This board does not know how a digest is computed and must not learn: a
    second implementation of "which harness is this" is the drift #305 exists to
    end, and a frozen set of schemes written here would drop the next one on the
    release that introduced it — #647 argues the same case for
    `fix_range_source`. The tag rides on the VALUE instead, so a consumer
    grouping on the whole string can never compare two schemes by accident.
    """
    grown = "loops-blake3-2:" + "a" * 64
    run = await detail(client, (await record(client, 16, harness_digest=grown))["id"])
    assert run["harness_digest"] == grown


async def test_a_nul_in_a_harness_path_is_refused_at_ingest(client):
    """The 500 these columns would otherwise have introduced.

    Postgres cannot store `\\u0000` in a `text` column any more than in a JSONB
    string, and Python's `str` carries one happily — so without a check the value
    passes every bound in the module and the refusal happens at INSERT, as a 500
    on a panel round that had done nothing wrong. That is the opposite of "a
    dropped field says so", and it is the same class #643 and #647 both found on
    the JSONB side.

    **The refusal moved in #646 and the answer did not.** #112 caught this inside
    `_word_or_none`; #646 found the same 500 in thirty places and moved the check
    to one whole-body pass that runs before any coercer here, so the per-field test
    became dead code and was removed. The value still lands on NULL, for the reason
    this file gives — `harness_path` is a LOCATOR, and a marked one names a
    directory that does not exist — and the drop is now reported under
    `nul_dropped`, which says WHY, rather than under `unreadable_fields`, which can
    only say the value was not the shape this field takes. It was.
    """
    posted = await record(client, 17, harness_path=CHECKOUT + "\x00")
    assert posted["nul_dropped"] == ["harness_path"]
    assert "harness_path" not in posted.get("unreadable_fields", [])
    assert (await detail(client, posted["id"]))["harness_path"] is None


async def test_a_nul_in_a_scope_is_marked_rather_than_nulled(client):
    """...and the same hole in #647's two words, which #646 answers differently.

    `scope` and `fix_range_source` have gone through `_word_or_none` since #647
    and it checked shape, blankness and length — not this. `scope: "pr\\u0000"` was
    therefore a 500 at INSERT on a round with one bad character in it. #112 found
    that and refused the value; the test lives here because this is the change that
    made it.

    **#646 keeps the value and marks it, and the argument is `_word_or_none`'s
    own.** These two are read against no vocabulary on purpose — that docstring
    says a frozen set here would have dropped `reconstructed` on the release that
    introduced it — and its conclusion is that a value outside the set reclassifies
    nothing, because a consumer grouping a population by this field gets an extra
    group it can SEE rather than one folded into a group it cannot. NULL is that
    fold: it is what every round recorded before #647 carries, so refusing the word
    makes a corrupted round indistinguishable from an old one. `increment␦` is
    visibly not a scope, is reported under `nul_replaced`, and cannot be mistaken
    for either.

    `harness_path` one test up goes the other way, which is the distinction #646
    draws: that field is MATCHED, and a marked token answers its comparison wrongly
    forever, where an unmatched grouping word answers nothing at all.
    """
    posted = await record(client, 18, scope="increment\x00", fix_range_source="compare")
    assert posted["nul_replaced"] == ["scope"]
    assert "scope" not in posted.get("unreadable_fields", [])
    run = await detail(client, posted["id"])
    assert run["scope"] == "increment\u2426"
    assert run["fix_range_source"] == "compare"


async def test_a_stored_identity_is_not_reported_as_dropped(client):
    """The negative half: no drop signal fires on an ordinary round.

    A `harness_rev_dropped` that were always set would make every assertion above
    pass while telling every real sender its harness record had been refused.
    """
    posted = await record(client, 19)
    assert "harness_rev_dropped" not in posted
    assert not posted.get("unreadable_fields")


def test_no_harness_field_is_deferred():
    """The three grouping fields ride `_run_view`, so none of them may be deferred.

    Under async SQLAlchemy a deferred column read in a list view raises
    `MissingGreenlet` rather than quietly issuing a second query, so getting this
    backwards breaks `GET /reviews` outright. `harness_path` is not deferred
    either, and deliberately: it is bounded at 512 characters, which is two orders
    below the column `rules` was deferred for, and the detail view reads it.
    """
    from app.models.review import ReviewRun

    mapper = ReviewRun.__mapper__
    for name in ("harness_rev", "harness_dirty", "harness_digest", "harness_path"):
        assert not mapper.attrs[name].deferred, name

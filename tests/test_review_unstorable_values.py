"""#646: a NUL or a NaN anywhere in a round payload, and the round is still recorded.

Postgres will not hold a NUL in a ``text`` column (``invalid byte sequence for
encoding "UTF8": 0x00``) nor inside a ``JSONB`` string (``unsupported Unicode
escape sequence``), and it will not hold ``NaN``/``Infinity`` — which Python's
``json.loads`` accepts as non-standard literals and hands straight through. So a
``POST /review`` carrying one passed every validator in ``app/api/reviews.py``,
reached the INSERT, and **500ed**. The panel reads an empty stdout under exit 0 as
"the board did not answer", so the round simply was not recorded and from the
panel's side that is indistinguishable from a board that was down — which is the
one failure a cycle's own record cannot survive, because every stop decision and
every provenance figure reads it.

#643 fixed one field (``review_panel``) and #647 fixed two more, each in its own
validator. The probe behind this issue found the same 500 in **twenty-eight
other places**: every free-text field, both path lists, the reviewer mapping's
KEYS, the finding titles, the per-reviewer accounts. This file is the class.

What it pins is the answer to the question the issue was filed to settle — for
each kind of caller-supplied value, strip, refuse, or 422 — and the answer is
three different ones, decided on consequence:

* a **path** is dropped, because a marked path is not a shortened path but a
  different file, and it would sit in the column matching nothing;
* an **opaque policy record** keeps #643/#647's whole-object refusal, which the
  whole-body pass must therefore not reach — the regression guard below;
* **everything else** keeps its value with the NUL marked ``␦``, because the
  alternative for a reviewer's account or a stop's veto is to store nothing;
* and **nothing is ever a 422**, because a 422 loses the round, the findings, the
  scorecards and the accounts over one byte, which is the loss this issue is about.

Every one of them is reported. A marked value that read like the value the sender
sent would be the same silence #93, #626 and #643 were filed over.
"""

from __future__ import annotations

import copy

import pytest

# The module, not the names off it: a `from ... import` of a constant that arrives
# with this feature turns the red half of every test in this file into a collection
# error, which demonstrates nothing about the behaviour they pin. `reviews.` is only
# ever dereferenced INSIDE a test, for the same reason.
from app.api import reviews

from .conftest import LAPTOP

REPO = "acme/unstorable"
AGENT = {**LAPTOP, "X-Agent-Instance": "u646a1"}

#: A NUL with a character either side, so a test that passed by trimming the ends
#: would still fail.
NUL = "a\x00b"
#: What a NUL becomes. Written out rather than read off `reviews.UNPRINTABLE`,
#: because that constant arrives with this feature and a module-level reference to
#: it turns the red half of every test in this file into a collection error, which
#: demonstrates nothing about the behaviour they pin. The mark is part of the
#: contract anyway: a consumer reading a stored `detail` sees this character.
MARK = "\u2426"
MARKED = f"a{MARK}b"


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
        "round_stop": {"stop": True, "reason": "no new findings",
                       "confident": True, "veto": []},
        "code_access": {"setting": True, "convention_files_removed": []},
        "changed_files": [{"path": "app/api/reviews.py", "additions": 3}],
        "to_fix": [{"severity": "P2", "file": "app/api/reviews.py", "line": 3,
                    "title": "a thing", "detail": "the detail",
                    "reason": "the judge's rationale", "reviewers": ["claude"],
                    "reported_by": [{"reviewer": "claude", "severity": "P2",
                                     "account": "what claude said"}]}],
    }
    return {**body, **over}


def at(body: dict, path: tuple, value: object) -> dict:
    """Set one nested position, so a case can name where it put the NUL."""
    node = body
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return body


async def post(client, pr: int, path: tuple, value: object, **over) -> dict:
    body = at(copy.deepcopy(payload(pr, **over)), path, value)
    r = await client.post("/review", json=body, headers=AGENT)
    assert r.status_code == 201, r.text
    return r.json()


async def detail(client, run_id: int) -> dict:
    r = await client.get(f"/review/{run_id}", headers=AGENT)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# The two the issue names, each with the driver error it used to raise.
# ---------------------------------------------------------------------------


async def test_a_nul_in_a_stop_veto_records_the_round(client):
    """``{"round_stop": {"veto": ["a\\x00b"]}}`` — one of the issue's two probes.

    ``stop_veto`` is ``JSONB``, so this raised
    ``asyncpg.exceptions.UntranslatableCharacterError: unsupported Unicode escape
    sequence`` at the INSERT, with ``DETAIL: \\u0000 cannot be converted to text``.
    ``StopIn._veto`` runs the value through ``_phrases``, which bounds it and drops
    non-strings and never looked at a control character.

    The veto is kept and marked rather than dropped: it is the panel's own reason
    for refusing to call a round convergent, and a round that stopped with its
    veto silently emptied reads as a clean finish.
    """
    posted = await post(client, 1, ("round_stop", "veto"), [NUL])
    assert posted["nul_replaced"] == ["round_stop.veto[0]"]
    assert (await detail(client, posted["id"]))["stop_veto"] == [MARKED]


async def test_a_nul_in_an_unread_path_records_the_round(client):
    """``{"unread_files": ["app/a\\x00b.py"]}`` — the issue's other probe.

    Same ``UntranslatableCharacterError`` from the same ``JSONB`` INSERT.
    ``_unread_paths`` bounds and filters the list and never looked at a NUL.

    Dropped rather than marked, and counted where a path that was not a path is
    already counted. ``unread_files`` is matched against the next round's diff by
    exact path to fill the ``missed-unread`` bucket, so ``app/a␦b.py`` would be a
    file that never matches — the same reason `_unread_paths` drops an over-long
    path instead of truncating it.
    """
    posted = await post(client, 2, ("unread_files",), ["app/a\x00b.py"])
    assert posted["nul_paths_dropped"] == ["unread_files[0]"]
    # The arithmetic still closes: one entry arrived and one was unusable.
    assert posted["unread_files_dropped"] == {"over_cap": 0, "unusable": 1}
    assert (await detail(client, posted["id"]))["unread_files"] is None


# ---------------------------------------------------------------------------
# The class. Every position the probe found, and what happens to each.
# ---------------------------------------------------------------------------

#: ``(where the NUL goes, the position the response must name)``. Marked, not
#: dropped: prose, identities, vocabulary words and mapping keys all keep their
#: value with the byte replaced.
MARKED_POSITIONS = [
    (("pr_title",), "pr_title"),
    (("base",), "base"),
    (("skip_reason",), "skip_reason"),
    (("stop_reason",), "stop_reason"),
    (("cycle",), "cycle"),
    (("session",), "session"),
    (("run_key",), "run_key"),
    (("judge_model",), "judge_model"),
    (("judge_skip",), "judge_skip"),
    (("coverage_note",), "coverage_note"),
    (("sonar_gate",), "sonar_gate"),
    (("ci_status",), "ci_status"),
    (("reviewers_override",), "reviewers_override"),
    (("scope",), "scope"),
    (("fix_range_source",), "fix_range_source"),
    (("head_sha",), "head_sha"),
    (("round_stop", "reason"), "round_stop.reason"),
    (("reviewers", "claude", "model"), "reviewers.claude.model"),
    (("reviewers", "claude", "effort"), "reviewers.claude.effort"),
    (("reviewers", "claude", "skip"), "reviewers.claude.skip"),
    (("to_fix", 0, "title"), "to_fix[0].title"),
    (("to_fix", 0, "detail"), "to_fix[0].detail"),
    (("to_fix", 0, "reason"), "to_fix[0].reason"),
    (("to_fix", 0, "severity"), "to_fix[0].severity"),
    (("to_fix", 0, "key"), "to_fix[0].key"),
    (("to_fix", 0, "recurs_of"), "to_fix[0].recurs_of"),
    (("to_fix", 0, "needs_human_class"), "to_fix[0].needs_human_class"),
    (("to_fix", 0, "needs_human_reason"), "to_fix[0].needs_human_reason"),
    (("to_fix", 0, "reported_by", 0, "reviewer"), "to_fix[0].reported_by[0].reviewer"),
    (("to_fix", 0, "reported_by", 0, "account"), "to_fix[0].reported_by[0].account"),
    (("to_fix", 0, "reported_by", 0, "severity"), "to_fix[0].reported_by[0].severity"),
]

#: The same, for a NUL inside a LIST entry rather than a scalar.
MARKED_ENTRIES = [
    (("reviewers_selected",), "reviewers_selected[0]"),
    (("skipped",), "skipped[0]"),
    (("round_stop", "veto"), "round_stop.veto[0]"),
    (("to_fix", 0, "reviewers"), "to_fix[0].reviewers[0]"),
    (("to_fix", 0, "related"), "to_fix[0].related[0]"),
    (("to_fix", 0, "rereview_by"), "to_fix[0].rereview_by[0]"),
    (("to_fix", 0, "needs_human_by"), "to_fix[0].needs_human_by[0]"),
    (("reviewers", "claude", "could_not_assess"), "reviewers.claude.could_not_assess[0]"),
]

#: Paths. Dropped rather than marked — see :data:`app.api.reviews._PATH_KEYS`.
PATH_POSITIONS = [
    (("unread_files",), [NUL], "unread_files[0]"),
    (("code_access", "convention_files_removed"), [NUL],
     "code_access.convention_files_removed[0]"),
    (("changed_files", 0, "path"), NUL, "changed_files[0].path"),
    (("to_fix", 0, "file"), NUL, "to_fix[0].file"),
]


@pytest.mark.parametrize("path,named", MARKED_POSITIONS,
                         ids=[n for _, n in MARKED_POSITIONS])
async def test_a_nul_in_any_caller_supplied_value_is_marked_and_named(
        client, path, named):
    """Every one of these was its own 500, and none of them was in the issue.

    Parametrised rather than written out because the point is the CLASS: a fix
    that handled the two fields the issue names would leave twenty-eight others,
    which is precisely how #643 and #647 came to fix one field each.
    """
    posted = await post(client, 100 + MARKED_POSITIONS.index((path, named)),
                        path, NUL)
    assert named in posted["nul_replaced"]


@pytest.mark.parametrize("path,named", MARKED_ENTRIES,
                         ids=[n for _, n in MARKED_ENTRIES])
async def test_a_nul_in_a_list_entry_is_marked_and_its_index_named(
        client, path, named):
    """The index is in the position, not just the field.

    A payload whose ``reviewers_selected`` has forty entries and one bad byte
    should not send its author reading all forty.
    """
    posted = await post(client, 200 + MARKED_ENTRIES.index((path, named)),
                        path, [NUL])
    assert named in posted["nul_replaced"]


@pytest.mark.parametrize("path,value,named", PATH_POSITIONS,
                         ids=[n for _, _, n in PATH_POSITIONS])
async def test_a_nul_in_a_path_drops_that_path_and_says_so(
        client, path, value, named):
    """The one class that is dropped rather than marked, and why it is separate.

    ``app/a␦b.py`` is not ``app/ab.py`` and it is not the file the sender meant.
    Every one of these four is matched by exact string somewhere — the unread
    list against the next round's diff, ``changed_files`` against another PR's in
    ``GET /review/collisions``, a finding's ``file`` in ``_derive_key`` — so a
    marked path is a value that looks like data and matches nothing forever.
    """
    posted = await post(client, 300 + [n for _, _, n in PATH_POSITIONS].index(named),
                        path, value)
    assert named in posted["nul_paths_dropped"]
    assert named not in posted.get("nul_replaced", [])


async def test_a_nul_in_a_reviewer_key_is_marked(client):
    """A mapping KEY reaches a column too.

    ``reviewers`` is keyed by vendor name and that name is ``review_reviewers.name``,
    which is ``NOT NULL`` ``Text`` — so ``{"cla\\x00ude": {...}}`` was the same
    ``CharacterNotInRepertoireError`` as any value. Only a walk reaches a key,
    which is the lesson ``_has_nul`` learned in #647 for the opaque blocks; this is
    the writing half of it.
    """
    posted = await post(client, 400, ("reviewers",),
                        {"cla\x00ude": {"model": "sonnet", "ran": True}})
    assert posted["nul_replaced"] == [f"reviewers.cla{MARK}ude"]
    run = await detail(client, posted["id"])
    # Beside the seat `reviewers_selected` names — the marked key is a seat of its
    # own, stored, rather than a row the INSERT could not write.
    assert f"cla{MARK}ude" in [c["name"] for c in run["reviewers"]]


# ---------------------------------------------------------------------------
# What the sender can read back — the half that makes this not a silent edit.
# ---------------------------------------------------------------------------


async def test_the_marked_value_is_stored_marked_and_not_stripped(client):
    """Marked, not stripped, and the difference is the whole argument.

    A stripped ``detail`` is prose a reader cannot tell from prose the reviewer
    wrote; a marked one says a character arrived that could not be shown. This is
    ``_echo``'s rule (``␦`` rather than deletion) applied to values on their way
    IN to a column, which the issue notes it never was.
    """
    posted = await post(client, 500, ("to_fix", 0, "detail"), f"before{NUL}after")
    run = await detail(client, posted["id"])
    assert run["findings"][0]["detail"] == f"beforea{MARK}bafter"


async def test_an_account_survives_the_round_rather_than_being_dropped(client):
    """The consequence test for the strip-vs-refuse decision on free text.

    Refusing the field would have stored NULL here, which is "this reviewer wrote
    no account" — a false statement about a reviewer that wrote one. One marked
    character is a smaller lie than a missing paragraph, and it is a visible one.
    """
    posted = await post(client, 501, ("to_fix", 0, "reported_by", 0, "account"),
                        f"it {NUL} matters")
    run = await detail(client, posted["id"])
    assert run["findings"][0]["reported_by"][0]["account"] == f"it {MARKED} matters"


async def test_an_ordinary_payload_reports_nothing(client):
    """The negative half. A signal that always fires tells every honest sender
    its payload was edited, which would make the three keys worthless."""
    r = await client.post("/review", json=payload(502), headers=AGENT)
    assert r.status_code == 201, r.text
    body = r.json()
    assert not any(k in body for k in
                   ("nul_replaced", "nul_paths_dropped", "nonfinite_dropped"))


async def test_a_sender_cannot_write_its_own_account_of_what_was_marked(client):
    """Evidence the sender can write is not evidence.

    The three signals are set unconditionally by the model validator, exactly as
    ``provenance_sent`` and ``changed_files_sent`` are, so a caller spelling one
    itself has it overwritten rather than merged.
    """
    posted = await post(client, 503, ("pr_title",), NUL,
                        nul_replaced=["nothing happened"],
                        nul_paths_dropped=["nor here"],
                        nonfinite_dropped=["nor here either"])
    assert posted["nul_replaced"] == ["pr_title"]
    assert "nul_paths_dropped" not in posted
    assert "nonfinite_dropped" not in posted


async def test_a_payload_wrong_in_hundreds_of_places_names_a_bounded_few(client):
    """Bounded like every other echo on this path, with the count beside it.

    A body can be wrong in five hundred positions and naming all of them costs
    one enormous log line to say what the first twenty-five already said.
    """
    posted = await post(client, 504, ("reviewers_selected",),
                        [f"seat{n}{NUL}" for n in range(reviews.MAX_UNKNOWN_BUCKETS + 7)])
    assert len(posted["nul_replaced"]) == reviews.MAX_UNKNOWN_BUCKETS
    assert posted["nul_replaced_total"] == reviews.MAX_UNKNOWN_BUCKETS + 7


# ---------------------------------------------------------------------------
# NaN and the infinities — the other half of the issue, and a 500 that never
# reached the database at all.
# ---------------------------------------------------------------------------

#: Every number the probe reached with a non-standard JSON literal. The literals
#: have to be written into a raw body: `json.dumps` refuses to emit them and
#: `httpx`'s `json=` goes through it.
NONFINITE_POSITIONS = [
    ('"changed_lines": NaN', "changed_lines"),
    ('"changed_lines": Infinity', "changed_lines"),
    ('"changed_lines": -Infinity', "changed_lines"),
    ('"diff_chars": NaN', "diff_chars"),
    ('"new_findings": Infinity', "new_findings"),
    ('"round": Infinity', "round"),
    ('"changed_files_total": Infinity', "changed_files_total"),
    ('"to_fix": [{"title": "t", "line": NaN}]', "to_fix[0].line"),
    ('"reviewers": {"claude": {"duration_ms": Infinity}}',
     "reviewers.claude.duration_ms"),
    ('"reviewers": {"claude": {"input_tokens": NaN}}',
     "reviewers.claude.input_tokens"),
    ('"reviewers": {"claude": {"cost_usd": NaN}}', "reviewers.claude.cost_usd"),
    ('"reviewers": {"claude": {"max_diff_chars": Infinity}}',
     "reviewers.claude.max_diff_chars"),
    ('"changed_files": [{"path": "a.py", "additions": Infinity}]',
     "changed_files[0].additions"),
]


@pytest.mark.parametrize("fragment,named", NONFINITE_POSITIONS,
                         ids=[f"{n}-{f.split(':')[-1].strip()}"
                              for f, n in NONFINITE_POSITIONS])
async def test_a_non_finite_number_becomes_nobody_said_and_is_named(
        client, fragment, named):
    """Two different 500s, and only one of them was at the INSERT.

    ``diff_chars`` and ``changed_lines`` are plain ``int`` fields with no tolerant
    coercer, so a non-finite one **fails validation** — and FastAPI renders that
    422 by quoting the offending input back into JSON, which cannot represent a
    ``nan``. The refusal itself then raised ``ValueError: Out of range float values
    are not JSON compliant: nan`` and the request 500ed without ever reaching the
    database. Dropping the value before binding is what turns both mechanisms into
    a recorded round with a note.
    """
    pr = 600 + NONFINITE_POSITIONS.index((fragment, named))
    raw = f'{{"repo": "{REPO}", "pr": {pr}, "reviewed": true, {fragment}}}'
    r = await client.post("/review", content=raw.encode(),
                          headers={**AGENT, "content-type": "application/json"})
    assert r.status_code == 201, r.text
    assert named in r.json()["nonfinite_dropped"]


async def test_a_body_that_is_not_an_object_is_refused_rather_than_500ing(client):
    """The corner where there is nothing to record and a 422 is the right answer.

    A bare array cannot bind to ``ReviewIn``, so FastAPI 422s it — and quotes the
    input back, so ``[NaN]`` made the refusal unserialisable. Walked anyway, for
    that reason alone.
    """
    r = await client.post("/review", content=b"[NaN]",
                          headers={**AGENT, "content-type": "application/json"})
    assert r.status_code == 422, r.text


async def test_a_repo_that_is_not_a_repo_is_still_refused(client):
    """The one field where a 422 stays right, stated so nobody quietly relaxes it.

    ``repo`` is the row's identity: every read compares it with ``==`` and #326
    put a CHECK on the column. A marked spelling would be a second repository
    nothing ever asks about, so ``REPO_RE`` refuses it — which it already did,
    because a NUL is not in the character class.
    """
    r = await client.post("/review", json=payload(700, repo=f"acme/un{NUL}storable"),
                          headers=AGENT)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# The regression guard: the whole-body pass must NOT reach the opaque blocks.
# ---------------------------------------------------------------------------

OPAQUE = [
    ("review_panel", {"fix_severity_floor": f"P2{NUL}"}, "review_panel_dropped"),
    ("rules", {"dials": {"review_panel.max_rounds": {"layer": f"board{NUL}"}}},
     "rules_dropped"),
    ("provenance_restored", {"count": 4, "why": f"no checkout{NUL}"},
     "provenance_restored_dropped"),
]


@pytest.mark.parametrize("field,value,signal", OPAQUE, ids=[f for f, _, _ in OPAQUE])
async def test_a_policy_record_is_still_refused_whole_and_never_marked(
        client, field, value, signal):
    """#643 and #647's decision, unchanged, and this is what keeps it that way.

    These three are stored verbatim and never interpreted, and ``_opaque_or_none``
    argues that half a dial set is not a smaller policy but one no round ran under.
    A whole-body normalisation that reached inside one would store a policy
    differing by a character from the policy that ran, with the row claiming it was
    intact — which is worse than either the 500 or the refusal. So the walk skips
    them and they keep their own signal.
    """
    posted = await post(client, 800 + [f for f, _, _ in OPAQUE].index(field),
                        (field,), value)
    assert "NUL" in posted[signal]
    assert not posted.get("nul_replaced")
    assert (await detail(client, posted["id"]))[field] is None


async def test_a_nan_inside_a_policy_record_is_still_refused_whole(client):
    """The same guard for the other literal.

    ``_opaque_or_none`` finds it with ``json.dumps(..., allow_nan=False)``, and the
    body-wide pass must not have turned it into a ``null`` dial first — a policy
    record with one dial silently nulled is exactly the fiction that refusal exists
    to prevent.
    """
    raw = (f'{{"repo": "{REPO}", "pr": 810, "reviewed": true, '
           f'"review_panel": {{"max_fix_growth": NaN}}}}')
    r = await client.post("/review", content=raw.encode(),
                          headers={**AGENT, "content-type": "application/json"})
    assert r.status_code == 201, r.text
    assert "NaN" in r.json()["review_panel_dropped"]
    assert not r.json().get("nonfinite_dropped")
    assert (await detail(client, r.json()["id"]))["review_panel"] is None


async def test_two_keys_that_marking_makes_one_leave_the_last(client):
    """The corner marking creates, stated rather than discovered later.

    ``{"cla\\x00ude": …, "cla␦ude": …}`` is one key after the mark. Last one wins,
    which is what a dict comprehension over the same coercion does everywhere else
    in this module — ``_prov_counts`` settles the identical question for
    ``Introduced`` beside ``introduced``, on the grounds that inventing a merge
    rule would be composing a value nobody stated. Nothing real sends this shape,
    and the mark is reported either way, so no sender is told its payload arrived
    intact.
    """
    posted = await post(client, 900, ("reviewers",), {
        "cla\x00ude": {"model": "first", "ran": True},
        f"cla{MARK}ude": {"model": "second", "ran": True}})
    assert posted["nul_replaced"] == [f"reviewers.cla{MARK}ude"]
    run = await detail(client, posted["id"])
    seat = next(c for c in run["reviewers"] if c["name"] == f"cla{MARK}ude")
    assert seat["model"] == "second"

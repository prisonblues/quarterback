"""#508: carrying what was confirmed next door into the round that has not seen it.

`finding_recurrence` chains a finding to earlier rounds of ITS OWN pull request.
The defect this fleet ships is one file over and an hour later: a P1 confirmed in
``app.auth.delegated()`` on one PR, the identical shape in ``app.auth.human()`` on
another, copied from the same source function, and the round that missed it was a
round 1 with nothing of its own to recur against.

The board side of that is `GET /review/next-door` (``tests/test_next_door.py``).
This is the harness side, and it is where the property that makes the whole thing
safe has to live, because **the board cannot enforce it**: a hint must not be able
to become a finding on its own. All the board can do is decline to publish
anything shaped like a verdict; what stops a seat reporting a listed defect it
never found is the paragraph in front of the list.

So this file pins four things, and three of them are about what does NOT happen:

* **the OFF path** — `next_door_days: 0` makes no board call at all and leaves the
  reviewer prompt byte-identical to its pre-#508 self. The same holds with the dial
  on and nothing next door, which is the common case: a round with no hints must
  not differ from an archived round, or comparing two rounds is also comparing two
  prompts;
* **the BRACE trap** — the block is built from model-authored finding titles and
  the prompt is rendered with `.format()`. Substituted before the render, one
  reviewer who ever wrote `{` into a title raises `KeyError` on an unrelated round
  months later. The swap therefore happens AFTER the render, the way
  `panel_rounds` swaps `JUDGE_CODE_SLOT`;
* **the CONTRACT** — the rendered block says, in terms, that nobody has looked for
  these here and that a seat must find one itself before reporting it. Asserted on
  the text because the text IS the mechanism;
* **the MANIFEST round** — asks for no hints and is given none. Its whole
  instruction is "do not review the moved code", and a list of defects confirmed
  in those files is an invitation to do exactly that.

A failure that costs the round nothing is a failure that gets REPORTED, never one
that is silently swallowed: unlike `board_escalations`, an unreachable board here
leaves the round exactly as correct as every round before this feature existed, so
the note is for the operator who switched it on and sees nothing.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_seats  # noqa: E402


#: The real fetch, captured at import — BEFORE any fixture has run, so this is the
#: function itself and not the conftest's stub.
#:
#: `conftest._no_next_door_fetch` replaces `panel.board_next_door` for every test in
#: the suite, so that no round makes a live board call. That is right for every
#: other file and wrong for this one, which is the file that tests the function. The
#: fixture below puts it back, and it has to be an explicit restore rather than an
#: opt-out: a test here that silently ran against the stub would assert on `([], "")`
#: and pass no matter what the real function did.
REAL_FETCH = panel.board_next_door


@pytest.fixture(autouse=True)
def _the_real_fetch(monkeypatch):
    """Undo the suite-wide stub for this file only. Module-level autouse fixtures
    run after conftest's, so this wins; no test here reaches a board, because every
    one of them pins `board_request` underneath it."""
    monkeypatch.setattr(panel, "board_next_door", REAL_FETCH)


def hint(**over) -> dict:
    h = {"pr": 493, "pr_title": "the other PR", "run_id": 12,
         "ts": "2026-08-26T07:30:00+00:00", "age_hours": 1.0,
         "file": "app/auth.py", "line": 412, "severity": "P1",
         "title": "dev bypass consulted before the credential check",
         "detail": "on any board with BROWSER_DEV_HUMAN set, a caller with no "
                   "credential authenticated",
         "finding_key": "delegated-order", "outcome": None}
    return {**h, **over}


def rendered(next_door: str, scope: str = "diff") -> str:
    """The reviewer prompt as `panel.prompt_for` builds it — format, then swap.

    The ORDER is the thing under test in half this file, so it is spelled out here
    once rather than approximated per test.
    """
    return (panel_core.reviewer_brief(scope)
            .format(n=1, repo="acme/app", base="main", ci="CI: green",
                    diff="DIFF", code="CODE")
            .replace(panel_core.NEXT_DOOR_SLOT, next_door))


# ---- the off path, and the byte-identical prompt ---------------------------


def answering(monkeypatch, body, err="", code=200, seen=None):
    """Pin what the board says, at the one seam `board_next_door` reads it through."""
    def fake(path, params):
        if seen is not None:
            seen.update({"path": path, **params})
        return body, err, code
    monkeypatch.setattr(panel, "board_request", fake)


def test_the_dial_at_zero_makes_no_board_call_at_all(monkeypatch):
    """`0` is a switch, not a window of zero days. A round told not to look must
    not depend on the board being reachable, or switching the feature off would
    still be able to add a note to somebody's report."""
    called: list = []
    monkeypatch.setattr(panel, "board_request",
                        lambda *a, **k: called.append(a) or ({}, "", 200))
    hints, why = panel.board_next_door("acme/app", 1, 0)
    assert hints == [] and why == ""
    assert called == [], "the dial was off and the board was called anyway"


def test_no_hints_leaves_the_prompt_byte_identical_to_its_pre_508_self():
    """The commonest round has nothing next door, and on that round the prompt
    must be the one this panel has always sent. A block saying "no recent findings
    nearby" would be a new sentence every round in exchange for no information —
    and would make every round's prompt differ from every archived round's.

    Asserted as the ABSENCE of a seam rather than against a stored copy: the
    template around the slot is the part a fill would disturb.
    """
    out = rendered(panel_core.next_door_brief([]))
    assert panel_core.NEXT_DOOR_SLOT not in out
    assert "repository is not.\n\nReview for:\n" in out
    assert "NEXT DOOR" not in out


def test_an_empty_or_junk_hint_list_renders_nothing():
    """The renderer is fed straight off the wire, so "the board answered with a
    list of nulls" has to reach the same place as "the board answered with an
    empty list" — nothing in the prompt, rather than a heading with no rows under
    it."""
    assert panel_core.next_door_brief([]) == ""
    assert panel_core.next_door_brief([None, "not a hint", 7]) == ""


# ---- the brace trap --------------------------------------------------------


def test_a_brace_in_a_finding_title_does_not_blow_up_the_render():
    """**The regression this ordering exists for.**

    The block carries model-authored titles and the prompt is rendered with
    `.format()`. Substituted BEFORE the render, a title containing `{scope}` — or
    any brace at all — is read as a format field and raises `KeyError` on a round
    that has nothing to do with the PR that wrote it. Swapped after, that text is
    never scanned for fields.
    """
    block = panel_core.next_door_brief(
        [hint(title="dict literal {'a': 1} left in the handler",
              detail="the {placeholder} was never filled")])
    out = rendered(block)          # would raise KeyError under the old ordering
    assert "{'a': 1}" in out and "{placeholder}" in out


def test_the_token_survives_the_format_it_is_swapped_after():
    """The swap only works because the token has no braces of its own. If somebody
    ever spells the next slot `{{NEXT_DOOR}}`, `.format` eats it and the fill lands
    nowhere — silently, and only on rounds that had hints."""
    assert "{" not in panel_core.NEXT_DOOR_SLOT
    assert "}" not in panel_core.NEXT_DOOR_SLOT
    assert panel_core.NEXT_DOOR_SLOT in panel_core.REVIEW_PROMPT


# ---- the contract, which is the mechanism ----------------------------------


def test_the_block_tells_the_seat_the_lines_are_not_findings():
    """The one property #508 asks to keep is that a hint cannot become a finding on
    its own, and the board cannot enforce it. This paragraph is the enforcement, so
    it is asserted rather than assumed: without it the recurrence chain eats its own
    tail and a seat is rewarded for repeating what it was told."""
    block = panel_core.next_door_brief([hint()])
    assert panel_core.NEXT_DOOR_HEADING in block
    assert "NOT findings about this diff" in block
    assert "ONLY if you find it yourself" in block
    assert "Nobody has looked for" in block


def test_a_hint_line_carries_the_evidence_to_dismiss_it():
    """A reviewer that cannot check a line will report it to be safe. The PR
    number and the age are what let it decide the line is stale or irrelevant on
    the line's own evidence."""
    line = panel_core._hint_line(hint())
    assert "P1" in line and "app/auth.py:412" in line
    assert "PR #493" in line and "1h ago" in line


def test_only_a_fixed_outcome_is_said_out_loud():
    """`fixed` means somebody confirmed the defect AND acted on it, which is the
    strongest form the hint takes. A bare `deferred` or `superseded` on the line
    would read as a verdict about THIS diff, which is the one thing the block must
    never imply."""
    assert "and fixed there" in panel_core._hint_line(hint(outcome="fixed"))
    for quiet in (None, "deferred", "superseded"):
        assert "fixed" not in panel_core._hint_line(hint(outcome=quiet))


def test_the_block_is_capped_at_a_handful_of_lines():
    """"A handful of lines in a prompt, not a second review." Capped here as well
    as at the board, because the two caps are different promises and a caller that
    trusted only the far one would be trusting a number it does not control."""
    block = panel_core.next_door_brief([hint(pr=n, finding_key=f"k{n}",
                                             title=f"defect {n}", detail="")
                                        for n in range(40)])
    assert block.count("\n- ") == panel_core.NEXT_DOOR_MAX


# ---- the fetch reports rather than swallows --------------------------------


@pytest.mark.parametrize("body,err,expect", [
    (None, "board unreachable at https://qb (URLError)", "board unreachable"),
    (["not", "an", "object"], "", "not an object"),
    ({"hints": "nope"}, "", "not a list"),
    ({}, "", "no `hints`"),
])
def test_a_board_that_cannot_answer_is_reported_and_costs_the_round_nothing(
        monkeypatch, body, err, expect):
    """Absence here is NOT dangerous, unlike `board_escalations` one function up —
    a round with no hints reviews the way every round did before #508. So the
    contract is: no hints, a note saying why, and nothing a stop rule can read."""
    answering(monkeypatch, body, err, code=500 if err else 200)
    hints, why = panel.board_next_door("acme/app", 1, 7)
    assert hints == []
    assert expect in why
    assert panel_core.next_door_brief(hints) == ""


@pytest.mark.parametrize("code", [404, 422])
def test_a_board_older_than_the_feature_is_silent_not_warned_about(monkeypatch, code):
    """**The regression four unrelated e2e tests found first.**

    A board predating this endpoint is the ordinary state of a fleet mid-rollout,
    and a note about it would land on EVERY round of EVERY pull request — which is
    a note that gets trained away, taking the one that fires when something is
    genuinely wrong with it.

    `422` is the code nobody predicts and the one the live board really returned:
    `GET /review/{run_id}` is declared on the same prefix, so the path falls
    through to it and `next-door` fails the `int` validation. It reads like "your
    request was malformed" and means "this board is older than the feature".
    """
    answering(monkeypatch, {"detail": "nope"}, err=f"board answered HTTP {code}",
              code=code)
    hints, why = panel.board_next_door("acme/app", 1, 7)
    assert hints == []
    assert why == "", f"HTTP {code} is a capability answer and must not be reported"


def test_a_real_failure_from_a_board_that_has_the_route_is_still_reported():
    """The silence above is narrow on purpose. A 500 is a board that HAS the
    endpoint and broke, and an operator who switched this on is owed that."""
    assert 500 not in panel.NEXT_DOOR_ABSENT
    assert set(panel.NEXT_DOOR_ABSENT) == {404, 422}


def test_the_fetch_drops_rows_that_are_not_objects(monkeypatch):
    """The wire is not trusted to be well-formed. A list with a null in it must
    not reach the renderer as a hint with no fields."""
    answering(monkeypatch, {"hints": [hint(), None, "x"]})
    hints, why = panel.board_next_door("acme/app", 1, 7)
    assert why == "" and len(hints) == 1


def test_the_fetch_asks_for_no_more_than_it_will_render(monkeypatch):
    """Asking the board for 20 and rendering 8 would spend the wire on rows nobody
    reads, and would make `hints_dropped` a number about the wrong cap."""
    seen: dict = {}
    answering(monkeypatch, {"hints": []}, seen=seen)
    panel.board_next_door("acme/app", 42, 7)
    assert seen["limit"] == panel_core.NEXT_DOOR_MAX
    assert seen["days"] == 7 and seen["pr"] == 42


# ---- the dial --------------------------------------------------------------


def test_the_dial_defaults_on_and_reads_zero_as_off():
    notes: list[str] = []
    assert panel_seats.next_door_days({}, notes) == panel_core.DEFAULT_NEXT_DOOR_DAYS
    assert panel_seats.next_door_days({"next_door_days": 0}, notes) == 0
    assert panel_seats.next_door_days({"next_door_days": "3"}, notes) == 3
    assert notes == []


@pytest.mark.parametrize("bad", [True, False, -1, 1.5, "soon"])
def test_a_value_that_could_mean_two_things_is_refused_not_guessed(bad):
    """`false` is how a hand writes "off" and would read as `0` — right by
    accident. `true` would read as `1` and silently narrow the window to a day.
    A reader that cannot tell those apart gets the second one wrong later."""
    with pytest.raises(SystemExit):
        panel_seats.next_door_days({"next_door_days": bad}, [])


def test_a_window_wider_than_the_board_accepts_is_clamped_and_said_out_loud():
    """The one value here that is clamped rather than refused, and the reason is
    that passing it through breaks the feature silently: the board caps `days` at
    3650, so `5000` would leave, come back **HTTP 422**, and the operator who
    widened the window would get no hints at all. `5000` also means exactly one
    thing — "reach back as far as you can" — so there is nothing to refuse.

    Said out loud because `Dials` records what was APPLIED: a payload reading 3650
    under a rules file reading 5000 is otherwise unexplained.
    """
    notes: list[str] = []
    assert panel_seats.next_door_days({"next_door_days": 5000}, notes) == \
        panel_core.NEXT_DOOR_DAYS_MAX
    assert len(notes) == 1 and "accepts at most" in notes[0]

    quiet: list[str] = []
    assert panel_seats.next_door_days(
        {"next_door_days": panel_core.NEXT_DOOR_DAYS_MAX}, quiet) == \
        panel_core.NEXT_DOOR_DAYS_MAX
    assert quiet == [], "the ceiling itself is not a clamp and must not warn"


def test_the_round_records_which_window_it_applied():
    """The payload records what was APPLIED, not what was written — a report that
    says one thing while the round did another is the failure `Dials` exists to
    make impossible."""
    dials = panel_seats.resolve_dials({"next_door_days": 2}, None, [])
    assert dials.next_door_days == 2
    assert dials.as_dict()["next_door_days"] == 2


# ---- the manifest round ----------------------------------------------------


def test_a_manifest_prompt_carries_no_slot_to_fill():
    """A manifest round is told not to review the moved code. A list of defects
    confirmed in those same files is an invitation to do exactly that, so it gets
    none — and the template has no slot even if a future caller tries."""
    assert panel_core.NEXT_DOOR_SLOT not in panel_core.MOVE_MANIFEST_PROMPT

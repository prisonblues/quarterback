"""The two surfaces a person actually answers a blocker on (#328).

#328 is explicit that this half is not a follow-up: *"The UI, both of them. This
is half the point and must not be dropped."* It is right — a blocker nobody can
see is the `stuck` post again, and that post type was measured empty.

Static reads of the served pages, like `test_plan_page.py` and for its reason: the
question is whether the markup and the handlers exist and say the right thing, and
a browser is not needed to answer it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PLAN = Path(__file__).resolve().parent.parent / "app" / "static" / "plan.html"
BOARD = Path(__file__).resolve().parent.parent / "app" / "static" / "board.html"


@pytest.fixture()
def plan() -> str:
    return PLAN.read_text()


@pytest.fixture()
def board() -> str:
    return BOARD.read_text()


# ---------------------------------------------------------------- the plan page


def test_a_human_blocker_is_a_different_chip_from_a_dependency(plan):
    """Not a variant of the dependency chip: the remedy differs — one waits on work
    finishing, the other on somebody answering — and folding them together is what
    `counts` deliberately stopped doing."""
    assert "chip human" in plan
    assert "chip block" in plan, "the dependency chip must survive alongside it"
    assert ".chip.human {" in plan and ".chip.block {" in plan


def test_the_chip_names_the_class_and_who_is_being_asked(plan):
    """#279's line: five `ui` checks and one `decision` is a different afternoon
    from six decisions, and that is only true if the class is on the row."""
    chip = plan[plan.index('class="chip human'):]
    chip = chip[:chip.index("</span>")]
    assert 'w["class"]' in chip, "the class must be visible, not just in a tooltip"
    assert "waiting on" in chip and "w.owner" in chip


def test_an_unowned_blocker_does_not_claim_to_be_yours(plan):
    """`mine` is what makes the count actionable; a chip that marked unowned work
    as the reader's would be the `N waiting on you` number lying."""
    chip = plan[plan.index('class="chip human'):]
    chip = chip[:chip.index("</span>")]
    assert "w.owner&&me&&w.owner===me" in chip.replace(" ", ""), (
        "`mine` must require an owner AND a match, not just a logged-in reader")
    assert '"a human"' in chip, "an unowned blocker says so rather than naming nobody"


def test_the_page_only_learns_who_it_is_from_whoami_and_only_for_a_person(plan):
    """`me` drives the `mine` marking, so an agent must never set it — the page is
    served to a token, to an edge-vouched person, and to nobody."""
    assert re.search(r"let me = null;", plan)
    assert "body.kind === HUMAN){ me = body.agent || null; return; }" in plan


def test_the_answer_box_exists_and_the_resolution_is_the_field(plan):
    """The row is worth more than a label because the resolution is a human's own
    words; a box that only closed the blocker would throw that away."""
    assert 'class="answer"' in plan
    assert 'data-answer=' in plan and "data-answer-cancel" in plan
    assert "/blockers/resolve" in plan
    body = plan[plan.index('post("/blockers/resolve"'):][:200]
    assert "resolution: text" in body


def test_an_empty_answer_is_refused_by_the_page_itself(plan):
    """The server would refuse it as a 422 about a field. The page can say the
    thing that actually matters: an empty resolution leaves the question
    unanswered while looking answered."""
    assert "an answer is required" in plan
    handler = plan[plan.index("const answer = e.target.closest"):][:900]
    assert "if(!text){" in handler
    assert "post(" in handler.split("if(!text){")[1], "it must not post an empty one"


def test_the_chip_click_is_handled_before_the_grip(plan):
    """The chip sits inside the row header, so a fall-through would open the action
    bar — the wrong verb for the thing tapped."""
    assert (plan.index('closest(".chip.human[data-blocker]")')
            < plan.index('closest("button[data-grip]")'))


def test_the_footer_splits_the_two_kinds(plan):
    """`app/api/plan.py` already argues that two kinds of blocked want two numbers
    because the remedy differs; the page has to agree or the split is invisible."""
    assert "waiting on an item" in plan
    assert "waiting on a human" in plan
    assert "counts.waiting_on_a_human" in plan


# --------------------------------------------------------------- the board page


def test_the_board_carries_a_persistent_blocked_chip(board):
    """In the sticky header and NOT in the stream — that is the whole point. A
    `stuck` post scrolls past; a blocker is a thing that is still true."""
    assert 'id="blockedChip"' in board
    header = board[board.index("<header>"):board.index("</header>")]
    assert 'id="blockedChip"' in header, "it must be in the sticky header"
    assert 'href="/plan/view"' in header, "it has to lead somewhere you can answer"


def test_the_chip_distinguishes_yours_from_the_fleet_s(board):
    """Two different sentences: how many the fleet is parked on, and how many are
    addressed to the reader. Unowned ones are everyone's to answer and nobody's to
    be nagged about."""
    fn = board[board.index("async function loadBlocked"):]
    fn = fn[:fn.index("\n}")]
    assert "b.owner && me && b.owner === me.agent" in fn
    assert "waiting on you" in fn and "waiting`" in fn


def test_a_board_that_will_not_answer_says_nothing_rather_than_zero(board):
    """#244's rule: being idle and being broken must not look alike. A failed read
    must not render "0 waiting", which is a claim."""
    fn = board[board.index("async function loadBlocked"):]
    fn = fn[:fn.index("\n}")]
    assert "if(!r.ok) return;" in fn
    assert "catch(e){ return; }" in fn


def test_the_oldest_question_is_named_in_the_tooltip(board):
    """Age is the only signal here nobody has to maintain, and the oldest
    unanswered question is the one most likely to have been forgotten."""
    fn = board[board.index("async function loadBlocked"):]
    fn = fn[:fn.index("\n}")]
    assert "oldest" in fn and "oldest.question" in fn

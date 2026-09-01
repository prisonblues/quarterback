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


def test_the_chip_leads_somewhere_a_person_can_actually_answer(board):
    """The assertion this replaces was `href="/plan/view"` anywhere in the header,
    and #677 is what it could not see. The chip DID carry that href and the page it
    named still cannot answer every row: a blocker reaches the plan page only as a
    chip on a plan ROW, and `_human_blockers_for` attaches one by matching an OPEN
    item's forge ref — so a blocker nothing plans is counted on the board and absent
    from the page its own tooltip sends you to.

    The intent survives; the letter cannot. The same header carries a plain `plan →`
    nav link with that identical href, so an assertion spelled that way now passes
    on the wrong element whatever the chip does. It is spelled against the chip
    instead, and against the thing that makes it lead somewhere: the list it opens
    is on THIS page."""
    chip = board[board.index('id="blockedChip"'):]
    chip = chip[:chip.index(">") + 1]
    assert "<a " not in board[board.rindex("<", 0, board.index('id="blockedChip"')):
                              board.index('id="blockedChip"')], \
        "a signpost is an anchor; a control that opens the list is a button"
    assert 'aria-controls="blockedPanel"' in chip, "it must name what it expands"
    assert 'aria-expanded=' in chip, "and say whether that thing is open"
    assert 'id="blockedPanel"' in board, "the thing it expands has to exist"


def test_the_board_answer_box_does_not_route_through_the_plan(board):
    """The whole of #677. The board already holds every row from
    `/blockers?open=true`, so the answer box posts straight to `/blockers/resolve`
    with the blocker's own id — no plan item in the path, because the missing plan
    item is the defect and not a step."""
    assert "/blockers/resolve" in board
    assert 'fetch("/blockers/resolve"' in board, "the board never posts a resolution"
    handler = board[board.index('fetch("/blockers/resolve"'):][:400]
    assert "blocker_id: id" in handler
    assert "resolution: text" in handler, (
        "the resolution is the payload — a box that only closed the row would throw "
        "away the human's own words, which is the reason the row beats a label")


def test_an_empty_answer_is_refused_by_the_board_page_itself(board):
    """Mirrors the plan page and for its reason: the server would refuse an empty
    resolution as a 422 about a field, and the page can say the thing that actually
    matters."""
    assert 'const btn = e.target.closest("button[data-answer]")' in board, \
        "there is no answer button on the board to handle"
    handler = board[board.index('const btn = e.target.closest("button[data-answer]")'):]
    handler = handler[:handler.index('\n});')]
    assert "an answer is required" in handler
    assert "if(!text){" in handler
    after = handler.split("if(!text){", 1)[1]
    assert "fetch(" not in after.split("}", 1)[0], "it must not post an empty one"


def test_the_board_answer_box_is_gated_on_a_person(board):
    """`POST /blockers/resolve` decides this and its refusal is the authority — but
    a page that offered the box to a browser it knows cannot write would be
    inviting a refusal, and `loadBlocked` is what stops the list existing at all."""
    fn = board[board.index("async function loadBlocked"):]
    fn = fn[:fn.index("\n}")]
    assert "if(!canWrite()) return;" in fn
    assert 'const btn = e.target.closest("button[data-answer]")' in board, \
        "there is no answer button on the board to handle"
    handler = board[board.index('const btn = e.target.closest("button[data-answer]")'):]
    handler = handler[:handler.index('\n});')]
    assert "if(!canWrite()){" in handler


def test_the_question_is_text_on_the_board_row_not_only_a_tooltip(board):
    """`test_plan_page.py`'s guard makes this argument for the other page — "`title=`
    is a desktop-only answer" — and it holds here: somebody triaging the queue on a
    phone must be able to read what is being asked without opening anything."""
    assert "function blockedRow(b){" in board, "there is no board row to read the question off"
    row = board[board.index("function blockedRow(b){"):]
    row = row[:row.index("\n}")]
    assert 'class="bl-q">${esc(b.question)}' in row
    assert "esc(b.kind)" in row, "and the class, for #279's five-ui-checks line"


def test_the_board_no_longer_sends_a_person_to_the_plan_page_to_answer(board):
    """The tooltip said "Answer on the plan page" for rows that will not be there.
    Whatever else changed, it must stop saying that."""
    fn = board[board.index("async function loadBlocked"):]
    fn = fn[:fn.index("\n}")]
    assert "Answer on the plan page" not in fn
    assert "plan page" not in fn


def test_an_emptied_queue_keeps_the_chip_only_while_the_list_is_open(board):
    """A chip that vanished the moment the last question was answered would leave an
    open panel with nothing that opened it. This zero is not the one #244 forbids —
    that rule is about a FAILED read rendering "0 waiting" as if it knew, and the
    two early returns above it are what keep that."""
    fn = board[board.index("async function loadBlocked"):]
    fn = fn[:fn.index("\n}")]
    assert "blockedChip.hidden = !all.length && !blockedOpen;" in fn
    assert "if(!r.ok) return;" in fn and "catch(e){ return; }" in fn


def test_the_list_keeps_the_server_s_oldest_first_order(board):
    """Age is the only signal in this queue nobody has to maintain. `mine` marks the
    row rather than sorting it to the top — re-sorting would cost the list the one
    property it is ordered for."""
    assert "function renderBlocked(){" in board, "there is no list to render"
    fn = board[board.index("function renderBlocked(){"):]
    fn = fn[:fn.index("\n}")]
    assert ".sort(" not in fn
    assert "oldest first" in fn
    assert "b.owner && me && b.owner === me.agent" in fn


def test_a_re_render_does_not_take_a_half_typed_answer_away(board):
    """The list is re-read after every answer, and somebody working down a queue may
    well have started two. Losing the other one to the refresh is the sort of thing
    that stops a person using a queue at all — which is the failure this whole issue
    is about."""
    assert "function renderBlocked(){" in board, "there is no list to render"
    fn = board[board.index("function renderBlocked(){"):]
    fn = fn[:fn.index("\n}")]
    assert "const typed = new Map();" in fn
    assert fn.index("typed.set(") < fn.index("blockedList.innerHTML ="), \
        "the values have to be read BEFORE the markup that holds them is replaced"
    assert "box.value = v;" in fn


def test_a_slower_earlier_read_cannot_paint_over_a_later_one(board):
    """Answer two rows in quick succession and each starts a read of its own. If the
    first response lands second, the list reverts to the state that still held the
    row just resolved — an open answer box on a question the server has closed, and
    clicking it earns a 409. The panel deliberately does not poll while it is open,
    so nothing would come along and correct it."""
    fn = board[board.index("async function loadBlocked"):]
    fn = fn[:fn.index("\n}")]
    assert "const seq = ++blockedSeq;" in fn
    assert "if(seq !== blockedSeq) return;" in fn
    assert fn.index("await fetch(") < fn.index("if(seq !== blockedSeq) return;") \
        < fn.index("blockedRows = all;"), \
        "the check has to sit after the await and before anything it would render"


def test_the_count_is_re_read_and_never_over_an_open_list(board):
    """A blocker is a thing that is still true, so a board left open all afternoon
    must not still be showing the count it had when the tab was opened. Not while
    the list is open, though: a poll landing mid-sentence is a re-render nobody
    asked for."""
    assert "setInterval(() => { if(!blockedOpen) loadBlocked(); }, 15000);" in board


def test_an_empty_board_queue_says_so_rather_than_rendering_blank(board):
    """"nobody is waiting on you" is what this list is for saying, and a blank panel
    says it in a way indistinguishable from a broken one."""
    assert "function renderBlocked(){" in board, "there is no list to render"
    fn = board[board.index("function renderBlocked(){"):]
    fn = fn[:fn.index("\n}")]
    assert "if(!blockedRows.length){" in fn
    assert "waiting on a person" in fn


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


def test_the_board_reads_the_blocker_class_off_the_key_that_endpoint_sends(board):
    """The chip reads `GET /blockers`, whose `_view` spells the column `kind`. It is
    `GET /plan` that renames it to `class` for the chip on a plan row — and the
    tooltip was written against that other shape, so it rendered the literal
    "undefined" as the class of the oldest question. That is the one sentence a
    person reads before deciding whether to open the queue, and #279's line about
    five `ui` checks being a different afternoon from six `decision`s is supposed to
    land exactly there."""
    fn = board[board.index("async function loadBlocked"):]
    fn = fn[:fn.index("\n}")]
    assert 'oldest["class"]' not in fn and "oldest.class" not in fn
    assert "oldest.kind" in fn
    row = board[board.index("function blockedRow(b){"):]
    row = row[:row.index("\n}")]
    assert 'b["class"]' not in row and "b.class" not in row, (
        "the rows come from the same read as the tooltip and take the same key")

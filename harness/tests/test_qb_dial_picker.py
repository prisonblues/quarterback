"""Setting a dial from the pane, with the vocabulary on screen — #539.

`qb-dash-tui`'s dial modal shipped as four empty boxes and one placeholder each.
The value placeholder read `P3, 2, true, null` — four value kinds in one line,
because it had to cover all 29 dials at once and therefore could not answer the
only question a person has, which is what THIS one takes. Nothing said which dials
exist, what one is set to now, or which way it may move; and a misspelt name saved
clean, because the board stores `dial` as opaque text on purpose and the refusal
arrives from a round hours later, on the old value.

What is pinned here is the behaviour that gap produced, in the order it bites:

  * **the names are reachable** — filtered by the half of a name a person
    remembers, and the completion is named on the refusal as well as under the
    box. A refusal that says "not a dial" while the answer sits one row below,
    unmentioned, is the same dead end with better manners.
  * **the refusal happens in the box** — the earlier cut dismissed the modal and
    let the app say it, which spends the other three fields to report a mistake in
    one of them and makes a person retype a reason they already wrote.
  * **a box that cannot read the harness's table still writes** — `{}` is "cannot
    tell", never "nothing is settable". Refusing there would leave a person with
    no door at all, which is worse than the form as it shipped.

The vocabulary itself — that it is the harness's own table and not a copy — is
`test_qb_dials_surface.py`'s half. This file is about the screen.

Run: pytest harness/tests/test_qb_dial_picker.py
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

import qbdata as qd                                       # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("textual") is None,
    reason="the clickable dashboard needs textual")

REPO = "prisonblues/quarterback"

#: One board row, so the spec line has something to say about what is in force.
#: `max_rounds` because it is the dial whose value a drain actually moves.
IN_FORCE = {"now": None, "dials": [{
    "dial": "review_panel.max_rounds", "value": 4, "repo": REPO, "scope": "repo",
    "reason": "draining", "set_by": "rich", "expires_at": None}]}


@pytest.fixture(scope="module")
def tui():
    """`qb-dash-tui.py` as a module — it has no importable name of its own."""
    loader = importlib.machinery.SourceFileLoader("qb_dash_tui",
                                                  str(BIN / "qb-dash-tui.py"))
    spec = importlib.util.spec_from_loader("qb_dash_tui", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vocabulary():
    vocab = qd.dial_vocabulary()
    assert vocab, "the loops are beside this checkout, so the table must be readable"
    return vocab


#: The pane this has to fit. `qb-dash-tui` runs in a tmux pane beside the seats,
#: and 78 columns is the width every panel in the file is written against.
PANE = (78, 24)


def drive(tui, screen, steps):
    """Run one modal to completion and hand back `(screen, what it dismissed with)`.

    `asyncio.run` around Textual's own harness rather than `pytest-asyncio`: the
    sandbox this suite runs in has pytest and nothing else, and a test that needed
    a plugin would not fail there, it would ERROR at collection — which is the
    failure #163 is named after.
    """
    from textual.app import App

    class Harness(App):
        got = "not dismissed"

        def on_mount(self) -> None:
            self.push_screen(screen, lambda result: setattr(self, "got", result))

    app = Harness()

    async def go():
        async with app.run_test(size=PANE) as pilot:
            await pilot.pause()
            await steps(app.screen, pilot)
            await pilot.pause()

    asyncio.run(go())
    return screen, app.got


def modal(tui, vocabulary, row=None, **kw):
    return tui.DialEdit(row, kw.pop("repo", REPO), kw.pop("label", REPO),
                        vocabulary=kw.pop("vocabulary", vocabulary),
                        in_force=kw.pop("in_force", IN_FORCE), **kw)


def text_of(screen, selector):
    from textual.widgets import Static
    return str(screen.query_one(selector, Static).content)


def field(screen, selector):
    from textual.widgets import Input
    return screen.query_one(selector, Input)


# ---- the names are reachable -------------------------------------------------


def test_every_settable_dial_is_offered_before_anything_is_typed(tui, vocabulary):
    """The first answer to "which dials are there" is all of them. A picker that
    started empty would be a second box you have to already know the answer to."""
    from textual.widgets import OptionList

    async def steps(screen, pilot):
        assert screen.query_one("#names", OptionList).option_count == len(vocabulary)
        # And the row the eye is already on is described, rather than counted at.
        first = next(iter(vocabulary.values()))
        assert text_of(screen, "#spec") == first["what"]
        assert "↓ names" in text_of(screen, "#hint")

    drive(tui, modal(tui, vocabulary), steps)


def test_scrolling_the_list_says_what_each_one_does(tui, vocabulary):
    """The point of the picker. A name under the cursor is a person asking "is this
    the one I meant", and answering that only once a choice is made answers it after
    the moment it was useful — which is how you end up choosing by opening the
    source. The box is not touched: what is highlighted is being READ, and what is
    typed is what will be written."""
    async def steps(screen, pilot):
        screen.action_to_names()
        await pilot.pause()
        seen = []
        for _ in range(4):
            seen.append(text_of(screen, "#spec"))
            await pilot.press("down")
            await pilot.pause()
        assert len(set(seen)) == 4, seen
        assert seen == [vocabulary[name]["what"] for name in screen.matches[:4]]
        assert field(screen, "#f_dial").value == ""

    drive(tui, modal(tui, vocabulary), steps)


def test_browsing_shows_the_description_and_choosing_shows_the_rest(tui, vocabulary):
    """The names and the full block cannot both fit a 78x24 pane, and the tie is
    broken by which question is being asked: scrolling is "which one", and a chosen
    dial is "what do I type, what is it now, what happens if I clear it"."""
    async def steps(screen, pilot):
        assert "default" not in text_of(screen, "#spec")
        field(screen, "#f_dial").value = "review_panel.max_rounds"
        await pilot.pause()
        said = text_of(screen, "#spec")
        assert said.startswith(vocabulary["review_panel.max_rounds"]["what"])
        assert "default 2" in said and "in force" in said

    drive(tui, modal(tui, vocabulary), steps)


def test_typing_the_half_a_person_remembers_narrows_the_list(tui, vocabulary):
    """`budget` is the word somebody has in mind; `review_panel.budget.` is the
    part they would have to look up."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "budget"
        await pilot.pause()
        assert len(screen.matches) >= 5
        assert all("budget" in name for name in screen.matches)

    drive(tui, modal(tui, vocabulary), steps)


def test_a_name_taken_from_the_list_fills_the_box_and_moves_on(tui, vocabulary):
    """Enter on the list is the whole point of it, and where it leaves the cursor
    is the difference between a picker and a lookup table: the next thing a person
    types is the value."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "max_rounds"
        await pilot.pause()
        screen.action_to_names()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert field(screen, "#f_dial").value == "review_panel.max_rounds"
        assert screen.focused.id == "f_value"

    drive(tui, modal(tui, vocabulary), steps)


# ---- what the chosen dial takes ----------------------------------------------


def test_the_value_hint_becomes_this_dials_own(tui, vocabulary):
    """The generic placeholder is what a form says when it cannot tell which dial it
    is on, and this one can tell from the moment it opens: the list is up with its
    first row highlighted, so the box describes that row rather than four spellings
    borrowed from four different dials."""
    async def steps(screen, pilot):
        first = next(iter(vocabulary.values()))
        assert field(screen, "#f_value").placeholder == first["hint"]
        field(screen, "#f_dial").value = "review_panel.max_rounds"
        await pilot.pause()
        assert field(screen, "#f_value").placeholder == "a number"

    drive(tui, modal(tui, vocabulary), steps)


def test_the_line_under_the_box_says_what_the_dial_decides(tui, vocabulary):
    """The question at the moment a name is picked out of 29 is not "how do I set
    this" — it is "is this the one I meant". A screen that answered only the first
    is the one that shipped: 29 dotted paths and no way to tell them apart."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "review_panel.round_trigger_floor"
        await pilot.pause()
        said = text_of(screen, "#spec")
        assert said.startswith("the lowest severity that buys another round")
        assert vocabulary["review_panel.round_trigger_floor"]["what"] in said

    drive(tui, modal(tui, vocabulary), steps)


def test_the_list_retires_once_a_name_is_actually_chosen(tui, vocabulary):
    """A list of names is help with choosing and has stopped helping the moment a
    choice is made — and the rows it gives back are the ones the description then
    spends. Typing again brings it back."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "max_rounds"
        await pilot.pause()
        assert screen.query_one("#names").display, "still choosing"
        field(screen, "#f_dial").value = "review_panel.max_rounds"
        await pilot.pause()
        assert not screen.query_one("#names").display
        for selector in ("#f_dial", "#f_value", "#spec", "#f_reason", "#f_expiry",
                         "#hint"):
            assert visible(screen, selector), selector
        field(screen, "#f_dial").value = "review_panel.max_round"
        await pilot.pause()
        assert screen.query_one("#names").display

    drive(tui, modal(tui, vocabulary), steps)


def test_the_spec_line_says_the_default_and_what_is_in_force(tui, vocabulary):
    """Two different questions and both are asked at this moment: what happens if I
    clear it, and am I the second person to move it today. The board returns the
    row it replaced for the same reason — this just says so first."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "review_panel.max_rounds"
        await pilot.pause()
        said = text_of(screen, "#spec")
        assert "default 2" in said and "in force" in said and "this repo" in said

    drive(tui, modal(tui, vocabulary), steps)


def test_a_dial_nobody_has_set_says_the_repos_own_value_stands(tui, vocabulary):
    """"No board dial" is a state, not a blank. A line that showed nothing there
    would read as a value the screen failed to fetch."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "review_panel.judge_model"
        await pilot.pause()
        assert "no board dial" in text_of(screen, "#spec")

    drive(tui, modal(tui, vocabulary), steps)


def test_a_narrow_dial_says_which_way_it_may_move(tui, vocabulary):
    """The direction rule is invisible in the value: `true` looks like a legal
    write and is discarded by `apply_dials` without the board ever objecting."""
    async def steps(screen, pilot):
        assert "narrow only" in text_of(screen, "#spec")

    drive(tui, modal(tui, vocabulary,
                     row={"dial": "reviewers.pi.enabled", "value": True,
                          "repo": REPO, "scope": "repo"}), steps)


def test_editing_an_existing_dial_offers_no_name_box(tui, vocabulary):
    """A dial is identified by its name, so an editable one would silently create a
    second dial rather than change the one on screen."""
    async def steps(screen, pilot):
        assert not screen.query("#f_dial") and not screen.query("#names")
        assert field(screen, "#f_value").value == "P2"

    drive(tui, modal(tui, vocabulary,
                     row={"dial": "review_panel.fix_severity_floor", "value": "P2",
                          "repo": REPO, "scope": "repo"}), steps)


# ---- the refusal happens in the box ------------------------------------------


def test_a_name_the_harness_will_ignore_stops_the_first_ctrl_s(tui, vocabulary):
    """The board takes this write and reports it as in force for ever while nothing
    applies it. Only this side has the table, so only this side can say so — and it
    says so without spending the three fields already filled in."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "review_panel.fix_sevrity_floor"
        field(screen, "#f_value").value = "P2"
        field(screen, "#f_reason").value = "tightening for the drain"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        assert "nothing this box knows applies" in text_of(screen, "#err")
        assert field(screen, "#f_reason").value == "tightening for the drain"

    _, got = drive(tui, modal(tui, vocabulary), steps)
    assert got == "not dismissed"


def test_and_the_second_ctrl_s_sets_it_anyway(tui, vocabulary):
    """A WARNING, not a veto, and the asymmetry with the value check is the point.
    The table is the harness beside THIS dashboard, and the two are installed
    separately — so a hard refusal would make a box one release behind a box that
    cannot set a dial the rest of the fleet already applies. `tempo` (#474) is the
    standing case: both dashboards draw it, `BOARD_DIALS` does not hold it."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "tempo"
        field(screen, "#f_value").value = "eager"
        field(screen, "#f_reason").value = "draining the backlog"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        screen.action_save()

    _, got = drive(tui, modal(tui, vocabulary), steps)
    assert got["dial"] == "tempo" and got["value"] == "eager"


def test_insisting_on_one_name_does_not_wave_the_next_one_through(tui, vocabulary):
    """The confirmation is about the dial that was warned over, not about the
    session: a person who typed one unknown name and meant it has said nothing at
    all about the next one they mistype."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "tempo"
        field(screen, "#f_reason").value = "draining"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        field(screen, "#f_dial").value = "review_panel.fix_sevrity_floor"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        assert "nothing this box knows applies" in text_of(screen, "#err")

    _, got = drive(tui, modal(tui, vocabulary), steps)
    assert got == "not dismissed"


def test_a_value_a_known_dial_cannot_take_is_never_waved_through(tui, vocabulary):
    """The other half of the asymmetry. The kind came from the same table as the
    name, so there is no version of the harness in which `max_rounds: "2"` is a
    value somebody applies — pressing again gets the same sentence."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "review_panel.max_rounds"
        field(screen, "#f_value").value = '"2"'
        field(screen, "#f_reason").value = "shorter cycles"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        assert "must be a number" in text_of(screen, "#err")

    _, got = drive(tui, modal(tui, vocabulary), steps)
    assert got == "not dismissed"


def test_the_refusal_names_the_completion_when_there_is_only_one(tui, vocabulary):
    """`max_rounds` is how the name is remembered and `review_panel.max_rounds` is
    what it is called. Withholding that when the filter has narrowed to exactly one
    would make a person go and look for what is already on the screen."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "max_rounds"
        field(screen, "#f_value").value = "3"
        field(screen, "#f_reason").value = "shorter cycles"
        await pilot.pause()
        # The line under the box is describing the row the picker landed on — the
        # completion itself rides on the refusal, where the person is asking to
        # write rather than to read.
        assert text_of(screen, "#spec") == vocabulary["review_panel.max_rounds"]["what"]
        screen.action_save()
        await pilot.pause()
        assert "↓ takes review_panel.max_rounds" in text_of(screen, "#err")

    drive(tui, modal(tui, vocabulary), steps)


def test_several_matches_name_none_of_them(tui, vocabulary):
    """Picking the first would be the screen guessing which dial somebody meant to
    move, which is the one guess a settings form must not make."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "budget"
        field(screen, "#f_value").value = "1"
        field(screen, "#f_reason").value = "capping"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        assert "↓ takes" not in text_of(screen, "#err")

    drive(tui, modal(tui, vocabulary), steps)


def test_a_value_the_harness_would_refuse_is_refused_here(tui, vocabulary):
    """A quoted `"2"` is a string the board stores happily and `max_rounds` will
    not take. The sentence is the harness's own, because the reason is a fact about
    the thing that will ignore it."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "review_panel.max_rounds"
        field(screen, "#f_value").value = '"2"'
        field(screen, "#f_reason").value = "shorter cycles"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        assert "must be a number" in text_of(screen, "#err")

    _, got = drive(tui, modal(tui, vocabulary), steps)
    assert got == "not dismissed"


def test_a_duration_that_is_not_one_is_refused_before_the_modal_closes(
        tui, vocabulary):
    """This used to be caught after dismissal, in the app, where the sentence
    arrived beside an empty form. `timedelta` raises OverflowError rather than
    ValueError past its range, which is why the bounded regex and this are two
    halves of one fix."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "review_panel.max_rounds"
        field(screen, "#f_value").value = "3"
        field(screen, "#f_reason").value = "shorter cycles"
        field(screen, "#f_expiry").value = "soon"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        assert "not a duration" in text_of(screen, "#err")

    _, got = drive(tui, modal(tui, vocabulary), steps)
    assert got == "not dismissed"


def test_a_dial_with_no_argument_is_refused_by_this_screen_too(tui, vocabulary):
    """The board refuses one without, and a dial whose argument was never written
    down is one nobody can later decide to remove."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "review_panel.max_rounds"
        field(screen, "#f_value").value = "3"
        field(screen, "#f_reason").value = "   "
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        assert "needs a reason" in text_of(screen, "#err")

    drive(tui, modal(tui, vocabulary), steps)


def test_a_clean_write_goes_back_as_the_strings_that_were_typed(tui, vocabulary):
    """Unparsed, so the app's own checks still run on the path where this screen had
    no vocabulary to check against. Parsing twice is cheaper than one of the two
    forgetting."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "review_panel.max_rounds"
        field(screen, "#f_value").value = "3"
        field(screen, "#f_reason").value = "shorter cycles while draining"
        field(screen, "#f_expiry").value = "4h"
        await pilot.pause()
        screen.action_save()

    _, got = drive(tui, modal(tui, vocabulary), steps)
    assert got == {"dial": "review_panel.max_rounds", "value": "3",
                   "reason": "shorter cycles while draining", "expiry": "4h",
                   "repo": REPO}


# ---- and a box that cannot read the table ------------------------------------


def test_with_no_vocabulary_it_is_the_form_that_shipped(tui):
    """`{}` is "this box cannot tell", and the write still goes through — the board
    is the judge, as it was before any of this. The line under the value says which
    of the two states the screen is in, because a picker that is simply absent
    looks like one that is broken."""
    async def steps(screen, pilot):
        assert "not checked here" in text_of(screen, "#spec")
        assert not screen.query_one("#names").display
        field(screen, "#f_dial").value = "review_panel.invented"
        field(screen, "#f_value").value = "whatever"
        field(screen, "#f_reason").value = "the board will judge it"
        await pilot.pause()
        screen.action_save()

    _, got = drive(tui, modal(tui, {}, in_force={}), steps)
    assert got["dial"] == "review_panel.invented"


def test_a_dial_with_no_name_is_refused_whatever_the_table_says(tui):
    """The one refusal that needs no vocabulary at all."""
    async def steps(screen, pilot):
        field(screen, "#f_reason").value = "because"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        assert "which dial?" in text_of(screen, "#err")

    _, got = drive(tui, modal(tui, {}, in_force={}), steps)
    assert got == "not dismissed"


# ---- and it has to fit the pane it opens in ----------------------------------


def visible(app_screen, selector):
    """Is this widget drawn inside the screen, rather than off the bottom of it?

    A Textual modal taller than its screen does not scroll into view or complain —
    it CLIPS, and what it clips is whatever was composed last. Which was the scope
    line, before #539 moved it into the title: the one control on this form whose
    mistake cannot be seen afterwards, hidden by the picker that was added above
    it. So this asks about the region rather than about the widget's existence.
    """
    widget = app_screen.query_one(selector)
    return widget.region.bottom <= app_screen.size.height and widget.region.height > 0


def test_the_whole_form_fits_the_pane_it_opens_in(tui, vocabulary):
    """78x24 with the picker open, the reason, the expiry and the keys all drawn.
    The margins, the scope line's home and the refusal line's absence are all paid
    for by this — it is one row from the edge either way."""
    async def steps(screen, pilot):
        for selector in ("#f_dial", "#names", "#f_value", "#spec", "#f_reason",
                         "#f_expiry", "#hint"):
            assert visible(screen, selector), selector

    drive(tui, modal(tui, vocabulary), steps)


def test_the_scope_is_on_the_title_line_where_nothing_can_clip_it(tui, vocabulary):
    """`fleet` and `this repo` are two settings with one name, and the mistake is
    invisible once made. It was composed last and went off the bottom the moment
    the picker was added, which is exactly the failure this form must not have."""
    from textual.widgets import Static

    async def steps(screen, pilot):
        title = screen.query_one("#box").query(Static).first()
        assert REPO in str(title.content)
        assert title.region.y < screen.query_one("#f_dial").region.y

    drive(tui, modal(tui, vocabulary), steps)


def test_the_refusal_line_takes_no_room_until_there_is_a_refusal(tui, vocabulary):
    """A line that is always drawn is one a person stops reading, and on this form
    it is also a row that has to come from somewhere."""
    async def steps(screen, pilot):
        assert not screen.query_one("#err").display
        screen.action_save()
        await pilot.pause()
        assert screen.query_one("#err").display
        assert visible(screen, "#err")

    drive(tui, modal(tui, vocabulary), steps)


def test_a_refusal_clears_when_the_field_it_is_about_is_edited(tui, vocabulary):
    """A refusal left standing beside a field that has changed reads as a second,
    still-live objection — and the two fields it is ever about are the name and the
    value. The reason and the expiry leave it alone."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "review_panel.max_rounds"
        field(screen, "#f_value").value = '"2"'
        field(screen, "#f_reason").value = "shorter cycles"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        assert screen.query_one("#err").display
        field(screen, "#f_expiry").value = "4h"
        await pilot.pause()
        assert screen.query_one("#err").display, "an unrelated field cleared it"
        field(screen, "#f_value").value = "2"
        await pilot.pause()
        assert not screen.query_one("#err").display

    drive(tui, modal(tui, vocabulary), steps)


def test_the_longest_description_still_fits_the_pane(tui, vocabulary):
    """The description is allowed to wrap to a second line where a dial needs one —
    `low_severity_fix_lines` is a budget over the band between two other dials, and
    there is no honest short way to say that. What it may not do is push the reason,
    the expiry or the keys off the bottom, so the ceiling is measured here rather
    than guessed at in the table."""
    longest = max(vocabulary, key=lambda name: len(vocabulary[name]["what"]))

    async def steps(screen, pilot):
        field(screen, "#f_dial").value = longest
        await pilot.pause()
        assert vocabulary[longest]["what"] in text_of(screen, "#spec")
        for selector in ("#f_dial", "#f_value", "#spec", "#f_reason", "#f_expiry",
                         "#hint"):
            assert visible(screen, selector), (longest, selector)

    drive(tui, modal(tui, vocabulary), steps)


def test_four_names_are_visible_at_once_while_browsing(tui, vocabulary):
    """Two is not a list you can scan, and two is what a default `OptionList` border
    leaves once the description has taken its rows — the frame costs half of what
    the picker is allowed. Measured on the drawn region, because the CSS number is
    the one thing that cannot say how many rows a person actually sees."""
    async def steps(screen, pilot):
        names = screen.query_one("#names")
        assert names.region.height == 4, names.region
        assert visible(screen, "#names") and visible(screen, "#hint")

    drive(tui, modal(tui, vocabulary), steps)


def test_scrolling_also_says_what_that_dial_will_take(tui, vocabulary):
    """The other half of "is this the one I meant" is "and what would I type into
    it". The value box is where that answer belongs, and it costs no rows — which is
    the only reason both halves fit a pane this size."""
    async def steps(screen, pilot):
        screen.action_to_names()
        await pilot.pause()
        seen = []
        for _ in range(3):
            seen.append(field(screen, "#f_value").placeholder)
            await pilot.press("down")
            await pilot.pause()
        assert seen == [vocabulary[name]["hint"] for name in screen.matches[:3]]
        assert "severity band" in seen[0] and "P1" in seen[0]

    drive(tui, modal(tui, vocabulary), steps)


def test_a_dial_the_table_does_not_know_takes_the_generic_hint_back(tui, vocabulary):
    """Four spellings from four different dials is what a form can say when it does
    not know which one it is on — and after a real dial has been looked at, leaving
    ITS hint under a name nobody recognises would be describing the wrong thing."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "review_panel.max_rounds"
        await pilot.pause()
        assert field(screen, "#f_value").placeholder == "a number"
        field(screen, "#f_dial").value = "review_panel.invented"
        await pilot.pause()
        assert field(screen, "#f_value").placeholder == screen.GENERIC_HINT

    drive(tui, modal(tui, vocabulary), steps)


def test_the_lines_under_the_boxes_line_up_with_the_text_in_them(tui, vocabulary):
    """An `Input` draws a border and pads inside it, so its text starts three
    columns in; a bare `Static` starts at the panel's padding. The description sat
    three columns to the left of every field it describes, and so did the names and
    the refusal — a ragged left edge down the middle of a form that is otherwise one
    column. `content_region` is asked rather than the CSS, because the padding is
    only correct in terms of what the border and the widget's own padding did."""
    async def steps(screen, pilot):
        want = screen.query_one("#f_value").content_region.x
        for selector in ("#spec", "#names"):
            assert screen.query_one(selector).content_region.x == want, selector
        screen.action_save()
        await pilot.pause()
        assert screen.query_one("#err").content_region.x == want

    drive(tui, modal(tui, vocabulary), steps)


def test_the_form_still_fits_with_the_list_up_and_a_wrapped_refusal(tui, vocabulary):
    """The tallest state there is, and the one the earlier fit tests did not reach:
    the picker still showing (an unknown name that filters to several), a two-line
    description under the value box, and a refusal wrapped onto a second line. This
    form grows downward as it objects, and what a Textual modal does when it
    outgrows its screen is clip whatever was composed last."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "budget"
        field(screen, "#f_value").value = "x"
        field(screen, "#f_reason").value = "capping the drain"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        assert screen.query_one("#names").display, "the list is up in this state"
        assert screen.query_one("#err").region.height >= 2, "and the refusal wrapped"
        for selector in ("#f_dial", "#names", "#f_value", "#spec", "#f_reason",
                         "#f_expiry", "#err", "#hint"):
            assert visible(screen, selector), selector

    drive(tui, modal(tui, vocabulary), steps)


def test_the_longest_refusal_a_known_dial_can_raise_also_fits(tui, vocabulary):
    """The other tall state: no list, the full four-line block, and the harness's
    own sentence about a value — which names the dial, so the longest dial name
    makes the longest refusal."""
    longest = max(vocabulary, key=len)

    async def steps(screen, pilot):
        field(screen, "#f_dial").value = longest
        field(screen, "#f_value").value = '"not a value"'
        field(screen, "#f_reason").value = "an experiment"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        assert longest in text_of(screen, "#err")
        for selector in ("#f_dial", "#f_value", "#spec", "#f_reason", "#f_expiry",
                         "#err", "#hint"):
            assert visible(screen, selector), selector

    drive(tui, modal(tui, vocabulary), steps)


def test_an_armed_warning_stays_on_screen_while_it_is_armed(tui, vocabulary):
    """The unknown-name warning is not a complaint about the value — it says the
    NAME is one nothing here applies, and it stays true while the name does. Cleared
    on a value edit, the sentence vanished while the next ctrl+s went on being held
    open: a confirmation nobody can see they have given."""
    async def steps(screen, pilot):
        field(screen, "#f_dial").value = "tempo"
        field(screen, "#f_reason").value = "draining"
        await pilot.pause()
        screen.action_save()
        await pilot.pause()
        assert "nothing this box knows applies" in text_of(screen, "#err")
        field(screen, "#f_value").value = "eager"
        await pilot.pause()
        assert screen.query_one("#err").display, "the warning went while it still held"
        # And a NEW name disarms it, so the next unknown one is warned about again.
        field(screen, "#f_dial").value = "review_panel.invented"
        await pilot.pause()
        assert not screen.query_one("#err").display

    drive(tui, modal(tui, vocabulary), steps)


def test_the_reason_the_table_is_unreadable_is_the_one_that_is_printed(tui):
    """An absent harness, one that will not import and one older than the dial table
    all end in an empty vocabulary, and the screen used to tell all three as the
    first — sending somebody to look for a directory that is sitting right there."""
    said = "…/loops/harness_rules.py would not import: SyntaxError: invalid syntax"

    async def steps(screen, pilot):
        assert said in text_of(screen, "#spec")
        assert "not checked here" in text_of(screen, "#spec")

    drive(tui, modal(tui, {}, in_force={}, trouble=said), steps)

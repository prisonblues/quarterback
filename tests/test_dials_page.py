"""What the `/dials/view` page shows, and the one thing only it can do — #477.

`GET /dials` and `POST /dials` shipped with #305 and reached **no screen**: a dial
was set from an endpoint with curl and read back by one function in
`harness/loops/panel_seats.py`, so the values governing every round on the fleet
were invisible on `qb-dash`, `qb-dash-tui`, `qb-board` and the web board alike.
The two dashboards now render what is in force; this page is the other half, and
without it their "set one in a browser: …" line would point at a 404.

**It is not a nicer curl.** `POST /dials` takes :func:`app.auth.human` —
`Remote-User` plus the edge secret — and a dashboard authenticates with the
machine bearer token every agent on the box holds, which is precisely the
credential that gate is built to refuse. The edge identity a browser carries is
what makes setting a dial a person's decision, the same way it is what makes the
plan's order one (#443).

There is no JS test runner here, so the rendering half greps the file that ships
— the same crude-but-real guard ``test_plan_page.py`` and ``test_prs_page.py``
use on theirs. It is what stands between a re-edit and a page that quietly stops
distinguishing the two expiry states.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .conftest import LAPTOP

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = REPO_ROOT / "app/static"
PAGE = STATIC / "dials.html"


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


# ---- the page is served, and only to somebody the board will talk to ---------


async def test_the_page_is_served_and_needs_the_same_authentication_as_the_board(client):
    """A read, authorised like every other read: an agent's token, or a browser the
    edge vouched for. Reading is not deciding — an agent may look at what is in
    force, and must, because it is what its own next round will run under."""
    r = await client.get("/dials/view", headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert "quarterback dials" in r.text
    assert (await client.get("/dials/view")).status_code == 401


async def test_the_page_does_not_take_the_path_of_the_json_it_fetches(client):
    """`/dials` is the JSON this page reads, and one path cannot be both without
    content negotiation nobody would remember was there. The same rule that put
    the plan's page at `/plan/view`."""
    assert (await client.get("/dials", headers=LAPTOP)).status_code == 200
    assert (await client.get("/dials/view", headers=LAPTOP)).headers[
        "content-type"].startswith("text/html")


async def test_the_terminals_url_is_the_one_the_board_actually_serves(client):
    """The two dashboards print this path (`qbdata.dials_url`), with the screen's
    own repo on it. #443 is the record of what a surface that says "go and do it
    somewhere else" costs when it does not say where — so the somewhere has to
    exist, and with a query string it does not choke on."""
    r = await client.get("/dials/view?repo=prisonblues/quarterback", headers=LAPTOP)
    assert r.status_code == 200, r.text


# ---- what the page has to keep saying ---------------------------------------


def test_the_two_expiry_states_do_not_render_alike(page):
    """A `tempo: eager` with forty minutes on it and a `tempo: eager` set
    indefinitely are different situations — #244's rule (being idle and being
    broken must not look alike) applied to a switch instead of a queue.

    Two chip classes, two colours, and the indefinite one is the loud one: a dial
    that expires takes itself off the board with nobody remembering it, and one
    with no end stays until a person comes and clears it.
    """
    assert "chip forever" in page and "chip until" in page
    forever = re.search(r"\.chip\.forever\s*\{([^}]*)\}", page)
    until = re.search(r"\.chip\.until\s*\{([^}]*)\}", page)
    assert forever and until and forever.group(1) != until.group(1), \
        "the two expiry states share a style, so they render alike"
    assert "no end" in page


def test_the_page_applies_the_precedence_rather_than_showing_two_answers(page):
    """A repo read returns BOTH scopes so one call answers "what is in force
    here", and the board says in one line that applying the precedence is the
    client's. A page that drew both would state two values for one setting."""
    assert re.search(r'scope === "fleet" && overridden\.has', page), \
        "the fleet row a repo overrides has to come out of the in-force list"
    # …and be kept on screen, dimmed. The person who set it fleet-wide has to be
    # able to see what became of it.
    assert "shadowed" in page and "overridden" in page


def test_a_value_is_sent_as_a_value_and_not_as_its_spelling(page):
    """`2`, `true`, `null` and a list are values several dials document, and a
    `max_rounds` of `"2"` is a dial the harness refuses to apply and reports by
    name — a puzzle to be handed at a keyboard. Anything that is not JSON is the
    string it looks like, because `P3` and `eager` are values too."""
    assert "JSON.parse" in page
    assert re.search(r"catch\s*\(\s*err\s*\)\s*\{\s*return text", page), \
        "a value that is not JSON has to fall back to the string, not be refused"


def test_the_page_says_who_is_looking_before_a_write_is_refused(page):
    """`/whoami` answers agent, person, or nobody, and the difference decides
    whether either verb on this page will work. A refusal a reader can see coming
    is not the same event as a dead button."""
    assert "/whoami" in page and "kind === HUMAN" in page
    assert "fSubmit.disabled = true" in page


def test_the_reason_is_required_and_shown(page):
    """The board refuses a dial with no reason — "a dial nobody can read an
    argument for is a dial nobody can decide to remove" — so the form asks for one
    and every row renders it."""
    assert re.search(r'id="fReason"[^>]*required', page)
    assert "d.reason" in page


def test_the_page_is_built_for_a_phone(page):
    """The fleet is driven from a terminal and answered from a phone. 16px on the
    controls, because below it Safari zooms the viewport when one takes focus; 44px
    of height, because below that a control is missed rather than pressed."""
    assert "viewport-fit=cover" in page
    assert re.search(r"select, input \{[^}]*min-height:44px", page)
    assert re.search(r"select, input, textarea, button \{[^}]*font-size:16px", page, re.S)


def test_a_countdown_does_not_lose_an_hour_to_flooring(page):
    """A dial set to expire in four hours must not render as "for 3h" the instant
    it is written: a reader who has just chosen four hours and is shown three has
    been told the write did something other than what they asked."""
    assert re.search(r"function left\(s\)", page)
    assert re.search(r'left\(s\)[^}]*?"h"[^}]*?"m"', page, re.S), \
        "the countdown carries the minutes, unlike `ago`"


def test_the_expiry_is_read_off_the_board_and_not_off_the_browsers_clock(page):
    """A browser whose clock is minutes out would otherwise report an expiry that
    has not happened, or miss one that has. `expires_in` is the board's own
    seconds-remaining; the subtraction is only the fallback."""
    assert "d.expires_in != null" in page


def test_an_expiry_is_written_against_the_boards_clock_too(page):
    """"In 4 hours" has to become a timestamp somewhere, and from `Date.now()`
    alone it is four hours from THIS machine's idea of now: a laptop ten minutes
    slow writes a dial that expires early, and one an hour slow has its "in 1 hour"
    refused outright, because the endpoint rejects an expiry in the past and by the
    board's clock that is what it just sent. Neither failure names a clock.

    `GET /dials` returns its own `now`, so the correction costs nothing."""
    assert re.search(r"skew\s*=\s*Date\.parse\(body\.now\)\s*-\s*Date\.now\(\)", page)
    assert "boardNow() + seconds*1000" in page, \
        "the expiry a write sends has to be measured from the board's clock"
    assert not re.search(r"expires_at\s*=\s*new Date\(Date\.now\(\)", page)


# ---- and it is reachable ------------------------------------------------------


@pytest.mark.parametrize("name", ["plan.html", "prs.html", "fleet.html", "board.html"])
def test_every_page_with_a_nav_can_reach_the_dials(name):
    """The issue is "no screen anywhere shows which dials are in force", and a page
    nothing links to is a page nobody finds. Each of these already offers the
    others; this is one more of the same."""
    assert "/dials/view" in (STATIC / name).read_text(encoding="utf-8"), \
        f"{name} has a nav and does not offer the dials page"

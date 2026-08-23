"""What the `/prs` page says, and — as with `/fleet` — what it refuses to say.

Three endpoints shipped, were tested, were documented, and were called by nothing
on any screen (#395). ``GET /reviews`` has carried ``round``, ``cycle``,
``stopped``, ``stop_reason``, ``pr_state`` and ``ci_status`` per panel run since
v2.15; ``GET /review/needs-human`` answers what kind of judgement is waiting on a
person and how old the question is; ``GET /merge-queue`` says which pull request
is landing and who is behind it. Rich asked for *"the round"* and *"is it blocked
by anything"* and both were already computed, already served, and reached no
screen.

The data is all served, so this is a rendering job and the risk is entirely in
the rendering. Four readings can be wrong in ways nothing downstream recovers
from, and each has a test here:

* **A memory must not read as a reading.** ``pr_state`` and ``ci_status`` are
  what the panel saw at run time — ``app/models/review.py`` is explicit that a PR
  merged after its final round still reads ``OPEN`` — so neither may appear
  without the round and the age it came from.
* **A verdict is pinned to a commit.** A merge-queue entry whose ``verdict`` is
  ``ready`` against a ``ready_sha`` the head has moved off is not a PR that may
  land, and the row has to say so rather than drawing the word ``ready``.
* **An empty line is not a drained one.** Nothing enqueues automatically (#258),
  so a queue with no entries and a queue nobody feeds are one reading from out
  here. The page names both instead of drawing a clean zero — ``/fleet``'s rule
  for an absent lease, one contract over.
* **Round is not stage.** #262's ``stage`` is a fact about a *session*;
  ``ReviewRun.round`` is a fact about a *pull request*. A blank cell that could be
  read as either is worse than two honest cells, so an absent round says which of
  the two absences it is — and never "never panelled", which is a claim about the
  whole table made from the edge of a windowed query.

There is no JS test runner here, so the page half greps the file that ships — the
same crude-but-real guard ``test_fleet_page.py`` uses on ``fleet.html`` and
``test_plan_page.py`` uses on ``plan.html``. The endpoint half runs for real
against the app.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.models.merge_queue import VERDICTS
from app.needs_human import NEEDS_HUMAN_CLASSES

from .conftest import LAPTOP


def body(script: str, name: str) -> str:
    """One function's source, comments stripped.

    Every assertion below that names a behaviour looks inside the function that
    has to implement it. A bare grep over the whole page is satisfied by the
    comment arguing for the behaviour, which is exactly the test that passes
    while the behaviour is gone.
    """
    m = re.search(rf"\n(?:async )?function {name}\([^)]*\)\s*\{{", script)
    assert m, f"{name}() has gone"
    depth, i = 0, m.end() - 1
    while i < len(script):
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return re.sub(r"^\s*//.*$", "", script[m.end():i], flags=re.M)


REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE = REPO_ROOT / "app/static/prs.html"
REVIEW_MODEL = REPO_ROOT / "app/models/review.py"


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script(page: str) -> str:
    """The page's JS with its string concatenation joined up.

    Every long sentence on this page is written as ``"a " + "b"`` to stay inside
    the line width, and a test that greps for the sentence would never find one.
    ``test_fleet_page.py`` does the same normalisation for the same reason.
    """
    return re.sub(r'"\s*\+\s*"', "", page)


# ---- the page is served, and only to somebody the board will talk to ---------


async def test_the_page_is_served_and_needs_the_same_authentication_as_the_board(client):
    """A read, authorised like every other read: an agent's token, or a browser
    the edge vouched for. Not looser — this page names every PR the board holds a
    round for, and who is driving each one."""
    r = await client.get("/prs", headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert "quarterback prs" in r.text
    assert (await client.get("/prs")).status_code == 401


async def test_the_page_does_not_take_the_path_of_any_json_it_fetches(client):
    """`/reviews`, `/review/needs-human` and `/merge-queue` are the JSON this page
    reads, and one path cannot be both without content negotiation nobody would
    remember was there. The same rule that put the panel at `/panel`."""
    for path in ("/reviews", "/review/needs-human",
                 "/merge-queue?repo=prisonblues/quarterback&base=main"):
        r = await client.get(path, headers=LAPTOP)
        assert r.status_code == 200, r.text
        assert "<!doctype html>" not in r.text.lower()


def test_the_page_holds_no_credential(page):
    """It is served to a browser the edge vouched for and it fetches same-origin.
    A token in the page would be a token in every reader's cache."""
    assert "Bearer" not in page
    assert "Authorization" not in page


def test_the_page_is_built_for_a_phone(page):
    """Rich reads this away from the desk. `viewport-fit=cover` or every safe-area
    inset resolves to zero and a notch eats the first column."""
    assert 'name="viewport"' in page
    assert "viewport-fit=cover" in page
    for token in ("--sat", "--sab", "--sal", "--sar"):
        assert token in page, token


def test_every_touch_target_is_big_enough_to_hit(page):
    """44px, the same floor `/fleet` holds itself to."""
    heights = [int(m) for m in re.findall(r"min-height:(\d+)px", page)]
    assert heights, "no min-height rules at all — has the page lost its controls?"
    assert min(heights) >= 44, heights


# ---- it reads the three endpoints that had no reader ------------------------


def test_the_page_calls_all_three_endpoints_that_had_none(page):
    """The whole of #395. Any one of these dropping out is the defect returning."""
    assert "/reviews?limit=" in page
    assert "/review/needs-human?" in page
    assert "/merge-queue?repo=" in page


def test_the_page_writes_to_nothing(page):
    """Every fact here is a board read. There is no verb, and adding one would need
    an argument this page has not made — `/fleet` has exactly one and argued it."""
    assert not re.findall(r'method:\s*"POST"', page)
    assert "POST" not in page


def test_it_does_not_call_the_endpoint_a_browser_cannot_call(page):
    """`POST /review-queue` takes caller-supplied GitHub PR state, because the board
    holds no GitHub credential. A browser has no `gh`, so a page calling it would
    send an empty PR list and be honestly told the queue had drained. #395 says
    that is exactly why the other three are the ones to render."""
    assert "/review-queue" not in page


def test_it_adds_no_endpoint_of_its_own(page):
    """#395's own line: *if this needs one, it has drifted.* The paths the page
    fetches are the three it was written against, plus nothing."""
    fetched = set(re.findall(r'fetch\(\s*[`"](/[^`"?]+)', page))
    assert fetched == {"/reviews", "/review/needs-human", "/merge-queue"}, fetched


# ---- round is not stage -----------------------------------------------------


def test_a_pr_with_no_round_says_so_rather_than_going_blank(script):
    """#262 is adding a *session* stage; `ReviewRun.round` is a different fact about
    a different object. A blank cell that could be read as either is worse than two
    honest cells, so the absence is spelt."""
    assert "none in this window" in script


def test_an_absent_round_is_never_reported_as_a_pr_nobody_panelled(script):
    """The page reads a WINDOW of runs, so an absent round means absent from the
    window and nothing more — and where the row carries a needs-human defect it
    means less again, because only a panel round can raise one. "Never panelled"
    would be a conclusion drawn from the edge of a query, which is the same
    collapse `/fleet` refuses when a lease falls out of `/active`."""
    code = re.sub(r"^\s*//.*$", "", script, flags=re.M)   # the comment argues it
    assert "never panelled" not in code
    assert "outside this window" in code
    assert "r.needs.length" in body(script, "roundCell"), (
        "roundCell no longer distinguishes a PR whose defect proves a round ran")


def test_the_page_never_draws_a_session_stage(page):
    """It has no access to one — #262 has not landed the column — and a page that
    invented one would be the conflation this issue was careful about. With every
    comment stripped, the word does not survive: nothing the reader sees says it."""
    code = re.sub(r"/\*.*?\*/", "", page, flags=re.S)          # CSS and block comments
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)           # line comments
    assert "stage" not in code.lower(), [
        ln for ln in code.splitlines() if "stage" in ln.lower()]


def test_the_round_cell_names_the_run_it_came_from(script):
    """A round number with no as-of is the same class of lie as a stage column that
    fills in for local rows only."""
    assert "ago(r.run.ts)" in body(script, "roundCell")


# ---- a memory is drawn as a memory ------------------------------------------


def test_the_page_words_the_panel_memory_the_way_the_model_already_does():
    """`ReviewRun.pr_state`'s own docstring makes this argument. Two readers of one
    caveat wording it two ways is how an operator learns to believe whichever they
    read first, so the page's sentence has to be recognisably the model's."""
    # The model's sentence is a `#:` attribute docstring wrapped over two lines,
    # so it has to be unwrapped before it is one sentence to look for.
    model = re.sub(r"\n\s*#:\s*", " ", REVIEW_MODEL.read_text(encoding="utf-8"))
    assert "The board is told about panels, not about merges" in model
    page = re.sub(r'"\s*\+\s*"', "", PAGE.read_text(encoding="utf-8"))
    assert "the board is told about panels, not about merges" in page.lower()


def test_pr_state_and_ci_are_never_drawn_without_the_round_they_came_from(script):
    """Both are what the panel saw at run time and nothing has refreshed them. The
    one function that renders either always attaches the round and the age."""
    src = body(script, "memoryCell")
    assert "as of round ${r.run.round}" in src
    assert "ago(r.run.ts)" in src
    # And nothing else may draw them: every other mention of either field, with
    # the comments stripped, has to be handing it TO memoryCell or deciding
    # whether the staleness caveat is owed.
    code = re.sub(r"^\s*//.*$", "", script, flags=re.M)
    code = code.replace(src, "")
    for field in ("pr_state", "ci_status"):
        for line in code.splitlines():
            if f".{field}" not in line:
                continue
            assert "memoryCell(" in line or "out.push" in line or "old &&" in line, (
                f"{field} is drawn somewhere other than memoryCell: {line!r}")


# ---- a verdict is pinned to a commit ----------------------------------------


def test_a_ready_verdict_on_a_moved_head_is_not_drawn_as_ready(script):
    """`GET /merge-queue` already computes `ready = verdict == "ready" and
    ready_sha == head`. A page that drew the word and ignored the boolean would
    report a PR as landable on a commit nobody judged."""
    cell = body(script, "queueCell")
    assert 'e.verdict === "ready" && e.ready === false' in cell
    assert "retired" in cell
    # And the queue listing, which draws the same entry a second time.
    listing = body(script, "renderQueues")
    assert 'e.verdict==="ready" && e.ready===false' in listing.replace(" ===", "===")
    assert "retired" in listing


def test_the_page_shows_the_verdict_the_board_issued_rather_than_a_gloss(script):
    """`VERDICTS` is a closed set the board owns and may extend. A page mapping each
    to a word of its own would silently draw a blank for a verdict added later, so
    it renders the board's own string and only interprets the one case the board
    itself already computes — `ready` against a head that has moved."""
    assert "esc(e.verdict)" in script
    glossed = [v for v in VERDICTS if f'"{v}":' in script]
    assert not glossed, (
        "the page has started glossing merge-queue verdicts; the board owns that "
        f"vocabulary and may add to it: {glossed}")


# ---- an empty line is not a drained one -------------------------------------


def test_an_empty_queue_says_it_cannot_tell_empty_from_unfed(script):
    """#258: preland is a gate nothing invokes, so nothing enqueues automatically.
    A queue that has never had an entry is indistinguishable from one nobody
    enqueues to, and the only way that ever gets noticed is a surface that shows
    the zero and says what it cannot rule out."""
    assert "nothing enqueues automatically" in script
    assert "#258" in script
    # And the sentence is rendered where the zero is, not filed in a comment.
    assert "QUEUE_UNFED" in body(script, "renderQueues")


def test_a_queue_that_could_not_be_read_is_not_an_empty_queue(script):
    """The other half of the same collapse: a 500 from `/merge-queue` rendered as an
    empty list reads as a drained line. `qbdata.fetch_review_queue` refuses the same
    conflation for the same reason."""
    listing = body(script, "renderQueues")
    assert "q.error" in listing
    assert "not an empty queue" in listing
    # And an unreadable line must not settle a row's own cell either: a PR
    # missing from a queue nobody could read is not a PR that is not queued.
    assert "if(q.error) continue" in re.sub(r"^\s*//.*$", "", script, flags=re.M)


def test_a_zero_on_the_human_count_says_what_it_covers(script):
    """Only a panel round raises a needs-human defect, so a PR nothing has panelled
    contributes nothing. Without that said, the zero reads as "nothing needs you"."""
    assert "only a panel round raises one of these" in script
    assert "NEEDS_HUMAN_SCOPE" in body(script, "load")


def test_the_classes_the_page_shows_are_the_classes_the_board_defines(page):
    """The page renders whatever class the endpoint hands back rather than a list of
    its own — so there is no second vocabulary to drift. This pins that it does not
    hardcode one that is missing a class."""
    hardcoded = [c for c in NEEDS_HUMAN_CLASSES if f'"{c}"' in page]
    assert not hardcoded, (
        "the page has started naming needs-human classes itself; the endpoint "
        f"already returns `classes` and `labels`: {hardcoded}")


# ---- nothing outstanding is ever hidden -------------------------------------


def test_only_a_row_the_board_holds_nothing_outstanding_about_can_be_hidden(script):
    """The toggle may hide a settled row and nothing else. A PR waiting on a human,
    or standing in the line, being one tap away is the false-clean this page is
    about — `/fleet`'s rule, and the same shape of failure."""
    src = [ln.strip() for ln in body(script, "quiet").splitlines() if ln.strip()]
    # The refusal is the FIRST thing the function does, so nothing later can
    # reach past it: a row with an outstanding judgement or a place in the line
    # returns before any other rule is consulted.
    assert src[0] == "if(r.needs.length || r.entry) return false;", src
    assert any("r.run.stopped === true" in ln for ln in src), src


def test_the_page_caps_nothing(script):
    """`/fleet` caps its settled tail because three months of dead sessions is a page
    that does not open. There is no equivalent here — the run window is the cap, it
    is on the FETCH, and the footer says so. Nothing slices the row list."""
    assert ".slice(" not in body(script, "render")


def test_the_footer_says_what_the_page_did_not_look_at(script):
    """A list that reads as complete and is not is the same defect as an empty queue
    that reads as drained. The window, the needs-human cap and the number of lines
    it declined to ask about are all stated."""
    src = body(script, "load")
    assert "has no row" in src
    assert "asked about" in src
    assert "undiscoverable from here" in src


# ---- it never silently stops refreshing -------------------------------------


def test_an_unreachable_board_is_said_and_not_left_on_a_stale_page(script):
    """Without this the last good page stays on screen looking live, with nothing to
    say it stopped refreshing — this page's own failure mode, in the page."""
    src = body(script, "load")
    assert "the board is unreachable" in src
    assert "the board would not answer" in src
    # Both go through `fell`, which marks what is already on screen as the older
    # reading it now is. An error message beside numbers that still look current
    # leaves the reader no way to tell which of the two they are seeing.
    assert src.count("fell(") == 2, src
    assert "staleSince" in body(script, "fell")
    assert "staleSince" in body(script, "render")


def test_a_slow_read_cannot_paint_over_a_newer_one(script):
    """Three of the four fetches are awaited after another await, so two ticks can
    overlap. The generation counter is what stops the older one landing last."""
    src = body(script, "load")
    assert "const my = ++gen" in src
    # Every line that commits a read to module state has to come AFTER a guard.
    guards = [i for i, ln in enumerate(src.splitlines())
              if "if(my !== gen) return" in ln]
    commits = [i for i, ln in enumerate(src.splitlines())
               if re.match(r"\s*(rows|queues|scopeNote|readAt|waiting) =", ln)]
    assert guards and commits
    assert all(any(g < c for g in guards) for c in commits), (guards, commits)


# ---- the page reads a sample, and never speaks for what it did not read ------


def test_an_absent_queue_entry_is_never_reported_as_a_pr_nobody_queued(script):
    """`GET /merge-queue` answers about exactly one `(repo, base)` and this page
    asks about a derived, capped handful. So a missing entry means "not in a line
    this page read" — and where the PR's base was never among them, not even
    that. Reporting it as "not queued" would be the same collapse as reading an
    absent lease as a dead agent."""
    cell = body(script, "queueCell")
    assert "not queued" not in cell
    assert "r.lineRead" in cell, "the cell no longer distinguishes the two absences"
    assert "line not read" in cell


def test_a_line_that_could_not_be_read_settles_nothing_about_its_rows(script):
    """A 500 from one `(repo, base)` must not make every PR that would land there
    read as unqueued. The rows are only marked read against lines that answered."""
    src = body(script, "load")
    assert "answered" in src
    assert "if(q.error) continue" in src


def test_a_trimmed_needs_human_list_is_not_rendered_as_a_complete_one(script):
    """`truncated` says the endpoint's own cap trimmed the LIST while `waiting`
    still counts the whole of it. A row with no listed defect under truncation
    means "none among the ones returned" — and a footnote at the bottom of the
    page does not repair a false cell in the middle of it."""
    cell = body(script, "humanCell")
    assert "r.listTrimmed" in cell
    assert "none listed" in cell


def test_the_headline_human_count_is_the_boards_own_and_not_the_drawn_rows(script):
    """Summing the rows reports the trimmed number under the untrimmed label, and
    this is the one figure on the page a person is meant to act on."""
    src = body(script, "render")
    assert "waiting === null" in src
    assert "drawn" in src


def test_the_default_line_is_asked_about_before_any_derived_one(script):
    """The cap is real, and derived bases are a long tail. Eight recent feature
    branches inserted first would consume it and drop the one line this page
    promises to ask about, for every repo on it."""
    src = body(script, "load")
    default_at = src.index("DEFAULT_BASE")
    derived_at = src.index("run.base) pairs.set") if "run.base) pairs.set" in src \
        else src.index("pairs.set(`${run.repo}")
    assert default_at < derived_at, (
        "the derived pairs go in before the default ones; the cap now drops main")


def test_a_pr_in_two_of_the_lines_read_is_not_silently_collapsed(script):
    """Overwriting would report one membership and one depth for a PR standing in
    two lines, and undercount "in line". The first wins the cell and the row says
    there were others."""
    src = body(script, "load")
    assert "r.entries += 1" in src
    assert "if(!r.entry)" in src
    assert "r.entries > 1" in body(script, "caveats")


# ---- every payload value crosses esc() on its way to innerHTML --------------


def test_no_payload_value_reaches_innerhtml_without_esc(script):
    """Every value on this page is agent-supplied. `esc()` is safe in an attribute
    as well as in text, and it is the only door — an interpolation that skips it is
    a hole whether or not today's value happens to be a number.

    Only the four functions that BUILD markup are checked. The cell builders
    (`roundCell`, `queueCell`, …) return plain strings that `factEl` escapes on the
    way in, so escaping them twice would render `&amp;` in a PR title."""
    #: Interpolations the page composes ITSELF, listed one by one so that adding
    #: another is a deliberate act rather than a token that happens to match.
    #: `r.repo`/`r.pr` build the GitHub URL, which is escaped where it is used.
    OWN = {
        'r.repo', 'r.pr', 'v', 'r.title?"":" untitled"', 'n', 'claim',
        'hidden', 'human', 'drawn', 'waiting', 'rows.length',
        'counts.queued + counts.landing', 'counts.unclear + counts.noround',
    }
    holes = []
    for fn in ("prRow", "render", "renderQueues"):
        for expr in re.findall(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}",
                               body(script, fn)):
            e = " ".join(expr.split())
            if not e or e in OWN:
                continue
            if any(tok in e for tok in ("esc(", "who(", "factEl", "caveats(",
                                        "LABEL[", "span(")):
                continue
            holes.append((fn, e))
    assert not holes, holes


def test_the_page_is_free_of_stray_control_characters(page):
    """A NUL that reached this file through an editor once already. It survived a
    green test run and a commit, because nothing looked."""
    stray = sorted({ord(c) for c in page if ord(c) < 32 and c not in "\t\n\r"})
    assert not stray, stray

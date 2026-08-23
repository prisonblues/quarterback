"""What the browser plan page says about work somebody else's plan claim covers.

There is no JS test runner here, so this greps the file that ships — the same
crude-but-real guard `test_reviewer_cost.py` uses on `reviews.html`. It is the
only thing standing between a re-edit and a header that contradicts the panel
directly below it.

The defect it pins: `counts` gained a `covered` key and `next` began skipping an
item held by a plan claim, and the page was not told. With one agent holding a
plan, the header read "2 open · 0 held · 0 waiting" above a panel asserting that
everything open was claimed or waiting — nothing on the page claimed, nothing
waiting, and no number anywhere for the thing that was actually true. The page's
own invariant comment ("the counts, the list and the next panel always describe
the same set of rows") is there to prevent exactly that.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE = REPO_ROOT / "app/static/plan.html"
API = REPO_ROOT / "app/api/plan.py"


def _counts_keys(source: str) -> set[str]:
    """The string keys of the ``"counts"`` dict `GET /plan` returns."""
    keys: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (isinstance(key, ast.Constant) and key.value == "counts"
                    and isinstance(value, ast.Dict)):
                keys |= {k.value for k in value.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return keys


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text()


def test_the_header_counts_work_a_plan_claim_covers(page):
    """The header is the first place a reader looks, and `covered` is the answer
    to "then why is nothing next?" — so it is a number there, not only a chip on
    each row."""
    assert "counts.covered" in page, "the header must name the covered count"
    # Beside the claimed count rather than in a corner: held item-by-item and
    # held by the plan over it are the two "somebody has this" facts.
    assert re.search(r"counts\.claimed.{0,80}counts\.covered", page, re.S), \
        "covered belongs next to held — both answer 'who has this'"


def test_the_empty_next_panel_names_every_reason_next_skipped_a_row(page):
    """"claimed or waiting" was a complete list of the reasons before plan claims
    existed. An agent whose whole plan is covered by one claim reads that panel
    with nothing claimed and nothing waiting on the page."""
    assert "everything open is claimed or waiting" not in page, \
        "a covered item is neither claimed nor waiting"
    panel = re.search(r"nothing is free[^<]*", page)
    assert panel, "the empty-next panel must still say nothing is free"
    assert "plan somebody else holds" in panel.group(0), \
        "the panel must name the plan claim as a reason"


def test_every_count_the_page_reads_is_one_the_endpoint_sends():
    """The other half of the same contradiction: a key the page invents renders
    as 0 for ever, and reads exactly like a true zero. `GET /plan` builds the
    dict, so that dict is the vocabulary.

    Read off the syntax tree rather than by grepping the source: the endpoint's
    formatting is nobody's contract, and a guard that broke when the dict was
    rewrapped would be deleted rather than fixed."""
    sent = _counts_keys(API.read_text())
    assert sent, "could not find the counts dict in app/api/plan.py"
    assert "covered" in sent, "the endpoint is meant to count covered items"

    read = set(re.findall(r"counts\.([a-z_]+)", PAGE.read_text()))
    assert read <= sent, f"the page reads counts the endpoint never sends: {read - sent}"


# ---- a scope that is not a repo (#323) --------------------------------------


def test_the_page_never_builds_a_github_url_for_a_project_scope(page):
    """A GitHub URL is constructible only from a scope bound to a repository.

    `project:65lowther` names none, so interpolating it into
    `github.com/<repo>/issues/<n>` would produce a link to a page that does not
    exist. The server refuses to attach an issue or PR ref to such an item at all;
    this is the second half of that rule, at the one place the URL is built."""
    built = re.search(r"function refUrl\(it\)\{[^}]*\}", page)
    assert built, "could not find refUrl"
    assert "isProject(it.repo)" in built.group(0), \
        "refUrl must exclude a scope with no repo behind it"


def test_the_sigil_the_page_uses_is_the_one_the_server_defines():
    """Two spellings of one scope is the defect this whole feature is downstream
    of, so the page may not carry its own idea of the prefix."""
    server = re.search(r'PROJECT_SIGIL = "([^"]+)"',
                       (REPO_ROOT / "app/scope.py").read_text())
    assert server, "could not find PROJECT_SIGIL in app/scope.py"
    assert f'const PROJECT_SIGIL = "{server.group(1)}"' in PAGE.read_text()


def test_the_scope_picker_offers_a_scope_that_has_no_items_yet(page):
    """Built from the items alone, a freshly declared scope is invisible until
    somebody has already managed to put work in it — through a picker that does
    not list it. So the read's own `scopes` feeds it too."""
    assert "d.scopes" in page, "the page must read the declared scopes off the plan read"
    assert re.search(r"want = \[.*declared.*\]", page), \
        "the picker's options must include the declared scopes"


def test_the_option_value_is_the_canonical_scope_and_only_the_label_is_shortened(page):
    """The value is what the server is asked for and the text is what a person
    reads. Shortening the value would send `65lowther` to an endpoint that
    correctly refuses it."""
    options = re.search(r"repoSel\.innerHTML = want\.map\([^;]*;", page)
    assert options, "could not find the option builder"
    assert 'value="${esc(v)}"' in options.group(0), "the value stays canonical"
    assert "scopeLabel(v)" in options.group(0), "only the visible text is shortened"


def test_declaring_a_scope_is_on_the_page_because_it_is_a_human_decision(page):
    """`POST /plan/scope` is behind `app.auth.human`, which refuses a bearer token
    — so a browser is the only way in, and this page is the browser. Without the
    control the endpoint ships unreachable by the person it is for."""
    assert 'id="newScope"' in page, "the page needs a way to declare a scope"
    assert '"/plan/scope"' in page, "and it has to call the human-only endpoint"


# ---- the page is read on a phone, where nothing hovers (#387) ---------------
#
# The move gate is correct and stays: order is per exact scope, so a reorder of one
# repo's list must not be able to renumber the fleet band. What was wrong is that
# its reason lived in a `title=` attribute — and a phone has no hover, so on first
# load a reader got three dead glyphs and no text anywhere. Three different states
# looked identical from a thumb: "pick a scope", "this row is ordered in another
# list", and "the edge refused your write". These pin each one to its own surface.


def test_the_page_is_built_for_a_phone(page):
    """Same floor `fleet.html` set: a viewport meta, and room for the notch on
    every edge — a phone in landscape has it on the side, not along the bottom."""
    assert re.search(r'<meta name="viewport"[^>]*width=device-width', page), \
        "no viewport meta — the page renders at desktop width on a phone"
    assert "viewport-fit=cover" in page, "safe-area insets are all zero without it"
    assert "env(safe-area-inset-bottom)" in page and "env(safe-area-inset-left)" in page, \
        "a notch is on the side in landscape, not only along the bottom"


def test_every_touch_target_the_page_declares_is_big_enough_to_hit(page):
    """44px is the floor below which a control is missed rather than pressed.

    Written as "every one it declares" on purpose: the ▲▼ themselves are still the
    size they always were, and #388 rebuilds that row's controls entirely. This is
    the guard against a half-measure creeping into what IS declared."""
    heights = [int(n) for n in re.findall(r"min-height:(\d+)px", page)]
    assert heights, "no min-height on any control — nothing is guaranteed tappable"
    assert min(heights) >= 44, f"a control is only {min(heights)}px tall"


def test_the_scope_picker_does_not_zoom_the_viewport_when_it_is_tapped(page):
    """Safari zooms the page when a control under 16px takes focus, and the header
    jumps out from under the thumb. The picker is the control this page now tells
    a reader to go and use, so it is the one that must not do that."""
    rule = re.search(r"select, input \{[^}]*\}", page)
    assert rule, "could not find the select/input rule"
    size = re.search(r"font-size:(\d+)px", rule.group(0))
    assert size and int(size.group(1)) >= 16, \
        "a picker under 16px zooms the viewport when it is focused"


def test_the_gate_itself_is_unchanged(page):
    """The fix is not "enable the buttons". A reorder posts the whole scope's order,
    so a row of another list in that payload is a 422 at best and a renumbered fleet
    band at worst. Only an OPEN row of the EXACT scope on screen may move."""
    gate = re.search(r"const canMove = ([^;]+);", page)
    assert gate, "could not find the move gate"
    assert 'it.state==="open"' in gate.group(1), "a done row carries no order"
    assert "s!==undefined" in gate.group(1), "(all) is several lists at once"
    assert "it.repo===s" in gate.group(1), "the scope being reordered is exact"


def test_a_control_that_will_not_act_still_answers_a_tap(page):
    """`disabled` takes no events and no focus, so the reason it is dead cannot be
    asked for — which on a desktop was fine (the `title` answered a hover) and on a
    phone left the button indistinguishable from a broken page. It stays visibly
    dead, and it answers."""
    dead = re.search(r"function dead\(ok, why\)\{[^}]*\}", page)
    assert dead, "could not find the dead() helper"
    assert "disabled" not in dead.group(0).replace("aria-disabled", ""), \
        "a `disabled` control cannot be asked why it is disabled"
    assert 'aria-disabled="true"' in dead.group(0), \
        "assistive tech still has to be told the control is unavailable"
    assert "data-why" in dead.group(0), "and the reason has to travel with it"
    # The tap is answered, and answered FIRST: a dead move button carries
    # `data-move` too, and must not fall through to the reorder the gate refused.
    handler = re.search(r'listEl\.addEventListener\("click".{0,400}', page, re.S)
    assert handler and 'closest("[data-why]")' in handler.group(0), \
        "the click handler must read the reason before it reads the verb"


def test_no_explanation_on_a_row_is_reachable_only_by_hovering(page):
    """The defect, generalised. `title=` is a desktop-only answer, so every one of
    them inside a row — the move gate, the drop button, the rank's provenance
    (#183), the ref with no repo to link to — carries a `data-why` beside it that a
    tap can reach."""
    body = page[page.index("function row(it, s)"):page.index("// The one invariant")]
    titles = [m.start() for m in re.finditer(r'title="', body)]
    assert titles, "the row template carries no titles at all — has it been rewritten?"
    for at in titles:
        # same tag: back to the opening `<`/`` ` ``, forward to the closing `>`
        start = max(body.rfind("<", 0, at), body.rfind("`", 0, at))
        tag = body[start:body.index(">", at)]
        # Either spelled out, or emitted by `dead()` — which the test above pins
        # to writing one.
        assert "data-why" in tag or "dead(" in tag, \
            f"a title with no tap-reachable form: {tag[:90]!r}"


def test_the_reason_every_row_is_dead_in_all_is_said_once_and_visibly(page):
    """In `(all)` the reason is the same for every row, and a forty-times-repeated
    banner is worse than the tooltip it replaces. So it is one line, above the list,
    present exactly when the page is in that state."""
    hint = re.search(r"hintEl\.innerHTML = s===undefined\s*\?([^:]+):", page, re.S)
    assert hint, "the scope hint must be keyed off the same `undefined` the gate is"
    assert "(all)" in hint.group(1) and "scope" in hint.group(1), \
        "the line has to name the state and what to do about it"
    assert "picker" in hint.group(1), "and point at the control that fixes it"
    # ...and it is not ALSO on every row.
    assert page.count("hintEl.innerHTML") == 1, "said once means written once"


def test_the_page_says_who_is_looking_before_a_write_is_refused(page):
    """Every write here — reorder, drop, declare a scope — is behind
    `app.auth.human`. A 403 arriving under a thumb looks exactly like a dead button,
    so the page asks `/whoami` first and says what it was told, the way `fleet.html`
    does about the one verb on that page."""
    assert '"/whoami"' in page, "the page must ask who is looking"
    assert "bannerEl.innerHTML" in page, "and say so where it can be read"
    assert 'body.kind === HUMAN' in page, "a person is the case with nothing to warn about"
    banner = page[page.index("async function loadMe()"):page.index("// ---- the picker")]
    assert "refused" in banner, "an agent has to be told the writes will not land"


def test_a_refusal_and_an_explanation_do_not_share_one_line(page):
    """The three states are three surfaces, or they are the same ambiguity again:
    the banner is who you are, the hint is which list you are in, and the note is
    this row. A failed write keeps the red line to itself."""
    for el in ('id="banner"', 'id="hint"', 'id="note"', 'id="err"'):
        assert el in page, f"missing surface: {el}"
    assert re.search(r"function note\(msg\)\{ noteEl", page), \
        "an explanation is not an error and does not go in errEl"
    assert re.search(r"function say\(msg\)\{ errEl", page), \
        "a refusal is not an explanation and does not go in noteEl"


def test_an_explanation_outlives_the_refresh_tick(page):
    """The list re-reads every 20 seconds. An answer that vanished half a second
    after the tap that asked for it would be worse than the tooltip it replaced, so
    nothing on the read path clears the note or the banner."""
    load = page[page.index("async function load()"):page.index("function buildRepoOptions")]
    assert "note(" not in load and "bannerEl" not in load, \
        "the 20s read path must not wipe an explanation the reader asked for"


# ---- rank provenance, on a page with no hover (#183 on a phone) -------------


def test_a_position_somebody_chose_is_legible_without_reading_the_sentence(page):
    """#183 landed provenance so a reader could tell ranks 1-17 (a real sequence)
    from 18-28 (the order the adds arrived in). In a `title` that distinction is
    unreachable on a phone — so it is weight, not only colour, plus the sentence on
    a tap."""
    chosen = re.search(r"const RANK_CHOSEN = new Set\(\[([^\]]*)\]\)", page)
    assert chosen, "the page must say which sources mean somebody chose the position"
    assert "ordered" in chosen.group(1) and "placed" in chosen.group(1), \
        "a human ordering and an agent placing are both choices"
    assert "appended" not in chosen.group(1), "an append is precisely nobody choosing"
    rule = re.search(r"\.rank\.chosen \{[^}]*\}", page)
    assert rule and "font-weight" in rule.group(0), \
        "colour alone is not a distinction every reader can see"


def test_a_rank_source_the_page_cannot_read_is_not_reported_as_an_append(page):
    """The same class of hole #335 found in the review marker: the one row nobody
    can attribute is the one that renders as nothing.

    `RANK_WHY[it.rank_source]` misses when the board names a source this page has
    no sentence for. Left to `|| ""` that row lost its explanation *and* kept the
    muted weight that means "nobody chose this position" — a definite claim about
    the one row no claim can be made about. Three-valued, like `fleet.html`'s
    verdict: what was reported, and the shape of an answer that cannot be read."""
    cls = re.search(r"function rankClass\(it\)\{(.*?)\n\}", page, re.S)
    assert cls, "could not find rankClass"
    assert "in RANK_WHY" in cls.group(1), \
        "an unknown source has to be detected before it is classified"
    assert "unclear" in cls.group(1), "and given its own reading, not one of the two"
    assert re.search(r"\.rank\.unclear \{[^}]*\}", page), \
        "an unclear rank needs to look like neither answer"
    # The row still carries a reason — an empty one reads as no provenance at all.
    why = re.search(r"function rankWhy\(it\)\{(.*?)\n\}", page, re.S)
    assert why and '|| ""' not in why.group(1), \
        "an empty explanation is indistinguishable from no explanation"
    assert "no sentence for" in why.group(1), "it has to say what it cannot say"


def test_every_rank_source_the_server_sends_has_a_reason_on_the_page():
    """A source the page has no sentence for renders as an empty explanation, which
    reads exactly like a rank with no provenance at all."""
    tree = ast.parse(API.read_text())
    sent = {c.value for node in ast.walk(tree)
            if isinstance(node, ast.keyword) and node.arg == "rank_source"
            for c in ast.walk(node.value)
            if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    # `reorder` writes it through a local, so it is not a keyword anywhere.
    sent.add("ordered")
    known = set(re.findall(r"^  (\w+):", re.search(
        r"const RANK_WHY = \{(.*?)\n\};", PAGE.read_text(), re.S).group(1), re.M))
    assert sent, "could not find any rank_source the endpoint writes"
    assert sent <= known, f"the page has no reason for: {sent - known}"


# ---- the picker remembers, so (all) is not where every visit starts ---------


def test_the_scope_the_reader_last_chose_survives_a_page_load(page):
    """Every load landing in `(all)` is every load landing in the one state where
    nothing can be moved. Restored BEFORE the first read, so that read is already
    the reader's scope rather than `(all)` followed by a second fetch."""
    assert 'const SCOPE_KEY = "qb.plan.scope"' in page, "the key must be namespaced"
    assert "rememberScope(repoSel.value)" in page, "a change to the picker is remembered"
    tail = page[page.index("const saved = rememberedScope();"):]
    assert tail.index("repoSel.value = saved") < tail.index("load();"), \
        "restoring after the first read makes the first read the wrong one"
    assert re.search(r"function rememberedScope\(\)\{\s*try", page), \
        "Safari throws on localStorage in private browsing; the page must still render"


def test_nothing_shadows_the_function_that_puts_an_explanation_on_screen(page):
    """`note` is a function and `note` is also what a scope's one-line description
    is called. A local of that name shadows the function for its whole enclosing
    handler — the temporal dead zone above the declaration included — so calling it
    there throws, and everything after the call in the successful path is skipped.
    That is a runtime error no `node --check` and no grep of the shipped page would
    otherwise see."""
    shadows = re.findall(r"\b(?:const|let|var)\s+note\b", page)
    assert not shadows, "a local named `note` shadows note() for its whole scope"

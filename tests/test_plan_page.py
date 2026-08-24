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

from .conftest import LAPTOP

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


def test_explaining_a_live_control_is_not_a_way_of_disabling_it(page):
    """#398's ⊘ is a live control that also needs a tap-reachable explanation — a
    thumb has no other way to be told what tapping it will do. So the two cannot be
    the same thing: what stops a tap falling through to the verb is the REFUSAL,
    which only `dead()` writes and only as `aria-disabled`, not the mere presence of
    a reason. Keyed off the reason instead, every control that learned to explain
    itself would go dead in the act of learning."""
    handler = page[page.index('listEl.addEventListener("click"'):]
    handler = handler[: handler.index("let path, body;")]
    assert 'aria-disabled' in handler, \
        "the fall-through must be stopped by the refusal, not by the explanation"
    assert not re.search(r'closest\("\[data-why\]"\);\s*if\(why\)\{[^}]*return;', handler), \
        "a bare `data-why` return swallows every live control that carries a reason"
    acts = page[page.index('<div class="acts">') : page.index("</div>`;")]
    exempt = re.search(r"<button data-exempt=.*?>⊘</button>", acts, re.S)
    assert exempt, "the ⊘ must still be in the row's controls (#335)"
    assert "data-why" in exempt.group(0), "and its explanation must be tap-reachable"
    assert "dead(" not in exempt.group(0) and "aria-disabled" not in exempt.group(0), \
        "the ⊘ is rendered conditionally, never disabled — it is never a dead control"


def test_a_review_marker_nobody_can_attribute_still_answers_a_tap(page):
    """The other half of #398's own last fix. A request typed by hand carries no
    author and no reason, and an empty `title` was invisible on a desktop and is a
    tap that clears the header on a phone — silence rendered as an answer. Both
    review chips resolve to a sentence before they are put on the page."""
    for name in ("grantWhy", "askedWhy"):
        why = re.search(rf"const {name} = ([^;]+);", page, re.S)
        assert why, f"{name} must be one sentence, worked out once"
        assert '""' not in why.group(1), \
            f"{name} falls back to an empty string — an empty tap is not an answer"


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


def test_exempting_a_pr_from_review_is_on_the_page_because_only_a_person_may(page):
    """The disposal half of #335, and the reason a refusal is not a dead end.

    An agent may ask for an exemption and may not grant one, so the grant has to
    be reachable by the person it is for — on a phone, in one tap. Without the
    control the endpoint ships unwired, which is the pattern #169 is about: a
    mechanism nobody can reach is not a control.
    """
    assert '"/plan/item/exempt"' in page, "the page has to call the endpoint"
    assert "data-exempt" in page, "and offer it on the row"
    # A reason in both directions. #279 refuses a bare flag at the API and at the
    # database CHECK; a button that sent one would just move the refusal.
    assert re.search(r"data-exempt.{0,600}reason", page, re.S), \
        "the control must collect a reason, because the endpoint requires one"


def test_the_page_never_re_derives_what_a_note_means_about_review(page):
    """The marker is a token in free text, so a regex on this page is how the page
    and the queue would come to disagree about the same row. `GET /plan` publishes
    `review` on every PR item; the page reads that and nothing else."""
    assert "it.review" in page, "the page reads the server's derivation"
    for token in ("exempt-requested", "review: exempt", "review:exempt"):
        assert token not in page, f"the page must not look for {token!r} itself"


# ---- reordering more than one place at a time (#388) ------------------------
#
# `POST /plan/reorder` takes the ORDER for one exact scope, not a move instruction,
# so drag, send-to-top, send-to-bottom and jump-5 are all that one call with a
# differently computed array — and no API change. What these guard is the three
# ways that goes wrong: sending N requests where the whole point was one, wrapping
# where the ask was to clamp, and a drag that picks up a row the buttons refuse.

VENDOR = REPO_ROOT / "app/static/vendor/sortable.min.js"
VENDOR_README = REPO_ROOT / "app/static/vendor/README.md"
BOARD_VIEW = REPO_ROOT / "app/api/board_view.py"

# The pin. A minified blob cannot say which version it is, so this is where the
# question is answered — and where a silent swap is caught.
SORTABLE_SHA256 = "6d0a831fc19b4bae851797ad3393157e861afb7862459c11226359b27e2c4337"
SORTABLE_VERSION = "1.15.6"


def test_moving_five_places_is_one_request_and_not_five(page):
    """Seventeen taps and seventeen POSTs to drag rank 20 to rank 3 is the reason
    the plan's order goes uncorrected, and a loop of single-place moves would be
    the same defect wearing the new buttons. Every control computes the FINAL
    array and one caller posts it once."""
    fn = re.search(r"function moved\(ids, from, how\)\{(.*?)\n\}", page, re.S)
    assert fn, "could not find the order computation"
    body = fn.group(1)
    for how in ("top", "bottom", "up", "down", "up5", "down5"):
        assert f'"{how}"' in body, f"no computation for {how!r}"
    assert "post(" not in body and "fetch(" not in body and "for(" not in body, \
        "the whole order is computed here and sent once by the caller"
    # The button path: one `moved()`, one body, and one shared write below.
    branch = re.search(r"\} else if\(move\)\{(.*?)\n  \} else return;", page, re.S)
    assert branch, "could not find the move branch"
    assert "const order = moved(ids, i, move.dataset.move);" in branch.group(1), \
        "one computation per tap, whatever the distance"
    assert "for(" not in branch.group(1) and "while(" not in branch.group(1), \
        "a loop of single-place moves is the old defect wearing the new buttons"
    assert "return order && {repo: s, order}" in branch.group(1), \
        "the request carries the whole order, not a delta"
    # ...and it is DEFERRED, because a sequence means something only relative to the
    # list at the instant it is computed. Two taps 150ms apart used to be two
    # requests worked out from the same list, the second made without the first.
    assert "build = ()=>{" in branch.group(1), \
        "a reorder body has to be computed after the write before it has landed"


def test_a_jump_clamps_at_the_ends_instead_of_wrapping(page):
    """"Down 5" on the third-from-last row goes to the bottom. It does not
    reappear at the top, which on a list somebody is trying to put in order is not
    a near miss — it is the opposite of what was asked for."""
    fn = re.search(r"function moved\(ids, from, how\)\{(.*?)\n\}", page, re.S)
    assert fn, "could not find the order computation"
    body = fn.group(1)
    assert "Math.max(0, Math.min(last" in body, "the destination has to be clamped"
    assert "%" not in body, "a modulo is a wrap, and a wrap is the wrong answer here"
    # A move that changes nothing is not a write: it would stamp `ordered` on a
    # sequence nobody chose to change (#183 is what that value means).
    assert "if(at === from) return null" in body, \
        "a no-op must not be posted as a human's decision"


def test_the_controls_asked_for_are_all_on_the_row(page):
    """⏫/⏬ as their own buttons, not a double-tap gesture: double-tap on mobile is
    claimed by zoom, and a gesture that sometimes zooms and sometimes reorders is
    worse than no gesture at all."""
    for glyph, verb in (("⏫", "top"), ("⏬", "bottom")):
        assert re.search(rf'data-move="{verb}".*?>{glyph}', page, re.S), \
            f"no {glyph} button for send-to-{verb}"
    assert 'const JUMP = 5' in page, "the jump is fixed at five, so it is one tap"
    assert 'data-move="up5"' in page and 'data-move="down5"' in page, \
        "the jump has to be a control, not only a computation"
    assert "data-grip" in page, "and the drag needs something to take hold of"


def test_a_drag_refuses_exactly_the_rows_the_buttons_refuse(page):
    """The `canMove` rule stands. A drag that could pick up a row of another list
    would put that row in a payload naming this scope — a 422 at best, and at worst
    the fleet band renumbered by a reorder that never mentioned it."""
    assert 'draggable: ".item.movable"' in page, \
        "only rows the page may reorder are draggable"
    cls = re.search(r'el\.className = "item " \+ it\.state.*?;', page, re.S)
    assert cls and "canMove" in cls.group(0), \
        "the class the drag reads must be written from the same gate the buttons use"
    # ...and a refusal still explains itself, in #387's vocabulary rather than a
    # `disabled` that takes no events and so cannot be asked.
    assert 'filter: ".grip.dead"' in page, "a dead grip cannot be lifted"
    assert "preventOnFilter: false" in page, \
        "and its tap must still reach the page, or the refusal is silent again"
    grip = re.search(r'<button class="grip.*?>☰</button>', page, re.S)
    assert grip, "could not find the grip"
    assert "data-why" in grip.group(0), "the grip says why it will not lift"
    # ...and it says it as its NAME, not as `aria-disabled`. The grip always acts —
    # it opens the bar the drop button lives on, and that button is live on a row
    # whose order this page may not touch. Announced as unavailable, the readers
    # most likely to need that route would be told there is none.
    assert "aria-disabled" not in grip.group(0), \
        "the grip is not a control that will not act — it is one that will not LIFT"
    assert "aria-label=" in grip.group(0), \
        "☰ names nothing on its own; the sentence has to be the accessible name"


def test_the_drag_is_not_built_on_html5_drag_and_drop(page):
    """The trap this feature exists inside. `draggable`/`dragstart` DO NOT FIRE ON
    TOUCH — a drag written that way tests perfectly on the desktop it was written on
    and does nothing whatsoever on the phone this was asked for."""
    # Comments stripped: this page ARGUES about HTML5 dragging at some length, and
    # a guard that cannot tell the warning from the mistake would fail on the
    # warning.
    code = re.sub(r"^\s*//.*$", "", page, flags=re.M)
    for token in ("dragstart", "dragover", "ondrop", 'draggable="true"'):
        assert token not in code, f"{token!r} is desktop-only and this page is a phone's"
    assert "forceFallback: true" in page, \
        "native HTML5 dragging on a desktop means the tested path is not the shipped one"
    grip = re.search(r"\.acts \.grip \{[^}]*\}", page)
    assert grip and "touch-action:none" in grip.group(0), \
        "without touch-action the browser claims the gesture as a scroll"
    assert "scroll: true" in page, \
        "a list taller than the screen cannot be reordered without autoscroll"
    # ...and the band that triggers it has to clear the STICKY header, or scrolling
    # up means holding the finger where the header covers the row you are aiming at.
    # The header wraps at 320px and grows a line when it has something to say, so it
    # is measured per drag rather than guessed at once.
    assert re.search(r'sorter\.option\("scrollSensitivity".*?'
                     r'querySelector\("header"\)\.getBoundingClientRect\(\)\.height',
                     page, re.S), \
        "autoscroll's edge band must be sized from the header that covers it"
    # And the controls a person taps repeatedly must not be held back to see whether
    # the second tap was a zoom: the page is deliberately zoomable, so a fast double
    # tap on ▼ moved the row one place and sent one request without this.
    bar = re.search(r"\.bar button \{[^}]*\}", page)
    assert bar and "touch-action:manipulation" in bar.group(0), \
        "a second tap inside the double-tap window is a zoom gesture without this"


def test_a_drag_the_server_refuses_puts_the_row_back_at_once(page):
    """A drag that visually succeeds and then silently reverts on the next 20s tick
    is worse than a button that never moved: the reader has been shown a lie and
    given twenty seconds to act on it."""
    fn = re.search(r"function write\(path, build\)\{(.*?)\n\}", page, re.S)
    assert fn, "could not find the one place a write goes out"
    assert "if(ok) await load(); else render();" in fn.group(1), \
        "a refused reorder repaints from what the board still has"
    # `post()` puts the server's own sentence in the header, and `render()` does not
    # wipe it — so the row goes back AND the reason stays up.
    assert "note(" not in fn.group(1), "the explanation outlives the repaint"
    # And writes are serialised. Two overlapping reorders are two sequences computed
    # from one list; the server takes them in whatever order they arrive, so the
    # second silently undoes the first.
    assert "let queue = Promise.resolve()" in page and "queue.then" in fn.group(1), \
        "writes go out one at a time, in the order they were asked for"


def test_the_refresh_tick_does_not_repaint_under_a_finger(page):
    """The 20s read rebuilds the list. Landing mid-drag it would destroy the row
    being dragged — the same reason a write in flight already holds it off."""
    assert "if(!busy && !dragging) load()" in page, "a lift counts as busy"
    start = re.search(r"onStart\(\)\{(.*?)\n  \},", page, re.S)
    assert start and "dragging = true" in start.group(1), "and says so when it starts"
    # The flag only stops the tick STARTING a read. One already in flight was asked
    # for before the lift, lands during it, and `render()`s the row out from under
    # the finger — so the lift supersedes it, using the generation counter that
    # already discards a read overtaken by a filter change.
    assert "gen++" in start.group(1), \
        "a read in flight when the lift begins must not be allowed to land"
    write = re.search(r"function write\(path, build\)\{(.*?)\n\}", page, re.S)
    assert write and "gen++" in write.group(1), "and the same for one in flight at a write"


def test_the_click_a_drag_leaves_behind_is_not_a_tap(page):
    """A drop ending in a synthesized click would open a bar nobody asked for, or
    land on whatever button the finger came up over. Suppressed by consuming the
    click itself rather than by a flag on a zero-delay timer, which assumes the
    click is dispatched before the next task — not a promise any browser makes."""
    fn = re.search(r"function eatTheClick\(\)\{(.*?)\n\}", page, re.S)
    assert fn, "could not find the click suppression"
    assert 'addEventListener("click", eat, true)' in fn.group(1), \
        "the click is caught before it reaches the row, not filtered inside it"
    assert "removeEventListener" in fn.group(1) and "setTimeout(stop" in fn.group(1), \
        "exactly one click, and given up on if none arrives"
    assert "suppressClick" not in page, "the timer-flag version must be gone"


def test_the_row_sheds_its_controls_rather_than_shrinking_them(page):
    """Eight controls at 44px do not fit beside a rank, a ref, a title and the chips
    on a 320px screen, and 44px is the floor below which a control is missed rather
    than pressed. So the row keeps ONE permanent control — fewer than it had — and
    the rest live on a bar that control opens."""
    acts = re.search(r'<div class="acts">(.*?)</div>', page, re.S)
    assert acts, "could not find the row's permanent controls"
    assert acts.group(1).count("<button") == 1, \
        "the row carries one permanent control; the rest are on the bar"
    bar = re.search(r"\.bar button \{[^}]*\}", page)
    assert bar, "could not find the bar's button rule"
    for prop in ("min-height:44px", "min-width:44px"):
        assert prop in bar.group(0), f"the bar's buttons need {prop}"
    assert "flex-wrap:wrap" in re.search(r"\.bar\.open \{[^}]*\}", page).group(0), \
        "eight controls wrap onto more lines rather than shrinking below 44px"
    # Three to a line at every width, rather than six that split five-and-one at
    # 320px and put the ⏬ that wrapped next to the ✕ instead of next to its ▼.
    moves = re.search(r"\.bar button\[data-move\] \{[^}]*\}", page)
    assert moves and "flex:1 1 30%" in moves.group(0), \
        "the move set has to break into equal lines, not spill one control"
    # ...and the row itself has to be able to give the bar a line.
    assert "flex-wrap:wrap" in re.search(r"\.item \{[^}]*\}", page).group(0)


def test_every_control_on_the_bar_says_what_it_does_before_it_is_pressed(page):
    """#387's rule, applied to a bar of eight glyphs. A `title` is a desktop-only
    answer and a thumb has no way to ask; `dead()` already writes the refusal, so
    this is the other half — what pressing a LIVE one will do."""
    fn = re.search(r"function ctl\(ok, refusal, does\)\{(.*?)\n\}", page, re.S)
    assert fn, "a live control needs a sentence too"
    assert "dead(false, refusal)" in fn.group(1), \
        "the refusal keeps exactly one definition, and it is dead()"
    assert "data-why" in fn.group(1), "a live control's sentence is reachable by a tap"
    bar = page[page.index('<div class="bar'):page.index("</div>`;")]
    moves = re.findall(r"<button data-move=.*?</button>", bar, re.S)
    assert len(moves) == 6, f"expected six move controls on the bar, found {len(moves)}"
    for button in moves:
        assert "ctl(" in button, f"a move control with no sentence: {button[:60]!r}"


def test_an_open_action_bar_survives_the_twenty_second_repaint(page):
    """The list is rebuilt every 20s. A bar that closed itself on the tick would
    take the drop button and the jump controls with it, mid-decision."""
    assert "let openRow = null" in page, "which row is open has to outlive the paint"
    assert "const open = it.item_id === openRow" in page, "and be re-read when it is drawn"
    # ...and not outlive the row itself.
    assert "if(openRow && !shown.some(it => it.item_id === openRow)) openRow = null" in page, \
        "a bar left open would reattach to whatever id happened to match"


# ---- the library: vendored, pinned, and actually served ---------------------


def test_the_vendored_library_is_the_version_that_was_pinned():
    """A minified blob cannot say which version it is. Without a checksum "which
    Sortable is this" has no answer, and a swap is invisible in review."""
    import hashlib

    assert VENDOR.exists(), "the vendored library is missing"
    got = hashlib.sha256(VENDOR.read_bytes()).hexdigest()
    assert got == SORTABLE_SHA256, f"the vendored file is not the pinned one: {got}"
    doc = VENDOR_README.read_text()
    assert SORTABLE_SHA256 in doc and SORTABLE_VERSION in doc, \
        "the pin has to be readable beside the file, not only in a test"
    assert "https://" in doc, "and say where the bytes came from"


def test_the_asset_is_read_at_import_so_a_missing_one_is_a_crash():
    """The shape the four pages already use, and the reason for it: read at import,
    a file the build failed to ship is a startup crash. Fetched lazily it would be a
    silent 404 leaving `/plan/view` looking healthy with no drag on it — which is
    #169's pattern, and this repo has closed several defects that were exactly it."""
    src = BOARD_VIEW.read_text()
    assert re.search(r'^_SORTABLE_JS = \(_STATIC / "vendor" / "sortable\.min\.js"\)'
                     r'\.read_text\(', src, re.M), \
        "the asset must be read at import, like every page beside it"
    # And a route, not a mount: one served asset does not need a second way in,
    # with an auth boundary of its own to reason about.
    assert "from fastapi.staticfiles" not in src and ".mount(" not in src
    route = re.search(r'@router\.get\("/vendor/[^"]+"\)\s*\nasync def \w+\((.*?)\) ->',
                      src, re.S)
    assert route and "Depends(reader)" in route.group(1), \
        "the asset is read behind the same identity the page that asks for it is"


def test_the_page_asks_for_the_path_the_route_serves():
    """Two places have to agree and neither can see the other. A typo here is a
    404 the page cannot report — it would simply have no drag."""
    src = BOARD_VIEW.read_text()
    served = re.search(r'@router\.get\("(/vendor/[^"]+\.js)"\)', src)
    assert served, "no route serves the vendored script"
    asked = re.search(r'<script src="([^"]+\.js)"></script>', PAGE.read_text())
    assert asked, "the page does not load the library"
    assert asked.group(1) == served.group(1), \
        f"the page asks for {asked.group(1)} and the app serves {served.group(1)}"


async def test_the_vendored_library_is_served_and_gated(client):
    """#169 again, from the other end: a control nobody can reach is not a control,
    and the way this one would fail is by 404ing in the deployed container while
    every local test passed."""
    r = await client.get("/vendor/sortable.min.js", headers=LAPTOP)
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "Sortable" in r.text
    assert r.headers.get("cache-control"), "45KB over a phone connection, every page load"
    assert (await client.get("/vendor/sortable.min.js")).status_code == 401


def test_the_container_ships_the_vendored_asset():
    """The thing most likely to be missed. Nothing under `app/static/` had ever
    needed to exist as a separately served file before, so that path was untested —
    and the failure mode is a 404 in production under a page that looks healthy.

    It works today because the image copies `app/` wholesale. This is the guard
    against a later Dockerfile that enumerates what to copy and quietly leaves the
    one directory out."""
    docker = (REPO_ROOT / "Dockerfile").read_text()
    assert re.search(r"^COPY app/ app/$", docker, re.M), \
        "the image must copy app/ wholesale, or the vendor directory needs its own line"
    assert not (REPO_ROOT / ".dockerignore").exists(), \
        "a .dockerignore could exclude the vendored asset — check it if this is added"

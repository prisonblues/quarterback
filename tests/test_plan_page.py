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

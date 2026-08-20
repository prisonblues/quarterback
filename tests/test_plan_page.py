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

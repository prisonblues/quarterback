"""The joins and the parsing the dashboards share, tested without a dashboard.

qbdata is stdlib-only on purpose, so this runs anywhere `pytest` does — unlike
test_qb_dash.py, which wants textual and a configured board. The join between
board claims and `gh issue list` is the piece worth pinning: it decides which
issues the panel offers as free, and offering a held one sends two agents at the
same work.

Run: pytest harness/tests/test_qbdata.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

import qbdata as qd                                       # noqa: E402


def claim(key: str, holder: str = "zeus/seat-1", kind: str = "issue") -> dict:
    return {"key": key, "holder": holder, "kind": kind}


def test_an_issue_claim_joins_on_its_number():
    held = qd.issue_claims([claim(f"{qd.REPO}#175", holder="zeus/badger-ember")])
    assert list(held) == [175]
    assert held[175]["holder"] == "zeus/badger-ember"


def test_another_repos_issue_does_not_mark_ours_held():
    """Two repos both have a #12; joining on the bare number would confuse them."""
    assert qd.issue_claims([claim("someone/other#12")]) == {}


def test_claims_that_are_not_issues_are_ignored():
    keys = [f"{qd.REPO}:main", f"{qd.REPO}:2.44", "", "#7", f"{qd.REPO}#abc"]
    assert qd.issue_claims([claim(k, kind="merge") for k in keys]) == {}


def test_free_issues_sort_above_held_ones_newest_first():
    issues = [{"number": n} for n in (170, 169, 168, 167)]
    held = qd.issue_claims([claim(f"{qd.REPO}#170"), claim(f"{qd.REPO}#168")])
    assert [i["number"] for i in qd.sort_issues(issues, held)] == [169, 167, 170, 168]


def test_the_first_claim_on_an_issue_is_the_one_shown():
    held = qd.issue_claims([claim(f"{qd.REPO}#9", holder="zeus/one"),
                            claim(f"{qd.REPO}#9", holder="laptop/two")])
    assert held[9]["holder"] == "zeus/one"

"""A key the panel sends and ``ReviewIn`` does not name is dropped in silence (#643).

``ReviewIn`` is declared ``populate_by_name=True`` with no ``extra=``, so
pydantic v2's default ``extra="ignore"`` applies: a top-level key on the panel's
payload that the model does not name is discarded without a word, without a log
line, and without a 422. That has now happened **five times** — ``head_sha``,
``unread_files``, the provenance pair, ``converged`` (#626), and ``review_panel``,
which this issue's PR stores. Every one was found by a human noticing a number
was missing, months apart. Nothing failed.

This file is what fails.

It reads the top-level keys of every payload ``harness/loops/panel.py`` builds and
compares them against ``ReviewIn``'s field names **and validation aliases** — the
aliases because the panel calls the repo slug ``github`` and the PR subject
``title``, and a check that missed those would report two of its own accepted
fields as drift on the first run. What is left over is what ingest drops. That set
is held against :data:`DROPPED_BY_DESIGN`, a list written out by hand.

**Written out, not computed.** A computed allow-list passes forever, which is the
property every earlier attempt at this check would have had. The point of the list
is that adding to it is a deliberate act somebody has to type, in a diff somebody
has to review, so that the two questions a new dropped key raises — is this a
field somebody meant to add, or a key somebody meant to stop sending — get an
answer instead of a silence.

**Read by ``ast``, not by import.** ``harness/loops`` is installed by
home-manager as ``share/quarterback-harness/loops`` with no ``app/`` beside it, so
the harness suite cannot see ``ReviewIn`` and this suite is the only place both
halves are readable at once — the position ``tests/test_needs_human_drift.py``
is in. Reading the source rather than importing it is that file's lesson and
``tests/test_post_type_drift.py``'s before it: a test that SKIPS when an import
fails is a test that never runs anywhere, which is how the drift got in.

The static read has one failure mode of its own — a rename that makes the scan
match nothing would leave it comparing two empty sets and passing — so
:func:`test_the_scan_actually_found_the_panels_payloads` pins a floor and a
handful of keys that must be in it, and the drift assertion below fails on a
missing allow-list entry as loudly as on a new one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import AliasChoices

from app.api.reviews import ReviewIn
from app.models.review import ReviewRun

REPO_ROOT = Path(__file__).resolve().parent.parent
PANEL = REPO_ROOT / "harness" / "loops" / "panel.py"

#: Keys the panel sends at the top level of a run payload and this board does not
#: store. Every one of them was checked against ``ReviewRun.__table__.columns``
#: and none has a column, so there is no second ``converged``-shaped bug in here —
#: :func:`test_no_dropped_key_shares_a_name_with_a_column` is that check, kept as
#: a test rather than as this sentence.
#:
#: Grouped by why each is dropped. The grouping is prose; the test reads the union.
DROPPED_BY_DESIGN = frozenset({
    # ---- The round's own bookkeeping, useful to the NEXT round and to nobody on
    # this board. These travel in `--json-file`, which is how a cycle chains its
    # rounds, and the board stores the round's OUTCOME rather than its working.
    "escalated",            # key -> the round an escalation was first declared in
    "acknowledged",         # key -> the round an unverifiable claim was accepted in
    "unresolved_claims",    # #547's ledger, per claim
    "new_finding_keys",     # the keys behind `new_findings`, which IS stored
    "prior_rounds",         # derivable from this board's own rows for the cycle
    "prior_findings",       # likewise
    "cycle_trend",          # #490, rebuilt per round from every baseline's raw fields
    "proposals",            # #507's constructive pass — not a finding, never stored as one
    # ---- The round's own account of itself, published and not aggregated here.
    "preflight",            # #138's verdict: was this round worth running, and why
    "config_notes",         # free prose the panel puts in its own report
    # ---- Measurements this board has not decided to keep. Each is a number the
    # panel publishes and nothing here aggregates; storing one is a decision about
    # what the /panel page reports, not a plumbing fix.
    "guard_ratio",          # #492, instrument-before-gate — #618 may still move this
    "context_chars",        # chars prepared ALONGSIDE the target under increment scope
    "pr_chars",             # the whole PR's size, whatever this round reviewed (#298)
    "timing",               # #192's wall-clock block
    "local_suite",          # #548's record of what stood in for CI
    "ci_failing",           # which checks were red; `ci_status` is the stored verdict
    "ci_unrunnable",        # #628, null on every round that did not establish it
    # ---- Provenance working. `provenance_counts` — the answer — IS stored; these
    # say how it was reached, and #637 wants at least two of them. See the PR body:
    # they are one design job and not this one.
    "fix_range_source",     # #512: `increment` or `compare`
    "provenance_restored",  # #559: what the attribution declined to count, and why
    "fix_range_rebuilt",    # #504: the correspondence a rewritten history forced
    # ---- Scope. `diff_chars` is stored and is scope-dependent, so a consumer
    # comparing it across rounds needs these — which is an argument for storing
    # them, made in its own issue rather than here.
    "scope",                # "pr" or "increment"
    "since_sha",            # the increment's anchor
    # ---- Configuration, as opposed to policy. `review_panel` — the dials as
    # APPLIED — is stored since #643; these two say how the round was set up.
    "rules",                # #305: WHICH LAYER supplied each dial
    "diff_budgets",         # per-seat char budgets; the per-seat answer is on the seat
    "reviewers_ran",        # which seats answered; `review_reviewers` has the rows
})


@pytest.fixture(scope="module")
def panel_source() -> ast.Module:
    return ast.parse(PANEL.read_text(encoding="utf-8"))


def _dict_keys(node: ast.Dict) -> set[str]:
    """The literal string keys of a dict display, ignoring any ``**spread``.

    A spread is not lost: ``_payload_defaults()`` is the only one the panel uses
    here and :func:`panel_payload_keys` reads it directly.
    """
    return {k.value for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def panel_payload_keys(module: ast.Module) -> set[str]:
    """Every top-level key the panel puts on a run payload.

    Four exits build one — the reviewed round, the title-pattern skip, the
    pre-flight refusal and the unconfigured-repo refusal — and all four are read,
    not just the reviewed one. They are not interchangeable: the refusal path sets
    ``round_stop`` by subscript assignment after the literal, and a scan that read
    only dict displays would miss it.
    """
    keys: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            named_payload = (isinstance(target, ast.Name)
                             and target.id.endswith("payload"))
            if named_payload and isinstance(node.value, ast.Dict):
                keys |= _dict_keys(node.value)
            # `refuse_payload["round_stop"] = {...}` — a key added after the
            # literal, and the panel does exactly this on the refusal path.
            if (isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id.endswith("payload")
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)):
                keys.add(target.slice.value)
    return keys | _payload_defaults_keys(module)


def _payload_defaults_keys(module: ast.Module) -> set[str]:
    """The keys of ``panel._payload_defaults()``, which every exit spreads.

    Its own reader because it is a `return {...}`, not an assignment: it is the
    shape a payload has on EVERY non-error exit, so a key that lives only there
    is still a key the board is sent.
    """
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == "_payload_defaults":
            returns = [n for n in ast.walk(node)
                       if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)]
            assert len(returns) == 1, (
                f"_payload_defaults has {len(returns)} dict returns; this scan reads one")
            return _dict_keys(returns[0].value)
    raise AssertionError(f"no def _payload_defaults in {PANEL}")


def review_in_accepts() -> set[str]:
    """Every top-level key ``ReviewIn`` will bind — field names AND aliases.

    The aliases matter: the panel sends the repo slug as ``github`` and the PR
    subject as ``title``, because ``ReviewIn`` takes the panel's words rather than
    making it translate. A check that compared field names alone would report two
    of this model's own accepted fields as drift.
    """
    accepted: set[str] = set()
    for name, info in ReviewIn.model_fields.items():
        accepted.add(name)
        alias = info.validation_alias
        if isinstance(alias, AliasChoices):
            accepted |= {c for c in alias.choices if isinstance(c, str)}
        elif isinstance(alias, str):
            accepted.add(alias)
        if isinstance(info.alias, str):
            accepted.add(info.alias)
    return accepted


def dropped_keys(payload_keys: set[str]) -> set[str]:
    """What ``extra="ignore"`` would discard out of these keys.

    A function rather than an expression inlined into the test, so that
    :func:`test_the_check_catches_an_injected_key` can put a key in front of the
    detector and watch it be reported. A drift check nobody has seen fire is an
    assertion that there is no drift.
    """
    return payload_keys - review_in_accepts()


def test_the_scan_actually_found_the_panels_payloads(panel_source):
    """The scan's own failure mode: match nothing, compare two empty sets, pass.

    A floor and a spot-check rather than an exact count — the count is the thing
    that legitimately moves, and pinning it here would duplicate the drift
    assertion below while failing for a reason that names nothing.
    """
    keys = panel_payload_keys(panel_source)
    assert len(keys) >= 60, f"only {len(keys)} payload keys found — did the scan break?"
    # One key from each of the four exits' literals, one added by subscript on the
    # refusal path, and one that lives only in `_payload_defaults`.
    assert {"github", "pr", "round_stop", "skip_reason", "run_key", "proposals"} <= keys


def test_every_dropped_key_is_one_somebody_decided_to_drop(panel_source):
    """The check itself. Green today, and it is meant to be: the point is what it
    does on the commit that adds the twenty-seventh key.

    Two assertions, not one equality, because the two directions are different
    events with different remedies. A key in the payload and not on the list is
    either a field somebody meant to add here or a key somebody meant to stop
    sending. A key on the list and not in the payload is a list nobody pruned when
    the panel stopped sending it — which matters, because a stale entry is exactly
    what would swallow the key if it ever came back under the same name.
    """
    dropped = dropped_keys(panel_payload_keys(panel_source))
    unexpected = dropped - DROPPED_BY_DESIGN
    assert not unexpected, (
        f"the panel sends {sorted(unexpected)} and ReviewIn does not name them, so "
        f"POST /review discards them in silence. Decide which it is: add a field to "
        f"ReviewIn (with a column and a migration if the board should keep it), stop "
        f"sending the key from harness/loops/panel.py, or add it to "
        f"DROPPED_BY_DESIGN in this file with the reason. What is not an option is "
        f"leaving it — that is #93, #626 and #643, three times over.")
    stale = DROPPED_BY_DESIGN - dropped
    assert not stale, (
        f"DROPPED_BY_DESIGN lists {sorted(stale)}, which the panel no longer sends "
        f"(or which ReviewIn now names). Remove the entries — a stale line here is a "
        f"standing exemption for a key nobody has looked at.")


def test_no_dropped_key_shares_a_name_with_a_column(panel_source):
    """The ``converged`` shape, as a test rather than as a note in an issue.

    That bug was not "a key is dropped" — most of them are, on purpose. It was a
    key being dropped while a column sat waiting for it, so the board had a place
    to put the value, was being sent the value, and stored NULL. This is the only
    pairing in the set that is unambiguously a defect, and it is cheap to check.
    """
    columns = {c.name for c in ReviewRun.__table__.columns}
    collisions = dropped_keys(panel_payload_keys(panel_source)) & columns
    assert not collisions, (
        f"review_runs has columns named {sorted(collisions)} and the panel sends "
        f"keys of the same name that ingest drops — the exact shape of #626. Bind "
        f"them on ReviewIn.")


def test_the_check_catches_an_injected_key(panel_source):
    """Red/green for a check that starts green, done to the detector instead.

    There is no pre-fix state to run this against: nothing dropped a key before
    this file existed, so the honest proof is not "it fails on old code" but "it
    reports a key that is not accepted and is not exempt". Both halves are
    asserted — a detector that flagged everything would pass the first half alone.
    """
    real = panel_payload_keys(panel_source)
    injected = real | {"guard_churn_delta"}
    assert "guard_churn_delta" in dropped_keys(injected)
    assert dropped_keys(injected) - DROPPED_BY_DESIGN == {"guard_churn_delta"}
    # ...and a key ReviewIn DOES name is not reported, so the check is not simply
    # flagging everything it is shown.
    assert dropped_keys(real | {"head_sha", "github"}) == dropped_keys(real)


def test_review_panel_is_bound_and_no_longer_dropped(panel_source):
    """#643's second half: the one key that came off the list rather than onto it.

    Asserted by name because the argument for storing it is specific to it — it is
    the policy ``converged`` was decided under, and two of that flag's conjuncts
    are cut at ``cleared_floor``, which is a function of three dials in this
    object. A round's verdict and the policy it was computed under now sit on the
    same row.
    """
    assert "review_panel" in panel_payload_keys(panel_source)
    assert "review_panel" in review_in_accepts()
    assert "review_panel" in {c.name for c in ReviewRun.__table__.columns}
    assert "review_panel" not in DROPPED_BY_DESIGN

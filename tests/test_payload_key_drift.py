"""A key the panel sends and ``ReviewIn`` does not name is dropped in silence (#643).

``ReviewIn`` is declared ``populate_by_name=True`` with no ``extra=``, so
pydantic v2's default ``extra="ignore"`` applies: a top-level key on the panel's
payload that the model does not name is discarded without a word, without a log
line, and without a 422. That has now happened **six times** — ``head_sha``,
``unread_files``, the provenance pair, ``converged`` (#626), ``review_panel``
(#643), and the provenance working #647 stores: ``rules``, ``fix_range_source``,
``provenance_restored``, ``scope`` and ``since_sha``. Every one of the first five
was found by a human noticing a number was missing, months apart. Nothing failed.

The sixth is the first that was found by this file: #643 wrote the list, #647 read
it and asked which of the twenty-five entries were dropped because somebody decided
to and which were dropped because nobody had looked. Five of them were the second
kind.

There is no seventh. #112 adds four keys to the payload — the harness identity —
and binds all four in the same commit, which is what this file was written to make
the only available move: the drift assertion goes red on the commit that adds an
unbound key, so the choice between "a field somebody meant to add" and "a key
somebody meant to stop sending" gets made while the person adding it is still
holding the question.

This file is what fails.

It reads the top-level keys of every payload ``harness/loops/panel.py`` builds and
compares them against ``ReviewIn``'s field names **and validation aliases** — the
aliases because the panel calls the repo slug ``github`` and the PR subject
``title``, and a check that missed those would report two of its own accepted
fields as drift on the first run. What is left over is what ingest drops. That set
is held against :data:`DROPPED_BY_DESIGN`, a list written out by hand.

**And it reads one tier down (#732), which for a year it did not.** The paragraphs
above used to say "the top-level keys" as a description of scope and it read as a
guarantee. ``round_stop`` is a nested object bound by ``StopIn``, and nothing
compared that model's fields against what ``panel_rounds.round_stop`` nests under
it: the producer returned **24** keys, the model declared **6**, and the other
eighteen were discarded by the same ``extra="ignore"`` on the same
``populate_by_name=True`` config, one indent in from where this file was looking.

#717 is the proof the gap was live rather than theoretical. ``outstanding`` was
sent from #42 and bound by nothing until a human went looking for it — after #643
shipped, with this file green the whole time, found the way all five before it
were found. A check whose scope is a sentence in its own docstring is a check that
covers what somebody remembered to point it at.

So the same three-part apparatus now runs over the nested blocks as well:
:data:`NESTED_BLOCKS` names each payload key this board binds with a model, the
producer's keys are read by ``ast`` from wherever that block is actually built,
and the remainder is held against :data:`NESTED_DROPPED_BY_DESIGN` — written out
by hand, per block, with a reason per key, for the reason the top-level list is.

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
from pydantic import AliasChoices, BaseModel

# The MODULE for everything #732 reaches for, and the names only for what
# predates it, on `tests/test_review_outstanding.py`'s reason: this file is a
# drift check, so the state it most has to work in is the one where the binding
# it is checking for does not exist yet. A `from app.api.reviews import
# STOP_RUNGS` turns that state into a collection error — every test in the file
# red, none of them for the reason the file is about — which is exactly the
# failure mode that makes a red run prove nothing.
from app.api import reviews
from app.api.reviews import ReviewIn
from app.models.review import ReviewRun

REPO_ROOT = Path(__file__).resolve().parent.parent
PANEL = REPO_ROOT / "harness" / "loops" / "panel.py"
#: Where ``round_stop`` actually lives. The panel exports it and builds a payload
#: out of its return, so the keys of the nested block are in a second file and a
#: scan of ``panel.py`` alone would find only the five the refusal path writes by
#: hand — which are all bound, so the check would have passed and covered nothing.
PANEL_ROUNDS = REPO_ROOT / "harness" / "loops" / "panel_rounds.py"

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
    # #665. The register itself is the next ROUND's input, exactly as the two above
    # are, and it is dropped here for their reason. What this board keeps of it is
    # the verdict: `round_stop.declined_outstanding` rides the stored stop, and a
    # cycle ending with declarations outstanding is already unable to store
    # `converged: true`. The named defects reach the board separately, as a
    # `needs-human` post (`panel.announce_declinations`), which is where a question
    # for a person belongs rather than on a review row nobody queries by it.
    "declined",             # key -> {round, reason} a fix pass could not make it
    # #674, and dropped for the same reason as the three above it: it is the next
    # ROUND's input rather than this round's outcome. What the board keeps is the
    # consequence — a retracted declination stops riding `declined_outstanding` and
    # stops costing the stop its `confident`, both of which ARE stored — so the row
    # already says whether the hold is lifted without carrying the register that
    # lifted it.
    "retracted",            # key -> the round a human retracted a declination in
    "unresolved_claims",    # #547's ledger, per claim
    # #718's pair, dropped for the reason every register above it is: they are the
    # next ROUND's input rather than this round's outcome. What the board keeps is
    # the consequence — a declaration somebody answered stops appearing in
    # `stop_veto` and stops costing the stop its `confident`, and both of those ARE
    # stored — so the row already says whether the hold is lifted without carrying
    # the register that lifted it.
    "assessed",             # key -> {round, note, set_by, attested} of an answer
    "coverage_declarations",  # #718's ledger, per declaration
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
    # ---- Provenance working. Four of this tier came OFF the list in #647 —
    # `fix_range_source`, `provenance_restored`, `scope` and `since_sha` — because
    # they say what `provenance_counts` was MEASURED AGAINST. This one is still
    # dropped, and on a narrower argument than "nobody has asked": it is a
    # correspondence between two histories rather than a property of the round's
    # measurement, and #647 stored the four that change what a stored count means.
    "fix_range_rebuilt",    # #504: the correspondence a rewritten history forced
    # ---- Configuration, as opposed to policy. `review_panel` — the dials as
    # APPLIED — is stored since #643 and `rules` — which LAYER supplied each of
    # them — since #647; these two say how the round was set UP, and neither
    # changes the reading of a number on the row.
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


def model_accepts(model: type[BaseModel]) -> set[str]:
    """Every key ``model`` will bind — field names AND validation aliases.

    The aliases matter: the panel sends the repo slug as ``github`` and the PR
    subject as ``title``, because ``ReviewIn`` takes the panel's words rather than
    making it translate. A check that compared field names alone would report two
    of this model's own accepted fields as drift.

    Takes the model rather than closing over ``ReviewIn`` (#732), because the same
    reading is now owed to every nested block this board binds — and the reason
    the nested tier went unchecked for a year is that there was one function here
    and it named one model.
    """
    accepted: set[str] = set()
    for name, info in model.model_fields.items():
        accepted.add(name)
        alias = info.validation_alias
        if isinstance(alias, AliasChoices):
            accepted |= {c for c in alias.choices if isinstance(c, str)}
        elif isinstance(alias, str):
            accepted.add(alias)
        if isinstance(info.alias, str):
            accepted.add(info.alias)
    return accepted


def review_in_accepts() -> set[str]:
    """Every top-level key ``ReviewIn`` will bind. See :func:`model_accepts`."""
    return model_accepts(ReviewIn)


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
    does on the commit that adds the twenty-first key.

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


#: The keys #647 took off :data:`DROPPED_BY_DESIGN`. Written out here as well as
#: removed from the list above, because the two assertions are different: removing
#: an entry says "this is no longer exempt", and this says "it is BOUND" — and a
#: key deleted from the list while nobody added the field fails the drift test with
#: a message about a stale entry, which names the wrong repair.
PROVENANCE_WORKING = ("rules", "fix_range_source", "provenance_restored",
                      "scope", "since_sha")


def test_the_provenance_working_is_bound_and_no_longer_dropped(panel_source):
    """#647: the working behind a count this board already stores.

    Asserted by name for the reason ``review_panel``'s test above is, and the
    reason is specific to each: ``rules`` is the only key of the five that carries
    ``review_panel.escalate_on.fix_injection``, because ``review_panel`` is
    ``Dials.as_dict()`` and ``escalate_on`` is not in it; ``fix_range_source`` and
    ``provenance_restored`` say what ``provenance_counts`` was measured against;
    ``scope`` and ``since_sha`` say what the round reviewed, which is what makes
    the stored ``diff_chars`` comparable across a cycle.

    Four assertions each rather than one, because the halves fail differently. A
    field on ``ReviewIn`` with no column stores nothing; a column with no field is
    the ``converged`` shape exactly; and an entry left on the allow-list would
    swallow the key again if the field were ever removed.
    """
    payload, accepted = panel_payload_keys(panel_source), review_in_accepts()
    columns = {c.name for c in ReviewRun.__table__.columns}
    for key in PROVENANCE_WORKING:
        assert key in payload, f"the panel no longer sends {key}"
        assert key in accepted, f"ReviewIn does not bind {key}"
        assert key in columns, f"review_runs has no column for {key}"
        assert key not in DROPPED_BY_DESIGN, f"{key} is still exempted"


#: The keys #112 added to the payload and bound in the same commit. Written out
#: here for :data:`PROVENANCE_WORKING`'s reason and one more that is specific to
#: this set: these four never appeared on :data:`DROPPED_BY_DESIGN` at all, so
#: there is no removal for a reader to notice, and without this list a commit that
#: deleted the columns would leave the drift test complaining about four
#: *unexpected dropped keys* — true, and naming the repair backwards.
HARNESS_IDENTITY = ("harness_rev", "harness_dirty", "harness_digest", "harness_path")


def test_the_harness_identity_is_bound_and_no_longer_dropped(panel_source):
    """#112: which harness produced the round, on the row with the round.

    Four keys and not one, because the question has no single true answer from
    inside a running panel: ``harness_rev`` names a commit and is null on every
    installed harness, ``harness_digest`` is a content proxy that is always there
    and can only say "same code or not", ``harness_path`` says whether the round
    came from the deployed harness at all, and ``harness_dirty`` is what stops a
    rev being read as more than it is. A single field would have to be one of
    those, and each of them is silent in a case the others cover.

    Four assertions each, for the reason the two tests above give: a field with no
    column stores nothing, a column with no field is the ``converged`` shape
    exactly, and an entry on the allow-list would swallow the key again if the
    field were ever removed.
    """
    payload, accepted = panel_payload_keys(panel_source), review_in_accepts()
    columns = {c.name for c in ReviewRun.__table__.columns}
    for key in HARNESS_IDENTITY:
        assert key in payload, f"the panel no longer sends {key}"
        assert key in accepted, f"ReviewIn does not bind {key}"
        assert key in columns, f"review_runs has no column for {key}"
        assert key not in DROPPED_BY_DESIGN, f"{key} is exempted rather than stored"


# --------------------------------------------------------------------------- #
# One tier down (#732)
#
# Everything above reads the payload's top level. `round_stop` is an object on it,
# `StopIn` binds it, and until this section nothing anywhere compared that model's
# fields against what `panel_rounds.round_stop` nests inside it — so the class #643
# closed was still open one indent in, on eighteen live keys.
# --------------------------------------------------------------------------- #

#: The payload keys this board binds with a model of their own, and the model.
#:
#: Written out rather than discovered from ``ReviewIn.model_fields`` by looking for
#: ``BaseModel`` annotations, on :data:`DROPPED_BY_DESIGN`'s rule one level up: a
#: computed list passes forever. A block added to ``ReviewIn`` and left out here
#: would be a nested tier nobody checks, which is the exact state ``round_stop``
#: was in — and this file's whole argument is that the way past a drift check has
#: to be a line somebody types in a diff somebody reviews.
#:
#: ``reviewers`` and the three list-element models (``FindingIn``, ``ReportIn``,
#: ``ChangedFileIn``) are deliberately absent and it is a scope decision rather
#: than an oversight. Their producers are not a dict literal under a known payload
#: key — a finding is assembled across several sites and a changed file comes off
#: ``gh``'s own JSON — so reading their keys needs a producer scan that does not
#: exist yet, where these three needed none. ``round_stop`` is where the eighteen
#: live drops were measured and is the block this issue is about; starting with
#: the measured one is the answer #732 asks for and this comment is the record of
#: what it leaves.
NESTED_BLOCKS = {
    "round_stop": reviews.StopIn,
    "code_access": reviews.CodeAccessIn,
    "pr_claim": reviews.PrClaimIn,
}

#: Keys the panel nests inside a payload block and this board does not bind.
#: Per block, and — like :data:`DROPPED_BY_DESIGN` — **written out, not computed**,
#: with the reason on each line. The test reads the union of one block's set.
#:
#: Two blocks are absent from this mapping and that is the assertion: ``code_access``
#: and ``pr_claim`` send exactly what their models bind, and
#: :func:`test_a_block_with_nothing_dropped_says_so_by_having_no_entry` is what
#: stops an empty set here from being mistaken for a block nobody checked.
NESTED_DROPPED_BY_DESIGN = {
    "round_stop": frozenset({
        # ---- The DISPOSAL, published a second time under a flatter name. Each of
        # these three is `round_stop.outstanding.<bucket>` off the same local, so
        # the two cannot disagree by construction — and `outstanding_counts` stores
        # the length of every one of the five buckets (#717). A column here would
        # be one number in two places with two chances to drift.
        "escalated_outstanding",   # == outstanding.escalated, from `blocking`
        "declined_outstanding",    # == outstanding.declined, from `unfixed`
        "narrowed",                # == outstanding.narrowed, from `narrowed_cleared`
        # ---- Already on the row, by another route. `round` is sent at the TOP
        # level as well and `ReviewIn` binds it there into `review_runs.round`;
        # this is the same integer one indent in.
        "round",
        # ---- Configuration, and `review_panel` has held it verbatim since #643.
        # `max_rounds` is `Dials.max_rounds` and `trigger_floor` is
        # `Dials.round_trigger_floor` — the call site in `panel.py` passes exactly
        # those two attributes, so a second copy would be one dial in two places,
        # free to disagree about the policy one round ran under.
        #
        # `cleared_floor` is NOT here and the difference is the whole of why this
        # pair is: it is a `Dials` *property*, derived from three dials, and the
        # board must not re-derive a policy it holds as opaque JSON. A value the
        # producer computed is the producer's answer; a value the board would have
        # to compute is a second reading of the rules. See `StopIn.cleared_floor`.
        "max_rounds",
        "trigger_floor",
    }),
}


@pytest.fixture(scope="module")
def panel_rounds_source() -> ast.Module:
    return ast.parse(PANEL_ROUNDS.read_text(encoding="utf-8"))


def _round_stop_return_keys(module: ast.Module) -> set[str]:
    """The keys ``panel_rounds.round_stop`` returns.

    Its own reader, for :func:`_payload_defaults_keys`' reason and one more: this
    block is not a dict literal sitting in a payload at all. The panel calls the
    function and assigns its return, so a scan of payload dict displays sees the
    key ``round_stop`` and no keys under it.

    The single-return assertion is the scan's own failure mode written down: a
    second ``return {...}`` added to this function would be a second shape of the
    same block, and a reader that took the first would check half of it while
    reading as though it had checked all of it.
    """
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == "round_stop":
            returns = [n for n in ast.walk(node)
                       if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)]
            assert len(returns) == 1, (
                f"round_stop has {len(returns)} dict returns; this scan reads one")
            return _dict_keys(returns[0].value)
    raise AssertionError(f"no def round_stop in {PANEL_ROUNDS}")


def nested_block_keys(module: ast.Module, block: str) -> set[str]:
    """Every key the panel nests under ``block`` in a payload it builds.

    Two shapes, both of which the panel uses and neither of which the other's
    reader would find:

    * ``"code_access": {...}`` — a dict literal as the value of the block's key,
      which is how the reviewed exit and ``_payload_defaults`` both build one;
    * ``refuse_payload["round_stop"] = {...}`` — a dict assigned to the key by
      subscript after the payload literal, which is what the spend-ceiling
      refusal does and what :func:`panel_payload_keys` already reads one tier up
      for exactly this reason.

    The union of every site, not the first: the skip payloads and the reviewed
    payload build the same block with the same keys, and a block that ever
    carries a key is a block ingest is ever sent one on.
    """
    keys: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == block
                        and isinstance(value, ast.Dict)):
                    keys |= _dict_keys(value)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == block):
                    keys |= _dict_keys(node.value)
    return keys


def block_keys(block: str, panel: ast.Module, panel_rounds: ast.Module) -> set[str]:
    """Every key the panel puts inside one nested payload block, from every
    producer of it."""
    keys = nested_block_keys(panel, block)
    if block == "round_stop":
        keys |= _round_stop_return_keys(panel_rounds)
    return keys


def nested_dropped_keys(block: str, keys: set[str]) -> set[str]:
    """What ``extra="ignore"`` would discard out of one block's keys.

    A function rather than an expression inlined into the test, for
    :func:`dropped_keys`' reason: :func:`test_the_nested_check_catches_an_injected_key`
    puts a key in front of the detector and watches it be reported, and a drift
    check nobody has seen fire is an assertion that there is no drift.
    """
    return keys - model_accepts(NESTED_BLOCKS[block])


def test_the_nested_scan_actually_found_the_blocks(panel_source, panel_rounds_source):
    """The nested scan's own failure mode, and it is the top-level scan's exactly:
    match nothing, compare two empty sets, pass.

    A floor and a spot-check per block rather than an exact count, for
    :func:`test_the_scan_actually_found_the_panels_payloads`' reason — the count is
    what legitimately moves, and pinning it here would duplicate the drift
    assertion below while failing for a reason that names nothing.
    """
    stop = block_keys("round_stop", panel_source, panel_rounds_source)
    assert len(stop) >= 20, f"only {len(stop)} round_stop keys found — scan broken?"
    # One key from the verdict, one from the disposal, one rung, one floor, and one
    # that only the hand-built refusal payload in panel.py carries.
    assert {"stop", "outstanding", "guard_churn", "cleared_floor", "veto"} <= stop
    assert {"setting", "seats", "convention_files_removed"} <= block_keys(
        "code_access", panel_source, panel_rounds_source)
    assert {"setting", "sent"} <= block_keys(
        "pr_claim", panel_source, panel_rounds_source)


@pytest.mark.parametrize("block", sorted(NESTED_BLOCKS))
def test_every_dropped_nested_key_is_one_somebody_decided_to_drop(
        block, panel_source, panel_rounds_source):
    """#643's check, one tier down. Green today, and it is meant to be: the point
    is what it does on the commit that nests a new key under one of these blocks.

    Two assertions and not one equality, for the reason the top-level check gives:
    a key in the block and not on the list is either a field somebody meant to add
    to the model or a key somebody meant to stop sending, while a key on the list
    and not in the block is a list nobody pruned — and a stale entry is exactly
    what would swallow the key if it came back under the same name.
    """
    exempt = NESTED_DROPPED_BY_DESIGN.get(block, frozenset())
    dropped = nested_dropped_keys(
        block, block_keys(block, panel_source, panel_rounds_source))
    unexpected = dropped - exempt
    assert not unexpected, (
        f"the panel nests {sorted(unexpected)} inside {block!r} and "
        f"{NESTED_BLOCKS[block].__name__} does not name them, so POST /review "
        f"discards them in silence. Decide which it is: add a field to that model "
        f"(with a column and a migration if the board should keep it), stop "
        f"sending the key from harness/loops/, or add it to "
        f"NESTED_DROPPED_BY_DESIGN[{block!r}] in this file with the reason. What "
        f"is not an option is leaving it — that is #626, #643 and #717, and #717 "
        f"is the one that got past this very file.")
    stale = exempt - dropped
    assert not stale, (
        f"NESTED_DROPPED_BY_DESIGN[{block!r}] lists {sorted(stale)}, which the "
        f"panel no longer nests there (or which "
        f"{NESTED_BLOCKS[block].__name__} now names). Remove the entries — a "
        f"stale line here is a standing exemption for a key nobody has looked at.")


def test_a_block_with_nothing_dropped_says_so_by_having_no_entry():
    """``NESTED_DROPPED_BY_DESIGN`` may not name a block ``NESTED_BLOCKS`` does not.

    The failure this stops is the quiet one. An entry for a block that is no
    longer checked — renamed, or taken out of ``NESTED_BLOCKS`` — is a written-down
    exemption pointing at nothing, and the parametrised test above would never run
    it, so it would sit there reading like coverage.
    """
    orphans = set(NESTED_DROPPED_BY_DESIGN) - set(NESTED_BLOCKS)
    assert not orphans, (
        f"NESTED_DROPPED_BY_DESIGN exempts keys inside {sorted(orphans)}, which "
        f"NESTED_BLOCKS does not check. Either the block belongs in NESTED_BLOCKS "
        f"or its exemptions belong in the bin.")


def test_the_nested_check_catches_an_injected_key(panel_source, panel_rounds_source):
    """Red/green for a check that starts green, done to the detector — the shape
    ``test_the_check_catches_an_injected_key`` uses one tier up and for its reason.

    There is no pre-fix state to run the nested check against: before #732 nothing
    read this tier at all, so the honest proof is not "it fails on old code" but
    "it reports a key that is not accepted and is not exempt". Both halves are
    asserted, because a detector that flagged everything would pass the first
    half on its own.
    """
    real = block_keys("round_stop", panel_source, panel_rounds_source)
    injected = real | {"guard_churn_ceiling"}
    assert "guard_churn_ceiling" in nested_dropped_keys("round_stop", injected)
    assert (nested_dropped_keys("round_stop", injected)
            - NESTED_DROPPED_BY_DESIGN["round_stop"]) == {"guard_churn_ceiling"}
    # ...and a key `StopIn` DOES name is not reported, so the check is not simply
    # flagging everything it is shown.
    assert nested_dropped_keys(
        "round_stop", real | {"converged", "outstanding"}) == nested_dropped_keys(
        "round_stop", real)
    # ...and the same, done to a block whose exemption list is empty: an injected
    # key must be reported there too, or "no drops" would be indistinguishable
    # from "nothing looked".
    claim = block_keys("pr_claim", panel_source, panel_rounds_source)
    assert nested_dropped_keys("pr_claim", claim | {"judge_sent"}) == {"judge_sent"}


def test_no_dropped_nested_key_is_a_column_nothing_else_fills(
        panel_source, panel_rounds_source):
    """The ``converged`` shape, one tier down — a key dropped while a column sits
    waiting for it.

    **Narrowed by what fills the column, which the top-level version of this test
    does not have to do.** ``round`` is nested inside ``round_stop``, is dropped
    there on purpose, and ``review_runs.round`` exists — and that is correct,
    because the panel also sends ``round`` at the TOP level and ``ReviewIn`` binds
    it there. Subtracting the top-level payload keys is what tells a nested key
    whose column is filled from a key whose column is not. Without it this test
    would demand a second binding for a value already stored, which is the repair
    named backwards.
    """
    columns = {c.name for c in ReviewRun.__table__.columns}
    filled_elsewhere = panel_payload_keys(panel_source)
    for block in NESTED_BLOCKS:
        dropped = nested_dropped_keys(
            block, block_keys(block, panel_source, panel_rounds_source))
        collisions = (dropped & columns) - filled_elsewhere
        assert not collisions, (
            f"review_runs has columns named {sorted(collisions)} and the panel "
            f"nests keys of the same name inside {block!r} that ingest drops, "
            f"with nothing at the top level filling them — the exact shape of "
            f"#626. Bind them on {NESTED_BLOCKS[block].__name__}.")


#: The three ``round_stop`` keys #732 bound to a column of their own. Written out
#: here as well as removed from the exemption list above, for
#: :data:`PROVENANCE_WORKING`'s reason: removing an entry says "this is no longer
#: exempt", and this says "it is BOUND AND STORED" — and a key deleted from the
#: list while nobody added the field fails the drift test with a message about a
#: stale entry, which names the wrong repair.
STOP_FLOORS = ("cleared_floor", "new_below_trigger_floor",
               "repeated_below_trigger_floor")


def test_the_stop_floors_are_bound_and_no_longer_dropped(
        panel_source, panel_rounds_source):
    """#732's three scalars: the cut a stored disposal is split at, and the two
    counts the trigger floor turned away.

    Asserted by name for the reason ``review_panel``'s test above is, and the
    reason is specific to each. ``cleared_floor`` is where
    ``outstanding_counts.fixable`` and ``.below_floor`` are divided, so a reader
    holding those two numbers could not say what either meant; the other two are
    the findings that were in the round's buckets and bought no round, which is
    the difference #710 had to reassemble by hand to calibrate a trigger floor.

    Four assertions each rather than one, because the halves fail differently. A
    field on ``StopIn`` with no column stores nothing; a column with no field is
    the ``converged`` shape exactly; and an entry left on the exemption list would
    swallow the key again if the field were ever removed.
    """
    sent = block_keys("round_stop", panel_source, panel_rounds_source)
    accepted = model_accepts(reviews.StopIn)
    columns = {c.name for c in ReviewRun.__table__.columns}
    for key in STOP_FLOORS:
        assert key in sent, f"the panel no longer nests {key} in round_stop"
        assert key in accepted, f"StopIn does not bind {key}"
        assert key in columns, f"review_runs has no column for {key}"
        assert key not in NESTED_DROPPED_BY_DESIGN["round_stop"], (
            f"{key} is still exempted")
    # ...and the two of the three that ingest reports a shape fault on are the two
    # `BELOW_TRIGGER_FLOOR` names, so the drop signal and the columns cannot come
    # apart. `cleared_floor` is not among them: it is one word, not a key list.
    assert set(reviews.BELOW_TRIGGER_FLOOR) == set(STOP_FLOORS) - {"cleared_floor"}


def test_the_stop_rungs_are_bound_and_land_in_one_column(
        panel_source, panel_rounds_source):
    """#732's nine measurement blocks — the eight ``escalate_on`` rungs and the
    premise brake's state.

    **Nine fields and ONE column, which is why this is a separate test from the
    three floors above.** Each rung is a measurement beside its own verdict, and
    which scalar out of each matters is what #710 has yet to answer; a column per
    rung would be this board answering it from a position of never having held the
    numbers. So the assertion is not "each key has a column" — it is that each key
    is a field somebody named, that no key is quietly exempt, and that
    ``reviews.STOP_RUNGS`` (what ``record_review`` assembles) and the
    fields on ``StopIn`` are the same nine.

    That last one is the pairing this test exists for. A rung declared on the
    model and left out of ``STOP_RUNGS`` binds and is never stored — the field
    would satisfy the drift check above while the value went nowhere, which is a
    silent drop wearing the shape of a fix.
    """
    sent = block_keys("round_stop", panel_source, panel_rounds_source)
    accepted = model_accepts(reviews.StopIn)
    assert "stop_rungs" in {c.name for c in ReviewRun.__table__.columns}
    for key in reviews.STOP_RUNGS:
        assert key in sent, f"the panel no longer nests {key} in round_stop"
        assert key in accepted, f"StopIn does not bind {key}"
        assert key not in NESTED_DROPPED_BY_DESIGN["round_stop"], (
            f"{key} is exempted rather than stored")
    # Both directions. A field named on the model and missing from `STOP_RUNGS`
    # would be bound, checked and never written.
    assert set(reviews.STOP_RUNGS) <= accepted
    assert set(reviews.StopIn().rungs()) == set(reviews.STOP_RUNGS)

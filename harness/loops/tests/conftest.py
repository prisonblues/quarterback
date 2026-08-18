"""One `gh` double that knows every call `panel.py` makes.

There was no conftest here, so the `panel.sh` stub was hand-rolled in thirteen
modules and **every module had to know about every `gh` call panel.py makes**.
Adding a call therefore meant editing thirteen stubs, and the failure when you
did not was silent: an unanswered call does not raise, it falls through to
whatever the stub returns for everything else, `json.loads` fails inside the
helper's own `except`, and the round carries on with a degraded value plus a
`config_notes` entry nobody asserts on. A green suite, reviewing a panel that
quietly could not read half of what it asked for.

That has now happened three times — the `compare` call, then the base-tip read.
The third is the measured one: `_base_tip_now` landed with exactly one module
swept, and **48 tests across 6 modules** then spent hours emitting *"the tip of
base branch could not be read"* about a failure that never happened. Nothing was
red. It was found by instrumenting the note to raise, not by a test (128-F09).

So the stub lives here and knows the whole surface. Adding a `gh` call to
panel.py means teaching :func:`gh_stub` about it once — and if you forget,
`strict=True` (the default) raises on the unknown call instead of letting it rot
into a plausible answer.

**The six calls panel.py makes**, all through ``panel.sh``:

===========================================  ===========================
call                                          answered by
===========================================  ===========================
``gh pr view … --json title,additions,…``    ``meta``
``gh pr view … --json headRefOid``           ``head`` / ``head_moves_to``
``gh pr view … --json baseRefOid``           ``merge_base`` / ``…_after``
``gh api repos/…/git/ref/heads/…``           ``base_tip``
``gh api repos/…/compare/a...b``             ``compare`` / ``compare_diff``
``gh pr diff …``                             ``diff``
===========================================  ===========================

This is deliberately a plain factory rather than a fixture: the modules here set
``panel.sh`` inside each test, often several times per test with different
answers, and a fixture cannot see those. Call it and hand the result to
``monkeypatch.setattr(panel, "sh", …)``.
"""

import json
import sys
from pathlib import Path

import pytest

# The modules under test are scripts in the parent directory, not an installed
# package. Every test module here inserts that directory itself; doing it HERE too
# is what makes the insert a property of the package rather than of whichever
# module pytest happened to collect first — `import panel` below used to work only
# because some other module's insert had already run, so selecting a single node
# (`pytest tests/test_x.py::test_y`) in a module that does not insert could raise
# ModuleNotFoundError out of a fixture that has nothing to do with the panel.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402


@pytest.fixture
def every_seat_installed(monkeypatch):
    """Pin every seat as present on this box (#222).

    `budgets` is built from the seats this host can actually RUN, not merely the
    configured ones — a seat with no CLI cannot be handed a diff, so it must not
    acquire a budget, an argv clamp, a `config_notes` line, or a truncation
    record. That makes `seat_installed` a PATH read on the critical path of every
    round, and therefore a test-outcome dependency on which vendor CLIs the
    machine running the suite happens to carry.

    Left unpinned, nine tests across `test_panel_scope.py` and
    `test_panel_provenance.py` fail on a CI runner (which carries none of the
    four) and pass on a workstation (which carries some) — while testing budgets,
    scope and truncation, none of which is about host capability. And they fail
    through a test-double artefact rather than a real state: those suites replace
    `review_llm` wholesale, so a seat "runs" without ever reaching `run_seat`'s
    absence check and lands in `ran_names` with no budget. Production cannot
    produce that pairing, because `run_seat` refuses an absent seat before it runs.

    **Requested by module, not autouse.** It was autouse over the whole package
    first, and that is the wrong default here: a pin that applies to tests nobody
    chose it for turns every absence assertion in the directory into a presence
    assertion, silently and without ever failing first. `test_panel_absent_seat.py`
    is the module whose subject IS absence; a blanket pin would have it asserting
    the opposite of what it says. So the three modules that need a host stated say so
    at the top (`pytestmark = pytest.mark.usefixtures(...)`), which is greppable,
    and a new test that quietly depends on the host fails on a bare runner rather
    than passing there for the wrong reason.

    **Patched on `panel` only — deliberately NOT on `panel_seats`.** `budgets` is
    the consumer this restores; `run_seat`'s own check is a real safety mechanism
    in a test suite, because it is what stops a test reaching
    `subprocess.run(["agy", ...])` for a binary this box does not have. Forcing it
    True hangs the run: the exec fails and the seat retries with backoff, on every
    test that dispatches. Not hypothetical — it is what the first version of this
    fixture did.

    The two therefore disagree here, and only here. In production they cannot.
    """
    # No `raising=False`. The attribute is guaranteed to exist for this fixture to
    # have a purpose, and tolerating its absence is how the pin fails OPEN: rename
    # `seat_installed`, or drop the star-import that puts it in `panel`'s globals,
    # and `setattr` becomes a silent no-op that hands every dependent test back to
    # the host's PATH with nothing anywhere reporting it.
    monkeypatch.setattr(panel, "seat_installed", lambda name: True)

#: Sentinel for "the caller said nothing", so a test can ask for a value that is
#: genuinely ``None`` — a read that FAILED — as distinct from not specifying one.
UNSET = object()

#: A one-file PR diff, in the shape `_diff_added_lines` reads.
PR_DIFF = ("diff --git a/a.py b/a.py\n"
           "index 1111111..2222222 100644\n"
           "--- a/a.py\n"
           "+++ b/a.py\n"
           "@@ -1,0 +1,1 @@\n"
           "+first\n")

DEFAULT_HEAD = "aaa1110000000000000000000000000000000000"
DEFAULT_MERGE_BASE = "0ddba5e00000000000000000000000000000000"
DEFAULT_BASE_TIP = "beef00010000000000000000000000000000000"


class _Meta(dict):
    """A metadata body that is already complete.

    :func:`gh_stub` merges a plain mapping over :func:`pr_meta`'s defaults, which
    is the convenience most callers want — but it makes an explicit OMISSION
    impossible to express, because the default puts the key straight back. That
    matters for `baseRefOid`: leaving it out is what a `gh` too old to know the
    field does, and it is a case with its own test. So a body built by `pr_meta`
    is marked, and used exactly as given.
    """


def pr_meta(title="feat: a thing", *, additions=20, deletions=2,
            base_ref="main", head_ref="feat/x", head=DEFAULT_HEAD,
            merge_base=DEFAULT_MERGE_BASE, files=UNSET, changed_files=UNSET,
            state="OPEN", draft=False, **extra):
    """The opening metadata read, as `gh pr view --json …` returns it.

    `merge_base=None` omits `baseRefOid` entirely rather than sending null —
    that is what a `gh` too old to know the field actually does, and the two are
    not the same thing to the code reading it.
    """
    meta = {"title": title, "additions": additions, "deletions": deletions,
            "baseRefName": base_ref, "headRefName": head_ref,
            "headRefOid": head, "state": state, "isDraft": draft}
    if merge_base is not None:
        meta["baseRefOid"] = merge_base
    if files is not UNSET:
        meta["files"] = files
    if changed_files is not UNSET:
        meta["changedFiles"] = changed_files
    meta.update(extra)
    return _Meta(meta)


def gh_stub(*, meta=UNSET, diff=PR_DIFF, base_tip=DEFAULT_BASE_TIP,
            head=UNSET, head_moves_to=None, merge_base=UNSET,
            merge_base_after=UNSET, compare=UNSET, compare_diff="",
            calls=None, strict=True):
    """A `panel.sh` double answering every `gh` call panel.py makes.

    Every answer takes a value, a `BaseException` to raise, or None meaning "the
    call came back empty" — because each of those is a distinct thing the code
    under test is supposed to survive differently, and a stub that cannot express
    the difference is why the failures above went unnoticed.

    Args:
      meta: mapping for the opening read. A plain dict is merged over
        :func:`pr_meta`'s defaults; a `pr_meta(...)` body is used as given, so a
        key it deliberately omits stays omitted.
      head_moves_to: makes the head move mid-round — the race the re-read exists
        for. It answers the single-field `headRefOid` read, which is
        `_head_sha_now`; the opening read has already given the original head, so
        this applies from the first such read rather than the second.
      merge_base_after: what the `baseRefOid` re-read answers once the head has
        moved. Defaults to the unchanged `merge_base` (the no-op path).
      calls: a list to append every argv to, for tests asserting on what was asked.
      strict: raise on a `gh` call this stub does not recognise. Leave it on. The
        whole point of this file is that an unanswered call must be loud.

    One limit, stated because a guard you trust further than it goes is worse
    than none: `strict` raises `AssertionError`, so it is caught by anything
    catching it. Every reader in panel.py today uses a narrow tuple
    (`OSError, subprocess.SubprocessError, ValueError, AttributeError`) and an
    untaught call is loud through all of them — verified by adding one, guarded
    and unguarded: 57 tests red either way. A future reader catching bare
    `Exception` would swallow it and be silent again. Catch narrowly.
    """
    # A plain mapping is merged over the defaults (the common case: name the two
    # fields your test cares about). A `pr_meta(...)` body is already complete and
    # is used as given, so an omitted key stays omitted.
    if meta is UNSET:
        base = pr_meta()
    elif isinstance(meta, _Meta) or not isinstance(meta, dict):
        base = meta
    else:
        base = _Meta({**pr_meta(), **meta})
    the_head = base.get("headRefOid", DEFAULT_HEAD) if head is UNSET else head
    the_merge_base = (base.get("baseRefOid") if merge_base is UNSET else merge_base)
    head_reads = []

    def answer(value):
        if isinstance(value, BaseException):
            raise value
        return value

    def sh(args, **kw):
        if calls is not None:
            calls.append(args)
        if isinstance(base, BaseException) and args[:3] == ["gh", "pr", "view"]:
            raise base

        if args[:3] == ["gh", "pr", "view"]:
            field = args[-1]
            # `_head_sha_now` and `_merge_base_now` each ask for ONE field; the
            # opening read asks for a comma list. That is the only thing telling
            # them apart, and it is what panel.py actually sends.
            if field == "headRefOid":
                # The opening metadata read already answered with the ORIGINAL
                # head; this single-field read is `_head_sha_now`, which exists
                # precisely to notice a move. So `head_moves_to` applies from the
                # first such read — counting "the second gh pr view" instead
                # would mean the move was never detected and a test asking for
                # one would silently exercise the unmoved path.
                head_reads.append(args)
                moved = head_moves_to if head_moves_to is not None else the_head
                return json.dumps({} if moved is None else {"headRefOid": answer(moved)})
            if field == "baseRefOid":
                after = the_merge_base if merge_base_after is UNSET else merge_base_after
                return json.dumps({} if after is None else {"baseRefOid": answer(after)})
            return json.dumps(base)

        if args[:3] == ["gh", "pr", "diff"]:
            return answer(diff)

        if args[:2] == ["gh", "api"]:
            path = args[2]
            if "/git/ref/heads/" in path:
                tip = answer(base_tip)
                if tip is None:
                    return json.dumps({})
                # Already a body (a test pinning a malformed shape) or a bare sha.
                return tip if isinstance(tip, str) and tip.lstrip().startswith("{") \
                    else json.dumps({"object": {"sha": tip, "type": "commit"}})
            if "/compare/" in path:
                # Two callers, told apart by how they ask: the diff media type
                # wants a raw diff, `--jq` wants a JSON body.
                if "Accept: application/vnd.github.diff" in args:
                    return answer(compare_diff)
                return answer("" if compare is UNSET else compare)

        if strict:
            raise AssertionError(
                f"gh_stub does not know this call: {args!r}\n"
                "panel.py gained a `gh` call and this stub was not taught it. "
                "Teach it in harness/loops/tests/conftest.py rather than letting "
                "it fall through — an unanswered call degrades a round silently "
                "and no test goes red (128-F09).")
        return ""

    return sh

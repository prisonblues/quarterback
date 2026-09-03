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

**The seven calls panel.py makes**, all through ``panel.sh``:

=============================================  ===========================
call                                            answered by
=============================================  ===========================
``gh pr view … --json title,additions,…``      ``meta``
``gh pr view … --json headRefOid``             ``head`` / ``head_moves_to``
``gh pr view … --json mergeable``              ``mergeable_now``
``gh api repos/…/compare/… --jq .merge_base…`` ``fork_point`` / ``…_after``
``gh api repos/…/git/ref/heads/…``             ``base_tip``
``gh api repos/…/compare/a...b --jq {status…`` ``compare`` / ``compare_diff``
``gh pr diff …``                               ``diff``
=============================================  ===========================

Two of those are the SAME endpoint told apart by their ``--jq``, which is how
panel.py itself tells them apart: one asks the compare API for a fork point
(:func:`panel_scope._merge_base_now`, #241) and the other for a range of file
patches (``_fix_range_diff``). There used to be a ``gh pr view --json baseRefOid``
here instead of the first — that read is the defect #241 is about, because
``baseRefOid`` is GitHub's stored base and not a merge base.

This is deliberately a plain factory rather than a fixture: the modules here set
``panel.sh`` inside each test, often several times per test with different
answers, and a fixture cannot see those. Call it and hand the result to
``monkeypatch.setattr(panel, "sh", …)``.

THE OTHER DOUBLE, AND ITS ONE REQUIRED KEY: ``load_repo_cfg``.

Most modules here replace ``panel.load_repo_cfg`` with a hand-written cfg literal,
and every one of those literals carries ``"_rules_baseline"``. That field is
``resolve_repo``'s statement of WHICH file supplied the baseline
(``.harness-rules.sample``, ``.harness-rules``, or ``""`` for "there was no rules
file at all"), and ``run()`` and ``ask()`` REFUSE to review a repo whose answer is
``""`` — a defaults-only review is a review nobody configured, and its findings then
brief a fixer that edits the repo.

So a cfg literal without the key is a double claiming something its original cannot
say, and the refusal it triggers is the test's own fixture being wrong rather than
the code under test. Set it to ``".harness-rules.sample"`` for an ordinary repo, and
to ``""`` deliberately when the refusal IS the subject (see
``test_panel_merge.UNCONFIGURED_CFG``). This is the same lesson as the ``gh`` stub
above, one seam over: a double that does not know the whole contract fails green.
"""

import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

# The modules under test are scripts in the parent directory, not an installed
# package. Every test module here inserts that directory itself; doing it HERE too
# is what makes the insert a property of the package rather than of whichever
# module pytest happened to collect first — `import panel` below used to work only
# because some other module's insert had already run, so selecting a single node
# (`pytest tests/test_x.py::test_y`) in a module that does not insert could raise
# ModuleNotFoundError out of a fixture that has nothing to do with the panel.
# THE SUITE DOES NOT TALK TO A BOARD. `resolve_repo` reads the dial layer (#305)
# from `GET /dials`, and `QUARTERBACK_DIALS` set AT ALL — the empty string
# included — is the offline switch: the variable becomes the whole layer, so this
# is "there are no dials" rather than "ask, and hope". Without it a checkout whose
# host is enrolled on a board would make a live HTTP call per resolution and the
# same test would mean different things on two machines, which is the leak
# `tests/conftest.py` pins every other setting to close.
#
# `setdefault`, so a caller that exported its own body still gets it, and
# monkeypatch still wins inside a test.
os.environ.setdefault("QUARTERBACK_DIALS", "")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — the `gh` seam every stub here replaces
import panel_seats  # noqa: E402  — where `record_run` is defined


@pytest.fixture(autouse=True)
def every_seat_installed(monkeypatch):
    """Pin every seat as present on this box (#222).

    `budgets` is built from the seats this host can actually RUN, not merely the
    configured ones — a seat with no CLI cannot be handed a diff, so it must not
    acquire a budget, an argv clamp, a `config_notes` line, or a truncation record.
    That makes `seat_installed` a PATH read on the critical path of every round, and
    therefore a test-outcome dependency on which vendor CLIs the machine running the
    suite happens to carry.

    Left unpinned, tests across `test_panel_scope`, `test_panel_provenance`,
    `test_panel_argv_limit`, `test_panel_declarations` and `test_panel_code_access`
    fail on a CI runner (which carries none of the four) and pass on a workstation
    (which carries some) — while testing budgets, scope, prompts and truncation,
    none of which is about host capability. They fail through a test-double artefact
    rather than a real state: those suites replace `review_llm` wholesale, so a seat
    "runs" without reaching `run_seat`'s absence check. Production cannot produce
    that pairing, because `run_seat` refuses an absent seat before it runs.

    **Autouse, and it was opt-in first.** Opt-in is the tidier argument — a pin
    nobody chose could turn an absence assertion into a presence assertion — but it
    put the cost on the wrong people. Three of the five modules above arrived from
    other branches while this was in review, and each landed green on its author's
    workstation and red here, for a reason having nothing to do with their work.

    Autouse puts that cost back on this feature. The failure mode it risks is
    confined to one module — `test_panel_absent_seat.py`, the only one whose subject
    IS absence — and that module does not rely on this default: its `_host()` helper
    states the host per test, for every module the predicate is resolved through, and
    a fixture runs before the test body that overrides it. Its predicate tests patch
    `panel_core.shutil.which` directly, and its reader tests never touch PATH.

    **On `panel` only — deliberately not on `panel_seats`.** `budgets` is the
    consumer this restores; `run_seat`'s own check is a real safety mechanism in a
    test suite, because it is what stops a test reaching `subprocess.run(["agy",
    ...])` for a binary this box does not have. Forcing it True hangs the run: the
    exec fails and the seat retries with backoff, on every test that dispatches.
    Not hypothetical — it is what the first version of this fixture did.
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


@pytest.fixture(autouse=True)
def _no_real_tarball_fetch(monkeypatch):
    """Every test gets a PR tarball without touching the network.

    Autouse, and that is the point rather than convenience. `sh_bytes` is a SECOND
    place the panel shells out to `gh`, so every suite that stubs `panel_core.sh`
    and nothing else — most of them, each with its own hand-rolled `fake_sh` — left
    this one live. `reviewer_code_access` defaults on, so every `run()` test reached
    it: the suite kept passing, quietly made a real API call per test, and went from
    7 seconds to 30. A hole that makes tests slower and network-dependent while
    staying green is the shape that survives review, so it is closed here, once, for
    everything.

    A test that wants a fetch FAILURE overrides this itself — `monkeypatch` inside
    the test runs after the fixture and wins."""
    monkeypatch.setattr(panel_core, "sh_bytes", lambda *a, **k: pr_tarball())


def pr_tarball(files=None, prefix="acme-board-aaa1110"):
    """A gzipped tar shaped like GitHub's `repos/{repo}/tarball/{sha}` response: one
    top-level `owner-repo-sha/` directory with the tree inside it.

    The wrapper directory is the part worth reproducing rather than simplifying
    away — `fetch_pr_tree` unwraps it, and a stub that skipped it would let a bug in
    that unwrapping pass every test while every real run came out rooted one
    directory off, with every path a reviewer quotes subtly wrong.

    `files` maps repo-relative path to text. The default is ORDINARY source and
    deliberately carries no vendor instruction file: the strip reports what it
    removed into `config_notes`, so a default that shipped an `AGENTS.md` would put
    a note on every run() test in the suite and break the several that assert
    `config_notes == []` about something else entirely. A test exercising the strip
    passes its own `files` and says so."""
    if files is None:
        files = {"app/main.py": "def main():\n    return 1\n",
                 "README.md": "# acme board\n"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{prefix}/{rel}")
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def pr_meta(title="feat: a thing", *, additions=20, deletions=2,
            base_ref="main", head_ref="feat/x", head=DEFAULT_HEAD,
            merge_base=DEFAULT_MERGE_BASE, files=UNSET, changed_files=UNSET,
            state="OPEN", draft=False, mergeable="MERGEABLE", **extra):
    """The opening metadata read, as `gh pr view --json …` returns it.

    `merge_base=None` omits `baseRefOid` entirely rather than sending null —
    that is what a `gh` too old to know the field actually does, and the two are
    not the same thing to the code reading it.

    `mergeable` defaults to the state that lets a round proceed. It is not a
    convenience: the panel refuses a CONFLICTING branch before any seat runs
    (#271) and records a `config_notes` line for anything that is not MERGEABLE,
    so a default of "unset" would put a note on every run() test in this suite and
    refuse the several that assert on a round that ran. A test about the gate says
    `mergeable="CONFLICTING"` and means it.
    """
    meta = {"title": title, "additions": additions, "deletions": deletions,
            "baseRefName": base_ref, "headRefName": head_ref,
            "headRefOid": head, "state": state, "isDraft": draft,
            "mergeable": mergeable}
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
            fork_point=UNSET, merge_base_after=UNSET, compare=UNSET,
            compare_diff="", mergeable_now=UNSET, tree=UNSET, calls=None,
            strict=True):
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
      merge_base: GitHub's STORED base for the PR (`baseRefOid` on the opening
        read). Not a merge base — see `fork_point`.
      fork_point: what the compare API answers for the TRUE merge base
        (`_merge_base_now`). Defaults to `merge_base`, i.e. the two agree and no
        stale-base note fires — which is the state nearly every test in this suite
        is implicitly about. Give it a different value to reproduce #241.
      merge_base_after: what the fork-point read answers once the head has
        moved. Defaults to the unchanged `fork_point` (the no-op path).
      mergeable_now: what the mergeability RE-READ answers (#271). Only reached
        when the opening read said UNKNOWN, which is what GitHub returns while it
        computes the merge test. Defaults to the metadata's own state.
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
    # The tarball bytes for the PR-tree fetch. A real gzip of a real (tiny) tree by
    # default, because a `b""` would exercise the unpack-failure path on every test
    # rather than the success path they are all implicitly on.
    tree = pr_tarball() if tree is UNSET else tree
    the_merge_base = (base.get("baseRefOid") if merge_base is UNSET else merge_base)
    # The TRUE fork point, which agrees with the stored base unless a test says
    # otherwise. `_commit_id` rejects a non-sha, so a `None` here has to come back
    # as an empty body rather than the string "None".
    the_fork = the_merge_base if fork_point is UNSET else fork_point
    head_reads = []
    fork_reads = []

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
            if field == "mergeable":
                # The re-read the #271 gate makes when the opening answer was
                # UNKNOWN, which is what GitHub returns while it computes. It
                # answers the metadata's own state unless a test separates them.
                return json.dumps(
                    {} if mergeable_now is None else
                    {"mergeable": answer(base.get("mergeable")
                                         if mergeable_now is UNSET else mergeable_now)})
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
                # THREE callers now, told apart by how they ask: the diff media
                # type wants a raw diff, `.merge_base_commit.sha` wants the fork
                # point (#241), and the remaining `--jq` wants the range body.
                # Discriminated on the jq expression panel.py actually sends rather
                # than on a copy of it here, so a change to that expression cannot
                # leave this stub answering the wrong caller.
                if "Accept: application/vnd.github.diff" in args:
                    return answer(compare_diff)
                if panel._MERGE_BASE_JQ in args:
                    # The first read is the round's own; any later one is the
                    # re-read after the head moved, exactly as `head_moves_to`
                    # applies from the first single-field head read.
                    fork_reads.append(args)
                    after = the_fork if merge_base_after is UNSET else merge_base_after
                    got = the_fork if len(fork_reads) == 1 else after
                    # `gh --jq` prints a JSON string RAW and a missing field as
                    # `null`, which is what the caller's own guard is written for.
                    return "" if got is None else f"{answer(got)}\n"
                return answer("" if compare is UNSET else compare)
            if "/tarball/" in path:
                # The PR's own tree (#113). Answered by DEFAULT, and that matters:
                # `reviewer_code_access` defaults on, so every run() test reaches
                # this call. Left unanswered it fell through to the network — the
                # suite kept passing and went from 7 seconds to 30, which is the
                # slowest possible way to learn that a stub had a hole in it.
                return answer(tree)

        if strict:
            raise AssertionError(
                f"gh_stub does not know this call: {args!r}\n"
                "panel.py gained a `gh` call and this stub was not taught it. "
                "Teach it in harness/loops/tests/conftest.py rather than letting "
                "it fall through — an unanswered call degrades a round silently "
                "and no test goes red (128-F09).")
        return ""

    return sh


@pytest.fixture(autouse=True)
def _no_escalation_posts(monkeypatch):
    """No test posts a real escalation to a real board (#274).

    Autouse and unconditional, because the failure it prevents is invisible from
    inside the suite: `announce` resolves the board out of this HOST's site
    config, so on a developer's enrolled machine a `preland` or `epic` test that
    reaches a producer would put a `stuck` post addressed to a person on the live
    board — green suite, real interruption. In the nix sandbox there is no board
    and nothing would have happened, which is precisely why nobody would have
    found this by running CI.

    The two suites that exercise the door turn it back on themselves, around a
    stubbed opener — see `test_needs_human.py`.
    """
    monkeypatch.setenv("QUARTERBACK_NEEDS_HUMAN", "off")


@pytest.fixture(autouse=True)
def _no_board_reads(monkeypatch):
    """No test READS a real board either, and this is the read half of the two
    fixtures above.

    `board_terminal_verdict` (#617) is called on every round, unconditionally and
    before any gate — so every panel test that does not stub it puts an HTTPS
    request to whatever board THIS host's site config names. On an enrolled
    workstation that is a live board, which makes the whole suite depend on a
    remote service being up: under `-n 8` it answers some of those requests with a
    503, the round appends *"the board could not be asked …"* to `config_notes`,
    and the several tests asserting `config_notes == []` go red in a different
    combination on every run. Nothing is wrong with the code under test, and
    nothing about the failure says so.

    The answer given is `{"runs": []}` — a board that holds no recorded run for
    this PR, which is what the live board says for the fictional repos this suite
    uses, and which `board_terminal_verdict` reads as "no cycle ended here" in
    silence. So the fixture changes no assertion; it only stops them depending on
    the network.

    A test about the board answers for itself: `monkeypatch` inside the test runs
    after this and wins, which is how `test_panel_prior_cycle` drives every verdict
    in this module.
    """
    monkeypatch.setattr(panel, "board_get", lambda path, params: ({"runs": []}, ""))


@pytest.fixture(autouse=True)
def _no_next_door_fetch(monkeypatch):
    """No test asks a real board what was confirmed next door (#508).

    Autouse and unconditional, on `_no_escalation_posts`' rule and for exactly its
    failure mode: `board_next_door` runs on EVERY round, resolves the board out of
    this HOST's site config, and on an enrolled machine therefore makes a live HTTP
    call per round — while the header of this file says in capitals that the suite
    does not talk to a board. In the nix sandbox there is no board and nothing
    happens, which is why CI would never have found it.

    It was not found by reading, either. It surfaced as four unrelated e2e panel
    tests going red on an assertion about `config_notes` being empty, because the
    live board answered 422 (it predates the endpoint) and the round dutifully
    reported that it could not fetch. A leak that announces itself in somebody
    else's assertion is the good case; the same call succeeding against a real
    board would have made these tests mean different things on two machines, which
    is the leak this whole file exists to close.

    Stubbed at `board_next_door` rather than at `board_request` so that the reason
    a round has no hints is "nothing was asked", not "the board said no" — the
    second would put a note in every report and change what the e2e tests assert.

    `test_panel_next_door.py` is the file this would blind, and it restores the
    real function explicitly (`_the_real_fetch`) rather than opting out: a test of
    this function that silently ran against the stub would assert on `([], "")` and
    pass whatever the function did.
    """
    monkeypatch.setattr(panel, "board_next_door", lambda *a, **k: ([], ""))


@pytest.fixture(autouse=True)
def recorded_runs(monkeypatch):
    """No test records a real run on a real board (#94), and here is the list of
    what it would have recorded.

    Autouse and unconditional, on the same argument as `_no_escalation_posts`
    above: `record_run` pipes the payload to `qb record-review`, `qb` resolves
    the board out of THIS host's site config, and `qb` is on the PATH of every
    enrolled workstation. A test that reaches it on such a machine writes a run
    to the live board — green suite, real data — while in the nix sandbox, which
    carries no `qb`, nothing happens at all. So the suite would be quietly
    correct on the one box nobody develops on.

    It was survivable before only by accident: the two exits that recorded were
    the reviewed one and the pre-flight refusal, and the modules exercising those
    happened to stub `record_run` per test. Since the title-pattern skip records
    too, the exits that reach the board are no longer the ones a test author
    thinks about, and "remember to stub it" is not a guard.

    Yielding the list rather than a bare no-op, because the assertion this fixture
    exists to make possible is the one #94 turns on: that the skip path reaches
    the board at all. Returns "" — what a successful record returns — so a caller
    appending its result to `config_notes` gets a run recorded cleanly, which is
    the ordinary case; a test wanting the failure branch overrides it itself.
    """
    seen: list[dict] = []

    def _record(payload: dict) -> str:
        seen.append(payload)
        return ""

    # Both names: `panel` imports it into its own namespace, and `panel_seats` is
    # where it is defined. A test that patches one and a call site that reads the
    # other is exactly how a guard like this comes to cover nothing.
    monkeypatch.setattr(panel, "record_run", _record)
    monkeypatch.setattr(panel_seats, "record_run", _record)
    return seen


@pytest.fixture(autouse=True)
def pr_claims(monkeypatch):
    """No test claims a real PR on a real board (#253), and here is what it would
    have claimed.

    Autouse and unconditional, for `recorded_runs`' argument exactly: `hold_pr`
    shells out to `qb-claim`, `qb-claim` resolves the board out of THIS host's
    site config, and it is on the PATH of every enrolled workstation. A test that
    reaches it there takes a live claim on a live PR — green suite, and an agent
    somewhere is then told the PR it is reviewing is held by a test run — while in
    the nix sandbox, which carries no `qb-claim`, nothing happens at all. Quietly
    correct on the one box nobody develops on is the failure both these fixtures
    exist to rule out.

    Worse than the record it copies, in one way worth stating: a spurious run on
    the board is bad data, and a spurious CLAIM is bad data that REFUSES somebody.
    `create-worktree --require-claim` and the plan's pickup gate both stop for
    one.

    Yields what was taken and handed back, in order, so a test can assert the pair
    rather than only the first half — a claim taken and never released is the
    failure mode `release_pr` exists for. Both return "", which is what the real
    pair return when the board answered.
    """
    seen: list[tuple] = []

    def _hold(repo_path, pr_number, round_no) -> tuple[str, bool]:
        seen.append(("hold", str(repo_path), int(pr_number), int(round_no)))
        return "", True

    def _release(repo_path, pr_number) -> str:
        seen.append(("release", str(repo_path), int(pr_number)))
        return ""

    # Both names, for the reason `recorded_runs` gives: `panel` star-imports these
    # into its own namespace and `panel_seats` is where they are defined, so a
    # guard on one name and a call site on the other covers nothing.
    for mod in (panel, panel_seats):
        monkeypatch.setattr(mod, "hold_pr", _hold)
        monkeypatch.setattr(mod, "release_pr", _release)
    return seen

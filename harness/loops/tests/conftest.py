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

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel_core  # noqa: E402  — the `gh` seam every stub here replaces

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
            tree=UNSET, calls=None, strict=True):
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
    # The tarball bytes for the PR-tree fetch. A real gzip of a real (tiny) tree by
    # default, because a `b""` would exercise the unpack-failure path on every test
    # rather than the success path they are all implicitly on.
    tree = pr_tarball() if tree is UNSET else tree
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

"""What a round READS and what it reports about itself: the fix range, provenance,
increment scope, and the two non-LLM signals (SonarCloud and CI).

Split out of panel_seats (#129), which was the last module over antigravity's
120,000-byte argv cap. The seam is real rather than arbitrary: everything here
answers "what was this round looking at, and what else already knows something
about this commit", where panel_seats answers "how do you run a seat".

A MOVE, not a rewrite.
"""

from __future__ import annotations

from panel_core import *            # noqa: F401,F403
import panel_core                   # noqa: F401

#: What a missing fix range MEANS, as a value rather than as a sentence (#500).
#:
#: `no-fix` and `blind` both come back with no diff and both disable provenance,
#: recurrence and increment scoping — and they are not the same news. Nothing
#: landed between the rounds, so there is nothing to attribute and the instruments
#: are VACUOUS; against that, the branch was rewritten (or the API refused, or the
#: range would not fit) and the instruments are BLIND on a fix pass that really
#: happened. The second is a coverage gap and takes a veto; the first is a fact
#: about the cycle and takes none.
#:
#: A value and not a substring match on ``why``, on this repo's own rule: deriving
#: a fact back out of a sentence written for a human is how the two drift apart,
#: and `harness_rules` says as much where it refuses to sniff a layer out of its
#: own provenance string. The caller gates on this; the sentence stays for the
#: reader.
#: `rewritten` is split out of `blind` (#512) because the two forbid different
#: things. Blind is "this reader could not get the range" — too large to hold, an
#: API refusal, patches it cannot parse — and says nothing about whether a sound
#: copy of that range exists elsewhere; the round's own `Review.increment` often IS
#: one, and refusing to attribute from it is a false blindness on a round that read
#: the fix pass perfectly well. Rewritten is "the range is NOT the fix pass": after
#: a rebase the three-dot merge base moves back, so any diff of that span — this
#: reader's or the round's — widens toward the whole PR and attributing from either
#: blames the fixer for every line the PR ever added. Nothing may attribute from
#: THAT SPAN, which is not the same as nothing being attributable: the commits the
#: fix pass wrote are still on the branch under new SHAs, and
#: :func:`reconstruct_fix_range` (#504) names them by patch equivalence instead.
#: This value is what sends it looking; a reconstruction that declines leaves the
#: round exactly as blind as this verdict found it.
FIX_RANGE_OK, FIX_RANGE_NO_FIX = "ok", "no-fix"
FIX_RANGE_BLIND, FIX_RANGE_REWRITTEN = "blind", "rewritten"


def _fix_range_diff(gh_repo: str, base_sha: str | None, head_sha: str | None
                    ) -> tuple[str | None, str | None, str]:
    """The diff of everything that landed BETWEEN two rounds — i.e. the fix pass
    whose damage (or thoroughness) provenance is trying to attribute — as
    `(diff, None, "ok")`; or `(None, why, kind)` when there is no range to read,
    where `kind` is :data:`FIX_RANGE_NO_FIX` or :data:`FIX_RANGE_BLIND`.

    It never raises. A force-push that orphaned the earlier head, a baseline
    written before `head_sha` was recorded, no `gh` on PATH, an API refusal, a
    range too large to hold: provenance is a signal and not a verdict, so all of
    them have to degrade to "unknown" and leave the rest of the round untouched.
    The alternative is a round that dies because an attribution nobody gates on
    could not be computed. The REASON comes back with the None because the four
    of them read very differently to an operator — "the branch was rewritten"
    and "nothing landed between rounds" are not the same news.

    Read as JSON rather than as a raw diff for the `status` field, which is the
    only thing that can tell a rewritten branch from a linear one: `compare/a...b`
    is the THREE-dot form, so GitHub diffs *merge-base(a, b) → b*. On a branch
    that only ever grew between rounds that is exactly the fix range; on one that
    was rebased or force-pushed it is every line the PR ever added, which would
    read as the fixer having written all of it. GitHub calls that case `diverged`
    and it is refused here. Two-dot is not an option — this endpoint 404s on it.
    Refused, and then handed on: :func:`reconstruct_fix_range` rebuilds the pass
    from the local object store when there is one (#504), because the range is
    wrong rather than the history.

    Two biases remain and are written down rather than fixed. Merging the base
    branch INTO the PR between rounds leaves the old head an ancestor of the new
    one (status `ahead`, correctly), so main's own commits fall inside the range
    and their lines are attributed to the fix pass — `introduced` then
    over-counts. And the compare endpoint returns at most 300 files, so a fix
    pass wider than that is attributed on the first 300 and the rest read as
    `missed`. #41 has landed and #512 acted on it — see :func:`_provenance` for what
    that removed and what it did not.
    """
    if not gh_repo:
        return None, "no GitHub repo is configured for this run", FIX_RANGE_BLIND
    if not base_sha:
        return None, ("the baseline does not record which commit it reviewed "
                      "(written before `head_sha` existed)"), FIX_RANGE_BLIND
    if not head_sha:
        return None, "this round did not record the commit it reviewed", FIX_RANGE_BLIND
    span = f"{base_sha[:8]}..{head_sha[:8]}"
    if base_sha == head_sha:
        # Not a failure and not worth an API call to be told nothing changed —
        # but told apart from one, or the operator goes looking for a GitHub
        # fault that never happened.
        return (None, f"no commit landed between rounds (head unchanged at {head_sha[:8]})",
                FIX_RANGE_NO_FIX)
    try:
        got = json.loads(panel_core.sh(["gh", "api", f"repos/{gh_repo}/compare/{base_sha}...{head_sha}",
                             "--jq", _FIX_RANGE_JQ], timeout=FIX_RANGE_TIMEOUT_S))
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        # Widened past CalledProcessError deliberately: no `gh` on PATH is an
        # OSError, a hung call is a TimeoutExpired, a truncated body is a
        # ValueError, and each of them would otherwise take down a whole review
        # round over an attribution nothing gates on.
        return None, f"could not read the range {span} ({type(e).__name__})", FIX_RANGE_BLIND
    if not isinstance(got, dict):
        return (None, f"the compare API answered {span} with something that is not an object",
                FIX_RANGE_BLIND)
    if got.get("status") == "diverged":
        return None, (f"{span} have diverged — the branch was rewritten between rounds, so "
                      "the range would span commits no fix pass wrote"), FIX_RANGE_REWRITTEN
    # `behind` is the other rewrite, and it reaches here looking like nothing at all
    # (#500, found on review). The head is an ANCESTOR of the commit the last round
    # reviewed — a reset backwards, a force-push that dropped commits — so the
    # three-dot merge base IS the head and the compare comes back with no files. Left
    # to the empty-compare road below that reads as "no commit landed between
    # rounds", which is the opposite of what happened: commits were REMOVED, the fix
    # pass this round is meant to attribute is gone from the branch, and the round is
    # as blind as a diverged one. Named here rather than inferred from an empty file
    # list, because the file list cannot tell the two apart.
    if got.get("status") == "behind":
        return None, (f"{span} is BEHIND — the branch was reset to an ancestor of the "
                      "commit the last round reviewed, so the fix pass it would "
                      "attribute is no longer on the branch"), FIX_RANGE_REWRITTEN
    out: list[str] = []
    size = 0
    for f in got.get("files") or []:
        name, patch = f.get("filename"), f.get("patch")
        if not (name and patch):
            # Binary, or too large for the API to send a patch for. SKIPPED, and the
            # range still comes back `ok` when other files were readable — raised on
            # review as a possible under-veto and declined deliberately (#500).
            #
            # Partial attribution is a documented BIAS of this signal, not a blind
            # instrument. The two ways a patch goes missing are worth separating,
            # because they are not equally harmless and a reason that only covers the
            # easy one is not a reason:
            #
            #   * BINARY — nothing is lost. A finding has a file and a line, and a
            #     binary file has no lines for one to sit on, so there was never an
            #     attribution here to miss.
            #   * TOO LARGE for the compare API to send — lines ARE lost, and a
            #     finding in that file comes back `missed` when the fix pass may well
            #     have written it. This is accepted, and it is accepted because the
            #     docstring above already accepts the identical loss from the same
            #     API for the same reason: "the compare endpoint returns at most 300
            #     files, so a fix pass wider than that is attributed on the first 300
            #     and the rest read as `missed`". Same mechanism, same consequence,
            #     same bias — and `_provenance` already documents `introduced` as a
            #     FLOOR rather than a measurement, which is the honest place for it.
            #
            # What decides it either way is the alternative: vetoing here fires on any
            # PR that touched a lockfile or an image, which is most of them, and a
            # veto that fires on most rounds is the one readers learn to skip — the
            # failure this whole change is against. #41 (review the increment) is what
            # removes the guess rather than re-weighing it.
            #
            # The case where it IS blind is every file being unreadable, and that is
            # caught below: then there is no attribution left at all.
            continue
        body = patch.rstrip("\n")
        chunk = f"diff --git a/{name} b/{name}\n{body}\n"
        size += len(chunk)
        if size > FIX_RANGE_MAX_CHARS:
            return None, (f"the range {span} is larger than {FIX_RANGE_MAX_CHARS:,} chars — "
                          "not attributed, rather than held whole in memory"), FIX_RANGE_BLIND
        out.append(chunk)
    if not out:
        # An empty compare — a revert that nets to nothing, an empty commit — is
        # "no range", not "a range with no added lines". The second reading calls
        # every new finding `missed`, confidently and with nothing to say so.
        #
        # **Which KIND it is turns on whether any file changed at all**, and the two
        # roads out of this loop are not the same news (#500, found on review). No
        # files means nothing landed: the instruments are vacuous, there is no fix
        # pass they failed to see, and a veto would fire on an honest empty round.
        # Files that changed but carried no readable patch — every one binary, or
        # each too large for the API to send — means a fix pass DID happen and this
        # cannot see it, which is blind in exactly the sense a rewritten branch is.
        # Collapsing them would suppress the veto on a round that genuinely lost its
        # attribution, which is the failure this whole change exists to stop.
        changed = len(got.get("files") or [])
        if changed:
            return (None, f"the range {span} changed {changed} file(s) and none of them "
                    "carried a readable patch (binary, or too large for the compare "
                    "API to send) — a fix pass landed that this cannot attribute",
                    FIX_RANGE_BLIND)
        return (None, f"the range {span} changed no line this can attribute against",
                FIX_RANGE_NO_FIX)
    return "".join(out), None, FIX_RANGE_OK


#: What a reconstruction will spend before it gives up (#504). Both bounds are
#: :func:`_fix_range_diff`'s, for its reason — nothing gates on provenance, so a
#: slow or enormous history has to degrade to "unknown" rather than make a round
#: wait on it — and the commit ceiling is the tighter of the two on purpose: a
#: branch carrying more commits than this either side of a rewrite is not a fix
#: pass anybody is about to attribute, and patching every one of them to discover
#: that is the whole cost. Shorter than `FIX_RANGE_TIMEOUT_S` because these calls
#: are local: a git command against an object store that is already on the disk
#: does not get the allowance a network round trip does. It is a PER-CALL bound and
#: a reconstruction makes at most six, so the figure a stalled round would show is
#: the multiple — still a bounded delay on a path only a rewritten round takes, and
#: still preferable to an unbounded one on an attribution nothing gates on.
RECONSTRUCT_TIMEOUT_S = 30
RECONSTRUCT_MAX_COMMITS = 250


def _git(repo_path: str, *args: str, stdin: str = "") -> str | None:
    """stdout of `git -C repo_path <args>`, or None if it could not run or failed.

    ONE None for every failure — no `git` on PATH, no checkout at that path, a ref
    this clone does not have, a hung call — because the caller does the same thing
    with all of them: it stops reconstructing and says so. That is
    :func:`panel_timing._commit_time`'s contract, arriving here for its reason, and
    it is the contract the whole of #504 rests on: a repair that cannot run must
    leave the round exactly as blind as it was, never half-attributed.

    `subprocess.run` and not `panel_core.sh`, on two counts. `sh` raises on a
    non-zero exit and every non-zero exit here is an ANSWER — "that commit is not
    in this clone" is the reconstruction's commonest and most informative outcome.
    And `sh` is what the suites replace with a `gh` double, so routing local git
    through it would put every one of these calls in front of a stub that knows
    only the forge.

    `errors="replace"` and `ValueError` in the `except` are what make "never raises"
    true rather than intended (found by Codex). `text=True` DECODES, and git output
    is not guaranteed UTF-8 — a filename or a commit message in another encoding is
    ordinary in a long-lived repository — so the decode raises `UnicodeDecodeError`
    out of `subprocess.run` itself, past an `except` that named only `OSError` and
    `SubprocessError`, and takes down a round over an attribution nothing gates on.
    Replacement is safe for both readers here: the added-line scan wants `diff --git`,
    `@@` and a leading `+`, and the patch stream is fed back to `git patch-id` after
    the SAME substitution on both sides of the comparison, so two ids that should
    match still do. `ValueError` (which `UnicodeDecodeError` subclasses) covers the
    rest of the family, including a `repo_path` carrying an embedded NUL.
    """
    if not repo_path:
        return None
    try:
        out = subprocess.run(["git", "-C", repo_path, *args], input=stdin,
                             capture_output=True, text=True, errors="replace",
                             timeout=RECONSTRUCT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return out.stdout if out.returncode == 0 else None


def _patch_ids(repo_path: str, shas: list[str]) -> dict[str, str] | None:
    """`{commit: patch-id}` for the commits named, or None if they could not be read.

    A patch-id is a hash of what a commit CHANGES with the line numbers, the hunk
    headers and the whitespace taken out, which is exactly the property a rebase
    preserves and a SHA does not. `--stable` pins the algorithm to the one that
    does not depend on the order git happened to emit the files in; without it two
    clones can disagree about whether the same patch is the same patch.

    Commits git cannot reduce to a patch simply do not appear: an empty commit has
    no lines and a merge has no single parent to diff against, so `diff-tree` emits
    nothing for either and `patch-id` has nothing to hash. Both are correct to
    drop — a commit that added no line cannot have introduced a defect on one —
    and the caller reads a missing id as "not attributable", never as "matched".

    None, not `{}`, when git failed. The two are opposite instructions: an empty
    map is "these commits changed nothing", and the caller may go on; a failure is
    "this cannot be corresponded", and the caller must stop, because a fix set
    computed against patch-ids that were never read is every commit on the branch.
    """
    if not shas:
        return {}
    patches = _git(repo_path, "diff-tree", "--stdin", "-p", "-M", "--root",
                   stdin="\n".join(shas) + "\n")
    if patches is None or len(patches) > FIX_RANGE_MAX_CHARS:
        return None
    out = _git(repo_path, "patch-id", "--stable", stdin=patches)
    if out is None:
        return None
    ids: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2:
            # A line this cannot read is a HOLE, and a hole is not the same as a
            # commit with no patch (found by Codex). A commit missing from the prior
            # side reserves nothing, so its counterpart on the new side reads as the
            # fix pass — over-attribution bought by a parse this silently skipped.
            # `patch-id` does not emit such a line today; the point is that if it
            # ever did, the caller must decline rather than correspond two histories
            # from a map it knows is incomplete.
            return None
        ids[parts[1]] = parts[0]
    return ids


def reconstruct_fix_range(repo_path: str, gh_repo: str, base_ref: str,
                          anchor: str | None, head_sha: str | None) -> dict:
    """The fix pass's own commits, identified across a branch REWRITE by patch
    equivalence rather than by walking a range (#504).

    :data:`FIX_RANGE_REWRITTEN` is where :func:`_fix_range_diff` stops, and #500's
    observation is that stopping there is a choice rather than a limit: *the range
    is wrong, not the history*. After a rebase `compare/a...b` diffs from a merge
    base that has moved and widens toward the whole PR, so nothing may attribute
    from that span — but the commits the fix pass actually wrote are still on the
    branch, wearing new SHAs. `git patch-id` names them anyway, because it hashes
    what a commit CHANGES and a rebase that did not resolve a conflict changes
    nothing about that.

    So: take the commits the last round had reviewed (`merge_base..anchor`), take the
    commits the branch carries now (`merge_base..head`), and call the fix pass
    whatever is left in the second once each of the first has claimed one
    patch-equivalent. Both bounded by the same fork point — the comment at the
    `rev-list` calls says why the obvious `head..anchor` is wrong for the first — and
    claimed by COUNT rather than by membership, with any count the branch cannot
    settle refused outright rather than decided by position.

    Returns `{"diff", "why", "commits", "prior", "carried", "unmatched"}` and never
    raises. `diff` is None with `why` set for every way this can decline, and a
    decline leaves the round exactly as blind as :func:`_fix_range_diff` left it —
    #509's veto still fires, nothing is attributed, and the operator is told which
    of the reasons it was.

    **The merge base comes from the forge, not from the clone, and that is not
    fussiness.** A local `origin/<base>` is refreshed by nothing in this process, and
    a stale one is an ancestor of the rebased head — so `git merge-base` answers with
    the stale tip and every base-branch commit the rebase moved onto falls inside the
    "fix pass". That is the 21-commit over-attribution #500 measured, reintroduced by
    the repair meant to prevent it. :func:`_merge_base_now` asks GitHub where the
    branch actually forks, and this declines rather than guess.

    **It attributes only where the reconstruction is EXACT, and declines otherwise.**
    That is a correction this made under review, and the argument is not about
    honesty — every case below was reported in `config_notes` when this leaned — but
    about what the number DOES. `escalate_on.fix_injection` ends a cycle, and the
    README's case for a threshold at 0.5 is that "`introduced` is a documented FLOOR,
    which is what makes a threshold on it err safe: a measured 0.64 is at least
    0.64". A source that over-counts breaks that argument, and the price is a cycle
    stopped with real findings left unfixed. Nothing reads a note before firing a
    brake. So the five shapes below decline, and a decline costs only what was
    already lost.

    - **No correspondence at all** — the prior round had commits and none came
      through (a squash, a re-created branch). Every commit on the branch would read
      as the fix pass, which is the catastrophe `rewritten` exists to prevent.
    - **Nothing left over** — no fix pass here to attribute, told apart from the
      branch RESET BACKWARDS that removed one, which the caller needs in those words.
    - **An UNMATCHED prior commit** — a rebase that resolved a conflict changed that
      commit's content, so it is somewhere among the leftovers with no way to say
      which. #504's own wording settles it: *that commit's lines cannot be
      attributed*. `unmatched` bounds the damage at one COMMIT, which can be a
      thousand lines and cover every finding in the round, so it is not a bound on
      anything a reader could correct for.
    - **A pass that is not the branch TAIL** — then no single diff is the pass, and
      reading its commits' patches separately is a superset twice over: a line the
      pass added and a later commit removed is still in it, and each line's number
      comes from its own commit's tree rather than the head's.
    - **An ambiguous patch-id** — the branch carries more copies of a patch than the
      last round had, and which is the fixer's own cannot be told from which is the
      replayed one.

    What survives is one two-dot `git diff` from the commit before the pass to the
    head: the exact net change, numbered in the head's own tree, which is what
    findings are reported against. That is the ordinary rebase — #500's own measured
    case — and it is the case worth having.
    """
    out: dict = {"diff": None, "why": None, "commits": [],
                 "prior": 0, "carried": 0, "unmatched": 0}
    if not repo_path:
        out["why"] = ("no local checkout is configured for this repo, and patch-id is "
                      "git rather than the compare API")
        return out
    if not (anchor and head_sha):
        out["why"] = "the range has no two ends to reconstruct between"
        return out
    for ref in (anchor, head_sha):
        # Explicitly, rather than by letting git decline it. A value starting `-`
        # reads as an OPTION in argv, and while the worst that does here is make
        # `rev-parse` exit non-zero — which this already treats as "not in the
        # checkout" — the two are different news, and a decline that names the wrong
        # reason sends an operator looking for a commit rather than for the baseline
        # that is malformed. `_is_ref` is the same check `--since` already passes.
        if not _is_ref(ref):
            out["why"] = f"{ref!r} is not a ref this can ask git about"
            return out
        if _git(repo_path, "rev-parse", "--verify", "--quiet",
                f"{ref}^{{commit}}") is None:
            out["why"] = (f"{ref[:8]} is not in the checkout at {repo_path} — the "
                          "commits a rewrite orphans stay reachable only where "
                          "somebody still holds them")
            return out
    mb = _merge_base_now(gh_repo, base_ref, head_sha)
    if not mb or _git(repo_path, "rev-parse", "--verify", "--quiet",
                      f"{mb}^{{commit}}") is None:
        out["why"] = ("the branch's fork point could not be read, so the "
                      "reconstruction could not be bounded to the PR's own commits "
                      "— the base branch's would be inside it")
        return out
    # BOTH sides bounded by the SAME fork point, and the prior side is why it has to
    # be a fork point rather than the other head. `head..anchor` reads as the obvious
    # spelling of "what the last round reviewed and this branch no longer has", and
    # it is wrong on every rewrite that left part of the branch alone: an amended tip
    # puts only the amended commit in it, so every commit BELOW the amend is missing
    # from the prior set, matches nothing, and is attributed to a fix pass that did
    # not write it. `mb..anchor` is the PR exactly as that round saw it, whatever the
    # rewrite did — and `mb` bounds it correctly even though it is the NEW fork
    # point, because the old one is an ancestor of it (the base branch grew).
    # `--no-merges` on both sides. A merge has no single parent to diff against, so
    # `patch-id` can say nothing about one and a merge on either side would be
    # unmatchable by construction — every merge on the new branch would read as the
    # fix pass. Dropping them also removes, for a reconstructed round only, the
    # base-branch-merge over-count `_fix_range_diff`'s docstring records and cannot
    # fix: main's own commits arrive through a merge, and a merge is not here.
    new_out = _git(repo_path, "rev-list", "--no-merges", "--reverse",
                   f"{mb}..{head_sha}")
    prior_out = _git(repo_path, "rev-list", "--no-merges", f"{mb}..{anchor}")
    if new_out is None or prior_out is None:
        out["why"] = "the commits either side of the rewrite could not be listed"
        return out
    new, prior = new_out.split(), prior_out.split()
    if max(len(new), len(prior)) > RECONSTRUCT_MAX_COMMITS:
        out["why"] = (f"more than {RECONSTRUCT_MAX_COMMITS} commits sit either side of "
                      "the rewrite — past the size any fix pass is, and past what is "
                      "worth patching to find out")
        return out
    if not new:
        out["why"] = "the rewritten branch carries no commit of its own"
        return out
    new_ids, prior_ids = _patch_ids(repo_path, new), _patch_ids(repo_path, prior)
    if new_ids is None or prior_ids is None:
        out["why"] = "the commits' patch-ids could not be computed"
        return out
    # AMBIGUITY IS A REFUSAL, and this is the check that makes the matching below
    # safe to do by count (found by Codex, second pass). A patch-id is not unique: a
    # fix pass that re-applies a change the last round reviewed produces a second
    # commit with the SAME id. When the branch carries MORE of an id than the last
    # round had, one of them is the rebased copy and the other is the fixer's own —
    # and nothing here can say which. Deciding it by position was the first shape of
    # this, justified by "a rebase replays the reviewed commits first"; that holds
    # for a plain rebase and not for an interactive one, a reorder, a cherry-pick or
    # an arbitrary force-push, and getting it backwards puts an already-reviewed
    # commit inside the fix pass while leaving the fixer's own out. On a signal that
    # ends cycles, a coin toss is not available.
    prior_counts, new_counts = Counter(prior_ids.values()), Counter(new_ids.values())
    ambiguous = sum(1 for pid, n in new_counts.items() if 0 < prior_counts[pid] < n)
    if ambiguous:
        out["why"] = (f"{ambiguous} patch(es) appear more often on the rewritten branch "
                      "than in what the last round reviewed, so which copy is the "
                      "fixer's own and which is the replayed one cannot be told apart")
        return out
    # With ambiguity refused above, spending the counts in branch order is arithmetic
    # rather than a guess: every id the last round had appears at most that many
    # times here, so which commit takes which claim cannot change the leftover SET.
    unspent = Counter(prior_counts)
    fix: list[str] = []
    for c in new:
        pid = new_ids.get(c)
        if not pid:
            # No patch to hash — an empty commit. It added no line, so it can have
            # introduced a defect on none, and it is neither a match nor a leftover.
            continue
        if unspent[pid]:
            unspent[pid] -= 1
            continue
        fix.append(c)
    out["prior"] = len(prior_ids)
    out["unmatched"] = sum(unspent.values())
    out["carried"] = out["prior"] - out["unmatched"]
    if prior_ids and not out["carried"]:
        out["why"] = (f"none of the {len(prior_ids)} commit(s) the last round reviewed "
                      "has a patch-equivalent on the rewritten branch, so the two "
                      "histories cannot be corresponded and every commit on the "
                      "branch would read as the fix pass")
        return out
    if not fix:
        # Two different pieces of news, and `unmatched` is the only thing that can
        # tell them apart. Nothing left over AND nothing missing is a pure rebase
        # with no fix pass on it. Nothing left over WITH commits missing is a branch
        # RESET BACKWARDS — the `behind` rewrite, which reaches the compare API
        # looking like nothing at all — and the pass this round would attribute has
        # been taken off the branch rather than rewritten on it. Both decline, and an
        # operator reading the second needs to know a force-push dropped work.
        out["why"] = ("every commit on the rewritten branch is patch-equivalent to one "
                      "the last round already reviewed — "
                      + (f"and {out['unmatched']} of that round's commits are no longer "
                         "on the branch at all, so the fix pass was REMOVED rather than "
                         "rewritten"
                         if out["unmatched"] else
                         "there is no fix pass here to attribute, and a rewrite is not "
                         "evidence that one happened"))
        return out
    # ---- AND THE TWO REFUSALS THAT KEEP `introduced` A FLOOR (Codex, third pass).
    #
    # Both of these were leans this reported and attributed through, and the argument
    # against that is not that a lean is dishonest — it was in `config_notes` — but
    # that `escalate_on.fix_injection` ENDS A CYCLE, and the README's case for a
    # threshold at 0.5 is "`introduced` is a documented FLOOR, which is what makes a
    # threshold on it err safe: a measured 0.64 is at least 0.64". A source that
    # over-counts breaks that argument, and the cost is a cycle stopped with real
    # findings unfixed. A note beside the number does not stop that; nothing reads a
    # note before firing a brake.
    #
    # An UNMATCHED prior commit is the first. Its content moved in the rewrite, so it
    # is somewhere in the leftovers and there is no way to say which one it is — the
    # whole of an already-reviewed commit is then inside the pass, and `unmatched`
    # bounds that at one COMMIT, which can be a thousand lines and cover every finding
    # in the round. #504's own wording is what settles it: *that commit's lines cannot
    # be attributed*. Not "are attributed to the fixer with a caveat".
    if out["unmatched"]:
        out["why"] = (f"{out['unmatched']} of the {out['prior']} commit(s) the last round "
                      "reviewed changed content in the rewrite (a conflict resolved "
                      "during the rebase), so each is somewhere among the commits this "
                      "would call the fix pass and there is no way to say which — "
                      "attributing them would blame the fixer for work the last round "
                      "had already reviewed")
        return out
    # A pass that is NOT the branch tip is the second. Concatenating each leftover
    # commit's own patch was the fallback, and it is a superset twice over: a line the
    # pass added and a later commit removed is still in the set, and each line's
    # number comes from its own commit's tree rather than the head's. One net two-dot
    # diff has neither problem, and it is available exactly when the leftovers are the
    # tail of the branch — which a rebase makes them, by replaying the reviewed
    # commits first.
    #
    # `linear` is the other half of the test and not pedantry: `new` has had merges
    # dropped, so leftovers that look like the tail of `new` can still have a merge
    # sitting inside the span, and the two-dot diff would carry whatever that merge
    # brought in. Counting the range WITH merges is what tells the two apart.
    linear = _git(repo_path, "rev-list", "--count", f"{mb}..{head_sha}")
    if (linear or "").strip() != str(len(new)) or fix != new[len(new) - len(fix):]:
        out["why"] = ("the fix pass is not the tail of the rewritten branch, so no one "
                      "diff is the pass — and reading its commits' patches separately "
                      "would attribute lines the pass added and then removed")
        return out
    diff = _git(repo_path, "diff", "-M", f"{fix[0]}^", head_sha)
    if diff is None:
        # `fix[0]^` on a root commit is the one shape this covers in practice, and
        # a branch whose first commit is the repository's own is not a PR.
        out["why"] = "the reconstructed range's diff could not be read"
        return out
    if not diff.strip():
        out["why"] = ("the fix pass nets to no change against the commit before it — "
                      "there is nothing for a finding to sit on")
        return out
    out["diff"], out["commits"] = diff, fix
    return out


#: How many of a fix pass's commits a proposal will list by name (#506). A revert
#: proposal is read by a human deciding whether to undo a pass, and a pass of four
#: commits is the shape that decision is usually about; beyond this the list stops
#: informing and the COUNT is what matters, which is carried separately. Not a dial:
#: nothing gates on it, and it bounds a payload field rather than a policy.
FIX_PASS_COMMIT_CAP = 20

#: What #506's proposal reads out of the same compare endpoint provenance already
#: uses. `parents` is the load-bearing one — see :func:`fix_pass_commits`.
_FIX_PASS_COMMITS_JQ = (
    '{total: (.total_commits // 0), '
    'commits: [(.commits // [])[] | {sha: .sha, '
    'title: ((.commit.message // "") | split("\n")[0]), '
    'parents: ((.parents // []) | length)}]}')


def fix_pass_commits(gh_repo: str, base_sha: str | None, head_sha: str | None) -> dict:
    """The commits a fix pass actually consists of, as
    `{"commits": [...], "total": n, "merges": n}`, or `{}` when they cannot be read.

    #506 names a fix pass by its commit range and offers a `git revert` for it, and
    **the range alone is not enough to know that a revert of it is safe.** The bias is
    already documented on :func:`_fix_range_diff`: merging the base branch INTO the PR
    between rounds leaves the old head an ancestor, so the compare still reads `ahead`
    and main's own commits fall inside the range. For attribution that over-counts
    `introduced` and is recorded as a known lean. For a proposal it is worse than a
    lean — the offered command would revert other people's commits, and `git revert`
    refuses a merge commit outright without `-m`, so the invocation cannot even run as
    written. `merges` is what tells the two apart, and a proposal withholds its command
    unless it is zero.

    Its own call rather than a fourth element on :func:`_fix_range_diff`, and it is
    made ONLY on a round whose injection rate crossed the threshold — the terminal
    round of a diverging cycle, and no other. An ordinary round pays nothing for this,
    which is the whole reason it is not folded into the range read that every round
    makes.

    Never raises, on :func:`compare_facts`' contract and for its reason: this is an
    assurance about a proposal nothing acts on, and a round must not die because the
    commits behind an attribution it already made could not be listed. `{}` is the
    honest empty, and a proposal reading it withholds the command rather than
    assuming the range is clean.

    ``complete`` is the other half of that honesty. The compare endpoint returns at
    most 250 commits with `total_commits` naming the real figure, so on a longer range
    ``merges`` is a FLOOR — a merge past the ceiling is invisible — and a proposal has
    to read a zero there as "not known to be clean" rather than as clean."""
    if not (gh_repo and base_sha and head_sha) or base_sha == head_sha:
        return {}
    try:
        got = json.loads(panel_core.sh(
            ["gh", "api", f"repos/{gh_repo}/compare/{base_sha}...{head_sha}",
             "--jq", _FIX_PASS_COMMITS_JQ], timeout=FIX_RANGE_TIMEOUT_S))
    except Exception:                       # every one, per the contract above
        return {}
    if not isinstance(got, dict):
        return {}
    raw = got.get("commits")
    if not isinstance(raw, list):
        return {}
    commits, merges = [], 0
    for c in raw:
        if not isinstance(c, dict):
            continue
        sha = _commit_id(c.get("sha"))
        if not sha:
            continue
        # Counted over EVERY commit the compare returned, and the list is truncated
        # after — a merge past the display cap is still a merge, and a count that
        # stopped where the listing stopped would report a range as clean because it
        # was long. The compare endpoint's own 250-commit ceiling still applies and is
        # why `total` travels beside this: a range at that ceiling is one whose
        # merge count is a floor, and a proposal reads `total > len(commits)` as a
        # reason to say so rather than as a rounding.
        parents = c.get("parents")
        if isinstance(parents, int) and not isinstance(parents, bool) and parents > 1:
            merges += 1
        commits.append({"sha": sha, "title": str(c.get("title") or "").strip()})
    total = got.get("total")
    seen = len(commits)
    # WAS THE WHOLE RANGE RETURNED? The compare endpoint carries at most 250 commits
    # while `total_commits` reports the real number, so a long fix pass comes back
    # short with a 200 and no error — and a merge past that ceiling would be invisible
    # while `merges == 0` said the range was clean (found by Codex on the second pass).
    # `merges` is therefore a FLOOR on an incomplete range, exactly as `introduced` is
    # a floor on the attribution above, and only this flag can say which kind of zero a
    # zero is.
    #
    # `==`, not `<=`: `total_commits` absent comes back through the `--jq` as `0`, and
    # `0 <= seen` would call a range we know nothing about complete. An answer this
    # cannot verify has to read as "not verified", because the command it gates is the
    # one thing here a human runs.
    complete = (isinstance(total, int) and not isinstance(total, bool)
                and total == seen)
    return {"commits": commits[:FIX_PASS_COMMIT_CAP], "merges": merges,
            "total": total if isinstance(total, int) and not isinstance(total, bool)
            else seen,
            "complete": complete}


def _commit_id(value: object) -> str | None:
    """A commit id off a JSON response, or None if it is not one.

    The three readers below all ended in `.get("…") or None`, which keeps
    whatever the response held so long as it was truthy. A malformed or changed
    API shape can therefore hand back a number or an object, and the callers
    format what they get with `value[:8]` — so a bad response raises `TypeError`
    at the diagnostic, outside each helper's own `except`, and takes down a round
    whose entire purpose is to degrade gracefully (128-F13).

    Typed at the boundary rather than at the four format sites, because the
    invariant is "these functions return a commit id or nothing" and a check per
    caller is one caller away from being forgotten."""
    return value if isinstance(value, str) and value else None


def _mergeable_now(gh_repo: str, pr_number: int) -> str | None:
    """GitHub's mergeability for the PR, asked AGAIN. None if it cannot be had.

    Called only when the first answer was ``UNKNOWN``, and it is what makes the
    #271 gate work at all. GitHub computes mergeability LAZILY: the first query
    schedules the merge test and answers ``UNKNOWN`` while it runs, and the next
    one has the result. Measured on this repo, three consecutive reads of an open
    PR — ``UNKNOWN``, then ``CONFLICTING``, then ``CONFLICTING``. So a gate that
    asks once refuses only the PRs somebody happened to have looked at recently,
    which is a gate that appears to work and mostly does not.

    One extra call, and only on the cold answer. Bounded and swallowed like its
    siblings: a precondition this cannot read is reported as unread, never guessed
    at, and never allowed to stall the round."""
    try:
        return (json.loads(panel_core.sh(["gh", "pr", "view", str(pr_number), "--repo", gh_repo,
                               "--json", "mergeable"],
                              timeout=FIX_RANGE_TIMEOUT_S)).get("mergeable")) or None
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


def _head_sha_now(gh_repo: str, pr_number: int) -> str | None:
    """The PR's head commit, re-read. None if it cannot be had — the caller only
    uses it to notice that the head MOVED, and "could not tell" has to leave the
    earlier answer standing rather than erase it.

    Bounded for the same reason :func:`_fix_range_diff` is, and more urgently: this
    one runs on the critical path of every non-skipped round, before any reviewer
    is dispatched, so a hung `gh` would stall the whole panel indefinitely for an
    attribution nothing gates on. `SubprocessError` already covers the
    `TimeoutExpired` that then arrives."""
    try:
        return _commit_id(
            json.loads(panel_core.sh(["gh", "pr", "view", str(pr_number), "--repo", gh_repo,
                           "--json", "headRefOid"],
                          timeout=FIX_RANGE_TIMEOUT_S)).get("headRefOid"))
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


#: The one field the compare read below wants. `--jq` rather than parsing the
#: whole body here because the body is the entire comparison — up to 300 file
#: patches — and this call wants forty hex characters out of it.
_MERGE_BASE_JQ = ".merge_base_commit.sha"

#: What a commit id looks like coming back from `--jq`. `gh --jq` prints a JSON
#: string RAW (no quotes), and prints a missing field as the four characters
#: `null` — which is truthy, is not a commit, and would otherwise be recorded as
#: one. Short forms are accepted because a caller may hand this an abbreviated
#: sha; anything else is a None.
_SHA_TEXT = re.compile(r"[0-9a-f]{7,64}")


def _merge_base_now(gh_repo: str, base_ref: str, head_sha: str) -> str | None:
    """The TRUE merge base of `base_ref` and `head_sha` — the commit the branch
    actually forked from — or None if it cannot be had.

    **This used to return `baseRefOid` and that is the defect in #241.** GitHub
    maintains `baseRefOid` for its own purposes and recomputes it on a push to the
    head branch; it is not the merge base, and it has been measured wrong in both
    directions on this repo. On PR #187 it was OLDER than the fork point, because a
    commit shared with another PR landed on `main` and nothing recomputed the
    stored base — so `gh pr diff` returned code already on `main` and a full round
    was spent confirming 15 findings about it. On PR #270 it was NEWER: `f34aa89`,
    the tip of `main`, against a true fork point of `0819625` four commits back, so
    the recorded base named a commit the branch had never contained. The invariant
    to hold is the weak one — `baseRefOid` is not a merge base — and only asking
    for a merge base satisfies it.

    The compare endpoint's `merge_base_commit` IS `git merge-base <base> <head>`,
    computed by GitHub against the base branch as it stands now. Read from the API
    rather than from a local `git merge-base` because nothing else in this panel
    needs a checkout: the whole tool reads GitHub, runs anywhere `gh` is
    authenticated, and a local computation would be right only when the PR's head
    happens to have been fetched into whatever directory the panel was started in
    — silently falling back the rest of the time, which is the failure this exists
    to end.

    Bounded like its siblings, and None on every failure: a base commit is not
    required for a round to proceed, and a panel must never stall on a fact
    nothing gates on. The CALLER says in `config_notes` which base it ended up
    using — a fallback nobody is told about is the silent mis-scoping #241 is
    about."""
    if not (gh_repo and base_ref and head_sha):
        return None
    try:
        # `per_page=1` trims the commit list, which is the part of this response
        # that grows without bound on a long-lived branch. The file patches are
        # capped by the API at 300 and are downloaded either way — the cost is one
        # request on a path that then decides whether four seats and a judge run.
        got = panel_core.sh(
            ["gh", "api", f"repos/{gh_repo}/compare/{base_ref}...{head_sha}?per_page=1",
             "--jq", _MERGE_BASE_JQ], timeout=FIX_RANGE_TIMEOUT_S).strip()
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None
    return _commit_id(got) if _SHA_TEXT.fullmatch(got) else None


def _base_tip_now(gh_repo: str, base_ref: str) -> str | None:
    """The LIVE tip of the base branch. None if it cannot be had.

    The one field in this pair that actually moves, and the reason it needs its
    own call. `gh pr view --json baseRefOid` looks like the answer and is not:
    it reports the **merge base**, recomputed only when the head branch is
    pushed, and a merge base cannot move when the base branch advances — a
    common ancestor is unaffected by commits added to one side of it. Measured
    rather than assumed: PR #87 sat at `baseRefOid=88643c14` while `main` took
    ten commits, and `git merge-base` against `main` still answered `88643c14`
    afterwards.

    So a staleness check built on `baseRefOid` alone reads "unmoved, the review
    still stands" in exactly the case it exists to catch. Both ends are recorded
    because they answer different questions: `merge_base` is the PR's own base
    commit — what a whole-PR diff is built from, and what #41's tier-2 context is
    measured from under increment scope — while this is what the branch would be
    merged INTO.

    `git/ref/heads/…` rather than `commits/…`: it returns one object of a few
    hundred bytes where the commits endpoint ships the whole commit including its
    file list. Bounded and swallowed like :func:`_head_sha_now` — it runs on the
    critical path of every round, and nothing gates on it."""
    if not base_ref:
        return None
    try:
        got = json.loads(panel_core.sh(["gh", "api", f"repos/{gh_repo}/git/ref/heads/{base_ref}"],
                            timeout=FIX_RANGE_TIMEOUT_S))
        return _commit_id((got.get("object") or {}).get("sha"))
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


#: The buckets :func:`_provenance` sorts a new finding into. `unknown` is a real
#: answer and not a failure — it is what an unreadable fix range or an
#: unplaceable finding honestly leaves.
PROVENANCE = ("introduced", "missed", "missed-unread", "unknown")


def _provenance(file: str, line: int | None, added: dict[str, set[int]],
                unread: set[str], have_range: bool, all_unread: bool = False) -> str:
    """Did the previous round's FIX introduce this defect, or did that round MISS it?

    The two are one number today (`new_this_round`), and they want opposite
    remedies: self-inflicted findings say make fix passes smaller and more
    conservative, because more rounds will keep generating more work; missed ones
    say the earlier round under-read, and spending on coverage genuinely helps.
    Conflated, neither conclusion is available — including the one an operator
    has to draw at the cap.

    A SIGNAL, not a verdict, and recorded as one. A fix can break something at a
    distance, so a defect outside the fix's own lines is *evidence of* a miss
    rather than proof of one; #41 (review the increment) is what would make this
    exact, at which point a finding in the increment is introduced by
    construction and this heuristic can be retired.

    `missed-unread` is the honest bucket for a defect in a file the earlier round
    was truncated out of — a coverage failure rather than a reviewer failure, and
    the one bucket that indicts the harness instead of the panel. `all_unread`
    says that round read NOTHING (it was skipped, or lost every seat), which is
    the same failure with no file list to name it by.

    Two limits of the line-intersection rule itself, written down rather than
    fixed, because changing the matching rule trades a known bias for an unknown
    one and nothing gates on the answer:

    - **A defect a fix pass introduced by DELETING something is invisible here and
      reads as `missed`.** `added` only knows lines the fix pass ADDED, so removing
      a guard, a null check, a `finally` or an `await` introduces a defect with no
      added line to place it on. The `introduced` bucket therefore under-counts by
      however much of the fix pass was subtraction, and `missed` absorbs it.
    - **`introduced` requires EXACT membership in the added lines, and reviewer
      line numbers drift.** LLM reviewers routinely report a line a few off — the
      top of the enclosing function, the closing brace, the line after the defect —
      and Sonar reports the issue's own anchor, which need not be a line the fix
      wrote. Every one of those misses the set by a line or two and comes back
      `missed`. So the split is biased toward `missed` in BOTH directions, and the
      `introduced` count should be read as a floor rather than as a measurement.

    #41 (review the increment) HAS LANDED — `--scope increment`, v2.28, the default
    — and #512 is what acted on it: a round that reviewed the increment now
    attributes against that diff (`payload.fix_range_source == "increment"`) rather
    than re-fetching the span through :func:`_fix_range_diff`. What that removed is
    the second compare call, the anchor mismatch behind it, and the base-merge
    over-count named above; the two biases in this docstring it did NOT remove,
    because placing a finding in the increment is still a comparison and a deletion
    still has no added line to sit on.

    So this function is still reached, and still biased, on exactly the rounds #41
    never covered: `round_scope: pr`, and any round whose increment fell back. Read
    `fix_range_source` before reading these buckets.
    """
    if not have_range:
        return "unknown"
    # Which changed files this finding's path spelling could name. More than one
    # and nothing can be said: the suffix rule that lets `panel.py` match
    # `harness/loops/panel.py` also lets it match a second tree's copy, and a
    # coin toss between two files is not a measurement.
    hits = [f for f in added if _same_file(file, f)]
    if line is not None and len(hits) == 1 and line in added[hits[0]]:
        return "introduced"
    # Checked before the unplaceable cases: "the earlier round could not see this
    # file" is a better answer than "we could not place it", and a finding with
    # no line in an unread file is still squarely a coverage failure.
    if all_unread or any(_same_file(file, f) for f in unread):
        return "missed-unread"
    # An empty path is as unplaceable as a missing line, and belongs in the same
    # guard. Falling through to `missed` reads as "the earlier round looked at
    # this and did not see it" about a finding that cannot be placed anywhere,
    # which is exactly the invented attribution this bucket exists to avoid.
    if line is None or not file or len(hits) > 1:
        return "unknown"
    return "missed"


#: Where a new finding sits relative to the fix pass that preceded it — #67's
#: third question, beside #48's ``provenance`` and #24's ``new_this_round``.
#:
#: Those two ask what a round FOUND. This asks whether the round before it made
#: any progress on the same defect: *is this finding standing where the last fix
#: pass was working, on a complaint that pass was sent to answer?* A fix that
#: patches a wrong assumption produces the next round's findings; a fix that
#: removes the assumption does not.
#:
#: **The names describe a POSITION and not a verdict, and that is a correction
#: this made under measurement.** It was first written with a bucket called
#: `circling`, and replaying it over 36 rounds of this board's own history said
#: the word could not be earned — see :func:`_recurrence` for the numbers.
#: `revisited` says what is actually known: the previous round complained about
#: this file, the fixer wrote lines in it, and here is a fresh finding on top of
#: what it wrote. Whether that is one premise being patched twice or two
#: unrelated bugs in a busy file is the judge's question
#: (:data:`panel_core.PREMISE_VERDICTS`), not this one's.
#:
#: ``unknown`` is a real answer here for exactly the reason it is one under
#: :data:`PROVENANCE`: an unreadable fix range or an unplaceable finding leaves
#: the question asked and unanswered, which is not the same as never asking it
#: (that one is NULL).
RECURRENCE = ("revisited", "fix-site", "elsewhere", "unknown")

#: How near a finding must sit to a line the fix pass WROTE before it counts as
#: standing at that pass's site, in lines.
#:
#: Wider than :func:`_provenance`'s rule, deliberately, and the two are measuring
#: different things over the same input. ``introduced`` demands exact membership
#: because it is an accusation about authorship, and its docstring records that
#: reviewer line-drift therefore biases it toward ``missed``. This is not an
#: accusation about authorship — it asks whether the fixer was WORKING HERE — so
#: the drift that ``introduced`` must refuse is the drift this has to absorb. A
#: reviewer that names the top of the enclosing function, the closing brace or
#: the line after the defect is describing the same neighbourhood.
#:
#: **Twenty is a guess, it is written down as one, and the replay says the guess
#: barely matters.** Every radius from 0 to 20 moves the ``revisited`` rate on
#: #67's own cases and on the controls by the SAME amount (see
#: :func:`_recurrence`), so nothing here is tuned and there is nothing yet to
#: tune it against. It is roughly a short function, it is not a dial, and it is
#: not read by anything that stops a run — which is the whole design: the number
#: can be wrong for a few dozen cycles and cost nothing but a mislabelled row,
#: and those rows are the data that would settle it.
SITE_RADIUS = 20


def _recurrence(file: str, line: int | None, added: dict[str, set[int]],
                fixed_here: dict[str, set[str]], have_range: bool,
                radius: int = SITE_RADIUS) -> tuple[str, str | None]:
    """``(bucket, the earlier finding this one stands on)`` — where is this finding
    relative to the fix that preceded it, and was that fix answering a complaint
    about the same place?

    A MEASUREMENT, and nothing reads it to stop a run. #67 asks for it before
    anything gates on it and says why in one line: two pull requests in one day is
    an observation, not a calibrated rule. What follows is the first evidence
    about whether it generalises, and it argues the same way.

    Three predicates, and ``revisited`` is the conjunction:

    1. the previous round raised a finding in this file, and it was work that
       round's fixer was ASKED to do (``fixed_here`` is built from the findings a
       fix pass was briefed on, never from the ones the judge dismissed — a
       dismissed finding is nobody's premise);
    2. the fix pass wrote lines in this file;
    3. this finding sits within ``radius`` lines of one of them.

    Two of three — the fixer worked here, but no earlier round had complained
    about here — is ``fix-site``: fresh damage at the site, which is real
    information and is not the same news. Neither is ``elsewhere``.

    **What the replay found, and why this is called `revisited` rather than
    `circling`.** Run over 36 rounds from 26 pull requests on this board — every
    multi-round cycle it holds — ``revisited`` fires on a MEDIAN of about 80% of a
    round's new findings, and it does not separate the three cycles #67 identifies
    as circling (#61, #29, #88) from the rest of the dataset:

    ==========================  ================  ==============
    narrowing                   #61 / #29 / #88   every other PR
    ==========================  ================  ==============
    file + within 20 lines            83%              69%
    file + within 5 lines             79%              64%
    file + exactly on a line          65%              52%
    ...and prior finding ±20          29%              27%
    ==========================  ================  ==============

    Tightening the rule lowers both columns together. There is no radius at which
    this becomes a detector, and the reason is visible in the raw runs: under #41's
    increment scope a later round is READING the fix commit, so a new finding at
    the fix's site is the ordinary case rather than the exceptional one. That is
    the same fact ``provenance``'s ``introduced`` bucket reports, and a bucket that
    fires four times in five carries very little.

    So this half is context and a denominator, not a verdict — which is exactly
    what #67's own comment on PR #88 predicted when it said the grouping key needed
    is "not 'same file' but 'same way of being wrong'". The half that can see shape
    is the judge's (:data:`panel_core.RECURRENCE_BRIEF`), it is asked separately,
    and its answer is stored beside this one rather than folded into it: two
    witnesses, and the rows where they disagree are the interesting ones. Storing
    the negative result rather than deleting the measurement is the point — a rate
    that saturates is itself a finding about the loop, and it is the baseline any
    later rule has to beat.

    **The two ends are measured at different grains, and that is not an
    oversight.** The new finding is placed to the line; the earlier one only to
    the file. ``added`` holds NEW-side line numbers, so an earlier round's line
    number — written against the file as it stood BEFORE the fix — is not on the
    same axis and cannot be compared to them without reconstructing the fix's line
    mapping. File-grain on that end is the honest reading of what is actually
    known, the same call :func:`_diff_files_cut` makes and for the same reason.
    (The replay's last row is what a line-grain prior end would buy, ignoring that
    caveat: it halves both columns and separates neither.)

    A longer circle is out of scope too: a premise raised in round 1, quiet in
    round 2 and back in round 3 is invisible here, because the fix range under
    attribution is one round wide and ``fixed_here`` is the round that anchored it.
    """
    if not have_range:
        return "unknown", None
    # Same ambiguity guard as `_provenance`, and it has to be the same one: the
    # suffix rule that lets `panel.py` match `harness/loops/panel.py` also lets it
    # match a second tree's copy, and a coin toss between two files is not a
    # measurement.
    hits = [f for f in added if _same_file(file, f)]
    if line is None or not file or len(hits) > 1:
        return "unknown", None
    # NO hit is an answer, and a different one from an ambiguous hit. The fix pass
    # did not write in this file at all, so wherever this finding is, it is not
    # where that pass was working — which is `elsewhere` on the nose. Folding it
    # into `unknown` (the first spelling of this, and the one an end-to-end test
    # caught) made `elsewhere` almost unreachable: every finding outside the fix's
    # own files landed in the bucket that means "we could not tell", so the one
    # bucket that says a round looked away from the fix never fired.
    if not hits:
        return "elsewhere", None
    if not any(abs(line - wrote) <= radius for wrote in added[hits[0]]):
        return "elsewhere", None
    # Which earlier finding this one is standing on. Sorted, so a file the last
    # round raised two findings in names the same one on every run — an arbitrary
    # pick that moved between runs would make the column unauditable, which is the
    # one thing an uncalibrated signal cannot afford.
    was = sorted({k for path, keys in fixed_here.items()
                  if _same_file(file, path) for k in keys})
    return ("revisited", was[0]) if was else ("fix-site", None)


#: The key :func:`_diff_by_file` files anything before the first ``diff --git``
#: header under. Empty, so it can never collide with a path, and falsy, so the
#: callers that count FILES can skip it in one word.
DIFF_PREAMBLE = ""


def _diff_by_file(diff: str) -> dict[str, str]:
    """Split a unified diff into one text chunk per file, keyed the way
    :func:`_diff_file_path` keys it (the ``b/`` side).

    Used to sort the PR's diff into the files an increment touched and the files
    it did not, so a round reviewing the fix commit can be handed the rest of
    those files IN FULL before it is handed anything else. The seam between the
    fix and the code it landed in is the defect class the panel/fix cycle exists
    to catch (#24's motivating bug was a mirror added in one file meeting an early
    ``return`` in another), and that seam is inside the files the fix touched.

    Nothing is dropped, because the result is joined back into a prompt: a header
    that will not parse is keyed by the whole header line, and a preamble before
    the first header is keyed by :data:`DIFF_PREAMBLE`. Both then match no
    increment file and fall to the outer context tier, which is the harmless
    direction here — where dropping them would delete text from the reviewer's
    copy of the PR. (:func:`_diff_added_lines` drops an unparseable header, which
    is the harmless direction *there*: a line nobody can attribute scopes no
    Sonar issue. The two need not agree — the near/far tiering matches this
    function's keys against its own, and nothing matches the two together.)"""
    out: dict[str, list[str]] = {}
    cur = DIFF_PREAMBLE
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            cur = _diff_file_path(line) or line.strip()
        out.setdefault(cur, []).append(line)
    return {k: "".join(v) for k, v in out.items()}


def _diff_subset(by_file: dict[str, str], keep: set[str]) -> str:
    """The chunks of an already-split diff for the files in ``keep``, in their
    original order.

    This is what keeps an increment about the PR. A commit range between two
    rounds spans whatever the fixer did INCLUDING a merge of the base branch, and
    on this repo that is the normal case rather than a corner — landing six PRs
    in a day took eleven integration merges (#80). Measured on PR #62, the raw
    range between two of its rounds was 92,415 chars against a 45,370-char PR:
    the "increment" was twice the size of the whole thing, because it carried
    every unrelated file main had gained in between.

    Restricting to the PR's own files does not make the range perfect — main's
    changes to a file the PR also touches still ride along — but it removes the
    part that is both largest and certainly not the fixer's work. The size guard
    in :meth:`ReviewScope.decide` covers what is left.

    Takes the mapping rather than the text because its caller needs the same
    split to count what was left out: splitting twice is two partitions of one
    string that have to agree, and the cheapest way to keep them agreeing is for
    there to be one."""
    return "".join(by_file[f] for f in by_file if f in keep)


def _fit_parts(parts: list[str], budget: int | None) -> list[str]:
    """Spend one budget across several texts in PRIORITY order: each takes what
    it needs, the next takes what is left, and the tail gets "".

    This is what makes increment scope cheaper rather than merely different. The
    old rule cut one diff at one ceiling, so the thing lost was whatever happened
    to sort last in the diff — a test file, a migration, the end of the change.
    Here the review TARGET is always first, so a budget too small to hold
    everything drops context and never the thing under review.

    ``None`` means uncapped and returns the parts whole. A budget of zero or
    less is no capacity and every part gets "" — clamped up front and not only
    inside the loop, because ``part[:left]`` with a negative ``left`` returns
    everything BUT the last ``|left|`` characters, which is the opposite of what
    a caller asking for nothing meant and would hand a reviewer a target with its
    tail quietly removed.

    The summed allocation is monotone non-decreasing in ``budget``.
    :func:`fit_argv_budget` shrinks a budget until the RENDERED prompt fits, and
    the rendered prompt is this plus :func:`_compose`'s frame, where a section's
    ``[cut: …]`` marker disappears once that section becomes whole — so the
    rendered length can fall by one marker's width as the budget rises.
    :meth:`ReviewScope._compose` reserves each marker's width out of the budget
    before spending it, which bounds that wobble to what a marker occupies and
    keeps the rendered prompt inside the budget it was given."""
    if budget is None:
        return list(parts)
    out: list[str] = []
    left = max(0, budget)
    for part in parts:
        out.append(part[:left])
        left = max(0, left - len(part))
    return out


def fetch_increment(gh_repo: str, since: str, head: str) -> tuple[str, str]:
    """The diff between two commits — what the last fix pass actually wrote, or
    (with ``since`` = the base branch) the PR as an earlier round saw it — as
    ``(diff, problem)``, with ``problem`` empty on success.

    Fetched from GitHub's compare API rather than from a checkout ON PURPOSE.
    #75 established that ``cfg["path"]`` is the main checkout sitting on whatever
    branch it was last left on, and never the PR's code: a reviewer pointed there
    can quote a different branch as the code under review, which is a plausible
    wrong answer replacing a visible failure. The panel reads a PR as a diff and
    checks nothing out, and this keeps that true.

    **Three dots, not two.** The API 404s on ``a..b`` and accepts only ``a...b``,
    which is diff(merge-base(a, b), b). For the normal case — the fixer added
    commits on top — the merge base IS ``since`` and the two are identical. When
    the branch was force-pushed or rebased between rounds the merge base moves
    back and the "increment" widens toward the whole PR. That is the safe
    failure: the round re-reads more than it needed to, which costs budget, where
    the two-dot answer would have been a diff against a commit no longer in the
    history — code the round would report on as though it were new.

    **Never raises — that is the contract, and `except Exception` is how it is
    kept.** The caller has no `try` around it, because a scope optimisation must
    not be able to kill a review that would otherwise have happened. Naming the
    two obvious families was not enough: ``sh`` runs with ``text=True``, so a diff
    that is not valid UTF-8 raises ``UnicodeDecodeError`` — a ``ValueError``,
    caught by neither — and a ``timeout=`` passed through ``sh`` one day would
    raise ``TimeoutExpired``, a ``SubprocessError``, also caught by neither. The
    two that get their own branch get a better message, not a different fate."""
    what = f"the diff {since[:8]}...{head[:8]}"
    try:
        diff = panel_core.sh(["gh", "api", f"repos/{gh_repo}/compare/{since}...{head}",
                   "-H", "Accept: application/vnd.github.diff"])
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()
        return "", (f"could not fetch {what} "
                    + (f"({tail[-1][:120]})" if tail else "(gh api failed)"))
    except Exception as e:      # every one of them, per the contract above
        return "", f"could not fetch {what} ({e.__class__.__name__})"
    return diff, ""


#: What GitHub's compare endpoint stops at. Documented as "up to 250 commits" and
#: "responses that include comparisons of more than 300 files will be truncated",
#: and the diff media type cannot be paginated, so a range at either ceiling can
#: come back short with a 200 and no error.
#: https://docs.github.com/en/rest/commits/commits#compare-two-commits
COMPARE_FILE_CAP = 300


def _count(facts: dict, key: str) -> int:
    """One of the compare endpoint's own counts, or 0 when it is not a number.

    :func:`compare_facts` promises never to raise and its caller has no ``try``
    around it, so the promise has to survive the READING of what it returned as
    well: a field that came back the wrong shape (a `gh` whose `--jq` was ignored,
    a hand-rolled double, a future API change) would otherwise raise ``TypeError``
    out of :meth:`ReviewScope.decide` and kill a review every reviewer CLI has
    already been paid for, over a scope optimisation."""
    try:
        return int(facts.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def compare_facts(gh_repo: str, since: str, head: str) -> dict:
    """The compare endpoint's OWN account of the range it just returned a diff
    for: ``status``, how many files and commits it covers, and how many of those
    commits are merges. ``{}`` when it could not be read.

    Fetched because the diff alone cannot answer three questions the review
    target's honesty rests on, and one wrong answer to any of them is silent:

    - **was it complete?** A truncated compare is a 200 with fewer files in it.
      It still passes the "smaller than the PR" guard and still looks like a fix
      commit, so a target missing half the fix would be reviewed as the whole of
      it. Comparing the file COUNT against the diff we parsed catches that.
    - **was it an increment at all?** ``a...b`` is measured from the merge base,
      so after a force-push or a rebase it is not the delta from ``a``: anything
      the fixer REVERTED between the two heads is in neither. ``status`` says so
      (``ahead`` is the case the feature is for).
    - **whose changes are in it?** A merge commit in the range means main's
      changes to files the PR ALSO touches are in the target, where no file
      filter can reach them and a reviewer will read them as the fixer's.

    Never raises, for the same reason :func:`fetch_increment` does not: this is
    an assurance about a scope optimisation, not a review."""
    try:
        raw = panel_core.sh(["gh", "api", f"repos/{gh_repo}/compare/{since}...{head}",
                  "--jq", "{status: .status, files: (.files // [] | length), "
                          "commits: (.commits // [] | length), "
                          "total_commits: (.total_commits // 0), "
                          "merges: ([.commits // [] | .[] "
                          "| select((.parents // []) | length > 1)] | length)}"])
        facts = json.loads(raw)
    except Exception:           # every one, per the contract above
        return {}
    return facts if isinstance(facts, dict) else {}


def _range_notes(facts: dict, since: str, head: str, round_no: int) -> list[str]:
    """What the compare endpoint said about a range this round is still going to
    review — the caveats that degrade an increment without disqualifying it.

    Neither is inferable from the material a reviewer is handed: a reverted change
    is absent from it, and a merged-in change looks exactly like the fixer's."""
    out = []
    if not facts:
        # Said rather than swallowed. The increment is still used — the diff came
        # back and the diff is the thing being reviewed — but the checks below did
        # not run, and "no caveat" would otherwise read as "checked, nothing wrong".
        return [f"round {round_no}'s increment was not checked against GitHub's own "
                f"account of {since[:8]}...{head[:8]} (the compare metadata could not be "
                "read), so a truncated, rebased or merge-carrying range would not have "
                "been reported"]
    status = str(facts.get("status") or "")
    if status and status != "ahead":
        out.append(
            f"the range {since[:8]}...{head[:8]} is `{status}`, not `ahead`: the branch was "
            "rebased or force-pushed since the anchor, so the target is measured from the "
            "merge base and anything REVERTED between the two heads is in neither the "
            "target nor the context")
    merges = _count(facts, "merges")
    if merges:
        out.append(
            f"the increment {since[:8]}...{head[:8]} contains "
            f"{merges} merge commit(s). Files this PR does not touch were left "
            "out of the target, but main's changes to files it DOES touch are still in there "
            "and cannot be told apart from the fixer's")
    return out


# ------------------------------------------------------------------ #278: after an
# integration, how much of the merge is genuinely NEW material to this PR.
#
# The two orders this settles. "Review, then integrate" throws the round away,
# because any integration moves the head and `preland` reads a moved head as a
# review of earlier code. "Integrate, then review" pays for a fresh cycle whatever
# the merge contained. Both spend the same amount however trivial the merge was,
# and neither looks at what actually changed.
#
# So neither order is applied blindly. After an integration the question is how much
# of the merge is new material TO THIS PR, and the answer decides which order was the
# right one for that merge:
#
#   DISTANT   the merge touched nothing this PR touches, and the resolution was
#             trivial or absent. The earlier round STANDS. Nothing is claimed as
#             reviewed that was not, because the merged code is not this PR's change
#             and is not what the findings are about.
#   INVOLVED  real conflicts, resolved by hand, in code this PR also touches. That
#             resolution is unreviewed work and gets reviewed — only that part, not
#             the whole PR again, which is `--scope increment` pointed at the range
#             between the round and the merge rather than a new mechanism.
#
# WHAT IT DOES NOT SEE, stated rather than hidden. The churn below is everything the
# range put into this PR's own files: a hand resolution, `main`'s own edits to those
# files, and anything a fixer pushed alongside. `_range_notes` already says in as many
# words that the last two cannot be told apart from each other, and nothing here
# improves on that. It does not need to — every one of them is material the earlier
# round did not read, sitting in the code this PR is about, and the reading is "how
# much unreviewed material is in this PR's own files", which is the honest question.
# The one thing the count alone could get wrong is calling a small PUSH an
# integration, and that is why a range with no merge commit in it is never distant.

#: The three readings of a head that moved after the round that reviewed it.
#: `unread` is a real answer and not a failure: a range this cannot measure is
#: reported as unmeasured and treated as involved by every caller, because a
#: precondition that could not be read is not a satisfied one.
MERGE_DISTANT, MERGE_INVOLVED, MERGE_UNREAD = "distant", "involved", "unread"


def _changed_lines(chunk: str) -> int:
    """Added plus deleted lines in one file's slice of a unified diff.

    Both sides counted, not just additions. A resolution that DELETES a landed fix
    is the #80 incident this whole reading exists to catch — a function that had
    moved on one side meeting a `main` that already had it, git conflicting on
    neither and the second definition winning — and an added-lines measure scores
    that at zero and calls the merge distant.

    The `+++`/`---` file headers are skipped because they are not content and
    every file would otherwise carry two lines of churn it did not have, which at
    a low threshold is the difference between distant and involved on a merge
    that changed nothing.
    """
    n = 0
    for line in chunk.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line[:1] in ("+", "-"):
            n += 1
    return n


@dataclass(frozen=True)
class Integration:
    """What one commit range put into a PR, in the only terms the reading needs.

    Deliberately two facts and not a diff. The two callers stand in different
    places — the panel reads GitHub's compare API and never checks anything out
    (:func:`fetch_increment` says why), `preland` runs in a checkout and has real
    `git` — and a shape that carried the diff itself would force one of them to
    fetch what the other already has. What they share is the JUDGEMENT, which is
    the part that must not drift.

    ``churn`` is this PR's OWN files that the range changed, mapped to changed
    lines; ``None`` when the range could not be read at all, with ``problem``
    saying why. ``merges`` is how many merge commits the range contains — only
    ever tested for being nonzero, because "was there an integration in here" is
    the whole question it answers.
    """

    churn: dict[str, int] | None = None
    merges: int = 0
    problem: str = ""

    @classmethod
    def from_diff(cls, range_diff: str, pr_files: set[str], merges: int) -> Integration:
        """An ``Integration`` from the range's unified diff and the PR's own file
        set — the shape the panel already holds, since
        :meth:`ReviewScope.decide` has both by the time it asks."""
        return cls(churn={f: _changed_lines(v)
                          for f, v in _diff_by_file(range_diff).items()
                          if f and f in pr_files},
                   merges=merges)

    @property
    def lines(self) -> int:
        return sum((self.churn or {}).values())


@dataclass(frozen=True)
class MergeReading:
    """Which reading a moved head got, and the sentence that says so.

    ``why`` is carried rather than rebuilt by each caller on purpose. The round
    writes it into `config_notes` and `preland` writes it into the check's
    reasons or warnings, and those are two claims about the same coverage: a
    reader comparing a payload against a gate verdict must not have to work out
    whether the two were talking about the same merge.
    """

    verdict: str = MERGE_UNREAD
    lines: int = 0
    files: tuple[str, ...] = ()
    limit: int | None = DEFAULT_DISTANT_MERGE_LINES
    why: str = ""

    @property
    def distant(self) -> bool:
        """The one question every caller asks. `unread` is NOT distant, which is
        what makes an unreadable range fail toward the expensive answer."""
        return self.verdict == MERGE_DISTANT


def merge_involvement(step: Integration, limit: int | None,
                      span: tuple[str, str]) -> MergeReading:
    """How involved was the merge — the whole of #278's judgement, in one place.

    ``limit`` is `review_panel.distant_merge_lines`, resolved by
    :func:`panel_seats.distant_merge_lines`. ``None`` is the reading switched
    off, and it produces INVOLVED rather than UNREAD: nothing failed, the repo
    asked for the pre-#278 behaviour where any head move is a review of earlier
    code, and the sentence says which of the two happened.

    Never raises and takes no I/O. Both callers build the :class:`Integration`
    from something that already failed softly, so the failure has already been
    turned into ``churn=None`` by the time it arrives here.
    """
    where = f"{(span[0] or '')[:8]}...{(span[1] or '')[:8]}"
    if step.churn is None:
        return MergeReading(MERGE_UNREAD, limit=limit, why=(
            f"whether {where} is a distant integration could not be measured"
            + (f" ({step.problem})" if step.problem else "")))
    files = tuple(f for f, n in sorted(step.churn.items()) if n)
    lines = step.lines
    if limit is None:
        return MergeReading(MERGE_INVOLVED, lines, files, None, why=(
            "`distant_merge_lines` is null for this repo, so no head move is read as "
            f"distant and the reading was not taken — {where} put {lines:,} line(s) "
            "into this PR's own files"))
    if not step.merges:
        # Size is not consulted, and that is the point. A range with no merge in it
        # is a PUSH, and a push into this PR's own files is unreviewed work of
        # exactly the kind the round exists to read, however little of it there is.
        # Reading a small push as distant is the one way this measurement could
        # weaken a gate rather than sharpen it.
        return MergeReading(MERGE_INVOLVED, lines, files, limit, why=(
            f"{where} carries no merge commit, so it is a push and not an integration "
            f"— {lines:,} line(s) across {len(files)} of this PR's own file(s) went "
            "unreviewed, and size does not excuse a push"))
    if lines > limit:
        return MergeReading(MERGE_INVOLVED, lines, files, limit, why=(
            f"the integration {where} changed {lines:,} line(s) across {len(files)} of "
            f"this PR's own file(s), past the {limit}-line `distant_merge_lines` limit "
            "— an INVOLVED merge, and that resolution is unreviewed work"))
    return MergeReading(MERGE_DISTANT, lines, files, limit, why=(
        f"the integration {where} changed {lines:,} line(s) of this PR's own files, at "
        f"or under the {limit}-line `distant_merge_lines` limit — a DISTANT merge, so "
        "the round that ran before it is still a review of this PR's change"))


def _is_commitish(value: str) -> bool:
    """Does this look like a SHA — abbreviated or full? Used to decide whether two
    anchors can be compared by prefix, which is only meaningful for hex."""
    return bool(re.fullmatch(r"[0-9a-fA-F]{7,40}", value or ""))


def _is_ref(value: str) -> bool:
    """Can this value only address the ref it names?

    Every anchor — ``--since`` and a baseline's ``head_sha`` alike — is
    interpolated into a REST path (``compare/{since}...{head}``), and a baseline
    is a file the caller points at. There is no shell, so this is not injection,
    but ``..`` or a leading ``/`` walks to a different endpoint and a ``?``
    appends query parameters. Refs are far too permissive a grammar to whitelist
    (``--since main`` and ``--since v2.24`` are both reasonable), so this refuses
    only what would leave the endpoint. A well-formed anchor that is simply wrong
    needs no check: it 404s into the fetch-failed fallback, which explains
    itself."""
    return bool(value) and not (
        value.startswith(("-", "/")) or ".." in value
        or any(c in value for c in " \t\n?#%"))


def _same_commit(a: str, b: str) -> bool:
    """Are these two the same commit, allowing for one being abbreviated?

    ``--since`` is documented as taking a SHA and git SHAs are routinely written
    short, so a raw ``==`` against the head misses the unmoved-head case for
    anyone who typed seven characters — and the round then fetches an empty range
    and reports "the head moved without the PR's content moving", which is a
    description of something that did not happen."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    if not (_is_commitish(a) and _is_commitish(b)):
        return a == b
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def _prior_round(since_round: int | None, round_no: int) -> str:
    """How to name the round that reviewed the anchor, in text a reviewer or an
    operator reads.

    Not ``round_no - 1``: :func:`load_baseline` deliberately keeps an older anchor
    when the newest baseline names no commit, so a round 3 can be anchored on
    round 1's head. Telling its reviewers "Round 2 reviewed this PR at <round 1's
    sha>" states a falsehood in the very sentence that defines what they are to
    treat as already read, to the one audience that cannot check it."""
    if since_round is None:
        return "an earlier round"
    return f"round {since_round}"


#: The header a whole-PR round puts above its diff. Unchanged from every release
#: before scope existed, so a "pr" round's prompt is byte-identical to what it
#: has always been — the comparison between an increment round and a whole-PR
#: round is only worth anything if the second one did not also change.
PR_SCOPE_HEADER = "--- DIFF ---"

INCREMENT_BRIEF = """This is round {round_no} of a panel -> fix -> panel cycle, and it is scoped.
{prior_round} reviewed this PR at {since8}; a fixer has written more since. What changed
between them is YOUR REVIEW TARGET and comes first below. The PR AS IT STOOD AT {since8}
follows it as CONTEXT, and the target is where your effort belongs.

Read the context anyway, and read it hardest where the target touches it. What a fix pass
breaks, it usually breaks at the seam — the new code is correct on its own terms and wrong
where it meets what was already there. A defect that is only visible in the target BECAUSE of
what the context does is exactly what this round exists to find.

**A defect nobody has raised yet is in scope wherever you find it, context included.** Earlier
rounds read that code; reading it is not the same as being right about it, and they are
demonstrably wrong about some of it. What is out of scope is re-reporting a defect an earlier
round already raised — the fix for those is in the target you are reading, not in the context,
which is why the context does not show it.

If the context you were given is not enough to judge something, say so in `could_not_assess`
rather than guessing. Being short of context is expected here and saying so is useful; a
confident answer built on a file you could not see is not."""

JUDGE_INCREMENT_BRIEF = """This round of the panel was SCOPED, and you are seeing what the reviewers saw.
{prior_round} reviewed this PR at {since8}. The reviewers' target was what a fixer has
written since, shown first below; the PR as it stood at {since8} follows as context, which they
were told an earlier round had read.

Two consequences for your ruling, and they pull in opposite directions:

- A finding about the CONTEXT is not automatically out of scope. A defect in the target that
  is only visible against the code it landed in is precisely what this round was run to find,
  and it should be confirmed on its merits.
- What is out of scope is a finding an earlier round ALREADY RAISED, whose fix is in the
  target rather than in the context. A defect in the context that nobody has raised is NOT out
  of scope merely for sitting outside the target: earlier rounds read that code, which is not
  the same as being right about it, and the reviewers were told so."""


@dataclass
class ReviewScope:
    """What one round hands its reviewers, in the order it would rather lose.

    A round past the first exists to read the fix commit (#24) and is instead
    handed the whole PR — the fix plus everything earlier rounds already read and
    confirmed — and pays for all of it in budget, wall-clock and attention on
    every round. PR #34's four rounds went 140 KB -> 292 KB *because it was being
    reviewed*, until both reviewers declared they could not read ~600 lines of one
    test file. This is the thing that inverts that: the target stays about the
    size of one fix commit however large the PR grows, and the context absorbs
    the squeeze.

    Three tiers, and the order is the whole design:

    1. **the target** — the increment, never cut while anything else is present
    2. **near context** — the files the target also touches, AS THEY STOOD AT THE
       ANCHOR, because the seam between the fix and the code it landed in is where
       a fix pass does its damage, and that seam is inside these files
    3. **far context** — the rest of the PR, whatever budget survives

    Tier 2 is taken from ``base...anchor`` — the PR as the last round reviewed it
    — and not from the PR's current diff for those files. Sliced out of the
    current diff it would CONTAIN the increment, since the fix commit is part of
    the PR: the target would be sent twice, the second copy under a header saying
    an earlier round had already dealt with it, which is the one thing both briefs
    tell a reviewer not to re-report. The header can only be true if the material
    under it predates the fix.

    Under ``"pr"`` scope there is only tier 1 and it is the whole diff, so the
    prompt is byte-identical to the pre-scope one."""

    scope: str = "pr"
    #: The whole PR, as `gh pr diff` returns it.
    diff: str = ""
    #: Commits since ``since`` — empty under "pr" scope.
    increment: str = ""
    #: The PR as of ``since`` (``base...since``) — what the round that anchored
    #: this one actually read. Empty under "pr" scope.
    prior_diff: str = ""
    since: str = ""
    round_no: int = 1
    #: Which round supplied the anchor, when that is known. Usually
    #: ``round_no - 1``, but `load_baseline` deliberately keeps an older anchor
    #: when the newest baseline names no commit, and the brief must not then tell
    #: the reviewer a round number that did not review that commit.
    since_round: int | None = None
    #: The anchor-era changes to the files the target touches (tier 2), and the
    #: PR's changes to every other file (tier 3). Derived, never passed: they come
    #: out of `diff` and `prior_diff` keyed by `increment`, and letting a caller
    #: supply them separately is letting the three disagree.
    near: str = field(default="", init=False)
    far: str = field(default="", init=False)
    #: What labels the material under "pr" scope. Defaults to
    #: :data:`PR_SCOPE_HEADER`, so every round that has ever run is byte-identical
    #: to before; it is a parameter at all because #138's move manifest travels
    #: through this class as its `diff` and must not be announced as one.
    #:
    #: Substituting the material rather than adding a fifth review mode is what
    #: keeps the manifest inside the machinery it needs: per-seat budgets, the
    #: truncation measurement, the judge seeing what the parties saw, the board
    #: record and the rounds arithmetic all work on `diff` and none of them had to
    #: learn a new shape. What they then measure is the manifest's own length,
    #: which is the honest thing to measure — "was this seat handed the whole
    #: manifest" is the question, and the PR's char count is recorded separately
    #: in the pre-flight block.
    #:
    #: **Only meaningful under "pr" scope, and enforced rather than documented.**
    #: `_compose` interpolates it in the whole-target branch; the increment branch
    #: renders a brief and three labelled tiers, and there is no place in that
    #: layout for a single header that would be true of all of them. Today's only
    #: caller that sets one also sets `scope="pr"` (the manifest, which IS a whole
    #: target by construction), so nothing is wrong — but a future caller passing
    #: both would have got no error and no header, which is the class of quiet
    #: mismatch every other field on this class is written to prevent.
    header: str = PR_SCOPE_HEADER

    def __post_init__(self) -> None:
        if self.header != PR_SCOPE_HEADER and self.scope == "increment":
            # Raised rather than noted: this is a caller holding two settings that
            # contradict each other, which is a bug in the caller, and a scoped
            # prompt silently missing a header it was told to carry is exactly the
            # kind of failure that reads as fine until somebody compares two rounds.
            raise ValueError(
                f"header={self.header!r} was given with scope='increment', which "
                "composes a brief and three labelled tiers and has nowhere to put "
                "one — a custom header is only meaningful for a whole-target 'pr' "
                "scope, and being ignored in silence is worse than being refused")
        if self.scope != "increment":
            return
        # Real file keys only. A preamble is keyed by "" in every mapping, so
        # leaving it in `touched` would match the PR diff's own preamble and drop
        # it out of the far tier — text deleted from the reviewer's copy by a
        # coincidence of keys.
        touched = {f for f in _diff_by_file(self.increment) if f}
        # Both comprehensions iterate a dict, which is insertion-ordered, so the
        # prompt follows the diff's own order — the order `_diff_subset` promises
        # for the target — and two runs of one round compose the same prompt.
        # `touched` is only ever an `in` test, and a set is not iterated here.
        self.near = "".join(v for f, v in _diff_by_file(self.prior_diff).items()
                            if f in touched)
        self.far = "".join(v for f, v in _diff_by_file(self.diff).items()
                           if f not in touched)

    @classmethod
    def decide(cls, want: str, round_no: int, diff: str,
               commits: tuple[str, str], gh_repo: str, base: str = "",
               since_round: int | None = None,
               distant_lines: int | None = DEFAULT_DISTANT_MERGE_LINES
               ) -> tuple[ReviewScope, list[str]]:
        """Pick this round's scope and fetch what it needs, as ``(scope, notes)``.

        ``commits`` is (the anchor the previous round reviewed, this round's
        head) — the range an increment would cover. The anchor is ``--since`` if
        the caller passed one, else the ``head_sha`` of the latest baseline, else
        "". ``since_round`` is the round that supplied it, when a baseline did;
        ``base`` is the PR's base branch, which the near context tier is taken
        from. ``distant_lines`` is `review_panel.distant_merge_lines` (#278) and
        buys one sentence rather than a branch: when the range carries an
        integration merge, the round SAYS which reading that merge got and why,
        so a payload that stood on a distant merge and one that re-read a hand
        resolution are not left to be told apart by inference.

        **Every fallback to whole-PR scope produces a note.** A round that says
        it reviewed the increment and in fact re-read the PR is wrong about the
        one measurement this feature exists to produce, and it would be invisible
        in the numbers: ``diff_chars`` would simply be large, which is what it
        always was. Each way of ending up back at the whole PR is a different fact
        about the cycle, so each gets its own sentence rather than one "scope
        unavailable"."""
        anchor, head = commits
        notes: list[str] = []
        whole = cls(diff=diff, round_no=round_no)
        if want != "increment":
            return whole, notes
        if round_no <= 1:
            # Not a failure. Round 1 has nothing to be an increment from, and
            # `auto` reaches here on every round 1 of every cycle — so this is
            # silent unless an anchor was supplied, which is the one case where
            # the caller expected something else to happen. Which SOURCE supplied
            # it decides the wording: blaming --since for a baseline's `head_sha`
            # sends the reader looking for a flag they never passed.
            if anchor and since_round is None:
                notes.append("--since was passed on round 1, which has no earlier round "
                             "to be an increment from — the whole PR was reviewed")
            elif anchor:
                notes.append(f"a baseline for round {since_round} named a head, but this "
                             "run is round 1 and has no earlier round to be an increment "
                             "from — the whole PR was reviewed")
            return whole, notes
        if not anchor:
            notes.append(
                f"round {round_no} reviewed the whole PR, not the increment: no baseline "
                "said which commit it reviewed (`head_sha`). Pass --since <sha>, or a "
                "baseline written by v2.28 or later")
            return whole, notes
        if _same_commit(anchor, head):
            # A fact about the cycle rather than a failure, and a loud one: the
            # caller ran another round without the fixer pushing anything, so
            # there is no fix commit to read. Re-reviewing the PR is the useful
            # thing to do with a round that has already been paid for.
            notes.append(
                f"round {round_no} reviewed the whole PR, not the increment: the head is "
                f"still {head[:8]}, the same commit {_prior_round(since_round, round_no)} "
                "reviewed — nothing was pushed between the rounds, so there is no fix "
                "commit to read")
            return whole, notes
        raw, problem = fetch_increment(gh_repo, anchor, head)
        if problem:
            notes.append(f"round {round_no} reviewed the whole PR, not the "
                         f"increment: {problem}")
            return whole, notes
        # Down to the PR's own files. The range between two rounds also contains
        # whatever base branch the fixer merged in, which is not this PR's change
        # and not what the round is being run to read. Split ONCE: what goes into
        # the target and what was left out of it are two readings of one string,
        # and two splits are two partitions that can drift apart.
        by_raw = _diff_by_file(raw)
        raw_files = [f for f in by_raw if f]
        # The PR diff is split here for `mine` and again in `__post_init__` for the
        # far tier — one extra linear pass, kept on purpose. Threading the mapping
        # into the constructor is exactly the "letting a caller supply the tiers"
        # that field's comment refuses, and it would buy a pass next to two `gh
        # api` round trips.
        mine = {f for f in _diff_by_file(diff) if f}
        increment = _diff_subset(by_raw, mine)
        dropped = [f for f in raw_files if f not in mine]
        # Caveats DEGRADE an increment; they do not describe anything unless the
        # increment is what the round went on to review. Held back rather than
        # appended here, because every guard below returns the whole-PR scope and
        # a note about "the review target" is then an account of a target that was
        # discarded — beside the fallback note that says the whole PR was read.
        caveats: list[str] = []
        if dropped:
            # No cause is asserted. A base-branch merge is the usual one, but the
            # same set arises when the fixer REVERTED a file back to its base
            # state between the rounds — a normal way to address "this file should
            # not have been touched" — and `status` is still `ahead` then, so the
            # rebase caveat does not cover it either. Naming one of the two in the
            # single place an operator looks for the explanation gets it wrong
            # half the time.
            caveats.append(
                f"the increment {anchor[:8]}...{head[:8]} also touched {len(dropped)} "
                "file(s) this PR does not — a base-branch merge between the rounds, or "
                "files the fixer reverted out of the PR. They were left out of the "
                "review target")
        facts = compare_facts(gh_repo, anchor, head)
        said = _count(facts, "files")
        caveats.extend(_range_notes(facts, anchor, head, round_no))
        # #278's reading, and it goes into `notes` rather than into `caveats`
        # BECAUSE every guard below can still send this round back to the whole PR.
        # A caveat describes the review target and is discarded with it; this is a
        # fact about the MERGE, it stays true whichever way the fallbacks go, and it
        # is the one thing a reader must not have to infer — a round that stood on a
        # distant merge and one that re-read a hand resolution are different claims
        # about coverage.
        merges = _count(facts, "merges")
        reading = merge_involvement(Integration.from_diff(raw, mine, merges),
                                    distant_lines, (anchor, head))
        if merges and reading.verdict != MERGE_UNREAD:
            notes.append(
                f"round {round_no} follows an integration and takes the "
                f"{reading.verdict.upper()} reading: {reading.why}"
                + (". No round was required on this merge's account — read `scope` "
                   "for what this one looked at instead"
                   if reading.distant else
                   ". Read `scope` for whether this round's target was that "
                   "resolution, or the whole PR again past one of the fallbacks "
                   "below"))
        if said > len(raw_files) or said >= COMPARE_FILE_CAP:
            # The one class of degraded range that must not be reviewed anyway. A
            # truncated compare is a 200 with files missing from it: it is smaller
            # than the PR, it passes every guard below, and it becomes the REVIEW
            # TARGET — a fix commit reviewed as though the half that came back
            # were all of it, which is the exact failure `truncated` exists to
            # catch and the one place it cannot see.
            notes.append(
                f"round {round_no} reviewed the whole PR, not the increment: GitHub's "
                f"compare of {anchor[:8]}...{head[:8]} returned {len(raw_files):,} "
                f"file(s) against the {said:,} it reports for the range, and the endpoint "
                f"truncates past {COMPARE_FILE_CAP:,} — so the increment cannot be trusted "
                "to be the whole fix")
            return whole, notes
        if not increment.strip():
            notes.append(
                f"round {round_no} reviewed the whole PR, not the increment: the diff "
                f"{anchor[:8]}...{head[:8]} changed none of this PR's own files — the "
                "head moved without the PR's content moving (an empty commit, a rebase "
                "onto the same tree, or a merge that only brought in the base branch)")
            return whole, notes
        # The floor under the whole feature: a round must never cost MORE than it
        # did before scope existed. A big enough base-branch merge can leave the
        # restricted increment still larger than the PR — it carries main's
        # changes to files the PR also touches, which no file filter can remove —
        # and at that point the increment is neither cheaper nor sharper and the
        # justification for using it has gone.
        if len(increment) >= len(diff):
            notes.append(
                f"round {round_no} reviewed the whole PR, not the increment: the "
                f"increment since {anchor[:8]} is {len(increment):,} chars against the "
                f"PR's {len(diff):,} — a base-branch merge between the rounds made the "
                "range bigger than the thing it is a part of, so it is neither cheaper "
                "nor sharper")
            return whole, notes
        # The near context tier, and the second `gh api` call this costs. It is
        # the PR AS OF THE ANCHOR — what the round that anchored this one actually
        # read — because the alternative, slicing the current PR diff by the files
        # the fix touched, hands the reviewer the fix commit a second time under a
        # header saying an earlier round dealt with it already.
        #
        # Falls back to the whole PR rather than to a near tier we would have to
        # mislabel. Reviewing the whole PR is what this round did before v2.28 and
        # is never wrong, only dearer; a context section whose header is false is
        # wrong in the direction that suppresses findings.
        prior_diff, problem = fetch_increment(gh_repo, base, anchor) if base else (
            "", "no base branch was resolved for the PR")
        if problem:
            notes.append(
                f"round {round_no} reviewed the whole PR, not the increment: the "
                f"increment was fetched, but the PR as of {anchor[:8]} was not "
                f"({problem}) — and without it the context behind the fix cannot be "
                "shown as the earlier round saw it")
            return whole, notes
        # Past every fallback: the increment IS the target, so its caveats now
        # describe something.
        notes.extend(caveats)
        return cls(scope="increment", diff=diff, increment=increment,
                   prior_diff=prior_diff, since=anchor, round_no=round_no,
                   since_round=since_round), notes

    @property
    def target(self) -> str:
        """What this round is reviewing — the thing `diff_chars` measures and the
        thing a reviewer must never be silently handed a prefix of."""
        return self.increment if self.scope == "increment" else self.diff

    def material(self, budget: int | None) -> tuple[str, int, int]:
        """``(text, target_chars, context_chars)`` for one reviewer's budget.

        The counts are of what was actually SENT, after the cut, so a caller
        reporting them is reporting what the reviewer saw rather than what it was
        meant to see.

        A tier that got cut is LABELLED as cut, which is the one place this
        departs from how truncation has been handled until now. The old rule was
        that a truncated reviewer cannot notice its own truncation, so the panel
        measures it instead — still true of the target, which is why
        ``truncated`` is still measured and never asked for. But context is
        different: a reviewer told "the rest of the PR is here, minus the tail"
        can put the gap in ``could_not_assess`` and the judge can rule on it,
        which turns a silent omission into a declared one. Each marker's width is
        reserved out of the budget before the tiers are allocated, so a labelled
        cut cannot push the prompt past the ceiling that caused it."""
        return self._compose(budget, INCREMENT_BRIEF)

    def judge_material(self, budget: int | None) -> tuple[str, int, int]:
        """The same material, briefed for the adjudicator rather than for a party.

        The judge must see what the panel saw — ruling "not in the diff" while
        holding a different diff from the one the reviewers held is the one
        failure mode an independent adjudicator cannot recover from, and it would
        carry the authority of the final call. But it must not be told "YOUR
        REVIEW TARGET" and asked to review; its job is to rule."""
        return self._compose(budget, JUDGE_INCREMENT_BRIEF)

    def _compose(self, budget: int | None, brief_template: str) -> tuple[str, int, int]:
        if self.scope != "increment":
            # Cut at exactly the budget, with no allowance taken out of it for the
            # header: `max_diff_chars` has always meant "this many chars of diff"
            # under whole-PR scope, and a "pr" round's prompt is byte-identical to
            # what it has always been. The overhead below is a fact about the
            # scoped prompt, which did not exist before v2.28.
            body = _fit_parts([self.diff], budget)[0]
            return f"{self.header}\n{body}", len(body), 0

        brief = brief_template.format(
            round_no=self.round_no,
            prior_round=_prior_round(self.since_round, self.round_no).capitalize(),
            since8=self._since8)
        parts = [self.increment, self.near, self.far]
        # The budget buys the whole PROMPT, not just the diff text in it. The brief
        # and the section headers are over a kilobyte, they are added after the
        # budget has been spent, and they land on the side that matters: a model
        # whose context window is the reason the budget exists is handed more than
        # the number said, not less. Each cut marker is reserved too — the widest
        # form its own tier could produce — so a labelled cut cannot itself push
        # the prompt over.
        if budget is not None:
            budget = max(0, budget - len(self._frame(brief, "", "", ""))
                         - sum(_cut_note_reserve(p) for p in parts if p))
        target, near, far = _fit_parts(parts, budget)
        return (self._frame(brief,
                            target + _cut_note(target, self.increment),
                            near + _cut_note(near, self.near),
                            far + _cut_note(far, self.far)),
                len(target), len(near) + len(far))

    @property
    def _since8(self) -> str:
        """The anchor as it is written to a reader. One property, so the brief and
        the target header cannot disagree about it — they used to, and an empty
        anchor rendered "reviewed this PR at the previous round" above "what
        changed since  ". `decide` guarantees a non-empty anchor under increment
        scope, so the fallback is only reachable by constructing a scope by hand,
        which is exactly when the two lines would be read side by side."""
        return self.since[:8] or "the previous round"

    def _frame(self, brief: str, target: str, near: str, far: str) -> str:
        """The composed prompt around three already-cut, already-marked bodies.
        Also called with empty ones to measure its own overhead, which is why it
        is one function and not a literal at the call site: an overhead computed
        from a copy of the layout drifts from the layout.

        A tier that is empty gets no header. An empty far tier is ordinary — a PR
        whose every file the fix also touched has none — and a labelled section
        with nothing under it reads as material that went missing."""
        out = [brief, "",
               f"--- REVIEW TARGET: what changed since {self._since8} ---",
               target, ""]
        if self.near or self.far:
            # Not "already fixed". What an earlier round raised has been fixed, and
            # that fix is in the TARGET; this is the code it landed in, and the
            # briefs tell the reviewer in as many words that a defect nobody raised
            # is still in scope wherever it sits. A header claiming the section is
            # settled is the highest-salience text in the prompt and would argue
            # against the paragraph underneath it.
            out.append(f"--- CONTEXT: this PR as it stood at {self._since8}, which an "
                       "earlier round read — not the target ---")
        if self.near:
            out += ["--- the files the target touches, before the target changed them ---",
                    near]
        if self.far:
            out += ["--- the rest of the PR ---", far]
        return "\n".join(out)


def _cut_note(sent: str, whole: str) -> str:
    """The line that tells a reviewer this section is a prefix, or "" when it is
    whole. Says how much is missing in chars: "some of it" gives a reviewer
    nothing to calibrate a ``could_not_assess`` against, and the number is the
    difference between "the tail of one file" and "most of the PR"."""
    if len(sent) >= len(whole):
        return ""
    return (f"\n[cut: {len(sent):,} of {len(whole):,} chars shown — "
            f"{len(whole) - len(sent):,} not sent]")


def _cut_note_reserve(whole: str) -> int:
    """The widest marker :func:`_cut_note` can render for a tier this size,
    whatever the cut turns out to be — reserved out of a budget before the tiers
    are allocated, so a labelled cut cannot push the prompt past the ceiling that
    caused it.

    Not ``len(_cut_note(whole[:-1], whole))``. That reads as the widest case
    because two of the marker's numbers are at their longest when almost all of
    the tier was sent, but there is a THIRD, ``whole - sent``, and it is at its
    longest when ``sent`` is small. No single cut maximises all three: for a
    1,000,000-char tier the near-whole cut renders 17 digit characters
    (999,999 / 1,000,000 / 1) while a cut near the middle renders 23. So the
    bound is taken over the NUMBERS rather than over a guessed cut — none of the
    three can be wider than ``whole``'s own count.

    Reuses :func:`_cut_note` for the fixed text so the reservation cannot drift
    from what gets rendered: ``_cut_note("", whole)`` is that text with the sent
    figure at its narrowest (a single ``0``), which this then widens."""
    if not whole:
        return 0
    return len(_cut_note("", whole)) - len("0") + len(f"{len(whole):,}")


_SONAR_SEV = {"BLOCKER": "P1", "CRITICAL": "P1", "MAJOR": "P2", "MINOR": "P3", "INFO": "P3"}


def _sonar_findings(issues: list[dict]) -> list[Finding]:
    return [Finding(
        reviewer="sonarqube",
        severity=_SONAR_SEV.get(i.get("severity", "MINOR"), "P3"),
        file=(i.get("component", "").split(":")[-1] or "?"),
        line=i.get("line"),
        title=i.get("message", "")[:80],
        detail=i.get("rule", ""),
    ) for i in issues]


def _try(fn, *a):
    """Call fn(*a), or None if the API refused. Used where a partial answer beats
    no answer — the caller counts the Nones and says how many it lost."""
    try:
        return fn(*a)
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None


def review_sonarqube(sonar: dict, pr: dict,
                     changed_lines: dict[str, set[int]],
                     repo_path: str = "") -> tuple[str, list[Finding], list[Finding], str | None]:
    """Query SonarCloud/SonarQube for the PR.

    `pr` carries what identifies the change: `number`, `base`, and optionally
    `head` / `head_sha`. It is a dict rather than four more positional arguments
    because three tiers each need a different subset, and a five-argument call
    site is where the wrong branch name gets passed unnoticed.

    Returns (gate_status, hard_findings, soft_findings, skip_reason). Three tiers,
    best evidence first:

    1. The PR's own analysis: quality gate (HARD) + its PR issues.
    2. The HEAD BRANCH's analysis, if one exists at the PR's head commit: its
       gate is a real gate on this change's new code, so also HARD. Issues are
       scoped to the lines the PR adds, since a branch analysis reports the whole
       branch. This tier exists because PR analysis is not always available —
       where the SonarCloud org is bound to a different platform, a GitHub PR key
       cannot be resolved at all, and `sonar.branch.name` is the way in.
    3. Otherwise: open issues on the lines this PR ADDS, read from the BASE
       branch, as SOFT findings (judged on merits like any reviewer). The base
       branch's quality gate is NOT applied — it reflects all of that branch, not
       this PR, and would fail every PR.

    The fallback reads the branch this PR MERGES INTO (`base`), not the project's
    default branch, and that is the difference between findings and silence. On
    lexray, `test` is the integration branch and `main` lags it by a release
    train: measured on PR #1625 (2026-08-14), the default branch returned 33
    issues of which 0 fell on a line the PR added, while `base`=test returned 11
    of which 2 did. Reading a branch the PR is not based on doesn't merely add
    noise — stale line numbers stop intersecting the diff at all, so the reviewer
    reports nothing and reads as working.

    A base that Sonar has never analysed (an epic/stacked branch) answers 200 with
    total=0 rather than erroring, so it is checked against project_branches/list
    first and demoted to the default branch with a note. Silent zero is the one
    outcome this must never produce, because it is indistinguishable from a clean
    PR.
    """
    host = sonar.get("host") or os.environ.get(sonar.get("host_env", ""), "")
    org = sonar.get("organization", "")
    key = sonar.get("project_key", "")
    if not host:
        return "skipped", [], [], "sonarqube: host unset"
    if not key or key.startswith("TODO"):
        return "skipped", [], [], "sonarqube: project_key not confirmed"
    token = resolve_token(sonar, repo_path)
    if not token:
        return "skipped", [], [], ("sonarqube: token unavailable "
                                   "(env unset, no .env entry, op not signed in)")

    auth = base64.b64encode(f"{token}:".encode()).decode()
    hdr = {"Authorization": f"Basic {auth}"}
    org_q = f"&organization={org}" if org else ""
    ctx = _ssl_context()

    def api(path: str) -> dict:
        url = f"{host.rstrip('/')}/api/{path}"
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return json.loads(r.read().decode())

    pr_number = pr.get("number")
    base = pr.get("base", "")
    head = pr.get("head", "")
    head_sha = pr.get("head_sha", "")

    # 1) The PR's own analysis (hard quality gate), if it was scanned.
    try:
        gate = api(f"qualitygates/project_status?projectKey={key}&pullRequest={pr_number}")
        status = gate.get("projectStatus", {}).get("status", "no-analysis")
        issues = api(f"issues/search?componentKeys={key}{org_q}"
                     f"&pullRequest={pr_number}&resolved=false&ps=100")
        return status, _sonar_findings(issues.get("issues", [])), [], None
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return "skipped", [], [], f"sonarqube: HTTP {e.code}"
        # 404 == no analysis for this PR; try the head branch, then the base.
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        return "skipped", [], [], f"sonarqube: {e.__class__.__name__}"

    files = sorted(changed_lines)

    # 2) The head branch's own analysis. Its gate judges this change's new code
    #    against the base, so it is a REAL gate — but only for the commit it
    #    actually ran on. Branch analyses persist and are not superseded by a
    #    push, so an analysis three commits stale would gate confidently on code
    #    that is no longer there. Verified against the PR's head SHA, and
    #    declined (not reported stale-but-used) when they disagree.
    branch_note = None
    if head:
        try:
            branches = api(f"project_branches/list?project={key}").get("branches", [])
            entry = next((b for b in branches if b.get("name") == head), None)
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
            entry = None
        if entry:
            analysed = (entry.get("commit") or {}).get("sha", "")
            if head_sha and analysed and analysed != head_sha:
                branch_note = (f"sonarqube: branch analysis of '{head}' is at "
                               f"{analysed[:8]}, PR head is {head_sha[:8]} — stale, "
                               f"not used as a gate (rescan to get one)")
            else:
                try:
                    status = (entry.get("status") or {}).get("qualityGateStatus") or "no-analysis"
                    # safe="" so a branch name is fully escaped. The default
                    # leaves "/" alone, which is harmless in a query string —
                    # but a branch called `feat/a&b` would silently truncate the
                    # parameter and query the wrong branch.
                    branch_q = urllib.parse.quote(head, safe="")
                    raw = api(f"issues/search?componentKeys={key}{org_q}"
                              f"&branch={branch_q}&resolved=false&ps=500")
                    hard = [f for f in _sonar_findings(raw.get("issues", []))
                            if f.line in changed_lines.get(f.file, ())]
                    return status, hard, [], None
                except (urllib.error.HTTPError, urllib.error.URLError,
                        json.JSONDecodeError) as e:
                    branch_note = (f"sonarqube: head-branch analysis unreadable "
                                   f"({e.__class__.__name__})")

    # 3) Fallback: open issues on the PR's base branch, on the lines it adds (soft).
    if not files:
        return "no-pr-analysis", [], [], (
            f"sonarqube: PR #{pr_number} not scanned and no changed files to map")

    # Which branch to read. `fallback_branch` in .harness-rules pins it; otherwise
    # the PR's own base. Verified against the analysed set, because an unanalysed
    # branch returns an empty result rather than an error.
    want = sonar.get("fallback_branch") or base
    # A stale or unreadable head-branch analysis is the reason we are down here,
    # so it travels with the fallback's own caveats rather than being dropped.
    note = branch_note
    if want:
        try:
            branches = api(f"project_branches/list?project={key}").get("branches", [])
            known = {b.get("name") for b in branches}
            if want not in known:
                default = next((b.get("name") for b in branches if b.get("isMain")), "")
                note = ((note + "; ") if note else "") + (
                    f"sonarqube: base '{want}' has no Sonar analysis — read "
                    f"'{default or 'the default branch'}' instead (findings may be stale)")
                want = ""
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
            want = ""  # can't verify — the default branch is the safe read

    def issues_for(comps: list[str]) -> list[dict]:
        params = {"componentKeys": ",".join(f"{key}:{p}" for p in comps),
                  "resolved": "false", "ps": "500"}
        if org:
            params["organization"] = org
        if want:
            params["branch"] = want
        return api("issues/search?" + urllib.parse.urlencode(params)).get("issues", [])

    # One request for the whole component list, EXCEPT that Sonar refuses a list
    # mixing qualifiers ("All components must have the same qualifier, found
    # UTS,FIL") — which any PR touching both sources and tests does, i.e. nearly
    # every reviewable PR. There is no way to know a path's qualifier client-side
    # (it follows sonar.tests, which lives in the scanner's config, not here), so
    # the split is discovered from the refusal: a single component can never mix,
    # so retrying per file always resolves it.
    try:
        raw = issues_for(files[:100])
    except urllib.error.HTTPError as e:
        if e.code != 400:
            return "skipped", [], [], f"sonarqube: base-branch fallback failed (HTTP {e.code})"
        raw = []
        failed = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            for got in ex.map(lambda p: _try(issues_for, [p]), files[:100]):
                if got is None:
                    failed += 1
                else:
                    raw.extend(got)
        if failed:
            note = ((note + "; ") if note else "") + \
                f"sonarqube: {failed}/{len(files[:100])} files unreadable"
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        return "skipped", [], [], f"sonarqube: base-branch fallback failed ({e.__class__.__name__})"

    # Keep only issues on lines this PR actually added — drop pre-existing ones.
    soft = [f for f in _sonar_findings(raw)
            if f.line in changed_lines.get(f.file, ())]
    return "no-pr-analysis", [], soft, note


def ci_brief(status: str, failing: list[str], skip: str | None = None,
             unrunnable: dict | None = None) -> str:
    """The CI result, in words, for both prompts (#91).

    The panel has always computed this on every run and thrown it away before
    anyone reviewed: `review_ci` reached the payload and the human report and
    neither prompt. So reviewers judged a diff while a full suite had already
    passed or failed on that exact commit, and spent `could_not_assess` budget
    saying they could not run anything — which is not free, because each such
    declaration becomes a `coverage_veto` line and `round_stop` computes
    `confident` as `not veto`. A seat's inability to run the tests was costing
    the round its confident stop, with the answer already in the process.

    Three things this must not do, all of them the same discipline this codebase
    applies to NULL vs `[]`:

    * **`PENDING`/`blocked`/`none`/`unknown` must never read as `PASS`.** "CI has
      not run yet" and "CI passed" are different facts, and a reviewer told the
      wrong one is worse off than one told nothing. Each of the nine states says
      which it is — `blocked` being #324's, for a run that exists, will not execute
      without a person, and so reports nothing at all.
    * **A local run must never read as a CI run** (#548). The three `local-*` states
      are the repo's own suite executed on this box when GitHub had nothing to say,
      and each of them opens by saying so before it says anything else. They are
      weaker evidence than the state they stand in for, and a seat that cannot tell
      which it was handed would draw a conclusion the evidence does not carry.
      They open on "no settled result" rather than "no run exists", which is the
      narrower claim and the only one true of both states they can stand in for:
      `unknown` is a lookup that FAILED, and a run may well exist behind it. Saying
      the stronger thing would have put a confident falsehood in five prompts.
    * **A pass is not a licence to stop looking.** It says every test we thought
      to write passed — not that the code is correct. A reviewer treating green as
      evidence of correctness has stopped reviewing, and this repo's whole
      argument is that a passing signal is the dangerous kind.
    * **It never adds a fetch.** If `review_ci` was skipped or unreadable the
      brief says so, rather than retrying to make the prompt tidier.

    `unrunnable` is :func:`ci_unrunnable`'s record, and it corrects exactly one
    factual claim in the `none` body: "a fact about the commit rather than about the
    repo". On a PR whose base is in no workflow's trigger list that sentence is
    precisely false — the absence IS about the repo — and a seat told otherwise
    reasons that a run is coming when nothing the author can do will produce one.
    It is the same defect #628 fixes for the operator, arriving at the seat. Only
    the claim changes: the state is still `none`, the brief still says in as many
    words that this is not a pass, and nothing is added about what the reviewer
    should conclude from it. `None` leaves the body byte-identical to what it has
    always been, which is every round on a repo whose CI can run.
    """
    # One header for all nine states, and the WORDS that follow it are what say
    # which channel answered. A header that varied ("CI:" / "Local suite:") would
    # let a seat skim the label and miss the distinction the body spends a sentence
    # making — and the two are the same question about the same commit, asked of
    # two sources of differing strength, which is exactly what one heading with a
    # careful body says and two headings do not.
    head = "CI (the repo's own test suite, run on this exact commit):"
    if status == "PASS":
        body = ("PASSED. Every test the project has thought to write is green on this commit. "
                "That REFUTES findings of the form \"this new test never runs\", \"this may not "
                "even import\", or \"this migration looks syntactically incomplete\" — do not "
                "spend a finding or a `could_not_assess` entry on them. It is NOT evidence the "
                "code is correct: it says nothing about a case nobody wrote a test for, which is "
                "where the defects you are looking for live.")
    elif status == "FAIL":
        named = ", ".join(failing) if failing else "check names unavailable"
        body = (f"FAILED. Non-passing checks: {named}. Something the project already tests is "
                "broken by this diff. Treat that as a fact you may reason from, not as a finding "
                "to re-report — it is already visible to everyone.")
    elif status == "PENDING":
        body = ("STILL RUNNING, so its result is NOT known. This is not a pass. Anything you "
                "would have checked against a green suite is still unchecked.")
    elif status == "blocked":
        # #324. This state did not exist, and the run it describes is the reason
        # PR #282 sat for two days looking untouched: two commits pushed to fix a
        # red suite came back `action_required`, executed nothing, and emptied the
        # check list. "No checks" read as "nobody has pushed yet".
        body = ("EXISTS BUT WILL NOT RUN. A workflow run was created for this commit and "
                "is waiting on a human to approve it, so it has executed nothing and "
                "reports nothing. This is not a pass and it is not 'not started' — "
                "nothing will change until a person clicks. Whatever the last suite on "
                "this branch concluded is still the newest real result.")
    elif status == "none":
        body = ("NO RUN EXISTS for this commit, so there is no suite result either way. "
                "This is not a pass. It means nothing mechanical has looked at this "
                "code, "
                + (f"and none can: this pull request's base branch "
                   f"(`{unrunnable.get('base') or '?'}`) appears in no workflow's "
                   "trigger list in this repository, so no run will be created for "
                   "this commit however long anyone waits."
                   if unrunnable else
                   "which is a fact about the commit rather than about the repo."))
    elif status == LOCAL_PASS:
        # #548. Everything `PASS` says, plus the sentence that keeps the two apart.
        # A seat told "the suite passed" and left to assume it was CI's would draw a
        # stronger conclusion than the evidence carries, which is the failure the
        # first bullet above names — so the weakness is stated in the same breath as
        # the refutation rather than in a footnote nothing reads.
        body = ("GITHUB HAS NO SETTLED RESULT for this commit, so the repo's own suite was "
                "run HERE instead, on a checkout at this exact commit, and it PASSED. That "
                "REFUTES findings of the form \"this new test never runs\", \"this may "
                "not even import\", or \"this migration looks syntactically incomplete\" "
                "— do not spend a finding or a `could_not_assess` entry on them. Two "
                "limits on it. It is NOT evidence the code is correct: it says nothing "
                "about a case nobody wrote a test for, which is where the defects you "
                "are looking for live. And it is WEAKER than a green CI run — a "
                "different machine, possibly different service versions, and no "
                "guarantee this is the commit that will merge.")
    elif status == LOCAL_FAIL:
        named = ", ".join(failing) if failing else "command names unavailable"
        body = (f"GITHUB HAS NO SETTLED RESULT for this commit, so the repo's own suite was "
                f"run HERE instead, on a checkout at this exact commit, and it FAILED: "
                f"{named}. Something the project already tests is broken by this diff. "
                "Treat that as a fact you may reason from, not as a finding to "
                "re-report — it is already visible to everyone.")
    elif status == LOCAL_UNREAD:
        body = ("GITHUB HAS NO SETTLED RESULT for this commit. The repo's own suite was run "
                "HERE instead and produced no result"
                + (f" ({skip})" if skip else "")
                + ". This is not a pass, and it is not a failure either: nothing was "
                "established either way, so anything you would have checked against a "
                "green suite is still unchecked.")
    else:
        body = ("could NOT be read"
                + (f" ({skip})" if skip else "")
                + ". Its result is unknown. This is not a pass.")
    return f"{head} {body}"


#: `qbdata.CI_STATES` in this function's own vocabulary. The panel has spoken PASS
#: /FAIL/PENDING since #91 and those names are in prompts, payloads and a refusal
#: notice, so #324's two new states join them rather than renaming the three that
#: already exist.
CI_STATE_WORDS = {"green": "PASS", "red": "FAIL", "pending": "PENDING",
                  "blocked": "blocked", "none": "none", "unknown": "unknown"}


def _settle_no_checks(gh_repo: str, pr_number: int) -> tuple[str, str]:
    """`(status, why)` when `gh pr checks` reported nothing at all.

    `gh pr checks` says "no checks reported on the X branch" for two situations
    that are not remotely the same: a repo with no CI, and a run GitHub created and
    parked behind its workflow-approval gate — which executes nothing, contributes
    no check runs and so is invisible to every checks endpoint there is. This asks
    the workflow-runs API, which is the only place a gated run can be seen (#324).

    A second fetch, and only here. The rule `ci_brief` states — never add a fetch
    to make a prompt tidier — is about tidiness; this is the difference between
    telling a reviewer "there is no CI here" and telling it the truth.
    """
    import harness_rules                                    # noqa: PLC0415
    try:
        raw = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", gh_repo,
             "--json", "headRefOid,headRefName,statusCheckRollup"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        pr = json.loads(raw.stdout or "{}") if raw.returncode == 0 else {}
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
        return "unknown", f"the PR's head could not be read ({e.__class__.__name__})"
    if not pr:
        return "unknown", "the PR's head could not be read"
    report = harness_rules.ci_report(pr, gh_repo)
    return CI_STATE_WORDS.get(report.state, "unknown"), report.reason


def review_ci(gh_repo: str, pr_number: int) -> tuple[str, list[str], str | None]:
    """Fetch the PR's CI status via `gh pr checks`. Returns
    (status, failing, skip_reason); status is PASS | FAIL | PENDING | blocked |
    none | unknown and `failing` names the non-passing checks. This is a HARD-gate
    signal: a clean
    LLM/Sonar panel means little if CI (the repo's pytest run — slow tests and all)
    is red or still pending. Panel only SURFACES it; the merge gate itself lives in
    fix-and-land's own `gh pr checks` step. `gh pr checks` exits non-zero when checks
    fail/pend, but still prints the JSON, so we parse stdout regardless of exit code.

    `blocked` is #324's addition and the state that had no name: a run exists, is
    waiting on a human to approve it, and will never report. Reaching it costs a
    second fetch, taken only on the path that would otherwise have said `none`.
    """
    try:
        proc = subprocess.run(
            ["gh", "pr", "checks", str(pr_number), "--repo", gh_repo,
             "--json", "name,bucket"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        return "unknown", [], f"ci: {e.__class__.__name__}"
    raw = (proc.stdout or "").strip()
    if not raw:
        # No JSON -> usually "no checks reported on the 'X' branch" (exit 1, stderr).
        tail = (proc.stderr or "").strip().splitlines()
        hint = tail[-1][:80] if tail else f"exit {proc.returncode}"
        if "no checks" in hint.lower():
            settled, why = _settle_no_checks(gh_repo, pr_number)
            return settled, [], (f"ci: {why}" if settled == "unknown" and why else None)
        return "unknown", [], f"ci: {hint}"
    try:
        checks = json.loads(raw)
    except json.JSONDecodeError:
        return "unknown", [], "ci: unparseable gh output"
    buckets = [str(c.get("bucket", "")).lower() for c in checks if isinstance(c, dict)]
    failing = [str(c.get("name", "?")) for c in checks
               if isinstance(c, dict) and str(c.get("bucket", "")).lower() == "fail"]
    if not buckets:
        settled, why = _settle_no_checks(gh_repo, pr_number)
        return settled, [], (f"ci: {why}" if settled == "unknown" and why else None)
    if "fail" in buckets:
        return "FAIL", failing, None
    if "pending" in buckets:
        return "PENDING", failing, None
    return "PASS", failing, None


# ------------------------------------------- #628: CI that CANNOT run, which is
# not CI that has not run

#: The events that put a run against a PR's commits without anybody clicking.
#: `workflow_dispatch`, `schedule` and `workflow_call` are deliberately absent —
#: each of them can produce runs on this repo all day and none of them produces
#: one for THIS commit, so counting them would answer "does this repo have CI"
#: when the question is "can a run exist for this pull request".
#:
#: The two halves take their branch filter from different ends of the PR, and that
#: is the whole subtlety here. A `pull_request` filter is matched against the BASE
#: — the branch being merged INTO — while a `push` filter is matched against the
#: branch the commits are on, which for a PR is the HEAD. `prisonblues/lexray#1780`
#: is the push case: `test.yml` fires on pushes to `main` and `test`, the PR's head
#: was neither, its base `fca` was neither, and no run could ever exist.
_PR_TRIGGERS = ("pull_request", "pull_request_target")
_PUSH_TRIGGERS = ("push",)

#: How many workflow files to read before giving up. A repo with more than this
#: many is not one this check can afford to be exhaustive about, and an answer
#: taken over a prefix of the set would be a confident "no workflow can run" made
#: from a fraction of the workflows — the one wrong answer this whole check must
#: not produce. Over the cap it declines instead.
WORKFLOW_FILE_CAP = 30

#: Per-file read timeout, and the same discipline `review_ci` uses: a hung API is
#: an unanswered question, never a verdict.
WORKFLOW_READ_TIMEOUT = 30

#: What a trigger EVENT may be spelled as — a bare YAML identifier and nothing else.
#:
#: It exists because the scanner reads NAMES out of text, and a reader that takes
#: whatever it finds as a name turns a shape it cannot parse into an event nobody
#: has, which matches no trigger and reports the repo as having no runnable
#: workflow. `on: {pull_request: {branches: [fca]}}` did exactly that: the whole flow
#: mapping came back as one "event name", `workflow_can_run` said no, and the panel
#: told an operator that `fca` was in no trigger list while handing them a remedy to
#: add a branch the workflow already lists. A confident falsehood with an
#: unperformable instruction under it, which is the failure #628 exists to remove
#: rather than to commit.
#:
#: So an item that is not an identifier WITHDRAWS THE WHOLE READ. Not "an unknown
#: event", which is what the bug was: this parser's one design constraint is that
#: every ambiguity resolves to "a run may exist", and the only way to honour that on
#: a shape it cannot read is to stop reading.
_EVENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")


def _yaml_comment_stripped(line: str) -> str:
    """A workflow line with its trailing comment removed.

    Deliberately crude and deliberately conservative: a `#` inside a quoted
    string would be cut too, which in this parser can only ever turn a branch
    pattern into a shorter one and so can only ever produce a WIDER match — the
    fail-open direction. Branch names carrying a `#` do not exist in practice
    (git allows it; refs conventions and every CI example do not), and the
    alternative is a quote-tracking scanner written to be right about a case
    nobody has.
    """
    if line.lstrip().startswith("#"):
        return ""
    return re.sub(r"\s+#.*$", "", line).rstrip()


def _yaml_list(rest: str, block: list[str], indent: int) -> list[str] | None:
    """The items of a YAML sequence written either inline (`[a, b]`) or as `- a`
    lines under `indent`. ``None`` when neither shape is present, which is the
    answer that matters: an absent `branches:` key means "every branch", and a
    caller that read it as an empty list would refuse every workflow in the repo.
    """
    rest = rest.strip()
    if rest.startswith("[") and rest.endswith("]"):
        return [i.strip().strip("'\"") for i in rest[1:-1].split(",") if i.strip()]
    if rest:
        return [rest.strip("'\"")]
    out = []
    for line in block:
        if not line.strip():
            continue
        # `<` and not `<=`: a sequence may be written at the SAME indent as the key
        # that owns it, which is valid YAML and is how several repos write
        # `branches:`. A sibling key at that indent still ends the list, because it
        # does not open with `- `.
        if len(line) - len(line.lstrip()) < indent:
            break
        item = line.strip()
        if not item.startswith("- "):
            break
        out.append(item[2:].strip().strip("'\""))
    return out or None


def workflow_triggers(text: str) -> dict[str, dict] | None:
    """``{event: {"branches": [...] | None, "branches-ignore": [...] | None}}`` for
    one workflow file, or ``None`` when the ``on:`` block could not be found.

    A hand-written scanner rather than a YAML parse, because there is no YAML
    parser on this host — and the shape being read is small enough that the honest
    trade is stating what it does NOT handle rather than pretending to a general
    reader:

    * anchors, aliases, merge keys and multi-line flow collections are not read, and
      neither is `on` written as a flow mapping (`on: {push: {...}}`, and its
      sequence spelling). Every one of them comes back ``None`` — the read is
      WITHDRAWN — and the caller reports that it could not be established rather
      than that no run can exist.

      That is the whole of the fix for the defect this bullet used to describe. It
      claimed those shapes yielded "an event with no filters", which fails open;
      what actually happened is that the flow mapping came back as one long string
      and was taken as an EVENT NAME, matching no trigger, so a workflow that
      triggers on `pull_request: {branches: [fca]}` was reported as one that cannot
      run for `fca`. An unreadable shape must not become a name — see
      :data:`_EVENT_NAME`.
    * a flow mapping as the VALUE of a recognised event (`push: {branches: [main]}`)
      is a different case and does still fail open: the event name was read
      normally, and only its filter is unread, which :func:`_event_filter` reports
      as "not stated" — i.e. every branch.
    * ``on`` is a YAML 1.1 boolean, so a real parser hands it back under the key
      ``True``. This one matches the text and does not have that problem, which is
      the one place the crude reader is the more reliable of the two.

    Failing open is the whole design constraint. The output of this feeds a
    sentence telling an operator that **no run can ever exist** for their PR, and a
    parser that misreads a trigger it has never seen would print that about a repo
    whose CI is working. Every ambiguity here therefore resolves to "a run may
    exist", which costs nothing but the old wording.
    """
    lines = [_yaml_comment_stripped(ln) for ln in text.splitlines()]
    head = next((i for i, ln in enumerate(lines)
                 if re.match(r"""^['"]?on['"]?\s*:""", ln)), None)
    if head is None:
        return None
    rest = lines[head].split(":", 1)[1]
    # `on: [push, pull_request]` and `on: push` — every event, no branch filter.
    # A flow MAPPING (`on: {push: {...}}`) arrives here as one long item and is not
    # an identifier, so `_named` withdraws rather than minting an event out of it.
    inline = _yaml_list(rest, [], 0) if rest.strip() else None
    if inline is not None:
        return _named(inline)
    # The block form. It ends at the next key in column 0, which is what keeps a
    # `jobs:` block below from being read as a list of trigger events.
    body = []
    for ln in lines[head + 1:]:
        if ln.strip() and not ln.startswith((" ", "\t")):
            break
        body.append(ln)
    at = next((len(ln) - len(ln.lstrip()) for ln in body if ln.strip()), None)
    if at is None:
        return None
    out: dict[str, dict] = {}
    for i, ln in enumerate(body):
        if not ln.strip() or len(ln) - len(ln.lstrip()) != at:
            continue
        if ln.strip().startswith("- "):
            # `- {push: {...}}` is the sequence spelling of the same unreadable
            # shape, and takes the same answer as the inline one.
            listed = _named([ln.strip()[2:].strip().strip("'\"")])
            if listed is None:
                return None
            out.update(listed)
            continue
        if ":" not in ln:
            continue
        event, tail = ln.strip().split(":", 1)
        event = event.strip().strip("'\"")
        # A key that is not an identifier is a shape this reader does not
        # understand sitting where an event belongs — `? [a, b]` , a quoted
        # sentence, an anchor. Withdraw, for the reason `_EVENT_NAME` gives: the
        # alternative is an event name nothing can match, which reads downstream as
        # a workflow that cannot fire.
        if not _EVENT_NAME.match(event):
            return None
        sub = body[i + 1:]
        out[event] = {
            "branches": _event_filter(sub, at, tail, "branches"),
            "branches-ignore": _event_filter(sub, at, tail, "branches-ignore"),
        }
    return out or None


def _named(items: list[str]) -> dict[str, dict] | None:
    """``items`` as filterless trigger events, or ``None`` if any of them is not a
    bare identifier.

    All or nothing, deliberately. Dropping the unreadable item and keeping the rest
    would answer "can a run exist" from a subset of the triggers — the same
    partial-population answer :func:`ci_unrunnable` refuses when there are more
    workflow files than it reads."""
    if not all(_EVENT_NAME.match(i) for i in items):
        return None
    return {i: {"branches": None, "branches-ignore": None} for i in items}


def _event_filter(sub: list[str], at: int, tail: str, key: str) -> list[str] | None:
    """One event's ``branches`` / ``branches-ignore`` list, or ``None`` for "not
    stated", which GitHub reads as *every* branch. ``tail`` is whatever followed the
    event's own colon: non-empty means a flow mapping this reader does not parse, so
    it declines rather than guessing — see :func:`workflow_triggers`."""
    if tail.strip():
        return None
    keys_at = None
    for j, ln in enumerate(sub):
        if not ln.strip():
            continue
        deeper = len(ln) - len(ln.lstrip())
        if deeper <= at:
            break
        # The event's own keys are whatever indent its first line sits at.
        # Anything deeper belongs to one of them (`types:`, a `paths:` list) and
        # must not be read as a sibling: a `branches:` nested inside another key
        # is not this event's filter.
        keys_at = deeper if keys_at is None else keys_at
        if deeper != keys_at:
            continue
        name, sep, rest = ln.strip().partition(":")
        if sep and name.strip().strip("'\"") == key:
            return _yaml_list(rest, sub[j + 1:], deeper)
    return None


def _branch_matches(branch: str, pattern: str) -> bool:
    """GitHub's branch-filter glob, narrowly: ``**`` crosses ``/``, ``*`` does not,
    ``?`` is one character, everything else is literal. Written out rather than
    handed to :mod:`fnmatch`, whose ``*`` crosses ``/`` — which would make
    ``release/*`` match ``release/1/hotfix`` and quietly turn an unrunnable PR into
    a runnable-looking one."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            out.append(".*" if pattern[i:i + 2] == "**" else "[^/]*")
            i += 2 if pattern[i:i + 2] == "**" else 1
            continue
        out.append("." if c == "?" else re.escape(c))
        i += 1
    return re.fullmatch("".join(out), branch) is not None


def _branches_admit(branch: str, patterns: list[str]) -> bool:
    """One ``branches:`` list, read the way GitHub reads it: the patterns are
    evaluated IN ORDER and the LAST one that matches decides. A `!` pattern after a
    positive excludes the ref; a positive after a `!` puts it back.

    Order is the whole point, and this function exists because the code it replaces
    threw it away. That version split the list by sign and gave every exclusion
    unconditional precedence, so::

        branches: ['**', '!release/**', 'release/special']

    refused `release/special` — a branch GitHub runs, named explicitly, in the list.
    A repo whose workflows all have that shape would be reported by
    :func:`ci_unrunnable` as one where no run can ever exist, under a remedy telling
    the operator to add a base branch that is already in the trigger list. That is
    the #628 failure exactly — a confident falsehood with an unperformable
    instruction beneath it — arriving through a second door, and the reason
    :data:`_EVENT_NAME` says every ambiguity in this parser resolves to "a run may
    exist".

    The starting state is the other half of the rule. A list holding any positive
    pattern is an allow-list, so a branch that nothing in it matches is out; a list
    that is nothing BUT exclusions names what it refuses and admits everything else.
    """
    admitted = all(p.startswith("!") for p in patterns)
    for p in patterns:
        excluded = p.startswith("!")
        if _branch_matches(branch, p[1:] if excluded else p):
            admitted = not excluded
    return admitted


def _filter_admits(branch: str, f: dict) -> bool:
    """Does one event's branch filter admit ``branch``? An absent list is "every
    branch", which is why the two are read with ``is None`` rather than for
    truthiness.

    The two keys are kept independent rather than merged into one ordered list:
    GitHub refuses a workflow that uses `branches` and `branches-ignore` for the
    same event, so there is no interleaving of them to get right, and inventing one
    would be this reader deciding a question the file cannot ask.
    """
    allow, deny = f.get("branches"), f.get("branches-ignore")
    if deny is not None:
        if any(p.startswith("!") for p in deny):
            # `branches-ignore` already means "not these", and GitHub does not state
            # what a further negation inside it does. An unestablished list is no
            # evidence for the one claim this module is not allowed to get wrong, so
            # the exclusion is WITHDRAWN and the event admits — the direction
            # :func:`workflow_triggers` takes with every shape it cannot read.
            return True
        if any(_branch_matches(branch, p) for p in deny):
            return False
    if allow is None:
        return True
    return _branches_admit(branch, allow)


def workflow_can_run(triggers: dict[str, dict], base: str, head_branch: str) -> bool:
    """Could this workflow produce a run for a PR from ``head_branch`` into
    ``base``? A `pull_request` filter is read against the BASE and a `push` filter
    against the HEAD — see :data:`_PR_TRIGGERS`."""
    for event, f in triggers.items():
        if event in _PR_TRIGGERS and _filter_admits(base, f):
            return True
        if event in _PUSH_TRIGGERS and _filter_admits(head_branch, f):
            return True
    return False


def _gh_api(path: str, raw: bool = False) -> tuple[str, str]:
    """``(body, why-not)`` for one `gh api` read. Never raises: this whole check is
    additive, and an unreadable workflow directory must degrade to "could not be
    checked" rather than take a review down."""
    argv = ["gh", "api", path]
    if raw:
        argv += ["-H", "Accept: application/vnd.github.raw"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL,
                              timeout=WORKFLOW_READ_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as e:
        return "", f"{e.__class__.__name__}"
    if proc.returncode:
        tail = (proc.stderr or "").strip().splitlines()
        return "", (tail[-1][:120] if tail else f"gh api exited {proc.returncode}")
    return proc.stdout or "", ""


def ci_unrunnable(gh_repo: str, base: str, head_branch: str,
                  read=None) -> tuple[dict | None, str]:
    """``(what makes a run impossible, why this could not be established)`` for a PR
    whose CI status is ``none``.

    #628. `prisonblues/lexray#1780` was based on `fca`; that repo's `test.yml`
    triggers on `main` and `test` alone, so no run could ever exist for it, and the
    panel printed *"🚫 no run exists for this commit — do not merge, even if the
    review below is clean"* on all five rounds. That instruction is unsatisfiable by
    anything the author can do, so it was waived — and a hard gate that gets waived
    teaches everyone that hard gates are waivable. "CI has not run" and "CI cannot
    run" have completely different remedies and had one sentence between them.

    **It adds no value to `ci_status` and is not allowed to.** `app/ordering.py`
    compares that field against `PASS`/`FAIL` for equality and matches
    `CI_SETTLED`/`CI_NOT_APPLICABLE` as sets; a new member of it ripples into
    consumers this module does not own. So this rides BESIDE the status as its own
    record, and unrunnable stays not-a-pass exactly as `none` already is.

    Read at the BASE ref rather than at the head, because the base is where the
    remedy has to land: a `pull_request` trigger is taken from the base branch's
    copy of the workflow, and a PR that adds a trigger to its own copy still gets no
    run. That does mean a PR which fixes this is reported as unrunnable until it
    merges, which is the truth about it.

    Three answers, and the middle one is the one worth being careful about:

    * a dict — every workflow was read and none of them can fire for this PR;
    * ``(None, "")`` — some workflow can fire, so the absent run is an absent run;
    * ``(None, why)`` — the question could not be put. NOT reported as runnable and
      not reported as unrunnable: the caller says it could not be checked, on the
      same rule `board_escalations` keeps, because "no workflow can run here" is a
      strong claim and a failed API read is no evidence for it.
    """
    read = read or _gh_api
    listing, why = read(f"repos/{gh_repo}/contents/.github/workflows?ref={base}")
    if why:
        return None, f"the repo's workflow directory could not be read ({why})"
    try:
        entries = json.loads(listing or "[]")
    except json.JSONDecodeError:
        return None, "the repo's workflow directory came back unparseable"
    paths = [str(e.get("path") or "") for e in entries if isinstance(e, dict)
             and str(e.get("name") or "").endswith((".yml", ".yaml"))]
    if not paths:
        # A repo with no workflows at all is not a trigger-list mistake, and the
        # remedy this function prints ("add the base to the triggers") would name a
        # file that does not exist. `none` says it already: nothing mechanical has
        # looked at this code.
        return None, ""
    if len(paths) > WORKFLOW_FILE_CAP:
        return None, (f"{len(paths)} workflow files is over the {WORKFLOW_FILE_CAP} "
                      "this check reads")
    blocked = []
    for path in sorted(paths):
        text, why = read(f"repos/{gh_repo}/contents/{path}?ref={base}", True)
        if why:
            return None, f"`{path}` could not be read ({why})"
        triggers = workflow_triggers(text)
        if triggers is None:
            # An `on:` block this reader could not find is a workflow it knows
            # nothing about, and one unknown workflow is enough to withdraw the
            # claim — the claim is about ALL of them.
            return None, f"the `on:` block of `{path}` could not be read"
        if workflow_can_run(triggers, base, head_branch):
            return None, ""
        blocked.append({"path": path, "events": sorted(triggers)})
    return {
        "base": base,
        "head": head_branch,
        "ref": base,
        "reason": (f"no workflow in this repo can produce a run for a pull request "
                   f"into `{base}`: {len(blocked)} workflow file(s) were read at "
                   f"`{base}` and none of them triggers on it"),
        "remedy": (f"add `{base}` to the trigger list of the workflow that should "
                   f"gate it — `on.pull_request.branches` (matched against the "
                   f"base) or `on.push.branches` (matched against the PR's own "
                   f"branch). Waiting cannot fix this and neither can re-running: "
                   f"no run is scheduled to wait for."),
        "workflows": blocked,
    }, ""


#: How long a round may wait for a PENDING CI to settle before dispatching the
#: seats. Measured rather than guessed: on this repo a full run is ~4m30s wall
#: clock with a 1-9 second queue wait, and a panel round is 20-40 minutes — so the
#: build reliably finishes DURING a round that was told it had not.
CI_SETTLE_WAIT = 600

#: How often to ask. `gh pr checks` is a couple of seconds, and the thing being
#: waited on changes on the order of minutes.
CI_SETTLE_POLL = 20


def review_ci_settled(gh_repo: str, pr_number: int, *,
                      read=None,
                      budget: float = CI_SETTLE_WAIT,
                      poll: float = CI_SETTLE_POLL,
                      now=time.monotonic, sleep=time.sleep):
    """:func:`review_ci`, but give a PENDING build a bounded chance to finish.

    **Why waiting beats reading it again afterwards.** The obvious fix for a stale
    CI reading is to take a second one when the round ends. It does not work here,
    and the reason is :func:`panel_rounds.coverage_veto`'s standing rule: a veto is
    exempted *"off RECORDED STATE, never off the wording of a message or a
    declaration"*. The veto in question is not the panel's own — it is a REVIEWER
    saying "could not assess: CI result is unknown (still running)", free-form
    prose produced because its prompt said so. Answering that afterwards would mean
    matching model text for something CI-shaped, which is the one thing that rule
    forbids, and forbids for a good reason: a regex over prose exempts a genuine
    round-specific gap whose wording happens to match, and misses the structural
    one that does not.

    So the cause is removed instead of the symptom filtered. If the seats are told
    a real answer, no reviewer has a gap to declare and nothing needs exempting.

    Measured, fleet-wide over five days: 19 rounds, ``ci_status`` PENDING on 9 of
    them, and ``stop_confident`` true on **none** — the field that separates a
    converged cycle from one that gave up carried no information at all. That is
    the failure `coverage_veto`'s docstring already names in the abstract: *"a
    signal that is never positive carries no information and trains its reader to
    ignore it."*

    Bounded, and the bound fails in the honest direction: a build that is still
    pending when the budget runs out is reported exactly as it is today, PENDING,
    veto and all. Waiting can only ever turn an unknown into a fact.

    Returns ``(status, failing, skip, waited_seconds)`` — the wait is returned
    rather than logged so the caller can put it in `config_notes`, because a round
    that sat for four minutes should say so rather than look slow for no reason.
    """
    # `read` is injected rather than resolved here, and the caller passes its OWN
    # `review_ci`. That is not ceremony: a dozen suites stub `panel.review_ci` to
    # keep a round off the network, and a wrapper that reached for this module's
    # binding instead would slip past every one of them — shelling out to `gh` in
    # tests and, on a PENDING answer, sitting on the whole budget.
    read = read or review_ci
    started = now()
    status, failing, skip = read(gh_repo, pr_number)
    if status != "PENDING":
        return status, failing, skip, 0.0
    while now() - started < budget:
        sleep(min(poll, max(0.0, budget - (now() - started))))
        status, failing, skip = read(gh_repo, pr_number)
        if status != "PENDING":
            break
    return status, failing, skip, round(now() - started, 1)


# ------------------------------------------- #548: the empty channel, filled locally
#
# #501 built a channel and #546 priced its emptiness. `none` is the state neither of
# them can help: there is nothing to wait for and nothing to read, so every seat is
# told "no run exists for this commit" about a repo that may have a perfectly good
# suite sitting in it. On the PR that prompted this the channel was empty for a
# structural reason — a stacked PR whose CI branch filter never fired — and no
# amount of waiting was ever going to fill it.
#
# So the repo's OWN suite is run once, before the seats are dispatched, and its
# result travels down the same channel in the same vocabulary.
#
# **Evidence, not a seat.** `ALL_REVIEWERS` is a list of things that produce
# FINDINGS — sonar is a member because it produces them, in a different shape,
# through an API. A test run produces EVIDENCE, and evidence has a channel already:
# it raises the floor under every seat at once instead of adding a fifth voice with
# a coverage row whose meaning is not the question the other rows answer. It is also
# not "give the seats a shell": three vendor CLIs each holding a live database is
# expensive, nondeterministic, and costs the seats the independence that is the only
# reason to seat four of them. One execution, attributable to one commit, shared by
# all of them — the shape `review_ci` already has.
#
# **Three things it must never become**, each of them a way this would do more harm
# than the gap it fills:
#
#   * *It must never read as CI.* A local run is weaker evidence — a different box,
#     possibly different service versions, and nothing guaranteeing this is the
#     commit that merges. So it gets three states of its OWN rather than borrowing
#     PASS and FAIL, and every renderer says which one it is reading. That is
#     `_ci_line`'s refusal to let a missing gate read as a green one, one step along.
#   * *It must never loosen a merge gate.* `preland.check_ci` reads GitHub and has
#     never heard of this; a repo whose CI reports nothing is refused there exactly
#     as it was before. A local pass can buy a round its confident stop — which is
#     the point — and it cannot buy a merge. The board agrees without being told:
#     `app.ordering` compares `ci_status` against `PASS` and `FAIL` for EQUALITY and
#     treats everything else as neither known-green nor known-red, so a `local-pass`
#     reaching the plan orders it as conservatively as a `none` would. That is the
#     fail-closed direction and it is left alone deliberately — the board is ordering
#     work for people, and "a suite passed on somebody's laptop" is not the fact its
#     green rule is about.
#   * *It must never run code nobody chose to put on this box.* See
#     :func:`_local_head_problem`, which is the security boundary of the feature.
#
# **The honest limit, recorded before anyone leans on it.** This answers a SUBSET.
# On the PR that prompted the issue the open questions were a jsonb size ceiling,
# whether a reader delivers a field to a browser, and whether stored rows are now a
# mixed corpus — a test database holds no corpus, so a local `make test` would have
# answered none of the three. This is the structural fix for a class, not a fix for
# that round.

#: The CI states a local run may answer FOR: the two where GitHub has no result and
#: nothing is coming. `PASS` and `FAIL` are real results about this exact commit and a
#: weaker reading must never displace either. `PENDING` is excluded because it belongs
#: to #501's bounded wait, which is in the business of turning it into one of those
#: two — running a local suite instead of waiting would spend minutes to arrive at a
#: worse answer than the one already on its way.
#:
#: **`blocked` is excluded, and it is the one that had to be argued out.** #324 gave
#: that state a name precisely because it is ACTIONABLE and distinguishable: a run
#: exists for this commit, it is waiting on a person to click, and nothing changes
#: until they do. Replacing it with `local-pass` overwrites `ci_status` — the field
#: every downstream consumer reads, from `app.ordering` to the review queue — with a
#: value that says nothing about the click, and buys the round the confident stop that
#: is the only thing still making anyone look. The remedy for a gated run is the
#: approval, and a feature that quietly makes the gate stop mattering is not a floor
#: under the seats, it is a way past a control somebody chose to put there.
LOCAL_SUITE_WHEN = frozenset({"none", "unknown"})

#: The three states a local run can produce. Deliberately NOT members of
#: `CI_STATE_WORDS`: that mapping is `qbdata.CI_STATES` in this module's vocabulary
#: and every one of its names is something GitHub can report, which none of these
#: is. They are lower-case for #324's reason — the three shouted names are #91's and
#: are in prompts, payloads and a refusal notice — and hyphenated so that no reader
#: skimming a payload can mistake one for `PASS`.
LOCAL_PASS = "local-pass"
LOCAL_FAIL = "local-fail"
LOCAL_UNREAD = "local-unknown"
LOCAL_STATES = (LOCAL_PASS, LOCAL_FAIL, LOCAL_UNREAD)

#: Wall clock for the WHOLE declared run, not per command. A ceiling per command
#: bounds nothing: a repo declaring four of them would get four times the number it
#: read in the docs, and the thing being bounded is how long a round waits before it
#: dispatches a seat.
#:
#: The bound fails in the honest direction, which is the same discipline
#: `review_ci_settled`'s does: a run that does not finish is reported as not having
#: finished (:data:`LOCAL_UNREAD`) and vetoes. It never becomes a pass, and it never
#: silently becomes a failure either — "the suite is broken" and "the suite did not
#: fit in the budget" are different facts about a diff, and only the first is one a
#: reviewer should reason from.
LOCAL_SUITE_TIMEOUT = 900

#: How much of a failing command's own output is handed back to the caller, out of
#: the :data:`LOCAL_SUITE_TAIL_BYTES` the run kept. Enough to recognise which
#: assertion went red, on one line. It reaches the operator's terminal and the
#: round's payload — never a prompt and never the `config_notes` `--post` publishes;
#: see :func:`review_local_suite` for which reader is which.
LOCAL_SUITE_GIST = 400


def _local_head_problem(root: str, head_sha: str) -> str:
    """Why the declared suite must NOT be run in `root`, or ``""`` when it may.

    **This is the security boundary of the whole feature**, so it refuses by default
    and every uncertain answer is a refusal too. What it forbids is the panel
    executing a command out of a rules file against code somebody merely pointed it
    at. `panel.py --repo x --pr N` reviews a PR without ever checking it out — the
    diff and the seats' trees come from the forge (`fetch_increment`,
    `fetch_pr_tree`) precisely so that nothing has to be — and a feature that read
    `local_suite` from one branch and ran it over another would turn a review into an
    execution channel for a PR nobody has read.

    The rule is that the checkout must ALREADY be at the PR's head. Then whoever
    checked that branch out has accepted this code on this box, and running its test
    suite adds no capability that `git checkout` did not: the fix loop that calls the
    panel is sitting in exactly that worktree, which is the case this exists to
    serve. It also means a :data:`LOCAL_PASS` is attributable to the sha the round
    reports, because the tree that ran is the tree that commit names.

    What it does NOT mean is that the code and the COMMAND come from the same place,
    and an earlier draft of this docstring claimed they did. The command is read from
    the DEFAULT BRANCH — :func:`harness_rules.default_branch_rules`, deliberately —
    and the code under test is the PR head. Two refs, on purpose: a pull request that
    supplied its own command would be choosing what the thing reviewing it executes,
    and "the operator checked this branch out" is consent to POSSESS these files, not
    to run one of them. Anything else and the panel keeps today's behaviour and the
    seats are told CI reports nothing, which remains true.

    Edits are refused for the second half of that: a tree with changes in it runs a
    suite against code that is in no commit, while `local-pass` claims one.

    **Untracked files are refused too, and IGNORED ones are not**, which is the line
    `git status --porcelain` already draws and the reason this asks it without
    `--untracked-files=no`. A worktree from `create-worktree` carries a `.env`, a
    virtualenv and scratch of its own — all of it gitignored, none of it listed, and
    refusing that would mean this never runs anywhere real. A file that is untracked
    and NOT ignored is a different animal: a stray `conftest.py` is loaded by pytest
    before a line of the suite runs, a shadowing executable is found on `PATH` first,
    and either can decide the result of a run that will be recorded as evidence about
    a commit neither of them is in. That the ignore list is itself part of the commit
    is what makes this a line the repo drew rather than one drawn around it.

    Never raises, and every branch returns a sentence a human can act on: this runs
    inside a round that must not die because a checkout was somewhere unexpected.
    """
    if not head_sha:
        return "the PR's head sha is not known"
    if not root:
        return "no checkout path resolved for this repo"
    at = (_git(root, "rev-parse", "HEAD") or "").strip()
    if not at:
        return f"{root} could not be read as a git checkout"
    if at != head_sha:
        return (f"the checkout is at {at[:8]}, not the PR head {head_sha[:8]} — a "
                "run there would be a run of different code")
    dirty = _git(root, "status", "--porcelain")
    if dirty is None:
        return f"the working tree at {root} could not be read"
    if dirty.strip():
        # NOT `dirty.strip().splitlines()`: porcelain v1 is `XY PATH`, and an
        # unstaged edit's X is a SPACE — so stripping the blob before splitting eats
        # the first line's status column and takes a character off the one filename
        # this sentence quotes. Caught by the name it printed.
        lines = [ln for ln in dirty.splitlines() if ln.strip()]
        first = (lines[0][3:].strip() or "?") if len(lines[0]) > 3 else "?"
        return (f"the checkout has {len(lines)} uncommitted or unignored file(s) — "
                f"{first}{' and others' if len(lines) > 1 else ''} — so a run there "
                f"would not be a run of {head_sha[:8]}")
    return ""


#: How much of a command's own output is kept, out of a file that may hold far more.
#: Read from the END, because that is where a test runner puts its summary.
LOCAL_SUITE_TAIL_BYTES = 4096


def _kill_group(proc) -> None:
    """SIGKILL the whole process group `proc` leads, then reap it.

    `start_new_session=True` in :func:`_run_bounded` is what makes there BE a group,
    and the two exist together: a suite that starts a database, a dev server or a
    `docker compose` leaves descendants that `Popen.kill()` does not touch, and they
    keep running on the box long after the round that started them has published its
    report. Killing the leader alone is what makes a wall-clock bound a bound on THIS
    process rather than on the machine.

    Falls back to killing the leader if the group has already gone, and swallows a
    reap that hangs: this is cleanup on the timeout path, and an exception raised
    here would replace `TimeoutExpired` — turning "the suite did not finish", which
    the caller has a state for, into a crash it has none for.

    **Two limits, stated rather than implied.** A descendant that calls `setsid`,
    double-forks, or hands its work to a container runtime or a service manager has
    left this group and survives; bounding that needs a cgroup or a systemd scope,
    which is a heavier feature than this one. And the reap below can add up to ten
    seconds past the configured budget before the caller returns — SIGKILL is not
    catchable, so that only bites a process already stuck in the kernel, but the
    number a repo writes bounds the RUN and not the call.
    """
    import signal                                            # noqa: PLC0415
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _run_bounded(argv: list[str], cwd: str, timeout: float) -> tuple[int, str]:
    """Run `argv` in `cwd` under `timeout`, as ``(exit code, tail of its output)``.

    **Not `subprocess.run`,** and each difference answers something `run` gets wrong
    for a command whose length and volume nobody controls:

    * **Output goes to a FILE, not a pipe into memory.** `capture_output=True`
      buffers the lot, so a suite that prints for fifteen minutes is measured in the
      panel's RSS — on a box already holding four reviewer prompts and a diff. Only
      the last :data:`LOCAL_SUITE_TAIL_BYTES` are ever read back. The residual is
      stated rather than hidden: this bounds MEMORY and not disk, and a repo whose
      own declared command can fill a disk has a problem this function is not the
      place to solve.
    * **A file also removes the pipe deadlock**, which is the shape that would have
      defeated the timeout: `subprocess.run` kills its child and then reads the pipes
      to EOF, and a surviving grandchild holding the write end keeps that read open
      for as long as it likes. The bound would have been advisory exactly when it
      mattered.
    * **`start_new_session=True`,** so there is a process group to kill. See
      :func:`_kill_group`.

    `errors="replace"` is the decode, and it is not a nicety: a test suite's output
    is not guaranteed UTF-8 — a filename, a doctest, a library printing latin-1 —
    and a strict decode would take a whole round down over a byte in somebody's
    stack trace. That is `_git`'s lesson, arriving here for its reason.
    """
    with tempfile.TemporaryFile("w+b") as sink:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=sink,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            raise
        sink.seek(max(0, sink.tell() - LOCAL_SUITE_TAIL_BYTES))
        return code, sink.read().decode("utf-8", "replace")


def review_local_suite(commands, root: str, head_sha: str, *,
                       timeout: float = LOCAL_SUITE_TIMEOUT,
                       run=None, now=time.monotonic):
    """The repo's own declared suite, run on this commit, in `review_ci`'s vocabulary.

    Returns ``(status, failing, why, output, seconds)``.

    **`why` and `output` are separate because their audiences are.** `why` is the
    harness's own sentence — a command name and an exit code — and it goes in the
    round's `config_notes`, which `--post` publishes as a **public PR comment**.
    `output` is the tail of what the command printed, which is neither the harness's
    words nor safe to publish: a failing test prints whatever it was holding, and on
    this fleet that includes a `DATABASE_URL` with a password in it. It reaches the
    round's payload and the operator's terminal, and it reaches no prompt and no
    comment. One field carrying both would have made that distinction a matter of
    who remembered it at each call site.

    `status` is one of :data:`LOCAL_STATES` — or ``""``, which is **not a state**:
    it means nothing was run, and the caller must leave its `ci_status` exactly as it
    found it. That is why this returns an empty string rather than
    :data:`LOCAL_UNREAD` when the run was never attempted. "The repo declared no
    suite" and "a suite was run and told us nothing" are different facts, the second
    vetoes a confident stop and the first cannot, and collapsing them would make
    every repo on the fleet that has not opted in permanently unable to stop
    confidently the day this landed.

    `why` is a sentence in every case it is non-empty, including the ``""``-status
    ones — a suite that was configured and skipped is something the round should say
    out loud, or the setting looks live while doing nothing.

    **The commands run in order and the first failure stops the run.** A suite that
    is already red tells you what is wrong; spending the rest of the budget
    collecting a second opinion from a DB-backed target delays the seats for
    information the round will not use.

    **The checkout is checked before the first command and again after the last**, and
    a pass whose tree moved underneath it becomes :data:`LOCAL_UNREAD` rather than a
    pass. See the comment at that second call for the three ways one instant's answer
    stops being true, none of which needs an adversary.

    **A failing command's OUTPUT does not travel into the reviewer prompt**, only its
    name. Two reasons, and the first is enough on its own: that output is text
    produced by code from the PR under review, and pasting it into four reviewers and
    a judge would hand a PR a direct channel into the prompts of everything judging
    it — the exact door `member_sandbox` closes. It is also unbounded, on a budget
    the diff is already competing for.

    `run` is injected for the reason `review_ci_settled`'s `read` is: the suites must
    be able to exercise this without a real suite, a real database and fifteen
    minutes. Its contract is :func:`_run_bounded`'s — ``(argv, cwd, timeout)`` in,
    ``(exit code, output tail)`` out, `TimeoutExpired` on the bound — and NOT
    `subprocess.run`'s, because the three things that make the bound real (a file
    instead of a pipe, a new session, a group kill) are not arguments to `run` and
    would have had to be re-implemented by every caller that passed one.
    """
    import shlex                                            # noqa: PLC0415

    run = run or _run_bounded
    started = now()
    cmds = tuple(commands or ())
    if not cmds:
        return "", [], "", "", 0.0
    problem = _local_head_problem(root, head_sha)
    if problem:
        return "", [], f"the local suite was not run — {problem}", "", 0.0

    for cmd in cmds:
        spent = now() - started
        left = timeout - spent
        if left <= 0:
            return (LOCAL_UNREAD, [cmd],
                    f"the {timeout:.0f}s budget ran out before `{cmd}` started", "",
                    round(spent, 1))
        try:
            argv = shlex.split(cmd)
        except ValueError as e:
            return (LOCAL_UNREAD, [cmd],
                    f"`{cmd}` is not a command this can parse ({e})", "",
                    round(spent, 1))
        if not argv:
            return (LOCAL_UNREAD, [cmd], f"`{cmd}` is empty", "", round(spent, 1))
        try:
            code, output = run(argv, root, left)
        except subprocess.TimeoutExpired:
            return (LOCAL_UNREAD, [cmd],
                    f"`{cmd}` did not finish within the {timeout:.0f}s budget", "",
                    round(now() - started, 1))
        except (OSError, ValueError) as e:
            # `ValueError` beside `OSError` so that "never raises" is true rather
            # than intended: an injected `run` that decodes strictly raises it, and
            # a round must not die because a byte in a stack trace was not UTF-8.
            return (LOCAL_UNREAD, [cmd],
                    f"`{cmd}` could not be run ({e.__class__.__name__})", "",
                    round(now() - started, 1))
        if code != 0:
            return (LOCAL_FAIL, [cmd], f"`{cmd}` exited {code}",
                    harness_rules.tail_gist(output, LOCAL_SUITE_GIST),
                    round(now() - started, 1))
    # ASKED AGAIN, and only on the way out. The guard above is a statement about one
    # instant, and three things can have falsified it since: another agent working the
    # same box, a command in this very list that rewrote a tracked file or moved HEAD,
    # and the ordinary race between the check and the first `execve`. None of them is
    # a hostile-actor story — a fix pass committing while a round runs is a Tuesday —
    # and all three end the same way: a `local-pass` attributed to a commit whose
    # files are not what ran. Re-reading costs one `git` call against a run measured
    # in minutes, and it converts the whole class into `local-unknown`, the state for
    # "something executed and established nothing". A pass is the ONLY outcome worth
    # re-checking: a failure and an unread run already veto, and asking again could
    # only turn a veto into a differently-worded veto.
    moved = _local_head_problem(root, head_sha)
    if moved:
        return (LOCAL_UNREAD, [], "the suite passed, but the checkout no longer "
                                  f"matches the commit it was run against — {moved}",
                "", round(now() - started, 1))
    return LOCAL_PASS, [], "", "", round(now() - started, 1)


#: Everything this module offers, INCLUDING the underscore names — the suites
#: reach for several of them through `panel`, and a plain star import would drop
#: them silently. Generated from the module's own top level, so a helper added here
#: is exported without anyone remembering to list it.
__all__ = [
    "panel_core", "_fix_range_diff", "_commit_id", "_head_sha_now",
    "FIX_RANGE_OK", "FIX_RANGE_NO_FIX", "FIX_RANGE_BLIND", "FIX_RANGE_REWRITTEN",
    "RECONSTRUCT_TIMEOUT_S", "RECONSTRUCT_MAX_COMMITS",
    "_git", "_patch_ids", "reconstruct_fix_range",
    "_mergeable_now",
    "_merge_base_now", "_MERGE_BASE_JQ", "_SHA_TEXT",
    "_base_tip_now", "PROVENANCE", "_provenance",
    "RECURRENCE", "SITE_RADIUS", "_recurrence",
    "DIFF_PREAMBLE", "_diff_by_file", "_diff_subset", "_fit_parts",
    "review_ci_settled", "CI_SETTLE_WAIT", "CI_SETTLE_POLL",
    "fetch_increment", "COMPARE_FILE_CAP", "_count", "compare_facts",
    "FIX_PASS_COMMIT_CAP", "_FIX_PASS_COMMITS_JQ", "fix_pass_commits",
    "_range_notes", "MERGE_DISTANT", "MERGE_INVOLVED", "MERGE_UNREAD",
    "_changed_lines", "Integration", "MergeReading", "merge_involvement",
    "_is_commitish", "_is_ref", "_same_commit",
    "_prior_round", "PR_SCOPE_HEADER", "INCREMENT_BRIEF", "JUDGE_INCREMENT_BRIEF",
    "CI_STATE_WORDS", "_settle_no_checks",
    "WORKFLOW_FILE_CAP", "WORKFLOW_READ_TIMEOUT", "workflow_triggers",
    "workflow_can_run", "ci_unrunnable",
    "ReviewScope", "_cut_note", "_cut_note_reserve", "_SONAR_SEV",
    "_sonar_findings", "_try", "review_sonarqube", "ci_brief",
    "review_ci",
    "LOCAL_SUITE_WHEN", "LOCAL_PASS", "LOCAL_FAIL", "LOCAL_UNREAD", "LOCAL_STATES",
    "LOCAL_SUITE_TIMEOUT", "LOCAL_SUITE_GIST", "LOCAL_SUITE_TAIL_BYTES",
    "_local_head_problem", "_kill_group", "_run_bounded", "review_local_suite",
]

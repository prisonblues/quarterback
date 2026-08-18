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

def _fix_range_diff(gh_repo: str, base_sha: str | None,
                    head_sha: str | None) -> tuple[str | None, str | None]:
    """The diff of everything that landed BETWEEN two rounds — i.e. the fix pass
    whose damage (or thoroughness) provenance is trying to attribute — as
    `(diff, None)`; or `(None, why)` when there is no range to read.

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

    Two biases remain and are written down rather than fixed. Merging the base
    branch INTO the PR between rounds leaves the old head an ancestor of the new
    one (status `ahead`, correctly), so main's own commits fall inside the range
    and their lines are attributed to the fix pass — `introduced` then
    over-counts. And the compare endpoint returns at most 300 files, so a fix
    pass wider than that is attributed on the first 300 and the rest read as
    `missed`. #41 (review the increment) is what removes the guess altogether.
    """
    if not gh_repo:
        return None, "no GitHub repo is configured for this run"
    if not base_sha:
        return None, ("the baseline does not record which commit it reviewed "
                      "(written before `head_sha` existed)")
    if not head_sha:
        return None, "this round did not record the commit it reviewed"
    span = f"{base_sha[:8]}..{head_sha[:8]}"
    if base_sha == head_sha:
        # Not a failure and not worth an API call to be told nothing changed —
        # but told apart from one, or the operator goes looking for a GitHub
        # fault that never happened.
        return None, f"no commit landed between rounds (head unchanged at {head_sha[:8]})"
    try:
        got = json.loads(panel_core.sh(["gh", "api", f"repos/{gh_repo}/compare/{base_sha}...{head_sha}",
                             "--jq", _FIX_RANGE_JQ], timeout=FIX_RANGE_TIMEOUT_S))
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        # Widened past CalledProcessError deliberately: no `gh` on PATH is an
        # OSError, a hung call is a TimeoutExpired, a truncated body is a
        # ValueError, and each of them would otherwise take down a whole review
        # round over an attribution nothing gates on.
        return None, f"could not read the range {span} ({type(e).__name__})"
    if not isinstance(got, dict):
        return None, f"the compare API answered {span} with something that is not an object"
    if got.get("status") == "diverged":
        return None, (f"{span} have diverged — the branch was rewritten between rounds, so "
                      "the range would span commits no fix pass wrote")
    out: list[str] = []
    size = 0
    for f in got.get("files") or []:
        name, patch = f.get("filename"), f.get("patch")
        if not (name and patch):
            continue  # binary, or too large for the API to send a patch for
        body = patch.rstrip("\n")
        chunk = f"diff --git a/{name} b/{name}\n{body}\n"
        size += len(chunk)
        if size > FIX_RANGE_MAX_CHARS:
            return None, (f"the range {span} is larger than {FIX_RANGE_MAX_CHARS:,} chars — "
                          "not attributed, rather than held whole in memory")
        out.append(chunk)
    if not out:
        # An empty compare — a revert that nets to nothing, an empty commit — is
        # "no range", not "a range with no added lines". The second reading calls
        # every new finding `missed`, confidently and with nothing to say so.
        return None, f"the range {span} changed no line this can attribute against"
    return "".join(out), None


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


def _merge_base_now(gh_repo: str, pr_number: int) -> str | None:
    """The PR's merge base, re-read. None if it cannot be had.

    Only called when the head has been seen to move mid-round. `baseRefOid` is
    recomputed by GitHub on every push to the head branch, so a head that moved
    may have taken the merge base with it — and on this repo the usual reason a
    head moves is a merge of the base branch into the PR, which is precisely the
    push that moves it. Pairing a re-stamped head with a merge base computed for
    the commit before it yields a range nothing ever reviewed.

    Bounded like its siblings: an attribution nothing gates on must never be able
    to stall a panel."""
    try:
        return _commit_id(
            json.loads(panel_core.sh(["gh", "pr", "view", str(pr_number), "--repo", gh_repo,
                           "--json", "baseRefOid"],
                          timeout=FIX_RANGE_TIMEOUT_S)).get("baseRefOid"))
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


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

    #41 (review the increment) is what removes both: a finding raised against the
    increment is introduced by construction, with no line arithmetic in the middle.
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
               since_round: int | None = None) -> tuple[ReviewScope, list[str]]:
        """Pick this round's scope and fetch what it needs, as ``(scope, notes)``.

        ``commits`` is (the anchor the previous round reviewed, this round's
        head) — the range an increment would cover. The anchor is ``--since`` if
        the caller passed one, else the ``head_sha`` of the latest baseline, else
        "". ``since_round`` is the round that supplied it, when a baseline did;
        ``base`` is the PR's base branch, which the near context tier is taken
        from.

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


def ci_brief(status: str, failing: list[str], skip: str | None = None) -> str:
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

    * **`PENDING`/`unknown`/`none` must never read as `PASS`.** "CI has not run
      yet" and "CI passed" are different facts, and a reviewer told the wrong one
      is worse off than one told nothing. Each of the five states says which it is.
    * **A pass is not a licence to stop looking.** It says every test we thought
      to write passed — not that the code is correct. A reviewer treating green as
      evidence of correctness has stopped reviewing, and this repo's whole
      argument is that a passing signal is the dangerous kind.
    * **It never adds a fetch.** If `review_ci` was skipped or unreadable the
      brief says so, rather than retrying to make the prompt tidier.
    """
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
    elif status == "none":
        body = ("no checks are configured for this repository, so there is no suite result "
                "either way. This is not a pass.")
    else:
        body = ("could NOT be read"
                + (f" ({skip})" if skip else "")
                + ". Its result is unknown. This is not a pass.")
    return f"{head} {body}"


def review_ci(gh_repo: str, pr_number: int) -> tuple[str, list[str], str | None]:
    """Fetch the PR's CI status via `gh pr checks`. Returns
    (status, failing, skip_reason); status is PASS | FAIL | PENDING | none | unknown
    and `failing` names the non-passing checks. This is a HARD-gate signal: a clean
    LLM/Sonar panel means little if CI (the repo's pytest run — slow tests and all)
    is red or still pending. Panel only SURFACES it; the merge gate itself lives in
    fix-and-land's own `gh pr checks` step. `gh pr checks` exits non-zero when checks
    fail/pend, but still prints the JSON, so we parse stdout regardless of exit code."""
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
            return "none", [], None
        return "unknown", [], f"ci: {hint}"
    try:
        checks = json.loads(raw)
    except json.JSONDecodeError:
        return "unknown", [], "ci: unparseable gh output"
    buckets = [str(c.get("bucket", "")).lower() for c in checks if isinstance(c, dict)]
    failing = [str(c.get("name", "?")) for c in checks
               if isinstance(c, dict) and str(c.get("bucket", "")).lower() == "fail"]
    if not buckets:
        return "none", [], None
    if "fail" in buckets:
        return "FAIL", failing, None
    if "pending" in buckets:
        return "PENDING", failing, None
    return "PASS", failing, None


#: Everything this module offers, INCLUDING the underscore names — the suites
#: reach for several of them through `panel`, and a plain star import would drop
#: them silently. Generated from the module's own top level, so a helper added here
#: is exported without anyone remembering to list it.
__all__ = [
    "panel_core", "_fix_range_diff", "_commit_id", "_head_sha_now",
    "_merge_base_now", "_base_tip_now", "PROVENANCE", "_provenance",
    "DIFF_PREAMBLE", "_diff_by_file", "_diff_subset", "_fit_parts",
    "fetch_increment", "COMPARE_FILE_CAP", "_count", "compare_facts",
    "_range_notes", "_is_commitish", "_is_ref", "_same_commit",
    "_prior_round", "PR_SCOPE_HEADER", "INCREMENT_BRIEF", "JUDGE_INCREMENT_BRIEF",
    "ReviewScope", "_cut_note", "_cut_note_reserve", "_SONAR_SEV",
    "_sonar_findings", "_try", "review_sonarqube", "ci_brief",
    "review_ci",
]

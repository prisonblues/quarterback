#!/usr/bin/env python3
"""Refuse a pull request that closes an issue its own commits only `Refs`.

Twice on 2026-08-22 a PR nearly closed the issue it was written to keep open, and a third
had already done it two days earlier without anyone noticing:

  * **#372 / #371.** The body opened `**This does not close #371** — see the bottom.`
    GitHub's closing-keyword parser does not understand negation: it matched the literal
    `close #371` and listed the issue in `closingIssuesReferences`. A keyword grep over that
    body returns one hit, and the hit reads to a human as a disclaimer.
  * **#363 / #63.** Commit `Fixes #63`, body `Closes #63`, and a PR arguing at length that
    #63's actor half is deliberately unbuilt. Caught at the last gate by a landing agent.
  * **#243 / #165.** Commit `Refs #165, #223, #237, #236`, body "Implements the settings half
    of #165" — and #165 was closed by the merge at 02:06:11 on 2026-08-20, one second after
    it landed. Nobody caught this one at all; it turned up in the survey written for #374.

## The two facts it compares

Prose is not one of them. "Does not close" versus "closes" is exactly the reading that
failed on #372, and no amount of care with the regex fixes a sentence whose meaning is
carried by a negation the tool has to understand. What this compares is structured at both
ends and needs no judgement:

  * **What GitHub parsed** — `closingIssuesReferences` on the pull request, read from the
    GraphQL API. That is not an interpretation of the body; it is the list the merge will
    act on, and it is what the merge button reads.
  * **What the branch says** — the reference lines in the commits of `onto..branch`, in the
    vocabulary this repo already writes: 92 `Refs`, 86 `Fixes`, 12 `Closes` across main's
    history at the time this was written.

A branch whose commits say `Refs #N` and whose pull request would close #N is contradicting
itself, and the contradiction is machine-visible. That is the whole check.

## Silence is not a refusal

Most pull requests carry no reference line at all and legitimately close their issue. If no
commit mentions #N, this says nothing — a check that fires on almost every PR is switched
off within a week, which is the argument the `stamped` job's comment in
`.github/workflows/tests.yml` makes at length and it applies here unchanged.

A closing keyword anywhere in the range also settles it: a branch whose first commit said
`Refs #N` and whose last says `Fixes #N` has finished the work, and the two are not in
conflict. Only `Refs` with no `Fixes`/`Closes` anywhere is a contradiction.

Silence is not free, though, and it is not nothing: an OPEN issue the merge would close that
no commit on the branch mentions is reported as `unclaimed:` and the job raises a
`::warning::` for it — the third state `changelog_fragments.required` and `release_stamp.py
frozen` already use for "passed, and say so out loud". Refusing it was considered: #207
closed #174 on a body keyword alone and was right to, and across eighty merged pull requests
that is one of two, so the rule would be correct most of the time — which is exactly how the
`stamped` job's comment describes a check that gets switched off.

## What it does NOT catch, said here rather than left to be discovered

**#363's original state.** Its commit said `Fixes #63` and GitHub would have closed #63, so
the two facts this reads AGREED. The contradiction existed only against the PR's prose, and
reading prose is the thing that does not work. #363 is caught by a human, by the `changelog`
job (it refuses #363 for a different reason), or not at all.

**An issue no commit mentions at all.** Silence is a pass, by design — see below — so a body
that picks up an issue in passing is only WARNED about, never refused. This is not
theoretical: the first body of the pull request that added this file closed #63, #165 and
#371 alongside the #374 it meant, because it was a body about closing keywords and quoted
them next to real numbers, and every commit on the branch said only `Fixes #374`.

**A body edited through the sidebar.** Linking an issue by hand in the Development panel
populates `closingIssuesReferences` and fires no `pull_request` event, so nothing re-runs.

**A cross-repo reference.** Only a bare `#N` is read as a reference line, so
`Fixes other/repo#5` is invisible here; it also cannot close an issue in this repo.

## Why there is no waiver trailer

Every other refusal in this repo names a trailer that overrides it — `Release-Body-Edit`,
`Changelog-Exempt` — and this one deliberately does not. The remedy for a refusal here is
not to assert an exception; it is to make the branch and the pull request agree, and both
halves are in the author's hands and take one line:

  * the PR really does close #N → say `Fixes #N` on a commit, and push. That is a
    `synchronize`, so the check re-runs.
  * the PR really does not → reword the body so GitHub stops linking it (`#N stays open`
    parses as nothing), and the `edited` trigger re-runs the check.

A waiver here would be a way to merge a pull request that closes an issue while its own
commits say it should not, which is the event this exists to prevent. Run over the eighty
most recently merged pull requests it refuses exactly one — #243 — and that one is a real
instance rather than a case wanting an exemption.

The condition that would justify adding one, so the next reader need not re-derive it: a
branch that legitimately closes #N and *cannot* say so on a commit. Nothing in this fleet
has that shape today — every PR here is opened from a branch its author can amend.

## Exit codes

0 = go · 2 = STOP, a human decides. The same scheme as `scripts/migration_reconcile.py` and
`scripts/release_stamp.py`, and for their reason: a caller reading 0/2 takes Python's
uncaught-exception 1 as "unknown", so every refusal here is an explicit 2 with a sentence.

Usage:
    closing_refs.py check [--repo DIR] [--onto REF] [--branch REF] [--closes FILE] [--json]

`--closes` is GitHub's own answer — the JSON from the GraphQL query below, or `-` for stdin,
which is how `.github/workflows/closing-refs.yml` passes it:

    gh api graphql -f query='{repository(owner:"OWNER",name:"NAME"){pullRequest(number:N){
      closingIssuesReferences(first:50){totalCount nodes{number state}}}}}'
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

#: GitHub's closing keywords, exactly as its documentation lists them. Written out rather
#: than generated from stems: `closing` and `fixing` are NOT in the set, and a stem pattern
#: that swept them in would read a branch as agreeing with GitHub when GitHub had parsed
#: nothing at all — a false pass, which is the direction that costs an issue.
_CLOSING = r"close[sd]?|fixe[sd]|fix|resolve[sd]?"

#: The referencing keywords, kept deliberately narrow. Across main's whole history the
#: non-closing form written here is `Refs`, 92 times, and nothing else. Widening this to
#: `see`, `re` or `related` would start reading ordinary prose as an assertion that the
#: issue stays open, and every one of those readings is a REFUSAL of a correct branch.
_REFING = r"refs?|references?"

#: A reference line: a keyword at the start of a line, then a comma-separated run of bare
#: issue numbers, and nothing else is read.
#:
#: The run stops at the first thing that is not `, #<digits>`, which is what keeps
#: `Refs #165, #223, #237, #236` (PR #243's, all four) apart from `Refs #63 — the actor half
#: … see #100 for why` (one, not two). Reading every `#N` on the line would let a number
#: mentioned in passing be treated as an assertion about that issue, and the assertion this
#: makes on the strength of it is a refusal.
def _line(keywords: str, indent: str) -> re.Pattern[str]:
    return re.compile(
        rf"^{indent}(?:{keywords})[ \t]*:?[ \t]+(#\d+(?:[ \t]*,[ \t]*#\d+)*)",
        re.IGNORECASE | re.MULTILINE,
    )


#: The indentation rule is ASYMMETRIC, and deliberately: a closing line must start at column
#: zero, a reference line may be indented. Which side is strict follows from which direction
#: costs an issue. A match on the closing side is what makes this check PASS, so a `Fixes #N`
#: found inside quoted text would be consent granted by a paste — `release_stamp.py`'s
#: `Release-Body-Edit` comment is about exactly that, and the refusal below is the likeliest
#: text this branch will ever quote. A match on the referencing side only ever refuses, so
#: reading an indented one costs a red check that a `Fixes #N` at column zero clears.
#: Across main's whole history, all ninety-nine reference lines of either kind are at column
#: zero, so nothing real is on the strict side of that line.
_CLOSING_LINE, _REFING_LINE = _line(_CLOSING, ""), _line(_REFING, r"[ \t]*")

#: An issue number in a matched run.
_NUMBER = re.compile(r"#(\d+)")


class ClosingRefsError(Exception):
    """A question this tool will not answer, and will not pass while unanswered."""


def references(text: str, pattern: re.Pattern[str]) -> dict[int, str]:
    """Issue number → the line that referenced it, for every reference line in `text`."""
    found: dict[int, str] = {}
    for match in pattern.finditer(text):
        line = match.group(0).strip()
        for number in _NUMBER.findall(match.group(1)):
            found.setdefault(int(number), line)
    return found


def commits(repo: Path, onto: str, branch: str) -> str:
    """Every commit message in `onto..branch`, concatenated.

    `onto..branch` rather than `merge-base(onto, branch)..branch`, and the difference is
    load-bearing: a branch that merged the base in carries the base's commits through the
    fork point, so a fork-relative range includes messages this branch did not write. One of
    those saying `Fixes #N` would be read as this branch agreeing with GitHub — a false pass
    on somebody else's sentence. Excluding everything reachable from `onto` leaves exactly
    the commits the pull request adds.

    Fails CLOSED. A range git will not walk raises rather than yielding an empty string,
    because an empty string is indistinguishable from a branch that referenced nothing, and
    that is the answer this check treats as consent.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%B%x00", f"{onto}..{branch}"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise ClosingRefsError(
            f"git could not walk {onto}..{branch}, so the commits of this pull request were "
            "never read and nothing here can say whether they contradict GitHub. In CI that "
            "is a checkout without `fetch-depth: 0` — at the default depth of 1 the base "
            f"branch is not in the clone at all. ({proc.stderr.strip()})")
    return proc.stdout.replace("\x00", "\n")


def closes(text: str) -> list[tuple[int, str]]:
    """`(number, state)` for every issue GitHub says this pull request would close.

    Takes the GraphQL response whole rather than a pre-extracted list, so the thing recorded
    in the landing procedure and the thing this reads are one artefact and cannot drift.

    A response carrying `errors` is a refusal and not an empty list. The API answers a query
    it could not satisfy with `data: null` beside them, and `null` read as "closes nothing"
    is this check reporting green because it failed — the one shape an unrunnable gate must
    never take.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise ClosingRefsError(
            f"GitHub's answer is not JSON, so what this pull request would close is unknown "
            f"rather than empty: {e}") from e
    if isinstance(payload, dict) and payload.get("errors"):
        raise ClosingRefsError(
            "the GraphQL query for this pull request's closing references returned errors, "
            "so what it would close is unknown rather than empty: "
            + json.dumps(payload["errors"]))

    node = payload
    for key in ("data", "repository", "pullRequest", "closingIssuesReferences"):
        if isinstance(node, dict) and key in node:
            node = node[key]
    if not isinstance(node, dict) or "nodes" not in node:
        raise ClosingRefsError(
            "no `closingIssuesReferences` in GitHub's answer, so this check has nothing "
            "authoritative to compare the branch against and will not guess from the body")
    nodes = node["nodes"] or []

    # A page is a page. `first: 50` is far past anything this repo has done, but a connection
    # that returned a partial list looks exactly like a complete short one, and the missing
    # entries would be issues this check silently never compared — the failure mode where a
    # gate reports green over the part it did not read. `totalCount` is in the documented
    # query for this and nothing else.
    total = node.get("totalCount")
    if isinstance(total, int) and total > len(nodes):
        raise ClosingRefsError(
            f"GitHub says this pull request closes {total} issues and the query returned "
            f"{len(nodes)} of them, so the rest were never compared against the branch. "
            "Raise `first:` in the query rather than reading a page as the whole list")

    return [(int(n["number"]), str(n.get("state", "OPEN"))) for n in nodes]


def contradictions(
    message: str, closing: list[tuple[int, str]]
) -> list[tuple[int, str]]:
    """`(number, the line)` for every open issue the branch refs and the merge would close.

    Three states per issue, and only the middle one is a refusal:

      * a commit says `Fixes #N` / `Closes #N` — the branch and GitHub agree. Pass, whatever
        else the branch also said about #N: a range whose first commit refs an issue and
        whose last one closes it is a branch that finished the work.
      * a commit says `Refs #N` and none closes it — the contradiction. Refuse.
      * no commit mentions #N at all — silence, which is most pull requests and must not be
        a refusal. This is the whole reason the check survives past its first week.

    Issues already CLOSED are skipped: merging closes nothing there, so there is no event to
    prevent and a red check would be about bookkeeping.
    """
    refs = references(message, _REFING_LINE)
    closed_by_branch = references(message, _CLOSING_LINE)
    return [(number, refs[number]) for number, state in closing
            if state.upper() == "OPEN" and number in refs and number not in closed_by_branch]


def unclaimed(message: str, closing: list[tuple[int, str]]) -> list[int]:
    """Open issues the merge would close that NO commit on this branch mentions at all.

    Not a refusal, and not silence either — the third thing `changelog_fragments.required`
    and `release_stamp.py frozen` both already do with a `waived:` line: report it, pass, and
    make the job say it out loud. The job turns this into a `::warning::`, which is read in
    the checks panel where a green log is not.

    Refusing it was considered and rejected. #207 closed #174 with the keyword in the body
    alone and was entirely correct to; across the eighty most recently merged pull requests
    that is one of two silent closes, so the rule would be right most of the time — and "most
    of the time" is how the `stamped` job's comment describes a check that gets switched off.
    The remedy for a false one is a commit, which is a push, which is the thing an author
    should not have to do to a correct branch.

    Warning rather than nothing, because this is the gap the check has and it is not
    theoretical: the FIRST body of the pull request that added this file closed four issues
    this way. It was a body about closing keywords, so it quoted `Fixes` and `close` beside
    real issue numbers, GitHub linked #63, #165 and #371 along with the #374 it meant, and
    every commit on the branch said only `Fixes #374`. Silence passed it. A partial PR
    arguing in prose about the issue it must not close is now the normal shape here (#277,
    #371, #63), so the body most likely to do this is the body most likely to be written.
    """
    mentioned = references(message, _REFING_LINE) | references(message, _CLOSING_LINE)
    return [number for number, state in closing
            if state.upper() == "OPEN" and number not in mentioned]


def cmd_check(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    source = sys.stdin.read() if args.closes == "-" else Path(args.closes).read_text("utf-8")
    closing = closes(source)

    payload: dict[str, object] = {
        "onto": args.onto, "branch": args.branch,
        "closes": [{"number": n, "state": s} for n, s in closing],
    }

    def report(ok: bool, note: str, loose: list[int] | None = None) -> int:
        payload["ok"] = ok
        payload["unclaimed"] = loose or []
        if args.json:
            payload["note" if ok else "refusal"] = note
            print(json.dumps(payload, indent=2))
            return 0 if ok else 2
        if loose:
            # stderr, and worded as a gap rather than folded into the `ok:` line. The job
            # turns this line into a `::warning::`; an issue closed by a body no commit
            # corroborates is still that, and the one place it has to be visible is the run
            # that let it through.
            print("unclaimed: " + ", ".join(f"#{n}" for n in loose)
                  + " — GitHub will close these and no commit on this branch names them. If "
                    "that is right, nothing to do. If the body picked them up in passing "
                    "(quoting a keyword next to a number does it), reword it and re-read the "
                    "list.", file=sys.stderr)
        print(note if ok else f"STOP: {note}", file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 2

    if not closing:
        # Before reading a single commit. Most pull requests close nothing, and a job that
        # walks the range to say so is spending the budget on the common case.
        return report(True, "ok: GitHub says this pull request closes no issue")

    message = commits(repo, args.onto, args.branch)
    found = contradictions(message, closing)
    loose = unclaimed(message, closing)
    listed = ", ".join(f"#{n}" for n, _ in closing)
    if not found:
        return report(True, f"ok: merging closes {listed}, and no commit on this branch says "
                            "otherwise", loose=loose)

    detail = "\n".join(
        f"    #{number}: merging this pull request closes it, and this branch says\n"
        f"        {line}"
        for number, line in found)
    return report(False, (
        f"this pull request would close an issue its own commits only reference:\n{detail}\n"
        "GitHub's closing-keyword parser does not understand negation — #372's body opened "
        "`**This does not close #371**` and #371 was on the list all the same — so the body "
        "is not evidence and neither is a grep over it. What is on the list is what the "
        "merge acts on.\n"
        "Two ways out, and there is no third; this check has no waiver trailer on purpose:\n"
        "  * it really does close the issue → say so on a commit of this branch, where a "
        "reviewer sees it:  Fixes #" + str(found[0][0]) + "\n"
        "  * it really does not → reword the body until GitHub stops linking it (`#"
        + str(found[0][0]) + " stays open` parses as nothing), and re-read the list:\n"
        f"    gh api graphql -f query='{{repository(owner:\"{args.owner}\",name:"
        f"\"{args.name}\"){{pullRequest(number:N){{closingIssuesReferences(first:50)"
        "{totalCount nodes{number state}}}}}'"), loose=loose)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    ck = sub.add_parser(
        "check",
        help="fail if the merge would close an issue this branch's commits only reference")
    ck.add_argument("--repo", default=".", help="repo dir (default: cwd)")
    ck.add_argument("--json", action="store_true", help="machine-readable verdict")
    ck.add_argument("--onto", default="origin/main", help="the ref you are merging into")
    ck.add_argument(
        "--branch", default="HEAD",
        help="the ref being judged (default: HEAD). A commit, not a worktree — the same "
        "reason as `release_stamp.py frozen`: a gate judges what is being merged.")
    ck.add_argument(
        "--closes", default="-",
        help="GitHub's GraphQL answer for this PR's closingIssuesReferences; `-` is stdin")
    ck.add_argument("--owner", default="prisonblues", help="owner named in the refusal's "
                                                          "pasteable query")
    ck.add_argument("--name", default="quarterback", help="repo named in the refusal's "
                                                          "pasteable query")
    ck.set_defaults(func=cmd_check)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (ClosingRefsError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

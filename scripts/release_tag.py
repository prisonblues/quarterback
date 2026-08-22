#!/usr/bin/env python3
"""Give the release number the lock its own stamper says it does not have.

`release_stamp.py` opens with the diagnosis and does not pretend otherwise:

    A release number is a shared namespace with no lock on it.

Everything it does follows from that sentence. A branch writes `## vNEXT`, the number is
resolved at land time against `origin/main`, and three separate mechanisms exist to catch
the case where two branches resolved it in the same minute. What none of them is, and
what the file says plainly, is an allocator: `max(headings at --onto) + 1` is a *reading*
of a shared file, and two landers reading it seconds apart read the same answer.

The primitive that IS an allocator was sitting unused in git the whole time. Creating a
ref on a remote is atomic and it is compare-and-swap:

    git push origin <sha>:refs/tags/v2.96

succeeds for exactly one caller and is rejected for every other, forever, with no lock
file, no server, no table of numbers going stale for every PR still open — which is the
allocator #172 deleted, and it was deleted for good reasons that do not apply here. That
one recorded an INTENTION to take a number and nothing consulted it. This one is the
number: after it succeeds, v2.96 cannot be issued again, whether or not anybody remembers
to look.

## The invariant

    A tag `vX.Y` points at a commit whose CHANGELOG.md declares `## vX.Y`.

Every command here maintains it and `check` verifies it. It is what makes the tag a fact
about the repository rather than a label somebody attached — and it is why `reserve` wants
the stamp COMMITTED rather than sitting in the worktree: a tag on the commit before the
stamp would name a release that commit has never heard of.

## When the tag is taken, and what that closes

At PUSH time, by `harness/githooks/pre-push`, which already refuses a branch whose number
somebody else has taken and now also takes the number for the branch that is entitled to
it. That placement is the whole point and it is not the obvious one:

  * **At stamp time** the tag would be fork-relative like everything else — `apply` runs in
    a worktree, and a tag created locally is a note to self until it is pushed.
  * **At merge time** the merge is `gh pr merge` through the GitHub API, which runs no local
    hook at all. That is #351's finding one domain over, and a tag taken there would be a
    RECORD of what landed rather than a reservation that stops the second lander.
  * **At push time** the commit exists, the remote is right there, the hook already runs,
    and the create either succeeds or is rejected. Two landers pushing release commits in
    the same second: one push carries the tag, the other is refused with the repair.

**What it does not close, stated here rather than left to be discovered.** `git push
--no-verify` skips the hook, as does a push from a checkout where the hook is not installed,
and neither can be closed from inside a hook. For those the `tagged` CI job on `main`
creates the tag AFTER the merge — a record, not a lock, and it is described as one. And if
NEITHER lander reserves, nothing here helps: both stamp the same number, the second merge
lands a duplicate heading, and `release_stamp.py check` turns main red exactly as it does
today. The lock is real; it is not automatic in a checkout that has opted out of hooks.

A hook also runs BEFORE the push it guards, so a reservation can outlive a push git then
fails to deliver, and a reservation whose pull request is abandoned outlives it too. Both
cost a skipped release number and nothing else: `check` lists a tag that is not on the
integration ref as a reservation rather than a defect, because that is also exactly what
every release still in flight looks like.

## Commands

    release_tag.py reserve  [--repo DIR] [--remote NAME] [--commit REF] [--version vX.Y]
    release_tag.py backfill [--repo DIR] [--ref REF] [--remote NAME] [--push]
    release_tag.py taken    [--repo DIR]
    release_tag.py check    [--repo DIR] [--ref REF]

`backfill` is how the ninety-seven releases that shipped before any of this got tags. It reads the
CHANGELOG at each commit along `--ref`'s first-parent line and tags every release at the
commit that first declared it, which is where it landed. It **never moves a tag** — a tag
that already exists is left exactly where it is, and one pointing somewhere the invariant
does not hold is REPORTED rather than corrected. A moved tag is worse than an absent one:
absent, everybody knows they have to look it up; moved, everybody trusts the wrong answer.

## Exit codes

0 = go / clean · 1 = checked, but a condition could not be checked · 2 = STOP.

The 1 is the third answer `qb-reconcile` settled the shape for in #255, and it is deliberate
rather than inherited: a doctor that cannot reach the remote must say so, not report a tidy
repo. Every unexpected exception is mapped to 2 with a sentence, so a bare 1 out of the
Python interpreter can never be mistaken for the documented one — the same promise
`release_stamp.py` makes about never exiting 1 at all, kept by a different route because
this file has a use for the code.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


class TagError(Exception):
    """A refusal with a sentence attached. Always exits 2."""


class Unavailable(Exception):
    """A condition that could not be checked. Exits 1 — never folded into a finding."""


def _load_stamper():
    """Import `release_stamp.py` from beside this file.

    `scripts/` is a directory of standalone tools rather than a package, so this is a path
    load and not an import statement. It is worth the awkwardness: what counts as a release
    heading in this repo is a masked-markdown question with fenced blocks, inline code spans,
    four-backtick fences and blockquote-prefixed fences all mattering, and this repo's own
    CHANGELOG quotes `## vX.Y` inside examples. A second regex here would agree with the
    first until the day it did not, and the day it did not is the day a tag gets created for
    a release that only ever existed in a code sample.
    """
    path = Path(__file__).resolve().parent / "release_stamp.py"
    if not path.exists():
        raise TagError(
            f"no {path} beside this script. The release tag is the number the stamper hands "
            "out, and what counts as a release heading is that file's answer, not a second "
            "one kept in step by hand"
        )
    spec = importlib.util.spec_from_file_location("release_stamp", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise TagError(f"{path} could not be loaded as a module")
    module = importlib.util.module_from_spec(spec)
    # Registered before it executes: @dataclass resolves annotations through
    # sys.modules[cls.__module__], and `release_stamp` defines dataclasses at import.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rs = _load_stamper()

#: A tag this tool owns. `v2`, `v2.96` — the exact spellings `release_stamp.fmt` produces,
#: anchored at both ends so `v2.96-rc1`, `salvage/issue-85` and `v2.96.1` are somebody
#: else's refs and are left entirely alone. This tool has no opinion about tags it did not
#: create, and a repo is allowed to have others.
#:
#: The stamper's pattern, not a copy of it: `next_release` skips past the numbers these tags
#: hold, so the two files have to mean the same thing by "release tag" or one of them hands
#: out a number the other has already locked.
_TAG = rs.TAG_NAME

CHANGELOG = "CHANGELOG.md"


# --------------------------------------------------------------------------------- git


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def _git(repo: Path, *args: str) -> str:
    proc = _run(repo, *args)
    if proc.returncode != 0:
        raise TagError(f"git {' '.join(args)} failed: {proc.stderr.strip() or 'no output'}")
    return proc.stdout


def _git_ok(repo: Path, *args: str) -> bool:
    return _run(repo, *args).returncode == 0


def resolve(repo: Path, ref: str) -> str:
    if not _git_ok(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
        raise TagError(
            f"ref {ref!r} does not exist here. Fetch it first — a tag is only meaningful "
            "relative to the commit it names"
        )
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return _git_ok(repo, "merge-base", "--is-ancestor", older, newer)


def changelog_at(repo: Path, rev: str) -> str | None:
    """CHANGELOG.md at a commit, or None when that commit has none.

    None rather than a refusal: this tool walks history, and the early commits of any repo
    predate its CHANGELOG. A commit without one declares no releases, which is the truth.
    """
    if not _git_ok(repo, "cat-file", "-e", f"{rev}:{CHANGELOG}"):
        return None
    return _git(repo, "show", f"{rev}:{CHANGELOG}")


def releases_at(repo: Path, rev: str) -> set[rs.Release]:
    text = changelog_at(repo, rev)
    if text is None:
        return set()
    return set(rs.releases_in(text, f"{rev}:{CHANGELOG}"))


# ------------------------------------------------------------------------------ tags


def local_tags(repo: Path) -> dict[rs.Release, str]:
    """Every release tag in this checkout, as {release: commit sha}.

    The stamper's own reader, not a second one. `release_stamp.next_release` folds these
    numbers into the counter, so "which tags are release tags" has to have exactly one
    answer: two implementations of that predicate agree until the day one of them reads
    `v2.96-rc1` as a release, and by then a tag exists for a number nothing explains.
    """
    try:
        return rs.tag_releases(repo)
    except rs.StampError as e:
        raise TagError(str(e)) from e


def remote_tag(repo: Path, remote: str, name: str) -> str | None:
    """The sha `refs/tags/<name>` has ON THE REMOTE right now, or None if it is not there.

    The authority, and the reason `reserve` does not trust the local tag list. A sibling's
    reservation points at a commit on the sibling's branch, which is not reachable from
    `main` — and `git fetch <remote>` follows tags only into history it fetched, so a plain
    fetch does not bring it back. The one place the answer is always current is the remote.
    """
    proc = _run(repo, "ls-remote", "--tags", "--", remote, f"refs/tags/{name}")
    if proc.returncode != 0:
        raise Unavailable(
            f"could not read tags from {remote!r}: "
            f"{proc.stderr.strip() or 'git ls-remote failed'}"
        )
    found: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if sha and ref:
            found[ref.strip()] = sha.strip()
    # An annotated tag comes back twice: the tag object, and a `^{}` line holding the commit
    # it peels to. The peeled one is the answer wherever both are present — comparing a tag
    # object's sha against a commit sha would report every annotated tag as pointing
    # somewhere unrelated.
    return found.get(f"refs/tags/{name}^{{}}") or found.get(f"refs/tags/{name}")


def remote_release_tags(repo: Path, remote: str) -> set[rs.Release]:
    """Every release number the remote already has a tag for, in ONE `ls-remote`.

    Whole-namespace rather than a lookup per name, because `backfill` asks this about
    ninety-seven of them at once and ninety-seven round trips to answer one question is not
    a shape anybody would choose to run after every merge.
    """
    proc = _run(repo, "ls-remote", "--tags", "--", remote)
    if proc.returncode != 0:
        raise Unavailable(
            f"could not read tags from {remote!r}: "
            f"{proc.stderr.strip() or 'git ls-remote failed'}"
        )
    found: set[rs.Release] = set()
    for line in proc.stdout.splitlines():
        _, _, ref = line.partition("\t")
        ref = ref.strip().removesuffix("^{}")
        if ref.startswith("refs/tags/"):
            m = _TAG.match(ref[len("refs/tags/"):])
            if m:
                found.add(rs.release(m.group(1), m.group(2)))
    return found


def has_remote(repo: Path, remote: str) -> bool:
    return remote in _git(repo, "remote").split()


# --------------------------------------------------------------------------- reserve


def newest_release_at(repo: Path, rev: str) -> rs.Release:
    """The highest release the CHANGELOG declares at a commit.

    Highest and not first, for `next_release`'s reason: the file is newest-first and a test
    enforces it, but a tool that hands out or locks a number must not be the one thing
    trusting an ordering it is about to disturb.
    """
    text = changelog_at(repo, rev)
    if text is None:
        raise TagError(
            f"no {CHANGELOG} at {rev[:12]}, so there is no release there to reserve a number "
            "for. Commit the stamped entry first"
        )
    found = rs.releases_in(text, f"{rev}:{CHANGELOG}")
    if not found:
        raise TagError(
            f"{CHANGELOG} at {rev[:12]} declares no `## vX.Y` release. A branch still "
            f"carrying `## {rs.PLACEHOLDER}` has no number yet — run `release_stamp.py "
            "apply --onto origin/main` and commit it, then reserve"
        )
    return max(found)


def cmd_reserve(args: argparse.Namespace) -> int:
    """Take the number, atomically, against the remote.

    The whole mechanism is one `git push` of one ref that does not exist yet. Everything
    around it is there to produce a message worth reading when that push is rejected —
    because "rejected (already exists)" is correct and tells a lander nothing about what to
    do next, and the repair here is genuinely two tokens.

    Without `--version` the number is the newest release the COMMIT declares, which is what a
    lander has just stamped. On a branch that stamped nothing that is a number it merely
    inherited — harmless once `backfill` has run, since the tag for a landed release is then
    an ancestor of every branch and this reports "already ours" without creating anything,
    and avoided outright by `--onto`, which the pre-push hook always passes.
    """
    repo = Path(args.repo).resolve()
    commit = resolve(repo, args.commit)

    if args.version:
        m = _TAG.match(args.version)
        if not m:
            raise TagError(
                f"{args.version!r} is not a release number this repo uses. They are spelled "
                "`v2` and `v2.96` — two components, no prefix, no suffix"
            )
        version = rs.release(m.group(1), m.group(2))
        declared = releases_at(repo, commit)
        if version not in declared:
            raise TagError(
                f"{CHANGELOG} at {args.commit} ({commit[:12]}) does not declare "
                f"`## {rs.fmt(version)}`. A tag names a release the commit it points at "
                "actually contains — reserving one it does not would put a number in the "
                "repository that no document explains"
            )
    else:
        version = newest_release_at(repo, commit)

    name = rs.fmt(version)

    # `--onto` makes this safe to run on EVERY push, which is what puts it in the pre-push
    # hook rather than in a runbook step somebody has to remember. A branch that stamped
    # nothing carries the base's newest release as its own newest heading — inherited, not
    # claimed — and reserving that would be one pointless round-trip per push at best and,
    # in a repo whose tags are behind, a tag created for a release somebody else landed.
    # Present at the base means landed, means already somebody's, means nothing to take.
    if args.onto:
        onto = resolve(repo, args.onto)
        if version in releases_at(repo, onto):
            _say(args, {"version": name, "commit": commit, "state": "nothing-to-reserve"},
                 f"nothing to reserve: {name} is already at {args.onto} — this commit adds "
                 "no release number of its own.")
            return 0

    if not has_remote(repo, args.remote):
        raise Unavailable(
            f"no remote named {args.remote!r} in this repo, so {name} cannot be reserved "
            "against anything. A tag that exists only here reserves nothing — the lock is "
            "the remote refusing the second create"
        )

    held = remote_tag(repo, args.remote, name)
    if held is not None:
        if held == commit or is_ancestor(repo, held, commit):
            _say(args, {"version": name, "commit": commit, "held_by": held,
                        "state": "already-ours"},
                 f"{name} is already tagged at {held[:12]}, which this commit contains — "
                 "nothing to reserve.")
            return 0
        raise TagError(
            f"{name} is already reserved on {args.remote}, at {held[:12]}, which is not a "
            f"commit this branch contains. Another branch stamped {name} and pushed first, "
            f"so {name} is theirs.\n"
            f"{_repair(name)}"
        )

    proc = _run(repo, "push", "--no-verify", "--", args.remote,
                f"{commit}:refs/tags/{name}")
    if proc.returncode != 0:
        # The `ls-remote` above is a courtesy that produces a good message; THIS is the lock.
        # A rejection here after a clean read is the real race — two landers between the read
        # and the push — and it is the case the whole file exists for, so it is reported as
        # the collision it is rather than as a git error.
        again = None
        with contextlib.suppress(Unavailable):
            again = remote_tag(repo, args.remote, name)
        if again is not None and again != commit:
            raise TagError(
                f"{name} was taken on {args.remote} between reading it and creating it — "
                f"it is at {again[:12]} now. That is two landers in the same second, which "
                "is the collision this tag exists to make impossible rather than merely "
                f"unlikely: the create is atomic, so exactly one of you has {name}.\n"
                f"{_repair(name)}"
            )
        raise Unavailable(
            f"could not create refs/tags/{name} on {args.remote}: "
            f"{(proc.stderr or proc.stdout).strip() or 'git push failed'}"
        )

    _say(args, {"version": name, "commit": commit, "held_by": commit, "state": "reserved"},
         f"reserved {name} at {commit[:12]} on {args.remote} — "
         f"refs/tags/{name} now exists and cannot be created again.")
    return 0


def _repair(name: str) -> str:
    return (
        f"Put THIS branch's entry back to `## {rs.PLACEHOLDER} — …` (and its README bullet "
        f"back to `- **{rs.PLACEHOLDER}** — …`), then:\n"
        "    git fetch origin --tags\n"
        "    scripts/release_stamp.py apply --onto origin/main\n"
        "    git commit --amend -a\n"
        "then push again. Nothing on the branch was written in terms of the number, which "
        "is what makes that a two-token edit rather than a rewrite."
    )


# -------------------------------------------------------------------------- backfill


def landings(repo: Path, ref: str) -> dict[rs.Release, str]:
    """{release: the commit on `ref`'s first-parent line that first declared it}.

    First-parent, so a merge commit is where a release LANDED and the branch commits that
    built it are not candidates. That matters for exactly the case this repo has: a release
    is stamped on a branch and merged minutes later, and the honest answer to "where is
    v2.63" is the commit that put it on main.

    One pass over the commits that touched the CHANGELOG, oldest first, reading each blob
    once. `git log -S` per release would be 97 walks of the same history and would still
    need this to disambiguate a heading whose text later changed.
    """
    out = _git(repo, "rev-list", "--reverse", "--first-parent", ref, "--", CHANGELOG)
    seen: dict[rs.Release, str] = {}
    for rev in out.split():
        for r in sorted(releases_at(repo, rev)):
            seen.setdefault(r, rev)
    return seen


def cmd_backfill(args: argparse.Namespace) -> int:
    """Tag every release that has already landed, at the commit that landed it.

    Additive and idempotent by construction: it creates tags that are missing and touches
    nothing else. Run it twice and the second run does nothing. Run it on a repo whose tags
    are all correct and it does nothing.

    **It never moves a tag.** A tag that exists at a commit where the invariant does not
    hold is reported and left there. Tags are immutable by convention because everything
    downstream of one — a `git describe`, a deploy that pinned it, somebody's bookmark —
    quietly changes meaning when they are not, and a moved tag is worse than an absent one:
    absent, everybody knows to look it up.
    """
    repo = Path(args.repo).resolve()
    ref = resolve(repo, args.ref)
    where = landings(repo, ref)
    if not where:
        raise TagError(
            f"no release headings in {CHANGELOG} anywhere along {args.ref}. Either that is "
            "not the ref you meant or this repo does not use this convention"
        )

    have = local_tags(repo)
    created: list[tuple[str, str]] = []
    misplaced: list[str] = []
    for r in sorted(where):
        name = rs.fmt(r)
        target = where[r]
        if r in have:
            if have[r] != target and r not in releases_at(repo, have[r]):
                misplaced.append(
                    f"{name} -> {have[r][:12]}, whose {CHANGELOG} does not declare it "
                    f"(it landed at {target[:12]}). Left where it is; tags are not moved"
                )
            continue
        if not args.dry_run:
            _git(repo, "tag", "-a", "-m", f"{name}\n\nBackfilled from {CHANGELOG}.", name,
                 target)
        created.append((name, target))

    pushed: list[str] = []
    unpushed: list[str] = []
    if args.push and not args.dry_run:
        if not has_remote(repo, args.remote):
            raise Unavailable(
                f"no remote named {args.remote!r}, so nothing can be published. A tag that "
                "is only local records nothing for anybody else"
            )
        # WHAT IS MISSING ON THE REMOTE, not what this run happened to create — and the
        # difference is a real bug rather than a tidiness point. A run whose push failed
        # (no write access on the token, say) leaves the tags created and unpublished; the
        # next run then finds them already present locally, creates nothing, has nothing in
        # `created` to push, and reports success over a remote that still has none of them.
        # That is a clean bill from a check that was never made, in the one command whose
        # whole job is publishing.
        want = sorted(set(local_tags(repo)) - remote_release_tags(repo, args.remote))
        if want:
            # One `git push` for the lot, and NOT `--force`: a tag another machine created
            # while this ran is somebody else's answer to the same question and is left
            # alone. Those come back as rejections, which is why the outcome is judged on
            # what is on the remote afterwards rather than on this command's return code.
            _run(repo, "push", "--no-verify", "--", args.remote,
                 *(f"refs/tags/{rs.fmt(r)}" for r in want))
            landed = remote_release_tags(repo, args.remote)
            pushed = [rs.fmt(r) for r in want if r in landed]
            unpushed = [rs.fmt(r) for r in want if r not in landed]

    payload = {"created": [n for n, _ in created], "pushed": pushed, "unpushed": unpushed,
               "misplaced": misplaced, "tagged": len(have) + len(created),
               "releases": len(where)}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        verb = "would create" if args.dry_run else "created"
        print(f"{verb} {len(created)} tag(s) for {len(where)} release(s) along {args.ref}"
              + (f"; pushed {len(pushed)} to {args.remote}" if args.push else ""))
        for name, target in created:
            print(f"  {name} -> {target[:12]}")
        for line in misplaced:
            print(f"warning: {line}", file=sys.stderr)
    if unpushed:
        raise Unavailable(
            f"{len(unpushed)} tag(s) exist here and not on {args.remote} "
            f"({', '.join(unpushed[:6])}{'…' if len(unpushed) > 6 else ''}). They are a "
            "record of nothing until they are pushed, and re-running this will try again"
        )
    return 0


# ----------------------------------------------------------------------------- check


def cmd_taken(args: argparse.Namespace) -> int:
    """Which numbers are spoken for, according to this checkout's tags.

    Deliberately local and deliberately not the authority — `reserve` is. This answers "what
    would `release_stamp.py` skip past", which is a question about the refs that are here.
    """
    repo = Path(args.repo).resolve()
    have = local_tags(repo)
    if args.json:
        print(json.dumps({rs.fmt(r): have[r] for r in sorted(have)}, indent=2))
    else:
        for r in sorted(have):
            print(f"{rs.fmt(r)}\t{have[r]}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Reconcile the tags against the CHANGELOG. One condition per line, never one verdict.

    The doctor discipline this repo settled in #255 and #163: folding several answers into
    one number leaves a reader unable to tell a clean repo from one where a check could not
    run. So each condition reports for itself, and "could not be checked" is its own exit
    code rather than a lesser version of "clean".

    An UNREACHABLE tag is not a finding. A reservation points at a branch commit, and until
    that branch merges the tag is correctly not on `--ref` — that is the state of every
    release in flight. It is listed, because an abandoned one means a skipped number and
    somebody should be able to see why v2.96 has no entry.
    """
    repo = Path(args.repo).resolve()
    ref = resolve(repo, args.ref)
    declared = releases_at(repo, ref)
    if not declared:
        raise TagError(
            f"{CHANGELOG} at {args.ref} declares no releases, so there is nothing for the "
            "tags to be reconciled against"
        )
    have = local_tags(repo)

    untagged = sorted(declared - set(have))
    unreachable: list[tuple[rs.Release, str]] = []
    wrong: list[tuple[rs.Release, str]] = []
    for r in sorted(have):
        sha = have[r]
        if r not in releases_at(repo, sha):
            wrong.append((r, sha))
        elif not is_ancestor(repo, sha, ref):
            unreachable.append((r, sha))

    lines = [
        f"{len(declared) - len(untagged)}/{len(declared)} release(s) at {args.ref} have a tag",
        f"{len(have) - len(wrong)}/{len(have)} tag(s) point at a commit that declares them",
        f"{len(unreachable)} tag(s) not on {args.ref} (a release in flight, or abandoned)",
    ]
    findings = bool(untagged or wrong)

    if args.json:
        print(json.dumps({
            "clean": not findings,
            "ref": args.ref,
            "untagged": [rs.fmt(r) for r in untagged],
            "misplaced": {rs.fmt(r): sha for r, sha in wrong},
            "unreachable": {rs.fmt(r): sha for r, sha in unreachable},
        }, indent=2))
        return 2 if findings else 0

    for line in lines:
        print(line)
    for r, sha in unreachable:
        print(f"  {rs.fmt(r)} reserved at {sha[:12]}, not merged into {args.ref}")
    if untagged:
        print(f"\nSTOP: no tag for {', '.join(rs.fmt(r) for r in untagged)}. That number is "
              "not locked, so nothing stops it being issued twice.\n"
              f"    scripts/release_tag.py backfill --ref {args.ref} --push",
              file=sys.stderr)
    if wrong:
        for r, sha in wrong:
            print(f"STOP: {rs.fmt(r)} is tagged at {sha[:12]}, whose {CHANGELOG} does not "
                  "declare it. Tags are not moved by this tool — a human decides whether "
                  "that tag or that entry is the wrong one.", file=sys.stderr)
    if findings:
        return 2
    print("clean: every release is tagged and every tag names a release that commit carries.")
    return 0


# ------------------------------------------------------------------------------- cli


def _say(args: argparse.Namespace, payload: dict, line: str) -> None:
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(line)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--repo", default=".", help="repo dir (default: cwd)")
        sp.add_argument("--json", action="store_true", help="machine-readable output")

    rv = sub.add_parser("reserve", help="take this commit's release number on the remote")
    common(rv)
    rv.add_argument("--remote", default="origin", help="the remote that holds the lock")
    rv.add_argument("--commit", default="HEAD",
                    help="the commit to tag (default: HEAD). A commit, not a worktree — the "
                         "pre-push hook reserves for what is being PUSHED.")
    rv.add_argument("--version", default=None,
                    help="the number to reserve (default: the newest release the commit "
                         "declares)")
    rv.add_argument("--onto", default=None,
                    help="do nothing unless the number is one this commit ADDS — i.e. it is "
                         "not already present at this ref. What makes the command safe to "
                         "run on every push.")
    rv.set_defaults(func=cmd_reserve)

    bf = sub.add_parser("backfill", help="tag every release that has already landed")
    common(bf)
    bf.add_argument("--ref", default="HEAD", help="the integration ref to read (default: HEAD)")
    bf.add_argument("--remote", default="origin", help="where --push sends them")
    bf.add_argument("--push", action="store_true", help="publish the tags it creates")
    bf.add_argument("--dry-run", action="store_true", help="report, create nothing")
    bf.set_defaults(func=cmd_backfill)

    tk = sub.add_parser("taken", help="the release numbers this checkout's tags hold")
    common(tk)
    tk.set_defaults(func=cmd_taken)

    ck = sub.add_parser("check", help="reconcile the tags against CHANGELOG.md")
    common(ck)
    ck.add_argument("--ref", default="HEAD", help="the integration ref to read (default: HEAD)")
    ck.set_defaults(func=cmd_check)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except Unavailable as e:
        print(f"limited: {e}", file=sys.stderr)
        return 1
    except TagError as e:
        print(f"STOP: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        # 1 means "could not be checked" here and nothing else. Python's own uncaught
        # exception exits 1 too, so an unexpected error MUST NOT reach the interpreter:
        # a caller told that 1 is a soft answer would read a traceback as one.
        print(f"STOP: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

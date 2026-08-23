#!/usr/bin/env python3
"""Every release has a tag, and every tag points at the commit that shipped it.

    A tag `vX.Y` points at a commit whose CHANGELOG.md declares `## vX.Y`.

That is the whole invariant. `backfill` establishes it and `check` verifies it, and between
them they make a release number a fact about the repository rather than a label somebody
attached. `git describe`, `gh release view`, "what was running on the box that afternoon" —
all of it is downstream of this one line holding.

    release_tag.py backfill [--repo DIR] [--ref REF] [--remote NAME] [--push]
    release_tag.py taken    [--repo DIR]
    release_tag.py check    [--repo DIR] [--ref REF]

`backfill` is how the ninety-seven releases that shipped before any of this got tags. It reads
the CHANGELOG at each commit along `--ref`'s first-parent line and tags every release at the
commit that first declared it, which is where it landed. It **never moves a tag** — a tag that
already exists is left exactly where it is, and one pointing somewhere the invariant does not
hold is REPORTED rather than corrected. A moved tag is worse than an absent one: absent,
everybody knows they have to look it up; moved, everybody trusts the wrong answer.

The tags it writes are annotated, which means it writes an OBJECT, which means git wants to
know who is tagging. It supplies a tagger itself when the environment cannot name one, so the
command works on a bare CI runner and not only where somebody remembered to run `git config`
first (#379). A resolvable identity is never overridden: see `_tagger_env`.

## There is nothing to reserve any more (#122)

`reserve` is deleted. It existed to take `refs/tags/vX.Y` on the remote at PUSH time, as a
compare-and-swap that succeeded for exactly one lander — the lock a branch-side stamp needed,
because `max(headings at the base) + 1` is a *reading* of a shared file and two landers
reading it seconds apart read the same answer.

Branches do not stamp. The number is issued on `main`, after the merge, by
`scripts/release.py run`, which reads the CHANGELOG at the commit it is about to tag. There is
no race on `main`, so there is nothing to reserve, nothing to collide, and no separate
`chore(release)` commit on a branch for a squash merge to discard — which is #406 removed
rather than detected. `release.py run` creates the tag itself, on the commit it just made;
`backfill` is the repair for a release that landed without one.

## The tag that is off the ref, and what it means now

Off the integration ref is two things wearing one face, and the CHANGELOG at the ref is what
separates them:

  * the ref does **not** declare `vX.Y` — the release is not there. Since #122 that means a
    release cut locally and not yet pushed, or a tag left over from the reservation era.
    Listed as `reserved`, never a finding: it holds a number nothing else will hand out, and
    deleting a tag is something this file will not do for the same reason it never moves one.
  * the ref **does** declare `vX.Y` — the release HAS landed, so its tag must be reachable
    from the commit that landed it. Off the ref, it is `orphaned`, and that is a finding.
    `v3.8` shipped that way in the reservation era and every check the repo had reported it
    as fully tagged, because they asked whether a tag of that NAME resolved (#406).

One `git merge-base --is-ancestor` cannot make that call on its own; it needs the file to say
which releases have landed. `reconcile` below is the one place either question is answered,
because two implementations of "defect or leftover" agree right up until the morning one of
them is asked about a squash.

## Exit codes

0 = go / clean · 1 = checked, but a condition could not be checked · 2 = STOP.

The 1 is the third answer `qb-reconcile` settled the shape for in #255, and it is deliberate
rather than inherited: a doctor that cannot reach the remote must say so, not report a tidy
repo. Every unexpected exception is mapped to 2 with a sentence, so a bare 1 out of the
Python interpreter can never be mistaken for the documented one — the same promise
`release.py` makes about never exiting 1 at all, kept by a different route because this file
has a use for the code.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


class TagError(Exception):
    """A refusal with a sentence attached. Always exits 2."""


class Unavailable(Exception):
    """A condition that could not be checked. Exits 1 — never folded into a finding."""


def _load_stamper():
    """Import `release.py` from beside this file.

    `scripts/` is a directory of standalone tools rather than a package, so this is a path
    load and not an import statement. It is worth the awkwardness: what counts as a release
    heading in this repo is a masked-markdown question with fenced blocks, inline code spans,
    four-backtick fences and blockquote-prefixed fences all mattering, and this repo's own
    CHANGELOG quotes `## vX.Y` inside examples. A second regex here would agree with the
    first until the day it did not, and the day it did not is the day a tag gets created for
    a release that only ever existed in a code sample.
    """
    path = Path(__file__).resolve().parent / "release.py"
    if not path.exists():
        raise TagError(
            f"no {path} beside this script. The release tag is the number the stamper hands "
            "out, and what counts as a release heading is that file's answer, not a second "
            "one kept in step by hand"
        )
    spec = importlib.util.spec_from_file_location("release", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise TagError(f"{path} could not be loaded as a module")
    module = importlib.util.module_from_spec(spec)
    # Registered before it executes: @dataclass resolves annotations through
    # sys.modules[cls.__module__], and `release` defines dataclasses at import.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rs = _load_stamper()

#: A tag this tool owns. `v2`, `v2.96` — the exact spellings `release.fmt` produces,
#: anchored at both ends so `v2.96-rc1`, `salvage/issue-85` and `v2.96.1` are somebody
#: else's refs and are left entirely alone. This tool has no opinion about tags it did not
#: create, and a repo is allowed to have others.
#:
#: The release tool's pattern, not a copy of it: `next_release` folds the numbers these tags
#: hold into the same `max`, so the two files have to mean the same thing by "release tag" or
#: one of them hands out a number the other has already issued.
_TAG = rs.TAG_NAME

CHANGELOG = "CHANGELOG.md"


# --------------------------------------------------------------------------------- git


def _run(repo: Path, *args: str, env: dict[str, str] | None = None
         ) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False, env=env
    )


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = _run(repo, *args, env=env)
    if proc.returncode != 0:
        raise TagError(f"git {' '.join(args)} failed: {proc.stderr.strip() or 'no output'}")
    return proc.stdout


def _git_ok(repo: Path, *args: str) -> bool:
    return _run(repo, *args).returncode == 0


#: Who a backfilled tag is FROM when nothing else can say. An annotated tag is an object and
#: an object has a tagger, so git refuses to write one where it cannot name anybody — and the
#: place where it never can is a CI runner: no `user.name`, and no GECOS field to guess one
#: from. That is #379, where the `tagged` job died on `fatal: empty ident name` and two
#: releases landed untagged. The address is `.invalid` on purpose: a backfill's tagger is a
#: script, and an address that cannot receive mail says so rather than implying somebody.
_TAGGER_NAME = "release_tag.py"
_TAGGER_EMAIL = "release-tag@quarterback.invalid"


def _tagger_env(repo: Path) -> dict[str, str]:
    """The environment for a git command that writes a tag OBJECT.

    Whatever git can already work out wins. The gate is `git var GIT_COMMITTER_IDENT`, which
    is the same question git asks itself before it writes an object: when it answers, this
    returns the environment untouched and the tag carries the caller's own name exactly as it
    would have. Only when it refuses does anything here apply — and then a configured half is
    still preferred over the fallback, because a set `user.name` with no resolvable email is a
    real shape and inventing a name over it would be a lie about who tagged.

    Env rather than `-c user.name=…`: an exported `GIT_COMMITTER_NAME` outranks any config, so
    the empty one that CI leaves behind would outrank a `-c` too. Reading each var and filling
    only what is blank covers unset, set-to-empty and set-to-whitespace alike.

    `git tag -a` reads the committer half only; the author half is set alongside it because an
    environment where the two disagree is a trap for whatever writes an object here next, and
    it costs two dict entries.
    """
    env = dict(os.environ)
    if _run(repo, "var", "GIT_COMMITTER_IDENT").returncode == 0:
        return env
    for var, key, fallback in (
        ("GIT_COMMITTER_NAME", "user.name", _TAGGER_NAME),
        ("GIT_AUTHOR_NAME", "user.name", _TAGGER_NAME),
        ("GIT_COMMITTER_EMAIL", "user.email", _TAGGER_EMAIL),
        ("GIT_AUTHOR_EMAIL", "user.email", _TAGGER_EMAIL),
    ):
        # `.strip()`, because git strips an ident before it judges it: a name of one space
        # is truthy here and still `empty ident name` there.
        if not env.get(var, "").strip():
            env[var] = _run(repo, "config", "--get", key).stdout.strip() or fallback
    return env


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

    The release tool's own reader, not a second one. `release.next_release` folds these
    numbers into the counter, so "which tags are release tags" has to have exactly one
    answer: two implementations of that predicate agree until the day one of them reads
    `v2.96-rc1` as a release, and by then a tag exists for a number nothing explains.
    """
    try:
        return rs.tag_releases(repo)
    except rs.ReleaseError as e:
        raise TagError(str(e)) from e


def remote_tag(repo: Path, remote: str, name: str) -> str | None:
    """The sha `refs/tags/<name>` has ON THE REMOTE right now, or None if it is not there.

    The authority. A tag created in another checkout and pushed points at a commit that need
    not be reachable from `main` — and `git fetch <remote>` follows tags only into history it
    fetched, so a plain fetch does not bring it back. The one place the answer is always
    current is the remote.
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


# ------------------------------------------------------------------- what a ref declares


def newest_release_at(repo: Path, rev: str) -> rs.Release:
    """The highest release the CHANGELOG declares at a commit.

    Highest and not first, for `next_release`'s reason: the file is newest-first and a test
    enforces it, but a tool that hands out or locks a number must not be the one thing
    trusting an ordering it is about to disturb.
    """
    text = changelog_at(repo, rev)
    if text is None:
        raise TagError(
            f"no {CHANGELOG} at {rev[:12]}, so there is no release there to tag"
        )
    found = rs.releases_in(text, f"{rev}:{CHANGELOG}")
    if not found:
        raise TagError(
            f"{CHANGELOG} at {rev[:12]} declares no `## vX.Y` release, so there is no "
            "number here to tag. Releases are cut on `main` by `release.py run`"
        )
    return max(found)


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
    # Resolved once, before anything is written: the probe inside it is a git call, and the
    # loop below runs once per release this repo has ever had.
    tagger = None if args.dry_run else _tagger_env(repo)
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
                 target, env=tagger)
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

    Deliberately local and deliberately not the authority — the remote is. This answers "what
    would `release.py` skip past", which is a question about the refs that are here.
    """
    repo = Path(args.repo).resolve()
    have = local_tags(repo)
    if args.json:
        print(json.dumps({rs.fmt(r): have[r] for r in sorted(have)}, indent=2))
    else:
        for r in sorted(have):
            print(f"{rs.fmt(r)}\t{have[r]}")
    return 0


def reconcile(repo: Path, ref: str) -> dict:
    """Every release tag judged against an integration ref, one condition per key.

    The one place either "is this tag a defect" question is answered, and public so that
    it stays that way: `cmd_check` renders it, the `tagged` CI job exits on it, and
    `qb-doctor` reads it back out of `--json` (#406). The discriminator between an orphan
    and a reservation is subtle enough that a second implementation would get it wrong,
    and would do so silently — both look like "tag not on the ref".

    The keys, and none of them is a summary of another:

    ``untagged``      releases the ref declares that no tag holds. The number is unlocked.
    ``misplaced``     a tag whose own commit's CHANGELOG does not declare it. The invariant
                      this whole file maintains, broken.
    ``orphaned``      a tag for a release the ref DOES declare, pointing off the ref. #406:
                      the release landed, so its tag has to be reachable from where it
                      landed. A squash or rebase merge is how this happens.
    ``reserved``      a tag for a release the ref does NOT declare, pointing off the ref.
                      A release cut locally and not pushed, or a leftover from before #122
                      deleted push-time reservation. Never a finding.

    ``findings`` is the subset that means something is wrong: `untagged`, `misplaced`,
    `orphaned`. `reserved` is deliberately outside it.
    """
    ref_sha = resolve(repo, ref)
    declared = releases_at(repo, ref_sha)
    if not declared:
        raise TagError(
            f"{CHANGELOG} at {ref} declares no releases, so there is nothing for the "
            "tags to be reconciled against"
        )
    have = local_tags(repo)

    untagged = sorted(declared - set(have))
    misplaced: list[tuple[rs.Release, str]] = []
    orphaned: list[tuple[rs.Release, str]] = []
    reserved: list[tuple[rs.Release, str]] = []
    for r in sorted(have):
        sha = have[r]
        if r not in releases_at(repo, sha):
            misplaced.append((r, sha))
        elif is_ancestor(repo, sha, ref_sha):
            continue
        elif r in declared:
            orphaned.append((r, sha))
        else:
            reserved.append((r, sha))

    return {"ref": ref, "ref_sha": ref_sha, "declared": sorted(declared), "tags": have,
            "untagged": untagged, "misplaced": misplaced, "orphaned": orphaned,
            "reserved": reserved, "findings": bool(untagged or misplaced or orphaned)}


def _ref_oid(repo: Path, r: rs.Release) -> str:
    """What `refs/tags/vX.Y` itself holds, unpeeled — the value a lease has to expect.

    NOT the peeled commit `tag_releases` reports. A lightweight tag and its commit are the
    same sha; an annotated one — which `backfill` and `release.py run` both write — is not,
    and a
    `--force-with-lease` quoting the commit against an annotated tag is refused. Refused
    safely, but the printed remedy is somebody's starting point and it should work.
    """
    return _git(repo, "rev-parse", f"refs/tags/{rs.fmt(r)}").strip()


def cmd_check(args: argparse.Namespace) -> int:
    """Reconcile the tags against the CHANGELOG. One condition per line, never one verdict.

    The doctor discipline this repo settled in #255 and #163: folding several answers into
    one number leaves a reader unable to tell a clean repo from one where a check could not
    run. So each condition reports for itself, and "could not be checked" is its own exit
    code rather than a lesser version of "clean".

    A RESERVED tag is not a finding. It points at a branch commit, and until that branch
    merges the tag is correctly not on `--ref` — that is the state of every release in
    flight. It is listed, because an abandoned one means a skipped number and somebody
    should be able to see why v2.96 has no entry.

    An ORPHANED one is (#406), and telling the two apart is `reconcile`'s subject. The
    remedy is deliberately a sentence rather than a command this tool runs: nothing here
    moves a tag, and a release tag that has to move is a person's call.
    """
    repo = Path(args.repo).resolve()
    r = reconcile(repo, args.ref)
    untagged, misplaced = r["untagged"], r["misplaced"]
    orphaned, reserved = r["orphaned"], r["reserved"]
    declared, have = r["declared"], r["tags"]
    sound = len(have) - len(misplaced)

    lines = [
        f"{len(declared) - len(untagged)}/{len(declared)} release(s) at {args.ref} have a tag",
        f"{sound}/{len(have)} tag(s) point at a commit that declares them",
        f"{len(orphaned)} landed release(s) whose tag is not on {args.ref}",
        f"{len(reserved)} tag(s) not on {args.ref} (cut locally and unpushed, or a "
        "pre-#122 reservation)",
    ]

    if args.json:
        print(json.dumps({
            "clean": not r["findings"],
            "ref": args.ref,
            "untagged": [rs.fmt(x) for x in untagged],
            "misplaced": {rs.fmt(x): sha for x, sha in misplaced},
            "orphaned": {rs.fmt(x): sha for x, sha in orphaned},
            "reserved": {rs.fmt(x): sha for x, sha in reserved},
        }, indent=2))
        return 2 if r["findings"] else 0

    for line in lines:
        print(line)
    for x, sha in reserved:
        print(f"  {rs.fmt(x)} tagged at {sha[:12]}, not reachable from {args.ref}")
    if untagged:
        print(f"\nSTOP: no tag for {', '.join(rs.fmt(x) for x in untagged)}. That number is "
              "not locked, so nothing stops it being issued twice.\n"
              f"    scripts/release_tag.py backfill --ref {args.ref} --push",
              file=sys.stderr)
    if orphaned:
        # Where each of them actually landed, computed only when there is an orphan to
        # repair: `landings` walks every commit that ever touched the CHANGELOG, which is a
        # hundred blob reads on this repo and nothing at all to a clean run.
        where = landings(repo, r["ref_sha"])
        for x, sha in orphaned:
            landed = where.get(x)
            print(f"STOP: {rs.fmt(x)} is tagged at {sha[:12]}, which is NOT on {args.ref} — "
                  f"but {args.ref} declares {rs.fmt(x)}, so it shipped. A squash or rebase "
                  "merge of a branch-side release commit discards the commit the tag named "
                  "and leaves the tag addressing history nobody can reach.\n"
                  + (f"    It landed at {landed[:12]}. Re-point it deliberately, with a "
                     f"lease so it is atomic:\n"
                     f"        git tag -f {rs.fmt(x)} {landed[:12]}\n"
                     f"        git push --force-with-lease={rs.fmt(x)}:{_ref_oid(repo, x)} "
                     f"origin refs/tags/{rs.fmt(x)}\n"
                     if landed else
                     f"    Nothing on {args.ref}'s first-parent line declares it, so where "
                     "it landed is a person's question.\n")
                  + "    Then stop the next one: allow merge commits only "
                    "(qb-doctor's `merges` row).", file=sys.stderr)
    if misplaced:
        for x, sha in misplaced:
            print(f"STOP: {rs.fmt(x)} is tagged at {sha[:12]}, whose {CHANGELOG} does not "
                  "declare it. Tags are not moved by this tool — a human decides whether "
                  "that tag or that entry is the wrong one.", file=sys.stderr)
    if r["findings"]:
        return 2
    print("clean: every release is tagged, every tag names a release that commit carries, "
          f"and every landed release's tag is on {args.ref}.")
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

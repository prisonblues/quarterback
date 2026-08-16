"""A release number is one fact written in several files, and nothing checked they agree.

#46's smaller half. Two branches once claimed v2.14 at the same time — each correctly,
from what it could see, since `main` was on v2.13 when both forked — and the rename cost
a pass across `CHANGELOG.md`, `README.md`'s release list, `pyproject.toml`, `app/main.py`,
a migration docstring, two module docstrings and a test filename. The docstrings and the
filename were missed on the first pass and caught by a reviewer, because nothing ties them
together. On 2026-08-15 it happened four more times in one day, and a `pyproject.toml` at
2.15.0 shipped for several commits beside an `app/main.py` at 2.14.0.

Allocation (a board that hands out the next free number) is the issue's larger half and
needs a board. This needs nothing, and it catches the class: a number that disagrees with
itself, a heading claimed twice, a new release whose entry was written but never listed.

**Scope, stated because the paragraph above names more than this enforces.** The four sites
asserted here are `pyproject.toml`, `app/main.py`, `CHANGELOG.md` and `README.md` — the ones
that decide what a running instance reports and what a reader is told is current. Docstrings
naming the release that added a feature (`migrations/versions/0015_*.py`, `app/api/reviews.py`)
are NOT asserted and can still drift: they are prose about history, so a check would have to
encode "does this sentence still describe the past correctly". Narrow and honest beats broad
and noisy — this suite's whole argument is that a check which fires on a correctly-updated
repo gets switched off.

**Two things changed under this file's feet, and it now asserts both.**

*A branch no longer writes a number at all* (#122). It writes the `vNEXT` placeholder and
`scripts/release_stamp.py` resolves it against the ref being merged into, so a repo in this
suite's sights is legitimately in one of two states: released, or carrying exactly one
unstamped entry. Both are asserted, and the placeholder assertions are here rather than
delegated to the stamper for the same reason the version regexes are duplicated below — this
suite checks the FILES, and a check that ran the tool would agree with the tool by
construction rather than agree with the repo.

*The filename rule flipped.* An earlier version of this docstring argued that a
`tests/test_vNNN.py` rule "would be wrong outright, since a harness-side release ships no app
test file at all" — which is true of the rule it was rejecting (every release must have one)
and not of the rule now enforced (no test file may be NAMED after one). That distinction cost
something to learn: two branches taking the same number both add `tests/test_v234.py`, and
two branches adding the same path with different contents is not a conflict git can resolve
by keeping both sides. Test files are named after their subject now, and
`test_no_test_file_is_named_after_a_release` keeps them that way.

**Why it lives under `harness/` rather than in `tests/`.** It reads four text files and needs
nothing else, but `tests/conftest.py` resolves `DATABASE_URL`, imports the app and can raise
`pytest.UsageError` at collection when a worktree would rebuild another checkout's database.
That made the cheapest check in the repo the hardest to run — needing `docker compose up -d
postgres`, and impossible in an isolation-flagged worktree, which is exactly the release-day
situation it exists for. CI discovers every `harness/**/tests` directory, so it still runs on
every push.

**What is deliberately NOT asserted: that the served version matches the newest release.**
It routinely does not, and correctly — v2.16, v2.17, v2.18, v2.20 and v2.21 are all
harness-side and each left the served version where it was. That looks like five missed
bumps and is none, which is exactly why a check that got this wrong would be worse than no
check: it would be loud, wrong every second release, and switched off within a week. What
IS asserted is the direction — the served version may lag the newest release, never lead
it, because nothing but a mistake gets a board release into `app/main.py` before its entry
exists.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Releases are named with two components (`v2.20`); the packaged and served versions carry
#: three (`2.20.0`). So they are compared at the grain the CHANGELOG actually uses, which
#: also means a hypothetical patch release does not have to invent a heading of its own.
Release = tuple[int, int]


def _release(text: str) -> Release:
    """`v2.20` -> (2, 20). The earliest two releases are spelled `v1` and `v2` with no
    minor at all, which is why the minor defaults rather than being unpacked."""
    major, _, minor = text.lstrip("v").partition(".")
    return int(major), int(minor.split(".")[0] or 0)


def _fmt(r: Release) -> str:
    """Spelled the way the files spell it: `v3`, not `v3.0`.

    A major-only release is not hypothetical — `## v2` and `## v1` are in the file, and
    both CHANGELOG and README already announce the next one as **v3**. A formatter that
    rendered `v3.0` would put a string nobody will ever write into the failure messages
    of the one test whose whole subject is that a number is written consistently."""
    return f"v{r[0]}" if r[1] == 0 else f"v{r[0]}.{r[1]}"


#: `v2.20`, `v3` — a minor is optional, because this repo spells its major-only releases
#: without one. Both README patterns share it with the CHANGELOG's, so v3 cannot fail
#: two assertions on a correctly-updated repo.
_V = r"v\d+(?:\.\d+)?"

#: The token a branch writes instead of a number, until `scripts/release_stamp.py` resolves
#: it. Spelled here rather than imported from the script: see the docstring.
PLACEHOLDER = "vNEXT"


@pytest.fixture(scope="module")
def changelog_text() -> str:
    return (REPO_ROOT / "CHANGELOG.md").read_text()


@pytest.fixture(scope="module")
def readme_text() -> str:
    return (REPO_ROOT / "README.md").read_text()


@pytest.fixture(scope="module")
def changelog_releases(changelog_text) -> list[Release]:
    """Every `## vX[.Y]` heading, in the order the file lists them (newest first).

    The `## vNEXT` heading is deliberately not in here. Every test below reads position 0 as
    "the newest release that exists", and an unstamped entry is the one thing that is not one
    — folding it in would make `served <= newest` pass against a release nothing has shipped.
    """
    found = re.findall(rf"^## ({_V})", changelog_text, flags=re.MULTILINE)
    assert found, "CHANGELOG.md has no release headings — the parser is wrong, not the file"
    return [_release(v) for v in found]


@pytest.fixture(scope="module")
def packaged_version() -> str:
    """The RAW string, not a (major, minor): `pyproject.toml` and `app/main.py` are the
    two places carrying a full three-component version, and truncating them to the
    CHANGELOG's grain is what would hide a patch-level disagreement (see below)."""
    return str(tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"])


@pytest.fixture(scope="module")
def served_version() -> str:
    """The version `GET /openapi.json` reports, read out of the source rather than by
    importing the app: the assertion is that these two FILES agree, and an import would
    resolve the same value through whichever one happened to win.

    Anchored on the `app = FastAPI(` assignment AND bounded to that call. Two earlier
    versions of this pattern each failed in a different direction, and the second was
    worse than the first:

      `FastAPI\\([^)]*version=` stopped at the first close-paren, so the ordinary
      `FastAPI(title=…, lifespan=make_lifespan(), version=…)` broke it and four tests
      errored with "the parser is wrong, not the file" on a repo whose numbers agree.

      `^app = FastAPI\\(.*?version=` with DOTALL fixed that by never stopping at all: the
      lazy `.*?` walks past the call's closing paren and through the rest of the file, so
      the day the version stops being an inline literal here it does NOT fail loudly — it
      latches onto the next `version="X.Y.Z"` anywhere below (a router kwarg, a schema
      constant, a docstring example) and every dependent test then asserts about a string
      unrelated to what the app serves. If that stray literal happens to match pyproject's,
      the whole suite passes green while OpenAPI reports something else.

    So the match is bounded to the call's own parentheses, tolerating one level of nesting
    (`[^()]` or a balanced `\\([^()]*\\)`) and needing no DOTALL. A version that moves out of
    the call now fails the assert, which is the honest outcome: this fixture is coupled to
    an inline literal, and it should say so rather than quietly find another one."""
    text = (REPO_ROOT / "app" / "main.py").read_text()
    m = re.search(r"^app\s*=\s*FastAPI\((?:[^()]|\([^()]*\))*?version=\"(\d+\.\d+\.\d+)\"",
                  text, flags=re.MULTILINE)
    assert m, ("app/main.py has no `app = FastAPI(… version=\"X.Y.Z\" …)` — the parser is "
               "wrong, or the version stopped being an inline literal in that call")
    return m.group(1)


def test_the_packaged_and_served_versions_agree(packaged_version, served_version):
    """The one that actually shipped broken. These two have no reason to ever differ:
    `pyproject.toml`'s own comment says "keep the two in step", which is a convention
    with nothing enforcing it.

    Compared as raw strings. Neither is a CHANGELOG heading, so neither needs the
    CHANGELOG's two-component grain — and applying it here would let `2.19.1` beside
    `2.19.0` pass green while the package and `GET /openapi.json` serve different
    versions, which is this test's own subject escaping through its own comparison."""
    assert packaged_version == served_version, (
        f"pyproject.toml says {packaged_version} and app/main.py serves "
        f"{served_version} — a release bumped one and not the other")


def test_no_release_number_is_claimed_twice(changelog_releases):
    """The collision itself, as it looks once it is in the file.

    Two branches taking the same number surfaced as a merge conflict in CHANGELOG.md,
    which is the LUCKY case — git caught it. Two branches whose entries land without
    conflicting, or a merge that keeps both (this repo has had one: `stderr_gist` ended
    up defined twice in harness_rules.py and the second silently won), produce a repo
    where a version number means two things and nothing says so."""
    seen, dupes = set(), []
    for r in changelog_releases:
        if r in seen:
            dupes.append(_fmt(r))
        seen.add(r)
    assert not dupes, f"CHANGELOG.md claims {', '.join(dupes)} more than once"


def test_the_changelog_is_newest_first(changelog_releases):
    """The file says so in its own header, and the tests below read position 0 as "the
    newest release". An entry inserted in the wrong place would quietly make every one
    of them assert something else."""
    assert changelog_releases == sorted(changelog_releases, reverse=True), (
        "CHANGELOG.md headings are not in descending order: "
        + ", ".join(_fmt(r) for r in changelog_releases[:6]))


def test_the_served_version_is_a_release_that_exists(served_version, changelog_releases):
    """At the CHANGELOG's grain, which is the right one here: `2.19.0` is the release
    written up as `## v2.19`, and a patch release would not get a heading of its own."""
    assert _release(served_version) in changelog_releases, (
        f"app/main.py serves {served_version}, which has no CHANGELOG entry")


def test_the_served_version_never_leads_the_newest_release(served_version,
                                                           changelog_releases):
    """It may lag — most releases are harness-side and leave it alone. Leading is the
    error: a board change bumped the version and its entry was never written."""
    newest = changelog_releases[0]
    assert _release(served_version) <= newest, (
        f"app/main.py serves {served_version} but the newest release is "
        f"{_fmt(newest)} — a board version was bumped ahead of its CHANGELOG entry")


#: Two assertions used to live here and no longer can, because the prose they read is
#: deliberately gone (#122). "Latest release: **vX** — …" restated the previous four
#: releases in fresh prose every time, which is why merging two branches meant WRITING a
#: paragraph rather than keeping both sides of one; and "(Anything built off this branch
#: says X.Y.Z)" was a fourth copy of a version the README's own argument says to read from
#: `GET /openapi.json`. Deleting the copies is what removes the drift — a test that
#: asserted them accurately was still a test whose subject should not have existed.


def test_at_most_one_release_is_unstamped(changelog_text):
    """A branch ships one release, so there is at most one placeholder.

    Two `## vNEXT` headings cannot both become one number, and choosing between them is
    exactly the judgement nothing here is entitled to make. It is also the shape a bad merge
    leaves behind: two branches whose entries were kept side by side, which under the old
    convention would have been two different numbers and is now visibly one question."""
    found = re.findall(rf"^## {PLACEHOLDER}\b.*$", changelog_text, flags=re.MULTILINE)
    assert len(found) <= 1, (
        f"CHANGELOG.md has {len(found)} unstamped entries: "
        + " | ".join(f.strip() for f in found))


def test_an_unstamped_entry_is_at_the_top(changelog_text, changelog_releases):
    """The file is newest first and an unreleased entry is newer than everything in it.

    Stamped where it sits below a numbered heading, it would be a number out of order in a
    file every other test here reads by position — and `test_the_changelog_is_newest_first`
    would then fail on the release AFTER the mistake, pointing at the wrong commit."""
    placeholder = re.search(rf"^## {PLACEHOLDER}\b", changelog_text, flags=re.MULTILINE)
    if not placeholder:
        pytest.skip("nothing unstamped — this branch is not writing a release")
    first_numbered = re.search(rf"^## {_V}\b", changelog_text, flags=re.MULTILINE)
    assert first_numbered and placeholder.start() < first_numbered.start(), (
        f"CHANGELOG.md's `## {PLACEHOLDER}` entry sits below "
        f"{_fmt(changelog_releases[0])} — an unreleased entry belongs above every released one")


def test_the_changelog_and_the_readme_are_unstamped_together(changelog_text, readme_text):
    """Half a release entry is the failure this catches: a README bullet stamped with a
    number whose CHANGELOG section does not exist, or an entry written up and never listed.

    Both directions, because they fail on different days. The bullet without the entry is
    the release-day mistake; the entry without the bullet is the one the old convention
    made every time, since the bullet lived under a paragraph that had to be rewritten by
    hand and the hand sometimes stopped there."""
    in_changelog = bool(re.search(rf"^## {PLACEHOLDER}\b", changelog_text, flags=re.MULTILINE))
    in_readme = bool(re.search(rf"^- \*\*{PLACEHOLDER}\*\* —", readme_text, flags=re.MULTILINE))
    assert in_changelog == in_readme, (
        f"CHANGELOG.md {'has' if in_changelog else 'has no'} `## {PLACEHOLDER}` entry but "
        f"README.md {'has' if in_readme else 'has no'} `- **{PLACEHOLDER}** —` bullet")


def test_no_test_file_is_named_after_a_release():
    """`tests/test_v234.py` is a release number a branch had to guess before landing, and
    two branches guessing the same one add the same PATH with different contents — which
    git does not conflict on the way it conflicts on a heading, and cannot resolve by
    keeping both sides. Whichever lands second clobbers the other's suite or presents a
    reviewer with a choice between two unrelated files that happen to share a name.

    Every suite in the repo, not just `tests/` — the harness ones are written by the same
    hands on the same day and drift the same way."""
    named = sorted(
        str(p.relative_to(REPO_ROOT))
        for p in REPO_ROOT.rglob("test_v[0-9]*.py")
        if ".venv" not in p.parts and "node_modules" not in p.parts)
    assert not named, (
        "test files are named after their subject, not the release that shipped them: "
        + ", ".join(named))


def test_the_readme_release_list_has_an_entry_for_the_newest_release(changelog_releases):
    """The bulleted history below the prose, which is a second place and drifts
    independently of the first.

    Only the newest is required to have a bullet of its own. Older releases are
    deliberately collapsed into range entries as the list ages — `- **v2.2–v2.5** — the
    session registry…`, `- **v1–v2.1** —` — so asserting a bullet per release would fail
    on a README that is doing exactly what it is supposed to. The docstring's claim is
    scoped to match: what this catches is a release written up and never listed, which
    is the mistake that happens on the day of the release."""
    newest = _fmt(changelog_releases[0])
    text = (REPO_ROOT / "README.md").read_text()
    assert re.search(rf"^- \*\*{re.escape(newest)}\*\* —", text, flags=re.MULTILINE), (
        f"README.md's release list has no `- **{newest}** —` entry")

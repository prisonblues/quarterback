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

REPO_ROOT = Path(__file__).resolve().parent.parent

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


@pytest.fixture(scope="module")
def changelog_releases() -> list[Release]:
    """Every `## vX[.Y]` heading, in the order the file lists them (newest first)."""
    text = (REPO_ROOT / "CHANGELOG.md").read_text()
    found = re.findall(rf"^## ({_V})", text, flags=re.MULTILINE)
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

    Anchored on the `app = FastAPI(` assignment across newlines rather than scanning for
    the first `FastAPI(` with `[^)]*`. That earlier pattern broke on a close-paren inside
    the call — `FastAPI(title=…, lifespan=make_lifespan(), version=…)` is the ordinary
    next edit for an app that already has a module-level engine — and would have failed
    four tests with "the parser is wrong, not the file" on a repo whose numbers agree."""
    text = (REPO_ROOT / "app" / "main.py").read_text()
    m = re.search(r"^app\s*=\s*FastAPI\(.*?version=\"(\d+\.\d+\.\d+)\"",
                  text, flags=re.MULTILINE | re.DOTALL)
    assert m, ("app/main.py has no `app = FastAPI(… version=\"X.Y.Z\" …)` — the parser is "
               "wrong, or the version stopped being an inline literal")
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


def test_the_readme_names_the_newest_release_as_the_latest(changelog_releases):
    """README's prose is one of the eight places, and the one most often left behind —
    it was left behind by the very commit that added this test's sibling."""
    newest = changelog_releases[0]
    text = (REPO_ROOT / "README.md").read_text()
    m = re.search(rf"Latest release: \*\*({_V})\*\*", text)
    assert m, "README.md has no 'Latest release: **vX[.Y]**' line"
    assert _release(m.group(1)) == newest, (
        f"README.md calls {m.group(1)} the latest release; the CHANGELOG's newest is "
        f"{_fmt(newest)}")


def test_the_readme_prose_names_the_version_this_branch_serves(served_version):
    """A THIRD copy of the full version sits in README's deploy paragraph — "(Anything
    built off this branch says 2.19.0 …)". It drifts like the others and is likelier to
    be missed, being parenthetical prose rather than a labelled field: a board bump can
    leave it stale while the latest-release line and the bullet are both updated, and
    every other assertion here still passes."""
    text = (REPO_ROOT / "README.md").read_text()
    m = re.search(r"says (\d+\.\d+\.\d+)", text)
    assert m, "README.md's deploy paragraph no longer says what this branch serves"
    assert m.group(1) == served_version, (
        f"README.md says this branch serves {m.group(1)}; app/main.py serves "
        f"{served_version}")


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

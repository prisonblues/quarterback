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

import ast
import re
import subprocess
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


#: Ends a version, and does it with `(?![\w.])` rather than `\b` for the reason the stamper's
#: own `_END` gives: `\b` fires between a digit and a dot, so `## v2.33.1` would be read here
#: as release v2.33 — truncating a three-component heading to two and putting a number into
#: the ordering and duplicate checks that nobody wrote. The stamper refuses such a heading as
#: unparseable, and this suite exists to agree with the stamper about what a release heading
#: is, so a heading it will not number must not match here either.
_END = r"(?![\w.])"

#: `v2.20`, `v3` — a minor is optional, because this repo spells its major-only releases
#: without one. Both README patterns share it with the CHANGELOG's, so v3 cannot fail
#: two assertions on a correctly-updated repo.
_V = rf"v\d+(?:\.\d+)?{_END}"

#: The token a branch writes instead of a number, until `scripts/release_stamp.py` resolves
#: it. Spelled here rather than imported from the script: see the docstring.
PLACEHOLDER = "vNEXT"


#: A fence line: three or more backticks or tildes, then whatever info string follows. Both
#: groups are load-bearing — see the closer rule in `_without_fenced_blocks` — and the
#: indentation is unbounded rather than CommonMark's three spaces, because this repo's command
#: docs fence blocks inside numbered list items where they are legitimately indented further.
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*(.*)$")


def _without_fenced_blocks(text: str) -> str:
    """Blank out fenced code blocks, keeping the text exactly as long as it was.

    A fenced block is documentation OF this convention rather than a use of it: a ```` ```md ````
    example showing `## vNEXT — <title>` at column 0 is how the README teaches a branch what to
    write, and every `^##` pattern below would read it as a real unstamped entry. The stamper
    masks fences before it matches, so `check` would be quietly right while this suite went red
    on a repo doing exactly what it was told.

    The logic is duplicated from `scripts/release_stamp.py` rather than imported, for the reason
    the module docstring gives at length about the version regexes: this suite checks the FILES,
    and a check that borrowed the tool's matching would agree with the tool by construction
    instead of agreeing with the repo. The duplication is the point.

    Inline code spans are NOT handled here and do not need to be. Every pattern in this file is
    anchored at line start on `##` or `- **`, and an inline span opens with a backtick, so no
    span can ever contain the beginning of one of these matches.

    Length is preserved (blanked to NUL, which no pattern here can match) so that offsets taken
    from the masked text — `test_an_unstamped_entry_is_at_the_top` compares two of them — still
    describe positions in the file a reader would open.
    """
    out = list(text)
    pos, fence, opened_at = 0, None, 0
    for line_no, line in enumerate(text.split("\n"), start=1):
        m = _FENCE.match(line)
        marker, info = (m.group(1), m.group(2)) if m else (None, "")
        inside = fence is not None
        if fence is None:
            if marker:
                fence, opened_at = marker, line_no
        elif marker and marker[0] == fence[0] and len(marker) >= len(fence) and not info:
            # A closing fence is the same character, AT LEAST as long as the opener, and
            # carries no info string. All three conditions are CommonMark's, all three are the
            # stamper's, and each matters here: the README fences a ```` ```md ```` sample
            # containing its own ``` lines, so a length-blind close would end the block on the
            # first of them, and an info-blind close would end an outer ``` block on an inner
            # ```py — either way the rest of the sample reads as prose and a `## vNEXT` in it
            # is counted as a real unstamped entry that the stamper, masking correctly, ignores.
            fence = None
        if inside or marker:  # the fence lines themselves are blanked too
            out[pos:pos + len(line)] = "\0" * len(line)
        pos += len(line) + 1
    # A fence left open blanks every remaining line, which would make a real unstamped entry
    # below it invisible to every assertion here at once — the same silent hole the stamper
    # refuses on, and the reason to refuse rather than mask a best effort. Loud, and it names
    # the line the fence was opened on, because "your CHANGELOG is fine and this suite went
    # red" is not a failure anybody can act on, and neither is a marker that occurs forty times.
    assert fence is None, (
        f"a `{fence}` code fence is opened at line {opened_at} and never closed, so everything "
        "below it reads as code — including any release heading. Close the fence")
    return "".join(out)


# Every `read_text` below spells its encoding out. The files this suite reads are prose — em
# dashes, ellipses, the occasional accented name — and `read_text()` with no encoding takes the
# platform default, which under a C/POSIX locale is ASCII. That is not a hypothetical shell: a
# nix build, a minimal CI image and a cron job all commonly run without a UTF-8 locale, and the
# failure there is a `UnicodeDecodeError` at collection time rather than anything about releases.


@pytest.fixture(scope="module")
def changelog_text() -> str:
    return (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme_text() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def changelog_prose(changelog_text) -> str:
    """CHANGELOG.md with its fenced examples blanked — what every heading pattern reads."""
    return _without_fenced_blocks(changelog_text)


@pytest.fixture(scope="module")
def readme_prose(readme_text) -> str:
    """README.md with its fenced examples blanked — what every bullet pattern reads."""
    return _without_fenced_blocks(readme_text)


@pytest.fixture(scope="module")
def changelog_releases(changelog_prose) -> list[Release]:
    """Every `## vX[.Y]` heading, in the order the file lists them (newest first).

    Read from the fence-masked text: a sample release entry inside a fenced block is not a
    release, and parsing one as though it were would put an invented number into the ordering
    check and into `served <= newest`.

    The `## vNEXT` heading is deliberately not in here. Every test below reads position 0 as
    "the newest release that exists", and an unstamped entry is the one thing that is not one
    — folding it in would make `served <= newest` pass against a release nothing has shipped.
    """
    found = re.findall(rf"^## ({_V})", changelog_prose, flags=re.MULTILINE)
    assert found, "CHANGELOG.md has no release headings — the parser is wrong, not the file"
    return [_release(v) for v in found]


@pytest.fixture(scope="module")
def packaged_version() -> str:
    """The RAW string, not a (major, minor): `pyproject.toml` and `app/main.py` are the
    two places carrying a full three-component version, and truncating them to the
    CHANGELOG's grain is what would hide a patch-level disagreement (see below)."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return str(tomllib.loads(text)["project"]["version"])


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
    text = (REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")
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


def test_at_most_one_release_is_unstamped(changelog_prose):
    """A branch ships one release, so there is at most one placeholder.

    Two `## vNEXT` headings cannot both become one number, and choosing between them is
    exactly the judgement nothing here is entitled to make. It is also the shape a bad merge
    leaves behind: two branches whose entries were kept side by side, which under the old
    convention would have been two different numbers and is now visibly one question."""
    found = re.findall(rf"^## {PLACEHOLDER}\b.*$", changelog_prose, flags=re.MULTILINE)
    assert len(found) <= 1, (
        f"CHANGELOG.md has {len(found)} unstamped entries: "
        + " | ".join(f.strip() for f in found))


def test_an_unstamped_entry_is_at_the_top(changelog_prose, changelog_releases):
    """The file is newest first and an unreleased entry is newer than everything in it.

    Stamped where it sits below a numbered heading, it would be a number out of order in a
    file every other test here reads by position — and `test_the_changelog_is_newest_first`
    would then fail on the release AFTER the mistake, pointing at the wrong commit."""
    placeholder = re.search(rf"^## {PLACEHOLDER}\b", changelog_prose, flags=re.MULTILINE)
    if not placeholder:
        pytest.skip("nothing unstamped — this branch is not writing a release")
    first_numbered = re.search(rf"^## {_V}", changelog_prose, flags=re.MULTILINE)
    assert first_numbered and placeholder.start() < first_numbered.start(), (
        f"CHANGELOG.md's `## {PLACEHOLDER}` entry sits below "
        f"{_fmt(changelog_releases[0])} — an unreleased entry belongs above every released one")


def test_the_changelog_and_the_readme_are_unstamped_together(changelog_prose, readme_prose):
    """Half a release entry is the failure this catches: a README bullet stamped with a
    number whose CHANGELOG section does not exist, or an entry written up and never listed.

    Both directions, because they fail on different days. The bullet without the entry is
    the release-day mistake; the entry without the bullet is the one the old convention
    made every time, since the bullet lived under a paragraph that had to be rewritten by
    hand and the hand sometimes stopped there."""
    in_changelog = bool(re.search(rf"^## {PLACEHOLDER}\b", changelog_prose, flags=re.MULTILINE))
    # No punctuation after the bold run. The stamper's `_SITE` rewrites `**vNEXT` followed by
    # anything, so `- **vNEXT**: …` is a bullet it stamps; requiring the em dash here would
    # fail that branch with a message swearing the bullet does not exist while it sits in the
    # file, correctly written in a punctuation style this test happened not to have seen.
    in_readme = bool(re.search(rf"^- \*\*{PLACEHOLDER}\*\*", readme_prose, flags=re.MULTILINE))
    assert in_changelog == in_readme, (
        f"CHANGELOG.md {'has' if in_changelog else 'has no'} `## {PLACEHOLDER}` entry but "
        f"README.md {'has' if in_readme else 'has no'} `- **{PLACEHOLDER}**` bullet")


def test_no_test_file_is_named_after_a_release():
    """`tests/test_v234.py` is a release number a branch had to guess before landing, and
    two branches guessing the same one add the same PATH with different contents — which
    git does not conflict on the way it conflicts on a heading, and cannot resolve by
    keeping both sides. Whichever lands second clobbers the other's suite or presents a
    reviewer with a choice between two unrelated files that happen to share a name.

    Every suite in the repo, not just `tests/` — the harness ones are written by the same
    hands on the same day and drift the same way.

    Asked of git rather than of the filesystem. Only a TRACKED file can be named badly by this
    repo; anything untracked came from somewhere else and is not this repo's to rename. Walking
    the tree instead meant maintaining a deny-list of directories to ignore, which was already
    wrong the day it was written — it knew `.venv` and `node_modules` and not `venv`, `env`,
    `.tox`, `.nox`, `build`, `site-packages` or `.git`, so a developer whose virtualenv is named
    the other way got a red suite from a vendored file they do not control and cannot fix. "Is
    it committed here" is the question that was actually being asked, and git answers it without
    a list that grows every time somebody's setup differs."""
    try:
        proc = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
                              capture_output=True, text=True, check=False)
    except OSError:
        pytest.skip("git is not on PATH, so there is no set of tracked files to check")
    if proc.returncode != 0:
        pytest.skip("not a git checkout (an export or a tarball), so nothing here is tracked")
    named = sorted(p for p in proc.stdout.split("\0")
                   if re.fullmatch(r"test_v\d[^/]*\.py", p.rpartition("/")[2]))
    assert not named, (
        "test files are named after their subject, not the release that shipped them: "
        + ", ".join(named))


def _readme_bullet(release_name: str) -> re.Pattern[str]:
    """The README's own list entry for one release, in whatever punctuation style it is
    written: `- **v2.33** — …`, `- **v2.33**: …`, `- **v2.33**`.

    The closing `**` right after the number is still required, and is what keeps this
    specific: the list's collapsed range entries and its `- **v3 (next)** —` do not match,
    and neither does a three-component `- **v2.33.1** —`, so a README listing something
    adjacent to the newest release cannot satisfy the assertion instead of it."""
    return re.compile(rf"^- \*\*{re.escape(release_name)}\*\*", re.MULTILINE)


def test_the_readme_release_list_has_an_entry_for_the_newest_release(changelog_releases,
                                                                     readme_prose):
    """The bulleted history below the prose, which is a second place and drifts
    independently of the first.

    Only the newest is required to have a bullet of its own. Older releases are
    deliberately collapsed into range entries as the list ages — `- **v2.2–v2.5** — the
    session registry…`, `- **v1–v2.1** —` — so asserting a bullet per release would fail
    on a README that is doing exactly what it is supposed to. The docstring's claim is
    scoped to match: what this catches is a release written up and never listed, which
    is the mistake that happens on the day of the release.

    No separator is required after the bold run, for the reason
    `test_the_changelog_and_the_readme_are_unstamped_together` gives: the stamper rewrites
    `**vNEXT` followed by anything, so `- **v2.34**: …` is a bullet it stamps and this repo
    accepts. Demanding the em dash here would have failed a correctly-listed release on its
    punctuation alone, and said the bullet did not exist while it sat in the file."""
    newest = _fmt(changelog_releases[0])
    assert _readme_bullet(newest).search(readme_prose), (
        f"README.md's release list has no `- **{newest}**` entry")


#: A release bullet and only a release bullet: `- **v2.33** — …`. Anchoring the closing `**`
#: right after the number is what keeps the list's deliberate range entries out of this — a
#: `- **v1–v2.1** —` or a `- **v3 (next)** —` simply does not match, rather than matching as
#: a bare `v1` and `v3` and inventing a duplicate out of the README's own summarising style.
_README_RELEASE_BULLET = re.compile(rf"^- \*\*({_V})\*\*", re.MULTILINE)


def test_no_release_number_appears_twice(changelog_releases, readme_prose):
    """A number that means two releases is the exact state the placeholder convention exists
    to make impossible, and it is the one state nothing else here can see.

    `release_stamp.py check` looks for the literal `vNEXT`, which is the right thing to look
    for right up until the moment a number is stamped: after that there is no placeholder left
    anywhere, so two branches that were each stamped v2.34 against the same base and then
    merged with both sides kept produce a repo that passes the guard, passes preflight, and
    documents one number twice. Git does not conflict on it either — two entries added in
    different places of a long file merge cleanly, and "keep both" is the resolution a human
    reaches for on the ones where it does conflict.

    Both files, because they carry the number independently. The CHANGELOG half restates
    `test_no_release_number_is_claimed_twice` from the merge's side rather than the parser's,
    and is kept because a failure here says what happened; the README half is not asserted
    anywhere else at all."""

    def dupes(numbers: list[Release]) -> list[str]:
        seen, twice = set(), []
        for r in numbers:
            if r in seen and _fmt(r) not in twice:
                twice.append(_fmt(r))
            seen.add(r)
        return twice

    in_changelog = dupes(changelog_releases)
    in_readme = dupes(
        [_release(m.group(1)) for m in _README_RELEASE_BULLET.finditer(readme_prose)])
    assert not in_changelog and not in_readme, (
        "a release number is used twice — two branches were stamped the same number and the "
        "merge kept both sides, so the number now means two releases: "
        + "; ".join(filter(None, [
            f"CHANGELOG.md headings for {', '.join(in_changelog)}" if in_changelog else "",
            f"README.md bullets for {', '.join(in_readme)}" if in_readme else ""])))


# --------------------------------------------------------------- the helpers themselves
#
# Everything above reads the real CHANGELOG.md and README.md, which is this suite's whole
# argument and also its blind spot: a helper that stops matching what the stamper matches
# stays green for as long as the repo happens to be in a shape the loosened helper still
# accepts, and goes wrong on the one day a release is written in the shape it lost. Each
# test below pins a rule where this file and `scripts/release_stamp.py` had already drifted
# apart, on text written here rather than on whatever the repo currently contains.


def test_a_three_component_heading_is_not_read_as_a_release():
    """`## v2.33.1` is a heading the stamper refuses to number. Parsed here as v2.33 it would
    be a release nobody wrote — duplicating the real v2.33 in the collision checks, or landing
    out of order in the newest-first check."""
    text = "## v2.33.1 — a patch\n\n## v2.33 — the release\n"
    assert re.findall(rf"^## ({_V})", text, flags=re.MULTILINE) == ["v2.33"]
    assert not _README_RELEASE_BULLET.findall("- **v2.33.1** — a patch\n")
    assert _README_RELEASE_BULLET.findall("- **v2.33** — the release\n") == ["v2.33"]


def test_a_release_bullet_is_found_whatever_punctuation_follows_it():
    """The em dash is this README's house style and not a rule the stamper enforces."""
    for line in ("- **v2.34** — the release\n", "- **v2.34**: the release\n",
                 "- **v2.34** - the release\n", "- **v2.34**\n"):
        assert _readme_bullet("v2.34").search(line), line
    for line in ("- **v2.34.1** — a patch\n", "- **v2.34 (next)** — not this release\n",
                 "- **v2.340** — a later release\n"):
        assert not _readme_bullet("v2.34").search(line), line


def test_an_inner_info_fence_does_not_close_an_outer_block():
    """A closer carries no info string, which is CommonMark's rule and `mask_code`'s. Without
    it a ```` ```py ```` inside a ```` ``` ```` sample ends the block early, and this suite
    reads the rest of the sample as prose while the stamper reads it as code."""
    text = ("```\n"
            "## vNEXT — a sample entry\n"
            "```py\n"
            "## v9.9 — still inside the sample\n"
            "```\n"
            "\n"
            "## v9.8 — real prose below the block\n")
    masked = _without_fenced_blocks(text)
    assert len(masked) == len(text), "offsets into the masked text must still find the file"
    assert PLACEHOLDER not in masked and "v9.9" not in masked
    assert "## v9.8" in masked


def test_an_unclosed_fence_says_which_line_opened_it():
    """The marker alone does not locate anything in a file with forty fences in it."""
    with pytest.raises(AssertionError, match="line 3"):
        _without_fenced_blocks("intro\n\n```md\n## vNEXT — a sample entry\n")


#: A `cp ${./some/path}` inside the flake's release-metadata check, anchored to the `cp` that
#: makes it a copy. The anchor is the whole point: `${./x}` on its own is Nix's "put this path
#: in the store and interpolate where it landed", which a script may do to RUN a file
#: (`bash ${./scripts/foo.sh}`) or to pass one as an argument — neither of which puts the file
#: in the sandbox at the path this suite reads. Unanchored, any such reference would answer
#: "yes, that file is supplied" for a file nothing copies. Flags are allowed between the two
#: because `cp -r` and `cp --no-preserve=mode` are both things that script may grow.
_FLAKE_COPY = re.compile(r"\bcp\b(?:[ \t]+-\S+)*[ \t]+\$\{\./([^}]+)\}")

#: The line that DEFINES the check, as opposed to any of the sentences that mention it by name.
#: `flake.nix` already discusses `release-metadata-tests` in prose around the attribute, and a
#: search for the bare name takes whichever mention comes first in the file — so a comment added
#: above the definition would silently move the slice below to a block that is not this check.
#: Line-start indentation plus `= pkgs.runCommand` is the shape only a definition has.
_FLAKE_CHECK_HEAD = re.compile(r"^[ \t]*release-metadata-tests = pkgs\.runCommand\b", re.MULTILINE)

#: The end of that attribute's script, anchored to the indentation the block closes at. The
#: previous version took the first bare `'';` after the head, which the script's own text can
#: contain — an `echo "… '';"`, a comment quoting Nix syntax — and a slice truncated there
#: compares the guard below against half a cp list while looking entirely healthy.
_FLAKE_CHECK_END = re.compile(r"\n {8}'';")


def _flake_check_block(text: str) -> str:
    """The `release-metadata-tests` runCommand script, sliced out of `flake.nix`'s source.

    Both ends assert, and both say the parser broke rather than the flake did. This is a hand
    parser over another language's syntax, so the day it stops matching is a day somebody
    reshaped that attribute — and "this suite reads files the check does not copy" would be a
    confusing way to be told about it."""
    heads = list(_FLAKE_CHECK_HEAD.finditer(text))
    assert len(heads) == 1, (
        f"flake.nix has {len(heads)} lines defining `release-metadata-tests = pkgs.runCommand`, "
        "and this parser needs exactly one. If the check was renamed, moved or reshaped, update "
        "this pattern to match — the slice it takes is the only thing tying this suite to the "
        "sandbox that feeds it")
    end = _FLAKE_CHECK_END.search(text, heads[0].end())
    assert end, (
        "the release-metadata-tests check has no closing `'';` on a line of its own at the "
        "block's indentation, so this parser cannot tell where the script ends — the parser "
        "broke, not the flake. Update the delimiter pattern to however the block now closes")
    return text[heads[0].start():end.start()]


def _unreadable_join(origin: str, node: ast.AST, shape: str) -> AssertionError:
    """The refusal `_repo_root_paths_in` raises, phrased so the author can act on it."""
    return AssertionError(
        f"{origin} line {node.lineno} builds a repo-root path as {shape}, and the reader in "
        "test_release_numbers.py only understands a literal join — `REPO_ROOT / \"app\" / "
        "\"main.py\"`. It refuses rather than skipping the expression, because a read it "
        "cannot see is a file the flake's sandbox will not be given, and the test that does "
        "the reading then ERRORS there with FileNotFoundError instead of failing here, which "
        "is the #163 failure moved one level up. Write the join with string literals, or teach "
        "`_repo_root_paths_in` the shape you need")


def _joined_components(node: ast.BinOp, origin: str) -> tuple[str, ...] | None:
    """The components of a `REPO_ROOT / … / …` chain, or None if the chain is rooted elsewhere.

    `/` is division far more often than it is a path join, so a chain whose base is anything but
    `REPO_ROOT` is not this reader's business and yields nothing. One that IS rooted there and
    carries a segment which is not a string literal raises: see `_unreadable_join`."""
    operands: list[ast.expr] = []
    cur: ast.expr = node
    while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
        operands.append(cur.right)
        cur = cur.left
    if not (isinstance(cur, ast.Name) and cur.id == "REPO_ROOT"):
        return None
    components: list[str] = []
    for operand in reversed(operands):
        if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
            components.append(operand.value)
        else:
            raise _unreadable_join(origin, operand, "a segment that is not a string literal")
    return tuple(components)


def _refuse_unreadable_call(node: ast.Call, origin: str) -> None:
    """The two other ways to build a repo-root path that this reader cannot follow.

    `str(REPO_ROOT)` — the directory handed to `git ls-files` — is deliberately not one of them,
    and neither is `Path(__file__)`: the first is a call ON the name rather than a join, and the
    second does not mention `REPO_ROOT` at all."""
    func = node.func
    if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id == "REPO_ROOT"):
        raise _unreadable_join(origin, node, f"`REPO_ROOT.{func.attr}(…)`")
    if (isinstance(func, ast.Name) and func.id == "Path"
            and any(isinstance(a, ast.Name) and a.id == "REPO_ROOT" for a in node.args)):
        raise _unreadable_join(origin, node, "`Path(REPO_ROOT, …)`")


def _repo_root_paths_in(source: str, origin: str) -> set[str]:
    """Every repo-root path `source` joins onto `REPO_ROOT`, read out of its syntax tree.

    Parsed rather than grepped, and the first version of this was grepped. A pattern over the
    raw source cannot tell an expression from a sentence, and it read the path out of the
    comment that documented it — so the guard failed on a repo where nothing was wrong, which
    is the one failure this whole suite argues gets a check switched off. The tree has only
    the expressions.

    A bare `REPO_ROOT` with nothing joined onto it is not a file read: it is the directory
    handed to `git ls-files`, and it yields no chain here.

    Takes source text rather than reading the file itself so that the refusals above can be
    exercised on a snippet — a reader that fails loudly is only worth having if something
    checks that it does.

    `ast.walk` offers a two-component join twice, once whole and once as its own left operand,
    so the DIRECTORY `app` arrives here alongside the file `app/main.py`. Rather than tracking
    which nodes were somebody's left operand, every chain is collected and then any path that
    is a component-wise prefix of another is dropped. Component-wise, not string-wise: `app` is
    a string prefix of `appendix.md` and a directory prefix of nothing but `app/…`."""
    tree = ast.parse(source)
    found: set[tuple[str, ...]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            components = _joined_components(node, origin)
            if components is not None:
                found.add(components)
        elif isinstance(node, ast.Call):
            _refuse_unreadable_call(node, origin)
    return {"/".join(path) for path in found
            if not any(other[:len(path)] == path for other in found - {path})}


def _paths_this_suite_reads() -> set[str]:
    """The repo-root paths THIS file reads — what the flake's sandbox has to contain."""
    return _repo_root_paths_in(Path(__file__).read_text(encoding="utf-8"),
                               Path(__file__).name)


def test_the_flake_check_supplies_every_repo_root_file_this_suite_reads():
    """The enumeration in `flake.nix` is the thing that goes stale, so nothing relies on
    somebody remembering it.

    This suite reads files at the repo ROOT while living two directories below it, and
    `nix build .#checks.<system>.release-metadata-tests` runs it in a sandbox containing
    only the files that check names one by one. Add another file to the ones it copies and
    the sandbox does not have it: the new assertion does not fail there, it ERRORS on a
    missing file — and an ERROR line in a check somebody has to go and read is exactly how
    #163 sat unnoticed for a day with every assertion in this suite inert.

    So the coupling is asserted here, where it fails in the ordinary `pytest harness/tests`
    a developer runs before pushing, rather than in a nix build they may not run at all.

    Skipped rather than failed when `flake.nix` is absent: this file is also collected from
    a sandbox, and a check that cannot see the expression cannot judge it."""
    flake = REPO_ROOT / "flake.nix"
    if not flake.is_file():
        pytest.skip("no flake.nix beside this checkout, so there is no check to compare against")
    copied = set(_FLAKE_COPY.findall(_flake_check_block(flake.read_text(encoding="utf-8"))))

    missing = sorted(_paths_this_suite_reads() - copied)
    assert not missing, (
        "this suite reads repo-root files that flake.nix's release-metadata-tests check does "
        "not copy into its sandbox, so they will error there as FileNotFoundError rather than "
        "be asserted: " + ", ".join(missing) + ". Add a `cp ${./<path>}` for each")


def test_the_reader_finds_the_paths_it_is_meant_to_find():
    """The guard above is only worth having if its parser works, and a parser that silently
    finds NOTHING would make it pass on any flake at all.

    `app` is the other half of the same worry, from the opposite direction: the file is read as
    `REPO_ROOT / "app" / "main.py"`, whose left operand is a perfectly good chain of its own, so
    a reader that took every chain would demand the flake copy a DIRECTORY as if it were a file
    and fail on a check that is doing exactly the right thing."""
    found = _paths_this_suite_reads()
    assert {"CHANGELOG.md", "README.md", "pyproject.toml", "app/main.py"} <= found
    assert "app" not in found


#: Every way of building a repo-root path that the reader is meant to refuse rather than skip.
#: Each is a shape somebody could reasonably reach for — a loop over filenames, an f-string, a
#: constant named at the top of the file, the `joinpath` spelling — and each was silently
#: dropped by the version of the reader that broke out of its loop and moved on.
_UNREADABLE_JOINS = [
    "REPO_ROOT / name",
    'REPO_ROOT / f"{name}.md"',
    "REPO_ROOT / CHANGELOG",
    'REPO_ROOT / "app" / name',
    'REPO_ROOT.joinpath("app", "main.py")',
    'Path(REPO_ROOT, "app", "main.py")',
]


@pytest.mark.parametrize("expression", _UNREADABLE_JOINS)
def test_the_reader_refuses_a_repo_root_path_it_cannot_resolve(expression):
    """Loudly, and naming the line — the alternative is what this whole guard exists against.

    A dropped read leaves the flake assertion green while the sandbox is missing a file, so the
    suite errors in the nix build with a FileNotFoundError about a path nothing here mentions.
    Refusing costs the author who wrote the dynamic join one clear message on the line they
    wrote it; skipping costs whoever is next to run `nix build` an afternoon."""
    source = f"import os\n\n{expression}\n"
    with pytest.raises(AssertionError, match="line 3"):
        _repo_root_paths_in(source, "a snippet")


def test_the_reader_accepts_the_shapes_this_file_already_uses():
    """The refusals above are narrow on purpose. `str(REPO_ROOT)` is the directory handed to
    `git ls-files` and `Path(__file__)` is how REPO_ROOT itself is computed — both are calls
    near the name rather than joins onto it, and a guard that tripped on either would fail this
    suite on its own unmodified source."""
    source = ('REPO_ROOT = Path(__file__).resolve().parent.parent.parent\n'
              'proc = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-z"])\n'
              'text = (REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")\n')
    assert _repo_root_paths_in(source, "a snippet") == {"app/main.py"}


def test_the_reader_drops_a_directory_and_keeps_a_name_that_merely_starts_the_same():
    """The prefix rule is component-wise, and `appendix.md` is why.

    As string prefixes, `app` starts both `app/main.py` and `appendix.md`, so a `startswith`
    would drop a file the sandbox genuinely has to be given — a missing copy that the guard
    would then report as satisfied."""
    source = ('a = REPO_ROOT / "app" / "main.py"\n'
              'b = REPO_ROOT / "app"\n'
              'c = REPO_ROOT / "appendix.md"\n')
    assert _repo_root_paths_in(source, "a snippet") == {"app/main.py", "appendix.md"}


def test_the_flake_block_is_found_past_a_mention_of_its_own_name():
    """`flake.nix` talks about this check in prose beside it, and prose gets added over time.
    A first-occurrence search for the bare attribute name would slice from the comment and take
    whatever block happened to follow — comparing the guard against a different check's copies
    while looking, from the outside, exactly as healthy as a correct one."""
    text = ("{\n"
            "  # The release-metadata-tests = check is described here, in the prose that sits\n"
            "  # above it, because that is where this file explains its checks.\n"
            "  checks = {\n"
            "        release-metadata-tests = pkgs.runCommand \"release-metadata-tests\"\n"
            "          { } ''\n"
            "          cp ${./CHANGELOG.md}  repo/CHANGELOG.md\n"
            "        '';\n"
            "  };\n"
            "}\n")
    assert set(_FLAKE_COPY.findall(_flake_check_block(text))) == {"CHANGELOG.md"}


def test_a_closing_delimiter_inside_the_script_does_not_truncate_the_block():
    """`'';` is two characters a shell script may legitimately contain — this file's own check
    prints and comments on Nix syntax — and a slice that ended at the first one would compare
    against half the cp list and report the rest of the files as never copied."""
    text = ("        release-metadata-tests = pkgs.runCommand \"release-metadata-tests\"\n"
            "          { } ''\n"
            "          echo \"a nix string is closed with '';\" > /dev/null\n"
            "          cp ${./README.md}  repo/README.md\n"
            "        '';\n")
    assert set(_FLAKE_COPY.findall(_flake_check_block(text))) == {"README.md"}


def test_only_a_copy_counts_as_supplying_a_file():
    """`${./x}` is Nix putting a path in the store, which is not the same as the sandbox having
    that file at the path this suite reads. Running a script or passing a file as an argument
    both interpolate one, and neither is a `cp`."""
    block = ("          bash ${./scripts/release_stamp.py} check\n"
             "          cp   ${./CHANGELOG.md}  repo/CHANGELOG.md\n"
             "          cp -r ${./app} repo/app\n")
    assert set(_FLAKE_COPY.findall(block)) == {"CHANGELOG.md", "app"}


def _repo_root_uses_the_reader_cannot_follow(source: str) -> list[int]:
    """The lines where `source` uses `REPO_ROOT` in a shape `_repo_root_paths_in` cannot see.

    That reader refuses most of the ways of building a repo-root path it cannot resolve — the
    `.joinpath` and `Path(REPO_ROOT, …)` spellings, and any segment that is not a string
    literal — and names the line when it does. One shape gets past it: binding a second name to
    `REPO_ROOT` and joining onto that. `R = REPO_ROOT` then `R / "DEPLOY.md"` reads a repo-root
    file, and the reader sees a join onto `R`, which is not a name it is looking for. It reports
    nothing, the flake enumeration goes stale, and the sandbox errors on a file nobody copied
    in — #163 again, by the same mechanism.

    So this closes the reader's blind spot from the other side. Rather than resolving the alias
    — which is chasing the general case, and a parser that tried would fail silently at the
    first idiom it missed — it walks every load of `REPO_ROOT` itself and reports the ones that
    are not a literal join the reader would follow. The coupling asserted is not "the reader
    sees every read", which is unachievable, but "every read is written in a shape the reader
    sees", which is checkable.

    `str(REPO_ROOT)` is the exception: it is the directory handed to `git ls-files`, not a file
    read. Takes source text rather than reading the file itself for the same reason
    `_repo_root_paths_in` does — a guard that fails loudly is only worth having if something
    checks that it does."""
    tree = ast.parse(source)
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    unreadable: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or node.id != "REPO_ROOT":
            continue
        if not isinstance(node.ctx, ast.Load):
            continue  # the assignment that defines it
        parent = parents.get(id(node))
        if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and \
                parent.func.id == "str":
            continue
        cur: ast.AST = node
        joined = False
        while isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div) \
                and parent.left is cur:
            if not isinstance(parent.right, ast.Constant) or \
                    not isinstance(parent.right.value, str):
                joined = False
                break
            joined = True
            cur, parent = parent, parents.get(id(parent))
        if not joined:
            unreadable.append(node.lineno)
    return unreadable


def test_every_use_of_repo_root_in_this_file_is_one_the_reader_can_follow():
    """The guard this file relies on to keep `flake.nix` honest can only report the reads it can
    see, and an alias bound to `REPO_ROOT` is one it cannot. Nothing stops somebody writing one:
    it is a perfectly ordinary thing to do, it reads a real file, and it leaves both guards with
    nothing to say until the nix build errors on the missing copy."""
    unreadable = _repo_root_uses_the_reader_cannot_follow(
        Path(__file__).read_text(encoding="utf-8"))
    assert not unreadable, (
        "REPO_ROOT is used at line(s) " + ", ".join(str(n) for n in unreadable) + " in a shape "
        "`_repo_root_paths_in` cannot follow, so the file being read there will not reach "
        "the list checked against flake.nix. Write it as `REPO_ROOT / \"literal\"`, or teach "
        "the reader the new shape and add the file to the release-metadata-tests check")


def test_the_alias_the_reader_cannot_resolve_is_reported_by_line():
    """The gap this guard exists for, planted rather than argued about.

    `_repo_root_paths_in` returns an empty set for this source and raises nothing — it is the
    one bypass that survives its refusals — so if this guard were also silent the pair would
    pass a file that errors in the sandbox."""
    source = ('R = REPO_ROOT\n'
              'text = (R / "DEPLOY.md").read_text(encoding="utf-8")\n')
    assert _repo_root_paths_in(source, "a snippet") == set()
    assert _repo_root_uses_the_reader_cannot_follow(source) == [1]


def test_the_guard_accepts_the_shapes_this_file_already_uses():
    """Narrow on purpose, and for the same reason as the reader's own refusals: a guard that
    tripped on `str(REPO_ROOT)` or on the assignment that defines it would fail this suite on
    its own unmodified source, and the fix somebody reaches for then is to delete the guard."""
    source = ('REPO_ROOT = Path(__file__).resolve().parent.parent.parent\n'
              'proc = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-z"])\n'
              'text = (REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")\n')
    assert _repo_root_uses_the_reader_cannot_follow(source) == []

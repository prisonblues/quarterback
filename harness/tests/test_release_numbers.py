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

**Why it lives under `harness/` rather than in `tests/`.** It reads a handful of text files and
needs nothing else, but `tests/conftest.py` resolves `DATABASE_URL`, imports the app and can raise
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

#: `harness/`, and the repo root above it. Every `read_text` below passes `encoding="utf-8"`
#: explicitly: these files carry em dashes, and Python otherwise decodes with
#: `locale.getpreferredencoding()` — which in a nix sandbox or a minimal CI image is ASCII,
#: so the suite would die on a `UnicodeDecodeError` about the very files it is checking.
HARNESS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = HARNESS_ROOT.parent

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
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(pyproject["project"]["version"])


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


#: A `cp ${./some/path}` inside the flake's release-metadata check.
_FLAKE_COPY = re.compile(r"\$\{\./([^}]+)\}")

#: Where the check's build script begins. `pkgs.runCommand` is part of the anchor rather than
#: just the attribute name, because what is being sliced is that call's script, and an
#: expression wrapped in anything else is one this parser has not been taught to read. The
#: first version was a `find("release-metadata-tests =")`, which matched on exact spacing:
#: a second space around the `=` and the guard went red claiming the check had been renamed,
#: which is the kind of failure that gets a check deleted rather than fixed.
_FLAKE_CHECK_START = re.compile(r"^\s*release-metadata-tests\s*=\s*pkgs\.runCommand\b",
                                re.MULTILINE)

#: Where it ends: the `''` closing the script, alone on its line. Anchoring on the first `'';`
#: ANYWHERE after the start was the other half of that first version, and that one failed
#: quietly — `'';` occurs legitimately inside a build script, in a quoted shell string or a
#: comment, and every `cp` below such a line then went uncompared. A guard shown half the list
#: has nothing to complain about.
_FLAKE_CHECK_END = re.compile(r"^[ \t]*'';[ \t]*$", re.MULTILINE)

#: The conftest files pytest would load beside this suite in a developer's checkout. Neither
#: exists today. They are spelled relative to `harness/` rather than as `REPO_ROOT / …` joins
#: deliberately: they are not repo-root reads, and writing them that way would enrol them in
#: the flake-copy guard below, which would then demand a `cp` for a file that does not exist.
_CONFTESTS = ("conftest.py", "tests/conftest.py")


def _flake_release_check(text: str) -> str:
    """The build script of `flake.nix`'s `release-metadata-tests` check, as source text.

    Both anchors assert rather than return nothing. A parser that quietly hands back an empty
    slice on a renamed attribute makes the guards below report that the flake copies no files
    at all — which reads as "everything is missing" on a day when something is, and as nothing
    to complain about on the day the read set is empty too."""
    start = _FLAKE_CHECK_START.search(text)
    assert start, (
        "flake.nix has no `release-metadata-tests = pkgs.runCommand` check. If it was renamed, "
        "or wrapped in something else, teach this parser the new shape — these assertions are "
        "the only thing tying the suite to the sandbox that feeds it")
    end = _FLAKE_CHECK_END.search(text, start.end())
    assert end, (
        "flake.nix's release-metadata-tests build script has no closing `'';` alone on a line, "
        "so there is no bounded block to compare — the parser is wrong, or the check was "
        "reshaped")
    return text[start.end():end.start()]


def _flake_copies(text: str) -> set[str]:
    """The repo paths the release-metadata check copies into its sandbox."""
    return set(_FLAKE_COPY.findall(_flake_release_check(text)))


def _repo_root_uses(source: str | None = None) -> tuple[set[str], list[str]]:
    """Every repo-root path a source joins onto `REPO_ROOT`, and every OTHER use of that name.

    Parsed rather than grepped, and the first version of this was grepped. A pattern over the
    raw source cannot tell an expression from a sentence, and it read the path out of the
    comment that documented it — so the guard failed on a repo where nothing was wrong, which
    is the one failure this whole suite argues gets a check switched off. The tree has only
    the expressions.

    Only one shape is recognised: a chain of `/` joins whose right operands are all string
    literals and whose left end is the bare name, `REPO_ROOT / "app" / "main.py"`. Everything
    else a reader might reasonably write is valid Python and yields no path —
    `REPO_ROOT.joinpath("app", "main.py")`, `Path(str(REPO_ROOT), "x")`, `REPO_ROOT /
    f"{name}.md"`, an alias bound first. Teaching the walk all of them is a losing game, so the
    second return value exists instead: every `REPO_ROOT` the walk did not turn into a path,
    quoted with its line number, for a caller to assert about. An unreadable form has to be
    REPORTED, because the alternative is the guard below asking the flake for one file fewer
    and being told yes.

    Exactly one such use is legitimate here — the directory handed to `git ls-files`, which is
    not a file read at all. `test_every_use_of_repo_root_here_is_a_join_or_the_known_exception`
    is what pins that to one.

    `source` is for the tests that exercise this parser on text of their own; the callers that
    matter pass nothing and get this file."""
    text = Path(__file__).read_text(encoding="utf-8") if source is None else source
    tree = ast.parse(text)
    lines = text.split("\n")
    # `ast.walk` yields every node, so a two-component join offers itself twice: once whole,
    # and once as its own left operand. Taking both would register the DIRECTORY `app` as a
    # file to copy alongside `app/main.py`. Only maximal chains are reads.
    inner = {id(n.left) for n in ast.walk(tree)
             if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)}
    paths: set[str] = set()
    read: set[int] = set()  # the ids of the REPO_ROOT names a chain accounted for
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        if id(node) in inner:
            continue
        parts: list[str] = []
        cur: ast.expr = node
        while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
            if not isinstance(cur.right, ast.Constant) or not isinstance(cur.right.value, str):
                break
            parts.append(cur.right.value)
            cur = cur.left
        else:
            if isinstance(cur, ast.Name) and cur.id == "REPO_ROOT":
                paths.add("/".join(reversed(parts)))
                read.add(id(cur))
    others = sorted((n.lineno, lines[n.lineno - 1].strip()) for n in ast.walk(tree)
                    if isinstance(n, ast.Name) and n.id == "REPO_ROOT"
                    # Store context is the assignment at the top of this file, not a use.
                    and isinstance(n.ctx, ast.Load) and id(n) not in read)
    return paths, [f"line {lineno}: {line}" for lineno, line in others]


def _paths_this_suite_reads(source: str | None = None) -> set[str]:
    """The paths half of `_repo_root_uses`, for the guards that only want those."""
    return _repo_root_uses(source)[0]


def test_the_flake_check_supplies_every_repo_root_file_this_suite_reads():
    """The enumeration in `flake.nix` is the thing that goes stale, so nothing relies on
    somebody remembering it.

    This suite reads files at the repo ROOT while living two directories below it, and
    `nix build .#checks.<system>.release-metadata-tests` runs it in a sandbox containing
    only the files that check names one by one. Add a read and the sandbox does not have it:
    the new assertion does not fail there, it ERRORS on a missing file — and an ERROR line in
    a check somebody has to go and read is exactly how #163 sat unnoticed for a day with all
    eight of these assertions inert.

    So the coupling is asserted here, where it fails in the ordinary `pytest harness/tests`
    a developer runs before pushing, rather than in a nix build they may not run at all.

    Skipped rather than failed when `flake.nix` is absent: this file is also collected from
    a sandbox, and a check that cannot see the expression cannot judge it."""
    flake = REPO_ROOT / "flake.nix"
    if not flake.is_file():
        pytest.skip("no flake.nix beside this checkout, so there is no check to compare against")
    missing = sorted(_paths_this_suite_reads()
                     - _flake_copies(flake.read_text(encoding="utf-8")))
    assert not missing, (
        "this suite reads repo-root files that flake.nix's release-metadata-tests check does "
        "not copy into its sandbox, so they will error there as FileNotFoundError rather than "
        "be asserted: " + ", ".join(missing) + ". Add a `cp ${./<path>}` for each")


def test_the_flake_check_supplies_every_conftest_that_would_load_beside_this_suite():
    """The guard above compares the flake's `cp` list against the files this suite READS, and
    a conftest is read by nothing here — pytest imports it before collection, from wherever it
    sits at or above the test file's directory. So neither `harness/conftest.py` nor
    `harness/tests/conftest.py` is in that comparison, and the day somebody adds one the nix
    sandbox starts collecting this file without the fixtures, markers or collection hooks a
    developer's `pytest harness/tests` gives it. The two runs are then running different suites
    and the nix one is still green.

    Neither file exists as this is written, so this pins a coupling rather than fixing a bug.
    It is written down because the cost of learning it the other way is a check that passes in
    the store and fails on the machine, or the reverse.

    Same skip-when-absent treatment as the guard above, for the same reason: this file is also
    collected from a sandbox with no flake.nix in it to judge."""
    flake = REPO_ROOT / "flake.nix"
    if not flake.is_file():
        pytest.skip("no flake.nix beside this checkout, so there is no check to compare against")
    copied = _flake_copies(flake.read_text(encoding="utf-8"))
    uncopied = [f"harness/{rel}" for rel in _CONFTESTS
                if (HARNESS_ROOT / rel).is_file() and f"harness/{rel}" not in copied]
    assert not uncopied, (
        "pytest loads " + ", ".join(uncopied) + " beside this suite, and flake.nix's "
        "release-metadata-tests check does not copy it into its sandbox — so the nix run "
        "collects this file without it while a developer's run does not. Add a `cp` for it "
        "there, or drop it from `_CONFTESTS` deliberately if the sandbox is meant to run "
        "without it")


def test_the_reader_finds_the_paths_it_is_meant_to_find():
    """The guards above are only worth having if this parser works, and one that silently
    found NOTHING would make them pass against any flake at all.

    The WHOLE set, compared with `==`. The earlier shape was a subset assertion over four
    paths, and `flake.nix` was added as a fifth read without joining it — so a regression that
    dropped single-segment joins would have left both this test and the guard green while the
    guard stopped checking the one copy that keeps it honest about itself. `==` also catches a
    read spelled as a local (`d = REPO_ROOT / "app"`, then `d / "main.py"`), which this parser
    reads as a read of the DIRECTORY `app`: a path that is not in this set."""
    assert _paths_this_suite_reads() == {
        "CHANGELOG.md", "README.md", "pyproject.toml", "app/main.py", "flake.nix"}


def test_every_use_of_repo_root_here_is_a_join_or_the_known_exception():
    """The accounting that makes this parser's silence trustworthy.

    A read written in a shape the walk cannot follow contributes no path, so the guard above
    asks the flake for one file fewer and the flake says yes — a check that has stopped
    checking, with nothing anywhere going red. `_repo_root_uses` hands back every `REPO_ROOT`
    it could not turn into a path, and exactly one is meant to exist: the directory given to
    `git ls-files`, which is not a file read.

    A second one means a read went unseen. Rewrite it as a plain chain of string literals, or
    teach `_repo_root_uses` the new shape — widening this count is the one repair that puts
    the hole back."""
    _, others = _repo_root_uses()
    assert len(others) == 1 and "ls-files" in others[0], (
        "REPO_ROOT is used here in a way `_repo_root_uses` cannot read as a path, so the "
        "flake-copy guard cannot see it: " + "; ".join(others))


def test_a_join_this_parser_cannot_read_is_reported_rather_than_dropped():
    """Each of these is a legitimate way to write a repo-root read and none of them produces a
    chain. What matters is not that the parser handles them — it does not — but that it says
    so, since the alternative is the guard above quietly checking one file fewer."""
    for source in ('REPO_ROOT.joinpath("app", "main.py")\n',
                   'Path(str(REPO_ROOT), "app", "main.py")\n',
                   'name = "CHANGELOG"\nREPO_ROOT / f"{name}.md"\n',
                   'ROOT = REPO_ROOT\nROOT / "CHANGELOG.md"\n',
                   'REPO_ROOT / ("CHANGE" + "LOG.md")\n'):
        paths, others = _repo_root_uses(source)
        assert not paths, source
        assert len(others) == 1, source


def test_the_flake_slice_stops_at_the_script_and_not_at_the_first_quote_pair():
    """`'';` inside the build script — quoted in a shell string, or written in a comment — is
    not the end of the script, and a slice that stopped there would drop every `cp` below it
    and compare against a list with holes in it. The second check is here so the slice cannot
    run PAST its script either and collect a neighbour's copies."""
    text = ('      checks = {\n'
            '        release-metadata-tests = pkgs.runCommand "quarterback-release" { } \'\'\n'
            '          cp ${./CHANGELOG.md} repo/CHANGELOG.md\n'
            '          echo "a literal \'\'; inside a shell string"\n'
            '          cp ${./flake.nix}    repo/flake.nix\n'
            '        \'\';\n'
            '        other-check = pkgs.runCommand "quarterback-other" { } \'\'\n'
            '          cp ${./NOT-THIS-ONE.md} .\n'
            '        \'\';\n'
            '      };\n')
    assert _flake_copies(text) == {"CHANGELOG.md", "flake.nix"}


def test_the_flake_slice_says_so_when_an_anchor_does_not_match():
    """Neither anchor may fail by returning nothing. A renamed attribute or a reshaped call is
    a thing to go and look at, not a comparison against an empty list that passes."""
    with pytest.raises(AssertionError, match="release-metadata-tests"):
        _flake_release_check("      checks = {\n        harness-build = self.packages.x;\n")
    with pytest.raises(AssertionError, match="closing"):
        _flake_release_check('  release-metadata-tests = pkgs.runCommand "x" { } \'\'\n'
                             '    cp ${./CHANGELOG.md} .\n')

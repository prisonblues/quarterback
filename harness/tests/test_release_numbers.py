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
from collections.abc import Callable
from pathlib import Path

import pytest

import _flake_sandbox

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The repo root as a string, for the one use of it that is not a file read: the directory
#: handed to `git ls-files`. Bound to its own name rather than spelled `str(REPO_ROOT)` at
#: the call site so that `_repo_root_uses_the_reader_cannot_follow` below has one shape to
#: recognise instead of an exemption for every `str(REPO_ROOT)` anywhere in the file — which
#: would have waved through `Path(str(REPO_ROOT), "x")` and `os.path.join(str(REPO_ROOT), "x")`,
#: reads the flake enumeration never hears about.
_GIT_CWD = str(REPO_ROOT)

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
    raw = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return str(tomllib.loads(raw)["project"]["version"])


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
        # Named in flake.nix's allowed-skip list; see the note in the git test above.
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
        proc = subprocess.run(["git", "-C", _GIT_CWD, "ls-files", "-z"],
                              capture_output=True, text=True, check=False)
    except OSError:
        # Both skip reasons are matched by name in flake.nix's release-metadata check, which
        # allows exactly these two and fails the build on any other skip. Reword one and the
        # build says so; it does not go quietly green.
        pytest.skip("git is not on PATH, so there is no set of tracked files to check")
    if proc.returncode != 0:
        pytest.skip("not a git checkout (an export or a tarball), so nothing here is tracked")
    named = sorted(p for p in proc.stdout.split("\0")
                   if re.fullmatch(r"test_v\d[^/]*\.py", p.rpartition("/")[2]))
    assert not named, (
        "test files are named after their subject, not the release that shipped them: "
        + ", ".join(named))


def _git_answers(stdout: str = "",
                 returncode: int = 0) -> Callable[..., subprocess.CompletedProcess]:
    """A stand-in for `subprocess.run` returning one of git's answers.

    Monkeypatched in rather than a real repository being built, because what is under test is
    what this file does with each answer — absent, refused, and a listing — not what git says
    about a checkout. The three tests below are the only things exercising this test's three
    paths at all: in a normal run it takes the listing path against the real repo and neither
    skip is ever reached, and in the nix sandbox it takes a skip and asserts nothing.
    """
    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], returncode, stdout, "")
    return run


def test_the_tracked_filenames_check_skips_when_git_is_absent(monkeypatch):
    """The release-metadata sandbox has no git in it on purpose, and flake.nix says in a
    comment that this test skips there rather than failing. That claim held up only as prose
    until here; it is also the reason string the flake check allows by name, so a reword that
    silently turned the skip into something the build refuses is caught in the same breath."""
    def no_git(*_args, **_kwargs):
        raise OSError(2, "No such file or directory: 'git'")
    monkeypatch.setattr(subprocess, "run", no_git)
    with pytest.raises(pytest.skip.Exception, match="git is not on PATH"):
        test_no_test_file_is_named_after_a_release()


def test_the_tracked_filenames_check_skips_outside_a_checkout(monkeypatch):
    """The other skip: git is there and the directory is an export or a tarball. Also allowed
    by name in the flake check, for the sandbox where git happens to be present."""
    monkeypatch.setattr(subprocess, "run", _git_answers(returncode=128))
    with pytest.raises(pytest.skip.Exception, match="not a git checkout"):
        test_no_test_file_is_named_after_a_release()


def test_the_tracked_filenames_check_names_the_files_it_refuses(monkeypatch):
    """And the rule itself, on a listing written here: a release-named test file in any suite
    fails, and the ordinary ones beside it do not. Without this the two skips above are the
    only paths through the test that anything exercises."""
    monkeypatch.setattr(subprocess, "run", _git_answers(
        "tests/test_v234.py\0harness/tests/test_v2_release.py\0tests/test_reviews.py\0"
        "app/main.py\0tests/test_versioning.py\0"))
    with pytest.raises(AssertionError,
                       match=r"harness/tests/test_v2_release\.py, tests/test_v234\.py"):
        test_no_test_file_is_named_after_a_release()


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


# ----------------------------------------------- the sandbox this suite is run in
#
# This suite reads files at the repo ROOT while living two directories below it, and
# `nix build .#checks.<system>.release-metadata-tests` runs it in a sandbox holding only the
# files that check copies in by name. Enumeration is what goes stale: a read whose file
# nobody copied does not FAIL there, it errors on a missing file, and an ERROR line in a
# check somebody has to go and read is how #163 sat unnoticed. So the two lists are compared
# below, in both directions, where they fail in the ordinary `pytest harness/tests` a
# developer runs before pushing rather than in a nix build they may not run at all.
#
# The comparison needs the suite's own reads as data, which is what the two AST readers
# further down produce. Both take a source string, so the shapes they exist to catch are
# written out as three-line snippets in the tests rather than having to be smuggled into
# this file's real source.

#: The flake attribute whose sandbox runs this suite, spelled as flake.nix spells it.
_FLAKE_CHECK = "release-metadata-tests"

#: Copied in without being read through `REPO_ROOT`, so the copies-with-no-read half of the
#: comparison does not report it: pytest opens the suite's own file by path, and the shared
#: reader below is imported rather than read.
_COPIED_BUT_NOT_READ = frozenset({"harness/tests/test_release_numbers.py",
                                  "harness/tests/_flake_sandbox.py"})

#: Reading a check's block out of flake.nix, parsing its copy lines and checking they land
#: where this suite looks, is the same job for every suite with this problem — and it was
#: written out twice, here and in `_prose_sandbox` (#257). Two hand-rolled readers of one file
#: agree only until somebody edits one of them. `_SANDBOX_PREFIX` is kept as a thin alias
#: because this file's assertions read it directly; the logic has one home.
_SANDBOX_PREFIX = _flake_sandbox.SANDBOX_PREFIX


def _flake_check_region(text: str) -> str:
    """This check's own text, sliced out of flake.nix. See `_flake_sandbox.check_region`."""
    return _flake_sandbox.check_region(text, _FLAKE_CHECK)


def _flake_copies(region: str) -> dict[str, str]:
    """Source path -> destination, for every copy line in a check's script."""
    return _flake_sandbox.copies(region)


#: This file's own syntax tree, parsed once. Both readers below want it, and parsing the
#: source twice per run to hand them each their own copy bought nothing.
_OWN_TREE: ast.Module | None = None


def _source_tree(source: str | None = None) -> ast.Module:
    """This file's tree, or a snippet's when one is given."""
    global _OWN_TREE
    if source is not None:
        return ast.parse(source)
    if _OWN_TREE is None:
        _OWN_TREE = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    return _OWN_TREE


def _repo_root_chain(node: ast.AST) -> list[str] | None:
    """The literal segments joined onto `REPO_ROOT` with `/`, or None if this expression is
    not rooted at `REPO_ROOT` through string literals alone.

    A bare `REPO_ROOT` yields `[]` — rooted, but naming no file. One implementation of this
    rule rather than two, because both readers below have to agree about what a followable
    chain is and two hand-rolled versions of that only have to agree until someone edits one.
    """
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
        if not isinstance(cur.right, ast.Constant) or not isinstance(cur.right.value, str):
            return None
        parts.append(cur.right.value)
        cur = cur.left
    if not (isinstance(cur, ast.Name) and cur.id == "REPO_ROOT"):
        return None
    return list(reversed(parts))


def _paths_this_suite_reads(source: str | None = None) -> set[str]:
    """Every repo-root path joined onto `REPO_ROOT`, read out of the syntax tree.

    Parsed rather than grepped, and the first version of this was grepped. A pattern over the
    raw source cannot tell an expression from a sentence, and it read the path out of the
    comment that documented it — so the guard failed on a repo where nothing was wrong, which
    is the one failure this whole suite argues gets a check switched off.

    A bare `REPO_ROOT` with nothing joined onto it names no file and yields no path.

    Deliberately an over-approximation in one direction: a chain built to assert a file's
    ABSENCE (`assert not (REPO_ROOT / "gone").exists()`) is indistinguishable from a read
    here, and would be reported as a path the flake must copy — which for a path that does
    not exist fails Nix evaluation outright. No such chain exists today. Write that assertion
    against a path outside `REPO_ROOT` if one is ever needed.
    """
    tree = _source_tree(source)
    # `ast.walk` yields every node, so a two-component join offers itself twice: once whole,
    # and once as its own left operand. Taking both would register the DIRECTORY `app` as a
    # file to copy alongside `app/main.py`. Only maximal chains are reads.
    inner = {id(n.left) for n in ast.walk(tree)
             if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)}
    paths: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        if id(node) in inner:
            continue
        parts = _repo_root_chain(node)
        if parts:
            paths.add("/".join(parts))
    return paths


#: Callees that build a path out of their ARGUMENTS. `REPO_ROOT` reaching one of these at any
#: depth of wrapping — `Path(str(REPO_ROOT), "x")`, `os.path.join(str(REPO_ROOT), "x")` — is a
#: read the chain reader cannot see, which is why the wrapping is walked through rather than
#: only the immediate parent being looked at.
_PATH_BUILDERS = frozenset({"Path", "PurePath", "PosixPath", "WindowsPath", "join", "joinpath"})

#: Attribute access on `REPO_ROOT` that CONTINUES a path rather than asking a question about
#: one. `.is_dir()`, `.exists()`, `.name`, `.relative_to()` and the rest are not reads and are
#: none of this guard's business.
_JOINING_ATTRS = frozenset({"joinpath"})


def _builds_a_path(func: ast.expr) -> bool:
    """Whether a call's callee is one of the path constructors above."""
    if isinstance(func, ast.Name):
        return func.id in _PATH_BUILDERS
    return isinstance(func, ast.Attribute) and func.attr in _PATH_BUILDERS


def _repo_root_uses_the_reader_cannot_follow(source: str | None = None) -> list[int]:
    """The lines where `REPO_ROOT` builds a path in a shape `_paths_this_suite_reads` skips.

    That reader only understands `REPO_ROOT / "a" / "b"` with string literals throughout,
    which is every read in this file today and no guarantee about tomorrow's. A read written
    any other way leaves it with nothing to report, so the flake enumeration goes stale and
    the sandbox errors on a missing file — #163 again.

    What is reported is the shapes that BUILD a path and cannot be followed: a non-literal
    segment (including a loop variable), `.joinpath`, `Path(...)`/`os.path.join(...)` at any
    depth of wrapping, and a name bound to a `REPO_ROOT` chain that is later joined onto —
    `app_dir = REPO_ROOT / "app"` followed by `app_dir / "main.py"`, where the reader
    registers the directory and never hears about the file.

    What is NOT reported is every use that is not a path being built: `path.relative_to(
    REPO_ROOT)`, `REPO_ROOT.is_dir()`, `subprocess.run(..., cwd=REPO_ROOT)`, an f-string, or
    `REPO_ROOT` handed to a helper. Reporting those was the earlier rule here — refuse
    anything unrecognised — and it fires on correct code with remediation advice ("write it
    as `REPO_ROOT / \\"literal\\"`") that makes no sense for a non-path use. This suite's own
    argument is that a check which fires on a correct repo gets switched off, so the shapes
    are enumerated rather than the exemptions. The cost is a blind spot: a helper handed
    `REPO_ROOT` can read whatever it likes unseen. Nothing here can chase that, and the
    honest version of this guard says so rather than firing at everything.
    """
    tree = _source_tree(source)
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    # Names bound to a REPO_ROOT chain, so that a later join onto one can be spotted. The
    # binding itself is fine — `flake = REPO_ROOT / "flake.nix"` is a read the reader sees.
    aliases: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            targets = [node.target]
        if targets and node.value is not None and _repo_root_chain(node.value) is not None:
            aliases.update(t.id for t in targets
                           if isinstance(t, ast.Name) and t.id != "REPO_ROOT")

    def joined_onto(node: ast.AST) -> bool:
        parent = parents.get(id(node))
        if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div) and parent.left is node:
            return True
        return isinstance(parent, ast.Attribute) and parent.attr in _JOINING_ATTRS

    def inside_a_path_builder(node: ast.AST) -> bool:
        cur: ast.AST | None = node
        while cur is not None and not isinstance(cur, ast.stmt):
            parent = parents.get(id(cur))
            if isinstance(parent, ast.Call) and cur is not parent.func \
                    and _builds_a_path(parent.func):
                return True
            cur = parent
        return False

    unreadable: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
            continue
        if node.id in aliases and joined_onto(node):
            unreadable.add(node.lineno)
            continue
        if node.id != "REPO_ROOT":
            continue
        parent = parents.get(id(node))
        if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div) and parent.left is node:
            # Follow the chain to its top: a non-literal anywhere along it makes the whole
            # chain unreadable, and it is the `REPO_ROOT` line that names the read.
            top: ast.AST = node
            while True:
                above = parents.get(id(top))
                if isinstance(above, ast.BinOp) and isinstance(above.op, ast.Div) \
                        and above.left is top:
                    top = above
                else:
                    break
            if _repo_root_chain(top) is None:
                unreadable.add(node.lineno)
        elif joined_onto(node) or inside_a_path_builder(node):
            unreadable.add(node.lineno)
    # Sorted and de-duplicated: two unfollowable uses on one line (a comparison of two, a
    # multi-argument call) would otherwise name that line twice in the failure message.
    return sorted(unreadable)


def test_the_flake_check_supplies_every_repo_root_file_this_suite_reads():
    """The enumeration in flake.nix against the reads in this file, both directions.

    Skipped rather than failed when flake.nix is absent, because this file is also collected
    from sandboxes that do not contain it. That skip is itself a hole — this is the release
    check's only staleness guard, and a guard that can vanish silently is the thing this suite
    argues against — so the flake check allows exactly two skip reasons and neither is this
    one: drop the flake.nix copy line there and the build goes red rather than green.
    """
    flake = REPO_ROOT / "flake.nix"
    if not flake.is_file():
        pytest.skip("no flake.nix beside this checkout, so there is no check to compare against")
    copies = _flake_copies(_flake_check_region(flake.read_text(encoding="utf-8")))
    read = _paths_this_suite_reads()

    missing = sorted(read - set(copies))
    assert not missing, (
        f"this suite reads repo-root files that flake.nix's {_FLAKE_CHECK} check does not "
        "copy into its sandbox, so they will error there as FileNotFoundError rather than be "
        "asserted: " + ", ".join(missing) + ". Add an `install -Dm644 ${./<path>} "
        f"{_SANDBOX_PREFIX}<path>` line for each")

    unread = sorted(set(copies) - read - _COPIED_BUT_NOT_READ)
    assert not unread, (
        f"flake.nix's {_FLAKE_CHECK} check copies files this suite no longer reads, so the "
        "sandbox carries a store dependency for nothing and its file list has quietly stopped "
        "describing the suite: " + ", ".join(unread) + ". Drop the copy line, or add the path "
        "to `_COPIED_BUT_NOT_READ` with the reason it is there")

    misplaced = sorted(f"{src} -> {dest}" for src, dest in copies.items()
                       if dest != _SANDBOX_PREFIX + src)
    assert not misplaced, (
        f"flake.nix's {_FLAKE_CHECK} check copies files to destinations that do not mirror "
        f"their repo paths under `{_SANDBOX_PREFIX}`, so the suite will not find them where "
        "it looks and the source-path comparison above cannot see it: " + ", ".join(misplaced))


def test_the_reader_finds_every_literal_chain_and_only_those():
    """The comparison above is only worth having if its reader works.

    Asserted against a snippet rather than against this file's own reads. Writing those out
    would be a third copy of the enumeration — the flake's, this file's, and a hand-kept list
    of what the reader ought to see — and the third one is the one that goes stale unnoticed,
    since a subset check never fails when a read DISAPPEARS. What guards this file's reads is
    the two-directional comparison above."""
    source = (
        '(REPO_ROOT / "CHANGELOG.md").read_text()\n'
        'text = (REPO_ROOT / "app" / "main.py").read_text()\n'
        'flake = REPO_ROOT / "flake.nix"\n'
        'subprocess.run(["git", "-C", str(REPO_ROOT)])\n'
        'REPO_ROOT.joinpath("invisible.md")\n'
        '(REPO_ROOT / name).read_text()\n'
        '(OTHER_ROOT / "not-this-root.md").read_text()\n'
    )
    assert _paths_this_suite_reads(source) == {"CHANGELOG.md", "app/main.py", "flake.nix"}


def test_the_reader_is_not_silently_finding_nothing():
    """On this file, where a reader returning an empty set would make the comparison above
    pass against any flake at all."""
    assert _paths_this_suite_reads()


#: The shapes the shape guard exists to refuse, one snippet each. Measured rather than
#: supposed: every one of them reads a repo-root file while leaving `_paths_this_suite_reads`
#: with nothing to report, so the flake enumeration never hears about the file.
_SHAPES_THE_READER_CANNOT_FOLLOW = {
    "joinpath": 'REPO_ROOT.joinpath("CHANGELOG.md").read_text()',
    "Path(REPO_ROOT, ...)": 'Path(REPO_ROOT, "CHANGELOG.md").read_text()',
    "a str()-wrapped root in a Path()": 'Path(str(REPO_ROOT), "CHANGELOG.md").read_text()',
    "os.path.join on the root": 'os.path.join(str(REPO_ROOT), "CHANGELOG.md")',
    "a variable segment": 'name = "CHANGELOG.md"\n(REPO_ROOT / name).read_text()\n',
    "a loop over filenames": 'for name in NAMES:\n    (REPO_ROOT / name).read_text()\n',
    "an intermediate directory": 'app_dir = REPO_ROOT / "app"\n(app_dir / "main.py").read_text()\n',
    "an alias": 'root = REPO_ROOT\n(root / "app" / "main.py").read_text()\n',
}

#: Uses of `REPO_ROOT` that are not a path being built. The guard must stay quiet about these:
#: it fires with advice to rewrite the expression as `REPO_ROOT / "literal"`, which is not
#: something a `cwd=` argument or an `is_dir()` call can be rewritten as.
_SHAPES_THAT_ARE_NOT_READS = {
    "a literal chain": '(REPO_ROOT / "app" / "main.py").read_text()',
    "a chain bound to a name and then read": 'flake = REPO_ROOT / "flake.nix"\nflake.read_text()\n',
    "the git cwd string": '_GIT_CWD = str(REPO_ROOT)',
    "a subprocess cwd": 'subprocess.run(["git", "status"], cwd=REPO_ROOT)',
    "a question about the root": 'if REPO_ROOT.is_dir():\n    pass\n',
    "a relative_to argument": 'p.relative_to(REPO_ROOT)',
    "a comparison": 'assert Path.cwd() != REPO_ROOT',
    "a message naming the root": 'raise SystemExit(f"no repo at {REPO_ROOT}")',
}


@pytest.mark.parametrize("shape", sorted(_SHAPES_THE_READER_CANNOT_FOLLOW))
def test_the_shape_guard_refuses_reads_the_reader_cannot_follow(shape):
    """Each of these passes `_paths_this_suite_reads` in silence and then errors in the
    sandbox on a file nobody copied in. The guard is what stands between the two, and a
    refactor that made it return nothing unconditionally would leave this suite green
    without them."""
    source = _SHAPES_THE_READER_CANNOT_FOLLOW[shape]
    assert _repo_root_uses_the_reader_cannot_follow(source), source


@pytest.mark.parametrize("shape", sorted(_SHAPES_THAT_ARE_NOT_READS))
def test_the_shape_guard_is_quiet_about_uses_that_are_not_reads(shape):
    source = _SHAPES_THAT_ARE_NOT_READS[shape]
    assert _repo_root_uses_the_reader_cannot_follow(source) == [], source


def test_the_shape_guard_names_a_line_once():
    """Two unfollowable uses on one line is one line to go and look at, not two."""
    assert _repo_root_uses_the_reader_cannot_follow(
        'shutil.copy(Path(REPO_ROOT, "a"), Path(REPO_ROOT, "b"))') == [1]


def test_every_use_of_repo_root_is_one_the_reader_can_follow():
    """On this file. The coupling is not "the reader sees every read" — it cannot be — but
    "every read here is written in a shape the reader sees"."""
    unreadable = _repo_root_uses_the_reader_cannot_follow()
    assert not unreadable, (
        "REPO_ROOT builds a path at line(s) " + ", ".join(str(n) for n in unreadable)
        + " in a shape `_paths_this_suite_reads` cannot follow, so the file being read there "
        "will not reach the list checked against flake.nix. Write it as `REPO_ROOT / "
        "\"literal\"`, or teach the reader the new shape and add the file to the "
        f"{_FLAKE_CHECK} check")


# The reader itself is exercised in `test_flake_sandbox.py`, beside the module that implements
# it — five tests here duplicated its cases after the extraction, with a fixture that had
# already drifted from the shared one (this copy had no `cp -r` of a directory, so the shape
# most likely to regress was covered in one place and not the other). The coupling test above
# still runs the reader against the real flake.nix, which is what this suite needs from it.



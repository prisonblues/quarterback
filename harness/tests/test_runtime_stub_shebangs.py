"""No test may write a `#!/usr/bin/env` stub — the guard #177 asked for.

**There is no `/usr/bin/env` inside a nix build sandbox.** `patchShebangs` rewrites
the scripts shipped in `harness/bin` at build time and cannot reach a file a test
writes *while it runs*, so a runtime-written stub carrying `#!/usr/bin/env bash`
cannot exec there at all.

This has now shipped five times: `test_qb_seats.py` (#171), `test_qb_seat.py` and
`create_worktree_nginx.test.sh` (v2.55), and `test_create_worktree_claim.py` twice
over — once in its stub factory and once in three tests that write their own. #177
was opened after the third with one sentence about the rest: *"All three are fixed.
This issue is about the fourth."* The fourth arrived anyway, because the rule lived
in review comments and a changelog entry, and neither of those runs.

**Why a fourth instance was worth a guard and a third was not.** The two seats
suites failed loudly — `assert 126 == 0`, `bad interpreter` in stderr. The nginx one
did not: its stub was `docker`, exec'd by the script under test rather than by the
test, and `command -v docker` succeeds on any file that exists and is `chmod +x`.
The exec then failed, the script concluded there was no container to proxy to, and
skipped the step *exactly as designed*. A suite can be green about nothing here, so
"someone will notice" is not a control.

**What this asserts and what it deliberately does not.** It reads the sources of
both sandboxed test trees — `harness/tests` and `harness/loops/tests`, `.py` and
`.sh` alike — as text, and looks for a shebang naming `/usr/bin/env`: inside a
string literal, or at the head of a line, which is where one lands in a
triple-quoted stub body. It cannot know whether that string is ever written to a
file, so a mention in a comment or a deliberate literal is not evidence of a defect
— and `test_qb_seat.py` has one of each, on purpose, in a test *about* this very
rule. Both are allowed by path below rather than by a pattern, so a new one has to
be argued for here rather than slipped past.

The remedy is one of two, and the suite picks by what the stub needs:
`#!/bin/sh` where the body is POSIX (`test_qb_seats.py`), or the absolute path of
the interpreter the suite already resolved (`test_create_worktree_claim.py`, whose
stubs use `[[ ]]`).

Run: pytest harness/tests/test_runtime_stub_shebangs.py
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1]

#: Both sandboxed test trees, because both run inside a nix build that has no
#: `/usr/bin/env`: `harness/tests` is the `worktree-tests` check and
#: `harness/loops/tests` is `loops-tests`. The rule is about the sandbox, not
#: about which directory a suite happens to live in.
TREES = (HARNESS / "tests", HARNESS / "loops" / "tests")

#: `/usr/bin/env` at the START of a string literal — which covers the shebang
#: written whole (`"#!/usr/bin/env bash"`) and the two ways of assembling one that
#: a `#!`-anchored pattern would miss: `"#!" + "/usr/bin/env bash"`, and a named
#: constant (`SHEBANG = "/usr/bin/env bash"`) interpolated later.
#:
#: …and the form with no quote in front of it at all: a stub body written as a
#: triple-quoted block, where the shebang begins its own line. That is how a stub
#: of more than one line is usually written and a quote-anchored pattern walked
#: straight past it, so a `#!` at the head of a line counts too — LINE 1 EXCEPTED,
#: since a file's own shebang is not a stub anything writes at runtime.
#:
#: It cannot catch every possible assembly — a path built character by character
#: defeats any pattern — so this is a tripwire for the forms people actually write,
#: not a proof. The nix build is still what decides.
_ENV_SHEBANG = re.compile(r"""(?:['"]\s*(?:#!\s*)?|^\s*#!\s*)/usr/bin/env\b""")

#: Lines allowed to carry one, each for a stated reason rather than a shape:
#: `(path relative to harness/, substring that must also be on the line, how many
#: matches it may carry)`. A PATH and not a bare filename, because two trees are
#: read now and `conftest.py` is a name both may hold. The COUNT is the point:
#: exempting a LINE rather than an occurrence would let a second bad literal be
#: appended to an allowed line and go unread.
ALLOWED = {
    # A test whose SUBJECT is which shebangs work: it writes all three forms and
    # asserts on what each does. Rewriting it would delete the coverage.
    ("tests/test_qb_seat.py", 'for body in (', 1),
}


def _sources() -> list[Path]:
    """Every test source in both trees — `.py` and `.sh` alike.

    NOT only `test_*.py`. `create_worktree_nginx.test.sh` is one of the five
    instances the docstring above counts and it is a shell script; and a
    `conftest.py` or a `_helper.py` writes stubs for a whole suite at once, which
    makes it the worst place for one to hide rather than a place the rule stops at.
    """
    me = Path(__file__).resolve()
    found = sorted(
        p for tree in TREES if tree.is_dir()
        for p in tree.rglob("*")
        if p.is_file() and p.suffix in (".py", ".sh") and p.resolve() != me
    )
    assert found, f"no test sources under {TREES} — this guard is asserting nothing"
    return found


def _rel(path: Path) -> str:
    """The path as ALLOWED spells it, and as a parametrize id reads."""
    return path.resolve().relative_to(HARNESS).as_posix()


def _allowed(path: Path, line: str) -> bool:
    """Exempt only if the line is named AND carries no more hits than it was granted."""
    for name, needle, count in ALLOWED:
        if _rel(path) == name and needle in line:
            return len(_ENV_SHEBANG.findall(line)) <= count
    return False


@pytest.mark.parametrize("path", _sources(), ids=_rel)
def test_no_test_writes_a_usr_bin_env_stub(path: Path):
    """A stub a test writes at runtime cannot name `/usr/bin/env` (#177)."""
    offenders = [
        f"{_rel(path)}:{n}: {line.strip()}"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        # Line 1 is the file's own shebang, and nothing writes that at runtime.
        if n > 1 and _ENV_SHEBANG.search(line) and not _allowed(path, line)
    ]
    assert not offenders, (
        "a shebang naming /usr/bin/env appears in a string literal here, and there "
        "is no /usr/bin/env inside a nix build sandbox — a stub written at runtime "
        "cannot exec, and the suite then fails (or worse, passes) for a reason that "
        "has nothing to do with the code under test. Use `#!/bin/sh` for a POSIX "
        "body, or the absolute interpreter path the suite already resolved. If this "
        "line is a comment or a deliberate literal, add it to ALLOWED with its "
        "reason.\n  " + "\n  ".join(offenders))


def test_both_trees_are_actually_read():
    """Silently scanning one of the two trees would be half a guard.

    `_sources` tolerates a missing tree so it can say something useful rather
    than raising at collection, and that tolerance is exactly what would let this
    shrink without anyone noticing: `worktree-tests` is the only check that runs
    this file, and it holds `harness/loops` for no other reason than this read.
    The day that copy line goes away, this fails instead of the coverage halving.
    """
    for tree in TREES:
        assert tree.is_dir(), (
            f"{tree} is not here, so this guard is reading one tree and claiming "
            "two — if a check stopped installing it, put the line back rather "
            "than narrowing TREES")
    assert {_rel(p).split("/")[0] for p in _sources()} == {"tests", "loops"}


def test_the_allowlist_still_matches_something():
    """An allowlist entry that matches nothing is a rule nobody is being held to.

    It would also hide the next real instance in that file the day the line it was
    written for is edited, so a stale entry is worse than an absent one.
    """
    for name, needle, count in ALLOWED:
        path = HARNESS / name
        assert path.exists(), f"ALLOWED names {name}, which is gone — drop the entry"
        matched = [
            line for n, line in enumerate(path.read_text().splitlines(), 1)
            if n > 1 and _ENV_SHEBANG.search(line) and needle in line
        ]
        assert matched, (
            f"ALLOWED exempts {name} lines containing {needle!r}, but no such line "
            "carries a /usr/bin/env shebang any more — the exemption is stale and "
            "should be deleted, not left to cover a future one")
        for line in matched:
            assert len(_ENV_SHEBANG.findall(line)) <= count, (
                f"{name}: the exempt line carries more than the {count} literal(s) it "
                "was granted — say why in ALLOWED rather than raising the number")


def test_the_pattern_catches_the_form_that_actually_shipped():
    """The guard's own red, since a pattern that matches nothing passes everything."""
    assert _ENV_SHEBANG.search("""    fake.write_text('#!/usr/bin/env bash\\n' + body)""")
    assert _ENV_SHEBANG.search('''    stub.write_text("#!/usr/bin/env sh\\n")''')
    # …the two ways of assembling one that a `#!`-anchored pattern would miss…
    assert _ENV_SHEBANG.search('    stub.write_text("#!" + "/usr/bin/env bash")')
    assert _ENV_SHEBANG.search('SHEBANG = "/usr/bin/env bash"')
    # …and the triple-quoted body, where the shebang has no quote in front of it
    # at all and begins its own line. That is how a stub of more than one line is
    # usually written, and a quote-anchored pattern walked straight past it.
    assert _ENV_SHEBANG.search("#!/usr/bin/env bash")
    assert _ENV_SHEBANG.search("        #!/usr/bin/env python3")
    # …and does not fire on the two forms that are correct.
    assert not _ENV_SHEBANG.search("""    fake.write_text('#!/bin/sh\\n' + body)""")
    assert not _ENV_SHEBANG.search('''    fake.write_text(f"#!{BASH}\\n" + body)''')
    # …nor on prose that merely names the path, which is how this rule gets
    # explained wherever it is obeyed.
    assert not _ENV_SHEBANG.search("# There is no /usr/bin/env in a nix sandbox.")

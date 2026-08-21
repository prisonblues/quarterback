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

**What this asserts and what it deliberately does not.** It reads the test sources
as text and looks for a shebang inside a string literal. It cannot know whether that
string is ever written to a file, so a mention in a comment or a deliberate literal
is not evidence of a defect — and `test_qb_seat.py` has one of each, on purpose, in
a test *about* this very rule. Both are allowed by name below rather than by a
pattern, so a new one has to be argued for here rather than slipped past.

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

TESTS = Path(__file__).resolve().parent

#: `/usr/bin/env` at the START of a string literal — which covers the shebang
#: written whole (`"#!/usr/bin/env bash"`) and the two ways of assembling one
#: that a `#!`-anchored pattern would miss: `"#!" + "/usr/bin/env bash"`, and a
#: named constant (`SHEBANG = "/usr/bin/env bash"`) interpolated later. It cannot
#: catch every possible assembly — a path built character by character defeats
#: any pattern — so this is a tripwire for the forms people actually write, not
#: a proof. The nix build is still what decides.
_ENV_SHEBANG = re.compile(r"""['"](?:#!\s*)?/usr/bin/env\b""")

#: Lines allowed to carry one, each for a stated reason rather than a shape:
#: `(filename, substring that must also be on the line, how many matches it may
#: carry)`. The COUNT is the
#: point: exempting a LINE rather than an occurrence would let a second bad literal
#: be appended to an allowed line and go unread.
ALLOWED = {
    # A test whose SUBJECT is which shebangs work: it writes all three forms and
    # asserts on what each does. Rewriting it would delete the coverage.
    ("test_qb_seat.py", 'for body in (', 1),
}


def _sources() -> list[Path]:
    found = sorted(p for p in TESTS.glob("test_*.py") if p.name != Path(__file__).name)
    assert found, f"no test modules found under {TESTS} — this guard is asserting nothing"
    return found


def _allowed(path: Path, line: str) -> bool:
    """Exempt only if the line is named AND carries no more hits than it was granted."""
    for name, needle, count in ALLOWED:
        if path.name == name and needle in line:
            return len(_ENV_SHEBANG.findall(line)) <= count
    return False


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_test_writes_a_usr_bin_env_stub(path: Path):
    """A stub a test writes at runtime cannot name `/usr/bin/env` (#177)."""
    offenders = [
        f"{path.name}:{n}: {line.strip()}"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if _ENV_SHEBANG.search(line) and not _allowed(path, line)
    ]
    assert not offenders, (
        "a shebang naming /usr/bin/env appears in a string literal here, and there "
        "is no /usr/bin/env inside a nix build sandbox — a stub written at runtime "
        "cannot exec, and the suite then fails (or worse, passes) for a reason that "
        "has nothing to do with the code under test. Use `#!/bin/sh` for a POSIX "
        "body, or the absolute interpreter path the suite already resolved. If this "
        "line is a comment or a deliberate literal, add it to ALLOWED with its "
        "reason.\n  " + "\n  ".join(offenders))


def test_the_allowlist_still_matches_something():
    """An allowlist entry that matches nothing is a rule nobody is being held to.

    It would also hide the next real instance in that file the day the line it was
    written for is edited, so a stale entry is worse than an absent one.
    """
    for name, needle, count in ALLOWED:
        path = TESTS / name
        assert path.exists(), f"ALLOWED names {name}, which is gone — drop the entry"
        matched = [
            line for line in path.read_text().splitlines()
            if _ENV_SHEBANG.search(line) and needle in line
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
    # …and does not fire on the two forms that are correct.
    assert not _ENV_SHEBANG.search("""    fake.write_text('#!/bin/sh\\n' + body)""")
    assert not _ENV_SHEBANG.search('''    fake.write_text(f"#!{BASH}\\n" + body)''')

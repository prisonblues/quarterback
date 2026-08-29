"""One vocabulary, two distributions — and the second copy has to be provably identical.

`app/needs_human.py` is #279's single definition of "a human has to look at
this". The harness cannot import it as a package: `app` is the server's
distribution and `harness/loops` is installed by home-manager as
`share/quarterback-harness/loops` with no `app/` beside it at all. So
`harness/loops/needs_human.py` does both things — it loads the canonical file by
path when a checkout is in reach, which is every developer box and every CI run,
and it holds a pinned copy for the installed case.

A pinned copy nobody checks is exactly the drift `tests/test_post_type_drift.py`
exists to catch, and that file's lesson is the reason this one reads the harness
source with `ast` rather than importing it: a test that SKIPS when the import
fails is a test that never runs anywhere, which is how the drift got in.

This suite is the only place both halves are readable at once. The loops suite
runs in a nix sandbox holding `harness/loops` alone, so it can assert the two
arms of the harness module agree with each other but not that either agrees with
the app.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.needs_human import LABEL_PREFIX, MAX_REASON_CHARS, NEEDS_HUMAN_CLASSES, label_for

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS_MODULE = REPO_ROOT / "harness" / "loops" / "needs_human.py"
CANONICAL = REPO_ROOT / "app" / "needs_human.py"


@pytest.fixture(scope="module")
def harness_source() -> ast.Module:
    return ast.parse(HARNESS_MODULE.read_text(encoding="utf-8"))


def _assigned(module: ast.Module, name: str):
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"no module-level {name} in {HARNESS_MODULE}")


def test_the_harness_fallback_is_the_apps_vocabulary_exactly(harness_source):
    """The copy that runs on an installed harness, where `app/` is not there.

    Order matters as well as membership: `other` is deliberately last, because it
    is the escape hatch and the classes are rendered in vocabulary order.
    """
    pinned = _assigned(harness_source, "_FALLBACK_CLASSES")
    assert tuple(pinned) == tuple(NEEDS_HUMAN_CLASSES)


def test_the_harness_bound_on_a_reason_is_the_apps_bound(harness_source):
    """A reason the harness trims to a different length than the API accepts
    would be refused at ingest after the announcement already went out."""
    assert _assigned(harness_source, "_FALLBACK_REASON_MAX") == MAX_REASON_CHARS


def test_the_harness_points_at_the_real_file():
    """`CANONICAL` is computed from `__file__`, so a move of either file breaks
    the import arm silently — the fallback would simply take over and nothing
    would say so. This is what notices."""
    assert CANONICAL.is_file()
    text = HARNESS_MODULE.read_text(encoding="utf-8")
    assert 'parents[2] / "app" / "needs_human.py"' in text
    # …and the relationship that expression encodes is still true.
    assert HARNESS_MODULE.resolve().parents[2] == REPO_ROOT


def test_the_harness_really_imports_it_here(harness_source):
    """In a checkout the import arm is the one that runs, and it is the point.

    Loaded the same way the harness does rather than by `import`, because `app`
    is not on the harness's path and never will be — the assertion is that this
    file can be executed standalone, which is the property the whole two-arm
    design rests on.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_drift_needs_human", CANONICAL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert tuple(mod.NEEDS_HUMAN_CLASSES) == tuple(NEEDS_HUMAN_CLASSES)
    assert mod.label_for("ui") == label_for("ui") == f"{LABEL_PREFIX}/ui"


def test_the_label_shape_the_harness_falls_back_to_matches():
    """The harness renders `needs-human/<cls>` itself when the canonical module
    is out of reach; a different prefix would put an announcement's label and the
    repo's actual labels one hyphen apart."""
    text = HARNESS_MODULE.read_text(encoding="utf-8")
    assert f'return f"{LABEL_PREFIX}/{{cls}}"' in text


def test_every_class_a_producer_names_is_in_the_vocabulary():
    """The four #274 producers hardcode classes at their decision points. A
    misspelt one would leave every by-class count while still reading as a flag
    — the direction that hides the signal, which is why #279 constrains the
    value in the database as well as at the API."""
    import re

    literals: set[str] = set()
    for path, pattern in (
        ("harness/loops/preland.py", r'hold_for_human\(\s*\n?\s*"([a-z]+)"'),
        ("harness/loops/epic.py", r'^(?:UNTRIAGED|RULING)_CLASS = "([a-z]+)"'),
        # #578 made this one per REGISTRATION rather than per file: `qb-doctor`
        # sent a single class for all twenty of its rows and every escalation it
        # made said `environment`. Twenty literals is exactly the case this scan
        # is for — `CheckSpec` cannot check them itself, because the door is
        # imported at call time and there is no vocabulary in that file to check
        # against.
        ("harness/bin/qb-doctor", r'^\s*needs_human="([a-z]+)",$'),
        ("harness/bin/qb-bump", r'^NEEDS_HUMAN_CLASS = "([a-z]+)"'),
    ):
        src = (REPO_ROOT / path).read_text(encoding="utf-8")
        found = set(re.findall(pattern, src, re.M))
        assert found, f"no hardcoded needs-human class found in {path}"
        literals |= found
    assert literals <= set(NEEDS_HUMAN_CLASSES), sorted(literals - set(NEEDS_HUMAN_CLASSES))


def test_the_qb_doctor_scan_sees_every_registration():
    """`assert found` above proves the scan saw ONE literal, not all twenty. A
    registration whose class is formatted differently — no trailing comma, or
    sharing a line — is skipped silently and never checked against the
    vocabulary, which is the guard going half-blind rather than red."""
    import re

    src = (REPO_ROOT / "harness" / "bin" / "qb-doctor").read_text(encoding="utf-8")
    specs = len(re.findall(r"^    CheckSpec\(", src, re.M))
    seen = len(re.findall(r'^\s*needs_human="[a-z]+",$', src, re.M))
    assert seen == specs, f"{specs} registrations but the scan sees {seen}"


def test_the_seat_prompt_teaches_the_whole_vocabulary():
    """A class the panel seats are never shown is a class they cannot emit, and
    the counts #279 built would be short by exactly that kind of judgement."""
    envelope = (REPO_ROOT / "harness" / "loops" / "panel_core.py").read_text(
        encoding="utf-8")
    start = envelope.index("_FINDINGS_ENVELOPE = ")
    block = envelope[start:envelope.index('"""', envelope.index('"""', start) + 3)]
    for cls in NEEDS_HUMAN_CLASSES:
        assert cls in block, f"the seats are never told about {cls!r}"

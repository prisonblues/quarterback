"""`qb-admit` and `harness_rules.DEFAULTS` must agree about the bound's keys (#337).

The bound's ceiling lives in the repo's rules file, and TWO pieces of code read
that file: `harness_rules.py`, which every loop resolves policy through, and
`qb-admit`, which enforces the ceiling at the checkout. They cannot share a
module — the package installs `loops/` under `share/quarterback-harness` and
`bin/` on PATH as separate store paths, which is why `remove-worktree` already
mirrors `detect_default_branch` rather than importing it — so the duplication is
structural and the risk is the ordinary one: somebody renames the key in one
place and the other silently reads a key that is no longer written.

Silently is the operative word. `harness_rules` WARNS about a key it does not
recognise and drops it, so a rename that lands only in the sample leaves
`qb-admit` reading `None` from a repo that has configured a ceiling — a bound
believed to be on and provably off. This suite is the join the import cannot be.

Run: pytest harness/tests
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADMIT = ROOT / "harness" / "bin" / "qb-admit"

sys.path.insert(0, str(ROOT / "harness" / "loops"))
import harness_rules as hr  # noqa: E402


def admit_module():
    """`qb-admit` imported as a module — it has no extension, so `import` cannot.

    Loaded rather than parsed so the constants under test are the values the
    script actually runs on, not a regex's opinion of them.
    """
    import importlib.util
    spec = importlib.util.spec_from_loader(
        "qb_admit", importlib.machinery.SourceFileLoader("qb_admit", str(ADMIT)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_block_name_is_one_harness_rules_knows():
    """A block absent from DEFAULTS is warned about and dropped as a typo, so a
    ceiling under a name only `qb-admit` knows is a ceiling nothing enforces and
    nothing complains about."""
    mod = admit_module()
    assert mod.BLOCK in hr.DEFAULTS, (
        f"qb-admit reads `{mod.BLOCK}` and harness_rules does not know that block")


def test_the_key_names_are_the_ones_defaults_declares():
    mod = admit_module()
    block = hr.DEFAULTS[mod.BLOCK]
    assert set(block) == {mod.MAX_KEY, mod.MIN_KEY}, (
        f"DEFAULTS[{mod.BLOCK!r}] declares {sorted(block)}; qb-admit reads "
        f"{sorted((mod.MAX_KEY, mod.MIN_KEY))}")


def test_the_shipped_default_is_no_bound_at_all():
    """The property that makes #337 landable unattended: every repo that has not
    opted in behaves exactly as it did before, because the default IS off. A
    non-null here would silently bound the whole fleet on upgrade."""
    mod = admit_module()
    block = hr.DEFAULTS[mod.BLOCK]
    assert block[mod.MAX_KEY] is None, (
        "the in-flight ceiling now ships ON — that is a fleet-wide behaviour "
        "change, not a default")
    assert block[mod.MIN_KEY] is None


def test_the_block_is_merged_one_level_deep_like_its_neighbours():
    """A repo setting only `max` must keep the default `min`, and vice versa. A
    block missing from `_DEEP_BLOCKS` is REPLACED wholesale instead."""
    mod = admit_module()
    assert mod.BLOCK in hr._DEEP_BLOCKS


def test_the_files_qb_admit_reads_are_the_two_harness_rules_reads():
    """And in the same preference order: the tracked sample is policy, and an
    untracked `.harness-rules` beside it is one box's overlay, which may narrow a
    reviewer seat and nothing else. A box that could lower the fleet's ceiling
    from an untracked file is a box that can raise it."""
    mod = admit_module()
    assert mod.RULES_FILES == (hr.SAMPLE_FILENAME, hr.RULES_FILENAME)


def test_this_repos_sample_declares_the_block_at_the_default():
    """Written out rather than omitted, the convention this file follows for every
    key whose absence a reader would otherwise have to interpret."""
    mod = admit_module()
    rules = json.loads((ROOT / hr.SAMPLE_FILENAME).read_text())
    assert mod.BLOCK in rules, f"{hr.SAMPLE_FILENAME} does not declare `{mod.BLOCK}`"
    assert rules[mod.BLOCK][mod.MAX_KEY] is None
    assert rules[mod.BLOCK][mod.MIN_KEY] is None

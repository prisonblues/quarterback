"""`prune-worktrees` sweeps bytecode caches whose worktree is gone.

With `PYTHONPYCACHEPREFIX` set, Python mirrors every source path under the prefix, so each
worktree leaves a cache behind when it is removed and nothing ever removes it. On the box
that found this that was 183 dead checkouts and ~40G, while every other category read
"none". The sweep is a stanza of the real script, extracted rather than copied.

What is pinned, and why each is a way to look fixed while being wrong:

* an existing directory's cache is never an orphan — a *leftover* (de-registered but present)
  checkout is somebody's to look at, and deleting its cache first is the quiet failure.
* only `<project>` / `<project>-*` is considered, so a run in one project cannot reach
  another project's cache.
* a prefix that is not set is "nothing to sweep", not "unknown"; one that is set but cannot
  be listed IS unknown, and must not read as an empty answer (#735).

Run: pytest harness/tests
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "prune-worktrees"
BASH = shutil.which("bash")
FIND = shutil.which("find")

pytestmark = pytest.mark.skipif(
    BASH is None or FIND is None, reason="bash and find must be on PATH")


def _block() -> str:
    src = SCRIPT.read_text()
    start, end = "# >>> pycache-sweep", "# <<< pycache-sweep"
    assert start in src and end in src, (
        "the pycache-sweep markers are gone from prune-worktrees, so this suite is "
        "asserting nothing — fix the markers rather than deleting the test")
    block = src.split(start, 1)[1].split("\n", 1)[1].split(end, 1)[0]
    assert "ORPHAN_PYCACHE+=" in block, "the markers no longer bracket the sweep"
    return block


def sweep(tmp_path, *, cached, present, project="acme", prefix_set=True, make_root=True):
    """Run the real sweep. `cached` are dirs under the cache; `present` exist on disk."""
    parent = tmp_path / "src"
    parent.mkdir()
    for name in present:
        (parent / name).mkdir()
    prefix = tmp_path / "cache"
    root = Path(f"{prefix}{parent}")
    if make_root:
        root.mkdir(parents=True)
        for name in cached:
            (root / name / "mod.cpython-313.pyc").parent.mkdir(parents=True)
            (root / name / "mod.cpython-313.pyc").write_bytes(b"x")
    script = f"""
set -uo pipefail
PROJECT={project!r}
PARENT={str(parent)!r}
{f"export PYTHONPYCACHEPREFIX={str(prefix)!r}" if prefix_set else "unset PYTHONPYCACHEPREFIX"}
{_block()}
printf 'UNKNOWN=%s\\n' "$PYCACHE_UNKNOWN"
printf 'ORPHAN=%s\\n' "${{ORPHAN_PYCACHE[@]:-}}"
"""
    out = subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=True).stdout
    lines = dict(l.split("=", 1) for l in out.splitlines())
    orphans = sorted(Path(p).name for p in lines["ORPHAN"].split() if p)
    return orphans, lines["UNKNOWN"]


def test_a_cache_with_no_directory_is_an_orphan(tmp_path):
    orphans, unknown = sweep(tmp_path, cached=["acme-gone", "acme-live"], present=["acme-live"])
    assert orphans == ["acme-gone"]
    assert unknown == ""


def test_the_main_checkout_and_live_trees_are_kept(tmp_path):
    orphans, _ = sweep(tmp_path, cached=["acme", "acme-a", "acme-b"], present=["acme", "acme-a", "acme-b"])
    assert orphans == []


def test_a_present_but_deregistered_directory_keeps_its_cache(tmp_path):
    # Present on disk, not registered: that is a leftover directory, whose cache goes
    # with it on the next run — never before.
    orphans, _ = sweep(tmp_path, cached=["acme-leftover"], present=["acme-leftover"])
    assert orphans == []


def test_another_projects_cache_is_not_touched(tmp_path):
    orphans, _ = sweep(tmp_path, cached=["other-gone", "acmecorp-gone", "acme-gone"], present=[])
    assert orphans == ["acme-gone"]


def test_no_prefix_means_nothing_to_sweep_not_unknown(tmp_path):
    orphans, unknown = sweep(tmp_path, cached=["acme-gone"], present=[], prefix_set=False)
    assert (orphans, unknown) == ([], "")


def test_a_missing_cache_root_is_empty_not_unknown(tmp_path):
    orphans, unknown = sweep(tmp_path, cached=[], present=[], make_root=False)
    assert (orphans, unknown) == ([], "")


def test_an_unlistable_root_is_unknown_not_empty(tmp_path):
    import os
    if os.geteuid() == 0:
        pytest.skip("root can list anything")
    parent = tmp_path / "src"
    root = Path(f"{tmp_path / 'cache'}{parent}")
    root.mkdir(parents=True)
    (root / "acme-gone").mkdir()
    root.chmod(0o000)
    try:
        script = f"""
set -uo pipefail
PROJECT=acme
PARENT={str(parent)!r}
export PYTHONPYCACHEPREFIX={str(tmp_path / 'cache')!r}
{_block()}
printf '%s' "$PYCACHE_UNKNOWN"
"""
        out = subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=True).stdout
    finally:
        root.chmod(0o755)
    assert "could not be listed" in out

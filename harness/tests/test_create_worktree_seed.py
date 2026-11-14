"""`create-worktree`'s opt-in `seed` step (#830): copy files from the source checkout.

The block is extracted from the real script between `# >>> seed` / `# <<< seed`.

Run: pytest harness/tests
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "create-worktree"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")


def seed_block() -> str:
    src = SCRIPT.read_text()
    assert "# >>> seed" in src and "# <<< seed" in src, "seed markers are gone"
    block = src.split("# >>> seed", 1)[1].split("\n", 1)[1].split("# <<< seed", 1)[0]
    assert "SEED" in block
    return block


def run_block(tmp_path: Path, items: list[str]) -> subprocess.CompletedProcess:
    (tmp_path / "main").mkdir(exist_ok=True)
    (tmp_path / "worktree").mkdir(exist_ok=True)
    array = " ".join(f'"{i}"' for i in items)
    script = f"""
set -euo pipefail
RED=''; GREEN=''; YELLOW=''; NC=''
step() {{ :; }}
MAIN_REPO={tmp_path / "main"}
WORKTREE_DIR={tmp_path / "worktree"}
SEED=({array})
{seed_block()}
echo REACHED_END
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)


def test_file_is_copied(tmp_path):
    (tmp_path / "main").mkdir()
    (tmp_path / "main" / ".testmondata").write_bytes(b"sqlite-bytes")
    r = run_block(tmp_path, [".testmondata"])
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "worktree" / ".testmondata").read_bytes() == b"sqlite-bytes"


def test_copy_is_not_a_symlink_and_is_independent(tmp_path):
    (tmp_path / "main").mkdir()
    src = tmp_path / "main" / ".testmondata"
    src.write_bytes(b"one")
    run_block(tmp_path, [".testmondata"])
    dst = tmp_path / "worktree" / ".testmondata"
    assert not dst.is_symlink()
    dst.write_bytes(b"two")
    assert src.read_bytes() == b"one"


def test_source_symlink_is_copied_as_bytes(tmp_path):
    (tmp_path / "main").mkdir()
    (tmp_path / "real").write_bytes(b"real")
    (tmp_path / "main" / ".testmondata").symlink_to(tmp_path / "real")
    run_block(tmp_path, [".testmondata"])
    dst = tmp_path / "worktree" / ".testmondata"
    assert not dst.is_symlink() and dst.read_bytes() == b"real"


def test_nested_path_is_created(tmp_path):
    (tmp_path / "main" / "a" / "b").mkdir(parents=True)
    (tmp_path / "main" / "a" / "b" / "f").write_text("x")
    run_block(tmp_path, ["a/b/f"])
    assert (tmp_path / "worktree" / "a" / "b" / "f").read_text() == "x"


def test_absent_file_warns_in_one_line_and_continues(tmp_path):
    r = run_block(tmp_path, [".testmondata"])
    assert r.returncode == 0, r.stderr
    assert "REACHED_END" in r.stdout
    lines = [ln for ln in r.stdout.splitlines() if ".testmondata" in ln]
    assert len(lines) == 1 and "not in the source checkout" in lines[0]
    assert not (tmp_path / "worktree" / ".testmondata").exists()


def test_absent_file_does_not_stop_later_ones(tmp_path):
    (tmp_path / "main").mkdir()
    (tmp_path / "main" / "later").write_text("y")
    run_block(tmp_path, ["missing", "later"])
    assert (tmp_path / "worktree" / "later").read_text() == "y"


def test_option_absent_changes_nothing(tmp_path):
    (tmp_path / "main").mkdir()
    (tmp_path / "main" / ".testmondata").write_text("x")
    r = run_block(tmp_path, [])
    assert r.returncode == 0 and "REACHED_END" in r.stdout
    assert list((tmp_path / "worktree").iterdir()) == []


def test_seed_is_read_from_config():
    assert "cfg_array '.seed'" in SCRIPT.read_text()
